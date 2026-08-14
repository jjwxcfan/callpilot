"""每通通话的指标汇总（WIL-95 第一期）：从事件流生成 ``metrics.json``。

契约要点（与 WIL-95 规格逐条对应）：

- **schema_version 是硬要求**：任何字段的定义变更必须升版本；汇总工具按版本
  分组，拒绝跨版本直接平均（改了定义不升版本，历史数据会静默变得不可比，
  且不会报错——最危险的一类问题）。
- **测不了/条件未满足的指标记 null + 原因**（``unavailable`` 表），**绝不记 0**
  ——0 会被读成「瞬时完成」。
- **命名诚实**：``local_response`` 而非 ``e2e``——蜂窝两腿在观测范围外
  （WIL-95 2.1），把本地测量叫端到端会系统性低估。
- **只读事件、不碰音频**：音频派生指标（每轮出声时长、打断率等）归 WIL-89
  离线审计工具——两个生产者、一份契约，字段定义以先落盘者为准。

本模块是纯函数（events 进、dict 出），落盘由 ``call_log.CallRecord.finish``
调用；判定层（任务是否达成）刻意不在这里——那是可重算的派生视图（WIL-95 §4），
不写进不可变的原始记录。
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any

# 指标契约版本（2026-08-14 v1）。改任何字段定义前先读模块 docstring 第一条。
SCHEMA_VERSION = 1

# 逐轮时延指标：events.jsonl 里 latency 事件的 stage → metrics.json 字段名。
# 各 stage 的精确定义（2026-08-14）：
# - local_response：本地音尾 → 下行首块回复**到达**，每轮一条（call_agent 埋点；
#   覆盖上行传输 + VAD 判停 + 生成，不含播出排队与蜂窝两腿）
# - first_audio_delta：provider 判停(speech_stopped) → 首个音频增量到达
#   （openai_agent 埋点；语音到语音架构下 TTFT/TTFB 的唯一可观测合并量，
#   WIL-95 2.2——local provider 三段式下语义不同，看 config.provider 归因）
# - playout_backlog：本轮首块回复入队时，前面已排未播的音频时长（WIL-112
#   实测体感 3~5s 的主因；0 是有效读数=无积压）
_LATENCY_STAGES = {
    "local_response": "local_response_latency_ms",
    "first_audio_delta": "first_audio_delta_ms",
    "playout_backlog": "playout_backlog_ms",
}

# 本版本先天测不了/未埋点的指标及原因——字段常驻，等埋点补上后从这里移走。
# 打断类的原因按 barge_in 开关分岔（WIL-95 2.3），在 build 时动态填。
_STATIC_UNAVAILABLE = {
    "e2e_latency_ms": "carrier_legs_unobservable",  # WIL-95 2.1：不要假装能测
    "tool_call_latency_ms": "tool_call_event_has_no_duration",  # 第二期补
    "termination_kind": "not_instrumented",  # 第二期补
    "hangup_latency_ms": "not_instrumented",  # 第二期补
}


def _nearest_rank_p90(ordered: list[float]) -> float:
    """最近秩 p90：ceil(0.9n)-1。int(0.9n) 在 n=10 时取到最大值，小样本会
    系统性偏高（与 scripts/naturalness_audit.py 同一实现与教训）。"""
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return ordered[rank - 1]


def _describe(values: list[float]) -> dict[str, Any] | None:
    """n/median/p90/max + 原始值；空样本返回 None（落到 unavailable，不记 0）。

    保留 ``values``（升序）是刻意的：单通轮次数很小（通常 <50），多存几十个
    数换来跨通分位数**精确可算**——汇总工具只读 metrics.json，不必回头重解析
    全部 events.jsonl（WIL-76 全量重扫退化的教训）。
    """
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 1),
        "p90": round(_nearest_rank_p90(ordered), 1),
        "max": round(ordered[-1], 1),
        "values": [round(v, 1) for v in ordered],
    }


def build_call_metrics(
    events: list[dict[str, Any]],
    *,
    call_id: str,
    direction: str,
    duration_s: float,
    answered: bool,
    status: str,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """从一通通话的事件流构建 metrics.json 内容（纯函数，不做 IO）。

    只消费事件与元数据；隐私约束（WIL-95 §7）：不含转写文本、不含按键值、
    不含任何凭证——字数/次数这类派生量除外。
    """
    latencies: dict[str, list[float]] = {stage: [] for stage in _LATENCY_STAGES}
    config_snapshot: dict[str, Any] | None = None
    first_audio_ms: float | None = None
    agent_turn_chars: list[float] = []
    peer_turns = 0
    tool_calls: dict[str, int] = {}
    dtmf_actions = 0
    dtmf_outcomes: dict[str, int] = {}
    barge_in_fallback = False

    for event in events:
        etype = event.get("type")
        if etype == "latency":
            stage = event.get("stage")
            ms = event.get("ms")
            if stage in latencies and isinstance(ms, (int, float)):
                latencies[stage].append(float(ms))
        elif etype == "config_snapshot":
            config_snapshot = {
                k: v for k, v in event.items() if k not in ("type", "ts")
            }
        elif etype == "first_audio":
            ms = event.get("ms")
            if isinstance(ms, (int, float)):
                first_audio_ms = float(ms)
        elif etype == "transcript":
            role = event.get("role")
            text = event.get("text") or ""
            if role == "agent":
                agent_turn_chars.append(float(len(text)))
            elif role == "user":
                peer_turns += 1
        elif etype == "tool_call":
            tool = str(event.get("tool") or "unknown")
            tool_calls[tool] = tool_calls.get(tool, 0) + 1
        elif etype == "dtmf_action":
            dtmf_actions += 1
        elif etype == "dtmf_outcome":
            outcome_status = str(event.get("status") or "unknown")
            dtmf_outcomes[outcome_status] = dtmf_outcomes.get(outcome_status, 0) + 1
        elif etype == "barge_in_fallback":
            barge_in_fallback = True

    unavailable = dict(_STATIC_UNAVAILABLE)
    if config_snapshot is None:
        unavailable["config"] = "config_snapshot_event_missing"
    # 打断类（WIL-95 2.3）：随 BARGE_IN_ENABLED 分岔。开着也还没埋点（WIL-94
    # E2E 之后补），关着结构上不发生——原因必须区分，混在一起没法归因。
    barge_in = bool(config_snapshot.get("barge_in_enabled")) if config_snapshot else False
    interruption_reason = (
        "not_instrumented_yet" if barge_in and not barge_in_fallback
        else "half_duplex_mode"
    )
    unavailable["interruption_latency_ms"] = interruption_reason
    unavailable["post_interruption_recovery"] = interruption_reason
    # 音频派生指标归 WIL-89 离线审计；录音关闭时连离线也无从谈起。
    recording = bool(config_snapshot.get("recording_enabled")) if config_snapshot else False
    unavailable["audio_derived_metrics"] = (
        "offline_audit_wil89" if recording else "recording_disabled"
    )

    latency_out: dict[str, Any] = {}
    for stage, field in _LATENCY_STAGES.items():
        described = _describe(latencies[stage])
        if described is None:
            unavailable[field] = "no_samples"
        else:
            latency_out[field] = described
    if first_audio_ms is None:
        unavailable["first_audio_ms"] = "no_samples"

    metrics: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        # 调用方（finish）传通话结束时刻，保持纯函数不读钟；直调时才落到 now。
        "generated_at": time.time() if generated_at is None else generated_at,
        "call_id": call_id,
        "direction": direction,
        "duration_s": round(duration_s, 1),
        "answered": answered,
        "status": status,
        "config": config_snapshot,
        "latency": latency_out,
        # greeting 下发→整通首块音频到达；**不是**文档的「接起→首字」——
        # 那要用 answered/greeting_sent 事件的 t_ms 推导（WIL-95 A 类注）。
        "first_audio_ms": first_audio_ms,
        "agent_turn_chars": _describe(agent_turn_chars),
        "peer_turns": peer_turns,
        "tool_calls": tool_calls,
        "dtmf": {"actions": dtmf_actions, "outcomes": dtmf_outcomes},
        "barge_in_fallback": barge_in_fallback,
        "unavailable": unavailable,
    }
    return metrics
