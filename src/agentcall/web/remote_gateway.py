"""Least-privilege public HTTP gateway for paired Remote Web Dialer phones."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .. import config
from ..remote_pairing import (
    InvalidPairingCodeError,
    PairingCapacityError,
    RemotePairingStore,
)

STATIC_DIR = Path(__file__).parent / "static"
DEVICE_COOKIE = "__Host-callpilot-device"
_COOKIE_MAX_AGE = 180 * 24 * 60 * 60
def livekit_media_host(value: str) -> str:
    """从配置的 LiveKit 地址取出 host[:port]；任何不合法输入一律返回空串。

    与云侧 `cloud/src/csp.ts` 的 `liveKitConnectSources()` 同语义。**只认整段
    netloc**：只钉 hostname 会连带放行同主机的其它端口。

    「不合法就返回空串」是硬契约——调用方（CSP 与 PWA 白名单）都靠空串
    fail-closed。所以这里把畸形输入全部吃掉，绝不让异常漏出去变成 500，
    也绝不让 `@host` / `:badport` 这类残缺串流进 CSP。
    """
    try:
        parsed = urlsplit((value or "").strip())
        if parsed.scheme != "wss" or not parsed.hostname:
            return ""
        # username/password 为空串时是 falsey，不能只用 `or` 判断——
        # `wss://@host` 会漏过去（Codex 评审）。直接看 netloc 里有没有 userinfo。
        if "@" in parsed.netloc:
            return ""
        parsed.port  # 端口非法时抛 ValueError（如 wss://h:bad）
    except ValueError:
        # 畸形 IPv6（wss://[::1）与非法端口都走这里。
        return ""
    return parsed.netloc


def livekit_connect_sources(value: str) -> str:
    """把配置的 LiveKit 地址收敛成 CSP connect-src 片段（无效配置返回空串）。

    空串意味着 connect-src 只剩 'self'——宁可让远程拨号连不上（可见故障），
    也不要退回 `wss:` 通配（静默放行任意媒体端点）。
    """
    host = livekit_media_host(value)
    return f" https://{host} wss://{host}" if host else ""


def security_headers() -> dict[str, str]:
    """PWA 页面的安全响应头。

    connect-src 必须钉死到配置的 LiveKit host：曾经写的是 `wss:` 通配，配合
    legacy fragment 直连入口，真域名上一个恶意 fragment 就能把媒体连到任意
    WSS 端点（#41 / G-04）。fragment 入口已移除，这里是浏览器强制的第二道。
    """
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self'; connect-src 'self'"
            f"{livekit_connect_sources(config.get_str('LIVEKIT_URL'))}; "
            "media-src blob:; worker-src 'self' blob:; "
            "manifest-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'"
        ),
    }


class _AttemptLimiter:
    def __init__(self, limit: int, window_seconds: float = 300.0) -> None:
        self.limit = max(1, limit)
        self.window_seconds = window_seconds
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = 4096

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts.pop(key, deque())
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if len(attempts) >= self.limit:
            self._attempts[key] = attempts
            return False
        attempts.append(now)
        self._attempts[key] = attempts
        if len(self._attempts) > self._max_keys:
            self._attempts.popitem(last=False)
        return True


def build_remote_gateway(
    service,
    pairing_store: RemotePairingStore,
    *,
    public_url: str,
    max_pair_attempts: int = 10,
) -> web.Application:
    """Build a public app that intentionally has no general AgentCall admin routes."""

    parsed = urlsplit(public_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("REMOTE_CONTROL_URL 必须是 HTTPS URL")
    public_origin = f"{parsed.scheme}://{parsed.netloc}"
    app = web.Application(client_max_size=16 * 1024)
    app["service"] = service
    app["pairing_store"] = pairing_store
    app["public_origin"] = public_origin
    app["pair_limiter"] = _AttemptLimiter(max_pair_attempts)

    app.router.add_get("/", _page)
    app.router.add_get("/remote_dialer.html", _page)
    app.router.add_get("/remote-dialer", _page)
    app.router.add_get("/remote-dialer/", _page)
    app.router.add_get("/remote_dialer.css", _asset)
    app.router.add_get("/remote_dialer.js", _asset)
    app.router.add_get("/manifest.webmanifest", _asset)
    app.router.add_get("/remote_dialer_sw.js", _asset)
    app.router.add_get("/callpilot-192.png", _asset)
    app.router.add_get("/callpilot-512.png", _asset)
    app.router.add_get("/api/device", _device)
    app.router.add_post("/api/pair", _pair)
    app.router.add_post("/api/session", _session)
    app.router.add_post("/api/unpair", _unpair)
    return app


async def _page(_request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "remote_dialer.html", headers=security_headers())


async def _asset(request: web.Request) -> web.StreamResponse:
    filename = request.path.rsplit("/", 1)[-1]
    allowed = {
        "remote_dialer.css",
        "remote_dialer.js",
        "manifest.webmanifest",
        "remote_dialer_sw.js",
        "callpilot-192.png",
        "callpilot-512.png",
    }
    if filename not in allowed:
        raise web.HTTPNotFound()
    headers = {
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return web.FileResponse(STATIC_DIR / filename, headers=headers)


async def _device(request: web.Request) -> web.Response:
    device = _authenticated_device(request)
    service = request.app["service"]
    status = service.remote_dialer_status()
    if device is None:
        return _json(
            {
                "ok": True,
                "paired": False,
                "edge": {
                    "enabled": bool(status.get("enabled")),
                    "configured": bool(status.get("configured")),
                    # 未配对也要给 media_host：临时链接（fragment 邀请）的使用者
                    # 按定义就是未配对的，拿不到白名单就会 fail-closed，把这个
                    # 在售功能整个堵死。它非机密——同一个 host 就写在 CSP 里。
                    "media_host": str(status.get("media_host") or ""),
                },
            }
        )
    return _json(
        {
            "ok": True,
            "paired": True,
            "device": _device_payload(device),
            "edge": status,
        }
    )


async def _pair(request: web.Request) -> web.Response:
    if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
        return _error("远程网页拨号未启用", 403)
    if not _same_origin(request):
        return _error("请求来源不受信任", 403)
    limiter: _AttemptLimiter = request.app["pair_limiter"]
    peer = _client_key(request)
    if not limiter.allow(peer):
        return _error("配对尝试过于频繁，请稍后再试", 429)
    data = await _read_json(request)
    if data is None:
        return _error("请求体不是合法 JSON", 400)
    code = data.get("code")
    display_name = data.get("display_name")
    if not isinstance(code, str) or not isinstance(display_name, str):
        return _error("配对码和设备名称不能为空", 400)
    store: RemotePairingStore = request.app["pairing_store"]
    try:
        credential = store.pair(code, display_name)
    except InvalidPairingCodeError:
        return _error("配对码无效或已过期", 401)
    except PairingCapacityError as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)

    response = _json(
        {"ok": True, "paired": True, "device": _device_payload(credential.device)}
    )
    response.set_cookie(
        DEVICE_COOKIE,
        f"{credential.device.device_id}.{credential.secret}",
        max_age=_COOKIE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


async def _session(request: web.Request) -> web.Response:
    if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
        return _error("远程网页拨号未启用", 403)
    if not _same_origin(request):
        return _error("请求来源不受信任", 403)
    if await _read_json(request) is None:
        return _error("请求体不是合法 JSON", 400)
    device = _authenticated_device(request)
    if device is None:
        return _error("设备未配对或已撤销", 401)
    service = request.app["service"]
    loop = asyncio.get_running_loop()
    invite, error = await loop.run_in_executor(
        None, service.create_paired_remote_dialer_invite, device.device_id
    )
    if invite is None:
        return _error(error or "无法创建远程拨号会话", 409)
    return _json({"ok": True, "invite": invite})


async def _unpair(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _error("请求来源不受信任", 403)
    if await _read_json(request) is None:
        return _error("请求体不是合法 JSON", 400)
    credential = _cookie_parts(request)
    store: RemotePairingStore = request.app["pairing_store"]
    if credential is not None:
        device_id, secret = credential
        if store.authenticate(device_id, secret) is not None:
            store.revoke(device_id)
    response = _json({"ok": True, "paired": False})
    response.del_cookie(DEVICE_COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
    return response


def _authenticated_device(request: web.Request):
    credential = _cookie_parts(request)
    if credential is None:
        return None
    device_id, secret = credential
    store: RemotePairingStore = request.app["pairing_store"]
    return store.authenticate(device_id, secret)


def _cookie_parts(request: web.Request) -> tuple[str, str] | None:
    value = request.cookies.get(DEVICE_COOKIE, "")
    if "." not in value or len(value) > 256:
        return None
    device_id, secret = value.split(".", 1)
    return device_id, secret


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin", "")
    expected = request.app["public_origin"]
    return bool(origin) and secrets.compare_digest(origin, expected)


def _client_key(request: web.Request) -> str:
    peer = request.remote or "unknown"
    try:
        is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_loopback = False
    if is_loopback:
        forwarded = request.headers.get("CF-Connecting-IP", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


async def _read_json(request: web.Request) -> dict | None:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _device_payload(device) -> dict:
    return {
        "device_id": device.device_id,
        "display_name": device.display_name,
        "created_at": device.created_at,
        "last_used_at": device.last_used_at,
    }


def _json(payload: dict, *, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


def _error(message: str, status: int) -> web.Response:
    return _json({"ok": False, "error": message}, status=status)


__all__ = ["DEVICE_COOKIE", "build_remote_gateway"]
