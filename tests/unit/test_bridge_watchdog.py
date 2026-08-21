"""拔插挂死自愈（issue #124 / WIL-139）的离线单测。

覆盖四块可离线测试的逻辑：
1. 实例锁持有者状态判定（含两段式挂死确认、ps 失败与进程不存在的区分）；
2. 「删锁重建」的竞态防护（rescue 锁串行化 + 只删判定过的 inode + 锁内重判
   + 正常拿锁的提交校验）——评审实证变异 M1（去掉 inode 校验）必须被测试打死；
3. 桥内看门狗的停摆判定与自杀触发；
4. 外层看门狗的判定与救援编排（只杀确认挂死的 pid，健康桥绝不误杀）。

进程级行为离线测不了，真机验收待做（issue #124 验收标准：任意时机物理拔插
≥3 次，桥 60s 内自动恢复 PTY、服务自动重连）：
- kill -9 对 U 态进程排队、离开不可中断等待后生效；
- flock 绑定 inode，删锁文件后新桥拿新锁、僵尸进程留置无害；
- os._exit 在主线程 U 态挂死时可能无法终止进程。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="USB PTY 桥与 flock 实例锁是 POSIX-only")

import fcntl  # noqa: E402

# scripts 包从仓库根导入；不依赖「先收集到的其他测试模块恰好补过 sys.path」，
# 单独跑本文件也能过。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
    assert classify(None, True, "U") == "unknown"     # 锁文件无 PID：保守不抢
    assert classify(123, False, None) == "missing"    # kill(pid,0) 确认不存在：可抢
    assert classify(123, True, "U+") == "hung"        # U 态挂死：候选
    assert classify(123, True, "Ss") == "healthy"     # 健康实例：绝不抢
    # 顺手修 2（评审）：ps 失败 ≠ 进程不存在——进程明明在（或探测本身失败）
    # 时一律 unknown，绝不判 missing 去抢健康持有者的锁。
    assert classify(123, True, None) == "unknown"
    assert classify(123, None, None) == "unknown"
    assert classify(123, None, "U") == "unknown"


def test_process_alive_true_for_self_false_for_reaped_child():
    assert ec20_usb_pty.process_alive(os.getpid()) is True
    child = subprocess.Popen(["/usr/bin/true"])
    child.wait()  # 已回收：该 PID（极大概率）不再存在
    assert ec20_usb_pty.process_alive(child.pid) is False


def test_judge_lock_holder_requires_two_hung_samples():
    """顺手修 1（评审）：健康进程瞬时 U/D（普通磁盘 I/O）不得判挂死，
    连续两次采样（间隔 ~1s）都 hung 才算。"""
    slept: list[float] = []

    def judge(stats):
        it = iter(stats)
        return ec20_usb_pty.judge_lock_holder(
            123,
            stat_fn=lambda pid: next(it),
            alive_fn=lambda pid: True,
            sleep_fn=slept.append,
        )

    assert judge(["U", "Ss"]) == "healthy"   # 瞬态 U：第二次已恢复
    assert judge(["U", "U+"]) == "hung"      # 持续挂死：确认
    assert slept == [1.0, 1.0]               # 只有第一次采到 hung 才复检
    assert judge(["Ss"]) == "healthy"        # 首采健康：单次即定，无复检
    assert slept == [1.0, 1.0]


def test_judge_lock_holder_second_sample_overrides():
    def judge(alives, stats):
        ia, is_ = iter(alives), iter(stats)
        return ec20_usb_pty.judge_lock_holder(
            123,
            stat_fn=lambda pid: next(is_),
            alive_fn=lambda pid: next(ia),
            sleep_fn=lambda s: None,
        )

    assert judge([True, False], ["U", None]) == "missing"   # 复检时进程已消失
    assert judge([True, True], ["U", None]) == "unknown"    # 复检 ps 失败：保守
    assert ec20_usb_pty.judge_lock_holder(None) == "unknown"


# ---- acquire_instance_lock：删锁重建 + 竞态防护 ----
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


def _patch_holder_state(monkeypatch, alive, stat):
    monkeypatch.setattr(ec20_usb_pty, "process_alive", lambda pid: alive)
    monkeypatch.setattr(ec20_usb_pty, "process_stat", lambda pid: stat)
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: None)  # 跳过复检间隔


def test_acquire_lock_takes_over_hung_holder(tmp_path, monkeypatch):
    """持锁进程确认 U 态挂死 → 删锁重建成功，新锁在新 inode 上。"""
    lock_path = tmp_path / "bridge.lock"
    zombie = _hold_lock(lock_path, "54321")
    old_ino = os.fstat(zombie.fileno()).st_ino
    _patch_holder_state(monkeypatch, alive=True, stat="U+")

    lock = ec20_usb_pty.acquire_instance_lock(lock_path)

    assert lock_path.read_text() == str(os.getpid())
    # flock 绑定 inode：新锁必须在新文件上，僵尸的旧锁留在旧 inode 上无害
    assert os.stat(lock_path).st_ino != old_ino
    lock.close()
    zombie.close()


def test_acquire_lock_takes_over_missing_holder(tmp_path, monkeypatch):
    """进程确认不存在但 flock 仍被持有（异常态）→ 同样走删锁重建。"""
    lock_path = tmp_path / "bridge.lock"
    zombie = _hold_lock(lock_path, "54321")
    _patch_holder_state(monkeypatch, alive=False, stat=None)

    lock = ec20_usb_pty.acquire_instance_lock(lock_path)

    assert lock_path.read_text() == str(os.getpid())
    lock.close()
    zombie.close()


def test_acquire_lock_refuses_healthy_holder(tmp_path, monkeypatch):
    """健康持锁进程：维持原有拒绝行为，绝不抢锁。"""
    lock_path = tmp_path / "bridge.lock"
    holder = _hold_lock(lock_path, "54321")
    _patch_holder_state(monkeypatch, alive=True, stat="Ss")

    with pytest.raises(RuntimeError, match="正在运行"):
        ec20_usb_pty.acquire_instance_lock(lock_path)
    assert lock_path.read_text() == "54321"  # 锁文件不被动
    holder.close()


def test_acquire_lock_refuses_when_ps_fails(tmp_path, monkeypatch):
    """顺手修 2（评审）：ps 超时/失败但进程存在 → unknown，保守拒绝不抢。"""
    lock_path = tmp_path / "bridge.lock"
    holder = _hold_lock(lock_path, "54321")
    _patch_holder_state(monkeypatch, alive=True, stat=None)

    with pytest.raises(RuntimeError, match="unknown"):
        ec20_usb_pty.acquire_instance_lock(lock_path)
    assert lock_path.exists()
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
    lock.close()


def test_acquire_lock_end_to_end_second_instance_refused(tmp_path):
    """全真集成（不打桩）：本进程健康持锁时，第二次 acquire 走完
    「flock 失败 → rescue 锁内重判 → healthy」全链路后拒绝。"""
    lock_path = tmp_path / "bridge.lock"
    lock = ec20_usb_pty.acquire_instance_lock(lock_path)
    with pytest.raises(RuntimeError, match="healthy"):
        ec20_usb_pty.acquire_instance_lock(lock_path)
    assert lock_path.read_text() == str(os.getpid())
    lock.close()


# ---- 删锁竞态防护（评审必修 M1：去掉 inode 校验必须让下列测试变红）----


def test_takeover_refuses_stale_judged_inode(tmp_path, monkeypatch):
    """交错 a：救援者 B 判定的是旧 inode，路径上已是救援者 A 重建的活锁——
    B 绝不 unlink，A 的锁原封不动。"""
    lock_path = tmp_path / "bridge.lock"
    lock_path.write_text("54321")          # 旧僵尸锁
    judged = lock_path.open("r")           # B 判定时拿着旧文件的 fd
    # A 完成删锁重建并持锁写入自己的 PID
    lock_path.unlink()
    rival = _hold_lock(lock_path, "77777")
    rival_ino = os.fstat(rival.fileno()).st_ino
    # 变异防护：即使判定说 hung（陈旧判定），inode 比对也必须先拦下
    monkeypatch.setattr(ec20_usb_pty, "judge_lock_holder", lambda pid, **kw: "hung")

    verdict, taken = ec20_usb_pty._takeover_lock(lock_path, judged)

    assert (verdict, taken) == ("stale", None)
    assert os.stat(lock_path).st_ino == rival_ino   # A 的活锁未被删
    assert lock_path.read_text() == "77777"
    judged.close()
    rival.close()


def test_takeover_rejudges_inside_rescue_lock(tmp_path):
    """交错 c：同一 inode 已被健康新持有者接管（内容=本进程 PID）——
    rescue 锁内重判看到 healthy，拒绝接管。真实 judge，无打桩。"""
    lock_path = tmp_path / "bridge.lock"
    lock_path.write_text(str(os.getpid()))
    judged = lock_path.open("r")

    verdict, taken = ec20_usb_pty._takeover_lock(lock_path, judged)

    assert (verdict, taken) == ("healthy", None)
    assert lock_path.read_text() == str(os.getpid())
    judged.close()


def test_takeover_backs_off_when_rescue_lock_busy(tmp_path, monkeypatch):
    """rescue 锁被另一救援者持有：退让（busy），路径不动。"""
    lock_path = tmp_path / "bridge.lock"
    lock_path.write_text("54321")
    judged = lock_path.open("r")
    other = ec20_usb_pty.rescue_lock_path(lock_path).open("a+")
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(ec20_usb_pty, "judge_lock_holder", lambda pid, **kw: "hung")

    verdict, taken = ec20_usb_pty._takeover_lock(lock_path, judged)

    assert (verdict, taken) == ("busy", None)
    assert lock_path.read_text() == "54321"
    judged.close()
    other.close()


def test_commit_lock_aborts_when_file_swapped(tmp_path):
    """交错 b：正常 acquirer flock 到手后、提交前被救援者换了文件——
    提交必须失败，绝不出现两把「已提交」的锁并立。"""
    lock_path = tmp_path / "bridge.lock"
    mine = lock_path.open("a+")
    fcntl.flock(mine, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # 救援者按（陈旧）判定换掉了脚下的文件
    lock_path.unlink()
    rival = _hold_lock(lock_path, "77777")

    assert ec20_usb_pty._commit_lock(lock_path, mine) is False
    assert lock_path.read_text() == "77777"   # 救援者的新锁不受影响
    mine.close()
    rival.close()


def test_unlink_judged_lock_rejudges_current_holder(tmp_path):
    """外层看门狗同族竞态：判定后、删锁前锁被新持有者接管（同 inode 重写
    PID）——rescue 锁内重读重判必须放行，且判定的是重读后的新持有者。"""
    lock_path = tmp_path / "bridge.lock"
    lock_path.write_text("777")
    judged = lock_path.open("r")
    # 新桥接管：同一 inode 上重写自己的 PID（missing 异常态下 flock 本就空闲）
    with lock_path.open("r+") as f:
        f.truncate(0)
        f.write("888")
    judged_pids: list[int | None] = []

    def judge(pid):
        judged_pids.append(pid)
        return "healthy" if pid == 888 else "hung"

    assert ec20_usb_pty.unlink_judged_lock(lock_path, judged, judge_fn=judge) is False
    assert lock_path.exists()
    assert judged_pids == [888]   # 重判的是当前内容，不是陈旧判定
    judged.close()


def test_unlink_judged_lock_removes_confirmed_stale(tmp_path):
    lock_path = tmp_path / "bridge.lock"
    lock_path.write_text("777")
    judged = lock_path.open("r")
    assert ec20_usb_pty.unlink_judged_lock(lock_path, judged, judge_fn=lambda pid: "hung") is True
    assert not lock_path.exists()
    judged.close()


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
    assert watchdog.clear_stale_lock(lock, judge_fn=lambda pid: "hung") is True
    assert not lock.exists()


def test_clear_stale_lock_removes_when_holder_missing(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")
    assert watchdog.clear_stale_lock(lock, judge_fn=lambda pid: "missing") is True
    assert not lock.exists()


def test_clear_stale_lock_keeps_healthy_holder(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")
    assert watchdog.clear_stale_lock(lock, judge_fn=lambda pid: "healthy") is False
    assert lock.exists()


def test_clear_stale_lock_conservative_on_garbage(tmp_path):
    """内容解析不出 PID：保守不动，与 acquire_instance_lock 的 unknown 口径一致。"""
    lock = tmp_path / "l.lock"
    lock.write_text("not-a-pid")
    assert watchdog.clear_stale_lock(lock, judge_fn=lambda pid: "hung") is False
    assert lock.exists()


def test_clear_stale_lock_missing_file(tmp_path):
    assert watchdog.clear_stale_lock(tmp_path / "absent.lock") is False


def test_rescue_kills_only_given_hung_pids(tmp_path):
    """评审必修：rescue 只收到并只杀确认挂死的 pid，健康桥不在名单里。"""
    lock = tmp_path / "l.lock"
    lock.write_text("777")
    killed: list[tuple[int, int]] = []
    verdicts = {777: "hung", 888: "missing"}  # 777 僵尸留置；888 已被杀掉

    watchdog.rescue(
        [777, 888], lock,
        kill_fn=lambda pid, sig: killed.append((pid, sig)),
        judge_fn=lambda pid: verdicts.get(pid, "healthy"),
        sleep_fn=lambda s: None,
    )

    assert killed == [(777, signal.SIGKILL), (888, signal.SIGKILL)]
    assert not lock.exists()  # 持锁者 777 确认挂死 → 删锁让新桥拿新锁


def test_rescue_tolerates_already_dead_process(tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("777")

    def kill_fn(pid: int, sig: int) -> None:
        raise ProcessLookupError

    watchdog.rescue([777], lock, kill_fn=kill_fn, judge_fn=lambda pid: "missing", sleep_fn=lambda s: None)
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

    action, missing_since, skips = watchdog.run_once(
        tmp_path / "sim7600-at",  # 不存在 = PTY 缺失
        "sim7600_usb_pty", tmp_path / "l.lock", ["bridge-cmd"], None,
        missing_since=0.0, threshold=20.0, now=100.0,
    )

    assert action == "start"
    assert started == [["bridge-cmd"]]
    assert missing_since is not None  # 重置计时，给新桥启动时间
    assert skips == 0                 # 拉了桥 = 连续跳过计数归零


def test_run_once_leaves_healthy_waiting_bridge_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "judge_lock_holder", lambda pid: "healthy")
    monkeypatch.setattr(
        watchdog, "start_bridge",
        lambda cmd, log: pytest.fail("健康桥等待设备时不得拉起新桥"),
    )

    action, _, skips = watchdog.run_once(
        tmp_path / "sim7600-at", "sim7600_usb_pty", tmp_path / "l.lock",
        ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
    )
    assert action == "none"
    assert skips == 1  # 记一次「因健康桥跳过」，连续超上限会强行拉桥（issue #139）


def test_run_once_rescues_only_hung_and_spares_healthy(tmp_path, monkeypatch):
    """评审必修：僵尸+健康桥并存时，只处置僵尸、不杀健康桥、不重复拉桥。"""
    lock = tmp_path / "l.lock"
    lock.write_text("111")
    rescued: list[list[int]] = []
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111, 222])
    monkeypatch.setattr(
        watchdog, "judge_lock_holder", lambda pid: "hung" if pid == 111 else "healthy",
    )
    monkeypatch.setattr(watchdog, "rescue", lambda pids, lp, **kw: rescued.append(list(pids)))
    monkeypatch.setattr(
        watchdog, "start_bridge",
        lambda cmd, log: pytest.fail("健康桥仍在时不得重复拉桥"),
    )

    action, missing_since, skips = watchdog.run_once(
        tmp_path / "sim7600-at", "sim7600_usb_pty", lock,
        ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
    )

    assert action == "rescue"
    assert rescued == [[111]]        # 只有确认挂死的 111，健康的 222 不在名单
    assert missing_since is not None
    assert skips == 1                # 健康桥 222 挡下了拉桥：计入连续跳过


def test_run_once_rescues_and_restarts_when_all_hung(tmp_path, monkeypatch):
    lock = tmp_path / "l.lock"
    lock.write_text("111")
    started: list[list[str]] = []
    rescued: list[list[int]] = []
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "judge_lock_holder", lambda pid: "hung")
    monkeypatch.setattr(watchdog, "rescue", lambda pids, lp, **kw: rescued.append(list(pids)))
    monkeypatch.setattr(
        watchdog, "start_bridge", lambda cmd, log: started.append(list(cmd)) or 4242,
    )

    action, missing_since, skips = watchdog.run_once(
        tmp_path / "sim7600-at", "sim7600_usb_pty", lock,
        ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
    )

    assert action == "rescue"
    assert rescued == [[111]]
    assert started == [["bridge-cmd"]]
    assert missing_since is not None
    assert skips == 0


def test_run_once_all_clear_resets_timer(tmp_path, monkeypatch):
    pty = tmp_path / "sim7600-at"
    pty.write_text("")  # PTY 在位
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "judge_lock_holder", lambda pid: "healthy")

    action, missing_since, skips = watchdog.run_once(
        pty, "sim7600_usb_pty", tmp_path / "l.lock", ["bridge-cmd"], None,
        missing_since=50.0, threshold=20.0, now=100.0,
    )
    assert action == "none"
    assert missing_since is None
    assert skips == 0


# ---- 外层看门狗：桥进程识别（issue #139 / WIL-145）----
#
# 真机 2026-08-20 首次自愈实测的误判现场：pgrep -f sim7600_usb_pty 同时匹配到
# 僵尸桥 21646 与**启动桥的 shell 包装** 21643（/bin/zsh -c … sim7600_usb_pty.py），
# 后者被当成「健康桥仍在场」→ 跳过拉桥。下列真实命令行取自本机 ps。

_REAL_BRIDGE_CMD = (
    "/opt/homebrew/Cellar/python@3.12/3.12.13_4/Frameworks/Python.framework/Versions/"
    "3.12/Resources/Python.app/Contents/MacOS/Python "
    "/Users/dev/CallPilot/callpilot/scripts/sim7600_usb_pty.py"
)
_REAL_WRAPPER_CMD = (
    "/bin/zsh -c cd /Users/dev/CallPilot/callpilot && "
    ".venv/bin/python scripts/sim7600_usb_pty.py >> /tmp/bridge.log 2>&1"
)


def test_classify_bridge_command_accepts_real_interpreter_forms():
    """解释器直接执行脚本 = 桥本体。

    注意真机解释器是 …/Python.app/Contents/MacOS/Python（**大写 P**）——
    「命令以 python 开头」的白名单会把真桥判成非桥，故用排除法。
    """
    classify = watchdog.classify_bridge_command
    assert classify(_REAL_BRIDGE_CMD, "sim7600_usb_pty") == "bridge"
    assert classify("/repo/.venv/bin/python /repo/scripts/sim7600_usb_pty.py", "sim7600_usb_pty") == "bridge"
    assert classify("/usr/bin/python3.12 scripts/sim7600_usb_pty.py --pty /tmp/x", "sim7600_usb_pty") == "bridge"
    # env/nohup 只是 exec 直通前缀，剥掉后仍是解释器直接执行脚本
    assert classify("/usr/bin/env python3 /repo/scripts/sim7600_usb_pty.py", "sim7600_usb_pty") == "bridge"
    assert classify("nohup /repo/.venv/bin/python /repo/scripts/sim7600_usb_pty.py", "sim7600_usb_pty") == "bridge"
    assert classify("/usr/bin/env PYTHONPATH=/repo/src python3 /repo/scripts/sim7600_usb_pty.py",
                    "sim7600_usb_pty") == "bridge"
    # 打包版（CallPilot.app --bridge，配 --pattern 覆盖）同样是桥本体
    assert classify("/Applications/CallPilot.app/Contents/MacOS/CallPilot --bridge", "CallPilot --bridge") == "bridge"


def test_classify_bridge_command_rejects_shell_wrappers():
    """issue #139 主修：shell 包装绝不能被当成桥本体。"""
    classify = watchdog.classify_bridge_command
    assert classify(_REAL_WRAPPER_CMD, "sim7600_usb_pty") == "wrapper"
    assert classify("/bin/sh -c exec python scripts/sim7600_usb_pty.py", "sim7600_usb_pty") == "wrapper"
    assert classify("/bin/bash /repo/scripts/run_sim7600_usb_pty.sh", "sim7600_usb_pty") == "wrapper"
    assert classify("-zsh -c python scripts/sim7600_usb_pty.py", "sim7600_usb_pty") == "wrapper"  # 登录 shell argv[0]
    assert classify("nohup /bin/zsh -c python scripts/sim7600_usb_pty.py &", "sim7600_usb_pty") == "wrapper"
    assert classify("tmux new-session -d python scripts/sim7600_usb_pty.py", "sim7600_usb_pty") == "wrapper"
    # 任何解释器的 `-c 内联代码` 都不是「直接执行脚本」
    assert classify("/repo/.venv/bin/python -c import scripts.sim7600_usb_pty as m; m.main()",
                    "sim7600_usb_pty") == "wrapper"


def test_classify_bridge_command_other_when_pattern_absent():
    assert watchdog.classify_bridge_command("/usr/bin/python3 other.py", "sim7600_usb_pty") == "other"
    assert watchdog.classify_bridge_command("", "sim7600_usb_pty") == "other"


def test_filter_bridge_pids_drops_wrapper_from_real_incident():
    """真机现场复现：21643(shell 包装) + 21646(桥本体) → 只认 21646。"""
    rows = [(21643, _REAL_WRAPPER_CMD), (21646, _REAL_BRIDGE_CMD)]
    assert watchdog.filter_bridge_pids(rows, "sim7600_usb_pty") == [21646]


def test_parse_ps_commands():
    text = f"21643 {_REAL_WRAPPER_CMD}\n 21646 {_REAL_BRIDGE_CMD}\nrubbish line\n"
    rows = watchdog.parse_ps_commands(text)
    assert [pid for pid, _ in rows] == [21643, 21646]
    assert rows[1][1] == _REAL_BRIDGE_CMD


def test_find_bridge_pids_excludes_shell_wrapper():
    pids = watchdog.find_bridge_pids(
        "sim7600_usb_pty",
        pgrep_fn=lambda pattern: [21643, 21646],
        commands_fn=lambda pids: [(21643, _REAL_WRAPPER_CMD), (21646, _REAL_BRIDGE_CMD)],
    )
    assert pids == [21646]


def test_find_bridge_pids_drops_exited_pid():
    """pgrep 与 ps 之间进程退出：ps 不列出它 → 自然剔除。"""
    pids = watchdog.find_bridge_pids(
        "sim7600_usb_pty",
        pgrep_fn=lambda pattern: [111, 222],
        commands_fn=lambda pids: [(222, _REAL_BRIDGE_CMD)],
    )
    assert pids == [222]


def test_find_bridge_pids_falls_back_when_ps_fails():
    """ps 探测失败：退回 pgrep 原始结果（宁可多认，别误判成「无桥」重复拉桥）。"""
    pids = watchdog.find_bridge_pids(
        "sim7600_usb_pty",
        pgrep_fn=lambda pattern: [21643, 21646],
        commands_fn=lambda pids: None,
    )
    assert pids == [21643, 21646]


def test_find_bridge_pids_skips_ps_when_no_candidates():
    called: list[int] = []
    assert watchdog.find_bridge_pids(
        "sim7600_usb_pty",
        pgrep_fn=lambda pattern: [],
        commands_fn=lambda pids: called.append(len(pids)) or [],
    ) == []
    assert called == []


# ---- 外层看门狗：连续跳过上限兜底（issue #139 第二道保险）----


def test_should_force_start_cap():
    assert watchdog.should_force_start(0, 3) is False
    assert watchdog.should_force_start(2, 3) is False
    assert watchdog.should_force_start(3, 3) is True
    assert watchdog.should_force_start(9, 0) is False   # 0 = 禁用兜底


def test_decide_action_forces_start_after_repeated_healthy_skips():
    """任何未预见的误判形态都不得导致永久不自愈：连续跳过超上限 → 强行拉桥。"""
    decide = watchdog.decide_action
    for skips in (0, 1, 2):
        assert decide([111], [False], pty_ok=False, missing_seconds=25.0, threshold=20.0,
                      healthy_skips=skips, max_healthy_skips=3) == "none"
    assert decide([111], [False], pty_ok=False, missing_seconds=25.0, threshold=20.0,
                  healthy_skips=3, max_healthy_skips=3) == "force_start"


def test_decide_action_skip_cap_never_overrides_healthy_pty_ok():
    """PTY 在位/未超阈值时，跳过计数再高也不该动手。"""
    assert watchdog.decide_action([111], [False], pty_ok=True, missing_seconds=0.0, threshold=20.0,
                                  healthy_skips=99, max_healthy_skips=3) == "none"
    assert watchdog.decide_action([111], [False], pty_ok=False, missing_seconds=5.0, threshold=20.0,
                                  healthy_skips=99, max_healthy_skips=3) == "none"


def test_decide_action_hung_still_rescues_at_cap():
    assert watchdog.decide_action([111], [True], pty_ok=False, missing_seconds=25.0, threshold=20.0,
                                  healthy_skips=99, max_healthy_skips=3) == "rescue"


def test_run_once_starts_bridge_when_only_shell_wrapper_matches(tmp_path, monkeypatch):
    """issue #139 端到端回归：pgrep 只匹配到 shell 包装时，必须照常拉桥。"""
    started: list[list[str]] = []
    monkeypatch.setattr(watchdog, "pgrep_pids", lambda pattern: [21643])
    monkeypatch.setattr(watchdog, "process_commands", lambda pids: [(21643, _REAL_WRAPPER_CMD)])
    monkeypatch.setattr(
        watchdog, "judge_lock_holder",
        lambda pid: pytest.fail("shell 包装不该进入挂死判定"),
    )
    monkeypatch.setattr(watchdog, "start_bridge", lambda cmd, log: started.append(list(cmd)) or 4242)

    action, _, skips = watchdog.run_once(
        tmp_path / "sim7600-at", "sim7600_usb_pty", tmp_path / "l.lock",
        ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
    )

    assert (action, started, skips) == ("start", [["bridge-cmd"]], 0)


def test_run_once_force_starts_after_skip_cap(tmp_path, monkeypatch):
    """兜底：连续 3 次因「健康桥仍在场」跳过后，第 4 轮无视健康判定强行拉桥。"""
    started: list[list[str]] = []
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "judge_lock_holder", lambda pid: "healthy")
    monkeypatch.setattr(watchdog, "start_bridge", lambda cmd, log: started.append(list(cmd)) or 4242)

    skips = 0
    actions: list[str] = []
    for _ in range(4):
        action, _, skips = watchdog.run_once(
            tmp_path / "sim7600-at", "sim7600_usb_pty", tmp_path / "l.lock",
            ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
            healthy_skips=skips, max_healthy_skips=3,
        )
        actions.append(action)

    assert actions == ["none", "none", "none", "force_start"]
    assert started == [["bridge-cmd"]]   # 只强拉一次
    assert skips == 0                    # 拉桥后计数归零，不会每轮风暴式强拉


def test_run_once_rescue_force_starts_after_skip_cap(tmp_path, monkeypatch):
    """救援路径的同一条兜底：僵尸+「健康」桥并存且已连续跳过到上限 → 照样拉桥。"""
    lock = tmp_path / "l.lock"
    lock.write_text("111")
    started: list[list[str]] = []
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111, 222])
    monkeypatch.setattr(watchdog, "judge_lock_holder", lambda pid: "hung" if pid == 111 else "healthy")
    monkeypatch.setattr(watchdog, "rescue", lambda pids, lp, **kw: None)
    monkeypatch.setattr(watchdog, "start_bridge", lambda cmd, log: started.append(list(cmd)) or 4242)

    action, _, skips = watchdog.run_once(
        tmp_path / "sim7600-at", "sim7600_usb_pty", lock,
        ["bridge-cmd"], None, missing_since=0.0, threshold=20.0, now=100.0,
        healthy_skips=3, max_healthy_skips=3,
    )

    assert (action, started, skips) == ("rescue", [["bridge-cmd"]], 0)


def test_run_once_skip_counter_resets_when_pty_returns(tmp_path, monkeypatch):
    pty = tmp_path / "sim7600-at"
    pty.write_text("")  # PTY 恢复在位
    monkeypatch.setattr(watchdog, "find_bridge_pids", lambda pattern: [111])
    monkeypatch.setattr(watchdog, "judge_lock_holder", lambda pid: "healthy")

    _, _, skips = watchdog.run_once(
        pty, "sim7600_usb_pty", tmp_path / "l.lock", ["bridge-cmd"], None,
        missing_since=50.0, threshold=20.0, now=100.0, healthy_skips=2, max_healthy_skips=3,
    )
    assert skips == 0  # 连续性被打断：不能把历史跳过累计到下一次故障


def test_classify_bridge_command_honours_regex_pattern():
    """pattern 与 pgrep -f 同口径当正则用（--pattern 可传正则），非法正则退回子串。"""
    classify = watchdog.classify_bridge_command
    assert classify(_REAL_BRIDGE_CMD, r"sim7600_usb_pty\.py$") == "bridge"
    assert classify(_REAL_WRAPPER_CMD, r"sim7600_usb_pty\.py") == "wrapper"
    assert classify(_REAL_BRIDGE_CMD, r"ec20_usb_pty\.py") == "other"
    assert classify("/usr/bin/python3 scripts/sim7600_usb_pty[.py", "sim7600_usb_pty[") == "bridge"
