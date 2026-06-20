"""Minimal MSP v1/v2 codec. Only implements commands the tools actually use.

MSP v1 frame (request):  $M< <size:u8> <cmd:u8> [payload] <xor_checksum:u8>
MSP v1 frame (response): $M> <size:u8> <cmd:u8> [payload] <xor_checksum:u8>
MSP v1 frame (error):    $M! <size:u8> <cmd:u8> [payload] <xor_checksum:u8>

MSP v2 frame: $X <dir:u8> <flag:u8> <fn:u16le> <len:u16le> [payload] <crc8_dvb_s2:u8>
"""
from __future__ import annotations
import struct

# ── Command IDs ───────────────────────────────────────────────────────────────

MSP_API_VERSION   = 1
MSP_FC_VARIANT    = 2
MSP_FC_VERSION    = 3
MSP_BOARD_INFO    = 4
MSP_BUILD_INFO    = 5
MSP_MODE_RANGES   = 34
MSP_STATUS        = 101
MSP_RC            = 105
MSP_RAW_GPS       = 106
MSP_ATTITUDE      = 108
MSP_ANALOG        = 110
MSP_BOXNAMES      = 116
MSP_BOXIDS        = 119
MSP_STATUS_EX     = 150
MSP_SENSOR_STATUS = 151

# MSP v2 (iNAV-specific). The iNAV Configurator reads arming flags exclusively
# from this command — the legacy MSP_STATUS (101) is not used by 9.x tooling.
MSPV2_INAV_STATUS = 0x2000   # 8192

# Sensor presence bits in MSP_STATUS.sensorStatus (uint16, offset 4)
# Source: iNAV src/main/msp/msp.c — the field packs active sensors in this order.
SENSOR_BITS: dict[str, int] = {
    "acc":         1 << 0,
    "baro":        1 << 1,
    "mag":         1 << 2,
    "gps":         1 << 3,
    "rangefinder": 1 << 4,
    "opflow":      1 << 5,
    "pitot":       1 << 6,
}

# ── MSP v1 ────────────────────────────────────────────────────────────────────

def _xor(data: bytes | bytearray) -> int:
    cs = 0
    for b in data:
        cs ^= b
    return cs


def encode_v1(cmd: int, payload: bytes = b"") -> bytes:
    """Build a $M< request frame."""
    size = len(payload)
    body = bytes([size, cmd]) + payload
    return b"$M<" + body + bytes([_xor(body)])


def decode_v1_response(frame: bytes) -> tuple[int, bytes]:
    """Parse a complete $M> or $M! frame. Returns (cmd, payload). Raises on bad frame."""
    if len(frame) < 6:
        raise ValueError(f"Frame too short ({len(frame)} bytes)")
    if frame[:2] != b"$M":
        raise ValueError(f"Bad preamble: {frame[:2]!r}")
    direction = frame[2:3]
    if direction == b"!":
        raise RuntimeError(f"MSP error response (cmd={frame[4]})")
    if direction != b">":
        raise ValueError(f"Unexpected direction byte: {direction!r}")
    size     = frame[3]
    cmd      = frame[4]
    payload  = frame[5 : 5 + size]
    cs_recv  = frame[5 + size]
    cs_calc  = _xor(frame[3 : 5 + size])
    if cs_recv != cs_calc:
        raise ValueError(f"Checksum mismatch: got {cs_recv:#04x}, expected {cs_calc:#04x}")
    return cmd, payload


# ── MSP v2 ────────────────────────────────────────────────────────────────────

def _crc8_dvb_s2(data: bytes | bytearray) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc


def encode_v2(cmd: int, payload: bytes = b"") -> bytes:
    """Build a $X< request frame."""
    flag = 0
    body = bytes([flag]) + struct.pack("<H", cmd) + struct.pack("<H", len(payload)) + payload
    return b"$X<" + body + bytes([_crc8_dvb_s2(body)])


def decode_v2_response(frame: bytes) -> tuple[int, bytes]:
    """Parse a complete $X> or $X! frame. Returns (cmd, payload). Raises on bad frame."""
    if len(frame) < 9:
        raise ValueError(f"v2 frame too short ({len(frame)} bytes)")
    if frame[:2] != b"$X":
        raise ValueError(f"Bad v2 preamble: {frame[:2]!r}")
    direction = frame[2:3]
    if direction == b"!":
        cmd = struct.unpack_from("<H", frame, 4)[0]
        raise RuntimeError(f"MSP v2 error response (cmd={cmd})")
    if direction != b">":
        raise ValueError(f"Unexpected v2 direction: {direction!r}")
    cmd     = struct.unpack_from("<H", frame, 4)[0]
    length  = struct.unpack_from("<H", frame, 6)[0]
    payload = frame[8 : 8 + length]
    cs_recv = frame[8 + length]
    cs_calc = _crc8_dvb_s2(frame[3 : 8 + length])
    if cs_recv != cs_calc:
        raise ValueError(f"CRC mismatch: got {cs_recv:#04x}, expected {cs_calc:#04x}")
    return cmd, payload


# ── Payload parsers ───────────────────────────────────────────────────────────

def parse_api_version(payload: bytes) -> dict:
    """MSP_API_VERSION (1): msp_protocol_version, api_version."""
    if len(payload) < 3:
        return {}
    return {
        "msp_protocol_version": payload[0],
        "api_version": f"{payload[1]}.{payload[2]}",
    }


def parse_fc_variant(payload: bytes) -> dict:
    """MSP_FC_VARIANT (2): e.g. 'INAV'."""
    if len(payload) < 4:
        return {}
    return {"variant": payload[:4].decode("ascii", errors="replace").rstrip("\x00")}


def parse_fc_version(payload: bytes) -> dict:
    """MSP_FC_VERSION (3): firmware version string."""
    if len(payload) < 3:
        return {}
    return {"fw_version": f"{payload[0]}.{payload[1]}.{payload[2]}"}


def parse_board_info(payload: bytes) -> dict:
    """MSP_BOARD_INFO (4): board identifier, hardware revision, target name."""
    if len(payload) < 4:
        return {}
    result: dict = {
        "board_identifier": payload[:4].decode("ascii", errors="replace").rstrip("\x00"),
    }
    offset = 4
    if offset + 2 <= len(payload):
        result["hardware_revision"] = struct.unpack_from("<H", payload, offset)[0]
        offset += 2
    if offset < len(payload):
        result["board_type"] = payload[offset]
        offset += 1
    if offset < len(payload):
        offset += 1  # targetCapabilities (not used yet)
    # target name (length-prefixed)
    if offset < len(payload):
        name_len = payload[offset]
        offset += 1
        if name_len and offset + name_len <= len(payload):
            result["target_name"] = payload[offset : offset + name_len].decode(
                "ascii", errors="replace"
            )
    return result


def parse_build_info(payload: bytes) -> dict:
    """MSP_BUILD_INFO (5): build date, time, git revision."""
    result: dict = {}
    if len(payload) >= 11:
        result["build_date"] = payload[:11].decode("ascii", errors="replace").strip("\x00 ")
    if len(payload) >= 19:
        result["build_time"] = payload[11:19].decode("ascii", errors="replace").strip("\x00 ")
    if len(payload) >= 26:
        result["git_revision"] = payload[19:26].decode("ascii", errors="replace").strip("\x00 ")
    return result


def parse_status(payload: bytes) -> dict:
    """MSP_STATUS (101): cycle time, sensors present, flight mode flags, arming flags.

    iNAV layout (offsets):
      0-1  cycleTime           uint16
      2-3  i2cErrors           uint16
      4-5  sensorStatus        uint16  ← sensor presence bits
      6-9  flightModeFlags     uint32
      10   profile             uint8
      11-12 averageSystemLoad  uint16  (iNAV extension)
      13   armingDisableCount  uint8   (iNAV extension)
      14-17 armingDisableFlags uint32  (iNAV extension)
    """
    if len(payload) < 11:
        return {}
    cycle_time        = struct.unpack_from("<H", payload, 0)[0]
    i2c_errors        = struct.unpack_from("<H", payload, 2)[0]
    sensors           = struct.unpack_from("<H", payload, 4)[0]
    flight_mode_flags = struct.unpack_from("<I", payload, 6)[0]
    profile           = payload[10]

    result: dict = {
        "cycle_time_us":    cycle_time,
        "i2c_error_count":  i2c_errors,
        "flight_mode_flags": flight_mode_flags,
        "profile":          profile,
        "sensors_present":  {name: bool(sensors & bit) for name, bit in SENSOR_BITS.items()},
    }

    if len(payload) >= 13:
        result["cpu_load_pct"] = struct.unpack_from("<H", payload, 11)[0]
    if len(payload) >= 18:
        result["arming_disable_flags"] = struct.unpack_from("<I", payload, 14)[0]

    return result


# State bits in the combined armingFlags field (not arming-prevention reasons).
ARMING_FLAG_ARMED          = 1 << 2   # value 4
ARMING_FLAG_SIMULATOR_MODE = 1 << 4   # value 16
# Arming-prevention reasons live in bits 6..30 (see knowledge/arming_flags.json).
ARMING_DISABLE_REASON_MASK = 0x7FFFFFC0   # bits 6..30


def parse_inav_status(payload: bytes) -> dict:
    """MSPV2_INAV_STATUS (0x2000): the authoritative status/arming source for iNAV 9.x.

    Layout (from iNAV Configurator 9.0.2 — the only status command it parses):
      0-1   cycleTime       uint16
      2-3   i2cErrors       uint16
      4-5   activeSensors   uint16   ← sensor presence bits (SENSOR_BITS)
      6-7   cpuLoad         uint16
      8     packed profile  uint8    (profile = low nibble, battery = high nibble)
      9-12  armingFlags     uint32   ← ARMED (bit2) + disable reasons (bits 6..30)
      13    mixer profile   uint8    (low nibble)

    Note: unlike legacy MSP_STATUS, this frame carries NO flightModeFlags — active
    modes come from MSP_MODE_RANGES / MSP_BOXIDS instead.
    """
    if len(payload) < 13:
        return {}

    cycle_time = struct.unpack_from("<H", payload, 0)[0]
    i2c_errors = struct.unpack_from("<H", payload, 2)[0]
    sensors    = struct.unpack_from("<H", payload, 4)[0]
    cpu_load   = struct.unpack_from("<H", payload, 6)[0]
    packed     = payload[8]
    arming     = struct.unpack_from("<I", payload, 9)[0]

    result: dict = {
        "cycle_time_us":        cycle_time,
        "i2c_error_count":      i2c_errors,
        "cpu_load_pct":         cpu_load,
        "profile":              packed & 0x0F,
        "battery_profile":      (packed & 0xF0) >> 4,
        "sensors_present":      {name: bool(sensors & bit) for name, bit in SENSOR_BITS.items()},
        "arming_disable_flags": arming,
        "armed":                bool(arming & ARMING_FLAG_ARMED),
        "simulator_mode":       bool(arming & ARMING_FLAG_SIMULATOR_MODE),
    }
    if len(payload) >= 14:
        result["mixer_profile"] = payload[13] & 0x0F
    return result


# ── M2 parsers ────────────────────────────────────────────────────────────────

def parse_rc_channels(payload: bytes) -> list[int]:
    """MSP_RC (105): list of channel values in µs (up to 16 channels, each uint16)."""
    n = len(payload) // 2
    return [struct.unpack_from("<H", payload, i * 2)[0] for i in range(n)]


def parse_attitude(payload: bytes) -> dict:
    """MSP_ATTITUDE (108): roll/pitch (deci-degrees signed), yaw (degrees unsigned)."""
    if len(payload) < 6:
        return {}
    roll  = struct.unpack_from("<h", payload, 0)[0]
    pitch = struct.unpack_from("<h", payload, 2)[0]
    yaw   = struct.unpack_from("<H", payload, 4)[0]
    return {
        "roll_deg":  roll  / 10.0,
        "pitch_deg": pitch / 10.0,
        "yaw_deg":   float(yaw),
    }


def parse_analog(payload: bytes) -> dict:
    """MSP_ANALOG (110): vbat, mAh drawn, RSSI, current draw.

    Layout: vbat(u8 dV), mah(u16), rssi(u16 0-1023), amperage(i16 cA)
    Newer iNAV appends vbat_hires(u16 cV) for 0.01V resolution.
    """
    if len(payload) < 7:
        return {}
    vbat_dv  = payload[0]
    mah      = struct.unpack_from("<H", payload, 1)[0]
    rssi     = struct.unpack_from("<H", payload, 3)[0]
    amperage = struct.unpack_from("<h", payload, 5)[0]

    result: dict = {
        "vbat_v":       vbat_dv / 10.0,
        "mah_drawn":    mah,
        "rssi":         rssi,
        "rssi_pct":     round(rssi / 1023.0 * 100.0, 1),
        "current_a":    amperage / 100.0,
    }
    # Higher-precision vbat (0.01V) appended in newer iNAV builds
    if len(payload) >= 9:
        vbat_hires = struct.unpack_from("<H", payload, 7)[0]
        if vbat_hires > 0:
            result["vbat_v"] = vbat_hires / 100.0
    return result


def parse_boxnames(payload: bytes) -> list[str]:
    """MSP_BOXNAMES (116): semicolon-separated mode name string."""
    text = payload.decode("ascii", errors="replace").rstrip(";").rstrip("\x00")
    return [n for n in text.split(";") if n]


def parse_boxids(payload: bytes) -> list[int]:
    """MSP_BOXIDS (119): one uint8 permanent-ID per mode (same order as BOXNAMES)."""
    return list(payload)


_MAX_MODE_SLOTS = 40


def parse_mode_ranges(payload: bytes) -> list[dict]:
    """MSP_MODE_RANGES (34): up to 40 mode-activation condition slots.

    Each slot is 4 bytes (older FW) or 6 bytes (iNAV 7+):
      modeId(u8), auxChannelIndex(u8), startStep(u8), endStep(u8)
      [modeLogic(u8), linkedTo(u8)]   ← iNAV 7+ only

    Range µs = 900 + step * 25  (step 0 = 900ms, step 48 = 2100ms)
    Slot is disabled when startStep >= endStep.
    """
    if not payload:
        return []

    # Detect per-entry size: prefer 6, fall back to 4
    for size in (6, 4):
        if len(payload) % size == 0 and 10 <= len(payload) // size <= 50:
            entry_size = size
            break
    else:
        entry_size = 4   # best-guess fallback

    num_slots = len(payload) // entry_size
    ranges: list[dict] = []
    for i in range(num_slots):
        off = i * entry_size
        if off + 4 > len(payload):
            break
        box_id     = payload[off]
        aux_ch     = payload[off + 1]     # 0-based (0=AUX1)
        start_step = payload[off + 2]
        end_step   = payload[off + 3]
        mode_logic = payload[off + 4] if entry_size >= 5 and off + 5 <= len(payload) else 0
        linked_to  = payload[off + 5] if entry_size >= 6 and off + 6 <= len(payload) else 0

        ranges.append({
            "slot":            i,
            "box_id":          box_id,
            "aux_channel":     aux_ch,
            "aux_channel_1":   aux_ch + 1,          # 1-based for display
            "range_start_ms":  900 + start_step * 25,
            "range_end_ms":    900 + end_step   * 25,
            "mode_logic":      mode_logic,           # 0=OR, 1=AND
            "linked_to":       linked_to,
            "enabled":         start_step < end_step,
        })
    return ranges


_HW_STATUS_NAMES = {0: "NONE", 1: "OK", 2: "UNAVAILABLE", 3: "UNHEALTHY"}
_SENSOR_STATUS_ORDER = ["overall", "gyro", "acc", "compass", "baro",
                        "gps", "rangefinder", "pitot", "opflow"]


def parse_sensor_status(payload: bytes) -> dict:
    """MSP_SENSOR_STATUS (151): per-sensor hardware health.

    Byte order: overall, gyro, acc, compass, baro, gps, rangefinder, pitot, opflow
    Values: 0=NONE, 1=OK, 2=UNAVAILABLE, 3=UNHEALTHY
    """
    return {
        name: _HW_STATUS_NAMES.get(payload[i], f"UNKNOWN({payload[i]})")
        for i, name in enumerate(_SENSOR_STATUS_ORDER)
        if i < len(payload)
    }


def parse_raw_gps(payload: bytes) -> dict:
    """MSP_RAW_GPS (106): GPS fix data.

    Layout (iNAV 7.x):
      fixType(u8), numSat(u8), lat(i32 *1e7), lon(i32 *1e7),
      alt(u16 m), speed(u16 cm/s), groundCourse(u16 deci-deg), hdop(u16)
    Total: 18 bytes.
    """
    if len(payload) < 16:
        return {}
    fix_type = payload[0]   # 0=no fix, 1=2D, 2=3D  (or 0/1 flag in some FW)
    num_sats = payload[1]
    lat      = struct.unpack_from("<i", payload,  2)[0]  # signed, degrees * 1e7
    lon      = struct.unpack_from("<i", payload,  6)[0]
    alt      = struct.unpack_from("<H", payload, 10)[0]  # metres
    speed    = struct.unpack_from("<H", payload, 12)[0]  # cm/s
    course   = struct.unpack_from("<H", payload, 14)[0]  # deci-degrees

    result: dict = {
        "fix_type":          fix_type,
        "fix_type_name":     {0: "NO_FIX", 1: "FIX_2D", 2: "FIX_3D"}.get(fix_type, f"FIX_{fix_type}"),
        "num_sats":          num_sats,
        "latitude_deg":      lat / 1e7,
        "longitude_deg":     lon / 1e7,
        "altitude_m":        float(alt),
        "speed_ms":          speed / 100.0,
        "ground_course_deg": course / 10.0,
    }
    if len(payload) >= 18:
        hdop = struct.unpack_from("<H", payload, 16)[0]
        result["hdop"] = hdop / 100.0
    return result
