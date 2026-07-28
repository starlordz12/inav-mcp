"""S3 — CLI command separators and control characters must be rejected (offline).

`SerialConnection.run_cli()` appends '\\r' to whatever string it is given, so a
carriage return embedded in a tool argument introduces a SECOND CLI command that
never passed confirmation, backup, or the armed guard. Constrained parameters —
failsafe procedure, GPS provider, SBAS mode, RTH landing policy — are currently
interpolated with nothing more than `.strip().upper()`.

Two layers are required:
  1. a CENTRAL validator that every command passed to run_cli() goes through, and
  2. explicit enum validation of constrained tool arguments, before the command
     string is ever built.

Tool-level rejection must return a normal error and must NOT enter CLI.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server
from inav_mcp.cli import is_replayable
from inav_mcp.connection import SerialConnection
from tests.fake_serial import FakeSerial


# Separators and control characters that must never reach the wire.
INJECTIONS = ["\r", "\n", "\r\n", "\x00", "\x1b", "\x07"]

# A valid token followed by a smuggled second command.
SMUGGLED = [
    "RTH\rset motor_pwm_protocol = ONESHOT125",
    "RTH\nsave",
    "RTH\r\ndefaults",
    "RTH\x00save",
]


class NoSerialConn:
    """A connection that fails the test if any serial I/O is attempted."""
    def __init__(self):
        self.entered = False

    def is_open(self):
        return True

    def enter_cli(self, timeout=5.0):
        self.entered = True
        raise AssertionError("entered CLI on invalid input — validation must run first")

    def run_cli(self, command, timeout=15.0):
        raise AssertionError(f"ran CLI command on invalid input: {command!r}")

    def exit_cli(self, save=False, reconnect=False):
        raise AssertionError("exited CLI on invalid input")

    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        raise TimeoutError("status not available")

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        raise TimeoutError("status not available")


def _call(fn, **kwargs):
    """Invoke a write tool in dry-run and return its result dict."""
    conn = NoSerialConn()
    state.set_connection(conn)
    try:
        return fn(**kwargs)
    finally:
        state.set_connection(None)


def _assert_rejected(result, needle):
    """A tool must report an error and must not surface the injected payload."""
    assert isinstance(result, dict), f"expected a dict result, got {type(result)}"
    assert result.get("error"), f"expected an error, got {result}"
    blob = repr(result)
    # Case-insensitive: the tools .upper() their arguments, so a smuggled verb
    # would otherwise slip past a lowercase needle.
    assert needle.lower() not in blob.lower(), \
        f"injected payload leaked into the result: {blob}"
    for ch in ("\\r", "\\n", "\\x00"):
        assert ch not in blob, f"control character survived into {blob}"


# ── 1. the central validator ──────────────────────────────────────────────────

def test_central_validator_rejects_control_characters():
    # RED: inav_mcp.cli.validate_cli_command() does not exist yet.
    from inav_mcp.cli import validate_cli_command
    for bad in INJECTIONS:
        cmd = f"set failsafe_procedure = RTH{bad}"
        try:
            validate_cli_command(cmd)
        except ValueError:
            continue
        raise AssertionError(f"validator accepted {cmd!r}")


def test_central_validator_accepts_normal_commands():
    from inav_mcp.cli import validate_cli_command
    for good in ["set failsafe_procedure = RTH", "diff all", "status",
                 "aux 0 0 0 1300 1700", "smix 0 1 0 100 0", "# comment"]:
        validate_cli_command(good)      # must not raise


def test_central_validator_rejects_leading_and_trailing_separators():
    from inav_mcp.cli import validate_cli_command
    for bad in ["\rsave", "save\r", "\nsave", "save\n"]:
        try:
            validate_cli_command(bad)
        except ValueError:
            continue
        raise AssertionError(f"validator accepted {bad!r}")


def test_run_cli_rejects_separators_before_writing_to_serial():
    # RED: run_cli() writes (cmd + '\r') with no validation at all.
    # The fake answers with a prompt so an unvalidated call returns immediately
    # instead of blocking for the full read timeout — the assertion below is
    # about what reached the wire, not about how the call ended.
    conn = SerialConnection("FAKE")
    conn._ser = FakeSerial([b"# "])
    conn.mode = "CLI"
    try:
        conn.run_cli("set failsafe_procedure = RTH\rsave")
    except ValueError:
        pass
    else:
        raise AssertionError("run_cli accepted an embedded carriage return")
    assert conn._ser.written == [], "nothing may be written for an invalid command"


# ── 2. constrained tool arguments ─────────────────────────────────────────────

def test_set_failsafe_rejects_smuggled_commands():
    # RED: the payload is interpolated straight into 'set failsafe_procedure = ...'.
    for payload in SMUGGLED:
        result = _call(server.set_failsafe, procedure=payload)
        _assert_rejected(result, "motor_pwm_protocol")


def test_set_failsafe_rejects_unknown_procedure():
    # RED: any token is accepted and only the FC rejects it, mid-write.
    result = _call(server.set_failsafe, procedure="BANANA")
    assert result.get("error"), f"expected an enum error, got {result}"


def test_set_failsafe_accepts_valid_procedures():
    # Regression lock: the enum must not lock out legitimate values.
    for proc in ["DROP", "LAND", "SET-THR", "RTH", "NONE"]:
        result = _call(server.set_failsafe, procedure=proc)
        assert result.get("dry_run") is True, f"{proc} should preview cleanly: {result}"
        assert result["commands"] == [f"set failsafe_procedure = {proc}"]


def test_configure_gps_rejects_smuggled_provider():
    for payload in SMUGGLED:
        result = _call(server.configure_gps, provider=payload)
        _assert_rejected(result, "motor_pwm_protocol")


def test_configure_gps_rejects_smuggled_sbas():
    for bad in INJECTIONS:
        result = _call(server.configure_gps, provider="UBLOX", sbas=f"AUTO{bad}save")
        assert result.get("error"), f"expected rejection of sbas={bad!r}, got {result}"


def test_configure_gps_rejects_unknown_enums():
    assert _call(server.configure_gps, provider="GARBAGE").get("error")
    assert _call(server.configure_gps, provider="UBLOX", sbas="NOPE").get("error")


def test_configure_gps_accepts_valid_enums():
    result = _call(server.configure_gps, provider="UBLOX", sbas="AUTO")
    assert result.get("dry_run") is True, f"valid input must preview cleanly: {result}"


def test_set_nav_rejects_smuggled_landing_policy():
    for payload in SMUGGLED:
        result = _call(server.set_nav, rth_allow_landing=payload)
        _assert_rejected(result, "motor_pwm_protocol")


def test_set_nav_rejects_unknown_landing_policy():
    result = _call(server.set_nav, rth_allow_landing="MAYBE")
    assert result.get("error"), f"expected an enum error, got {result}"


# ── 3. backup replay must not smuggle a separator through one logical line ────

def test_backup_replay_line_with_separator_is_rejected():
    from inav_mcp.cli import validate_cli_command
    # A crafted backup line that looks like one command but carries a second.
    line = "set failsafe_procedure = RTH\rsave"
    assert is_replayable(line) is True, "the skip-list alone does not catch this"
    try:
        validate_cli_command(line)
    except ValueError:
        return
    raise AssertionError("a backup line with an embedded separator was accepted")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
