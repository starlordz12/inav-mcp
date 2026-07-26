"""CLI prompt-detection tests — guards against silent truncation (AUDIT.md R1).

iNAV 'diff'/'dump' output is full of comment lines that begin with '# '
('# version', '# mixer', …). If a read boundary lands right after one of those
'# ' sequences, the buffer tail looks exactly like the CLI prompt — naive
endswith('# ') detection returns there and silently truncates the output. The
worst case is a truncated 'diff all' BACKUP that later restores an incomplete
config. These tests pin the quiet-window confirmation that prevents it.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp.connection import SerialConnection
from fake_serial import FakeSerial


def _cli_conn(fake: FakeSerial) -> SerialConnection:
    c = SerialConnection("FAKE")
    c._ser = fake
    c.mode = "CLI"
    c.stale = False
    return c


# A realistic diff-all shape: comment lines interleaved with settings.
_DIFF_BODY = (
    "diff all\r\n"
    "# version\r\n"
    "# INAV/SPEEDYBEEF405WING 6.1.0 Jun  1 2023 / 12:00:00 (0000000)\r\n"
    "# start the command batch\r\n"
    "batch start\r\n"
    "# mixer\r\n"
    "mixer AIRPLANE\r\n"
    "smix 0 1 0 50 0 0\r\n"
    "# servo\r\n"
    "servo 1 1000 2000 1500 100\r\n"
    "set platform_type = AIRPLANE\r\n"
    "set motor_pwm_protocol = DSHOT600\r\n"
    "batch end\r\n"
)
_PROMPT = "\r\n# "


def test_comment_line_at_read_boundary_does_not_truncate():
    """Regression for R1: force a read to return exactly after a comment's '# '.

    _read_until_prompt reads in 64-byte gulps, so we pad the transcript to make
    a '\\r\\n# ' (the start of '# mixer') land exactly on a 64-byte boundary.
    Naive endswith detection truncates there; the quiet-window check must not.
    """
    head = "diff all\r\n"
    # Pad with a comment line so that the text up to and including the next
    # line's '# ' is an exact multiple of 64 bytes.
    tail_after_boundary = "mixer comment continues\r\n" + "set foo = 1\r\n"
    for pad in range(200):
        prefix = head + "# " + "x" * pad + "\r\n# "
        if len(prefix) % 64 == 0:
            break
    else:
        raise AssertionError("could not construct a 64-aligned prefix")

    transcript = prefix + tail_after_boundary + _PROMPT
    conn = _cli_conn(FakeSerial([transcript.encode()]))
    raw = conn._read_until_prompt(timeout=2.0)
    assert "set foo = 1" in raw, "output was truncated at a comment-line '# '"
    assert raw.endswith("# ")


def test_full_diff_transcript_returned_intact():
    transcript = _DIFF_BODY + _PROMPT
    conn = _cli_conn(FakeSerial([transcript.encode()]))
    out = conn.run_cli("diff all", timeout=2.0)
    # Every line of the body must survive, including all '# comment' lines.
    for line in ("# version", "# mixer", "# servo", "batch end",
                 "set motor_pwm_protocol = DSHOT600"):
        assert line in out, f"missing line: {line!r}"


def test_quiet_gap_after_comment_prompt_lookalike_still_recovers():
    # The FC pauses (one quiet window) right after a comment-line '# ', then
    # resumes. The confirmation read picks the continuation up on resume.
    part1 = "diff all\r\n# version\r\n# "
    part2 = "resources continue here\r\nset bar = 2\r\n" + _PROMPT
    conn = _cli_conn(FakeSerial([part1.encode(), None, part2.encode()]))
    raw = conn._read_until_prompt(timeout=2.0)
    assert "set bar = 2" in raw


def test_enter_cli_banner_prompt_detected():
    fake = FakeSerial([b"Entering CLI Mode, type 'exit' to return, or 'help'\r\n\r\n# "])
    conn = _cli_conn(fake)
    conn.mode = "MSP"
    banner = conn.enter_cli(timeout=2.0)
    assert conn.mode == "CLI"
    assert "Entering CLI" in banner


def test_stale_bytes_flushed_before_command():
    """run_cli must reset the input buffer so leftovers from an earlier short
    read can't prepend to this command's response (AUDIT.md B3)."""
    class EchoSerial(FakeSerial):
        def __init__(self, response: bytes, **kw):
            super().__init__(**kw)
            self._response = response

        def write(self, data: bytes) -> int:
            n = super().write(data)
            self.feed(self._response)
            return n

    fake = EchoSerial(b"status\r\nSystem Uptime: 42 seconds\r\n" + _PROMPT.encode(),
                      clear_on_reset=True)
    fake.feed(b"STALE LEFTOVER BYTES ")   # already in the OS buffer pre-command
    conn = _cli_conn(fake)
    out = conn.run_cli("status", timeout=2.0)
    assert "STALE" not in out
    assert "System Uptime" in out
    assert fake.reset_count >= 1


def test_runaway_output_guard():
    conn = _cli_conn(FakeSerial([b"x" * (130 * 1024)]))
    try:
        conn._read_until_prompt(timeout=5.0)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "KB" in str(e)


def test_prompt_timeout_reports_tail():
    conn = _cli_conn(FakeSerial([b"no prompt here"]))
    try:
        conn._read_until_prompt(timeout=0.3)
        assert False, "should have raised"
    except TimeoutError as e:
        assert "no prompt here" in str(e)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
