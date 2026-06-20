"""Box-name / box-id mapping and mode-range reading.

Never hardcode box IDs — they are assigned by the firmware at build time and
can shift between versions. Always resolve them at runtime via MSP_BOXNAMES +
MSP_BOXIDS (§6 of the spec).
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connection import SerialConnection

from .msp import (
    MSP_BOXNAMES,
    MSP_BOXIDS,
    MSP_MODE_RANGES,
    parse_boxnames,
    parse_boxids,
    parse_mode_ranges,
)


def _read_msp(conn: "SerialConnection", cmd: int, timeout: float = 2.0) -> bytes:
    """Read an MSP command, preferring MSP v2.

    Critical for MSP_BOXNAMES: on iNAV the mode-name string exceeds 255 bytes,
    which overflows the MSP v1 size byte and corrupts the frame. MSP v2 has a
    16-bit length field and returns it intact. Falls back to v1 for any firmware
    that doesn't answer v2.
    """
    try:
        return conn.send_msp_v2(cmd, timeout=timeout)
    except Exception:
        return conn.send_msp_v1(cmd, timeout=timeout)


def get_box_map(conn: "SerialConnection") -> tuple[dict[int, str], dict[str, int]]:
    """Return ({box_id → name}, {name → box_id}) by querying the FC.

    Both lookups are needed: id→name for annotating mode ranges,
    name→id for finding ARM's slot when writing.
    """
    names_payload = _read_msp(conn, MSP_BOXNAMES)
    ids_payload   = _read_msp(conn, MSP_BOXIDS)

    names = parse_boxnames(names_payload)
    ids   = parse_boxids(ids_payload)

    # Trim to the shorter list in case of FW mismatch
    n = min(len(names), len(ids))
    id_to_name = {ids[i]: names[i] for i in range(n)}
    name_to_id = {names[i]: ids[i] for i in range(n)}
    return id_to_name, name_to_id


def get_active_mode_ranges(conn: "SerialConnection") -> list[dict]:
    """Read MSP_MODE_RANGES and return only enabled slots, annotated with mode names."""
    id_to_name, _ = get_box_map(conn)

    payload = _read_msp(conn, MSP_MODE_RANGES)
    ranges  = parse_mode_ranges(payload)

    result = []
    for r in ranges:
        if not r["enabled"]:
            continue
        r = dict(r)   # copy so we don't mutate the parser output
        r["mode_name"] = id_to_name.get(r["box_id"], f"UNKNOWN_BOX_{r['box_id']}")
        result.append(r)
    return result


def list_all_modes_with_assignments(conn: "SerialConnection") -> dict:
    """Return a full picture: all available modes + which are assigned.

    Used by the list_flight_modes tool.
    """
    id_to_name, _ = get_box_map(conn)

    payload = _read_msp(conn, MSP_MODE_RANGES)
    all_ranges = parse_mode_ranges(payload)

    # Map: mode_name → list of assignments
    assignments: dict[str, list[dict]] = {}
    for r in all_ranges:
        if not r["enabled"]:
            continue
        name = id_to_name.get(r["box_id"], f"UNKNOWN_BOX_{r['box_id']}")
        entry = {
            "slot":            r["slot"],
            "box_id":          r["box_id"],
            "aux_channel":     r["aux_channel_1"],   # 1-based for readability
            "range_start_ms":  r["range_start_ms"],
            "range_end_ms":    r["range_end_ms"],
            "range_str":       f"{r['range_start_ms']}–{r['range_end_ms']} µs",
        }
        assignments.setdefault(name, []).append(entry)

    all_mode_list = [
        {"id": box_id, "name": name, "assigned": name in assignments}
        for box_id, name in sorted(id_to_name.items(), key=lambda x: x[0])
    ]

    assigned_list = [
        {
            "mode_name": name,
            "assignments": entries,
        }
        for name, entries in sorted(assignments.items())
    ]

    unassigned = sorted(
        name for name in id_to_name.values() if name not in assignments
    )

    return {
        "all_modes":       all_mode_list,
        "assigned_modes":  assigned_list,
        "unassigned_modes": unassigned,
        "arm_assigned":    "ARM" in assignments,
    }


# ── M4: mode-range write helpers ──────────────────────────────────────────────

# iNAV mode-range µs bounds. Range µs = 900 + step*25; step 0..48 → 900..2100.
MIN_RANGE_US = 900
MAX_RANGE_US = 2100
MAX_MODE_SLOTS = 40   # MAX_MODE_ACTIVATION_CONDITION_COUNT in iNAV


def get_all_mode_ranges(conn: "SerialConnection") -> list[dict]:
    """Read MSP_MODE_RANGES and return ALL slots (enabled and disabled), annotated.

    Unlike get_active_mode_ranges (which filters to enabled), this returns every
    slot so callers can find free slots and existing assignments for read-modify-write.
    """
    id_to_name, _ = get_box_map(conn)
    payload = _read_msp(conn, MSP_MODE_RANGES)
    ranges  = parse_mode_ranges(payload)
    for r in ranges:
        r["mode_name"] = id_to_name.get(r["box_id"], f"UNKNOWN_BOX_{r['box_id']}")
    return ranges


def build_aux_command(
    slot: int,
    box_id: int,
    aux_channel_index: int,
    range_low_us: int,
    range_high_us: int,
    logic: int = 0,
    linked_to: int = 0,
) -> str:
    """Build an iNAV CLI 'aux' command line.

    Format: aux <slot> <modeId> <auxChannelIndex> <startUs> <endUs> <logic> <linkedTo>
    Microseconds are accepted directly; the FC converts to 25µs steps internally.
    This matches the format produced by 'diff'/'dump'.
    """
    return (
        f"aux {slot} {box_id} {aux_channel_index} "
        f"{range_low_us} {range_high_us} {logic} {linked_to}"
    )


def position_ranges(num_positions: int) -> list[tuple[int, int]]:
    """Split the 900–2100µs span into N equal detent bands.

    2-pos → [(900,1500),(1500,2100)]
    3-pos → [(900,1300),(1300,1700),(1700,2100)]
    6-pos → 200µs bands from 900 to 2100.
    """
    if num_positions < 1:
        raise ValueError("num_positions must be ≥ 1")
    span  = MAX_RANGE_US - MIN_RANGE_US
    width = span // num_positions
    bands: list[tuple[int, int]] = []
    for i in range(num_positions):
        low  = MIN_RANGE_US + i * width
        high = MAX_RANGE_US if i == num_positions - 1 else MIN_RANGE_US + (i + 1) * width
        bands.append((low, high))
    return bands


_POSITION_LABELS: dict[int, list[str]] = {
    2: ["low", "high"],
    3: ["low", "mid", "high"],
    6: ["pos1", "pos2", "pos3", "pos4", "pos5", "pos6"],
}


def resolve_position_index(key: str, num_positions: int) -> int:
    """Map a position key ('low'/'mid'/'high' or '1'..'N' or 'pos1'..) to a 0-based index."""
    labels = _POSITION_LABELS.get(num_positions, [f"pos{i+1}" for i in range(num_positions)])
    k = str(key).strip().lower()
    # Named label
    if k in labels:
        return labels.index(k)
    # 'posN'
    if k.startswith("pos") and k[3:].isdigit():
        idx = int(k[3:]) - 1
        if 0 <= idx < num_positions:
            return idx
    # Plain 1-based number
    if k.isdigit():
        idx = int(k) - 1
        if 0 <= idx < num_positions:
            return idx
    raise ValueError(
        f"Unknown switch position {key!r} for a {num_positions}-position switch. "
        f"Use one of {labels} or 1–{num_positions}."
    )


def plan_set_mode(
    conn: "SerialConnection",
    mode_name: str,
    aux_channel: int,
    range_low_us: int,
    range_high_us: int,
) -> dict:
    """Resolve a mode assignment to a concrete CLI 'aux' command (read-modify-write).

    aux_channel is the 1-based AUX number (1 = AUX1 = RC channel 5), matching what
    list_flight_modes displays. Returns a plan dict (does NOT apply anything):
      { command, slot, box_id, mode_name, aux_channel, aux_channel_index,
        range_low_us, range_high_us, action }   ('modify' or 'create')
    Raises ValueError on unknown mode name or invalid range.
    """
    _, name_to_id = get_box_map(conn)
    if mode_name not in name_to_id:
        available = ", ".join(sorted(name_to_id))
        raise ValueError(
            f"Unknown mode {mode_name!r}. Available modes on this FC: {available}"
        )
    box_id = name_to_id[mode_name]

    if aux_channel < 1:
        raise ValueError(f"aux_channel must be ≥ 1 (1 = AUX1 = RC channel 5), got {aux_channel}")
    aux_index = aux_channel - 1

    lo = int(range_low_us)
    hi = int(range_high_us)
    if not (MIN_RANGE_US <= lo < hi <= MAX_RANGE_US):
        raise ValueError(
            f"Invalid range {lo}–{hi}µs. Must satisfy "
            f"{MIN_RANGE_US} ≤ low < high ≤ {MAX_RANGE_US}."
        )

    all_slots = get_all_mode_ranges(conn)

    # Reuse an existing slot for this exact mode+channel if present, else first free slot.
    slot = None
    action = "create"
    for r in all_slots:
        if r["box_id"] == box_id and r["aux_channel"] == aux_index and r["enabled"]:
            slot = r["slot"]
            action = "modify"
            break
    if slot is None:
        for r in all_slots:
            if not r["enabled"]:
                slot = r["slot"]
                break
    if slot is None:
        raise RuntimeError(
            f"No free mode-range slots available (all {len(all_slots)} in use). "
            "Clear an unused mode with clear_flight_mode() first."
        )

    return {
        "command":           build_aux_command(slot, box_id, aux_index, lo, hi),
        "slot":              slot,
        "box_id":            box_id,
        "mode_name":         mode_name,
        "aux_channel":       aux_channel,
        "aux_channel_index": aux_index,
        "range_low_us":      lo,
        "range_high_us":     hi,
        "action":            action,
    }


def plan_clear_mode(conn: "SerialConnection", mode_name: str) -> dict:
    """Build CLI commands to disable every enabled slot assigned to a mode.

    Disabling = writing start==end (an unusable range), preserving the slot's
    box_id/channel. Returns { mode_name, box_id, commands, slots, found }.
    Raises ValueError on unknown mode name.
    """
    _, name_to_id = get_box_map(conn)
    if mode_name not in name_to_id:
        available = ", ".join(sorted(name_to_id))
        raise ValueError(
            f"Unknown mode {mode_name!r}. Available modes on this FC: {available}"
        )
    box_id = name_to_id[mode_name]

    all_slots = get_all_mode_ranges(conn)
    commands: list[str] = []
    slots: list[int] = []
    for r in all_slots:
        if r["box_id"] == box_id and r["enabled"]:
            # start==end disables the slot (IS_RANGE_USABLE: startStep < endStep)
            commands.append(
                build_aux_command(
                    r["slot"], box_id, r["aux_channel"],
                    MIN_RANGE_US, MIN_RANGE_US,
                    r["mode_logic"], r["linked_to"],
                )
            )
            slots.append(r["slot"])

    return {
        "mode_name": mode_name,
        "box_id":    box_id,
        "commands":  commands,
        "slots":     slots,
        "found":     len(slots),
    }


# ── M4: layout suggestion (pure knowledge, no FC required) ─────────────────────

def suggest_layout(skill_level: str, num_switches: int, has_gps: bool = False) -> dict:
    """Recommend a fixed-wing switch/mode layout. Pure knowledge — no FC needed.

    Allocates AUX channels sequentially (AUX1 = RC channel 5, AUX2 = CH6, …):
      Switch 1 (2-pos): ARM
      Switch 2 (3-pos): flight modes (ANGLE / HORIZON / MANUAL)
      Switch 3 (2-pos): NAV RTH        (only if has_gps)
      Switch 4 (2-pos): NAV LAUNCH     (recommended for wings; advanced/extra)
    """
    skill = (skill_level or "beginner").strip().lower()
    if skill not in ("beginner", "intermediate", "advanced"):
        skill = "beginner"

    switches: list[dict] = []
    notes: list[str] = []
    aux = 1   # 1-based AUX number

    def _switch(num_pos: int, purpose: str, assigns: list[dict]) -> None:
        nonlocal aux
        switches.append({
            "switch":      f"Switch {len(switches)+1}",
            "aux_channel": aux,
            "rc_channel":  aux + 4,                 # AUX1 = RC channel 5
            "type":        f"{num_pos}-position",
            "purpose":     purpose,
            "assignments": assigns,
        })
        aux += 1

    # 1) ARM — always, dedicated 2-position switch
    if num_switches >= 1:
        lo, hi = position_ranges(2)[1]   # high band = "on"
        _switch(2, "ARM (motor arming)", [
            {"mode": "ARM", "position": "high (on)", "range_low": lo, "range_high": hi},
        ])
    else:
        notes.append("At least one switch is required for ARM — the FC cannot arm without it.")

    # 2) Flight modes — 3-position switch
    if num_switches >= 2:
        bands  = position_ranges(3)
        modes  = ["ANGLE", "HORIZON", "MANUAL"]
        labels = ["low", "mid", "high"]
        assigns = [
            {"mode": m, "position": lbl, "range_low": b[0], "range_high": b[1]}
            for m, lbl, b in zip(modes, labels, bands)
        ]
        _switch(3, "Flight modes (stabilisation)", assigns)
        if skill == "beginner":
            notes.append(
                "Beginner: spend your first flights entirely in ANGLE (switch low). "
                "Only try HORIZON/MANUAL once comfortable — MANUAL has no stabilisation."
            )
        elif skill == "advanced":
            notes.append(
                "Advanced: leave the low position unassigned if you prefer ACRO (rate) "
                "as your default; remove the ANGLE assignment to do so."
            )
    else:
        notes.append("With only one switch, dedicate it to ARM and fly in the default mode.")

    # 3) NAV RTH — only with GPS
    if has_gps and num_switches >= 3:
        lo, hi = position_ranges(2)[1]
        _switch(2, "NAV RTH (return to home — safety)", [
            {"mode": "NAV RTH", "position": "high (on)", "range_low": lo, "range_high": hi},
        ])
        notes.append("Set 'nav_rth_altitude' to a safe height for your field before relying on RTH.")
    elif has_gps:
        notes.append("You have GPS but not enough switches for a dedicated RTH switch — "
                     "consider assigning RTH to a spare 2-position switch when available.")

    # 4) NAV LAUNCH — recommended for wings, if a switch remains
    remaining = num_switches - len(switches)
    if remaining >= 1:
        lo, hi = position_ranges(2)[1]
        _switch(2, "NAV LAUNCH (assisted hand-launch)", [
            {"mode": "NAV LAUNCH", "position": "high (on)", "range_low": lo, "range_high": hi},
        ])
        notes.append("NAV LAUNCH automates throttle/elevator after a throw — great for "
                     "flying wings. Configure 'nav_fw_launch_*' settings first.")

    if len(switches) < num_switches:
        notes.append(f"{num_switches - len(switches)} switch(es) left unassigned — "
                     "spare for BEEPER (lost-model finder), SERVO AUTOTRIM, or NAV CRUISE.")

    return {
        "skill_level":   skill,
        "num_switches":  num_switches,
        "has_gps":       has_gps,
        "switches":      switches,
        "notes":         notes,
        "next_step": (
            "Flip each switch and call read_rc_channels() to confirm which RC channel it uses, "
            "then apply with assign_switch(switch_channel=<AUX#>, switch_positions=<N>, "
            "mode_per_position={...}, confirm=True)."
        ),
    }
