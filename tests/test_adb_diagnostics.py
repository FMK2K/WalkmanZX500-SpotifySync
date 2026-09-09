"""
test_adb_diagnostics.py — verifies the ADB connection diagnostics added
2026-07-30 (get_device_state, reset_adb_pairing, diagnose_windows_driver in
adb_transfer.py), codifying a live ADB troubleshooting session on this
project's own ZX507 (see progress.md "Session: 2026-07-30").

No device required — adb_transfer._adb_cmd and the PowerShell subprocess
call are mocked throughout.
"""
from unittest import mock
from pathlib import Path
import tempfile

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


# ── get_device_state ──────────────────────────────────────────────────────
print("[1] get_device_state — authorized")
with mock.patch.object(adb, "_adb_cmd",
                        return_value="List of devices attached\n001045c585010020\tdevice"):
    _check("returns 'device'", adb.get_device_state() == "device")

print("\n[2] get_device_state — offline")
with mock.patch.object(adb, "_adb_cmd",
                        return_value="List of devices attached\n001045c585010020\toffline"):
    _check("returns 'offline'", adb.get_device_state() == "offline")

print("\n[3] get_device_state — unauthorized")
with mock.patch.object(adb, "_adb_cmd",
                        return_value="List of devices attached\n001045c585010020\tunauthorized"):
    _check("returns 'unauthorized'", adb.get_device_state() == "unauthorized")

print("\n[4] get_device_state — authorizing")
with mock.patch.object(adb, "_adb_cmd",
                        return_value="List of devices attached\n001045c585010020\tauthorizing"):
    _check("returns 'authorizing'", adb.get_device_state() == "authorizing")

print("\n[5] get_device_state — nothing listed at all")
with mock.patch.object(adb, "_adb_cmd", return_value="List of devices attached"):
    _check("returns 'absent'", adb.get_device_state() == "absent")

print("\n[6] get_device_state — empty output entirely")
with mock.patch.object(adb, "_adb_cmd", return_value=""):
    _check("returns 'absent'", adb.get_device_state() == "absent")

# ── reset_adb_pairing ────────────────────────────────────────────────────
print("\n[7] reset_adb_pairing — backs up existing keys, never deletes")
with tempfile.TemporaryDirectory() as td:
    fake_home = Path(td)
    android_dir = fake_home / ".android"
    android_dir.mkdir()
    (android_dir / "adbkey").write_text("fake-private-key")
    (android_dir / "adbkey.pub").write_text("fake-public-key")

    with mock.patch.object(adb, "_adb_cmd", return_value="") as m_cmd, \
         mock.patch.object(Path, "home", return_value=fake_home):
        ok, detail = adb.reset_adb_pairing()
        _check("returns ok=True", ok is True)
        _check("mentions both files backed up", "adbkey" in detail and "adbkey.pub" in detail)
        _check("kill-server called", any(c.args[0] == ["kill-server"] for c in m_cmd.call_args_list))
        _check("start-server called", any(c.args[0] == ["start-server"] for c in m_cmd.call_args_list))

    remaining = sorted(p.name for p in android_dir.iterdir())
    _check("original adbkey no longer present (renamed, not deleted)",
           "adbkey" not in remaining)
    _check("a .bak- backup file exists for adbkey",
           any(n.startswith("adbkey.bak-") for n in remaining))
    _check("a .bak- backup file exists for adbkey.pub",
           any(n.startswith("adbkey.pub.bak-") for n in remaining))
    _check("backup content matches original (not truncated/corrupted)",
           any((android_dir / n).read_text() == "fake-private-key"
               for n in remaining if n.startswith("adbkey.bak-")))

print("\n[8] reset_adb_pairing — no existing key is a clean no-op (not an error)")
with tempfile.TemporaryDirectory() as td:
    fake_home = Path(td)
    (fake_home / ".android").mkdir()
    with mock.patch.object(adb, "_adb_cmd", return_value=""), \
         mock.patch.object(Path, "home", return_value=fake_home):
        ok, detail = adb.reset_adb_pairing()
        _check("returns ok=True", ok is True)
        _check("detail says no existing key found", "No existing adbkey" in detail)

# ── diagnose_windows_driver ──────────────────────────────────────────────
print("\n[9] diagnose_windows_driver — ADB Interface bound and healthy")
with mock.patch("wpd_transfer._find_ps_exe", return_value="powershell"), \
     mock.patch.object(adb.subprocess, "run") as m_run:
    m_run.return_value = mock.Mock(
        stdout='[{"FriendlyName":"ADB Interface","Status":"OK"},'
               '{"FriendlyName":"WALKMAN","Status":"OK"}]',
        returncode=0,
    )
    result = adb.diagnose_windows_driver()
    _check("ok=True", result["ok"] is True)
    _check("driver_bound=True", result["driver_bound"] is True)

print("\n[10] diagnose_windows_driver — only MTP driver bound, no ADB Interface")
with mock.patch("wpd_transfer._find_ps_exe", return_value="powershell"), \
     mock.patch.object(adb.subprocess, "run") as m_run:
    m_run.return_value = mock.Mock(
        stdout='[{"FriendlyName":"WALKMAN","Status":"OK"},'
               '{"FriendlyName":"MTP USB Device","Status":"Unknown"}]',
        returncode=0,
    )
    result = adb.diagnose_windows_driver()
    _check("ok=False", result["ok"] is False)
    _check("driver_bound=False", result["driver_bound"] is False)
    _check("detail mentions MTP-only", "MTP" in result["detail"] or "ADB Interface" in result["detail"])

print("\n[11] diagnose_windows_driver — ADB Interface present but not OK")
with mock.patch("wpd_transfer._find_ps_exe", return_value="powershell"), \
     mock.patch.object(adb.subprocess, "run") as m_run:
    m_run.return_value = mock.Mock(
        stdout='[{"FriendlyName":"ADB Interface","Status":"Error"}]',
        returncode=0,
    )
    result = adb.diagnose_windows_driver()
    _check("ok=False", result["ok"] is False)
    _check("driver_bound=True", result["driver_bound"] is True)

print("\n[12] diagnose_windows_driver — nothing found matching the vendor ID")
with mock.patch("wpd_transfer._find_ps_exe", return_value="powershell"), \
     mock.patch.object(adb.subprocess, "run") as m_run:
    m_run.return_value = mock.Mock(stdout="", returncode=0)
    result = adb.diagnose_windows_driver()
    _check("ok=False", result["ok"] is False)
    _check("detail mentions vendor ID", "054C" in result["detail"])

print("\n[13] diagnose_windows_driver — no PowerShell available")
with mock.patch("wpd_transfer._find_ps_exe", return_value=None):
    result = adb.diagnose_windows_driver()
    _check("ok=False", result["ok"] is False)
    _check("does not raise, returns a detail string", isinstance(result["detail"], str))

print("\n[14] diagnose_windows_driver — never raises even on unexpected exceptions")
with mock.patch("wpd_transfer._find_ps_exe", side_effect=RuntimeError("boom")):
    try:
        result = adb.diagnose_windows_driver()
        _check("returns ok=False instead of raising", result["ok"] is False)
    except Exception as e:
        _check(f"does not raise (raised {e!r} instead)", False)

print(f"\n{'=' * 60}")
print(f"ADB diagnostics: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
