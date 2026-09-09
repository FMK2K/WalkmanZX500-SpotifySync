"""
test_mk40_com_verify_fix.py — WPD/COM verification false-positive-deletion fix
================================================================================
Covers the transport hardening pass described in task_plan.md (2026-07-22):
COM/WPD's Shell.Application size check can report 0 bytes for 60-90+s after a
file is actually written correctly (cache staleness deep in the WPD driver
stack, below anything a fresh COM re-navigation can see). Before this fix,
ANY _verify_com_copy failure — ambiguous cache-stale or genuine corruption —
was treated identically and the just-written remote file was deleted.

This file exercises:
  1. _adb_cross_check in isolation (mocking adb_transfer)
  2. _verify_com_copy's regression boundary (confirmed nonzero mismatch must
     still fail and must NOT consult the ADB cross-check)
  3. _verify_com_copy's ambiguous branches (ADB cross-check resolves / doesn't)
  4. copy_file_to_device's new branch: ambiguous failure -> device_status is
     marked 'pending' in sync_state, remote file is NOT deleted
  5. copy_file_to_device: sync_state unavailable -> falls back to today's
     delete+raise exactly (no silent data-loss-avoidance regression)
  6. verify_com_copy_isolated's dual-watchdog subprocess wrapper
  7. copy_file_to_device's PowerShell-path heartbeat ordering (fixed
     2026-07-22, previously zero test coverage)

walkman_sync.py's Stage 6 pending-recheck hook (Part C of the plan) is NOT
behaviourally unit-tested here: the hook lives inside _tag_worker, a closure
nested inside WalkmanSyncAgent's run() method, not a standalone callable —
same testability constraint test_mk17.py already documents for similarly
deep pipeline code (source-text checks instead of execution). Section 8
below verifies the wiring via source-text checks, matching that convention.

Offline only — mocks mtp_transfer_com/adb_transfer functions directly. No
device, no network, no real COM/Shell.Application calls.
"""
import os
import pathlib
import sys
import tempfile
import time
from unittest import mock

import mtp_transfer_com as com
import sync_state

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


def _reset_sync_state(db_path: pathlib.Path) -> None:
    """sync_state.open_db() is idempotent (module-level _initialised guard) —
    reset its internals so each section can point at its own throwaway DB."""
    try:
        conn = getattr(sync_state._tls, "conn", None)
        if conn is not None:
            conn.close()
    except Exception:
        pass
    sync_state._tls = __import__("threading").local()
    sync_state._initialised = False
    sync_state._db_path = None
    sync_state.open_db(db_path)


_tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="mk40_verify_test_"))


# ─── 1. _adb_cross_check ────────────────────────────────────────────────────
print("\n[1] _adb_cross_check")

with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/emulated/0"), \
     mock.patch("adb_transfer._verify_remote_size", return_value=(True, "")) as m_verify:
    result = com._adb_cross_check("WALKMAN", "Internal shared storage", ["Music", "Artist"], "t.flac", 12345)
_check("returns True when ADB confirms size", result is True)
_check("_verify_remote_size called with translated absolute path",
       m_verify.call_args[0][0] == "/storage/emulated/0/Music/Artist/t.flac")
_check("_verify_remote_size called with expected_size", m_verify.call_args[0][1] == 12345)

with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/emulated/0"), \
     mock.patch("adb_transfer._verify_remote_size", return_value=(False, "size mismatch")):
    result2 = com._adb_cross_check("WALKMAN", "Internal shared storage", ["Music"], "t.flac", 12345)
_check("returns False when ADB disagrees", result2 is False)

with mock.patch("adb_transfer.device_is_connected", return_value=False):
    result3 = com._adb_cross_check("WALKMAN", "SD card", ["Music"], "t.flac", 12345)
_check("returns None when ADB device not connected (no signal, not a negative)", result3 is None)

with mock.patch("adb_transfer.device_is_connected", side_effect=RuntimeError("adb boom")):
    result4 = com._adb_cross_check("WALKMAN", "SD card", ["Music"], "t.flac", 12345)
_check("returns None (never raises) when adb_transfer itself errors", result4 is None)


# ─── 2. _verify_com_copy — regression: confirmed nonzero mismatch still fails,
#        and does NOT consult the ADB cross-check ──────────────────────────
print("\n[2] _verify_com_copy — nonzero size mismatch (must still fail, must not ask ADB)")

_mismatch_item = mock.MagicMock()
_mismatch_item.Size = 999  # nonzero, but wrong vs expected_size=12345

with mock.patch("mtp_transfer_com._get_storage_root", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._navigate", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._find_remote_item", return_value=_mismatch_item), \
     mock.patch("adb_transfer.device_is_connected", return_value=True) as m_adb_conn:
    ok, reason = com._verify_com_copy(
        "WALKMAN", "SD card", ["Music", "Artist"], "t.flac", 12345, retries=0, base_wait_s=0,
    )
_check("returns False for confirmed nonzero mismatch", ok is False)
_check("reason is NOT tagged ambiguous (this is a real, unambiguous mismatch)",
       not reason.startswith(com._AMBIGUOUS_PREFIX))
_check("ADB cross-check was never consulted for a confirmed mismatch", not m_adb_conn.called)


# ─── 3. _verify_com_copy — ambiguous size=0, ADB resolves it ───────────────
print("\n[3] _verify_com_copy — size stuck at 0, ADB cross-check confirms it's actually fine")

_zero_item = mock.MagicMock()
_zero_item.Size = 0

with mock.patch("mtp_transfer_com._get_storage_root", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._navigate", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._find_remote_item", return_value=_zero_item), \
     mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/emulated/0"), \
     mock.patch("adb_transfer._verify_remote_size", return_value=(True, "")):
    ok, reason = com._verify_com_copy(
        "WALKMAN", "Internal shared storage", ["Music"], "t.flac", 12345, retries=0, base_wait_s=0,
    )
_check("returns True when ADB cross-check confirms a COM size=0 result", ok is True)
_check("reason mentions the ADB cross-check", "ADB cross-check" in reason)


# ─── 4. _verify_com_copy — ambiguous size=0, ADB unavailable/disagrees ─────
print("\n[4] _verify_com_copy — size stuck at 0, ADB can't resolve it either")

with mock.patch("mtp_transfer_com._get_storage_root", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._navigate", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._find_remote_item", return_value=_zero_item), \
     mock.patch("adb_transfer.device_is_connected", return_value=False):
    ok, reason = com._verify_com_copy(
        "WALKMAN", "SD card", ["Music"], "t.flac", 12345, retries=0, base_wait_s=0,
    )
_check("still returns False when nothing can resolve the ambiguity", ok is False)
_check("reason IS tagged ambiguous — caller must not delete on this", reason.startswith(com._AMBIGUOUS_PREFIX))


# ─── 5. copy_file_to_device — ambiguous failure marks device_status=pending,
#        does NOT delete the remote file ───────────────────────────────────
print("\n[5] copy_file_to_device — ambiguous verify -> pending, no deletion")

_db5 = _tmpdir / "sync_state_5.db"
_reset_sync_state(_db5)

_src_file = _tmpdir / "track.flac"
_src_file.write_bytes(b"fake audio data, exact bytes don't matter for this test")

with mock.patch("mtp_transfer_com._get_storage_root", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._navigate", return_value=mock.MagicMock(**{"Items.return_value": []})), \
     mock.patch("wpd_transfer._try_powershell", return_value=True), \
     mock.patch("mtp_transfer_com._verify_com_copy",
                return_value=(False, f"{com._AMBIGUOUS_PREFIX}remote size is 0 (likely empty placeholder)")), \
     mock.patch("mtp_transfer_com._delete_remote_after_failed_verify") as m_delete, \
     mock.patch("mtp_transfer_com._trigger_media_scan_file"):
    result = com.copy_file_to_device(_src_file, ["Music", "Artist"], "WALKMAN", "SD card")

_check("copy_file_to_device returns normally (soft-success), does not raise", result == "SD card")
_check("_delete_remote_after_failed_verify was NOT called", not m_delete.called)
_state5 = sync_state.get_state(str(_src_file.absolute()))
_check("sync_state row exists", _state5 is not None)
_check("device_status marked pending", _state5 is not None and _state5.get("device_status") == "pending")


# ─── 6. copy_file_to_device — confirmed mismatch still deletes (regression) ─
print("\n[6] copy_file_to_device — confirmed (non-ambiguous) verify failure still deletes")

_db6 = _tmpdir / "sync_state_6.db"
_reset_sync_state(_db6)
_src_file2 = _tmpdir / "track2.flac"
_src_file2.write_bytes(b"more fake audio data")

with mock.patch("mtp_transfer_com._get_storage_root", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._navigate", return_value=mock.MagicMock(**{"Items.return_value": []})), \
     mock.patch("wpd_transfer._try_powershell", return_value=True), \
     mock.patch("mtp_transfer_com._verify_com_copy",
                return_value=(False, "size mismatch: expected 999, got 111 (delta=888 bytes, tolerance=0)")), \
     mock.patch("mtp_transfer_com._delete_remote_after_failed_verify") as m_delete2, \
     mock.patch("mtp_transfer_com._trigger_media_scan_file"):
    try:
        com.copy_file_to_device(_src_file2, ["Music", "Artist"], "WALKMAN", "SD card")
        _check("raises RuntimeError on confirmed mismatch", False)
    except RuntimeError:
        _check("raises RuntimeError on confirmed mismatch", True)
_check("_delete_remote_after_failed_verify WAS called (unchanged behavior)", m_delete2.called)


# ─── 7. copy_file_to_device — sync_state itself unusable -> falls back to
#        today's delete+raise exactly (never silently keep an unverified file
#        if we can't even track it) ─────────────────────────────────────────
print("\n[7] copy_file_to_device — sync_state unavailable falls back to delete+raise")

_src_file3 = _tmpdir / "track3.flac"
_src_file3.write_bytes(b"yet more fake audio data")

with mock.patch("mtp_transfer_com._get_storage_root", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._navigate", return_value=mock.MagicMock(**{"Items.return_value": []})), \
     mock.patch("wpd_transfer._try_powershell", return_value=True), \
     mock.patch("mtp_transfer_com._verify_com_copy",
                return_value=(False, f"{com._AMBIGUOUS_PREFIX}remote size is 0 (likely empty placeholder)")), \
     mock.patch("sync_state.transition_to", side_effect=RuntimeError("db locked")), \
     mock.patch("mtp_transfer_com._delete_remote_after_failed_verify") as m_delete3, \
     mock.patch("mtp_transfer_com._trigger_media_scan_file"):
    try:
        com.copy_file_to_device(_src_file3, ["Music", "Artist"], "WALKMAN", "SD card")
        _check("raises RuntimeError when sync_state is unusable", False)
    except RuntimeError:
        _check("raises RuntimeError when sync_state is unusable", True)
_check("falls back to delete when sync_state can't be used", m_delete3.called)


# ─── 8. verify_com_copy_isolated — dual-watchdog subprocess wrapper ────────
print("\n[8] verify_com_copy_isolated — subprocess isolation + watchdog")


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


class _FakeProc:
    """Simulates a worker that exits quickly with a result marker."""
    def __init__(self, result_line):
        self.stdout = _FakeStream([result_line + "\n"])
        self.stderr = _FakeStream([])
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


import json as _json_test

_ok_line = com._VERIFY_RESULT_MARKER + _json_test.dumps([True, "verified, size=12345"])
with mock.patch("subprocess.Popen", return_value=_FakeProc(_ok_line)):
    result = com.verify_com_copy_isolated("WALKMAN", "SD card", ["Music"], "t.flac", 12345)
_check("clean exit with result marker returns parsed (ok, reason)", result == (True, "verified, size=12345"))


class _HangingProc:
    """Simulates a worker that never produces a heartbeat and never exits
    within quiet_timeout_s — the watchdog must kill it and return None."""
    def __init__(self):
        self.stdout = _FakeStream([])
        self.stderr = _FakeStream([])
        self.killed = False

    def wait(self, timeout=None):
        import subprocess as _sp
        raise _sp.TimeoutExpired(cmd="verify-worker", timeout=timeout)

    def kill(self):
        self.killed = True


_hanging = _HangingProc()
with mock.patch("subprocess.Popen", return_value=_hanging), \
     mock.patch("time.time", side_effect=[0.0, 0.0] + [200.0] * 20):  # jump well past quiet_timeout_s
    result_hang = com.verify_com_copy_isolated(
        "WALKMAN", "SD card", ["Music"], "t.flac", 12345, quiet_timeout_s=90.0, max_duration_s=180.0,
    )
_check("hung worker is killed", _hanging.killed is True)
_check("returns None (unknown), not a False verification result", result_hang is None)


# ─── 9. copy_file_to_device — PowerShell-path heartbeat fires BEFORE
#        _try_powershell is invoked (fixed 2026-07-22, previously untested —
#        the outer isolated-copy watchdog killed a real, successful transfer
#        because this call site had no heartbeat) ──────────────────────────
print("\n[9] PowerShell-path heartbeat ordering")

_call_order = []


def _tracking_beat():
    _call_order.append("beat")


def _tracking_try_powershell(**kwargs):
    _call_order.append("try_powershell")
    return True


_src_file4 = _tmpdir / "track4.flac"
_src_file4.write_bytes(b"ps heartbeat ordering test data")

_reset_sync_state(_tmpdir / "sync_state_9.db")

with mock.patch("mtp_transfer_com._get_storage_root", return_value=mock.MagicMock()), \
     mock.patch("mtp_transfer_com._navigate", return_value=mock.MagicMock(**{"Items.return_value": []})), \
     mock.patch("wpd_transfer._try_powershell", side_effect=_tracking_try_powershell), \
     mock.patch("mtp_transfer_com._verify_com_copy", return_value=(True, "verified, size=32")), \
     mock.patch("mtp_transfer_com._trigger_media_scan_file"):
    com.copy_file_to_device(
        _src_file4, ["Music", "Artist"], "WALKMAN", "SD card", progress_cb=_tracking_beat,
    )
_check("_try_powershell was invoked", "try_powershell" in _call_order)
_check("a heartbeat fired before _try_powershell (2026-07-22 fix — prevents the outer "
       "quiet-timeout watchdog from killing a silently-blocking-but-successful PS transfer)",
       "beat" in _call_order and _call_order.index("beat") < _call_order.index("try_powershell"))


# ─── 10. walkman_sync.py Stage 6 pending-recheck wiring (source-text check)─
# _tag_worker is a closure nested inside WalkmanSyncAgent's run() method, not
# a standalone callable — same testability constraint test_mk17.py already
# documents for similarly deep pipeline code ("can't import ... on Linux" /
# source-text checks instead of execution). Verifying the wiring markers
# directly rather than writing a test that can't actually drive the real
# closure.
print("\n[10] walkman_sync.py Stage 6 pending-recheck wiring")

with open(os.path.join(os.path.dirname(__file__), "walkman_sync.py"),
          "r", encoding="utf-8") as f:
    ws_src = f.read()

_check("_PENDING_MAX_RECHECKS cap constant defined", "_PENDING_MAX_RECHECKS = 5" in ws_src)
_check("checks device_status == 'pending' before deciding to re-copy",
       '_pending_state.get("device_status") == "pending"' in ws_src)
_check("cap short-circuits to retryable_error without a recheck",
       'failure_count", 0) >= _PENDING_MAX_RECHECKS' in ws_src
       and 'transition_to(_pending_relpath, "device_status", "retryable_error")' in ws_src)
_check("recheck uses the isolated, watchdog-guarded verify primitive (Part D)",
       "verify_com_copy_isolated(" in ws_src)
_check("recheck derives dest_parts deterministically (no new sync_state schema)",
       "folder_parts = self._device_folder_parts(track)" in ws_src)
_check("confirmed-pending success promotes device_status to synced",
       'transition_to(_pending_relpath, "device_status", "synced")' in ws_src)
_check("confirmed-pending success is consumed without a redundant re-copy",
       '"confirmed_pending"' in ws_src)


import shutil
shutil.rmtree(_tmpdir, ignore_errors=True)

print(f"\n{'='*60}\nMK40 COM-verify hardening: {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
