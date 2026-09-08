"""
test_mk17_p6.py — MK17 Patch 6 regression tests
=================================================
Source-level checks for the LOG_16_2 fix in walkman_reorganize.py.

LOG_16_2 (May 1, 01:13–01:16) captured a cascading failure where the
reorganizer's [Move] step was passing the destination FILENAME as part
of the destination FOLDER, causing adb_transfer to construct paths like
    /storage/.../Music/Don Toliver/Love Sick/<filename>.mp3/<filename>.mp3
ADB rejected each push with "Not a directory" because the first
<filename>.mp3 component was a file, not a directory.  COM fallback hit
the same root cause and reported "Created folder X but it did not
appear within 30s".  96 transfers failed in roughly an hour before
LOG_16_2 was uploaded, with no recovery in sight.

Two things needed fixing in walkman_reorganize.py's _move() function:
    1. dest_parts MUST be the folder only (no filename appended).
    2. The pre-existing safe_name rename was redundant (adb_transfer
       does its own ASCII-safety rename internally), and produced
       unicode filenames with `_` substituted on the device.
"""
from __future__ import annotations
import os, sys, pathlib

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


# Read source — these are textual checks.  walkman_reorganize.py imports
# pythoncom at module load on Linux, so we cannot import it directly.
WR = pathlib.Path(__file__).parent / "walkman_reorganize.py"
src = WR.read_text(encoding="utf-8")

print("\n[1] dest_parts no longer carries the filename")
# Pre-fix: `dest_parts   = dest_folder_parts + [dest_filename],`
# Post-fix: `dest_parts   = dest_folder_parts,`
_check(
    "dest_folder_parts + [dest_filename] is gone (as a call argument)",
    "dest_parts   = dest_folder_parts + [dest_filename]" not in src
    and "dest_parts = dest_folder_parts + [dest_filename]" not in src
    and "dest_parts=dest_folder_parts+[dest_filename]" not in src,
    "old contract-violating call argument still present",
)
_check(
    "dest_parts is now bare dest_folder_parts",
    "dest_parts   = dest_folder_parts,\n" in src
    or "dest_parts=dest_folder_parts," in src
    or "dest_parts = dest_folder_parts," in src,
    "expected `dest_parts = dest_folder_parts,` not found",
)
_check(
    "MK17 Patch 6 marker present",
    "MK17 Patch 6" in src,
    "patch comment marker missing",
)
_check(
    "comment names the LOG_16_2 root cause",
    "LOG_16_2" in src and "Not a directory" in src,
    "patch documentation incomplete",
)


print("\n[2] redundant ASCII-safe rename removed")
# Pre-fix: safe_name = re.sub(r"[^\x00-\x7F]", "_", dest_filename)
# Post-fix: safe_path = tmp_dir / dest_filename (no rename of bytes)
_check(
    "old `re.sub(r\"[^\\x00-\\x7F]\", \"_\", dest_filename)` is gone",
    'r"[^\\x00-\\x7F]"' not in src,
    "redundant ASCII rename still present",
)
_check(
    "safe_path now uses dest_filename directly",
    "safe_path = tmp_dir / dest_filename" in src,
    "expected `safe_path = tmp_dir / dest_filename` not found",
)


print("\n[3] adb_transfer still does its own ASCII-safety internally")
ADB = pathlib.Path(__file__).parent / "adb_transfer.py"
adb_src = ADB.read_text(encoding="utf-8")
_check(
    "adb_transfer.copy_file_to_device handles ASCII-safety internally",
    'safe_name = re.sub' in adb_src,
    "transport-layer rename should still exist",
)


print("\n[4] dest_parts contract documented unchanged")
_check(
    "adb_transfer documents `dest_parts is the folder` contract",
    "DESTINATION FOLDER" in adb_src
    or ("dest_parts" in adb_src and "no filename" in adb_src.lower()),
    "transport-layer contract documentation is missing",
)


print(f"\n{'=' * 50}")
print(f"MK17 Patch 6 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
