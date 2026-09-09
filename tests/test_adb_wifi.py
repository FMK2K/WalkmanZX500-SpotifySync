"""
test_adb_wifi.py — verifies the ADB-over-WiFi functions added 2026-07-30
(enable_wifi_debugging, connect_wifi, disconnect_wifi,
reconnect_wifi_if_configured in adb_transfer.py, plus the fallback wiring
in cli_runner.py's _attempt_adb_reconnect_if_needed).

No device required — adb_transfer._adb_cmd is mocked throughout, dispatching
canned output based on the argv it's called with (mirrors the real adb
command shapes: `adb tcpip <port>`, `adb shell ip addr show wlan0`,
`adb connect host:port`, `adb devices`, `adb disconnect [...]`).
"""
from unittest import mock

import adb_transfer as adb

passed = 0
failed = 0


def _check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


def _adb_cmd_stub(devices_line="192.168.68.50:5555\tdevice",
                   wlan0_out="inet 192.168.68.50/24 brd 192.168.68.255 scope global wlan0",
                   route_out=""):
    def _stub(args, timeout=None, input_data=None, check=False):
        if args[0] == "devices":
            return f"List of devices attached\n{devices_line}"
        if args[:1] == ["tcpip"]:
            return "restarting in TCP mode port: 5555"
        if args[:2] == ["shell", "ip"] and "wlan0" in args:
            return wlan0_out
        if args[:2] == ["shell", "ip"] and "route" in args:
            return route_out
        if args[0] == "connect":
            return f"connected to {args[1]}"
        if args[0] == "disconnect":
            return "disconnected"
        return ""
    return _stub


# ── enable_wifi_debugging ────────────────────────────────────────────────
print("[1] enable_wifi_debugging — not connected via USB")
with mock.patch.object(adb, "device_is_connected", return_value=False):
    r = adb.enable_wifi_debugging()
    _check("returns None", r is None)

print("\n[2] enable_wifi_debugging — connected, wlan0 has IP")
with mock.patch.object(adb, "device_is_connected", return_value=True), \
     mock.patch.object(adb, "_adb_cmd", side_effect=_adb_cmd_stub()), \
     mock.patch("time.sleep"):
    r = adb.enable_wifi_debugging(port=5555)
    _check("returns the wlan0 IP", r == "192.168.68.50")

print("\n[3] enable_wifi_debugging — wlan0 missing, falls back to ip route")
with mock.patch.object(adb, "device_is_connected", return_value=True), \
     mock.patch.object(adb, "_adb_cmd",
                        side_effect=_adb_cmd_stub(wlan0_out="",
                                                   route_out="1.0.0.0/0 via 192.168.68.1 dev wlan0 src 192.168.68.77")), \
     mock.patch("time.sleep"):
    r = adb.enable_wifi_debugging(port=5555)
    _check("returns the route-derived IP", r == "192.168.68.77")

print("\n[4] enable_wifi_debugging — no IP found anywhere")
with mock.patch.object(adb, "device_is_connected", return_value=True), \
     mock.patch.object(adb, "_adb_cmd", side_effect=_adb_cmd_stub(wlan0_out="", route_out="")), \
     mock.patch("time.sleep"):
    r = adb.enable_wifi_debugging(port=5555)
    _check("returns None", r is None)

# ── connect_wifi ──────────────────────────────────────────────────────────
print("\n[5] connect_wifi — success, pins ADB_SERIAL")
adb.ADB_SERIAL = None
with mock.patch.object(adb, "_adb_cmd",
                        side_effect=_adb_cmd_stub(devices_line="192.168.68.50:5555\tdevice")), \
     mock.patch.object(adb, "reset_storage_cache"), \
     mock.patch("time.sleep"):
    r = adb.connect_wifi("192.168.68.50", 5555)
    _check("returns True", r is True)
    _check("ADB_SERIAL pinned to host:port", adb.ADB_SERIAL == "192.168.68.50:5555")
adb.ADB_SERIAL = None

print("\n[6] connect_wifi — device stays offline")
with mock.patch.object(adb, "_adb_cmd",
                        side_effect=_adb_cmd_stub(devices_line="192.168.68.50:5555\toffline")), \
     mock.patch.object(adb, "reset_storage_cache"), \
     mock.patch("time.sleep"):
    r = adb.connect_wifi("192.168.68.50", 5555)
    _check("returns False", r is False)
    _check("ADB_SERIAL left unset", adb.ADB_SERIAL is None)

print("\n[7] connect_wifi — target never appears in `adb devices`")
with mock.patch.object(adb, "_adb_cmd",
                        side_effect=_adb_cmd_stub(devices_line="001045c585010020\tdevice")), \
     mock.patch.object(adb, "reset_storage_cache"), \
     mock.patch("time.sleep"):
    r = adb.connect_wifi("192.168.68.50", 5555)
    _check("returns False", r is False)

# ── disconnect_wifi ───────────────────────────────────────────────────────
print("\n[8] disconnect_wifi — un-pins a WiFi (host:port) ADB_SERIAL")
adb.ADB_SERIAL = "192.168.68.50:5555"
with mock.patch.object(adb, "_adb_cmd", return_value=""), \
     mock.patch.object(adb, "reset_storage_cache"):
    r = adb.disconnect_wifi("192.168.68.50", 5555)
    _check("returns True", r is True)
    _check("ADB_SERIAL cleared", adb.ADB_SERIAL is None)

print("\n[9] disconnect_wifi — leaves a non-WiFi ADB_SERIAL (plain USB serial) untouched")
adb.ADB_SERIAL = "001045c585010020"
with mock.patch.object(adb, "_adb_cmd", return_value=""), \
     mock.patch.object(adb, "reset_storage_cache"):
    adb.disconnect_wifi()
    _check("USB serial unchanged", adb.ADB_SERIAL == "001045c585010020")
adb.ADB_SERIAL = None

# ── reconnect_wifi_if_configured ─────────────────────────────────────────
print("\n[10] reconnect_wifi_if_configured — ADB_WIFI_ENABLED False is a no-op")
import config
with mock.patch.object(config, "ADB_WIFI_ENABLED", False), \
     mock.patch.object(adb, "connect_wifi") as m_connect:
    r = adb.reconnect_wifi_if_configured()
    _check("returns False", r is False)
    _check("connect_wifi never called", m_connect.call_count == 0)

print("\n[11] reconnect_wifi_if_configured — enabled but no saved host is a no-op")
with mock.patch.object(config, "ADB_WIFI_ENABLED", True), \
     mock.patch.object(config, "ADB_WIFI_HOST", None), \
     mock.patch.object(adb, "connect_wifi") as m_connect:
    r = adb.reconnect_wifi_if_configured()
    _check("returns False", r is False)
    _check("connect_wifi never called", m_connect.call_count == 0)

print("\n[12] reconnect_wifi_if_configured — enabled + saved host tries connect_wifi")
with mock.patch.object(config, "ADB_WIFI_ENABLED", True), \
     mock.patch.object(config, "ADB_WIFI_HOST", "192.168.68.50"), \
     mock.patch.object(config, "ADB_WIFI_PORT", 5555), \
     mock.patch.object(adb, "connect_wifi", return_value=True) as m_connect:
    r = adb.reconnect_wifi_if_configured()
    _check("returns True", r is True)
    _check("connect_wifi called with saved host/port",
           m_connect.call_args == mock.call("192.168.68.50", 5555))

# ── cli_runner.py integration ────────────────────────────────────────────
print("\n[13] _attempt_adb_reconnect_if_needed — falls back to WiFi when USB reconnect() fails")
import cli_runner
import mtp_transfer as mtp
with mock.patch.object(mtp, "ADB_AVAILABLE", False), \
     mock.patch.object(mtp, "refresh_adb_availability") as m_refresh, \
     mock.patch.object(adb, "reconnect", return_value=False), \
     mock.patch.object(adb, "reconnect_wifi_if_configured", return_value=True):
    cli_runner._attempt_adb_reconnect_if_needed()
    _check("refresh_adb_availability called after WiFi fallback succeeds", m_refresh.call_count == 1)

print("\n[14] _attempt_adb_reconnect_if_needed — both USB and WiFi fail, no refresh")
with mock.patch.object(mtp, "ADB_AVAILABLE", False), \
     mock.patch.object(mtp, "refresh_adb_availability") as m_refresh, \
     mock.patch.object(adb, "reconnect", return_value=False), \
     mock.patch.object(adb, "reconnect_wifi_if_configured", return_value=False):
    cli_runner._attempt_adb_reconnect_if_needed()
    _check("refresh_adb_availability not called", m_refresh.call_count == 0)

print("\n[15] _attempt_adb_reconnect_if_needed — USB reconnect() succeeding skips WiFi fallback")
with mock.patch.object(mtp, "ADB_AVAILABLE", False), \
     mock.patch.object(mtp, "refresh_adb_availability") as m_refresh, \
     mock.patch.object(adb, "reconnect", return_value=True), \
     mock.patch.object(adb, "reconnect_wifi_if_configured") as m_wifi:
    cli_runner._attempt_adb_reconnect_if_needed()
    _check("refresh_adb_availability called", m_refresh.call_count == 1)
    _check("WiFi fallback not attempted (USB already succeeded)", m_wifi.call_count == 0)

print(f"\n{'=' * 60}")
print(f"ADB-over-WiFi: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
