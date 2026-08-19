"""SIM7600 故障取证抓包：给模组原厂（SIMCom）分析 PCM 子系统劣化用。

**为什么走 direct-USB 而不是经 app/桥**：抓证据要把变量降到最少。本脚本用
pyusb 直连 USB bulk 端点，不经过 CallPilot 的桥、串口层与业务代码——这样原厂
拿到的日志里只有「模组的行为」，不掺任何上层软件因素（此前正是用这种方式证明
了劣化与宿主软件无关）。

产出（一次运行一个目录，直接打包发原厂）：
  at-trace.log      每条 AT 指令/响应/URC，毫秒时间戳（含失败的重试）
  usb-pcm.log       iface4 PCM 端点收发字节、速率、超时与错误码
  state-before.txt  通话前模组状态快照
  state-after.txt   通话后模组状态快照
  uplink.wav        上行 PCM（对端语音；劣化时可见削顶）
  downlink-sent.wav 下行实际写入模组的 PCM
  summary.json      机器可读摘要（型号/固件/各阶段耗时/判定）

典型取证流程（每步都是独立一次运行，日志目录各自留存）：
  1) 冷插拔后第 1 通（正常基线）  python scripts/sim7600_diag_capture.py --dial +1XXX --tag good
  2) 第 2 通（预期劣化）          python scripts/sim7600_diag_capture.py --dial +1XXX --tag degraded
  3) CFUN 复位后再打             python scripts/sim7600_diag_capture.py --dial +1XXX --cfun-reset --tag after-cfun
  4) USB 组合切换复位后再打       python scripts/sim7600_diag_capture.py --dial +1XXX --pid-reset --tag after-pidswitch

来电取证用 --wait-ring 代替 --dial。
依赖：pyusb + libusb（macOS: brew install libusb，运行需 DYLD_LIBRARY_PATH=/opt/homebrew/lib）。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import usb.core
import usb.util

SIMCOM_VID = 0x1E0E
AT_IFACE = 2
PCM_IFACE = 4
DIAG_IFACE = 0          # Qualcomm DIAG（实测 iface0，主动吐 HDLC 帧，0x7E 结尾）
PCM_RATE = 8000


# ---------------------------------------------------------------- DIAG

def _crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def diag_frame(payload: bytes) -> bytes:
    """按 Qualcomm DIAG 的 HDLC 封帧：payload + CRC16(X.25) + 转义 + 0x7E。"""
    frame = payload + _crc16_x25(payload).to_bytes(2, "little")
    out = bytearray()
    for b in frame:
        if b in (0x7D, 0x7E):
            out += bytes([0x7D, b ^ 0x20])
        else:
            out.append(b)
    out.append(0x7E)
    return bytes(out)


class DiagCapture:
    """后台持续抓 DIAG 原始码流存盘（原厂 QCAT/QXDM 可解析）。

    默认**被动抓取**：不下发任何改状态的指令，只读模组自发上报的事件帧，
    保证取证不引入新变量。仅在启动时发一条只读的版本查询（DIAG_VERNO_F），
    用于把固件构建号钉进日志。
    """

    def __init__(self, dev, raw_path: Path, trace) -> None:
        self.dev = dev
        self.raw_path = raw_path
        self.trace = trace
        self.bytes_read = 0
        self.version = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ok = False

    def start(self) -> bool:
        try:
            usb.util.claim_interface(self.dev, DIAG_IFACE)
        except usb.core.USBError as exc:
            self.trace(f"DIAG 口 iface{DIAG_IFACE} 无法占用，跳过 DIAG 抓取: {exc}")
            return False
        self._ok = True
        try:  # 只读版本查询：把固件构建号写进日志，便于原厂对号入座
            self.dev.write(0x01, diag_frame(b"\x00"), timeout=1000)
            time.sleep(0.5)
            resp = b""
            for _ in range(15):
                try:
                    resp += bytes(self.dev.read(0x81, 512, timeout=200))
                except usb.core.USBError:
                    break
            self.version = "".join(chr(c) if 32 <= c < 127 else "." for c in resp)
            self.trace(f"DIAG 版本查询: {self.version}")
        except usb.core.USBError as exc:
            self.trace(f"DIAG 版本查询失败（不影响被动抓取）: {exc}")
        self._enable_logging()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.trace(f"DIAG 抓取已启动 -> {self.raw_path.name}")
        return True

    def _enable_logging(self) -> None:
        """打开事件上报与 F3 调试消息。

        实测：纯被动读 DIAG 口拿不到数据（4s / 0 字节）；下发使能后才有流
        （事件 5s/78B，F3 消息 5s/226B）。下发内容全部记进 at-trace，便于原厂
        知道我们改了什么、排除「是你把模组配坏了」的疑虑。
        """
        for payload, desc in (
            (b"\x60\x01", "DIAG_EVENT_REPORT_F 事件上报使能"),
            (bytes([0x7D, 0x04]) + (0).to_bytes(2, "little") + (25).to_bytes(2, "little")
             + b"\x00\x00" + (0xFFFFFFFF).to_bytes(4, "little"),
             "DIAG_EXT_MSG_CONFIG_F F3 调试消息全开"),
        ):
            try:
                self.dev.write(0x01, diag_frame(payload), timeout=1000)
                time.sleep(0.3)
                self.trace(f"DIAG 下发: {desc} ({payload.hex()})")
            except usb.core.USBError as exc:
                self.trace(f"DIAG 下发失败 {desc}: {exc}")

    def _loop(self) -> None:
        with self.raw_path.open("wb") as fh:
            while not self._stop.is_set():
                try:
                    data = self.dev.read(0x81, 512, timeout=200)
                except usb.core.USBTimeoutError:
                    continue
                except usb.core.USBError:
                    break
                if data:
                    fh.write(bytes(data))
                    fh.flush()
                    self.bytes_read += len(data)

    def ensure_running(self, dev) -> None:
        """呼叫建立时 USB 会重枚举、旧句柄失效——用新句柄续抓，避免通话段日志断片。"""
        if self._thread is not None and self._thread.is_alive() and dev is self.dev:
            return
        self.dev = dev
        self._stop.clear()
        try:
            usb.util.claim_interface(self.dev, DIAG_IFACE)
        except usb.core.USBError as exc:
            self.trace(f"DIAG 续抓失败: {exc}")
            return
        self._ok = True
        self._enable_logging()  # 重枚举后掩码会复位，需重新打开
        self._thread = threading.Thread(target=self._loop_append, daemon=True)
        self._thread.start()
        self.trace("DIAG 抓取已在重枚举后续上")

    def _loop_append(self) -> None:
        with self.raw_path.open("ab") as fh:
            while not self._stop.is_set():
                try:
                    data = self.dev.read(0x81, 512, timeout=200)
                except usb.core.USBTimeoutError:
                    continue
                except usb.core.USBError:
                    break
                if data:
                    fh.write(bytes(data))
                    fh.flush()
                    self.bytes_read += len(data)

    def stop(self) -> None:
        if not self._ok:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            usb.util.release_interface(self.dev, DIAG_IFACE)
        except Exception:
            pass
        self.trace(f"DIAG 抓取结束，共 {self.bytes_read} 字节")

# 每通前后都抓的状态快照：音频链路 + 射频 + 上次通话结束原因。
STATE_QUERIES = (
    ("ATI", "型号/固件"),
    ("AT+CGMR", "固件版本"),
    ("AT+CFUN?", "功能模式"),
    ("AT+CPIN?", "SIM"),
    ("AT+CPCMREG?", "PCM-over-USB 当前态"),
    ("AT+CPCMREG=?", "PCM-over-USB 支持范围"),
    ("AT+CPCMFRM?", "PCM 采样率"),
    ("AT+CSDVC?", "音频通道路由"),
    ("AT+CLVL?", "RX 音量"),
    ("AT+CMICGAIN?", "MIC 增益"),
    ("AT+COUTGAIN?", "OUT 增益"),
    ("AT+CPCMBANDWIDTH?", "PCM 带宽"),
    ("AT+CUSBPIDSWITCH?", "USB 组合"),
    ("AT+CPSI?", "服务小区/射频"),
    ("AT+CSQ", "信号强度"),
    ("AT+CEER", "上次通话结束原因"),
    ("AT+CLCC", "当前通话列表"),
)


def call_stat(clcc: str) -> int | None:
    """取 +CLCC 的 stat 字段（第 3 个）：0=通话中 1=保持 2=拨号中 3=振铃 4=来电 5=等待。

    早期误把第 2 个字段（dir）当 stat，导致「振铃」被判成「已接通」而空抓一通。
    """
    for line in clcc.splitlines():
        line = line.strip()
        if line.startswith("+CLCC:"):
            fields = line[len("+CLCC:"):].split(",")
            if len(fields) >= 3:
                try:
                    return int(fields[2].strip())
                except ValueError:
                    return None
    return None


def ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]


class Trace:
    """同时写文件与终端的时间戳日志。"""

    def __init__(self, path: Path) -> None:
        self.fh = path.open("w", encoding="utf-8")

    def __call__(self, line: str, echo: bool = True) -> None:
        text = f"[{ts()}] {line}"
        self.fh.write(text + "\n")
        self.fh.flush()
        if echo:
            print(text)

    def close(self) -> None:
        self.fh.close()


class Modem:
    """direct-USB AT/PCM 通道；SIM7600 呼叫建立时会重枚举，故支持重开。"""

    def __init__(self, at_trace: Trace, usb_trace: Trace) -> None:
        self.at_trace = at_trace
        self.usb_trace = usb_trace
        self.dev = None
        self.ports: dict[int, tuple[int, int, int]] = {}
        self.open()

    def open(self, attempts: int = 10) -> None:
        self.close()
        last = None
        for _ in range(attempts):
            try:
                dev = usb.core.find(idVendor=SIMCOM_VID)
                if dev is None:
                    raise usb.core.USBError("device not present")
                cfg = dev.get_active_configuration()
                ports = {}
                for intf in cfg:
                    bin_ = bout = None
                    mp = 512
                    for ep in intf:
                        if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                            continue
                        if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                            bin_, mp = ep.bEndpointAddress, ep.wMaxPacketSize
                        else:
                            bout = ep.bEndpointAddress
                    if bin_ is not None and bout is not None:
                        ports[intf.bInterfaceNumber] = (bin_, bout, mp)
                usb.util.claim_interface(dev, AT_IFACE)
                self.dev, self.ports = dev, ports
                return
            except usb.core.USBError as exc:
                last = exc
                time.sleep(0.5)
        raise RuntimeError(f"无法打开 SIM7600: {last}")

    def close(self) -> None:
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
        self.dev = None

    def _raw_at(self, cmd: str, timeout: float) -> str:
        ep_in, ep_out, mp = self.ports[AT_IFACE]
        while True:  # 清残留 URC，避免串到本条响应里
            try:
                self.dev.read(ep_in, mp, timeout=50)
            except usb.core.USBError:
                break
        self.dev.write(ep_out, (cmd + "\r").encode("ascii"), timeout=1000)
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while time.monotonic() < deadline:
            try:
                data = self.dev.read(ep_in, mp, timeout=200)
            except usb.core.USBTimeoutError:
                continue
            if data:
                chunks.append(bytes(data))
                joined = b"".join(chunks)
                if any(m in joined for m in (b"\r\nOK\r\n", b"ERROR", b"NO CARRIER",
                                             b"NO ANSWER", b"BUSY")):
                    break
        return b"".join(chunks).decode("ascii", "ignore").strip()

    def at(self, cmd: str, timeout: float = 3.0, retry: bool = True) -> str:
        """发一条 AT 并把「原始响应」逐条记进 trace（原厂要看的就是这个）。"""
        self.at_trace(f"TX  {cmd}")
        try:
            resp = self._raw_at(cmd, timeout)
        except usb.core.USBError as exc:
            self.at_trace(f"ERR {cmd} -> USBError {exc}")
            if not retry:
                raise
            time.sleep(0.6)
            self.open()  # 呼叫建立瞬间会重枚举，重开后重试一次
            self.at_trace("--- USB 句柄失效，已重开设备后重试 ---")
            resp = self._raw_at(cmd, timeout)
        self.at_trace(f"RX  {resp!r}")
        return resp

    def snapshot(self, path: Path, label: str) -> dict:
        out = {}
        with path.open("w", encoding="utf-8") as fh:
            fh.write(f"# 模组状态快照（{label}） {datetime.now().isoformat()}\n\n")
            for cmd, desc in STATE_QUERIES:
                resp = self.at(cmd)
                out[cmd] = resp
                fh.write(f"{desc}\n  {cmd}\n  -> {resp!r}\n\n")
        return out


def pcm_capture(modem: Modem, seconds: float, tone: bytes | None = None) -> dict:
    """读 iface4 上行、可选写下行，记录字节/速率/错误——证明端点是否真的在流。"""
    if PCM_IFACE not in modem.ports:
        modem.usb_trace(f"iface{PCM_IFACE} 不存在，可用: {sorted(modem.ports)}")
        return {"error": "pcm interface missing"}
    ep_in, ep_out, mp = modem.ports[PCM_IFACE]
    usb.util.claim_interface(modem.dev, PCM_IFACE)
    stats = {"read_bytes": 0, "write_bytes": 0, "read_timeouts": 0,
             "write_timeouts": 0, "errors": []}
    uplink = bytearray()
    sent = bytearray()
    chunk = 640  # 40ms @ 8kHz/16-bit
    offset = 0
    deadline = time.monotonic() + seconds
    next_write = time.monotonic()
    modem.usb_trace(f"PCM 采集开始 iface{PCM_IFACE} in=0x{ep_in:02x} out=0x{ep_out:02x} {seconds:.0f}s")
    try:
        while time.monotonic() < deadline:
            try:
                data = modem.dev.read(ep_in, mp, timeout=100)
                if data:
                    uplink.extend(bytes(data))
                    stats["read_bytes"] += len(data)
            except usb.core.USBTimeoutError:
                stats["read_timeouts"] += 1
            except usb.core.USBError as exc:
                stats["errors"].append(f"read: {exc}")
                modem.usb_trace(f"PCM 读错误: {exc}")
                break
            if tone and time.monotonic() >= next_write:
                payload = tone[offset:offset + chunk] or tone[:chunk]
                offset = (offset + chunk) % max(len(tone), 1)
                try:
                    modem.dev.write(ep_out, payload, timeout=500)
                    sent.extend(payload)
                    stats["write_bytes"] += len(payload)
                except usb.core.USBTimeoutError:
                    stats["write_timeouts"] += 1
                    modem.usb_trace("PCM 写超时（模组未排空端点）")
                except usb.core.USBError as exc:
                    stats["errors"].append(f"write: {exc}")
                    modem.usb_trace(f"PCM 写错误: {exc}")
                    break
                next_write += chunk / 2 / PCM_RATE
    finally:
        try:
            usb.util.release_interface(modem.dev, PCM_IFACE)
        except Exception:
            pass
    stats["read_Bps"] = round(stats["read_bytes"] / seconds)
    stats["write_Bps"] = round(stats["write_bytes"] / seconds)
    stats["uplink_pcm"] = bytes(uplink)
    stats["sent_pcm"] = bytes(sent)
    modem.usb_trace(
        f"PCM 采集结束 读={stats['read_bytes']}B ({stats['read_Bps']}B/s) "
        f"写={stats['write_bytes']}B 读超时={stats['read_timeouts']} 写超时={stats['write_timeouts']}"
    )
    return stats


def analyze_uplink(pcm: bytes) -> dict:
    """上行电平与削波占比——劣化态的客观指纹（peak 恒 32768 / 高削波率）。"""
    if len(pcm) < 2:
        return {"samples": 0}
    import array
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    peak = max((abs(s) for s in samples), default=0)
    clipped = sum(1 for s in samples if abs(s) >= 32000)
    silent = sum(1 for s in samples if abs(s) < 200)
    rms = (sum(float(s) * s for s in samples) / len(samples)) ** 0.5 if samples else 0.0
    return {
        "samples": len(samples),
        "peak": peak,
        "rms": round(rms),
        "clipped_pct": round(100 * clipped / len(samples), 2),
        "silent_pct": round(100 * silent / len(samples), 1),
    }


def write_wav(path: Path, pcm: bytes) -> None:
    if not pcm:
        return
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(PCM_RATE)
        w.writeframes(pcm)


def tone_pcm(seconds: float = 3.0, freq: int = 440) -> bytes:
    import math
    import struct
    return b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / PCM_RATE)))
        for i in range(int(seconds * PCM_RATE))
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="SIM7600 PCM 劣化取证抓包（供原厂分析）")
    ap.add_argument("--dial", help="外呼号码（与 --wait-ring 二选一）")
    ap.add_argument("--wait-ring", action="store_true", help="等来电并自动接听")
    ap.add_argument("--tag", default="run", help="本次取证标签，如 good / degraded / after-cfun")
    ap.add_argument("--seconds", type=float, default=15.0, help="通话中 PCM 采集时长")
    ap.add_argument("--outdir", default="diag-logs", help="日志根目录")
    ap.add_argument("--cfun-reset", action="store_true",
                    help="通话前先跑 AT+CFUN=0 → 等待 → AT+CFUN=1（验证能否替代物理插拔）")
    ap.add_argument("--pid-reset", action="store_true",
                    help="通话前切换 USB 组合再切回（软件版“重插”，会触发重枚举）")
    ap.add_argument("--no-tone", action="store_true", help="不向下行写测试音（只测上行）")
    ap.add_argument("--diag-only", action="store_true",
                    help="只抓 DIAG（iface0），不碰 AT/PCM——可与桥+app 并行跑真实 AI 通话")
    ap.add_argument("--minutes", type=float, default=30.0, help="--diag-only 的最长抓取时长")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.outdir) / f"{stamp}-{args.tag}"
    outdir.mkdir(parents=True, exist_ok=True)

    at_trace = Trace(outdir / "at-trace.log")
    usb_trace = Trace(outdir / "usb-pcm.log")
    summary: dict = {"tag": args.tag, "started_at": datetime.now().isoformat(),
                     "options": {k: v for k, v in vars(args).items()}}

    at_trace(f"=== SIM7600 取证开始 tag={args.tag} ===")

    if args.diag_only:
        # 只占 iface0；iface2(AT)/iface4(PCM) 留给桥与 app，实现「真实 AI 通话 + 底层日志」并抓。
        at_trace("=== DIAG-only 模式：与桥/app 并行抓底层日志，不碰 AT/PCM ===")
        deadline = time.monotonic() + args.minutes * 60
        raw = outdir / "diag-raw.bin"
        total = 0
        while time.monotonic() < deadline:
            try:
                dev = usb.core.find(idVendor=SIMCOM_VID)
                if dev is None:
                    time.sleep(1)
                    continue
                cap = DiagCapture(dev, raw, at_trace)
                if not cap.start():
                    time.sleep(2)
                    continue
                summary.setdefault("diag_version", cap.version)
                while time.monotonic() < deadline and cap._thread and cap._thread.is_alive():
                    time.sleep(0.5)
                cap.stop()
                total += cap.bytes_read
                at_trace("DIAG 连接中断（多半是呼叫建立重枚举），1s 后重连续抓")
                usb.util.dispose_resources(dev)
                time.sleep(1)
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001
                at_trace(f"DIAG 抓取异常，重试: {exc}")
                time.sleep(2)
        summary["diag_bytes"] = total
        summary["result"] = "diag_only"
        (outdir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        at_trace(f"=== DIAG 抓取结束，共 {total} 字节，目录: {outdir} ===")
        at_trace.close()
        usb_trace.close()
        return 0

    modem = Modem(at_trace, usb_trace)
    modem.at("ATE0")

    diag = DiagCapture(modem.dev, outdir / "diag-raw.bin", at_trace)
    diag.start()
    summary["diag_version"] = diag.version

    try:
        if args.cfun_reset:
            at_trace("=== CFUN 复位试验：AT+CFUN=0 → 5s → AT+CFUN=1 ===")
            t0 = time.monotonic()
            modem.at("AT+CFUN=0", timeout=10)
            time.sleep(5)
            modem.at("AT+CFUN=1", timeout=10)
            for _ in range(20):  # 等注册回来
                time.sleep(1)
                if "+CPIN: READY" in modem.at("AT+CPIN?"):
                    if ",1" in modem.at("AT+CREG?") or ",5" in modem.at("AT+CREG?"):
                        break
            summary["cfun_reset_seconds"] = round(time.monotonic() - t0, 1)
            at_trace(f"=== CFUN 复位完成，耗时 {summary['cfun_reset_seconds']}s ===")

        if args.pid_reset:
            at_trace("=== USB 组合切换试验（软件版重插）===")
            cur = modem.at("AT+CUSBPIDSWITCH?")
            at_trace(f"当前组合: {cur!r}；切到 9011 再切回 9001")
            modem.at("AT+CUSBPIDSWITCH=9011,1,1", timeout=10)
            time.sleep(8)
            modem.open()
            modem.at("AT+CUSBPIDSWITCH=9001,1,1", timeout=10)
            time.sleep(8)
            modem.open()
            modem.at("ATE0")
            at_trace("=== USB 组合已切回 ===")

        summary["state_before"] = modem.snapshot(outdir / "state-before.txt", "通话前")

        # ---- 建立通话 ----
        if args.wait_ring:
            at_trace("=== 等待来电（对端拨入本模组号码）===")
            while True:
                clcc = modem.at("AT+CLCC")
                stat = call_stat(clcc)
                if stat in (4, 5):
                    at_trace("检测到来电，发送 ATA 接听")
                    modem.at("ATA", timeout=10)
                    break
                if stat == 0:  # 已经是通话中（ATA 之前就接通）
                    break
                time.sleep(1)
            for _ in range(15):  # 等真正进入通话态
                if call_stat(modem.at("AT+CLCC")) == 0:
                    break
                time.sleep(1)
        elif args.dial:
            at_trace(f"=== 外呼 {args.dial} ===")
            modem.at(f"ATD{args.dial};", timeout=10)
            connected = False
            for _ in range(45):
                time.sleep(1)
                clcc = modem.at("AT+CLCC")
                stat = call_stat(clcc)
                if stat == 0:
                    connected = True
                    break
                if stat is None:
                    at_trace("通话已结束（对方未接/拒接）")
                    summary["result"] = "not_connected"
                    return 2
            if not connected:
                at_trace("等待接通超时")
                summary["result"] = "answer_timeout"
                return 2
        else:
            at_trace("未指定 --dial / --wait-ring，仅抓状态快照后退出")
            summary["result"] = "state_only"
            return 0

        diag.ensure_running(modem.dev)  # 呼叫建立会重枚举，续上 DIAG 抓取
        at_trace("=== 通话已接通，开始使能 PCM 通道（每次尝试都记录原始响应）===")
        enable_attempts = []
        enabled = False
        for i in range(6):
            resp = modem.at("AT+CPCMREG=1")
            enable_attempts.append(resp)
            if "OK" in resp:
                enabled = True
                at_trace(f"CPCMREG 第 {i + 1} 次尝试成功")
                break
            time.sleep(0.3)
        summary["cpcmreg_attempts"] = enable_attempts
        summary["cpcmreg_enabled"] = enabled
        summary["cpcmreg_readback"] = modem.at("AT+CPCMREG?")
        if not enabled:
            at_trace("!!! CPCMREG 全部尝试失败——本通即为劣化态样本 !!!")

        stats = pcm_capture(modem, args.seconds,
                            tone=None if args.no_tone else tone_pcm())
        uplink, sent = stats.pop("uplink_pcm", b""), stats.pop("sent_pcm", b"")
        write_wav(outdir / "uplink.wav", uplink)
        write_wav(outdir / "downlink-sent.wav", sent)
        summary["pcm"] = stats
        summary["uplink_analysis"] = analyze_uplink(uplink)
        at_trace(f"上行分析: {summary['uplink_analysis']}")

        modem.at("AT+CPCMREG=0")
        modem.at("AT+CHUP", timeout=10)  # SIM7600 语音挂断须 CHUP，ATH 不可靠
        at_trace("=== 已挂断 ===")
        summary["state_after"] = modem.snapshot(outdir / "state-after.txt", "通话后")
        summary["result"] = "completed"
        return 0
    finally:
        try:
            modem.at("AT+CHUP", timeout=5, retry=False)
        except Exception:
            pass
        diag.stop()
        summary["diag_bytes"] = diag.bytes_read
        modem.close()
        summary["ended_at"] = datetime.now().isoformat()
        (outdir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        at_trace(f"=== 取证结束，日志目录: {outdir} ===")
        at_trace.close()
        usb_trace.close()
        print(f"\n日志已保存: {outdir}")


if __name__ == "__main__":
    raise SystemExit(main())
