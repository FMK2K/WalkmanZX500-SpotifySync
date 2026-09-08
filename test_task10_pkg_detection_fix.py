"""
test_task10_pkg_detection_fix.py — regression test for the substring-match
bug found live in walkman_app_refresh._detect_walkman_pkg() (Task 10
investigation, 2026-07-18): it used to match candidates via substring
containment ("package:com.sony.songpal" in out), which incorrectly matched
the installed-but-unrelated "com.sony.songpal.mdr" (Bluetooth headphone
companion app). This locks in the exact-match fix.

Offline only — mocks adb_transfer._adb_cmd, no device required.
"""
import sys
from unittest import mock

import walkman_app_refresh as war

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


def _reset():
    war._resolved_pkg = None


# ─── 1. Regression case: com.sony.songpal.mdr installed, real match absent ──
# Reproduces the exact live bug: the substring check used to match candidate
# "com.sony.songpal" against installed "com.sony.songpal.mdr" and stop there,
# before ever reaching a real player candidate lower in priority.
print("\n[1] Regression: songpal.mdr present must NOT false-match 'com.sony.songpal'")
_reset()
pm_output = (
    "package:com.sony.songpal.mdr\n"
    "package:com.android.settings\n"
)
with mock.patch("walkman_app_refresh._adb_cmd", return_value=pm_output):
    result = war._detect_walkman_pkg()
_check("no false match on substring-contained package", result is None)

# ─── 2. Real player app present — correctly detected as highest priority ────
print("\n[2] com.sony.walkman.highresmediaplayer present — correctly detected")
_reset()
pm_output = (
    "package:com.sony.songpal.mdr\n"
    "package:com.sony.walkman.highresmediaplayer\n"
    "package:com.android.settings\n"
)
with mock.patch("walkman_app_refresh._adb_cmd", return_value=pm_output):
    result = war._detect_walkman_pkg()
_check("detected the real player app", result == "com.sony.walkman.highresmediaplayer")

# ─── 3. Exact-match candidate (com.sony.songpal itself) still works ────────
print("\n[3] Exact match still works when the candidate IS actually installed")
_reset()
pm_output = "package:com.sony.songpal\n"
with mock.patch("walkman_app_refresh._adb_cmd", return_value=pm_output):
    result = war._detect_walkman_pkg()
_check("exact-match candidate still detected", result == "com.sony.songpal")

# ─── 4. Caching — second call doesn't re-probe adb ──────────────────────────
print("\n[4] Result is cached across calls")
_reset()
pm_output = "package:com.sony.walkman.highresmediaplayer\n"
with mock.patch("walkman_app_refresh._adb_cmd", return_value=pm_output) as m_adb:
    war._detect_walkman_pkg()
    war._detect_walkman_pkg()
_check("adb probed only once (cached)", m_adb.call_count == 1)

_reset()
print(f"\n{'='*60}\nTask 10 (pkg detection substring-match fix): {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
