"""Tests for M3 profiles.py — offline, no hardware required."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.profiles import (
    AircraftProfile,
    SUPPORTED_WING_TYPES,
    generate_cli_commands,
    lint_diff_against_profile,
)


# ── AircraftProfile validation ────────────────────────────────────────────────

def test_profile_valid_flying_wing():
    p = AircraftProfile(name="Test Wing", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    assert p.wing_type == "flying_wing"
    assert p.cells == 4
    assert p.motor_poles == 14


def test_profile_default_poles():
    p = AircraftProfile(name="X", wing_type="conventional", esc_protocol="PWM", cells=3)
    assert p.motor_poles == 14


def test_profile_invalid_wing_type():
    try:
        AircraftProfile(name="X", wing_type="quadcopter", esc_protocol="DSHOT600", cells=4)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "wing_type" in str(e)


def test_profile_invalid_cells_zero():
    try:
        AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=0)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "cells" in str(e)


def test_profile_to_dict():
    p = AircraftProfile(
        name="My Wing",
        wing_type="flying_wing",
        esc_protocol="DSHOT600",
        cells=4,
        fc_target="MATEKF405",
        motor_kv=2300,
        motor_poles=12,
    )
    d = p.to_dict()
    assert d["name"] == "My Wing"
    assert d["cells"] == 4
    assert d["fc_target"] == "MATEKF405"
    assert d["motor_poles"] == 12


def test_all_wing_types_supported():
    for wt in SUPPORTED_WING_TYPES:
        p = AircraftProfile(name="X", wing_type=wt, esc_protocol="DSHOT600", cells=4)
        plan = generate_cli_commands(p)
        assert "commands" in plan
        assert "summary" in plan
        assert "warnings" in plan


# ── generate_cli_commands ─────────────────────────────────────────────────────

def test_flying_wing_has_platform_airplane():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    cmds = generate_cli_commands(p)["commands"]
    assert any("set platform_type = AIRPLANE" in c for c in cmds)


def test_flying_wing_smix_reset_present():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    cmds = generate_cli_commands(p)["commands"]
    assert "smix reset" in cmds


def test_flying_wing_elevon_rules():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    cmds = generate_cli_commands(p)["commands"]
    smix = [c for c in cmds if c.startswith("smix ") and c != "smix reset"]
    assert len(smix) == 4
    # elevon 1 gets ROLL (input 0)
    assert any("smix 0 1 0" in c for c in smix)
    # elevon 1 gets PITCH (input 1)
    assert any("smix 1 1 1" in c for c in smix)
    # elevon 2 gets -ROLL (negative rate)
    assert any("smix 2 2 0 -50" in c for c in smix)


def test_dshot600_sets_protocol():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    cmds = generate_cli_commands(p)["commands"]
    assert any("set motor_pwm_protocol = DSHOT600" in c for c in cmds)


def test_pwm_protocol_cli_value():
    p = AircraftProfile(name="X", wing_type="conventional", esc_protocol="PWM", cells=3)
    cmds = generate_cli_commands(p)["commands"]
    assert any("set motor_pwm_protocol = STANDARD" in c for c in cmds)


def test_motor_poles_included_for_digital():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4, motor_poles=12)
    cmds = generate_cli_commands(p)["commands"]
    assert any("set motor_poles = 12" in c for c in cmds)


def test_motor_poles_not_included_for_pwm():
    p = AircraftProfile(name="X", wing_type="conventional", esc_protocol="PWM", cells=4)
    cmds = generate_cli_commands(p)["commands"]
    assert not any("motor_poles" in c for c in cmds)


def test_no_battery_cells_command():
    # iNAV has no `battery_cells` setting (cell count is auto-detected); emitting
    # it produces a silently-rejected '### ERROR: Invalid name' on the FC.
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=6)
    cmds = generate_cli_commands(p)["commands"]
    assert not any("battery_cells" in c for c in cmds)


def test_battery_voltage_commands():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    cmds = generate_cli_commands(p)["commands"]
    assert any("vbat_min_cell_voltage" in c for c in cmds)
    assert any("vbat_warning_cell_voltage" in c for c in cmds)
    assert any("vbat_max_cell_voltage" in c for c in cmds)


def test_unknown_esc_protocol_raises():
    p = AircraftProfile.__new__(AircraftProfile)
    p.name = "X"
    p.wing_type = "flying_wing"
    p.esc_protocol = "TURBO9000"
    p.cells = 4
    p.motor_poles = 14
    p.fc_target = None
    p.motor_kv = None
    p.servo_count = None
    p.notes = None
    try:
        generate_cli_commands(p)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "Unknown ESC protocol" in str(e)


def test_conventional_has_elevator_and_rudder():
    p = AircraftProfile(name="X", wing_type="conventional", esc_protocol="ONESHOT125", cells=3)
    cmds = generate_cli_commands(p)["commands"]
    smix = [c for c in cmds if c.startswith("smix ") and c != "smix reset"]
    # should have elevator (pitch), 2x flaperon (roll), rudder (yaw)
    assert len(smix) == 4
    inputs = [int(c.split()[3]) for c in smix]  # input index is 4th token
    assert 1 in inputs  # PITCH (elevator)
    assert 0 in inputs  # ROLL (ailerons)
    assert 2 in inputs  # YAW (rudder)


def test_warnings_not_empty():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    plan = generate_cli_commands(p)
    assert len(plan["warnings"]) > 0


def test_summary_contains_wing_type():
    p = AircraftProfile(name="My Wing", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    plan = generate_cli_commands(p)
    assert "flying" in plan["summary"].lower() or "elevon" in plan["summary"].lower()


# ── lint_diff_against_profile ─────────────────────────────────────────────────

def _make_diff(platform="AIRPLANE", proto="DSHOT600", poles="14"):
    lines = [
        f"set platform_type = {platform}",
        f"set motor_pwm_protocol = {proto}",
        f"set motor_poles = {poles}",
    ]
    return "\n".join(lines)


def test_lint_no_mismatches():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    diff = _make_diff()
    m = lint_diff_against_profile(diff, p)
    assert m == []


def test_lint_wrong_protocol():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    diff = _make_diff(proto="STANDARD")
    m = lint_diff_against_profile(diff, p)
    fields = [x["field"] for x in m]
    assert "motor_pwm_protocol" in fields


def test_lint_wrong_poles():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4, motor_poles=14)
    diff = _make_diff(poles="12")
    m = lint_diff_against_profile(diff, p)
    fields = [x["field"] for x in m]
    assert "motor_poles" in fields


def test_lint_wrong_platform():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    diff = _make_diff(platform="MULTIROTOR")
    m = lint_diff_against_profile(diff, p)
    fields = [x["field"] for x in m]
    assert "platform_type" in fields


def test_lint_empty_diff():
    p = AircraftProfile(name="X", wing_type="flying_wing", esc_protocol="DSHOT600", cells=4)
    m = lint_diff_against_profile("", p)
    # No settings found in empty diff → no mismatches (can't compare what's absent)
    assert m == []


# ── knowledge files ───────────────────────────────────────────────────────────

def test_esc_protocols_json_valid():
    path = os.path.join(os.path.dirname(__file__), "..", "inav_mcp", "knowledge", "esc_protocols.json")
    with open(path) as f:
        data = json.load(f)
    for key, entry in data.items():
        if key.startswith("_"):
            continue
        assert "cli_value" in entry, f"Missing cli_value in {key}"
        assert "notes" in entry, f"Missing notes in {key}"
        assert isinstance(entry.get("is_digital"), bool), f"Missing is_digital in {key}"


def test_fc_targets_json_valid():
    path = os.path.join(os.path.dirname(__file__), "..", "inav_mcp", "knowledge", "fc_targets.json")
    with open(path) as f:
        data = json.load(f)
    for key, entry in data.items():
        if key.startswith("_"):
            continue
        assert "notes" in entry, f"Missing notes in {key}"


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
