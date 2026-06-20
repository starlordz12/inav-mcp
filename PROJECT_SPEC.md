# iNAV MCP Server — Project Spec & Build Plan

**What this is:** a single, self-contained spec for an MCP server that lets Claude set up, configure, and troubleshoot an **iNAV** fixed-wing flight controller over USB — driven by plain-English requests like *"set this up as a flying wing with a 2207 1800KV on DSHOT600 and 4S,"* *"put ANGLE/HORIZON/MANUAL on my 3-position switch,"* or *"why won't it arm?"*

**How to use this file:** drop it into an empty repo as `PROJECT_SPEC.md`, then paste the kickoff prompt in §1 into Claude Code. Claude Code reads the rest of this file and builds the project against the milestones in §11.

> ⚠️ **Hard safety rule, stated once up front and enforced everywhere:** this tool can write flight-controller config and command motor output. **Props off the aircraft for every motor test, always.** Every write backs up first, applies, then reads back to verify. `save` reboots the FC. See §10.

---

## 1. Copy-paste kickoff for Claude Code

### 1a. One-time shell setup (run in an empty folder)

```bash
mkdir inav-mcp && cd inav-mcp
git init

# Put this spec file in the repo root as PROJECT_SPEC.md before launching Claude Code.

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install mcp pyserial

# Optional (GPL-3.0 — see §12 licensing note). Useful as an enum/MSP reference source:
# pip install "git+https://github.com/xznhj8129/uNAVlib"
```

Then launch Claude Code in that folder and paste the prompt below.

### 1b. Prompt to paste into Claude Code

```
Read PROJECT_SPEC.md in this directory in full. It is the complete specification for an
iNAV MCP server. Build it.

Constraints:
- Python 3.10+, sync code (no asyncio unless a tool genuinely needs it).
- Use the official MCP SDK: `from mcp.server.fastmcp import FastMCP`. Transport = stdio.
- Talk to the flight controller over a SINGLE pyserial connection that can switch between
  MSP mode and CLI mode (see §4). Do NOT open two serial handles to the same port.
- Lean on the iNAV CLI for all config WRITES and most reads (it's stable across firmware
  versions). Use a small built-in MSP v1/v2 codec only for the live/binary reads listed
  in §6 (arming flags, RC channels, GPS, analog, box names/ids, sensor status).
- Implement every safety gate in §10 as actual code, not just docstrings.
- Build in the milestone order in §11. After each milestone, stop and tell me how to test it
  against my real FC before continuing.

Before writing code: confirm the CLI enter/exit handshake (§4) and the exact `mcp` SDK
entrypoint by checking the installed package, since both can drift. If anything in the spec
conflicts with the installed library versions, follow the library and flag it to me.

When the server runs, register it with Claude Code/Claude Desktop and show me the exact
config entry to use, plus how to start it.

Start with Milestone 0. Ask me to plug in my FC and tell you the serial port when you need it.
```

---

## 2. Goals & non-goals

**Goals**
- Stand up a new iNAV fixed-wing from hardware description → applied config, with confirmation gates.
- Make flight-mode and switch assignment trivial (the part of iNAV setup people get wrong most).
- A real troubleshooter: decode *why* the board won't arm, surface sensor/GPS/RC/failsafe problems, lint the config against the declared aircraft.
- Everything reversible and verifiable (auto-backup + read-back).

**Non-goals (v1)**
- No autonomous flight / MSP-RC-override / missions. (That's uNAVlib's territory; not needed for setup.)
- No firmware flashing (point the user to the iNAV Configurator / DFU).
- Betaflight support is out of scope for v1, but keep the MSP/CLI layers generic so it's a later add.

---

## 3. Why CLI-first + thin MSP (the key architectural decision)

iNAV exposes two interfaces over the same USB VCP port:

- **CLI** — human-readable, *stable across firmware versions*, covers basically everything writable: `set`/`get`/`diff`/`dump`/`aux`/`feature`/`map`/`mixer`/`smix`/`servo`/`motor`/`status`/`tasks`/`save`. This is the workhorse for config and most diagnostics.
- **MSP** (MultiWii Serial Protocol) — binary request/response, v1 (IDs 0–254) and v2 (IDs 0x1000+). Best for *live* structured reads where parsing CLI text is awkward: arming flags, instantaneous RC channel values, GPS fix, battery, sensor health, mode-range tables.

**Decision:** CLI-first for writes and config reads; a small built-in MSP codec for the handful of live/binary reads in §6. This keeps the dependency surface tiny, makes behavior robust to iNAV version bumps, and avoids relying on a fast-moving third-party SDK for the critical path. `uNAVlib` is **optional**, used only as a convenient source of auto-generated INAV enums (mode IDs, MSP codes) and as reference — not as the live transport.

This also sidesteps a common confusion: iNAV's "MSP RX (control via MSP)" receiver mode is for *flying via MSP*. **We do not touch it.** Config/tuning works over plain USB with no receiver-mode change.

---

## 4. Connection layer — one serial handle, two modes

`inav_mcp/connection.py` owns a single `serial.Serial` (default 115200 baud) and a `mode` state of `IDLE | MSP | CLI`.

**MSP mode:** default. Frame and send MSP requests, read responses (codec in §6).

**CLI mode handshake** (verify empirically on first run — some firmware is picky about line endings):
1. Send `#` followed by a carriage return.
2. FC prints a banner and a `# ` prompt; reads suspend MSP parsing while in CLI.
3. Send each command terminated with `\r`; read until the `# ` prompt returns (that's your response boundary). Add a timeout + max-bytes guard.
4. Leave CLI with **`exit`** (discards unsaved changes, returns to MSP) or **`save`** (writes to EEPROM and **reboots** — connection drops; plan a reconnect).

Rules:
- Never interleave MSP and CLI on the same handle without switching `mode` first.
- `enter_cli()` / `run_cli(cmd) -> str` / `exit_cli(save: bool=False)`.
- After `save`, mark the connection stale and require an explicit reconnect.
- A small response parser strips the echoed command and the trailing prompt.

---

## 5. Repo layout

```
inav-mcp/
  PROJECT_SPEC.md            # this file
  README.md                 # generated: quickstart + safety + tool list
  pyproject.toml            # or requirements.txt
  inav_mcp/
    __init__.py
    server.py               # FastMCP app, tool/resource/prompt registration, run()
    connection.py           # SerialConnection: owns port, MSP/CLI mode switching
    msp.py                  # minimal MSP v1/v2 codec + the read commands in §6
    cli.py                  # CLI passthrough: enter/run/exit + response parsing
    modes.py                # box name/id mapping, mode-range read-modify-write, layouts
    profiles.py             # hardware knowledge → settings + generated CLI command lists
    troubleshoot.py         # diagnose(), arming-flag decode, config lint, checks
    safety.py               # gates: confirm, props-removed, armed-guard, auto-backup
    state.py                # in-memory current AircraftProfile + connection singleton
    knowledge/
      arming_flags.json     # arming-prevention bit -> human reason -> fix
      modes_reference.json  # iNAV mode glossary, fixed-wing oriented
      esc_protocols.json    # protocol -> CLI value + notes (DSHOT600/300, MULTISHOT, PWM…)
      fc_targets.json       # optional, sparse: known boards -> hints
  tests/
    test_msp_codec.py       # encode/decode round-trips against known frames
    test_profiles.py        # offline: flying_wing profile -> expected CLI command list
    test_modes.py           # mode-range read-modify-write logic (mock serial)
  examples/
    flying_wing_quickstart.md
```

---

## 6. MSP codec scope (`msp.py`)

Implement a minimal MSP **v1 and v2** encoder/decoder (~150 lines). Only the read-only commands below are required for v1. (If `uNAVlib` is installed, import its enums for decoding; otherwise hardcode the small tables needed and cite the source in comments.)

| MSP command | Purpose in this tool |
|---|---|
| `MSP_API_VERSION`, `MSP_FC_VARIANT`, `MSP_FC_VERSION`, `MSP_BUILD_INFO`, `MSP_BOARD_INFO` | Identify firmware/target → `board_info()` |
| `MSP_STATUS_EX` / `MSP_STATUS` | Cycle time, sensors present, **arming-prevention flags** → troubleshooter |
| `MSP_SENSOR_STATUS` | Per-sensor health (gyro/acc/mag/baro/GPS) |
| `MSP_RC` | Live channel values → `read_rc_channels()` ("wiggle the switch, see which channel moves") |
| `MSP_RAW_GPS` | Fix type, sats, position → troubleshooter |
| `MSP_ANALOG` | Battery V, current, mAh, RSSI |
| `MSP_BOXNAMES` + `MSP_BOXIDS` | Map mode names ↔ permanent IDs at runtime (never hardcode IDs) |
| `MSP_MODE_RANGES` | Read current switch→mode assignments |
| `MSP_ATTITUDE` | Roll/pitch/yaw sanity check |

Mode *writes* go via CLI `aux` (simpler, stable) rather than `MSP_SET_MODE_RANGE`; reads can use either. If you do use `MSP_SET_MODE_RANGE`, remember iNAV requires sending **all** mode-range slots, including disabled ones — never just the one you changed.

---

## 7. MCP tool reference

All tools are `@mcp.tool()` functions. Group by area. "Gate" = safety precondition enforced in code (§10). Writes are **dry-run-by-default**: they return the exact CLI commands and require `confirm=True` to apply.

### Connection & identity
| Tool | Signature | Behavior / Gate |
|---|---|---|
| `list_serial_ports` | `() -> list` | Enumerate available ports (pyserial `list_ports`). |
| `connect` | `(port: str, baud: int = 115200) -> dict` | Open the single serial handle; returns `board_info`. |
| `disconnect` | `() -> dict` | Close handle. |
| `board_info` | `() -> dict` | FC variant, FW version, target, API version, sensors present. Gate: connected. |

### Hardware setup ("input FC, ESC, motor, wing type")
| Tool | Signature | Behavior / Gate |
|---|---|---|
| `define_aircraft` | `(name, wing_type, fc_target=None, esc_protocol, motor_kv=None, motor_poles=14, cells, servo_count=None, notes=None) -> dict` | Stores an `AircraftProfile` in state and **generates** (does not apply) the CLI command list to configure mixer/platform, motor protocol, RPM/poles, battery cells, sane default features. Returns the plan + warnings. No FC required (works offline as a planner). |
| `apply_aircraft_setup` | `(confirm: bool = False, save: bool = False) -> dict` | Applies the generated commands via CLI. **Gate:** connected, not armed, `confirm=True`, auto-backup taken first. If `save`, persists + reboots (warns, marks stale). |
| `get_aircraft_profile` | `() -> dict` | The current declared profile. |

`wing_type ∈ {flying_wing, conventional, vtail, twin_tail, delta}` → maps to iNAV `platform_type`/mixer + servo/`smix` expectations (§8). `esc_protocol` maps via `knowledge/esc_protocols.json`.

### Flight modes & switches ("setting up flight modes easy" / "assigning switch commands")
| Tool | Signature | Behavior / Gate |
|---|---|---|
| `list_flight_modes` | `() -> dict` | All available modes (from BOXNAMES/IDS) + which are assigned, to which aux channel and range. |
| `read_rc_channels` | `() -> dict` | Live channel values (MSP_RC). Core UX: user flips a switch, sees which channel/value changed. |
| `suggest_mode_layout` | `(skill_level, num_switches, has_gps=False) -> dict` | Recommends a fixed-wing layout (e.g. 3-pos: ANGLE/HORIZON/MANUAL; ARM on a switch; RTH on a 2-pos if GPS) and the exact aux ranges. Pure knowledge, no FC needed. |
| `set_flight_mode` | `(mode_name, aux_channel, range_low, range_high, confirm=False) -> dict` | Read-modify-write the mode range via CLI `aux`. Dry-run unless `confirm`. **Gate:** connected, not armed, auto-backup. |
| `assign_switch` | `(switch_channel, switch_positions, mode_per_position: dict, confirm=False) -> dict` | Higher-level helper: maps a 2/3/6-pos switch's detents to modes, computes the ranges, calls `set_flight_mode` for each. Dry-run unless `confirm`. |
| `clear_flight_mode` | `(mode_name, confirm=False) -> dict` | Removes a mode's range(s). Gate as above. |

### Troubleshooter / debugger ("a debugging or troubleshooter as well")
| Tool | Signature | Behavior / Gate |
|---|---|---|
| `diagnose` | `() -> dict` | The flagship. Pulls status/sensors/GPS/analog/RC/failsafe, runs the checks in §9, returns a **prioritized** problem list with concrete fixes. |
| `why_wont_it_arm` | `() -> dict` | Focused decode of arming-prevention flags (the #1 question) → each set flag mapped to a plain reason + fix via `knowledge/arming_flags.json`. |
| `check_config` | `() -> dict` | `diff all` + lint against the declared `AircraftProfile` (e.g. "profile says flying_wing but platform_type is AIRPLANE"; "bidirectional DSHOT on but motor_poles unset"; "no ARM mode assigned"). |
| `read_sensors` | `() -> dict` | Live attitude + sensor health for a quick sanity pass. |
| `get_status` | `() -> dict` | Raw + parsed CLI `status` / `tasks`. |

### Config management (support + safety)
| Tool | Signature | Behavior / Gate |
|---|---|---|
| `backup_config` | `(label=None) -> dict` | `diff all` saved to a timestamped file under `./backups/`. Called automatically before every write. |
| `restore_config` | `(path, confirm=False) -> dict` | Replays a saved diff via CLI. Gate: connected, not armed, confirm. |
| `cli` | `(command: str, confirm_for_writes=False) -> str` | Raw CLI escape hatch. Read-only commands run freely; anything matching a write/`set`/`save`/`defaults`/`mixer`/`motor` pattern requires `confirm_for_writes=True`. |
| `save_and_reboot` | `(confirm=False) -> dict` | `save`. **Gate:** confirm; warns about reboot; marks connection stale. |

### MCP resources (read-only context Claude can pull)
- `inav://modes-reference` → the mode glossary (what each mode does, fixed-wing relevance).
- `inav://current-profile` → the declared aircraft.
- `inav://last-backup` → most recent `diff all`.

### MCP prompts (guided workflows)
- `new_fixed_wing_setup` — walks hardware questions → `define_aircraft` → review → apply.
- `troubleshoot_no_arm` — runs `why_wont_it_arm` then guides fixes.
- `configure_modes` — `read_rc_channels` to ID switches → `suggest_mode_layout` → `assign_switch`.

---

## 8. Hardware knowledge model (`profiles.py` + `knowledge/`)

This is the "input your hardware, get a working config" brain. It's pure data + logic; it runs with no FC attached (so `define_aircraft` doubles as an offline planner).

`AircraftProfile` dataclass: `name, wing_type, fc_target, esc_protocol, motor_kv, motor_poles, cells, servo_count, notes`.

Mapping logic (generates an ordered CLI command list):

- **wing_type → platform/mixer**
  - `flying_wing` → `set platform_type = FLYING_WING`; expect 2 servos as elevons; ensure the mixer/`smix` produces roll+pitch on the elevon outputs; motor on a motor output.
  - `conventional` → `set platform_type = AIRPLANE`; servos for ail/elv/rud per `servo_count`.
  - `vtail`/`twin_tail`/`delta` → appropriate `platform_type` + mixer notes.
  - Always `set model_preview_type` to match (cosmetic but nice).
- **esc_protocol → motor output** via `esc_protocols.json`: `DSHOT600 | DSHOT300 | MULTISHOT | ONESHOT125 | STANDARD(PWM)` → `set motor_pwm_protocol = …`. For DSHOT, optionally enable bidirectional + set `motor_poles` for RPM-based filtering/telemetry.
- **motor_poles** → `set motor_poles = N` (default 14 for typical outrunners) — required for correct RPM telem if bidirectional DSHOT is on.
- **cells → battery** → `set battery_cells` / vbat thresholds appropriate to the chemistry; sensible `vbat_min`/`vbat_warning`.
- **default features** → a conservative baseline (e.g. enable telemetry if relevant, OSD if present) — keep minimal and documented; don't surprise the user.

Every generated command list is returned for review first. Include a human-readable summary ("This will configure a flying wing: elevon mixer, DSHOT600, 4S battery thresholds, motor_poles=14") alongside the raw commands.

Keep `knowledge/*.json` small and honest — better to omit a board than guess its pinout. The user can always fill gaps via `cli`.

---

## 9. Troubleshooter design (`troubleshoot.py`)

`diagnose()` collects, then evaluates rules, then returns `{status, problems:[{severity, title, detail, fix}], raw}` sorted by severity.

Inputs collected: `MSP_STATUS_EX` (arming flags, sensors present), `MSP_SENSOR_STATUS`, `MSP_RAW_GPS`, `MSP_ANALOG`, `MSP_RC`, plus CLI `status` and relevant `get` values (failsafe, arming settings).

Checks (non-exhaustive — implement these and leave the rule list easy to extend):
- **Won't arm** → decode each arming-prevention flag (`arming_flags.json`): e.g. *not level / needs accelerometer calibration / no RX signal / failsafe active / throttle not low / navigation unsafe / system overload / CLI active*. Each → plain reason + fix.
- **No ARM mode assigned** → cross-check mode ranges; tell them to assign ARM to a switch.
- **RC / switch sanity** → if MSP_RC channels are flat or stuck, flag RX link / channel map; surface live values so they can confirm switch detents.
- **GPS** (if profile/has_gps) → fix type + sat count; warn if expecting nav with no 3D fix.
- **Battery** → implausible cell voltage vs declared cells; warn on miscount.
- **Sensor health** → gyro/acc/mag/baro present-but-unhealthy.
- **Config lint** (`check_config`) → profile vs actual `diff all` mismatches (platform type, poles, protocol, missing ARM).

Output must be actionable: every problem carries a `fix` string a beginner can follow.

---

## 10. Safety rules (`safety.py`) — non-negotiable, enforced in code

1. **Props-off gate.** Any tool that can spin a motor (`cli` motor commands, any motor test) requires an explicit `props_removed=True` argument *and* refuses if the board reports armed.
2. **Armed guard.** All writes refuse if `MSP_STATUS_EX` reports armed.
3. **Auto-backup before write.** Every write tool calls `backup_config()` first and includes the backup path in its result.
4. **Dry-run by default.** Writes return the exact commands and require `confirm=True` to apply.
5. **Read-back verify.** After applying, re-read the affected settings (`get`/`diff`) and report what actually changed; flag mismatches.
6. **`save` = reboot.** `save_and_reboot` and any `save` require confirm, warn clearly, and mark the connection stale (force reconnect).
7. **Mode-range completeness.** If writing mode ranges via MSP, send all slots; prefer CLI `aux` to avoid the footgun.
8. **No receiver-mode changes.** Never switch the FC to MSP-RX; config doesn't need it.
9. **Connection state.** Refuse FC operations when not connected; give a clear message.

---

## 11. Build milestones (Claude Code: do in order, stop & test after each)

- **M0 — Scaffold + connect.** Repo, `pyproject.toml`, FastMCP app, `list_serial_ports`/`connect`/`board_info`. MSP codec for the identity commands. *Test:* connect to the real FC, get correct variant/version/target.
- **M1 — CLI layer + backups.** `connection.py` mode switching, `cli.py` enter/run/exit, `backup_config`, `save_and_reboot`, raw `cli`. *Test:* `diff all` round-trips; backup file written; `exit` returns to MSP cleanly.
- **M2 — Read-only diagnostics.** `read_rc_channels`, `read_sensors`, `get_status`, `list_flight_modes`, `why_wont_it_arm`, `diagnose` (read-only first). *Test:* unplug RX → `why_wont_it_arm` says "no RX"; wiggle a switch → channel moves in `read_rc_channels`.
- **M3 — Hardware setup.** `profiles.py` + `knowledge/`, `define_aircraft` (offline dry-run), `apply_aircraft_setup`, `check_config`. *Test:* define a flying wing offline → inspect generated CLI; apply on bench → read-back matches.
- **M4 — Modes & switches.** `suggest_mode_layout`, `set_flight_mode`, `assign_switch`, `clear_flight_mode`. *Test:* assign ANGLE/HORIZON/MANUAL to a 3-pos switch → verify in `list_flight_modes` and in iNAV Configurator.
- **M5 — Resources, prompts, polish, tests.** MCP resources + prompts, README, the `tests/` suite (codec round-trips + offline profile generation + mock-serial mode logic). *Test:* `pytest` green; prompts run end-to-end.

**Acceptance checklist**
- [ ] Connects and identifies an iNAV FC over USB.
- [ ] `diagnose`/`why_wont_it_arm` give correct, actionable output for a deliberately broken setup.
- [ ] `define_aircraft` for a flying wing generates correct, reviewable CLI; applying it produces a flyable mixer/protocol/battery config verified by read-back.
- [ ] A 3-position switch can be mapped to three flight modes in one `assign_switch` call.
- [ ] Every write auto-backs-up, runs dry by default, and verifies after apply.
- [ ] Motor/`save` paths are gated (props-removed / confirm / reboot warning).
- [ ] Registered with Claude Code/Desktop; documented start command.

---

## 12. Known risks & decisions (read before building)

- **CLI enter/exit handshake** is the one empirically-fragile bit — confirm `#`+newline and prompt detection against the actual FC in M1 before building on it.
- **`mcp` SDK drift.** Confirm the installed entrypoint (`from mcp.server.fastmcp import FastMCP`, `mcp.run()` stdio). The standalone `fastmcp` package (`pip install fastmcp`, `from fastmcp import FastMCP`) is an alternative if the official one gives trouble — same decorator model.
- **uNAVlib is optional and fast-moving** (v0.1.x, "active development, no back-compat guarantees," GPL-3.0). Use it only for enum/MSP-code reference, behind a thin import that the rest of the code doesn't depend on. Don't put it on the critical path.
- **Licensing.** If you `import unavlib`/`yamspy` in distributed code, that's **GPL-3.0** copyleft — license the repo GPL-3.0 too. If you want to keep it permissive (MIT), don't import them at runtime: write the small MSP codec yourself (the recommended path) and at most use uNAVlib offline to *generate* enum tables. For personal-only use, either is fine.
- **iNAV version target.** Develop against current iNAV (8.x); the CLI-first approach is the most version-robust. Note the firmware version in `board_info` and let `check_config` warn on surprises.
- **MSP RX is intentionally untouched** — this is a setup/config/diagnostic tool, not a flight-control link.

---

## 13. References
- MCP Python SDK / FastMCP: github.com/modelcontextprotocol/python-sdk · gofastmcp.com
- iNAV MSP v2 + message reference: iNAV wiki "MSP V2"; `docs/development/msp/msp_ref.md`; `docs/API/MSP_extensions.md` (mode/adjustment ranges)
- uNAVlib (optional enum/MSP source, GPL-3.0): github.com/xznhj8129/uNAVlib
- YAMSPy (origin of the MSP Python lineage, GPL-3.0): github.com/thecognifly/YAMSPy
- Existing Betaflight MCP server (different firmware, useful as a shape reference): github.com/jir13/MCP
