"""CLI response parsing utilities (no serial I/O here).

The FC echoes our command on the first line and ends each response with '# ' (no newline).
strip_cli_response removes both so callers get clean output.
"""
from __future__ import annotations


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


# Commands that can WRITE to the FC — require explicit confirmation.
# Conservative: anything not explicitly read-only that matches a prefix below.
_WRITE_PREFIXES: tuple[str, ...] = (
    "set ",
    "save",
    "defaults",
    "mixer ",
    "motor ",
    "aux ",
    "feature ",
    "map ",
    "smix ",
    "servo ",
    "resource ",
    "led ",
    "color ",
    "mode_color ",
    "gpspassthrough",
    "adjrange ",
    "rxrange ",
    "vtx ",
    "beacon ",
    "batch ",
    "profile ",
    "rateprofile ",
)

_READ_PREFIXES: tuple[str, ...] = (
    "diff",
    "get ",
    "get\r",
    "status",
    "tasks",
    "dump",
    "help",
    "version",
    "boards",
    "exit",
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
    """Return True if the CLI command looks like a write operation."""
    c = cmd.strip().lower()
    # Explicit read-only list takes priority
    if any(c.startswith(p) or c == p.rstrip() for p in _READ_PREFIXES):
        return False
    return any(c.startswith(p) for p in _WRITE_PREFIXES)
