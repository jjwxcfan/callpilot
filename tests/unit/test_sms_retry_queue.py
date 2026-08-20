"""#127 通话后短信补发队列：CallSession 侧入队/收尾补发回归（FakeModem 驱动）。

真机坑：SIM7600 语音通话中（CPCMREG in-band 音频）AT+CMGS 必被模组拒，
0.6s 快速失败——「通话中转告机主」全踩。回归覆盖主线与评审补丁：
通话中失败→queued 语义→收尾补发成功；补发也失败→日志留痕不炸收尾；
白名单不过→根本不入队；背靠背来电→条目轮转到下一次通话收尾（必修 1）；
owner 口信终态失败→system 事件兜底通知（必修 2）；收尾后迟到入队→
立即补发不留队列等死（建议修 3）。
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
    join_worker(session)


def join_worker(session: CallSession) -> None:
    thread = session._pending_sms_thread
    if thread is not None:
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "补发线程未在超时内结束"


def pending_snapshot(session: CallSession) -> list[tuple[str, str, bool, int]]:
    with session._pending_sms_lock:
        return [
            (e.number, e.content, e.owner_relay, e.rounds)
            for e in session._pending_sms
        ]


def sms_out_events(hub: EventHub) -> list[dict]:
    return [e for e in hub.history() if e.get("type") == "sms_out"]


def system_events(hub: EventHub) -> list[dict]:
    return [e for e in hub.history() if e.get("type") == "system"]


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
    assert pending_snapshot(session) == [
        ("+16505550100", "call me back", False, 0)
    ]
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
    assert pending_snapshot(session) == []  # 补发成功后队列即空


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
    assert [e["status"] for e in sms_out_events(hub)] == ["sent"]


def test_flush_during_next_call_requeues_for_later(caplog):
    """必修 1（背靠背来电）：补发撞上通话中→不发 CMGS，条目轮转下一次收尾。"""
    hub = make_hub()
    modem = FakeModem()
    session = make_session(modem, hub)
    session._queue_pending_sms("+16505550111", "owner memo")
    modem.trigger_call_connected()  # 下一通已经接起，模组又在通话中

    with caplog.at_level("INFO", logger="agentcall.call_agent"):
        flush_and_join(session)

    # 通话中不做无谓的 CMGS 尝试，条目带轮数回到队列。
    assert [c for c in modem.calls if c[0] == "send_sms"] == []
    assert pending_snapshot(session) == [("+16505550111", "owner memo", False, 1)]
    assert sms_out_events(hub) == []  # 非终态不发事件
    assert any("转入下一次通话收尾" in r.getMessage() for r in caplog.records)

    # 下一通结束、模组空闲：这条口信在那次收尾里送达。
    modem.hangup()
    flush_and_join(session)
    assert [c for c in modem.calls if c[0] == "send_sms"] == [
        ("send_sms", ("+16505550111", "owner memo"))
    ]
    assert [e["status"] for e in sms_out_events(hub)] == ["sent"]
    assert pending_snapshot(session) == []


def test_flush_failure_requeues_until_rounds_exhausted(caplog):
    """必修 1（上限）：连败按轮转续试，轮数用尽才终态放弃，绝不无限轮回。"""
    hub = make_hub()
    modem = FakeModem()
    modem.sms_should_succeed = False
    session = make_session(modem, hub)
    session._pending_sms_max_rounds = 2
    session._queue_pending_sms("+16505550111", "owner memo")

    with caplog.at_level("WARNING", logger="agentcall.call_agent"):
        flush_and_join(session)  # 第 1 轮：失败→回队列
        assert pending_snapshot(session) == [
            ("+16505550111", "owner memo", False, 1)
        ]
        assert sms_out_events(hub) == []
        flush_and_join(session)  # 第 2 轮：轮数用尽→终态失败

    sends = [c for c in modem.calls if c[0] == "send_sms"]
    assert len(sends) == 4  # 每轮原次 + 重试一次，共 2 轮
    assert any("轮用尽" in r.getMessage() for r in caplog.records)
    assert [e["status"] for e in sms_out_events(hub)] == ["failed"]
    assert pending_snapshot(session) == []  # 终态后不再回队列


def test_owner_relay_terminal_failure_notifies_owner():
    """必修 2：owner 转告口信终态失败→system 事件兜底通知（不复述正文）。"""
    hub = make_hub()
    modem = FakeModem()
    modem.sms_should_succeed = False
    session = make_session(modem, hub)
    session._pending_sms_max_rounds = 1  # 一轮即终态，直击通知路径
    session._queue_pending_sms("+16505550111", "caller asks a callback", True)

    flush_and_join(session)

    assert [e["status"] for e in sms_out_events(hub)] == ["failed"]
    notices = system_events(hub)
    assert len(notices) == 1
    assert "口信" in notices[0]["text"]
    assert "caller asks a callback" not in notices[0]["text"]  # 不复述正文


def test_non_owner_terminal_failure_has_no_system_notice():
    """对照：非 owner 口信终态失败只有 sms_out failed，不打扰机主。"""
    hub = make_hub()
    modem = FakeModem()
    modem.sms_should_succeed = False
    session = make_session(modem, hub)
    session._pending_sms_max_rounds = 1
    session._queue_pending_sms("+16505550100", "reply to caller")

    flush_and_join(session)

    assert [e["status"] for e in sms_out_events(hub)] == ["failed"]
    assert system_events(hub) == []


def test_late_enqueue_after_flush_sends_immediately():
    """建议修 3：收尾 flush 之后迟到的入队不留队列等死，立即起线程补发。"""
    hub = make_hub()
    modem = FakeModem()
    session = make_session(modem, hub)

    session._flush_pending_sms()  # 空队列收尾：关闭入队（accepting=False）
    session._queue_pending_sms("+16505550100", "late message")

    join_worker(session)  # 迟到条目由一次性线程立即补发

    assert [c for c in modem.calls if c[0] == "send_sms"] == [
        ("send_sms", ("+16505550100", "late message"))
    ]
    assert [e["status"] for e in sms_out_events(hub)] == ["sent"]
    assert pending_snapshot(session) == []


def test_flush_send_exception_handled(caplog):
    """补发时串口抛异常同样兜住：留痕 + failed 事件，不炸线程。"""
    hub = make_hub()
    modem = FakeModem()
    modem.send_sms = lambda number, text: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("串口断开")
    )
    session = make_session(modem, hub)
    session._pending_sms_max_rounds = 1
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
    assert pending_snapshot(session) == []
    assert [c for c in modem.calls if c[0] == "send_sms"] == []


def test_flush_with_empty_queue_is_noop():
    """空队列收尾：不起线程、不发事件。"""
    hub = make_hub()
    modem = FakeModem()
    session = make_session(modem, hub)

    session._flush_pending_sms()

    assert session._pending_sms_thread is None
    assert sms_out_events(hub) == []


def test_next_call_reopens_queue_without_wiping_carryover():
    """下一通开始重新开门但不清空：轮转条目留到该通收尾续试。"""
    modem = FakeModem()
    session = make_session(modem)
    with session._pending_sms_lock:
        session._pending_sms_accepting = False  # 上一通收尾后的状态
    from agentcall.call_agent import _PendingSms

    with session._pending_sms_lock:
        session._pending_sms.append(
            _PendingSms(number="+16505550111", content="carryover", rounds=1)
        )

    # 模拟 _handle_call 开头的开门动作（不整跑通话主循环）。
    with session._pending_sms_lock:
        session._pending_sms_accepting = True

    assert pending_snapshot(session) == [("+16505550111", "carryover", False, 1)]
    session._queue_pending_sms("+16505550100", "new entry")
    assert pending_snapshot(session) == [
        ("+16505550111", "carryover", False, 1),
        ("+16505550100", "new entry", False, 0),
    ]
