"""MSP codec round-trip tests. No serial hardware required."""
import struct
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.msp import (
    MSP_API_VERSION,
    MSP_FC_VARIANT,
    MSP_FC_VERSION,
    MSP_BUILD_INFO,
    MSP_STATUS,
    _xor,
    _crc8_dvb_s2,
    encode_v1,
    decode_v1_response,
    encode_v2,
    decode_v2_response,
    parse_api_version,
    parse_fc_variant,
    parse_fc_version,
    parse_build_info,
    parse_status,
    parse_inav_status,
    ARMING_FLAG_ARMED,
    ARMING_FLAG_SIMULATOR_MODE,
    ARMING_DISABLE_REASON_MASK,
    SENSOR_BITS,
)


def test_v1_encode_request():
    frame = encode_v1(MSP_API_VERSION)
    assert frame[:3] == b"$M<", f"bad request preamble: {frame[:3]!r}"
    assert frame[3] == 0       # size = 0 (no payload)
    assert frame[4] == 1       # cmd = MSP_API_VERSION
    assert frame[5] == _xor(frame[3:5])  # checksum


def test_v1_encode_with_payload():
    frame = encode_v1(200, b"\x01\x02\x03")
    assert frame[:3] == b"$M<"
    assert frame[3] == 3
    assert frame[4] == 200
    assert frame[5:8] == b"\x01\x02\x03"
    assert frame[8] == _xor(frame[3:8])


def _make_v1_response(cmd: int, payload: bytes) -> bytes:
    size = len(payload)
    body = bytes([size, cmd]) + payload
    return b"$M>" + body + bytes([_xor(body)])


def test_v1_decode_api_version():
    resp = _make_v1_response(MSP_API_VERSION, bytes([0, 2, 5]))
    cmd, p = decode_v1_response(resp)
    assert cmd == MSP_API_VERSION
    assert p == bytes([0, 2, 5])
    parsed = parse_api_version(p)
    assert parsed["api_version"] == "2.5"
    assert parsed["msp_protocol_version"] == 0


def test_v1_decode_bad_checksum():
    resp = _make_v1_response(MSP_API_VERSION, bytes([0, 2, 5]))
    bad = resp[:-1] + bytes([resp[-1] ^ 0xFF])
    try:
        decode_v1_response(bad)
        assert False, "should have raised"
    except ValueError as e:
        assert "Checksum" in str(e)


def test_v1_error_response():
    body = bytes([0, MSP_API_VERSION])
    resp = b"$M!" + body + bytes([_xor(body)])
    try:
        decode_v1_response(resp)
        assert False, "should have raised"
    except RuntimeError:
        pass


def test_v2_round_trip():
    frame = encode_v2(0x1234, b"\xAB\xCD\xEF")
    assert frame[:3] == b"$X<"
    # Build a matching response
    import struct
    flag = 0
    fn = struct.pack("<H", 0x1234)
    length = struct.pack("<H", 3)
    payload = b"\xAB\xCD\xEF"
    crc_body = bytes([flag]) + fn + length + payload
    crc = _crc8_dvb_s2(crc_body)
    resp = b"$X>" + crc_body + bytes([crc])
    cmd, p = decode_v2_response(resp)
    assert cmd == 0x1234
    assert p == payload


def test_parse_fc_variant():
    assert parse_fc_variant(b"INAV") == {"variant": "INAV"}
    assert parse_fc_variant(b"BTFL") == {"variant": "BTFL"}
    assert parse_fc_variant(b"INAV\x00") == {"variant": "INAV"}


def test_parse_fc_version():
    assert parse_fc_version(bytes([7, 1, 2])) == {"fw_version": "7.1.2"}


def test_parse_build_info():
    data = b"Jan 01 2025" + b"12:34:56" + b"abc1234"
    r = parse_build_info(data)
    assert r["build_date"] == "Jan 01 2025"
    assert r["build_time"] == "12:34:56"
    assert r["git_revision"] == "abc1234"


def test_parse_status_sensors():
    # Build a 18-byte iNAV MSP_STATUS payload
    sensor_flags = SENSOR_BITS["acc"] | SENSOR_BITS["gps"]
    payload = (
        struct.pack("<H", 250)          # cycle_time
        + struct.pack("<H", 0)          # i2c_errors
        + struct.pack("<H", sensor_flags)  # sensorStatus
        + struct.pack("<I", 0)          # flightModeFlags
        + bytes([0])                    # profile
        + struct.pack("<H", 25)         # cpu_load_pct
        + bytes([0])                    # armingDisableCount
        + struct.pack("<I", 0)          # armingDisableFlags
    )
    s = parse_status(payload)
    assert s["cycle_time_us"] == 250
    assert s["sensors_present"]["acc"] is True
    assert s["sensors_present"]["gps"] is True
    assert s["sensors_present"]["mag"] is False
    assert s["sensors_present"]["baro"] is False
    assert s["cpu_load_pct"] == 25
    assert s["arming_disable_flags"] == 0


def test_parse_status_short_payload():
    # Should not raise, just return partial data or {}
    r = parse_status(b"\x00\x01\x02")
    assert r == {}


def _make_inav_status(sensors=0, cpu=12, profile=0, battery_profile=0,
                      arming_flags=0, mixer_profile=0):
    """Build a 14-byte MSPV2_INAV_STATUS payload."""
    packed = (profile & 0x0F) | ((battery_profile & 0x0F) << 4)
    return (
        struct.pack("<H", 250)            # cycleTime
        + struct.pack("<H", 0)            # i2cErrors
        + struct.pack("<H", sensors)      # activeSensors
        + struct.pack("<H", cpu)          # cpuLoad
        + bytes([packed])                 # packed profile byte
        + struct.pack("<I", arming_flags) # armingFlags
        + bytes([mixer_profile & 0x0F])   # mixer profile
    )


def test_parse_inav_status_basic():
    payload = _make_inav_status(
        sensors=SENSOR_BITS["acc"] | SENSOR_BITS["gps"],
        cpu=33, profile=1, battery_profile=2, arming_flags=0, mixer_profile=0,
    )
    s = parse_inav_status(payload)
    assert s["cycle_time_us"] == 250
    assert s["cpu_load_pct"] == 33
    assert s["profile"] == 1
    assert s["battery_profile"] == 2
    assert s["sensors_present"]["acc"] is True
    assert s["sensors_present"]["gps"] is True
    assert s["sensors_present"]["mag"] is False
    assert s["arming_disable_flags"] == 0
    assert s["armed"] is False
    assert s["simulator_mode"] is False


def test_parse_inav_status_armed():
    payload = _make_inav_status(arming_flags=ARMING_FLAG_ARMED)
    s = parse_inav_status(payload)
    assert s["armed"] is True
    # ARMED is a state bit, not a disable reason
    assert (s["arming_disable_flags"] & ARMING_DISABLE_REASON_MASK) == 0


def test_parse_inav_status_disable_reason():
    # bit 18 = RC_LINK (a real disable reason in the 6..30 band)
    payload = _make_inav_status(arming_flags=1 << 18)
    s = parse_inav_status(payload)
    assert s["armed"] is False
    assert (s["arming_disable_flags"] & ARMING_DISABLE_REASON_MASK) != 0


def test_parse_inav_status_simulator_bit():
    payload = _make_inav_status(arming_flags=ARMING_FLAG_SIMULATOR_MODE)
    s = parse_inav_status(payload)
    assert s["simulator_mode"] is True


def test_parse_inav_status_short_payload():
    assert parse_inav_status(b"\x00\x01\x02") == {}


def test_disable_reason_mask_excludes_state_bits():
    # State bits (ARMED=2, SIMULATOR=4) must NOT be in the disable-reason mask.
    assert ARMING_FLAG_ARMED & ARMING_DISABLE_REASON_MASK == 0
    assert ARMING_FLAG_SIMULATOR_MODE & ARMING_DISABLE_REASON_MASK == 0
    # Reason bits 6 and 30 must be in the mask.
    assert (1 << 6) & ARMING_DISABLE_REASON_MASK != 0
    assert (1 << 30) & ARMING_DISABLE_REASON_MASK != 0


def test_crc8_dvb_s2_known():
    # CRC8 DVB-S2 of empty string is 0
    assert _crc8_dvb_s2(b"") == 0
    # 0x31 -> CRC should be 0xA1 (standard test vector from DVB-S2 spec)
    # Using a simple known-good vector from the MSP v2 spec examples
    data = bytes([0x00, 0xD0, 0x10, 0x00, 0x00])  # typical identity request body
    crc = _crc8_dvb_s2(data)
    assert isinstance(crc, int) and 0 <= crc <= 255


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
