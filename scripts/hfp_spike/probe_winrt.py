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


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        calls, _backend = _import_calls()
    except ImportError as exc:
        print(_IMPORT_HINT.format(err=exc))
        return 1

    events: list[dict] = []
    t0 = time.monotonic()

    def on_state(sender, evt) -> None:
        entry = {
            "at": round(time.monotonic() - t0, 2),
            "is_call_active": bool(calls.PhoneCallManager.is_call_active),
            "is_call_incoming": bool(
                getattr(calls.PhoneCallManager, "is_call_incoming", False)
            ),
        }
        events.append(entry)
        print("[{at:7.2f}s] active={is_call_active} incoming={is_call_incoming}".format(**entry))

    try:
        token = calls.PhoneCallManager.add_call_state_changed(on_state)
    except Exception as exc:
        print("订阅 call_state_changed 失败: {}: {}".format(type(exc).__name__, exc))
        return 1

    print("监听 {}s，期间拨打/接听试试。Ctrl-C 提前结束。".format(args.seconds))
    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            calls.PhoneCallManager.remove_call_state_changed(token)
        except Exception:
            pass

    print("\n=== 判据 ===")
    print("收到状态事件: {} ({} 次)".format("PASS" if events else "FAIL", len(events)))
    _save("winrt_watch.json", {"events": events})
    return 0 if events else 1


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

    pw = sub.add_parser("watch", help="监听通话状态变化")
    pw.add_argument("--seconds", type=float, default=120.0)
    pw.set_defaults(func=cmd_watch)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
