"""
test_rev1494.py — Rev 14.9.4 regression tests
==============================================
Exercises the additional fixes added on top of Rev 14.9.3:

  * D1: dual-floor check (internal monitored even when WRITE_STORAGE=SD)
  * D2: mid-run advisory free-space watchdog
  * D5: yt-dlp title junk-stripper (_clean_title)
  * D6: master-artist enforcement (in-loop mutation)
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


# ─── 1. D5: title junk-stripping ────────────────────────────────────────
print("\n[1] D5: _clean_title")
# Import via re module-level since downloader's full import is expensive.
# Pull the function and patterns from the source directly.
import re
ns = {"re": re}
# Read source and exec just the D5 region
with open(os.path.join(os.path.dirname(__file__), "downloader.py"), "r", encoding="utf-8") as f:
    src = f.read()
# Take from the keyword constant through end of _clean_title
start_marker = "_TITLE_JUNK_KEYWORDS = ("
start = src.find(start_marker)
end = src.find("def _convert_raw_aac_to_mp3", start)
if start < 0 or end < 0:
    _check("D5 source extraction", False,
           f"start={start}, end={end} — markers moved")
else:
    exec(src[start:end], ns)
    _clean_title = ns["_clean_title"]

# Spotify clean titles must pass through unchanged
for clean in [
    "Daydreaming",
    "Sandman",
    "Run the World (Girls)",  # genuine title parens — must NOT strip
    "Live at the Royal Albert Hall",  # words inside parens (no parens here)
]:
    result = _clean_title(clean)
    _check(f"clean title untouched: {clean!r}", result == clean,
           f"got {result!r}")

# (2022 Remaster) is junk — should strip
_check(
    "(2022 Remaster) is junk",
    _clean_title("Down In A Hole (2022 Remaster)") == "Down In A Hole",
    f"got {_clean_title('Down In A Hole (2022 Remaster)')!r}",
)

# yt-dlp junk stripped
junk_cases = [
    ("Surf or Not (Official Video)", "Surf or Not"),
    ("Daydreaming [HD 1080p]", "Daydreaming"),
    ("Track Name (Official Music Video)", "Track Name"),
    ("Track Name [Lyric Video]", "Track Name"),
    ("Track Name (Audio)", "Track Name"),
    ("Song Title - Official Video", "Song Title"),
    ("Song Title (Explicit)", "Song Title"),
    ("Song (2019)", "Song"),  # bare year
    ("Song [HQ Audio]", "Song"),
]
for raw, expected in junk_cases:
    result = _clean_title(raw)
    _check(f"strips junk: {raw!r} -> {expected!r}",
           result == expected, f"got {result!r}")

# Edge: empty / None input
_check("empty input -> empty", _clean_title("") == "")
_check("None input -> None", _clean_title(None) is None)

# Idempotent
once = _clean_title("Surf or Not (Official Video)")
twice = _clean_title(once)
_check("idempotent", once == twice)


# ─── 2. D1: free_space_check returns internal_available_bytes ─────────
print("\n[2] D1: dual-floor probe in check_free_space")
from unittest.mock import patch
import config
import free_space_check

# Restore floor
config.MIN_FREE_SPACE_GB = 0.5
config.WRITE_STORAGE = "SD card"  # internal NOT being written to

# Scenario A: SD healthy, internal healthy → result OK + internal_available filled
def _fake_get(hint=None):
    if hint == "/storage/emulated":
        return 5 * 1024**3  # 5 GB internal free
    return 100 * 1024**3  # 100 GB SD free

with patch("free_space_check.get_device_free_bytes", side_effect=_fake_get):
    result = free_space_check.check_free_space(1024 * 1024 * 1024)
    _check("internal_available_bytes returned",
           "internal_available_bytes" in result)
    _check("SD healthy → ok=True",
           result["ok"] == True)
    _check("internal value populated",
           result["internal_available_bytes"] == 5 * 1024**3,
           f"got {result.get('internal_available_bytes')!r}")

# Scenario B: SD healthy, internal LOW (advisory only, do not halt)
def _fake_get_low_internal(hint=None):
    if hint == "/storage/emulated":
        return int(0.1 * 1024**3)  # 100 MB internal — below 0.5 GB floor
    return 100 * 1024**3  # SD fine

with patch("free_space_check.get_device_free_bytes", side_effect=_fake_get_low_internal):
    result = free_space_check.check_free_space(1024 * 1024 * 1024)
    _check("internal low + SD healthy → still ok (advisory only)",
           result["ok"] == True)
    _check("internal low → below_floor stays False (we only halt on WRITE_STORAGE)",
           result["below_floor"] == False)
    _check("internal_available reflected as low",
           result["internal_available_bytes"] < int(0.5 * 1024**3))


# ─── 3. D2: midrun_floor_check ──────────────────────────────────────────
print("\n[3] D2: midrun_floor_check")

with patch("free_space_check.get_device_free_bytes",
           return_value=10 * 1024**3):  # 10 GB free, plenty
    result = free_space_check.midrun_floor_check()
    _check("plenty free → ok=True", result["ok"] == True)

with patch("free_space_check.get_device_free_bytes",
           return_value=int(0.1 * 1024**3)):  # 100 MB
    result = free_space_check.midrun_floor_check()
    _check("below floor → ok=False", result["ok"] == False)
    _check("below floor message mentions floor",
           "floor" in result["message"].lower())

with patch("free_space_check.get_device_free_bytes", return_value=None):
    result = free_space_check.midrun_floor_check()
    _check("unknown free → ok=True (don't halt on unknown)",
           result["ok"] == True)


# ─── 4. D6: per-track loop master-artist enforcement ────────────────────
# We can't import downloader without pulling in mtp_transfer which exits on
# Linux.  Instead, test the LOGIC by reading the source and checking the
# mutation pattern is present.
print("\n[4] D6: master-artist enforcement (source-level check)")
with open(os.path.join(os.path.dirname(__file__), "downloader.py"), "r", encoding="utf-8") as f:
    dl_src = f.read()
_check(
    "canonical_album_artist_for_per_track variable defined in per-track loop",
    "canonical_album_artist_for_per_track" in dl_src
)
_check(
    "track['album_artist'] mutated in per-track loop",
    'track["album_artist"] = canonical_album_artist_for_per_track' in dl_src
)
_check(
    "D6 marker present",
    "D6 — Master Artist enforcement" in dl_src
    or "D6 — overwrite album_artist" in dl_src
)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"Rev 14.9.4 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
