import XCTest
@testable import CallPilot

final class VoipTokenRegistrationTests: XCTestCase {
    func testBeginThenSucceedReachesRegistered() {
        var machine = VoipTokenRegistrationMachine()
        XCTAssertEqual(machine.state, .idle)

        let attempt = machine.begin()
        XCTAssertEqual(machine.state, .registering)
        XCTAssertTrue(machine.succeed(.sandbox, for: attempt))
        XCTAssertEqual(machine.state, .registered(.sandbox))
    }

    func testBeginThenFailSurfacesErrorCode() {
        var machine = VoipTokenRegistrationMachine()
        let attempt = machine.begin()
        XCTAssertTrue(machine.fail(code: "FEATURE_DISABLED", for: attempt))
        XCTAssertEqual(machine.state, .failed(code: "FEATURE_DISABLED"))
    }

    func testStaleAttemptResultIsIgnored() {
        var machine = VoipTokenRegistrationMachine()
        let first = machine.begin()
        let second = machine.begin()

        XCTAssertFalse(machine.fail(code: "TRANSPORT_ERROR", for: first), "旧尝试的结果不得覆盖新尝试")
        XCTAssertEqual(machine.state, .registering)
        XCTAssertTrue(machine.succeed(.production, for: second))
        XCTAssertEqual(machine.state, .registered(.production))
    }

    func testResetReturnsToIdleAndInvalidatesInFlightAttempt() {
        var machine = VoipTokenRegistrationMachine()
        let attempt = machine.begin()
        machine.reset()

        XCTAssertEqual(machine.state, .idle)
        XCTAssertFalse(machine.succeed(.sandbox, for: attempt))
        XCTAssertEqual(machine.state, .idle)
    }
}
