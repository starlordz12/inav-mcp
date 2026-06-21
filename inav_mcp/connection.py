"""SerialConnection — the single serial handle, shared between MSP and CLI modes.

One instance at a time; the singleton lives in state.py.
"""
from __future__ import annotations
import struct
import time

import serial

from .msp import encode_v1, decode_v1_response, encode_v2, decode_v2_response
from .cli import strip_cli_response

# Max bytes we'll buffer while waiting for the CLI prompt before giving up.
_CLI_MAX_BYTES = 128 * 1024  # 128 KB is plenty for a full `dump all`

# Substrings that mark a USB-serial / CDC device worth probing for an FC after a
# reboot (kept in sync with server._FC_PORT_HINTS — duplicated here to avoid a
# circular import: server imports connection, not the other way round).
_FC_SERIAL_HINTS = ("USB", "STM", "CP210", "CH340", "CDC", "VCP", "ACM", "SERIAL")

# Substrings/USB-PID text that mark an STM32 sitting in DFU (bootloader) mode.
# The iNAV USB VCP is VID:PID 0483:5740; the DFU bootloader is 0483:DF11 and
# usually shows up as "STM32 BOOTLOADER" / "DFU in FS Mode". A board in DFU has
# dropped off USB-serial entirely — MSP can't reach it and it needs a power-cycle.
_DFU_HINTS = ("BOOTLOADER", "DFU", "DF11")


def _enumerate_ports() -> list[dict]:
    """Enumerate serial ports as plain dicts. Isolated for testability."""
    from serial.tools.list_ports import comports
    return [
        {"device": p.device, "description": p.description or "", "hwid": p.hwid or ""}
        for p in comports()
    ]


def port_in_dfu(ports: list[dict]) -> dict | None:
    """Return the first port that looks like an STM32 in DFU/bootloader mode, else None.

    `ports` is a list of {"device", "description", "hwid"} dicts (as from
    _enumerate_ports). A match means the FC re-enumerated as a bootloader device
    instead of coming back as its USB-serial port — the user must power-cycle it.
    """
    for p in ports:
        blob = f"{p.get('description', '')} {p.get('hwid', '')}".upper()
        if any(h in blob for h in _DFU_HINTS):
            return p
    return None


def fc_serial_candidates(ports: list[dict], original_port: str) -> list[str]:
    """Ordered list of ports to retry after a reboot: original first, then any other
    USB-serial-looking device the FC may have re-enumerated as.

    DFU/bootloader devices are excluded — they don't speak MSP. Pure function
    (ports injected) so the re-enumeration logic is unit-testable offline.
    """
    candidates = [original_port]
    for p in ports:
        dev = p.get("device")
        if not dev or dev == original_port:
            continue
        blob = f"{p.get('description', '')} {p.get('hwid', '')}".upper()
        if any(h in blob for h in _DFU_HINTS):
            continue   # bootloader device — can't MSP it
        if any(h in blob for h in _FC_SERIAL_HINTS):
            candidates.append(dev)
    return candidates


class SerialConnection:
    def __init__(self, port: str, baud: int = 115200) -> None:
        self.port  = port
        self.baud  = baud
        self._ser: serial.Serial | None = None
        self.mode: str   = "IDLE"   # IDLE | MSP | CLI
        self.stale: bool = False     # True after save/reboot — must reconnect

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            timeout=1.0,           # per-read timeout; frame assembly adds its own deadline
            write_timeout=2.0,
        )
        self.mode  = "MSP"
        self.stale = False
        time.sleep(0.15)           # let the VCP enumerate
        self._ser.reset_input_buffer()

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None
        self.mode  = "IDLE"

    def _try_msp_handshake(self, device: str) -> bool:
        """Open `device` and probe MSP_API_VERSION once. On success, leave the handle
        open and MSP-ready and return True; otherwise close it and return False.
        """
        from .msp import MSP_API_VERSION
        try:
            self._ser = serial.Serial(
                port=device, baudrate=self.baud,
                timeout=0.5, write_timeout=1.0,
            )
            self.mode  = "MSP"
            self.stale = False
            time.sleep(0.1)
            self._ser.reset_input_buffer()
            self._ser.write(encode_v1(MSP_API_VERSION))
            time.sleep(0.2)
            frame = self._read_msp_frame(timeout=0.8)
            decode_v1_response(frame)
            return True
        except Exception:
            if self._ser and self._ser.is_open:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None
            return False

    def reconnect(self, settle_timeout: float = 15.0, initial_settle: float = 1.0) -> float:
        """Close and reopen the port after an FC reboot, polling until MSP responds.

        iNAV's CLI `exit`/`save` both reboot the FC, which drops the USB VCP and
        re-enumerates it (~7s to answer MSP again). Call this after any CLI session
        to restore a usable MSP connection. Returns the elapsed reconnect time in
        seconds (so callers can surface/measure the per-reboot cost).

        Hardening over a naive "reopen the same port" loop:
          1. A short `initial_settle` before the first probe — hammering the port
             while the MCU is still resetting wastes attempts, and rapid reconnect
             churn is itself a suspected trigger for the board dropping into DFU.
          2. Exponential-ish backoff between attempts instead of a flat 0.5s.
          3. Re-enumeration awareness: if the original port doesn't return, scan
             OTHER USB-serial ports (the FC may have come back as a different COM
             device) and adopt the one that answers MSP — updating self.port.
          4. DFU detection: if it gives up, distinguish "board is in the STM32
             bootloader, power-cycle it" from a merely-slow board.
        """
        original_port = self.port
        self.close()
        t0 = time.monotonic()
        time.sleep(initial_settle)

        deadline = t0 + settle_timeout
        # Only fan out to other ports once the original is clearly not coming back,
        # to keep the common case (same port returns) fast.
        scan_others_after = time.monotonic() + min(5.0, settle_timeout / 2)
        backoff = 0.5
        while time.monotonic() < deadline:
            if self._try_msp_handshake(original_port):
                self.port = original_port
                return time.monotonic() - t0

            if time.monotonic() >= scan_others_after:
                for cand in fc_serial_candidates(_enumerate_ports(), original_port):
                    if cand == original_port:
                        continue
                    if self._try_msp_handshake(cand):
                        self.port = cand
                        return time.monotonic() - t0

            time.sleep(backoff)
            backoff = min(backoff * 1.5, 2.0)

        # Gave up — give the caller an actionable reason.
        self.mode = "IDLE"
        dfu = port_in_dfu(_enumerate_ports())
        if dfu:
            raise ConnectionError(
                f"FC did not return on USB-serial and a device is in STM32 "
                f"DFU/bootloader mode ({dfu['device']}: {dfu['description'] or 'STM32 BOOTLOADER'}). "
                "The board dropped into its bootloader — POWER-CYCLE it (unplug/replug "
                "USB), then reconnect. Rapid back-to-back CLI reboots can trigger this; "
                "batch commands with cli_batch() to cut reboot churn."
            )
        raise TimeoutError(
            f"FC did not respond to MSP within {settle_timeout:.0f}s after reboot "
            f"(original port {original_port}). It may need more time, re-enumerated as "
            "a different COM port, or dropped into DFU/bootloader mode — check for an "
            "'STM32 BOOTLOADER' device and power-cycle the board if so."
        )

    def is_open(self) -> bool:
        return (
            self._ser is not None
            and self._ser.is_open
            and not self.stale
        )

    def mark_stale(self) -> None:
        """Call after save/reboot. Connection is invalid until reconnect."""
        self.stale = True

    # ── MSP frame I/O ─────────────────────────────────────────────────────────

    def _read_msp_frame(self, timeout: float = 2.0) -> bytes:
        """Scan the byte stream for one complete MSP v1 or v2 response frame."""
        if self._ser is None:
            raise ConnectionError("Serial port not open")
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            b = self._ser.read(1)
            if not b or b != b"$":
                continue

            proto = self._ser.read(1)
            if not proto:
                continue

            if proto == b"M":
                direction = self._ser.read(1)
                if direction not in (b">", b"!"):
                    continue
                size_b = self._ser.read(1)
                cmd_b  = self._ser.read(1)
                if len(size_b) < 1 or len(cmd_b) < 1:
                    continue
                size = size_b[0]
                rest = self._ser.read(size + 1)   # payload + checksum
                if len(rest) < size + 1:
                    continue
                return b"$M" + direction + size_b + cmd_b + rest

            elif proto == b"X":
                direction = self._ser.read(1)
                if direction not in (b">", b"!"):
                    continue
                header = self._ser.read(5)         # flag(1) + function(2le) + length(2le)
                if len(header) < 5:
                    continue
                length = struct.unpack_from("<H", header, 3)[0]
                rest = self._ser.read(length + 1)  # payload + CRC
                if len(rest) < length + 1:
                    continue
                return b"$X" + direction + header + rest

        raise TimeoutError(f"No MSP frame received within {timeout:.1f}s")

    def send_msp_v1(self, cmd: int, payload: bytes = b"", timeout: float = 2.0) -> bytes:
        """Send an MSP v1 request and return the response payload.

        Retries until timeout, accepting only the frame that matches our cmd.
        """
        if not self.is_open():
            raise ConnectionError(
                "Not connected to FC. Call connect(port) first, "
                "or reconnect after a save/reboot."
            )
        if self.mode == "CLI":
            raise ConnectionError("Currently in CLI mode. Exit CLI before sending MSP.")

        req = encode_v1(cmd, payload)
        self._ser.reset_input_buffer()
        self._ser.write(req)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = self._read_msp_frame(timeout=min(1.0, remaining))
            except TimeoutError:
                break
            try:
                resp_cmd, resp_payload = decode_v1_response(frame)
            except (ValueError, RuntimeError):
                continue
            if resp_cmd == cmd:
                return resp_payload

        raise TimeoutError(
            f"No valid MSP v1 response to cmd {cmd} within {timeout:.1f}s"
        )

    def send_msp_v2(self, cmd: int, payload: bytes = b"", timeout: float = 2.0) -> bytes:
        """Send an MSP v2 request and return the response payload.

        Needed for iNAV-specific commands (cmd >= 0x1000) like MSPV2_INAV_STATUS,
        which the FC answers with a $X> frame. Retries until timeout, accepting only
        the frame matching our cmd.
        """
        if not self.is_open():
            raise ConnectionError(
                "Not connected to FC. Call connect(port) first, "
                "or reconnect after a save/reboot."
            )
        if self.mode == "CLI":
            raise ConnectionError("Currently in CLI mode. Exit CLI before sending MSP.")

        req = encode_v2(cmd, payload)
        self._ser.reset_input_buffer()
        self._ser.write(req)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = self._read_msp_frame(timeout=min(1.0, remaining))
            except TimeoutError:
                break
            try:
                resp_cmd, resp_payload = decode_v2_response(frame)
            except (ValueError, RuntimeError):
                continue
            if resp_cmd == cmd:
                return resp_payload

        raise TimeoutError(
            f"No valid MSP v2 response to cmd {cmd} within {timeout:.1f}s"
        )

    # ── CLI mode ──────────────────────────────────────────────────────────────

    def _read_until_prompt(self, timeout: float = 5.0) -> str:
        """Read bytes from the serial port until the CLI prompt '# ' appears.

        Returns the full raw string including the prompt.
        Raises TimeoutError if the prompt doesn't arrive within `timeout` seconds.
        """
        if self._ser is None:
            raise ConnectionError("Serial port not open")
        deadline = time.monotonic() + timeout
        buf = bytearray()

        while time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _CLI_MAX_BYTES:
                raise RuntimeError(
                    f"CLI response exceeded {_CLI_MAX_BYTES // 1024} KB — "
                    "possible runaway output or wrong baud rate"
                )
            if buf.endswith(b"# "):
                return buf.decode("utf-8", errors="replace")

        tail = bytes(buf[-300:])
        raise TimeoutError(
            f"CLI prompt '# ' not received within {timeout:.1f}s. "
            f"Last bytes: {tail!r}"
        )

    def _drain(self, timeout: float = 1.0) -> None:
        """Read and discard bytes until the serial stream goes quiet."""
        if self._ser is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if not chunk:
                break

    def enter_cli(self, timeout: float = 5.0) -> str:
        """Switch to CLI mode.

        Sends '#\\r' to trigger the FC CLI banner, waits for the '# ' prompt.
        Returns the banner text (informational).

        The spec notes this handshake may be firmware-picky; empirically verify
        line endings on first run (§4, §12).
        """
        if not self.is_open():
            raise ConnectionError("Not connected. Call connect() first.")
        if self.mode == "CLI":
            return ""   # already there

        self._ser.reset_input_buffer()
        self._ser.write(b"#\r")
        banner = self._read_until_prompt(timeout=timeout)
        self.mode = "CLI"
        return banner

    def run_cli(self, cmd: str, timeout: float = 15.0) -> str:
        """Send one CLI command and return its cleaned output.

        Strips the echoed command header and the trailing '# ' prompt.
        `timeout` should be generous for slow commands like 'dump all'.
        """
        if self.mode != "CLI":
            raise ConnectionError("Not in CLI mode. Call enter_cli() first.")
        self._ser.write((cmd + "\r").encode("ascii"))
        raw = self._read_until_prompt(timeout=timeout)
        return strip_cli_response(cmd, raw)

    def exit_cli(self, save: bool = False, reconnect: bool = False) -> float | None:
        """Leave CLI mode. On iNAV, BOTH paths reboot the FC.

        save=False → 'exit'  : discards unsaved changes, then reboots.
        save=True  → 'save'  : persists to EEPROM, then reboots.

        Because the FC reboots and the USB VCP re-enumerates, the current handle
        becomes invalid. The connection is marked stale. If reconnect=True, we poll
        the port back to a usable MSP state (~7s) and return the elapsed reconnect
        seconds (the measured reboot cost); otherwise the caller must reconnect()
        before further use and None is returned.

        NOTE: 'exit' DISCARDS any `set`/`aux`/etc. changes made in this session —
        only `save` makes CLI writes stick.
        """
        if self.mode != "CLI":
            return None

        cmd = b"save\r" if save else b"exit\r"
        try:
            self._ser.write(cmd)
        except Exception:
            pass   # FC may drop the link mid-write as it reboots
        self.mode = "MSP"
        self.mark_stale()
        time.sleep(0.3)   # let the reboot begin

        if reconnect:
            return self.reconnect()
        return None
