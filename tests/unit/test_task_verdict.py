"""判定层（WIL-95 §4 第三期）：fail-closed、复核门、证据提炼的契约测试。"""

from __future__ import annotations

import json
import random

from agentcall.task_verdict import (
    VERDICT_SCHEMA_VERSION,
    build_evidence,
    hard_evidence_present,
    judge_call,
)


def _evidence(**overrides):
    base = {
        "goal": "确认周六 19:00 有无空位",
        "transcripts": [("agent", "请问周六晚上七点有位吗"), ("user", "有的，帮您留了")],
        "dtmf_outcomes": {},
        "tool_results": [],
        "termination_status": "completed",
        "summary_ok": False,
        "summary_text": "",
    }
    base.update(overrides)
    return base


def _llm_returning(payload: dict):
    def llm(messages):
        return json.dumps(payload, ensure_ascii=False), None
    return llm


def _no_call_llm(messages):
    raise AssertionError("证据不足时不应烧 LLM 调用")


class _FixedRng(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__()
        self._value = value

    def random(self) -> float:
        return self._value


# ---- fail-closed ----


def test_no_goal_is_uncertain_without_llm_call():
    verdict = judge_call(_evidence(goal=""), _no_call_llm)
    assert verdict["conclusion"] == "uncertain"
    assert verdict["reasons"] == "no_task_goal"
    assert verdict["llm_used"] is False
    assert verdict["needs_review"] is True and verdict["review_reason"] == "uncertain"
    assert verdict["schema_version"] == VERDICT_SCHEMA_VERSION


def test_no_dialogue_is_uncertain_without_llm_call():
    verdict = judge_call(_evidence(transcripts=[]), _no_call_llm)
    assert verdict["conclusion"] == "uncertain"
    assert verdict["reasons"] == "no_dialogue"


def test_invalid_json_fails_closed_to_uncertain():
    verdict = judge_call(_evidence(), lambda m: ("这不是 JSON", None))
    assert verdict["conclusion"] == "uncertain"
    assert verdict["needs_review"] is True


def test_llm_error_and_exception_fail_closed():
    err = judge_call(_evidence(), lambda m: (None, "超时"))
    assert err["conclusion"] == "uncertain" and err["reasons"] == "超时"

    def boom(messages):
        raise RuntimeError("网络断了")

    exc = judge_call(_evidence(), boom)
    assert exc["conclusion"] == "uncertain"
    assert "llm_exception" in exc["reasons"]


def test_invalid_conclusion_enum_fails_closed():
    verdict = judge_call(
        _evidence(), _llm_returning({"conclusion": "success", "confidence": 0.99})
    )
    assert verdict["conclusion"] == "uncertain"


# ---- 复核门：uncertain 全审、无硬证据全审、高置信抽检 ----


def test_confident_without_hard_evidence_still_needs_review():
    """WIL-74 教训的直接编码：判官再自信，没有硬证据就必须过人眼。"""
    verdict = judge_call(
        _evidence(),
        _llm_returning({
            "conclusion": "achieved", "attribution": "unknown",
            "confidence": 0.97, "reasons": "对方口头确认",
        }),
        rng=_FixedRng(0.99),
    )
    assert verdict["conclusion"] == "achieved"
    assert verdict["hard_evidence"] is False
    assert verdict["needs_review"] is True
    assert verdict["review_reason"] == "no_hard_evidence"


def test_hard_evidence_confident_passes_unless_sampled():
    evidence = _evidence(dtmf_outcomes={"observed": 2})
    assert hard_evidence_present(evidence)
    payload = {"conclusion": "achieved", "confidence": 0.9, "reasons": "IVR 已推进"}

    passed = judge_call(
        evidence, _llm_returning(payload), rng=_FixedRng(0.99), sample_rate=0.15
    )
    assert passed["needs_review"] is False and passed["review_reason"] is None

    sampled = judge_call(
        evidence, _llm_returning(payload), rng=_FixedRng(0.0), sample_rate=0.15
    )
    assert sampled["needs_review"] is True
    assert sampled["review_reason"] == "sampled"


def test_confidence_is_clamped_and_junk_tolerated():
    verdict = judge_call(
        _evidence(summary_ok=True),
        _llm_returning({
            "conclusion": "not_achieved", "attribution": "外星人",
            "confidence": 7, "evidence_refs": list(range(20)),
        }),
        rng=_FixedRng(0.99),
    )
    assert verdict["confidence"] == 1.0
    assert verdict["attribution"] == "unknown"
    assert len(verdict["evidence_refs"]) == 8


# ---- 证据提炼 ----


def test_build_evidence_extracts_goal_transcripts_and_hard_signals():
    events = [
        {"type": "task_goal", "ts": 1.0, "goal": "查话费余额"},
        {"type": "transcript", "ts": 2.0, "role": "agent", "text": "帮我查下话费"},
        {"type": "transcript", "ts": 3.0, "role": "user", "text": "您的余额是 30 元"},
        {"type": "dtmf_outcome", "ts": 4.0, "status": "observed"},
        {"type": "tool_call", "ts": 5.0, "tool": "send_sms",
         "args": {}, "result": {"success": True}},
        {"type": "call_finished", "ts": 6.0, "status": "completed"},
    ]
    evidence = build_evidence(events, {"ok": True, "summary": "已查询到余额"})
    assert evidence["goal"] == "查话费余额"
    assert len(evidence["transcripts"]) == 2
    assert evidence["dtmf_outcomes"] == {"observed": 1}
    assert evidence["tool_results"] == [{"tool": "send_sms", "success": True}]
    assert evidence["termination_status"] == "completed"
    assert evidence["summary_ok"] is True
    assert hard_evidence_present(evidence)
