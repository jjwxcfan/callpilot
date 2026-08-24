"""pytest 共享配置：让测试能导入 tests/fakes，并隔离会写盘/联网的默认配置。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _isolate_side_effects(tmp_path, monkeypatch):
    """全局隔离：通话记录写进临时目录；摘要默认关避免单测触网。

    需要覆盖的测试可自行 monkeypatch.setenv/delenv（测试级优先于本 fixture）。
    """
    monkeypatch.setenv("CALL_LOG_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(tmp_path / "number_profiles.json"))
    # 呼叫情报库（WIL-129）：路径隔离 + 默认关闭，防止单测误触 learner 的真实模型调用
    monkeypatch.setenv("CALL_PLAYBOOKS_FILE", str(tmp_path / "call_playbooks.json"))
    monkeypatch.setenv("CALL_PLAYBOOKS_ENABLED", "false")
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    # 通话语言钉住 zh：大量既有用例断言中文文案/中文链路行为，测的是「zh 模式
    # 正确」而非「默认值是什么」。默认值本身（en，WIL-148）由
    # test_agent_language_defaults_to_english 用 delenv 单独锁定。
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")
    from agentcall.rate_limit import reset_sms_rate_limit_state

    reset_sms_rate_limit_state()
    yield
    reset_sms_rate_limit_state()
