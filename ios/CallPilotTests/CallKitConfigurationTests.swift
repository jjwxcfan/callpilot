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
        XCTAssertFalse(
            source.contains("#if DEBUG"),
            "环境判定不得回到编译配置猜测——签名 profile 才是 token 归属的真相源"
        )
        XCTAssertTrue(source.contains("ApnsEnvironmentDetector.detect()"))
    }

    func testPushRegistryIsCreatedInAppDelegateLaunch() throws {
        let app = try source("ios/CallPilot/CallPilotApp.swift")
        XCTAssertTrue(app.contains("@UIApplicationDelegateAdaptor(AppDelegate.self)"))

        let delegate = try source("ios/CallPilot/AppDelegate.swift")
        let launch = try XCTUnwrap(
            delegate.components(separatedBy: "didFinishLaunchingWithOptions").last
        )
        XCTAssertTrue(
            launch.contains("callKit.start()"),
            "PushKit 注册必须发生在 didFinishLaunching 内(冷启动 VoIP push 契约)"
        )

        let model = try source("ios/CallPilot/AppModel.swift")
        XCTAssertFalse(model.contains("callKit.start()"))
        XCTAssertFalse(model.contains("CallKitCoordinator()"))
        XCTAssertTrue(model.contains("callKit.attach(delegate:"))
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
