import Foundation
import Combine
import os

/// App 状态中枢:配对、线路状态轮询、来电 offer 轮询、通话状态机。
/// 对齐 Android CallManager + MainActivity 的组合职责。
/// 媒体会话(LiveKit)由 CallMediaSession 承担;本类只驱动状态与协议。
@MainActor
final class AppModel: ObservableObject {
    @Published var pairing: StoredPairing?
    @Published var callState: CallState = .idle
    /// 换来电即换上下文——统一在 didSet 里挂钩,而不是在八处赋值点各写一遍:
    /// 漏掉任何一处的表现是「新来电显示上一通的来电者」,静默且极难察觉。
    @Published var incomingOffer: InboundOffer? {
        didSet {
            guard oldValue?.offerId != incomingOffer?.offerId else { return }
            incomingContext = nil
            if let offer = incomingOffer {
                Task { _ = await callKitFetchContext(for: offer) }
            }
        }
    }
    /// 当前来电的对端上下文(WIL-137)。nil = 对端还没自报,卡片退回通用文案。
    @Published private(set) var incomingContext: TakeoverContext?
    @Published var lineStatusLabel = L10n.text("line.status.checking")
    @Published var pairingError: String?
    @Published var lineReady = false
    @Published private(set) var speakerphoneEnabled = false
    @Published private(set) var messageInbox: MessageInboxModel?
    @Published private(set) var callHistory: CallHistoryModel?
    @Published private(set) var deviceStatus: HostedDeviceStatus?
    @Published private(set) var deviceStatusSync: DeviceStatusSyncState = .idle
    @Published private(set) var deviceStatusRefreshing = false
    @Published private(set) var voipTokenRegistration: VoipTokenRegistrationState = .idle

    private let store = CredentialStore()
    private var client: HostedCloudClient?
    private var dismissedOffers = Set<String>()
    private var media: CallMediaSession?
    private var callAttempts = CallAttemptStateMachine()
    private let messageStore = FileMessageCacheStore()
    private let callHistoryStore = FileCallHistoryCacheStore()
    private var deviceStatusMachine = DeviceStatusStateMachine()
    private var voipTokenMachine = VoipTokenRegistrationMachine()
    private var contextFetches: [String: Task<TakeoverContext?, Never>] = [:]
    private let callKit: CallKitCoordinator
    // 只记错误码,绝不记 token 值。
    private static let voipLog = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "CallPilot",
        category: "voip-push"
    )

    // 接管媒体超时(对齐 Android takeoverMediaTimeoutMs;真机实证:失败会话不复位会挡后续 offer)。
    private let takeoverMediaTimeout: Duration = .seconds(20)
    private let offerPollInterval: Duration = .seconds(3)
    private let lineStatusInterval: Duration = .seconds(15)

    init(callKit: CallKitCoordinator) {
        self.callKit = callKit
        pairing = store.load()
        // 先建 client 再挂 delegate:PushKit 回调(接听/token)一到就要能干活。
        rebuildClient()
        callKit.delegate = self
    }

    private func rebuildClient() {
        resetDeviceStatus()
        guard let p = pairing else {
            client = nil
            messageInbox = nil
            callHistory = nil
            resetVoipTokenRegistration()
            return
        }
        client = try? HostedCloudClient(baseURL: p.gatewayURL).also { $0.credential = p.credential }
        if let client {
            messageInbox = MessageInboxModel(
                client: client,
                store: messageStore,
                deviceId: p.credential.deviceId,
                onUnauthorized: { [weak self] in self?.unpair() }
            )
            callHistory = CallHistoryModel(
                client: client,
                store: callHistoryStore,
                deviceId: p.credential.deviceId,
                onUnauthorized: { [weak self] in self?.unpair() }
            )
            registerCurrentVoipToken()
        } else {
            messageInbox = nil
            callHistory = nil
            resetVoipTokenRegistration()
        }
    }

    // MARK: - 配对

    func pair(code: String, gatewayURL: String, displayName: String) async {
        pairingError = nil
        do {
            let c = try HostedCloudClient(baseURL: gatewayURL)
            let result = try await c.claimPairing(code: code, displayName: displayName)
            let stored = StoredPairing(
                gatewayURL: gatewayURL, displayName: displayName,
                credential: result.credential, edgeId: result.edgeId
            )
            store.save(stored)
            pairing = stored
            rebuildClient()
        } catch let e as HostedCloudError {
            pairingError = PairingErrorCopy.message(code: e.code)
        } catch {
            pairingError = PairingErrorCopy.message(code: "TRANSPORT_ERROR")
        }
    }

    func unpair() {
        let pairedClient = client
        Task { try? await pairedClient?.unregisterVoipToken() }
        resetVoipTokenRegistration()
        messageInbox?.clearLocalData()
        callHistory?.clearLocalData()
        store.clear()
        pairing = nil
        client = nil
        messageInbox = nil
        callHistory = nil
        incomingOffer = nil
        resetDeviceStatus()
    }

    func clearLocalContent() {
        messageInbox?.clearLocalData()
        callHistory?.clearLocalData()
        try? messageStore.clear()
        try? callHistoryStore.clear()
    }

    func loadCachedContentForSettings() {
        messageInbox?.loadCachedContent()
        callHistory?.loadCachedContent()
    }

    // MARK: - 轮询(前台版:offer + 线路状态)

    func startOfferPolling() async {
        // 两条独立节奏合一:每 offerPollInterval 拉 offer,每 5 轮拉一次线路状态。
        var tick = 0
        while !Task.isCancelled {
            if let c = client {
                if let offers = try? await c.listInboundOffers() {
                    callKit.reconcile(
                        openOffers: offers,
                        nowUnixMs: Int64(Date().timeIntervalSince1970 * 1_000)
                    )
                    if callState == .idle, let offer = offers.first(where: {
                        !dismissedOffers.contains($0.offerId)
                            && $0.expiresAt > Int64(Date().timeIntervalSince1970 * 1000)
                    }) {
                        incomingOffer = offer
                    } else if callState == .idle {
                        incomingOffer = nil
                    }
                } else if let offer = incomingOffer,
                          offer.expiresAt <= Int64(Date().timeIntervalSince1970 * 1_000) {
                    incomingOffer = nil
                }
                if callState != .idle {
                    incomingOffer = nil
                }
                if tick % 5 == 0 {
                    await refreshDeviceStatus()
                    retryVoipTokenRegistrationIfNeeded()
                }
            }
            tick += 1
            try? await Task.sleep(for: offerPollInterval)
        }
    }

    func refreshDeviceStatus() async {
        guard !deviceStatusRefreshing, let currentClient = client else { return }
        let refresh = deviceStatusMachine.beginRefresh()
        publishDeviceStatus()
        deviceStatusRefreshing = true
        defer { deviceStatusRefreshing = false }
        do {
            let status = try await currentClient.deviceStatus()
            guard currentClient === client,
                  deviceStatusMachine.succeed(status, for: refresh) else { return }
        } catch {
            guard currentClient === client else { return }
            if error is CancellationError
                || (error as? URLError)?.code == .cancelled
                || Task.isCancelled {
                guard deviceStatusMachine.cancel(for: refresh) else { return }
            } else if (error as? HostedCloudError)?.code == "UNAUTHORIZED" {
                unpair()
                return
            } else {
                guard deviceStatusMachine.fail(for: refresh) else { return }
            }
        }
        publishDeviceStatus()
    }

    func dismissOffer(_ offer: InboundOffer) {
        dismissedOffers.insert(offer.offerId)
        incomingOffer = nil
        Task { _ = await callKit.requestEndIfManaged(offer) }
    }

    // MARK: - 外呼(US-2)

    func startCall(number: String) async {
        guard let c = client, let p = pairing, callState == .idle else { return }
        CallKitAudioSessionBridge.prepareForStandaloneCall()
        let attempt = beginCallAttempt(with: .preparing(label: number))
        // createSession → LiveKit 媒体 → 号码经 Dongle SIM ATD(dial 在 media_ready 后发)。
        // 具体媒体建立与 data-topic 控制由 CallMediaSession 承担。
        _ = apply(.waitingMedia(label: number), for: attempt)
        media = CallMediaSession(onState: { [weak self] st in
            _ = self?.apply(st, for: attempt)
        })
        await media?.startOutbound(client: c, edgeId: p.edgeId, number: number)
    }

    // MARK: - 来电接管(US-1 App 侧,前台版)

    func answerTakeover(_ offer: InboundOffer) async {
        incomingOffer = nil
        if await callKit.requestAnswerIfManaged(offer) { return }
        CallKitAudioSessionBridge.prepareForStandaloneCall()
        await performTakeover(offer)
    }

    private func performTakeover(_ offer: InboundOffer) async {
        guard let c = client, callState == .idle else { return }
        incomingOffer = nil
        let label = L10n.text("call.takeover.label")
        let waitingState = CallState.waitingMedia(label: label)
        let attempt = beginCallAttempt(with: waitingState)
        media = CallMediaSession(onState: { [weak self] st in
            _ = self?.apply(st, for: attempt)
        })
        // 20s 媒体超时:失败结果保持可见，等待用户显式返回拨号页。
        let timeoutTask = Task { [weak self] in
            do {
                try await Task.sleep(for: self?.takeoverMediaTimeout ?? .seconds(20))
            } catch {
                return
            }
            guard let self else { return }
            let failedState = CallState.failed(
                label: label,
                reason: L10n.text("call.takeover.timeout"),
                code: "TAKEOVER_MEDIA_TIMEOUT"
            )
            guard self.apply(failedState, for: attempt, from: waitingState) else { return }
            await self.media?.stop()
        }
        let joined = await media?.startTakeover(client: c, offerId: offer.offerId, onConnected: { [weak self] in
            timeoutTask.cancel()
            self?.callKit.markConnected(offerId: offer.offerId)
        }) ?? false
        if joined { callKit.markMediaJoined(offerId: offer.offerId) }
    }

    func dismissCallResult() {
        guard callAttempts.resetTerminal() else { return }
        callState = .idle
        media = nil
        speakerphoneEnabled = false
    }

    func hangup() {
        let activeMedia = media
        Task { [weak self] in
            guard let self else { return }
            if await self.callKit.requestEndActiveIfManaged() { return }
            await activeMedia?.hangup()
        }
    }

    func sendDTMF(_ digit: String) {
        let activeMedia = media
        Task { await activeMedia?.sendDTMF(digit) }
    }

    func setSpeakerphone(_ enabled: Bool) {
        guard callState.isActive else { return }
        speakerphoneEnabled = enabled
        media?.setSpeakerphone(enabled)
    }

    private func beginCallAttempt(with initialState: CallState) -> CallAttempt {
        let attempt = callAttempts.begin(with: initialState)
        speakerphoneEnabled = false
        callState = initialState
        return attempt
    }

    private func resetDeviceStatus() {
        deviceStatusMachine.reset()
        deviceStatusRefreshing = false
        publishDeviceStatus()
    }

    private func publishDeviceStatus() {
        deviceStatus = deviceStatusMachine.status
        deviceStatusSync = deviceStatusMachine.syncStatus
        lineReady = deviceStatusSync == .live && (deviceStatus?.lineReady ?? false)
        switch deviceStatusSync {
        case .idle, .loading:
            lineStatusLabel = L10n.text("line.status.checking")
        case .live:
            lineStatusLabel = deviceStatus?.lineReady == true
                ? L10n.text("line.status.ready")
                : (deviceStatus?.connected == false
                    ? L10n.text("line.status.edge_offline")
                    : L10n.text("line.status.sim_offline"))
        case .stale, .offline:
            lineStatusLabel = L10n.text("line.status.unavailable")
        }
    }

    @discardableResult
    private func apply(
        _ nextState: CallState,
        for attempt: CallAttempt,
        from expectedState: CallState? = nil
    ) -> Bool {
        guard callAttempts.transition(
            from: expectedState,
            to: nextState,
            for: attempt
        ) else { return false }
        callState = nextState
        if !nextState.isActive { speakerphoneEnabled = false }
        switch nextState {
        case .failed:
            callKit.finishActiveCall(reason: .failed)
        case .ended:
            callKit.finishActiveCall(reason: .remoteEnded)
        case .idle, .preparing, .waitingMedia, .dialing, .inCall:
            break
        }
        return true
    }

    private func registerCurrentVoipToken() {
        guard let token = callKit.currentToken else { return }
        registerVoipToken(token.value, environment: token.environment)
    }

    private func registerVoipToken(_ token: String, environment: ApnsEnvironment) {
        guard let currentClient = client else { return }
        let attempt = voipTokenMachine.begin()
        publishVoipTokenState()
        Task {
            do {
                try await currentClient.registerVoipToken(token, environment: environment)
                guard currentClient === client,
                      voipTokenMachine.succeed(environment, for: attempt) else { return }
            } catch {
                guard currentClient === client else { return }
                let code = (error as? HostedCloudError)?.code ?? "TRANSPORT_ERROR"
                Self.voipLog.error("voip token registration failed: \(code, privacy: .public)")
                guard voipTokenMachine.fail(code: code, for: attempt) else { return }
            }
            publishVoipTokenState()
        }
    }

    /// 对失败态重试;token 已失效则归位未注册(失败态没有可重试对象)。
    func retryVoipTokenRegistrationIfNeeded() {
        guard case .failed = voipTokenRegistration else { return }
        if callKit.currentToken == nil {
            resetVoipTokenRegistration()
        } else {
            registerCurrentVoipToken()
        }
    }

    private func resetVoipTokenRegistration() {
        voipTokenMachine.reset()
        publishVoipTokenState()
    }

    // 状态机变更与发布不许分离:漏一处镜像就是 Settings 静默显示陈旧状态。
    private func publishVoipTokenState() {
        voipTokenRegistration = voipTokenMachine.state
    }
}

extension AppModel: CallKitCoordinatorDelegate {
    var callKitCanAcceptIncomingCall: Bool {
        pairing != nil && client != nil && callState == .idle
    }

    func callKitDidUpdateToken(_ token: String, environment: ApnsEnvironment) {
        registerVoipToken(token, environment: environment)
    }

    func callKitDidInvalidateToken() {
        resetVoipTokenRegistration()
        guard let client else { return }
        Task { try? await client.unregisterVoipToken() }
    }

    func callKitDidReceiveOffer(_ offer: InboundOffer) {
        guard callKitCanAcceptIncomingCall,
              !dismissedOffers.contains(offer.offerId) else { return }
        incomingOffer = offer
    }

    func callKitFetchContext(for offer: InboundOffer) async -> TakeoverContext? {
        // 推送路径(CallKit 要更新锁屏)与前台路径(卡片要展示)会同时问同一个
        // offer:共用在途请求,别在来电的关键路径上打两次网络。
        if let inFlight = contextFetches[offer.offerId] { return await inFlight.value }
        guard let client else { return nil }
        let task = Task { () -> TakeoverContext? in
            // 取不到就当作「对端没自报」:上下文是接听决策的加分项、不是接听的前提,
            // 任何失败都不该挡住这通电话。
            try? await client.takeoverContext(offerId: offer.offerId)
        }
        contextFetches[offer.offerId] = task
        let context = await task.value
        contextFetches.removeValue(forKey: offer.offerId)
        // 只认当前这通来电的上下文,慢响应不许覆盖已经换了的来电。
        if incomingOffer?.offerId == offer.offerId { incomingContext = context }
        return context
    }

    func callKitDidRequestAnswer(_ offer: InboundOffer) {
        guard callKitCanAcceptIncomingCall else {
            callKit.finishActiveCall(reason: .failed)
            return
        }
        Task { await performTakeover(offer) }
    }

    func callKitDidRequestDecline(_ offer: InboundOffer) {
        dismissedOffers.insert(offer.offerId)
        if incomingOffer?.offerId == offer.offerId { incomingOffer = nil }
    }

    func callKitDidRequestHangup() {
        let activeMedia = media
        Task { await activeMedia?.hangup() }
    }
}

// 小工具:Kotlin `.also {}` 的 Swift 等价,链式配置。
private extension HostedCloudClient {
    func also(_ configure: (HostedCloudClient) -> Void) -> HostedCloudClient {
        configure(self); return self
    }
}
