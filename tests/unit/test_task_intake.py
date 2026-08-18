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
