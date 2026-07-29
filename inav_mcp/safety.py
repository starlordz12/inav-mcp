"""Safety gates — enforced as code, not just docstrings (§10).

Every function either returns cleanly or raises RuntimeError with a user-facing message.
"""
from __future__ import annotations
import os
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connection import SerialConnection

# Absolute path to the backups folder (project root / backups)
BACKUPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")


def require_connected(conn: "SerialConnection | None") -> "SerialConnection":
    """Raise if conn is None or not open."""
    if conn is None or not conn.is_open():
        raise RuntimeError(
            "Not connected to FC. Call connect(port) first. "
            "Use list_serial_ports() to find the right port."
        )
    return conn


def check_not_armed(conn: "SerialConnection") -> None:
    """Raise unless the FC is positively confirmed DISARMED.

    iNAV encodes ARMED as bit 2 (value 4) of the combined armingFlags field —
    the same bit the Configurator checks (ARMED:4). We read it from
    MSPV2_INAV_STATUS (the iNAV 9.x source of truth), falling back to the legacy
    MSP_STATUS layout on older firmware.

    FAIL CLOSED. There are three outcomes, not two:
      - a status read reports DISARMED          → return (caller proceeds),
      - a status read reports ARMED             → raise,
      - no read establishes either              → raise.

    The third case previously returned cleanly, so a caller drove motors and
    wrote config without ever knowing the armed state. A missing, truncated, or
    unanswered status read is not evidence of a disarmed board.

    Note both parsers OMIT 'arming_disable_flags' when the payload is too short
    to carry it, so presence of the key — not its truthiness — is what marks a
    successful determination: 0 is a perfectly good "disarmed" answer.

    Every caller reads status over MSP *before* entering CLI, so this never has
    to contend with a CLI-mode connection.
    """
    from .msp import (
        MSPV2_INAV_STATUS, MSP_STATUS,
        parse_inav_status, parse_status,
        ARMING_FLAG_ARMED,
    )

    arming_flags = None

    # Prefer MSP v2 (authoritative on current firmware).
    try:
        status = parse_inav_status(conn.send_msp_v2(MSPV2_INAV_STATUS, timeout=2.0))
        if "arming_disable_flags" in status:
            arming_flags = status["arming_disable_flags"]
    except Exception:
        pass

    # Fall back to the legacy v1 layout (older firmware, or no v2 support).
    if arming_flags is None:
        try:
            status = parse_status(conn.send_msp_v1(MSP_STATUS, timeout=2.0))
            if "arming_disable_flags" in status:
                arming_flags = status["arming_disable_flags"]
        except Exception:
            pass

    if arming_flags is None:
        raise RuntimeError(
            "Unable to verify that the FC is disarmed; refusing a safety-sensitive "
            "operation. Neither MSPV2_INAV_STATUS nor legacy MSP_STATUS returned a "
            "readable armingFlags field. Reconnect (the link may be down, or the FC "
            "left in CLI mode by an interrupted session) and retry."
        )

    if arming_flags & ARMING_FLAG_ARMED:
        raise RuntimeError(
            "FC is ARMED (ARMED bit set in armingFlags). "
            "DISARM before making configuration changes."
        )


def next_backup_path(label: str | None = None) -> str:
    """Return a timestamped backup file path (does not create the file)."""
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"backup_{ts}"
    if label:
        safe = "".join(c for c in label if c.isalnum() or c in "-_ ")[:40].strip().replace(" ", "_")
        if safe:
            name = f"backup_{ts}_{safe}"
    return os.path.join(BACKUPS_DIR, f"{name}.txt")
