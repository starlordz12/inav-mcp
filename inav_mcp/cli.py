"""CLI response parsing utilities (no serial I/O here).

The FC echoes our command on the first line and ends each response with '# ' (no newline).
strip_cli_response removes both so callers get clean output.
"""
from __future__ import annotations


# ── Command-injection guard (S3) ────────────────────────────────────────────────
#
# run_cli() sends `cmd + '\r'` verbatim. The FC's CLI parser treats ANY control
# character as a line terminator, not just '\r' — so a caller-supplied argument
# that embeds one splits the wire payload into two (or more) commands, the
# second of which never passed confirmation, backup, or the armed guard. We
# therefore reject the whole control-character class (every codepoint below
# 0x20, plus DEL 0x7F), not just '\r'/'\n' — a future caller could just as
# easily be tripped up by, say, a literal ESC or NUL in an argument, and this
# validator must not need updating every time someone finds a new one.

def validate_cli_command(cmd: str) -> None:
    """Raise ValueError if `cmd` could smuggle a second command onto the wire.

    Rejects any control character (0x00-0x1F, 0x7F) anywhere in the string —
    leading, trailing, or embedded — since run_cli() appends only a single
    trailing '\\r' and sends the result as one write. A clean command must be
    exactly one line with no such bytes in it.
    """
    if not isinstance(cmd, str):
        raise ValueError(f"CLI command must be a string, got {type(cmd).__name__}")
    for ch in cmd:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(
                f"CLI command contains a control character (0x{ord(ch):02X}) — "
                "rejected to prevent smuggling a second command onto the wire"
            )


# ── Constrained CLI enum arguments (S3) ─────────────────────────────────────────
#
# Tools that accept a short enum-like token (failsafe procedure, GPS provider,
# SBAS mode, RTH landing policy) used to interpolate it into a 'set ...' command
# with nothing more than .strip().upper() — any string passed validate_cli_command
# fine as long as it had no control characters, INCLUDING an unrecognised token
# that the FC alone would reject. Validating against an explicit allow-list here
# means junk (or a smuggled command whose control character got caught above,
# but whose survives-as-text-only case still doesn't match a real token) is
# rejected before a command string is even built, not mid-write on the FC.

FAILSAFE_PROCEDURES: frozenset[str] = frozenset({"DROP", "LAND", "SET-THR", "RTH", "NONE"})
GPS_PROVIDERS: frozenset[str] = frozenset({"UBLOX", "NMEA", "MSP"})
GPS_SBAS_MODES: frozenset[str] = frozenset({"AUTO", "EGNOS", "WAAS", "MSAS", "GAGAN", "NONE"})
RTH_LANDING_POLICIES: frozenset[str] = frozenset({"NEVER", "ALWAYS", "FS_ONLY"})


def validate_enum_value(value: str, allowed: frozenset[str], field: str) -> str:
    """Normalise `value` and check it against `allowed`; return the clean token.

    Raises ValueError on anything outside the allow-list. The raw `value` is
    deliberately NEVER included in the error message: an invalid value may be a
    smuggled second command (e.g. an embedded '\\r' followed by another `set`),
    and echoing it back — even after .strip().upper() — would leak that payload
    into a tool result that is otherwise supposed to have rejected it outright.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalised = value.strip().upper()
    if normalised not in allowed:
        raise ValueError(
            f"Invalid {field} — must be one of: {', '.join(sorted(allowed))}"
        )
    return normalised


def strip_cli_response(cmd: str, raw: str) -> str:
    """Remove the echoed command and trailing '# ' prompt from a CLI response.

    FC behaviour:
      - Echoes our command as the first line (exact match of what we sent).
      - Appends '# ' (without a preceding newline) as the prompt after output.
    """
    # Remove the trailing "# " prompt
    if raw.endswith("# "):
        raw = raw[:-2]

    # Normalise line endings
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    lines = raw.split("\n")

    # Drop the echoed command (first non-empty line matching what we sent)
    cmd_stripped = cmd.strip()
    if lines and lines[0].strip() == cmd_stripped:
        lines = lines[1:]

    # Drop leading/trailing blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


# Commands that are POSITIVELY known to be read-only (S2) — a caller may run
# these WITHOUT confirm_for_writes. This is a READ allowlist, not a write
# blocklist: anything absent from it — a known write verb (set/save/aux/...)
# *or* a verb the classifier has simply never heard of (a new/renamed firmware
# CLI command, a typo, `serialpassthrough`, `msc`, `bl`, `flash_erase`, ...) —
# is treated as a write and requires confirmation. Fail-closed by design: an
# unrecognised command must never get a free pass just because nobody taught
# the classifier about it yet. `exit` is deliberately NOT here — it always
# ends the CLI session (and iNAV always reboots on CLI exit), and cli() must
# refuse it as session control before touching the connection at all (see
# is_session_control_command / cli()'s own guard), never send it as a raw
# "read" pass-through.
#
# Built from what this codebase and test suite already document as read-only
# CLI verbs (see tests/test_cli.py, tests/test_cli_unknown_unsafe.py) — not
# from a live board `help` dump. Spot-check this list against a real board's
# `help` output at the next bench session before trusting it further.
_READ_ONLY_VERBS: tuple[str, ...] = (
    "diff",
    "get ",
    "status",
    "tasks",
    "dump",
    "help",
    "version",
    "boards",
    "# ",       # comment/noop
)


# Lines in a saved 'diff all' / 'dump' that manage batch/save/reboot themselves.
# When REPLAYING a backup we drive the single CLI session (and its save+reboot)
# ourselves, so these must be skipped — a mid-replay `save` would reboot the FC
# and abort the rest of the session, and `batch start` without our control would
# defer commits. `defaults noreboot` is intentionally NOT skipped: it's the clean
# base a diff is meant to be applied onto.
_REPLAY_SKIP: frozenset[str] = frozenset(
    {"batch start", "batch end", "save", "exit", "diff", "diff all", "dump", "dump all"}
)


def is_replayable(cmd: str) -> bool:
    """False for backup-file lines that manage their own batch/save/reboot."""
    return cmd.strip().lower() not in _REPLAY_SKIP


def parse_get_output(text: str) -> dict[str, str]:
    """Parse the CLI `get <prefix>` output into a {name: value} dict.

    iNAV prints one `name = value` line per matching setting. Lines that are
    comments, errors, or lack '=' are skipped.
    """
    settings: dict[str, str] = {}
    for line in text.replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("###") or "=" not in s:
            continue
        name, _, value = s.partition("=")
        name = name.strip()
        value = value.strip()
        if name and " " not in name:   # a setting name is a single token
            settings[name] = value
    return settings


def cli_error(raw: str) -> str | None:
    """Return the CLI error text if the response indicates a rejected command.

    iNAV prints an '### ERROR: ...' line (e.g. 'Invalid name', 'Invalid value')
    when a command or setting name/value is rejected. A normal response never
    starts a line with '###', so that marker is a reliable rejection signal.
    Without this, a silently-rejected write (e.g. an unknown `set` variable) would
    look successful because the serial read itself didn't time out.
    """
    for line in raw.replace("\r", "\n").split("\n"):
        s = line.strip()
        if s.startswith("###"):
            return s
    return None


def is_write_command(cmd: str) -> bool:
    """Return True unless `cmd` positively matches a known read-only verb (S2).

    Fail-closed inversion: this is NOT "is it on a list of known writes" — it's
    "is it positively proven safe". A command only counts as read-only (False)
    if it matches `_READ_ONLY_VERBS`. Every known write verb (set/save/aux/...)
    AND every unrecognised/unknown verb both return True, so an unfamiliar
    firmware command requires the same confirmation a write would.
    """
    c = cmd.strip().lower()
    return not any(c.startswith(p) or c == p.rstrip() for p in _READ_ONLY_VERBS)


# Commands that drive the CLI session lifecycle itself (commit/leave/reboot). A
# batched CLI session manages save/exit on its own, so these must not appear in a
# user-supplied batch — a mid-batch `save`/`exit`/`batch end` would reboot or
# commit early and break the one-reboot-per-batch guarantee.
_SESSION_CONTROL_VERBS: frozenset[str] = frozenset({"save", "exit", "batch"})


def is_session_control_command(cmd: str) -> bool:
    """Return True for save/exit/batch — commands the batch runner owns itself."""
    tokens = cmd.strip().lower().split()
    return bool(tokens) and tokens[0] in _SESSION_CONTROL_VERBS


# Commands that drive a LIVE motor output the instant they run — `motor <index>
# <value>` overrides a motor and spins it immediately while in CLI. These are
# momentary bench tests, NOT persistent config: they gate on props-off (§10.1)
# and must never be SAVEd (saving a momentary value is wrong, and 'save' reboots
# the FC mid-test). Bare read forms — `motor` / `motor <index>`, which only PRINT
# values — do not match, so reading stays free. NOTE: `servo ...` in the CLI is a
# persistent servo-config write (min/max/middle/rate), not a live output, so it
# stays on the normal write path and is NOT gated here.
_ACTUATOR_VERBS: frozenset[str] = frozenset({"motor"})


def is_actuator_command(cmd: str) -> bool:
    """Return True if the command drives a live motor output (can spin a prop).

    Matches `motor <index> <value>` (verb + index + value). Read forms — `motor`
    or `motor <index>`, which only print values — do not match. Enforces the
    props-off gate: such commands require props_removed=True, refuse while armed,
    and are never saved.
    """
    tokens = cmd.strip().lower().split()
    return len(tokens) >= 3 and tokens[0] in _ACTUATOR_VERBS
