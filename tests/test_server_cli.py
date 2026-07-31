"""Tests for the server.cli() escape-hatch tool — offline with a fake connection.

Focus: a write the FC REJECTS ('### ERROR') must not be reported as saved, and
must exit the CLI WITHOUT saving (a rejected command changed nothing).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server
from fake_conn import DisarmedStatus


class FakeConn(DisarmedStatus):
    """Minimal connection that records exit_cli(save=...) and returns a canned reply.

    Reports DISARMED so the armed guard (which fails closed) lets the write
    through — these tests are about save/reject behaviour, not about arming.
    """
    def __init__(self, reply):
        self._reply = reply
        self.exit_saved = None      # what save= was passed to exit_cli
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


def _run(reply, command="set foo = 1", confirm=True):
    conn = FakeConn(reply)
    state.set_connection(conn)
    try:
        out = server.cli(command, confirm_for_writes=confirm)
    finally:
        state.set_connection(None)
    return conn, out


def test_rejected_write_not_saved():
    conn, out = _run("### ERROR: Invalid name")
    assert conn.exit_saved is False, "rejected write must NOT be saved"
    assert "REJECTED" in out


def test_successful_write_saved():
    conn, out = _run("foo set to 1")
    assert conn.exit_saved is True
    assert "SAVED" in out


def test_read_command_not_saved():
    conn, out = _run("motor_poles = 14", command="get motor_poles")
    assert conn.exit_saved is False
    assert "SAVED" not in out


def test_write_without_confirm_blocked():
    conn, out = _run("should not run", command="set foo = 1", confirm=False)
    # Blocked before entering CLI — never touched the connection.
    assert conn.entered is False
    assert "confirm_for_writes" in out


# ── command validated before any serial I/O; run_cli() exceptions never save ──

def test_invalid_command_rejected_before_entering_cli():
    # An embedded control character must be rejected before enter_cli() ever
    # runs — validating only inside run_cli() would still force a real
    # CLI-mode-entry (and the reboot that follows on exit) for input that was
    # always going to be refused.
    conn, out = _run("unused", command="set foo = 1\rsave", confirm=True)
    assert conn.entered is False, "must be rejected before any serial I/O"
    assert "SAFETY" in out


class RaisingConn(DisarmedStatus):
    """A connection whose run_cli() raises instead of returning — models a
    validate_cli_command() failure that slips past the pre-check, a timeout,
    or any other mid-session failure."""
    def __init__(self, exc):
        self._exc = exc
        self.exit_saved = None
        self.entered = False

    def is_open(self):
        return True

    def enter_cli(self, timeout=5.0):
        self.entered = True
        return "Entering CLI Mode..."

    def run_cli(self, command, timeout=15.0):
        raise self._exc

    def exit_cli(self, save=False, reconnect=False):
        self.exit_saved = save


def test_run_cli_exception_does_not_save_and_does_not_escape():
    conn = RaisingConn(ValueError("boom"))
    state.set_connection(conn)
    try:
        # Must return a normal string, not raise — an uncaught exception here
        # would escape the MCP tool call instead of a clean rejection.
        out = server.cli("set foo = 1", confirm_for_writes=True)
    finally:
        state.set_connection(None)
    assert conn.exit_saved is False, "an exception mid-session must never save"
    assert "SAFETY" in out
    assert "boom" in out
