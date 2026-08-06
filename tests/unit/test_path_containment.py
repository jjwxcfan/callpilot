"""通话产物路径必须落在 base_dir 之内（WIL-87）。

调用方已经用 `_CALL_ID_RE`（字符集不含 `/` 与 `.`）过了一遍，穿越本来就构造不
出来。这一层是第二道，防的是「正则没错、拼接写错」这类未来回归——例如把
base_dir 换成可配置项、或多拼了一段用户输入。

顺带解决 CodeQL 长期误报：它不把 `re.fullmatch` 或集合成员判断建模成路径消毒器，
于是这几处一直报 py/path-injection，把真告警淹在噪音里，整个安全检查长年是红的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcall.web.server import _call_artifact_path


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    (tmp_path / "20260806-000000-inbound-123").mkdir()
    return tmp_path


# ---- 正常路径 ----


def test_normal_call_id_resolves(base: Path):
    got = _call_artifact_path(base, "20260806-000000-inbound-123", "events.jsonl")
    assert got is not None
    assert got == (base / "20260806-000000-inbound-123" / "events.jsonl").resolve()


def test_accepts_str_base_dir(base: Path):
    """base_dir 在调用方是 str，不是 Path。"""
    assert _call_artifact_path(str(base), "20260806-000000-inbound-123", "a.wav") is not None


def test_multiple_parts(base: Path):
    got = _call_artifact_path(base, "20260806-000000-inbound-123", "mixed.wav")
    assert got is not None and got.name == "mixed.wav"


# ---- 越界必须返回 None ----


@pytest.mark.parametrize(
    "call_id",
    [
        "..",
        "../..",
        "../../../etc",
        "a/../../..",
    ],
)
def test_traversal_is_rejected(base: Path, call_id: str):
    assert _call_artifact_path(base, call_id, "events.jsonl") is None


def test_absolute_part_is_rejected(base: Path):
    """joinpath 遇到绝对路径会直接跳到该路径——必须被归属检查拦下。"""
    assert _call_artifact_path(base, "/etc", "passwd") is None


def test_traversal_in_trailing_part_is_rejected(base: Path):
    assert _call_artifact_path(base, "20260806-000000-inbound-123", "../../x") is None


def test_symlink_escape_is_rejected(base: Path, tmp_path: Path):
    """符号链接指向 base 之外——resolve() 之后才看得出来。

    这正是「只做字符串检查」挡不住的一类：call_id 字符集完全合法。
    """
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    link = base / "escapelink"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本平台不支持创建符号链接")
    assert _call_artifact_path(base, "escapelink", "events.jsonl") is None


# ---- 网关静态资源：真跑，不看源码字符串 ----
#
# 原本这两条用 inspect.getsource 找关键字，注释里写着同样的词也能过
# （2026-08-07 Codex 评审 P2）。改成真调 _asset。


def _asset_request(path: str):
    """造一个只带 path 的最小 request 替身：_asset 只用 request.path。"""

    class _Req:
        def __init__(self, p: str) -> None:
            self.path = p

    return _Req(path)


def test_gateway_serves_allowed_asset():
    import asyncio

    from agentcall.web.remote_gateway import _asset

    resp = asyncio.run(_asset(_asset_request("/remote_dialer.js")))
    assert resp.status == 200


@pytest.mark.parametrize(
    "path",
    [
        "/../../../etc/passwd",
        "/secrets.env",
        "/remote_dialer.js.map",
        "/",
    ],
)
def test_gateway_rejects_everything_else(path: str):
    import asyncio

    from aiohttp import web

    from agentcall.web.remote_gateway import _asset

    with pytest.raises(web.HTTPNotFound):
        asyncio.run(_asset(_asset_request(path)))


def test_qwen_prewarm_pins_tls_minimum(monkeypatch):
    """真跑预热，断言它实际用的 context 钉了 TLS 下限。"""
    import socket
    import ssl

    from agentcall.agents import qwen_agent

    seen = {}
    real_ctx = ssl.create_default_context

    def spy_context(*a, **kw):
        ctx = real_ctx(*a, **kw)
        seen["ctx"] = ctx
        return ctx

    monkeypatch.setattr(ssl, "create_default_context", spy_context)

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def wrap_socket(self, *a, **kw):  # pragma: no cover - 由 ctx 提供
            return self

    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _Sock())
    monkeypatch.setattr(
        ssl.SSLContext, "wrap_socket", lambda self, sock, **kw: _Sock()
    )

    fn = getattr(qwen_agent, "prewarm_connection", None)
    if fn is None:
        pytest.skip("预热函数不存在")
    fn()
    assert seen.get("ctx") is not None, "预热没有创建 SSL context"
    assert seen["ctx"].minimum_version == ssl.TLSVersion.TLSv1_2


# ---- 真实 call_id 生成格式必须能通过校验（Codex 评审：这条缺口没人测）----


def test_generated_call_id_passes_validation_and_containment(tmp_path: Path):
    """用 call_log 真正生成的目录名跑一遍，别只用手写的假 ID。

    手写 ID 恰好合法不代表生成器产出的也合法——两者一旦漂移，历史接口会
    对所有真实通话返回 400，而单测全绿。
    """
    from agentcall.call_log import CallLogger
    from agentcall.web.server import _CALL_ID_RE

    logger_ = CallLogger(base_dir=tmp_path)
    record = logger_.begin_call("inbound", "13800138000")
    call_id = record.id
    assert _CALL_ID_RE.fullmatch(call_id), f"生成的 call_id 过不了校验: {call_id}"
    assert _call_artifact_path(tmp_path, call_id, "events.jsonl") is not None
