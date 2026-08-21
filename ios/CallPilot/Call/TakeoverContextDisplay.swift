import Foundation

/// 来电者上下文的展示规则(WIL-137)。纯函数,与 CallKit / SwiftUI 无关,便于单测。
///
/// 贯穿始终的一条:`claimedName` 是对端**自称**、未经核实。任何展示都必须带上
/// 「自称」这类限定词——把未核实的自报姓名显示成身份,等于用系统级来电界面为
/// 诈骗来电背书,比不显示更糟。
enum TakeoverContextDisplay {
    /// 锁屏 CallKit 来电界面的单行标题。CallKit 只给一行,按信息价值取:
    /// 自称姓名 > 号码 > 通用文案(交给调用方兜底)。来意不进这一行——挤掉姓名或
    /// 被系统截断都比放在 App 内卡片上差。
    static func callerName(_ context: TakeoverContext?, locale: Locale? = nil) -> String? {
        guard let context else { return nil }
        if let name = context.claimedName {
            return String(
                format: L10n.text("callkit.incoming.claimed_name", locale: locale),
                locale: .current,
                name
            )
        }
        return context.peerNumber
    }
}
