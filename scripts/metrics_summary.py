#!/usr/bin/env python3
"""跨通指标汇总（WIL-95）：读每通 metrics.json，出 P50/P95。

    .venv/bin/python scripts/metrics_summary.py                  # 人读表格
    .venv/bin/python scripts/metrics_summary.py --json out.json  # 机读，供对比
    .venv/bin/python scripts/metrics_summary.py --dir data/recordings

汇总规则（按版本分组拒绝跨版本平均、逐方向出数、原始值精确合并）实现在
``agentcall.metrics_report``——web 看板端点共用同一实现，本脚本只负责 CLI 输出。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcall.metrics_report import collect, summarize  # noqa: E402,F401

__all__ = ["collect", "summarize", "main"]


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
            rows["hangup_latency_ms"] = ddata.get("hangup_latency_ms")
            for field, stats in rows.items():
                if stats is None:
                    print(f"  {field:28s}  （无样本）")
                else:
                    print(
                        f"  {field:28s}  n={stats['n']:<4d} "
                        f"p50={stats['p50']:<8.1f} p95={stats['p95']:<8.1f} "
                        f"max={stats['max']:.1f}"
                    )
        if vdata.get("terminations"):
            pairs = "  ".join(
                f"{k}={v}" for k, v in sorted(vdata["terminations"].items())
            )
            print(f"  终止方式：{pairs}")
        contact = vdata.get("contact_known") or {}
        if contact.get("known") or contact.get("unknown"):
            print(
                f"  熟人占比：known={contact.get('known', 0)} "
                f"unknown={contact.get('unknown', 0)} na={contact.get('na', 0)}"
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
