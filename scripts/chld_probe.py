#!/usr/bin/env python3
"""AT+CHLD 三方通话能力探针（WIL-120 四期 Path B 前置）。

Path B（机主并入通话）依赖两个真机事实，本探针逐一确认：
1. 模组固件是否实现 3GPP 补充业务（AT+CHLD 系列）——空闲态即可查；
2. 运营商线路是否真的开通了呼叫保持/多方通话业务——必须通话中实测
   （固件应答 OK 不代表网络侧执行了，CLCC 的 state/mpty 才是证据）。

用法（模组开机、桥就绪后）：

    .venv/bin/python scripts/chld_probe.py                # 空闲态能力查询
    .venv/bin/python scripts/chld_probe.py --live         # 通话中 hold/resume 实测
    .venv/bin/python scripts/chld_probe.py --port /dev/xx # 非默认 AT 口

--live 流程：先用手机打进来并接通（或发起一通外呼），脚本会引导执行
hold → 验证 → resume → 验证。完整三方（CHLD=3 并入）需要两路通话，
探针只打印手动步骤清单，不自动执行——第二路要拨真实号码，由机主自行操作。

探针只读不改配置；结论三档：SUPPORTED / UNSUPPORTED / INCONCLUSIVE。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import serial  # noqa: E402

from agentcall.modem import parse_clcc_lines  # noqa: E402

DEFAULT_PORT = "/tmp/sim7600-at"


def at(port: serial.Serial, cmd: str, wait: float = 3.0) -> str:
    port.reset_input_buffer()
    port.write((cmd + "\r").encode())
    deadline = time.time() + wait
    buf = b""
    while time.time() < deadline:
        chunk = port.read(256)
        if chunk:
            buf += chunk
            if b"OK" in buf or b"ERROR" in buf:
                break
    text = buf.decode(errors="replace").strip()
    print(f">>> {cmd}\n{text}\n")
    return text


def probe_idle(port: serial.Serial) -> bool:
    print("== 空闲态能力查询 ==")
    at(port, "AT")
    caps = at(port, "AT+CHLD=?")
    ccwa = at(port, "AT+CCWA?")
    at(port, "AT+CLCC")
    ok = "OK" in caps and "CHLD" in caps.upper()
    if ok:
        print("[固件] AT+CHLD 能力集已上报 → 固件层 SUPPORTED")
        print("       注意：这只证明固件实现了指令；运营商侧要 --live 实测。")
    elif "ERROR" in caps:
        print("[固件] AT+CHLD=? 报 ERROR → 固件层 UNSUPPORTED，Path B 不可行")
    else:
        print("[固件] 无明确应答 → INCONCLUSIVE，检查 AT 口后重试")
    if "ERROR" in ccwa:
        print("[提示] CCWA 查询失败——呼叫等待状态未知，不影响 hold 实测")
    return ok


def probe_live(port: serial.Serial) -> None:
    print("== 通话中 hold/resume 实测 ==")
    calls = parse_clcc_lines(at(port, "AT+CLCC"))
    if not any(c["state"] == "active" for c in calls):
        print("没有进行中的通话。先用手机打进来并接通（或发起外呼），再重跑 --live。")
        return
    print(f"[1/4] 当前通话: {calls}")
    at(port, "AT+CHLD=2")
    time.sleep(1.5)
    held = parse_clcc_lines(at(port, "AT+CLCC"))
    hold_ok = any(c["state"] == "held" for c in held)
    print(f"[2/4] hold 后: {held} → {'✓ 已保持' if hold_ok else '✗ 未进入 held'}")
    at(port, "AT+CHLD=2")
    time.sleep(1.5)
    resumed = parse_clcc_lines(at(port, "AT+CLCC"))
    resume_ok = any(c["state"] == "active" for c in resumed)
    print(f"[3/4] resume 后: {resumed} → {'✓ 已恢复' if resume_ok else '✗ 未恢复 active'}")
    print("[4/4] 结论:", "SUPPORTED（hold/resume 网络侧生效）" if hold_ok and resume_ok
          else "UNSUPPORTED 或线路未开通呼叫保持——联系运营商确认补充业务")
    print(
        "\n完整三方（并入）手动步骤（探针不自动执行，第二路需拨真实号码）：\n"
        "  1. 保持第一路：AT+CHLD=2\n"
        "  2. 拨第二路：  ATD<第二号码>;（等接通，CLCC 应见一路 held 一路 active）\n"
        "  3. 并入三方：  AT+CHLD=3\n"
        "  4. 验证：      AT+CLCC——两路 state 均 active 且 mpty=1 即成功"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--live", action="store_true", help="通话中 hold/resume 实测")
    args = parser.parse_args()
    try:
        port = serial.Serial(args.port, 115200, timeout=1)
    except (OSError, serial.SerialException) as exc:
        print(f"打不开 AT 口 {args.port}: {exc}")
        return 2
    try:
        supported = probe_idle(port)
        if args.live:
            probe_live(port)
        elif supported:
            print("\n下一步：接通一通电话后跑 `--live` 做网络侧实测。")
        return 0 if supported else 1
    finally:
        port.close()


if __name__ == "__main__":
    raise SystemExit(main())
