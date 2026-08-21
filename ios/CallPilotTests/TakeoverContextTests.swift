import Foundation
import XCTest
@testable import CallPilot

final class TakeoverContextDisplayTests: XCTestCase {
    private let zh = Locale(identifier: "zh-Hans")

    private func context(
        peerNumber: String? = "+15105550123",
        claimedName: String? = "Kevin",
        purpose: String? = "约周六吃饭"
    ) -> TakeoverContext {
        TakeoverContext(
            peerNumber: peerNumber,
            claimedName: claimedName,
            purpose: purpose,
            updatedAtUnixMs: 1_787_000_000_000
        )
    }

    func testClaimedNameIsAlwaysMarkedAsSelfReported() {
        let name = TakeoverContextDisplay.callerName(context(), locale: zh)
        XCTAssertEqual(name, "自称 Kevin")
        XCTAssertNotEqual(
            name, "Kevin",
            "未核实的自报姓名不得以身份形式呈现——那是替可能的诈骗来电背书"
        )
    }

    func testEnglishCatalogAlsoMarksTheNameAsClaimed() {
        XCTAssertEqual(
            TakeoverContextDisplay.callerName(context(), locale: Locale(identifier: "en")),
            "Claims to be Kevin"
        )
    }

    func testFallsBackToNumberThenToNothing() {
        XCTAssertEqual(
            TakeoverContextDisplay.callerName(context(claimedName: nil), locale: zh),
            "+15105550123"
        )
        XCTAssertNil(TakeoverContextDisplay.callerName(
            context(peerNumber: nil, claimedName: nil, purpose: "只说了来意"),
            locale: zh
        ))
        XCTAssertNil(TakeoverContextDisplay.callerName(nil, locale: zh))
    }

    func testContextWithNoUsableFieldIsEmpty() {
        XCTAssertTrue(context(peerNumber: nil, claimedName: nil, purpose: nil).isEmpty)
        XCTAssertFalse(context(peerNumber: nil, claimedName: nil).isEmpty)
        XCTAssertFalse(context(claimedName: nil, purpose: nil).isEmpty)
    }
}

@MainActor
final class TakeoverContextClientTests: XCTestCase {
    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    func testReadsContextFromTheOfferScopedPath() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers/offer_abcdefghijkl/context")
            XCTAssertEqual(request.httpMethod, "GET")
            return Self.response(for: request, json: """
            {"context":{"v":1,"peerNumber":"+15105550123","claimedName":"Kevin",
             "purpose":"约周六吃饭","updatedAtUnixMs":1787000000000}}
            """)
        }

        let context = try await client.takeoverContext(offerId: "offer_abcdefghijkl")

        XCTAssertEqual(context?.peerNumber, "+15105550123")
        XCTAssertEqual(context?.claimedName, "Kevin")
        XCTAssertEqual(context?.purpose, "约周六吃饭")
        XCTAssertEqual(context?.updatedAtUnixMs, 1_787_000_000_000)
    }

    func testNullContextIsNotAnError() async throws {
        // 对端什么都没说是常态,必须与「取失败」区分:前者安静退回通用文案。
        let client = try makeClient { request in
            Self.response(for: request, json: #"{"context":null}"#)
        }
        let context = try await client.takeoverContext(offerId: "offer_abcdefghijkl")
        XCTAssertNil(context)
    }

    func testMalformedFieldsDegradeInsteadOfFailingTheCall() async throws {
        let client = try makeClient { request in
            Self.response(for: request, json: """
            {"context":{"v":1,"peerNumber":"+15105550123","claimedName":"\(String(repeating: "n", count: 61))",
             "purpose":"   ","updatedAtUnixMs":1787000000000}}
            """)
        }

        let context = try await client.takeoverContext(offerId: "offer_abcdefghijkl")

        XCTAssertEqual(context?.peerNumber, "+15105550123")
        XCTAssertNil(context?.claimedName, "超过 60 字符按缺失处理,与服务端同一上限")
        XCTAssertNil(context?.purpose, "只有空白等于没说")
    }

    func testEveryFieldUnusableReadsAsNoContext() async throws {
        let client = try makeClient { request in
            Self.response(for: request, json: """
            {"context":{"v":1,"peerNumber":15105550123,"claimedName":null,
             "purpose":null,"updatedAtUnixMs":1787000000000}}
            """)
        }
        let context = try await client.takeoverContext(offerId: "offer_abcdefghijkl")
        XCTAssertNil(context)
    }

    func testMissingTimestampIsRejected() async throws {
        // updatedAtUnixMs 是新鲜度比较的唯一依据,缺了就无法判断该不该覆盖。
        let client = try makeClient { request in
            Self.response(for: request, json: """
            {"context":{"v":1,"peerNumber":"+15105550123","claimedName":"Kevin","purpose":null}}
            """)
        }
        let context = try await client.takeoverContext(offerId: "offer_abcdefghijkl")
        XCTAssertNil(context)
    }

    func testRejectsAnOfferIdThatIsNotOpaqueBeforeSendingAnything() async throws {
        var sawRequest = false
        let client = try makeClient { request in
            sawRequest = true
            return Self.response(for: request, json: #"{"context":null}"#)
        }

        do {
            _ = try await client.takeoverContext(offerId: "../v1/messages")
            XCTFail("Expected a malformed offer id to be rejected")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.code, "BAD_OFFER_ID")
        }
        XCTAssertFalse(sawRequest, "格式非法的 offerId 不该发出任何请求(路径拼接是注入面)")
    }

    private func makeClient(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> HostedCloudClient {
        MockURLProtocol.requestHandler = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        let client = try HostedCloudClient(
            baseURL: "https://cloud.example.test/",
            urlSession: URLSession(configuration: configuration),
            clockMilliseconds: { 1_000 },
            sleepMilliseconds: { _ in }
        )
        client.credential = DeviceCredential(deviceId: "device_abcdefghijkl", secret: "secret-value")
        return client
    }

    nonisolated private static func response(
        for request: URLRequest,
        json: String
    ) -> (HTTPURLResponse, Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: [:]
        )!
        return (response, Data(json.utf8))
    }
}
