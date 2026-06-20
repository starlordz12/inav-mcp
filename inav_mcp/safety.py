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
    """Raise if the FC is armed.

    iNAV encodes ARMED as bit 2 (value 4) of the combined armingFlags field —
    the same bit the Configurator checks (ARMED:4). We read it from
    MSPV2_INAV_STATUS (the iNAV 9.x source of truth), falling back to the legacy
    MSP_STATUS layout on older firmware.

    If the read fails (e.g., stuck in CLI mode) we let it pass rather than
    hard-blocking — the armed guard is best-effort for bench use.
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
        if status:
            arming_flags = status.get("arming_disable_flags")
    except Exception:
        pass

    # Fall back to legacy v1 layout.
    if arming_flags is None:
        try:
            arming_flags = parse_status(
                conn.send_msp_v1(MSP_STATUS, timeout=2.0)
            ).get("arming_disable_flags")
        except Exception:
            return   # can't read — don't block

    if arming_flags and (arming_flags & ARMING_FLAG_ARMED):
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
