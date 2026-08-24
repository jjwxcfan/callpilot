"""WinRtPhoneModem 单测：注入假 WinRT 命名空间驱动，无需 winsdk/真机。

覆盖 duck-type 契约的关键行为：来电/接通/挂断事件流转（CLCC 等价轮询）、
answer/dial/hangup/send_dtmf 对 WinRT 对象的调用、SIM 身份 fail-closed 与
CARRIER_HOTLINE 覆盖、轮询失联降级。
"""

from __future__ import annotations

from enum import IntEnum

import pytest

from agentcall.modem_winrt_phone import (
    CALL_ENDED,
    CALL_INCOMING,
    CALL_TALKING,
    WinRtPhoneModem,
)


class _DtmfKey(IntEnum):
    D0 = 0; D1 = 1; D2 = 2; D3 = 3; D4 = 4  # noqa: E702
    D5 = 5; D6 = 6; D7 = 7; D8 = 8; D9 = 9  # noqa: E702
    STAR = 10
    POUND = 11


class _Playback(IntEnum):
    PLAY = 0
    DO_NOT_PLAY = 1


class FakeCallInfo:
    def __init__(self, number: str) -> None:
        self.phone_number = number


class FakeCall:
    def __init__(self, call_id: str, status: int, number: str = "") -> None:
        self.call_id = call_id
        self.status = status
        self._number = number
        self.actions: list[object] = []

    def get_phone_call_info(self) -> FakeCallInfo:
        return FakeCallInfo(self._number)

    def accept_incoming(self) -> None:
        self.actions.append("accept")
        self.status = CALL_TALKING

    def end(self) -> None:
        self.actions.append("end")
        self.status = CALL_ENDED

    def send_dtmf_key(self, key: object, playback: object = None) -> None:
        self.actions.append(("dtmf", int(key)))  # type: ignore[call-overload]


class FakeCallsResult:
    def __init__(self, calls: list[FakeCall]) -> None:
        self.all_active_phone_calls = calls


class FakeLine:
    def __init__(self) -> None:
        self.display_name = "Pixel 7"
        self.can_dial = True
        self.calls: list[FakeCall] = []
        self.dialed: list[tuple[str, str]] = []

    def get_all_active_phone_calls(self) -> FakeCallsResult:
        return FakeCallsResult(list(self.calls))

    def dial(self, number: str, display_name: str) -> None:
        self.dialed.append((number, display_name))


class FakeCallsNamespace:
    DtmfKey = _DtmfKey
    DtmfToneAudioPlayback = _Playback


@pytest.fixture()
def modem() -> tuple[WinRtPhoneModem, FakeLine]:
    m = WinRtPhoneModem(calls_module=FakeCallsNamespace())
    line = FakeLine()
    m._line = line  # 绕过 connect 的 asyncio/winsdk 路径，直接注入线路
    return m, line


def _events_recorder(m: WinRtPhoneModem) -> dict[str, list]:
    events: dict[str, list] = {"ring": [], "connected": [], "hangup": []}
    m.on_ring(lambda num: events["ring"].append(num))
    m.on_call_connected(lambda num: events["connected"].append(num))
    m.on_hangup(lambda: events["hangup"].append(True))
    return events


def test_incoming_to_talking_to_ended_event_flow(modem) -> None:
    m, line = modem
    events = _events_recorder(m)

    call = FakeCall("1", CALL_INCOMING, "+15105550100")
    line.calls = [call]
    m._poll_once()
    assert events["ring"] == ["+15105550100"]
    assert not m.is_call_connected()

    call.status = CALL_TALKING
    m._poll_once()
    assert events["connected"] == ["+15105550100"]
    assert m.is_call_connected()

    call.status = CALL_ENDED
    m._poll_once()
    assert events["hangup"] == [True]
    assert not m.is_call_connected()
    # 通话彻底消失后不再重复触发
    line.calls = []
    m._poll_once()
    assert events["hangup"] == [True]


def test_ring_fires_once_per_call(modem) -> None:
    m, line = modem
    events = _events_recorder(m)
    line.calls = [FakeCall("1", CALL_INCOMING, "+15105550100")]
    m._poll_once()
    m._poll_once()
    assert events["ring"] == ["+15105550100"]


def test_second_call_rings_after_first_hangup(modem) -> None:
    m, line = modem
    events = _events_recorder(m)
    line.calls = [FakeCall("1", CALL_INCOMING, "+1111")]
    m._poll_once()
    line.calls = []
    m._poll_once()  # hangup + 通知集合清空
    line.calls = [FakeCall("2", CALL_INCOMING, "+2222")]
    m._poll_once()
    assert events["ring"] == ["+1111", "+2222"]


def test_answer_accepts_incoming_call(modem) -> None:
    m, line = modem
    call = FakeCall("1", CALL_INCOMING)
    line.calls = [call]
    m.answer()
    assert "accept" in call.actions


def test_answer_without_incoming_is_noop(modem) -> None:
    m, line = modem
    m.answer()  # 不抛异常


def test_dial_goes_through_line(modem) -> None:
    m, line = modem
    assert m.dial("611") == "OK"
    assert line.dialed == [("611", "611")]


def test_dial_without_line_raises() -> None:
    m = WinRtPhoneModem(calls_module=FakeCallsNamespace())
    with pytest.raises(RuntimeError):
        m.dial("611")


def test_hangup_ends_all_active_calls(modem) -> None:
    m, line = modem
    talking = FakeCall("1", CALL_TALKING)
    incoming = FakeCall("2", CALL_INCOMING)
    ended = FakeCall("3", CALL_ENDED)
    line.calls = [talking, incoming, ended]
    m.hangup()
    assert "end" in talking.actions
    assert "end" in incoming.actions
    assert "end" not in ended.actions


def test_send_dtmf_maps_digits_star_pound(modem) -> None:
    m, line = modem
    call = FakeCall("1", CALL_TALKING)
    line.calls = [call]
    assert m.send_dtmf("1*#") is True
    assert call.actions == [("dtmf", 1), ("dtmf", 10), ("dtmf", 11)]


def test_send_dtmf_rejects_invalid_and_no_call(modem) -> None:
    m, line = modem
    # HFP/WinRT 无 A-D 键
    assert m.send_dtmf("A") is False
    assert m.send_dtmf("") is False
    # 无通话中的呼叫
    assert m.send_dtmf("1") is False


def test_send_sms_unsupported(modem) -> None:
    m, _line = modem
    assert m.send_sms("+15105550100", "hi") is False


def test_sim_identity_fail_closed_without_hotline(modem, monkeypatch) -> None:
    m, _line = modem
    monkeypatch.setenv("CARRIER_HOTLINE", "")
    m.refresh_sim_identity()
    sim = m.sim_identity
    assert sim.present is True
    assert sim.registered is True
    assert sim.carrier == "未知"
    assert sim.service_number == ""  # dial_guard 会拦已知客服号——fail-closed


def test_sim_identity_hotline_override(modem, monkeypatch) -> None:
    m, _line = modem
    monkeypatch.setenv("CARRIER_HOTLINE", "611")
    notified = []
    m.on_sim_identity(notified.append)
    m.refresh_sim_identity()
    assert m.sim_identity.service_number == "611"
    assert notified and notified[0].service_number == "611"


def test_poll_failures_degrade_to_offline(modem) -> None:
    m, line = modem
    states: list[bool] = []
    m.on_connection_state(states.append)
    m._set_connection(True)

    def boom() -> FakeCallsResult:
        raise OSError("line gone")

    line.get_all_active_phone_calls = boom  # type: ignore[method-assign]
    m._poll_stop.clear()
    # 直接驱动循环体：失败达到阈值应置离线并退出循环
    m._poll_interval = 0.0
    m._poll_loop()
    assert states[-1] is False
    assert not m.is_connected()


def test_pcm_ready_and_voice_init_are_trivial(modem) -> None:
    m, _line = modem
    m.initialize_for_voice("hfp")
    assert m.pcm_ready() is True


def test_start_listener_idempotent(modem) -> None:
    m, _line = modem
    m.start_listener()
    first = m._poll_thread
    m.start_listener()
    assert m._poll_thread is first
    m.stop_listener()
    assert m._poll_thread is None
