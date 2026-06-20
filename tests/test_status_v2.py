"""Tests for the V2-status reader, armed-guard fallback, and disable-reason logic."""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.msp import (
    MSPV2_INAV_STATUS, MSP_STATUS,
    ARMING_FLAG_ARMED, SENSOR_BITS,
)
from inav_mcp.server import _read_inav_status
from inav_mcp.safety import check_not_armed


def _v2_status_payload(arming_flags=0, cpu=10):
    packed = 0
    return (
        struct.pack("<H", 200)            # cycleTime
        + struct.pack("<H", 0)            # i2cErrors
        + struct.pack("<H", SENSOR_BITS["acc"])  # activeSensors
        + struct.pack("<H", cpu)          # cpuLoad
        + bytes([packed])                 # packed profile
        + struct.pack("<I", arming_flags) # armingFlags
        + bytes([0])                      # mixer profile
    )


def _v1_status_payload(arming_flags=0):
    return (
        struct.pack("<H", 200)            # cycleTime
        + struct.pack("<H", 0)            # i2cErrors
        + struct.pack("<H", 0)            # sensorStatus
        + struct.pack("<I", 0)            # flightModeFlags
        + bytes([0])                      # profile
        + struct.pack("<H", 10)           # cpu_load
        + bytes([0])                      # armingDisableCount
        + struct.pack("<I", arming_flags) # armingDisableFlags
    )


class V2Conn:
    """Mock that answers MSPV2_INAV_STATUS over v2."""
    def __init__(self, arming_flags=0):
        self._flags = arming_flags

    def is_open(self):
        return True

    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        if cmd == MSPV2_INAV_STATUS:
            return _v2_status_payload(self._flags)
        raise AssertionError(f"unexpected v2 cmd {cmd}")

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        raise AssertionError("v1 should not be called when v2 works")


class V1FallbackConn:
    """Mock where v2 fails (old firmware) and v1 answers MSP_STATUS."""
    def __init__(self, arming_flags=0):
        self._flags = arming_flags

    def is_open(self):
        return True

    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        raise TimeoutError("no v2 support")

    def send_msp_v1(self, cmd, payload=b"", timeout=2.0):
        if cmd == MSP_STATUS:
            return _v1_status_payload(self._flags)
        raise AssertionError(f"unexpected v1 cmd {cmd}")


# ── _read_inav_status ─────────────────────────────────────────────────────────

def test_read_status_prefers_v2():
    status = _read_inav_status(V2Conn(arming_flags=0))
    assert status["_status_source"] == "v2"
    assert status["armed"] is False


def test_read_status_v2_reports_armed():
    status = _read_inav_status(V2Conn(arming_flags=ARMING_FLAG_ARMED))
    assert status["_status_source"] == "v2"
    assert status["armed"] is True


def test_read_status_falls_back_to_v1():
    status = _read_inav_status(V1FallbackConn(arming_flags=0))
    assert status["_status_source"] == "v1"
    assert "arming_disable_flags" in status


# ── check_not_armed ───────────────────────────────────────────────────────────

def test_check_not_armed_passes_when_disarmed():
    check_not_armed(V2Conn(arming_flags=0))   # should not raise


def test_check_not_armed_raises_when_armed_v2():
    try:
        check_not_armed(V2Conn(arming_flags=ARMING_FLAG_ARMED))
        assert False, "Expected RuntimeError (armed)"
    except RuntimeError as e:
        assert "ARMED" in str(e)


def test_check_not_armed_v1_fallback_armed():
    # ARMED bit also present in v1 combined flags
    try:
        check_not_armed(V1FallbackConn(arming_flags=ARMING_FLAG_ARMED))
        assert False, "Expected RuntimeError (armed via v1 fallback)"
    except RuntimeError as e:
        assert "ARMED" in str(e)


def test_check_not_armed_ignores_disable_reason():
    # A disable reason (e.g. RC_LINK bit 18) is NOT the ARMED bit — must not block writes.
    check_not_armed(V2Conn(arming_flags=1 << 18))   # should not raise


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
