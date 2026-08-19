import UIKit

/// PushKit 注册必须在 didFinishLaunching 内完成:App 被杀后由 VoIP push 冷启动时,
/// PKPushRegistry 与 delegate 不就位,iOS 会把该 push 判为未处理并终止进程,反复
/// 发生后甚至停止投递。因此 coordinator 由 AppDelegate 持有,不再挂在 SwiftUI
/// 视图的 @StateObject 构造上。
@MainActor
final class AppDelegate: NSObject, UIApplicationDelegate {
    let callKit = CallKitCoordinator()

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        callKit.start()
        return true
    }
}
