from __future__ import annotations

import asyncio

from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.takeover_coordinator import TakeoverState
from agentcall.triage_judge import TriageVerdict


def _service() -> CallAgentService:
    service = CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = 4
    session._initialize_takeover_context("inbound")
    session._triage_mode = "enforce"
    return service


def _verdict(*, action: str, turn_id: int, confidence: float, category: str):
    return TriageVerdict(
        category=category,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        confidence=confidence,
        reason_code="test_reason",
        turn_id=turn_id,
        call_generation=4,
    )


def test_transfer_verdict_calls_orchestrator_not_realtime_tool(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    session = service.session
    session._triage_results.put_nowait(
        _verdict(
            action="transfer", turn_id=1, confidence=0.7, category="personal"
        )
    )

    outcome = asyncio.run(
        session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
    )

    assert outcome == "transfer"
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert service.next_inbound_takeover_offer() is not None
    assert session._triage_terminal is True


def test_reject_needs_two_turns_then_uses_fixed_line_and_bounded_timer() -> None:
    service = _service()
    session = service.session
    agent = FakeAgent()
    bridge = FakeAudioBridge()
    session._triage_results.put_nowait(
        _verdict(
            action="reject", turn_id=1, confidence=0.9, category="marketing"
        )
    )
    assert asyncio.run(session._consume_triage_results(agent, bridge, 4)) is None
    assert "具体事情找本人" in agent.said[-1]
    assert session._triage_terminal is False

    session._triage_results.put_nowait(
        _verdict(
            action="reject", turn_id=2, confidence=0.9, category="marketing"
        )
    )
    outcome = asyncio.run(session._consume_triage_results(agent, bridge, 4))

    assert outcome == "reject"
    assert "目前不需要这项服务" in agent.said[-1]
    assert session._triage_terminal is True
    assert session._triage_reject_deadline is not None


def test_stale_or_low_confidence_verdict_has_no_irreversible_effect() -> None:
    service = _service()
    session = service.session
    session._triage_results.put_nowait(
        TriageVerdict(
            category="personal",
            action="transfer",
            confidence=0.99,
            reason_code="owner_requested",
            turn_id=1,
            call_generation=3,
        )
    )
    session._triage_results.put_nowait(
        _verdict(
            action="reject", turn_id=2, confidence=0.84, category="marketing"
        )
    )

    outcome = asyncio.run(
        session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
    )
    assert outcome is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._triage_terminal is False


def test_transfer_precommit_failure_reopens_policy_lane(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    service = _service()
    session = service.session
    session._triage_results.put_nowait(
        _verdict(
            action="transfer", turn_id=1, confidence=0.9, category="personal"
        )
    )
    assert (
        asyncio.run(
            session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
        )
        is None
    )
    assert session._triage_terminal is False

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    session._triage_results.put_nowait(
        _verdict(
            action="transfer", turn_id=2, confidence=0.9, category="personal"
        )
    )
    assert (
        asyncio.run(
            session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
        )
        == "transfer"
    )


def test_owner_preference_is_reserved_for_judge_not_realtime(monkeypatch) -> None:
    service = _service()
    service.session._triage_mode = "enforce"
    # 限制话术自 #76 起跟随 _triage_pending 而不是 mode（放行裁决要能解除它）。
    # 生产里两者由 _initialize_triage_context 一起写，这里补齐等价初始状态。
    service.session._triage_pending = True
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv(
        "INBOUND_TAKEOVER_PREFERENCE",
        "PRIVATE_OWNER_POLICY_SENTINEL",
    )

    instructions = service.session._build_agent_instructions("inbound")

    assert "PRIVATE_OWNER_POLICY_SENTINEL" not in instructions
    assert "分诊等待态" in instructions


def test_takeover_hold_line_discards_stale_bridge_backlog(monkeypatch) -> None:
    """真机 2026-08-19（#121）：判决落地前 OpenAI 已突发投递的旧话轮
    （「他现在不方便接…我复述一下」）整段积压在桥里慢慢播——请求路径清的
    应用层队列够不到这层，push 已到机主手机而 caller 还在听「不方便接」。
    垫话开播前必须丢弃桥内未播积压，让「正在转接」立即接上。"""
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    session = service.session
    agent = FakeAgent()
    bridge = FakeAudioBridge()
    session._triage_results.put_nowait(
        _verdict(action="transfer", turn_id=1, confidence=0.9, category="personal")
    )
    assert asyncio.run(session._consume_triage_results(agent, bridge, 4)) == "transfer"

    bridge.pending_bytes = 80_000  # 模拟桥内 ~5s 旧话轮积压
    asyncio.run(session._speak_takeover_hold_if_needed(agent, bridge, 4))

    assert bridge.discarded_bytes == 80_000
    assert bridge.pending_bytes == 0
    assert "正在为您转接" in agent.said[-1] or "putting you through" in agent.said[-1]


def test_takeover_hold_text_states_transfer_in_progress() -> None:
    """垫话按实际状态说（#121）：播这句时转接请求已发出，措辞必须是
    「正在转接」而不是「我确认一下」。"""
    from agentcall.call_agent import _INBOUND_TAKEOVER_HOLD_TEXT

    assert "正在为您转接" in _INBOUND_TAKEOVER_HOLD_TEXT["zh"]
    assert "putting you through" in _INBOUND_TAKEOVER_HOLD_TEXT["en"]
    assert "确认一下" not in _INBOUND_TAKEOVER_HOLD_TEXT["zh"]
