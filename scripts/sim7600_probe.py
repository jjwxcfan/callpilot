"""SIM7600 (SIMCom) USB 音频可行性一次性探测 —— CallPilot Phase 0 go/no-go 闸门。

macOS 不一定给 SIM7600 的厂商接口自动建 ``/dev/cu.*``（这正是 CallPilot 给
Quectel 写 libusb→PTY 桥的原因），所以这里直接用 pyusb/libusb 走 bulk 端点做
AT 探测，回答一个问题：**这块 SIM7600 能不能通过 USB 传通话音频进/出？**

流程：
  1. 枚举 SIMCom VID(0x1e0e，兼看 Qualcomm 0x05c6) 下的 USB 接口与 bulk 端点。
  2. 逐个 bulk 接口发 ``AT`` 找出 AT 口。
  3. 在 AT 口跑只读查询：``ATI`` / ``AT+CGMM`` / ``AT+CGMR`` 确认型号固件，
     ``AT+CPCMREG=?`` / ``AT+CPCMREG?`` 问它认不认 SIMCom 的 PCM-over-USB，
     ``AT+CUSBPIDSWITCH?`` / ``=?`` 看当前 USB 组合及是否存在带音频的组合。
  4. ``--enable`` 时再尝试使能指令（依 ``=?`` 实际支持情况，不盲发写死指令表），
     然后提示重插 USB 后重跑阶段 A 的音频检查（system_profiler SPAudioDataType）。

SIM7600 的音频 AT 指令随固件差异很大，本脚本以真机实际响应为准、逐条打印，
不预设「哪条一定管用」。只读探测无副作用；状态变更集中在 ``--enable`` 分支。

用法：
    python scripts/sim7600_probe.py            # 只读探测（默认）
    python scripts/sim7600_probe.py --list     # 仅列出 USB bulk 接口
    python scripts/sim7600_probe.py --enable    # 只读探测 + 尝试使能 USB 音频

依赖：pyusb + 系统 libusb（macOS: ``brew install libusb``）。
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import usb.core
import usb.util

# SimTech(SIMCom) 官方 VID；SIM7600 各 USB 组合共用。
SIMCOM_VID = 0x1E0E
# 少数 SIM7600 固件/组合会以 Qualcomm VID 枚举（如 diag/Android 组合），一并扫。
QUALCOMM_VID = 0x05C6
CANDIDATE_VIDS = (SIMCOM_VID, QUALCOMM_VID)

# 只读查询：确认型号 + 探 SIMCom 音频/USB 组合能力。不改任何状态。
READ_ONLY_QUERIES: tuple[tuple[str, str], ...] = (
    ("ATI", "型号/固件"),
    ("AT+CGMM", "型号"),
    ("AT+CGMR", "固件版本"),
    ("AT+CPIN?", "SIM 卡"),
    ("AT+CPCMREG=?", "PCM-over-USB 支持?"),
    ("AT+CPCMREG?", "PCM-over-USB 当前态"),
    ("AT+CPCMFRM?", "PCM 采样率"),
    ("AT+CUSBPIDSWITCH?", "当前 USB 组合(PID)"),
    ("AT+CUSBPIDSWITCH=?", "可选 USB 组合"),
)

# 使能候选：仅在 --enable 且对应 ``=?`` 探到支持时才尝试；逐条打印真机响应。
# 不写死「哪条一定对」——SIM7600 音频使能随固件差异大，以实际响应为准。
ENABLE_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("AT+CPCMREG=1", "使能 PCM-over-USB"),
)


@dataclass(frozen=True)
class UsbPort:
    vid: int
    pid: int
    interface: int
    bulk_in: int
    bulk_out: int
    max_packet: int


def find_devices() -> list[usb.core.Device]:
    """返回所有候选 VID 下的 SIM7600 USB 设备；libusb 缺失时给出安装提示。"""
    try:
        devices: list[usb.core.Device] = []
        for vid in CANDIDATE_VIDS:
            devices.extend(usb.core.find(find_all=True, idVendor=vid) or [])
        return devices
    except usb.core.NoBackendError:
        raise SystemExit(
            "libusb not found — pyusb needs the system libusb library.\n"
            "  安装:  brew install libusb   (macOS)\n"
            "         sudo apt install libusb-1.0-0   (Debian/Ubuntu)"
        ) from None


def discover_ports(dev: usb.core.Device) -> list[UsbPort]:
    """枚举设备各接口的 bulk IN/OUT 端点（AT/数据口都是 bulk 对）。"""
    try:
        cfg = dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
        cfg = dev.get_active_configuration()
    ports: list[UsbPort] = []
    for intf in cfg:
        bulk_in = bulk_out = None
        max_packet = 512
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                bulk_in = ep.bEndpointAddress
                max_packet = ep.wMaxPacketSize
            else:
                bulk_out = ep.bEndpointAddress
        if bulk_in is not None and bulk_out is not None:
            ports.append(
                UsbPort(
                    vid=dev.idVendor,
                    pid=dev.idProduct,
                    interface=intf.bInterfaceNumber,
                    bulk_in=bulk_in,
                    bulk_out=bulk_out,
                    max_packet=max_packet,
                )
            )
    return ports


def _read_response(dev: usb.core.Device, port: UsbPort, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        try:
            data = dev.read(port.bulk_in, port.max_packet, timeout=200)
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError:
            break
        if data:
            chunks.append(bytes(data))
            joined = b"".join(chunks)
            if b"\r\nOK\r\n" in joined or b"\r\nERROR\r\n" in joined or b"+CME ERROR" in joined:
                break
    return b"".join(chunks)


def send_at(dev: usb.core.Device, port: UsbPort, command: str, timeout: float = 2.0) -> str:
    """在已 claim 的接口上发一条 AT 指令，返回清洗后的响应文本。"""
    while True:  # 清空 claim 前遗留的 URC/回显
        try:
            dev.read(port.bulk_in, port.max_packet, timeout=50)
        except usb.core.USBError:
            break
    dev.write(port.bulk_out, (command + "\r").encode("ascii"), timeout=1000)
    raw = _read_response(dev, port, timeout)
    return raw.decode("ascii", "ignore").strip()


def _claim(dev: usb.core.Device, port: UsbPort) -> bool:
    try:
        usb.util.claim_interface(dev, port.interface)
        return True
    except usb.core.USBError as exc:
        print(f"    [interface {port.interface} 无法占用] {exc}")
        return False


def probe_at_port(dev: usb.core.Device, ports: list[UsbPort]) -> UsbPort | None:
    """逐个 bulk 接口发 AT，返回第一个回 OK 的口。"""
    for port in ports:
        if not _claim(dev, port):
            continue
        try:
            resp = send_at(dev, port, "AT", timeout=1.5)
            marker = resp.replace("\r\n", " | ") or "(无响应)"
            print(f"  interface {port.interface}: AT -> {marker}")
            if "OK" in resp:
                return port
        finally:
            usb.util.release_interface(dev, port.interface)
    return None


def _fmt(resp: str) -> str:
    return resp.replace("\r\n", " | ").strip() or "(无响应)"


def run_queries(dev: usb.core.Device, port: UsbPort, queries: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """在 AT 口上跑一组指令，逐条打印并返回 {命令: 响应}。"""
    results: dict[str, str] = {}
    if not _claim(dev, port):
        return results
    try:
        for cmd, label in queries:
            resp = send_at(dev, port, cmd)
            results[cmd] = resp
            print(f"    {label:18s} {cmd:22s} -> {_fmt(resp)}")
    finally:
        usb.util.release_interface(dev, port.interface)
    return results


def _supports(cap_query_resp: str) -> bool:
    """``AT+X=?`` 回了 OK（非 ERROR）即视作该指令族被固件识别。"""
    return "OK" in cap_query_resp and "ERROR" not in cap_query_resp


def main() -> int:
    parser = argparse.ArgumentParser(description="SIM7600 USB 音频可行性探测 (CallPilot Phase 0)")
    parser.add_argument("--list", action="store_true", help="仅列出 USB bulk 接口后退出")
    parser.add_argument(
        "--enable",
        action="store_true",
        help="只读探测后，尝试使能 USB 音频（依 =? 实际支持，逐条打印响应）",
    )
    args = parser.parse_args()

    devices = find_devices()
    if not devices:
        print(f"[FAIL] 未发现 SIM7600 USB 设备 (VID {' / '.join(f'0x{v:04x}' for v in CANDIDATE_VIDS)})")
        print("       确认：模组已上电、用的是数据线(非只充电线)、已插好并重插一次。")
        return 1

    for dev in devices:
        print(f"\n===== 设备 {dev.idVendor:04x}:{dev.idProduct:04x} =====")
        ports = discover_ports(dev)
        if not ports:
            print("  (无 bulk 接口——该组合可能不含串行/AT 口)")
            continue
        for p in ports:
            print(f"  interface {p.interface}: in=0x{p.bulk_in:02x} out=0x{p.bulk_out:02x} max={p.max_packet}")
        if args.list:
            continue

        print("  --- 探测 AT 口 ---")
        at_port = probe_at_port(dev, ports)
        if at_port is None:
            print("  [跳过] 该设备无 AT 响应口")
            continue
        print(f"  [OK] AT 口 = interface {at_port.interface}")

        print("  --- 只读查询 ---")
        results = run_queries(dev, at_port, READ_ONLY_QUERIES)

        pcm_supported = _supports(results.get("AT+CPCMREG=?", ""))
        print(
            "  [判读] SIMCom PCM-over-USB 指令族: "
            + ("已识别 (AT+CPCMREG 可用)" if pcm_supported else "未识别/被拒 —— 该固件可能只走板载 PCM 针脚")
        )

        if args.enable:
            print("  --- 尝试使能 USB 音频 ---")
            for cmd, label in ENABLE_CANDIDATES:
                cap_key = cmd.split("=")[0] + "=?"
                if cap_key in results and not _supports(results[cap_key]):
                    print(f"    [跳过] {label} {cmd}: 前面 {cap_key} 未探到支持，不盲发")
                    continue
                if not _claim(dev, at_port):
                    continue
                try:
                    resp = send_at(dev, at_port, cmd)
                finally:
                    usb.util.release_interface(dev, at_port.interface)
                print(f"    {label:18s} {cmd:22s} -> {_fmt(resp)}")
            print(
                "\n  [下一步] 拔下再插上 SIM7600，然后重跑阶段 A 的音频检查：\n"
                "           system_profiler SPAudioDataType   # 看是否多出输入/输出声卡\n"
                "           ffmpeg -f avfoundation -list_devices true -i \"\"\n"
                "           出现新声卡 -> USB 音频可行(记下声卡名)；仍无 -> 该硬件不可行。"
            )
        usb.util.dispose_resources(dev)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
