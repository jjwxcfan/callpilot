"""#127 通话后短信补发队列：CallSession 侧入队/收尾补发回归（FakeModem 驱动）。

真机坑：SIM7600 语音通话中（CPCMREG in-band 音频）AT+CMGS 必被模组拒，
0.6s 快速失败——「通话中转告机主」全踩。回归覆盖三条主线：
通话中失败→queued 语义→收尾补发成功；补发也失败→日志留痕不炸收尾；
白名单不过→根本不入队。
"""

from __future__ import annotations

import asyncio

from fakes import FakeModem

from agentcall.call_agent import CallAgentService, CallSession
from agentcall.events import EventHub


def make_session(
    modem: FakeModem, hub: EventHub | None = None
) -> CallSession:
    service = CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="qwen",
        hub=hub,
        modem=modem,  # type: ignore[arg-type]  # FakeModem 与 Eg25Modem 同形
    )
    session = service.session
    # 工具的 effect_guard 只在会话活跃时放行——单测模拟「通话进行中」。
    session._active = True
    # 单测不等真实静置/重试间隔。
    session._pending_sms_flush_delay = 0.0
    session._pending_sms_retry_delay = 0.0
    return session


def make_hub() -> EventHub:
    return EventHub(asyncio.new_event_loop())


def flush_and_join(session: CallSession) -> None:
    session._flush_pending_sms()
    thread = session._pending_sms_thread
    if thread is not None:
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "补发线程未在超时内结束"


def sms_out_events(hub: EventHub) -> list[dict]:
    return [e for e in hub.history() if e.get("type") == "sms_out"]


def test_in_call_failure_queued_then_flushed_after_call():
    """主线回归：通话中发送失败→回传 queued→收尾补发成功→事件 sent。"""
    hub = make_hub()
    modem = FakeModem()
    modem.sms_should_succeed = False  # 模拟通话中 CMGS 被模组拒
    session = make_session(modem, hub)
    session.current_caller = "+16505550100"

    registry = session._build_tools("inbound")
    result = registry.dispatch("send_sms", {"content": "call me back"})

    assert result["success"] is True
    assert result["queued"] is True
    with session._pending_sms_lock:
        assert session._pending_sms == [("+16505550100", "call me back")]
    assert sms_out_events(hub) == []  # 入队阶段不发事件

    modem.sms_should_succeed = True  # 挂断后模组恢复空闲，可发送
    flush_and_join(session)

    sends = [c for c in modem.calls if c[0] == "send_sms"]
    assert sends == [
        ("send_sms", ("+16505550100", "call me back")),  # 通话中的首次尝试
        ("send_sms", ("+16505550100", "call me back")),  # 收尾补发
    ]
    events = sms_out_events(hub)
    assert len(events) == 1
    assert events[0]["status"] == "sent"
    assert events[0]["number"] == "+16505550100"
    with session._pending_sms_lock:
        assert session._pending_sms == []  # 队列按通话生命周期清空


def test_flush_retries_once_then_succeeds():
    """补发首次失败自动重试一次：第二次成功即算送达。"""
    hub = make_hub()
    modem = FakeModem()
    session = make_session(modem, hub)
    attempts: list[str] = []

    def flaky(number: str, text: str) -> bool:
        attempts.append(number)
        return len(attempts) >= 2

    modem.send_sms = flaky  # type: ignore[method-assign]
    session._queue_pending_sms("+16505550111", "owner memo")

    flush_and_join(session)

    assert attempts == ["+16505550111", "+16505550111"]
    events = sms_out_events(hub)
    assert [e["status"] for e in events] == ["sent"]


def test_flush_failure_logs_and_does_not_raise(caplog):
    """补发（含一次重试）仍失败：日志留痕 + failed 事件，绝不外抛炸收尾。"""
    hub = make_hub()
    modem = FakeModem()
    modem.sms_should_succeed = False
    session = make_session(modem, hub)
    session._queue_pending_sms("+16505550111", "owner memo")

    with caplog.at_level("WARNING", logger="agentcall.call_agent"):
        flush_and_join(session)  # 不抛异常本身即是断言之一

    sends = [c for c in modem.calls if c[0] == "send_sms"]
    assert len(sends) == 2  # 原次 + 重试一次，不无限重试
    assert any("补发短信失败" in r.getMessage() for r in caplog.records)
    events = sms_out_events(hub)
    assert [e["status"] for e in events] == ["failed"]
    with session._pending_sms_lock:
        assert session._pending_sms == []


def test_flush_send_exception_handled(caplog):
    """补发时串口抛异常同样兜住：留痕 + failed 事件，不炸线程。"""
    hub = make_hub()
    modem = FakeModem()
    modem.send_sms = lambda number, text: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("串口断开")
    )
    session = make_session(modem, hub)
    session._queue_pending_sms("+16505550111", "owner memo")

    with caplog.at_level("WARNING", logger="agentcall.call_agent"):
        flush_and_join(session)

    assert any("补发短信异常" in r.getMessage() for r in caplog.records)
    assert [e["status"] for e in sms_out_events(hub)] == ["failed"]


def test_disallowed_target_never_enters_queue():
    """白名单不过（陌生第三方号码）：当场拒绝，队列全程为空。"""
    modem = FakeModem()
    modem.sms_should_succeed = False
    session = make_session(modem)
    session.current_caller = "+16505550100"

    registry = session._build_tools("inbound")
    result = registry.dispatch(
        "send_sms", {"to": "+19995550199", "content": "spam relay"}
    )

    assert result["success"] is False
    assert "queued" not in result
    assert "只能" in result["message"]  # 确认走的是白名单拒绝，不是别的失败
    with session._pending_sms_lock:
        assert session._pending_sms == []
    assert [c for c in modem.calls if c[0] == "send_sms"] == []


def test_flush_with_empty_queue_is_noop():
    """空队列收尾：不起线程、不发事件。"""
    hub = make_hub()
    modem = FakeModem()
    session = make_session(modem, hub)

    session._flush_pending_sms()

    assert session._pending_sms_thread is None
    assert sms_out_events(hub) == []
