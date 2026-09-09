"""
test_canary_tag_roundtrip_retry.py — isolated verification for
SpiralOrchestrator._probe_tag_roundtrip()'s multi-file retry, 2026-07-26.

Previously this canary probe only ever tried the FIRST file `find` returned
— a single genuinely corrupt file on the device (confirmed live: an entire
16-track album of 0-byte stubs) would permanently abort every audit/
reorganize/full run before it even started, despite 1200+ other files being
perfectly fine. No device required — adb_transfer._adb_cmd and
walkman_tag_scan.adb_pull_head are mocked.
"""
from pathlib import Path
from unittest import mock

from spiral_orchestrator import SpiralOrchestrator

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


def _make_orch():
    return SpiralOrchestrator(mode="audit")


find_output = "\n".join(f"/storage/emulated/0/Music/A/t{i}.mp3" for i in range(5))


def _pull_head_side_effect(remote_path, dest):
    # t0..t2 are "corrupt" (pull succeeds but writes 0 bytes / fails);
    # t3 is the first genuinely good one.
    idx = int(Path(remote_path).stem[1:])
    if idx < 3:
        Path(dest).write_bytes(b"")
        return True
    Path(dest).write_bytes(b"\x00" * 5000)
    return True


print("[1] first candidate is corrupt, later one succeeds -> probe still passes")
orch = _make_orch()
with mock.patch("spiral_orchestrator.config.USE_ADB", True), \
     mock.patch("adb_transfer._adb_cmd", return_value=find_output), \
     mock.patch("walkman_tag_scan.adb_pull_head", side_effect=_pull_head_side_effect):
    result = orch._probe_tag_roundtrip()
    _check("ok=True once a later candidate succeeds", result["ok"] is True)
    _check("detail references the successful file", "t3.mp3" in result["detail"])

print("\n[2] every candidate is corrupt -> probe correctly fails, tried multiple")
orch = _make_orch()
with mock.patch("spiral_orchestrator.config.USE_ADB", True), \
     mock.patch("adb_transfer._adb_cmd", return_value=find_output), \
     mock.patch("walkman_tag_scan.adb_pull_head",
                 side_effect=lambda remote, dest: (Path(dest).write_bytes(b""), True)[1]):
    result = orch._probe_tag_roundtrip()
    _check("ok=False when every candidate fails", result["ok"] is False)
    _check("detail reports how many were tried", "tried" in result["detail"])

print("\n[3] first candidate is healthy -> succeeds immediately, no wasted retries")
orch = _make_orch()
call_count = {"n": 0}


def _pull_first_ok(remote_path, dest):
    call_count["n"] += 1
    Path(dest).write_bytes(b"\x00" * 5000)
    return True


with mock.patch("spiral_orchestrator.config.USE_ADB", True), \
     mock.patch("adb_transfer._adb_cmd", return_value=find_output), \
     mock.patch("walkman_tag_scan.adb_pull_head", side_effect=_pull_first_ok):
    result = orch._probe_tag_roundtrip()
    _check("ok=True on the very first candidate", result["ok"] is True)
    _check("only tried once (didn't keep retrying after success)", call_count["n"] == 1)

print("\n[4] no .mp3 files found at all -> clean failure, no crash")
orch = _make_orch()
with mock.patch("spiral_orchestrator.config.USE_ADB", True), \
     mock.patch("adb_transfer._adb_cmd", return_value=""):
    result = orch._probe_tag_roundtrip()
    _check("ok=False with no candidates", result["ok"] is False)
    _check("clear 'no mp3 found' detail", "No .mp3" in result["detail"])

print(f"\n{'=' * 60}")
print(f"canary tag_roundtrip retry: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
