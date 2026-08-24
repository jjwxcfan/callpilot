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
    # WDM-KS 垫底：能用（回调式），但限制多；WASAPI/MME 端点若在（通话中可能
    # 出现）优先。
    order = ("wasapi", "mme", "directsound", "wdm-ks")

    def rank(h: dict) -> int:
        api = h["hostapi"].lower()
        if prefer_api.lower() in api:
            return -1
        for i, name in enumerate(order):
            if name in api:
                return i
        return len(order)

    return sorted(usable, key=rank)[0]


def _refresh_devices() -> None:
    """强制 PortAudio 重新枚举设备。

    sounddevice 在进程内缓存设备表，SCO 链路建立后新出现的 HFP 端点不会自己
    冒出来——轮询等待必须先重来一遍，否则等到天荒地老也看不见。
    """
    import sounddevice as sd

    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def _resolve_endpoint(
    keywords: tuple[str, ...], hostapi: str
) -> tuple[dict, dict, int] | None:
    """找到成对且能开流的 HFP 输入/输出端点，返回 (rx, tx, rate)；找不到返回 None。"""
    import sounddevice as sd

    hits = _candidates(keywords)
    if not hits:
        return None
    rx = _pick(hits, "input", hostapi)
    tx = _pick(hits, "output", hostapi)
    if rx is None or tx is None:
        return None

    # 采样率：先信设备自报，失败再退 8000/16000。这一项的结果决定
    # audio_bridge.MODEM_RATE=8000 写死能不能直接用。
    rates: list[int] = []
    for r in (int(rx["default_samplerate"]), 8000, 16000):
        if r and r not in rates:
            rates.append(r)
    for r in rates:
        try:
            sd.check_input_settings(
                device=rx["index"], samplerate=r, channels=1, dtype="int16"
            )
            sd.check_output_settings(
                device=tx["index"], samplerate=r, channels=1, dtype="int16"
            )
            return rx, tx, r
        except Exception:
            continue
    return None


def cmd_test(args: argparse.Namespace) -> int:
    import sounddevice as sd

    keywords = tuple(k.lower() for k in (args.keyword or DEFAULT_KEYWORDS))

    # 真机实测（2026-08-21）：HFP 端点可能只以 WDM-KS 形态出现，而 PortAudio 的
    # WDM-KS 后端不支持阻塞式 read/write（'Blocking API not supported yet',
    # PaErrorCode -9999）。回调式在全部 host API 下都可用，统一走回调 + 缓冲。
    captured: list[bytes] = []
    stats = {"overflows": 0}
    tx_lock = threading.Lock()
    tx_pending = bytearray()  # 待送下行的 PCM；空则回调自动补静音

    def in_callback(indata, frames, time_info, status) -> None:
        if status:
            stats["overflows"] += 1
        captured.append(bytes(indata))

    def out_callback(outdata, frames, time_info, status) -> None:
        need = len(outdata)
        with tx_lock:
            chunk = bytes(tx_pending[:need])
            del tx_pending[:need]
        outdata[: len(chunk)] = chunk
        if len(chunk) < need:
            outdata[len(chunk):] = b"\x00" * (need - len(chunk))

    last_error: dict = {"exc": None}

    def _try_start(resolved: tuple[dict, dict, int]):
        """开并启动双向流；成功返回 (in_stream, out_stream)，失败返回 None。

        端点存在 ≠ 能开流：手机一连蓝牙端点就在了，但 KS pin 要 SCO（通话
        接通）后才实例化得出来——真机实测没在通话时 start() 报
        'WdmSyncIoctl: DeviceIoControl GLE = 0x00000001'。失败就继续等。
        """
        rx, tx, rate = resolved
        block = int(rate * 0.02)
        streams = []
        try:
            ins = sd.RawInputStream(
                samplerate=rate, blocksize=block, dtype="int16", channels=1,
                device=rx["index"], callback=in_callback,
            )
            streams.append(ins)
            outs = sd.RawOutputStream(
                samplerate=rate, blocksize=block, dtype="int16", channels=1,
                device=tx["index"], callback=out_callback,
            )
            streams.append(outs)
            ins.start()
            outs.start()
            return ins, outs
        except Exception as exc:
            last_error["exc"] = exc
            for s in streams:
                try:
                    s.abort()
                    s.close()
                except Exception:
                    pass
            return None

    # 把「试开流」本身放进等待循环：先开脚本，再拨号，接通瞬间自动开始。
    print("等待通话接通（最多 {:.0f}s）——现在去手机上拨 611。".format(args.wait))
    deadline = time.monotonic() + max(args.wait, 1.0)
    opened = None
    resolved = None
    waited = 0
    while time.monotonic() < deadline:
        resolved = _resolve_endpoint(keywords, args.hostapi)
        if resolved is not None:
            opened = _try_start(resolved)
            if opened is not None:
                break
        time.sleep(1.0)
        waited += 1
        print("  ...{}s".format(waited), end="\r", flush=True)
        # 注意：开流成功后绝不能再 _refresh_devices()（会把活流拆了）。
        _refresh_devices()
    if opened is None:
        print("\n!! 等待超时，流没能启动。")
        if resolved is not None:
            print("   端点找到了（{}），但开流失败：{}".format(
                resolved[0]["name"], last_error["exc"]))
            print("   - 通话真的接通了吗？端点存在但 SCO 没起来就会这样。")
            print("   - 手机通话界面里把音频输出切到这台电脑（蓝牙）。")
        else:
            print("   - 没找到 HFP 端点。先 `list` 看设备名，必要时 --keyword 覆盖。")
            print("   - 检查手机蓝牙已连接、且「免提电话」服务勾选。")
        return 1
    in_stream, out_stream = opened
    rx, tx, rate = resolved
    block = int(rate * 0.02)  # 20ms，与 audio_bridge.MODEM_BLOCK_MS 一致

    print("\n流已启动，采集中：")
    print("RX [{}] {}  api={}".format(rx["index"], rx["name"], rx["hostapi"]))
    print("TX [{}] {}  api={}".format(tx["index"], tx["name"], tx["hostapi"]))
    print("↑ 确认括号里是目标手机；不是就 Ctrl-C 后用 --keyword 指定（如 --keyword pixel）。")
    print("采样率={} 块={} 采样 ({:.0f}ms)".format(rate, block, block / rate * 1000))
    print("录 {}s；第 {}s 注入 DTMF {!r}\n".format(args.seconds, args.inject_at, args.dtmf))

    tone = dtmf_tone(args.dtmf, rate) if args.dtmf else b""
    injected = False
    inject_mark = None
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds:
            elapsed = time.monotonic() - t0
            if not injected and elapsed >= args.inject_at and tone:
                inject_mark = len(captured) * block
                with tx_lock:
                    tx_pending.extend(tone)
                injected = True
                print(
                    "[{:.1f}s] 已注入 DTMF {!r} ({} 字节)".format(
                        elapsed, args.dtmf, len(tone)
                    )
                )
            time.sleep(0.05)
    finally:
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
    pt.add_argument("--wait", type=float, default=120.0,
                    help="等端点就绪的秒数（先开脚本再拨号，0=不等）")
    pt.set_defaults(func=cmd_test)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
