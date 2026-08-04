"""网关必须记请求日志，且必须脱敏。

回归 #98 / WIL-84：本机远程网关（`AppRunner(..., access_log=None)`）**不记录任何
请求日志**。2026-08-04 实测一次**成功**的手机配对 + 拨号 10086 + 双向通话，日志里
一条 `/api/device`、`/api/pair`、`/api/session` 都没有。

后果不只是「不方便」：WIL-64 第 4 项把「Edge 日志无 session.start」写成排障佐证，
而那个判据**根本不成立** —— 请求数恒为 0，与手机侧发生了什么无关。验收时据此报过
两次结论，都是错的。

远程网关是手机侧入口：用户在手机上、日志在电脑上，本就隔一层。再没有请求日志，
就只剩「用户描述现象」这一个信息源。

但也不能简单打开 aiohttp 默认日志 —— 那会记完整请求行、完整来源地址与
User-Agent，而这是个公网可达、处理配对凭证的网关。所以本文件同时钉住两件事：
**要记**，以及**记什么绝对不能出现**。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from agentcall.remote_pairing import RemotePairingStore
from agentcall.web.remote_gateway import _client_prefix, build_remote_gateway

PAIR_CODE = "SENTINEL-PAIRCODE-9Z"


class FakeService:
    def remote_dialer_status(self) -> dict:
        return {"enabled": True, "configured": True, "media_host": "m.example.com"}


def _api(tmp_path, fn):
    """与 test_remote_gateway.py 同一套跑法。"""
    store = RemotePairingStore(tmp_path / "pairing.json")
    app = build_remote_gateway(
        FakeService(), store, public_url="https://dial.example.com"
    )

    async def runner():
        async with TestClient(TestServer(app), cookie_jar=None) as client:
            return await fn(client)

    return asyncio.run(runner())


def gateway_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "网关" in r.getMessage()]


def test_requests_are_logged(tmp_path, caplog):
    """核心：网关必须留下请求痕迹，否则手机侧排障无从下手。"""
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get("/api/device"))
    logged = gateway_lines(caplog)
    assert logged, "网关请求没有留下任何日志"
    assert "/api/device" in logged[-1] and "GET" in logged[-1]


def test_status_and_duration_are_recorded(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get("/api/device"))
    line = gateway_lines(caplog)[-1]
    assert "-> 200" in line and "ms" in line


def test_failures_are_logged_too(tmp_path, caplog):
    """4xx/5xx 更需要留痕 —— 排障时找的就是它们。"""
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get("/api/nonexistent"))
    lines = gateway_lines(caplog)
    assert lines and "-> 404" in lines[-1]


# ---- 脱敏：以下内容绝不能出现在日志里 ----


def test_pairing_code_never_reaches_the_log(tmp_path, caplog):
    """配对码在请求体里。记 body 等于把凭证写进日志。"""
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.post(
            "/api/pair",
            json={"code": PAIR_CODE, "display_name": "我的手机"},
            headers={"Origin": "https://dial.example.com"},
        ))
    everything = " ".join(r.getMessage() for r in caplog.records)
    assert PAIR_CODE not in everything, "配对码泄漏进日志"


def test_device_cookie_never_reaches_the_log(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get(
            "/api/device",
            headers={"Cookie": "__Host-callpilot-device=dev123.SENTINEL_SECRET"},
        ))
    everything = " ".join(r.getMessage() for r in caplog.records)
    assert "SENTINEL_SECRET" not in everything, "设备凭证泄漏进日志"


def test_user_agent_is_not_logged(tmp_path, caplog):
    """UA 是设备指纹面，且对排障几无帮助。"""
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get(
            "/api/device", headers={"User-Agent": "SENTINEL_UA/1.0"}))
    everything = " ".join(r.getMessage() for r in caplog.records)
    assert "SENTINEL_UA" not in everything


def test_query_string_is_not_logged(tmp_path, caplog):
    """当前端点不用 query，但日后有人加了也不该被记进去。"""
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get("/api/device?token=SENTINEL_TOKEN"))
    everything = " ".join(r.getMessage() for r in caplog.records)
    assert "SENTINEL_TOKEN" not in everything


# ---- 客户端地址只记网段 ----


def test_ipv4_is_truncated_to_a_network_prefix():
    class Req:
        remote = "198.51.100.77"
        headers: dict = {}

    assert _client_prefix(Req()) == "198.51.100.x"  # type: ignore[arg-type]


def test_ipv6_compressed_forms_are_truncated_correctly():
    """Codex 评审 P2：按原串切 ":" 在压缩写法下会切出畸形结果。

    实测旧实现：
        2001:db8::1  ->  2001:db8:::x     （畸形）
        ::1          ->  ::1::x           （**整个回环地址还在里面**）
    """
    cases = {
        "2001:db8:1234:5678::1": "2001:0db8:1234::x",
        "2001:db8::1": "2001:0db8:0000::x",
        "::1": "0000:0000:0000::x",
    }
    for raw, expected in cases.items():
        class Req:
            remote = raw
            headers: dict = {}

        got = _client_prefix(Req())  # type: ignore[arg-type]
        assert got == expected, f"{raw} -> {got}，应为 {expected}"
        assert not got.endswith(":1::x"), "完整地址不得残留"


def test_forwarded_ipv6_is_also_truncated():
    """_client_prefix 依赖 _client_key，环回下会取 CF-Connecting-IP。"""
    class Req:
        remote = "127.0.0.1"
        headers = {"CF-Connecting-IP": "2001:db8:abcd:ef01::9"}

    assert _client_prefix(Req()) == "2001:0db8:abcd::x"  # type: ignore[arg-type]


def test_unparseable_address_degrades_safely():
    class Req:
        remote = "not-an-ip"
        headers: dict = {}

    assert _client_prefix(Req()) == "unknown"  # type: ignore[arg-type]


def test_loopback_is_also_truncated():
    """环回也不例外：完整 IP 一律不落盘。"""
    class Req:
        remote = "127.0.0.1"
        headers: dict = {}

    assert _client_prefix(Req()) == "127.0.0.x"  # type: ignore[arg-type]


def test_app_py_keeps_the_default_logger_disabled():
    """默认 aiohttp 日志必须保持关闭 —— 它会记完整请求行/地址/UA。

    Codex 评审 P2：只查字串 "access_log=None" 太弱，注释里出现或挂到别的
    runner 上都能蒙混过关。改为绑定到 remote_app 的那次构造。
    """
    source = Path("app.py").read_text(encoding="utf-8")
    assert "web.AppRunner(remote_app, access_log=None)" in source, (
        "远程网关必须保持默认访问日志关闭"
    )


def test_secret_pasted_into_a_path_is_not_logged(tmp_path, caplog):
    """Codex 评审 P1：原始路径是调用方可控文本，凭证贴进 URL 会被原样记下。"""
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get(f"/api/pair/{PAIR_CODE}"))
    everything = " ".join(r.getMessage() for r in caplog.records)
    assert PAIR_CODE not in everything, "贴进路径的凭证泄漏进日志"
    assert "<unmatched>" in everything, "未匹配路由应记成 <unmatched>"


def test_matched_routes_log_the_route_template(tmp_path, caplog):
    """匹配到的路由仍要能看出是哪个端点，否则日志就没用了。"""
    with caplog.at_level(logging.INFO, logger="agentcall.web.remote_gateway"):
        _api(tmp_path, lambda c: c.get("/api/device"))
    assert "/api/device" in gateway_lines(caplog)[-1]
