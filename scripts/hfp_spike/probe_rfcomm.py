"""Phase 0.2b：验证 Windows 上能否自己开 RFCOMM 连到手机的 HFP AG 拿 AT 通道。

Python 3.12 的 socket 模块在 Windows 上没有 ``AF_BTH``（实测 hasattr 为 False），
所以这里用 ctypes 直调 Winsock2——仓库已有 ctypes 直调先例（``coreaudio.py``）。

**要回答的关键问题**：Windows 自带蓝牙栈已经占着手机的 HFP 连接（那是音频端点
的来源），此时我们还能不能再开一条 HF 连接拿到 AT 通道？
  - 能 → 音频用 Windows 端点、控制用这条 AT 通道，是最理想的组合
    （``+CLIP`` 来电号码、``AT+VTS`` 带外 DTMF 全都拿得到）；
  - 不能 → 控制退回 adb（probe_adb.py），来电号码可能拿不到。

用法::

    python probe_rfcomm.py devices              # 列已配对设备
    python probe_rfcomm.py connect --addr <MAC> # 连 HFP AG 并跑 SLC 握手
"""

from __future__ import annotations

import argparse
import binascii
import ctypes
import json
import pathlib
import sys
import time
from ctypes import wintypes

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = _REPO_ROOT / "docs" / "fixtures" / "hfp_spike"

AF_BTH = 32
SOCK_STREAM = 1
BTHPROTO_RFCOMM = 3
SOL_RFCOMM = 0x0003

# Handsfree Audio Gateway 服务类 UUID：设了 serviceClassId 且 port=0 时，
# Windows 自己做 SDP 解析找通道号，省得我们手写 SDP 查询。
HFP_AG_UUID = "{0000111F-0000-1000-8000-00805F9B34FB}"
HSP_AG_UUID = "{00001112-0000-1000-8000-00805F9B34FB}"

ws2 = ctypes.WinDLL("ws2_32.dll")
bt = ctypes.WinDLL("bthprops.cpl")

# x64 上 SOCKET 是 UINT_PTR、HANDLE 是指针，都是 64 位。ctypes 默认按 c_int
# 取返回值会**截断高 32 位**，拿到的句柄看着像 0/负数，后续调用全失败——
# 必须显式声明 restype/argtypes。
INVALID_SOCKET = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, s: str) -> "GUID":
        g = cls()
        if ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g)):
            raise ValueError("bad GUID {!r}".format(s))
        return g


class SOCKADDR_BTH(ctypes.Structure):
    # ws2bth.h 里该结构体是 pshpack1（紧凑布局，sizeof=30）。ctypes 默认按
    # 自然对齐会把 btAddr 推到 offset 8（sizeof=40），Windows 按 offset 2 读
    # 地址读到垃圾 → connect 报 WSAEADDRNOTAVAIL(10049)（真机实测）。
    _pack_ = 1
    _fields_ = [
        ("addressFamily", ctypes.c_ushort),
        ("btAddr", ctypes.c_ulonglong),
        ("serviceClassId", GUID),
        ("port", ctypes.c_ulong),
    ]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, wintypes.WORD) for n in (
        "wYear", "wMonth", "wDayOfWeek", "wDay",
        "wHour", "wMinute", "wSecond", "wMilliseconds")]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Address", ctypes.c_ulonglong),
        ("ulClassofDevice", wintypes.ULONG),
        ("fConnected", wintypes.BOOL),
        ("fRemembered", wintypes.BOOL),
        ("fAuthenticated", wintypes.BOOL),
        ("stLastSeen", SYSTEMTIME),
        ("stLastUsed", SYSTEMTIME),
        ("szName", ctypes.c_wchar * 248),
    ]


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("fReturnAuthenticated", wintypes.BOOL),
        ("fReturnRemembered", wintypes.BOOL),
        ("fReturnUnknown", wintypes.BOOL),
        ("fReturnConnected", wintypes.BOOL),
        ("fIssueInquiry", wintypes.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", wintypes.HANDLE),
    ]


class WSADATA(ctypes.Structure):
    _fields_ = [
        ("wVersion", wintypes.WORD),
        ("wHighVersion", wintypes.WORD),
        ("szDescription", ctypes.c_char * 257),
        ("szSystemStatus", ctypes.c_char * 129),
        ("iMaxSockets", ctypes.c_ushort),
        ("iMaxUdpDg", ctypes.c_ushort),
        ("lpVendorInfo", ctypes.c_char_p),
    ]


# --- 函数原型：必须在结构体定义之后声明 ---
ws2.WSAStartup.argtypes = [wintypes.WORD, ctypes.POINTER(WSADATA)]
ws2.WSAStartup.restype = ctypes.c_int
ws2.WSAGetLastError.restype = ctypes.c_int
ws2.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
ws2.socket.restype = ctypes.c_void_p          # SOCKET = UINT_PTR
ws2.connect.argtypes = [ctypes.c_void_p, ctypes.POINTER(SOCKADDR_BTH), ctypes.c_int]
ws2.connect.restype = ctypes.c_int
ws2.send.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
ws2.send.restype = ctypes.c_int
ws2.recv.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
ws2.recv.restype = ctypes.c_int
ws2.closesocket.argtypes = [ctypes.c_void_p]
ws2.closesocket.restype = ctypes.c_int
ws2.setsockopt.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int
]
ws2.setsockopt.restype = ctypes.c_int

bt.BluetoothFindFirstDevice.argtypes = [
    ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS),
    ctypes.POINTER(BLUETOOTH_DEVICE_INFO),
]
bt.BluetoothFindFirstDevice.restype = wintypes.HANDLE
bt.BluetoothFindNextDevice.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)
]
bt.BluetoothFindNextDevice.restype = wintypes.BOOL
bt.BluetoothFindDeviceClose.argtypes = [wintypes.HANDLE]
bt.BluetoothFindDeviceClose.restype = wintypes.BOOL

bt.BluetoothFindFirstRadio.restype = wintypes.HANDLE
bt.BluetoothFindRadioClose.argtypes = [wintypes.HANDLE]
bt.BluetoothFindRadioClose.restype = wintypes.BOOL


class BLUETOOTH_FIND_RADIO_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD)]


def radio_present() -> bool:
    """蓝牙是否**开着**。适配器插着但开关关掉时，找设备只会返回空列表——
    分不清「没配对」和「蓝牙关了」，这里显式区分。"""
    params = BLUETOOTH_FIND_RADIO_PARAMS()
    params.dwSize = ctypes.sizeof(params)
    radio = wintypes.HANDLE()
    find = bt.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(radio))
    if not find:
        return False
    bt.BluetoothFindRadioClose(find)
    if radio:
        ctypes.windll.kernel32.CloseHandle(radio)
    return True


def _wsa_start() -> None:
    data = WSADATA()
    rc = ws2.WSAStartup(0x0202, ctypes.byref(data))
    if rc != 0:
        raise OSError("WSAStartup failed: {}".format(rc))


def _fmt_addr(addr: int) -> str:
    raw = addr.to_bytes(6, "little")
    return ":".join("{:02X}".format(b) for b in reversed(raw))


def _parse_addr(text: str) -> int:
    clean = text.replace(":", "").replace("-", "").strip()
    if len(clean) != 12:
        raise ValueError("蓝牙地址应为 12 个十六进制位，收到 {!r}".format(text))
    return int.from_bytes(binascii.unhexlify(clean)[::-1], "little")


def list_devices() -> list[dict]:
    params = BLUETOOTH_DEVICE_SEARCH_PARAMS()
    params.dwSize = ctypes.sizeof(params)
    params.fReturnAuthenticated = 1
    params.fReturnRemembered = 1
    params.fReturnUnknown = 0
    params.fReturnConnected = 1
    params.fIssueInquiry = 0
    params.cTimeoutMultiplier = 0
    params.hRadio = None

    info = BLUETOOTH_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(info)

    found: list[dict] = []
    handle = bt.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
    if not handle:
        return found
    try:
        while True:
            found.append({
                "name": info.szName,
                "address": _fmt_addr(info.Address),
                "class_of_device": "0x{:06X}".format(info.ulClassofDevice),
                "connected": bool(info.fConnected),
                "authenticated": bool(info.fAuthenticated),
                "remembered": bool(info.fRemembered),
            })
            info = BLUETOOTH_DEVICE_INFO()
            info.dwSize = ctypes.sizeof(info)
            if not bt.BluetoothFindNextDevice(handle, ctypes.byref(info)):
                break
    finally:
        bt.BluetoothFindDeviceClose(handle)
    return found


def _require_radio() -> bool:
    if radio_present():
        return True
    print("!! 找不到可用的蓝牙无线电——蓝牙是关着的（或适配器被禁用）。")
    print("   打开：设置 → 蓝牙和其他设备 → 把「蓝牙」开关打开。")
    print("   （适配器硬件在位不等于蓝牙开着；开关关掉时无线电不存在。）")
    return False


def cmd_devices(args: argparse.Namespace) -> int:
    _wsa_start()
    if not _require_radio():
        return 2
    devices = list_devices()
    if not devices:
        print("蓝牙开着，但没有已配对/已记住的设备——先把安卓手机配对上。")
        return 1
    for d in devices:
        # Class of Device 高位 0x02 = Phone 大类，帮用户认出手机。
        cod = int(d["class_of_device"], 16)
        major = (cod >> 8) & 0x1F
        kind = "手机" if major == 0x02 else ("音频" if major == 0x04 else "其他")
        print("{:<28s} {}  {:4s} connected={} auth={}".format(
            d["name"][:28], d["address"], kind, d["connected"], d["authenticated"]))
    _save("bt_devices.json", {"devices": devices})
    return 0


def _sock_connect(addr: int, uuid: str, timeout: float) -> int:
    """开 AF_BTH RFCOMM 并按服务 UUID 连接（Windows 自动做 SDP 解析）。"""
    sock = ws2.socket(AF_BTH, SOCK_STREAM, BTHPROTO_RFCOMM)
    if sock is None or sock == INVALID_SOCKET:
        err = ws2.WSAGetLastError()
        raise OSError(
            "socket(AF_BTH) 失败, WSAGetLastError={} ({})".format(err, _wsa_hint(err))
        )

    sa = SOCKADDR_BTH()
    sa.addressFamily = AF_BTH
    sa.btAddr = addr
    sa.serviceClassId = GUID.from_string(uuid)
    sa.port = 0  # 0 = 让 Windows 按 serviceClassId 查 SDP

    rc = ws2.connect(sock, ctypes.byref(sa), ctypes.sizeof(sa))
    if rc != 0:
        err = ws2.WSAGetLastError()
        ws2.closesocket(sock)
        raise OSError("connect 失败, WSAGetLastError={} ({})".format(err, _wsa_hint(err)))
    return sock


def _wsa_hint(err: int) -> str:
    return {
        10060: "WSAETIMEDOUT—对端没响应",
        10061: "WSAECONNREFUSED—对端拒绝（很可能已被 Windows 自带 HFP 占用）",
        10064: "WSAEHOSTDOWN—设备不在线，先在手机上连上",
        10050: "WSAENETDOWN—蓝牙适配器异常",
        10047: "WSAEAFNOSUPPORT—系统不支持 AF_BTH",
        10022: "WSAEINVAL—参数非法（服务未发布？）",
    }.get(err, "见 Winsock 错误码表")


def cmd_connect(args: argparse.Namespace) -> int:
    _wsa_start()
    if not _require_radio():
        return 2
    if args.addr:
        target_addr = _parse_addr(args.addr)
        target_name = args.addr
    else:
        phones = [d for d in list_devices()
                  if ((int(d["class_of_device"], 16) >> 8) & 0x1F) == 0x02]
        if not phones:
            print("!! 没找到已配对的手机，用 --addr 指定地址。先跑 devices 看列表。")
            return 1
        if len(phones) > 1:
            print("!! 配对了多台手机，用 --addr 指定: {}".format(
                [(p["name"], p["address"]) for p in phones]))
            return 1
        target_addr = _parse_addr(phones[0]["address"])
        target_name = "{} ({})".format(phones[0]["name"], phones[0]["address"])

    uuid = HSP_AG_UUID if args.headset else HFP_AG_UUID
    print("连接 {} 的 {} 服务...".format(
        target_name, "HSP AG" if args.headset else "HFP AG"))

    try:
        sock = _sock_connect(target_addr, uuid, args.timeout)
    except OSError as exc:
        print("!! {}".format(exc))
        print("\n=== 判据 ===")
        print("RFCOMM 拿到 AT 通道: FAIL")
        print("→ 控制通道退回 adb（scripts/hfp_spike/probe_adb.py）。")
        _save("rfcomm_connect.json", {"ok": False, "error": str(exc)})
        return 1

    print("已连上！开始 SLC 握手。\n")
    transcript: list[dict] = []

    def send(cmd: str) -> str:
        payload = (cmd + "\r").encode("ascii")
        ws2.send(sock, payload, len(payload), 0)
        time.sleep(0.4)
        buf = ctypes.create_string_buffer(4096)
        n = ws2.recv(sock, buf, 4096, 0)
        reply = buf.raw[:n].decode("ascii", "replace") if n > 0 else ""
        print("  > {}\n  < {}".format(cmd, reply.replace("\r\n", " | ").strip()))
        transcript.append({"sent": cmd, "recv": reply})
        return reply

    try:
        # HF 侧 SLC 建立序列（HFP 1.6）。BRSF=63 声明 bit0-5，含 bit5
        # 「enhanced call status」——AT+CLCC 要靠它。不声明 codec negotiation
        # (bit7)，避免协商出我们接不住的 mSBC。
        send("AT+BRSF=63")
        send("AT+CIND=?")
        send("AT+CIND?")
        send("AT+CMER=3,0,0,1")
        send("AT+CHLD=?")
        send("AT+CLIP=1")
        send("AT+CLCC")
        if args.watch:
            print("\n监听 {}s 的主动上报（现在拨这张卡试试来电）...".format(args.watch))
            deadline = time.monotonic() + args.watch
            while time.monotonic() < deadline:
                buf = ctypes.create_string_buffer(4096)
                n = ws2.recv(sock, buf, 4096, 0)
                if n > 0:
                    urc = buf.raw[:n].decode("ascii", "replace")
                    print("  URC< {}".format(urc.replace("\r\n", " | ").strip()))
                    transcript.append({"urc": urc})
    finally:
        ws2.closesocket(sock)

    joined = " ".join(t.get("recv", "") + t.get("urc", "") for t in transcript)
    print("\n=== 判据 ===")
    print("RFCOMM 拿到 AT 通道 : PASS")
    print("AG 回了 +BRSF       : {}".format("PASS" if "+BRSF" in joined else "FAIL"))
    print("AG 回了 +CIND       : {}".format("PASS" if "+CIND" in joined else "FAIL"))
    print("看到 +CLIP 来电号码 : {}".format(
        "PASS" if "+CLIP" in joined else "未观察到（要在响铃时看，用 --watch）"))
    _save("rfcomm_connect.json", {"ok": True, "transcript": transcript})
    return 0


def _save(name: str, data: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), **data}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已落盘: {}".format(path))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("devices", help="列已配对蓝牙设备")
    pd.set_defaults(func=cmd_devices)

    pc = sub.add_parser("connect", help="连 HFP AG 并跑 SLC 握手")
    pc.add_argument("--addr", help="蓝牙地址，如 AA:BB:CC:DD:EE:FF（省略则自动挑手机）")
    pc.add_argument("--timeout", type=float, default=10.0)
    pc.add_argument("--watch", type=float, default=0.0, help="握手后监听主动上报的秒数")
    pc.add_argument("--headset", action="store_true", help="改试 HSP AG（HFP 被占时的退路）")
    pc.set_defaults(func=cmd_connect)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
