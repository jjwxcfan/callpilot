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
