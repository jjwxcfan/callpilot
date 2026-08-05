"""安卓短信网关适配器：把 capcom6/android-sms-gateway 的 HTTP/webhook 协议翻译成
CallAgentService 期待的 modem 接口子集。

只覆盖短信收发；语音相关方法（接听/拨号/按键）全部是明确报错的桩——安卓从 9/10 起
把通话音频源锁进系统权限，非 root 三方 App 拿不到，这不是本模块能绕开的限制。
`on_ring` 永远不会被触发，来电路径天然不会走到；只有用户在网页上点"外呼"才会碰到
`dial()` 抛出的错误，异常会被 `call_agent.CallSession._run()` 捕获记日志，不会
让服务进程崩掉。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import socket
import threading
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlsplit

from aiohttp import web

logger = logging.getLogger(__name__)

_VOICE_UNSUPPORTED = (
    "android_sms_gateway 后端不支持语音通话，请改用真实 EC20/EG25 模组"
    "（MODEM_BACKEND=eg25）"
)

_CONNECT_TIMEOUT_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 10.0


class AndroidSmsGatewayModem:
    """Duck-types 供 ``CallAgentService(modem=...)`` 注入的 modem 接口子集。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        webhook_host: str,
        webhook_port: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._webhook_host = webhook_host
        self._webhook_port = webhook_port
        # 每次进程启动换一个随机路径/id：webhook 接收端点不做任何鉴权时，
        # 猜不出路径本身就是第一道门槛；id 用于注册/注销配对。
        self._webhook_path = f"/{secrets.token_urlsafe(24)}"
        self._webhook_id = f"callpilot-{secrets.token_hex(8)}"

        self._on_sms: Callable[[str | None, str, str], None] | None = None
        self._on_ring: Callable[[str | None], None] | None = None
        self._on_hangup: Callable[[], None] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None
        self._listener_lock = threading.Lock()

    # ---- 生命周期：被 CallAgentService._modem_supervisor 依次调用 ----

    def connect(self) -> None:
        """轻量可达性探测：只测 TCP 连通性，不假设具体健康检查路径。"""
        parts = urlsplit(self._base_url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not host:
            raise RuntimeError(f"ANDROID_SMS_GATEWAY_URL 配置无效: {self._base_url!r}")
        try:
            with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS):
                pass
        except OSError as exc:
            raise RuntimeError(f"安卓短信网关不可达: {self._base_url} ({exc})") from exc

    def initialize_for_voice(self, audio_mode: str = "uac") -> None:
        return None

    def start_listener(self) -> None:
        with self._listener_lock:
            if self._thread is None or not self._thread.is_alive():
                ready = threading.Event()
                self._thread = threading.Thread(
                    target=self._run_webhook_server,
                    args=(ready,),
                    name="android-sms-webhook",
                    daemon=True,
                )
                self._thread.start()
                if not ready.wait(timeout=_CONNECT_TIMEOUT_SECONDS):
                    raise RuntimeError("安卓短信 webhook 接收端启动超时")
        # 每次都重新注册（幂等）：supervisor 重连时可能已有一份旧注册，先清后建。
        self._deregister_webhook(quiet=True)
        self._register_webhook()

    def stop_listener(self) -> None:
        self._deregister_webhook(quiet=True)
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._runner = None

    def close(self) -> None:
        self.stop_listener()

    # ---- 回调注册（on_ring/on_hangup 永不触发，来电路径不存在）----

    def on_ring(self, callback: Callable[[str | None], None]) -> None:
        self._on_ring = callback

    def on_hangup(self, callback: Callable[[], None]) -> None:
        self._on_hangup = callback

    def on_sms(self, callback: Callable[[str | None, str, str], None]) -> None:
        self._on_sms = callback

    # ---- 短信 ----

    def send_sms(self, number: str, text: str) -> bool:
        """同步阻塞调用：调用方（web/server.py）通过 run_in_executor 跑在线程池里。"""
        body = json.dumps(
            {"textMessage": {"text": text}, "phoneNumbers": [number]}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/message",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self._basic_auth_header(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
                ok = 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            logger.warning("安卓短信网关发短信失败: status=%s", exc.code)
            return False
        except urllib.error.URLError as exc:
            logger.warning("安卓短信网关发短信失败: %s", exc)
            return False
        if ok:
            logger.info("短信已提交给安卓网关: 字符数=%d", len(text))
        return ok

    # ---- 语音桩：明确报错/安全失败，不静默、不崩进程 ----

    def answer(self) -> None:
        raise RuntimeError(_VOICE_UNSUPPORTED)

    def dial(self, number: str) -> str:
        raise RuntimeError(_VOICE_UNSUPPORTED)

    def hangup(self) -> None:
        # 多处兜底调用（如外呼未接通、on_hangup 回调）；没有真实通话可挂，no-op。
        return None

    def is_call_connected(self) -> bool:
        return False

    def send_dtmf(self, digits: str) -> bool:
        return False

    def pcm_ready(self) -> bool:
        return False

    # ---- 内部：webhook 接收端 + 网关 HTTP 客户端 ----

    def _basic_auth_header(self) -> str:
        token = base64.b64encode(
            f"{self._username}:{self._password}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"

    def _run_webhook_server(self, ready: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_webhook)
        runner = web.AppRunner(app, access_log=None)
        self._runner = runner

        async def _start() -> None:
            await runner.setup()
            site = web.TCPSite(runner, self._webhook_host, self._webhook_port)
            await site.start()

        try:
            loop.run_until_complete(_start())
        except OSError as exc:
            logger.error(
                "安卓短信 webhook 接收端监听失败: %s:%s (%s)",
                self._webhook_host, self._webhook_port, exc,
            )
            ready.set()
            loop.close()
            return
        ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(runner.cleanup())
            loop.close()

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.Response(status=400)
        if not isinstance(body, dict) or body.get("event") != "sms:received":
            return web.Response(status=204)
        payload = body.get("payload") or {}
        sender = payload.get("phoneNumber")
        text = payload.get("message", "") or ""
        sms_ts = payload.get("receivedAt", "") or ""
        callback = self._on_sms
        if callback is not None:
            callback(sender, text, sms_ts)
        return web.Response(status=200)

    def _webhook_public_url(self) -> str:
        return f"http://{self._webhook_host}:{self._webhook_port}{self._webhook_path}"

    def _register_webhook(self) -> None:
        body = json.dumps(
            {
                "id": self._webhook_id,
                "url": self._webhook_public_url(),
                "event": "sms:received",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/webhooks",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self._basic_auth_header(),
            },
        )
        try:
            urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise RuntimeError(f"注册短信 webhook 失败: {exc}") from exc
        logger.info("已向安卓短信网关注册 webhook: id=%s", self._webhook_id)

    def _deregister_webhook(self, *, quiet: bool = False) -> None:
        request = urllib.request.Request(
            f"{self._base_url}/webhooks/{self._webhook_id}",
            method="DELETE",
            headers={"Authorization": self._basic_auth_header()},
        )
        try:
            urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            if not quiet:
                logger.warning("注销安卓短信网关 webhook 失败（忽略）: %s", exc)
