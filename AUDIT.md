# AUDIT — `inav-mcp` technical review

> Reviewer brief: senior Python / pyserial / binary-protocol / MCP / iNAV-safety review.
> Scope: static code review + offline local validation. **No write/motor actions were
> performed against a real flight controller.** Read like this is going near a real aircraft.
>
> Commit reviewed: `5446aee` (v0.3.4), branch `master`. Date: 2026-06-25.

---

## 1. Executive summary

**Is this repo good?** Yes — genuinely. It is one of the more careful hobby/embedded
MCP projects I've reviewed. The architecture is clean and well-separated, the iNAV
domain knowledge is real (reboot-on-CLI-exit, DFU churn from rapid reboots, runtime
box-ID resolution, the BOXNAMES>255-byte MSP-v1 overflow, the 8.x/9.x arming-flag bit
map), and the documentation is unusually honest about its own limits (firmware
calibration warnings, "bench-verified on this date" notes, version skew). The author
clearly bench-tested on real hardware (`hwtest.py` is a real bring-up harness).

**Is it safe enough to connect to a real FC?**
- **Read-only / diagnostics: yes.** `board_info`, `why_wont_it_arm`, `diagnose`,
  `read_sensors`, `read_gps`, `list_flight_modes`, `get_status` decode correctly for
  iNAV 8.x/9.x and never write. I'd trust these on my own aircraft.
- **Writes on the bench: with care, yes.** The atomic *backup → apply → save / rollback*
  model is well-designed and bench-verified, dry-run-by-default + auto-backup + read-back
  is solid. But there are real robustness gaps (fragile CLI prompt detection that can
  truncate a backup, fail-open armed guard, zero offline tests of the serial byte layer)
  that mean you should still verify in iNAV Configurator afterwards and never run it
  unattended.
- **Motor test: bench-only, props physically off.** The props gate is enforced and
  cannot be bypassed by the write-confirm flag (good, and tested). The residual risk is
  the *fail-open* armed guard and the fact that "props removed" is an honor-system flag
  the code cannot actually verify.

**Would I use it on my own aircraft?** Yes for diagnostics and bench config, **with iNAV
Configurator as the verification cross-check**, and never as the sole source of truth for
safety-critical settings (failsafe, mixer reversal). I would not let it run writes
unattended.

**Confidence level (1–10):**
- Read/diagnostic correctness: **8/10**
- Write path safety on a bench: **6/10**
- Serial-layer robustness under noise / re-enumeration / large dumps: **5/10** (mostly
  because it's untested offline, not because it's visibly wrong)
- Overall, as a tool to *improve and use*: **7/10** — better than expected.

---

## 2. What the repo gets right (specific)

1. **MSP arming-flag table is actually correct.** `inav_mcp/knowledge/arming_flags.json`
   bit positions (2=ARMED, 6=GEOZONE, 7=FAILSAFE_SYSTEM … 30=LANDING_DETECTED, with 17
   noted as removed) match the real iNAV `armingFlag_e` enum. The table self-declares its
   calibrated majors (`_calibrated_majors: [8, 9]`) and the code warns when the connected
   firmware is out of range (`troubleshoot.firmware_calibration_status`,
   surfaced in `connect`/`board_info`/`why_wont_it_arm`/`diagnose`). This is the single
   most safety-relevant decode in the project and it's done right.

2. **Correct separation of state bits vs disable reasons.** `msp.ARMING_DISABLE_REASON_MASK
   = 0x7FFFFFC0` (bits 6..30) is used to compute `can_arm`, while ARMED (bit 2) /
   SIMULATOR (bit 4) are treated as informational. `test_disable_reason_mask_excludes_state_bits`
   guards this. Many naive decoders get this wrong; this one doesn't.

3. **Runtime box-ID resolution, never hardcoded.** `modes.get_box_map` reads
   `MSP_BOXNAMES` + `MSP_BOXIDS` live and trims to the shorter list on mismatch
   (`modes.py:49-52`). This is the correct, version-robust approach and matches the spec.

4. **BOXNAMES MSP-v1 overflow handled.** `modes._read_msp` prefers MSP v2 because the
   mode-name string exceeds 255 bytes and overflows the v1 size byte (`modes.py:23-34`).
   This is a subtle, real iNAV gotcha that the author found empirically (`hwtest.boxprobe`).

5. **The reboot model is understood and engineered around.** Leaving the iNAV CLI always
   reboots; the code marks the connection stale (`connection.mark_stale`), and
   `reconnect()` does settle → backoff → re-enumeration scan → DFU detection with an
   actionable power-cycle message (`connection.py:130-191`). `cli_batch` collapses N
   commands into one reboot. This is mature handling of a genuinely annoying platform.

6. **Atomic write model.** `_apply_cli_writes` (`server.py:339-399`) takes the pre-change
   `diff all` backup *in the same CLI session* (no extra reboot), applies, detects silent
   FC rejections via `### ERROR` scanning (`cli.cli_error`), and `exit`s (discard) on any
   failure vs `save`s on full success — all-or-nothing. This is better than the spec asked
   for (the spec said "call backup_config() first", which would have cost an extra reboot).

7. **Props-off gate is real code, unbypassable, and tested.** `cli.is_actuator_command`
   matches only `motor <i> <v>` (not reads, not `set motor_*`, not `servo`), and the gate
   in `server.cli` is enforced *before any serial I/O* and is independent of
   `confirm_for_writes` (`server.py:519-545`). `test_cli_props_gate.py` covers the bypass
   attempt explicitly.

8. **Honest, well-written docs and changelog.** README's "Reboot model" and "Safety model"
   sections are accurate; the changelog records bench-verification dates and firmware
   skew. The firmware-calibration warning is plumbed through every arming-related tool.

9. **Clean packaging / CI hygiene.** `pyproject.toml` is minimal and correct (MIT, no GPL
   runtime deps — the MSP codec is hand-written precisely to stay permissive), CI runs the
   offline suite on 3.10/3.11/3.12, and a drift-guard test fails if the README tool table
   diverges from the registered tools.

---

## 3. Biggest risks

Ranked by potential to do harm.

### R1 — Fragile CLI prompt detection can silently truncate a backup or read (data integrity)
`connection._read_until_prompt` returns as soon as the buffer **ends with `b"# "`**
(`connection.py:349`). But iNAV `diff`/`dump` output is full of comment lines that begin
with `"# "` (`# version`, `# resources`, `# mixer`, …). `read(64)` returns whatever bytes
are available, so a chunk boundary can land immediately after the `"# "` of a comment line,
producing a **false prompt match mid-stream**. Consequences:
- A `diff all` **backup is truncated** → an incomplete file that, if later restored, drops
  settings. This is the worst case because backups are the safety net for every write.
- The unread remainder stays in the OS buffer. `run_cli` does **not** `reset_input_buffer()`
  before reading (unlike `send_msp_v1/v2`), so stale bytes prepend to the *next* command's
  response, cascading the corruption.

This is the project's most important latent bug. It's low-probability per call but the
backup path runs on every write, and the failure is silent. **Recommend** anchoring the
prompt to a line boundary (e.g. require `"\r\n# "` / "prompt is the tail after the last
newline") plus a quiet-period confirmation, and flushing before each CLI read. It needs a
golden-transcript test (see §5).

### R2 — Armed guard is fail-open (unsafe default for actuation)
`safety.check_not_armed` returns cleanly (does **not** raise) if the status read fails for
any reason (`safety.py:60` — `except Exception: return`). Every write and the motor test
treats "couldn't read armed state" as "not armed". For diagnostics that's fine; for
`test_motor` / live `motor` it's the wrong direction — if you can't *confirm* disarmed, you
should refuse, not proceed. `test_cli_props_gate.FakeConn` even documents this ("no
send_msp_* → check_not_armed passes"). Mitigations that exist: iNAV firmware itself ignores
MSP motor override while armed, and reaching the motor path needs a live MSP link. Still,
the actuation paths should be **fail-closed**.

### R3 — `restore_config` replays unfiltered commands (no actuator/safety filtering)
`restore_config` keeps every non-comment, `is_replayable` line and feeds them to
`_apply_cli_writes` (`server.py:463-481`), which does **not** call `is_actuator_command` or
the props gate. A normal `diff all` never contains `motor` lines, but the tool trusts the
file's contents completely — a hand-edited or wrong file could replay arbitrary CLI,
including a live `motor` spin, with no props-off gate. Restore should run the same actuator
/ session-control rejection that `cli_batch` already implements, and ideally warn on any
line that isn't a recognized `set`/`aux`/`feature`/`smix`/`map`/`servo`/etc.

### R4 — No iNAV-variant guard (cross-firmware misdiagnosis)
`connect` returns `connected: True` as long as the *serial port opens*, even for a non-iNAV
board or a non-FC USB device (identity errors are swallowed into `_error_cmd_*` keys,
`server._gather_board_info:86-87`). If you connect to a **Betaflight** board (`variant=BTFL`),
every iNAV-specific decode — `MSPV2_INAV_STATUS`, the arming-flag table — is silently wrong,
because Betaflight's bit positions differ entirely. There is a firmware-*version* warning but
no firmware-*variant* guard. A cheap `if variant != "INAV": warn/refuse` would close this.

### R5 — Serial byte-layer is entirely untested offline
Every test stubs at the `send_msp_v1/_v2` / `run_cli` method level. The actual byte-stream
code — `_read_msp_frame` (partial frames, noise, interleaved/wrong-cmd frames, error
frames), `_read_until_prompt`, the cmd-matching retry loops, `reconnect()` — has **zero**
offline coverage (the reconnect test only covers the pure port-classification helpers, and
says so). This is the most bug-prone category of code in any serial project, and it's the
part you can't see is correct just by reading. Not a bug per se, but it means the green
suite proves much less than it appears to.

### R6 — Concurrency: one shared serial handle, no lock
State is module-level singletons (`state._connection`) and tools are **synchronous**
functions, which FastMCP runs in a thread pool. Two overlapping tool calls (or a 5-second
`test_motor` loop while another read arrives) would interleave writes/reads on the single
handle and corrupt frames. In practice MCP clients usually serialize calls, so this rarely
bites — but nothing in the code enforces it. A single `threading.Lock` around connection
use would make the single-handle assumption real instead of hoped-for.

---

## 4. Bugs / suspicious code (file · function · reasoning)

| # | Location | Issue |
|---|---|---|
| B1 | `msp.parse_inav_status` (`msp.py:296-297`) | `result["mixer_profile"] = payload[13] & 0x0F` is almost certainly wrong. In real `MSP2_INAV_STATUS`, byte 13 is the **start of the flight-mode box bitmask**, not the mixer profile (the config-profile byte is at offset 8; the mixer profile is emitted much later, after the box bitmask). The unit test `_make_inav_status` encodes byte 13 as mixer profile, so the test validates the code's *assumption*, not firmware reality. Cosmetic (mixer_profile isn't used for safety), but it's a wrong field that "passes". |
| B2 | `connection._read_until_prompt` (`connection.py:349`) | False prompt match mid-stream → truncation. See **R1**. |
| B3 | `connection.run_cli` (`connection.py:388-398`) | No `reset_input_buffer()` before reading in CLI mode, so any leftover bytes from a previous truncated read corrupt the next command's response. (MSP path flushes; CLI path doesn't.) |
| B4 | `safety.check_not_armed` (`safety.py:54-66`) | Fail-open. See **R2**. Also TOCTOU: read once, then `test_motor` spins for up to 5 s. |
| B5 | `cli.is_write_command` / `_WRITE_PREFIXES` (`cli.py:41-64`) | Classification gaps: `serial`, `serialpassthrough`, `msc`, `bl`, `dfu`, `flash_erase`, `bind_rx` are **not** in the write list, so they're treated as read-only and run without `confirm_for_writes`. Mitigated because non-saved CLI writes are discarded on `exit`, but `serialpassthrough`/`msc`/`bl`/`dfu` can hang or reboot the link into a non-MSP state. A legitimate but unlisted write also *silently fails to persist* (footgun). |
| B6 | `server.connect` (`server.py:197-226`) | Returns `connected: True` for any port that opens, even non-iNAV / non-FC. No variant guard. See **R4**. |
| B7 | `msp.parse_mode_ranges` (`msp.py:378-384`) | Entry-size auto-detect tries 6 then 4 bytes via `len % size == 0 and 10 <= count <= 50`. Works for the standard 40-slot return, but is a heuristic that can mis-detect on a non-standard payload length where both 4 and 6 divide cleanly into the valid count range (e.g. 60 bytes). Needs golden fixtures from real 6-byte (iNAV 7+) and 4-byte (older) dumps. |
| B8 | `connection._read_msp_frame` v2 branch (`connection.py:235-246`) | No upper bound on the claimed v2 `length` (u16, up to 65535) before issuing `read(length+1)`. Self-recovers via the short-read `continue`, but a corrupt length wastes a full read timeout each time. Low severity. |
| B9 | `server.cli` writing `save`/`exit` as a `command` | If a user passes `cli("save")`, `run_cli("save")` triggers an immediate save+reboot, the `# ` prompt never returns → `TimeoutError`, then `finally: exit_cli(save=True)` writes to a rebooting FC. Messy but not dangerous (nothing extra persists). `cli_batch` guards this with `is_session_control_command`; `cli` does not. |
| B10 | `profiles._SMIX_RULES["conventional"]` (`profiles.py:64-69`) | Both flaperons get `ROLL +100%` and rely on one servo being *mechanically* reversed (the warning says so). If a user applies this without reversing one aileron, both ailerons deflect the same way → no roll authority / reversed roll. This is documented, but it's a config the tool can produce that is unflyable until a physical/`servo` reversal is done. Treat as "must verify on bench", which the warnings do say. |

None of B1–B10 are outright crashes; the suite is green. They are correctness/robustness/safety-margin issues that matter specifically *because* this touches an aircraft.

---

## 5. Missing tests (most important first)

1. **Golden MSP packet fixtures + a fake serial.** Add a `FakeSerial` that yields scripted
   bytes and drive `_read_msp_frame` / `send_msp_v1` / `send_msp_v2` through: clean frame,
   frame split across multiple `read()` calls, leading garbage/noise, an interleaved
   wrong-cmd frame before the right one, an `$M!` / `$X!` error frame, a truncated frame,
   and back-to-back frames. This is the highest-value missing test.
2. **CLI transcript fixtures for `_read_until_prompt` / `strip_cli_response`.** Feed a real
   `diff all` capture (with `# comment` lines) chunked at adversarial boundaries and assert
   the full output is returned and **not truncated at a comment-line `# `**. Directly
   targets R1/B2/B3.
3. **`reconnect()` I/O loop with a fake port factory.** Simulate: same port returns after N
   tries; original gone but a re-enumerated port answers; nothing answers + a DFU device
   present → `ConnectionError`; nothing answers, no DFU → `TimeoutError`.
4. **Fail-closed armed guard tests.** Once R2 is fixed: status-read-fails → `test_motor`
   refuses; armed=True → refuses; disarmed → proceeds.
5. **`restore_config` filtering tests.** A backup file containing a `motor 1 2000` line (or
   `save`/`exit`/`defaults`) must be rejected/stripped, not replayed.
6. **`parse_mode_ranges` golden fixtures** for 4-byte and 6-byte real payloads, including a
   disabled slot (`start>=end`) and the 40-slot full return.
7. **Variant guard test** — connecting to a `BTFL`/`BTFL`-identifying probe surfaces a
   warning/refusal rather than decoding arming flags as iNAV.
8. **`cli_error` / silent-rejection coverage** for the real iNAV strings (`### ERROR: Invalid
   name`, `Invalid value`) to lock the rejection signal.

---

## 6. Improvement roadmap

### Must fix before (further) real-aircraft use
- **R1/B2/B3** — robust CLI prompt detection + flush before CLI reads; it protects backup
  integrity.
- **R2/B4** — make the armed guard **fail-closed** for actuation (`test_motor`, live
  `motor`).
- **R3** — filter `restore_config` (and `_apply_cli_writes`) for actuator/session-control
  commands.
- **R5** — add the fake-serial + golden-frame tests so the riskiest code is actually
  exercised offline.

### Should fix soon
- **R4/B6** — iNAV variant guard on connect.
- **R6** — a connection lock (or documented "single-flight" assumption enforced in code).
- **B5** — expand write/destructive command classification; add an explicit deny-list for
  `msc`/`bl`/`dfu`/`serialpassthrough`/`flash_erase`.
- **B1** — fix or drop `mixer_profile` and fix its test to reflect real firmware bytes.
- Add a **structured command log** (every CLI line and MSP cmd sent to the FC, with
  timestamps) — invaluable for post-incident review on a config tool.

### Nice to have
- Explicit **read-only / safe mode** env flag that disables every write/actuator tool.
- **Firmware-version compatibility table** beyond arming flags (payload layouts that differ
  across majors).
- Split `server.py` (2048 lines) into tool modules (see §8).
- Lint/type/security in CI (ruff + mypy + pip-audit).

### Future advanced
- Recorded **hardware-in-loop** transcript replay as a CI job (capture once on a bench, replay
  forever).
- A bundled **fake FC simulator** (answers MSP + emulates CLI + the reboot/re-enumeration
  dance) so the whole tool can be exercised end-to-end with no hardware.
- Optional checksum/round-trip of the backup file before trusting it for restore.

---

## 7. Suggested GitHub issues (copy-ready)

1. **Robust CLI prompt detection — guard against false `# ` match mid-`diff all`.** Anchor
   the prompt to a line boundary + quiet period; add a chunked-transcript regression test.
2. **Flush input buffer before each CLI read in `run_cli`.** Prevents cascade corruption
   after a short/early read.
3. **Make the armed guard fail-closed for actuation.** `test_motor` and live `motor` should
   refuse when armed state cannot be confirmed.
4. **Filter `restore_config` replays.** Reject `motor`/`save`/`exit`/`defaults` and warn on
   unrecognized command verbs before applying.
5. **Add a `FakeSerial` and golden MSP frame fixtures.** Unit-test `_read_msp_frame` /
   `send_msp_v1` / `send_msp_v2` for split/noisy/error/interleaved frames.
6. **iNAV variant guard on `connect`.** Warn/refuse if `variant != "INAV"`; don't decode
   arming flags for Betaflight.
7. **Add a connection lock.** Serialize serial access so overlapping tool calls can't
   corrupt frames.
8. **Fix `mixer_profile` decode in `parse_inav_status` (byte 13 is the box bitmask).** Or
   drop the field; fix the misleading test.
9. **Golden fixtures for `parse_mode_ranges` (4-byte and 6-byte real payloads).** Remove
   reliance on the size-guess heuristic for correctness.
10. **Expand write/destructive command classification.** Add a deny-list for
    `msc`/`bl`/`dfu`/`serialpassthrough`/`flash_erase`; treat unknown writes explicitly.
11. **`reconnect()` I/O loop test with a fake port factory.** Cover same-port, re-enumerated,
    DFU, and give-up paths.
12. **Structured command log.** Persist every FC-bound CLI/MSP command with timestamps for
    audit/incident review.
13. **Explicit read-only mode.** `INAV_MCP_READ_ONLY=1` disables all write/actuator tools.
14. **Backup integrity check before restore.** Verify the file looks like a complete
    `diff all` (expected header/footer) before replaying.
15. **CI: add ruff + mypy + pip-audit.** Lint/type/security gates alongside the test job.
16. **Bound MSP v2 frame length in `_read_msp_frame`.** Reject absurd lengths instead of
    issuing a giant `read`.
17. **Guard `cli("save"/"exit")`.** Apply `is_session_control_command` in `cli`, not just
    `cli_batch`.
18. **Document the props gate is honor-system.** Make the UI/return text explicit that the
    tool cannot verify props are physically removed.

---

## 8. Suggested architecture changes

- **Split `server.py` (2048 lines).** It currently holds every tool, resource, and prompt.
  Suggested split, keeping FastMCP registration in each module:
  `tools/connection.py`, `tools/config.py` (backup/restore/cli/cli_batch/save),
  `tools/diagnostics.py`, `tools/setup.py` (profile/apply/check), `tools/modes.py`,
  `tools/bench.py` (motor/calibration/failsafe), `tools/nav_tuning.py`, plus
  `resources.py` / `prompts.py`. `main()` imports them to register.
- **Add a fake FC / serial simulator** (`tests/fake_fc.py`) that speaks MSP and emulates the
  CLI + reboot/re-enumeration. Unlocks true end-to-end tests with no hardware.
- **Golden MSP packet fixtures** captured from a real board (`tests/fixtures/*.bin` + a small
  capture script extracted from `hwtest.py`). The `armingmap`/`boxprobe`/`reconnect_timing`
  probes in `hwtest.py` already produce exactly this data — persist it.
- **Firmware-version compatibility tables**: generalize `firmware_calibration_status` from
  "arming flags only" to a per-payload-layout registry, so `parse_*` functions can be
  version-gated rather than best-effort.
- **Structured command log**: a thin `audit_log.py` that every `run_cli` / `send_msp_*` call
  routes through (JSONL with ts, mode, bytes/cmd, outcome).
- **Safer restore filtering**: a `restore_filter.py` allow-listing the command verbs a
  backup may legitimately contain.
- **Explicit read-only mode**: gate registration of write/actuator tools on an env flag.
- **Hardware-in-loop notes**: promote `hwtest.py` from "scratch" to a documented bench
  procedure (`docs/HIL.md`) with the exact steps and expected outputs, and wire its capture
  output into the fixtures above.
- **Connection lock**: wrap the singleton connection in a small manager that holds a
  `threading.Lock` for the duration of each tool's serial use.

---

## 9. Final recommendation

**Fork-and-improve, and in the meantime use it freely for read-only diagnostics; use the
write tools only on the bench with iNAV Configurator cross-checking.**

This is a well-built, honestly-documented project from someone who clearly knows iNAV. The
read/diagnostic path is trustworthy today. The write path is well-architected but rests on a
fragile CLI-scraping primitive (R1) and a fail-open safety default (R2), and the whole
serial layer is unverified offline (R5). None of those are hard to fix — they're a focused
weekend of hardening, not a rewrite. After R1–R3 and the fake-serial tests land, I'd move my
write-path confidence from 6 to 8.

Do **not** "use as-is, unattended, for safety-critical writes (failsafe/mixer)". Do **not**
avoid it either — it's better than that. The right move is a small hardening PR (below), then
adopt.

---

## 10. Proposed first PR (test coverage + safety hardening — no new features)

Small, reviewable commits, in order:

1. **`test: add FakeSerial + golden MSP frame fixtures`** — new `tests/fake_serial.py` and
   `tests/test_frame_io.py` covering `_read_msp_frame` / `send_msp_v1` / `send_msp_v2`
   (split, noisy, error, interleaved, truncated frames). Pure additive, no source change.
2. **`test: CLI transcript fixtures for prompt detection`** — capture a real `diff all`
   (sanitized) and assert no truncation at comment-line `# ` boundaries. This test should
   **fail** against current `_read_until_prompt`, demonstrating R1.
3. **`fix: robust CLI prompt detection + flush before CLI reads`** — anchor prompt to a line
   boundary + short quiet-period; `reset_input_buffer()` at the top of `run_cli`. Makes
   commit 2 pass. (B2/B3/R1.)
4. **`fix: fail-closed armed guard for actuation`** — `check_not_armed` grows a
   `require_reading: bool` (default off for diagnostics, **on** for `test_motor` / live
   `motor`); add tests. (B4/R2.)
5. **`fix: filter restore_config / _apply_cli_writes for actuator + session commands`** —
   reuse `is_actuator_command` / `is_session_control_command`; add tests. (R3.)

Each commit keeps the suite green (commit 2 is the deliberate red-then-green pair with
commit 3). No tool signatures change, so the README drift-guard stays happy.

---

### Appendix — local validation performed (no FC writes)
- `python -m pip install -e ".[dev]"` — package builds; editable install of the console
  script was blocked only by a file lock (the live `inav` MCP server process is running from
  this `.venv`), not a packaging error. Source imports fine.
- `python -c "import inav_mcp"` → `import ok`; `FastMCP name: iNAV Flight Controller`.
- `python -m pytest -q` → **186 passed in 0.81s** (all offline, all mocked at the
  `send_msp_*`/`run_cli` boundary — see R5 for what that does *not* prove).
- No `connect`/write/`test_motor` tool was invoked against hardware.
