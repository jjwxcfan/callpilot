"""Offline Edge wiring for the #95 offer request and claimed media injection."""

from __future__ import annotations

import re
import threading
import time

from fakes import FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.remote_dialer import (
    IssuedLiveKitSession,
    RemoteDialerInvite,
)
from agentcall.takeover_coordinator import (
    ClaimFence,
    InboundTakeoverSession,
    TakeoverOffer,
    TakeoverRejection,
    TakeoverState,
)


def _service() -> CallAgentService:
    return CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )


def _prepare_inbound(service: CallAgentService, generation: int = 7) -> None:
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = generation
    session._initialize_takeover_context("inbound")


def _issued(expires_at: float) -> IssuedLiveKitSession:
    return IssuedLiveKitSession(
        invite=RemoteDialerInvite(
            session_id="session_takeover_1234",
            url="",
            expires_at=expires_at,
        ),
        room_name="callpilot-takeover-room",
        browser_identity="web-device-primary",
        edge_identity="edgepart-takeover",
        browser_token="",
        edge_token="edge-token",
        livekit_url="wss://livekit.example",
    )


def _claim_for_request(request) -> InboundTakeoverSession:
    device_id = "web-device-primary"
    offer = TakeoverOffer(
        offer_id=request.offer_id,
        nonce=request.nonce,
        call_id=request.call_id,
        generation=request.generation,
        target_device_id=device_id,
        expires_at=request.expires_at,
    )
    fence = ClaimFence(
        call_id=request.call_id,
        generation=request.generation,
        claim_id="claim_primary_1234",
        device_id=device_id,
    )
    return InboundTakeoverSession(
        offer=offer,
        fence=fence,
        issued=_issued(request.expires_at),
    )


def _tool_names(service: CallAgentService, direction: str) -> set[str]:
    registry = service.session._build_tools(direction)
    return {spec["function"]["name"] for spec in registry.specs()}


def test_takeover_tool_is_registered_only_for_enabled_inbound(monkeypatch) -> None:
    service = _service()
    _prepare_inbound(service)

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    assert "request_owner_takeover" not in _tool_names(service, "inbound")

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    assert "request_owner_takeover" in _tool_names(service, "inbound")
    assert "request_owner_takeover" not in _tool_names(service, "outbound")

    service.session._triage_mode = "enforce"
    assert "request_owner_takeover" not in _tool_names(service, "inbound")


def test_tool_request_is_opaque_bounded_and_double_gated(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv(
        "INBOUND_TAKEOVER_PREFERENCE",
        "快递也转给我，不要把这段偏好发到云端。",
    )
    service = _service()
    _prepare_inbound(service)
    registry = service.session._build_tools("inbound")

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    disabled = registry.dispatch("request_owner_takeover", {})
    assert disabled["success"] is False
    assert disabled["code"] == "TAKEOVER_DISABLED"
    assert service.next_inbound_takeover_offer() is None

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    accepted = registry.dispatch("request_owner_takeover", {})
    request = service.next_inbound_takeover_offer()

    assert accepted["success"] is True
    assert request is not None
    assert request.offer_id.startswith("offer_")
    assert request.call_id.startswith("call_")
    assert request.generation == 7
    assert request.expires_at > request.created_at
    serialized = repr(request)
    # 裸 "138" 子串会撞时间戳浮点(如 …384.391…),CI flaky 实锤;
    # 改为带数字边界的完整手机号模式:任何独立 11 位号都不得进 repr。
    assert re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", serialized) is None
    assert "快递" not in serialized
    # 这里没有来电号码（未设 current_caller），故上下文为空。号码在已知时
    # 也只留在 Edge、由设备按需拉取（ADR-003：不上 Cloud），见
    # test_takeover_context_snapshot_carries_caller_number；机主偏好永不出会话。
    assert service.session.takeover_state is TakeoverState.TAKEOVER_PREPARING

    repeated = registry.dispatch("request_owner_takeover", {})
    assert repeated["success"] is False
    assert repeated["code"] == "TAKEOVER_NOT_AI_ACTIVE"
    assert service.next_inbound_takeover_offer() is None


def test_claimed_session_injection_validates_fence_and_queues_media(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    registry = service.session._build_tools("inbound")
    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    request = service.next_inbound_takeover_offer()
    assert request is not None

    stale_offer = TakeoverOffer(
        offer_id=request.offer_id,
        nonce=request.nonce,
        call_id=request.call_id,
        generation=request.generation + 1,
        target_device_id="web-device-primary",
        expires_at=request.expires_at,
    )
    stale_session = InboundTakeoverSession(
        offer=stale_offer,
        fence=ClaimFence(
            request.call_id,
            request.generation + 1,
            "claim_stale_1234",
            "web-device-primary",
        ),
        issued=_issued(request.expires_at),
    )

    stale = service.provide_inbound_takeover_session(stale_session)
    assert not stale.accepted
    assert stale.code is TakeoverRejection.STALE_GENERATION
    assert service.session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert service.take_inbound_takeover_session() is None

    claimed_session = _claim_for_request(request)
    accepted = service.provide_inbound_takeover_session(claimed_session)

    assert accepted.accepted
    assert service.session.takeover_state is TakeoverState.WAITING_OWNER
    assert service.session.takeover_fence == claimed_session.fence
    assert service.take_inbound_takeover_session() is claimed_session


def test_cloud_claim_adapter_rebuilds_offer_from_edge_local_expiry(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    registry = service.session._build_tools("inbound")
    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    request = service.next_inbound_takeover_offer()
    assert request is not None

    result = service.accept_inbound_takeover_claim(
        offer_id=request.offer_id,
        call_id=request.call_id,
        claim_id="claim_cloud_1234",
        generation=request.generation,
        nonce=request.nonce,
        issued=_issued(request.expires_at + 300),
    )

    assert result.accepted
    claimed = service.take_inbound_takeover_session()
    assert claimed is not None
    assert claimed.offer.expires_at == request.expires_at
    assert claimed.offer.target_device_id == claimed.issued.browser_identity
    assert claimed.fence.device_id == claimed.issued.browser_identity


def test_offer_request_does_not_publish_nonce_or_preference(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_PREFERENCE", "快递也转接")
    service = _service()
    _prepare_inbound(service)

    assert service.session._build_tools("inbound").dispatch(
        "request_owner_takeover", {}
    )["success"] is True
    request = service.next_inbound_takeover_offer()
    assert request is not None

    history = service.hub.history() if service.hub is not None else []
    assert request.nonce not in repr(history)
    assert "快递也转接" not in repr(history)


def test_service_force_takeover_hook_supports_active_outbound_smoke() -> None:
    service = _service()
    session = service.session
    session._active = True
    session._outbound_number = "10086"
    session._session_generation = 11
    session._initialize_takeover_context("outbound")

    not_connected = service.force_takeover_request()
    assert not_connected["code"] == "CALL_NOT_CONNECTED"
    service.modem.trigger_call_connected("10086")

    result = service.force_takeover_request()
    request = service.next_inbound_takeover_offer()

    assert result["success"] is True
    assert request is not None
    assert request.generation == 11
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING


def test_takeover_context_snapshot_carries_caller_number(monkeypatch) -> None:
    """接管上下文按 offerId 现答现回（WIL-137）：号码在请求发出时就有，
    先落地成快照，机主设备按需拉取。

    ADR-003：完整号码不上 Cloud——所以上下文留在 Edge，不塞进 offer 消息。
    """
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_PREFERENCE", "快递也转给我。")
    service = _service()
    _prepare_inbound(service)
    service.session.current_caller = "+15105550123"
    registry = service.session._build_tools("inbound")

    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    request = service.next_inbound_takeover_offer()
    assert request is not None
    # offer 消息本身不带上下文（上云路径）
    assert not hasattr(request, "context")
    assert "快递" not in repr(request)

    snapshot = service.takeover_context_snapshot(request.offer_id)
    assert snapshot is not None
    assert snapshot["peerNumber"] == "+15105550123"
    assert snapshot["claimedName"] is None  # 还没问出来，可空
    assert snapshot["purpose"] is None
    assert snapshot["updatedAtUnixMs"] > 0
    # 别的 offerId 读不到——防止设备拿任意 id 钓上下文
    assert service.takeover_context_snapshot("offer_someone_elses_id") is None
    assert service.takeover_context_snapshot("") is None


def test_context_summary_backfills_name_and_purpose(monkeypatch) -> None:
    """身份/来意往往在 offer 发出后才问清（WIL-137「可后补」）：后台摘要产出后
    更新快照，号码保持不变、时间戳前进。"""
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    session = service.session
    session.current_caller = "+15105550123"
    session._live_transcripts = [("user", "我是 Kevin，想约机主周六吃饭")]
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call_context",
        lambda turns, **kwargs: ("Kevin", "约机主周六吃饭"),
    )
    registry = session._build_tools("inbound")

    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    offer = service.next_inbound_takeover_offer()
    assert offer is not None
    initial = service.takeover_context_snapshot(offer.offer_id)
    assert initial is not None

    deadline = time.monotonic() + 3.0
    filled = None
    while time.monotonic() < deadline:
        snapshot = service.takeover_context_snapshot(offer.offer_id)
        if snapshot and snapshot["claimedName"]:
            filled = snapshot
            break
        time.sleep(0.02)

    assert filled is not None
    assert filled["claimedName"] == "Kevin"
    assert filled["purpose"] == "约机主周六吃饭"
    assert filled["peerNumber"] == "+15105550123"
    assert filled["updatedAtUnixMs"] >= initial["updatedAtUnixMs"]


def test_context_summary_never_leaks_across_calls(monkeypatch) -> None:
    """摘要在飞时换了一通电话：迟到的结果必须丢弃，绝不能覆盖到**新通话**上
    （上一通的来电者姓名串进这一通是隐私事故）。

    断言落在「新 offer 的快照里没有上一通的姓名」——只断言旧 offerId 读不到
    是不够的：跨通重置本身就会让那个断言成立，与 worker 里的 offer_id 守卫
    无关（独立评审用变异实验证明过那种写法是永真的）。
    """
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    session = service.session
    session.current_caller = "+15105550123"
    session._live_transcripts = [("user", "我是 Kevin")]
    released = threading.Event()

    def slow_summary(turns, **kwargs):
        released.wait(3.0)
        return "Kevin", "约周六吃饭"

    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call_context", slow_summary
    )
    registry = session._build_tools("inbound")
    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    old_offer = service.next_inbound_takeover_offer()
    assert old_offer is not None

    # 上一通的摘要还堵在模型调用里，新一通电话已经开始并自己发了 offer
    session._initialize_takeover_context("inbound")
    session.current_caller = "+16505550777"
    session._live_transcripts = []
    new_registry = session._build_tools("inbound")
    assert new_registry.dispatch("request_owner_takeover", {})["success"] is True
    new_offer = service.next_inbound_takeover_offer()
    assert new_offer is not None and new_offer.offer_id != old_offer.offer_id

    released.set()  # 上一通的摘要现在返回
    time.sleep(0.4)

    assert service.takeover_context_snapshot(old_offer.offer_id) is None
    fresh = service.takeover_context_snapshot(new_offer.offer_id)
    assert fresh is not None
    assert fresh["peerNumber"] == "+16505550777"
    assert fresh["claimedName"] is None, "上一通的姓名不得串进新通话"
    assert fresh["purpose"] is None


def test_context_is_unreadable_after_call_ends(monkeypatch) -> None:
    """通话结束即作废：offer 已 revoke，持有旧 offerId 的设备不该还能读到
    号码与来意（此前只在下一通开始时才清，中间可能隔几小时）。"""
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    session = service.session
    session.current_caller = "+15105550123"
    registry = session._build_tools("inbound")

    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    offer = service.next_inbound_takeover_offer()
    assert offer is not None
    assert service.takeover_context_snapshot(offer.offer_id) is not None

    session._end_takeover_context("CALL_ENDED")

    assert service.takeover_context_snapshot(offer.offer_id) is None


def test_context_summary_silent_when_model_finds_nothing(monkeypatch) -> None:
    """摘要没提取到任何东西时快照保持原样（只有号码），不写空值。"""
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    session = service.session
    session.current_caller = "+15105550123"
    session._live_transcripts = [("user", "喂")]
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call_context",
        lambda turns, **kwargs: (None, None),
    )
    registry = session._build_tools("inbound")

    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    offer = service.next_inbound_takeover_offer()
    assert offer is not None
    time.sleep(0.3)

    snapshot = service.takeover_context_snapshot(offer.offer_id)
    assert snapshot is not None
    assert snapshot["claimedName"] is None and snapshot["purpose"] is None
