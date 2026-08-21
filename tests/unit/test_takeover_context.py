"""接管来电上下文（WIL-137）：归一化、摘要解析、offer 载荷与后补更新。

机主收到接管请求时要看到「谁 + 什么事 + 号码」才好决定接不接。上下文含
PII——测试同时锁住隐私边界：不进日志、offer 可空、字段可空。
"""

from __future__ import annotations

import json

from agentcall.takeover_context import (
    MAX_CLAIMED_NAME_CHARS,
    MAX_PURPOSE_CHARS,
    TakeoverCallContext,
    build_context,
    build_summary_messages,
    parse_summary,
    summarize_call_context,
)


def test_payload_shape_is_stable_and_explicit_about_nulls():
    """线上形状固定：字段全在、空值显式 null、时间戳毫秒（与链路同单位）。

    iOS/Worker 按这个形状接；缺字段与 null 在解码侧是两回事，故不省略。
    """
    context = build_context(
        peer_number="+15105550123",
        claimed_name="Alex from Xfinity",
        purpose="确认账单地址",
        updated_at_ms=1787180000123,
    )

    assert context.as_payload() == {
        "v": 1,
        "peerNumber": "+15105550123",
        "claimedName": "Alex from Xfinity",
        "purpose": "确认账单地址",
        "updatedAtUnixMs": 1787180000123,
    }
    # 只有号码（AI 还没问出身份/来意）也是合法形状，两项显式 null
    only_number = build_context(peer_number="+15105550123", updated_at_ms=1)
    assert only_number.as_payload()["claimedName"] is None
    assert only_number.as_payload()["purpose"] is None
    assert not only_number.is_empty()
    # 什么都没有 → is_empty，调用方据此把 context 整体置 null
    assert build_context(peer_number=None).is_empty()


def test_fields_are_trimmed_collapsed_and_hard_truncated():
    """CallKit 的来电者名是单行且会截断——长度上限在 Edge 侧就压住，
    并把换行压平，避免单行 UI 里出现半截第二行。"""
    context = build_context(
        peer_number="  +15105550123 ",
        claimed_name="  Alex\n  from Xfinity  " + "x" * 100,
        purpose="确认账单地址\n顺便问问套餐" + "话" * 200,
        updated_at_ms=5,
    )

    assert context.peer_number == "+15105550123"
    assert "\n" not in context.claimed_name and "\n" not in context.purpose
    assert len(context.claimed_name) == MAX_CLAIMED_NAME_CHARS
    assert len(context.purpose) == MAX_PURPOSE_CHARS
    # 空白串归一为 None，不是空字符串——UI 只需判 null 一种缺失形态
    assert build_context(peer_number="   ", claimed_name="\n").is_empty()


def test_parse_summary_rejects_malformed_output():
    """模型输出不合约就整体作废：宁可没上下文，也不给机主错的身份。"""
    assert parse_summary("not json") == (None, None)
    assert parse_summary("[1, 2]") == (None, None)
    assert parse_summary(json.dumps({"claimed_name": 42, "purpose": None})) == (
        None,
        None,
    )
    assert parse_summary(
        json.dumps({"claimed_name": "李明", "purpose": "约周六吃饭"})
    ) == ("李明", "约周六吃饭")


def test_summary_prompt_bounds_turns_and_marks_name_as_claimed():
    """输入有界（与分诊判官同口径）；提示词必须把姓名定性为「自报/自称」——
    把自称当已核实身份展示是安全问题。"""
    turns = [("user", f"第{i}句") for i in range(30)]
    messages = build_summary_messages(turns)

    payload = json.loads(messages[1]["content"])
    assert len(payload["turns"]) == 12
    assert payload["turns"][-1]["text"] == "第29句"
    system = messages[0]["content"]
    assert "自报" in system and "不要核实" not in system
    assert "claimed_name" in system and "purpose" in system


def test_summarize_falls_back_to_empty_on_model_failure(caplog):
    """摘要只是锦上添花：模型报错/超时都不能影响接管，且不泄露通话内容。"""
    turns = [("user", "我是李明，想约机主周六吃饭")]

    def failing_call(messages, timeout):
        return None, "timeout"

    def raising_call(messages, timeout):
        raise RuntimeError("backend down")

    with caplog.at_level("INFO"):
        assert summarize_call_context(turns, model_call=failing_call) == (None, None)
        assert summarize_call_context(turns, model_call=raising_call) == (None, None)
        assert summarize_call_context([], model_call=raising_call) == (None, None)

    # 隐私边界：日志里不得出现通话内容
    assert "李明" not in caplog.text
    assert "吃饭" not in caplog.text


def test_summarize_returns_cleaned_fields():
    def ok_call(messages, timeout):
        return json.dumps(
            {"claimed_name": "  Kevin  ", "purpose": "约周六吃饭\n很随意"}
        ), None

    assert summarize_call_context([("user", "hi")], model_call=ok_call) == (
        "Kevin",
        "约周六吃饭 很随意",
    )


def test_merged_with_supplements_and_never_erases_known_fields():
    """「可后补」是补充不是覆盖：这一轮没提取出身份，不代表上一轮问到的作废。"""
    base = build_context(
        peer_number="+15105550123",
        claimed_name="Kevin",
        purpose=None,
        updated_at_ms=100,
    )

    filled = base.merged_with(
        claimed_name=None, purpose="约周六吃饭", updated_at_ms=200
    )
    assert filled.claimed_name == "Kevin"  # 旧值保留
    assert filled.purpose == "约周六吃饭"
    assert filled.peer_number == "+15105550123"
    assert filled.updated_at_ms == 200

    replaced = filled.merged_with(
        claimed_name="Kevin Zhang", purpose=None, updated_at_ms=300
    )
    assert replaced.claimed_name == "Kevin Zhang"  # 新值覆盖
    assert replaced.purpose == "约周六吃饭"


def test_context_is_immutable_value_object():
    context = TakeoverCallContext(peer_number="+1510", updated_at_ms=1)
    try:
        context.peer_number = "+1620"  # type: ignore[misc]
    except Exception as exc:
        assert type(exc).__name__ == "FrozenInstanceError"
    else:  # pragma: no cover - 冻结失效才会走到
        raise AssertionError("上下文必须是不可变值对象")


def test_claimed_name_field_contract_forbids_verified_identity_reuse():
    """``claimed_name`` 按定义是未核实的自报值，契约必须写死这一点。

    iOS 侧对该字段有不可绕过的「自称 / Claims to be」前缀展示规则。若将来
    的「已核实身份」（通讯录匹配、CNAM）被填进同一字段，UI 要么把已核实的
    错标成自称，要么有人为显示好看去掉前缀——把「自称」的安全语义抹掉。
    这条边界只能靠文档与本测试守住，故显式断言它写在模块契约里。
    """
    import agentcall.takeover_context as module

    doc = module.__doc__ or ""
    assert "未经核实" in doc
    assert "必须新增独立字段" in doc
    assert "绝不能把核实结果填进" in doc
    # 字段名本身也是契约的一部分：改名成 name 就失去了「自称」的语义锚点
    assert "claimed_name" in TakeoverCallContext.__dataclass_fields__
    assert "name" not in TakeoverCallContext.__dataclass_fields__
