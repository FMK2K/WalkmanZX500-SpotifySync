"""
test_cohort_classifier_data_key.py — verifies cohort_classifier reads the
device snapshot's real "_data" path field, 2026-07-28.

Previously _classify_audit/_classify_reorganize/_device_basenames looked for
f["device_path"]/f["path"], but walkman_snapshot.json's actual per-file key is
"_data" (confirmed live: 0/1420 real files matched). Every tag_snap lookup
silently missed, and _audit_risk()'s `if not tags: return 3` classified every
single file "high" regardless of real state — confirmed live as
cohorts={'low':0,'medium':0,'high':1420,'skip':0,'quarantine':0}. No device
required — reads/writes are monkeypatched onto the module's snapshot loaders.
"""
from unittest import mock

import cohort_classifier as cc

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


DEVICE_FILES = [
    {"_data": "/storage/sd/Music/A/Good Track.flac", "ext": ".flac"},
    {"_data": "/storage/sd/Music/B/Unknown Track.mp3", "ext": ".mp3"},
    {"_data": "/storage/sd/Music/C/No Tags At All.mp3", "ext": ".mp3"},
]

TAG_SNAP = {
    "/storage/sd/Music/A/Good Track.flac": {
        "tag_artist": "A", "tag_album": "Alb", "tag_title": "Good Track",
        "tag_genre": "Rock", "has_apic": True, "id3_version": 3,
    },
    "/storage/sd/Music/B/Unknown Track.mp3": {
        "tag_artist": "", "tag_album": "", "tag_title": "",
        "tag_genre": "", "has_apic": False, "id3_version": 3,
    },
    # "No Tags At All" deliberately absent from tag_snap -> genuinely unknown
}

print("[1] _classify_audit reads _data, differentiates real cohorts (not all-high)")
with mock.patch("cohort_classifier._load_device_files", return_value=DEVICE_FILES), \
     mock.patch("cohort_classifier._load_tag_snap", return_value=TAG_SNAP):
    buckets = cc.classify("audit")
    sizes = {k: len(v) for k, v in buckets.items()}
    _check("not everything dumped into 'high'", sizes["high"] < len(DEVICE_FILES))
    _check("the fully-tagged track lands in skip or low",
           any(f["_data"].endswith("Good Track.flac") for f in buckets["skip"] + buckets["low"]))
    _check("the track missing from tag_snap still correctly lands in high (genuinely unknown)",
           any(f["_data"].endswith("No Tags At All.mp3") for f in buckets["high"]))

print("\n[2] old field names ('device_path'/'path') still work as a fallback")
old_style_files = [
    {"device_path": "/storage/sd/Music/A/Good Track.flac", "ext": ".flac"},
]
with mock.patch("cohort_classifier._load_device_files", return_value=old_style_files), \
     mock.patch("cohort_classifier._load_tag_snap", return_value=TAG_SNAP):
    buckets = cc.classify("audit")
    _check("device_path fallback still resolves a match",
           len(buckets["skip"]) + len(buckets["low"]) == 1)

print("\n[3] _classify_reorganize also reads _data")
reorg_files = [{"_data": "/storage/sd/Music/A/Good Track.flac", "parts": ["Music", "A", "Good Track.flac"]}]
with mock.patch("cohort_classifier._load_device_files", return_value=reorg_files), \
     mock.patch("cohort_classifier._load_tag_snap", return_value=TAG_SNAP):
    buckets = cc.classify("reorganize")
    _check("reorganize classification runs without error", sum(len(v) for v in buckets.values()) == 1)

print("\n[4] _device_basenames (used by download-mode 'already on device' skip) reads _data")
with mock.patch("cohort_classifier._load_device_files", return_value=DEVICE_FILES):
    names = cc._device_basenames()
    _check("basenames extracted from _data, not empty", names == {"good track", "unknown track", "no tags at all"})

print(f"\n{'=' * 60}")
print(f"cohort_classifier _data key fix: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
