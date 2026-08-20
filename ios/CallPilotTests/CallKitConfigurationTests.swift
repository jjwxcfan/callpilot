import Foundation
import XCTest

final class CallKitConfigurationTests: XCTestCase {
    func testVoipBackgroundModeAndPushEntitlementAreDeclared() throws {
        let info = try plist("ios/CallPilot/Info.plist")
        XCTAssertEqual(info["UIBackgroundModes"] as? [String], ["voip"])

        let entitlements = try plist("ios/CallPilot/CallPilot.entitlements")
        XCTAssertEqual(entitlements["aps-environment"] as? String, "development")

        let project = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/project.yml"),
            encoding: .utf8
        )
        XCTAssertTrue(project.contains("CODE_SIGN_ENTITLEMENTS: CallPilot/CallPilot.entitlements"))
        XCTAssertFalse(project.contains("APS_ENVIRONMENT:"))
    }

    func testVoipPushReportsToCallKitWithoutAnAsynchronousActorHop() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent(
                "ios/CallPilot/Call/CallKitCoordinator.swift"
            ),
            encoding: .utf8
        )
        let callback = try XCTUnwrap(
            source.components(separatedBy: "didReceiveIncomingPushWith payload").last?
                .components(separatedBy: "extension CallKitCoordinator: CXProviderDelegate").first
        )

        XCTAssertTrue(callback.contains("MainActor.assumeIsolated"))
        XCTAssertFalse(callback.contains("Task { @MainActor"))
        XCTAssertTrue(callback.contains("self.report(decoded, completion: completionBox)"))
    }

    func testApnsEnvironmentComesFromRuntimeDetectionNotBuildConfiguration() throws {
        let source = try source("ios/CallPilot/Call/CallKitCoordinator.swift")
        XCTAssertTrue(source.contains("ApnsEnvironmentDetector.detect()"))

        // 只约束 token 上报路径:签名 profile 才是 token 归属的真相源,这里不许
        // 回到 #if DEBUG 猜测。文件其他位置的编译条件不在本契约范围内。
        let tokenHandler = try XCTUnwrap(
            source.components(separatedBy: "didUpdate pushCredentials").last?
                .components(separatedBy: "didInvalidatePushTokenFor").first
        )
        XCTAssertFalse(tokenHandler.contains("#if DEBUG"))
        XCTAssertTrue(tokenHandler.contains("self.apnsEnvironment"))
    }

    func testPushKitStartsAtLaunchWithDelegateAlreadyAttached() throws {
        let app = try source("ios/CallPilot/CallPilotApp.swift")
        XCTAssertTrue(app.contains("@UIApplicationDelegateAdaptor(AppDelegate.self)"))

        let delegate = try source("ios/CallPilot/AppDelegate.swift")
        let launch = try XCTUnwrap(
            delegate.components(separatedBy: "didFinishLaunchingWithOptions").last
        )
        let modelRange = try XCTUnwrap(launch.range(of: "AppModel(callKit:"))
        let startRange = try XCTUnwrap(launch.range(of: "callKit.start()"))
        XCTAssertTrue(
            modelRange.lowerBound < startRange.lowerBound,
            "AppModel(即 delegate)必须先于 PushKit start():后台冷启动不连接场景,延迟挂 delegate 会丢接听回调"
        )

        let model = try source("ios/CallPilot/AppModel.swift")
        XCTAssertFalse(model.contains("callKit.start()"))
        XCTAssertFalse(model.contains("CallKitCoordinator()"))
        XCTAssertTrue(model.contains("callKit.delegate = self"))
    }

    func testIncomingPushWindowConfiguresPlayAndRecordCategory() throws {
        // LiveKit CallKit 实测契约:类别必须在 push 回调窗口(prepareForIncoming)
        // 内设好,否则麦克风引擎无法初始化,锁屏接听双向无声。
        let source = try source("ios/CallPilot/Call/CallKitCoordinator.swift")
        let prepare = try XCTUnwrap(
            source.components(separatedBy: "static func prepareForIncoming").last?
                .components(separatedBy: "static func didActivate").first
        )
        XCTAssertTrue(prepare.contains(".playAndRecord"))
    }

    private func source(_ path: String) throws -> String {
        try String(
            contentsOf: repositoryRoot.appendingPathComponent(path),
            encoding: .utf8
        )
    }

    private func plist(_ path: String) throws -> [String: Any] {
        try XCTUnwrap(
            PropertyListSerialization.propertyList(
                from: Data(contentsOf: repositoryRoot.appendingPathComponent(path)),
                options: [],
                format: nil
            ) as? [String: Any]
        )
    }

    private var repositoryRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }
}
