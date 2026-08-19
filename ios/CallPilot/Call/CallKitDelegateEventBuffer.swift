import Foundation

/// CallKitCoordinator 面向 delegate 的四类回调。delegate 尚未挂接时(冷启动:被杀的
/// App 由 VoIP push 拉起,PushKit 上报早于 SwiftUI 场景构建)以值形式缓存,attach 后
/// 按序重放。
enum CallKitDelegateEvent: Equatable {
    case offerReceived(InboundOffer)
    case answerRequested(InboundOffer)
    case declineRequested(InboundOffer)
    case hangupRequested
}

struct CallKitDelegateEventBuffer {
    static let limit = 8
    private(set) var events: [CallKitDelegateEvent] = []

    /// 超限拒收新事件:空窗只有场景构建的亚秒级窗口,溢出意味着异常风暴,保留最早
    /// 的事件(来电 offer 通常最先到)比保留最新的更可能让通话成立。
    mutating func append(_ event: CallKitDelegateEvent) {
        guard events.count < Self.limit else { return }
        events.append(event)
    }

    mutating func drain() -> [CallKitDelegateEvent] {
        let drained = events
        events = []
        return drained
    }
}
