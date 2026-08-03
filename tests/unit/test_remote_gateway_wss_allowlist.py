"""本机网关不得放行任意 WSS 媒体端点。

回归 #41 / G-04（高危）：本机网关的 CSP 写的是 `connect-src 'self' wss:` —— 放行
**全部** wss。配合 PWA 的 legacy fragment 直连入口（URL fragment 是攻击者可控的），
在真域名上一条恶意链接就能把一台已配对手机的媒体连到攻击者的 WSS 服务器。

云侧 `cloud/src/csp.ts` 早已把 host 钉死，本机网关这条一直没跟上。

#41 给了两条修法：移除 fragment 入口，**或** exact-host 白名单。临时链接
（fragment 邀请）至今仍是在售功能——面板有「临时链接」按钮、remote_dialer.py
仍在签发 `URL#<payload>`——所以只能走白名单那条（Codex 评审 P1 指出的）。

两道防线，都要有：
1. PWA 侧 exact-host 白名单：端点必须等于本机配置的那一个 LiveKit host，
   白名单没取到就一律不接受 fragment（fail-closed）；
2. CSP connect-src 钉死同一个 host（浏览器强制，JS 被绕过也拦得住）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentcall.web.remote_gateway import (
    livekit_connect_sources,
    livekit_media_host,
    security_headers,
)

DIALER_JS = Path("src/agentcall/web/static/remote_dialer.js")


def connect_src(headers: dict[str, str]) -> str:
    csp = headers["Content-Security-Policy"]
    match = re.search(r"connect-src ([^;]*)", csp)
    assert match, f"CSP 里必须有 connect-src: {csp}"
    return match.group(1).strip()


def test_wildcard_wss_is_gone(monkeypatch):
    """核心断言：任何配置下都不得出现裸 `wss:` 通配。"""
    monkeypatch.setenv("LIVEKIT_URL", "wss://media.example.com")
    sources = connect_src(security_headers())
    assert "wss:" not in sources.replace("wss://", ""), (
        f"connect-src 仍放行任意 wss: {sources}"
    )


def test_connect_src_pins_the_configured_host(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://media.example.com")
    sources = connect_src(security_headers())
    assert "wss://media.example.com" in sources
    assert "https://media.example.com" in sources


def test_a_different_host_is_not_allowed(monkeypatch):
    """攻击者的端点必须不在放行列表里 —— 验收标准「恶意 fragment 无法改端点」。"""
    monkeypatch.setenv("LIVEKIT_URL", "wss://media.example.com")
    sources = connect_src(security_headers())
    assert "evil.example.net" not in sources


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "https://media.example.com",  # 协议不对
        "wss://user:pass@media.example.com",  # 内嵌凭证
        "wss://",  # 无 host
        "not a url",
    ],
)
def test_invalid_config_fails_closed_not_open(bad):
    """无效配置必须收敛到空串，绝不能回落成 `wss:` 通配。

    fail-closed 的代价是远程拨号连不上（可见故障）；fail-open 的代价是
    静默放行任意媒体端点。这里选前者。
    """
    assert livekit_connect_sources(bad) == ""


def test_invalid_config_leaves_only_self(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "")
    sources = connect_src(security_headers())
    assert sources == "'self'", f"无效配置下 connect-src 应只剩 'self'，实际: {sources}"


def test_port_is_part_of_the_pinned_host(monkeypatch):
    """host:port 必须整体钉死，只钉 hostname 会放行同主机的其它端口。"""
    monkeypatch.setenv("LIVEKIT_URL", "wss://media.example.com:7880")
    sources = connect_src(security_headers())
    assert "wss://media.example.com:7880" in sources


# ---- PWA 侧：exact-host 白名单 ----


def test_temporary_link_flow_is_preserved():
    """Codex P1：临时链接（fragment 邀请）仍是在售功能，不能连入口一起删。

    面板有「临时链接」按钮，remote_dialer.py 仍在签发 URL#<payload>。
    #41 给了两条路（移除入口 / exact-host 白名单），既然功能还在，
    只能走白名单那条。
    """
    source = DIALER_JS.read_text(encoding="utf-8")
    body = source.split("async function initialize()", 1)[1].split("\n  }", 1)[0]
    assert "parseInviteFragment" in body, "临时链接入口被误删，产品功能会坏"


def test_pairing_code_fragment_still_works():
    """pair= 前缀是配对码不是端点，属于正常功能。"""
    source = DIALER_JS.read_text(encoding="utf-8")
    assert 'fragment.startsWith("pair=")' in source


def test_invite_parser_enforces_the_exact_host_allowlist():
    """核心：端点 host 必须等于本机配置的那一个，且白名单缺失时 fail-closed。"""
    source = DIALER_JS.read_text(encoding="utf-8")
    parser = source.split("function parseInviteFragment", 1)[1].split("\n  }", 1)[0]
    for guard in (
        "url.username",
        "url.password",
        "!url.hostname",
        "!allowedMediaHost",
        "url.host !== allowedMediaHost",
    ):
        assert guard in parser, f"parseInviteFragment 缺少 {guard} 校验"


def test_allowlist_is_fetched_before_the_fragment_is_trusted():
    """白名单必须先取回来再解析 fragment，否则第一次加载必然 fail-closed 到不可用。"""
    source = DIALER_JS.read_text(encoding="utf-8")
    body = source.split("async function initialize()", 1)[1].split("\n  }", 1)[0]
    assert body.index("loadAllowedMediaHost") < body.index("parseInviteFragment")


def test_media_host_is_served_to_the_pwa():
    """白名单要有来源：/api/device 的 edge 负载里必须带 media_host。"""
    source = Path("src/agentcall/call_agent.py").read_text(encoding="utf-8")
    assert '"media_host"' in source


@pytest.mark.parametrize(
    "malformed",
    [
        "wss://@media.example.com",   # 空 userinfo：username == "" 是 falsey
        "wss://media.example.com:bad",  # 非法端口
        "wss://[::1",                   # 畸形 IPv6，urlsplit 会抛
    ],
)
def test_malformed_urls_fail_closed_without_raising(malformed):
    """Codex P2：这些既不能漏进 CSP，也不能抛异常把页面变成 500。"""
    assert livekit_media_host(malformed) == ""
    assert livekit_connect_sources(malformed) == ""


def test_ipv6_literal_is_pinned_intact():
    assert livekit_media_host("wss://[::1]:7880") == "[::1]:7880"


def test_unpaired_client_still_receives_the_allowlist():
    """E2E 抓到的真 bug：未配对时 /api/device 走的是精简负载。

    临时链接（fragment 邀请）的使用者按定义就是**未配对**的。如果精简负载
    里没有 media_host，白名单永远取不到 → 一律 fail-closed → 这个在售功能
    被整个堵死。单测层面钉住两个分支都带 media_host。
    """
    source = Path("src/agentcall/web/remote_gateway.py").read_text(encoding="utf-8")
    unpaired = source.split('"paired": False', 1)[1].split("    return _json", 1)[0]
    assert "media_host" in unpaired, "未配对分支必须也下发 media_host"
