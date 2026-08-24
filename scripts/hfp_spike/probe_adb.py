"""Phase 0.2c：验证 adb 能否当控制通道（拨号/接听/挂断/来电号码/SIM 身份）。

这是与 OS 无关的兜底路线：音频仍走蓝牙 HFP 端点，控制走 adb shell。
手机侧不装任何 app，只需开「USB 调试」。

**本脚本要回答的关键问题**：
  1. `dumpsys telephony.registry` 里的来电号码是否被脱敏（新 Android 常见）；
  2. `input keyevent` 能否可靠接听/挂断；
  3. `getprop gsm.sim.operator.numeric` 拿到的 PLMN 能否喂进 sim_identity 的
     运营商表 → 决定 Phase 1 要不要加 ``identify_from_plmn()``。

用法::

    python probe_adb.py check       # adb/设备是否就绪
    python probe_adb.py sim         # SIM 身份与解析出的免费客服号
    python probe_adb.py state       # 当前通话状态快照
    python probe_adb.py watch       # 持续监听状态变化（观察来电）
    python probe_adb.py dial        # 拨本卡免费客服号（有护栏）
    python probe_adb.py answer / hangup
    python probe_adb.py sms         # 能否读收件箱
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from agentcall.sim_identity import (  # noqa: E402
    _MCC_FALLBACK,
    _PLMN_CARRIERS,
    _SERVICE_NUMBERS,
)

OUT_DIR = _REPO_ROOT / "docs" / "fixtures" / "hfp_spike"

# adb 常见安装位置（PATH 里没有时按序探测）。
_ADB_CANDIDATES = (
    pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/platform-tools/adb.exe",
    pathlib.Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools/adb.exe",
    pathlib.Path("C:/Program Files/Android/platform-tools/adb.exe"),
    pathlib.Path("C:/platform-tools/adb.exe"),
    pathlib.Path.home() / "scoop/apps/adb/current/adb.exe",
)

# KEYCODE_CALL / KEYCODE_ENDCALL / KEYCODE_HEADSETHOOK
KEY_CALL, KEY_ENDCALL, KEY_HEADSETHOOK = 5, 6, 79


def find_adb(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if pathlib.Path(explicit).exists() else None
    found = shutil.which("adb")
    if found:
        return found
    for cand in _ADB_CANDIDATES:
        try:
            if cand.exists():
                return str(cand)
        except (OSError, ValueError):
            continue
    return None


class Adb:
    def __init__(self, exe: str, serial: str | None = None) -> None:
        self.exe = exe
        self.serial = serial

    def run(self, *args: str, timeout: float = 15.0) -> tuple[int, str, str]:
        cmd = [self.exe]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", "timeout after {}s".format(timeout)

    def shell(self, cmd: str, timeout: float = 15.0) -> str:
        _rc, out, _err = self.run("shell", cmd, timeout=timeout)
        return out

    def getprop(self, name: str) -> str:
        return self.shell("getprop {}".format(name)).strip()


def resolve_hotline(plmn: str) -> tuple[str, str]:
    """PLMN → (运营商, 免费客服号)。复用 sim_identity 的表，不另抄一份。

    这正是 Phase 1 要加的 ``identify_from_plmn()`` 的逻辑原型。
    """
    if not plmn:
        return ("未知", "")
    carrier = _PLMN_CARRIERS.get(plmn[:5])
    if carrier:
        return (carrier, _SERVICE_NUMBERS.get(carrier, ""))
    return _MCC_FALLBACK.get(plmn[:3], ("未知", ""))


def _need_device(args: argparse.Namespace) -> Adb | None:
    exe = find_adb(args.adb)
    if not exe:
        print("!! 找不到 adb。装 Android platform-tools 后重试，或用 --adb 指定路径。")
        print("   下载: https://developer.android.com/tools/releases/platform-tools")
        return None
    adb = Adb(exe, args.serial)
    rc, out, err = adb.run("devices")
    if rc != 0:
        print("!! adb devices 失败: {}".format(err or out))
        return None
    lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    devices = [ln.split()[0] for ln in lines if ln.split()[-1] == "device"]
    unauthorized = [ln for ln in lines if "unauthorized" in ln]
    if unauthorized:
        print("!! 设备未授权——在手机上勾选「始终允许此计算机调试」。")
        return None
    if not devices:
        print("!! 没有已连接的设备。检查 USB 线、开发者选项里的「USB 调试」。")
        return None
    if len(devices) > 1 and not args.serial:
        print("!! 连了多台设备，用 --serial 指定: {}".format(devices))
        return None
    return adb


def cmd_check(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    info = {
        "adb_exe": adb.exe,
        "adb_version": adb.run("version")[1].splitlines()[0] if adb.run("version")[1] else "",
        "model": adb.getprop("ro.product.model"),
        "brand": adb.getprop("ro.product.brand"),
        "android_release": adb.getprop("ro.build.version.release"),
        "sdk_int": adb.getprop("ro.build.version.sdk"),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    _save("adb_check.json", info)
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    plmn = adb.getprop("gsm.sim.operator.numeric")
    carrier, hotline = resolve_hotline(plmn)
    info = {
        "plmn_raw": plmn,
        "operator_alpha": adb.getprop("gsm.sim.operator.alpha"),
        "sim_state": adb.getprop("gsm.sim.state"),
        "network_type": adb.getprop("gsm.network.type"),
        "resolved_carrier": carrier,
        "resolved_hotline": hotline,
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("\n=== 判据 ===")
    ok = bool(plmn) and bool(hotline)
    print("PLMN 可读            : {} {!r}".format("PASS" if plmn else "FAIL", plmn))
    print("能解析出免费客服号   : {} {!r} ({})".format(
        "PASS" if hotline else "FAIL", hotline, carrier))
    if ok:
        print("\n→ Phase 1 的 identify_from_plmn() 可行，直接复用 sim_identity 的表。")
    _save("adb_sim.json", info)
    return 0 if ok else 1


_CALL_STATE_RE = re.compile(r"mCallState=(\d+)")
_INCOMING_RE = re.compile(r"mCallIncomingNumber=([^\s,}]*)")
_STATE_NAMES = {"0": "IDLE", "1": "RINGING", "2": "OFFHOOK"}


def _snapshot(adb: Adb) -> dict:
    raw = adb.shell("dumpsys telephony.registry")
    state = _CALL_STATE_RE.search(raw)
    incoming = _INCOMING_RE.search(raw)
    num = incoming.group(1) if incoming else ""
    return {
        "call_state": state.group(1) if state else "?",
        "call_state_name": _STATE_NAMES.get(state.group(1) if state else "", "?"),
        "incoming_number": num,
        "incoming_number_present": bool(num),
        "raw_len": len(raw),
    }


def cmd_state(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    snap = _snapshot(adb)
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    print("\n注：来电号码要在**响铃时**才有值。空值可能是没来电，也可能是被脱敏——")
    print("    用 `watch` 在真实来电时观察才能区分。")
    _save("adb_state.json", snap)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    print("持续监听通话状态，Ctrl-C 结束。现在从另一台手机拨这张卡试试。")
    last = None
    transitions = []
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds:
            snap = _snapshot(adb)
            key = (snap["call_state"], snap["incoming_number"])
            if key != last:
                elapsed = round(time.monotonic() - t0, 2)
                entry = {"at": elapsed, **snap}
                transitions.append(entry)
                print("[{:7.2f}s] {:8s} incoming={!r}".format(
                    elapsed, snap["call_state_name"], snap["incoming_number"]))
                last = key
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止。")
    ringing = [t for t in transitions if t["call_state_name"] == "RINGING"]
    got_number = [t for t in ringing if t["incoming_number_present"]]
    print("\n=== 判据 ===")
    print("观察到 RINGING       : {} ({} 次)".format(
        "PASS" if ringing else "NO DATA", len(ringing)))
    print("响铃时拿到来电号码   : {}".format(
        "PASS" if got_number else "FAIL（被脱敏 → 只能靠 HFP 的 +CLIP）"))
    _save("adb_watch.json", {"transitions": transitions})
    return 0


def cmd_dial(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    plmn = adb.getprop("gsm.sim.operator.numeric")
    carrier, hotline = resolve_hotline(plmn)
    if not hotline:
        print("!! 识别不出本卡运营商（PLMN={!r}），按 CLAUDE.md 硬约束拒绝拨号。".format(plmn))
        return 1
    target = args.number or hotline
    # 硬约束：真机外呼只拨本卡运营商的免费客服号。护栏写在脚本里，
    # 不靠人记（与 src/agentcall/dial_guard.py 同一政策）。
    if target != hotline:
        print("!! 拒绝拨 {!r}：本卡（{}）的免费客服号是 {}。".format(target, carrier, hotline))
        print("   CLAUDE.md 硬约束——真机只拨本卡运营商免费客服热线。")
        return 1
    print("拨号 {} （{}）...".format(target, carrier))
    rc, out, err = adb.run(
        "shell", "am", "start", "-a", "android.intent.action.CALL",
        "-d", "tel:{}".format(target),
    )
    print("rc={}\n{}\n{}".format(rc, out, err))
    time.sleep(3)
    print(json.dumps(_snapshot(adb), ensure_ascii=False, indent=2))
    return 0 if rc == 0 else 1


def cmd_answer(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    before = _snapshot(adb)
    key = KEY_HEADSETHOOK if args.headsethook else KEY_CALL
    adb.shell("input keyevent {}".format(key))
    time.sleep(1.5)
    after = _snapshot(adb)
    print("before={}\nafter ={}".format(
        json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
    ok = before["call_state_name"] == "RINGING" and after["call_state_name"] == "OFFHOOK"
    print("\n接听 keyevent {}: {}".format(key, "PASS" if ok else "未观察到 RINGING→OFFHOOK"))
    return 0 if ok else 1


def cmd_hangup(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    before = _snapshot(adb)
    adb.shell("input keyevent {}".format(KEY_ENDCALL))
    time.sleep(1.5)
    after = _snapshot(adb)
    print("before={}\nafter ={}".format(
        json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
    ok = after["call_state_name"] == "IDLE"
    print("\n挂断: {}".format("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def cmd_sms(args: argparse.Namespace) -> int:
    adb = _need_device(args)
    if not adb:
        return 1
    out = adb.shell(
        "content query --uri content://sms/inbox "
        "--projection address:body:date --sort 'date DESC' "
    )
    readable = bool(out) and "Error" not in out and "Permission Denial" not in out
    print(out[:1500] if out else "(空)")
    print("\n=== 判据 ===")
    print("收件箱可读: {}".format("PASS" if readable else "FAIL"))
    if not readable:
        print("  → 收短信只能另想办法（蓝牙 MAP profile）。")
    print("注：发短信本脚本不试——adb 侧只能做 UI 自动化，方案里已标为 v1 不做。")
    _save("adb_sms.json", {"readable": readable, "sample": out[:2000]})
    return 0 if readable else 1


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
    p.add_argument("--adb", help="adb 可执行文件路径（PATH 里没有时用）")
    p.add_argument("--serial", help="设备序列号（连了多台时用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (
        ("check", cmd_check, "adb / 设备是否就绪"),
        ("sim", cmd_sim, "SIM 身份与解析出的免费客服号"),
        ("state", cmd_state, "当前通话状态快照"),
        ("dial", cmd_dial, "拨本卡免费客服号（有护栏）"),
        ("answer", cmd_answer, "接听当前来电"),
        ("hangup", cmd_hangup, "挂断当前通话"),
        ("sms", cmd_sms, "能否读收件箱"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.set_defaults(func=fn)
        if name == "dial":
            sp.add_argument("--number", help="覆盖号码（非本卡客服号会被拒）")
        if name == "answer":
            sp.add_argument("--headsethook", action="store_true",
                            help="用 KEYCODE_HEADSETHOOK 而非 KEYCODE_CALL")

    sw = sub.add_parser("watch", help="持续监听状态变化（观察来电）")
    sw.add_argument("--seconds", type=float, default=120.0)
    sw.add_argument("--interval", type=float, default=0.5)
    sw.set_defaults(func=cmd_watch)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
