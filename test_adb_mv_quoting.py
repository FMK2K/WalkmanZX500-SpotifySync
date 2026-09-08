"""
test_adb_mv_quoting.py — isolated verification that rename_file_on_device,
rename_folder_on_device, and _adb_rename_mojibake_in_folder's mv call all
quote paths before sending them to `adb shell`, 2026-07-27.

adb shell joins trailing argv with a single space before it reaches the
device's own shell, so `_adb_cmd(["shell", "mv", old_path, new_path])`
word-splits any path containing a space — nearly every track/album/artist
name in this library. This project already has a documented fix for this
exact gotcha (adb_transfer._shell_quote, used by delete/mkdir call sites);
these three mv call sites were the same bug, just missed. No device
required — adb_transfer._adb_cmd is mocked.
"""
from unittest import mock

import adb_transfer as adb

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


print("[1] rename_file_on_device quotes both old and new paths via sh -c")
with mock.patch("adb_transfer._get_storage_path", return_value="/storage/emulated/0"), \
     mock.patch("adb_transfer._adb_cmd", return_value="") as mock_cmd:
    ok = adb.rename_file_on_device(
        "dev", "Internal shared storage", ["Music", "The Weeknd"],
        "old (bad).mp3", "new (fixed).mp3",
    )
    _check("returns True on success", ok is True)
    args = mock_cmd.call_args[0][0]
    _check("routed through sh -c", args[:3] == ["shell", "sh", "-c"])
    _check("old path quoted", "'/storage/emulated/0/Music/The Weeknd/old (bad).mp3'" in args[3])
    _check("new path quoted", "'/storage/emulated/0/Music/The Weeknd/new (fixed).mp3'" in args[3])

print("\n[2] rename_folder_on_device quotes both old and new paths via sh -c")
with mock.patch("adb_transfer._get_storage_path", return_value="/storage/emulated/0"), \
     mock.patch("adb_transfer._adb_cmd", return_value="") as mock_cmd:
    ok = adb.rename_folder_on_device(
        "dev", "Internal shared storage", ["Music"],
        "Old Folder (v1)", "New Folder (v2)",
    )
    _check("returns True on success", ok is True)
    args = mock_cmd.call_args[0][0]
    _check("routed through sh -c", args[:3] == ["shell", "sh", "-c"])
    _check("old path quoted", "'/storage/emulated/0/Music/Old Folder (v1)'" in args[3])
    _check("new path quoted", "'/storage/emulated/0/Music/New Folder (v2)'" in args[3])

print("\n[3] _adb_rename_mojibake_in_folder quotes ls target and mv paths via sh -c")
calls = []
_ls_done = {"n": False}


def _fake_adb_cmd(args, **kwargs):
    calls.append(args)
    joined = " ".join(args)
    if joined.startswith("shell sh -c ls -1"):
        if _ls_done["n"]:
            return ""
        _ls_done["n"] = True
        return "AminÃ©"
    if "test -d" in joined:
        return "1"
    return ""


with mock.patch("adb_transfer._adb_cmd", side_effect=_fake_adb_cmd):
    adb._adb_rename_mojibake_in_folder("/storage/emulated/0/Music/Weird Artist")

ls_calls = [c for c in calls if c[:3] == ["shell", "sh", "-c"] and c[3].startswith("ls -1")]
mv_calls = [c for c in calls if c[:3] == ["shell", "sh", "-c"] and c[3].startswith("mv ")]
_check("ls call routed through sh -c with quoted dir", len(ls_calls) >= 1 and
       "'/storage/emulated/0/Music/Weird Artist'" in ls_calls[0][3])
_check("mv call present and both paths quoted",
       len(mv_calls) == 1 and
       "'/storage/emulated/0/Music/Weird Artist/AminÃ©'" in mv_calls[0][3])

print(f"\n{'=' * 60}")
print(f"adb mv/ls quoting: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
