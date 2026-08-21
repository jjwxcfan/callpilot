"""分诊等待态的工具门禁（#126）：机制层拒绝对外副作用工具。

真机 2026-08-19 spam 演练：TRIAGE_PENDING 提示词明文禁止转告，agent 仍调用
send_sms 把推销话术原样转发机主——提示词约束挡不住工具调用惯性（WIL-134
同一课）。这里验证：等待态 send_sms 在执行层被拒（明确 code）、判官放行后
同一注册表恢复可用、hangup_call / send_dtmf 等只作用于当前通话的工具不受影响。
"""

from __future__ import annotations

import json

from fakes import FakeModem

from agentcall.agents.tools import ToolRegistry
from agentcall.call_agent import CallAgentService


class SpyRecord:
    """CallRecord 替身：只记录审计事件（与 CallRecord.log_event 同形）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log_event(self, type: str, **fields) -> None:  # noqa: A002
        self.events.append((type, fields))


def _service() -> tuple[CallAgentService, FakeModem]:
    modem = FakeModem()
    service = CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="openai",
        modem=modem,  # type: ignore[arg-type]
    )
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = 4
    session._initialize_takeover_context("inbound")
    session._triage_mode = "enforce"
    session._triage_pending = True
    session.current_caller = "+16505550100"  # 占位号（Alex 来电）
    return service, modem


def test_triage_pending_blocks_send_sms_with_explicit_code() -> None:
    service, modem = _service()
    session = service.session
    session._record = SpyRecord()  # type: ignore[assignment]
    registry = session._build_tools("inbound")

    result = registry.dispatch(
        "send_sms",
        {"to": "owner", "content": "Sarah says the enrollment deadline is today"},
    )

    assert result["success"] is False
    assert result["code"] == "TRIAGE_PENDING_BLOCKED"
    # 机制层拦截：modem 层完全没有 send_sms 执行记录（不是发了才失败）。
    assert all(name != "send_sms" for name, _ in modem.calls)
    # 拦截落审计事件，真机演练可据此核查「等待态全程无 send_sms 执行」。
    assert ("triage_tool_blocked", {"tool": "send_sms"}) in session._record.events


def test_release_restores_send_sms_on_same_registry(monkeypatch) -> None:
    """判官 continue_ai 只清 _triage_pending，不重建注册表——门禁读活状态。"""
    monkeypatch.setenv("OWNER_PHONE", "+15550100001")  # 占位号（机主李明）
    service, modem = _service()
    session = service.session
    registry = session._build_tools("inbound")

    blocked = registry.dispatch("send_sms", {"to": "owner", "content": "hold on"})
    assert blocked["code"] == "TRIAGE_PENDING_BLOCKED"

    session._triage_pending = False  # 等价于判官 continue_ai 放行
    released = registry.dispatch(
        "send_sms", {"to": "owner", "content": "Alex asks you to call back"}
    )

    assert released["success"] is True
    assert any(name == "send_sms" for name, _ in modem.calls)


def test_triage_pending_keeps_hangup_available() -> None:
    """等待态也要能正常挂断——hangup 只作用于当前通话，不是对外副作用。"""
    service, _modem = _service()
    session = service.session
    registry = session._build_tools("inbound")
    try:
        result = registry.dispatch("hangup_call", {})
    finally:
        with session._hangup_lock:
            session._cancel_hangup_timer()

    assert result["success"] is True


def test_triage_pending_does_not_gate_in_call_dtmf() -> None:
    """DTMF 按键作用于当前通话本身（IVR 导航），等待态不拦。"""
    service, _modem = _service()
    session = service.session
    registry = session._build_tools("inbound")

    result = registry.dispatch("send_dtmf", {"digits": "1"})

    assert result.get("code") != "TRIAGE_PENDING_BLOCKED"


def test_gate_only_intercepts_tools_marked_external_effect() -> None:
    """门禁按注册时的 external_effect 性质标记生效，与工具名无关。"""
    registry = ToolRegistry()
    spec_external = {
        "type": "function",
        "function": {"name": "tool_a", "parameters": {}},
    }
    spec_in_call = {
        "type": "function",
        "function": {"name": "tool_b", "parameters": {}},
    }
    registry.register(
        spec_external, lambda args: {"success": True}, external_effect=True
    )
    registry.register(spec_in_call, lambda args: {"success": True})
    registry.set_external_effect_gate(
        lambda tool: {"success": False, "code": "TRIAGE_PENDING_BLOCKED"}
    )

    assert registry.dispatch("tool_a", {})["code"] == "TRIAGE_PENDING_BLOCKED"
    assert registry.dispatch("tool_b", {})["success"] is True


def test_gate_result_is_json_serializable() -> None:
    """拒绝结果会作为 function_call_output 回传模型，必须可 JSON 序列化。"""
    service, _modem = _service()
    session = service.session
    registry = session._build_tools("inbound")

    result = registry.dispatch("send_sms", {"content": "x"})

    json.dumps(result, ensure_ascii=False)
    # 文案是给模型的行为指令，不描述系统状态（见
    # test_blocked_result_instructs_behavior_and_forbids_narrating_system_state）
    assert "不要向来电者提起" in result["message"]


def test_all_outward_side_effect_tools_are_marked() -> None:
    """机制层性质声明的完整性（评审缺口）：ask_owner 等对外副作用工具的
    external_effect 标记缺失时测试要红——门禁按标记生效，漏标即漏拦。"""
    from fakes import FakeModem

    from agentcall.agents.tools import ASK_OWNER_SPEC, SEND_SMS_SPEC
    from agentcall.call_tools import CallTools

    tools = CallTools(
        FakeModem(),  # type: ignore[arg-type]
        hub=None,
        get_caller=lambda: None,
        get_record=lambda: None,
        schedule_hangup=lambda: None,
        direction="outbound",
    )
    registry = tools.register()
    marked = registry.external_effect_tool_names()
    # request_owner_takeover 的生产注册点在 call_agent._build_tools，其打标
    # 由 test_inbound_triage_wiring 侧的门禁行为测试覆盖；这里审计 CallTools
    # 注册点的完整性。
    for spec in (SEND_SMS_SPEC, ASK_OWNER_SPEC):
        assert spec["function"]["name"] in marked


def test_blocked_result_instructs_behavior_and_forbids_narrating_system_state() -> None:
    """拒绝文案是给模型的行为指令，不是给它念的台词（真机 2026-08-21）。

    原文案描述系统状态（「分诊尚未放行…等待系统放行」），模型就如实转述给
    了来电者：「the system is currently blocking that kind of message, so I
    can't send it yet」——对来电者既莫名其妙又暴露实现细节。
    """
    service, _modem = _service()
    session = service.session
    registry = session._build_tools("inbound")

    blocked = registry.dispatch("send_sms", {"to": "owner", "content": "x"})

    assert blocked["code"] == "TRIAGE_PENDING_BLOCKED"
    message = blocked["message"]
    # 必须明确禁止把系统状态说出口
    assert "不要向来电者提起" in message
    assert "不要说系统限制" in message
    assert "照常继续" in message
    # 不得再出现描述系统内部状态的措辞——那正是被模型念出去的东西
    for leak in ("分诊", "系统放行", "限制话术"):
        assert leak not in message
