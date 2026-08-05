#!/usr/bin/env python3
"""自然度审计：跑一遍已有录音，产出「像不像人」的基线。

WIL-89 / 父规格 WIL-85 第四节。指标定义与实现在 ``agentcall.naturalness``，
本脚本只负责遍历、汇总与输出。

    .venv/bin/python scripts/naturalness_audit.py                  # 人读
    .venv/bin/python scripts/naturalness_audit.py --json out.json  # 机读，供复测对比
    .venv/bin/python scripts/naturalness_audit.py --dir data/recordings

输出两份：人读表格用于当场判断，JSON 用于 WIL-93 复测时逐项对比。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcall.naturalness import CallMetrics, analyze_call  # noqa: E402

# 与 agentcall.naturalness 的定义绑定。改了任何指标的算法就要升版本，
# 否则历史 JSON 会静默变得不可比（WIL-95 第五节同一条要求）。
SCHEMA_VERSION = 1


def collect(root: Path) -> tuple[list[CallMetrics], list[str]]:
    metrics: list[CallMetrics] = []
    skipped: list[str] = []
    for call_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        result = analyze_call(call_dir)
        if result is None:
            skipped.append(call_dir.name)
        else:
            metrics.append(result)
    return metrics, skipped


def _pool(metrics: list[CallMetrics], attr: str) -> list[float]:
    pooled: list[float] = []
    for m in metrics:
        pooled.extend(getattr(m, attr))
    return pooled


def _describe(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "median": None, "p90": None, "max": None}
    ordered = sorted(values)
    # 最近秩 p90：ceil(0.9n) - 1。用 int(0.9n) 是错的——n=10 时它给出下标 9，
    # 也就是最大值，会让小样本的 p90 系统性偏高（2026-08-05 Codex 评审 P2）。
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 1),
        "p90": round(ordered[rank - 1], 1),
        "max": round(ordered[-1], 1),
    }


def summarize(metrics: list[CallMetrics], skipped: list[str]) -> dict:
    yields = _pool(metrics, "yield_after_interruption_ms")
    interruptions = sum(m.interruption_count for m in metrics)
    return {
        "schema_version": SCHEMA_VERSION,
        "calls_analyzed": len(metrics),
        "calls_skipped": len(skipped),
        "skipped_ids": skipped,
        "metrics": {
            "response_latency_ms": _describe(_pool(metrics, "response_latency_ms")),
            "agent_turn_ms": _describe(_pool(metrics, "agent_turn_ms")),
            "agent_turn_chars": _describe(
                [float(c) for c in _pool(metrics, "agent_turn_chars")]
            ),
            "opening_ms": _describe(
                [m.opening_ms for m in metrics if m.opening_ms is not None]
            ),
            "yield_after_interruption_ms": _describe(yields),
        },
        "interruptions_total": interruptions,
        "late_overlaps_excluded": sum(m.late_overlaps for m in metrics),
        # 按方向分开统计打断：外呼打到 IVR 时，「对方」是在播菜单的机器，
        # 它压着 AI 说话不是「有人插话」。混在一起会把打断率讲成一个假故事。
        "interruptions_by_direction": {
            d: sum(m.interruption_count for m in metrics if m.direction == d)
            for d in ("inbound", "outbound", "unknown")
        },
        "calls_by_direction": {
            d: sum(1 for m in metrics if m.direction == d)
            for d in ("inbound", "outbound", "unknown")
        },
        "calls": [m.to_dict() for m in metrics],
    }


def render(report: dict) -> str:
    out: list[str] = []
    add = out.append
    add("=" * 78)
    add("自然度基线 · WIL-89   (schema v%d)" % report["schema_version"])
    add("=" * 78)
    add(
        f"分析 {report['calls_analyzed']} 通，"
        f"跳过 {report['calls_skipped']} 通（缺 mixed.wav 或 uplink.wav）"
    )
    add("")
    add(f"{'指标':<24}{'样本':>7}{'中位数':>10}{'p90':>10}{'最大':>10}")
    add("-" * 78)
    labels = {
        "response_latency_ms": "应答时延↑ (ms)",
        "agent_turn_ms": "AI 单轮时长 (ms)",
        "agent_turn_chars": "AI 单轮字数",
        "opening_ms": "开场白时长 (ms)",
        "yield_after_interruption_ms": "被打断后仍说 (ms)",
    }
    for key, label in labels.items():
        d = report["metrics"][key]
        if not d["n"]:
            add(f"{label:<24}{'—':>7}{'不可用':>10}")
            continue
        add(
            f"{label:<24}{d['n']:>7}{d['median']:>10.0f}"
            f"{d['p90']:>10.0f}{d['max']:>10.0f}"
        )
    add("-" * 78)
    by_dir = report["interruptions_by_direction"]
    calls_by_dir = report["calls_by_direction"]
    add(f"打断总次数: {report['interruptions_total']}"
        f"   （尾部重叠已排除 {report['late_overlaps_excluded']} 次）")
    add(f"  来电 {by_dir['inbound']} 次 / {calls_by_dir['inbound']} 通"
        f"    外呼 {by_dir['outbound']} 次 / {calls_by_dir['outbound']} 通")
    add("  ⚠ 外呼多为拨 10086，「对方」是在播菜单的 IVR——它压着 AI 说话")
    add("    并不是「有人插话」。判断打断率请只看来电那一列。")
    add("↑ 应答时延是**上限**：含 server_vad 为确认对方说完而刻意等的静音时长，")
    add("  不是纯处理时延。要压这个数就得调 VAD 静音阈值，代价见 WIL-85 N3。")
    add("")
    add("【WIL-94 的反面基线】")
    y = report["metrics"]["yield_after_interruption_ms"]
    if y["n"]:
        add(f"  对方插话后 AI 仍继续说了 中位 {y['median']:.0f}ms、最长 {y['max']:.0f}ms。")
        add("  当前代码里对方的上行在 AI 说话期间被丢弃（call_agent.py:795），")
        add("  AI 收不到插话——所以这个分布反映的是「该轮剩余时长」，不是让路时延。")
        add("  WIL-94 做完后，这个分布应当整体塌向几百毫秒以内。")
    else:
        add("  样本里没有检出打断，无法给出基线——需要补录含打断的通话。")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="自然度审计 (WIL-89)")
    parser.add_argument("--dir", default="data/recordings", help="录音根目录")
    parser.add_argument("--json", help="同时写一份 JSON，供 WIL-93 复测对比")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"录音目录不存在: {root}", file=sys.stderr)
        return 1

    metrics, skipped = collect(root)
    if not metrics:
        print("没有可分析的通话（都缺 mixed.wav 或 uplink.wav）", file=sys.stderr)
        return 1

    report = summarize(metrics, skipped)
    print(render(report))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
