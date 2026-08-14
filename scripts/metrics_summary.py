#!/usr/bin/env python3
"""跨通指标汇总（WIL-95 第一期）：读每通 metrics.json，出 P50/P95。

    .venv/bin/python scripts/metrics_summary.py                  # 人读表格
    .venv/bin/python scripts/metrics_summary.py --json out.json  # 机读，供对比
    .venv/bin/python scripts/metrics_summary.py --dir data/recordings

硬规则（WIL-95 §5/§9 验收 8/11）：

- 按 ``schema_version`` 分组，**拒绝跨版本平均**——版本不同意味着字段定义
  变过，混着算的数没有意义且不会报错。
- 逐方向（inbound/outbound）出数，不做全局大平均——「跨场景平均会让简单
  场景的好数据稀释困难场景的问题」（ATI_Metrics 文档 1.3；完整场景格标签
  在第二期，方向是今天就有的第①维度）。
- 分位数从各通保留的原始 ``values`` 精确合并计算，不做中位数的中位数。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcall.call_metrics import _LATENCY_STAGES  # noqa: E402

_LATENCY_FIELDS = list(_LATENCY_STAGES.values())


def _nearest_rank(ordered: list[float], q: float) -> float:
    """最近秩分位数：ceil(q*n)-1。int() 截断会让小样本高分位系统性偏高
    （scripts/naturalness_audit.py 2026-08-05 评审教训，同一实现）。"""
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def collect(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读取每通 metrics.json；缺文件/坏 JSON 的通话记入 skipped，不悄悄丢。"""
    metrics: list[dict[str, Any]] = []
    skipped: list[str] = []
    for call_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        path = call_dir / "metrics.json"
        if not path.is_file():
            skipped.append(call_dir.name)
            continue
        try:
            metrics.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            skipped.append(call_dir.name)
    return metrics, skipped


def _pool_values(calls: list[dict[str, Any]], field: str) -> list[float]:
    pooled: list[float] = []
    for m in calls:
        entry = (m.get("latency") or {}).get(field)
        if isinstance(entry, dict):
            pooled.extend(
                float(v) for v in entry.get("values", [])
                if isinstance(v, (int, float))
            )
    return pooled


def _describe_pooled(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(_nearest_rank(ordered, 0.5), 1),
        "p95": round(_nearest_rank(ordered, 0.95), 1),
        "max": round(ordered[-1], 1),
    }


def _config_combo(m: dict[str, Any]) -> str:
    """归因分组键：provider/判停/barge-in——切一次开关，时延就换口径。"""
    cfg = m.get("config") or {}
    return "{}/{}/barge_in={}".format(
        cfg.get("provider", "?"),
        cfg.get("turn_detection", "?"),
        cfg.get("barge_in_enabled", "?"),
    )


def summarize(metrics: list[dict[str, Any]], skipped: list[str]) -> dict[str, Any]:
    by_version: dict[int, list[dict[str, Any]]] = {}
    for m in metrics:
        version = m.get("schema_version")
        if isinstance(version, int):
            by_version.setdefault(version, []).append(m)

    versions_out: dict[str, Any] = {}
    for version, calls in sorted(by_version.items()):
        directions: dict[str, Any] = {}
        for direction in sorted({m.get("direction", "unknown") for m in calls}):
            group = [m for m in calls if m.get("direction", "unknown") == direction]
            first_audio = [
                float(m["first_audio_ms"]) for m in group
                if isinstance(m.get("first_audio_ms"), (int, float))
            ]
            directions[direction] = {
                "calls": len(group),
                "latency": {
                    field: _describe_pooled(_pool_values(group, field))
                    for field in _LATENCY_FIELDS
                },
                "first_audio_ms": _describe_pooled(first_audio),
            }
        combos: dict[str, int] = {}
        for m in calls:
            key = _config_combo(m)
            combos[key] = combos.get(key, 0) + 1
        versions_out[str(version)] = {
            "calls": len(calls),
            "directions": directions,
            "config_combos": combos,
            "barge_in_fallback_calls": sum(
                1 for m in calls if m.get("barge_in_fallback")
            ),
        }

    return {
        "calls_analyzed": len(metrics),
        "calls_skipped": len(skipped),
        "skipped_ids": skipped,
        "schema_versions": versions_out,
    }


def _print_human(report: dict[str, Any]) -> None:
    print(
        f"通话 {report['calls_analyzed']} 通"
        f"（跳过 {report['calls_skipped']} 通无/坏 metrics.json）"
    )
    for version, vdata in report["schema_versions"].items():
        print(f"\n== schema v{version}（{vdata['calls']} 通；版本间不可平均）==")
        for direction, ddata in vdata["directions"].items():
            print(f"-- {direction}（{ddata['calls']} 通）--")
            rows = dict(ddata["latency"])
            rows["first_audio_ms"] = ddata["first_audio_ms"]
            for field, stats in rows.items():
                if stats is None:
                    print(f"  {field:28s}  （无样本）")
                else:
                    print(
                        f"  {field:28s}  n={stats['n']:<4d} "
                        f"p50={stats['p50']:<8.1f} p95={stats['p95']:<8.1f} "
                        f"max={stats['max']:.1f}"
                    )
        print("  配置组合：")
        for combo, count in sorted(vdata["config_combos"].items()):
            print(f"    {combo}: {count} 通")
        if vdata["barge_in_fallback_calls"]:
            print(f"  ⚠️ 自激兜底触发过 {vdata['barge_in_fallback_calls']} 通")


def main() -> int:
    # Windows 控制台默认 cp1252，中文表头直接 UnicodeEncodeError——主动切 UTF-8
    # （2026-08-14 本机实测）；reconfigure 仅 TextIOWrapper 有，重定向时兜底跳过。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="data/recordings", help="通话记录根目录")
    parser.add_argument("--json", help="额外输出机读 JSON 到该路径")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 1
    metrics, skipped = collect(root)
    report = summarize(metrics, skipped)
    _print_human(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写机读报告：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
