"""SIM7600 USB 桥外层看门狗：物理拔插挂死自愈（issue #124 / WIL-139）。

背景
----
物理拔插 SIM7600 时桥进程（scripts/sim7600_usb_pty.py）可能挂死为不可中断
U 态。最恶劣形态（真机 2026-08-19）：主线程卡死在 os.close() 内核调用——拔出
瞬间正关闭已失效的设备 fd——kill -9 与再次拔插均无效，/tmp/sim7600-at 无法
重建，服务连不上模组。桥内看门狗（ec20_usb_pty.start_watchdog_thread）对
U 态挂死同样自杀无效，必须由本脚本这样的**外部**守护按真机验证过的救援剧本
处理：

1. 对桥进程 kill -9 尝试——对 U 态无效但无害（SIGKILL 排队，进程一旦离开
   不可中断等待即生效，覆盖「卡在 bulk I/O、再次拔插可解」的形态）；
2. 若锁文件持有者确认 U 态挂死（或已不存在）→ 删除锁文件
   （flock 绑定 inode，删文件后新桥拿新锁；僵尸进程只引用旧设备实例，
   不会再碰新插回的设备，无双桥争抢）；
3. 拉起新桥（新桥的 acquire_instance_lock 自带同款僵尸检测，双保险）。

判定条件（decide_action，纯函数、离线可测）
------------------------------------------
- PTY symlink 在位 → 一切正常，什么都不做；
- PTY 缺失未超阈值 → 等（桥启动中/设备刚拔，给它时间）；
- 超阈值且无桥进程 → 直接拉起新桥（launchd 崩溃风暴、手工误杀等兜底）；
- 超阈值且有桥进程且至少一个 U 态挂死 → 走救援剧本；
- 超阈值且桥进程都健康 → 不动：健康桥等待设备重插时 PTY 同样缺失（S 态），
  误杀会打断桥自身的等待-重连循环。桥进程健康但停摆的形态由桥内看门狗
  自杀解决，不归本脚本管。

幂等：健康桥+PTY 在位时反复运行无副作用；救援后重置计时给新桥启动时间，
不会风暴式重复拉桥。本脚本自身无状态，崩溃重启无副作用，适合 launchd
KeepAlive 托管。

用法
----
    .venv/bin/python scripts/sim7600_bridge_watchdog.py             # 常驻轮询（推荐 launchd 托管）
    .venv/bin/python scripts/sim7600_bridge_watchdog.py --once      # 单轮检查后退出（cron/调试）
    .venv/bin/python scripts/sim7600_bridge_watchdog.py \\
        --pty /tmp/sim7600-at --threshold 20 --interval 5 \\
        --bridge-cmd "/path/to/python /path/to/sim7600_usb_pty.py"

launchd 托管示例（KeepAlive 常驻，与 com.agentcall.bridge 并存）：
    <key>ProgramArguments</key>
    <array>
      <string>/path/to/.venv/bin/python</string>
      <string>/path/to/scripts/sim7600_bridge_watchdog.py</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>

真机验收待做（issue #124 验收标准）
----------------------------------
任意时机物理拔插 ≥3 次，桥须在 60s 内自动恢复 PTY、服务自动重连，无需人工
介入。离线单测（tests/unit/test_bridge_watchdog.py）只覆盖判定与救援步骤的
编排逻辑；进程级行为（kill -9 对 U 态排队生效、flock 换 inode 后新桥拿锁）
需真机拔插验证。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

# 支持直接 `python scripts/sim7600_bridge_watchdog.py`（把仓库根放进 sys.path）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.ec20_usb_pty import (  # noqa: E402  # 需先补 sys.path
    LOCK_PATH,
    is_hung_stat,
    parse_lock_holder_pid,
    process_stat,
)

logger = logging.getLogger("sim7600_bridge_watchdog")

DEFAULT_PTY = "/tmp/sim7600-at"
# 桥进程匹配模式：pgrep -f 全命令行匹配。打包版（CallPilot.app --bridge）如需
# 托管请用 --pattern 覆盖。
DEFAULT_PATTERN = "sim7600_usb_pty"
DEFAULT_INTERVAL = 5.0
# PTY 缺失多久判异常：桥启动+设备枚举正常远快于此；阈值+救援+新桥启动的总
# 时长须落在 issue #124 的 60s 验收窗口内。
DEFAULT_THRESHOLD = 20.0


def decide_action(
    pids: Sequence[int],
    hung_flags: Sequence[bool],
    pty_ok: bool,
    missing_seconds: float,
    threshold: float,
) -> str:
    """纯判定：本轮该做什么。返回 "none" | "start" | "rescue"。

    详见模块 docstring「判定条件」。hung_flags 与 pids 一一对应。
    """
    if pty_ok:
        return "none"
    if missing_seconds <= threshold:
        return "none"
    if not pids:
        return "start"
    if any(hung_flags):
        return "rescue"
    return "none"  # 桥进程都健康：等设备重插中，绝不误杀


def clear_stale_lock(
    lock_path: Path,
    stat_fn: Callable[[int], str | None] = process_stat,
) -> bool:
    """锁文件持有者已挂死/不存在则删锁；返回是否删了。

    内容解析不出 PID 时保守不动（新桥的 acquire_instance_lock 同样不会抢
    unknown 状态的锁，两边口径一致）。
    """
    if not lock_path.exists():
        return False
    pid = parse_lock_holder_pid(lock_path.read_text(errors="ignore"))
    if pid is None:
        return False
    stat = stat_fn(pid)
    if stat is None or is_hung_stat(stat):
        logger.warning(
            "锁文件持有者 pid=%s 状态=%s（挂死/不存在），删除 %s 让新桥拿新锁",
            pid, stat or "不存在", lock_path,
        )
        lock_path.unlink(missing_ok=True)
        return True
    return False


def rescue(
    pids: Sequence[int],
    lock_path: Path,
    kill_fn: Callable[[int, int], None] = os.kill,
    stat_fn: Callable[[int], str | None] = process_stat,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """救援剧本第 1、2 步：kill -9 尝试 + 持锁进程确认挂死则删锁。

    kill -9 对 U 态挂死无效但无害（SIGKILL 排队，离开不可中断等待即生效）；
    删锁后僵尸进程留置无害——它卡死在旧设备实例上，不会再碰新设备。
    """
    for pid in pids:
        try:
            kill_fn(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    sleep_fn(1.0)  # 给可中断的进程一点退出时间
    survivors = {pid: stat_fn(pid) for pid in pids}
    hung = [pid for pid, stat in survivors.items() if stat is not None and is_hung_stat(stat)]
    if hung:
        logger.warning("进程 %s kill -9 后仍 U 态挂死（预期内，需重启系统才消失），走删锁路径", hung)
    clear_stale_lock(lock_path, stat_fn=stat_fn)


def find_bridge_pids(pattern: str) -> list[int]:
    """pgrep -f 找桥进程（排除本看门狗自身——其命令行也含 pattern）。"""
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(tok) for tok in out.stdout.split() if tok.isdigit() and int(tok) != os.getpid()]


def pty_present(path: Path) -> bool:
    """symlink 在且指向存在的 slave 才算在位（dangling symlink 视为缺失）。"""
    return path.exists()


def start_bridge(cmd: Sequence[str], log_path: Path | None) -> int:
    """拉起新桥（独立会话，看门狗退出不连带杀桥）；返回新桥 PID。"""
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stdout = log_path.open("ab")
    else:
        stdout = None
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        if stdout is not None:
            stdout.close()  # 子进程已继承 fd，父进程侧关闭即可
    logger.warning("已拉起新桥 pid=%d: %s", proc.pid, " ".join(cmd))
    return proc.pid


def run_once(
    pty_path: Path,
    pattern: str,
    lock_path: Path,
    bridge_cmd: Sequence[str],
    bridge_log: Path | None,
    missing_since: float | None,
    threshold: float,
    now: float | None = None,
) -> tuple[str, float | None]:
    """单轮检查+处置；返回 (本轮动作, 新的 missing_since) 供主循环持有状态。"""
    now = time.monotonic() if now is None else now
    pty_ok = pty_present(pty_path)
    if pty_ok:
        missing_since = None
    elif missing_since is None:
        missing_since = now
    missing_seconds = 0.0 if missing_since is None else now - missing_since

    pids = find_bridge_pids(pattern)
    hung_flags = [(stat := process_stat(pid)) is not None and is_hung_stat(stat) for pid in pids]
    action = decide_action(pids, hung_flags, pty_ok, missing_seconds, threshold)

    if action == "rescue":
        logger.error(
            "桥进程 %s 存在但 %s 缺失 %.0fs 且检测到 U 态挂死，执行救援（kill -9 + 删锁 + 起新桥）",
            pids, pty_path, missing_seconds,
        )
        rescue(pids, lock_path)
        start_bridge(bridge_cmd, bridge_log)
        missing_since = time.monotonic()  # 重置计时，给新桥启动时间
    elif action == "start":
        logger.error("无桥进程且 %s 缺失 %.0fs，拉起新桥", pty_path, missing_seconds)
        clear_stale_lock(lock_path)
        start_bridge(bridge_cmd, bridge_log)
        missing_since = time.monotonic()
    return action, missing_since


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sim7600_bridge_watchdog",
        description="SIM7600 USB 桥外层看门狗：拔插挂死自愈（issue #124）",
    )
    parser.add_argument("--pty", default=DEFAULT_PTY, help=f"桥的 PTY symlink 路径（默认 {DEFAULT_PTY}）")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN,
                        help=f"pgrep -f 匹配桥进程的模式（默认 {DEFAULT_PATTERN}）")
    parser.add_argument("--lock", default=str(LOCK_PATH),
                        help=f"桥实例锁文件路径（默认 {LOCK_PATH}）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"PTY 缺失判异常阈值秒数（默认 {DEFAULT_THRESHOLD:.0f}）")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"轮询间隔秒数（默认 {DEFAULT_INTERVAL:.0f}）")
    parser.add_argument("--bridge-cmd", default="",
                        help="拉桥命令（空格分隔；默认：当前解释器 + 本仓库 sim7600_usb_pty.py）")
    parser.add_argument("--bridge-log", default="", help="新桥 stdout/stderr 追加写入的日志文件")
    parser.add_argument("--once", action="store_true", help="只跑一轮检查后退出（cron/调试）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bridge_cmd = (
        args.bridge_cmd.split()
        if args.bridge_cmd
        else [sys.executable, str(_REPO_ROOT / "scripts" / "sim7600_usb_pty.py")]
    )
    bridge_log = Path(args.bridge_log) if args.bridge_log else None
    pty_path = Path(args.pty)
    lock_path = Path(args.lock)

    logger.info(
        "看门狗启动：pty=%s pattern=%s threshold=%.0fs interval=%.0fs",
        pty_path, args.pattern, args.threshold, args.interval,
    )
    missing_since: float | None = None
    while True:
        _, missing_since = run_once(
            pty_path, args.pattern, lock_path, bridge_cmd, bridge_log,
            missing_since, args.threshold,
        )
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
