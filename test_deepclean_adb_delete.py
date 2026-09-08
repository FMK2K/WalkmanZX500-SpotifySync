"""
test_deepclean_adb_delete.py — isolated verification for wpd_transfer.delete_files_adb

Stage 2 of this project's verification ladder (py_compile -> offline mock
validation -> live). No device required. Mocks adb_transfer.device_is_connected /
_get_storage_path and subprocess.Popen so no real ADB call is made.
"""
from unittest import mock

import wpd_transfer

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
    def __init__(self, stdout, timeout=False):
        self._stdout = stdout
        self._timeout = timeout

    def communicate(self, input=None, timeout=None):
        if self._timeout:
            import subprocess
            raise subprocess.TimeoutExpired(cmd="adb", timeout=timeout)
        return (self._stdout, "")

    def kill(self):
        pass


print("[1] ADB not connected -> returns {} (caller falls back)")
with mock.patch("adb_transfer.device_is_connected", return_value=False):
    r = wpd_transfer.delete_files_adb_by_path("WALKMAN", "SD card", "Music", {("Music", "A"): ["x.mp3"]})
    _check("empty dict when ADB unavailable", r == {})

print("\n[2] storage path resolution fails -> returns {}")
with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", side_effect=RuntimeError("no SD card")):
    r = wpd_transfer.delete_files_adb_by_path("WALKMAN", "SD card", "Music", {("Music", "A"): ["x.mp3"]})
    _check("empty dict when storage path unresolvable", r == {})

print("\n[3] normal batch: deleted / not_found / error status mapping + key format")
groups = {
    ("Music", "ArtistA"): ["a1.mp3", "a2.mp3"],
    ("Music", "ArtistB", "AlbumB"): ["b1.flac"],
}
# 3 files total -> tokens F0, F1 (group A), F2 (group B), in dict-iteration order
fake_stdout = "D:F0\nN:F1\nE:F2\n"
with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/4A21-0000"), \
     mock.patch("subprocess.Popen", return_value=_FakeProc(fake_stdout)):
    r = wpd_transfer.delete_files_adb_by_path("WALKMAN", "SD card", "Music", groups)
    _check("3 results returned", len(r) == 3)
    _check("key format is parts_tuple + (filename,)",
           ("Music", "ArtistA", "a1.mp3") in r)
    _check("deleted status mapped", r.get(("Music", "ArtistA", "a1.mp3")) == "deleted")
    _check("not_found status mapped", r.get(("Music", "ArtistA", "a2.mp3")) == "not_found")
    _check("error status mapped", r.get(("Music", "ArtistB", "AlbumB", "b1.flac")) == "error")

print("\n[4] batch timeout -> all tokens marked error, not silently dropped")
with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/4A21-0000"), \
     mock.patch("subprocess.Popen", return_value=_FakeProc("", timeout=True)):
    r = wpd_transfer.delete_files_adb_by_path("WALKMAN", "SD card", "Music", {("Music", "A"): ["x.mp3"]})
    _check("timeout -> error status, not missing/silently-done",
           r.get(("Music", "A", "x.mp3")) == "error")

print("\n[5] a token with no returned line at all -> marked error, not omitted")
with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/4A21-0000"), \
     mock.patch("subprocess.Popen", return_value=_FakeProc("D:F0\n")):  # F1 never reported
    r = wpd_transfer.delete_files_adb_by_path(
        "WALKMAN", "SD card", "Music",
        {("Music", "A"): ["x.mp3", "y.mp3"]},
    )
    _check("reported token correct", r.get(("Music", "A", "x.mp3")) == "deleted")
    _check("unreported token defaults to error (not silently dropped)",
           r.get(("Music", "A", "y.mp3")) == "error")

print("\n[6] empty groups -> returns {} without touching ADB")
with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/4A21-0000"), \
     mock.patch("subprocess.Popen") as mock_popen:
    r = wpd_transfer.delete_files_adb_by_path("WALKMAN", "SD card", "Music", {})
    _check("empty groups -> empty result", r == {})
    _check("no subprocess spawned for empty input", not mock_popen.called)

print("\n[7] batching: > batch_size items span multiple Popen calls")
big_groups = {("Music", "Big"): [f"t{i}.mp3" for i in range(5)]}
call_count = {"n": 0}


def _popen_side_effect(*a, **kw):
    call_count["n"] += 1
    return _FakeProc("D:F0\nD:F1\n")


with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/4A21-0000"), \
     mock.patch("subprocess.Popen", side_effect=_popen_side_effect):
    r = wpd_transfer.delete_files_adb_by_path(
        "WALKMAN", "SD card", "Music", big_groups, batch_size=2,
    )
    _check("5 items / batch_size=2 -> 3 Popen calls", call_count["n"] == 3)

print(f"\n{'=' * 60}")
print(f"delete_files_adb: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
