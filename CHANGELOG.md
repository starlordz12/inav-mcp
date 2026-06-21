# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [0.2.0] - 2026-06-20

Feature, safety, and tooling expansion. Tools grew **22 → 34**; tests **134 → 168**
(all offline, no FC required).

### Added
- **Navigation / GPS:** `read_gps` (live fix/sats/position + nav-readiness),
  `configure_gps` (enable GPS feature + provider/SBAS), `set_nav` (RTH altitude /
  climb-first / landing / loiter radius).
- **Tuning:** `read_tuning` (fixed-wing PIDs, rates, filters), `set_pid` (per-axis
  P/I/D/FF write).
- **Bench tests & calibration:** `test_motor` (gated live single-motor test),
  `calibrate_accelerometer`, `calibrate_magnetometer`.
- **Failsafe:** `check_failsafe` (read + explain RC-loss behaviour, flag risky setups),
  `set_failsafe` (procedure / throttle / delays).
- **Connection / config:** `find_fc` (auto-detect the FC's serial port via MSP probe),
  `list_backups` (enumerate saved config backups).
- **Firmware-version guard** on arming-flag decoding — warns when the connected
  firmware is outside the calibrated 8.x/9.x range (bit positions shift between
  majors), surfaced in `connect` / `board_info` / `why_wont_it_arm` / `diagnose`.
- **README tool-reference generator** (`tools/gen_readme_tools.py`) plus drift-guard
  tests that fail if the docs and the registered tools diverge.
- **`examples/flying_wing_quickstart.md`** — end-to-end walkthrough.
- **Continuous integration:** GitHub Actions running the offline suite on Python
  3.10 / 3.11 / 3.12, README status badges, and `.gitattributes` (LF normalization).

### Changed
- Test suite grown from 134 to 168 tests.

### Fixed
- README documented a `save=` parameter on `apply_aircraft_setup` that no longer exists.

### Security
- **Props-off gate is now enforced in code** (previously documentation-only): a live
  `motor` command via `cli()` and the new `test_motor()` require `props_removed=True`,
  refuse while the board is armed, and are never saved.
- The armed-guard now applies to **all** raw `cli()` writes, not just actuator commands.

### Notes
- No `test_servo`: iNAV has no live servo-output override that doesn't require MSP-RX
  mode (which this tool intentionally never touches). Verify control surfaces with the
  TX sticks + `read_rc_channels()` instead.

## [0.1.0] - 2026-06-20

Initial release — milestones M0–M5, hardware-verified against a SpeedyBee F405 Wing.

### Added
- iNAV fixed-wing flight-controller MCP server with **22 tools** across connection &
  identity, hardware setup, flight modes & switches, diagnostics, and config management.
- Single serial handle with MSP + CLI mode switching; CLI-first writes with atomic
  backup → apply → save/rollback, and read-back verification.
- Knowledge base (arming flags, mode glossary, ESC protocols, FC targets); MCP
  resources and guided prompts.
- Safety model: dry-run + confirm on every write, auto-backup, armed-guard, and a
  built-in MSP v1/v2 codec (no GPL runtime dependencies — MIT licensed).
- 134 offline tests.

[Unreleased]: https://github.com/starlordz12/inav-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/starlordz12/inav-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/starlordz12/inav-mcp/releases/tag/v0.1.0
