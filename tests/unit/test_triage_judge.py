import json
import threading
import time

import pytest

from agentcall.triage_judge import (
    InboundTriageJudge,
    TriageJudgeError,
    TriageVerdictConsumer,
    judge_transcript,
    parse_triage_verdict,
)


def _response(**overrides):
    payload = {
        "category": "personal",
        "action": "transfer",
        "confidence": 0.93,
        "reason_code": "owner_requested",
        "turn_id": 1,
        "call_generation": 7,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_verdict_is_strict_and_rejects_duplicate_fields():
    verdict = parse_triage_verdict(_response())
    assert verdict.action == "transfer"
    assert verdict.call_generation == 7
    assert verdict.public_fields()["category"] == "personal"

    with pytest.raises(TriageJudgeError, match="invalid_schema"):
        parse_triage_verdict(_response(extra="leak"))
    with pytest.raises(TriageJudgeError, match="duplicate_fields"):
        parse_triage_verdict(
            '{"category":"personal","action":"transfer","action":"reject",'
            '"confidence":0.9,"reason_code":"owner_requested","turn_id":1,'
            '"call_generation":7}'
        )


def test_judge_transcript_fences_turn_and_generation():
    def stale(_messages, _timeout):
        return _response(turn_id=2), None

    with pytest.raises(TriageJudgeError, match="turn_mismatch"):
        judge_transcript(
            [("user", "找本人")],
            "找本人的都转接",
            turn_id=1,
            call_generation=7,
            model_call=stale,
        )


def test_event_worker_debounces_and_uses_a_per_call_lane():
    seen = []
    ready = threading.Event()

    def model(messages, _timeout):
        payload = json.loads(messages[1]["content"])
        return _response(
            turn_id=payload["turn_id"],
            call_generation=payload["call_generation"],
        ), None

    judge = InboundTriageJudge(
        call_generation=7,
        preference="找本人的都转接",
        on_verdict=lambda verdict, _latency: (seen.append(verdict), ready.set()),
        model_call=model,
        debounce_seconds=0.3,
    )
    judge.start()
    judge.submit_turn("user", "我是老王")
    judge.submit_turn("agent", "请问什么事")
    judge.submit_turn("user", "找本人有急事")
    assert ready.wait(1.0)
    judge.stop()

    assert len(seen) == 1
    assert seen[0].turn_id == 2
    assert seen[0].call_generation == 7


def test_timeout_reports_error_and_never_emits_verdict():
    errors = []
    ready = threading.Event()

    def blocked(_messages, _timeout):
        time.sleep(0.3)
        return _response(call_generation=3), None

    judge = InboundTriageJudge(
        call_generation=3,
        preference="",
        on_verdict=lambda *_args: pytest.fail("timeout must not emit a verdict"),
        on_error=lambda *args: (errors.append(args), ready.set()),
        model_call=blocked,
        debounce_seconds=0.3,
        timeout_seconds=0.05,
    )
    judge.start()
    judge.submit_turn("user", "你好")
    assert ready.wait(1.0)
    judge.stop()
    assert errors[0][0] == "timeout"


def test_new_caller_turn_fences_inflight_older_verdict():
    seen = []
    ready = threading.Event()
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def model(messages, _timeout):
        nonlocal calls
        calls += 1
        payload = json.loads(messages[1]["content"])
        if calls == 1:
            first_started.set()
            release_first.wait(1.0)
        return _response(
            turn_id=payload["turn_id"],
            call_generation=payload["call_generation"],
        ), None

    judge = InboundTriageJudge(
        call_generation=7,
        preference="找本人的都转接",
        on_verdict=lambda verdict, _latency: (seen.append(verdict), ready.set()),
        model_call=model,
        debounce_seconds=0.3,
        timeout_seconds=1.0,
    )
    judge.start()
    judge.submit_turn("user", "我是老王")
    assert first_started.wait(0.8)
    judge.submit_turn("user", "找本人有急事")
    release_first.set()
    assert ready.wait(1.2)
    judge.stop()

    assert calls == 2
    assert [verdict.turn_id for verdict in seen] == [2]


def test_consumer_fences_and_requires_second_reject_confirmation():
    consumer = TriageVerdictConsumer()
    first = parse_triage_verdict(
        _response(
            category="marketing",
            action="reject",
            confidence=0.91,
            turn_id=1,
        )
    )
    stale = parse_triage_verdict(_response(call_generation=6))
    second = parse_triage_verdict(
        _response(
            category="marketing",
            action="reject",
            confidence=0.9,
            turn_id=2,
        )
    )

    assert consumer.consume(stale, current_generation=7).outcome == "ignored"
    assert consumer.consume(first, current_generation=7).outcome == "clarify"
    assert consumer.consume(first, current_generation=7).outcome == "ignored"
    assert consumer.consume(second, current_generation=7).outcome == "reject"


def test_consumer_transfers_at_threshold_without_realtime_discretion():
    consumer = TriageVerdictConsumer()
    verdict = parse_triage_verdict(_response(confidence=0.7))
    result = consumer.consume(verdict, current_generation=7)
    assert result.outcome == "transfer"
    assert result.reason == "threshold_met"


# ---- 转接闸门（真机 2026-08-19 spam 演练回归）----


def test_replay_20260819_pressure_talk_cannot_flip_pending_reject():
    """真机回放：健保推销判 marketing/reject(0.9) 待确认后，「我要马上跟本人说」
    的加压话术让判官出 unknown/transfer(0.8)——修复前直接转接，修复后不执行、
    且拒绝候选保留，下一轮同类别 reject 即完成二次确认。"""
    consumer = TriageVerdictConsumer()
    spam = parse_triage_verdict(
        _response(
            category="marketing",
            action="reject",
            confidence=0.9,
            reason_code="spam_call",
            turn_id=1,
        )
    )
    pressure = parse_triage_verdict(
        _response(
            category="unknown",
            action="transfer",
            confidence=0.8,
            reason_code="valid_request",
            turn_id=2,
        )
    )
    confirm = parse_triage_verdict(
        _response(
            category="marketing",
            action="reject",
            confidence=0.88,
            reason_code="spam_call",
            turn_id=3,
        )
    )

    assert consumer.consume(spam, current_generation=7).outcome == "clarify"
    gated = consumer.consume(pressure, current_generation=7)
    assert gated.outcome == "observe"
    assert gated.reason == "transfer_category_not_eligible"
    assert consumer.consume(confirm, current_generation=7).outcome == "reject"


def test_transfer_requires_identified_category():
    """unknown/marketing 的 transfer 判决即使高置信也不执行——判官自己都定不出
    来电性质时，不能做不可逆转接。"""
    for category in ("unknown", "marketing"):
        consumer = TriageVerdictConsumer()
        verdict = parse_triage_verdict(
            _response(category=category, confidence=0.95)
        )
        result = consumer.consume(verdict, current_generation=7)
        assert result.outcome == "observe"
        assert result.reason == "transfer_category_not_eligible"


def test_transfer_over_pending_reject_needs_reject_grade_confidence():
    """有待确认拒绝时，翻案为转接要拿出不低于拒绝门槛(0.85)的置信度。"""
    consumer = TriageVerdictConsumer()
    spam = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=1)
    )
    weak_flip = parse_triage_verdict(
        _response(category="personal", confidence=0.8, turn_id=2)
    )
    strong_flip = parse_triage_verdict(
        _response(category="personal", confidence=0.9, turn_id=3)
    )

    assert consumer.consume(spam, current_generation=7).outcome == "clarify"
    gated = consumer.consume(weak_flip, current_generation=7)
    assert gated.outcome == "observe"
    assert gated.reason == "transfer_needs_stronger_evidence"
    assert consumer.consume(strong_flip, current_generation=7).outcome == "transfer"


def test_below_threshold_transfer_keeps_reject_candidate():
    """低置信 transfer 摇摆不得重置拒绝确认——否则施压话术可无限拖延 reject。"""
    consumer = TriageVerdictConsumer()
    spam = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=1)
    )
    wobble = parse_triage_verdict(
        _response(category="personal", confidence=0.5, turn_id=2)
    )
    confirm = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=3)
    )

    assert consumer.consume(spam, current_generation=7).outcome == "clarify"
    wobbled = consumer.consume(wobble, current_generation=7)
    assert wobbled.outcome == "observe"
    assert wobbled.reason == "below_threshold"
    assert consumer.consume(confirm, current_generation=7).outcome == "reject"


def test_judge_prompt_gates_transfer_semantics():
    """系统提示的承重语句不得回退：unknown 不转、施压话术不构成来意、
    旧的「找本人优先 transfer」通道已移除。"""
    from agentcall.triage_judge import _SYSTEM_PROMPT

    assert "不构成正当来意" in _SYSTEM_PROMPT
    assert "性质仍是 unknown 时先 clarify" in _SYSTEM_PROMPT
    assert "维持原判" in _SYSTEM_PROMPT
    assert "清晰意图应优先 transfer" not in _SYSTEM_PROMPT


def test_clarify_keeps_reject_candidate():
    """clarify 是待定不是改判：判官按新提示词对加压话术输出 unknown/clarify 时，
    不得重置拒绝确认（否则 transfer 关掉的摇摆重置通道原样搬进 clarify）。"""
    consumer = TriageVerdictConsumer()
    spam = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=1)
    )
    wobble = parse_triage_verdict(
        _response(category="unknown", action="clarify", confidence=0.6, turn_id=2)
    )
    confirm = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=3)
    )

    assert consumer.consume(spam, current_generation=7).outcome == "clarify"
    assert consumer.consume(wobble, current_generation=7).outcome == "clarify"
    assert consumer.consume(confirm, current_generation=7).outcome == "reject"


def test_continue_ai_still_clears_reject_candidate():
    """continue_ai 是与拒绝相反的积极改判，应照旧清空候选、重新起算确认。"""
    consumer = TriageVerdictConsumer()
    spam = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=1)
    )
    release = parse_triage_verdict(
        _response(category="service", action="continue_ai", confidence=0.8, turn_id=2)
    )
    again = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=3)
    )

    assert consumer.consume(spam, current_generation=7).outcome == "clarify"
    assert consumer.consume(release, current_generation=7).outcome == "continue_ai"
    # 候选已被 continue_ai 清空，再次 reject 需重新走确认
    assert (
        consumer.consume(again, current_generation=7).reason
        == "reject_confirmation_required"
    )


# ---- clarify 死锁兜底（真机 2026-08-21）----


def test_clarify_deadlock_releases_to_ai_when_nothing_looks_like_spam():
    """判官卡在 unknown 上连续 clarify 时放行给 AI，别把通话变成一堵墙。

    真机 2026-08-21：转写把机主名字听成了别的词，判官三轮都判
    unknown/clarify，来电者三次明确要求转接都没反应，而 AI 被锁在分诊限制
    话术里只会说「我没法转接也没法转告」。放行只是让 AI 正常接待——
    不把电话递给机主，不构成 spam 触达。
    """
    consumer = TriageVerdictConsumer(max_clarify_streak=3)
    outcomes = []
    for turn_id in (1, 2, 3):
        verdict = parse_triage_verdict(
            _response(
                category="unknown", action="clarify", confidence=0.5, turn_id=turn_id
            )
        )
        result = consumer.consume(verdict, current_generation=7)
        outcomes.append((result.outcome, result.reason))

    assert outcomes[0][0] == "clarify" and outcomes[1][0] == "clarify"
    assert outcomes[2] == ("continue_ai", "clarify_deadlock_released")


def test_clarify_deadlock_never_releases_while_a_reject_is_pending():
    """有待确认拒绝时绝不放行——那正是 spam 场景，放行等于给推销让路。"""
    consumer = TriageVerdictConsumer(max_clarify_streak=2)
    spam = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=1)
    )
    assert consumer.consume(spam, current_generation=7).outcome == "clarify"

    for turn_id in (2, 3, 4, 5):
        verdict = parse_triage_verdict(
            _response(
                category="unknown", action="clarify", confidence=0.5, turn_id=turn_id
            )
        )
        result = consumer.consume(verdict, current_generation=7)
        assert result.outcome == "clarify"
        assert result.reason != "clarify_deadlock_released"

    # 拒绝候选仍在：同类别第二票照常完成拒绝
    confirm = parse_triage_verdict(
        _response(category="marketing", action="reject", confidence=0.9, turn_id=6)
    )
    assert consumer.consume(confirm, current_generation=7).outcome == "reject"


def test_clarify_streak_resets_on_progress():
    """判官一旦给出实质判决，连击计数归零——不能让跨阶段的零星 clarify 累加
    成误放行。"""
    consumer = TriageVerdictConsumer(max_clarify_streak=3)
    for turn_id in (1, 2):
        consumer.consume(
            parse_triage_verdict(
                _response(
                    category="unknown", action="clarify", confidence=0.5, turn_id=turn_id
                )
            ),
            current_generation=7,
        )
    consumer.consume(
        parse_triage_verdict(
            _response(
                category="service", action="continue_ai", confidence=0.8, turn_id=3
            )
        ),
        current_generation=7,
    )
    result = consumer.consume(
        parse_triage_verdict(
            _response(category="unknown", action="clarify", confidence=0.5, turn_id=4)
        ),
        current_generation=7,
    )
    assert result.outcome == "clarify"  # 计数已归零，这只是第 1 次


def test_judge_input_carries_owner_name_for_mis_transcribed_requests():
    """判官必须知道机主叫什么：电话转写常把人名听错（真机把「Shaocheng」
    听成「ChatGPT」），不知道名字就会把「我找 X」当语无伦次而一直判 unknown。"""
    from agentcall.triage_judge import _SYSTEM_PROMPT, build_triage_messages

    messages = build_triage_messages(
        [("user", "I'm ChatGPT's friend, put me through")],
        "真心找机主的转接",
        turn_id=1,
        call_generation=0,
        owner="李明",
    )
    payload = json.loads(messages[1]["content"])
    assert payload["owner_name"] == "李明"
    # 提示词要给出两条纠偏：音近容忍 + 反复明确要求转接的升级路径
    assert "音近" in _SYSTEM_PROMPT
    assert "反复、明确地要求转接本人" in _SYSTEM_PROMPT
    assert "不要继续 clarify" in _SYSTEM_PROMPT
