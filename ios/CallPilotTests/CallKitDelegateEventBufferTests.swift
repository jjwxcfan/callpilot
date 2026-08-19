import XCTest
@testable import CallPilot

final class CallKitDelegateEventBufferTests: XCTestCase {
    private let offer = InboundOffer(
        offerId: "offer_test_0001",
        callUUID: UUID(),
        expiresAt: 1_750_000_000_000
    )

    func testDrainReplaysInAppendOrderAndEmpties() {
        var buffer = CallKitDelegateEventBuffer()
        buffer.append(.offerReceived(offer))
        buffer.append(.answerRequested(offer))
        buffer.append(.hangupRequested)

        XCTAssertEqual(
            buffer.drain(),
            [.offerReceived(offer), .answerRequested(offer), .hangupRequested]
        )
        XCTAssertTrue(buffer.events.isEmpty)
        XCTAssertEqual(buffer.drain(), [])
    }

    func testOverflowKeepsEarliestEvents() {
        var buffer = CallKitDelegateEventBuffer()
        buffer.append(.offerReceived(offer))
        for _ in 0..<(CallKitDelegateEventBuffer.limit * 2) {
            buffer.append(.hangupRequested)
        }

        let drained = buffer.drain()
        XCTAssertEqual(drained.count, CallKitDelegateEventBuffer.limit)
        XCTAssertEqual(drained.first, .offerReceived(offer), "溢出时最早的事件(来电 offer)必须保留")
    }
}
