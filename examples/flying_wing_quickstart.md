# Flying-wing quickstart

An end-to-end walkthrough: take a bare iNAV flying wing from "just flashed" to
"ready to maiden," driving the MCP conversationally through Claude. The running
example is a **4S flying wing on DSHOT600**.

> ⚠️ **Two things to internalise first**
> 1. **Props off the aircraft** for the whole bench session — `test_motor` spins a
>    real motor and refuses to run without `props_removed=True`.
> 2. **Every config write saves to EEPROM and reboots the FC** (iNAV reality — the
>    CLI discards on `exit` and only persists on `save`). The tools handle the
>    reboot + reconnect (~7 s) for you, but expect a brief pause after each apply.

Every write is **dry-run by default**: you'll see the exact CLI commands first, and
nothing changes until you confirm.

---

## 0. Prerequisites

- FC connected over USB, its serial driver installed (CP210x / CH340 / native VCP).
- The server registered with Claude (see the README "Register with Claude" section).

## Two ways to drive it

- **Guided prompts** (easiest): ask Claude to run `new_fixed_wing_setup`,
  `configure_modes`, or `troubleshoot_no_arm` and follow along.
- **Conversational** (this guide): just say what you want. The tool calls shown
  below are what Claude runs under the hood.

---

## 1. Find and connect

> "Find my flight controller and connect to it."

```
find_fc()                 → detects the port (e.g. COM4) without you guessing
connect("COM4")           → opens the link, returns board identity
```

`connect` returns the variant, firmware, target, and sensors. If your firmware is
outside the arming-flag table's calibrated range (iNAV 8.x/9.x), the result carries
a `firmware_warning` — decoding still works, but flag *names* may be approximate.

## 2. Describe the aircraft (offline plan — nothing written yet)

> "Set this up as a 4S flying wing on DSHOT600."

```
define_aircraft(name="My Wing", wing_type="flying_wing",
                esc_protocol="DSHOT600", cells=4)
```

This stores a profile and **generates** the CLI commands (elevon mixer / platform
type, motor protocol, battery thresholds, sane defaults) plus warnings. Review them.

## 3. Apply and verify

> "Looks good — apply it."

```
apply_aircraft_setup(confirm=True)   # backs up → applies → saves+reboots → reconnects
check_config()                       # diff all + lint against the profile + ARM check
```

`apply_aircraft_setup` auto-backs-up first and reads back the diff to confirm what
actually changed. `check_config` flags mismatches (e.g. platform type) and whether
ARM is assigned yet.

## 4. Calibrate the accelerometer

Set the board **level** in its normal flight orientation and keep it still.

> "Calibrate the accelerometer."

```
calibrate_accelerometer(confirm=True)   # samples ~2 s, saves automatically
```

This clears most "not level" / "accel not calibrated" arming blocks.

## 5. Flight modes and switches

Identify which channel each switch is on by flipping it:

> "I'll flip my mode switch — which channel moved?"

```
read_rc_channels()         # flip switch, call again, watch the value change
suggest_mode_layout(skill_level="beginner", num_switches=2, has_gps=False)
```

Then map a switch in one call (example: a 3-position switch = ANGLE / HORIZON / MANUAL):

```
assign_switch(switch_channel=6, switch_positions=3,
              mode_per_position={"low": "ANGLE", "mid": "HORIZON", "high": "MANUAL"},
              confirm=True)
list_flight_modes()        # confirm assignments; check arm_assigned is True
```

Put **ARM on its own dedicated 2-position switch** — `assign_switch` / `set_flight_mode`.

## 6. Failsafe (do not skip)

> "What happens on RC loss?"

```
check_failsafe()           # explains the procedure, flags risky setups
```

For a wing without GPS, a safe choice is a controlled descent rather than RTH:

```
set_failsafe(procedure="LAND", throttle_us=1300, confirm=True)
```

(`RTH` needs a working GPS + home fix — `check_failsafe` warns if you pick it without one.)

## 7. Bench-test the motor — PROPS OFF

> "Props are off. Spin motor 1 gently for a second."

```
test_motor(motor=1, throttle_us=1100, duration_s=1.0,
           props_removed=True, confirm=True)
```

It refuses without `props_removed=True`, refuses while armed, clamps the values, and
always commands the motor back to stop. (There's no `test_servo` — iNAV has no live
servo override; check control surfaces by moving the TX sticks and watching
`read_rc_channels()`, or use the Configurator Servos tab.)

## 8. Pre-flight sanity

> "Why won't it arm?" / "Run a full check."

```
why_wont_it_arm()    # decodes each arming-prevention flag → plain reason + fix
diagnose()           # full sweep: arming, sensors, RC, battery, GPS → prioritized fixes
```

## 9. Back up the working config

```
backup_config(label="maiden-ready")
list_backups()       # see all backups, newest first
```

If anything ever breaks, roll back:

```
restore_config("backups/backup_YYYYMMDD_HHMMSS_maiden-ready.txt", confirm=True)
```

---

## Optional: GPS and navigation

```
configure_gps(provider="UBLOX", sbas="AUTO", confirm=True)
read_gps()                                   # wait for a 3D fix, 6+ sats
set_nav(rth_altitude_m=80, rth_climb_first=True,
        rth_allow_landing="FS_ONLY", loiter_radius_m=75, confirm=True)
```

With a working GPS you can then switch failsafe to RTH and add `NAV RTH` to a switch.

## Optional: tuning

```
read_tuning()                                # current fixed-wing PIDs / rates / filters
set_pid("pitch", p=22, i=15, ff=70, confirm=True)   # change gradually, test carefully
```

---

## Good habits

- **Dry-run first.** Call any write without `confirm=True` to see the exact commands.
- **One change at a time** on the bench, then re-verify — every write reboots the FC.
- **Keep a backup** before big changes; `backup_config` runs automatically before each
  write, and `list_backups` / `restore_config` get you back.
- **Maiden conservatively:** props off for all bench work, low rates/throws first flight,
  and confirm failsafe behaves before you rely on it.
