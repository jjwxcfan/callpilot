"""看板数据源（WIL-95 第四期）：汇总扩展、裁决/复核/标注、web 端点全链路。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentcall.call_log import CallLogger
from agentcall.metrics_report import (
    build_dashboard_report,
    collect,
    collect_verdicts,
    review_queue,
    summarize,
    write_label,
)
from agentcall.web.server import build_app


def _write_call(root: Path, name: str, metrics: dict | None = None,
                verdict: dict | None = None) -> Path:
    call_dir = root / name
    call_dir.mkdir(parents=True, exist_ok=True)
    if metrics is not None:
        (call_dir / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False), encoding="utf-8"
        )
    if verdict is not None:
        (call_dir / "verdicts.json").write_text(
            json.dumps(verdict, ensure_ascii=False), encoding="utf-8"
        )
    return call_dir


def _metrics(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "direction": "outbound",
        "config": {"provider": "openai", "turn_detection": "server_vad",
                   "barge_in_enabled": False},
        "latency": {},
        "first_audio_ms": None,
        "hangup_latency_ms": 800.0,
        "termination": {"kind": "peer_hangup", "reason": "inferred_no_local_signal"},
        "contact_known": True,
        "barge_in_fallback": False,
    }
    base.update(overrides)
    return base


def _verdict(**overrides) -> dict:
    base = {
        "schema_version": 1, "prompt_version": 1, "judge_model": "qwen/qwen-plus",
        "conclusion": "uncertain", "attribution": "unknown", "confidence": 0.0,
        "reasons": "no_task_goal", "needs_review": True, "review_reason": "uncertain",
    }
    base.update(overrides)
    return base


# ---- 汇总扩展：终止分布 / 挂断时延 / 熟人占比 ----


def test_summary_rolls_up_terminations_hangup_and_contact(tmp_path):
    _write_call(tmp_path, "a", _metrics())
    _write_call(tmp_path, "b", _metrics(
        termination={"kind": "agent_hangup", "reason": "hangup_tool"},
        contact_known=False, hangup_latency_ms=1200.0,
    ))
    report = summarize(*collect(tmp_path))
    vdata = report["schema_versions"]["2"]
    assert vdata["terminations"] == {"peer_hangup": 1, "agent_hangup": 1}
    assert vdata["contact_known"] == {"known": 1, "unknown": 1, "na": 0}
    stats = vdata["directions"]["outbound"]["hangup_latency_ms"]
    assert stats["n"] == 2 and stats["max"] == 1200.0


def test_summary_rolls_up_v3_additions(tmp_path):
    """v3 汇总：归因表 / DTMF / 接管 / 工具 / pcm 指纹 / 对话画像 / 趋势序列。"""
    _write_call(tmp_path, "a", _metrics(
        schema_version=3, generated_at=100.0, call_id="a", duration_s=42.0,
        peer_turns=5,
        latency={"local_response_latency_ms": {
            "n": 2, "median": 900.0, "p90": 1000.0, "max": 1000.0,
            "values": [800.0, 1000.0]}},
        dtmf={"actions": 3, "outcomes": {"advanced": 2, "no_progress": 1}},
        tool_calls={"send_sms": 1},
        takeover={"requested": 1, "committed": 1},
        takeover_latency_ms=3500.0,
        answered_to_first_audio_ms=1600.0,
        abnormal_drop=False, pcm_enable_failed=False, pcm_degraded=False,
    ))
    _write_call(tmp_path, "b", _metrics(
        schema_version=3, generated_at=50.0, call_id="b", duration_s=30.0,
        peer_turns=1,
        termination={"kind": "dead_media", "reason": "uplink_digital_silence"},
        abnormal_drop=True, pcm_enable_failed=True, pcm_degraded=True,
    ))
    report = summarize(*collect(tmp_path))
    vdata = report["schema_versions"]["3"]
    assert vdata["termination_reasons"] == {
        "peer_hangup": {"inferred_no_local_signal": 1},
        "dead_media": {"uplink_digital_silence": 1},
    }
    assert vdata["dtmf"] == {
        "calls_with_actions": 1, "actions": 3,
        "outcomes": {"advanced": 2, "no_progress": 1},
    }
    assert vdata["takeover"] == {"requested": 1, "committed": 1}
    assert vdata["tool_calls"] == {"send_sms": 1}
    assert vdata["abnormal_drop_calls"] == 1
    # pcm 占比分母只算已埋点的通（None/缺键的老通话不进分母）。
    assert vdata["pcm"] == {"instrumented": 2, "enable_failed": 1, "degraded": 1}
    group = vdata["directions"]["outbound"]
    assert group["duration_s"]["n"] == 2 and group["duration_s"]["max"] == 42.0
    assert group["peer_turns"]["max"] == 5.0
    assert group["answered_to_first_audio_ms"]["n"] == 1
    assert group["takeover_latency_ms"]["p50"] == 3500.0
    # 趋势按 generated_at 升序，每通一点：逐轮字段取中位数，标量取原值。
    assert [p["call_id"] for p in vdata["trend"]] == ["b", "a"]
    a_point = vdata["trend"][1]
    assert a_point["values"]["local_response_latency_ms"] == 900.0
    assert a_point["values"]["answered_to_first_audio_ms"] == 1600.0


def test_summary_tolerates_v2_calls_without_v3_fields(tmp_path):
    """老 v2 metrics.json 无 v3 键：不报错、进 na/未埋点桶，不猜值。"""
    _write_call(tmp_path, "old", _metrics())
    report = summarize(*collect(tmp_path))
    vdata = report["schema_versions"]["2"]
    assert vdata["pcm"] == {"instrumented": 0, "enable_failed": 0, "degraded": 0}
    assert vdata["abnormal_drop_calls"] == 0
    assert vdata["directions"]["outbound"]["answered_to_first_audio_ms"] is None


# ---- 判定层：复核队列与标注 ----


def test_review_queue_excludes_labeled_calls(tmp_path):
    _write_call(tmp_path, "a", verdict=_verdict())
    labeled = _write_call(tmp_path, "b", verdict=_verdict())
    write_label(labeled / "verdict_label.json", "wrong")
    _write_call(tmp_path, "c", verdict=_verdict(
        conclusion="achieved", needs_review=False, review_reason=None,
    ))

    queue = review_queue(tmp_path)
    assert [row["call_id"] for row in queue] == ["a"]

    report = build_dashboard_report(tmp_path)
    assert report["verdicts"]["total"] == 3
    assert report["verdicts"]["conclusions"] == {"uncertain": 2, "achieved": 1}
    assert report["verdicts"]["labels"] == {"wrong": 1}


def test_write_label_rejects_unknown_values(tmp_path):
    with pytest.raises(ValueError):
        write_label(tmp_path / "verdict_label.json", "maybe")


# ---- web 端点全链路 ----


class _FakeService:
    def __init__(self, call_logger: CallLogger) -> None:
        self.call_logger = call_logger
        self.session = None


def _api(app, fn):
    async def runner():
        async with TestClient(TestServer(app)) as client:
            return await fn(client)

    return asyncio.run(runner())


def _make_app(tmp_path: Path):
    clog = CallLogger(tmp_path)
    return build_app(  # type: ignore[arg-type]
        hub=None, modem=None, service=_FakeService(clog)
    )


def test_metrics_summary_endpoint_serves_dashboard_report(tmp_path):
    _write_call(tmp_path, "call-1", _metrics(), _verdict())
    app = _make_app(tmp_path)

    async def scenario(client):
        res = await client.get("/api/metrics/summary")
        assert res.status == 200
        return await res.json()

    data = _api(app, scenario)
    assert data["summary"]["calls_analyzed"] == 1
    assert data["verdicts"]["total"] == 1
    assert data["review"][0]["call_id"] == "call-1"


def test_metrics_label_endpoint_writes_and_validates(tmp_path):
    call_dir = _write_call(tmp_path, "call-1", _metrics(), _verdict())
    app = _make_app(tmp_path)

    async def scenario(client):
        ok = await client.post(
            "/api/metrics/label",
            json={"call_id": "call-1", "label": "correct"},
        )
        bad_label = await client.post(
            "/api/metrics/label",
            json={"call_id": "call-1", "label": "maybe"},
        )
        traversal = await client.post(
            "/api/metrics/label",
            json={"call_id": "../evil", "label": "correct"},
        )
        missing = await client.post(
            "/api/metrics/label",
            json={"call_id": "nonexistent", "label": "correct"},
        )
        return ok.status, bad_label.status, traversal.status, missing.status

    statuses = _api(app, scenario)
    assert statuses == (200, 400, 400, 404)
    label = json.loads(
        (call_dir / "verdict_label.json").read_text(encoding="utf-8")
    )
    assert label["label"] == "correct"
    # 标注后不再占复核队列。
    assert review_queue(tmp_path) == []


# ---- 网络边界不带通话原文（WIL-131）----


def _seed_verdict_call(root, call_id, *, needs_review=True):
    d = root / call_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "verdicts.json").write_text(json.dumps({
        "conclusion": "achieved",
        "attribution": "agent",
        "confidence": 0.8,
        "reasons": "对方说本月账单是 128 元，机主身份证后四位 1234 已核对",
        "evidence_refs": ["客服：请提供身份证后四位", "AI：1234"],
        "needs_review": needs_review,
        "review_reason": "no_hard_evidence",
        "judge_model": "gpt-4o",
        "prompt_version": "v1",
    }, ensure_ascii=False), encoding="utf-8")
    return d


def test_dashboard_report_strips_call_content_from_review_rows(tmp_path):
    """判官散文会引用通话原文，不能随无鉴权接口下发。

    该字段是模型用自己的话复述这通发生了什么，提示词明确要求引用证据片段；
    而看板当前根本没有渲染复核队列，等于挂在网上却没人看——最不该有的形态。
    """
    _seed_verdict_call(tmp_path, "20260812-041410-outbound-10086")

    report = build_dashboard_report(tmp_path)

    assert report["review"], "复核队列本身要保留"
    row = report["review"][0]
    assert "reasons" not in row, "判官散文不得下发"
    assert "evidence_refs" not in row, "证据片段不得下发"
    # 机器产出的判定元数据照常保留，复核队列才有用
    assert row["conclusion"] == "achieved"
    assert row["needs_review"] is True
    assert row["review_reason"] == "no_hard_evidence"
    # 兜底：整份响应里不得出现通话原文
    assert "128 元" not in json.dumps(report, ensure_ascii=False)


def test_on_disk_and_cli_paths_keep_full_verdict(tmp_path):
    """收窄只发生在 HTTP 边界：盘上与 CLI 仍是全量，离线分析不受影响。"""
    _seed_verdict_call(tmp_path, "20260812-041410-outbound-10086")

    rows = collect_verdicts(tmp_path)
    assert rows[0]["reasons"].startswith("对方说本月账单")
    assert review_queue(tmp_path)[0]["reasons"]
