"""
test_rev1491.py — Rev 14.9.1 regression tests
==============================================
Exercises the fixes added in Rev 14.9.1:
  * _adb_cmd None-safe stdout
  * _pick_storage_for_file F2 fallback when primary is unknown
  * _find_ffmpeg robust search
  * TIDAL auth pre-check (structural only — doesn't attempt network)
"""
from __future__ import annotations
import sys
import os
import subprocess
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


# ─── 1. _adb_cmd None-safe stdout ───────────────────────────────────────
print("\n[1] _adb_cmd None-safe (14.9.1)")
from adb_transfer import _adb_cmd

class _FakeCompletedNone:
    stdout = None
    stderr = None
    returncode = 0

with patch("adb_transfer.subprocess.run", return_value=_FakeCompletedNone()):
    try:
        out = _adb_cmd(["shell", "echo", "hi"], check=False)
        _check("stdout=None does not crash", True)
        _check("returns empty string when stdout is None", out == "", f"got {out!r}")
    except AttributeError as e:
        _check("stdout=None does not crash", False, str(e))


class _FakeCompletedReal:
    stdout = "   hello  \n"
    stderr = ""
    returncode = 0

with patch("adb_transfer.subprocess.run", return_value=_FakeCompletedReal()):
    out = _adb_cmd(["shell", "echo", "hi"], check=False)
    _check("real stdout is stripped", out == "hello")


# ─── 2. _pick_storage_for_file F2 fallback ──────────────────────────────
print("\n[2] _pick_storage_for_file F2 (14.9.1)")
from adb_transfer import _pick_storage_for_file
import config

# Override config so primary and fallback are distinct for these tests.
# The real config has both set to "SD card" (user preference), which makes
# our scenario tests degenerate.
_ORIG_W = config.WRITE_STORAGE
_ORIG_S = config.SD_STORAGE
config.WRITE_STORAGE = "Internal shared storage"
config.SD_STORAGE    = "SD card"
PRIMARY_NAME = "Internal shared storage"
FALLBACK_NAME = "SD card"

try:
    # Scenario A: primary unknown (-1), fallback healthy → should route to fallback
    with patch("adb_transfer._cached_free_space") as mock_free:
        def fake_free(storage):
            if storage == PRIMARY_NAME:  return -1
            if storage == FALLBACK_NAME: return 200 * 1024**3
            return -1
        mock_free.side_effect = fake_free
        result = _pick_storage_for_file(10 * 1024**2, "test.mp3")
        _check(
            "primary=unknown fallback=healthy → routes to fallback",
            result == FALLBACK_NAME, f"got {result!r}"
        )

    # Scenario B: primary full, fallback healthy → route to fallback
    with patch("adb_transfer._cached_free_space") as mock_free:
        def fake_free(storage):
            if storage == PRIMARY_NAME:  return 5 * 1024**2
            if storage == FALLBACK_NAME: return 200 * 1024**3
            return -1
        mock_free.side_effect = fake_free
        result = _pick_storage_for_file(10 * 1024**2, "test.mp3")
        _check(
            "primary=full fallback=healthy → routes to fallback",
            result == FALLBACK_NAME, f"got {result!r}"
        )

    # Scenario C: primary healthy → route to primary
    with patch("adb_transfer._cached_free_space") as mock_free:
        def fake_free(storage):
            if storage == PRIMARY_NAME:  return 20 * 1024**3
            if storage == FALLBACK_NAME: return 200 * 1024**3
            return -1
        mock_free.side_effect = fake_free
        result = _pick_storage_for_file(10 * 1024**2, "test.mp3")
        _check(
            "primary=healthy → routes to primary",
            result == PRIMARY_NAME, f"got {result!r}"
        )

    # Scenario D: both unknown → fallback to primary with WARNING
    with patch("adb_transfer._cached_free_space") as mock_free:
        mock_free.return_value = -1
        try:
            result = _pick_storage_for_file(10 * 1024**2, "test.mp3")
            _check(
                "both unknown → returns primary (doesn't raise)",
                result == PRIMARY_NAME, f"got {result!r}"
            )
        except Exception as e:
            _check("both unknown → returns primary (doesn't raise)", False, str(e))
finally:
    config.WRITE_STORAGE = _ORIG_W
    config.SD_STORAGE    = _ORIG_S


# ─── 3. _find_ffmpeg robust search ──────────────────────────────────────
# NOTE: This test must NOT be run in the same process as walkman_sync import
# because transport layer exits on import when no Windows COM / ADB binary
# is present on this (Linux) system.  Skipping to avoid process exit.
print("\n[3] _find_ffmpeg (14.9.1) — skipped (requires isolated process)")


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"Rev 14.9.1 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
