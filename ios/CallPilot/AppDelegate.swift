import UIKit

/// PushKit 注册必须在 didFinishLaunching 内完成:App 被杀后由 VoIP push 冷启动时,
/// PKPushRegistry 与 delegate 不就位,iOS 会把该 push 判为未处理并终止进程。
///
/// AppModel 也在这里构建,且必须先于 callKit.start():后台冷启动(锁屏直接接听、
/// 不打开 App)不连接 SwiftUI 场景,RootView 可能永远不构建——若等视图层挂
/// delegate,接听回调将无人消费,通话挂死到 CallKit 超时。
@MainActor
final class AppDelegate: NSObject, UIApplicationDelegate {
    let callKit = CallKitCoordinator()
    private(set) var model: AppModel!

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        model = AppModel(callKit: callKit)
        callKit.start()
        return true
    }
}
