"""各 provider 的中途 session.update 必须真的发到线上。

Codex 评审 P2：`test_triage_release_instructions.py` 用的是 RecordingAgent 桩，
它只能证明 CallSession 调用了这个方法——即使 OpenAI 实现直接 `return True`
而不发 ws，或者千问漏掉 voice/audio/tools，那些测试照样绿。

所以这里断言**线上真正发出去的那份 payload**。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentcall.agents.base import VoiceAgent
from agentcall.agents.openai_agent import OpenAIVoiceAgent
from agentcall.agents.qwen_agent import QwenVoiceAgent

NEW_PROMPT = "RELEASED_PROMPT_SENTINEL"


# ---- 基类：默认不支持，且必须如实返回 False ----


class _BareAgent(VoiceAgent):
    input_rate = 8000
    output_rate = 8000

    async def start(self, on_audio_out):  # pragma: no cover - 契约占位
        pass

    async def send_audio(self, pcm):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass


def test_base_reports_unsupported_rather_than_pretending():
    agent = _BareAgent()
    assert asyncio.run(agent.update_session_instructions(NEW_PROMPT)) is False, (
        "基类没有下发能力，必须返回 False —— 谎报 True 就是 #76 的静默失效重演"
    )
    assert agent._session_instructions == NEW_PROMPT


# ---- OpenAI：断言 ws 上真的发了 session.update ----


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _openai_agent() -> OpenAIVoiceAgent:
    return OpenAIVoiceAgent(
        api_key="k", model="m", model_display_name="m", voice="alloy"
    )


def test_openai_sends_session_update_on_the_wire(monkeypatch):
    monkeypatch.setenv("OPENAI_VIBE", "")
    agent = _openai_agent()
    ws = FakeWs()
    agent._ws = ws

    assert asyncio.run(agent.update_session_instructions(NEW_PROMPT)) is True
    assert len(ws.sent) == 1, "必须真的发一条 session.update"
    event = ws.sent[0]
    assert event["type"] == "session.update"
    assert event["session"]["instructions"] == NEW_PROMPT


def test_openai_partial_update_carries_only_instructions(monkeypatch):
    """Realtime 的 session.update 是增量合并，只带 instructions 才不会误清音频/工具配置。"""
    monkeypatch.setenv("OPENAI_VIBE", "")
    agent = _openai_agent()
    ws = FakeWs()
    agent._ws = ws

    asyncio.run(agent.update_session_instructions(NEW_PROMPT))

    session = ws.sent[0]["session"]
    for clobberable in ("audio", "tools", "output_modalities"):
        assert clobberable not in session, (
            f"增量更新不该带 {clobberable} —— 带了就有覆盖连接期配置的风险"
        )


def test_openai_without_connection_reports_false(monkeypatch):
    """还没连上时如实返回 False，但新提示词要留住给 start() 用。"""
    monkeypatch.setenv("OPENAI_VIBE", "")
    agent = _openai_agent()

    assert asyncio.run(agent.update_session_instructions(NEW_PROMPT)) is False
    assert agent._session_instructions == NEW_PROMPT


# ---- 千问：SDK 的 update_session 是整份覆盖，必须重建全量 ----


class FakeConversation:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update_session(self, **kwargs) -> None:
        self.updates.append(kwargs)


def _qwen_agent() -> QwenVoiceAgent:
    return QwenVoiceAgent(api_key="k", model="m", model_display_name="m", voice="Chelsie")


def test_qwen_rebuilds_the_full_session_not_just_instructions():
    agent = _qwen_agent()
    conversation = FakeConversation()
    agent._conversation = conversation

    assert asyncio.run(agent.update_session_instructions(NEW_PROMPT)) is True
    assert len(conversation.updates) == 1
    kwargs = conversation.updates[0]
    assert kwargs["instructions"] == NEW_PROMPT
    # SDK 是整份覆盖：漏掉这些字段会把音色/采样率/转写一起抹掉。
    for required in (
        "voice",
        "output_modalities",
        "input_audio_format",
        "output_audio_format",
        "enable_input_audio_transcription",
    ):
        assert required in kwargs, f"整份覆盖必须带上 {required}"


def test_qwen_without_connection_reports_false():
    agent = _qwen_agent()
    assert asyncio.run(agent.update_session_instructions(NEW_PROMPT)) is False
    assert agent._session_instructions == NEW_PROMPT


def test_qwen_blocking_send_cannot_stall_the_call_loop(monkeypatch):
    """Codex P1：dashscope 的 ws send 同步阻塞，死链时可挂几十秒。

    必须走 to_thread + 超时熔断，否则一次放行裁决就能把音频主循环拖停
    （2026-07-10 真机两通 180s+ 卡死的同一形态）。
    """
    import agentcall.agents.qwen_agent as qwen_module

    monkeypatch.setattr(qwen_module, "_SEND_TIMEOUT_SECONDS", 0.05)

    class HangingConversation:
        def update_session(self, **kwargs):
            import time

            time.sleep(5.0)  # 模拟死链

    agent = _qwen_agent()
    agent._conversation = HangingConversation()

    async def run():
        return await asyncio.wait_for(
            agent.update_session_instructions(NEW_PROMPT), timeout=2.0
        )

    # 不得抛 TimeoutError 到调用方，也不得挂满 5s —— 应熔断后返回 False。
    assert asyncio.run(run()) is False


@pytest.mark.parametrize("agent_factory", [_openai_agent, _qwen_agent])
def test_new_instructions_survive_a_failed_push(agent_factory, monkeypatch):
    """下发失败也要留住新提示词：重连/重建会话时它才是对的那份。"""
    monkeypatch.setenv("OPENAI_VIBE", "")
    agent = agent_factory()
    asyncio.run(agent.update_session_instructions(NEW_PROMPT))
    assert agent._session_instructions == NEW_PROMPT
