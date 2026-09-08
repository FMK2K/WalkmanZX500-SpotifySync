"""
test_rev1493.py — Rev 14.9.3 regression tests
==============================================
Exercises the fixes added in Rev 14.9.3:
  * F1: track_number sequential allocation (no more all-tracks-as-01)
  * F2: cover-art module imports cleanly (full-pipeline test requires net)
  * F5: MIN_FREE_SPACE_GB floor produces below_floor halt result
  * F6: _track_filename refuses empty title
"""
from __future__ import annotations
import sys
import os

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


# ─── 1. F1: sequential track-number allocation ──────────────────────────
print("\n[1] F1: _allocate_sequential_track_numbers")
from spotify_local_data import _allocate_sequential_track_numbers

# Scenario A: three tracks from same album, no track_numbers → 1, 2, 3
tracks = [
    {"album_artist": "Dan Mason", "album": "Miami Virtual", "title": "I Can't Take the"},
    {"album_artist": "Dan Mason", "album": "Miami Virtual", "title": "Make Me Love You"},
    {"album_artist": "Dan Mason", "album": "Miami Virtual", "title": "Waiting for You"},
]
_allocate_sequential_track_numbers(tracks)
_check("track 1 gets number '1'", tracks[0]["track_number"] == "1")
_check("track 2 gets number '2'", tracks[1]["track_number"] == "2")
_check("track 3 gets number '3'", tracks[2]["track_number"] == "3")
_check("disc_number defaults to '1'", all(t["disc_number"] == "1" for t in tracks))

# Scenario B: tracks from DIFFERENT albums get independent counters
tracks = [
    {"album_artist": "A", "album": "Album1", "title": "X"},
    {"album_artist": "A", "album": "Album2", "title": "Y"},
    {"album_artist": "A", "album": "Album1", "title": "Z"},
]
_allocate_sequential_track_numbers(tracks)
_check("Album1 first track is '1'", tracks[0]["track_number"] == "1")
_check("Album2 first track is '1' (separate counter)", tracks[1]["track_number"] == "1")
_check("Album1 second track is '2'", tracks[2]["track_number"] == "2")

# Scenario C: existing track_number is NOT overwritten
tracks = [
    {"album_artist": "A", "album": "X", "title": "T1", "track_number": "5"},
    {"album_artist": "A", "album": "X", "title": "T2"},
]
_allocate_sequential_track_numbers(tracks)
_check("existing track_number preserved", tracks[0]["track_number"] == "5")
_check("missing track_number gets allocated", tracks[1]["track_number"] == "1")

# Scenario D: case-insensitive album matching (avoid double-counting)
tracks = [
    {"album_artist": "Dan Mason", "album": "Miami Virtual", "title": "T1"},
    {"album_artist": "DAN MASON", "album": "miami virtual", "title": "T2"},
]
_allocate_sequential_track_numbers(tracks)
_check("case-insensitive album match", tracks[1]["track_number"] == "2",
       f"got {tracks[1]['track_number']!r}")


# ─── 2. F5: MIN_FREE_SPACE_GB floor ──────────────────────────────────────
# Test the floor logic directly without invoking the live device call.
# Build the result dict manually and verify the structure.
print("\n[2] F5: free-space floor")
from unittest.mock import patch
import config
import free_space_check

# Force a known floor for the test
_orig_floor = getattr(config, "MIN_FREE_SPACE_GB", 0.5)
config.MIN_FREE_SPACE_GB = 0.5

# Scenario A: device has plenty of space (50 GB free, 1 GB needed)
with patch("free_space_check.get_device_free_bytes",
           return_value=50 * 1024**3):
    result = free_space_check.check_free_space(1024 * 1024 * 1024)  # 1 GB
    _check("plenty of space → ok=True", result["ok"] == True)
    _check("plenty of space → below_floor=False", result["below_floor"] == False)

# Scenario B: device exactly at floor (0.5 GB free, small download)
with patch("free_space_check.get_device_free_bytes",
           return_value=int(0.5 * 1024**3)):
    result = free_space_check.check_free_space(100 * 1024 * 1024)  # 100 MB
    _check("at floor → not below_floor", result["below_floor"] == False)

# Scenario C: device below floor (5 MB free, sync should HALT)
with patch("free_space_check.get_device_free_bytes",
           return_value=5 * 1024 * 1024):  # 5 MB
    result = free_space_check.check_free_space(1024 * 1024 * 1024)  # 1 GB
    _check("below floor → ok=False", result["ok"] == False)
    _check("below floor → below_floor=True", result["below_floor"] == True)

# Scenario D: unknown free space → ok=True, below_floor=False
with patch("free_space_check.get_device_free_bytes", return_value=None):
    result = free_space_check.check_free_space(1024 * 1024 * 1024)
    _check("unknown free → ok=True", result["ok"] == True)
    _check("unknown free → below_floor=False", result["below_floor"] == False)

config.MIN_FREE_SPACE_GB = _orig_floor


# ─── 3. F2: cover-art module loads ──────────────────────────────────────
print("\n[3] F2: cover-art module loads cleanly")
try:
    import cover_art
    _check("cover_art importable", True)
    _check("normalize_bytes is callable", callable(cover_art.normalize_bytes))
    _check("build_apic is callable", callable(cover_art.build_apic))
    _check("build_flac_picture is callable", callable(cover_art.build_flac_picture))
except Exception as e:
    _check("cover_art importable", False, str(e))


# ─── 4. F6: _track_filename refuses empty title ─────────────────────────
# (deferred: requires downloader.py import which exits on Linux due to mtp)
print("\n[4] F6: _track_filename refuses empty title — skipped (requires Windows env)")


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"Rev 14.9.3 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
