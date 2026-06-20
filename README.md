# iNAV MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets Claude configure,
diagnose, and troubleshoot an **iNAV fixed-wing flight controller** over USB.

It talks to the FC through a single serial connection, using the iNAV **CLI**
for configuration writes and a small built-in **MSP** codec for live/binary
reads. Every write is dry-run by default, auto-backs-up first, refuses while the
board is armed, and reads back to verify.

> ⚠️ **Safety:** Always remove props from the aircraft before any motor test.
> This tool never switches the FC into MSP-RX mode and never arms the aircraft.

---

## Requirements

- Python 3.10+
- An iNAV flight controller (developed against iNAV 8.x/9.x) connected over USB
- The serial port the FC enumerates as (e.g. `COM3` on Windows, `/dev/ttyACM0` on Linux)

## Install

```bash
# from the repo root
python -m venv .venv
.venv/Scripts/python -m pip install -e .          # Windows
# source .venv/bin/activate && pip install -e .    # Linux/macOS
```

For development (tests):

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
```

## Register with Claude

Add the server to your Claude (Code or Desktop) MCP config. Example
(`~/.claude/settings.json` or the Claude Desktop config):

```json
{
  "mcpServers": {
    "inav": {
      "command": "C:\\dev\\inav-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "inav_mcp.server"],
      "cwd": "C:\\dev\\inav-mcp"
    }
  }
}
```

The server speaks MCP over **stdio**. You can also run it directly with the
installed entry point `inav-mcp`.

---

## Typical workflows

The server ships **prompts** that walk Claude through the common jobs — just pick one:

- **`new_fixed_wing_setup`** — gather hardware details → `define_aircraft` → review → apply.
- **`troubleshoot_no_arm`** — decode arming-prevention flags → guided fixes.
- **`configure_modes`** — identify switches from live RC → suggest a layout → assign modes.

Or drive it conversationally, e.g.:

> "Connect to my FC on COM3 and tell me why it won't arm."

> "Set up a 4S flying wing on DSHOT600, then show me the commands before applying."

---

## Tools (22)

### Connection & identity
| Tool | What it does |
|---|---|
| `list_serial_ports()` | List available serial ports. |
| `connect(port, baud=115200)` | Open the FC connection, return board identity. |
| `disconnect()` | Close the connection. |
| `board_info()` | FC variant, firmware version, target, API version, sensors. |

### Hardware setup
| Tool | What it does |
|---|---|
| `define_aircraft(name, wing_type, esc_protocol, cells, …)` | Offline planner — stores a profile and generates the CLI config plan. |
| `get_aircraft_profile()` | The current declared profile + plan. |
| `apply_aircraft_setup(confirm=False, save=False)` | Apply the plan (gated: not armed, auto-backup, read-back verify). |
| `check_config()` | `diff all` + lint against the declared profile + ARM check. |

### Flight modes & switches
| Tool | What it does |
|---|---|
| `suggest_mode_layout(skill_level, num_switches, has_gps=False)` | Recommend a fixed-wing switch/mode layout (offline). |
| `set_flight_mode(mode_name, aux_channel, range_low, range_high, confirm=False)` | Assign one mode to an aux range. |
| `assign_switch(switch_channel, switch_positions, mode_per_position, confirm=False)` | Map a whole 2/3/6-pos switch in one call. |
| `clear_flight_mode(mode_name, confirm=False)` | Remove a mode's switch assignments. |

### Diagnostics
| Tool | What it does |
|---|---|
| `diagnose()` | Full sweep: arming, sensors, RC, battery, GPS → prioritized fixes. |
| `why_wont_it_arm()` | Decode arming-prevention flags into plain reasons + fixes. |
| `read_rc_channels()` | Live RC channel values — flip a switch, see which channel moves. |
| `read_sensors()` | Live attitude, per-sensor health, battery. |
| `get_status()` | MSP status + CLI `status`/`tasks`. |
| `list_flight_modes()` | All modes and their current switch assignments. |

### Config management
| Tool | What it does |
|---|---|
| `backup_config(label=None)` | Save `diff all` to a timestamped file under `./backups/`. |
| `restore_config(path, confirm=False)` | Replay a saved backup via CLI. |
| `cli(command, confirm_for_writes=False)` | Raw CLI escape hatch (writes need the confirm flag). |
| `save_and_reboot(confirm=False)` | `save` to EEPROM and reboot (marks the connection stale). |

## Resources

- `inav://modes-reference` — iNAV mode glossary with fixed-wing relevance.
- `inav://current-profile` — the declared aircraft profile + generated plan.
- `inav://last-backup` — the most recent `diff all` backup.

---

## Safety model

1. **Props-off gate** for anything that can spin a motor.
2. **Armed guard** — all writes refuse if the FC reports armed.
3. **Auto-backup** before every write; the backup path is returned.
4. **Dry-run by default** — writes return the exact commands; `confirm=True` applies.
5. **Read-back verify** — after applying, settings are re-read and mismatches flagged.
6. **`save` = reboot** — `save_and_reboot` warns and marks the connection stale.
7. **No receiver-mode changes** — the FC is never switched to MSP-RX.

## How it works

- **Single serial handle** shared between MSP and CLI modes (`connection.py`),
  tracked by a `mode` state machine. Never two handles on one port.
- **CLI-first writes** — the CLI is stable across firmware versions; MSP command
  IDs can drift. A thin MSP v1/v2 codec (`msp.py`) handles only the live binary
  reads (status, RC, attitude, analog, GPS, sensor health, mode ranges, box maps).
- **Box IDs are resolved at runtime** via `MSP_BOXNAMES` + `MSP_BOXIDS` — never hardcoded.
- **Arming flags** are decoded from `knowledge/arming_flags.json`, calibrated to
  iNAV 9.x bit positions.

## Development

```bash
.venv/Scripts/python -m pytest          # 111 tests, all offline (no FC needed)
```

The suite covers the MSP codec round-trips, CLI response parsing, the diagnostic
rule engine, offline profile/command generation, mode-range read-modify-write
logic (against a mock connection), and resource/prompt registration.

Project layout:

```
inav_mcp/
  server.py          # FastMCP app: all tools, resources, prompts
  connection.py      # single serial handle, MSP + CLI mode switching
  msp.py             # MSP v1/v2 codec + parsers
  cli.py             # CLI response parsing, write-command detection
  modes.py           # box maps, mode-range read/write, layout planner
  profiles.py        # AircraftProfile + offline CLI command generator
  troubleshoot.py    # diagnose() rule engine + arming-flag decode
  safety.py          # armed guard, backup paths
  state.py           # connection + profile singletons
  knowledge/         # arming_flags / modes_reference / esc_protocols / fc_targets (JSON)
tests/               # offline pytest suite
```

## License

MIT. This project ships its own MSP codec and does **not** import GPL libraries
(uNAVlib / YAMSPy) at runtime, keeping it permissively licensed.
