"""单轮长度闸门：跑飞的长轮次要被掐掉（WIL-90 / WIL-85 N2）。

**为什么不能只写在提示词里**：`prompts.py:157` 早就写着「一次只说一句、简短自然、
口语化，别长篇大论、别念稿子」，英文版同样（`prompts.py:302`）。而 WIL-89 在 22 通
真实录音上测出来：AI 单轮时长中位 6.8s、p90 17.8s、**最长 36.6s**，单轮字数最长
**436 字**。也就是说这条指令**已经在了，模型照样不听**——与 WIL-83 完全同一形态
（受限话术明令禁止「说会转告」，AI 照说不误）。

所以本闸门是模型之外的确定性手段，判官范式。分工：提示词压中位数，闸门兜长尾。

掐的是**生成**不是**播放**：provider 把整轮音频以突发写入、远快于实时播放
（见 `call_log._build_stereo_mix` 注释），所以按「已播出时长」去掐会完全失效——
等播到 20 秒时，模型早就生成完了，cancel 是个空操作。必须按**已生成时长**判。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallSession


class RecordingAgent(FakeAgent):
    """记录 cancel_response 被调了几次、以及是否声称下发成功。"""

    output_rate = 24000

    def __init__(self, delivered: bool = True) -> None:
        super().__init__()
        self.cancel_calls = 0
        self._delivered = delivered

    async def cancel_response(self) -> bool:
        self.cancel_calls += 1
        return self._delivered


class Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log_event(self, type: str, **fields):  # noqa: A002
        self.events.append((type, fields))

    def __getattr__(self, name):  # 其余 CallRecord 接口一律 no-op
        return lambda *a, **k: None

    def capped(self) -> list[dict]:
        return [f for t, f in self.events if t == "turn_length_capped"]


def make_session(limit: float) -> CallSession:
    session = CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )
    session._max_turn_seconds = limit
    session._set_active(True)
    return session


def run_gate(limit: float, seconds: float, chunk: float = 0.5, record=None):
    """在真实事件循环里喂音频并等打断派发完成。

    必须接真实 loop：闸门只有在 run_coroutine_threadsafe 派发成功之后才会置
    「已打断」标志并落事件（Codex 评审 P2 之三），没有 loop 就等于没掐断。
    """
    async def scenario():
        session = make_session(limit)
        session._loop = asyncio.get_running_loop()
        agent = RecordingAgent()
        rec = Events() if record is None else record
        feed(session, agent, rec, agent.output_rate, seconds, chunk)
        for _ in range(6):
            await asyncio.sleep(0)
        return session, agent, rec

    return asyncio.run(scenario())


def seconds_of(rate: int, seconds: float) -> bytes:
    """生成指定时长的 PCM（16bit 单声道）。"""
    return b"\x00\x00" * int(rate * seconds)


def feed(session: CallSession, agent, record, rate: int, seconds: float, chunk=0.5):
    """按 chunk 秒一块喂进去，模拟 provider 的分块下发。"""
    remaining = seconds
    while remaining > 1e-9:
        step = min(chunk, remaining)
        session._check_turn_length(seconds_of(rate, step), agent, record)
        remaining -= step


# ---- 闸门本身 ----


def test_long_turn_is_capped():
    """超过阈值必须打断，并落一条可观测的事件。"""
    session, agent, record = run_gate(limit=5.0, seconds=8.0)

    assert session._turn_cancel_sent is True
    assert agent.cancel_calls == 1
    assert record.capped(), "必须落 turn_length_capped 事件，否则闸门不可观测"
    assert record.capped()[0]["limit"] == 5.0
    assert record.capped()[0]["seconds"] >= 5.0


def test_normal_turn_is_untouched():
    """正常长度的轮次不能被碰——默认阈值取在 p90 之上就是为了这个。"""
    session, agent, record = run_gate(limit=20.0, seconds=6.8)  # WIL-89 基线中位数

    assert session._turn_cancel_sent is False
    assert agent.cancel_calls == 0
    assert record.capped() == []


def test_cap_fires_only_once_per_turn():
    """cancel 之后余下的分块还会继续到，不能每块都下发一次打断。"""
    _session, agent, record = run_gate(limit=3.0, seconds=10.0)

    assert len(record.capped()) == 1, f"重复下发打断：{record.capped()}"
    assert agent.cancel_calls == 1


def test_new_turn_resets_the_counter(monkeypatch):
    """轮次之间有真空档；空档之后计数与「已打断」标志都要重置，
    否则第一次打断之后整通都不再计数了。"""
    session, agent, record = run_gate(limit=5.0, seconds=8.0)
    assert session._turn_cancel_sent is True

    # 模拟轮次之间的空档
    session._turn_last_chunk_at = time.monotonic() - (session._TURN_GAP_SECONDS + 0.5)
    session._check_turn_length(seconds_of(agent.output_rate, 0.5), agent, record)

    assert session._turn_cancel_sent is False, "新的一轮必须重新开始计数"
    assert session._turn_audio_bytes == len(seconds_of(agent.output_rate, 0.5))


def test_bursty_chunks_within_one_turn_accumulate():
    """provider 是突发写入的：同一轮的分块几乎连着到，必须累加而不是各算各的。"""
    _session, _agent, record = run_gate(limit=5.0, seconds=6.0, chunk=0.5)
    assert record.capped(), "同一轮内的分块没有累加"


# ---- 关闭与边界 ----


@pytest.mark.parametrize("limit", [0.0, -1.0])
def test_gate_disabled(limit):
    """0 = 关闭，回到旧行为。"""
    session, agent, record = run_gate(limit=limit, seconds=60.0, chunk=5.0)
    assert record.capped() == []
    assert session._turn_cancel_sent is False
    assert agent.cancel_calls == 0


def test_empty_chunk_is_ignored():
    session = make_session(limit=5.0)
    agent, record = RecordingAgent(), Events()
    session._check_turn_length(b"", agent, record)
    assert session._turn_audio_bytes == 0


def test_zero_output_rate_does_not_divide_by_zero():
    """采样率拿不到时要安静退出，不能崩在热路径上。"""
    session = make_session(limit=5.0)
    session._agent_output_rate = 0
    agent, record = RecordingAgent(), Events()
    agent.output_rate = 0
    session._check_turn_length(b"\x00\x00" * 100, agent, record)
    assert record.capped() == []


def test_no_record_still_gates():
    """没有录音对象时闸门照样要工作，只是不落事件。"""
    async def scenario():
        session = make_session(limit=3.0)
        session._loop = asyncio.get_running_loop()
        agent = RecordingAgent()
        feed(session, agent, None, agent.output_rate, seconds=6.0)
        for _ in range(6):
            await asyncio.sleep(0)
        return session, agent

    session, agent = asyncio.run(scenario())
    assert session._turn_cancel_sent is True
    assert agent.cancel_calls == 1


# ---- 打断是否**真的派发出去**（Codex 评审 P2）----
#
# 上面那些用例只断言 _turn_cancel_sent 与事件，一个**从不调用 cancel_response()**
# 的实现照样能全绿——正是本项目「绿测试 ≠ 正确」那条教训的形态。以下用例接真实
# 事件循环，断言 provider 侧确实被调到了。


def test_cancel_is_actually_dispatched_to_provider():
    """闸门触发后，provider 的 cancel_response 必须真的被调用。"""
    async def scenario():
        session = make_session(limit=3.0)
        session._loop = asyncio.get_running_loop()
        agent, record = RecordingAgent(), Events()
        feed(session, agent, record, agent.output_rate, seconds=6.0)
        for _ in range(6):
            await asyncio.sleep(0)
        return agent, record

    agent, record = asyncio.run(scenario())
    assert agent.cancel_calls == 1, "闸门触发了却没有真的下发打断"
    assert record.capped()


def test_extra_chunks_do_not_dispatch_more_cancels():
    async def scenario():
        session = make_session(limit=2.0)
        session._loop = asyncio.get_running_loop()
        agent, record = RecordingAgent(), Events()
        feed(session, agent, record, agent.output_rate, seconds=10.0)
        for _ in range(6):
            await asyncio.sleep(0)
        return agent

    assert asyncio.run(scenario()).cancel_calls == 1


def test_truncation_works_without_an_event_loop():
    """没有事件循环也照样截断。

    截断是靠**丢弃下行**完成的，与能不能给 provider 发 cancel 无关。
    2026-08-06 真机验证正是这一条：cancel 被 provider 拒了
    （response_cancel_not_active），若截断依赖它，闸门就完全失效。
    """
    session = make_session(limit=3.0)
    session._loop = None
    agent, record = RecordingAgent(), Events()

    dropped = []
    remaining = 6.0
    while remaining > 1e-9:
        step = min(0.5, remaining)
        dropped.append(
            session._check_turn_length(
                seconds_of(agent.output_rate, step), agent, record
            )
        )
        remaining -= step

    assert any(dropped), "超限后必须开始丢弃下行"
    assert session._turn_cancel_sent is True
    assert record.capped(), "截断已发生，事件必须落"


# ---- 护窗期内生成的音频也要计入（Codex 评审 P1）----


def test_audio_generated_during_dtmf_guard_still_counts():
    """护窗丢的是「要不要送出去」，闸门量的是「模型生成了多久」——两件事。

    放在护窗之后结算，护窗期内生成的音频对闸门完全不可见；而模型边按键边说话
    正是 WIL-49 记录在案的行为，跑飞的长轮次会就此绕过闸门。
    """
    async def scenario():
        session = make_session(limit=3.0)
        session._loop = asyncio.get_running_loop()
        agent, record = RecordingAgent(), Events()
        # 开一段护窗，期间照常喂音频
        session._dtmf_guard_until = time.monotonic() + 10.0
        on_audio = session._make_agent_audio_handler(agent, FakeAudioBridge(), record)
        for _ in range(12):  # 6s > 3s 阈值
            on_audio(seconds_of(agent.output_rate, 0.5))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return agent

    assert asyncio.run(scenario()).cancel_calls == 1, (
        "护窗期内生成的音频没有计入单轮长度，闸门被绕过"
    )


# ---- 静默失效防护（WIL-75 的形态）----


def test_truncation_is_independent_of_cancel_being_accepted():
    """provider 拒绝 cancel 时，截断仍然必须生效。

    这是 2026-08-06 真机验证的核心教训：OpenAI 回 response_cancel_not_active，
    而原实现把截断寄托在 cancel 上，于是事件记了 turn_length_capped、对端却把
    14.4s 和 29.6s 的两轮整段听完了。
    """
    session = make_session(limit=3.0)
    agent, record = RecordingAgent(delivered=False), Events()

    dropped = [
        session._check_turn_length(
            seconds_of(agent.output_rate, 0.5), agent, record
        )
        for _ in range(12)
    ]
    assert any(dropped), "provider 不接受 cancel 时，截断依然要靠丢弃下行生效"
    assert record.capped()


def test_cancel_rejection_is_not_a_warning(caplog):
    """cancel 被拒是**常态**不是故障：闸门不依赖它，不该刷 WARNING。"""
    session = make_session(limit=5.0)
    agent = RecordingAgent(delivered=False)
    with caplog.at_level("WARNING"):
        asyncio.run(session._cancel_agent_turn(agent))
    assert agent.cancel_calls == 1
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_cancel_exception_does_not_escape():
    """provider 抛异常不能把通话主循环带崩。"""
    class Boom(RecordingAgent):
        async def cancel_response(self) -> bool:
            raise RuntimeError("ws dead")

    session = make_session(limit=5.0)
    asyncio.run(session._cancel_agent_turn(Boom()))  # 不抛就算过


# ---- 基类默认实现 ----


def test_base_agent_reports_unsupported():
    """基类默认「没下发」——新 provider 忘了实现时，调用方能看出来。"""
    from agentcall.agents.base import VoiceAgent

    class Bare(VoiceAgent):
        async def start(self, on_audio_out):  # pragma: no cover
            pass

        async def send_audio(self, pcm):  # pragma: no cover
            pass

        async def say(self, instructions):  # pragma: no cover
            pass

        async def stop(self):  # pragma: no cover
            pass

    assert asyncio.run(Bare().cancel_response()) is False


# ---- 轮首静音掐除（WIL-112 最后一块：TTS 前缘垫中位 470ms 纯静音）----

def _trim_session() -> CallSession:
    session = CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider="qwen",
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )
    from agentcall.call_agent import _TURN_TRIM_CAP_BYTES
    session._turn_trim_budget = _TURN_TRIM_CAP_BYTES
    session._turn_trimmed_bytes = 0
    return session


def test_leading_silence_is_trimmed_until_first_voiced_sample():
    session = _trim_session()
    silence = b"\x00\x00" * 160
    assert session._trim_leading_silence(silence) == b""       # 整块静音被掐
    mixed = b"\x00\x00" * 100 + b"\x00\x10" * 60               # 静音前缀+语音
    out = session._trim_leading_silence(mixed)
    assert out == b"\x00\x10" * 60                             # 前缀被掐、语音保留
    # 见声后本轮预算清零：后续静音（句中停顿）原样通过
    pause = b"\x00\x00" * 160
    assert session._trim_leading_silence(pause) == pause


def test_leading_silence_trim_respects_cap():
    session = _trim_session()
    from agentcall.call_agent import _TURN_TRIM_CAP_BYTES
    # 连续灌超过上限的纯静音：超出上限的部分必须放行（防止无声轮被无限吞）
    chunk = b"\x00\x00" * 8000  # 16000B = 1s
    first = session._trim_leading_silence(chunk)
    assert first == b""
    second = session._trim_leading_silence(chunk)
    swallowed = _TURN_TRIM_CAP_BYTES - len(chunk)
    assert len(second) == len(chunk) - swallowed
