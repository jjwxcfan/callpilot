"""识别不到运营商时，必须有办法把免费客服号补上。

#72 的验收项之一：「携号转网/识别不到时优雅兜底（用户可手动选运营商或直接填
`CARRIER_HOTLINE`）」。自动识别（`sim_identity`）本身早已实现，缺的是这条兜底。

**为什么它不只是「体验问题」**：`dial_guard` 的误拨保护靠

    拨的号在已知客服号里  且  不等于本卡的 service_number  →  拦

来阻止拨到别家运营商的客服号（跨运营商按普通通话计费，2026-07-13 实测）。
识别不到时 `service_number` 为空，**这道保护整个失效**——不是拦不准，是根本不拦。
"""

from __future__ import annotations

import pytest

from agentcall.dial_guard import check_dial_guard
from agentcall.sim_identity import (
    VALID_SERVICE_NUMBERS,
    SimIdentity,
    identify,
    with_service_number_override,
)


def sim(service_number: str = "", carrier: str = "中国移动") -> SimIdentity:
    return SimIdentity(
        present=True,
        plmn="46007",
        carrier=carrier,
        service_number=service_number,
        registered=True,
        reg_status="已注册",
    )


# ---- 覆盖函数本身 ----


def test_override_replaces_the_detected_number():
    """携号转网：识别结果指向别家时，用户填的要赢。"""
    got = with_service_number_override(sim("10010"), "10086")
    assert got.service_number == "10086"


def test_override_fills_in_when_detection_failed():
    """识别不到（非大陆卡 / 读不到 IMSI / PLMN 不在表里）。"""
    got = with_service_number_override(sim(""), "10086")
    assert got.service_number == "10086"


def test_empty_override_keeps_auto_detection():
    """留空 = 默认行为不变。"""
    assert with_service_number_override(sim("10086"), "").service_number == "10086"
    assert with_service_number_override(sim("10086"), "   ").service_number == "10086"


@pytest.mark.parametrize(
    "bad",
    [
        "abc",
        "100-86",
        "10086 ext",
        "+8610086",
        "１００８６",       # 全角数字：str.isdigit() 对它返回 True，必须另判 isascii
        "13800138000",   # 完整手机号——最危险的误输入
        "12345",         # 数字但不是已知客服号
    ],
)
def test_only_known_hotlines_are_accepted(bad):
    """Codex 评审 P1：只判「是数字」不够，误填会让保护**反向**失效。

    实测：移动卡（识别为 10086）误填 10010 后
        拨 10086（本卡免费）→ 被拦
        拨 10010（对移动收费）→ 放行
    所以取值必须限定在已知免费客服号集合里，顺带挡掉粘完整手机号。
    """
    assert with_service_number_override(sim("10086"), bad).service_number == "10086"


def test_valid_set_is_exactly_the_known_hotlines():
    assert VALID_SERVICE_NUMBERS == {"10086", "10010", "10000", "10099"}


def test_disagreeing_with_a_successful_detection_is_warned(caplog):
    """携号转网时这是对的，填错时这是危险的——两种都必须看得见。"""
    with caplog.at_level("WARNING"):
        with_service_number_override(sim("10086", carrier="中国移动"), "10010")
    assert any("覆盖了自动识别结果" in r.getMessage() for r in caplog.records)


def test_no_warning_when_detection_failed(caplog):
    """识别不到时用覆盖是正常用法，不该刷告警。"""
    with caplog.at_level("WARNING"):
        with_service_number_override(sim(""), "10086")
    assert not [r for r in caplog.records if "覆盖了自动识别结果" in r.getMessage()]


def test_other_identity_fields_are_untouched():
    before = sim("10010")
    after = with_service_number_override(before, "10086")
    assert (after.carrier, after.plmn, after.registered) == (
        before.carrier,
        before.plmn,
        before.registered,
    )


# ---- 真正的收益：误拨保护重新生效 ----


def test_misdial_guard_is_dead_without_a_service_number():
    """前置证明：识别不到时，那道保护确实拦不住。"""
    unknown = SimIdentity(
        present=True, plmn="", carrier="未知", service_number="",
        registered=True, reg_status="已注册",
    )
    assert check_dial_guard(modem_online=True, sim_identity=unknown, number="10010") is None, (
        "本用例是反面基线：service_number 为空时保护本就不拦"
    )


def test_override_restores_the_misdial_guard():
    """核心收益：填了 CARRIER_HOTLINE 之后，拨别家客服号会被拦下。"""
    unknown = SimIdentity(
        present=True, plmn="", carrier="未知", service_number="",
        registered=True, reg_status="已注册",
    )
    fixed = with_service_number_override(unknown, "10086")

    failure = check_dial_guard(modem_online=True, sim_identity=fixed, number="10010")
    assert failure is not None, "拨别家客服号应被拦下"
    assert failure.code == "SERVICE_NUMBER_MISMATCH"


def test_own_hotline_still_dialable_after_override():
    fixed = with_service_number_override(sim(""), "10086")
    assert check_dial_guard(modem_online=True, sim_identity=fixed, number="10086") is None


# ---- 与自动识别的组合 ----


def test_auto_detection_still_works_when_no_override():
    """不填配置时，IMSI 识别链路完全不受影响。"""
    identity = identify("460071234567890", "+CREG: 0,1")
    assert identity.carrier == "中国移动"
    assert identity.service_number == "10086"
    assert with_service_number_override(identity, "").service_number == "10086"


# ---- 接线：确保 modem 真的调了它（Codex 评审 P2）----


def test_modem_refresh_applies_the_override(monkeypatch):
    """光测纯函数不够：把 modem 里那次调用删掉，上面的用例照样全绿。

    这里断言 refresh_sim_identity 之后，缓存里的身份确实带上了覆盖值。
    """
    from agentcall import modem as modem_module

    monkeypatch.setenv("CARRIER_HOTLINE", "10086")

    captured: dict = {}

    class FakeModem:
        _SIM_READ_RETRIES = 1
        _SIM_READ_RETRY_DELAY = 0
        _sim_refresh_generation = 0

        def _send(self, cmd: str) -> str:
            # 未知 PLMN（46099 不在表里）→ 自动识别拿不到客服号
            return "460991234567890" if "CIMI" in cmd else "+CREG: 0,1"

        def _set_sim_identity(self, identity, notify=True):
            captured["identity"] = identity
            self._sim_identity = identity

    fake = FakeModem()
    modem_module.Eg25Modem.refresh_sim_identity(fake, notify=False)  # type: ignore[arg-type]

    identity = captured["identity"]
    assert identity.service_number == "10086", (
        "modem 刷新时没有施加 CARRIER_HOTLINE 覆盖"
    )
