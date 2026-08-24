"""Phase 0.2a：验证 WinRT PhoneLine API 在本机 Windows 上是否还活着。

`Windows.ApplicationModel.Calls` 是微软给「PC 通过蓝牙配对的手机打电话」做的
公开 API（Phone Link 的公开面）。若它在 Win11 26200 上还能用，就能拿到最干净的
控制通道：不用 adb、不用额外硬件、不用手机开 USB 调试。

**风险已知**：这套 API 大体处于半废弃状态，很可能直接不可用——所以它只是
0.2 的三条并行探路之一，不押注。跑失败是有用的结论，不是问题。

依赖（未安装时脚本会提示，不自动装）::

    D:/Callpilot/.venv/Scripts/pip.exe install winsdk

用法::

    python probe_winrt.py check     # API 是否可导入、能否拿到 PhoneCallStore
    python probe_winrt.py lines     # 枚举 PhoneLine，看有没有蓝牙线路
    python probe_winrt.py watch     # 监听通话状态变化
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = _REPO_ROOT / "docs" / "fixtures" / "hfp_spike"

_IMPORT_HINT = """
!! 导入 WinRT 失败: {err}

装依赖后重试（装进项目 venv，不要装全局）：
    D:/Callpilot/.venv/Scripts/pip.exe install winsdk

若 winsdk 装不上或不支持本 Python 版本，这条路直接判 FAIL 即可——
0.2b(RFCOMM) / 0.2c(adb) 是并行的另外两条路。
"""


def _import_calls():
    """返回 (calls 模块, 后端名)；失败抛 ImportError。"""
    try:
        from winsdk.windows.applicationmodel import calls  # type: ignore

        return calls, "winsdk"
    except ImportError:
        pass
    from winrt.windows.applicationmodel import calls  # type: ignore

    return calls, "winrt"


def cmd_check(args: argparse.Namespace) -> int:
    try:
        calls, backend = _import_calls()
    except ImportError as exc:
        print(_IMPORT_HINT.format(err=exc))
        _save("winrt_check.json", {"importable": False, "error": str(exc)})
        return 1

    print("WinRT 后端: {}".format(backend))
    result: dict = {"importable": True, "backend": backend}

    names = [n for n in dir(calls) if not n.startswith("_")]
    print("Calls 命名空间可见类型 {} 个，关键几个：".format(len(names)))
    for key in ("PhoneCallManager", "PhoneCallStore", "PhoneLine",
                "PhoneLineTransportDevice", "PhoneCallVideoCapabilities"):
        print("  {:28s} {}".format(key, "OK" if key in names else "缺失"))
    result["types_present"] = {k: (k in names) for k in (
        "PhoneCallManager", "PhoneCallStore", "PhoneLine", "PhoneLineTransportDevice")}

    # PhoneCallStore 需要 phoneCall capability；非打包应用上很可能直接抛。
    try:
        store = asyncio.run(_get_store(calls))
        result["store_ok"] = store is not None
        print("\nPhoneCallStore: {}".format("拿到了" if store else "返回 None"))
    except Exception as exc:
        result["store_ok"] = False
        result["store_error"] = "{}: {}".format(type(exc).__name__, exc)
        print("\nPhoneCallStore 失败: {}".format(result["store_error"]))
        print("（多半是缺 phoneCall capability——非打包 Python 进程拿不到）")

    print("\n=== 判据 ===")
    ok = bool(result.get("store_ok"))
    print("WinRT PhoneLine 可用: {}".format("PASS" if ok else "FAIL"))
    if not ok:
        print("→ 控制通道走 0.2b(RFCOMM) 或 0.2c(adb)。")
    _save("winrt_check.json", result)
    return 0 if ok else 1


async def _get_store(calls):
    return await calls.PhoneCallManager.request_store_async()


async def _enumerate_lines(calls) -> list[dict]:
    store = await calls.PhoneCallManager.request_store_async()
    if store is None:
        return []
    lines: list[dict] = []
    try:
        default_id = await store.get_default_line_async()
    except Exception as exc:
        return [{"error": "get_default_line_async: {}".format(exc)}]
    if default_id is None:
        return []
    line = await calls.PhoneLine.from_id_async(default_id)
    if line is None:
        return [{"error": "PhoneLine.from_id_async 返回 None"}]
    # PhoneLineTransport 枚举：0=Cellular, 1=VoipApp, 2=Bluetooth。
    # winsdk 返回原始 int，必须解码——真机实测 transport=2 曾被字符串判据误判。
    transport_raw = getattr(line, "transport", None)
    transport_names = {0: "cellular", 1: "voip_app", 2: "bluetooth"}
    lines.append({
        "id": str(default_id),
        "display_name": getattr(line, "display_name", None),
        "number": getattr(line, "number", None),
        "transport": transport_names.get(
            int(transport_raw) if transport_raw is not None else -1,
            str(transport_raw),
        ),
        "can_dial": bool(getattr(line, "can_dial", False)),
        "voicemail_count": getattr(line, "voicemail_count", None),
    })
    return lines


def cmd_lines(args: argparse.Namespace) -> int:
    try:
        calls, _backend = _import_calls()
    except ImportError as exc:
        print(_IMPORT_HINT.format(err=exc))
        return 1
    try:
        lines = asyncio.run(_enumerate_lines(calls))
    except Exception as exc:
        print("枚举失败: {}: {}".format(type(exc).__name__, exc))
        _save("winrt_lines.json", {"ok": False, "error": str(exc)})
        return 1

    print(json.dumps(lines, ensure_ascii=False, indent=2, default=str))
    bt_lines = [ln for ln in lines if "bluetooth" in str(ln.get("transport", "")).lower()]
    print("\n=== 判据 ===")
    print("枚举到 PhoneLine   : {} ({} 条)".format("PASS" if lines else "FAIL", len(lines)))
    print("其中蓝牙线路       : {} ({} 条)".format(
        "PASS" if bt_lines else "FAIL", len(bt_lines)))
    if bt_lines:
        print("→ 这条路能走通，控制通道优先选它（比 adb 干净得多）。")
    _save("winrt_lines.json", {"ok": bool(lines), "lines": lines})
    return 0 if bt_lines else 1


async def _default_line(calls):
    store = await calls.PhoneCallManager.request_store_async()
    line_id = await store.get_default_line_async()
    return await calls.PhoneLine.from_id_async(line_id)


def _mask_number(number: str) -> str:
    """落盘用打码：真实号码不入库（CLAUDE.md 硬约束），保尾 2 位供比对。"""
    digits = [ch for ch in (number or "") if ch.isdigit()]
    if len(digits) < 4:
        return "***" if number else ""
    return "***{}{} ({}位)".format(digits[-2], digits[-1], len(digits))


def cmd_watch(args: argparse.Namespace) -> int:
    """轮询活跃通话，验证模组契约要用的每一环：

    来电事件 + 对端号码（← on_ring/+CLIP）、状态流转（← CLCC 轮询）、
    --answer 自动接听（← answer/ATA）、--end-after 自动挂断（← hangup）。
    轮询而非事件订阅：WinRT 事件回调落在 MTA 线程且不能直接调 async 方法，
    spike 里轮询更省事，正式实现（Phase 1）也正好对齐现有 CLCC 轮询模型。
    """
    try:
        calls, _backend = _import_calls()
    except ImportError as exc:
        print(_IMPORT_HINT.format(err=exc))
        return 1

    try:
        line = asyncio.run(_default_line(calls))
    except Exception as exc:
        print("拿默认线路失败: {}: {}".format(type(exc).__name__, exc))
        return 1
    print("线路: {}（transport={}）".format(line.display_name, line.transport))
    print("监听 {}s——现在用另一台手机拨这张卡。answer={} end_after={}s".format(
        args.seconds, args.answer, args.end_after))

    # 状态/方向名直接从枚举取，不手写映射（真机实测猜错过一轮）。
    status_names = {m.value: m.name.lower() for m in calls.PhoneCallStatus}

    events: list[dict] = []
    t0 = time.monotonic()
    last_snapshot: list[tuple] = []
    answered_ids: set = set()
    talking_since: dict = {}

    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            snapshot = []
            # get_all_active_phone_calls 返回 PhoneCallsResult 包装，不可直接迭代。
            result = line.get_all_active_phone_calls()
            for call in list(result.all_active_phone_calls or []):
                try:
                    info = call.get_phone_call_info()
                    number = info.phone_number
                    direction = int(info.call_direction)
                except Exception:
                    number, direction = "?", -1
                status_raw = int(call.status)
                status = status_names.get(status_raw, str(status_raw))
                snapshot.append((call.call_id, status, number, direction))

                if status == "incoming" and args.answer and call.call_id not in answered_ids:
                    answered_ids.add(call.call_id)
                    try:
                        call.accept_incoming()
                        print("  → accept_incoming() 已调用")
                    except Exception as exc:
                        print("  → accept_incoming() 失败: {}".format(exc))
                if status == "talking":
                    talking_since.setdefault(call.call_id, time.monotonic())
                    if (args.end_after
                            and time.monotonic() - talking_since[call.call_id]
                            >= args.end_after):
                        try:
                            call.end()
                            print("  → end() 已调用")
                            talking_since[call.call_id] = float("inf")
                        except Exception as exc:
                            print("  → end() 失败: {}".format(exc))

            if snapshot != last_snapshot:
                at = round(time.monotonic() - t0, 2)
                if snapshot:
                    for cid, status, number, direction in snapshot:
                        # 终端显示全号（本地）；events 落盘打码（真实号码不入库）。
                        entry = {"at": at, "call_id": cid, "status": status,
                                 "number": _mask_number(number),
                                 "direction": direction}
                        events.append(entry)
                        print("[{:7.2f}s] {:8s} number={!r} dir={}".format(
                            at, status, number, direction))
                else:
                    events.append({"at": at, "status": "no-active-calls"})
                    print("[{:7.2f}s] （无活跃通话）".format(at))
                last_snapshot = snapshot
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n已停止。")

    ringing = [e for e in events
               if e.get("status") in ("incoming", "talking", "ended")]
    with_number = [e for e in ringing if e.get("number")]
    print("\n=== 判据 ===")
    print("观察到来电/通话     : {} ({} 条)".format(
        "PASS" if ringing else "NO DATA", len(ringing)))
    print("拿到对端号码        : {}".format(
        "PASS " + repr(with_number[0]["number"]) if with_number
        else "FAIL（PhoneCallInfo.phone_number 为空）"))
    _save("winrt_watch.json", {"events": events})
    return 0 if ringing else 1


def _save(name: str, data: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), **data}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("\n已落盘: {}".format(path))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("check", help="API 是否可导入、能否拿到 PhoneCallStore")
    pc.set_defaults(func=cmd_check)

    pln = sub.add_parser("lines", help="枚举 PhoneLine，看有没有蓝牙线路")
    pln.set_defaults(func=cmd_lines)

    pw = sub.add_parser("watch", help="轮询活跃通话：来电号码 / 状态流转，可选自动接听挂断")
    pw.add_argument("--seconds", type=float, default=120.0)
    pw.add_argument("--answer", action="store_true", help="响铃时自动 accept_incoming()")
    pw.add_argument("--end-after", type=float, default=0.0,
                    help="接通 N 秒后自动 end()（0=不挂）")
    pw.set_defaults(func=cmd_watch)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
