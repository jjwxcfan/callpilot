"""自然度审计工具的单测（WIL-89）。

重点不在「函数能跑」，而在锁住**首次实现时踩过的三个方法学错误**——
它们都不会让程序报错，只会让基线悄悄变成假数字：

1. 应答时延配对方向反了：遍历对方每一段去找之后的 AI 开口，会把 IVR 长菜单
   中间的停顿当成一次提问，中位数被抬到 3866ms（实测），修正后 2404ms。
2. 外呼也算开场白：外呼首轮是任务陈述不是招呼，混进来把 N4 基线带偏。
3. 打断不分方向：外呼打到 IVR 时「对方」是播菜单的机器，压着 AI 说话不是插话。
"""

from __future__ import annotations

import json
import wave

import numpy as np
import pytest

from agentcall.naturalness import (
    SAMPLE_RATE,
    Segment,
    agent_segments,
    analyze_call,
    call_direction,
    peer_segments,
)


def _tone(ms: int, amplitude: int = 6000) -> np.ndarray:
    """一段可被 VAD 判为「说话」的音频。"""
    n = int(SAMPLE_RATE * ms / 1000)
    t = np.arange(n)
    return (amplitude * np.sin(2 * np.pi * 300 * t / SAMPLE_RATE)).astype("<i2")


def _speechlike(ms: int, amplitude: int = 6000) -> np.ndarray:
    """宽带信号，代表**语音**。

    不能用 ``_tone``（单频正弦）来当 AI 的语音：``agent_segments`` 会把纯音
    当成 DTMF 双音剔除掉，测试会以「AI 一句话都没说」的形式假通过。
    """
    n = int(SAMPLE_RATE * ms / 1000)
    rng = np.random.default_rng(1)
    return (rng.normal(0, amplitude / 3, n)).clip(-32000, 32000).astype("<i2")


def _quiet(ms: int, amplitude: int = 20) -> np.ndarray:
    """一段底噪：不该被判成说话。实测真实通话底噪 RMS 约 45~56。"""
    n = int(SAMPLE_RATE * ms / 1000)
    rng = np.random.default_rng(0)
    return rng.integers(-amplitude, amplitude + 1, n).astype("<i2")


def _write_call(tmp_path, name, agent_track, peer_track, transcripts=()):
    """按真实落盘格式造一通：mixed.wav 立体声(左=AI) + uplink.wav 原始上行。"""
    call = tmp_path / name
    call.mkdir()
    n = max(len(agent_track), len(peer_track))
    left = np.zeros(n, dtype="<i2")
    left[: len(agent_track)] = agent_track
    right = np.zeros(n, dtype="<i2")
    right[: len(peer_track)] = peer_track
    stereo = np.empty(n * 2, dtype="<i2")
    stereo[0::2] = left
    stereo[1::2] = right
    with wave.open(str(call / "mixed.wav"), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(stereo.tobytes())
    with wave.open(str(call / "uplink.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(right.tobytes())
    if transcripts:
        (call / "events.jsonl").write_text(
            "\n".join(
                json.dumps({"type": "transcript", "ts": 0, "role": r, "text": t})
                for r, t in transcripts
            )
            + "\n",
            encoding="utf-8",
        )
    return call


# ---- 声道检测 ----


def test_agent_segments_use_exact_silence_not_vad():
    """AI 声道写进全零缓冲区，轮次间是精确数字静音——不该有阈值参与。

    幅度极小的一段也必须被算作发声，因为它确实是 AI 的声音；用能量阈值就会漏掉。
    """
    track = np.concatenate([_speechlike(400, amplitude=30),
                            np.zeros(4000, dtype="<i2"), _speechlike(400)])
    segments = agent_segments(track)
    assert len(segments) == 2, "极轻的 AI 语音也必须算作一轮"


def test_peer_vad_ignores_noise_floor():
    """底噪不该被判成说话，否则每通都会凭空多出一堆「对方发言」。"""
    assert peer_segments(_quiet(3000)) == []


def test_peer_vad_finds_speech():
    segments = peer_segments(
        np.concatenate([_quiet(500), _tone(800), _quiet(500)])
    )
    assert len(segments) == 1
    assert 700 <= segments[0].duration_ms <= 900


def test_short_blips_are_dropped():
    """咔哒声/爆音短于 150ms，不该计成一次发言。"""
    assert peer_segments(np.concatenate([_quiet(500), _tone(50), _quiet(500)])) == []


# ---- 回归 1：应答时延的配对方向 ----


def test_response_latency_pairs_backward_from_agent_onset(tmp_path):
    """IVR 菜单被 VAD 切成多段时，不能把菜单中间的停顿算成「AI 答得慢」。

    构造：对方连说三段（中间各停 600ms，模拟菜单条目间停顿），
    最后一段结束后 500ms AI 开口。
    正确答案只有一条 ≈500ms；错误实现会额外产出两条 ~1.1s、~1.7s 的假数据。
    """
    peer = np.concatenate([
        _tone(600), _quiet(600), _tone(600), _quiet(600), _tone(600),
        _quiet(500),                      # AI 在这之后开口
        np.zeros(int(SAMPLE_RATE * 2), dtype="<i2"),
    ])
    agent_start = len(peer) - int(SAMPLE_RATE * 2)
    agent = np.concatenate([
        np.zeros(agent_start, dtype="<i2"), _speechlike(1500),
    ])
    call = _write_call(tmp_path, "20260805-1200-outbound-10086", agent, peer)
    metrics = analyze_call(call)
    assert metrics is not None
    assert len(metrics.response_latency_ms) == 1, (
        f"每次 AI 开口只该产出一条应答时延，实际 {metrics.response_latency_ms}"
    )
    assert 300 <= metrics.response_latency_ms[0] <= 800


# ---- 回归 2：开场白只算来电 ----


def test_opening_only_counted_for_inbound(tmp_path):
    agent = _speechlike(2000)
    peer = np.concatenate([np.zeros(int(SAMPLE_RATE * 2.5), dtype="<i2"), _tone(500)])

    inbound = analyze_call(_write_call(tmp_path, "20260805-1-inbound-400100", agent, peer))
    outbound = analyze_call(_write_call(tmp_path, "20260805-2-outbound-10086", agent, peer))
    assert inbound is not None and outbound is not None
    assert inbound.opening_ms is not None
    assert outbound.opening_ms is None, "外呼首轮是任务陈述，不是开场白"
    assert any("外呼" in n for n in outbound.notes)


def test_call_direction_parsing():
    assert call_direction("20260805-093600-inbound-4001007441") == "inbound"
    assert call_direction("20260721-112833-outbound-13534086960") == "outbound"
    assert call_direction("garbage") == "unknown", "解析不出方向要说不知道，不能猜"


# ---- 回归 3：打断与尾部重叠 ----


def test_overlap_detected_and_measures_remaining_agent_speech(tmp_path):
    """对方在 AI 说话中途起话 → 记一次重叠，并量出 AI 之后还说了多久。

    这是 WIL-94 的反面基线：当前代码 AI 收不到插话，所以这个值会很大。
    """
    agent = _speechlike(6000)
    peer = np.concatenate([
        np.zeros(int(SAMPLE_RATE * 1.0), dtype="<i2"), _tone(800),
    ])
    metrics = analyze_call(
        _write_call(tmp_path, "20260805-3-inbound-400100", agent, peer)
    )
    assert metrics is not None
    assert metrics.interruption_count == 1
    remaining = metrics.yield_after_interruption_ms[0]
    assert remaining > 4000, "AI 未让路时，剩余时长应当很大——这正是基线要显示的"


def test_late_overlap_excluded_from_yield_stats(tmp_path):
    """对方在 AI 这一轮的最后 500ms 内起话，剩余时长天然很小，
    会伪装成「让路很快」——必须排除，否则 WIL-94 的效果无法证明。"""
    agent = _speechlike(2000)
    peer = np.concatenate([
        np.zeros(int(SAMPLE_RATE * 1.8), dtype="<i2"), _tone(600),
    ])
    metrics = analyze_call(
        _write_call(tmp_path, "20260805-4-inbound-400100", agent, peer)
    )
    assert metrics is not None
    assert metrics.interruption_count == 0
    assert metrics.late_overlaps == 1


# ---- 字数与缺数据 ----


def test_agent_turn_chars_from_transcript(tmp_path):
    call = _write_call(
        tmp_path, "20260805-5-inbound-400100", _speechlike(500), _quiet(500),
        transcripts=[("agent", "你好呀"), ("user", "喂你哪位啊"), ("agent", "我是助理")],
    )
    metrics = analyze_call(call)
    assert metrics is not None
    assert metrics.agent_turn_chars == [3, 4], "只统计 AI 的轮次"


def test_missing_recording_returns_none(tmp_path):
    empty = tmp_path / "20260805-6-inbound-400100"
    empty.mkdir()
    assert analyze_call(empty) is None, "缺录音要返回 None 让调用方去说明跳过了几通"


def test_unmeasurable_metrics_are_none_not_zero(tmp_path):
    """规格验收 2：测不了的记 null，不记 0。0 会被读成「延迟为零」。"""
    metrics = analyze_call(
        _write_call(tmp_path, "20260805-7-inbound-400100", _speechlike(500), _quiet(3000))
    )
    assert metrics is not None
    assert metrics.peer_turns == 0
    assert metrics.to_dict()["response_latency_ms_median"] is None
    assert any("对方发声" in n for n in metrics.notes)


@pytest.mark.parametrize("start,end,expected", [(0, 800, 100.0), (800, 1600, 100.0)])
def test_segment_duration(start, end, expected):
    assert Segment(start, end).duration_ms == expected


# ---- Codex 评审 P1 之一：带内 DTMF 双音不能算作 AI 的一轮 ----


def _dtmf(ms: int, low: int = 697, high: int = 1209) -> np.ndarray:
    """按键「1」的双音（dtmf.py: 697/1209Hz）。实测落盘时长 200~400ms。"""
    n = int(SAMPLE_RATE * ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    wave_ = 8000 * (np.sin(2 * np.pi * low * t) + np.sin(2 * np.pi * high * t)) / 2
    return wave_.astype("<i2")


def test_pure_tone_detected_and_speech_not():
    from agentcall.naturalness import is_pure_tone

    assert is_pure_tone(_dtmf(200)), "DTMF 双音必须判为纯音"
    assert is_pure_tone(_tone(200)), "单频正弦同样是纯音"
    assert not is_pure_tone(_speechlike(200)), "宽带语音不能被误判成纯音"


def test_dtmf_tone_is_not_counted_as_an_agent_turn():
    """带内 DTMF 走 write_downlink 进录音（call_agent.py:1889），
    非零 ≠ AI 在说话。实测 22 通里 173 段「AI 发声」有 14 段是双音。"""
    speech = _speechlike(1200)
    track = np.concatenate([
        speech, np.zeros(4000, dtype="<i2"), _dtmf(300),
        np.zeros(4000, dtype="<i2"), speech,
    ])
    segments = agent_segments(track)
    assert len(segments) == 2, f"双音被当成了一轮 AI 说话：{[s.duration_ms for s in segments]}"
    assert all(s.duration_ms > 1000 for s in segments)


# ---- Codex 评审 P1 之二：合并会吞掉真实重叠 ----


def test_overlap_survives_short_peer_pause(tmp_path):
    """对方说→停 200ms→AI 插进来→对方接着说。

    按 300ms 合并，对方前后两段并成一段、start 落在 AI 这轮之前，
    「对方在 AI 说话期间起话」就不成立，重叠被静默吞掉。
    """
    peer = np.concatenate([
        _tone(700),                                   # 对方先说
        _quiet(250),                                  # 短停顿：300ms 合并会吃掉它
        _tone(1500),                                  # 对方接着说（此时 AI 正在说）
    ])
    agent = np.concatenate([
        np.zeros(int(SAMPLE_RATE * 0.80), dtype="<i2"),  # AI 在停顿里插进来
        _speechlike(4000),
    ])
    metrics = analyze_call(
        _write_call(tmp_path, "20260805-8-inbound-400100", agent, peer)
    )
    assert metrics is not None
    assert metrics.interruption_count >= 1, "对方停顿后又压着 AI 说，这次重叠不能漏掉"


def test_near_silent_agent_turn_is_not_mistaken_for_a_tone(tmp_path):
    """近乎静音的一段不能被当成 DTMF 剔除——那会让缺陷本身「消失」。

    2026-08-06 真机：开场白只说「喂」时下行峰值仅 36（正常语音约 2 万），
    频谱同样高度集中，于是被判为纯音整段丢掉，工具把**自我介绍**报成了开场白，
    opening_ms 从真实的 0.4s 变成 3.95s——方向完全相反，会把一个真实缺陷
    读成「开场白还是太长」。
    """
    quiet_blip = (_speechlike(400) // 200).astype("<i2")  # 峰值降到几十
    agent = np.concatenate([
        quiet_blip, np.zeros(int(SAMPLE_RATE * 2), dtype="<i2"), _speechlike(3000),
    ])
    metrics = analyze_call(
        _write_call(tmp_path, "20260806-1-inbound-400100", agent, _quiet(6000))
    )
    assert metrics is not None
    # 开场白必须仍然是那段近乎静音的首轮，不能变成后面的长段
    assert metrics.opening_ms is not None and metrics.opening_ms < 1000, (
        f"近乎静音的首轮被丢掉了，opening_ms={metrics.opening_ms}"
    )
    assert any("异常静音" in n for n in metrics.notes), "静音段必须被显式报出来"


def test_real_dtmf_is_still_filtered(tmp_path):
    """幅度下限不能把真正的 DTMF 放进来——它是正常幅度的。"""
    from agentcall.naturalness import is_pure_tone

    assert is_pure_tone(_dtmf(300)), "正常幅度的双音仍必须被判为纯音"


def test_high_duty_cycle_peer_is_flagged(tmp_path):
    """对方几乎不停口时，噪声基底估计失效——要标注，不要给一个看着正常的假数。"""
    metrics = analyze_call(
        _write_call(tmp_path, "20260805-9-inbound-400100", _speechlike(500), _tone(6000))
    )
    assert metrics is not None
    assert any("动态范围" in n for n in metrics.notes)
