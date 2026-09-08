"""
test_ensure_folder_isolated.py — isolated verification for
mtp_transfer_com.ensure_folder_isolated(), added 2026-07-25 to replace
walkman_dedup._ensure_deleted_folder()'s raw, unguarded COM fallback
(confirmed live to hang 90+s on this device) with the same subprocess
+ dual-guard pattern used by file_exists_isolated/copy_file_to_device_isolated.
No device required — subprocess.Popen is mocked.
"""
from unittest import mock

import mtp_transfer_com as m

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


class _FakeProc:
    def __init__(self, stdout_lines, stderr_lines=None, timeout=False):
        self.stdout = iter(stdout_lines)
        self.stderr = iter(stderr_lines or [])
        self._timeout = timeout
        self.returncode = 0
        self._waited = False

    def wait(self, timeout=None):
        if self._timeout and not self._waited:
            import subprocess
            raise subprocess.TimeoutExpired(cmd="mtp_transfer_com.py", timeout=timeout)
        self._waited = True
        return 0

    def kill(self):
        self._timeout = False  # let the next wait() succeed, like a real kill would


print("[1] success case: worker reports result marker")
with mock.patch("subprocess.Popen",
                return_value=_FakeProc([f"{m._ENSURE_FOLDER_RESULT_MARKER}1\n"])):
    r = m.ensure_folder_isolated("WALKMAN", "SD card", ["Music", ".deleted"])
    _check("returns True on result marker", r is True)

print("\n[2] worker error: returns None, not an exception")
with mock.patch("subprocess.Popen",
                return_value=_FakeProc([f"{m._ENSURE_FOLDER_ERROR_MARKER}RuntimeError: boom\n"])):
    r = m.ensure_folder_isolated("WALKMAN", "SD card", ["Music", ".deleted"])
    _check("returns None on worker error", r is None)

print("\n[3] no result marker at all: returns None")
with mock.patch("subprocess.Popen", return_value=_FakeProc(["some unrelated line\n"])):
    r = m.ensure_folder_isolated("WALKMAN", "SD card", ["Music", ".deleted"])
    _check("returns None when no marker found", r is None)

print("\n[4] subprocess never starts: returns None, does not raise")
with mock.patch("subprocess.Popen", side_effect=OSError("cannot spawn")):
    try:
        r = m.ensure_folder_isolated("WALKMAN", "SD card", ["Music", ".deleted"])
        _check("returns None when Popen fails", r is None)
    except Exception as e:
        _check(f"returns None when Popen fails (raised {e!r} instead)", False)

print("\n[5] stuck subprocess: guard kills it, returns None (not hang)")
with mock.patch("subprocess.Popen", return_value=_FakeProc([], timeout=True)):
    r = m.ensure_folder_isolated(
        "WALKMAN", "SD card", ["Music", ".deleted"],
        quiet_timeout_s=0.05, max_duration_s=0.1,
    )
    _check("returns None after guard fires on a stuck worker", r is None)

print(f"\n{'=' * 60}")
print(f"ensure_folder_isolated: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
