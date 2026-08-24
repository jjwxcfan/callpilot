"""安卓手机蓝牙话机后端（WIL-147）：SIM 在手机里，PC 经 WinRT PhoneLine 控制通话。

手机零改动、不装任何 app：Windows 自带蓝牙栈以 HFP 免提角色连接手机，
通话音频经系统的 Hands-Free 音频端点进出（见 ``audio_bridge.HfpAudioBridge``），
通话控制走 ``Windows.ApplicationModel.Calls``（WinRT）。Phase 0 真机实证
（``docs/fixtures/hfp_spike/RESULTS.md``）：接听/挂断/来电号码/CLCC 等价
轮询全链路可用；裸 RFCOMM 被 Windows 独占（10048），故控制面只此一条路。

本类照 ``AndroidSmsGatewayModem`` 的先例 duck-type ``Eg25Modem`` 的接口子集
（即 ``CallAgentService``/``CallSession`` 实际调用的那 14 个方法 + 3 个
getattr 探测的可选项），经 ``CallAgentService(modem=...)`` 注入，不改
``call_agent.py``。``connect``/``start_listener`` 幂等——supervisor 与连接
看门狗都会重复调用。

已知限制（按批次计划）：
- 短信不支持：``send_sms`` 恒 False（HFP/WinRT 均无短信面，见 WIL-96）。
- SIM 身份拿不到运营商：蓝牙线路的 ``cellular_details`` 真机抛 Not
  implemented、``network_name`` 为空——fail-closed 为「未知运营商」，
  免费客服号必须经 ``CARRIER_HOTLINE`` 人工指定（误拨保护因此仍然生效）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from . import config
from .sim_identity import (
    UNKNOWN_SIM,
    SimIdentity,
    with_service_number_override,
)

logger = logging.getLogger(__name__)

# PhoneCallStatus 枚举原始值（2026-08-24 真机 dump，见 hfp_spike/probe_winrt.py）。
# 用 int 常量而非引用 winsdk 枚举：轮询逻辑因此可在无 winsdk 的平台单测。
CALL_LOST = 0
CALL_INCOMING = 1
CALL_DIALING = 2
CALL_TALKING = 3
CALL_HELD = 4
CALL_ENDED = 5

_ACTIVE_STATUSES = frozenset({CALL_INCOMING, CALL_DIALING, CALL_TALKING, CALL_HELD})
_CONNECTED_STATUSES = frozenset({CALL_TALKING, CALL_HELD})

# DTMF 字符 → DtmfKey 枚举原始值（真机 dump：D0..D9=0..9, STAR=10, POUND=11）。
_DTMF_KEY_VALUES = {**{str(d): d for d in range(10)}, "*": 10, "#": 11}


def _import_calls() -> Any:
    """延迟导入 winsdk（仅 Windows 有 wheel；其他平台/CI 不许 import 失败）。"""
    from winsdk.windows.applicationmodel import calls  # type: ignore

    return calls


class WinRtPhoneModem:
    """经 WinRT PhoneLine 把蓝牙配对的安卓手机当模组用（duck-type 契约）。"""

    POLL_INTERVAL_SECONDS = 0.5
    # 通话接通后的轮询间隔（对齐 SerialModem CLCC 的 2s 节奏）。真机实测
    # （2026-08-24，三通 611 对照）：WinRT 轮询会经蓝牙 ACL 链路问手机，
    # ACL 与 SCO 音频共享 radio——0.5s 高频轮询把上行打出周期性空洞
    # （中位 32-45ms、间隔约 1s；对照探针裸采集 0 空洞）。响铃/空闲期
    # 保持 0.5s 保证来电响应，接通后降频让音频独占空口。
    POLL_INTERVAL_IN_CALL_SECONDS = 2.0
    # 连续轮询失败达到阈值判定连接丢失（对齐 SerialModem 的 CLCC 失联思路）。
    _POLL_FAIL_THRESHOLD = 6

    def __init__(
        self,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        calls_module: Any = None,
    ) -> None:
        # calls_module 供测试注入假 WinRT 命名空间；生产传 None 走真 winsdk。
        self._calls = calls_module
        self._poll_interval = poll_interval
        self._line: Any = None
        self._lock = threading.RLock()
        self._closed = False
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._poll_fail_count = 0
        self._connection_online = False

        # 通话状态跟踪：call_id → 最近一次快照的 (status, number)。
        self._tracked: dict[str, tuple[int, str]] = {}
        # PhoneCallInfo 按 call_id 缓存：号码/方向在通话生命周期内不变，
        # 每轮重查会产生多余的蓝牙 ACL 往返、抢 SCO 音频时隙（见类注释）。
        # 空号码不缓存（info 可能晚到，下一轮重试）。
        self._info_cache: dict[str, str] = {}
        self._had_active_call = False
        self._ring_notified: set[str] = set()
        self._connected_notified: set[str] = set()

        self._sim_identity: SimIdentity = UNKNOWN_SIM
        self._on_ring: Callable[[str | None], None] | None = None
        self._on_hangup: Callable[[], None] | None = None
        self._on_sms: Callable[[str | None, str, str], None] | None = None
        self._on_call_connected: Callable[[str | None], None] | None = None
        self._on_connection_state: Callable[[bool], None] | None = None
        self._on_sim_identity: Callable[[SimIdentity], None] | None = None

    # ---- 连接生命周期（supervisor/看门狗会重复调用，须幂等）----

    def connect(self) -> None:
        if self._closed:
            raise RuntimeError("modem 已关闭")
        if self._calls is None:
            self._calls = _import_calls()
        line = asyncio.run(self._fetch_default_line())
        if line is None:
            raise RuntimeError(
                "WinRT 未返回默认电话线路——检查手机已与本机蓝牙配对且"
                "「免提电话」服务已启用"
            )
        with self._lock:
            self._line = line
            self._poll_fail_count = 0
        self._set_connection(True)
        self.refresh_sim_identity()
        logger.info(
            "蓝牙话机已连接: %s (can_dial=%s)",
            getattr(line, "display_name", "?"),
            getattr(line, "can_dial", "?"),
        )

    async def _fetch_default_line(self) -> Any:
        calls = self._calls
        store = await calls.PhoneCallManager.request_store_async()
        if store is None:
            return None
        line_id = await store.get_default_line_async()
        if line_id is None:
            return None
        return await calls.PhoneLine.from_id_async(line_id)

    def refresh_sim_identity(self, **_kwargs: Any) -> None:
        """构造蓝牙线路的 SIM 身份（fail-closed）。

        WinRT 蓝牙线路拿不到 PLMN/运营商（真机实测 cellular_details 抛
        Not implemented），carrier 只能诚实标「未知」；免费客服号经
        CARRIER_HOTLINE 覆盖（与 SerialModem 同一施加点语义：下游
        dial_guard/展示拿到的是同一份有效身份）。
        """
        line = self._line
        registered = bool(getattr(line, "can_dial", False)) if line else False
        base = SimIdentity(
            present=line is not None,
            plmn="",
            carrier="未知",
            service_number="",
            registered=registered,
            reg_status="蓝牙话机线路" if registered else "线路不可拨号",
        )
        identity = with_service_number_override(
            base, config.get_str("CARRIER_HOTLINE")
        )
        self._sim_identity = identity
        if self._on_sim_identity:
            try:
                self._on_sim_identity(identity)
            except Exception:  # noqa: BLE001
                logger.exception("on_sim_identity 回调异常")
        if not identity.service_number:
            logger.warning(
                "蓝牙话机后端识别不到运营商，免费客服号未知——拨打已知客服号"
                "会被误拨保护拦下；如需拨测请设置 CARRIER_HOTLINE（美国卡=611）"
            )

    @property
    def sim_identity(self) -> SimIdentity:
        return self._sim_identity

    def initialize_for_voice(self, audio_mode: str = "hfp") -> None:
        """语音通道由 Windows 蓝牙栈管理（SCO 随通话自动建立），无需初始化。"""

    def start_listener(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return  # 幂等：看门狗重连会重复调用
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name="winrt-phone-poll", daemon=True
            )
            self._poll_thread.start()

    def stop_listener(self) -> None:
        self._poll_stop.set()
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._poll_thread = None

    def close(self) -> None:
        self._closed = True
        self.stop_listener()
        with self._lock:
            self._line = None
        self._set_connection(False)

    def is_connected(self) -> bool:
        return self._connection_online

    def pcm_ready(self) -> bool:
        return True

    # ---- 通话控制 ----

    def answer(self) -> None:
        call = self._find_call(_wanted={CALL_INCOMING})
        if call is None:
            logger.warning("answer: 当前没有响铃中的来电")
            return
        call.accept_incoming()
        logger.info("已接听来电")

    def dial(self, number: str) -> str:
        line = self._line
        if line is None:
            raise RuntimeError("蓝牙话机线路未连接")
        # dial(number, displayName)：同步发起；接通与否由轮询循环跟踪上报。
        line.dial(number, number)
        logger.info("外呼请求已发出")
        return "OK"

    def hangup(self) -> None:
        ended = 0
        for call in self._active_calls():
            try:
                call.end()
                ended += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("end() 失败: %s", exc)
        if ended:
            logger.info("已挂断 %d 路通话", ended)

    def is_call_connected(self) -> bool:
        with self._lock:
            return any(
                status in _CONNECTED_STATUSES
                for status, _number in self._tracked.values()
            )

    def send_dtmf(self, digits: str) -> bool:
        digits = (digits or "").strip().upper()
        if not digits:
            return False
        if any(ch not in _DTMF_KEY_VALUES for ch in digits):
            # HFP/WinRT 只支持 0-9*#（DtmfKey 无 A-D）。
            logger.warning("DTMF 输入无效: count=%d, result=failure", len(digits))
            return False
        call = self._find_call(_wanted={CALL_TALKING})
        if call is None:
            logger.warning("DTMF 发送失败: 无通话中的呼叫")
            return False
        dtmf_key = self._calls.DtmfKey
        for ch in digits:
            key = dtmf_key(_DTMF_KEY_VALUES[ch])
            try:
                self._send_dtmf_key(call, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DTMF 发送失败: %s", exc)
                return False
            time.sleep(0.15)  # 位间间隔，与 SerialModem 同参
        logger.info("DTMF 发送完成: count=%d, result=success", len(digits))
        return True

    def _send_dtmf_key(self, call: Any, key: Any) -> None:
        """send_dtmf_key 的双签名兼容：部分 SDK 版本要求带 playback 参数。"""
        try:
            call.send_dtmf_key(key)
        except TypeError:
            call.send_dtmf_key(key, self._calls.DtmfToneAudioPlayback.PLAY)

    def send_sms(self, number: str, text: str) -> bool:
        # HFP/WinRT 均无短信面；本批次明确不做（WIL-96 单独立项）。
        logger.warning("蓝牙话机后端不支持发短信（本批次范围外），已丢弃")
        return False

    # ---- 回调注册（与 Eg25Modem 对齐）----

    def on_ring(self, callback: Callable[[str | None], None]) -> None:
        self._on_ring = callback

    def on_hangup(self, callback: Callable[[], None]) -> None:
        self._on_hangup = callback

    def on_sms(self, callback: Callable[[str | None, str, str], None]) -> None:
        self._on_sms = callback  # 注册但永不触发（无短信面）

    def on_call_connected(self, callback: Callable[[str | None], None]) -> None:
        self._on_call_connected = callback

    def on_connection_state(self, callback: Callable[[bool], None]) -> None:
        self._on_connection_state = callback

    def on_sim_identity(self, callback: Callable[[SimIdentity], None]) -> None:
        self._on_sim_identity = callback

    # ---- 轮询循环（CLCC 等价）----

    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                self._poll_once()
                self._poll_fail_count = 0
            except Exception as exc:  # noqa: BLE001
                self._poll_fail_count += 1
                logger.warning(
                    "通话轮询失败 (%d/%d): %s",
                    self._poll_fail_count, self._POLL_FAIL_THRESHOLD, exc,
                )
                if self._poll_fail_count >= self._POLL_FAIL_THRESHOLD:
                    # 线路失联：置离线并停轮询，交给连接看门狗重建
                    # （对齐 SerialModem「读循环死→看门狗重连」的分工）。
                    self._set_connection(False)
                    return
            self._poll_stop.wait(self._current_poll_interval())

    def _current_poll_interval(self) -> float:
        """接通期降频（ACL 轮询抢 SCO 时隙，见类注释）；其余时段保持灵敏。"""
        if self.is_call_connected():
            return self.POLL_INTERVAL_IN_CALL_SECONDS
        return self._poll_interval

    def _snapshot_calls(self) -> dict[str, tuple[int, str, Any]]:
        """当前活跃通话快照：call_id → (status, number, call 对象)。"""
        line = self._line
        if line is None:
            return {}
        result = line.get_all_active_phone_calls()
        snapshot: dict[str, tuple[int, str, Any]] = {}
        for call in list(result.all_active_phone_calls or []):
            status = int(call.status)
            call_id = str(call.call_id)
            number = self._info_cache.get(call_id, "")
            if not number:
                try:
                    info = call.get_phone_call_info()
                    number = str(info.phone_number or "")
                except Exception:  # noqa: BLE001
                    pass
                if number:
                    self._info_cache[call_id] = number
            snapshot[call_id] = (status, number, call)
        # 结束的通话从缓存清掉，防止 call_id 复用时拿到陈旧号码。
        self._info_cache = {
            cid: num for cid, num in self._info_cache.items() if cid in snapshot
        }
        return snapshot

    def _poll_once(self) -> None:
        snapshot = self._snapshot_calls()
        events: list[tuple[str, str | None]] = []
        with self._lock:
            active_now = False
            for call_id, (status, number, _call) in snapshot.items():
                if status in _ACTIVE_STATUSES:
                    active_now = True
                if status == CALL_INCOMING and call_id not in self._ring_notified:
                    self._ring_notified.add(call_id)
                    events.append(("ring", number or None))
                if (
                    status in _CONNECTED_STATUSES
                    and call_id not in self._connected_notified
                ):
                    self._connected_notified.add(call_id)
                    events.append(("connected", number or None))
            # 之前有活跃通话、现在全没了（消失或 ENDED）→ 挂断收尾。
            if self._had_active_call and not active_now:
                events.append(("hangup", None))
                self._ring_notified.clear()
                self._connected_notified.clear()
            self._had_active_call = active_now
            self._tracked = {
                cid: (status, number)
                for cid, (status, number, _call) in snapshot.items()
            }
        for kind, event_number in events:
            self._dispatch(kind, event_number)

    def _dispatch(self, kind: str, number: str | None) -> None:
        try:
            if kind == "ring" and self._on_ring:
                self._on_ring(number)
            elif kind == "connected" and self._on_call_connected:
                self._on_call_connected(number)
            elif kind == "hangup" and self._on_hangup:
                self._on_hangup()
        except Exception:  # noqa: BLE001
            logger.exception("%s 回调异常", kind)

    # ---- 内部工具 ----

    def _active_calls(self) -> list[Any]:
        try:
            snapshot = self._snapshot_calls()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取活跃通话失败: %s", exc)
            return []
        return [
            call for _status, _number, call in snapshot.values()
            if _status in _ACTIVE_STATUSES
        ]

    def _find_call(self, _wanted: frozenset[int] | set[int]) -> Any:
        try:
            snapshot = self._snapshot_calls()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取活跃通话失败: %s", exc)
            return None
        for _cid, (status, _number, call) in snapshot.items():
            if status in _wanted:
                return call
        return None

    def _set_connection(self, online: bool) -> None:
        changed = online != self._connection_online
        self._connection_online = online
        if changed and self._on_connection_state:
            try:
                self._on_connection_state(online)
            except Exception:  # noqa: BLE001
                logger.exception("on_connection_state 回调异常")
