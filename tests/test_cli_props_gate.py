"""Tests for the props-off safety gate on the cli() escape hatch — offline.

A live 'motor <index> <value>' command can spin a propeller (§10.1). It must:
  - be refused without props_removed=True, BEFORE any serial I/O,
  - NOT be bypassable by the generic confirm_for_writes flag, and
  - never be saved (it's a momentary bench test, not a persisted write).

Ordinary writes (set/aux/…) and servo CONFIG writes are unaffected.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server
from inav_mcp.cli import is_actuator_command


class FakeConn:
    """Records whether CLI was entered and what save= exit_cli received.

    Deliberately has NO send_msp_* methods → check_not_armed reads nothing and
    passes (best-effort), matching real behaviour when status can't be read.
    """
    def __init__(self, reply="motor 0 set to 1500"):
        self._reply = reply
        self.exit_saved = None
        self.entered = False

    def is_open(self):
        return True

    def enter_cli(self, timeout=5.0):
        self.entered = True
        return "Entering CLI Mode..."

    def run_cli(self, command, timeout=15.0):
        return self._reply

    def exit_cli(self, save=False, reconnect=False):
        self.exit_saved = save


def _run(command, **kwargs):
    conn = FakeConn()
    state.set_connection(conn)
    try:
        out = server.cli(command, **kwargs)
    finally:
        state.set_connection(None)
    return conn, out


# ── is_actuator_command (verb + index + value; reads excluded) ─────────────────

def test_is_actuator_detects_live_motor():
    assert is_actuator_command("motor 0 2000") is True
    assert is_actuator_command("MOTOR 3 1500") is True   # case-insensitive


def test_is_actuator_excludes_reads_and_config():
    # Bare read forms only print values — not a live output.
    assert is_actuator_command("motor") is False
    assert is_actuator_command("motor 0") is False
    # Config writes that don't drive a momentary motor output.
    assert is_actuator_command("set motor_poles = 14") is False
    assert is_actuator_command("get motor_poles") is False
    # servo CLI is persisted config, not a live output → not gated here.
    assert is_actuator_command("servo 0 1000 2000 1500 100") is False


# ── the gate, through the cli() tool ───────────────────────────────────────────

def test_motor_refused_without_props_removed():
    conn, out = _run("motor 0 2000")
    assert conn.entered is False, "must refuse BEFORE entering CLI / any serial I/O"
    assert conn.exit_saved is None
    assert "props_removed" in out
    assert "SAFETY" in out


def test_confirm_for_writes_does_not_bypass_props_gate():
    conn, out = _run("motor 0 2000", confirm_for_writes=True)
    assert conn.entered is False, "the write-confirm must NOT unlock a motor spin"
    assert "props_removed" in out


def test_motor_runs_with_props_removed_but_is_never_saved():
    conn, out = _run("motor 0 2000", props_removed=True)
    assert conn.entered is True
    assert conn.exit_saved is False, "a live motor test must never be saved"
    assert "NOT saved" in out


def test_ordinary_write_unaffected():
    conn = FakeConn(reply="foo set to 1")
    state.set_connection(conn)
    try:
        out = server.cli("set foo = 1", confirm_for_writes=True)
    finally:
        state.set_connection(None)
    assert conn.exit_saved is True
    assert "SAVED" in out


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
