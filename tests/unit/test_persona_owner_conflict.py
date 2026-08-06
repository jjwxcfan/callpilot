"""AGENT_PERSONA 与 OWNER_NAME 同名时必须回退（WIL-98）。

真机 2026-08-06 13:21 真实来电，两项都配成「罗源」：

    Agent  我是罗源的罗源，目前罗源不方便接电话。
    用户   你是罗原的罗原什么意思?          ← 对端听不懂，只能反问

提示词里到处是 f"{owner}的{persona}"，同名时这句话本身就不成立。更要紧的是
它让「不要冒充{owner}本人」自相矛盾——AI 每次自我介绍都在用机主的名字称呼自己。

空值早就有兜底，同名却直接透传，而同名产出的文本比空值糟得多。
"""

from __future__ import annotations

import pytest

from agentcall.prompts import agent_persona, build_instructions


@pytest.fixture(autouse=True)
def _reset_warn_dedupe():
    """告警去重是模块级状态，每个用例前清掉，否则用例之间会互相吞告警。"""
    from agentcall import prompts

    prompts._warned_persona_conflicts.clear()
    yield
    prompts._warned_persona_conflicts.clear()


def _set(monkeypatch, owner: str, persona: str) -> None:
    monkeypatch.setenv("OWNER_NAME", owner)
    monkeypatch.setenv("AGENT_PERSONA", persona)


# ---- 核心行为 ----


def test_same_name_falls_back_to_neutral_persona(monkeypatch):
    """真机那次的配置：两项都是「罗源」。"""
    _set(monkeypatch, "罗源", "罗源")
    assert agent_persona("zh") == "AI 助理"
    assert agent_persona("en") == "AI assistant"


def test_normal_config_is_untouched(monkeypatch):
    """两者不同 = 绝大多数情况，必须完全不受影响。"""
    _set(monkeypatch, "William", "Lily")
    assert agent_persona("zh") == "Lily"
    assert agent_persona("en") == "Lily"


def test_case_and_whitespace_insensitive(monkeypatch):
    """`William` vs ` william ` 是同一个人，应当算冲突。"""
    _set(monkeypatch, "William", " william ")
    assert agent_persona("zh") == "AI 助理"


def test_different_names_that_merely_overlap_are_fine(monkeypatch):
    """只是包含关系不算冲突——「小李」和「李明」是两个称谓。"""
    _set(monkeypatch, "李明", "小李")
    assert agent_persona("zh") == "小李"


def test_empty_persona_still_falls_back(monkeypatch):
    """原有的空值兜底不能被改坏。"""
    _set(monkeypatch, "William", "")
    assert agent_persona("zh") == "AI 助理"


def test_empty_owner_does_not_trigger_conflict(monkeypatch):
    """机主没配时，人设不该被误判成「与机主同名」而丢掉。"""
    _set(monkeypatch, "", "Lily")
    assert agent_persona("zh") == "Lily"


def test_both_empty(monkeypatch):
    _set(monkeypatch, "", "")
    assert agent_persona("zh") == "AI 助理"


# ---- 告警：不能静默 ----


def test_conflict_is_warned(monkeypatch, caplog):
    """用户得知道自己配的值没生效，否则只会觉得 AI 说话很怪。"""
    _set(monkeypatch, "罗源", "罗源")
    with caplog.at_level("WARNING"):
        agent_persona("zh")
    messages = [r.getMessage() for r in caplog.records]
    assert any("AGENT_PERSONA" in m and "罗源" in m for m in messages), messages


def test_warning_is_not_repeated_every_call(monkeypatch, caplog):
    """agent_persona 每通要被调四五次，不去重会把日志刷满。"""
    _set(monkeypatch, "罗源", "罗源")
    with caplog.at_level("WARNING"):
        for _ in range(5):
            agent_persona("zh")
    hits = [r for r in caplog.records if "AGENT_PERSONA" in r.getMessage()]
    assert len(hits) == 1, f"同一配置告警了 {len(hits)} 次"


def test_normal_config_does_not_warn(monkeypatch, caplog):
    _set(monkeypatch, "William", "Lily")
    with caplog.at_level("WARNING"):
        agent_persona("zh")
    assert not [r for r in caplog.records if "AGENT_PERSONA" in r.getMessage()]


# ---- 端到端：提示词里不再出现「X的X」 ----


def test_instructions_no_longer_say_owner_of_owner(monkeypatch):
    """真正的收益：提示词里不该再出现「罗源的罗源」这种说不通的自称。"""
    _set(monkeypatch, "罗源", "罗源")
    text = build_instructions("inbound", "罗源", agent_persona("zh"), "")
    assert "罗源的罗源" not in text
    assert "罗源的AI 助理" in text or "罗源的AI助理" in text or "AI 助理" in text
