"""
test_mk17.py — MK17 regression tests
=====================================
Exercises the five MK17 patches:

  P1 — Tag-write resilience (writability pre-check + retry + copy-tag-replace fallback)
  P2 — Router both-unknown recovery + degraded-mode flag
  P3 — Device-index self-heal (3 attempts + adb-server-restart + shallow fallback)
  P4 — COM verification parity with ADB (size verification + delete-on-fail)
  P5 — Run-level health summary (counters + verdict line)
"""
from __future__ import annotations
import sys
import os
import re
import pathlib
import tempfile
import shutil
import stat
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def _check(name: str, cond: bool, details: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}  {details}")
        print(f"  \u2717 {name}  {details}")


# ─── 1. P1: id3_helpers writability + atomic-replace ────────────────────
print("\n[1] P1: id3_helpers writability + atomic-replace helpers")

import id3_helpers as _idh

_check("is_writable function exists", callable(getattr(_idh, "is_writable", None)))
_check("ensure_writable function exists", callable(getattr(_idh, "ensure_writable", None)))
_check("copy_to_writable_temp function exists", callable(getattr(_idh, "copy_to_writable_temp", None)))
_check("atomic_replace function exists", callable(getattr(_idh, "atomic_replace", None)))

# Functional tests
tmpdir = tempfile.mkdtemp(prefix="mk17_p1_")
try:
    # is_writable: missing path returns False
    missing = pathlib.Path(tmpdir) / "missing.mp3"
    _check("is_writable(missing) -> False", _idh.is_writable(missing) == False)

    # is_writable: existing path returns True
    test_file = pathlib.Path(tmpdir) / "test.mp3"
    test_file.write_bytes(b"x")
    _check("is_writable(existing) -> True", _idh.is_writable(test_file) == True)

    # ensure_writable on a read-only file makes it writable
    test_file.chmod(stat.S_IRUSR)
    pre = _idh.is_writable(test_file)
    _idh.ensure_writable(test_file)
    post = _idh.is_writable(test_file)
    _check("ensure_writable clears RO bit", post == True,
           f"pre={pre} post={post}")

    # copy_to_writable_temp produces a writable copy in a new dir
    test_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # restore writable
    copy_path = _idh.copy_to_writable_temp(test_file)
    _check("copy_to_writable_temp returns Path", copy_path is not None)
    _check("copy is in a different directory",
           copy_path.parent != test_file.parent if copy_path else False)
    _check("copy contents match",
           copy_path.read_bytes() == b"x" if copy_path else False)

    # atomic_replace moves src to dst
    src_file = pathlib.Path(tmpdir) / "src.mp3"
    src_file.write_bytes(b"new")
    dst_file = pathlib.Path(tmpdir) / "dst.mp3"
    dst_file.write_bytes(b"old")
    ok = _idh.atomic_replace(src_file, dst_file)
    _check("atomic_replace returns True", ok == True)
    _check("atomic_replace overwrites dst", dst_file.read_bytes() == b"new")
    _check("atomic_replace removes src", not src_file.exists())
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 2. P1: source-level wiring in downloader.py ────────────────────────
print("\n[2] P1: downloader.py wires the recovery layers")

with open(os.path.join(os.path.dirname(__file__), "downloader.py"),
          "r", encoding="utf-8") as f:
    dl_src = f.read()

_check(
    "downloader pre-checks writability before tag",
    "_idh.ensure_writable(src_file)" in dl_src,
)
_check(
    "downloader has copy-tag-replace fallback",
    "_idh.copy_to_writable_temp" in dl_src
    and "_idh.atomic_replace" in dl_src,
)
_check(
    "downloader has 3-tier retry+copy structure",
    "(50, 200, 800)" in dl_src,
)
_check(
    "downloader has clear fallback log line",
    "Tag write recovered via copy-tag-replace" in dl_src
    or "Tag write recovered via copy" in dl_src,
)
_check(
    "downloader counts tag-fallback events",
    "_XFER_COUNTERS_TAGFALLBACK" in dl_src,
)
_check(
    "downloader counts tag-fail events",
    "_XFER_COUNTERS_TAGFAIL" in dl_src,
)
_check(
    "downloader applies recovery to BOTH per-track and album-wise paths",
    dl_src.count("copy_to_writable_temp") >= 2,
)


# ─── 3. P2: router degraded-mode recovery ───────────────────────────────
print("\n[3] P2: _pick_storage_for_file recovery sequence")

# Need to stub pythoncom etc. before importing adb_transfer (which is
# already imported in test_rev1491; here we just use it directly)
import adb_transfer

_check(
    "_attempt_free_space_recovery exists",
    callable(getattr(adb_transfer, "_attempt_free_space_recovery", None)),
)
_check(
    "_reset_both_unknown_state exists",
    callable(getattr(adb_transfer, "_reset_both_unknown_state", None)),
)

# Reset session flags
adb_transfer._reset_both_unknown_state()

# Scenario: both unknown → recovery attempted → still unknown → degraded mode
import config as _cfg
_orig_w = _cfg.WRITE_STORAGE
_orig_s = _cfg.SD_STORAGE
_cfg.WRITE_STORAGE = "Internal shared storage"
_cfg.SD_STORAGE = "SD card"

try:
    # Mock _cached_free_space to always return -1 (unknown)
    with patch.object(adb_transfer, "_cached_free_space", return_value=-1), \
         patch.object(adb_transfer, "_adb_cmd", return_value=""):
        # First call: triggers recovery (warn + recovery + degraded warn)
        # The recovery itself returns -1 too, so degraded-mode kicks in
        result1 = adb_transfer._pick_storage_for_file(1024 * 1024, "test1.mp3")
        _check("first both-unknown call returns primary (degraded)",
               result1 == "Internal shared storage")

        # Second call: should NOT log degraded again — we capture log output
        import io, logging
        cap = io.StringIO()
        h = logging.StreamHandler(cap)
        h.setLevel(logging.WARNING)
        logging.getLogger("adb_transfer").addHandler(h)

        result2 = adb_transfer._pick_storage_for_file(1024 * 1024, "test2.mp3")
        _check("second both-unknown call also returns primary",
               result2 == "Internal shared storage")
        log_text = cap.getvalue()
        # The degraded warning should NOT appear again on the second call
        _check("degraded-mode warning fires only ONCE per session",
               "DEGRADED MODE" not in log_text,
               f"log_text contained DEGRADED MODE on second call")
        logging.getLogger("adb_transfer").removeHandler(h)

    # Reset for the next scenario
    adb_transfer._reset_both_unknown_state()

    # Scenario: recovery succeeds (free-space comes back healthy after retry)
    call_count = [0]
    def staged_free(storage):
        call_count[0] += 1
        if call_count[0] <= 2:  # initial query for both = -1
            return -1
        else:  # after recovery, returns healthy
            return 100 * 1024**3

    with patch.object(adb_transfer, "_cached_free_space", side_effect=staged_free), \
         patch.object(adb_transfer, "_adb_cmd", return_value=""):
        result3 = adb_transfer._pick_storage_for_file(1024 * 1024, "test3.mp3")
        _check("recovery succeeds → returns primary normally",
               result3 == "Internal shared storage")
finally:
    _cfg.WRITE_STORAGE = _orig_w
    _cfg.SD_STORAGE = _orig_s


# ─── 4. P3: device_index self-heal flag ─────────────────────────────────
print("\n[4] P3: device_index session-disabled flag")

import device_index

di = device_index.DeviceIndex(root="/storage/emulated/0/Music")
_check("DeviceIndex has _session_disabled field", hasattr(di, "_session_disabled"))
_check("DeviceIndex has is_session_disabled() method",
       callable(getattr(di, "is_session_disabled", None)))
_check("Initial state: _session_disabled=False", di.is_session_disabled() == False)

# Source-level checks for the recovery path
with open(os.path.join(os.path.dirname(__file__), "device_index.py"),
          "r", encoding="utf-8") as f:
    di_src = f.read()
_check("refresh has 3-attempt loop", "for attempt in (1, 2, 3)" in di_src)
_check("refresh restarts adb between attempts 2 and 3",
       "kill-server" in di_src and "start-server" in di_src)
_check("refresh has shallow-fallback path",
       "Shallow fallback" in di_src or "max-depth 1" in di_src)
_check("refresh sets _session_disabled on persistent failure",
       "self._session_disabled = True" in di_src)


# ─── 5. P4: COM verification parity ─────────────────────────────────────
print("\n[5] P4: COM verification parity with ADB")

# Read source to verify wiring (can't import mtp_transfer_com on Linux)
with open(os.path.join(os.path.dirname(__file__), "mtp_transfer_com.py"),
          "r", encoding="utf-8") as f:
    com_src = f.read()

_check("_verify_com_copy function defined",
       "def _verify_com_copy(" in com_src)
_check("_find_remote_item function defined",
       "def _find_remote_item(" in com_src)
_check("_delete_remote_after_failed_verify function defined",
       "def _delete_remote_after_failed_verify(" in com_src)
_check("verification compares item.Size to expected_size",
       "expected_size" in com_src and "item.Size" in com_src)
_check("verification triggers AFTER copy completes",
       # 2026-07-20: was an exact-string match ending right after
       # expected_size; loosened to a prefix check so it survives the
       # addition of progress_cb= (subprocess-isolation heartbeat fix —
       # this call had none, unlike _navigate/_wait_for_stable_size, and a
       # real live transfer's verification step hung as a result) without
       # re-breaking on the next added kwarg either.
       # 2026-07-22: call site changed from a single cached target_folder
       # to (device_name, storage_name, dest_parts) so the folder can be
       # re-navigated fresh on every retry (a folder object fetched once
       # before the transfer was found live to report stale size=0 for
       # 90+s after a transfer had already genuinely succeeded, confirmed
       # via ADB) — updated to a prefix match on the new call shape.
       "_verify_com_copy(\n            device_name, storage_name, dest_parts, safe_name, expected_size"
       in com_src)
_check("verification surfaces real failure (raises RuntimeError)",
       "Post-COM-transfer verification failed" in com_src)
_check("verification deletes corrupt remote on failure",
       "_delete_remote_after_failed_verify" in com_src
       and "Removing corrupt remote file" in com_src)
_check("verification respects VERIFY_TRANSFERS_ENABLED config flag",
       "VERIFY_TRANSFERS_ENABLED" in com_src)


# ─── 6. P5: run-level health summary ────────────────────────────────────
print("\n[6] P5: run-level health summary")

# Source-level checks since downloader can't be imported on Linux
_check("downloader has _XFER_COUNTERS_TAGFAIL", "_XFER_COUNTERS_TAGFAIL" in dl_src)
_check("downloader has _XFER_COUNTERS_TAGFALLBACK", "_XFER_COUNTERS_TAGFALLBACK" in dl_src)
_check("downloader has _XFER_COUNTERS_UNKNOWN_FREE", "_XFER_COUNTERS_UNKNOWN_FREE" in dl_src)
_check("downloader has _XFER_COUNTERS_VERIFY_FAIL", "_XFER_COUNTERS_VERIFY_FAIL" in dl_src)
_check("downloader has _XFER_COUNTERS_COM_FALLBACK", "_XFER_COUNTERS_COM_FALLBACK" in dl_src)
_check("downloader has _XFER_COUNTERS_PROVIDER_EXHAUST", "_XFER_COUNTERS_PROVIDER_EXHAUST" in dl_src)
_check("downloader has _health_reset() helper", "def _health_reset()" in dl_src)
_check("downloader has _health_summary() helper", "def _health_summary()" in dl_src)
_check("run_downloads calls _health_reset at start", "_health_reset()" in dl_src)

# walkman_sync emits the verdict
with open(os.path.join(os.path.dirname(__file__), "walkman_sync.py"),
          "r", encoding="utf-8") as f:
    ws_src = f.read()
_check("walkman_sync emits Stage 0 health verdict",
       "Stage 0 verdict" in ws_src)
_check("walkman_sync has 3 verdict tiers (CLEAN / WARNINGS / ATTENTION)",
       "CLEAN" in ws_src and "WARNINGS" in ws_src and "ATTENTION" in ws_src)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"MK17 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
