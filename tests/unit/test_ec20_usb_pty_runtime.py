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
