import Foundation

/// VoIP push token 向控制面注册的结果状态。此前注册失败被 `try?` 静默吞掉,
/// Worker 未开 VOIP_PUSH_ENABLED 或 Origin 不符时(403)毫无征兆——#118 的
/// 排障盲区之一。
enum VoipTokenRegistrationState: Equatable {
    case idle                      // 未配对 / 尚无 token
    case registering
    case registered(ApnsEnvironment)
    case failed(code: String)      // HostedCloudError.code 或 TRANSPORT_ERROR
}

struct VoipTokenRegistrationAttempt: Equatable {
    fileprivate let generation: Int
}

/// generation 防陈旧,仿 DeviceStatusStateMachine:后发起的注册使先前在途结果失效。
struct VoipTokenRegistrationMachine {
    private(set) var state: VoipTokenRegistrationState = .idle
    private var generation = 0

    mutating func begin() -> VoipTokenRegistrationAttempt {
        generation += 1
        state = .registering
        return VoipTokenRegistrationAttempt(generation: generation)
    }

    @discardableResult
    mutating func succeed(
        _ environment: ApnsEnvironment,
        for attempt: VoipTokenRegistrationAttempt
    ) -> Bool {
        guard attempt.generation == generation else { return false }
        state = .registered(environment)
        return true
    }

    @discardableResult
    mutating func fail(code: String, for attempt: VoipTokenRegistrationAttempt) -> Bool {
        guard attempt.generation == generation else { return false }
        state = .failed(code: code)
        return true
    }

    mutating func reset() {
        generation += 1
        state = .idle
    }
}
