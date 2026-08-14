"""barge-in 与按键护窗 / 自激兜底的交互（WIL-94 坑 1 / 坑 4 的测试锁）。

坑 1（护窗让位）：inband 双音与 Agent 语音共用 ``_outgoing_audio``。IVR 对端
持续出声会触发 provider 的 speech_started——若护窗期内照常清队列，排队中的
双音会被清掉，WIL-49 修好的 IVR 导航回归到修复前。修法：护窗期内
``_handle_peer_barge_in`` 返回 False（不清、也让 provider 侧不 cancel）。

坑 4（自激兜底）：barge-in 的前提是「模组无回采」的一次性实测；前提失效时
AI 会被自己的回声反复「打断」。指纹是「回复刚出声即被打断且连续多次」——
连续 3 次即本通退回半双工，而不是放任自循环。
"""

from __future__ import annotations

import time

import pytest
from fakes import FakeAudioBridge, FakeModem

from agentcall.call_agent import CallSession


def make_session(barge_in: bool = True) -> CallSession:
    session = CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )
    # 正常由 _handle_call 每通开始时从 config 读取；单测直接置位。
    session._barge_in = barge_in
    return session


def queued(session: CallSession) -> list[bytes]:
    chunks = []
    while not session._outgoing_audio.empty():
        chunks.append(session._outgoing_audio.get_nowait())
    return chunks


# ---- 坑 1：护窗期让位 ----


def test_guard_window_rejects_barge_in_and_keeps_the_tone(monkeypatch):
    """护窗期内对端开口：不接受打断，排队中的双音必须原样活着。"""
    monkeypatch.setenv("DTMF_MODE", "inband")
    monkeypatch.setenv("DTMF_GUARD_MS", "400")
    session = make_session()
    bridge = FakeAudioBridge()
    ok, _mode = session._send_dtmf_raw("1", source="agent_tool")
    assert ok and time.monotonic() < session._dtmf_guard_until

    accepted = session._handle_peer_barge_in(bridge)

    assert accepted is False, "护窗期内 barge-in 必须让位（WIL-49 不回归）"
    assert len(queued(session)) == 1, "双音必须还在队列里，不能被清掉"


def test_guard_installed_mid_flight_is_caught_by_the_in_lock_recheck():
    """竞态双检：首查时护窗未装、拿到 _media_lock 时刚装上 → 仍要让位。

    模拟 _send_dtmf_raw 在打断处理中途完成「装护窗 + 入队双音」：用一个
    进锁瞬间装上护窗的假锁复现这个时序，锁内重查必须把双音保下来。
    """
    session = make_session()
    bridge = FakeAudioBridge()
    session._outgoing_audio.put(b"\x77\x77" * 100)  # 竞态中刚入队的双音
    session._turn_audio_started_at = time.monotonic() - 10.0

    class GuardInstallingLock:
        def __enter__(self) -> "GuardInstallingLock":
            session._dtmf_guard_until = time.monotonic() + 1.0
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    session._media_lock = GuardInstallingLock()  # type: ignore[assignment]

    accepted = session._handle_peer_barge_in(bridge)

    assert accepted is False, "锁内重查必须发现护窗并让位"
    assert len(queued(session)) == 1, "竞态中入队的双音必须活着"


def test_barge_in_disabled_rejects_without_touching_queue():
    session = make_session(barge_in=False)
    bridge = FakeAudioBridge()
    session._outgoing_audio.put(b"\x11\x11" * 100)

    assert session._handle_peer_barge_in(bridge) is False
    assert len(queued(session)) == 1


# ---- 正常打断：让路 ----


def test_barge_in_clears_backlog_and_accepts():
    """护窗外的真插话：清桥内积压 + 清队列，返回接受（provider 侧才 cancel）。"""
    session = make_session()
    bridge = FakeAudioBridge()
    bridge.pending_bytes = 1600
    session._outgoing_audio.put(b"\x11\x11" * 100)
    session._outgoing_audio.put(b"\x22\x22" * 100)
    # 回复出声已久（10s 前）——真人抢话，不是回声。
    session._turn_audio_started_at = time.monotonic() - 10.0

    accepted = session._handle_peer_barge_in(bridge)

    assert accepted is True
    assert queued(session) == []
    assert bridge.discarded_bytes == 1600


def test_late_interruption_resets_echo_strikes():
    session = make_session()
    bridge = FakeAudioBridge()
    session._barge_in_echo_strikes = 2
    session._outgoing_audio.put(b"\x11\x11" * 100)
    session._turn_audio_started_at = time.monotonic() - 10.0

    assert session._handle_peer_barge_in(bridge) is True
    assert session._barge_in_echo_strikes == 0


# ---- 坑 4：自激兜底 ----


def test_echo_strikes_revert_to_half_duplex():
    """连续 3 次「刚出声即被打断」→ 本通退回半双工，第 3 次起拒绝且不再清音频。"""
    session = make_session()
    bridge = FakeAudioBridge()

    for strike in (1, 2):
        session._outgoing_audio.put(b"\x11\x11" * 100)
        session._turn_audio_started_at = time.monotonic()
        assert session._handle_peer_barge_in(bridge) is True, f"第 {strike} 次仍应让路"
        assert session._barge_in_echo_strikes == strike

    session._outgoing_audio.put(b"\x33\x33" * 100)
    session._turn_audio_started_at = time.monotonic()
    accepted = session._handle_peer_barge_in(bridge)

    assert accepted is False, "第 3 次即判定疑似自激，不再接受打断"
    assert session._barge_in is False, "本通必须退回半双工"
    assert len(queued(session)) == 1, "判定自激的这次不应再清音频"
    # 之后的事件一律拒绝（半双工语义恢复）。
    session._outgoing_audio.put(b"\x44\x44" * 100)
    assert session._handle_peer_barge_in(bridge) is False
    assert len(queued(session)) == 1


def test_quiet_turn_start_does_not_count_strikes():
    """AI 没在出声（无积压无队列）时的对端开口是正常轮次起点，不计自激。"""
    session = make_session()
    bridge = FakeAudioBridge()
    session._turn_audio_started_at = time.monotonic()

    assert session._handle_peer_barge_in(bridge) is True
    assert session._barge_in_echo_strikes == 0


@pytest.mark.parametrize("mode", ["inband", "qvts"])
def test_guard_expiry_restores_barge_in(monkeypatch, mode):
    """护窗过期后 barge-in 恢复正常让路——让位是暂时的，不是本通失效。"""
    monkeypatch.setenv("DTMF_MODE", mode)
    monkeypatch.setenv("DTMF_GUARD_MS", "0")
    monkeypatch.setenv("DTMF_TONE_MS", "1")
    session = make_session()
    bridge = FakeAudioBridge()
    session._send_dtmf_raw("1", source="agent_tool")
    # 等护窗（0ms 护窗 + 1ms 双音时长）自然过期。
    deadline = time.monotonic() + 1.0
    while time.monotonic() < session._dtmf_guard_until:
        assert time.monotonic() < deadline, "护窗应在毫秒级过期"
        time.sleep(0.005)
    queued(session)  # 清掉残留双音，模拟已播完
    session._outgoing_audio.put(b"\x55\x55" * 100)
    session._turn_audio_started_at = time.monotonic() - 10.0

    assert session._handle_peer_barge_in(bridge) is True
    assert queued(session) == []
