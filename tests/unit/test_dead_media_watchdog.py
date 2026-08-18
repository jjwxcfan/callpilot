"""媒体死亡看门狗：对端挂断而模组没察觉时，主动收尾（WIL-100）。

真机 2026-08-06 19:09：机主挂断后会话**僵尸十分钟**——Realtime 连接空转，
录音目录一直是空的（`finish()` 只在收尾时调用），手动打 /api/call/hangup
才落盘。本次真机验证的证据差点全丢。

两道既有保护都没救场：
- `NO CARRIER` / `+CEND:` 被串口抖动吞掉（modem.py:225 记的 2026-07-08 事故）；
- CLCC 消失判定的前提是模组自己知道通话没了。按 CLCC 失败路径最多 60 秒就会
  收尾，实际僵尸十分钟，只能推断模组一直报着有活跃通话。

于是只剩媒体本身可判。三种状态签名互不相同（真机实测）：

    通话中 AI 在说    frames=0            peak=0      ← 半双工屏蔽，本来就没帧
    通话中对方在说    frames=121~147      peak=439~591
    僵尸              frames=156~157      **peak=0**  ← 有帧但精确为 0

真实电话音频始终带底噪（实测 RMS 45~56），**出帧却精确为 0** 不是真音频。
"""

from __future__ import annotations

import pytest
from fakes import FakeModem

from agentcall.call_agent import CallSession

SILENCE = b"\x00\x00" * 160          # 20ms 纯数字静音
NOISE = b"\x01\x00" * 160            # 带极轻底噪：真实电话音频的样子


def make_session(timeout: float = 60.0) -> CallSession:
    session = CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )
    session._dead_media_seconds = timeout
    session._dead_media_silent_seconds = 0.0
    session._dead_media_reported = False
    return session


# ---- 会触发的情形 ----


def feed_silence(s, seconds: float, now: float = 0.0) -> bool:
    """按 20ms 一块喂入纯静音，返回是否判死。"""
    hit = False
    for i in range(int(seconds / 0.02)):
        hit = s._dead_media_expired(SILENCE, now=now + i * 0.02)
    return hit


def test_sustained_pure_silence_with_frames_triggers():
    """僵尸的签名：有帧、内容恒为 0、累计够久。"""
    s = make_session(timeout=60.0)
    assert feed_silence(s, 30.0) is False
    assert feed_silence(s, 31.0, now=30.0) is True


def test_counts_received_audio_not_wall_clock():
    """必须累计**收到的静音时长**，不能用墙上时钟（Codex 评审 P1）。

    按墙上时钟算的话：静音 50 秒 → 半双工屏蔽无帧 20 秒 → 再来一块静音，
    会立刻越过 60 秒阈值；而实际只收到 50 秒静音，不该判死。
    """
    s = make_session(timeout=60.0)
    assert feed_silence(s, 50.0) is False
    # 半双工屏蔽：20 秒没有帧
    for i in range(1000):
        s._dead_media_expired(b"", now=50.0 + i * 0.02)
    # 屏蔽结束后再来一块静音——只收到 50.02 秒，远不到 60
    assert s._dead_media_expired(SILENCE, now=70.0) is False


# ---- 不该触发的情形（误挂断比晚挂断严重得多）----


def test_real_audio_with_noise_floor_resets():
    """真实电话音频始终带底噪，绝不能被判成死媒体。"""
    s = make_session(timeout=10.0)
    feed_silence(s, 9.0)
    assert s._dead_media_expired(NOISE, now=9.0) is False
    assert s._dead_media_silent_seconds == 0.0, "收到真实音频必须清零"
    assert s._dead_media_expired(SILENCE, now=20.0) is False


def test_no_frames_does_not_count():
    """半双工屏蔽期间本来就没帧——不能把它当成对端消失。

    真机实测：AI 说话时 frames=0，这是正常状态，一通话里出现很多次。
    按「没帧」计时会在每次 AI 长时间说话后误挂断。
    """
    s = make_session(timeout=10.0)
    assert s._dead_media_expired(b"", now=0.0) is False
    assert s._dead_media_silent_seconds == 0.0
    assert s._dead_media_expired(b"", now=999.0) is False


def test_single_silent_frame_does_not_trigger():
    s = make_session(timeout=60.0)
    assert s._dead_media_expired(SILENCE, now=1.0) is False


def test_intermittent_silence_never_accumulates():
    """对方沉默一会儿又说话——很常见，不能累加成判死。"""
    s = make_session(timeout=10.0)
    for i in range(20):
        feed_silence(s, 5.0, now=float(i * 10))
        assert s._dead_media_expired(NOISE, now=float(i * 10 + 5)) is False
    assert s._dead_media_silent_seconds == 0.0


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_disabled_never_triggers(timeout):
    """0 = 关闭，回到旧行为。"""
    s = make_session(timeout=timeout)
    for t in range(0, 6000, 100):
        assert s._dead_media_expired(SILENCE, now=float(t)) is False


def test_negative_amplitude_counts_as_real_audio():
    """静音判据是「所有字节为 0」，负幅度样本同样是真音频。"""
    s = make_session(timeout=10.0)
    feed_silence(s, 5.0)
    assert s._dead_media_expired(b"\xff\xff" * 160, now=5.0) is False
    assert s._dead_media_silent_seconds == 0.0


def test_threshold_is_conservative_by_default():
    """默认阈值不能太短——误挂断比晚挂断严重得多。"""
    from agentcall import config

    assert config.get_spec("DEAD_MEDIA_TIMEOUT_SECONDS").default == "60"


def test_hangup_action_is_off_by_default():
    """精确静音只证明**媒体静默**，不等于对端挂断（Codex 评审 P1）。

    对方按静音、通话被 hold、UAC 补零、采集链路可恢复故障，签名都一样；
    GSM DTX 持续丢帧后也允许静音而非补舒适噪声。所以先只观测、不动作，
    攒够真实数据再决定是否默认开启——与 WIL-83 同一处理方式。
    """
    from agentcall import config

    assert config.get_spec("DEAD_MEDIA_HANGUP").default == "false"


# ---- 近失观测：让「是否默认开启挂断」这个问题真的能被回答 ----


def test_recovered_long_silence_is_recorded():
    """活着的通话出现长段精确静音 = 一个将来会误挂断的反例，必须留痕。

    观测模式（DEAD_MEDIA_HANGUP=false）要回答的是「活跃通话里到底会不会出现
    60 秒精确静音」。可 `_dead_media_expired` 收到真实音频就静默清零，反例
    全被吃掉——只记越过阈值的正例，永远攒不出决策所需的另一半数据。
    """
    s = make_session(timeout=60.0)
    feed_silence(s, 45.0)  # 过半阈值但没越线
    assert s._dead_media_expired(NOISE, now=45.0) is False, "通话还活着"
    assert s._dead_media_recovered_run is not None, "近失静音段必须留痕"
    assert s._dead_media_recovered_run == pytest.approx(45.0, abs=0.1)


def test_short_silence_is_not_reported_as_nearmiss():
    """短静音是通话常态（换气、IVR 间隙），留痕会淹掉真正的信号。"""
    s = make_session(timeout=60.0)
    feed_silence(s, 5.0)
    assert s._dead_media_expired(NOISE, now=5.0) is False
    assert s._dead_media_recovered_run is None


def test_recovered_run_is_consumed_once():
    """取走即清：同一段近失不能每块音频都重复落一次事件。"""
    s = make_session(timeout=60.0)
    feed_silence(s, 45.0)
    s._dead_media_expired(NOISE, now=45.0)
    assert s._dead_media_recovered_run is not None
    s._dead_media_recovered_run = None
    assert s._dead_media_expired(NOISE, now=46.0) is False
    assert s._dead_media_recovered_run is None, "真实音频不该反复触发近失"


# ---- 接线：断言**可观测行为**（事件 + snapshot 调用），不是私有字段 ----
#
# 上面那几个测试只断言 `_dead_media_recovered_run`，把 _run_agent_loop 里的落
# 事件与 snapshot 调用整段删掉它们照样绿——本仓库已经在这个坑里栽过
# （见 test_dtmf_outcome_evidence.py 的「生产上没有任何调用者」）。
# 所以这里驱动真实的 _run_agent_loop，断言外部真正看得见的东西。


class _RecordSpy:
    """只记录被调用了什么：事件与 snapshot。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.snapshots: list[str] = []

    def log_event(self, type: str, **fields) -> None:  # noqa: A002
        self.events.append((type, fields))

    def write_uplink(self, pcm: bytes) -> None:
        pass

    def write_downlink(self, pcm: bytes) -> None:
        pass

    def snapshot(self, reason: str) -> None:
        self.snapshots.append(reason)

    def events_of(self, type: str) -> list[dict]:  # noqa: A002
        return [f for t, f in self.events if t == type]


def _drive_loop(monkeypatch, chunks: list[bytes], *, timeout: float, hangup=False):
    """把给定的上行块喂进真实 _run_agent_loop，返回 RecordSpy。"""
    import asyncio

    from fakes import FakeAgent, FakeAudioBridge, FakeModem

    from agentcall.call_agent import CallAgentService

    service = CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="qwen",
        modem=FakeModem(),  # type: ignore[arg-type]
    )
    bridge = FakeAudioBridge()
    for chunk in chunks:
        bridge.feed_uplink(chunk)

    record = _RecordSpy()
    agent = FakeAgent()
    session = service.session
    session._active = True
    session._hangover_seconds = 0.0
    session._dead_media_seconds = timeout
    session._dead_media_hangup = hangup
    session._dead_media_silent_seconds = 0.0
    session._dead_media_reported = False
    session._dead_media_recovered_run = None
    session._dead_media_max_run = 0.0

    # 喂完就停，避免空转
    async def stop_when_drained(pcm: bytes) -> None:
        if not bridge.uplink:
            session._active = False

    monkeypatch.setattr(agent, "send_audio", stop_when_drained)
    asyncio.run(
        session._run_agent_loop(agent, bridge, record, [])  # type: ignore[arg-type]
    )
    return record


def test_loop_emits_recovered_event(monkeypatch):
    """静音够久后真实音频回来 → 必须落一条 dead_media_recovered 事件。

    删掉 _run_agent_loop 里那段落事件的代码，本测试必须变红。
    """
    chunks = [SILENCE] * 300 + [NOISE] * 5  # 6 秒静音（阈值 4）后恢复
    record = _drive_loop(monkeypatch, chunks, timeout=4.0)

    recovered = record.events_of("dead_media_recovered")
    assert len(recovered) == 1, f"应恰好一条，实得 {record.events}"
    assert recovered[0]["silent_seconds"] == pytest.approx(6.0, abs=0.2)
    assert recovered[0]["threshold_seconds"] == 4.0


def test_loop_snapshots_recording_on_detection(monkeypatch):
    """判死 → 必须落 dead_media_detected 并调用 record.snapshot()。

    快照是「重启也不丢证据」的全部价值所在；没有这条断言，
    删掉 snapshot 调用测试照样绿。
    """
    record = _drive_loop(monkeypatch, [SILENCE] * 300, timeout=4.0)

    assert record.events_of("dead_media_detected"), "判死必须落事件"
    assert record.snapshots == ["dead_media"], "判死必须抢救录音"


def test_recovery_rearms_the_detector(monkeypatch):
    """判死→恢复→再判死：第二次仍要落事件。

    否则「已经证明会误判」的那通通话，在对端**真的**挂断时反而失去保护。
    """
    chunks = [SILENCE] * 300 + [NOISE] * 5 + [SILENCE] * 300
    record = _drive_loop(monkeypatch, chunks, timeout=4.0)

    assert len(record.events_of("dead_media_detected")) == 2, "恢复后必须重新武装"
    assert len(record.snapshots) == 2


def test_loop_always_emits_max_run_data_point(monkeypatch):
    """静音后直接结束（没有恢复）也要留下数据点，否则样本有系统性偏差。"""
    record = _drive_loop(monkeypatch, [SILENCE] * 100, timeout=60.0)

    max_run = record.events_of("dead_media_max_run")
    assert len(max_run) == 1, "每通固定一条"
    assert max_run[0]["silent_seconds"] == pytest.approx(2.0, abs=0.2)
    assert not record.events_of("dead_media_recovered"), "没恢复就不该有恢复事件"
