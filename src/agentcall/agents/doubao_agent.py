"""豆包端到端实时语音 Agent（火山引擎 Realtime Dialogue）。"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct
import uuid
from typing import Any, Callable

import websockets

from .base import VoiceAgent

logger = logging.getLogger(__name__)

WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"

# 协议常量（参考火山引擎 Realtime Dialogue 二进制协议）
PROTOCOL_VERSION = 0x1
HEADER_SIZE = 0x1
MSG_TYPE_FULL_CLIENT = 0x1
MSG_TYPE_AUDIO_ONLY = 0x2
MSG_TYPE_FULL_SERVER = 0x9
MSG_TYPE_AUDIO_SERVER = 0xB
MSG_SERIAL_JSON = 0x1
MSG_SERIAL_RAW = 0x0
MSG_COMPRESS_GZIP = 0x1
MSG_COMPRESS_NONE = 0x0


def _build_header(
    msg_type: int,
    serial: int,
    compress: int,
) -> bytes:
    byte0 = (PROTOCOL_VERSION << 4) | HEADER_SIZE
    byte1 = (msg_type << 4) | serial
    byte2 = (compress << 4) | 0x0
    byte3 = 0x0
    return bytes([byte0, byte1, byte2, byte3])


def _pack_json_event(event: dict) -> bytes:
    payload = gzip.compress(json.dumps(event, ensure_ascii=False).encode("utf-8"))
    header = _build_header(MSG_TYPE_FULL_CLIENT, MSG_SERIAL_JSON, MSG_COMPRESS_GZIP)
    return header + struct.pack(">I", len(payload)) + payload


def _pack_audio_payload(pcm: bytes) -> bytes:
    header = _build_header(MSG_TYPE_AUDIO_ONLY, MSG_SERIAL_RAW, MSG_COMPRESS_NONE)
    return header + struct.pack(">I", len(pcm)) + pcm


def _unpack_server_message(
    data: bytes,
) -> tuple[str | None, bytes | None, dict[str, Any] | None]:
    if len(data) < 8:
        return None, None, None
    msg_type = (data[1] >> 4) & 0xF
    serial = data[1] & 0xF
    compress = (data[2] >> 4) & 0xF
    payload_size = struct.unpack(">I", data[4:8])[0]
    payload = data[8 : 8 + payload_size]
    if compress == MSG_COMPRESS_GZIP and payload:
        payload = gzip.decompress(payload)

    if msg_type in (MSG_TYPE_FULL_SERVER,) and serial == MSG_SERIAL_JSON:
        try:
            event = json.loads(payload.decode("utf-8"))
            # 连同整个事件一起返回：原实现只取事件名就把负载丢了（#69），
            # 转写即便在里面也拿不到。豆包的 ASR 事件字段名尚未确认，故这里
            # 不猜字段、只把原始事件交给上层，由 _recv_loop 负责一次性打样。
            return event.get("event") or event.get("type"), None, event
        except json.JSONDecodeError:
            return None, None, None

    if msg_type in (MSG_TYPE_AUDIO_SERVER, MSG_TYPE_AUDIO_ONLY):
        return "audio", payload, None
    return None, None, None


def _fallback_system_role(model_display_name: str) -> str:
    """CallSession 未下发提示词时的兜底人设（正常通话路径不会走到）。"""
    return (
        f"你叫红茶语音助手，是接入电话的语音 Agent。接通后先用中文自我介绍，"
        f"说明你是红茶语音助手，并说明底层模型是「{model_display_name}」。"
        "回答简洁，适合电话语音。"
    )


class DoubaoVoiceAgent(VoiceAgent):
    input_rate = 16000
    output_rate = 24000

    def __init__(
        self,
        app_id: str,
        access_key: str,
        resource_id: str,
        app_key: str,
        model_display_name: str,
    ) -> None:
        self.app_id = app_id
        self.access_key = access_key
        self.resource_id = resource_id
        self.app_key = app_key
        self.model_display_name = model_display_name
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._running = False
        # 已打过样的事件名：每种只打一次，避免刷屏
        self._sampled_events: set[str] = set()

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        if not self.app_id or not self.access_key:
            raise RuntimeError("豆包凭证未配置，请设置 DOUBAO_APP_ID 和 DOUBAO_ACCESS_KEY")

        self._on_audio_out = on_audio_out
        self._running = True

        headers = {
            "X-Api-App-ID": self.app_id,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-App-Key": self.app_key,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        self._ws = await websockets.connect(WS_URL, additional_headers=headers)
        logger.info("豆包 Realtime 连接已建立")

        start_session = {
            "event": "StartSession",
            "req_params": {
                "bot_name": "AgentCall",
                # 用 CallSession 下发的会话提示词，而不是硬编码人设（#69）。
                # 原实现完全无视 prompts.py，机主姓名/助理人设/本通任务/分诊
                # 限制/按键指引一概到不了豆包，等于换了个 provider 就换了套行为。
                "system_role": self._resolve_system_role(),
                "speaking_style": "语速适中，口语自然。",
                "input_mod": "audio",
                "model": "O",
            },
        }
        await self._ws.send(_pack_json_event(start_session))
        self._recv_task = asyncio.create_task(self._recv_loop())

    def set_tools(self, registry: Any) -> None:
        """豆包尚未接入 function calling —— 大声说出来，别静默吞掉（#69）。

        基类默认实现只是存下 registry，于是 send_dtmf / hangup_call /
        send_sms 全部**静默**失效：调用方以为工具挂上了，实际一个都不会被调用。
        对按键导航这类场景，静默失效比直接报错危险得多。
        """
        super().set_tools(registry)
        if registry is not None and registry.has_tools():
            logger.warning(
                "豆包 provider 未接入 function calling：已注册的 %d 个工具"
                "（按键 / 挂断 / 发短信等）在本通电话中都不会生效。"
                "需要这些能力请改用 openai 或 qwen。",
                len(registry.specs()),
            )

    def _resolve_system_role(self) -> str:
        """取会话提示词；没有才用兜底人设，并告警。

        生产路径上 CallSession 一定在 start() 前下发提示词（call_agent.py:433），
        所以走到兜底就说明接线断了 —— 那种情况下模型会丢掉机主姓名、本通任务、
        分诊限制等全部上下文，必须可见而不是悄悄降级（Codex 评审 P2）。
        """
        if self._session_instructions:
            return self._session_instructions
        logger.warning(
            "豆包会话未收到提示词，回落到内置人设："
            "机主姓名 / 本通任务 / 分诊限制等上下文都不会进入模型"
        )
        return _fallback_system_role(self.model_display_name)

    def _sample_unknown_event(self, event_name: str, event: dict[str, Any] | None) -> None:
        """每种服务端事件打样一次（INFO 级）。

        豆包的 ASR / 工具调用事件字段名尚未确认，直接猜字段是不可验证的。
        打样让任何一个有豆包凭证的人跑一通电话就能拿到真实事件结构，
        把「静默不支持」变成「一通电话就能补齐」。
        """
        if event_name in self._sampled_events:
            return
        self._sampled_events.add(event_name)
        logger.info(
            "豆包事件打样 event=%s keys=%s",
            event_name,
            sorted(event.keys()) if isinstance(event, dict) else None,
        )

    async def send_audio(self, pcm: bytes) -> None:
        if not self._ws or not pcm:
            return
        await self._ws.send(_pack_audio_payload(pcm))

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                finish = {"event": "FinishSession"}
                await self._ws.send(_pack_json_event(finish))
            except Exception as exc:  # noqa: BLE001
                logger.warning("结束豆包会话异常: %s", exc)
            await self._ws.close()
        if self._recv_task:
            await asyncio.gather(self._recv_task, return_exceptions=True)
        self._ws = None

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    continue
                event_name, audio, event = _unpack_server_message(message)
                if audio and self._on_audio_out:
                    self._on_audio_out(audio)
                elif event is not None:
                    # 连没有 event/type 字段的 JSON 帧也要打样：schema 本就未确认，
                    # 漏掉的很可能正是要找的那几种（Codex 评审 P2）。
                    self._sample_unknown_event(event_name or "__unnamed__", event)
                if not self._running:
                    break
        except websockets.ConnectionClosed:
            logger.info("豆包连接已关闭")
        except Exception as exc:  # noqa: BLE001
            logger.error("豆包接收循环异常: %s", exc)
        finally:
            # 豆包实现无重连机制：通话进行中接收循环退出（服务端关闭/异常）
            # 即会话不可恢复，置 fatal 让 CallSession 主循环结束整通电话，
            # 避免"电话活着但 AI 已死"。主动 stop() 已先置 _running=False，
            # 不会误伤正常收尾路径。
            if self._running:
                self.fatal = True
