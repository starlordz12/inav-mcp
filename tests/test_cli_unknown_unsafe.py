"""S2 — unknown raw CLI verbs must default to UNSAFE (offline).

`is_write_command()` currently answers from a fixed `_WRITE_PREFIXES` list and
returns False for anything absent from it, so an unknown or newly-introduced
firmware verb runs as a read: no confirmation, no backup, no armed guard.

Required inversion: keep a small explicit allowlist of known read-only commands
and treat everything not positively identified as read-only as write-like.

`cli()` must also refuse the session-control verbs it owns itself (save / exit /
batch) before any serial I/O — `cli_batch()` already does.

Every cli() test here uses a connection that positively reports DISARMED, so a
refusal can only come from command classification (not from the S1 armed guard).
"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server
from inav_mcp.cli import is_write_command, is_session_control_command
from inav_mcp.msp import MSPV2_INAV_STATUS, SENSOR_BITS


def _v2_disarmed():
    return (
        struct.pack("<H", 200)
        + struct.pack("<H", 0)
        + struct.pack("<H", SENSOR_BITS["acc"])
        + struct.pack("<H", 10)
        + bytes([0])
        + struct.pack("<I", 0)      # armingFlags = 0 → disarmed
        + bytes([0])
    )


class DisarmedConn:
    """Answers the armed guard truthfully (disarmed) and records CLI activity."""
    def __init__(self, reply=""):
        self._reply = reply
        self.entered = False
        self.exit_saved = None
        self.cli_commands: list[str] = []

    def is_open(self):
        return True

    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        assert cmd == MSPV2_INAV_STATUS
        return _v2_disarmed()

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        raise AssertionError("v1 status must not be read when v2 answers")

    def enter_cli(self, timeout=5.0):
        self.entered = True
        return "Entering CLI Mode..."

    def run_cli(self, command, timeout=15.0):
        self.cli_commands.append(command)
        return self._reply

    def exit_cli(self, save=False, reconnect=False):
        self.exit_saved = save


def _run(command, **kwargs):
    conn = DisarmedConn()
    state.set_connection(conn)
    try:
        out = server.cli(command, **kwargs)
    finally:
        state.set_connection(None)
    return conn, out


# ── classification: documented reads stay reads ───────────────────────────────

DOCUMENTED_READS = [
    "status", "tasks", "version", "boards", "help",
    "diff", "diff all", "dump", "dump all",
    "get failsafe_delay", "get nav_rth_altitude",
]


def test_documented_reads_remain_read_only():
    for cmd in DOCUMENTED_READS:
        assert is_write_command(cmd) is False, f"{cmd!r} must stay read-only"


def test_known_writes_are_writes():
    for cmd in ["set failsafe_delay = 5", "save", "defaults", "aux 0 0 0 1300 1700",
                "feature GPS", "smix 0 1 0 100 0", "servo 0 1000 2000 1500 100",
                "mixer FLYING_WING", "profile 1", "rxrange 0 1000 2000"]:
        assert is_write_command(cmd) is True, f"{cmd!r} must require confirmation"


# ── the inversion ─────────────────────────────────────────────────────────────

UNKNOWN_COMMANDS = [
    "wibble",                       # not a real verb at all
    "serialpassthrough 0 115200",   # real iNAV verb, absent from _WRITE_PREFIXES
    "msc",                          # mounts mass storage / reboots
    "bl",                           # reboots to bootloader
    "flash_erase",
    "sd_info",
    "timer 0 af 1",
    "serial 0 1 115200 57600 0 115200",
    "safehome 0 1 500 500",
    "temp_sensor 0 1 0 0 0 0",
]


def test_unknown_commands_default_to_unsafe():
    # RED: is_write_command() falls through to False for every one of these.
    for cmd in UNKNOWN_COMMANDS:
        assert is_write_command(cmd) is True, \
            f"{cmd!r} is not positively known to be read-only — must be treated as a write"


def test_unknown_command_requires_confirmation_in_cli():
    # RED: today an unknown verb runs straight through as a read.
    conn, out = _run("serialpassthrough 0 115200")
    assert conn.entered is False, "must refuse BEFORE entering CLI"
    assert conn.cli_commands == []
    assert "confirm_for_writes" in str(out)


def test_unknown_command_runs_when_confirmed():
    # The inversion must not make unknown verbs unreachable — only confirmed.
    conn, out = _run("serialpassthrough 0 115200", confirm_for_writes=True)
    assert conn.entered is True
    assert conn.cli_commands == ["serialpassthrough 0 115200"]


# ── session-control verbs ─────────────────────────────────────────────────────

SESSION_CONTROL = ["save", "exit", "batch start", "batch end"]


def test_session_control_is_detected():
    for cmd in SESSION_CONTROL:
        assert is_session_control_command(cmd) is True, f"{cmd!r} is session control"


def test_exit_is_not_classified_read_only():
    # RED: 'exit' currently sits in _READ_PREFIXES, so it reads as a safe command.
    assert is_write_command("exit") is True


def test_cli_refuses_session_control_before_serial_io():
    # RED: cli() never consults is_session_control_command().
    for cmd in SESSION_CONTROL:
        conn, out = _run(cmd, confirm_for_writes=True)
        assert conn.entered is False, f"{cmd!r} must be refused before serial I/O"
        assert conn.cli_commands == []
        assert conn.exit_saved is None
        assert "SAFETY" in str(out) or "refus" in str(out).lower()


# ── the motor gate must survive the inversion ─────────────────────────────────

def test_write_confirm_still_does_not_bypass_motor_gate():
    # Regression lock: reclassifying commands must not let confirm_for_writes
    # unlock a live motor spin.
    conn, out = _run("motor 0 2000", confirm_for_writes=True)
    assert conn.entered is False
    assert "props_removed" in str(out)


def test_documented_reads_still_run_without_confirmation():
    # Regression lock: the allowlist must keep ordinary diagnostics friction-free.
    conn, out = _run("status")
    assert conn.entered is True
    assert conn.exit_saved is False, "a read must never be saved"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
