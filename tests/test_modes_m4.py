"""Tests for M4 mode/switch logic — offline with a mock connection where needed."""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import modes
from inav_mcp.modes import (
    build_aux_command,
    position_ranges,
    resolve_position_index,
    suggest_layout,
    plan_set_mode,
    plan_clear_mode,
    MIN_RANGE_US,
    MAX_RANGE_US,
)


# ── Mock connection ───────────────────────────────────────────────────────────

class MockConn:
    """Returns canned MSP payloads for BOXNAMES / BOXIDS / MODE_RANGES."""
    def __init__(self, boxnames, boxids, ranges_slots):
        self._boxnames = boxnames
        self._boxids = boxids
        self._ranges = ranges_slots   # list of 6-byte tuples

    def send_msp_v1(self, cmd, timeout=2.0):
        from inav_mcp.msp import MSP_BOXNAMES, MSP_BOXIDS, MSP_MODE_RANGES
        if cmd == MSP_BOXNAMES:
            return (";".join(self._boxnames) + ";").encode("ascii")
        if cmd == MSP_BOXIDS:
            return bytes(self._boxids)
        if cmd == MSP_MODE_RANGES:
            out = b""
            for (box_id, aux, start, end, logic, linked) in self._ranges:
                out += bytes([box_id, aux, start, end, logic, linked])
            # pad to a plausible 40-slot table so size detection picks 6 bytes
            while len(out) // 6 < 20:
                out += bytes([0, 0, 0, 0, 0, 0])
            return out
        raise AssertionError(f"unexpected cmd {cmd}")


def _empty_conn():
    # ARM=0, ANGLE=1, HORIZON=2, MANUAL=3, NAV RTH=10 — all slots empty
    return MockConn(
        boxnames=["ARM", "ANGLE", "HORIZON", "MANUAL", "NAV RTH"],
        boxids=[0, 1, 2, 3, 10],
        ranges_slots=[],
    )


# ── build_aux_command ─────────────────────────────────────────────────────────

def test_build_aux_command_format():
    cmd = build_aux_command(0, 0, 0, 1700, 2100)
    assert cmd == "aux 0 0 0 1700 2100 0 0"


def test_build_aux_command_logic_linked():
    cmd = build_aux_command(3, 10, 2, 1300, 1700, logic=1, linked_to=5)
    assert cmd == "aux 3 10 2 1300 1700 1 5"


# ── position_ranges ───────────────────────────────────────────────────────────

def test_position_ranges_2pos():
    assert position_ranges(2) == [(900, 1500), (1500, 2100)]


def test_position_ranges_3pos():
    assert position_ranges(3) == [(900, 1300), (1300, 1700), (1700, 2100)]


def test_position_ranges_6pos():
    bands = position_ranges(6)
    assert len(bands) == 6
    assert bands[0] == (900, 1100)
    assert bands[-1] == (1900, 2100)
    # contiguous and covering full span
    assert bands[0][0] == MIN_RANGE_US
    assert bands[-1][1] == MAX_RANGE_US


# ── resolve_position_index ────────────────────────────────────────────────────

def test_resolve_named_3pos():
    assert resolve_position_index("low", 3) == 0
    assert resolve_position_index("mid", 3) == 1
    assert resolve_position_index("high", 3) == 2


def test_resolve_named_2pos():
    assert resolve_position_index("low", 2) == 0
    assert resolve_position_index("high", 2) == 1


def test_resolve_numeric():
    assert resolve_position_index("1", 3) == 0
    assert resolve_position_index("3", 3) == 2


def test_resolve_posN():
    assert resolve_position_index("pos1", 6) == 0
    assert resolve_position_index("pos6", 6) == 5


def test_resolve_invalid_raises():
    try:
        resolve_position_index("middle", 3)
        assert False, "Expected ValueError"
    except ValueError:
        pass


def test_resolve_out_of_range_raises():
    try:
        resolve_position_index("4", 3)
        assert False, "Expected ValueError"
    except ValueError:
        pass


# ── suggest_layout ────────────────────────────────────────────────────────────

def test_suggest_layout_beginner_basic():
    layout = suggest_layout("beginner", num_switches=2, has_gps=False)
    purposes = [s["purpose"] for s in layout["switches"]]
    assert any("ARM" in p for p in purposes)
    assert any("Flight modes" in p for p in purposes)
    assert layout["skill_level"] == "beginner"


def test_suggest_layout_arm_is_first_switch():
    layout = suggest_layout("beginner", num_switches=3, has_gps=True)
    assert "ARM" in layout["switches"][0]["purpose"]
    assert layout["switches"][0]["aux_channel"] == 1
    assert layout["switches"][0]["rc_channel"] == 5


def test_suggest_layout_gps_adds_rth():
    layout = suggest_layout("intermediate", num_switches=3, has_gps=True)
    purposes = " ".join(s["purpose"] for s in layout["switches"])
    assert "RTH" in purposes


def test_suggest_layout_no_gps_no_rth():
    layout = suggest_layout("intermediate", num_switches=4, has_gps=False)
    purposes = " ".join(s["purpose"] for s in layout["switches"])
    assert "RTH" not in purposes


def test_suggest_layout_mode_switch_is_3pos():
    layout = suggest_layout("beginner", num_switches=2, has_gps=False)
    mode_sw = [s for s in layout["switches"] if "Flight modes" in s["purpose"]][0]
    assert mode_sw["type"] == "3-position"
    modes_assigned = [a["mode"] for a in mode_sw["assignments"]]
    assert modes_assigned == ["ANGLE", "HORIZON", "MANUAL"]


def test_suggest_layout_one_switch_only_arm():
    layout = suggest_layout("beginner", num_switches=1, has_gps=False)
    assert len(layout["switches"]) == 1
    assert "ARM" in layout["switches"][0]["purpose"]


def test_suggest_layout_invalid_skill_defaults_beginner():
    layout = suggest_layout("wizard", num_switches=2)
    assert layout["skill_level"] == "beginner"


# ── plan_set_mode (mock FC) ───────────────────────────────────────────────────

def test_plan_set_mode_create_empty():
    conn = _empty_conn()
    plan = plan_set_mode(conn, "ANGLE", aux_channel=2, range_low_us=1300, range_high_us=1700)
    assert plan["action"] == "create"
    assert plan["box_id"] == 1
    assert plan["aux_channel_index"] == 1   # AUX2 → index 1
    assert plan["slot"] == 0                # first free slot
    assert plan["command"] == "aux 0 1 1 1300 1700 0 0"


def test_plan_set_mode_unknown_mode_raises():
    conn = _empty_conn()
    try:
        plan_set_mode(conn, "ACRO_PLUS", 1, 1700, 2100)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "Unknown mode" in str(e)


def test_plan_set_mode_bad_range_raises():
    conn = _empty_conn()
    try:
        plan_set_mode(conn, "ANGLE", 1, 1700, 1700)  # low == high
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "Invalid range" in str(e)


def test_plan_set_mode_aux_channel_too_low():
    conn = _empty_conn()
    try:
        plan_set_mode(conn, "ANGLE", 0, 1300, 1700)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "aux_channel" in str(e)


def test_plan_set_mode_modify_existing():
    # ARM already on AUX1 (index 0), steps 32..48 (1700..2100), enabled
    conn = MockConn(
        boxnames=["ARM", "ANGLE"],
        boxids=[0, 1],
        ranges_slots=[(0, 0, 32, 48, 0, 0)],   # slot 0 = ARM on AUX1
    )
    plan = plan_set_mode(conn, "ARM", aux_channel=1, range_low_us=1800, range_high_us=2100)
    assert plan["action"] == "modify"
    assert plan["slot"] == 0
    assert plan["command"] == "aux 0 0 0 1800 2100 0 0"


def test_plan_set_mode_finds_next_free_slot():
    # slot 0 used by ARM; new ANGLE assignment should land in slot 1
    conn = MockConn(
        boxnames=["ARM", "ANGLE"],
        boxids=[0, 1],
        ranges_slots=[(0, 0, 32, 48, 0, 0)],
    )
    plan = plan_set_mode(conn, "ANGLE", aux_channel=2, range_low_us=900, range_high_us=1300)
    assert plan["slot"] == 1
    assert plan["action"] == "create"


# ── plan_clear_mode (mock FC) ─────────────────────────────────────────────────

def test_plan_clear_mode_finds_slots():
    conn = MockConn(
        boxnames=["ARM", "ANGLE", "HORIZON"],
        boxids=[0, 1, 2],
        ranges_slots=[
            (0, 0, 32, 48, 0, 0),   # ARM
            (2, 1, 16, 32, 0, 0),   # HORIZON on AUX2
        ],
    )
    plan = plan_clear_mode(conn, "HORIZON")
    assert plan["found"] == 1
    assert plan["slots"] == [1]
    # disable = start==end
    assert plan["commands"][0] == "aux 1 2 1 900 900 0 0"


def test_plan_clear_mode_not_assigned():
    conn = MockConn(
        boxnames=["ARM", "ANGLE"],
        boxids=[0, 1],
        ranges_slots=[(0, 0, 32, 48, 0, 0)],
    )
    plan = plan_clear_mode(conn, "ANGLE")
    assert plan["found"] == 0
    assert plan["commands"] == []


def test_plan_clear_mode_unknown_raises():
    conn = _empty_conn()
    try:
        plan_clear_mode(conn, "NONEXISTENT")
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "Unknown mode" in str(e)


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
