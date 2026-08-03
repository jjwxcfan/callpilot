"""分诊放行必须真的解除限制话术（不是只翻个标志位）。

回归 #76 / WIL-80：`INBOUND_TRIAGE_MODE=enforce` 下，即使判官裁决
`continue_ai`，AI 在整通电话剩余时间里仍被锁在分诊限制话术里。

根因：`_triage_pending` 是**只写标志**（4 处写、0 处读）。真正决定提示词的
是 `triage_pending=triage_mode == "enforce"`（读 mode，不是标志），而
`set_session_instructions` 只在建会话前有效、全程只调用一次。提示词里
「在系统明确解除等待态前…」这句承诺，没有任何机制去兑现。

所以本文件的断言全部落在「**agent 实际收到的 instructions 变了**」上——
只断言标志位翻转，正是让这个 bug 在 1092 条测试下存活至今的原因。
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeAudioBridge, FakeModem

from agentcall.call_agent import CallSession
from agentcall.triage_judge import TriageVerdict

# 限制话术里的特征句；解除后必须消失。
PENDING_MARKER = "分诊等待态"


class RecordingAgent:
    """记录 provider 侧实际收到过哪几份 instructions。"""

    output_rate = 24000

    def __init__(self, *, supports_update: bool = True) -> None:
        self.supports_update = supports_update
        self.session_instructions: str | None = None
        self.pushed: list[str] = []

    def set_session_instructions(self, instructions: str | None) -> None:
        self.session_instructions = instructions

    async def update_session_instructions(self, instructions: str) -> bool:
        if not self.supports_update:
            return False
        self.pushed.append(instructions)
        return True

    async def say(self, text: str) -> None:  # pragma: no cover - 放行分支用不到
        pass


def make_session(monkeypatch) -> CallSession:
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", "enforce")
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")
    return CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )


def continue_ai_verdict(session: CallSession) -> TriageVerdict:
    return TriageVerdict(
        category="business",
        action="continue_ai",
        confidence=0.9,
        reason_code="normal_business",
        turn_id=1,
        call_generation=session._session_generation,
    )


def drive(session: CallSession, agent: RecordingAgent) -> None:
    session._triage_results.put(continue_ai_verdict(session))
    asyncio.run(
        session._consume_triage_results(
            agent,  # type: ignore[arg-type]
            FakeAudioBridge(),  # type: ignore[arg-type]
            session._session_generation,
        )
    )


def test_enforce_starts_with_the_restricted_prompt(monkeypatch):
    """前置：enforce 下开局确实带限制话术，否则后面的断言没有意义。"""
    session = make_session(monkeypatch)
    session._initialize_triage_context("inbound", None)
    assert PENDING_MARKER in session._build_agent_instructions("inbound")


def test_continue_ai_actually_pushes_a_lifted_prompt(monkeypatch):
    """核心：放行后 agent 必须收到一份**不含**限制话术的新提示词。"""
    session = make_session(monkeypatch)
    session._initialize_triage_context("inbound", None)
    agent = RecordingAgent()

    drive(session, agent)

    assert agent.pushed, "放行后必须向 provider 下发新的 instructions"
    assert PENDING_MARKER not in agent.pushed[-1], (
        "下发的提示词仍带限制话术 —— 放行没有生效"
    )


def test_flag_alone_is_not_enough(monkeypatch):
    """标志翻转 + 提示词跟着标志走，两者缺一不可。"""
    session = make_session(monkeypatch)
    session._initialize_triage_context("inbound", None)
    agent = RecordingAgent()

    drive(session, agent)

    assert session._triage_pending is False
    # 提示词构造必须跟随标志——这是「只写标志」的直接反测。
    assert PENDING_MARKER not in session._build_agent_instructions("inbound")


def test_unsupported_provider_is_reported_not_silent(monkeypatch, caplog):
    """provider 不支持中途更新时要告警，不能静默降级。"""
    session = make_session(monkeypatch)
    session._initialize_triage_context("inbound", None)
    agent = RecordingAgent(supports_update=False)

    with caplog.at_level("WARNING"):
        drive(session, agent)

    assert any("仍受限于分诊话术" in r.message for r in caplog.records), (
        "不支持中途更新必须留下可观测的告警"
    )


def test_outbound_never_carries_the_triage_prompt(monkeypatch):
    """外呼与分诊无关，不得被 inbound 的标志污染。"""
    session = make_session(monkeypatch)
    session._initialize_triage_context("inbound", None)
    assert PENDING_MARKER not in session._build_agent_instructions("outbound")


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_non_enforce_modes_have_no_restriction_to_lift(monkeypatch, mode):
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", mode)
    session = make_session(monkeypatch)
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", mode)
    session._initialize_triage_context("inbound", None)
    assert PENDING_MARKER not in session._build_agent_instructions("inbound")
