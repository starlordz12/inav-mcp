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

    def reconnect(self, settle_timeout: float = 15.0) -> bool:
        """Close and reopen the port after an FC reboot, polling until MSP responds.

        iNAV's CLI `exit`/`save` both reboot the FC, which re-enumerates the USB
        VCP (~7s to answer MSP again). Call this after any CLI session to restore a
        usable MSP connection. Returns True once MSP_API_VERSION answers.
        """
        from .msp import MSP_API_VERSION
        self.close()
        deadline = time.monotonic() + settle_timeout
        while time.monotonic() < deadline:
            try:
                self._ser = serial.Serial(
                    port=self.port, baudrate=self.baud,
                    timeout=0.5, write_timeout=1.0,
                )
                self.mode  = "MSP"
                self.stale = False
                time.sleep(0.1)
                self._ser.reset_input_buffer()
                # Probe MSP readiness.
                self._ser.write(encode_v1(MSP_API_VERSION))
                time.sleep(0.2)
                frame = self._read_msp_frame(timeout=0.8)
                decode_v1_response(frame)
                return True
            except Exception:
                # Not ready yet — close and retry.
                if self._ser and self._ser.is_open:
                    try:
                        self._ser.close()
                    except Exception:
                        pass
                self._ser = None
                time.sleep(0.5)
        self.mode = "IDLE"
        raise TimeoutError(
            f"FC did not respond to MSP within {settle_timeout:.0f}s after reboot. "
            "It may need more time, or re-enumerated as a different COM port."
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

    def exit_cli(self, save: bool = False, reconnect: bool = False) -> None:
        """Leave CLI mode. On iNAV, BOTH paths reboot the FC.

        save=False → 'exit'  : discards unsaved changes, then reboots.
        save=True  → 'save'  : persists to EEPROM, then reboots.

        Because the FC reboots and the USB VCP re-enumerates, the current handle
        becomes invalid. The connection is marked stale. If reconnect=True, we poll
        the port back to a usable MSP state (~7s); otherwise the caller must
        reconnect() before further use.

        NOTE: 'exit' DISCARDS any `set`/`aux`/etc. changes made in this session —
        only `save` makes CLI writes stick.
        """
        if self.mode != "CLI":
            return

        cmd = b"save\r" if save else b"exit\r"
        try:
            self._ser.write(cmd)
        except Exception:
            pass   # FC may drop the link mid-write as it reboots
        self.mode = "MSP"
        self.mark_stale()
        time.sleep(0.3)   # let the reboot begin

        if reconnect:
            self.reconnect()
