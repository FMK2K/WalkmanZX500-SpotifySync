"""
test_task_swap_com_freespace.py — isolated verification for the COM/MTP
free-space fallback added to free_space_check.get_device_free_bytes()
(2026-07-19, in response to ADB flakiness observed live tonight).

Offline only — mocks adb_transfer._adb_cmd and mtp_transfer.get_device_free_space.
"""
import sys
from unittest import mock

import config
import free_space_check as fsc

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


# ─── 1. ADB df succeeds — COM fallback never consulted ─────────────────────
print("\n[1] ADB df succeeds — COM fallback not touched")
_DF_OK = (
    "Filesystem     1K-blocks     Used Available Use% Mounted on\n"
    "/dev/fuse       25000000 20000000   5000000  81% /storage/4A21-0000\n"
)
config.WRITE_STORAGE = "SD card"
with mock.patch("adb_transfer._adb_cmd", return_value=_DF_OK), \
     mock.patch("mtp_transfer.get_device_free_space") as m_com:
    result = fsc.get_device_free_bytes()
_check("returns real ADB reading", result == 5000000 * 1024)
_check("COM fallback never called when ADB succeeds", not m_com.called)

# ─── 2. ADB df fails both retries — COM fallback triggers ──────────────────
print("\n[2] ADB df fails both retries — COM fallback triggers")
with mock.patch("adb_transfer._adb_cmd", return_value=None), \
     mock.patch("time.sleep"), \
     mock.patch("mtp_transfer.get_device_free_space", return_value=3_000_000_000) as m_com:
    result = fsc.get_device_free_bytes()
_check("COM fallback was consulted", m_com.called)
_check("returns the COM reading", result == 3_000_000_000)

# ─── 3. ADB fails AND COM fails/returns -1 — degrades to None cleanly ──────
print("\n[3] Both ADB and COM fail — degrades to None, no exception")
with mock.patch("adb_transfer._adb_cmd", return_value=None), \
     mock.patch("time.sleep"), \
     mock.patch("mtp_transfer.get_device_free_space", return_value=-1):
    try:
        result = fsc.get_device_free_bytes()
        _check("no exception raised", True)
        _check("returns None when both backends fail", result is None)
    except Exception:
        _check("no exception raised", False)

# ─── 4. COM fallback itself raises — degrades to None, no exception ───────
print("\n[4] COM fallback raises internally — still degrades cleanly")
with mock.patch("adb_transfer._adb_cmd", return_value=None), \
     mock.patch("time.sleep"), \
     mock.patch("mtp_transfer.get_device_free_space", side_effect=RuntimeError("com error")):
    try:
        result = fsc.get_device_free_bytes()
        _check("no exception raised", True)
        _check("returns None on COM exception", result is None)
    except Exception:
        _check("no exception raised", False)

# ─── 5. COM fallback returns an implausibly small value — rejected ─────────
print("\n[5] COM returns implausibly small value — rejected, not trusted")
with mock.patch("adb_transfer._adb_cmd", return_value=None), \
     mock.patch("time.sleep"), \
     mock.patch("mtp_transfer.get_device_free_space", return_value=100):  # 100 bytes — garbage
    result = fsc.get_device_free_bytes()
_check("implausible COM reading rejected (returns None, not 100)", result is None)

print(f"\n{'='*60}\nCOM/MTP free-space fallback: {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
