"""
test_adb_reconnect_refresh.py — isolated verification for
mtp_transfer.refresh_adb_availability(), added 2026-07-26.

ADB_AVAILABLE used to be computed exactly once, at import time — if ADB was
disconnected at that instant, every later operation in the process used
COM/WPD exclusively for its whole lifetime, even after ADB reconnected.
Confirmed live: an overnight scan-duplicates run correctly detected ADB was
down at startup, never reconsidered, and spent ~22 minutes exhausting the
WPD/Shell.Application fallback chain instead of retrying ADB. No device
required — adb_transfer.device_is_connected is mocked.
"""
from unittest import mock

import mtp_transfer as mtp

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


print("[1] refresh picks up ADB coming back online")
mtp.ADB_AVAILABLE = False
with mock.patch.object(mtp._adb, "device_is_connected", return_value=True):
    r = mtp.refresh_adb_availability()
    _check("returns True", r is True)
    _check("module flag updated to True", mtp.ADB_AVAILABLE is True)

print("\n[2] refresh picks up ADB going offline")
mtp.ADB_AVAILABLE = True
with mock.patch.object(mtp._adb, "device_is_connected", return_value=False):
    r = mtp.refresh_adb_availability()
    _check("returns False", r is False)
    _check("module flag updated to False", mtp.ADB_AVAILABLE is False)

print("\n[3] refresh never raises even if the live check errors")
mtp.ADB_AVAILABLE = True
with mock.patch.object(mtp._adb, "device_is_connected", side_effect=RuntimeError("adb died")):
    try:
        r = mtp.refresh_adb_availability()
        _check("returns the last-known flag on error rather than raising", r is True)
    except Exception as e:
        _check(f"does not raise (raised {e!r} instead)", False)

print("\n[4] refresh is a no-op returning the current flag when USE_ADB is off / _adb unavailable")
with mock.patch.object(mtp, "_USE_ADB", False):
    mtp.ADB_AVAILABLE = False
    r = mtp.refresh_adb_availability()
    _check("returns False without touching _adb", r is False)

print(f"\n{'=' * 60}")
print(f"refresh_adb_availability: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
