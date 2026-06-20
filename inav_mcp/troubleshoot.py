"""Diagnostic rule engine (§9).

diagnose() calls run_checks(raw), which runs each checker and merges the results.
Add new checkers to _CHECKERS to extend coverage without touching existing code.

Each checker returns a list of problem dicts:
  { severity, title, detail, fix, source }   (source is optional)
"""
from __future__ import annotations
import json
import os

# ── Arming-flags knowledge base ───────────────────────────────────────────────

_FLAGS_JSON = os.path.join(
    os.path.dirname(__file__), "knowledge", "arming_flags.json"
)

_arming_flags_db: dict | None = None


def _get_flags_db() -> dict:
    global _arming_flags_db
    if _arming_flags_db is None:
        with open(_FLAGS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        _arming_flags_db = {k: v for k, v in data.items() if not k.startswith("_")}
    return _arming_flags_db


# ── Individual checkers ───────────────────────────────────────────────────────

def _check_arming_flags(raw: dict) -> list[dict]:
    """Decode each set bit in arming_disable_flags into a problem entry."""
    flags = raw.get("status", {}).get("arming_disable_flags", None)
    if flags is None:
        return []
    if flags == 0:
        return []

    db = _get_flags_db()
    problems: list[dict] = []
    for bit in range(32):
        if not (flags & (1 << bit)):
            continue
        info = db.get(str(bit), {})
        problems.append({
            "severity": info.get("severity", "error"),
            "title":    info.get("name", f"ARMING_DISABLED_BIT_{bit}"),
            "detail":   info.get("reason", f"Unknown arming-prevention flag (bit {bit}, value {hex(1 << bit)})"),
            "fix":      info.get("fix", "Consult iNAV documentation or source for this flag."),
            "source":   "arming_flags",
            "flag_bit": bit,
        })
    return problems


def _check_arm_mode_assigned(raw: dict) -> list[dict]:
    """Verify that ARM is assigned to at least one aux channel."""
    if "active_modes" not in raw:
        # Mode data wasn't collected — skip rather than false-positive
        return []
    active = raw["active_modes"]   # may be empty list (no modes assigned)
    arm_assigned = any(m.get("mode_name") == "ARM" for m in active)
    if not arm_assigned:
        return [{
            "severity": "error",
            "title":    "ARM mode not assigned to any switch",
            "detail":   "No aux channel/range is configured for the ARM mode. "
                        "The FC cannot be armed via a switch.",
            "fix":      "Use assign_switch() or set_flight_mode('ARM', aux_channel, low, high, confirm=True) "
                        "to assign ARM to a switch position (typically 1800–2100 µs on the high side).",
            "source":   "mode_ranges",
        }]
    return []


def _check_rc_signal(raw: dict) -> list[dict]:
    """Check for RC signal presence and basic sanity."""
    channels = raw.get("rc_channels")
    if channels is None:
        return []   # MSP_RC failed — arming_flags will catch RC_LINK if needed

    if not channels:
        return [{
            "severity": "error",
            "title":    "No RC channels readable",
            "detail":   "MSP_RC returned an empty payload. Receiver may not be configured or not sending data.",
            "fix":      "Check the receiver type and UART port in the Ports tab. "
                        "Verify receiver protocol (SBUS/CRSF/IBUS/etc.) matches the configured protocol.",
            "source":   "rc",
        }]

    problems: list[dict] = []

    # Stuck/flat signal check — all channels at the same value is a strong sign of no signal
    unique_vals = set(channels)
    if len(unique_vals) <= 1:
        problems.append({
            "severity": "error",
            "title":    "RC channels all identical — no signal?",
            "detail":   f"All {len(channels)} channels read {channels[0]} µs. "
                        "This usually means the receiver is not sending data.",
            "fix":      "Check receiver binding, power, and serial protocol configuration.",
            "source":   "rc",
        })

    # Throttle (CH3, index 2) should be at minimum to arm
    if len(channels) >= 3:
        thr = channels[2]
        if thr > 1100 and len(unique_vals) > 1:   # only warn if signal looks real
            problems.append({
                "severity": "warning",
                "title":    f"Throttle not at minimum ({thr} µs)",
                "detail":   f"Channel 3 (throttle) reads {thr} µs. Most FCs require ≤1100 µs to arm.",
                "fix":      "Push the throttle stick to its lowest position before arming.",
                "source":   "rc",
            })

    return problems


def _check_sensor_health(raw: dict) -> list[dict]:
    """Flag unhealthy or missing required sensors."""
    health = raw.get("sensor_health")
    if not health:
        return []

    required = {"gyro", "acc"}
    problems: list[dict] = []

    for sensor, status in health.items():
        if sensor == "overall":
            if status == "UNHEALTHY":
                problems.append({
                    "severity": "critical",
                    "title":    "Overall hardware health: UNHEALTHY",
                    "detail":   "The FC reports at least one sensor in a failing state.",
                    "fix":      "Check individual sensor statuses below for specifics.",
                    "source":   "sensor_health",
                })
            continue

        if status == "UNHEALTHY":
            problems.append({
                "severity": "critical" if sensor in required else "error",
                "title":    f"{sensor.upper()} sensor UNHEALTHY",
                "detail":   f"The {sensor} sensor is detected but reports as unhealthy.",
                "fix":      f"Check {sensor} wiring and connections. Try a factory reset (defaults). "
                            "If it persists the sensor hardware may be damaged.",
                "source":   "sensor_health",
            })
        elif status == "UNAVAILABLE" and sensor in required:
            problems.append({
                "severity": "critical",
                "title":    f"{sensor.upper()} sensor UNAVAILABLE (required for flight)",
                "detail":   f"The {sensor} sensor is not detected. This sensor is required.",
                "fix":      f"Check FC hardware. {sensor.upper()} is essential — a missing gyro/acc means the FC is non-functional.",
                "source":   "sensor_health",
            })
    return problems


def _check_gps(raw: dict) -> list[dict]:
    """Check GPS fix quality if GPS data was collected."""
    gps = raw.get("gps")
    if not gps:
        return []

    fix_type = gps.get("fix_type", 0)
    num_sats = gps.get("num_sats", 0)
    problems: list[dict] = []

    if fix_type == 0:
        problems.append({
            "severity": "warning",
            "title":    f"No GPS fix ({num_sats} sats visible)",
            "detail":   "GPS has no fix. Navigation modes (RTH, WP, PosHold) are unsafe.",
            "fix":      "Move to open sky. Wait for 3D fix (≥6 satellites). "
                        "Check GPS module wiring if this never resolves.",
            "source":   "gps",
        })
    elif num_sats < 6:
        problems.append({
            "severity": "warning",
            "title":    f"GPS fix but only {num_sats} satellites",
            "detail":   f"GPS has a {'2D' if fix_type == 1 else '3D'} fix with {num_sats} sats. "
                        "Navigation accuracy may be poor.",
            "fix":      "Wait for more satellites (aim for 8+) before using nav modes.",
            "source":   "gps",
        })
    return problems


def _check_battery(raw: dict) -> list[dict]:
    """Check battery voltage plausibility."""
    analog = raw.get("analog")
    if not analog:
        return []

    vbat = analog.get("vbat_v", 0.0)
    problems: list[dict] = []

    if vbat < 0.5:
        problems.append({
            "severity": "warning",
            "title":    "Battery voltage reads near zero",
            "detail":   f"vbat reads {vbat:.2f} V — battery may not be connected or VBAT is unconfigured.",
            "fix":      "Connect a battery, or configure the VBAT ADC pin and scale in the Power & Battery tab.",
            "source":   "analog",
        })
    elif vbat < 3.0:
        problems.append({
            "severity": "warning",
            "title":    f"Suspiciously low battery voltage ({vbat:.2f} V)",
            "detail":   "Voltage is below what any LiPo cell count would produce.",
            "fix":      "Check vbat ADC pin, vbat_scale, and battery connection.",
            "source":   "analog",
        })
    return problems


_SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}

_CHECKERS = [
    _check_arming_flags,
    _check_arm_mode_assigned,
    _check_rc_signal,
    _check_sensor_health,
    _check_gps,
    _check_battery,
]


# ── Public API ────────────────────────────────────────────────────────────────

def run_checks(raw: dict) -> list[dict]:
    """Run all diagnostic checkers against collected MSP/CLI data.

    Returns a flat list of problem dicts sorted by severity (critical first).
    """
    problems: list[dict] = []
    for checker in _CHECKERS:
        try:
            problems.extend(checker(raw))
        except Exception as exc:
            problems.append({
                "severity": "info",
                "title":    f"Diagnostic error in {checker.__name__}",
                "detail":   str(exc),
                "fix":      "This is an internal MCP server error — report it.",
                "source":   "internal",
            })
    problems.sort(key=lambda p: _SEVERITY_ORDER.get(p.get("severity", "info"), 3))
    return problems


def decode_arming_flags(flags: int) -> list[dict]:
    """Decode a raw arming-disable flags uint32 into a problem list.

    Public helper used by why_wont_it_arm() directly.
    """
    if flags == 0:
        return []
    return _check_arming_flags({"status": {"arming_disable_flags": flags}})
