"""AndroidSmsGatewayModem 适配器单测：不起真实网络监听，只测协议翻译逻辑。"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import urllib.error
import urllib.request

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentcall.modem_android_sms import _VOICE_UNSUPPORTED, AndroidSmsGatewayModem


def make_adapter() -> AndroidSmsGatewayModem:
    return AndroidSmsGatewayModem(
        base_url="http://192.168.1.23:8080",
        username="user",
        password="pass",
        webhook_host="192.168.1.50",
        webhook_port=47101,
    )


class _FakeHttpResponse:
    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


# ---- send_sms ----


def test_send_sms_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHttpResponse(status=200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = make_adapter()

    ok = adapter.send_sms("+8613800000000", "hello")

    assert ok is True
    assert captured["url"] == "http://192.168.1.23:8080/message"
    assert captured["method"] == "POST"
    assert captured["body"] == {
        "textMessage": {"text": "hello"},
        "phoneNumbers": ["+8613800000000"],
    }
    assert captured["headers"]["Authorization"].startswith("Basic ")


def test_send_sms_http_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", None, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = make_adapter()

    assert adapter.send_sms("+8613800000000", "hello") is False


def test_send_sms_network_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = make_adapter()

    assert adapter.send_sms("+8613800000000", "hello") is False


# ---- connect() 可达性探测 ----


def test_connect_raises_when_gateway_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_create_connection(addr, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    adapter = make_adapter()

    with pytest.raises(RuntimeError, match="不可达"):
        adapter.connect()


def test_connect_succeeds_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: _FakeSocket())
    adapter = make_adapter()

    adapter.connect()  # 不抛即通过


def test_connect_rejects_invalid_url() -> None:
    adapter = AndroidSmsGatewayModem(
        base_url="not-a-url",
        username="u",
        password="p",
        webhook_host="192.168.1.50",
        webhook_port=47101,
    )
    with pytest.raises(RuntimeError):
        adapter.connect()


# ---- webhook 接收：sms:received -> on_sms 回调 ----


def _api(app: web.Application, fn):
    async def runner():
        async with TestClient(TestServer(app), cookie_jar=None) as client:
            return await fn(client)

    return asyncio.run(runner())


def _build_webhook_app(adapter: AndroidSmsGatewayModem) -> web.Application:
    app = web.Application()
    app.router.add_post(adapter._webhook_path, adapter._handle_webhook)
    return app


def test_webhook_sms_received_triggers_on_sms_callback() -> None:
    adapter = make_adapter()
    received: list[tuple] = []
    adapter.on_sms(lambda sender, text, ts: received.append((sender, text, ts)))
    app = _build_webhook_app(adapter)

    async def fn(client: TestClient) -> None:
        resp = await client.post(
            adapter._webhook_path,
            json={
                "event": "sms:received",
                "payload": {
                    "phoneNumber": "+8613800000000",
                    "message": "你好",
                    "receivedAt": "2026-08-05T12:00:00Z",
                },
            },
        )
        assert resp.status == 200

    _api(app, fn)

    assert received == [("+8613800000000", "你好", "2026-08-05T12:00:00Z")]


def test_webhook_ignores_non_sms_events() -> None:
    adapter = make_adapter()
    received: list[tuple] = []
    adapter.on_sms(lambda sender, text, ts: received.append((sender, text, ts)))
    app = _build_webhook_app(adapter)

    async def fn(client: TestClient) -> None:
        resp = await client.post(
            adapter._webhook_path,
            json={"event": "sms:sent", "payload": {}},
        )
        assert resp.status == 204

    _api(app, fn)
    assert received == []


def test_webhook_malformed_json_returns_400() -> None:
    adapter = make_adapter()
    app = _build_webhook_app(adapter)

    async def fn(client: TestClient) -> None:
        resp = await client.post(
            adapter._webhook_path,
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    _api(app, fn)


def test_webhook_before_on_sms_registered_does_not_raise() -> None:
    adapter = make_adapter()
    app = _build_webhook_app(adapter)

    async def fn(client: TestClient) -> None:
        resp = await client.post(
            adapter._webhook_path,
            json={
                "event": "sms:received",
                "payload": {"phoneNumber": "+861234", "message": "hi"},
            },
        )
        assert resp.status == 200

    _api(app, fn)  # 没注册回调时静默忽略，不抛异常


# ---- 语音桩：安全失败，不静默、不崩进程 ----


def test_answer_and_dial_raise_clear_unsupported_error() -> None:
    adapter = make_adapter()
    with pytest.raises(RuntimeError, match=_VOICE_UNSUPPORTED):
        adapter.answer()
    with pytest.raises(RuntimeError, match=_VOICE_UNSUPPORTED):
        adapter.dial("+8613800000000")


def test_voice_query_methods_fail_safely_without_raising() -> None:
    adapter = make_adapter()
    assert adapter.is_call_connected() is False
    assert adapter.send_dtmf("123") is False
    assert adapter.pcm_ready() is False
    adapter.hangup()  # no-op，不抛
    adapter.initialize_for_voice("uac")  # no-op，不抛
