"""Aircraft hardware profile and CLI command generator (§8).

AircraftProfile is a pure-data object; generate_cli_commands() is a pure function.
No FC connection required — works offline as a planner.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from typing import Optional

# ── Knowledge files ───────────────────────────────────────────────────────────

_KD = os.path.join(os.path.dirname(__file__), "knowledge")

def _load_json(name: str) -> dict:
    with open(os.path.join(_KD, name), encoding="utf-8") as f:
        return json.load(f)

def _esc_protocols() -> dict:
    return _load_json("esc_protocols.json")

def _fc_targets() -> dict:
    return _load_json("fc_targets.json")


# ── Wing-type mapping ─────────────────────────────────────────────────────────

# iNAV platform_type CLI string (all fixed-wing types use AIRPLANE)
_WING_PLATFORM: dict[str, str] = {
    "flying_wing": "AIRPLANE",
    "conventional": "AIRPLANE",
    "vtail":        "AIRPLANE",
    "twin_tail":    "AIRPLANE",
    "delta":        "AIRPLANE",
}

SUPPORTED_WING_TYPES = sorted(_WING_PLATFORM)

# Servo mixer rules per wing type.
# Tuple: (servo_index, input_index, rate)
# Input indices from iNAV Configurator: ROLL=0, PITCH=1, YAW=2
# Servo indices match the Configurator's SERVO enum (ELEVON_1=1, ELEVON_2=2, etc.)
#   servo 1 = ELEVATOR / ELEVON_1
#   servo 2 = FLAPERON_1 / ELEVON_2
#   servo 3 = FLAPERON_2
#   servo 4 = RUDDER
_SMIX_RULES: dict[str, list[tuple[int, int, int]]] = {
    # Flying wing: two elevon servos mix ROLL+PITCH
    "flying_wing": [
        (1, 0,  50),   # elevon 1 ← ROLL +50%
        (1, 1,  50),   # elevon 1 ← PITCH +50%
        (2, 0, -50),   # elevon 2 ← ROLL -50% (reversed for opposite wing)
        (2, 1,  50),   # elevon 2 ← PITCH +50%
    ],
    # Delta: same elevon arrangement as flying wing
    "delta": [
        (1, 0,  50),
        (1, 1,  50),
        (2, 0, -50),
        (2, 1,  50),
    ],
    # Conventional: elevator + ailerons (flaperons) + rudder
    "conventional": [
        (1, 1, 100),   # elevator ← PITCH +100%
        (2, 0, 100),   # flaperon 1 ← ROLL +100%
        (3, 0, 100),   # flaperon 2 ← ROLL +100% (reversed mechanically or via servo reverse)
        (4, 2, 100),   # rudder ← YAW +100%
    ],
    # V-tail: ailerons + v-tail surfaces (mix PITCH+YAW on two tail servos)
    "vtail": [
        (2, 0, 100),   # flaperon 1 ← ROLL +100%
        (3, 0, 100),   # flaperon 2 ← ROLL +100%
        (4, 1,  50),   # v-tail 1 ← PITCH +50%
        (4, 2,  50),   # v-tail 1 ← YAW +50%
        (1, 1,  50),   # v-tail 2 ← PITCH +50%
        (1, 2, -50),   # v-tail 2 ← YAW -50% (opposite for V)
    ],
    # Twin tail (conventional + two rudders sharing the YAW input)
    "twin_tail": [
        (1, 1, 100),   # elevator ← PITCH +100%
        (2, 0, 100),   # flaperon 1 ← ROLL +100%
        (3, 0, 100),   # flaperon 2 ← ROLL +100%
        (4, 2, 100),   # rudder 1 ← YAW +100%
    ],
}

# Human-readable description per wing type
_WING_DESCRIPTION: dict[str, str] = {
    "flying_wing": "Flying wing with two elevon servos mixing roll+pitch",
    "conventional": "Conventional airplane with elevator, two ailerons (flaperons), and rudder",
    "vtail": "V-tail airplane with two aileron servos and two V-tail surfaces mixing pitch+yaw",
    "twin_tail": "Twin-tail airplane (same servo layout as conventional; twin rudders share YAW)",
    "delta": "Delta wing (same elevon arrangement as flying wing)",
}

# Per-type warnings
_WING_WARNINGS: dict[str, list[str]] = {
    "flying_wing": [
        "Verify elevon 1 is servo output 1 and elevon 2 is servo output 2 in the Servos tab.",
        "Check servo directions: raise throttle slowly with one elevon disconnected to verify each surface moves the correct way.",
        "Rate of ±50% is the default; tune to match your control surface throw.",
    ],
    "conventional": [
        "Verify one aileron servo is mechanically reversed — both flaperons get the same +100% ROLL input.",
        "If the aircraft has no rudder, remove the rudder smix rule before applying.",
        "Elevator servo 1, aileron servos 2+3, rudder servo 4 — adjust if your wiring differs.",
    ],
    "vtail": [
        "V-tail mixing rates (±50%) are a starting point — tune after maiden flight.",
        "Verify which tail surface is servo 1 (left) and which is servo 4 (right); swap if needed.",
        "Aileron servos share the same ROLL input; verify one is mechanically reversed.",
    ],
    "twin_tail": [
        "Twin-tail configuration treated as conventional. If rudders are on separate servos, add a second YAW smix rule for servo 5.",
    ],
    "delta": [
        "Delta wing uses the same elevon mixing as flying wing.",
        "Verify elevon directions — delta wing elevons may need reversed rates depending on geometry.",
    ],
}

# ── Battery voltage presets (LiPo) in mV ─────────────────────────────────────
# iNAV uses centivolt (cV) units for vbat_*_cell_voltage: 1 cV = 10 mV
# e.g., 3.3 V = 330 cV

_LIPO_MIN_CV  = 330   # 3.30 V/cell
_LIPO_WARN_CV = 350   # 3.50 V/cell
_LIPO_MAX_CV  = 420   # 4.20 V/cell


# ── AircraftProfile dataclass ─────────────────────────────────────────────────

@dataclass
class AircraftProfile:
    name: str
    wing_type: str          # flying_wing | conventional | vtail | twin_tail | delta
    esc_protocol: str       # DSHOT600 | DSHOT300 | DSHOT150 | MULTISHOT | ONESHOT125 | PWM
    cells: int              # LiPo cell count (2–8)
    fc_target: Optional[str] = None
    motor_kv: Optional[int] = None
    motor_poles: int = 14
    servo_count: Optional[int] = None
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if self.wing_type not in SUPPORTED_WING_TYPES:
            raise ValueError(
                f"wing_type must be one of {SUPPORTED_WING_TYPES}, got {self.wing_type!r}"
            )
        if self.cells < 1 or self.cells > 12:
            raise ValueError(f"cells must be 1–12, got {self.cells}")
        if self.motor_poles < 2:
            raise ValueError(f"motor_poles must be ≥ 2, got {self.motor_poles}")

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "wing_type":    self.wing_type,
            "esc_protocol": self.esc_protocol,
            "cells":        self.cells,
            "fc_target":    self.fc_target,
            "motor_kv":     self.motor_kv,
            "motor_poles":  self.motor_poles,
            "servo_count":  self.servo_count,
            "notes":        self.notes,
        }


# ── Command generator ─────────────────────────────────────────────────────────

def generate_cli_commands(profile: AircraftProfile) -> dict:
    """Generate an ordered list of CLI commands to configure the FC for this profile.

    Returns:
        {
          "commands": [str, ...],   # exact CLI lines, in order, ready to paste/run
          "summary": str,           # human-readable one-paragraph description
          "warnings": [str, ...],   # things the user should verify before applying
        }

    No FC connection required.
    """
    protocols = _esc_protocols()
    proto_key = profile.esc_protocol.upper()
    if proto_key not in protocols:
        raise ValueError(
            f"Unknown ESC protocol {profile.esc_protocol!r}. "
            f"Supported: {', '.join(k for k in protocols if not k.startswith('_'))}"
        )
    proto_info = protocols[proto_key]

    commands: list[str] = []
    warnings: list[str] = []

    # 1. Platform type
    platform = _WING_PLATFORM[profile.wing_type]
    commands.append(f"set platform_type = {platform}")

    # 2. Motor ESC protocol
    commands.append(f"set motor_pwm_protocol = {proto_info['cli_value']}")

    # 3. Motor poles (needed for RPM telemetry; relevant for all digital protocols)
    if proto_info.get("is_digital"):
        commands.append(f"set motor_poles = {profile.motor_poles}")
        if proto_info.get("bidirectional_capable") and profile.motor_poles == 14:
            warnings.append(
                f"Default motor_poles=14 — verify this matches your motor's actual pole count "
                f"(pole count = magnet count, NOT stator slots). Wrong poles give wrong RPM readings."
            )

    # 4. Servo mixer
    smix_rules = _SMIX_RULES[profile.wing_type]
    commands.append("smix reset")
    for idx, (servo, inp, rate) in enumerate(smix_rules):
        commands.append(f"smix {idx} {servo} {inp} {rate} 0 0")

    # 5. Battery voltage thresholds (LiPo defaults).
    # NOTE: iNAV has no manual cell-count override (no `battery_cells` setting —
    # unlike Betaflight). Cell count is auto-detected at power-on, and the vbat
    # thresholds below are PER-CELL, so they don't depend on profile.cells.
    commands.append(f"set vbat_min_cell_voltage = {_LIPO_MIN_CV}")
    commands.append(f"set vbat_warning_cell_voltage = {_LIPO_WARN_CV}")
    commands.append(f"set vbat_max_cell_voltage = {_LIPO_MAX_CV}")

    # Wing-type specific warnings
    warnings.extend(_WING_WARNINGS.get(profile.wing_type, []))

    # ESC-protocol note
    warnings.append(f"ESC protocol: {proto_info['notes']}")

    # Battery note
    warnings.append(
        f"Battery set to {profile.cells}S LiPo defaults "
        f"(min {_LIPO_MIN_CV/100:.2f}V, warn {_LIPO_WARN_CV/100:.2f}V, max {_LIPO_MAX_CV/100:.2f}V per cell). "
        "Adjust for LiHV or Li-Ion chemistry if needed."
    )

    # FC target hint
    if profile.fc_target:
        fc_db = _fc_targets()
        target_key = profile.fc_target.upper()
        if target_key in fc_db:
            warnings.append(f"FC target {profile.fc_target}: {fc_db[target_key].get('notes', '')}")
        else:
            warnings.append(
                f"FC target {profile.fc_target!r} not in knowledge base — "
                "verify DShot/servo timer compatibility for your board."
            )

    # General safety reminder
    warnings.append(
        "After applying: run check_config() to compare these settings against the actual diff, "
        "then bench-test servo directions before flying."
    )

    # Build summary
    wing_desc = _WING_DESCRIPTION.get(profile.wing_type, profile.wing_type)
    summary = (
        f"Profile '{profile.name}': {wing_desc}. "
        f"Motor protocol: {profile.esc_protocol} (CLI: {proto_info['cli_value']}). "
        f"Battery: {profile.cells}S LiPo. "
        f"Motor poles: {profile.motor_poles}."
    )
    if profile.notes:
        summary += f" Notes: {profile.notes}"

    return {
        "commands": commands,
        "summary":  summary,
        "warnings": warnings,
    }


def lint_diff_against_profile(diff_output: str, profile: AircraftProfile) -> list[dict]:
    """Compare 'diff all' CLI output against a declared AircraftProfile.

    Returns a list of mismatch dicts: {severity, field, expected, actual, fix}
    """
    protocols = _esc_protocols()
    proto_info = protocols.get(profile.esc_protocol.upper(), {})
    expected_protocol_cli = proto_info.get("cli_value", profile.esc_protocol)

    mismatches: list[dict] = []
    lines_lower = diff_output.lower()

    def _extract(keyword: str) -> Optional[str]:
        """Pull the value from 'set keyword = value' in diff output."""
        import re
        m = re.search(rf'\bset\s+{re.escape(keyword)}\s*=\s*(\S+)', diff_output, re.IGNORECASE)
        return m.group(1).strip() if m else None

    # platform_type
    actual_platform = _extract("platform_type")
    expected_platform = _WING_PLATFORM.get(profile.wing_type, "AIRPLANE")
    if actual_platform and actual_platform.upper() != expected_platform.upper():
        mismatches.append({
            "severity": "error",
            "field":    "platform_type",
            "expected": expected_platform,
            "actual":   actual_platform,
            "fix":      f"run: set platform_type = {expected_platform}",
        })

    # motor_pwm_protocol
    actual_proto = _extract("motor_pwm_protocol")
    if actual_proto and actual_proto.upper() != expected_protocol_cli.upper():
        mismatches.append({
            "severity": "error",
            "field":    "motor_pwm_protocol",
            "expected": expected_protocol_cli,
            "actual":   actual_proto,
            "fix":      f"run: set motor_pwm_protocol = {expected_protocol_cli}",
        })

    # motor_poles (only relevant for digital protocols)
    if proto_info.get("is_digital"):
        actual_poles = _extract("motor_poles")
        if actual_poles:
            try:
                if int(actual_poles) != profile.motor_poles:
                    mismatches.append({
                        "severity": "warning",
                        "field":    "motor_poles",
                        "expected": str(profile.motor_poles),
                        "actual":   actual_poles,
                        "fix":      f"run: set motor_poles = {profile.motor_poles}",
                    })
            except ValueError:
                pass

    return mismatches
