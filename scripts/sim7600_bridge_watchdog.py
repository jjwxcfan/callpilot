"""SIM7600 USB 桥外层看门狗：物理拔插挂死自愈（issue #124 / WIL-139）。

背景
----
物理拔插 SIM7600 时桥进程（scripts/sim7600_usb_pty.py）可能挂死为不可中断
U 态。最恶劣形态（真机 2026-08-19）：主线程卡死在 os.close() 内核调用——拔出
瞬间正关闭已失效的设备 fd——kill -9 与再次拔插均无效，/tmp/sim7600-at 无法
重建，服务连不上模组。桥内看门狗（ec20_usb_pty.start_watchdog_thread）对
U 态挂死同样自杀无效，必须由本脚本这样的**外部**守护按真机验证过的救援剧本
处理：

1. **只对确认挂死的桥进程** kill -9 尝试（连续两次采样都 U/D 才算挂死，
   防止健康进程瞬时磁盘 I/O 被误判）——对 U 态无效但无害（SIGKILL 排队，
   进程一旦离开不可中断等待即生效，覆盖「卡在 bulk I/O、再次拔插可解」的
   形态）。健康桥进程绝不碰：「永久 U 僵尸（重启系统前不消失）+ 健康新桥
   + 设备拔走」是救援后的常态组合，健康桥可能正跑组合切换恢复（3×20s
   settle），无差别 SIGKILL 会把它反复腰斩、永不收敛；
2. 若锁文件持有者确认挂死/不存在 → 串行化删锁（ec20_usb_pty.
   unlink_judged_lock：rescue 锁内重读重判 + 只删自己判定过的那个 inode，
   防止把并发新桥刚建好的活锁删掉；flock 绑定 inode，删文件后新桥拿新锁；
   僵尸进程只引用旧设备实例，不会再碰新插回的设备，无双桥争抢）；
3. 无健康桥进程残留时才拉起新桥（有健康桥在场说明它正自行等待/重连，
   重复拉桥只会被实例锁拒绝，徒增噪音）。新桥的 acquire_instance_lock
   自带同款僵尸检测与竞态防护，双保险。

判定条件（decide_action，纯函数、离线可测）
------------------------------------------
- PTY symlink 在位 → 一切正常，什么都不做；
- PTY 缺失未超阈值 → 等（桥启动中/设备刚拔，给它时间）；
- 超阈值且无桥进程 → 直接拉起新桥（launchd 崩溃风暴、手工误杀等兜底）；
- 超阈值且有桥进程且至少一个确认挂死 → 走救援剧本（只处置挂死的那些）；
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
    judge_lock_holder,
    unlink_judged_lock,
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
    judge_fn: Callable[[int | None], str] | None = None,
) -> bool:
    """锁文件持有者确认挂死/不存在则删锁；返回是否删了。

    真正的重判与删除在 ec20_usb_pty.unlink_judged_lock 里、rescue 锁串行化
    之下完成（只删本函数判定过的那个 inode），防止把并发新桥刚建好的活锁
    删掉。内容解析不出 PID 时保守不动（新桥的 acquire_instance_lock 同样
    不会抢 unknown 状态的锁，两边口径一致）。
    """
    try:
        judged = lock_path.open("r")
    except FileNotFoundError:
        return False
    with judged:
        removed = unlink_judged_lock(lock_path, judged, judge_fn=judge_fn)
    if removed:
        logger.warning("锁文件持有者确认挂死/不存在，已删除 %s 让新桥拿新锁", lock_path)
    return removed


def rescue(
    hung_pids: Sequence[int],
    lock_path: Path,
    kill_fn: Callable[[int, int], None] = os.kill,
    judge_fn: Callable[[int | None], str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """救援剧本第 1、2 步：只对确认挂死的 pid kill -9 + 持锁者确认挂死则删锁。

    评审必修：绝不无差别杀所有匹配进程——「永久 U 僵尸 + 健康新桥」是救援后
    的常态组合，健康桥（可能正跑 3×20s 的组合切换恢复）被误杀会永不收敛，
    调用方只把确认挂死的 pid 传进来。kill -9 对 U 态挂死无效但无害（SIGKILL
    排队，离开不可中断等待即生效）；删锁后僵尸进程留置无害——它卡死在旧设备
    实例上，不会再碰新设备。
    """
    judge = judge_lock_holder if judge_fn is None else judge_fn
    for pid in hung_pids:
        try:
            kill_fn(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    sleep_fn(1.0)  # 给可中断的进程一点退出时间
    survivors = [pid for pid in hung_pids if judge(pid) == "hung"]
    if survivors:
        logger.warning("进程 %s kill -9 后仍 U 态挂死（预期内，需重启系统才消失），走删锁路径", survivors)
    clear_stale_lock(lock_path, judge_fn=judge_fn)


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
    # 两段式判定（连续两次采样都 U/D 才算挂死）：健康进程瞬时磁盘 I/O 也会
    # 短暂进入不可中断态，单次采样会误判误杀。
    hung_flags = [judge_lock_holder(pid) == "hung" for pid in pids]
    action = decide_action(pids, hung_flags, pty_ok, missing_seconds, threshold)

    if action == "rescue":
        hung_pids = [pid for pid, hung in zip(pids, hung_flags) if hung]
        healthy_pids = [pid for pid, hung in zip(pids, hung_flags) if not hung]
        logger.error(
            "桥进程挂死 %s（%s 缺失 %.0fs），执行救援（只 kill -9 挂死进程 + 串行化删锁）",
            hung_pids, pty_path, missing_seconds,
        )
        rescue(hung_pids, lock_path)
        if healthy_pids:
            # 评审必修：健康桥绝不误杀、也不重复拉桥——它正自行等待/重连
            # （可能在跑组合切换恢复），拉新桥只会被实例锁拒绝，徒增噪音。
            logger.warning("健康桥进程 %s 仍在（自行等待/重连中），僵尸已处置，不重复拉桥", healthy_pids)
        else:
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
