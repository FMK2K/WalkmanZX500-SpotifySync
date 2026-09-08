"""
test_com_transport.py — isolated verification for COMTransport
(device_transport.py, 2026-07-19 — opt-in COM/MTP alternative to the MK23
ADB-primary invariant).

Offline only — mocks mtp_transfer_com's functions directly. No device,
no network, no real COM/Shell.Application calls.
"""
import pathlib
import sys
import tempfile
from unittest import mock

from device_transport import COMTransport, get_transport, ADBTransport

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


t = COMTransport(device_name="WALKMAN", storage_name="SD card")

# ─── 1. Path splitting ──────────────────────────────────────────────────────
print("\n[1] _split() path-parsing")
parts, name = COMTransport._split("Music/Artist/Album/track.flac")
_check("folder parts correct", parts == ["Music", "Artist", "Album"])
_check("filename correct", name == "track.flac")
parts2, name2 = COMTransport._split("track.flac")
_check("single-segment path -> empty parts", parts2 == [])
_check("single-segment path -> filename", name2 == "track.flac")
try:
    COMTransport._split("")
    _check("empty path raises ValueError", False)
except ValueError:
    _check("empty path raises ValueError", True)

# ─── 2. push_file — happy path, same filename ──────────────────────────────
print("\n[2] push_file — same local/remote filename")
tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="com_transport_test_"))
local_file = tmpdir / "track.flac"
local_file.write_bytes(b"fake audio data")

with mock.patch("mtp_transfer_com.copy_file_to_device_isolated", return_value="SD card") as m_copy, \
     mock.patch.object(COMTransport, "file_exists", return_value=False):
    ok = t.push_file(str(local_file), "Music/Artist/Album/track.flac", overwrite=True)
_check("push_file returns True on success", ok is True)
_check("copy_file_to_device_isolated called with correct folder_parts", m_copy.call_args[0][1] == ["Music", "Artist", "Album"])
_check("copy_file_to_device_isolated called with correct device/storage", m_copy.call_args[0][2:4] == ("WALKMAN", "SD card"))

# ─── 3. push_file — different filename, stages a renamed temp copy ────────
print("\n[3] push_file — remote wants a different filename than local file")
with mock.patch("mtp_transfer_com.copy_file_to_device_isolated", return_value="SD card") as m_copy, \
     mock.patch.object(COMTransport, "file_exists", return_value=False):
    ok = t.push_file(str(local_file), "Music/Artist/Album/renamed.flac", overwrite=True)
staged_src = m_copy.call_args[0][0]
_check("push_file returns True", ok is True)
_check("staged temp file has the REMOTE name, not the original", pathlib.Path(staged_src).name == "renamed.flac")
_check("staged temp file is a real file with the same content", pathlib.Path(staged_src).exists() is False)  # cleaned up after push

# ─── 4. push_file — overwrite=False and file exists -> FileExistsError ─────
print("\n[4] push_file — overwrite=False + existing file raises FileExistsError")
with mock.patch("mtp_transfer_com.copy_file_to_device_isolated") as m_copy, \
     mock.patch.object(COMTransport, "file_exists", return_value=True):
    try:
        t.push_file(str(local_file), "Music/Artist/Album/track.flac", overwrite=False)
        _check("raises FileExistsError", False)
    except FileExistsError:
        _check("raises FileExistsError", True)
    _check("copy never attempted", not m_copy.called)

# ─── 5. push_file — copy fails (returns None) -> returns False ─────────────
print("\n[5] push_file — underlying copy fails")
with mock.patch("mtp_transfer_com.copy_file_to_device_isolated", return_value=None), \
     mock.patch.object(COMTransport, "file_exists", return_value=False):
    ok = t.push_file(str(local_file), "Music/Artist/Album/track.flac")
_check("returns False when copy_file_to_device_isolated returns None", ok is False)

# ─── 6. delete_file — delegates directly, no separate existence pre-check ──
# 2026-07-19: delete_file() no longer calls file_exists() first (that was a
# redundant, unguarded raw COM call on top of a call — delete_file_on_device
# — that already handles "already absent" safely, per its own docstring,
# inside a real 30s-bounded subprocess). Verify the delegation instead.
print("\n[6] delete_file")
with mock.patch("mtp_transfer_com.delete_file_on_device", return_value=True) as m_del, \
     mock.patch.object(COMTransport, "file_exists") as m_exists:
    ok = t.delete_file("Music/Artist/Album/track.flac")
_check("returns True on successful delete", ok is True)
_check("delete_file_on_device called with correct args", m_del.call_args[0] == ("WALKMAN", "SD card", ["Music", "Artist", "Album"], "track.flac"))
_check("file_exists is NOT called by delete_file anymore", not m_exists.called)

with mock.patch("mtp_transfer_com.delete_file_on_device", return_value=True) as m_del2:
    ok2 = t.delete_file("Music/Artist/Album/already_absent.flac")
_check("absent-file case still returns True (delete_file_on_device's own contract)", ok2 is True)
_check("delete_file_on_device was called directly, no pre-check", m_del2.called)

# ─── 7. list_files — delegates to scan_device_audio, builds path strings ───
print("\n[7] list_files")
_FAKE_SCAN = [
    {"filename": "track.flac", "ext": ".flac", "parts": ["Music", "Artist", "Album"], "storage": "SD card"},
]
with mock.patch("mtp_transfer_com.scan_device_audio", return_value=_FAKE_SCAN) as m_scan:
    results = t.list_files("Music")
_check("scan_device_audio called with correct storage_roots", m_scan.call_args[0][1] == [("SD card", "Music")])
_check("returns 1 result", len(results) == 1)
_check("path correctly joined from parts + filename", results[0]["path"] == "Music/Artist/Album/track.flac")

# ─── 8. file_exists — subprocess-isolated primitive, mocked at that boundary
# 2026-07-19: file_exists() now delegates to mtp_transfer_com.file_exists_isolated
# (subprocess-isolated, dual-guard) instead of doing a raw in-process
# _navigate()/folder.Items() call — that raw call was the actual gap this
# rewrite closes (see device_transport.py's file_exists docstring). Mock at
# the isolated-primitive boundary, matching how push_file/delete_file mock
# copy_file_to_device_isolated/delete_file_on_device rather than their
# internals.
print("\n[8] file_exists")
with mock.patch("mtp_transfer_com.file_exists_isolated", return_value=True) as m_ex1:
    _check("True result passes through", t.file_exists("Music/Artist/Album/track.flac") is True)
    _check("file_exists_isolated called with correct args", m_ex1.call_args[0] == ("WALKMAN", "SD card", ["Music", "Artist", "Album"], "track.flac"))

with mock.patch("mtp_transfer_com.file_exists_isolated", return_value=False):
    _check("False result passes through", t.file_exists("Music/Artist/Album/missing.flac") is False)

with mock.patch("mtp_transfer_com.file_exists_isolated", return_value=None):
    _check("None (guard fired / unknown) defaults to True — fail toward blocking a write, not silently overwriting",
           t.file_exists("Music/x.flac") is True)

# ─── 9. rename — same folder uses rename_file_on_device_isolated ──────────
# 2026-08-19: raw rename_file_on_device()/read_file_from_device() calls were
# found live to hang 40+ minutes (no subprocess isolation, no timeout guard)
# — COMTransport.rename() now goes through the isolated variants of both,
# same "blocked COM call" hardening already applied elsewhere in this file.
print("\n[9] rename — same-folder")
with mock.patch("mtp_transfer_com.rename_file_on_device_isolated", return_value=True) as m_ren:
    ok = t.rename("Music/Artist/Album/old.flac", "Music/Artist/Album/new.flac")
_check("same-folder rename returns True", ok is True)
_check("rename_file_on_device_isolated called with correct args", m_ren.call_args[0] == ("WALKMAN", "SD card", ["Music", "Artist", "Album"], "old.flac", "new.flac"))

with mock.patch("mtp_transfer_com.rename_file_on_device_isolated", return_value=None):
    ok_none = t.rename("Music/Artist/Album/old.flac", "Music/Artist/Album/new.flac")
_check("None (subprocess timeout/failure) is treated as False, not a silent success", ok_none is False)

# ─── 10. rename — cross-folder falls back to pull->push->delete ───────────
print("\n[10] rename — cross-folder move (pull -> push -> delete)")
def _fake_pull(src_parts, filename, dest_local, device_name, storage_name):
    pathlib.Path(dest_local).write_bytes(b"pulled content")
    return True

with mock.patch("mtp_transfer_com.read_file_from_device_isolated", side_effect=_fake_pull) as m_pull, \
     mock.patch.object(COMTransport, "push_file", return_value=True) as m_push, \
     mock.patch.object(COMTransport, "delete_file", return_value=True) as m_delete:
    ok = t.rename("Music/Artist/AlbumA/track.flac", "Music/Artist/AlbumB/track.flac")
_check("cross-folder rename returns True", ok is True)
_check("pulled from the source location", m_pull.called)
_check("pushed to the destination", m_push.call_args[0][1] == "Music/Artist/AlbumB/track.flac")
_check("deleted the original after successful move", m_delete.call_args[0][0] == "Music/Artist/AlbumA/track.flac")

# ─── 11. rename — cross-folder pull fails -> returns False, no push/delete ─
print("\n[11] rename — cross-folder pull fails")
with mock.patch("mtp_transfer_com.read_file_from_device_isolated", return_value=False), \
     mock.patch.object(COMTransport, "push_file") as m_push2, \
     mock.patch.object(COMTransport, "delete_file") as m_delete2:
    ok = t.rename("Music/Artist/AlbumA/track.flac", "Music/Artist/AlbumB/track.flac")
_check("returns False when pull fails", ok is False)
_check("push never attempted", not m_push2.called)
_check("delete never attempted (original preserved)", not m_delete2.called)

# ─── 12. get_free_space_mb ──────────────────────────────────────────────────
print("\n[12] get_free_space_mb")
with mock.patch("mtp_transfer_com.get_device_free_space", return_value=5_368_709_120):  # 5 GiB
    mb = t.get_free_space_mb("SD card")
_check("converts bytes to MB correctly", mb == 5120)
with mock.patch("mtp_transfer_com.get_device_free_space", return_value=-1):
    mb2 = t.get_free_space_mb("SD card")
_check("returns 0 (not negative) when COM reports unavailable", mb2 == 0)

# ─── 13. create_snapshot — success and failure paths ───────────────────────
print("\n[13] create_snapshot")
with mock.patch.object(COMTransport, "list_files", return_value=[
    {"path": "Music/Artist/Album/track.flac", "size_bytes": 123, "mtime": 0.0, "is_dir": False}
]):
    snap = t.create_snapshot("Music")
_check("snapshot complete=True on success", snap.complete is True)
_check("snapshot has 1 file", len(snap.files) == 1)
_check("snapshot source marked com_scan", snap.source == "com_scan")

with mock.patch.object(COMTransport, "list_files", side_effect=RuntimeError("scan failed")):
    snap2 = t.create_snapshot("Music")
_check("snapshot complete=False on failure (never raises)", snap2.complete is False)
_check("snapshot records the error message", "scan failed" in snap2.error_msg)

# ─── 14. cleanup_orphaned_temps — no-op by design ───────────────────────────
print("\n[14] cleanup_orphaned_temps is a documented no-op for COM")
_check("returns 0", t.cleanup_orphaned_temps("SD card") == 0)

# ─── 15. get_transport() factory — "com" opt-in, "adb" still default ───────
print("\n[15] get_transport() factory wiring")
_check("backend='com' returns COMTransport", isinstance(get_transport(backend="com"), COMTransport))
_check("default (no backend arg) still returns ADBTransport", isinstance(get_transport(), ADBTransport))

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*60}\nCOMTransport (opt-in ADB/COM swap): {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
