"""Tests for M5 MCP resources and prompts. No hardware required."""
import sys, os, json, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.server import mcp
from inav_mcp import state


def _run(coro):
    return asyncio.run(coro)


# ── Registration ──────────────────────────────────────────────────────────────

def test_resources_registered():
    res = _run(mcp.list_resources())
    uris = {str(r.uri) for r in res}
    assert "inav://modes-reference" in uris
    assert "inav://current-profile" in uris
    assert "inav://last-backup" in uris


def test_prompts_registered():
    prompts = _run(mcp.list_prompts())
    names = {p.name for p in prompts}
    assert "new_fixed_wing_setup" in names
    assert "troubleshoot_no_arm" in names
    assert "configure_modes" in names


def test_tool_count_and_new_tools_registered():
    tools = _run(mcp.list_tools())
    names = {t.name for t in tools}
    assert len(tools) == 35
    # M6 + follow-up + nav/tuning + reboot-churn additions
    for expected in (
        "test_motor", "calibrate_accelerometer", "calibrate_magnetometer",
        "check_failsafe", "set_failsafe", "list_backups", "find_fc",
        "read_gps", "configure_gps", "set_nav", "read_tuning", "set_pid",
        "cli_batch",
    ):
        assert expected in names, f"missing tool: {expected}"


# ── Resource content ──────────────────────────────────────────────────────────

def test_modes_reference_resource_valid_json():
    content = _run(mcp.read_resource("inav://modes-reference"))
    text = content[0].content
    data = json.loads(text)
    assert "ARM" in data
    assert "ANGLE" in data


def test_current_profile_resource_no_profile():
    state.set_profile(None)
    content = _run(mcp.read_resource("inav://current-profile"))
    data = json.loads(content[0].content)
    assert data["profile"] is None
    assert "supported_wing_types" in data


def test_current_profile_resource_with_profile():
    from inav_mcp.profiles import AircraftProfile
    state.set_profile(
        AircraftProfile(name="Test Wing", wing_type="flying_wing",
                        esc_protocol="DSHOT600", cells=4)
    )
    try:
        content = _run(mcp.read_resource("inav://current-profile"))
        data = json.loads(content[0].content)
        assert data["profile"]["name"] == "Test Wing"
        assert "commands" in data
        assert any("platform_type" in c for c in data["commands"])
    finally:
        state.set_profile(None)


def test_last_backup_resource_returns_text():
    # Whether or not backups exist, it should return a string without raising.
    content = _run(mcp.read_resource("inav://last-backup"))
    assert isinstance(content[0].content, str)
    assert len(content[0].content) > 0


# ── Prompt content ────────────────────────────────────────────────────────────

def test_prompt_new_fixed_wing_setup_renders():
    result = _run(mcp.get_prompt("new_fixed_wing_setup", {}))
    text = " ".join(m.content.text for m in result.messages)
    assert "define_aircraft" in text
    assert "apply_aircraft_setup" in text


def test_prompt_troubleshoot_no_arm_renders():
    result = _run(mcp.get_prompt("troubleshoot_no_arm", {}))
    text = " ".join(m.content.text for m in result.messages)
    assert "why_wont_it_arm" in text


def test_prompt_configure_modes_renders():
    result = _run(mcp.get_prompt("configure_modes", {}))
    text = " ".join(m.content.text for m in result.messages)
    assert "suggest_mode_layout" in text
    assert "assign_switch" in text


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
