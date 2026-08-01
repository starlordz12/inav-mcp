"""restore_config()'s pre-flight actuator-command gate (AUDIT R3).

restore_config() used to replay every 'replayable' line straight into
_apply_cli_writes, including a live actuator command like 'motor 1 2000' — no
props-removed gate, no confirmation. Fixed by rejecting the WHOLE restore,
before any serial I/O (no connection needed to reach this branch), if any
replayable line is an actuator command.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server


def write_backup(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_restore_rejects_actuator_lines(tmp_path):
    """A motor line in a backup rejects the WHOLE restore, before any serial
    I/O — nothing reaches the FC, including the other (otherwise-harmless)
    line in the same file."""
    path = write_backup(tmp_path, "backup_20260729_120000_x.txt",
                         body="motor 1 2000\ndefaults noreboot\n")
    state.set_connection(None)
    out = server.restore_config(path, confirm=True)
    assert out["restored"] is False
    assert "motor 1 2000" in out["error"]
    assert out["rejected_lines"] == ["motor 1 2000"]
    assert "applied" not in out   # nothing was even attempted


def test_restore_rejects_actuator_lines_even_on_a_dry_run(tmp_path):
    """The gate fires whether or not confirm=True — a dry run must not report
    a falsely reassuring command count for a file it would actually refuse."""
    path = write_backup(tmp_path, "backup_20260729_120000_x.txt",
                         body="motor 1 2000\nset foo = 1\n")
    state.set_connection(None)
    out = server.restore_config(path)
    assert out["restored"] is False
    assert out["rejected_lines"] == ["motor 1 2000"]
    assert "dry_run" not in out


def test_restore_dry_run_unaffected_when_no_actuator_lines(tmp_path):
    """Sanity check: a clean file (no actuator lines) still gets the normal
    dry-run report, not the rejection path."""
    path = write_backup(tmp_path, "backup_20260729_120000_x.txt",
                         body="set foo = 1\nset bar = 2\n")
    state.set_connection(None)
    out = server.restore_config(path)
    assert out["dry_run"] is True
    assert out["command_count"] == 2


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            import inspect
            if "tmp_path" in inspect.signature(fn).parameters:
                continue
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
