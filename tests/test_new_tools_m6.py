"""Tests for M6 tools: motor test, calibration, failsafe, list_backups, and the
firmware-version guard on arming-flag decoding. All offline (mock connection).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server
from inav_mcp.msp import firmware_major, encode_motor_values, MSP_SET_MOTOR, MSP_ACC_CALIBRATION
from inav_mcp.cli import parse_get_output
from inav_mcp.troubleshoot import firmware_calibration_status
from inav_mcp.safety import next_backup_path


# ── firmware version guard ─────────────────────────────────────────────────────

def test_firmware_major():
    assert firmware_major("6.1.0") == 6
    assert firmware_major("9.0.2") == 9
    assert firmware_major(None) is None
    assert firmware_major("garbage") is None


def test_firmware_calibration_out_of_range_warns():
    cal = firmware_calibration_status("6.1.0")
    assert cal["calibrated"] is False
    assert cal["fw_major"] == 6
    assert "warning" in cal and "6.1.0" in cal["warning"]


def test_firmware_calibration_in_range_ok():
    cal = firmware_calibration_status("9.0.2")
    assert cal["calibrated"] is True
    assert "warning" not in cal


def test_firmware_calibration_unknown_does_not_warn():
    cal = firmware_calibration_status(None)
    assert cal["calibrated"] is True   # can't tell → don't cry wolf


# ── motor payload encoding ──────────────────────────────────────────────────────

def test_encode_motor_values_length_and_clamp():
    payload = encode_motor_values([2500, 1100])   # 2500 over-range, rest default
    assert len(payload) == 16                      # 8 × u16
    import struct
    vals = list(struct.unpack("<8H", payload))
    assert vals[0] == 2000                          # clamped down
    assert vals[1] == 1100
    assert vals[2:] == [1000] * 6                    # padded with stop


# ── CLI get-output parsing ──────────────────────────────────────────────────────

def test_parse_get_output():
    text = ("failsafe_procedure = RTH\r\n"
            "failsafe_throttle = 1200\r\n"
            "# a comment\r\n"
            "### ERROR: nope\r\n"
            "failsafe_delay = 5\r\n")
    out = parse_get_output(text)
    assert out["failsafe_procedure"] == "RTH"
    assert out["failsafe_throttle"] == "1200"
    assert out["failsafe_delay"] == "5"
    assert "### ERROR" not in " ".join(out.keys())


# ── motor test gating (FakeConn records MSP sends) ──────────────────────────────

class FakeMotorConn:
    """Records send_msp_v1 calls. No send_msp_v2 → check_not_armed passes (best-effort)."""
    def __init__(self):
        self.sent = []   # (cmd, payload)
    def is_open(self):
        return True
    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        self.sent.append((cmd, bytes(payload)))
        return b""

    def _motor_sends(self):
        return [p for (c, p) in self.sent if c == MSP_SET_MOTOR]


def _with_conn(conn, fn):
    state.set_connection(conn)
    try:
        return fn()
    finally:
        state.set_connection(None)


def test_test_motor_refuses_without_props():
    conn = FakeMotorConn()
    out = _with_conn(conn, lambda: server.test_motor(1, confirm=True))   # props_removed defaults False
    assert out["ran"] is False
    assert "props_removed" in out["error"]
    assert conn._motor_sends() == [], "must not command any motor without props_removed"


def test_test_motor_dry_run():
    conn = FakeMotorConn()
    out = _with_conn(conn, lambda: server.test_motor(1, props_removed=True, confirm=False))
    assert out.get("dry_run") is True
    assert conn._motor_sends() == []


def test_test_motor_runs_and_always_stops():
    conn = FakeMotorConn()
    out = _with_conn(
        conn,
        lambda: server.test_motor(2, throttle_us=1200, duration_s=0.2,
                                  props_removed=True, confirm=True),
    )
    assert out["ran"] is True and out["stopped"] is True

    expected_test = encode_motor_values([1000, 1200, 1000, 1000, 1000, 1000, 1000, 1000])
    expected_stop = encode_motor_values([1000] * 8)

    sends = conn._motor_sends()
    assert expected_test in sends, "should have driven motor 2 to 1200 µs"
    assert sends[-1] == expected_stop, "the LAST motor command must be stop (1000 µs)"


def test_test_motor_bad_index():
    conn = FakeMotorConn()
    out = _with_conn(conn, lambda: server.test_motor(99, props_removed=True, confirm=True))
    assert "error" in out and conn._motor_sends() == []


# ── accelerometer calibration gating ────────────────────────────────────────────

def test_calibrate_accel_dry_run():
    conn = FakeMotorConn()
    out = _with_conn(conn, lambda: server.calibrate_accelerometer(confirm=False))
    assert out["calibrated"] is False and out.get("dry_run") is True
    assert conn.sent == []


def test_calibrate_accel_sends_command():
    conn = FakeMotorConn()
    out = _with_conn(conn, lambda: server.calibrate_accelerometer(confirm=True))
    assert out["calibrated"] is True
    assert any(c == MSP_ACC_CALIBRATION for (c, _) in conn.sent)


# ── check_failsafe (CLI mock) ───────────────────────────────────────────────────

class FakeCliConn:
    def __init__(self, reply):
        self._reply = reply
        self.mode = "MSP"
    def is_open(self):
        return True
    def enter_cli(self, timeout=5.0):
        self.mode = "CLI"
        return ""
    def run_cli(self, cmd, timeout=15.0):
        return self._reply
    def exit_cli(self, save=False, reconnect=False):
        self.mode = "MSP"


def test_check_failsafe_flags_rth_without_gps():
    conn = FakeCliConn("failsafe_procedure = RTH\nfailsafe_throttle = 1000\n")
    state.set_board_info({"sensors_present": {"gps": False}})
    state.set_connection(conn)
    try:
        out = server.check_failsafe()
    finally:
        state.set_connection(None)
        state.set_board_info(None)
    assert out["procedure"] == "RTH"
    assert out["status"] == "review"
    assert any("RTH" in w and "GPS" in w for w in out["warnings"])


def test_check_failsafe_flags_none():
    conn = FakeCliConn("failsafe_procedure = NONE\n")
    state.set_connection(conn)
    try:
        out = server.check_failsafe()
    finally:
        state.set_connection(None)
    assert out["procedure"] == "NONE"
    assert any("NOTHING" in w.upper() for w in out["warnings"])


# ── list_backups ────────────────────────────────────────────────────────────────

def test_list_backups_finds_a_written_file():
    path = next_backup_path("unittest_marker")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# test backup\nset foo = 1\n")
    try:
        out = server.list_backups()
        names = [b["filename"] for b in out["backups"]]
        assert os.path.basename(path) in names
        mine = next(b for b in out["backups"] if b["filename"] == os.path.basename(path))
        assert mine["label"] == "unittest_marker"
        assert mine["lines"] == 2
    finally:
        os.remove(path)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
