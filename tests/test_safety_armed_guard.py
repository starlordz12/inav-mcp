"""S1 — the armed guard must FAIL CLOSED (offline).

`check_not_armed()` currently returns cleanly when neither MSPV2_INAV_STATUS nor
legacy MSP_STATUS can be read, so a caller proceeds without ever establishing
whether the FC is armed. These tests pin the required behaviour:

  - a status read that positively reports DISARMED  → proceed,
  - a status read that reports ARMED                → refuse,
  - no read that can establish either               → refuse.

The second half asserts the three safety-sensitive callers — test_motor(), the
raw cli() write path, and cli_batch() — perform NO serial I/O at all when armed
state cannot be established.
"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server
from inav_mcp.msp import (
    MSPV2_INAV_STATUS, MSP_STATUS, MSP_SET_MOTOR,
    ARMING_FLAG_ARMED, SENSOR_BITS,
)
from inav_mcp.safety import check_not_armed


def _v2_status(arming_flags=0):
    return (
        struct.pack("<H", 200)             # cycleTime
        + struct.pack("<H", 0)             # i2cErrors
        + struct.pack("<H", SENSOR_BITS["acc"])
        + struct.pack("<H", 10)            # cpuLoad
        + bytes([0])                       # packed profile
        + struct.pack("<I", arming_flags)  # armingFlags
        + bytes([0])                       # mixer profile
    )


def _v1_status(arming_flags=0):
    return (
        struct.pack("<H", 200)
        + struct.pack("<H", 0)
        + struct.pack("<H", 0)
        + struct.pack("<I", 0)
        + bytes([0])
        + struct.pack("<H", 10)
        + bytes([0])
        + struct.pack("<I", arming_flags)
    )


class RecordingConn:
    """Base mock: every status read fails, and all serial activity is recorded.

    Subclasses override the status answers. `enter_cli`/`run_cli`/`msp_writes`
    exist so a test can prove a refusal happened BEFORE any serial I/O.
    """
    def __init__(self):
        self.entered = False
        self.exit_saved = None
        self.cli_commands: list[str] = []
        self.msp_writes: list[tuple] = []

    def is_open(self):
        return True

    def enter_cli(self, timeout=5.0):
        self.entered = True
        return "Entering CLI Mode..."

    def run_cli(self, command, timeout=15.0):
        self.cli_commands.append(command)
        return ""

    def exit_cli(self, save=False, reconnect=False):
        self.exit_saved = save

    # ── status reads (overridden below) ──
    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        raise TimeoutError("no v2 reply")

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        if cmd != MSP_STATUS:
            self.msp_writes.append((cmd, payload))
        raise TimeoutError("no v1 reply")


class V2Conn(RecordingConn):
    """v2 status answers authoritatively."""
    def __init__(self, arming_flags=0):
        super().__init__()
        self._flags = arming_flags

    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        assert cmd == MSPV2_INAV_STATUS
        return _v2_status(self._flags)

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        if cmd == MSP_STATUS:
            raise AssertionError("v1 status must not be read when v2 answers")
        self.msp_writes.append((cmd, payload))
        return b""


class V1FallbackConn(RecordingConn):
    """Old firmware: v2 unsupported, legacy v1 status answers."""
    def __init__(self, arming_flags=0):
        super().__init__()
        self._flags = arming_flags

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        if cmd == MSP_STATUS:
            return _v1_status(self._flags)
        self.msp_writes.append((cmd, payload))
        return b""


class ShortV2Conn(V1FallbackConn):
    """v2 replies with a truncated (unparseable) payload; v1 may still answer."""
    def __init__(self, arming_flags=0, v1_works=True):
        super().__init__(arming_flags)
        self._v1_works = v1_works

    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        return b"\x00" * 12          # < 13 bytes → parse_inav_status returns {}

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        if cmd == MSP_STATUS and not self._v1_works:
            raise TimeoutError("no v1 reply")
        return super().send_msp_v1(cmd, payload, timeout)


class NoMspConn:
    """A handle exposing no MSP methods at all — e.g. the FC is stuck in CLI mode.

    Deliberately NOT a RecordingConn subclass: attribute lookup for send_msp_*
    must genuinely fail, which is how the current code reaches its fail-open path.
    """
    def __init__(self):
        self.entered = False
        self.cli_commands: list[str] = []

    def is_open(self):
        return True

    def enter_cli(self, timeout=5.0):
        self.entered = True
        return "Entering CLI Mode..."

    def run_cli(self, command, timeout=15.0):
        self.cli_commands.append(command)
        return ""

    def exit_cli(self, save=False, reconnect=False):
        pass


def _refuses(conn, why):
    try:
        check_not_armed(conn)
    except RuntimeError as exc:
        assert "Unable to verify" in str(exc) or "ARMED" in str(exc), \
            f"unhelpful refusal message: {exc}"
        return str(exc)
    raise AssertionError(f"check_not_armed must refuse: {why}")


# ── check_not_armed: the five required cases ──────────────────────────────────

def test_v2_disarmed_passes():
    check_not_armed(V2Conn(arming_flags=0))            # must not raise


def test_v2_armed_refuses():
    msg = _refuses(V2Conn(arming_flags=ARMING_FLAG_ARMED), "v2 reports ARMED")
    assert "ARMED" in msg


def test_v2_fails_and_v1_disarmed_passes():
    check_not_armed(V1FallbackConn(arming_flags=0))    # must not raise


def test_v2_fails_and_v1_armed_refuses():
    msg = _refuses(V1FallbackConn(arming_flags=ARMING_FLAG_ARMED), "v1 reports ARMED")
    assert "ARMED" in msg


def test_both_protocols_fail_refuses():
    # RED against current code: safety.py returns cleanly on the v1 exception path.
    msg = _refuses(RecordingConn(), "neither v2 nor v1 could be read")
    assert "Unable to verify" in msg


def test_disable_reason_is_not_armed():
    # A disable reason (RC_LINK, bit 18) is not the ARMED bit — must still pass.
    check_not_armed(V2Conn(arming_flags=1 << 18))


# ── unparseable / short payloads ──────────────────────────────────────────────

def test_short_v2_payload_and_no_v1_refuses():
    # RED: v2 parses to {}, v1 raises → current code returns cleanly.
    _refuses(ShortV2Conn(v1_works=False), "no protocol could establish armed state")


def test_short_v2_payload_falls_back_to_v1_disarmed():
    # Regression lock: a truncated v2 reply must not stop the working v1 fallback.
    check_not_armed(ShortV2Conn(arming_flags=0, v1_works=True))


def test_short_v2_payload_falls_back_to_v1_armed():
    _refuses(ShortV2Conn(arming_flags=ARMING_FLAG_ARMED, v1_works=True), "v1 reports ARMED")


def test_missing_msp_methods_refuses():
    # RED: an AttributeError is swallowed exactly like a timeout today.
    _refuses(NoMspConn(), "the handle cannot answer status at all")


# ── callers must not touch serial when armed state is unknown ─────────────────

def _with_conn(conn, fn):
    state.set_connection(conn)
    try:
        return fn()
    finally:
        state.set_connection(None)


def test_cli_write_does_no_serial_io_when_unverifiable():
    conn = RecordingConn()
    out = _with_conn(conn, lambda: server.cli("set failsafe_delay = 5",
                                              confirm_for_writes=True))
    assert conn.entered is False, "must refuse BEFORE entering CLI"
    assert conn.cli_commands == []
    assert "SAFETY" in str(out)


def test_cli_batch_does_no_serial_io_when_unverifiable():
    conn = RecordingConn()
    out = _with_conn(conn, lambda: server.cli_batch(["set failsafe_delay = 5"],
                                                    confirm_for_writes=True))
    assert conn.entered is False, "must refuse BEFORE entering CLI"
    assert conn.cli_commands == []
    assert "SAFETY" in str(out) or "error" in out


def test_motor_tool_sends_no_motor_command_when_unverifiable():
    conn = RecordingConn()
    out = _with_conn(conn, lambda: server.test_motor(
        motor=1, throttle_us=1100, duration_s=0.2,
        props_removed=True, confirm=True))
    motor_cmds = [c for c, _ in conn.msp_writes if c == MSP_SET_MOTOR]
    assert motor_cmds == [], "no motor output may be driven with armed state unknown"
    assert out["ran"] is False
    assert "SAFETY" in str(out.get("error", ""))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
