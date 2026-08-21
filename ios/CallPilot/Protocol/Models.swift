import Foundation

// 平台无关协议模型。对齐 hosted `/v1` 契约(#42/#95)与 Android
// `protocol/Models.kt` + `HostedCloudClient.kt`。纯 Foundation,不依赖
// UIKit/SwiftUI/CallKit,命令行 `swiftc -typecheck` 即可验证。

/// 配对后持有的设备凭证(写入 __Host-callpilot-device Cookie)。
struct DeviceCredential: Equatable, Codable {
    let deviceId: String
    let secret: String

    /// Cookie 值:deviceId.secret(对齐 Android DeviceCredential.asCookieValue)。
    var cookieValue: String { "\(deviceId).\(secret)" }
}

/// 一次外呼或来电接管的入房凭证。
struct HostedCallSession: Equatable, CustomStringConvertible, CustomDebugStringConvertible {
    let sessionId: String
    let livekitURL: String
    let token: String
    let expiresAt: Int64

    /// Keep one-time room credentials out of logs and crash reports.
    var description: String {
        "HostedCallSession(sessionId: \(sessionId), livekitURL: \(livekitURL), token: ***, expiresAt: \(expiresAt))"
    }

    var debugDescription: String { description }
}

/// #95 一条可接管的来电 offer;云端只暴露 opaque id、CallKit UUID 与过期时间。
struct InboundOffer: Equatable {
    let offerId: String
    let callUUID: UUID?
    let expiresAt: Int64

    init(offerId: String, callUUID: UUID? = nil, expiresAt: Int64) {
        self.offerId = offerId
        self.callUUID = callUUID
        self.expiresAt = expiresAt
    }
}

/// WIL-137 来电者上下文:谁在打、自称是谁、什么事。
/// 每一项都可能缺失——来电者什么都没说是常态,UI 有就显示、没有就退回通用文案。
/// `claimedName` 是对端**自称**、从未核实:字段名保留这个语义,展示时必须标注,
/// 把它呈现成已核实身份会把诈骗来电洗成可信来电。
/// 不经 APNs(ADR-003/005),claim 前经瞬时 relay 按需取,云端不留存。
struct TakeoverContext: Equatable {
    let peerNumber: String?
    let claimedName: String?
    let purpose: String?
    let updatedAtUnixMs: Int64

    /// 一条可用信息都没有的上下文与「没有上下文」等价,UI 不必分别处理。
    var isEmpty: Bool { peerNumber == nil && claimedName == nil && purpose == nil }
}

enum ApnsEnvironment: String, Equatable {
    case sandbox
    case production
}

/// 线路就绪状态(hosted `/api/device`)。
struct HostedDeviceStatus: Equatable {
    let connected: Bool
    let modemOnline: Bool

    /// 是否允许拨号/接管:电脑端在线且模组在线(对齐 Android connected && modemOnline)。
    var lineReady: Bool { connected && modemOnline }
}

/// 配对成功结果。
struct HostedPairResult: Equatable {
    let deviceId: String
    let edgeId: String
    let credential: DeviceCredential
}

/// 云控制面结构化错误(HTTP 非 2xx 时携带稳定 code)。
struct HostedCloudError: Error, Equatable {
    let statusCode: Int
    let code: String
    let message: String
}
