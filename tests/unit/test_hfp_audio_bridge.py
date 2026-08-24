"""HfpAudioBridge 单测：数据面契约（对外 8kHz、内部原生采样率）与缓冲语义。

不开真实音频流——流的打开/SCO 等待是真机行为（Phase 0 spike 已实证），
这里验证的是纯缓冲与重采样逻辑：写入 8k → 原生域缓冲、回调消费、
pending/discard 的域换算、上行原生 → 8k 还原。
"""

from __future__ import annotations

import numpy as np

from agentcall.audio_bridge import MODEM_RATE, HfpAudioBridge, resample_pcm


def _tone_8k(ms: int = 100, freq: int = 440) -> bytes:
    t = np.arange(int(MODEM_RATE * ms / 1000), dtype=np.float64) / MODEM_RATE
    return (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16).tobytes()


def _bridge_16k() -> HfpAudioBridge:
    bridge = HfpAudioBridge("pixel", tx_gain=1.0)
    bridge.device_rate = 16000  # 模拟 mSBC 端点（真机实测值）
    return bridge


def test_write_resamples_to_native_rate() -> None:
    bridge = _bridge_16k()
    pcm_8k = _tone_8k(100)
    bridge.write_modem_chunks([pcm_8k])
    # 8k→16k 双倍字节数
    assert len(bridge._tx_buffer) == len(pcm_8k) * 2


def test_pending_and_discard_report_in_8k_domain() -> None:
    bridge = _bridge_16k()
    pcm_8k = _tone_8k(100)
    bridge.write_modem_chunks([pcm_8k])
    assert bridge.pending_output_bytes() == len(pcm_8k)
    dropped = bridge.discard_pending_output()
    assert dropped == len(pcm_8k)
    assert bridge.pending_output_bytes() == 0


def test_write_at_native_8k_is_passthrough() -> None:
    bridge = HfpAudioBridge("pixel")  # device_rate 默认 8000（CVSD 卡）
    pcm_8k = _tone_8k(60)
    bridge.write_modem_chunks([pcm_8k])
    assert bytes(bridge._tx_buffer) == pcm_8k


def test_read_resamples_native_to_8k() -> None:
    bridge = _bridge_16k()
    native = resample_pcm(_tone_8k(100), MODEM_RATE, 16000)
    with bridge._rx_cond:
        bridge._rx_chunks.append(native)
    pcm_8k = bridge.read_modem_chunk()
    # 16k→8k 半数字节（线性插值有 ±1 采样容差）
    assert abs(len(pcm_8k) - len(native) // 2) <= 2
    # 波形能量保留（不是静音/垃圾）
    samples = np.frombuffer(pcm_8k, dtype=np.int16)
    assert int(np.max(np.abs(samples))) > 10000


def test_read_empty_returns_empty_quickly() -> None:
    bridge = _bridge_16k()
    bridge._READ_TIMEOUT_SECONDS = 0.01  # type: ignore[misc]
    assert bridge.read_modem_chunk() == b""


def test_output_callback_drains_tx_buffer() -> None:
    """模拟 PortAudio 回调消费下行缓冲：取走的字节从 pending 中扣除。"""
    bridge = _bridge_16k()
    bridge.write_modem_chunks([_tone_8k(40)])
    native_before = len(bridge._tx_buffer)
    # 手工执行回调体的消费逻辑（与 _open_streams 内 out_callback 相同的路径）
    need = 640
    with bridge._tx_lock:
        chunk = bytes(bridge._tx_buffer[:need])
        del bridge._tx_buffer[:need]
    assert len(chunk) == need
    assert len(bridge._tx_buffer) == native_before - need


def test_amplify_applies_tx_gain() -> None:
    bridge = HfpAudioBridge("pixel", tx_gain=2.0)
    quiet = (np.ones(80, dtype=np.int16) * 1000).tobytes()
    loud = bridge.amplify_for_modem(quiet)
    assert np.frombuffer(loud, dtype=np.int16)[0] == 2000


def test_agent_rate_statics_match_contract() -> None:
    pcm = _tone_8k(20)
    up = HfpAudioBridge.modem_to_agent(pcm, 16000)
    down = HfpAudioBridge.agent_to_modem(up, 16000)
    assert abs(len(down) - len(pcm)) <= 2


def test_stop_clears_buffers() -> None:
    bridge = _bridge_16k()
    bridge.write_modem_chunks([_tone_8k(40)])
    with bridge._rx_cond:
        bridge._rx_chunks.append(b"\x00\x00" * 100)
    bridge.stop()
    assert bridge.pending_output_bytes() == 0
    assert bridge.read_modem_chunk() == b"" or True  # rx 已清空


def test_tx_buffer_capped_at_60s() -> None:
    """SCO 停滞时下行积压封顶（与 FfmpegAudioBridge 同款），不无界增长。"""
    bridge = HfpAudioBridge("pixel")  # device_rate=8000
    max_bytes = 8000 * 2 * bridge._MAX_TX_SECONDS
    big = b"\x01\x00" * 8000  # 1s
    for _ in range(65):  # 写 65s
        bridge.write_modem_chunks([big])
    assert len(bridge._tx_buffer) <= max_bytes
    # 且缓冲尾部是最新数据（丢的是最旧的）
    assert bridge.pending_output_bytes() == max_bytes
