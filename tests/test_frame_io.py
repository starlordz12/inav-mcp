"""Byte-level MSP frame I/O tests — the serial layer, offline.

Drives SerialConnection._read_msp_frame / send_msp_v1 / send_msp_v2 through a
scripted FakeSerial: clean frames, frames interrupted by quiet gaps, leading
noise, interleaved wrong-command frames, error frames, corrupt checksums/CRCs,
and absurd claimed v2 lengths. This is the code a green mock-level suite never
touches (AUDIT.md R5).
"""
import struct
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.connection import SerialConnection
from inav_mcp.msp import _xor, _crc8_dvb_s2
from fake_serial import FakeSerial


def _conn(*chunks) -> SerialConnection:
    c = SerialConnection("FAKE")
    c._ser = FakeSerial(chunks)
    c.mode = "MSP"
    c.stale = False
    return c


def _v1_resp(cmd: int, payload: bytes = b"", direction: bytes = b">") -> bytes:
    body = bytes([len(payload), cmd]) + payload
    return b"$M" + direction + body + bytes([_xor(body)])


def _v2_resp(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([0]) + struct.pack("<H", cmd) + struct.pack("<H", len(payload)) + payload
    return b"$X>" + body + bytes([_crc8_dvb_s2(body)])


# ── MSP v1 ─────────────────────────────────────────────────────────────────────

def test_v1_clean_frame():
    conn = _conn(_v1_resp(101, b"\x01\x02\x03"))
    assert conn.send_msp_v1(101, timeout=0.5) == b"\x01\x02\x03"


def test_v1_leading_garbage_skipped():
    conn = _conn(b"\x00\xff\x13garbage$Mx" + _v1_resp(101, b"\xAA"))
    assert conn.send_msp_v1(101, timeout=0.5) == b"\xAA"


def test_v1_interleaved_wrong_cmd_skipped():
    conn = _conn(_v1_resp(105, b"\x11\x22"), _v1_resp(101, b"\x33"))
    assert conn.send_msp_v1(101, timeout=0.5) == b"\x33"


def test_v1_partial_frame_stall_recovers_on_next_frame():
    # Header stalls mid-frame (quiet gap > read timeout), then the FC re-sends.
    conn = _conn(b"$M", None, _v1_resp(101, b"\x42"))
    assert conn.send_msp_v1(101, timeout=0.5) == b"\x42"


def test_v1_truncated_payload_then_complete_frame():
    full = _v1_resp(101, b"\x01\x02\x03\x04")
    conn = _conn(full[:7], None, _v1_resp(101, b"\x05\x06"))
    assert conn.send_msp_v1(101, timeout=0.5) == b"\x05\x06"


def test_v1_bad_checksum_skipped_then_good():
    bad = bytearray(_v1_resp(101, b"\x01"))
    bad[-1] ^= 0xFF
    conn = _conn(bytes(bad), _v1_resp(101, b"\x02"))
    assert conn.send_msp_v1(101, timeout=0.5) == b"\x02"


def test_v1_error_frame_yields_timeout():
    # An $M! error response is skipped by the retry loop (by design), so with
    # nothing else scripted the call surfaces as a timeout, not a wrong payload.
    conn = _conn(_v1_resp(101, b"", direction=b"!"))
    try:
        conn.send_msp_v1(101, timeout=0.3)
        assert False, "should have raised"
    except TimeoutError:
        pass


def test_v1_no_response_times_out():
    conn = _conn()
    try:
        conn.send_msp_v1(101, timeout=0.3)
        assert False, "should have raised"
    except TimeoutError:
        pass


# ── MSP v2 ─────────────────────────────────────────────────────────────────────

def test_v2_clean_frame():
    conn = _conn(_v2_resp(0x2000, b"\x01\x02"))
    assert conn.send_msp_v2(0x2000, timeout=0.5) == b"\x01\x02"


def test_v2_bad_crc_skipped_then_good():
    bad = bytearray(_v2_resp(0x2000, b"\x01"))
    bad[-1] ^= 0xFF
    conn = _conn(bytes(bad), _v2_resp(0x2000, b"\x02"))
    assert conn.send_msp_v2(0x2000, timeout=0.5) == b"\x02"


def test_v2_wrong_cmd_skipped():
    conn = _conn(_v2_resp(0x1F01, b"\x11"), _v2_resp(0x2000, b"\x22"))
    assert conn.send_msp_v2(0x2000, timeout=0.5) == b"\x22"


def test_v2_absurd_length_rejected_then_good_frame_found():
    # A corrupt 16-bit length claiming 65535 must not swallow the read — the
    # frame is discarded as noise and the following good frame still parses.
    absurd = b"$X>" + bytes([0]) + struct.pack("<H", 0x2000) + struct.pack("<H", 0xFFFF)
    conn = _conn(absurd, _v2_resp(0x2000, b"\x7F"))
    assert conn.send_msp_v2(0x2000, timeout=0.5) == b"\x7F"


def test_v2_split_across_packets_coalesced():
    # Bytes arriving in separate USB packets (no quiet gap) are one frame.
    frame = _v2_resp(0x2000, b"\xAB\xCD")
    conn = _conn(frame[:4], frame[4:9], frame[9:])
    assert conn.send_msp_v2(0x2000, timeout=0.5) == b"\xAB\xCD"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
