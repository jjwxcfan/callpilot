"""跨通指标汇总与看板数据源（WIL-95 第四期）。

CLI（``scripts/metrics_summary.py``）与 web 看板端点（``/api/metrics/*``）共用
本实现，硬规则一处定义、两处生效：

- 按 ``schema_version`` 分组，**拒绝跨版本平均**（验收 8）——版本不同意味着
  字段定义变过，混着算的数没有意义且不会报错。
- 逐方向（inbound/outbound）出数，不做全局大平均（验收 11；完整场景格标签
  在任务预设补齐后接入）。
- 分位数从各通 metrics.json 保留的原始 ``values`` 精确合并——不做「中位数的
  中位数」，也不重扫 events.jsonl（WIL-76 教训）。
- 判定层（``verdicts.json``）只读展示；人工标注写单独的
  ``verdict_label.json``——标注是地面真值，攒起来就是判官的回归测试集。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from .call_metrics import LATENCY_FIELDS

# 人工标注枚举：对 / 错 / 看不出来（连人都不确定的样本对判官评估同样有价值）。
REVIEW_LABELS = ("correct", "wrong", "unsure")

_LABEL_FILE = "verdict_label.json"


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
            hangup = [
                float(m["hangup_latency_ms"]) for m in group
                if isinstance(m.get("hangup_latency_ms"), (int, float))
            ]
            directions[direction] = {
                "calls": len(group),
                "latency": {
                    field: _describe_pooled(_pool_values(group, field))
                    for field in LATENCY_FIELDS
                },
                "first_audio_ms": _describe_pooled(first_audio),
                "hangup_latency_ms": _describe_pooled(hangup),
            }
        combos: dict[str, int] = {}
        terminations: dict[str, int] = {}
        contact = {"known": 0, "unknown": 0, "na": 0}
        for m in calls:
            key = _config_combo(m)
            combos[key] = combos.get(key, 0) + 1
            kind = (m.get("termination") or {}).get("kind")
            if isinstance(kind, str) and kind:
                terminations[kind] = terminations.get(kind, 0) + 1
            known = m.get("contact_known")
            if known is True:
                contact["known"] += 1
            elif known is False:
                contact["unknown"] += 1
            else:
                contact["na"] += 1
        versions_out[str(version)] = {
            "calls": len(calls),
            "directions": directions,
            "config_combos": combos,
            "terminations": terminations,
            "contact_known": contact,
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


# ---- 判定层（WIL-95 §4）：裁决展示 + 复核队列 + 人工标注 ----


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        return None


def collect_verdicts(root: Path) -> list[dict[str, Any]]:
    """每通的裁决 + 已有人工标注（无裁决的通话不出现在列表里）。"""
    rows: list[dict[str, Any]] = []
    for call_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        verdict = _load_json(call_dir / "verdicts.json")
        if verdict is None:
            continue
        label = _load_json(call_dir / _LABEL_FILE) or {}
        rows.append({
            "call_id": call_dir.name,
            "conclusion": verdict.get("conclusion"),
            "attribution": verdict.get("attribution"),
            "confidence": verdict.get("confidence"),
            "reasons": verdict.get("reasons"),
            "needs_review": bool(verdict.get("needs_review")),
            "review_reason": verdict.get("review_reason"),
            "judge_model": verdict.get("judge_model"),
            "prompt_version": verdict.get("prompt_version"),
            "label": label.get("label"),
        })
    return rows


def review_queue(root: Path) -> list[dict[str, Any]]:
    """待人工复核清单：needs_review 且尚未标注（标注过的就不再占队列）。"""
    return [
        row for row in collect_verdicts(root)
        if row["needs_review"] and not row["label"]
    ]


def write_label(label_path: Path, label: str) -> dict[str, Any]:
    """写人工标注（调用方负责 call_id 校验与路径解析）。

    地面真值只增不改的取舍：重复标注允许覆盖——人会改主意，最后一次为准；
    历史版本不留（要审计轨迹时再升级为追加式）。
    """
    if label not in REVIEW_LABELS:
        raise ValueError(f"label 必须是 {REVIEW_LABELS} 之一，收到 {label!r}")
    payload = {"label": label, "labeled_at": time.time()}
    label_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def build_dashboard_report(root: Path) -> dict[str, Any]:
    """看板一次拉全：指标汇总 + 裁决 rollup + 复核队列。

    量级说明：metrics.json/verdicts.json 都是几百字节的小文件，当前规模
    （几十~几百通）逐目录读取可接受；WIL-76 的教训针对的是每请求重解析
    全部**事件流**，这里刻意只碰汇总物。规模上来再加增量索引。
    """
    metrics, skipped = collect(root)
    verdicts = collect_verdicts(root)
    review = [r for r in verdicts if r["needs_review"] and not r["label"]]
    conclusions: dict[str, int] = {}
    labels: dict[str, int] = {}
    for row in verdicts:
        c = str(row.get("conclusion") or "unknown")
        conclusions[c] = conclusions.get(c, 0) + 1
        if row["label"]:
            labels[row["label"]] = labels.get(row["label"], 0) + 1
    return {
        "summary": summarize(metrics, skipped),
        "verdicts": {
            "total": len(verdicts),
            "conclusions": conclusions,
            "labels": labels,
        },
        "review": review,  # 复用已收集的 verdicts，不二次扫目录
    }
