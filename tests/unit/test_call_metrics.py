"""指标契约（WIL-95 第一期）：build_call_metrics 纯函数 + finish 落盘 + 汇总 CLI。

锁住的契约点：schema_version 常驻；测不了记 null + 原因、绝不记 0；隐私
（不含转写文本/按键值）；p90 最近秩；跨版本拒绝合并。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from agentcall.call_log import CallLogger
from agentcall.call_metrics import SCHEMA_VERSION, build_call_metrics


def _load_summary_module():
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "metrics_summary.py"
    )
    spec = importlib.util.spec_from_file_location("metrics_summary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["metrics_summary"] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(**overrides):
    event = {
        "type": "config_snapshot",
        "ts": 1.0,
        "provider": "openai",
        "model": "gpt-realtime-mini",
        "turn_detection": "semantic_vad",
        "vad_eagerness": "auto",
        "vad_silence_ms": 300,
        "barge_in_enabled": False,
        "hangover_seconds": 0.5,
        "dtmf_mode": "qvts",
        "recording_enabled": True,
        "audio_mode": "nmea",
        "modem_vendor": "simcom",
    }
    event.update(overrides)
    return event


def _build(events, **kwargs):
    defaults = dict(
        call_id="20260814-120000-outbound-611",
        direction="outbound",
        duration_s=42.0,
        answered=True,
        status="completed",
    )
    defaults.update(kwargs)
    return build_call_metrics(events, **defaults)


# ---- 契约基本面 ----


def test_schema_version_and_meta_always_present():
    metrics = _build([])
    assert metrics["schema_version"] == SCHEMA_VERSION
    assert metrics["direction"] == "outbound"
    assert metrics["answered"] is True
    assert metrics["status"] == "completed"


def test_latency_stats_use_nearest_rank_p90():
    events = [
        {"type": "latency", "ts": 1.0, "stage": "local_response", "ms": float(v)}
        for v in range(100, 1100, 100)  # 100..1000，10 个样本
    ]
    metrics = _build(events)
    stats = metrics["latency"]["local_response_latency_ms"]
    assert stats["n"] == 10
    assert stats["median"] == 550.0
    # 最近秩 p90 = 第 ceil(0.9*10)=9 个 = 900（int 截断会错取 1000）。
    assert stats["p90"] == 900.0
    assert stats["max"] == 1000.0
    assert stats["values"][0] == 100.0 and stats["values"][-1] == 1000.0


def test_missing_metrics_are_null_with_reason_never_zero():
    metrics = _build([_snapshot()])
    unavailable = metrics["unavailable"]
    # 三个时延 stage 无样本 → 各自记原因；latency 表里不出现（更不会是 0）。
    assert unavailable["local_response_latency_ms"] == "no_samples"
    assert metrics["latency"] == {}
    assert metrics["first_audio_ms"] is None
    assert unavailable["first_audio_ms"] == "no_samples"
    # 先天测不了/未埋点的常驻原因（WIL-95 2.1 / 分期）。
    assert unavailable["e2e_latency_ms"] == "carrier_legs_unobservable"
    assert unavailable["termination_kind"] == "not_instrumented"


def test_interruption_reason_forks_on_barge_in_flag():
    half_duplex = _build([_snapshot(barge_in_enabled=False)])
    assert half_duplex["unavailable"]["interruption_latency_ms"] == "half_duplex_mode"
    barge_in = _build([_snapshot(barge_in_enabled=True)])
    assert (
        barge_in["unavailable"]["interruption_latency_ms"] == "not_instrumented_yet"
    )


def test_audio_derived_reason_forks_on_recording_flag():
    off = _build([_snapshot(recording_enabled=False)])
    assert off["unavailable"]["audio_derived_metrics"] == "recording_disabled"
    on = _build([_snapshot(recording_enabled=True)])
    assert on["unavailable"]["audio_derived_metrics"] == "offline_audit_wil89"


def test_missing_config_snapshot_is_flagged():
    metrics = _build([])
    assert metrics["config"] is None
    assert metrics["unavailable"]["config"] == "config_snapshot_event_missing"


def test_transcript_counts_but_text_never_leaks():
    events = [
        _snapshot(),
        {"type": "transcript", "ts": 2.0, "role": "agent", "text": "您好我是客服"},
        {"type": "transcript", "ts": 3.0, "role": "user", "text": "帮我查话费"},
        {"type": "transcript", "ts": 4.0, "role": "agent", "text": "好的"},
    ]
    metrics = _build(events)
    assert metrics["agent_turn_chars"]["n"] == 2
    assert metrics["peer_turns"] == 1
    # 隐私（WIL-95 §7）：metrics.json 里不允许出现任何转写原文。
    dumped = json.dumps(metrics, ensure_ascii=False)
    assert "您好我是客服" not in dumped
    assert "帮我查话费" not in dumped


def test_tool_dtmf_and_fallback_rollups():
    events = [
        {"type": "tool_call", "ts": 1.0, "tool": "send_sms", "args": {}, "result": {}},
        {"type": "tool_call", "ts": 2.0, "tool": "send_sms", "args": {}, "result": {}},
        {"type": "dtmf_action", "ts": 3.0, "action_id": "a1", "digits_len": 1},
        {"type": "dtmf_outcome", "ts": 4.0, "status": "observed"},
        {"type": "dtmf_outcome", "ts": 5.0, "status": "unobserved"},
        {"type": "barge_in_fallback", "ts": 6.0, "strikes": 3},
    ]
    metrics = _build(events)
    assert metrics["tool_calls"] == {"send_sms": 2}
    assert metrics["dtmf"] == {
        "actions": 1,
        "outcomes": {"observed": 1, "unobserved": 1},
    }
    assert metrics["barge_in_fallback"] is True


# ---- finish 落盘 ----


def test_finish_writes_metrics_json(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=False)
    record = clog.begin_call("outbound", "611")
    record.log_event(**{k: v for k, v in _snapshot().items() if k != "ts"})
    record.log_latency("local_response", 820.5)
    record.log_latency("playout_backlog", 0.0)
    record.log_event("first_audio", ms=1200)
    record.finish("completed")

    metrics = json.loads((record.path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["schema_version"] == SCHEMA_VERSION
    assert metrics["latency"]["local_response_latency_ms"]["n"] == 1
    # 0 是 playout_backlog 的有效读数（无积压），必须保留而不是当缺失。
    assert metrics["latency"]["playout_backlog_ms"]["median"] == 0.0
    assert metrics["first_audio_ms"] == 1200.0
    assert metrics["config"]["provider"] == "openai"


# ---- 汇总 CLI ----


def _write_call(root: Path, name: str, metrics: dict) -> None:
    call_dir = root / name
    call_dir.mkdir(parents=True)
    (call_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False), encoding="utf-8"
    )


def test_summary_pools_values_within_version(tmp_path):
    summary = _load_summary_module()
    base = {
        "schema_version": SCHEMA_VERSION,
        "direction": "outbound",
        "config": {"provider": "openai", "turn_detection": "server_vad",
                   "barge_in_enabled": True},
        "first_audio_ms": 1000.0,
        "barge_in_fallback": False,
    }
    _write_call(tmp_path, "call-a", {
        **base,
        "latency": {"local_response_latency_ms": {
            "n": 2, "median": 200.0, "p90": 300.0, "max": 300.0,
            "values": [200.0, 300.0]}},
    })
    _write_call(tmp_path, "call-b", {
        **base,
        "latency": {"local_response_latency_ms": {
            "n": 2, "median": 500.0, "p90": 600.0, "max": 600.0,
            "values": [500.0, 600.0]}},
    })
    metrics, skipped = summary.collect(tmp_path)
    report = summary.summarize(metrics, skipped)

    stats = report["schema_versions"][str(SCHEMA_VERSION)]["directions"][
        "outbound"]["latency"]["local_response_latency_ms"]
    # 精确合并原始值 [200,300,500,600]，不是中位数的中位数。
    assert stats["n"] == 4
    assert stats["p50"] == 300.0
    assert stats["max"] == 600.0


def test_summary_refuses_cross_version_merge(tmp_path):
    summary = _load_summary_module()
    common = {"direction": "outbound", "config": {}, "first_audio_ms": None,
              "barge_in_fallback": False,
              "latency": {"local_response_latency_ms": {
                  "n": 1, "median": 100.0, "p90": 100.0, "max": 100.0,
                  "values": [100.0]}}}
    _write_call(tmp_path, "call-v1", {"schema_version": 1, **common})
    _write_call(tmp_path, "call-v2", {"schema_version": 2, **common})
    metrics, skipped = summary.collect(tmp_path)
    report = summary.summarize(metrics, skipped)

    versions = report["schema_versions"]
    assert set(versions) == {"1", "2"}, "不同版本必须分组，绝不合并"
    for vdata in versions.values():
        assert vdata["calls"] == 1


def test_summary_reports_skipped_calls(tmp_path):
    summary = _load_summary_module()
    (tmp_path / "call-without-metrics").mkdir()
    metrics, skipped = summary.collect(tmp_path)
    report = summary.summarize(metrics, skipped)
    assert report["calls_skipped"] == 1
    assert report["skipped_ids"] == ["call-without-metrics"]
