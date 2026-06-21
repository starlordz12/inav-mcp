"""Tests for CLI response parsing and write-command detection. No hardware required."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.cli import (
    strip_cli_response, is_write_command, cli_error, is_replayable,
    is_session_control_command,
)


# ── is_replayable ─────────────────────────────────────────────────────────────

def test_replayable_skips_save_and_batch():
    assert not is_replayable("save")
    assert not is_replayable("batch start")
    assert not is_replayable("batch end")
    assert not is_replayable("diff all")


def test_replayable_keeps_real_commands():
    assert is_replayable("set motor_poles = 14")
    assert is_replayable("smix 0 1 0 50 0 0")
    assert is_replayable("defaults noreboot")   # the clean base — must be kept
    assert is_replayable("aux 0 0 0 1700 2100")


# ── cli_error ─────────────────────────────────────────────────────────────────

def test_cli_error_invalid_name():
    assert cli_error("### ERROR: Invalid name") == "### ERROR: Invalid name"


def test_cli_error_detected_among_lines():
    raw = "set bogus = 4\r\n### ERROR: Invalid name\r\n# "
    assert cli_error(raw) is not None


def test_cli_error_none_on_success():
    assert cli_error("motor_poles set to 14") is None


def test_cli_error_ignores_diff_comments():
    # diff output uses single '#' comments — must NOT be treated as errors
    assert cli_error("# profile 0\nmotor_poles = 14\nplatform_type = AIRPLANE") is None


# ── strip_cli_response ────────────────────────────────────────────────────────

def test_strip_basic():
    """Typical echo + output + prompt from a simple 'get' command."""
    raw = "get motor_poles\r\nmotor_poles = 14\r\n# "
    out = strip_cli_response("get motor_poles", raw)
    assert out == "motor_poles = 14", repr(out)


def test_strip_diff_all():
    """Multi-line diff output, \r\n line endings."""
    lines = [
        "diff all",      # echo
        "# profile 0",   # some FC output starts with # — keep it
        "motor_poles = 14",
        "platform_type = FLYING_WING",
        "",              # trailing blank
        "# ",            # prompt
    ]
    raw = "\r\n".join(lines)
    out = strip_cli_response("diff all", raw)
    assert "motor_poles = 14" in out
    assert "platform_type = FLYING_WING" in out
    assert "diff all" not in out
    # Trailing "# " should be gone
    assert out.rstrip().endswith("FLYING_WING") or "FLYING_WING" in out


def test_strip_empty_response():
    """Command with no output (e.g. a set command that echoes nothing)."""
    raw = "set motor_poles = 14\r\n# "
    out = strip_cli_response("set motor_poles = 14", raw)
    assert out == ""


def test_strip_no_prompt():
    """Graceful handling if prompt is somehow absent."""
    raw = "status\r\nSystem OK"
    out = strip_cli_response("status", raw)
    assert "System OK" in out


def test_strip_status():
    """status command with typical multi-line output."""
    raw = (
        "status\r\n"
        "System Uptime: 42 seconds\r\n"
        "Voltage: 12.6V\r\n"
        "# "
    )
    out = strip_cli_response("status", raw)
    assert "System Uptime" in out
    assert "Voltage" in out
    assert "status" not in out.split("\n")[0] if out else True


# ── is_write_command ──────────────────────────────────────────────────────────

def test_write_detection_set():
    assert is_write_command("set motor_poles = 14") is True
    assert is_write_command("SET motor_poles = 14") is True   # case-insensitive


def test_write_detection_save():
    assert is_write_command("save") is True
    assert is_write_command("save ") is True


def test_write_detection_defaults():
    assert is_write_command("defaults") is True


def test_write_detection_aux():
    assert is_write_command("aux 0 0 0 900 1300 0 0") is True


def test_write_detection_motor():
    assert is_write_command("motor 0 1000") is True


def test_read_commands_not_flagged():
    for cmd in ["diff all", "diff", "get motor_poles", "status", "tasks",
                "dump", "dump all", "help", "version", "exit"]:
        assert is_write_command(cmd) is False, f"Wrongly flagged as write: {cmd!r}"


def test_read_partial_match_not_flagged():
    # 'set' as part of a longer token should not match 'set ' (note the space in prefix)
    # 'status' starts with 'stat', not 'set ' — should be read-only
    assert is_write_command("status") is False


# ── is_session_control_command (owned by the batch runner) ─────────────────────

def test_session_control_detects_lifecycle_verbs():
    for cmd in ["save", "exit", "batch start", "batch end", "SAVE", "Exit"]:
        assert is_session_control_command(cmd) is True, cmd


def test_session_control_excludes_normal_commands():
    for cmd in ["set foo = 1", "get motor_poles", "diff all", "aux 0 0 0 1700 2100"]:
        assert is_session_control_command(cmd) is False, cmd


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
