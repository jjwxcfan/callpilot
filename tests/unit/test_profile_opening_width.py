"""开场白长度按显示宽度计，且内置示例必须能通过自身校验。

回归：上限曾是裸字符数 40，对中文是一整句、对英文只有约 7 个词。
内置 data/number_profiles.example.json 里 china_mobile_balance 的英文
opening 恰好 43 字符 → 首启种子生成的预设一打开编辑就存不回去，
即使用户只想改 scenario。缺的正是「示例数据必须通过自身校验」这条测试。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcall.number_profiles import ProfileValidationError, _validate_profile_payload
from agentcall.prompt_gen import MAX_OPENING_WIDTH, display_width

_EXAMPLE = Path(__file__).resolve().parents[2] / "data" / "number_profiles.example.json"


def _example_profiles() -> list[dict]:
    return json.loads(_EXAMPLE.read_text(encoding="utf-8"))["profiles"]


def test_display_width_counts_cjk_as_two_latin_as_one():
    assert display_width("abcd") == 4
    assert display_width("你好") == 4
    assert display_width("你好ab") == 6
    assert display_width("") == 0


@pytest.mark.parametrize("profile", _example_profiles(), ids=lambda p: p["id"])
def test_every_shipped_example_profile_passes_its_own_validation(profile):
    """内置示例必须能原样通过校验——本 bug 的直接成因就是缺这条断言。"""
    payload = {k: v for k, v in profile.items() if not k.startswith("_")}
    _validate_profile_payload(payload)


def test_the_exact_english_opening_that_used_to_fail_now_passes():
    text = "Hi, I'd like to check last month's charges."
    assert len(text) == 43, "回归基准：这正是曾经超 40 字符上限的那句"
    assert display_width(text) <= MAX_OPENING_WIDTH
    _validate_profile_payload(
        {"number": "10086", "task": "t", "scenario": "s", "opening": {"en": text, "zh": "你好"}}
    )


def test_chinese_opening_is_still_meaningfully_bounded():
    """放宽不等于放开：中文仍受约束，不能退化成整段文字。"""
    too_long = "你" * 60  # 120 宽度
    with pytest.raises(ProfileValidationError):
        _validate_profile_payload(
            {"number": "10086", "task": "t", "scenario": "s", "opening": too_long}
        )


def test_a_natural_english_sentence_is_accepted():
    text = "Hello, I am calling on behalf of the account owner to ask about last month's billing details."
    assert len(text) >= 90, "要覆盖一句真正自然的英文开场白"
    _validate_profile_payload(
        {"number": "10086", "task": "t", "scenario": "s", "opening": text}
    )


def test_threshold_is_pinned_from_both_sides():
    """恰好 100 宽度通过、101 拒绝——避免阈值被无意改动。"""
    ok = "a" * MAX_OPENING_WIDTH
    assert display_width(ok) == MAX_OPENING_WIDTH
    _validate_profile_payload(
        {"number": "10086", "task": "t", "scenario": "s", "opening": ok}
    )
    with pytest.raises(ProfileValidationError):
        _validate_profile_payload(
            {"number": "10086", "task": "t", "scenario": "s", "opening": "a" * (MAX_OPENING_WIDTH + 1)}
        )


def test_generated_opening_over_limit_is_discarded_not_truncated():
    """通话中真正被 TTS 念出来的那条路径：超限必须整条丢弃回退模板，绝不硬切半句。"""
    from agentcall.prompt_gen import _normalize_opening

    assert _normalize_opening("a" * (MAX_OPENING_WIDTH + 1)) == ""
    assert _normalize_opening("你" * 51) == "", "51 汉字 = 102 宽度，应丢弃"
    kept = _normalize_opening("你好，我想查一下上个月的话费。")
    assert kept == "你好，我想查一下上个月的话费。", "限内开场白必须原样保留"
    assert _normalize_opening("a" * MAX_OPENING_WIDTH) == "a" * MAX_OPENING_WIDTH
