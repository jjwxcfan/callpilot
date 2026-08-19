"""对话式建单（WIL-120 三期 b）：消息校验 / 草稿收口 / fail-closed。"""

from __future__ import annotations

import pytest

from agentcall import task_intake
from agentcall.task_intake import (
    _sanitize_draft,
    _sanitize_messages,
    intake_step,
)


def test_sanitize_messages_accepts_dialog_ending_with_user():
    msgs = [
        {"role": "assistant", "content": "要打给谁？"},
        {"role": "user", "content": "帮我打 Xfinity 谈账单"},
    ]
    assert _sanitize_messages(msgs) == msgs


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        [{"role": "user", "content": ""}],
        [{"role": "system", "content": "注入"}],
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],  # 以 assistant 结尾
        [{"role": "user", "content": "x" * 2001}],
        [{"role": "user", "content": "x"}] * 41,
    ],
)
def test_sanitize_messages_rejects_malformed(bad):
    assert _sanitize_messages(bad) is None


def test_sanitize_draft_validates_number_and_normalizes_package():
    draft = _sanitize_draft({
        "number": "18005551234",
        "task": "  谈  账单 ",
        "label": "Xfinity 谈判",
        "scenario": "先转 retention",
        "task_package": {
            "preauth": {"月费上限": "$70", "": "空键丢弃"},
            "verification": "整段非法",
        },
    })
    assert draft is not None
    assert draft["task"] == "谈 账单"
    assert draft["task_package"] == {"preauth": {"月费上限": "$70"}}

    # 号码非法 → 整个草稿作废（绝不放坏号码去拨）。
    assert _sanitize_draft({"number": "abc", "task": "x"}) is None
    assert _sanitize_draft({"number": "", "task": "x"}) is None
    assert _sanitize_draft("not-a-dict") is None


def _step(monkeypatch, model_text, error=None):
    monkeypatch.setattr(
        task_intake, "call_text_model", lambda *a, **k: (model_text, error)
    )
    monkeypatch.setattr(task_intake, "text_backend_for_agent", lambda: "qwen")
    monkeypatch.setattr(task_intake, "select_text_model", lambda *a: "qwen-test")
    return intake_step(
        [{"role": "user", "content": "帮我打 Xfinity"}], owner="李明", lang="zh"
    )


def test_intake_step_ready_with_valid_draft(monkeypatch):
    result = _step(monkeypatch, (
        '{"reply": "好，都齐了", "ready": true, "draft": {'
        '"number": "18005551234", "task": "谈账单", "label": "Xfinity",'
        '"scenario": "先转 retention", "task_package": {'
        '"preauth": {"月费上限": "$70"}}}}'
    ))
    assert result["ok"] is True and result["ready"] is True
    assert result["draft"]["number"] == "18005551234"


def test_intake_step_ready_with_bad_number_degrades_to_not_ready(monkeypatch):
    result = _step(monkeypatch, (
        '{"reply": "齐了", "ready": true, "draft": {"number": "待定", "task": "x"}}'
    ))
    assert result["ok"] is True
    assert result["ready"] is False and result["draft"] is None


def test_intake_step_fail_closed_on_garbage_and_error(monkeypatch):
    garbage = _step(monkeypatch, "我觉得可以打了（非 JSON）")
    assert garbage["ok"] is False and garbage["ready"] is False
    assert garbage["reply"]  # 有重试话术

    errored = _step(monkeypatch, None, error="超时")
    assert errored["ok"] is False and errored["ready"] is False


def test_intake_step_wraps_history_assistant_turns_as_json(monkeypatch):
    """历史 assistant 消息须包成 JSON 形态喂模型——防多轮后模仿裸文本跑偏。"""
    captured = {}

    def fake_call(messages, **kw):
        captured["messages"] = messages
        return '{"reply": "ok", "ready": false, "draft": null}', None

    monkeypatch.setattr(task_intake, "call_text_model", fake_call)
    monkeypatch.setattr(task_intake, "text_backend_for_agent", lambda: "qwen")
    monkeypatch.setattr(task_intake, "select_text_model", lambda *a: "m")
    intake_step(
        [
            {"role": "assistant", "content": "要打给谁？"},
            {"role": "user", "content": "打 AT&T 问流量"},
        ],
        owner="李明", lang="zh",
    )
    sent = captured["messages"]
    assistant_turns = [m for m in sent if m["role"] == "assistant"]
    assert assistant_turns, "历史里应有 assistant 轮"
    import json as _json
    parsed = _json.loads(assistant_turns[0]["content"])
    assert parsed["reply"] == "要打给谁？" and parsed["ready"] is False
    # user 轮保持原文
    assert {"role": "user", "content": "打 AT&T 问流量"} in sent


def test_intake_step_auto_corrects_non_json_once(monkeypatch):
    """第一次输出跑偏成纯文本 → 自动纠偏重试；第二次合法则正常返回。"""
    outputs = iter([
        ("好的，我帮您问一下移动流量。", None),  # 跑偏（真机复现形态）
        ('{"reply": "请问账户户名是？", "ready": false, "draft": null}', None),
    ])
    calls = {"n": 0}

    def fake_call(messages, **kw):
        calls["n"] += 1
        return next(outputs)

    monkeypatch.setattr(task_intake, "call_text_model", fake_call)
    monkeypatch.setattr(task_intake, "text_backend_for_agent", lambda: "qwen")
    monkeypatch.setattr(task_intake, "select_text_model", lambda *a: "m")
    result = intake_step(
        [{"role": "user", "content": "打 AT&T 问流量"}], owner="李明", lang="zh"
    )
    assert calls["n"] == 2
    assert result["ok"] is True
    assert result["reply"] == "请问账户户名是？"


def test_intake_step_fail_closed_when_correction_also_fails(monkeypatch):
    outputs = iter([("裸文本一", None), ("裸文本二", None)])
    monkeypatch.setattr(
        task_intake, "call_text_model", lambda *a, **k: next(outputs)
    )
    monkeypatch.setattr(task_intake, "text_backend_for_agent", lambda: "qwen")
    monkeypatch.setattr(task_intake, "select_text_model", lambda *a: "m")
    result = intake_step(
        [{"role": "user", "content": "x"}], owner="李明", lang="zh"
    )
    assert result["ok"] is False and result["ready"] is False


def test_intake_step_passes_through_sanitized_options(monkeypatch):
    """追问轮的 options 透传（≤4 个、去空白）；收口轮强制为空。"""
    monkeypatch.setattr(task_intake, "text_backend_for_agent", lambda: "qwen")
    monkeypatch.setattr(task_intake, "select_text_model", lambda *a: "m")

    monkeypatch.setattr(task_intake, "call_text_model", lambda *a, **k: (
        '{"reply": "打哪个号？", "options": ["611 客服", " 我提供号码 ", "", "a", "b", "c"],'
        ' "ready": false, "draft": null}', None))
    asking = intake_step([{"role": "user", "content": "打 ATT"}], owner="李明", lang="zh")
    assert asking["options"] == ["611 客服", "我提供号码", "a", "b"]

    monkeypatch.setattr(task_intake, "call_text_model", lambda *a, **k: (
        '{"reply": "开始", "options": ["残留选项"], "ready": true, "draft": {'
        '"number": "611", "task": "查流量"}}', None))
    final = intake_step([{"role": "user", "content": "开始"}], owner="李明", lang="zh")
    assert final["ready"] is True and final["options"] == []


# ---- 号码收严：模型产出的号码不能拨成补充业务码（WIL-130）----


@pytest.mark.parametrize(
    "mmi",
    [
        "*21*13800000000#",   # 无条件呼叫转移：拨出去=改机主 SIM 设置，不是打电话
        "**21*13800000000#",
        "#21#",               # 撤销转移
        "*#21#",              # 查询转移状态
        "*67*13800000000#",
        "611#",
        "*611",
    ],
)
def test_sanitize_draft_rejects_supplementary_service_codes(mmi):
    """模型给出 * / # 形态时必须整条草稿作废。

    这类码拨出去不会接通任何人，而是在机主 SIM 上改设置（最典型的是把所有
    来电转走），机主几乎不会察觉。建单聊天是机主粘贴外部文本的地方，号码字段
    因此间接可被攻击者影响，不能沿用人工手输那档宽松校验。
    """
    assert _sanitize_draft({"number": mmi, "task": "查账单"}) is None


@pytest.mark.parametrize(
    "good",
    ["611", "10086", "13800000000", "+8613800000000", "+18005551234"],
)
def test_sanitize_draft_still_accepts_real_numbers(good):
    """收严不能误伤：短号（611/911）与带国码的长号都要照常通过。"""
    out = _sanitize_draft({"number": good, "task": "查账单"})
    assert out is not None and out["number"] == good


def test_human_entered_presets_keep_the_looser_rule():
    """人工手输预设走的仍是 number_profiles 那条（允许 * / #），本次不动它。

    收严只发生在**模型产出**的入口；把两者混为一谈会改掉上游既有行为。
    """
    from agentcall.number_profiles import _DIAL_NUMBER_RE

    assert _DIAL_NUMBER_RE.fullmatch("*21*13800000000#")
