"""护窗内丢弃的 Agent 语音必须在记录里留痕。

背景（#81，由 #45 的 Codex 评审衍生）：按键护窗期间 `on_agent_audio` 直接丢弃
Agent 下行，`record.write_downlink` 在护窗判断之后，所以**录音只记实际发出的
内容** —— 这是刻意的，录音应当反映对端真正听到了什么。

但**文字 transcript 不受护窗影响**：模型「说」的那句话仍会完整落进 transcript，
哪怕它一个字节都没发出去。事后复盘一通失败的 IVR 导航时会看到转写与录音对不上，
误判成「音频链路丢包」或「录音坏了」，而实际上是护窗按设计工作。
"""

from __future__ import annotations

import time

from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallSession


class SpyRecord:
    """够用的 CallRecord 替身：事件按发生顺序记下来，好断言先后关系。"""

    id = "20260803-000000-outbound-10086"
    path = None
    recording_enabled = False

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.finished: str | None = None

    def log_event(self, event_type: str, **fields) -> None:
        self.events.append((event_type, fields))

    def log_latency(self, stage: str, ms: float, **fields) -> None:
        # 与真 CallRecord 同构：latency 是 log_event 的糖（call_log.py:205）。
        self.log_event("latency", stage=stage, ms=ms, **fields)

    def write_downlink(self, pcm: bytes) -> None:
        pass

    def finish(self, status: str) -> None:
        self.finished = status
        self.events.append(("__finish__", {"status": status}))

    def set_summary(self, *args, **kwargs) -> None:
        pass


def make_session() -> CallSession:
    session = CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )
    session._set_active(True)
    return session


def drops(record: SpyRecord) -> list[dict]:
    return [f for kind, f in record.events if kind == "agent_audio_dropped"]


def test_dropped_audio_is_recorded_once_the_window_closes(monkeypatch):
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "400")
    session = make_session()
    record = SpyRecord()
    handler = session._make_agent_audio_handler(
        FakeAgent(), FakeAudioBridge(), record  # type: ignore[arg-type]
    )

    session._send_dtmf_raw("1", source="agent_tool")
    speech = b"\x33\x33" * 480
    handler(speech)
    handler(speech)
    assert drops(record) == [], "窗口还没结束，不该提前落事件"

    session._dtmf_guard_until = 0.0
    handler(speech)  # 窗口结束后的第一块音频触发汇总

    recorded = drops(record)
    assert len(recorded) == 1, f"每个护窗应只落一条事件，实际 {len(recorded)}"
    assert recorded[0]["reason"] == "dtmf_guard"
    assert recorded[0]["bytes"] == len(speech) * 2
    assert recorded[0]["duration_ms"] > 0


def test_no_event_when_nothing_was_dropped(monkeypatch):
    """没丢东西就不该写事件，否则 events.jsonl 全是噪声。"""
    monkeypatch.setenv("DTMF_GUARD_MS", "400")
    session = make_session()
    record = SpyRecord()
    handler = session._make_agent_audio_handler(
        FakeAgent(), FakeAudioBridge(), record  # type: ignore[arg-type]
    )

    handler(b"\x33\x33" * 480)
    handler(b"\x33\x33" * 480)

    assert drops(record) == []


def test_hot_path_aggregates_instead_of_logging_per_chunk(monkeypatch):
    """热路径约每 20ms 一块，逐块记事件会把 events.jsonl 冲垮。"""
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "5000")
    session = make_session()
    record = SpyRecord()
    handler = session._make_agent_audio_handler(
        FakeAgent(), FakeAudioBridge(), record  # type: ignore[arg-type]
    )

    session._send_dtmf_raw("1", source="agent_tool")
    for _ in range(100):
        handler(b"\x33\x33" * 240)

    assert drops(record) == [], "护窗期间不得逐块写事件"
    session._dtmf_guard_until = 0.0
    handler(b"\x33\x33" * 240)
    assert len(drops(record)) == 1, "100 块只应汇总成 1 条"


def test_counter_resets_per_call(monkeypatch):
    """累计值是每通电话的，不能跨通话累加。"""
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "5000")
    session = make_session()
    record = SpyRecord()
    handler = session._make_agent_audio_handler(
        FakeAgent(), FakeAudioBridge(), record  # type: ignore[arg-type]
    )
    session._send_dtmf_raw("1", source="agent_tool")
    handler(b"\x33\x33" * 480)
    assert session._dtmf_guard_dropped_bytes > 0

    session._cancel_spoken_dtmf_followups(clear_recent=True)

    assert session._dtmf_guard_dropped_bytes == 0


def test_accounting_failure_never_breaks_the_call(monkeypatch):
    """记账是辅助功能，写事件失败不能把通话搞挂。"""
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "400")
    session = make_session()

    class BrokenRecord(SpyRecord):
        def log_event(self, event_type: str, **fields) -> None:
            raise RuntimeError("disk full")

    record = BrokenRecord()
    handler = session._make_agent_audio_handler(
        FakeAgent(), FakeAudioBridge(), record  # type: ignore[arg-type]
    )
    session._send_dtmf_raw("1", source="agent_tool")
    handler(b"\x33\x33" * 480)
    session._dtmf_guard_until = 0.0

    handler(b"\x33\x33" * 480)  # 不得抛出
    assert time.monotonic() >= session._dtmf_guard_until


def test_pending_bytes_are_flushed_at_call_teardown(monkeypatch):
    """Codex P1：模型按完键就安静，等不到下一块非护窗音频。

    这恰恰是最常见的形态（「我按一下」→ 按键 → 等 IVR），若只靠后续音频触发
    汇总，这条事件会整个丢掉——而它正是用来解释这一段的。
    """
    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_GUARD_MS", "5000")
    session = make_session()
    record = SpyRecord()
    handler = session._make_agent_audio_handler(
        FakeAgent(), FakeAudioBridge(), record  # type: ignore[arg-type]
    )

    session._send_dtmf_raw("1", source="agent_tool")
    handler(b"\x33\x33" * 480)
    handler(b"\x33\x33" * 480)
    assert drops(record) == [], "窗口内不落事件"

    # 之后再没有任何音频；通话直接收尾。
    session._finalize_record(record, "completed", [], "outbound", "10086")  # type: ignore[arg-type]

    recorded = drops(record)
    assert len(recorded) == 1, "收尾时必须把最后一个护窗的账结掉"
    assert recorded[0]["bytes"] == 480 * 2 * 2
    kinds = [kind for kind, _ in record.events]
    assert kinds.index("agent_audio_dropped") < kinds.index("__finish__"), (
        "必须在 record.finish 之前落账，否则事件进不了这通记录"
    )


def test_teardown_flush_is_idempotent(monkeypatch):
    """收尾兜底不能在没丢东西时凭空写事件。"""
    session = make_session()
    record = SpyRecord()
    session._finalize_record(record, "completed", [], "outbound", "10086")  # type: ignore[arg-type]
    assert drops(record) == []
