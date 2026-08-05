"""通话自然度指标：从已有录音离线量化「像不像人」。

WIL-89（父规格 WIL-85 第四节）。本模块**只读不写通话链路**，是纯离线分析。

设计上的三个关键决定，都有实测依据：

1. **全部指标在「上行采样时间轴」上算，不碰 wall clock。**
   ``events.jsonl`` 用 ``time.time()``，而音频时间轴是上行采样下标
   （``call_log.py:218``：``write_downlink`` 以「此刻上行已累计字节数」为锚点）。
   两个时钟会漂移，所以凡是能从音频直接算的都不去关联事件——只有「每轮字数」
   来自 ``transcript`` 事件，而它不需要对齐。

2. **两个声道用不同的检测方式，这是刻意的。**
   · AI（``mixed.wav`` 左声道）：写进全零缓冲区，轮次之间是**精确数字静音**
     （``call_log.py`` ``_build_stereo_mix``），取非零段即可，不需要 VAD。
   · 对方（``uplink.wav`` **原始**）：真实电话音频，需要能量 VAD。
     ⚠️ **不能用** ``mixed.wav`` **右声道**——它被 ``_boost_caller`` 按通自适应
     放大过，跨通阈值不一致，会让「安静的通话」和「吵的通话」不可比。

3. **对方检测不需要回声抑制。**
   2026-08-05 实测 22 通：AI 说话时上行 RMS 与 AI 静默时之比 ≈ 0.73~1.25
   （中位数约 0.93），互相关峰值 0.000~0.166。即模组**没有**把下行回采进上行
   （EC20 语音通路自带回声消除）。若换硬件或改上行增益，这条要重测。
   —— 这同时是 WIL-94（barge-in）的重要输入：没有回采，就不需要做 AEC。
"""

from __future__ import annotations

import json
import statistics
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20ms @ 8kHz

# 对方 VAD：噪声基底的倍数。实测上行 RMS 基底约 45~56，说话时显著高出。
_VAD_NOISE_PERCENTILE = 20.0
_VAD_THRESHOLD_FACTOR = 3.0
_VAD_ABSOLUTE_FLOOR = 90.0  # 全程近乎静音的通话不该被判出一堆「说话」

# 段落整理：短于此的段丢弃（咔哒声/爆音），间隔短于此的段合并（词间停顿）。
_MIN_SPEECH_MS = 150
_MERGE_GAP_MS = 300

# 检测「对方起话」时用更小的合并间隔。
#
# 用 300ms 合并会漏掉真实的重叠（2026-08-05 Codex 评审 P1）：对方说到一半停 200ms、
# AI 趁隙插进来、对方又接着说——300ms 会把对方前后两段并成一段，其 start 落在
# AI 这一轮之前，于是「对方在 AI 说话期间起话」的判定不成立，重叠被静默吞掉。
# 150ms 既不会拆散词内停顿，又能让「让出去又被抢回来」这种真实重叠现形。
_ONSET_MERGE_GAP_MS = 150

# 纯音判据：能量集中在极少数频点即为双音而非语音。
#
# 带内 DTMF 的双音也会被写进下行录音（call_agent.py:1889 write_downlink(tone)），
# 而下行左声道正是我们判定「AI 在说话」的依据。2026-08-05 实测 22 通样本：
# 173 段「AI 发声」里有 14 段是纯双音，时长 200~400ms，全部越过 150ms 门槛，
# 被当成了 14 个极短的「AI 轮次」——既虚增轮数，又把单轮时长中位数往下拉。
# 用「前若干频点能量占比」判纯音，而不是去比对 DTMF 频率表：前者对任何纯音都成立，
# 也不需要维护一张表。
_TONE_TOP_BINS = 6
_TONE_ENERGY_RATIO = 0.5
_TONE_MIN_SAMPLES = 256

# 应答时延只在这个窗口内配对；超过就认为不是对这句的回应。
_RESPONSE_WINDOW_MS = 8000

# 对方在 AI 一轮的最后这段时间内起话，不计入「让路」统计：
# 此时 AI 本来就快说完了，剩余时长天然很小，会伪装成「让路很快」。
_LATE_OVERLAP_MS = 500

# 上行动态范围低于这个倍数，说明本通没有足够安静的帧来估噪声基底，对方侧指标存疑。
_PEER_MIN_DYNAMIC_RANGE = 3.0


def _ms(samples: int) -> float:
    return samples * 1000.0 / SAMPLE_RATE


def call_direction(call_id: str) -> str:
    """从录音目录名解析呼叫方向，例如 ``20260805-093600-inbound-4001007441``。

    方向决定了哪些指标有意义（见 ``analyze_call``），所以解析不出来时返回
    ``unknown`` 并让调用方把这通排除在方向相关的统计之外，而不是猜一个。
    """
    parts = call_id.split("-")
    for part in parts:
        if part in ("inbound", "outbound"):
            return part
    return "unknown"


@dataclass(frozen=True)
class Segment:
    """一段连续发声，单位为样本下标（左闭右开）。"""

    start: int
    end: int

    @property
    def duration_ms(self) -> float:
        return _ms(self.end - self.start)


def read_wav(path: Path) -> np.ndarray:
    """读 WAV 为 int16 数组；立体声返回 (n, channels)，单声道返回 (n,)。"""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        usable = len(data) - (len(data) % channels)
        return data[:usable].reshape(-1, channels)
    return data


def _tidy(segments: list[Segment], merge_gap_ms: float = _MERGE_GAP_MS) -> list[Segment]:
    """合并过近的段、丢弃过短的段。"""
    if not segments:
        return []
    merge_gap = int(merge_gap_ms * SAMPLE_RATE / 1000)
    merged: list[Segment] = [segments[0]]
    for seg in segments[1:]:
        last = merged[-1]
        if seg.start - last.end <= merge_gap:
            merged[-1] = Segment(last.start, seg.end)
        else:
            merged.append(seg)
    min_len = int(_MIN_SPEECH_MS * SAMPLE_RATE / 1000)
    return [s for s in merged if s.end - s.start >= min_len]


def _runs(mask: np.ndarray, scale: int = 1) -> list[Segment]:
    """把布尔掩码里的 True 连续段转成 Segment（scale = 每个掩码元素代表的样本数）。"""
    if mask.size == 0 or not mask.any():
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [
        Segment(int(edges[i]) * scale, int(edges[i + 1]) * scale)
        for i in range(0, len(edges), 2)
    ]


def is_pure_tone(samples: np.ndarray) -> bool:
    """这一段是纯音（DTMF 双音）还是语音？

    语音是宽带的，能量散布在很多频点；DTMF 是两个正弦叠加，能量高度集中。
    """
    if samples.size < _TONE_MIN_SAMPLES:
        return False
    window = np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(samples.astype(np.float64) * window)) ** 2
    total = spectrum.sum()
    if total <= 0:
        return False
    top = np.sort(spectrum)[-_TONE_TOP_BINS:].sum()
    return bool(top / total > _TONE_ENERGY_RATIO)


def agent_segments(agent_pcm: np.ndarray) -> list[Segment]:
    """AI 发声段（已剔除 DTMF 双音）。

    左声道是写进全零缓冲区的，轮次之间是**精确数字静音**，所以取非零段即可——
    不需要 VAD，也就没有阈值选择带来的不确定性。

    但非零 ≠ 说话：带内 DTMF 的双音同样走下行、同样进录音，必须剔除，
    否则每次按键都会变成一个 200~400ms 的假「AI 轮次」（见 ``_TONE_ENERGY_RATIO``）。
    """
    return [
        seg
        for seg in _tidy(_runs(agent_pcm != 0))
        if not is_pure_tone(agent_pcm[seg.start : seg.end])
    ]


def peer_dynamic_range(uplink_pcm: np.ndarray) -> float:
    """上行的动态范围 = p90 帧能量 / p20 帧能量。

    这是判断「噪声基底估计还成不成立」的正确信号。**不能用「说话帧占比」**：
    对方不停口时，p20 本身就变成了语音电平，阈值随之抬到语音之上，于是一帧都
    判不出说话——占比趋近 0 而不是 1，往占比高的方向查会完全查不到
    （2026-08-05 首次实现即用错了方向）。

    真实通话实测：基底 RMS 约 45~56、语音数百以上，动态范围远大于 3。
    连续音乐/长播报则 p20≈p90，动态范围趋近 1。
    """
    if uplink_pcm.size < FRAME_SAMPLES:
        return 0.0
    n_frames = uplink_pcm.size // FRAME_SAMPLES
    frames = uplink_pcm[: n_frames * FRAME_SAMPLES].reshape(n_frames, FRAME_SAMPLES)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    low = float(np.percentile(rms, _VAD_NOISE_PERCENTILE))
    high = float(np.percentile(rms, 90))
    return high / low if low > 1e-9 else 0.0


def _peer_frame_mask(uplink_pcm: np.ndarray) -> np.ndarray:
    if uplink_pcm.size < FRAME_SAMPLES:
        return np.zeros(0, dtype=bool)
    n_frames = uplink_pcm.size // FRAME_SAMPLES
    frames = uplink_pcm[: n_frames * FRAME_SAMPLES].reshape(n_frames, FRAME_SAMPLES)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    noise = float(np.percentile(rms, _VAD_NOISE_PERCENTILE))
    threshold = max(noise * _VAD_THRESHOLD_FACTOR, _VAD_ABSOLUTE_FLOOR)
    return rms > threshold


def peer_segments(
    uplink_pcm: np.ndarray, merge_gap_ms: float = _MERGE_GAP_MS
) -> list[Segment]:
    """对方发声段：20ms 分帧能量 VAD，阈值按本通噪声基底自适应。

    阈值取「基底分位数 × 倍数」与一个绝对下限的较大者。绝对下限是为了让
    全程近乎静音的通话不至于把噪声判成说话——实测基底 RMS 约 45~56。

    ⚠️ 这个估计的前提是**本通至少有两成帧是真安静的**。对方几乎不停口的通话
    （连续音乐、长 IVR 播报）会把「基底」本身抬成语音电平，阈值随之抬高，
    反而把真正的说话漏掉。``analyze_call`` 用 ``peer_dynamic_range`` 检出这种
    情况并在 ``notes`` 里标注，而不是照常给出一个看着正常的假数。
    """
    return _tidy(_runs(_peer_frame_mask(uplink_pcm), scale=FRAME_SAMPLES), merge_gap_ms)


@dataclass
class Overlap:
    """对方在 AI 说话期间起话的一次重叠。"""

    peer_start: int
    agent_end: int
    agent_turn_end: int

    @property
    def agent_kept_talking_ms(self) -> float:
        """对方起话后 AI 又说了多久。让路则小，不让路则是这一轮的剩余时长。"""
        return _ms(self.agent_turn_end - self.peer_start)


@dataclass
class CallMetrics:
    """单通指标。测不了的项一律 None + 写明原因，**绝不填 0**。"""

    call_id: str
    direction: str  # inbound / outbound / unknown
    duration_ms: float
    agent_turns: int
    peer_turns: int
    agent_turn_ms: list[float] = field(default_factory=list)
    agent_turn_chars: list[int] = field(default_factory=list)
    # ⚠️ 这是**上限**，不是纯处理时延：它包含 server_vad 为确认「对方说完了」
    # 而刻意等待的静音时长（openai_agent.py 的 turn_detection）。要把两者分开，
    # 需要 provider 的 speech_stopped 事件时刻，那属于 wall clock 域——本模块
    # 刻意不跨时钟关联（见模块 docstring 第 1 条）。调小 VAD 静音阈值会让这个数
    # 变小，但代价是更容易把对方话中的停顿误判成说完（WIL-85 N3 已注明此取舍）。
    response_latency_ms: list[float] = field(default_factory=list)
    overlaps: list[Overlap] = field(default_factory=list)
    late_overlaps: int = 0
    opening_ms: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def interruption_count(self) -> int:
        return len(self.overlaps)

    @property
    def interruptions_per_minute(self) -> float | None:
        if self.duration_ms <= 0:
            return None
        return self.interruption_count / (self.duration_ms / 60000.0)

    @property
    def yield_after_interruption_ms(self) -> list[float]:
        """对方起话后 AI 还说了多久——**WIL-94 的反面基线**。

        当前代码里对方的上行在 AI 说话期间被丢弃（``call_agent.py:795``），
        AI 收不到插话，所以这里的值应当散布在「该轮剩余时长」上而不是集中在小值。
        WIL-94 做完后，这个分布应当整体塌向几百毫秒以内。
        """
        return [o.agent_kept_talking_ms for o in self.overlaps]

    def to_dict(self) -> dict[str, Any]:
        yields = self.yield_after_interruption_ms
        return {
            "call_id": self.call_id,
            "direction": self.direction,
            "duration_s": round(self.duration_ms / 1000, 1),
            "agent_turns": self.agent_turns,
            "peer_turns": self.peer_turns,
            "opening_ms": _round(self.opening_ms),
            "agent_turn_ms_median": _round(_median(self.agent_turn_ms)),
            "agent_turn_chars_median": _round(_median(self.agent_turn_chars)),
            "agent_turn_chars_max": max(self.agent_turn_chars, default=None),
            "response_latency_ms_median": _round(_median(self.response_latency_ms)),
            "response_latency_ms_stdev": _round(_stdev(self.response_latency_ms)),
            "interruption_count": self.interruption_count,
            "interruptions_per_minute": _round(self.interruptions_per_minute),
            "late_overlaps_excluded": self.late_overlaps,
            "yield_after_interruption_ms_median": _round(_median(yields)),
            "notes": self.notes,
        }


def _median(values: list[float] | list[int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _stdev(values: list[float]) -> float | None:
    return float(statistics.stdev(values)) if len(values) >= 2 else None


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _has_dropped_agent_audio(events_path: Path) -> bool:
    """本通是否有护窗期内被丢弃的 AI 语音（WIL-81 的 ``agent_audio_dropped``）。"""
    if not events_path.is_file():
        return False
    return "agent_audio_dropped" in events_path.read_text(encoding="utf-8")


def _agent_turn_chars(events_path: Path) -> list[int]:
    """AI 每轮字数，取自 ``transcript`` 事件。

    这是唯一来自事件而非音频的指标，且它不需要与音频时间轴对齐——只统计分布。
    """
    if not events_path.is_file():
        return []
    counts: list[int] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "transcript":
            continue
        if event.get("role") not in ("agent", "assistant"):
            continue
        text = event.get("text") or ""
        if text:
            counts.append(len(text))
    return counts


def analyze_call(call_dir: Path) -> CallMetrics | None:
    """分析一通。缺录音返回 None（调用方负责说明跳过了多少通）。"""
    mixed_path = call_dir / "mixed.wav"
    uplink_path = call_dir / "uplink.wav"
    if not mixed_path.is_file() or not uplink_path.is_file():
        return None

    mixed = read_wav(mixed_path)
    if mixed.ndim != 2 or mixed.shape[1] < 2:
        return None
    agent_pcm = mixed[:, 0]  # 左 = AI，未被 _boost_caller 放大
    peer_pcm = read_wav(uplink_path)  # 原始上行，不用右声道
    if peer_pcm.ndim != 1:
        return None

    agents = agent_segments(agent_pcm)
    peers = peer_segments(peer_pcm)
    duration = _ms(max(len(agent_pcm), len(peer_pcm)))

    direction = call_direction(call_dir.name)
    metrics = CallMetrics(
        call_id=call_dir.name,
        direction=direction,
        duration_ms=duration,
        agent_turns=len(agents),
        peer_turns=len(peers),
        agent_turn_ms=[s.duration_ms for s in agents],
        agent_turn_chars=_agent_turn_chars(call_dir / "events.jsonl"),
        # 开场白只在来电侧有意义：外呼时 AI 的第一句是任务陈述（「我想查上月话费」），
        # 不是接起电话的招呼，混在一起算会把 N4 的基线彻底带偏。
        opening_ms=(
            agents[0].duration_ms if agents and direction == "inbound" else None
        ),
    )
    if direction != "inbound":
        metrics.notes.append("外呼通话：不计开场白（首轮是任务陈述而非招呼）")

    # 应答时延：**按 AI 的每一次开口向前找**最近的一段对方发言，而不是反过来。
    #
    # 反过来做（遍历对方每一段、找它之后的 AI 开口）是错的：VAD 会把 IVR 的一段
    # 长菜单按停顿切成多段，于是「菜单中间的停顿」也被当成一次提问，算出 AI 用了
    # 好几秒才回答——而实际上 AI 是在正确地等菜单播完。2026-08-05 首次实现即踩此坑，
    # 中位数被抬到 3866ms。
    #
    # 另加约束：这段对方发言必须发生在**上一轮 AI 说完之后**，否则 AI 连着说两段时，
    # 第二段会去配上一轮之前的对方发言，得到一个虚高的时延。
    window = int(_RESPONSE_WINDOW_MS * SAMPLE_RATE / 1000)
    for index, agent in enumerate(agents):
        floor = agents[index - 1].end if index > 0 else 0
        preceding = [p for p in peers if floor <= p.end <= agent.start]
        if not preceding:
            continue
        latest = preceding[-1]
        gap = agent.start - latest.end
        if gap <= window:
            metrics.response_latency_ms.append(_ms(gap))

    # 重叠：对方在 AI 一轮**之内**起话。
    #
    # 用更小的合并间隔重新切一次对方的段（见 _ONSET_MERGE_GAP_MS）：按 300ms 合并
    # 会把「说→停 200ms→AI 插进来→接着说」并成一段，其 start 落在 AI 这轮之前，
    # 重叠就被静默吞掉了。
    onsets = peer_segments(peer_pcm, merge_gap_ms=_ONSET_MERGE_GAP_MS)
    late_cutoff = int(_LATE_OVERLAP_MS * SAMPLE_RATE / 1000)
    for agent in agents:
        for peer in onsets:
            if not (agent.start < peer.start < agent.end):
                continue
            if agent.end - peer.start <= late_cutoff:
                # 对方在这轮尾巴上起话——剩余时长天然很小，会伪装成「让路很快」。
                metrics.late_overlaps += 1
                continue
            metrics.overlaps.append(
                Overlap(
                    peer_start=peer.start,
                    agent_end=agent.end,
                    agent_turn_end=agent.end,
                )
            )

    if not metrics.agent_turn_chars:
        metrics.notes.append("无 agent transcript 事件，字数指标不可用")
    if not peers:
        metrics.notes.append("未检出对方发声，应答时延与打断指标不可用")

    # 噪声基底估计的前提是本通有足够的安静帧；不成立时说出来，别给假数。
    dyn = peer_dynamic_range(peer_pcm)
    if dyn < _PEER_MIN_DYNAMIC_RANGE:
        metrics.notes.append(
            f"上行动态范围仅 {dyn:.1f}×，噪声基底估计可能失效，对方侧指标存疑"
        )

    # WIL-81：护窗期内被丢弃的 AI 语音仍会进转写，但**没有进录音**。
    # 于是「字数」来自说过的话，「时长」来自听得到的话，两者口径不一致。
    if _has_dropped_agent_audio(call_dir / "events.jsonl"):
        metrics.notes.append(
            "本通有 agent_audio_dropped：字数含未播出的语音，与音频时长口径不一致"
        )

    return metrics
