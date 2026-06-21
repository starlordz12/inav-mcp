"""Tests for the cli_batch() tool — one CLI session (one reboot) for many commands.

Offline, with a fake connection. Focus:
  - a read-only batch exits WITHOUT saving (no EEPROM write), one reboot;
  - a write batch is SAVED once, after taking a backup;
  - a write batch with confirm_for_writes=False is a dry-run that never enters CLI;
  - any rejected command discards the whole batch (all-or-nothing, no save);
  - live 'motor' and session-control (save/exit/batch) commands are refused up front;
  - the armed-guard blocks a write batch before any serial I/O.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server


class FakeConn:
    """Records CLI entry + exit(save=...), returns canned per-command replies.

    `replies` maps a command string to its reply; anything else returns "ok".
    exit_cli returns a stub reboot duration so cli_batch can surface reboot_seconds.
    """
    def __init__(self, replies=None):
        self._replies = replies or {}
        self.entered = False
        self.exit_saved = None
        self.exit_reconnect = None
        self.ran = []

    def is_open(self):
        return True

    def enter_cli(self, timeout=5.0):
        self.entered = True
        return "Entering CLI Mode..."

    def run_cli(self, command, timeout=15.0):
        self.ran.append(command)
        return self._replies.get(command, "ok")

    def exit_cli(self, save=False, reconnect=False):
        self.exit_saved = save
        self.exit_reconnect = reconnect
        return 6.5   # measured reboot/reconnect seconds


def _run(commands, monkeypatch_backup=True, **kwargs):
    conn = FakeConn(kwargs.pop("replies", None))
    state.set_connection(conn)
    orig_backup = server._write_backup_file
    if monkeypatch_backup:
        server._write_backup_file = lambda diff, label: {"backup_path": f"/tmp/{label}.txt"}
    # Default: disarmed.
    orig_armed = server.check_not_armed
    server.check_not_armed = lambda _c: None
    try:
        out = server.cli_batch(commands, **kwargs)
    finally:
        server._write_backup_file = orig_backup
        server.check_not_armed = orig_armed
        state.set_connection(None)
    return conn, out


# ── read-only batch ─────────────────────────────────────────────────────────────

def test_read_batch_runs_all_and_does_not_save():
    conn, out = _run(["get motor_poles", "diff all", "version"])
    assert conn.entered is True
    assert conn.exit_saved is False, "a read-only batch must NOT save"
    assert conn.exit_reconnect is True
    assert out["saved"] is False
    assert out["command_count"] == 3
    assert len(out["results"]) == 3
    assert out["backup_path"] is None       # no backup for a read-only batch
    assert out["reboot_seconds"] == 6.5     # measured reboot cost surfaced
    assert out["rebooted"] is True


def test_read_batch_preserves_order_and_output():
    conn, out = _run(
        ["get a", "get b"],
        replies={"get a": "a = 1", "get b": "b = 2"},
    )
    assert [r["command"] for r in out["results"]] == ["get a", "get b"]
    assert out["results"][0]["output"] == "a = 1"
    assert out["results"][1]["output"] == "b = 2"


# ── write batch ─────────────────────────────────────────────────────────────────

def test_write_batch_requires_confirm():
    conn, out = _run(["set motor_poles = 14", "get motor_poles"])
    assert out["dry_run"] is True
    assert conn.entered is False, "dry-run must not enter CLI"
    assert out["write_commands"] == ["set motor_poles = 14"]


def test_write_batch_saves_once_and_backs_up():
    conn, out = _run(
        ["set motor_poles = 14", "set platform_type = FLYING_WING"],
        confirm_for_writes=True,
    )
    assert conn.entered is True
    assert conn.exit_saved is True, "a successful write batch saves once"
    assert out["saved"] is True
    assert out["backup_path"] is not None       # backup taken before writing
    assert "diff all" in conn.ran               # backup read happened in-session
    assert out["rejected"] == []


def test_write_batch_rejection_discards_everything():
    conn, out = _run(
        ["set motor_poles = 14", "set bogus = 9"],
        confirm_for_writes=True,
        replies={"set bogus = 9": "### ERROR: Invalid name"},
    )
    assert conn.exit_saved is False, "any rejected command must roll back the batch"
    assert out["saved"] is False
    assert out["rejected"] == ["set bogus = 9"]
    assert "REJECTED" in out["note"]


# ── refusals (before any serial I/O) ────────────────────────────────────────────

def test_motor_command_refused_in_batch():
    conn, out = _run(["set foo = 1", "motor 0 2000"], confirm_for_writes=True)
    assert conn.entered is False
    assert "motor" in out["error"].lower()
    assert "motor 0 2000" in out["rejected_commands"]


def test_session_control_refused_in_batch():
    for bad in ("save", "exit", "batch start"):
        conn, out = _run(["get x", bad])
        assert conn.entered is False, f"{bad!r} must be refused before entering CLI"
        assert bad in out["rejected_commands"]


def test_empty_batch_errors():
    conn, out = _run(["", "   "])
    assert conn.entered is False
    assert "error" in out


# ── armed guard (spec §10.2) ────────────────────────────────────────────────────

def test_write_batch_refused_while_armed():
    conn = FakeConn()
    state.set_connection(conn)
    orig_armed = server.check_not_armed
    def fake_armed(_c):
        raise RuntimeError("FC is ARMED (ARMED bit set in armingFlags).")
    server.check_not_armed = fake_armed
    try:
        try:
            server.cli_batch(["set foo = 1"], confirm_for_writes=True)
            raised = False
        except RuntimeError as exc:
            raised = "ARMED" in str(exc)
    finally:
        server.check_not_armed = orig_armed
        state.set_connection(None)
    assert raised, "write batch must refuse while armed"
    assert conn.entered is False, "must refuse before entering CLI"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
