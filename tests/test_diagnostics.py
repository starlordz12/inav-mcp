"""Tests for M2 MSP parsers, mode parsing, and troubleshoot engine. No hardware required."""
import sys, os, struct, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.msp import (
    parse_rc_channels, parse_attitude, parse_analog,
    parse_boxnames, parse_boxids, parse_mode_ranges,
    parse_sensor_status, parse_raw_gps,
    _xor, SENSOR_BITS,
)
from inav_mcp.troubleshoot import run_checks, decode_arming_flags


# ── Helper (mirror _make_v1_response_bytes if it doesn't exist) ──────────────

def make_response(cmd, payload):
    body = bytes([len(payload), cmd]) + payload
    return b"$M>" + body + bytes([_xor(body)])


# ── parse_rc_channels ─────────────────────────────────────────────────────────

def test_rc_channels_16():
    payload = b"".join(struct.pack("<H", 1000 + i * 10) for i in range(16))
    ch = parse_rc_channels(payload)
    assert len(ch) == 16
    assert ch[0] == 1000
    assert ch[15] == 1150


def test_rc_channels_empty():
    assert parse_rc_channels(b"") == []


# ── parse_attitude ────────────────────────────────────────────────────────────

def test_attitude_level():
    payload = struct.pack("<hhH", 0, 0, 180)
    a = parse_attitude(payload)
    assert a["roll_deg"] == 0.0
    assert a["pitch_deg"] == 0.0
    assert a["yaw_deg"] == 180.0


def test_attitude_banked():
    payload = struct.pack("<hhH", 450, -100, 90)  # 45.0°, -10.0°, 90°
    a = parse_attitude(payload)
    assert a["roll_deg"] == 45.0
    assert a["pitch_deg"] == -10.0
    assert a["yaw_deg"] == 90.0


def test_attitude_short():
    assert parse_attitude(b"\x00\x01") == {}


# ── parse_analog ──────────────────────────────────────────────────────────────

def test_analog_basic():
    payload = (
        bytes([126])                       # vbat_dv = 12.6V
        + struct.pack("<H", 500)           # mah = 500
        + struct.pack("<H", 820)           # rssi (80%)
        + struct.pack("<h", 200)           # amperage = 2.00A
    )
    a = parse_analog(payload)
    assert a["vbat_v"] == 12.6
    assert a["mah_drawn"] == 500
    assert a["current_a"] == 2.0
    assert abs(a["rssi_pct"] - 80.2) < 0.5


def test_analog_hires_vbat():
    payload = (
        bytes([0])
        + struct.pack("<H", 0)
        + struct.pack("<H", 0)
        + struct.pack("<h", 0)
        + struct.pack("<H", 1260)   # high-res vbat = 12.60V
    )
    a = parse_analog(payload)
    assert abs(a["vbat_v"] - 12.60) < 0.01


# ── parse_boxnames / parse_boxids ─────────────────────────────────────────────

def test_boxnames():
    payload = b"ARM;ANGLE;HORIZON;MANUAL;"
    names = parse_boxnames(payload)
    assert names == ["ARM", "ANGLE", "HORIZON", "MANUAL"]


def test_boxnames_no_trailing_semicolon():
    payload = b"ARM;ANGLE"
    assert parse_boxnames(payload) == ["ARM", "ANGLE"]


def test_boxids():
    payload = bytes([0, 1, 5, 8])
    ids = parse_boxids(payload)
    assert ids == [0, 1, 5, 8]


# ── parse_mode_ranges ─────────────────────────────────────────────────────────

def _make_range_slot_6(box_id, aux_ch, start_step, end_step, logic=0, linked=0):
    return bytes([box_id, aux_ch, start_step, end_step, logic, linked])


def test_mode_ranges_enabled():
    # ARM on AUX1 (ch 0), 1700-2100µs → steps (1700-900)/25=32 to (2100-900)/25=48
    slot = _make_range_slot_6(0, 0, 32, 48) * 1 + b"\x00" * (39 * 6)
    ranges = parse_mode_ranges(slot)
    active = [r for r in ranges if r["enabled"]]
    assert len(active) == 1
    assert active[0]["box_id"] == 0
    assert active[0]["aux_channel"] == 0
    assert active[0]["aux_channel_1"] == 1
    assert active[0]["range_start_ms"] == 1700
    assert active[0]["range_end_ms"] == 2100


def test_mode_ranges_disabled_slot():
    # Disabled: start_step == end_step
    slot = _make_range_slot_6(1, 0, 0, 0) + b"\x00" * (39 * 6)
    ranges = parse_mode_ranges(slot)
    assert all(not r["enabled"] for r in ranges if r["box_id"] == 1)


def test_mode_ranges_empty():
    assert parse_mode_ranges(b"") == []


# ── parse_sensor_status ───────────────────────────────────────────────────────

def test_sensor_status_all_ok():
    payload = bytes([1, 1, 1, 1, 1, 1, 1, 1, 1])  # overall + 8 sensors, all OK
    s = parse_sensor_status(payload)
    assert s["overall"] == "OK"
    assert s["gyro"] == "OK"
    assert s["acc"] == "OK"


def test_sensor_status_gyro_unhealthy():
    payload = bytes([3, 3, 1, 0, 0, 0, 0, 0, 0])  # overall=UNHEALTHY, gyro=UNHEALTHY
    s = parse_sensor_status(payload)
    assert s["overall"] == "UNHEALTHY"
    assert s["gyro"] == "UNHEALTHY"
    assert s["acc"] == "OK"


# ── parse_raw_gps ─────────────────────────────────────────────────────────────

def test_raw_gps_3d_fix():
    lat = int(51.5074 * 1e7)
    lon = int(-0.1278 * 1e7)
    payload = (
        bytes([2])                     # fix_type = 3D
        + bytes([12])                  # numSat = 12
        + struct.pack("<i", lat)
        + struct.pack("<i", lon)
        + struct.pack("<H", 100)       # alt = 100 m
        + struct.pack("<H", 500)       # speed = 5.00 m/s
        + struct.pack("<H", 900)       # groundCourse = 90.0°
        + struct.pack("<H", 120)       # hdop = 1.20
    )
    g = parse_raw_gps(payload)
    assert g["fix_type"] == 2
    assert g["fix_type_name"] == "FIX_3D"
    assert g["num_sats"] == 12
    assert abs(g["latitude_deg"] - 51.5074) < 1e-5
    assert abs(g["longitude_deg"] - (-0.1278)) < 1e-5
    assert g["altitude_m"] == 100.0
    assert abs(g["speed_ms"] - 5.0) < 0.01
    assert g["hdop"] == 1.2


def test_raw_gps_no_fix():
    payload = bytes([0, 0]) + b"\x00" * 14
    g = parse_raw_gps(payload)
    assert g["fix_type"] == 0
    assert g["fix_type_name"] == "NO_FIX"


# ── troubleshoot.run_checks ───────────────────────────────────────────────────

def _make_status(arming_flags=0, flight_mode=0, cpu_load=20):
    return {
        "cycle_time_us": 250,
        "i2c_error_count": 0,
        "flight_mode_flags": flight_mode,
        "profile": 0,
        "sensors_present": {k: True for k in SENSOR_BITS},
        "cpu_load_pct": cpu_load,
        "arming_disable_flags": arming_flags,
    }


def test_no_problems_when_healthy():
    raw = {
        "status": _make_status(arming_flags=0),
        "sensor_health": {k: "OK" for k in
                          ["overall","gyro","acc","compass","baro","gps"]},
        "rc_channels": [1500, 1500, 1000, 1500, 1000, 1500, 1500, 1500],
        "analog": {"vbat_v": 12.6, "mah_drawn": 0, "rssi_pct": 90.0, "current_a": 0.0},
        "active_modes": [{"mode_name": "ARM", "box_id": 0, "aux_channel": 0,
                          "range_start_ms": 1700, "range_end_ms": 2100, "enabled": True}],
    }
    problems = run_checks(raw)
    # Should only have the GPS warning (no GPS data), nothing critical/error
    critical_or_error = [p for p in problems if p["severity"] in ("critical", "error")]
    assert not critical_or_error, critical_or_error


def test_arming_flag_rc_link():
    raw = {"status": _make_status(arming_flags=1 << 18)}  # ARMING_DISABLED_RC_LINK = bit 18
    problems = run_checks(raw)
    titles = [p["title"] for p in problems]
    assert any("RC_LINK" in t or "RC" in t for t in titles), titles


def test_no_arm_mode_assigned():
    raw = {
        "status": _make_status(arming_flags=0),
        "active_modes": [],  # nothing assigned
    }
    problems = run_checks(raw)
    titles = [p["title"] for p in problems]
    assert any("ARM" in t for t in titles), titles


def test_unhealthy_gyro_is_critical():
    raw = {
        "status": _make_status(arming_flags=0),
        "sensor_health": {"overall": "UNHEALTHY", "gyro": "UNHEALTHY", "acc": "OK"},
        "active_modes": [{"mode_name": "ARM", "enabled": True,
                          "box_id": 0, "aux_channel": 0,
                          "range_start_ms": 1700, "range_end_ms": 2100}],
    }
    problems = run_checks(raw)
    crits = [p for p in problems if p["severity"] == "critical"]
    assert crits, "Expected critical problem for unhealthy gyro"


def test_decode_arming_flags_direct():
    # ARMING_DISABLED_THROTTLE = bit 19 in iNAV 9.x
    probs = decode_arming_flags(1 << 19)
    assert len(probs) == 1
    assert "THROTTLE" in probs[0]["title"]


def test_decode_arming_flags_zero():
    assert decode_arming_flags(0) == []


# ── knowledge/arming_flags.json integrity ────────────────────────────────────

def test_arming_flags_json_valid():
    path = os.path.join(os.path.dirname(__file__), "..", "inav_mcp", "knowledge", "arming_flags.json")
    with open(path) as f:
        data = json.load(f)
    for key, entry in data.items():
        if key.startswith("_"):
            continue
        assert int(key) >= 0
        assert "name" in entry, f"Missing 'name' in bit {key}"
        assert "reason" in entry, f"Missing 'reason' in bit {key}"
        assert "fix" in entry, f"Missing 'fix' in bit {key}"
        assert entry.get("severity") in ("critical", "error", "warning", "info"), \
            f"Bad severity in bit {key}: {entry.get('severity')}"


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
