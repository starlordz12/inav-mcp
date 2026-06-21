"""Module-level singletons: serial connection + aircraft profile.

Import get_connection/set_connection/require_connection everywhere;
never import SerialConnection directly to avoid circular deps at runtime.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .connection import SerialConnection
    from .profiles import AircraftProfile

_connection: "Optional[SerialConnection]" = None
_profile: "Optional[AircraftProfile]" = None
_board_info: "Optional[dict]" = None


def get_connection() -> "Optional[SerialConnection]":
    return _connection


def set_connection(conn: "Optional[SerialConnection]") -> None:
    global _connection
    _connection = conn


def require_connection() -> "SerialConnection":
    """Return the active connection or raise with a clear message."""
    conn = get_connection()
    if conn is None or not conn.is_open():
        raise RuntimeError(
            "Not connected to FC. Call the connect(port) tool first. "
            "Use list_serial_ports() to find the right port."
        )
    return conn


# ── Aircraft profile singleton ────────────────────────────────────────────────

def get_profile() -> "Optional[AircraftProfile]":
    return _profile


def set_profile(p: "Optional[AircraftProfile]") -> None:
    global _profile
    _profile = p


def require_profile() -> "AircraftProfile":
    """Return the declared profile or raise."""
    p = get_profile()
    if p is None:
        raise RuntimeError(
            "No aircraft profile defined. Call define_aircraft() first."
        )
    return p


# ── Board info singleton ──────────────────────────────────────────────────────
# Cached identity (incl. fw_version, sensors_present) from the last connect/
# board_info, so tools can check firmware-version compatibility without an extra
# MSP round-trip.

def get_board_info() -> "Optional[dict]":
    return _board_info


def set_board_info(info: "Optional[dict]") -> None:
    global _board_info
    _board_info = info
