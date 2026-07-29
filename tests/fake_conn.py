"""Shared connection-mock helpers for offline tests.

`check_not_armed()` fails closed: it refuses unless a status read positively
reports DISARMED. A mock that simply omits the MSP status methods therefore
makes every safety-gated tool refuse, which is almost never what a test about
some *other* behaviour wants.

So any mock standing in for a connected, disarmed FC must say so explicitly —
inherit `DisarmedStatus`, or answer MSPV2_INAV_STATUS with
`v2_disarmed_status()`. Tests that deliberately exercise an unverifiable link
(see test_safety_armed_guard.py) build their own mocks instead.
"""
from __future__ import annotations
import struct

from inav_mcp.msp import MSPV2_INAV_STATUS, SENSOR_BITS


def v2_disarmed_status() -> bytes:
    """A well-formed MSPV2_INAV_STATUS payload with armingFlags = 0 (disarmed)."""
    return (
        struct.pack("<H", 200)                    # cycleTime
        + struct.pack("<H", 0)                    # i2cErrors
        + struct.pack("<H", SENSOR_BITS["acc"])   # activeSensors
        + struct.pack("<H", 10)                   # cpuLoad
        + bytes([0])                              # packed profile
        + struct.pack("<I", 0)                    # armingFlags → ARMED bit clear
        + bytes([0])                              # mixer profile
    )


class DisarmedStatus:
    """Mixin: answers the armed guard truthfully with 'disarmed'."""

    def send_msp_v2(self, cmd, payload=b"", timeout=2.0):
        if cmd == MSPV2_INAV_STATUS:
            return v2_disarmed_status()
        raise AssertionError(f"unexpected v2 command {cmd}")
