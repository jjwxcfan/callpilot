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
