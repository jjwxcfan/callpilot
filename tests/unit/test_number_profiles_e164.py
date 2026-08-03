"""预设号码匹配必须容忍国家码前缀。

回归 #75：`lookup_profile` 做的是裸字符串相等比较，而拨号号码允许前导 `+`。
于是 `+8613800138000` 与 `13800138000` 是两条互不命中的预设。

用户从通讯录 / App 带 `+86` 拨出时，精心写好的预设会**静默失配**并落回动态
生成 —— 表现为「同一个号码有时用预设有时不用」，且没有任何日志说明原因。
预设是 IVR 场景调优的主要杠杆，静默失配会让调优结果不可复现。
"""

from __future__ import annotations

import json

import pytest

from agentcall.number_profiles import (
    canonical_dial_number,
    lookup_profile,
    same_dial_number,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("+8613800138000", "13800138000"),  # 本 issue 的原始形态
        ("13800138000", "+8613800138000"),  # 反向
        ("+86 138 0013 8000", "13800138000"),  # 带分隔符
        ("+86-138-0013-8000", "13800138000"),
        ("13800138000", "13800138000"),  # 两边都是裸号
    ],
)
def test_same_number_matches_across_formats(left, right):
    assert same_dial_number(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Codex P1 反例：第一版「试 1–3 位国家码」会把这两个判成同号
        # （只剥掉一个 "3" 就凑上了），但 +33 是法国。误判比漏判更糟。
        ("+33123456789", "3123456789"),
        ("+441234567890", "1234567890"),  # 英国：同理，非本机国家码不得剥离
        ("+11234567890", "1234567890"),   # NANP：同理
        ("+8613800138000", "+13800138000"),  # 两边都带 + 时不做任何剥离
        ("13800138000", "13800138001"),  # 真的是不同号码
        ("10086", "+8610086"),  # 短号不得靠后缀匹配
        ("10086", "1380010086"),  # 短号是长号的后缀 —— 必须不匹配
        ("+8613800138000", "8613800138000"),  # 没有 + 就不猜国家码
        ("", "13800138000"),
        ("13800138000", ""),
        ("+861380013800012345", "13800138000"),  # 前缀超过 3 位不是国家码
    ],
)
def test_different_numbers_do_not_match(left, right):
    assert not same_dial_number(left, right)


def test_short_codes_are_never_suffix_matched():
    """10086 的预设绝不能命中任意以 10086 结尾的号码 —— 那会误拨错预设。"""
    assert not same_dial_number("10086", "+8613910086")
    assert not same_dial_number("110", "+8613800000110")


def test_canonical_strips_separators_but_keeps_significant_chars():
    assert canonical_dial_number("+86 138-0013 8000") == "+8613800138000"
    assert canonical_dial_number(" 10086 ") == "10086"
    assert canonical_dial_number("*100#") == "*100#"
    assert canonical_dial_number(None) == ""


# ---- 端到端：真的走 lookup_profile ----


def write_profiles(tmp_path, number: str):
    path = tmp_path / "number_profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "number": number,
                        "task": "查询话费",
                        "scenario": "SENTINEL_SCENARIO",
                        "enabled": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_profile_stored_bare_is_found_when_dialing_with_country_code(tmp_path):
    """本 issue 的真实场景：预设写的是裸号，用户从通讯录带 +86 拨出。"""
    path = write_profiles(tmp_path, "13800138000")
    profile = lookup_profile("+8613800138000", "查询话费", path=path)
    assert profile is not None, "带 +86 拨出时预设静默失配了"
    assert profile["scenario"] == "SENTINEL_SCENARIO"


def test_profile_stored_with_country_code_is_found_when_dialing_bare(tmp_path):
    path = write_profiles(tmp_path, "+8613800138000")
    profile = lookup_profile("13800138000", "查询话费", path=path)
    assert profile is not None
    assert profile["scenario"] == "SENTINEL_SCENARIO"


def test_a_genuinely_different_number_still_misses(tmp_path):
    """容忍格式不等于放宽匹配：别的号码仍然不能命中。"""
    path = write_profiles(tmp_path, "13800138000")
    assert lookup_profile("13800138001", "查询话费", path=path) is None


def test_only_the_local_country_code_is_ever_stripped():
    """Codex P1 的正解：不是「试 1–3 位」，而是只剥本机那一个国家码。"""
    from agentcall.number_profiles import DIAL_COUNTRY_CODE

    assert same_dial_number(f"+{DIAL_COUNTRY_CODE}13800138000", "13800138000")
    # 换成任何别的国家码都不该匹配。
    for other in ("1", "33", "44", "81", "999"):
        if other == DIAL_COUNTRY_CODE:
            continue
        assert not same_dial_number(f"+{other}13800138000", "13800138000"), (
            f"+{other} 不是本机国家码，不得被剥离"
        )


def test_conflict_detection_uses_the_same_equivalence(tmp_path):
    """Codex P1：冲突检测若还用裸相等，两种写法可并存，命中哪条取决于文件顺序。"""
    from agentcall.number_profiles import ProfileConflictError, _check_conflicts

    existing = [{"number": "13800138000", "task": "查询话费", "enabled": True}]
    candidate = {"number": "+8613800138000", "task": "查询话费", "enabled": True}
    with pytest.raises(ProfileConflictError):
        _check_conflicts(existing, candidate, exclude_id=None)


def test_lookup_by_id_also_tolerates_the_country_code(tmp_path):
    """Codex P2：显式选中的预设不该因为号码写法不同而被拒。"""
    from agentcall.number_profiles import lookup_profile_by_id

    path = write_profiles(tmp_path, "13800138000")
    data = json.loads(path.read_text(encoding="utf-8"))
    profile_id = None
    from agentcall.number_profiles import _profiles_with_ids

    for _item, item_id in _profiles_with_ids(data["profiles"]):
        profile_id = item_id
    assert profile_id
    got = lookup_profile_by_id(profile_id, "+8613800138000", "查询话费", path=path)
    assert got is not None, "带国家码时按 ID 命中的预设被拒了"
