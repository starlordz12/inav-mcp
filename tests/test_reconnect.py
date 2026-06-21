"""Tests for the re-enumeration-aware reconnect helpers — offline, pure functions.

After a reboot the FC may come back on the same port, a DIFFERENT COM port, or
not at all (dropped into the STM32 DFU/bootloader, needing a power-cycle). These
cover the pure port-classification logic; the serial I/O of reconnect() itself
needs hardware and is exercised on the bench.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.connection import port_in_dfu, fc_serial_candidates


def _p(device, description="", hwid=""):
    return {"device": device, "description": description, "hwid": hwid}


# ── port_in_dfu ─────────────────────────────────────────────────────────────────

def test_dfu_detected_by_description():
    ports = [_p("COM7", "STM32 BOOTLOADER", "USB VID:PID=0483:DF11")]
    hit = port_in_dfu(ports)
    assert hit is not None and hit["device"] == "COM7"


def test_dfu_detected_by_pid_only():
    # Description blank but the DF11 PID gives it away.
    ports = [_p("COM7", "", "USB\\VID_0483&PID_DF11")]
    assert port_in_dfu(ports) is not None


def test_normal_vcp_is_not_dfu():
    # iNAV's running VCP is 0483:5740 — must not be mistaken for a bootloader.
    ports = [
        _p("COM3", "STM32 Virtual COM Port", "USB VID:PID=0483:5740"),
        _p("COM1", "Communications Port", "ACPI\\PNP0501"),
    ]
    assert port_in_dfu(ports) is None


# ── fc_serial_candidates ────────────────────────────────────────────────────────

def test_candidates_original_port_first():
    ports = [_p("COM3", "STM32 Virtual COM Port", "USB VID:PID=0483:5740")]
    cands = fc_serial_candidates(ports, "COM3")
    assert cands[0] == "COM3"


def test_candidates_include_reenumerated_port():
    # FC came back as COM9 instead of the original COM3.
    ports = [
        _p("COM1", "Communications Port", "ACPI\\PNP0501"),
        _p("COM9", "USB Serial Device (STM32)", "USB VID:PID=0483:5740"),
    ]
    cands = fc_serial_candidates(ports, "COM3")
    assert cands[0] == "COM3"          # always try the original first
    assert "COM9" in cands             # then the re-enumerated USB-serial port
    assert "COM1" not in cands         # non-USB-serial port is not a candidate


def test_candidates_exclude_dfu_device():
    # A DFU device must never be probed for MSP.
    ports = [_p("COM7", "STM32 BOOTLOADER", "USB VID:PID=0483:DF11")]
    cands = fc_serial_candidates(ports, "COM3")
    assert cands == ["COM3"], "DFU device must be excluded from MSP candidates"


def test_candidates_no_duplicate_original():
    ports = [_p("COM3", "STM32 Virtual COM Port", "USB VID:PID=0483:5740")]
    cands = fc_serial_candidates(ports, "COM3")
    assert cands.count("COM3") == 1


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
