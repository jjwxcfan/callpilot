"""按键护窗：DTMF 期间上行只留双音，Agent 语音让路。

回归（真机 2026-08-03 10:47 拨 10086，见 #45 / WIL-49 评论）：
模型一边说「好的，我来照你的选项操作。这边先按一下对应的键」一边按键，
4 次按键本机全部 success，10086 的 IVR 菜单一次都没推进。

根因在信号层：`_send_dtmf_raw` 把双音 `put` 进 `_outgoing_audio`——那是
Agent TTS 语音**同一条**上行队列。双音被夹在语音中间送出，对端听到的是
语音与双音的混叠，两者都识别不出。修法是按键时清空在途语音，并在按键
前后开一段护窗，护窗内丢弃 Agent 下行。
"""

from __future__ import annotations

import time

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallSession


def make_session() -> CallSession:
    return CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )


def drain(session: CallSession) -> list[bytes]:
    chunks = []
    while not session._outgoing_audio.empty():
        chunks.append(session._outgoing_audio.get_nowait())
    return chunks


@pytest.mark.parametrize("mode", ["inband", "qvts"])
def test_queued_agent_speech_is_dropped_before_the_tone(monkeypatch, mode):
    """在途语音必须先清空——否则双音排在一整段 TTS 之后才发出。"""
    monkeypatch.setenv("DTMF_MODE", mode)
    session = make_session()
    session._outgoing_audio.put(b"\x11\x11" * 400)
    session._outgoing_audio.put(b"\x22\x22" * 400)

    ok, resolved = session._send_dtmf_raw("1", source="agent_tool")

    assert ok and resolved == mode
    leftover = drain(session)
    assert b"\x11\x11" * 400 not in leftover, "按键前的 Agent 语音必须被丢弃"
    if mode == "inband":
        assert len(leftover) == 1, "inband 下队列里只应剩双音本身"
    else:
        assert leftover == [], "qvts 走 AT 带外，队列应被清空且不入双音"


@pytest.mark.parametrize("mode", ["inband", "qvts"])
def test_guard_window_opens_after_a_keypress(monkeypatch, mode):
    monkeypatch.setenv("DTMF_MODE", mode)
    monkeypatch.setenv("DTMF_GUARD_MS", "400")
    session = make_session()
    assert session._dtmf_guard_until == 0.0

    # 断言相对「按键前那一刻」，而不是相对「断言执行那一刻」：机器卡顿只会
    # 让截止时刻更靠后，不会让这条断言假红（Codex P2）。
    before = time.monotonic()
    session._send_dtmf_raw("1", source="agent_tool")

    assert session._dtmf_guard_until >= before + 0.4


def test_inband_guard_covers_the_tone_duration_too(monkeypatch):
    """双音本身也要算进护窗，否则语音会紧跟在双音尾巴上接着送。"""
    monkeypatch.setenv("DTMF_MODE", "inband")
    monkeypatch.setenv("DTMF_GUARD_MS", "0")
    monkeypatch.setenv("DTMF_TONE_MS", "200")
    session = make_session()

    before = time.monotonic()
    session._send_dtmf_raw("12", source="agent_tool")

    assert session._dtmf_guard_until >= before + 0.4, "两位 200ms 双音 = 0.4s"


def test_guard_can_be_disabled(monkeypatch):
    """DTMF_GUARD_MS=0 且带外发送时退回旧行为，便于对照排查。"""
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "0")
    session = make_session()

    session._send_dtmf_raw("1", source="agent_tool")

    assert session._dtmf_guard_until <= time.monotonic()


def test_guard_is_installed_before_the_blocking_at_send(monkeypatch):
    """Codex P1：qvts 的 send_dtmf 是阻塞 AT 往返，护窗必须先装。

    否则 realtime 在 AT 往返这段空档产出的语音会正好落进按键窗口——那正是
    真机上「按了 4 次、菜单没动」的形态。这里用一个在 send_dtmf 期间回调
    音频的假模组来复现那段空档。
    """
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "400")

    class SlowModem(FakeModem):
        """模拟 AT 往返期间 realtime 仍在推下行音频。"""

        during_send: list = []

        def send_dtmf(self, digits: str) -> bool:
            for callback in self.during_send:
                callback(b"\x44\x44" * 480)
            return super().send_dtmf(digits)

    modem = SlowModem()
    session = CallSession(
        modem=modem,  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )
    session._set_active(True)
    handler = session._make_agent_audio_handler(FakeAgent(), FakeAudioBridge(), None)
    modem.during_send = [handler]

    ok, _mode = session._send_dtmf_raw("1", source="agent_tool")

    assert ok
    assert drain(session) == [], "AT 往返期间产出的语音不得进入上行"


def test_failed_send_restores_the_previous_guard(monkeypatch):
    """按键没发出去就不该白白哑掉 Agent 一整个护窗。"""
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "5000")

    class DeadModem(FakeModem):
        def send_dtmf(self, digits: str) -> bool:
            return False

    session = CallSession(
        modem=DeadModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )

    ok, _mode = session._send_dtmf_raw("1", source="agent_tool")

    assert not ok
    assert session._dtmf_guard_until == 0.0, "失败要还原护窗，而不是留下 5s 静音"


def test_agent_audio_is_discarded_inside_the_guard_window(monkeypatch):
    """核心断言：护窗内 Agent 下行不得进入上行队列。

    这是「模型边说边按」的直接修法——没有它，前面清空队列只是把混叠
    推迟几十毫秒，realtime 会立刻把下一段 TTS 补进来。
    """
    monkeypatch.setenv("DTMF_MODE", "inband")
    monkeypatch.setenv("DTMF_GUARD_MS", "400")
    session = make_session()
    session._set_active(True)  # 生成号闸门要求会话在通话中
    bridge = FakeAudioBridge()
    handler = session._make_agent_audio_handler(FakeAgent(), bridge, None)

    speech = b"\x33\x33" * 480
    handler(speech)
    assert drain(session), "护窗外的 Agent 语音应正常入队（对照组）"

    session._send_dtmf_raw("1", source="agent_tool")
    drain(session)  # 丢掉双音，只看之后的语音

    handler(speech)
    assert drain(session) == [], "护窗内的 Agent 语音必须被丢弃"

    session._dtmf_guard_until = 0.0
    handler(speech)
    assert drain(session), "护窗结束后必须恢复送话"


def test_guard_is_reset_per_call(monkeypatch):
    """护窗是 monotonic 时刻：背靠背通话若不重置，新通话开场白会被静音。"""
    monkeypatch.setenv("DTMF_MODE", "inband")
    monkeypatch.setenv("DTMF_GUARD_MS", "5000")
    session = make_session()
    session._send_dtmf_raw("1", source="agent_tool")
    assert session._dtmf_guard_until > time.monotonic()

    session._cancel_spoken_dtmf_followups(clear_recent=True)

    assert session._dtmf_guard_until == 0.0
