"""拔插挂死自愈（issue #124 / WIL-139）的离线单测。

覆盖三块可离线测试的逻辑：
1. 实例锁持有者状态判定 + 「删锁重建」路径（ec20_usb_pty.acquire_instance_lock）；
2. 桥内看门狗的停摆判定与自杀触发（ec20_usb_pty.start_watchdog_thread）；
3. 外层看门狗的判定与救援编排（sim7600_bridge_watchdog）。

进程级行为离线测不了，真机验收待做（issue #124 验收标准：任意时机物理拔插
≥3 次，桥 60s 内自动恢复 PTY、服务自动重连）：
- kill -9 对 U 态进程排队、离开不可中断等待后生效；
- flock 绑定 inode，删锁文件后新桥拿新锁、僵尸进程留置无害；
- os._exit 在主线程 U 态挂死时可能无法终止进程。
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

pytest.importorskip("fcntl", reason="USB PTY 桥与 flock 实例锁是 POSIX-only")

import fcntl  # noqa: E402

from scripts import ec20_usb_pty  # noqa: E402
from scripts import sim7600_bridge_watchdog as watchdog  # noqa: E402

# ---- 锁持有者状态判定（纯函数）----


def test_parse_lock_holder_pid():
    assert ec20_usb_pty.parse_lock_holder_pid("1234") == 1234
    assert ec20_usb_pty.parse_lock_holder_pid("  567\n") == 567
    assert ec20_usb_pty.parse_lock_holder_pid("") is None
    assert ec20_usb_pty.parse_lock_holder_pid("abc") is None
    assert ec20_usb_pty.parse_lock_holder_pid("-1") is None
    assert ec20_usb_pty.parse_lock_holder_pid("0") is None


def test_is_hung_stat():
    assert ec20_usb_pty.is_hung_stat("U") is True          # macOS 不可中断等待
    assert ec20_usb_pty.is_hung_stat("U+") is True
    assert ec20_usb_pty.is_hung_stat("UE") is True
    assert ec20_usb_pty.is_hung_stat("D") is True          # Linux 等价态
    assert ec20_usb_pty.is_hung_stat("Ss") is False
    assert ec20_usb_pty.is_hung_stat("R+") is False
    assert ec20_usb_pty.is_hung_stat("Z") is False         # zombie 已死，flock 已释放
    assert ec20_usb_pty.is_hung_stat("") is False


def test_classify_lock_holder():
    classify = ec20_usb_pty.classify_lock_holder
    assert classify(None, None) == "unknown"       # 锁文件无 PID：保守不抢
    assert classify(123, None) == "missing"        # 进程不存在但 flock 仍被持有：异常态，可抢
    assert classify(123, "U+") == "hung"           # U 态挂死：按剧本抢
    assert classify(123, "Ss") == "healthy"        # 健康实例：绝不抢


# ---- acquire_instance_lock 删锁重建路径 ----
#
# flock 锁绑定 open file description：同进程内两次独立 open 同一文件，
# 第二次 LOCK_EX|LOCK_NB 会冲突——借此在单测里模拟「另一实例持锁」。


def _hold_lock(lock_path, pid_text: str):
    holder = lock_path.open("a+")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    holder.truncate(0)
    holder.write(pid_text)
    holder.flush()
    return holder


def test_acquire_lock_takes_over_hung_holder(tmp_path, monkeypatch):
    """持锁进程 U 态挂死 → 删锁重建成功，新锁在新 inode 上。"""
    lock_path = tmp_path / "bridge.lock"
    zombie = _hold_lock(lock_path, "54321")
    old_ino = os.fstat(zombie.fileno()).st_ino
    monkeypatch.setattr(ec20_usb_pty, "process_stat", lambda pid: "U+")

    lock = ec20_usb_pty.acquire_instance_lock(lock_path)

    assert lock_path.read_text() == str(os.getpid())
    # flock 绑定 inode：新锁必须在新文件上，僵尸的旧锁留在旧 inode 上无害
    assert os.stat(lock_path).st_ino != old_ino
    lock.close()  # type: ignore[attr-defined]
    zombie.close()


def test_acquire_lock_takes_over_missing_holder(tmp_path, monkeypatch):
    """进程不存在但 flock 仍被持有（异常态）→ 同样走删锁重建。"""
    lock_path = tmp_path / "bridge.lock"
    zombie = _hold_lock(lock_path, "54321")
    monkeypatch.setattr(ec20_usb_pty, "process_stat", lambda pid: None)

    lock = ec20_usb_pty.acquire_instance_lock(lock_path)

    assert lock_path.read_text() == str(os.getpid())
    lock.close()  # type: ignore[attr-defined]
    zombie.close()


def test_acquire_lock_refuses_healthy_holder(tmp_path, monkeypatch):
    """健康持锁进程：维持原有拒绝行为，绝不抢锁。"""
    lock_path = tmp_path / "bridge.lock"
    holder = _hold_lock(lock_path, "54321")
    monkeypatch.setattr(ec20_usb_pty, "process_stat", lambda pid: "Ss")

    with pytest.raises(RuntimeError, match="正在运行"):
        ec20_usb_pty.acquire_instance_lock(lock_path)
    assert lock_path.exists()  # 锁文件不被动
    holder.close()


def test_acquire_lock_refuses_unparseable_holder(tmp_path):
    """锁文件里没有可解析 PID：信息不足，保守拒绝（不误删他人锁）。"""
    lock_path = tmp_path / "bridge.lock"
    holder = _hold_lock(lock_path, "")

    with pytest.raises(RuntimeError, match="正在运行"):
        ec20_usb_pty.acquire_instance_lock(lock_path)
    holder.close()


def test_acquire_lock_normal_path_writes_own_pid(tmp_path):
    lock_path = tmp_path / "bridge.lock"
    lock = ec20_usb_pty.acquire_instance_lock(lock_path)
    assert lock_path.read_text() == str(os.getpid())
    lock.close()  # type: ignore[attr-defined]


# ---- 桥内看门狗 ----


def test_watchdog_stalled_pure():
    assert ec20_usb_pty.watchdog_stalled(100.0, 131.0, 30.0) is True
    assert ec20_usb_pty.watchdog_stalled(100.0, 129.0, 30.0) is False
    assert ec20_usb_pty.watchdog_stalled(100.0, 100.0, 30.0) is False


def test_heartbeat_feed_updates_timestamp():
    hb = ec20_usb_pty.Heartbeat()
    before = hb.last_feed()
    time.sleep(0.01)
    hb.feed()
    assert hb.last_feed() > before


def test_watchdog_thread_fires_exit_on_stall():
    """主循环停摆超阈值 → 看门狗调用注入的 _exit(70)。

    真机上 _exit 是 os._exit；主线程 U 态挂死时自杀可能无效（进程留置为
    僵尸），此时看门狗至少已把求救信息写进日志——该行为离线测不了。
    """
    hb = ec20_usb_pty.Heartbeat()
    fired = threading.Event()
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        fired.set()

    ec20_usb_pty.start_watchdog_thread(hb, threshold=0.05, poll_seconds=0.02, _exit=fake_exit)

    assert fired.wait(2.0), "停摆后看门狗未在 2s 内触发自杀"
    assert codes[0] == 70


def test_watchdog_thread_quiet_while_fed():
    hb = ec20_usb_pty.Heartbeat()
    fired = threading.Event()
    ec20_usb_pty.start_watchdog_thread(
        hb, threshold=10.0, poll_seconds=0.02, _exit=lambda code: fired.set(),
    )
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        hb.feed()
        time.sleep(0.01)
    assert not fired.is_set()


# ---- 外层看门狗：判定（纯函数）----


def test_decide_action_pty_present_is_none():
    assert watchdog.decide_action([111], [True], pty_ok=True, missing_seconds=0.0, threshold=20.0) == "none"


def test_decide_action_within_threshold_waits():
    """PTY 缺失未超阈值：桥启动中/设备刚拔，给它时间。"""
    assert watchdog.decide_action([], [], pty_ok=False, missing_seconds=5.0, threshold=20.0) == "none"


def test_decide_action_no_process_starts_bridge():
    assert watchdog.decide_action([], [], pty_ok=False, missing_seconds=25.0, threshold=20.0) == "start"


def test_decide_action_hung_process_rescues():
    assert watchdog.decide_action(
        [111, 222], [False, True], pty_ok=False, missing_seconds=25.0, threshold=20.0,
    ) == "rescue"


def test_decide_action_healthy_process_never_killed():
    """健康桥等设备重插时 PTY 同样缺失（S 态）：绝不误杀。"""
    assert watchdog.decide_action(
        [111], [False], pty_ok=False, missing_seconds=999.0, threshold=20.0,
    ) == "none"


# ---- 外层看门狗：救援编排 ----


def test_clear_stale_lock_removes_when_holder_hung(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")
    assert watchdog.clear_stale_lock(lock, stat_fn=lambda pid: "U") is True
    assert not lock.exists()


def test_clear_stale_lock_removes_when_holder_missing(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")
    assert watchdog.clear_stale_lock(lock, stat_fn=lambda pid: None) is True
    assert not lock.exists()


def test_clear_stale_lock_keeps_healthy_holder(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")
    assert watchdog.clear_stale_lock(lock, stat_fn=lambda pid: "Ss") is False
    assert lock.exists()


def test_clear_stale_lock_conservative_on_garbage(tmp_path):
    """内容解析不出 PID：保守不动，与 acquire_instance_lock 的 unknown 口径一致。"""
    lock = tmp_path / "l.lock"
    lock.write_text("not-a-pid")
    assert watchdog.clear_stale_lock(lock, stat_fn=lambda pid: None) is False
    assert lock.exists()


def test_rescue_kills_all_then_clears_lock_of_hung_holder(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")
    killed: list[tuple[int, int]] = []
    stats = {777: "U+", 888: None}  # 777 僵尸留置；888 已被 kill -9 杀掉

    watchdog.rescue(
        [777, 888], lock,
        kill_fn=lambda pid, sig: killed.append((pid, sig)),
        stat_fn=lambda pid: stats.get(pid),
        sleep_fn=lambda s: None,
    )

    assert killed == [(777, signal.SIGKILL), (888, signal.SIGKILL)]
    assert not lock.exists()  # 持锁者 U 态 → 删锁让新桥拿新锁


def test_rescue_tolerates_already_dead_process(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")

    def kill_fn(pid: int, sig: int) -> None:
        raise ProcessLookupError

    watchdog.rescue([777], lock, kill_fn=kill_fn, stat_fn=lambda pid: None, sleep_fn=lambda s: None)
    assert not lock.exists()  # 持锁者已不存在 → 同样删锁


def test_pty_present_dangling_symlink_counts_missing(tmp_path):
    link = tmp_path / "sim7600-at"
    link.symlink_to(tmp_path / "gone")
    assert watchdog.pty_present(link) is False


def test_run_once_starts_bridge_when_no_process(tmp_path, monkeypatch):
    started: list[list[str]] = []
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [])
    monkeypatch.setattr(
        watchdog, "start_bridge", lambda cmd, log: started.append(list(cmd)) or 4242,
    )

    action, missing_since = watchdog.run_once(
        tmp_path / "sim7600-at",  # 不存在 = PTY 缺失
        "sim7600_usb_pty", tmp_path / "l.lock", ["bridge-cmd"], None,
        missing_since=0.0, threshold=20.0, now=100.0,
    )

    assert action == "start"
    assert started == [["bridge-cmd"]]
    assert missing_since is not None  # 重置计时，给新桥启动时间


def test_run_once_leaves_healthy_waiting_bridge_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "process_stat", lambda pid: "Ss")
    monkeypatch.setattr(
        watchdog, "start_bridge",
        lambda cmd, log: pytest.fail("健康桥等待设备时不得拉起新桥"),
    )

    action, _ = watchdog.run_once(
        tmp_path / "sim7600-at", "sim7600_usb_pty", tmp_path / "l.lock",
        ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
    )
    assert action == "none"


def test_run_once_rescues_hung_bridge(tmp_path, monkeypatch):
    lock = tmp_path / "l.lock"
    lock.write_text("111")
    started: list[list[str]] = []
    rescued: list[list[int]] = []
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "process_stat", lambda pid: "U+")
    monkeypatch.setattr(watchdog, "rescue", lambda pids, lp, **kw: rescued.append(list(pids)))
    monkeypatch.setattr(
        watchdog, "start_bridge", lambda cmd, log: started.append(list(cmd)) or 4242,
    )

    action, missing_since = watchdog.run_once(
        tmp_path / "sim7600-at", "sim7600_usb_pty", lock,
        ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
    )

    assert action == "rescue"
    assert rescued == [[111]]
    assert started == [["bridge-cmd"]]
    assert missing_since is not None


def test_run_once_all_clear_resets_timer(tmp_path, monkeypatch):
    pty = tmp_path / "sim7600-at"
    pty.write_text("")  # PTY 在位
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "process_stat", lambda pid: "Ss")

    action, missing_since = watchdog.run_once(
        pty, "sim7600_usb_pty", tmp_path / "l.lock", ["bridge-cmd"], None,
        missing_since=50.0, threshold=20.0, now=100.0,
    )
    assert action == "none"
    assert missing_since is None
