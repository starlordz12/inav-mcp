"""A scriptable fake serial handle for offline byte-level tests.

Models pyserial read semantics faithfully: `read(size)` gathers bytes across
scripted chunks until `size` bytes are collected OR the stream goes quiet.
"Quiet" is scripted with a `None` entry in the chunk list — read() stops there
and returns what it has so far (possibly b''), exactly like a real timeout.
When the script is exhausted the line is permanently quiet (every read
returns b'').

Chunk boundaries WITHOUT a None between them are coalesced by read(), just
like the OS serial buffer coalesces USB packets — use None wherever the test
needs to control exactly where a read returns.
"""
from __future__ import annotations


class FakeSerial:
    def __init__(self, chunks=(), timeout: float = 1.0, clear_on_reset: bool = False):
        # Script: bytes entries are data, None entries are quiet gaps (timeouts).
        self._script: list[bytes | None] = list(chunks)
        self.timeout = timeout
        self.is_open = True
        self.written: list[bytes] = []
        self.reset_count = 0
        # If True, reset_input_buffer() discards everything currently scripted —
        # use to test that stale pre-command bytes are actually flushed.
        self._clear_on_reset = clear_on_reset

    def feed(self, *chunks: bytes | None) -> None:
        self._script.extend(chunks)

    def read(self, size: int = 1) -> bytes:
        out = bytearray()
        while len(out) < size and self._script:
            head = self._script[0]
            if head is None:                     # scripted quiet gap → timeout
                self._script.pop(0)
                break
            take = head[: size - len(out)]
            out.extend(take)
            rest = head[len(take):]
            if rest:
                self._script[0] = rest
            else:
                self._script.pop(0)
        return bytes(out)

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def reset_input_buffer(self) -> None:
        self.reset_count += 1
        if self._clear_on_reset:
            self._script.clear()

    def close(self) -> None:
        self.is_open = False
