"""按键是否真的生效，只能看对端接下来说了什么。

#50 第 4 项：「把『按键后菜单是否推进』落成结构化事件，取代只看本机 success:true」。

本机 `result: success` 是**假阳** —— 2026-08-03 拨 10086，4 次按键全部 success，
IVR 菜单一次没推进。

本事件**只记录证据，不下判断**。这个取舍来自 WIL-82 手工判读时踩的三次坑：

1. **单位**：必须按按键记，不能按通话记。实测同一通里第一次按键推进了、第二次
   对端回「很抱歉,小贝没太听清」。
2. **因果**：推进必须发生在按键**之后**。10086 菜单超时会自动转接 —— 实测有一通
   的「正在为您转回」发生在按键**前 18 秒**，按通话判就会把 IVR 的自动行为
   记成按键的功劳。
3. **不写关键词表**：ASR 会输出繁体（「正在**為**您**轉**回」），简体关键词表直接
   漏判。这既是项目非枚举硬原则，也是实测教训。

所以事件里存的是 前一句 / 后一句 / 时延，判读留给离线分析。
"""

from __future__ import annotations

from fakes import FakeModem

from agentcall.call_agent import CallSession


class SpyRecord:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log_event(self, event_type: str, **fields) -> None:
        self.events.append((event_type, fields))


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


def outcomes(record: SpyRecord) -> list[dict]:
    return [f for kind, f in record.events if kind == "dtmf_outcome"]


def press(session: CallSession, record: SpyRecord, action_id: str = "a1") -> None:
    """走真实的挂起入口，再补上 action_id（影子判官关闭时它本就是空的）。"""
    session._arm_dtmf_outcome()
    session._pending_dtmf_outcome["action_id"] = action_id  # type: ignore[index]


def test_evidence_pairs_the_press_with_the_next_remote_line():
    """核心：按键 → 对端下一句，配成一条事件。"""
    session = make_session()
    record = SpyRecord()

    session._settle_dtmf_outcome(record, "转回广东10086请按1,进入湖北10086请按2")  # type: ignore[arg-type]
    press(session, record)
    session._settle_dtmf_outcome(record, "正在为您转回广东10086,请稍候")  # type: ignore[arg-type]

    got = outcomes(record)
    assert len(got) == 1
    assert "请按1" in got[0]["menu_before"], "按键前的菜单必须留证"
    assert "正在为您转回" in got[0]["remote_after"], "按键后对端第一句必须留证"
    assert got[0]["action_id"] == "a1"
    assert got[0]["latency_ms"] >= 0


def test_only_speech_after_the_press_counts():
    """踩坑 ②：菜单超时自动转接发生在按键**前**，不得算作按键的功劳。

    实测那通里，「正在为您转回」在按键前 18 秒就说了。
    """
    session = make_session()
    record = SpyRecord()

    # 对端先自动转接（此时还没按键）
    session._settle_dtmf_outcome(record, "正在为您转回广东10086,请稍候")  # type: ignore[arg-type]
    assert outcomes(record) == [], "按键之前的对端话语不该产生 outcome"

    press(session, record)
    session._settle_dtmf_outcome(record, "副卡副号的各项费用由主号代付")  # type: ignore[arg-type]

    got = outcomes(record)
    assert len(got) == 1
    assert "正在为您转回" not in got[0]["remote_after"], (
        "按键后的证据不能取到按键之前的那句"
    )


def test_each_press_gets_its_own_evidence():
    """踩坑 ①：同一通里不同按键结果可以不同，必须按按键分开记。"""
    session = make_session()
    record = SpyRecord()

    press(session, record, "first")
    session._settle_dtmf_outcome(record, "正在为您转回广东10086")  # type: ignore[arg-type]
    press(session, record, "second")
    session._settle_dtmf_outcome(record, "很抱歉,小贝没太听清")  # type: ignore[arg-type]

    got = outcomes(record)
    assert [o["action_id"] for o in got] == ["first", "second"]
    assert "转回" in got[0]["remote_after"]
    assert "没太听清" in got[1]["remote_after"]


def test_only_the_first_remote_line_after_a_press_is_taken():
    """一次按键只配一条证据，后续对端话语不再重复挂账。"""
    session = make_session()
    record = SpyRecord()

    press(session, record)
    session._settle_dtmf_outcome(record, "正在为您转回")  # type: ignore[arg-type]
    session._settle_dtmf_outcome(record, "欢迎致电中国移动")  # type: ignore[arg-type]
    session._settle_dtmf_outcome(record, "请问您需要什么服务")  # type: ignore[arg-type]

    assert len(outcomes(record)) == 1


def test_traditional_chinese_is_preserved_verbatim():
    """踩坑 ③：ASR 会输出繁体。事件只存原文，不做任何关键词判定。"""
    session = make_session()
    record = SpyRecord()

    press(session, record)
    session._settle_dtmf_outcome(record, "正在為您轉回廣東10086,請稍候")  # type: ignore[arg-type]

    got = outcomes(record)[0]
    assert got["remote_after"] == "正在為您轉回廣東10086,請稍候", (
        "必须原样保留，简繁转换/关键词判定都不该在这里做"
    )


def test_no_verdict_field_is_emitted():
    """本事件刻意不下判断 —— 判读留给离线分析，避免把关键词表塞进热路径。"""
    session = make_session()
    record = SpyRecord()
    press(session, record)
    session._settle_dtmf_outcome(record, "正在为您转回")  # type: ignore[arg-type]

    fields = outcomes(record)[0]
    for banned in ("advanced", "verdict", "success", "result"):
        assert banned not in fields, f"不该有判定字段 {banned}"


def test_text_is_bounded():
    """整段通话不该被搬进事件流。"""
    session = make_session()
    record = SpyRecord()
    press(session, record)
    session._settle_dtmf_outcome(record, "话" * 500)  # type: ignore[arg-type]

    assert len(outcomes(record)[0]["remote_after"]) <= 80


def test_no_digits_in_the_event():
    """项目规定 DTMF 明文不得落盘（#40）。"""
    session = make_session()
    record = SpyRecord()
    press(session, record)
    session._settle_dtmf_outcome(record, "正在为您转回")  # type: ignore[arg-type]

    assert "digits" not in outcomes(record)[0]


def test_recording_failure_never_breaks_the_call():
    session = make_session()

    class BrokenRecord(SpyRecord):
        def log_event(self, event_type: str, **fields) -> None:
            raise RuntimeError("disk full")

    record = BrokenRecord()
    press(session, record)
    session._settle_dtmf_outcome(record, "正在为您转回")  # type: ignore[arg-type]


def test_no_record_is_tolerated():
    session = make_session()
    press(session, SpyRecord())
    session._settle_dtmf_outcome(None, "正在为您转回")


# ---- 接线：真按键路径必须产出证据（不是只有辅助函数能用）----


def test_real_keypress_path_emits_linked_evidence(monkeypatch):
    """端到端：_send_dtmf_raw → dtmf_action，对端下一句 → dtmf_outcome，两者由
    action_id 关联。

    只测 _settle_dtmf_outcome 不够 —— 把 _record_dtmf_action 里那次挂起删掉，
    上面的用例照样全绿。
    """
    from fakes import FakeAgent

    from agentcall.dtmf_judge import DtmfActionLedger

    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_JUDGE_MODE", "off")

    class Judge:
        def record_action(self, entry) -> None:
            pass

        def submit_remote_transcript(self, text: str, *, t_ms: float) -> None:
            pass

    session = make_session()
    record = SpyRecord()
    session._record = record  # type: ignore[assignment]
    session._dtmf_ledger = DtmfActionLedger()
    session._dtmf_judge = Judge()  # type: ignore[assignment]

    handler = session._make_transcript_handler(record, [], FakeAgent())  # type: ignore[arg-type]
    handler("user", "转回广东10086请按1,进入湖北10086请按2,请您按键选择")
    session._send_dtmf_raw("1", source="agent_tool")
    handler("user", "正在为您转回广东10086,请稍候")

    actions = [f for kind, f in record.events if kind == "dtmf_action"]
    got = outcomes(record)
    assert actions and got, "真按键路径没有产出证据"
    assert got[0]["action_id"] == actions[0]["action_id"], "两条事件必须能关联"
    assert "请按1" in got[0]["menu_before"]
    assert "正在为您转回" in got[0]["remote_after"]


# ---- Codex 评审 P1 的四条回归 ----


def test_event_fires_with_the_judge_off(monkeypatch):
    """最要命的一条：DTMF_JUDGE_MODE 默认就是 off。

    挂起点原本放在 _record_dtmf_action 里，而那个方法在判官关闭时提前 return
    —— 于是本功能在**生产默认配置下一次也不会触发**，而我的第一版测试用假判官
    把这个洞盖住了。
    """
    from fakes import FakeAgent

    monkeypatch.setenv("DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_JUDGE_MODE", "off")  # 生产默认

    session = make_session()
    record = SpyRecord()
    session._record = record  # type: ignore[assignment]
    assert session._dtmf_judge is None and session._dtmf_ledger is None

    handler = session._make_transcript_handler(record, [], FakeAgent())  # type: ignore[arg-type]
    handler("user", "转回广东10086请按1,请您按键选择")
    session._send_dtmf_raw("1", source="agent_tool")
    handler("user", "正在为您转回广东10086,请稍候")

    assert len(outcomes(record)) == 1, "判官关闭时本事件也必须产生"


def test_pending_press_does_not_leak_into_the_next_call():
    """跨通话隔离：上一位来电者的话不得写进下一通的记录（隐私，不只是脏数据）。"""
    session = make_session()
    record = SpyRecord()

    session._settle_dtmf_outcome(record, "上一位来电者的私密内容")  # type: ignore[arg-type]
    press(session, record)                      # 挂起，但本通没有下一句了

    session._cancel_spoken_dtmf_followups(clear_recent=True)   # 新通话重置

    session._settle_dtmf_outcome(record, "新通话第一句")  # type: ignore[arg-type]
    assert outcomes(record) == [], "上一通的挂起必须在新通话开始时清掉"


def test_menu_before_does_not_carry_over_across_calls():
    session = make_session()
    record = SpyRecord()
    session._settle_dtmf_outcome(record, "上一通的对端话语")  # type: ignore[arg-type]

    session._cancel_spoken_dtmf_followups(clear_recent=True)

    press(session, record)
    session._settle_dtmf_outcome(record, "本通对端话语")  # type: ignore[arg-type]
    assert outcomes(record)[0]["menu_before"] == "", (
        "menu_before 不能取到上一通的话语"
    )


def test_stale_press_is_not_paired_with_a_much_later_utterance(monkeypatch):
    """被无视的按键常常换来一片沉默；不设窗口它会跟几分钟后的无关话语凑成假证据。"""
    import agentcall.call_agent as module

    monkeypatch.setattr(module, "_OUTCOME_WINDOW_SECONDS", 0.0)
    # 宽限期一并归零：本用例模拟的是「几分钟后」，即已超出任何配对余地。
    # 不归零的话它模拟的其实是「刚超窗」，那属于 late（WIL-97 新增的一态），
    # 与用例名描述的场景不是同一件事。真正的「几分钟后」见
    # test_much_later_speech_still_never_pairs（用 300 秒真实时延）。
    monkeypatch.setattr(module, "_OUTCOME_LATE_GRACE_SECONDS", 0.0)
    session = make_session()
    record = SpyRecord()

    press(session, record)
    session._settle_dtmf_outcome(record, "几分钟后的无关话语")  # type: ignore[arg-type]

    got = outcomes(record)[0]
    assert got["status"] == "unobserved"
    assert got["remote_after"] == "", "超窗后不得把无关话语当成按键的反应"


def test_silence_after_a_press_is_recorded_as_evidence(monkeypatch):
    """沉默本身就是证据 —— 只在「有下一句」时才落事件，会把这类失败整个抹掉。"""
    import agentcall.call_agent as module

    monkeypatch.setattr(module, "_OUTCOME_WINDOW_SECONDS", 0.0)
    session = make_session()
    record = SpyRecord()

    press(session, record)
    session._expire_dtmf_outcome(record)  # type: ignore[arg-type]

    got = outcomes(record)
    assert len(got) == 1 and got[0]["status"] == "unobserved"
    assert got[0]["remote_after"] == ""


def test_expire_is_a_noop_before_the_window_elapses():
    session = make_session()
    record = SpyRecord()
    press(session, record)
    session._expire_dtmf_outcome(record)  # type: ignore[arg-type]
    assert outcomes(record) == [], "窗口内不该提前落超时证据"


# ---- 三态：判不出 ≠ 没生效（WIL-97）----
#
# 真机 2026-08-06 拨 10086：按 1 之后菜单**确实推进**了（对端「回廣東一零零八六,
# 請稍候」），但对端 9207ms 才应答，而窗口是 8000ms，于是被记成超时且丢掉原话。
# 一个用来纠正假阳性的机制，自己产生了假阴性，会让按键有效率被系统性低估。


def test_observed_when_peer_answers_in_window():
    session, record = make_session(), SpyRecord()
    press(session, record)
    session._settle_dtmf_outcome(record, "回廣東一零零八六,請稍候")  # type: ignore[arg-type]
    got = outcomes(record)
    assert len(got) == 1
    assert got[0]["status"] == "observed"
    assert got[0]["window_ms"] == 8000


def test_late_answer_keeps_the_text_and_latency():
    """复刻真机那一例：9207ms > 8000ms 窗口，但对端确实回了。

    必须记成 late 并**保留原话与真实时延**——这正是「窗口该开多长」所需的样本。
    原实现在这里丢掉文本、记成超时，等于把一次成功的按键记成失败。
    """
    import time as _t

    session, record = make_session(), SpyRecord()
    press(session, record)
    session._pending_dtmf_outcome["pressed_at"] = _t.monotonic() - 9.207  # type: ignore[index]
    session._settle_dtmf_outcome(record, "回廣東一零零八六,請稍候")  # type: ignore[arg-type]

    got = outcomes(record)[-1]
    assert got["status"] == "late", f"超窗应答被记成了 {got['status']}"
    assert "廣東" in got["remote_after"], "late 必须保留对端原话，否则证据没了"
    assert got["latency_ms"] >= 9000, "要留真实时延，才知道窗口该开多长"


def test_much_later_speech_still_never_pairs():
    """既有保护不能被削弱：几分钟后的无关话语不得配成证据（原 Codex P1）。"""
    import time as _t

    session, record = make_session(), SpyRecord()
    press(session, record)
    session._pending_dtmf_outcome["pressed_at"] = _t.monotonic() - 300.0  # type: ignore[index]
    session._settle_dtmf_outcome(record, "完全无关的一句")  # type: ignore[arg-type]

    got = outcomes(record)[-1]
    assert got["status"] == "unobserved"
    assert got["remote_after"] == "", "超出宽限期不能把无关话语记成证据"


def test_one_press_emits_exactly_one_event():
    """一次按键只产出一条事件——否则下游计数会重复计（Codex 评审 P1）。"""
    import time as _t

    session, record = make_session(), SpyRecord()
    press(session, record)
    session._pending_dtmf_outcome["pressed_at"] = _t.monotonic() - 9.2  # type: ignore[index]
    session._settle_dtmf_outcome(record, "回廣東一零零八六")  # type: ignore[arg-type]
    session._settle_dtmf_outcome(record, "又说了一句")  # type: ignore[arg-type]
    assert len(outcomes(record)) == 1
