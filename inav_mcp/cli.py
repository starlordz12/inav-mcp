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
    """Return True if the CLI command looks like a write operation."""
    c = cmd.strip().lower()
    # Explicit read-only list takes priority
    if any(c.startswith(p) or c == p.rstrip() for p in _READ_PREFIXES):
        return False
    return any(c.startswith(p) for p in _WRITE_PREFIXES)


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
