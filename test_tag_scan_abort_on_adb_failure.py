"""
test_tag_scan_abort_on_adb_failure.py — verifies walkman_tag_scan.main()
refuses to overwrite walkman_tag_snapshot.json when most of a run never
actually read the device, 2026-07-28.

Confirmed live: ADB was offline for an entire refresh-tag-snapshot run, every
one of 1420 adb_pull_head() calls failed identically, and main() still wrote
1420/1420 blank "ADB read failed" placeholders over a working snapshot and
exited 0 — no signal distinguished that from a genuine "no embedded art, no
genre" finding across the whole library. No device required — scan_track is
monkeypatched; SNAPSHOT/OUT point at scratch temp files, never the real
project files.
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import walkman_tag_scan as wts

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


def _fake_snapshot(n):
    return [
        {"_id": str(i), "_data": f"/storage/sd/Music/A/t{i}.mp3", "ext": ".mp3",
         "storage": "sd", "duration_minsec": "3:00"}
        for i in range(n)
    ]


ERROR_RESULT = {
    "error": True, "has_apic": False, "id3_version": None, "tag_genre": None,
    "issues": ["ADB read failed — device disconnected or path changed"],
}
GOOD_RESULT = {
    "error": False, "has_apic": True, "id3_version": "2.3", "tag_genre": "Rock",
    "tag_title": "T", "tag_artist": "A", "tag_album": "Alb",
    "issues": [],
}

with tempfile.TemporaryDirectory() as tmp:
    tmp_dir = Path(tmp)
    snap_path = tmp_dir / "walkman_snapshot.json"
    out_path = tmp_dir / "walkman_tag_snapshot.json"

    print("[1] every pull fails (ADB offline entire run) -> aborts, does NOT write, exit != 0")
    fake_snap = _fake_snapshot(20)
    snap_path.write_text(json.dumps(fake_snap), encoding="utf-8")
    out_path.write_text(json.dumps({"pre-existing": "real data"}), encoding="utf-8")
    with mock.patch.object(wts, "SNAPSHOT", snap_path), \
         mock.patch.object(wts, "OUT", out_path), \
         mock.patch.object(wts, "scan_track",
                            side_effect=lambda t, d: (t["_data"], dict(ERROR_RESULT))):
        try:
            wts.main()
            _check("raised SystemExit", False)
        except SystemExit as e:
            _check("raised SystemExit with a clear reason", "ABORTED" in str(e) and "ADB read failed" in str(e))
    _check("pre-existing snapshot file left untouched", json.loads(out_path.read_text()) == {"pre-existing": "real data"})

    print("\n[2] mostly successful run (a few real failures) -> writes normally")
    out_path.write_text(json.dumps({"pre-existing": "real data"}), encoding="utf-8")

    def _mostly_good(t, d):
        idx = int(t["_id"])
        return t["_data"], dict(ERROR_RESULT) if idx < 2 else dict(GOOD_RESULT)

    with mock.patch.object(wts, "SNAPSHOT", snap_path), \
         mock.patch.object(wts, "OUT", out_path), \
         mock.patch.object(wts, "scan_track", side_effect=_mostly_good):
        wts.main()  # should not raise
    written = json.loads(out_path.read_text(encoding="utf-8"))
    _check("snapshot was actually replaced", "pre-existing" not in written)
    _check("all 20 tracks present", len(written) == 20)

    print("\n[3] exactly at the 50% boundary (not over it) -> still writes")
    out_path.write_text(json.dumps({"pre-existing": "real data"}), encoding="utf-8")

    def _half_half(t, d):
        idx = int(t["_id"])
        return t["_data"], dict(ERROR_RESULT) if idx < 10 else dict(GOOD_RESULT)

    with mock.patch.object(wts, "SNAPSHOT", snap_path), \
         mock.patch.object(wts, "OUT", out_path), \
         mock.patch.object(wts, "scan_track", side_effect=_half_half):
        wts.main()
    written = json.loads(out_path.read_text(encoding="utf-8"))
    _check("50% failure rate (not >50%) still writes", "pre-existing" not in written)

print(f"\n{'=' * 60}")
print(f"tag_scan abort-on-ADB-failure: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
