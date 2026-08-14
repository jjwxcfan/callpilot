"""Runtime helper tests for the bundled EC20 USB bridge."""

from __future__ import annotations

import pytest

pytest.importorskip("fcntl", reason="EC20 PTY bridge is POSIX-only")

from scripts import ec20_usb_pty


def test_bundled_libusb_path_uses_pyinstaller_resources(tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "libusb-1.0.0.dylib"
    lib.parent.mkdir()
    lib.write_bytes(b"placeholder")

    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() == lib


def test_bundled_libusb_path_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() is None


def test_sim7600_wrapper_defaults_match_phase0_probe():
    """SIM7600 桥的 VID/PID 与接口映射来自 Phase 0 真机探测，定死防漂移。"""
    from scripts import sim7600_usb_pty

    assert sim7600_usb_pty.SIM7600_VID == 0x1E0E
    assert sim7600_usb_pty.SIM7600_PID == 0x9001
    # interface 2 = AT 口，interface 4 = PCM 音频口（8kHz/16-bit/mono）。
    assert sim7600_usb_pty.DEFAULT_MAPS == ["2:/tmp/sim7600-at", "4:/tmp/sim7600-pcm"]


def test_ec20_bridge_defaults_unchanged():
    """回归护栏：Quectel 桥默认 VID/PID 不因 SIM7600 参数化而改动。"""
    assert ec20_usb_pty.VID == 0x2C7C
    assert ec20_usb_pty.PID == 0x0125


# ---- PCM 卡死自动恢复（WIL-109）----

def test_degraded_flag_defaults_off():
    """只有「设备在总线上却不排空端点」才置 degraded，真拔线不触发恢复。"""
    handle = ec20_usb_pty.BridgeHandle(
        dev=None, port=None, link="/tmp/x", master_fd=-1, slave_fd=-1,
        stop=__import__("threading").Event(),
    )
    assert handle.degraded is False


def test_usb_recovery_switches_out_and_back_then_verifies(monkeypatch):
    """恢复流程：切到备用组合 → 切回目标组合 → 回读校验通过才算成功。"""
    sent = []
    state = {"pid": "9001"}

    class _Port:
        interface = 2

    def fake_find(vid, timeout=60.0):
        return object(), _Port()

    def fake_at(dev, port, cmd, wait=1.5, rounds=15):
        sent.append(cmd)
        if cmd == "AT+CUSBPIDSWITCH?":
            return f"+CUSBPIDSWITCH: {state['pid']}\r\nOK\r\n"
        if cmd.startswith("AT+CUSBPIDSWITCH="):
            state["pid"] = cmd.split("=")[1].split(",")[0]
            return "OK\r\n"
        return "OK\r\n"

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic", fake_find)
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface", fake_at)
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda d: None)

    assert ec20_usb_pty.usb_composition_recovery(0x1E0E, 0x9001, 0x9011, settle=0) is True
    switches = [c for c in sent if c.startswith("AT+CUSBPIDSWITCH=")]
    assert switches == ["AT+CUSBPIDSWITCH=9011,1,1", "AT+CUSBPIDSWITCH=9001,1,1"]
    assert sent[-1] == "AT+CUSBPIDSWITCH?"          # 最后必须回读校验


def test_usb_recovery_retries_when_switch_back_fails(monkeypatch):
    """切回失败滞留备用组合时（真机踩过）必须重试，不能当成功返回。"""
    state = {"pid": "9001", "reject_first": True}

    class _Port:
        interface = 4

    def fake_at(dev, port, cmd, wait=1.5, rounds=15):
        if cmd == "AT+CUSBPIDSWITCH?":
            return f"+CUSBPIDSWITCH: {state['pid']}\r\nOK\r\n"
        if cmd.startswith("AT+CUSBPIDSWITCH="):
            target = cmd.split("=")[1].split(",")[0]
            if target == "9001" and state["reject_first"]:
                state["reject_first"] = False      # 模拟发太早被拒，仍停在 9011
                return "ERROR\r\n"
            state["pid"] = target
            return "OK\r\n"
        return "OK\r\n"

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic",
                        lambda vid, timeout=60.0: (object(), _Port()))
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface", fake_at)
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda d: None)

    assert ec20_usb_pty.usb_composition_recovery(0x1E0E, 0x9001, 0x9011, settle=0) is True
    assert state["pid"] == "9001"                  # 重试后确实回到目标组合


def test_usb_recovery_gives_up_and_reports_failure(monkeypatch):
    """始终切不回时返回 False（上层据此告警，提示人工断电）。"""
    class _Port:
        interface = 2

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic",
                        lambda vid, timeout=60.0: (object(), _Port()))
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface",
                        lambda d, p, cmd, wait=1.5, rounds=15:
                        "+CUSBPIDSWITCH: 9011\r\nOK\r\n" if cmd.endswith("?") else "ERROR\r\n")
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda d: None)

    assert ec20_usb_pty.usb_composition_recovery(
        0x1E0E, 0x9001, 0x9011, attempts=2, settle=0) is False
