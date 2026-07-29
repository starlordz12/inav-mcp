"""S4 — motor-stop confirmation must be reported truthfully (offline).

`test_motor()` currently swallows every exception from all three stop attempts
and then returns `"ran": True, "stopped": True` unconditionally. If the link dies
mid-test the tool reports a clean stop for a motor that may still be spinning.

Required: count successful test pulses and successful stop acknowledgements
separately; set `ran` only when a pulse succeeded, `stopped` only when a stop was
acknowledged; and when no stop is acknowledged, say so prominently. Repeated stop
attempts in `finally` and the original transmission error must both be preserved.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server
from inav_mcp.msp import MSP_SET_MOTOR
from fake_conn import DisarmedStatus

STOP_PAYLOAD = server.encode_motor_values([1000] * server.MAX_SUPPORTED_MOTORS)


class MotorConn(DisarmedStatus):
    """Scriptable motor link.

    `pulse_script` / `stop_script` are lists of True (acknowledged) / False
    (raises). The LAST entry repeats for any further attempts, so [False] means
    "every attempt fails" and [True, False] means "the first works, then the link
    dies".
    """
    def __init__(self, pulse_script=(True,), stop_script=(True,)):
        self._pulse = list(pulse_script)
        self._stop = list(stop_script)
        self.pulses_attempted = 0
        self.stops_attempted = 0

    def is_open(self):
        return True

    def _next(self, script):
        return script.pop(0) if len(script) > 1 else script[0]

    def send_msp_v1(self, cmd, payload=b"", timeout=1.0):
        assert cmd == MSP_SET_MOTOR, f"unexpected v1 command {cmd}"
        if payload == STOP_PAYLOAD:
            self.stops_attempted += 1
            if not self._next(self._stop):
                raise TimeoutError("no stop acknowledgement")
        else:
            self.pulses_attempted += 1
            if not self._next(self._pulse):
                raise TimeoutError("no pulse acknowledgement")
        return b""


def _run(conn, **kwargs):
    state.set_connection(conn)
    try:
        return server.test_motor(motor=1, throttle_us=1100, duration_s=0.2,
                                 props_removed=True, confirm=True, **kwargs)
    finally:
        state.set_connection(None)


# ── the five required scenarios ───────────────────────────────────────────────

def test_successful_pulse_and_stop():
    conn = MotorConn()
    out = _run(conn)
    assert out["ran"] is True
    assert out["stopped"] is True
    assert out["pulses_sent"] >= 1
    assert conn.stops_attempted >= 1


def test_stop_acknowledged_after_initial_failure():
    conn = MotorConn(stop_script=(False, True))
    out = _run(conn)
    assert out["ran"] is True
    assert out["stopped"] is True, "a later stop attempt succeeded — that counts"
    assert conn.stops_attempted >= 2


def test_every_stop_attempt_fails():
    # RED: today this returns stopped=True with no warning whatsoever.
    conn = MotorConn(stop_script=(False,))
    out = _run(conn)
    assert out["ran"] is True, "the pulses themselves were acknowledged"
    assert out["stopped"] is False, "no stop was ever acknowledged"
    assert conn.stops_attempted == 3, "all three stop attempts must still be made"
    blob = str(out).lower()
    assert "cut power" in blob, "an unstopped motor must tell the user to cut power"


def test_first_pulse_fails_and_stop_succeeds():
    # RED: pulses_sent is 0 but ran is reported as True.
    conn = MotorConn(pulse_script=(False,))
    out = _run(conn)
    assert out["ran"] is False, "no test pulse was ever acknowledged"
    assert out["pulses_sent"] == 0
    assert out["stopped"] is True
    assert out.get("error"), "the original transmission failure must not be hidden"


def test_pulse_succeeds_then_link_dies_and_stops_fail():
    # RED: the worst case — motor commanded, link lost, reported as cleanly stopped.
    conn = MotorConn(pulse_script=(True, False), stop_script=(False,))
    out = _run(conn)
    assert out["ran"] is True, "one pulse was acknowledged before the link died"
    assert out["pulses_sent"] >= 1
    assert out["stopped"] is False
    assert out.get("error"), "the original transmission failure must not be hidden"
    assert "cut power" in str(out).lower()


# ── gates unchanged ───────────────────────────────────────────────────────────

def test_props_gate_still_refuses_before_any_motor_command():
    conn = MotorConn()
    state.set_connection(conn)
    try:
        out = server.test_motor(motor=1, throttle_us=1100, duration_s=0.2,
                                props_removed=False, confirm=True)
    finally:
        state.set_connection(None)
    assert out["ran"] is False
    assert conn.pulses_attempted == 0 and conn.stops_attempted == 0


def test_dry_run_sends_nothing():
    conn = MotorConn()
    state.set_connection(conn)
    try:
        out = server.test_motor(motor=1, throttle_us=1100, duration_s=0.2,
                                props_removed=True, confirm=False)
    finally:
        state.set_connection(None)
    assert out["ran"] is False and out["dry_run"] is True
    assert conn.pulses_attempted == 0 and conn.stops_attempted == 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
