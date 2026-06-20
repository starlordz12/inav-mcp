"""iNAV MCP server entry point.

Milestone 0: connection + board identity.
Milestone 1: CLI layer, backup_config, restore_config, cli, save_and_reboot.
Milestone 2: read_rc_channels, read_sensors, get_status, list_flight_modes,
             why_wont_it_arm, diagnose.
Milestone 3: define_aircraft, apply_aircraft_setup, get_aircraft_profile, check_config.
Milestone 4: suggest_mode_layout, set_flight_mode, assign_switch, clear_flight_mode.
"""
from __future__ import annotations
import os

from mcp.server.fastmcp import FastMCP

from . import state
from .cli import is_write_command, cli_error, is_replayable
from .connection import SerialConnection
from .msp import (
    MSP_API_VERSION,
    MSP_BOARD_INFO,
    MSP_BUILD_INFO,
    MSP_FC_VARIANT,
    MSP_FC_VERSION,
    MSP_STATUS,
    parse_api_version,
    parse_board_info,
    parse_build_info,
    parse_fc_variant,
    parse_fc_version,
    parse_status,
)
from .safety import check_not_armed, next_backup_path, BACKUPS_DIR
from . import modes as _modes
from . import troubleshoot as _troubleshoot
from . import profiles as _profiles
from .profiles import AircraftProfile, SUPPORTED_WING_TYPES, generate_cli_commands
from .msp import (
    MSP_RC, MSP_ATTITUDE, MSP_ANALOG, MSP_STATUS, MSP_RAW_GPS,
    MSP_SENSOR_STATUS,
    parse_rc_channels, parse_attitude, parse_analog,
    parse_sensor_status, parse_raw_gps,
)

mcp = FastMCP(
    "iNAV Flight Controller",
    instructions=(
        "Tools for configuring, diagnosing, and troubleshooting an iNAV fixed-wing "
        "flight controller over USB.\n\n"
        "WORKFLOW: call list_serial_ports() to find the port, then connect(port). "
        "All other tools require an active connection.\n\n"
        "SAFETY: props must be removed from the aircraft before any motor test. "
        "Every config write auto-backs-up and is dry-run by default."
    ),
)


# ── Internal helpers ──────────────────────────────────────────────────────────

_IDENTITY_COMMANDS = [
    (MSP_API_VERSION, parse_api_version),
    (MSP_FC_VARIANT,  parse_fc_variant),
    (MSP_FC_VERSION,  parse_fc_version),
    (MSP_BOARD_INFO,  parse_board_info),
    (MSP_BUILD_INFO,  parse_build_info),
    (MSP_STATUS,      parse_status),
]


def _gather_board_info(conn: SerialConnection) -> dict:
    """Run all identity MSP queries and return a merged dict."""
    info: dict = {}
    for cmd, parser in _IDENTITY_COMMANDS:
        try:
            payload = conn.send_msp_v1(cmd)
            info.update(parser(payload))
        except Exception as exc:
            info[f"_error_cmd_{cmd}"] = str(exc)
    return info


def _read_inav_status(conn: SerialConnection) -> dict:
    """Read FC status, preferring MSPV2_INAV_STATUS (the iNAV 9.x source of truth).

    The Configurator reads arming flags only from MSPV2_INAV_STATUS; the legacy
    MSP_STATUS (101) layout is unreliable for arming flags on current firmware.
    Falls back to MSP_STATUS v1 if the v2 command isn't supported (older builds).
    The returned dict carries a "_status_source" of "v2" or "v1".
    """
    from .msp import MSPV2_INAV_STATUS, parse_inav_status

    try:
        payload = conn.send_msp_v2(MSPV2_INAV_STATUS)
        status = parse_inav_status(payload)
        if status:
            status["_status_source"] = "v2"
            return status
    except Exception:
        pass   # fall through to v1

    payload = conn.send_msp_v1(MSP_STATUS)
    status = parse_status(payload)
    status["_status_source"] = "v1"
    # v1 carries no ARMED state bit reliably; derive best-effort from flags.
    status.setdefault("armed", bool(status.get("arming_disable_flags", 0) & 0x04))
    return status


# ── Tools: Connection & identity ──────────────────────────────────────────────

@mcp.tool()
def list_serial_ports() -> list[dict]:
    """List all available serial ports.

    Use this to find the COM port (Windows) or /dev/tty* (Linux/macOS) for the FC.
    Plug in the FC, run this, and look for a new entry (often labelled 'STM32' or
    'CP210x' for iNAV boards).
    """
    from serial.tools.list_ports import comports
    return [
        {
            "device":       p.device,
            "description":  p.description,
            "hwid":         p.hwid,
            "manufacturer": p.manufacturer,
        }
        for p in sorted(comports(), key=lambda p: p.device)
    ]


@mcp.tool()
def connect(port: str, baud: int = 115200) -> dict:
    """Open the serial connection to the FC and return board identity.

    Args:
        port: Serial port, e.g. 'COM3' (Windows) or '/dev/ttyACM0' (Linux).
        baud: Baud rate (default 115200 — matches iNAV's USB VCP default).

    Returns board variant, firmware version, target name, API version, build info,
    and a summary of detected sensors.
    """
    existing = state.get_connection()
    if existing and existing.is_open():
        existing.close()
        state.set_connection(None)

    conn = SerialConnection(port=port, baud=baud)
    try:
        conn.open()
    except Exception as exc:
        return {"connected": False, "port": port, "error": str(exc)}

    state.set_connection(conn)

    info = _gather_board_info(conn)
    info["connected"] = True
    info["port"]      = port
    info["baud"]      = baud
    return info


@mcp.tool()
def disconnect() -> dict:
    """Close the serial connection to the FC."""
    conn = state.get_connection()
    if conn is None:
        return {"disconnected": True, "note": "Was not connected."}
    port = conn.port
    conn.close()
    state.set_connection(None)
    return {"disconnected": True, "port": port}


@mcp.tool()
def board_info() -> dict:
    """Read flight-controller identity over MSP.

    Returns: FC variant (e.g. 'INAV'), firmware version, board/target name,
    API version, build date/time/git revision, and sensors detected.

    Requires an active connection (call connect first).
    """
    conn = state.require_connection()
    return _gather_board_info(conn)


# ── Tools: Config management (M1) ────────────────────────────────────────────

def _write_backup_file(diff_output: str, label: str | None) -> dict:
    """Write a diff-all dump to a timestamped backup file (no serial I/O)."""
    path = next_backup_path(label)
    with open(path, "w", encoding="utf-8") as f:
        f.write(diff_output)
        f.write("\n")
    return {
        "backup_path": path,
        "label": label,
        "lines": len(diff_output.splitlines()),
    }


def _do_backup(conn: SerialConnection, label: str | None = None) -> dict:
    """Standalone backup: 'diff all' → file.

    On iNAV, leaving the CLI reboots the FC, so this reboots and reconnects.
    """
    conn.enter_cli()
    try:
        diff_output = conn.run_cli("diff all", timeout=20.0)
    finally:
        conn.exit_cli(save=False, reconnect=True)   # exit reboots; reconnect restores MSP
    info = _write_backup_file(diff_output, label)
    info["rebooted"] = True
    return info


def _apply_cli_writes(
    conn: SerialConnection,
    commands: list[str],
    label: str,
    capture_after: bool = False,
) -> dict:
    """Atomic iNAV config write in ONE CLI session.

    Flow: armed-guard → enter CLI → 'diff all' (backup) → run write commands →
    optional 'diff all' (read-back) → save+reboot (or rollback on failure) → reconnect.

    iNAV reality: leaving the CLI always reboots, and `exit` DISCARDS unsaved
    changes — only `save` persists. So a config write is inherently a save+reboot.
    If any command errors, we `exit` instead of `save`, rolling back ALL changes
    (all-or-nothing) so the FC is never left half-configured.
    """
    check_not_armed(conn)

    conn.enter_cli()
    # 1) Backup the pre-change config (same session — no extra reboot).
    before = conn.run_cli("diff all", timeout=20.0)
    backup = _write_backup_file(before, label)

    # 2) Apply the write commands.
    applied: list[str]  = []
    failed:  list[dict] = []
    for cmd in commands:
        try:
            out = conn.run_cli(cmd, timeout=5.0)
            err = cli_error(out)   # iNAV rejects silently (no exception) — detect '### ERROR'
            if err:
                failed.append({"command": cmd, "error": err})
            else:
                applied.append(cmd)
        except Exception as exc:
            failed.append({"command": cmd, "error": str(exc)})

    # 3) Read back (only meaningful if we're going to keep the changes).
    after = None
    if capture_after and not failed:
        try:
            after = conn.run_cli("diff all", timeout=20.0)
        except Exception:
            after = None

    # 4) Persist or roll back, then reboot + reconnect.
    if failed:
        conn.exit_cli(save=False, reconnect=True)   # discard everything
        saved = False
    else:
        conn.exit_cli(save=True, reconnect=True)    # persist to EEPROM
        saved = True

    return {
        "applied":     applied,
        "failed":      failed,
        "backup_path": backup["backup_path"],
        "after_diff":  after,
        "saved":       saved,
        "rebooted":    True,
    }


def _apply_aux_commands(
    conn: SerialConnection,
    commands: list[str],
    label: str,
    extra: dict | None = None,
) -> dict:
    """Apply CLI 'aux' commands atomically (save+reboot), then read back via MSP.

    Shared by set_flight_mode / assign_switch / clear_flight_mode.
    """
    result = _apply_cli_writes(conn, commands, label=label, capture_after=False)
    if extra:
        result.update(extra)

    # Read-back verify over MSP (works after the post-save reconnect).
    if result["saved"]:
        try:
            result["active_modes_after"] = _modes.get_active_mode_ranges(conn)
        except Exception as exc:
            result["read_back_error"] = str(exc)

    result["note"] = (
        "Applied and SAVED to EEPROM; the FC rebooted and reconnected. Changes are persistent."
        if result["saved"] else
        f"{len(result['failed'])} command(s) failed — ALL changes were rolled back "
        "(exit without save). Nothing was persisted. See 'failed'."
    )
    return result


@mcp.tool()
def backup_config(label: str | None = None) -> dict:
    """Save the current FC config to a timestamped backup file.

    Runs 'diff all' in the CLI and writes the output to ./backups/<timestamp>.txt.

    NOTE: on iNAV, leaving the CLI reboots the FC, so this reboots and then
    auto-reconnects (~7s). The result includes 'rebooted': True.

    Args:
        label: Optional suffix added to the filename for easy identification.
    """
    conn = state.require_connection()
    return _do_backup(conn, label)


@mcp.tool()
def restore_config(path: str, confirm: bool = False) -> dict:
    """Restore FC config by replaying a backup file's CLI commands, then save+reboot.

    On iNAV this is atomic: a pre-restore backup is taken, the file's commands are
    replayed in one CLI session, then SAVED to EEPROM and the FC reboots (we
    reconnect). If any line errors, all changes are rolled back.

    Args:
        path:    Path to a backup file (as returned by backup_config).
        confirm: Must be True to apply. Dry-run by default.
    """
    if not os.path.isfile(path):
        return {"restored": False, "error": f"Backup file not found: {path}"}

    with open(path, encoding="utf-8") as f:
        lines = [
            ln.strip() for ln in f
            if ln.strip() and not ln.strip().startswith("#") and is_replayable(ln)
        ]

    if not confirm:
        return {
            "restored": False,
            "dry_run": True,
            "command_count": len(lines),
            "message": (
                f"Would replay {len(lines)} commands from '{path}', then save+reboot. "
                "Call with confirm=True to apply. A pre-restore backup is taken first."
            ),
        }

    conn = state.require_connection()
    result = _apply_cli_writes(conn, lines, label="pre-restore", capture_after=False)
    result["restored"] = result["saved"]
    result["note"] = (
        "Restored and SAVED — FC rebooted and reconnected."
        if result["saved"] else
        f"{len(result['failed'])} command(s) failed — restore rolled back (nothing saved)."
    )
    return result


@mcp.tool()
def cli(command: str, confirm_for_writes: bool = False) -> str:
    """Raw CLI escape hatch — run any iNAV CLI command directly.

    Read-only commands (diff, get, status, dump, tasks, help, version) run and the
    FC reboots on CLI exit (auto-reconnected). Write commands (set, aux, feature,
    smix, ...) require confirm_for_writes=True and are SAVED (persist + reboot),
    since on iNAV exiting CLI without save discards changes.

    Prefer the dedicated write tools (they back up and verify). Use this for
    one-off commands the other tools don't cover.

    Args:
        command:             The CLI command to run (without trailing newline).
        confirm_for_writes:  Set True to allow (and persist) write commands.
    """
    conn = state.require_connection()
    is_write = is_write_command(command)

    if is_write and not confirm_for_writes:
        return (
            f"⚠ '{command}' looks like a write operation. "
            "Set confirm_for_writes=True to execute it (it will be SAVED and the FC "
            "will reboot), or use a dedicated write tool which backs up and verifies."
        )

    conn.enter_cli()
    error = None
    try:
        output = conn.run_cli(command)
        error = cli_error(output)   # iNAV rejects silently (no exception) — detect '### ERROR'
    finally:
        # A rejected write changed nothing — don't persist it. Reads never save.
        # Either path reboots the FC; we reconnect.
        conn.exit_cli(save=(is_write and error is None), reconnect=True)

    if error:
        return f"{output}\n\n[⚠ command REJECTED by FC — nothing saved]"
    if is_write:
        return f"{output}\n\n[applied and SAVED; FC rebooted and reconnected]"
    return output


@mcp.tool()
def save_and_reboot(confirm: bool = False) -> dict:
    """Save the running config to EEPROM and reboot the FC.

    Takes a backup, then 'save' (persist + reboot), then auto-reconnects (~7s).

    NOTE: the dedicated write tools already save automatically on iNAV, so you
    rarely need this — it's for persisting changes made via the raw cli() reads
    or to force a clean reboot.

    Args:
        confirm: Must be True to proceed. Dry-run by default.
    """
    if not confirm:
        return {
            "saved": False,
            "dry_run": True,
            "warning": (
                "This will save config to EEPROM and REBOOT the FC "
                "(auto-reconnect ~7s). Call with confirm=True to proceed."
            ),
        }

    conn = state.require_connection()
    check_not_armed(conn)

    # Backup + save in ONE CLI session (one reboot).
    conn.enter_cli()
    before = conn.run_cli("diff all", timeout=20.0)
    backup = _write_backup_file(before, "pre-save")
    conn.exit_cli(save=True, reconnect=True)   # save + reboot + reconnect

    return {
        "saved": True,
        "rebooted": True,
        "backup_path": backup["backup_path"],
        "note": "Saved to EEPROM; FC rebooted and reconnected.",
    }


# ── Tools: Read-only diagnostics (M2) ────────────────────────────────────────

@mcp.tool()
def read_rc_channels() -> dict:
    """Read live RC channel values via MSP.

    Returns up to 16 channel values in microseconds (988–2012 µs typical).

    Core UX: flip a switch and watch which channel value changes — that's
    the aux channel to use when assigning flight modes.
    """
    conn = state.require_connection()
    payload  = conn.send_msp_v1(MSP_RC)
    channels = parse_rc_channels(payload)
    return {
        "channels": channels,
        "count":    len(channels),
        "labeled":  {
            f"CH{i + 1}{'_ROLL' if i==0 else '_PITCH' if i==1 else '_THR' if i==2 else '_YAW' if i==3 else f'_AUX{i-3}'}": v
            for i, v in enumerate(channels)
        },
        "tip": "Flip a switch and call read_rc_channels() again — the channel that moved is your aux channel.",
    }


@mcp.tool()
def read_sensors() -> dict:
    """Read live sensor values: attitude, per-sensor health, and analog (battery).

    Good for a quick sanity pass before flying:
    - Attitude should read ~0°/0° when the aircraft is level.
    - All required sensors (gyro, acc) should report OK.
    - Battery voltage should match your pack's cell count.
    """
    conn = state.require_connection()
    result: dict = {}

    for cmd, key, parser in [
        (MSP_ATTITUDE,     "attitude",      parse_attitude),
        (MSP_SENSOR_STATUS, "sensor_health", parse_sensor_status),
        (MSP_ANALOG,       "analog",        parse_analog),
    ]:
        try:
            result[key] = parser(conn.send_msp_v1(cmd))
        except Exception as exc:
            result[f"{key}_error"] = str(exc)

    return result


@mcp.tool()
def get_status() -> dict:
    """Read FC status via both MSP and CLI.

    Returns MSP_STATUS data (cycle time, sensor bits, CPU load, arming flags)
    plus the human-readable output of the CLI 'status' and 'tasks' commands.
    """
    conn = state.require_connection()
    result: dict = {}

    # MSP-only: on iNAV, leaving the CLI reboots the FC, so a routine status
    # check must not enter CLI. MSPV2_INAV_STATUS already carries the key data
    # (arming flags, sensors, CPU load, profile).
    try:
        result["msp_status"] = _read_inav_status(conn)
    except Exception as exc:
        result["msp_status_error"] = str(exc)

    result["note"] = (
        "Status read over MSP only (no reboot). For the human-readable CLI "
        "'status'/'tasks' text use cli('status') — note that exiting CLI reboots the FC."
    )
    return result


@mcp.tool()
def list_flight_modes() -> dict:
    """List all available flight modes and their current switch assignments.

    Returns:
      - all_modes: every mode the FC knows about (id + name + assigned flag)
      - assigned_modes: which modes are on which aux channel and range
      - unassigned_modes: mode names with no switch assignment yet
      - arm_assigned: bool — critical to know before first flight

    Tip: call read_rc_channels() to identify which channel your switches use,
    then use assign_switch() to map modes to them.
    """
    conn = state.require_connection()
    return _modes.list_all_modes_with_assignments(conn)


@mcp.tool()
def why_wont_it_arm() -> dict:
    """Decode the FC's arming-prevention flags into plain English.

    This is the #1 question from new iNAV users. Each set bit is mapped to:
      - A flag name
      - A plain-language reason
      - A concrete fix

    Also checks whether ARM mode is assigned to a switch.
    """
    from .msp import ARMING_DISABLE_REASON_MASK

    conn = state.require_connection()

    # Read status for arming flags (prefers MSPV2_INAV_STATUS; v1 fallback).
    try:
        status = _read_inav_status(conn)
    except Exception as exc:
        return {"error": f"Could not read FC status: {exc}"}

    arming_flags = status.get("arming_disable_flags", None)
    if arming_flags is None:
        return {
            "error": "FC did not return arming flags. "
                     "Older firmware? Try board_info() to check API version."
        }

    # Only bits 6..30 are arming-prevention reasons; bits 2/4 are state (ARMED/SIM).
    reason_flags = arming_flags & ARMING_DISABLE_REASON_MASK
    armed = bool(status.get("armed"))

    # Decode flag bits (decode_arming_flags maps every set bit via the JSON DB,
    # so ARMED/SIMULATOR appear as informational entries when present).
    flag_problems = _troubleshoot.decode_arming_flags(arming_flags)

    # Also check ARM mode assignment
    try:
        active_modes = _modes.get_active_mode_ranges(conn)
        arm_assigned = any(m["mode_name"] == "ARM" for m in active_modes)
    except Exception:
        active_modes = []
        arm_assigned = None   # unknown

    arm_problem = []
    if arm_assigned is False:
        arm_problem = [{
            "severity": "error",
            "title":    "ARM mode not assigned to any switch",
            "detail":   "No aux channel/range is configured for ARM.",
            "fix":      "Use assign_switch() to add an ARM assignment, e.g. assign ARM to the high "
                        "position (1800–2100 µs) of a 2-position switch.",
            "source":   "mode_ranges",
        }]

    all_problems = flag_problems + arm_problem
    _SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    all_problems.sort(key=lambda p: _SEVERITY_ORDER.get(p.get("severity", "info"), 3))

    can_arm = (reason_flags == 0) and (arm_assigned is True)
    if armed:
        note = "FC is currently ARMED."
    elif reason_flags == 0 and arm_assigned:
        note = "No arming-prevention flags set. Toggle the ARM switch to arm."
    else:
        note = "Fix the problems listed above before attempting to arm."

    return {
        "arming_disable_flags":     arming_flags,
        "arming_disable_flags_hex": hex(arming_flags),
        "armed":                    armed,
        "can_arm":                  can_arm,
        "arm_mode_assigned":        arm_assigned,
        "status_source":            status.get("_status_source"),
        "problem_count":            len(all_problems),
        "problems":                 all_problems,
        "note":                     note,
    }


@mcp.tool()
def diagnose() -> dict:
    """Full diagnostic sweep — the flagship troubleshooter.

    Collects: arming flags, sensor health, RC channels, battery, GPS, attitude,
    and mode assignments. Runs all diagnostic rules and returns a prioritized
    problem list with concrete fixes for each issue found.

    Use this when 'something is wrong but I'm not sure what.'
    """
    conn = state.require_connection()
    raw: dict = {}

    # Status comes from MSPV2_INAV_STATUS (v1 fallback) — the reliable arming source.
    try:
        raw["status"] = _read_inav_status(conn)
    except Exception as exc:
        raw["status_error"] = str(exc)

    # Collect remaining MSP data — best-effort, failures are noted in raw
    for cmd, key, parser in [
        (MSP_SENSOR_STATUS, "sensor_health", parse_sensor_status),
        (MSP_RC,            "rc_channels",   parse_rc_channels),
        (MSP_ANALOG,        "analog",        parse_analog),
        (MSP_ATTITUDE,      "attitude",      parse_attitude),
        (MSP_RAW_GPS,       "gps",           parse_raw_gps),
    ]:
        try:
            raw[key] = parser(conn.send_msp_v1(cmd))
        except Exception as exc:
            raw[f"{key}_error"] = str(exc)

    # Collect mode assignments
    try:
        raw["active_modes"] = _modes.get_active_mode_ranges(conn)
    except Exception as exc:
        raw["active_modes_error"] = str(exc)

    # Run all diagnostic checks
    problems = _troubleshoot.run_checks(raw)

    return {
        "status":          "healthy" if not problems else "problems_found",
        "problem_count":   len(problems),
        "critical_count":  sum(1 for p in problems if p.get("severity") == "critical"),
        "error_count":     sum(1 for p in problems if p.get("severity") == "error"),
        "warning_count":   sum(1 for p in problems if p.get("severity") == "warning"),
        "problems":        problems,
        "raw":             raw,
    }


# ── Tools: Hardware setup (M3) ───────────────────────────────────────────────

@mcp.tool()
def define_aircraft(
    name: str,
    wing_type: str,
    esc_protocol: str,
    cells: int,
    fc_target: str | None = None,
    motor_kv: int | None = None,
    motor_poles: int = 14,
    servo_count: int | None = None,
    notes: str | None = None,
) -> dict:
    """Define the aircraft hardware profile and generate a configuration plan.

    This is an OFFLINE planner — no FC connection required. Call it first to
    review the generated CLI commands, then call apply_aircraft_setup() to apply.

    Args:
        name:         A descriptive name for this aircraft (e.g., "My FPV Wing").
        wing_type:    One of: flying_wing, conventional, vtail, twin_tail, delta.
        esc_protocol: One of: DSHOT600, DSHOT300, DSHOT150, MULTISHOT, ONESHOT125, PWM.
        cells:        LiPo battery cell count (e.g. 4 for 4S).
        fc_target:    Optional FC board name (e.g. MATEKF405). Used for target hints.
        motor_kv:     Optional motor KV rating (informational only).
        motor_poles:  Motor pole count for RPM telemetry (default 14; verify with your motor).
        servo_count:  Optional total servo count (informational).
        notes:        Free text notes to store with the profile.

    Returns a plan dict with:
        - profile: the stored profile
        - commands: the CLI commands to apply
        - summary: human-readable description
        - warnings: things to verify before/after applying
    """
    try:
        profile = AircraftProfile(
            name=name,
            wing_type=wing_type.lower(),
            esc_protocol=esc_protocol.upper(),
            cells=cells,
            fc_target=fc_target,
            motor_kv=motor_kv,
            motor_poles=motor_poles,
            servo_count=servo_count,
            notes=notes,
        )
    except ValueError as exc:
        return {
            "error": str(exc),
            "supported_wing_types": SUPPORTED_WING_TYPES,
        }

    try:
        plan = generate_cli_commands(profile)
    except ValueError as exc:
        return {"error": str(exc)}

    state.set_profile(profile)

    return {
        "profile":   profile.to_dict(),
        "commands":  plan["commands"],
        "summary":   plan["summary"],
        "warnings":  plan["warnings"],
        "next_step": (
            "Review the commands above. When ready, call "
            "apply_aircraft_setup(confirm=True) to apply them to the FC."
        ),
    }


@mcp.tool()
def get_aircraft_profile() -> dict:
    """Return the currently declared aircraft profile.

    Call define_aircraft() first to set a profile.
    """
    p = state.get_profile()
    if p is None:
        return {
            "profile": None,
            "message": "No aircraft profile defined. Call define_aircraft() first.",
            "supported_wing_types": SUPPORTED_WING_TYPES,
        }
    plan = generate_cli_commands(p)
    return {
        "profile":  p.to_dict(),
        "commands": plan["commands"],
        "summary":  plan["summary"],
    }


@mcp.tool()
def apply_aircraft_setup(confirm: bool = False) -> dict:
    """Apply the declared aircraft profile to the FC, then save and reboot.

    On iNAV this is an ATOMIC operation: the commands are applied in one CLI
    session, backed up first, then SAVED to EEPROM (the only way changes persist)
    and the FC reboots. We reconnect automatically and verify by read-back.
    If any command fails, ALL changes are rolled back (nothing is saved).

    Gates:
      - A profile must be defined via define_aircraft().
      - Connected and not armed.
      - confirm=True required (dry-run by default).

    Args:
        confirm: True to apply+save+reboot. Default False = dry-run (shows commands).
    """
    profile = state.require_profile()
    plan    = generate_cli_commands(profile)
    commands = plan["commands"]

    if not confirm:
        return {
            "dry_run":   True,
            "profile":   profile.to_dict(),
            "commands":  commands,
            "warnings":  plan["warnings"],
            "message":   (
                "Dry run — call apply_aircraft_setup(confirm=True) to apply. "
                "NOTE: on iNAV, applying saves to EEPROM and reboots the FC "
                "(this is the only way config changes take effect)."
            ),
        }

    conn = state.require_connection()
    result = _apply_cli_writes(
        conn, commands, label=f"pre-apply-{profile.name}", capture_after=True,
    )
    result["profile"] = profile.to_dict()

    # Lint the post-apply diff against the profile (captured in the same session).
    if result["saved"] and result.get("after_diff"):
        mismatches = _profiles.lint_diff_against_profile(result["after_diff"], profile)
        result["mismatches"]     = mismatches
        result["mismatch_count"] = len(mismatches)
        result["read_back_diff"] = result["after_diff"][:2000]
        result["note"] = (
            "Applied, SAVED, and verified — FC rebooted and reconnected. Settings are persistent."
            if not mismatches else
            f"Applied and saved, but {len(mismatches)} setting(s) don't match the profile — "
            "see 'mismatches'. Some may be values the FC normalised; review them."
        )
    elif result["saved"]:
        result["note"] = "Applied and SAVED — FC rebooted and reconnected."
    else:
        result["note"] = (
            f"{len(result['failed'])} command(s) failed — ALL changes were rolled back "
            "(nothing saved). See 'failed'."
        )
    result.pop("after_diff", None)   # don't duplicate; surfaced as read_back_diff
    return result


@mcp.tool()
def check_config() -> dict:
    """Compare the FC's actual configuration against the declared aircraft profile.

    Runs 'diff all' via CLI, parses key settings, and reports mismatches against
    the profile declared with define_aircraft(). Also checks if ARM mode is
    assigned to a switch.

    Useful after apply_aircraft_setup() or to sanity-check a partially configured FC.
    """
    conn = state.require_connection()
    result: dict = {}

    # ARM assignment via MSP FIRST (before CLI, which reboots on exit).
    try:
        active_modes = _modes.get_active_mode_ranges(conn)
        arm_assigned = any(m["mode_name"] == "ARM" for m in active_modes)
        result["arm_mode_assigned"] = arm_assigned
        if not arm_assigned:
            result["arm_warning"] = (
                "ARM mode is not assigned to any switch. "
                "Use assign_switch() or set_flight_mode('ARM', ...) to add one."
            )
    except Exception as exc:
        result["arm_check_error"] = str(exc)

    # 'diff all' (CLI) — exiting CLI reboots the FC, so reconnect afterward.
    conn.enter_cli()
    try:
        diff_output = conn.run_cli("diff all", timeout=15.0)
    finally:
        conn.exit_cli(save=False, reconnect=True)

    result["diff_all"] = diff_output
    result["rebooted"] = True

    # Lint against profile (if one is defined)
    profile = state.get_profile()
    if profile:
        mismatches = _profiles.lint_diff_against_profile(diff_output, profile)
        result["profile"]        = profile.to_dict()
        result["mismatches"]     = mismatches
        result["mismatch_count"] = len(mismatches)
    else:
        result["profile"]       = None
        result["profile_note"]  = (
            "No aircraft profile defined — call define_aircraft() to enable profile comparison."
        )

    # Summary
    issues = []
    if profile and result.get("mismatches"):
        issues.append(f"{result['mismatch_count']} profile mismatch(es)")
    if not result.get("arm_mode_assigned", True):
        issues.append("ARM not assigned")

    result["status"] = "ok" if not issues else f"issues: {', '.join(issues)}"

    return result


# ── Tools: Flight modes & switches (M4) ──────────────────────────────────────

@mcp.tool()
def suggest_mode_layout(
    skill_level: str = "beginner",
    num_switches: int = 2,
    has_gps: bool = False,
) -> dict:
    """Recommend a fixed-wing flight-mode/switch layout. Pure knowledge — no FC needed.

    Suggests which switches to use for ARM, flight modes (ANGLE/HORIZON/MANUAL),
    and (with GPS) NAV RTH / NAV LAUNCH, with the exact aux channels and µs ranges.

    Args:
        skill_level:  beginner | intermediate | advanced (tailors the advice).
        num_switches: How many spare switches you have available.
        has_gps:      True if a GPS module is installed (enables RTH suggestion).

    Returns a layout plan you can hand to assign_switch() to apply.
    """
    return _modes.suggest_layout(skill_level, num_switches, has_gps)


@mcp.tool()
def set_flight_mode(
    mode_name: str,
    aux_channel: int,
    range_low: int,
    range_high: int,
    confirm: bool = False,
) -> dict:
    """Assign a flight mode to an aux channel range (read-modify-write via CLI 'aux').

    Finds an existing slot for this mode+channel (to modify) or the first free slot
    (to create), then writes the assignment. Dry-run by default.

    Gates: connected, not armed, auto-backup before write. Requires confirm=True to apply.

    Args:
        mode_name:   Exact mode name as shown by list_flight_modes (e.g. "ANGLE", "NAV RTH").
        aux_channel: 1-based AUX number (1 = AUX1 = RC channel 5), matching list_flight_modes.
        range_low:   Range start in µs (900–2100).
        range_high:  Range end in µs (must be > range_low, ≤ 2100).
        confirm:     True to apply. Default False = dry-run (returns the command only).
    """
    conn = state.require_connection()

    try:
        plan = _modes.plan_set_mode(conn, mode_name, aux_channel, range_low, range_high)
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc)}

    if not confirm:
        return {
            "dry_run": True,
            "plan":    plan,
            "message": (
                f"Dry run — would {plan['action']} slot {plan['slot']}: {plan['command']!r}. "
                "Call set_flight_mode(..., confirm=True) to apply."
            ),
        }

    return _apply_aux_commands(conn, [plan["command"]],
                               label=f"set-{mode_name}",
                               extra={"plan": plan})


@mcp.tool()
def assign_switch(
    switch_channel: int,
    switch_positions: int,
    mode_per_position: dict,
    confirm: bool = False,
) -> dict:
    """Map a multi-position switch's detents to flight modes in one call.

    Computes the µs range for each detent and writes one 'aux' assignment per mode.
    Dry-run by default. Gates: connected, not armed, auto-backup before write.

    Args:
        switch_channel:    1-based AUX number the switch is on (1 = AUX1 = RC channel 5).
        switch_positions:  Number of detents on the switch: 2, 3, or 6.
        mode_per_position: Map of position → mode name. Position keys may be
                           "low"/"mid"/"high" (for 2/3-pos) or "1".."N" / "pos1"..,
                           e.g. {"low": "ANGLE", "mid": "HORIZON", "high": "MANUAL"}.
        confirm:           True to apply. Default False = dry-run.
    """
    conn = state.require_connection()

    if switch_positions not in (2, 3, 6):
        return {"error": f"switch_positions must be 2, 3, or 6 (got {switch_positions})."}

    bands = _modes.position_ranges(switch_positions)

    # Resolve each requested position → range → concrete aux command plan.
    plans: list[dict] = []
    try:
        for pos_key, mode_name in mode_per_position.items():
            idx = _modes.resolve_position_index(pos_key, switch_positions)
            lo, hi = bands[idx]
            plan = _modes.plan_set_mode(conn, mode_name, switch_channel, lo, hi)
            plan["position"] = pos_key
            plans.append(plan)
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc)}

    # Guard against two positions resolving to the same slot (would clobber).
    slots_used = [p["slot"] for p in plans]
    if len(set(slots_used)) != len(slots_used):
        return {
            "error": "Two positions resolved to the same mode-range slot — apply them "
                     "individually with set_flight_mode(), or clear conflicting modes first.",
            "plans": plans,
        }

    commands = [p["command"] for p in plans]

    if not confirm:
        return {
            "dry_run":  True,
            "switch_channel":   switch_channel,
            "switch_positions": switch_positions,
            "plans":    plans,
            "commands": commands,
            "message":  "Dry run — call assign_switch(..., confirm=True) to apply.",
        }

    return _apply_aux_commands(conn, commands,
                               label=f"assign-AUX{switch_channel}",
                               extra={"plans": plans})


@mcp.tool()
def clear_flight_mode(mode_name: str, confirm: bool = False) -> dict:
    """Remove all switch assignments for a flight mode (disables its slots via CLI 'aux').

    Dry-run by default. Gates: connected, not armed, auto-backup before write.

    Args:
        mode_name: Exact mode name (e.g. "HORIZON", "NAV RTH").
        confirm:   True to apply. Default False = dry-run.
    """
    conn = state.require_connection()

    try:
        plan = _modes.plan_clear_mode(conn, mode_name)
    except ValueError as exc:
        return {"error": str(exc)}

    if plan["found"] == 0:
        return {
            "cleared":  False,
            "mode_name": mode_name,
            "message":  f"{mode_name} is not assigned to any switch — nothing to clear.",
        }

    if not confirm:
        return {
            "dry_run":  True,
            "mode_name": mode_name,
            "slots":    plan["slots"],
            "commands": plan["commands"],
            "message":  f"Dry run — would disable {plan['found']} slot(s) for {mode_name}. "
                        "Call clear_flight_mode(..., confirm=True) to apply.",
        }

    return _apply_aux_commands(conn, plan["commands"],
                               label=f"clear-{mode_name}",
                               extra={"mode_name": mode_name, "slots": plan["slots"]})


# ── Resources (M5): read-only context Claude can pull ────────────────────────

_KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")


@mcp.resource(
    "inav://modes-reference",
    name="iNAV mode reference",
    description="Glossary of iNAV flight modes with fixed-wing relevance notes.",
    mime_type="application/json",
)
def modes_reference_resource() -> str:
    """The fixed-wing-oriented mode glossary (knowledge/modes_reference.json)."""
    with open(os.path.join(_KNOWLEDGE_DIR, "modes_reference.json"), encoding="utf-8") as f:
        return f.read()


@mcp.resource(
    "inav://current-profile",
    name="Current aircraft profile",
    description="The aircraft profile declared via define_aircraft(), with its generated CLI plan.",
    mime_type="application/json",
)
def current_profile_resource() -> str:
    """The declared AircraftProfile + command plan, or a hint if none is set."""
    import json
    p = state.get_profile()
    if p is None:
        return json.dumps({
            "profile": None,
            "message": "No aircraft profile defined. Call define_aircraft() to create one.",
            "supported_wing_types": SUPPORTED_WING_TYPES,
        }, indent=2)
    plan = generate_cli_commands(p)
    return json.dumps({
        "profile":  p.to_dict(),
        "summary":  plan["summary"],
        "commands": plan["commands"],
        "warnings": plan["warnings"],
    }, indent=2)


@mcp.resource(
    "inav://last-backup",
    name="Last config backup",
    description="The most recent 'diff all' backup written under ./backups/.",
    mime_type="text/plain",
)
def last_backup_resource() -> str:
    """Contents of the newest backup file, or a hint if there are none."""
    if not os.path.isdir(BACKUPS_DIR):
        return "No backups yet. Run backup_config() or any write tool to create one."
    files = [
        os.path.join(BACKUPS_DIR, f)
        for f in os.listdir(BACKUPS_DIR)
        if f.endswith(".txt")
    ]
    if not files:
        return "No backups yet. Run backup_config() or any write tool to create one."
    newest = max(files, key=os.path.getmtime)
    with open(newest, encoding="utf-8") as f:
        body = f.read()
    return f"# Backup file: {os.path.basename(newest)}\n\n{body}"


# ── Prompts (M5): guided workflows ───────────────────────────────────────────

@mcp.prompt(
    name="new_fixed_wing_setup",
    title="Set up a new fixed-wing aircraft",
    description="Guided walkthrough: gather hardware details, define the aircraft, review, and apply.",
)
def new_fixed_wing_setup_prompt() -> str:
    return (
        "Help me set up a new fixed-wing aircraft in iNAV from scratch.\n\n"
        "Walk me through it step by step:\n"
        "1. First call list_serial_ports() and connect() if not already connected, "
        "then board_info() to confirm the FC target and firmware.\n"
        "2. Ask me the hardware questions one at a time: wing type "
        "(flying_wing / conventional / vtail / twin_tail / delta), ESC protocol "
        "(DSHOT600 / DSHOT300 / MULTISHOT / ONESHOT125 / PWM), battery cell count, "
        "motor pole count if known, and whether I have GPS.\n"
        "3. Call define_aircraft() with my answers and show me the generated CLI "
        "commands and warnings for review. Do NOT apply yet.\n"
        "4. After I approve, call apply_aircraft_setup(confirm=True), then "
        "check_config() to verify the read-back matches.\n"
        "5. Remind me to bench-test servo directions (props off!) before flying, "
        "and to call save_and_reboot(confirm=True) once I'm happy.\n\n"
        "Explain each step in plain language — assume I'm new to iNAV."
    )


@mcp.prompt(
    name="troubleshoot_no_arm",
    title="Troubleshoot why the FC won't arm",
    description="Runs the arming diagnostics and walks through fixes for each blocking flag.",
)
def troubleshoot_no_arm_prompt() -> str:
    return (
        "My iNAV flight controller won't arm. Help me figure out why and fix it.\n\n"
        "1. Make sure we're connected (list_serial_ports / connect if needed).\n"
        "2. Call why_wont_it_arm() and explain each arming-prevention flag in plain "
        "language, most critical first.\n"
        "3. For each problem, give me the concrete fix and offer to apply it "
        "(dry-run first, then confirm=True after I agree).\n"
        "4. If ARM isn't assigned to a switch, help me assign it: have me flip my "
        "arm switch, use read_rc_channels() to find its channel, then assign_switch() "
        "or set_flight_mode('ARM', ...).\n"
        "5. Re-run why_wont_it_arm() to confirm the issue is resolved.\n\n"
        "Don't make any config changes without showing me the dry-run first."
    )


@mcp.prompt(
    name="configure_modes",
    title="Configure flight modes and switches",
    description="Identify switches via live RC, suggest a layout, and assign modes.",
)
def configure_modes_prompt() -> str:
    return (
        "Help me set up my flight modes and switch assignments for a fixed wing.\n\n"
        "1. Connect if needed, then call suggest_mode_layout() — ask me my skill "
        "level, how many spare switches I have, and whether I have GPS.\n"
        "2. For each switch in the suggested layout, have me flip it and call "
        "read_rc_channels() so we confirm which RC/AUX channel it actually uses.\n"
        "3. Use assign_switch() to map each switch's positions to modes — show me "
        "the dry-run first, then apply with confirm=True after I approve.\n"
        "4. Call list_flight_modes() to confirm everything is assigned correctly, "
        "and remind me to verify in the iNAV Configurator Modes tab too.\n"
        "5. Once I'm happy, remind me to save_and_reboot(confirm=True) to persist.\n\n"
        "Make sure ARM ends up on its own dedicated 2-position switch."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
