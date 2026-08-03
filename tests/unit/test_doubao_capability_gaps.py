"""豆包 provider：能补的补上，补不了的必须大声说出来。

回归 #69：`doubao_agent.py` 全文 0 处引用 `_session_instructions` / `_tools` /
`_emit_transcript`。后果是它**看起来是个平等的 provider**，实际上：

- 忽略 `prompts.py`，改用硬编码人设 —— 机主姓名/助理人设/本通任务/分诊限制
  /按键指引一概到不了模型；
- 无 function calling —— `send_dtmf` / `hangup_call` / `send_sms` 全部**静默**失效；
- 零转写 —— summarizer、收尾裁判、分诊判官、复读抑制的输入恒空。

本次的取舍（写在这里以免被后人误解为「只做了一半」）：

- **提示词注入是确定的**，`system_role` 就是提示词槽位，直接接上；
- **function calling 与 ASR 事件的报文格式尚未确认**（factory 注释也这么写）。
  盲猜字段名是不可验证的改动，不做。改为：工具被注册时**大声告警**，并把
  服务端事件打样出来，让任何有豆包凭证的人跑一通电话就能拿到真实结构。

静默失效比直接报错危险得多 —— 尤其对按键导航。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import struct

from agentcall.agents.doubao_agent import (
    MSG_COMPRESS_NONE,
    MSG_SERIAL_JSON,
    MSG_TYPE_FULL_SERVER,
    DoubaoVoiceAgent,
    _unpack_server_message,
)


def make_agent() -> DoubaoVoiceAgent:
    return DoubaoVoiceAgent(
        app_id="x",
        access_key="y",
        resource_id="r",
        app_key="k",
        model_display_name="Doubao-Test",
    )


class FakeRegistry:
    def __init__(self, count: int) -> None:
        self._count = count

    def has_tools(self) -> bool:
        return self._count > 0

    def specs(self) -> list[dict]:
        return [{"name": f"tool{i}"} for i in range(self._count)]


def server_json_frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    header = bytes(
        [
            (0x1 << 4) | 0x1,
            (MSG_TYPE_FULL_SERVER << 4) | MSG_SERIAL_JSON,
            (MSG_COMPRESS_NONE << 4),
            0x0,
        ]
    )
    return header + struct.pack(">I", len(body)) + body


# ---- 提示词注入（可以补，所以补上） ----


class FakeWs:
    """记录真正发出去的帧，好断言 StartSession 的内容而不是源码文本。"""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, frame) -> None:
        self.sent.append(frame if isinstance(frame, bytes) else frame.encode())

    async def close(self) -> None:
        pass

    def __aiter__(self):
        async def _empty():
            if False:
                yield b""
        return _empty()


def start_session_payload(ws: FakeWs) -> dict:
    """从第一帧里解出 StartSession 的 JSON（Codex 评审 P2：断言真实报文）。"""
    frame = ws.sent[0]
    size = struct.unpack(">I", frame[4:8])[0]
    body = frame[8 : 8 + size]
    if (frame[2] >> 4) & 0xF == 0x1:
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


async def connect_and_capture(agent: DoubaoVoiceAgent, monkeypatch) -> FakeWs:
    import agentcall.agents.doubao_agent as module

    ws = FakeWs()

    async def fake_connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(module.websockets, "connect", fake_connect)
    await agent.start(lambda _pcm: None)
    agent._running = False
    if agent._recv_task:
        agent._recv_task.cancel()
    return ws


def test_session_instructions_reach_the_model(monkeypatch):
    """核心：CallSession 下发的提示词必须真的出现在发出去的 StartSession 里。"""
    agent = make_agent()
    agent.set_session_instructions("SENTINEL_PROMPT_FROM_PROMPTS_PY")
    ws = asyncio.run(connect_and_capture(agent, monkeypatch))

    payload = start_session_payload(ws)
    assert payload["event"] == "StartSession"
    assert payload["req_params"]["system_role"] == "SENTINEL_PROMPT_FROM_PROMPTS_PY"
    assert "红茶语音助手" not in payload["req_params"]["system_role"], (
        "硬编码人设不得凌驾于 prompts.py 之上"
    )


def test_falls_back_and_warns_when_no_instructions(monkeypatch, caplog):
    """兜底人设被用上说明接线断了，必须可见（Codex 评审 P2）。"""
    agent = make_agent()
    with caplog.at_level("WARNING"):
        ws = asyncio.run(connect_and_capture(agent, monkeypatch))

    role = start_session_payload(ws)["req_params"]["system_role"]
    assert "红茶语音助手" in role and "Doubao-Test" in role
    assert any("未收到提示词" in r.getMessage() for r in caplog.records), (
        "回落到内置人设必须告警，不能悄悄降级"
    )


# ---- 能力缺口：必须大声 ----


def test_registering_tools_warns_loudly(caplog):
    """静默吞掉工具最危险：调用方以为按键挂上了，实际一次都不会被调用。"""
    agent = make_agent()
    with caplog.at_level("WARNING"):
        agent.set_tools(FakeRegistry(3))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "function calling" in joined
    assert "3" in joined, "应说明有几个工具会失效"


def test_no_warning_when_there_are_no_tools(caplog):
    agent = make_agent()
    with caplog.at_level("WARNING"):
        agent.set_tools(FakeRegistry(0))
        agent.set_tools(None)
    assert [r for r in caplog.records if "function calling" in r.getMessage()] == []


def test_tools_are_still_stored_for_a_future_implementation():
    agent = make_agent()
    registry = FakeRegistry(2)
    agent.set_tools(registry)
    assert agent._tools is registry


# ---- 事件负载不再被丢弃 ----


def test_event_payload_is_returned_not_discarded():
    """原实现只取事件名就把整个负载扔了 —— 转写即便在里面也拿不到。"""
    frame = server_json_frame({"event": "ASRResponse", "results": [{"text": "你好"}]})
    name, audio, event = _unpack_server_message(frame)
    assert name == "ASRResponse"
    assert audio is None
    assert event is not None and event["results"][0]["text"] == "你好"


def test_gzipped_payload_still_parses():
    body = gzip.compress(json.dumps({"event": "Gzipped"}).encode("utf-8"))
    header = bytes(
        [(0x1 << 4) | 0x1, (MSG_TYPE_FULL_SERVER << 4) | MSG_SERIAL_JSON, (0x1 << 4), 0x0]
    )
    name, _audio, event = _unpack_server_message(header + struct.pack(">I", len(body)) + body)
    assert name == "Gzipped" and event is not None


def test_malformed_frame_does_not_raise():
    assert _unpack_server_message(b"\x00\x00") == (None, None, None)


# ---- 事件打样：让缺口一通电话内可补 ----


def test_each_event_type_is_sampled_once(caplog):
    agent = make_agent()
    with caplog.at_level("INFO"):
        agent._sample_unknown_event("ASRResponse", {"event": "ASRResponse", "text": "x"})
        agent._sample_unknown_event("ASRResponse", {"event": "ASRResponse", "text": "y"})
        agent._sample_unknown_event("ToolCall", {"event": "ToolCall", "name": "f"})
    sampled = [r for r in caplog.records if "豆包事件打样" in r.getMessage()]
    assert len(sampled) == 2, "每种事件只打一次样，同种不得刷屏"


def test_sample_records_the_field_names():
    """打样要给出字段名 —— 那正是后续补齐 ASR/工具所缺的信息。"""
    agent = make_agent()
    import logging

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("agentcall.agents.doubao_agent")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        agent._sample_unknown_event("ASRResponse", {"event": "ASRResponse", "text": "x"})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    rendered = " ".join(r.getMessage() for r in records)
    assert "event" in rendered and "text" in rendered, (
        f"打样必须给出字段名，实际: {rendered}"
    )


def test_unnamed_json_events_are_also_sampled(caplog):
    """Codex P2：没有 event/type 字段的 JSON 帧也要打样。

    schema 本就未确认，漏掉的很可能正是要找的那几种 —— 打样的全部意义就在于
    发现未知结构，按「有没有 event 字段」筛掉一部分等于自断线索。
    """
    agent = make_agent()
    with caplog.at_level("INFO"):
        agent._sample_unknown_event("__unnamed__", {"payload": {"text": "x"}})
    assert any("__unnamed__" in r.getMessage() for r in caplog.records)


def test_recv_loop_samples_on_event_presence_not_name():
    """守住上面那条：分支条件必须看事件对象是否存在，而不是事件名是否非空。"""
    import inspect

    src = inspect.getsource(DoubaoVoiceAgent._recv_loop)
    assert "elif event is not None:" in src, (
        "无名 JSON 帧会被重新漏掉"
    )
