"""PWA 的两个生产缺陷（#98.3 / #98.4）。

**先核实再动手**：原 issue 记了 4 项，其中前两项早已修复——
- #98.1 inbound 悬挂会话 → commit `1ff5e55`「inbound 会话级硬时限兜底」
- #98.2 云心跳 lineBusy → commit `334274d`「lineBusy 聚合全部线路占用者」

本文件只覆盖仍然存在的后两项，都在 PWA 侧。

#98.3 旧 cookie 吞掉新配对码：浏览器留着上一台 Edge 的
`__Host-callpilot-device` 时，`initialize()` 里 `if (await refreshDevice()) return;`
直接短路，新配对码永远消费不掉，页面还一直显示 Edge 不可用——用户明明刚扫了
新码，却完全没有出路。

#98.4 本机媒体错误被显示成「Edge is unavailable」：`getUserMedia()` 排在
`requestInvite()` 之前，通用 catch 把除权限拒绝外的所有本机错误（没有麦克风、
设备被占用…）统一显示为 connection_failed，把排障方向整个带偏——现场就是照着
这条去查 Edge 日志，而 Edge 侧连 session.start 都没有。
"""

from __future__ import annotations

from pathlib import Path

import pytest

DIALER_JS = Path("src/agentcall/web/static/remote_dialer.js")


@pytest.fixture(scope="module")
def source() -> str:
    return DIALER_JS.read_text(encoding="utf-8")


def initialize_body(source: str) -> str:
    return source.split("async function initialize()", 1)[1].split("\n  }", 1)[0]


def function_body(source: str, header: str) -> str:
    """按花括号配平取出整个函数体。

    Codex 评审 P2：原来用 `split("\n  }")` 只取到第一个内层 `}`，
    localMediaErrorKey 里后面的分支根本没被断言到。
    """
    start = source.index(header)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"未闭合的函数体: {header}")


# ---- #98.3 ----


def test_a_fresh_pairing_code_is_not_swallowed_by_an_old_cookie(source):
    body = initialize_body(source)
    assert "showPairingReplacePrompt" in body, (
        "带新配对码时必须给出替换入口，而不是被旧绑定短路"
    )


def test_the_pairing_code_branch_comes_before_the_plain_refresh_shortcut(source):
    """顺序是关键：新码分支必须先于那句无条件的 `if (await refreshDevice()) return;`。"""
    body = initialize_body(source)
    guarded = body.index("if (pairCode) {")
    shortcut = body.index("if (await refreshDevice()) return;")
    assert guarded < shortcut, "新配对码分支被无条件短路挡在了后面"


def test_replace_prompt_is_not_overwritten_by_the_ready_message(source):
    """提示语自己会设状态；若随后再设 ready，用户根本看不到提示。"""
    body = initialize_body(source)
    # 只看 `if (bound) { ... }` 这一支：else 支里的 ready 是另一条路径。
    branch = body.split("if (bound) {", 1)[1].split("} else {", 1)[0]
    assert "showPairingReplacePrompt()" in branch
    assert 'setStatus("idle", t("ready"))' not in branch, (
        "替换提示会被 ready 覆盖掉"
    )


def test_replace_prompt_has_copy_in_both_languages(source):
    assert source.count("pairing_replace:") == 2, "中英文案都要有"


# ---- #98.4 ----


def test_local_media_errors_map_to_their_own_codes(source):
    """本机媒体故障必须与 Edge 可达性分开呈现。"""
    assert "function localMediaErrorKey" in source
    for name in (
        "NotAllowedError",
        "NotFoundError",
        "NotReadableError",
        "OverconstrainedError",
    ):
        assert name in source, f"未覆盖 getUserMedia 的 {name}"


def test_missing_microphone_is_not_reported_as_edge_unavailable(source):
    """核心：没有麦克风时不得显示「Edge is unavailable」。"""
    assert "microphone_missing" in source
    body = function_body(source, "async function connectAndDial")
    media_catch = body.split("} catch (error) {", 1)[1].split("return;", 1)[0]
    assert "localMediaErrorKey" in media_catch


def test_local_media_has_its_own_try_block(source):
    """Codex P1：localMediaErrorKey 不能覆盖 room.connect 那一段。

    WebSocket 构造函数在被拦截 / 不安全端口时也抛 SecurityError，与
    getUserMedia 的 SecurityError 撞名——共用一个 catch 会把真正的 LiveKit
    连接失败显示成「麦克风不可用」，排障方向再次被带偏。
    """
    body = function_body(source, "async function connectAndDial")
    media_catch_at = body.index("} catch (error) {")
    connect_at = body.index("room.connect(")
    assert media_catch_at < connect_at, (
        "本机媒体的 catch 必须在 room.connect 之前闭合"
    )


def test_edge_unavailable_is_still_the_fallback(source):
    """分类不等于抛弃兜底：真的连不上 Edge 时仍要显示原文案。"""
    body = function_body(source, "async function connectAndDial")
    edge_catch = body.rsplit("} catch (", 1)[1]
    assert "connection_failed" in edge_catch
    assert "localMediaErrorKey" not in edge_catch, (
        "Edge 侧的 catch 不该再走本机媒体的错误映射"
    )


def test_copy_does_not_promise_a_replacement_the_server_never_does(source):
    """Codex P2：/api/pair 是新增设备+换 cookie，不吊销旧绑定，设备数满还会被拒。

    文案说「替换」就是承诺了做不到的事。
    """
    assert "replace the current device" not in source
    assert "替换当前配对设备" not in source


def test_error_mapping_uses_stable_names_not_message_text(source):
    """错误文案会随浏览器语言变，只能认标准 error.name。"""
    mapper = function_body(source, "function localMediaErrorKey")
    assert "error.name" in mapper
    # 唯一一处看 message 的是我们自己抛的哨兵，不是浏览器文案。
    assert mapper.count("error.message") <= 1
    # 花括号配平确保整段都被检查到，而不是只到第一个内层 }。
    for name in ("NotFoundError", "NotReadableError", "OverconstrainedError"):
        assert name in mapper, f"{name} 不在 localMediaErrorKey 函数体内"


def test_all_new_copy_exists_in_both_languages(source):
    for key in ("microphone_missing", "microphone_busy", "microphone_unavailable"):
        assert source.count(f"{key}:") == 2, f"{key} 缺中文或英文文案"
