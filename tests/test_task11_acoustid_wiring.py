"""
test_task11_acoustid_wiring.py — isolated verification for Task 11
(wiring the already-built mk39_phase_III_fallback.attempt_fingerprint_fallback
into audit.py's per-track resolution loop, via the new
audit._apply_acoustid_fallback helper)

Stage 2 of this project's verification ladder. No network, no fpcalc, no
device required — mocks mk39_phase_III_fallback.attempt_fingerprint_fallback
directly (the module this helper calls).
"""
import dataclasses
import pathlib
import sys
from unittest import mock

import audit

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


@dataclasses.dataclass
class _FakeIdentity:
    artist: str = ""
    album: str = ""
    title: str = ""
    year: str = ""
    musicbrainz_recording_id: str = ""
    score: float = 0.9
    provenance: str = "acoustic_fingerprint"


_dummy_path = pathlib.Path("dummy.flac")

# ─── 1. Already-resolved (non-blank) — fallback never even attempted ───────
print("\n[1] Already-resolved track — fallback not attempted")
expected = {"artist": "Real Artist", "title": "Real Title", "album": "", "release_year": ""}
with mock.patch("mk39_phase_III_fallback.attempt_fingerprint_fallback") as m_fb:
    changed = audit._apply_acoustid_fallback(expected, _dummy_path)
_check("returns False (nothing changed)", changed is False)
_check("attempt_fingerprint_fallback never called (blank-check short-circuits)", not m_fb.called)
_check("expected dict unmodified", expected == {"artist": "Real Artist", "title": "Real Title", "album": "", "release_year": ""})

# ─── 2. Blank track, fingerprint resolves — fields filled in ────────────────
print("\n[2] Blank track + confident fingerprint match — fields filled in")
expected = {"artist": "", "title": "", "album": "", "release_year": ""}
identity = _FakeIdentity(artist="Resolved Artist", title="Resolved Title", album="Resolved Album", year="2019")
with mock.patch("mk39_phase_III_fallback.attempt_fingerprint_fallback", return_value=identity) as m_fb:
    changed = audit._apply_acoustid_fallback(expected, _dummy_path)
_check("returns True", changed is True)
_check("attempt_fingerprint_fallback called once", m_fb.call_count == 1)
_check("artist filled in", expected["artist"] == "Resolved Artist")
_check("title filled in", expected["title"] == "Resolved Title")
_check("album filled in", expected["album"] == "Resolved Album")
_check("release_year filled in", expected["release_year"] == "2019")

# ─── 3. Blank track, no confident match (None) — nothing filled ────────────
print("\n[3] Blank track + no confident match — nothing filled")
expected = {"artist": "", "title": "", "album": "", "release_year": ""}
with mock.patch("mk39_phase_III_fallback.attempt_fingerprint_fallback", return_value=None):
    changed = audit._apply_acoustid_fallback(expected, _dummy_path)
_check("returns False", changed is False)
_check("expected dict still fully blank", all(not v for v in expected.values()))

# ─── 4. Partial blank (artist known, title blank) — only title fills in ───
print("\n[4] Partial blank — only the blank field is filled, existing value untouched")
expected = {"artist": "Known Artist", "title": "", "album": "", "release_year": ""}
identity = _FakeIdentity(artist="Different Artist Per Fingerprint", title="Resolved Title")
with mock.patch("mk39_phase_III_fallback.attempt_fingerprint_fallback", return_value=identity):
    changed = audit._apply_acoustid_fallback(expected, _dummy_path)
_check("returns True", changed is True)
_check("known artist NOT overwritten by fingerprint's differing artist", expected["artist"] == "Known Artist")
_check("blank title filled in", expected["title"] == "Resolved Title")

# ─── 5. Exception inside the fallback never propagates ─────────────────────
print("\n[5] Exception in attempt_fingerprint_fallback is swallowed")
expected = {"artist": "", "title": "", "album": "", "release_year": ""}
with mock.patch("mk39_phase_III_fallback.attempt_fingerprint_fallback", side_effect=RuntimeError("boom")):
    try:
        changed = audit._apply_acoustid_fallback(expected, _dummy_path)
        _check("no exception raised", True)
        _check("returns False on exception", changed is False)
    except Exception:
        _check("no exception raised", False)

print(f"\n{'='*60}\nTask 11 (AcoustID fallback wiring): {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
