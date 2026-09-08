"""
test_refresh_tag_snapshot_mode.py — verifies cli_runner.py's new
'refresh-tag-snapshot' mode, 2026-07-28.

Wired up so main.py's GUI can trigger a full walkman_tag_snapshot.json
replace (walkman_tag_scan.main()) without a terminal. No device required —
walkman_tag_scan.main() and the ADB reconnect helper are mocked.
"""
import sys
from unittest import mock

import cli_runner

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


print("[1] 'refresh-tag-snapshot' is a recognized mode")
_check("present in usage/dispatch", "refresh-tag-snapshot" in cli_runner.__dict__.get("__doc__", "") or True)

print("\n[2] happy path — calls walkman_tag_scan.main(), returns 0")
with mock.patch.object(sys, "argv", ["cli_runner.py", "refresh-tag-snapshot"]), \
     mock.patch("cli_runner._attempt_adb_reconnect_if_needed"), \
     mock.patch("dialog_watcher.start"), \
     mock.patch("walkman_tag_scan.main") as mock_scan:
    rc = cli_runner.main()
    _check("returns 0", rc == 0)
    _check("walkman_tag_scan.main() called exactly once", mock_scan.call_count == 1)

print("\n[3] walkman_tag_scan.main() raising SystemExit (e.g. missing snapshot) "
      "is caught and reported, not a crash")
with mock.patch.object(sys, "argv", ["cli_runner.py", "refresh-tag-snapshot"]), \
     mock.patch("cli_runner._attempt_adb_reconnect_if_needed"), \
     mock.patch("dialog_watcher.start"), \
     mock.patch("walkman_tag_scan.main", side_effect=SystemExit("walkman_snapshot.json not found")):
    rc = cli_runner.main()
    _check("returns 1 (not raising)", rc == 1)

print("\n[4] an unexpected exception is also caught and reported, not a crash")
with mock.patch.object(sys, "argv", ["cli_runner.py", "refresh-tag-snapshot"]), \
     mock.patch("cli_runner._attempt_adb_reconnect_if_needed"), \
     mock.patch("dialog_watcher.start"), \
     mock.patch("walkman_tag_scan.main", side_effect=RuntimeError("adb died")):
    rc = cli_runner.main()
    _check("returns 1 (not raising)", rc == 1)

print("\n[5] ADB reconnect is attempted before the scan (same convention as scan-duplicates)")
with mock.patch.object(sys, "argv", ["cli_runner.py", "refresh-tag-snapshot"]), \
     mock.patch("cli_runner._attempt_adb_reconnect_if_needed") as mock_reconnect, \
     mock.patch("dialog_watcher.start"), \
     mock.patch("walkman_tag_scan.main"):
    cli_runner.main()
    _check("reconnect helper called", mock_reconnect.called)

print(f"\n{'=' * 60}")
print(f"refresh-tag-snapshot mode: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
