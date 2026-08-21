"""Phase 0.1：验证 Windows 蓝牙 HFP 音频端点能否当模组声卡用。

安卓手机以 HFP Audio Gateway 身份把通话音频交给 PC（PC 扮演免提设备），
Windows 会为此建出一对 8kHz(CVSD) 或 16kHz(mSBC) 的音频端点。本脚本验证
``audio_bridge.ModemAudioBridge`` 能否零改动直接开这对端点。

方向约定与 audio_bridge.py 一致：
  上行(RX) = 从端点读 = 对端说话的声音
  下行(TX) = 往端点写 = 对端听到的声音

用法::

    python probe_audio.py list                    # 配对后随时可跑
    python probe_audio.py test --seconds 10 --dtmf 1   # 必须在通话接通时跑
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import threading
import time
import wave

# 复用项目已按 AMR 压缩标定过的 DTMF 合成，而不是自己再造一个。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import numpy as np  # noqa: E402

from agentcall.dtmf import dtmf_tone  # noqa: E402

# HFP 端点在 Windows 上的常见命名；MME host API 会把名字截断到 31 字符，
# 所以匹配词要短。用户可用 --keyword 覆盖。
DEFAULT_KEYWORDS = ("hands-free", "handsfree", "免提")
OUT_DIR = _REPO_ROOT / "docs" / "fixtures" / "hfp_spike"


def _hostapi_names() -> list[str]:
    import sounddevice as sd

    return [str(api["name"]) for api in sd.query_hostapis()]


def _candidates(keywords: tuple[str, ...]) -> list[dict]:
    """所有名字命中关键词的设备，带 host API 与能力信息。"""
    import sounddevice as sd

    apis = _hostapi_names()
    hits = []
    for idx, dev in enumerate(sd.query_devices()):
        name = str(dev.get("name", ""))
        if not any(k in name.lower() for k in keywords):
            continue
        hits.append(
            {
                "index": idx,
                "name": name,
                "hostapi": apis[int(dev["hostapi"])],
                "max_input_channels": int(dev.get("max_input_channels", 0)),
                "max_output_channels": int(dev.get("max_output_channels", 0)),
                "default_samplerate": float(dev.get("default_samplerate", 0)),
            }
        )
    return hits


def _probe_rates(idx: int, kind: str) -> list[int]:
    """探测该端点实际接受哪些采样率（决定要不要给 audio_bridge 加重采样）。"""
    import sounddevice as sd

    ok = []
    for rate in (8000, 16000, 24000, 44100, 48000):
        try:
            if kind == "input":
                sd.check_input_settings(
                    device=idx, samplerate=rate, channels=1, dtype="int16"
                )
            else:
                sd.check_output_settings(
                    device=idx, samplerate=rate, channels=1, dtype="int16"
                )
            ok.append(rate)
        except Exception:
            pass
    return ok


def cmd_list(args: argparse.Namespace) -> int:
    import sounddevice as sd

    keywords = tuple(k.lower() for k in (args.keyword or DEFAULT_KEYWORDS))
    print("=== 全部音频设备 ===")
    apis = _hostapi_names()
    for idx, dev in enumerate(sd.query_devices()):
        name = str(dev["name"])[:45]
        api = apis[int(dev["hostapi"])][:12]
        print(
            "[{:3d}] {:45s} api={:12s} in={:2d} out={:2d} rate={:.0f}".format(
                idx,
                name,
                api,
                int(dev["max_input_channels"]),
                int(dev["max_output_channels"]),
                float(dev["default_samplerate"]),
            )
        )

    hits = _candidates(keywords)
    print("\n=== HFP 候选（关键词 {}）===".format(keywords))
    if not hits:
        print("!! 没找到任何候选。检查：手机是否已配对、设备属性里")
        print("   「免提电话 / Hands-free Telephony」服务是否勾选。")
        print("   注意：多数 Windows 只在通话建立(SCO 起来)后端点才可用。")
        return 1
    for h in hits:
        h["accepts_rates_input"] = (
            _probe_rates(h["index"], "input") if h["max_input_channels"] else []
        )
        h["accepts_rates_output"] = (
            _probe_rates(h["index"], "output") if h["max_output_channels"] else []
        )
        print(json.dumps(h, ensure_ascii=False, indent=2))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / "audio_devices.json"
    report.write_text(
        json.dumps(
            {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "candidates": hits},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n已落盘: {}".format(report))
    return 0


def _pick(hits: list[dict], kind: str, prefer_api: str) -> dict | None:
    """挑端点：优先 WASAPI（名字不被截断、延迟低），否则取第一个。"""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    usable = [h for h in hits if h[key] > 0]
    if not usable:
        return None
    for h in usable:
        if prefer_api.lower() in h["hostapi"].lower():
            return h
    return usable[0]


def cmd_test(args: argparse.Namespace) -> int:
    import sounddevice as sd

    keywords = tuple(k.lower() for k in (args.keyword or DEFAULT_KEYWORDS))
    hits = _candidates(keywords)
    if not hits:
        print("!! 没找到 HFP 端点。必须在通话已接通时运行本命令。")
        return 1

    rx = _pick(hits, "input", args.hostapi)
    tx = _pick(hits, "output", args.hostapi)
    if rx is None or tx is None:
        print("!! 输入/输出端点不成对: rx={} tx={}".format(rx, tx))
        return 1

    # 采样率：先信设备自报，失败再退 8000/16000。这一项的结果决定
    # audio_bridge.MODEM_RATE=8000 写死能不能直接用。
    rates: list[int] = []
    for r in (int(rx["default_samplerate"]), 8000, 16000):
        if r and r not in rates:
            rates.append(r)

    rate = None
    last_err = None
    for r in rates:
        try:
            sd.check_input_settings(
                device=rx["index"], samplerate=r, channels=1, dtype="int16"
            )
            sd.check_output_settings(
                device=tx["index"], samplerate=r, channels=1, dtype="int16"
            )
            rate = r
            break
        except Exception as exc:
            last_err = exc
    if rate is None:
        print("!! 输入输出找不到共同采样率，最后错误: {}".format(last_err))
        return 1

    block = int(rate * 0.02)  # 20ms，与 audio_bridge.MODEM_BLOCK_MS 一致
    print("RX [{}] {}  api={}".format(rx["index"], rx["name"], rx["hostapi"]))
    print("TX [{}] {}  api={}".format(tx["index"], tx["name"], tx["hostapi"]))
    print("采样率={} 块={} 采样 ({:.0f}ms)".format(rate, block, block / rate * 1000))
    print("录 {}s；第 {}s 注入 DTMF {!r}\n".format(args.seconds, args.inject_at, args.dtmf))

    in_stream = sd.RawInputStream(
        samplerate=rate,
        blocksize=block,
        dtype="int16",
        channels=1,
        device=rx["index"],
    )
    out_stream = sd.RawOutputStream(
        samplerate=rate,
        blocksize=block,
        dtype="int16",
        channels=1,
        device=tx["index"],
    )

    captured: list[bytes] = []
    stop = threading.Event()
    stats = {"overflows": 0}

    def reader() -> None:
        while not stop.is_set():
            try:
                data, over = in_stream.read(block)
                if over:
                    stats["overflows"] += 1
                captured.append(bytes(data))
            except Exception as exc:  # 端点掉线（SCO 断）会走到这
                print("[reader] 读失败: {}".format(exc))
                stop.set()
                return

    in_stream.start()
    out_stream.start()
    t0 = time.monotonic()
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    # 下行持续喂静音，到点把 DTMF 换进去——模拟真实 bridge 的连续送流。
    tone = dtmf_tone(args.dtmf, rate) if args.dtmf else b""
    silence = b"\x00\x00" * block
    injected = False
    inject_mark = None
    try:
        while time.monotonic() - t0 < args.seconds:
            elapsed = time.monotonic() - t0
            if not injected and elapsed >= args.inject_at and tone:
                inject_mark = len(captured) * block
                for off in range(0, len(tone), block * 2):
                    out_stream.write(tone[off : off + block * 2])
                injected = True
                print(
                    "[{:.1f}s] 已注入 DTMF {!r} ({} 字节)".format(
                        elapsed, args.dtmf, len(tone)
                    )
                )
                continue
            out_stream.write(silence)
    except Exception as exc:
        print("[writer] 写失败: {}".format(exc))
    finally:
        stop.set()
        thread.join(timeout=2)
        for s in (in_stream, out_stream):
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    pcm = b"".join(captured)
    if not pcm:
        print("!! 一个采样都没读到——端点开了但没有数据流（SCO 可能没建立）")
        return 1

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(samples**2)))
    dbfs = 20 * math.log10(rms / 32768.0) if rms > 0 else -999.0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = OUT_DIR / "uplink_capture.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)

    # 注入前后的响度对比：IVR 收到 DTMF 会换菜单，内容变化常伴随能量变化。
    before_rms = after_rms = None
    if inject_mark and 0 < inject_mark < len(samples):
        before = samples[:inject_mark]
        after = samples[inject_mark:]
        if before.size and after.size:
            before_rms = float(np.sqrt(np.mean(before**2)))
            after_rms = float(np.sqrt(np.mean(after**2)))

    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rx_device": rx,
        "tx_device": tx,
        "samplerate": rate,
        "seconds_captured": round(len(samples) / rate, 2),
        "input_overflows": stats["overflows"],
        "uplink_peak": peak,
        "uplink_rms": round(rms, 1),
        "uplink_dbfs": round(dbfs, 1),
        "dtmf_injected": injected,
        "dtmf_digit": args.dtmf if injected else None,
        "rms_before_inject": round(before_rms, 1) if before_rms else None,
        "rms_after_inject": round(after_rms, 1) if after_rms else None,
        "wav": str(wav_path),
    }
    (OUT_DIR / "audio_test.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== 结果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n=== 判据 ===")
    ok_rx = peak > 500
    print("上行有声音 (peak>500): {}  peak={:.0f}".format("PASS" if ok_rx else "FAIL", peak))
    over = stats["overflows"]
    print("无溢出          : {}".format("PASS" if over == 0 else "WARN x{}".format(over)))
    print("下行写入未报错  : {}".format("PASS" if injected else "SKIP/FAIL"))
    print("\n听一下 {} 确认录到的是对端的声音；".format(wav_path.name))
    print("若 IVR 在注入 DTMF 后切了菜单，下行链路即证实可用。")
    return 0 if ok_rx else 1


def main() -> int:
    # Windows 控制台默认 cp1252，中文输出会 UnicodeEncodeError（与
    # scripts/ec20_record_test.py 同一处理）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="枚举设备并挑出 HFP 候选")
    pl.add_argument("--keyword", nargs="*", help="匹配词，默认 {}".format(DEFAULT_KEYWORDS))
    pl.set_defaults(func=cmd_list)

    pt = sub.add_parser("test", help="通话中：录上行 + 注入 DTMF 验下行")
    pt.add_argument("--keyword", nargs="*", help="匹配词，默认 {}".format(DEFAULT_KEYWORDS))
    pt.add_argument("--seconds", type=float, default=10.0, help="录制时长")
    pt.add_argument("--inject-at", type=float, default=3.0, help="第几秒注入 DTMF")
    pt.add_argument("--dtmf", default="1", help="注入的按键，空串则不注入")
    pt.add_argument("--hostapi", default="WASAPI", help="优先的 host API")
    pt.set_defaults(func=cmd_test)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
