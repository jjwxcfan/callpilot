"""Expose Quectel EC20 USB vendor serial interfaces as macOS PTYs.

macOS can see EC20/EG25 USB interfaces but does not create /dev/cu.* ports for
Quectel vendor-specific serial functions. This bridge talks to the bulk USB
endpoints with libusb/PyUSB and presents a pseudo terminal for pyserial.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import usb.backend.libusb1
import usb.core
import usb.util

logger = logging.getLogger("ec20_usb_pty")

VID = 0x2C7C
PID = 0x0125

# 连续写超时容忍上限：≤此值视作瞬时忙丢帧继续，超过判定设备掉线拆桥重连。
PTY_WRITE_TIMEOUT_TOLERANCE = 3

LOCK_PATH = Path("/tmp/ec20-usb-pty.lock")


def bundled_libusb_path() -> Path | None:
    """Return bundled libusb dylib path when running from the macOS app."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    candidate = Path(base) / "lib" / "libusb-1.0.0.dylib"
    return candidate if candidate.is_file() else None


def libusb_backend():
    """PyUSB backend, preferring the dylib bundled in CallPilot.app."""
    bundled = bundled_libusb_path()
    if bundled is None:
        return None

    def find_library(_name: str) -> str:
        return str(bundled)

    return usb.backend.libusb1.get_backend(find_library=find_library)


# ---- 实例锁持有者健康判定（issue #124）----
#
# 物理拔插可能让旧桥挂死为不可中断内核等待（macOS ps STAT 首字母 "U"，最恶劣
# 形态：主线程卡死在 os.close() 内核调用——拔出瞬间正关闭已失效的设备 fd），
# 此时 kill -9 无效、flock 永不释放，新桥 acquire 永远失败。真机验证过的救援
# 剧本（2026-08-19）：删除锁文件后直接起新桥——flock 绑定 inode，新文件即新锁；
# 僵尸桥卡死在旧设备实例的 close 上，不会再碰新插回的设备，无双桥争抢风险。
# 以下把该剧本固化：acquire 失败时校验持锁进程状态，确认挂死/不存在才走
# 「删锁重建」；健康持锁进程维持原有拒绝行为，绝不抢健康实例的锁。


def parse_lock_holder_pid(text: str) -> int | None:
    """解析锁文件内容里的持锁 PID；内容为空/非法时返回 None。"""
    text = text.strip()
    if not text.isdigit():
        return None
    pid = int(text)
    return pid if pid > 0 else None


def is_hung_stat(stat: str) -> bool:
    """ps STAT 是否为不可中断内核等待（macOS/BSD 为 "U" 开头；Linux 为 "D"）。"""
    return stat.strip()[:1] in {"U", "D"}


def classify_lock_holder(pid: int | None, alive: bool | None, stat: str | None) -> str:
    """纯判定：持锁进程一次采样处于什么状态（离线可测，issue #124）。

    - "unknown"：锁文件里没有可解析的 PID，或探测本身失败（alive/stat 拿不到）
      ——信息不足，保守不抢锁。ps 超时/失败≠进程不存在：高负载下把健康持有者
      判成 missing 会误抢活锁，故 stat 缺失但进程存在时一律 unknown；
    - "missing"：os.kill(pid, 0) 确认进程不存在但 flock 仍被持有（异常态）——可抢；
    - "hung"：进程挂死在不可中断内核等待（U/D），kill -9 也无效——按剧本抢锁
      （单次采样只是候选，确认需 judge_lock_holder 连续两次）；
    - "healthy"：进程存在且未挂死——维持拒绝，绝不抢健康实例的锁。
    """
    if pid is None:
        return "unknown"
    if alive is False:
        return "missing"
    if alive is None or stat is None:
        return "unknown"
    if is_hung_stat(stat):
        return "hung"
    return "healthy"


def process_alive(pid: int) -> bool | None:
    """os.kill(pid, 0) 探活：True 存在 / False 不存在（ESRCH）/ None 探测失败。

    与 ps 的区别：ps 超时或失败时无法区分「进程不存在」和「探测手段坏了」，
    kill(pid, 0) 是内核直答，EPERM 也说明进程存在。
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def process_stat(pid: int) -> str | None:
    """取进程的 ps STAT 字段；拿不到（进程不存在/ps 失败）返回 None。

    None 不代表进程不存在——「不存在 vs 探测失败」由 process_alive 区分。
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stat = out.stdout.strip()
    return stat or None


def judge_lock_holder(
    pid: int | None,
    stat_fn: Callable[[int], str | None] | None = None,
    alive_fn: Callable[[int], bool | None] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    confirm_interval: float = 1.0,
) -> str:
    """两段式挂死判定：连续两次采样（间隔 ~1s）都 U/D 才算 "hung"。

    健康进程做普通磁盘 I/O 也会瞬时进入不可中断态，单次 ps 采样会误判；
    真正的拔插挂死是持续性的（重启系统前不消失），两次采样零漏报。
    第二次采样为其他结果时按第二次算（healthy=瞬态、missing=已消失、
    unknown=保守不抢）。fn 参数默认晚绑定模块全局，便于单测注入。
    """
    stat_fn = process_stat if stat_fn is None else stat_fn
    alive_fn = process_alive if alive_fn is None else alive_fn
    sleep_fn = time.sleep if sleep_fn is None else sleep_fn
    if pid is None:
        return "unknown"
    first = classify_lock_holder(pid, alive_fn(pid), stat_fn(pid))
    if first != "hung":
        return first
    sleep_fn(confirm_interval)
    return classify_lock_holder(pid, alive_fn(pid), stat_fn(pid))


# ---- 删锁的竞态防护（评审必修，issue #124）----
#
# 「删锁重建」天然带三类交错风险：
#   a) 基于陈旧判定 unlink——B 对自己打开的旧文件判定 hung 后去删路径，但路径
#      上可能已是救援者 A 刚重建并持锁的活文件，B 会删掉 A 的活锁；
#   b) flock 后一次性 inode 校验防不住「校验通过后被 unlink」的交错，可能双桥
#      并立双双 claim USB；
#   c) 同一 inode 被新实例重新 flock（missing 异常态下 flock 本就空闲）——
#      inode 比对不变，但持有者已经换人，按陈旧内容判定会误删活锁。
# 封死方案：一把独立的 rescue 锁（<lock>.rescue）串行化所有「判定→删锁→重建」
# 与正常拿锁的「写 PID 提交」（_commit_lock）：
#   - 救援者在 rescue 锁内重读锁文件、重新判定持有者，再比对「路径当前 inode
#     == 自己判定过的那个 fd 的 inode」，全部通过才 unlink（防 a/c）；
#   - 正常 acquirer flock 到手后，进 rescue 锁内校验 inode 并写 PID 提交
#     （防 b：救援者与提交互斥，锁文件内容对救援者要么是旧持有者、要么是已
#     提交的新 PID，不存在「flock 已易主但内容还是陈旧 PID」的可见中间态；
#     未提交就被抢走的 acquirer 会在校验时发现并重试，绝无双锁并立）。
# rescue 锁持有者都是活进程且锁内只做 /tmp 元数据操作与进程探测（不碰 USB
# fd），不会成为新的挂死点；救援者取 rescue 锁用 LOCK_NB，拿不到（另一救援者
# 正在处理）就退让重判。


def rescue_lock_path(path: Path) -> Path:
    """串行化救援/提交操作的旁路锁文件路径（<lock>.rescue）。"""
    return path.with_name(path.name + ".rescue")


def unlink_judged_lock(
    path: Path,
    judged_file: TextIO,
    judge_fn: Callable[[int | None], str] | None = None,
) -> bool:
    """串行化删锁（不重建）：rescue 锁内重读重判，确认持有者挂死/不存在且
    路径仍指向 judged_file 的 inode 才删除。外层看门狗用。

    返回是否删了；False = 持有者其实健康 / 路径已被换新 / rescue 锁被占，
    一律不动。与 _takeover_lock 各自独立取 rescue 锁，两者不得嵌套调用
    （flock 对同进程的两个 open file description 同样互斥，嵌套会自锁）。
    """
    judge = judge_lock_holder if judge_fn is None else judge_fn
    with rescue_lock_path(path).open("a+") as rescue_lock:
        try:
            fcntl.flock(rescue_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # 另一救援者正在处理，退让
        try:
            if os.stat(path).st_ino != os.fstat(judged_file.fileno()).st_ino:
                return False  # 只删自己判定过的那个 inode（防交错 a）
        except FileNotFoundError:
            return False
        judged_file.seek(0)
        pid = parse_lock_holder_pid(judged_file.read())
        if pid is None or judge(pid) not in ("hung", "missing"):
            return False  # rescue 锁内重判（防交错 c：同 inode 已换健康持有者）
        path.unlink()
        return True


def _takeover_lock(path: Path, judged_file: TextIO) -> tuple[str, TextIO | None]:
    """救援式拿锁：rescue 锁内完成「比对判定过的 inode → 重读重判 → unlink →
    重建 → flock → 写 PID」全程，其他救援者随后看到的必然是带本进程 PID 的
    健康新锁。

    返回 (verdict, new_lock)：verdict 为 judge_lock_holder 的四态，外加
    "busy"（rescue 锁被占）与 "stale"（路径已被换新 / 与并发 acquirer 撞车）；
    new_lock 仅在成功接管时非 None，其余情况交回外层按新现状重新判定
    （重判会看到健康持有者并拒绝，绝不误删活锁）。
    """
    with rescue_lock_path(path).open("a+") as rescue_lock:
        try:
            fcntl.flock(rescue_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "busy", None
        try:
            if os.stat(path).st_ino != os.fstat(judged_file.fileno()).st_ino:
                return "stale", None  # 只删自己判定过的那个 inode（防交错 a）
        except FileNotFoundError:
            return "stale", None
        judged_file.seek(0)
        verdict = judge_lock_holder(parse_lock_holder_pid(judged_file.read()))
        if verdict not in ("hung", "missing"):
            return verdict, None  # rescue 锁内重判（防交错 c）
        path.unlink()
        new_file = path.open("a+")
        try:
            fcntl.flock(new_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # 正常 acquirer 与本进程同时 open 了刚重建的文件且先 flock：让它赢。
            new_file.close()
            return "stale", None
        new_file.truncate(0)
        new_file.write(str(os.getpid()))
        new_file.flush()
        return verdict, new_file


def _commit_lock(path: Path, lock_file: TextIO) -> bool:
    """正常拿锁的提交：rescue 锁内校验 inode 未被换、写入本进程 PID。

    与救援者互斥（防交错 b）：救援者判定时看到的锁文件要么还没易主（陈旧
    PID，抢走也只会让未提交的本进程校验失败重试），要么已带本进程 PID
    （判 healthy 而放行）。返回 False = 脚下的文件已被救援者换掉，本把作废。
    此处 rescue 锁用阻塞取锁：救援者持锁时长有界（两次采样 ~1s + ps 超时上限），
    且救援者绝不阻塞等 rescue 锁，无死锁回路。
    """
    with rescue_lock_path(path).open("a+") as rescue_lock:
        fcntl.flock(rescue_lock, fcntl.LOCK_EX)
        try:
            if os.stat(path).st_ino != os.fstat(lock_file.fileno()).st_ino:
                return False
        except FileNotFoundError:
            return False
        lock_file.truncate(0)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        return True


def acquire_instance_lock(lock_path: Path | None = None, max_attempts: int = 2) -> TextIO:
    """进程唯一锁：防止两个桥实例争抢 USB claim 导致双双不可用。

    返回持有的文件对象（进程退出自动释放）；已有健康实例时报错。

    issue #124：持锁进程若确认 U 态挂死（judge_lock_holder 连续两次采样）
    或已不存在，flock 永不释放——此时走 _takeover_lock 删锁重建（flock 绑定
    inode，新文件即新锁），重试一次。竞态安全见「删锁的竞态防护」注释块。
    """
    path = LOCK_PATH if lock_path is None else lock_path
    for attempt in range(1, max_attempts + 1):
        lock_file = path.open("a+")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                lock_file.seek(0)
                holder_text = lock_file.read().strip()
                if attempt < max_attempts:
                    verdict, taken = _takeover_lock(path, lock_file)
                    if taken is not None:
                        logger.warning(
                            "实例锁曾被 %s 状态的进程占用 (pid=%s)：判定为拔插挂死残留，"
                            "已删锁重建接管（僵尸进程只引用旧设备实例，无双桥争抢）",
                            verdict, holder_text or "未知",
                        )
                        return taken
                    if verdict in ("busy", "stale"):
                        continue  # 别的救援者正在/已经处理：重开重判，绝不删别人的活锁
                else:
                    verdict = judge_lock_holder(parse_lock_holder_pid(holder_text))
                raise RuntimeError(
                    f"另一个 ec20_usb_pty 实例正在运行 (pid={holder_text or '未知'}, 状态={verdict})；"
                    "同一时刻只能有一个桥占用 EC20 USB 接口。"
                ) from None
            finally:
                lock_file.close()
        # flock 到手 ≠ 拿锁完成：还须在 rescue 锁内提交（校验 inode + 写 PID），
        # 防「flock 后被救援者按陈旧判定换掉脚下文件」的交错（详见 _commit_lock）。
        if _commit_lock(path, lock_file):
            return lock_file
        lock_file.close()  # 脚下文件已被救援者换新：重开重判
    raise RuntimeError("实例锁竞争异常（多个救援进程反复重建锁文件），请稍后重试。")


# ---- 桥内看门狗（issue #124）----
#
# 主循环各长驻/长等待环节定期喂狗；看门狗线程发现停摆超阈值即认定主线程
# 挂死（典型：os.close()/USB I/O 卡在内核态），先自杀交由 launchd 冷启。
# 如实说明局限：挂死若发生在**不可中断**内核调用上，os._exit 和 kill -9
# 一样可能无法终止进程（整个进程留置为 U 态僵尸）；此时看门狗的价值退化为
# 把「我可能挂死了」写进 stderr/日志，供外层守护（scripts/
# sim7600_bridge_watchdog.py）发现并按删锁+起新桥的剧本救援。

WATCHDOG_STALL_SECONDS = 30.0


class Heartbeat:
    """主循环喂狗时间戳（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def feed(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def last_feed(self) -> float:
        with self._lock:
            return self._last


def watchdog_stalled(last_feed: float, now: float, threshold: float) -> bool:
    """纯判定：距上次喂狗超过阈值即视为主循环停摆（离线可测，issue #124）。"""
    return (now - last_feed) > threshold


HEARTBEAT = Heartbeat()


def start_watchdog_thread(
    heartbeat: Heartbeat,
    threshold: float = WATCHDOG_STALL_SECONDS,
    poll_seconds: float = 2.0,
    _exit: Callable[[int], None] = os._exit,
) -> threading.Thread:
    """启动看门狗线程：主循环停摆超阈值 → 记日志并 os._exit(70) 自杀。

    自杀走 os._exit 而非 sys.exit：主线程已挂死，正常退出路径（finally/
    atexit）走不完；_exit 直接终止全进程。局限见模块顶部注释——U 态挂死时
    自杀可能无效，日志是留给外层看门狗的求救信号。
    """

    def _watch() -> None:
        while True:
            time.sleep(poll_seconds)
            last = heartbeat.last_feed()
            now = time.monotonic()
            if watchdog_stalled(last, now, threshold):
                message = (
                    f"看门狗：主循环已停摆 {now - last:.0f}s（阈值 {threshold:.0f}s），"
                    "疑似挂死在不可中断内核调用（如拔插瞬间的 os.close）；"
                    "尝试 os._exit 自杀交由 launchd 重启。若本进程此后仍存活"
                    "（U 态挂死自杀无效），需外层看门狗删锁+起新桥救援。"
                )
                logger.critical(message)
                print(message, file=sys.stderr, flush=True)
                for handler in logging.getLogger().handlers:
                    try:
                        handler.flush()
                    except Exception:  # noqa: BLE001
                        pass
                _exit(70)
                return  # 仅测试注入的 _exit 不终止进程时会走到

    thread = threading.Thread(target=_watch, name="ec20-bridge-watchdog", daemon=True)
    thread.start()
    return thread


def _sleep_feeding(seconds: float) -> None:
    """分段 sleep 并喂狗：恢复流程等长等待是合法停顿，不应触发看门狗。"""
    deadline = time.monotonic() + seconds
    while True:
        HEARTBEAT.feed()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _stop_wait_feeding(stop: threading.Event, seconds: float) -> None:
    """分段 stop.wait 并喂狗：退避等待最长 30s，整段不喂会误触发看门狗。"""
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        HEARTBEAT.feed()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop.wait(min(1.0, remaining))


@dataclass(frozen=True)
class UsbPort:
    interface: int
    bulk_in: int
    bulk_out: int
    max_packet: int


@dataclass
class BridgeHandle:
    dev: usb.core.Device
    port: UsbPort
    link: str
    master_fd: int
    slave_fd: int
    stop: threading.Event
    closed: bool = False
    # 因「模组持续不排空端点」而拆桥（区别于真拔线）：置位后主循环会先跑
    # USB 组合切换恢复再重连，见 usb_composition_recovery。
    degraded: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        try:
            usb.util.release_interface(self.dev, self.port.interface)
        except Exception:
            pass
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        path = Path(self.link)
        if path.is_symlink():
            path.unlink()


def find_device(vid: int = VID, pid: int = PID) -> usb.core.Device:
    try:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=libusb_backend())
    except usb.core.NoBackendError:
        # pyusb 是纯 Python 包，真正的 USB 访问依赖系统 libusb；
        # 干净的 Mac 上没有它，裸 traceback 会劝退第一次跑桥的用户。
        raise SystemExit(
            "libusb not found — pyusb needs the system libusb library.\n"
            "  Install it:  brew install libusb   (macOS)\n"
            "               sudo apt install libusb-1.0-0   (Debian/Ubuntu)"
        ) from None
    if dev is None:
        raise RuntimeError(f"未找到 USB 设备 ({vid:04x}:{pid:04x})")
    return dev


def discover_ports(dev: usb.core.Device) -> dict[int, UsbPort]:
    try:
        cfg = dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
        cfg = dev.get_active_configuration()
    ports: dict[int, UsbPort] = {}
    for intf in cfg:
        bulk_in = None
        bulk_out = None
        max_packet = 512
        for ep in intf:
            attrs = usb.util.endpoint_type(ep.bmAttributes)
            direction = usb.util.endpoint_direction(ep.bEndpointAddress)
            if attrs != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if direction == usb.util.ENDPOINT_IN:
                bulk_in = ep.bEndpointAddress
                max_packet = ep.wMaxPacketSize
            elif direction == usb.util.ENDPOINT_OUT:
                bulk_out = ep.bEndpointAddress
        if bulk_in is not None and bulk_out is not None:
            ports[intf.bInterfaceNumber] = UsbPort(
                interface=intf.bInterfaceNumber,
                bulk_in=bulk_in,
                bulk_out=bulk_out,
                max_packet=max_packet,
            )
    return ports


def read_response(dev: usb.core.Device, port: UsbPort, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        try:
            data = dev.read(port.bulk_in, port.max_packet, timeout=200)
        except usb.core.USBTimeoutError:
            continue
        if data:
            chunks.append(bytes(data))
            joined = b"".join(chunks)
            if b"\r\nOK\r\n" in joined or b"\r\nERROR\r\n" in joined:
                break
    return b"".join(chunks)


def probe_at(dev: usb.core.Device, port: UsbPort) -> bytes:
    try:
        usb.util.claim_interface(dev, port.interface)
    except usb.core.USBError as exc:
        raise RuntimeError(
            f"无法占用 USB interface {port.interface}: {exc}. "
            "请确认没有另一个 ec20_usb_pty.py 正在运行；如刚异常退出，重插 EC20 USB 后再试。"
        ) from exc
    try:
        while True:
            try:
                dev.read(port.bulk_in, port.max_packet, timeout=50)
            except Exception:
                break
        dev.write(port.bulk_out, b"AT\r", timeout=1000)
        return read_response(dev, port, 1.5)
    finally:
        usb.util.release_interface(dev, port.interface)


def at_on_interface(
    dev: usb.core.Device, port: UsbPort, cmd: str,
    wait: float = 1.5, rounds: int = 15,
) -> str:
    """在指定 bulk 接口上发一条 AT 并返回响应文本（恢复流程用，独立于桥）。"""
    usb.util.claim_interface(dev, port.interface)
    try:
        while True:  # 清残留 URC
            try:
                dev.read(port.bulk_in, port.max_packet, timeout=50)
            except usb.core.USBError:
                break
        dev.write(port.bulk_out, (cmd + "\r").encode("ascii"), timeout=1000)
        time.sleep(wait)
        data = b""
        for _ in range(rounds):
            try:
                data += bytes(dev.read(port.bulk_in, port.max_packet, timeout=250))
            except usb.core.USBError:
                break
        return data.decode("ascii", "ignore")
    finally:
        try:
            usb.util.release_interface(dev, port.interface)
        except Exception:  # noqa: BLE001
            pass


def find_at_port_dynamic(
    vid: int, timeout: float = 60.0,
) -> tuple[usb.core.Device | None, UsbPort | None]:
    """只按 VID 找设备并逐个 bulk 接口探 AT。

    切换 USB 组合后 **AT 口会换接口号**（真机：9001 组合在 interface 2，
    9011 组合跑到 interface 4/5），写死接口号会直接失联，故必须动态探测。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        HEARTBEAT.feed()  # 动态探口最长 60s，属合法长等待（issue #124）
        try:
            dev = usb.core.find(idVendor=vid, backend=libusb_backend())
        except usb.core.NoBackendError:
            return None, None
        if dev is None:
            time.sleep(1.0)
            continue
        try:
            ports = discover_ports(dev)
        except usb.core.USBError:
            time.sleep(1.0)
            continue
        for port in ports.values():
            HEARTBEAT.feed()  # 逐口探测一轮可达 20s+，口间也喂一次狗
            try:
                if "OK" in at_on_interface(dev, port, "AT", wait=0.4, rounds=8):
                    return dev, port
            except usb.core.USBError:
                continue
        usb.util.dispose_resources(dev)
        time.sleep(1.0)
    return None, None


def usb_composition_recovery(
    vid: int, target_pid: int, alt_pid: int,
    attempts: int = 3, settle: float = 20.0,
) -> bool:
    """PCM 子系统卡死的软件恢复：切到备用 USB 组合再切回，等效物理断电重插。

    真机实测（2026-08-12）：``AT+CRESET`` 与 ``AT+CFUN=0/1`` 都救不回卡死的
    PCM——射频/SIM 重启了，音频子系统依旧坏；**只有触发 USB 重新枚举有效**。
    组合切换与物理断电都能触发，故用它做无人值守的自动恢复。

    两个真机踩过的坑，这里都必须处理：
    1. 备用组合下 AT 口不在原接口号——用 ``find_at_port_dynamic`` 动态探；
    2. 切回指令若在重枚举/启动完成前下发会返回 ``ERROR`` 并**滞留在备用组合**
       ——故每次切换后等待 ``settle`` 秒，并**回读校验**，失败重试。
    """
    logger.warning(
        "检测到 PCM 子系统卡死，开始 USB 组合切换恢复（%04x → %04x → %04x）",
        target_pid, alt_pid, target_pid,
    )
    for attempt in range(1, attempts + 1):
        dev, port = find_at_port_dynamic(vid)
        if dev is None or port is None:
            logger.error("恢复第 %d 次：找不到 AT 口", attempt)
            _sleep_feeding(settle)
            continue
        current = at_on_interface(dev, port, "AT+CUSBPIDSWITCH?")
        if f"{target_pid:04X}" in current.upper() and attempt > 1:
            logger.info("已回到目标组合 %04x，恢复完成", target_pid)
            usb.util.dispose_resources(dev)
            return True
        # 在目标组合上：先切走；已在备用组合（上次没切回来）：直接切回。
        going_out = f"{target_pid:04X}" in current.upper()
        pid = alt_pid if going_out else target_pid
        logger.info("恢复第 %d 次：切换到 %04x（AT 口 interface %d）",
                    attempt, pid, port.interface)
        at_on_interface(dev, port, f"AT+CUSBPIDSWITCH={pid:04X},1,1", wait=2.0)
        usb.util.dispose_resources(dev)
        _sleep_feeding(settle)  # 等重枚举+固件启动，发太早会 ERROR 并卡在备用组合

        if not going_out:
            continue  # 本轮是切回，下一轮开头会回读校验

        dev, port = find_at_port_dynamic(vid)
        if dev is None or port is None:
            logger.error("恢复第 %d 次：切走后失联，重试", attempt)
            continue
        logger.info("恢复第 %d 次：切回 %04x（AT 口 interface %d）",
                    attempt, target_pid, port.interface)
        at_on_interface(dev, port, f"AT+CUSBPIDSWITCH={target_pid:04X},1,1", wait=2.0)
        usb.util.dispose_resources(dev)
        _sleep_feeding(settle)

        dev, port = find_at_port_dynamic(vid)
        if dev is None or port is None:
            logger.error("恢复第 %d 次：切回后失联，重试", attempt)
            continue
        verify = at_on_interface(dev, port, "AT+CUSBPIDSWITCH?")
        usb.util.dispose_resources(dev)
        if f"{target_pid:04X}" in verify.upper():
            logger.warning("USB 组合切换恢复成功，已回到 %04x", target_pid)
            return True
        logger.error("恢复第 %d 次：回读未确认（%r），重试", attempt, verify.strip())
    logger.error(
        "USB 组合切换恢复连续 %d 次失败——需人工物理断电重插", attempts
    )
    return False


def make_raw(fd: int) -> None:
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def link_pty(slave_name: str, link: str) -> None:
    path = Path(link)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to(slave_name)


def bridge_port(
    dev: usb.core.Device,
    port: UsbPort,
    link: str,
) -> BridgeHandle:
    master_fd, slave_fd = pty.openpty()
    stop = threading.Event()
    handle = BridgeHandle(dev, port, link, master_fd, slave_fd, stop)
    try:
        # Keep the slave side open so the master does not see EIO before a client opens it.
        make_raw(slave_fd)
        slave_name = os.ttyname(slave_fd)
        try:
            usb.util.claim_interface(dev, port.interface)
        except usb.core.USBError as exc:
            raise RuntimeError(
                f"无法占用 USB interface {port.interface}: {exc}. "
                "请确认没有另一个 ec20_usb_pty.py 正在运行；如刚异常退出，重插 EC20 USB 后再试。"
            ) from exc
        link_pty(slave_name, link)
    except Exception:
        handle.close()
        raise
    logger.info(
        "interface %d: %s -> %s (in=0x%02x, out=0x%02x)",
        port.interface, link, slave_name, port.bulk_in, port.bulk_out,
    )

    def usb_to_pty() -> None:
        while not stop.is_set():
            try:
                data = dev.read(port.bulk_in, port.max_packet, timeout=100)
            except usb.core.USBTimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                if not stop.is_set():
                    logger.error("interface %d USB read failed: %s", port.interface, exc)
                stop.set()
                return
            if data:
                try:
                    os.write(master_fd, bytes(data))
                except OSError as exc:
                    if not stop.is_set():
                        logger.error("interface %d PTY write failed: %s", port.interface, exc)
                    stop.set()
                    return

    def pty_to_usb() -> None:
        consecutive_timeouts = 0
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(master_fd, port.max_packet)
            except OSError as exc:
                if not stop.is_set():
                    logger.error("interface %d PTY read failed: %s", port.interface, exc)
                stop.set()
                return
            if data:
                try:
                    dev.write(port.bulk_out, data, timeout=1000)
                    consecutive_timeouts = 0
                except usb.core.USBError as exc:
                    if isinstance(exc, usb.core.USBTimeoutError) or exc.errno == errno.ETIMEDOUT:
                        # 瞬时写超时（模组侧忙/端点暂 NAK）：丢帧继续，不因单帧超时拆桥
                        # （否则连累同桥 AT 口，通话中掉线）。但持续超时=设备可能已掉线/
                        # 重启，需拆桥触发外层重连，否则会永远空转丢帧连不回来。
                        consecutive_timeouts += 1
                        if consecutive_timeouts <= PTY_WRITE_TIMEOUT_TOLERANCE:
                            if not stop.is_set():
                                logger.warning("interface %d USB 写超时，丢帧继续", port.interface)
                            continue
                        if not stop.is_set():
                            logger.error(
                                "interface %d 连续写超时 %d 次，判定掉线，触发重连",
                                port.interface, consecutive_timeouts,
                            )
                        # 设备还在总线上却持续不收数据 = PCM 子系统卡死，不是拔线。
                        # 标记后由主循环跑组合切换恢复（真机唯一有效的软件手段）。
                        handle.degraded = True
                        stop.set()
                        return
                    if not stop.is_set():
                        logger.error("interface %d USB write failed: %s", port.interface, exc)
                    stop.set()
                    return

    threading.Thread(target=usb_to_pty, name=f"ec20-usb-to-pty-{port.interface}", daemon=True).start()
    threading.Thread(target=pty_to_usb, name=f"ec20-pty-to-usb-{port.interface}", daemon=True).start()
    return handle


def parse_map(value: str) -> tuple[int, str]:
    try:
        iface_text, link = value.split(":", 1)
        iface = int(iface_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--map 格式应为 IFACE:LINK，例如 2:/tmp/ec20-at") from exc
    if not link:
        raise argparse.ArgumentTypeError("--map 的 LINK 不能为空")
    return iface, link


def wait_for_device(
    stop: threading.Event,
    poll_seconds: float = 2.0,
    vid: int = VID,
    pid: int = PID,
) -> usb.core.Device | None:
    """阻塞等待设备出现（模组重插/通话重枚举场景）；stop 置位时返回 None。"""
    announced = False
    while not stop.is_set():
        HEARTBEAT.feed()  # 等设备重插是合法长等待，不算主循环停摆（issue #124）
        try:
            return find_device(vid, pid)
        except RuntimeError:
            if not announced:
                logger.warning("未检测到 USB 设备 (%04x:%04x)，等待设备接入…", vid, pid)
                announced = True
            stop.wait(poll_seconds)
    return None


def run_bridges_once(
    dev: usb.core.Device,
    maps: list[tuple[int, str]],
    stop: threading.Event,
    reset_first: bool = False,
    recovery_request: Path | None = None,
) -> bool:
    """建立全部桥并阻塞运行，直到 stop 置位或任一桥断开（如设备被拔出）。

    返回是否因「模组持续不排空端点」而断开（True 时主循环会跑组合切换恢复）。

    reset_first=True 时先 dev.reset()：macOS 睡眠/重枚举后 bulk 端点常处于 stall，
    不复位则重连后每次 read 立即 [Errno 5] 死循环（见 docs/roadmap.md USB 排查）。
    """
    if reset_first:
        try:
            dev.reset()
            logger.info("已复位 USB 设备（清除 stall 端点）")
            time.sleep(1.0)  # 复位后设备重新枚举需片刻
        except Exception as exc:  # noqa: BLE001
            logger.warning("USB 复位失败（继续尝试桥接）: %s", exc)
    ports = discover_ports(dev)
    handles: list[BridgeHandle] = []
    try:
        for iface, link in maps:
            if iface not in ports:
                raise RuntimeError(f"接口 {iface} 不存在，可用接口: {sorted(ports)}")
            handles.append(bridge_port(dev, ports[iface], link))

        requested = False
        while not stop.is_set() and all(not handle.stop.is_set() for handle in handles):
            HEARTBEAT.feed()  # 桥运行中的监视循环，0.2s 一喂（issue #124）
            # app 侧检测到「CPCMREG 启用需重试」= 模组已劣化，通话结束后写此文件请求
            # 自愈。它覆盖「模组收数据但播放卡」这一形态——那种形态不产生写超时，
            # 光靠桥自己发现不了（真机 2026-08-12）。
            if recovery_request is not None and recovery_request.exists():
                logger.warning("收到 app 的自愈请求（%s），拆桥执行组合切换", recovery_request)
                requested = True
                break
            time.sleep(0.2)
        return requested or any(handle.degraded for handle in handles)
    finally:
        for handle in handles:
            handle.close()
        usb.util.dispose_resources(dev)


def main(
    default_vid: int = VID,
    default_pid: int = PID,
    default_maps: list[str] | None = None,
    prog: str | None = None,
    description: str = "EC20 USB vendor serial PTY bridge for macOS",
    reset_on_start: bool = False,
    recover_alt_pid: int | None = None,
    recovery_request_path: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--list", action="store_true", help="列出 USB bulk 接口后退出")
    parser.add_argument("--probe", action="store_true", help="对每个 bulk 接口发送 AT 探测后退出")
    parser.add_argument(
        "--vid", type=lambda s: int(s, 0), default=default_vid,
        help=f"USB Vendor ID（默认 0x{default_vid:04x}）",
    )
    parser.add_argument(
        "--pid", type=lambda s: int(s, 0), default=default_pid,
        help=f"USB Product ID（默认 0x{default_pid:04x}）",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        type=parse_map,
        metavar="IFACE:LINK",
        help="桥接接口到 symlink，例如 2:/tmp/ec20-at；可重复",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="桥断开（设备拔出）后直接退出，不等待重插自动重连",
    )
    parser.add_argument(
        "--reset-on-start", action="store_true",
        help="首次桥接前先 dev.reset() 清除 stall 端点（SIM7600 对 USB 故障敏感，推荐开）",
    )
    parser.add_argument(
        "--recover-alt-pid", type=lambda s: int(s, 16),
        help="PCM 卡死时自动切到该备用 USB 组合再切回以恢复（十六进制，如 9011）；"
             "不给则只重连不恢复",
    )
    parser.add_argument("--log-file", help="同时把日志写入指定文件")
    args = parser.parse_args()
    reset_on_start = reset_on_start or args.reset_on_start
    if args.recover_alt_pid is not None:
        recover_alt_pid = args.recover_alt_pid
    recovery_request = (
        Path(recovery_request_path) if recovery_request_path else None
    )
    if recovery_request is not None:
        recovery_request.unlink(missing_ok=True)  # 启动时清掉陈旧请求

    # 未显式给 --map 时用厂商默认映射（Quectel 默认为空，仍要求显式 --map）。
    if not args.map and default_maps:
        args.map = [parse_map(m) for m in default_maps]

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

    _lock = acquire_instance_lock()  # noqa: F841  # 持有到进程退出

    if args.list or args.probe:
        dev = find_device(args.vid, args.pid)
        ports = discover_ports(dev)
        if args.list:
            for port in ports.values():
                print(
                    f"interface {port.interface}: "
                    f"in=0x{port.bulk_in:02x} out=0x{port.bulk_out:02x} max={port.max_packet}"
                )
            return 0
        for port in ports.values():
            try:
                response = probe_at(dev, port).decode("ascii", "ignore").replace("\r\n", " | ")
            except RuntimeError as exc:
                print(f"interface {port.interface}: {exc}")
                continue
            print(f"interface {port.interface}: {response or '(no response)'}")
        return 0

    if not args.map:
        parser.error("需要 --list、--probe 或至少一个 --map IFACE:LINK")

    stop = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # 桥内看门狗（issue #124）：主循环停摆超阈值即自杀交由 launchd 冷启；
    # U 态挂死时自杀可能无效（见 start_watchdog_thread 注释），至少留日志
    # 供外层看门狗（scripts/sim7600_bridge_watchdog.py）发现。0 = 禁用。
    watchdog_threshold = float(
        os.environ.get("EC20_BRIDGE_WATCHDOG_SECONDS", str(WATCHDOG_STALL_SECONDS))
    )
    if watchdog_threshold > 0:
        HEARTBEAT.feed()
        start_watchdog_thread(HEARTBEAT, threshold=watchdog_threshold)

    # 连续快速失败计数：超阈值则 sys.exit 交 launchd 冷启（含全新 libusb 上下文），
    # 比原地自旋更可能复位；手动运行（无 launchd）时同样退出，避免抖动风暴。
    consecutive_fast_fail = 0
    fail_threshold = int(os.environ.get("EC20_BRIDGE_FAIL_THRESHOLD", "6"))
    backoff = 1.0
    first_run = True
    while not stop.is_set():
        dev = wait_for_device(stop, vid=args.vid, pid=args.pid)
        if dev is None:
            break
        started_at = time.monotonic()
        # 非首轮（重连）先复位设备清 stall 端点；reset_on_start 时首轮也复位
        # （SIM7600 对 USB 故障敏感，残留 stall 会让首次桥接直接 EIO）。
        reset_first = consecutive_fast_fail > 0 or (first_run and reset_on_start)
        first_run = False
        degraded = False
        try:
            degraded = run_bridges_once(
                dev, args.map, stop, reset_first=reset_first,
                recovery_request=recovery_request,
            )
        except (RuntimeError, usb.core.USBError) as exc:
            # USBError：设备僵死/枚举中时 set_configuration 等处会抛，
            # 不捕获会炸穿进程，launchd 每 10s 重启一次形成崩溃风暴；
            # 捕获后走快速失败退避，下一轮自动带 dev.reset() 清 stall。
            logger.error("桥接失败: %s", exc)
            if args.once:
                return 1
        if stop.is_set() or args.once:
            break

        # 设备仍在总线上却不收 PCM = 模组音频子系统卡死；软重启/CFUN 都救不回，
        # 只有 USB 重新枚举有效（WIL-109）。此时通话已随拆桥结束，恢复不会打断通话。
        if degraded and recover_alt_pid is not None:
            usb_composition_recovery(args.vid, args.pid, recover_alt_pid)
            if recovery_request is not None:
                recovery_request.unlink(missing_ok=True)  # 消费掉，避免反复触发
            consecutive_fast_fail = 0
            backoff = 1.0
            continue

        # 判定本轮是否"秒挂"：桥接维持不足 5s 视为快速失败，触发退避。
        ran_seconds = time.monotonic() - started_at
        if ran_seconds < 5.0:
            consecutive_fast_fail += 1
            if consecutive_fast_fail >= fail_threshold:
                logger.error(
                    "桥连续 %d 次快速失败，退出交由 launchd 冷启（或请重插 EC20 / 检查睡眠）",
                    consecutive_fast_fail,
                )
                return 3
            logger.warning(
                "桥断开（第 %d 次快速失败），%.0fs 后带 USB 复位重连…",
                consecutive_fast_fail, backoff,
            )
            _stop_wait_feeding(stop, backoff)  # 退避最长 30s，分段喂狗防误触发看门狗
            backoff = min(backoff * 2, 30.0)
        else:
            # 曾正常运行过一段时间，属偶发掉线：重置退避。
            consecutive_fast_fail = 0
            backoff = 1.0
            logger.warning("桥已断开（设备可能被拔出），等待重插后自动重连…")
            _stop_wait_feeding(stop, 1.0)

    logger.info("桥已退出，symlink 已清理")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
