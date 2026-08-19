import XCTest
@testable import CallPilot

final class ApnsEnvironmentDetectorTests: XCTestCase {
    func testDevelopmentProfileResolvesToSandbox() {
        XCTAssertEqual(
            ApnsEnvironmentDetector.environment(fromProfileData: profile(apsEnvironment: "development")),
            .sandbox
        )
    }

    func testProductionProfileResolvesToProduction() {
        XCTAssertEqual(
            ApnsEnvironmentDetector.environment(fromProfileData: profile(apsEnvironment: "production")),
            .production
        )
    }

    func testWhitespaceBetweenKeyAndValueStillMatches() {
        let plist = "<key>aps-environment</key>\n\t  <string>production</string>"
        XCTAssertEqual(
            ApnsEnvironmentDetector.environment(fromProfileData: wrapped(plist)),
            .production
        )
    }

    func testProfileWithoutApsEnvironmentIsNil() {
        let plist = "<key>application-identifier</key><string>TEAM.ai.bondings.callpilot</string>"
        XCTAssertNil(ApnsEnvironmentDetector.environment(fromProfileData: wrapped(plist)))
    }

    func testUnknownEnvironmentValueIsNil() {
        let plist = "<key>aps-environment</key><string>staging</string>"
        XCTAssertNil(ApnsEnvironmentDetector.environment(fromProfileData: wrapped(plist)))
    }

    func testGarbageDataIsNil() {
        XCTAssertNil(ApnsEnvironmentDetector.environment(fromProfileData: Data([0x30, 0x82, 0xff, 0x00, 0x01])))
    }

    // mobileprovision 实际是 CMS(DER)包裹的 plist——用二进制噪声夹住 XML 模拟。
    private func profile(apsEnvironment: String) -> Data {
        wrapped("<key>aps-environment</key><string>\(apsEnvironment)</string>")
    }

    private func wrapped(_ plistFragment: String) -> Data {
        var data = Data([0x30, 0x82, 0x05, 0x00, 0xa0, 0xff])
        data.append(Data("<?xml version=\"1.0\"?><plist><dict>\(plistFragment)</dict></plist>".utf8))
        data.append(Data([0x00, 0xde, 0xad, 0xbe, 0xef]))
        return data
    }
}
