"""Tests for the second batch of follow-ups: armed-guard on raw cli() writes,
set_failsafe delay knobs, find_fc auto-detect, and a README↔tools drift guard.
All offline.
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inav_mcp import state, server


def _run(coro):
    return asyncio.run(coro)


# ── armed-guard on raw cli() writes (spec §10.2) ────────────────────────────────

class FakeWriteConn:
    def __init__(self):
        self.entered = False
        self.exit_saved = None
    def is_open(self):
        return True
    def enter_cli(self, timeout=5.0):
        self.entered = True
        return ""
    def run_cli(self, cmd, timeout=15.0):
        return "ok"
    def exit_cli(self, save=False, reconnect=False):
        self.exit_saved = save


def test_cli_write_refused_while_armed(monkeypatch=None):
    conn = FakeWriteConn()
    # Force the armed check to report ARMED.
    def fake_armed(_conn):
        raise RuntimeError("FC is ARMED (ARMED bit set in armingFlags).")
    orig = server.check_not_armed
    server.check_not_armed = fake_armed
    state.set_connection(conn)
    try:
        out = server.cli("set foo = 1", confirm_for_writes=True)
    finally:
        server.check_not_armed = orig
        state.set_connection(None)
    assert "SAFETY" in out and "ARMED" in out
    assert conn.entered is False, "must refuse before entering CLI"


def test_cli_write_proceeds_when_disarmed():
    conn = FakeWriteConn()
    server_orig = server.check_not_armed
    server.check_not_armed = lambda _c: None   # disarmed
    state.set_connection(conn)
    try:
        out = server.cli("set foo = 1", confirm_for_writes=True)
    finally:
        server.check_not_armed = server_orig
        state.set_connection(None)
    assert conn.entered is True
    assert conn.exit_saved is True
    assert "SAVED" in out


# ── set_failsafe delay knobs ────────────────────────────────────────────────────

class _ConnStub:
    def is_open(self):
        return True


def test_set_failsafe_builds_delay_commands_in_deciseconds():
    state.set_connection(_ConnStub())
    try:
        out = server.set_failsafe(procedure="DROP", delay_s=0.5, off_delay_s=20, confirm=False)
    finally:
        state.set_connection(None)
    assert out["dry_run"] is True
    cmds = out["commands"]
    assert "set failsafe_procedure = DROP" in cmds
    assert "set failsafe_delay = 5" in cmds        # 0.5 s → 5 ds
    assert "set failsafe_off_delay = 200" in cmds  # 20 s → 200 ds


def test_set_failsafe_nothing_to_set():
    state.set_connection(_ConnStub())
    try:
        out = server.set_failsafe(confirm=False)
    finally:
        state.set_connection(None)
    assert "error" in out


# ── find_fc auto-detect (monkeypatched probe) ───────────────────────────────────

def test_find_fc_selects_the_fc_port():
    ports = [
        {"device": "COM1", "description": "Communications Port", "hwid": "ACPI"},
        {"device": "COM4", "description": "USB Serial (STM32)", "hwid": "USB VID:PID=0483"},
    ]
    orig_list, orig_probe = server._list_port_devices, server._probe_fc_port
    server._list_port_devices = lambda: ports
    server._probe_fc_port = lambda dev, baud=115200: (
        {"port": dev, "variant": "INAV", "fw_version": "6.1.0"} if dev == "COM4" else None
    )
    state.set_connection(None)
    try:
        out = server.find_fc()
    finally:
        server._list_port_devices = orig_list
        server._probe_fc_port = orig_probe
    assert out["count"] == 1
    assert out["found"][0]["port"] == "COM4"
    assert "COM1" in out["skipped_non_serial"]   # not USB-serial-looking → not probed


def test_find_fc_probe_all_probes_everything():
    ports = [{"device": "COM1", "description": "x", "hwid": "y"}]
    orig_list, orig_probe = server._list_port_devices, server._probe_fc_port
    server._list_port_devices = lambda: ports
    server._probe_fc_port = lambda dev, baud=115200: None
    state.set_connection(None)
    try:
        out = server.find_fc(probe_all=True)
    finally:
        server._list_port_devices = orig_list
        server._probe_fc_port = orig_probe
    assert out["count"] == 0
    assert out["probed"] == ["COM1"]


# ── README ↔ registered tools drift guard (the root-cause fix) ──────────────────

def test_readme_lists_every_registered_tool():
    readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
    with open(readme_path, encoding="utf-8") as f:
        readme = f.read()
    tools = _run(server.mcp.list_tools())
    missing = [t.name for t in tools if t.name not in readme]
    assert not missing, f"README.md does not document these tools: {missing}"


def test_readme_tool_count_matches():
    readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
    with open(readme_path, encoding="utf-8") as f:
        readme = f.read()
    tools = _run(server.mcp.list_tools())
    assert f"## Tools ({len(tools)})" in readme, \
        f"README '## Tools (N)' header is out of sync — should be {len(tools)}"


def test_readme_autogen_block_current():
    """The auto-generated tool reference block must match the registered tools."""
    from tools.gen_readme_tools import generate_block, README
    with open(README, encoding="utf-8") as f:
        readme = f.read()
    assert generate_block() in readme, (
        "README AUTOGEN tool block is stale — run: python -m tools.gen_readme_tools"
    )


# ── navigation / tuning command generation (dry-run) ────────────────────────────

def test_set_nav_builds_commands_with_unit_conversion():
    state.set_connection(_ConnStub())
    try:
        out = server.set_nav(rth_altitude_m=80, rth_climb_first=True,
                             rth_allow_landing="FS_ONLY", loiter_radius_m=75, confirm=False)
    finally:
        state.set_connection(None)
    cmds = out["commands"]
    assert "set nav_rth_altitude = 8000" in cmds        # 80 m → 8000 cm
    assert "set nav_rth_climb_first = ON" in cmds
    assert "set nav_rth_allow_landing = FS_ONLY" in cmds
    assert "set nav_fw_loiter_radius = 7500" in cmds     # 75 m → 7500 cm


def test_configure_gps_dry_run():
    state.set_connection(_ConnStub())
    try:
        out = server.configure_gps(provider="ublox", sbas="auto", confirm=False)
    finally:
        state.set_connection(None)
    assert out["dry_run"] is True
    assert "feature GPS" in out["commands"]
    assert "set gps_provider = UBLOX" in out["commands"]
    assert "set gps_sbas_mode = AUTO" in out["commands"]


def test_set_pid_builds_axis_commands():
    state.set_connection(_ConnStub())
    try:
        out = server.set_pid("pitch", p=22, i=15, ff=70, confirm=False)
    finally:
        state.set_connection(None)
    cmds = out["commands"]
    assert "set fw_p_pitch = 22" in cmds
    assert "set fw_i_pitch = 15" in cmds
    assert "set fw_ff_pitch = 70" in cmds
    assert all("fw_d_pitch" not in c for c in cmds)   # d not provided → not set


def test_set_pid_rejects_bad_axis():
    state.set_connection(_ConnStub())
    try:
        out = server.set_pid("vertical", p=10, confirm=False)
    finally:
        state.set_connection(None)
    assert "error" in out


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback; print(f"  FAIL  {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
