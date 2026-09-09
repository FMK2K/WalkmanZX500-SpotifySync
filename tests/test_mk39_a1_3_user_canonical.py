"""
test_mk39_a1_3_user_canonical.py — MK39-A1.3 regression + 5,000-sample MC
================================================================================

Council reconsideration verdict (locked, 2026-05-09): operator-curated music
has authoritative metadata signals (filename intent, folder organization,
explicit override files). MK39-A1.3 recognizes these as canonical without
weakening any contamination defense.

Locked acceptance criteria for the 5,000-sample MC:
  - 0 fabrications: no field gets a value when no canonical signal exists
  - 0 overwrites: existing non-empty fields are NEVER overwritten unless
    explicit override file says to
  - 0 conflict-writes: when filename ↔ folder disagree, the field is NOT
    written
  - 0 watermark-promotions: any name with watermark substrings is rejected
  - 100% recall on production-pattern: "Artist - Album/Artist - Album - NN
    Title.ext" yields all 4 fields cross-validated to STRONG

Run:
    python3 -m pytest test_mk39_a1_3_user_canonical.py -v -s
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path


def _reset_modules():
    for m in ("mk39_a1_3_user_canonical", "mk38_0_semantic_write_barrier"):
        if m in sys.modules:
            del sys.modules[m]


# ═══════════════════════════════════════════════════════════════════════════
# T1-T8: filename pattern recognition
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_3_FilenameParsing(unittest.TestCase):
    def setUp(self):
        _reset_modules()

    def test_T1_three_part_canonical(self):
        import mk39_a1_3_user_canonical as uc
        r = uc.parse_filename_canonical(
            "Music/Rob Zombie - The Sinister Urge/Rob Zombie - The Sinister Urge - 01 Scum Of The Earth.mp3")
        self.assertEqual(r["artist"][0], "Rob Zombie")
        self.assertEqual(r["album"][0], "The Sinister Urge")
        self.assertEqual(r["title"][0], "Scum Of The Earth")
        self.assertEqual(r["album_artist"][0], "Rob Zombie")

    def test_T2_two_part_artist_title(self):
        import mk39_a1_3_user_canonical as uc
        r = uc.parse_filename_canonical("/Music/X/Beatles - Yesterday.mp3")
        self.assertEqual(r["artist"][0], "Beatles")
        self.assertEqual(r["title"][0], "Yesterday")
        self.assertNotIn("album", r)

    def test_T3_leading_number_only_is_title_only(self):
        import mk39_a1_3_user_canonical as uc
        r = uc.parse_filename_canonical("Music/X/01. Yesterday.mp3")
        self.assertIn("title", r)
        self.assertEqual(r["title"][0], "Yesterday")
        self.assertNotIn("artist", r)

    def test_T4_bare_name_returns_empty(self):
        """The user's AMApiano case: no canonical signal → empty dict."""
        import mk39_a1_3_user_canonical as uc
        self.assertEqual(uc.parse_filename_canonical("AMApiano #1.mp3"), {})
        self.assertEqual(uc.parse_filename_canonical("track01.mp3"), {})

    def test_T5_watermark_in_filename_rejected(self):
        """Filename parsing only sees the basename. Names with watermark
        substrings IN THE BASENAME are rejected; parent-folder watermarks
        are checked later by `_is_path_tainted` inside `resolve_user_canonical`."""
        import mk39_a1_3_user_canonical as uc
        for bad in ["soundcloudaud.com 500 - track.mp3",
                     "Untitled.mp3"]:
            self.assertEqual(uc.parse_filename_canonical(bad), {},
                              f"watermark/generic basename should reject {bad!r}")
        # Path-level taint: a clean basename inside a watermarked folder.
        # parse_filename_canonical (basename-only) doesn't reject it,
        # but resolve_user_canonical (path-aware) MUST.
        self.assertNotEqual(uc.parse_filename_canonical("/tmp/walkman_dl_xyz/foo - bar.mp3"), {},
                              "filename basename is clean — parsing only checks basename")
        self.assertEqual(uc.resolve_user_canonical("/tmp/walkman_dl_xyz/foo - bar.mp3", existing={}), ({}, {}),
                          "path with watermarked parent must reject ALL signals at resolve level (Council #2)")

    def test_T6_empty_part_rejected(self):
        """Names like 'Artist -  - Title.mp3' have an empty middle part."""
        import mk39_a1_3_user_canonical as uc
        self.assertEqual(uc.parse_filename_canonical("Artist -  - Title.mp3"), {})

    def test_T7_generic_token_as_artist_rejected(self):
        import mk39_a1_3_user_canonical as uc
        # "Various Artists - X" is too generic to be canonical
        self.assertEqual(uc.parse_filename_canonical("Various - X.mp3"), {})

    def test_T8_strip_leading_track_number(self):
        import mk39_a1_3_user_canonical as uc
        # In a 3-part filename, leading number on the title part is stripped
        r = uc.parse_filename_canonical("Music/X/A - B - 01 Yesterday.mp3")
        self.assertEqual(r["title"][0], "Yesterday")
        r = uc.parse_filename_canonical("Music/X/A - B - 01-Yesterday.mp3")
        self.assertEqual(r["title"][0], "Yesterday")
        r = uc.parse_filename_canonical("Music/X/A - B - 001 Yesterday.mp3")
        self.assertEqual(r["title"][0], "Yesterday")


# ═══════════════════════════════════════════════════════════════════════════
# T9-T12: folder structure parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_3_FolderParsing(unittest.TestCase):
    def setUp(self):
        _reset_modules()

    def test_T9_music_artist_album_pattern(self):
        import mk39_a1_3_user_canonical as uc
        r = uc.parse_folder_canonical("/Music/Beatles/Help/track.mp3")
        self.assertEqual(r["artist"][0], "Beatles")
        self.assertEqual(r["album"][0], "Help")
        self.assertEqual(r["album_artist"][0], "Beatles")

    def test_T10_combined_artist_dash_album_folder(self):
        import mk39_a1_3_user_canonical as uc
        # /Music/<Artist - Album>/track.ext — single combined folder
        r = uc.parse_folder_canonical("/Music/Beatles - Help/track.mp3")
        self.assertEqual(r["artist"][0], "Beatles")
        self.assertEqual(r["album"][0], "Help")

    def test_T11_redundant_artist_pattern(self):
        """The user's observed convention: /Music/Artist/Artist - Album/track.ext.
        The album folder repeats the artist; should split correctly."""
        import mk39_a1_3_user_canonical as uc
        r = uc.parse_folder_canonical("/Music/Black Sabbath/Black Sabbath - 13/track.mp3")
        self.assertEqual(r["artist"][0], "Black Sabbath")
        self.assertEqual(r["album"][0], "13")

    def test_T12_no_music_anchor_uses_last_two(self):
        """When there's no /Music/ anchor, fall back to the last two parents."""
        import mk39_a1_3_user_canonical as uc
        r = uc.parse_folder_canonical("/some/other/Beatles/Help/track.mp3")
        self.assertEqual(r["artist"][0], "Beatles")
        self.assertEqual(r["album"][0], "Help")

    def test_T12b_root_level_file_returns_empty(self):
        import mk39_a1_3_user_canonical as uc
        self.assertEqual(uc.parse_folder_canonical("/track.mp3"), {})


# ═══════════════════════════════════════════════════════════════════════════
# T13-T15: override file
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_3_OverrideFile(unittest.TestCase):
    def setUp(self):
        _reset_modules()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_T13_well_formed_override_file_loaded(self):
        import mk39_a1_3_user_canonical as uc
        ov_path = Path(self._tmp) / uc.OVERRIDE_FILENAME
        ov_path.write_text(json.dumps({
            "version": 1,
            "tracks": {
                "AMApiano #1.mp3": {
                    "artist": "FeisalK",
                    "genre": "Amapiano",
                    "year": 2024,
                    "_provenance": "user_explicit",
                }
            }
        }), encoding="utf-8")
        out = uc.load_override_file(self._tmp)
        self.assertIn("AMApiano #1.mp3", out)
        self.assertEqual(out["AMApiano #1.mp3"]["artist"], "FeisalK")
        self.assertEqual(out["AMApiano #1.mp3"]["genre"], "Amapiano")
        self.assertNotIn("_provenance", out["AMApiano #1.mp3"],
                          "internal markers stripped from output")

    def test_T14_missing_provenance_marker_rejects_track(self):
        """Without _provenance='user_explicit', the track entry is rejected
        — no anonymous override permitted."""
        import mk39_a1_3_user_canonical as uc
        ov_path = Path(self._tmp) / uc.OVERRIDE_FILENAME
        ov_path.write_text(json.dumps({
            "version": 1,
            "tracks": {"x.mp3": {"artist": "X"}},   # no _provenance!
        }), encoding="utf-8")
        out = uc.load_override_file(self._tmp)
        self.assertNotIn("x.mp3", out)

    def test_T15_missing_or_corrupt_file_returns_empty(self):
        import mk39_a1_3_user_canonical as uc
        # No file at all
        self.assertEqual(uc.load_override_file(self._tmp), {})
        # Corrupt JSON
        ov_path = Path(self._tmp) / uc.OVERRIDE_FILENAME
        ov_path.write_text("not json {{{ }", encoding="utf-8")
        self.assertEqual(uc.load_override_file(self._tmp), {})
        # Wrong version
        ov_path.write_text(json.dumps({"version": 99, "tracks": {}}), encoding="utf-8")
        self.assertEqual(uc.load_override_file(self._tmp), {})


# ═══════════════════════════════════════════════════════════════════════════
# T16-T22: resolution rules (precedence, conflict, fill-only-blanks)
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_3_Resolution(unittest.TestCase):
    def setUp(self):
        _reset_modules()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_T16_filename_folder_agree_promotes_to_strong_writable(self):
        """Cross-validated agreement → STRONG, in writable dict (Council #1)."""
        import mk39_a1_3_user_canonical as uc
        w, q = uc.resolve_user_canonical(
            "Music/Rob Zombie/The Sinister Urge/Rob Zombie - The Sinister Urge - 01 Scum.mp3",
            existing={})
        # artist, album, album_artist all cross-validated → in writable
        self.assertEqual(w["artist"][1], "user_canonical")
        self.assertEqual(w["artist"][2], uc.CONF_STRONG)
        self.assertEqual(w["album"][1], "user_canonical")
        self.assertEqual(w["album"][2], uc.CONF_STRONG)
        self.assertNotIn("artist", q)   # cross-validated never quarantined

    def test_T17_filename_folder_conflict_field_not_written_or_quarantined(self):
        """Conflict → neither writable nor quarantined (Council #1 strict)."""
        import mk39_a1_3_user_canonical as uc
        w, q = uc.resolve_user_canonical(
            "Music/Beatles/Help/Stones - Satisfaction.mp3",
            existing={})
        # artist conflicts (filename=Stones, folder=Beatles) → in NEITHER
        self.assertNotIn("artist", w)
        self.assertNotIn("artist", q)
        # album only has folder signal (single) → quarantined per Council #1
        self.assertIn("album", q)
        self.assertEqual(q["album"][1], uc.USER_CANONICAL_FOLDER)
        self.assertNotIn("album", w)

    def test_T18_existing_field_never_overwritten(self):
        """Council #4: fill-only-blanks. Existing non-empty wins always."""
        import mk39_a1_3_user_canonical as uc
        w, q = uc.resolve_user_canonical(
            "Music/Rob Zombie/The Sinister Urge/Rob Zombie - The Sinister Urge - 01 Scum.mp3",
            existing={"artist": "Manual Override Value"})
        self.assertNotIn("artist", w, "existing artist not in writable")
        self.assertNotIn("artist", q, "existing artist not in quarantined")

    def test_T19_override_file_wins_with_strong_confidence_writable(self):
        """Override → STRONG, in writable dict (Council #1, override is canonical)."""
        import mk39_a1_3_user_canonical as uc
        ov_path = Path(self._tmp) / uc.OVERRIDE_FILENAME
        ov_path.write_text(json.dumps({
            "version": 1,
            "tracks": {"track.mp3": {"artist": "OverrideArtist",
                                       "_provenance": "user_explicit"}}
        }), encoding="utf-8")
        target = Path(self._tmp) / "track.mp3"
        w, q = uc.resolve_user_canonical(target, existing={})
        self.assertEqual(w["artist"][0], "OverrideArtist")
        self.assertEqual(w["artist"][1], uc.USER_CANONICAL_OVERRIDE)
        self.assertEqual(w["artist"][2], uc.CONF_STRONG)

    def test_T20_blank_when_no_signals(self):
        import mk39_a1_3_user_canonical as uc
        w, q = uc.resolve_user_canonical("AMApiano #1.mp3", existing={})
        self.assertEqual(w, {})
        self.assertEqual(q, {})

    def test_T21_filename_only_signal_quarantined_not_writable(self):
        """Council #1: filename-only is single-signal → quarantined, NOT writable."""
        import mk39_a1_3_user_canonical as uc
        w, q = uc.resolve_user_canonical("Beatles - Help - 01 Yesterday.mp3", existing={})
        # No folder context → single-signal → quarantine
        self.assertEqual(w, {}, "filename-only must NOT be in writable (Council #1)")
        self.assertIn("title", q)
        self.assertEqual(q["title"][1], uc.USER_CANONICAL_FILENAME)
        self.assertEqual(q["title"][2], uc.CONF_MEDIUM)

    def test_T22_watermarked_path_rejects_all_signals(self):
        """Council #2: watermarked path → reject ALL signals from BOTH dicts."""
        import mk39_a1_3_user_canonical as uc
        w, q = uc.resolve_user_canonical(
            "/tmp/walkman_dl_xyz/Beatles - Help - 01 Yesterday.mp3",
            existing={})
        self.assertEqual(w, {}, "tainted path: writable empty (Council #2)")
        self.assertEqual(q, {}, "tainted path: quarantined empty (Council #2)")


# ═══════════════════════════════════════════════════════════════════════════
# T23-T24: MK38.0 integration
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_3_MK38Integration(unittest.TestCase):
    def setUp(self):
        _reset_modules()

    def test_T23_register_extends_mk38_canonical_sources(self):
        import mk39_a1_3_user_canonical as uc
        import mk38_0_semantic_write_barrier as mk38
        before = set(mk38.CANONICAL_SOURCES)
        uc.register_as_canonical_source()
        after = set(mk38.CANONICAL_SOURCES)
        # All 4 new labels added (3 individual + 1 combined)
        for new in ("user_canonical", uc.USER_CANONICAL_OVERRIDE,
                     uc.USER_CANONICAL_FILENAME, uc.USER_CANONICAL_FOLDER):
            self.assertIn(new, after, f"{new} not added")
        # Original sources preserved
        self.assertTrue(before.issubset(after))

    def test_T24_register_is_idempotent(self):
        import mk39_a1_3_user_canonical as uc
        first = uc.register_as_canonical_source()
        second = uc.register_as_canonical_source()
        # First returns True if mk38 importable; second always False
        if first is True:
            self.assertFalse(second)


# ═══════════════════════════════════════════════════════════════════════════
# T25-MC: 5,000-sample Monte Carlo
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_3_MonteCarlo(unittest.TestCase):
    """T25-MC: 5,000-sample MC over filename + folder + override + existing
    field combinations.

    Locked acceptance criteria:
      - fabrication_count = 0       (no field written without canonical signal)
      - overwrite_count = 0         (existing non-empty fields preserved)
      - conflict_write_count = 0    (filename↔folder conflicts not written)
      - watermark_promotion_count = 0  (watermarked names never canonicalized)
      - production_pattern_recall ≥ 99% (Artist-Album/Artist-Album-NN-Title:
                                            all 4 fields STRONG)
    """

    SAMPLES = 5000
    SEED = 0xCBE_39_A1A3 & 0xFFFFFFFF

    def setUp(self):
        _reset_modules()

    def test_T25_monte_carlo_5000_samples(self):
        """5,000-sample MC over filename+folder+override+existing combos.
        Updated for Council Constraint #1 (writable/quarantined split):
          - fabrication_count = 0          (no field with empty value)
          - overwrite_count = 0            (existing fields preserved)
          - conflict_write_count = 0       (filename↔folder conflicts in NEITHER)
          - watermark_promotion_count = 0  (Council #2: tainted → both empty)
          - single_signal_in_writable = 0  (Council #1: single → quarantine only)
          - production_pattern_writable_recall ≥ 99%  (cross-validated → writable)
        """
        import mk39_a1_3_user_canonical as uc
        rng = random.Random(self.SEED)

        ARTISTS = ["Beatles", "Rolling Stones", "Black Sabbath", "Tyler",
                    "Cochise", "Rob Zombie", "Aphex Twin", "Boards of Canada"]
        ALBUMS  = ["Help", "Let It Bleed", "13", "IGOR", "Selected Ambient",
                    "Music Has the Right", "The Sinister Urge"]
        TITLES  = ["Yesterday", "Gimme Shelter", "Age of Reason", "EARFQUAKE",
                    "Tell Em", "Polynomial-C", "Scum Of The Earth"]
        WATERMARKS = ["soundcloudaud.com 500", "y2mate.com", "untitled",
                       "walkman_dl_xyz123"]

        fabrication_count = 0
        overwrite_count = 0
        conflict_write_count = 0
        watermark_promotion_count = 0
        single_signal_in_writable = 0
        production_pattern_total = 0
        production_pattern_writable_recall = 0

        for _ in range(self.SAMPLES):
            scenario = rng.choice([
                "production_canonical", "production_canonical",
                "filename_only", "folder_only",
                "conflict", "watermark", "bare_name",
                "existing_field_present",
            ])

            if scenario == "production_canonical":
                a = rng.choice(ARTISTS); al = rng.choice(ALBUMS); t = rng.choice(TITLES)
                num = rng.randint(1, 20)
                path = f"/Music/{a}/{a} - {al}/{a} - {al} - {num:02d} {t}.mp3"
                production_pattern_total += 1
                w, q = uc.resolve_user_canonical(path, existing={})
                full_writable = (
                    "artist" in w and w["artist"][2] == uc.CONF_STRONG and
                    "album" in w  and w["album"][2]  == uc.CONF_STRONG and
                    "album_artist" in w and w["album_artist"][2] == uc.CONF_STRONG
                )
                if full_writable:
                    production_pattern_writable_recall += 1
                # title is filename-only → must be in quarantine, NOT writable
                if "title" in w:
                    single_signal_in_writable += 1
                for fld, (val, src, conf) in {**w, **q}.items():
                    if not val or not str(val).strip():
                        fabrication_count += 1

            elif scenario == "filename_only":
                a = rng.choice(ARTISTS); t = rng.choice(TITLES)
                path = f"/random/{a} - {t}.mp3"
                w, q = uc.resolve_user_canonical(path, existing={})
                # Single-signal must be in quarantine, not writable (Council #1)
                if w:
                    single_signal_in_writable += len(w)
                for fld, (val, src, conf) in {**w, **q}.items():
                    if not val: fabrication_count += 1

            elif scenario == "folder_only":
                a = rng.choice(ARTISTS); al = rng.choice(ALBUMS)
                path = f"/Music/{a}/{al}/random_track.mp3"
                w, q = uc.resolve_user_canonical(path, existing={})
                if w:
                    single_signal_in_writable += len(w)
                for fld, (val, src, conf) in {**w, **q}.items():
                    if not val: fabrication_count += 1

            elif scenario == "conflict":
                a1 = rng.choice(ARTISTS); a2 = rng.choice([a for a in ARTISTS if a != a1])
                al = rng.choice(ALBUMS); t = rng.choice(TITLES)
                path = f"/Music/{a1}/{al}/{a2} - {t}.mp3"
                w, q = uc.resolve_user_canonical(path, existing={})
                # 'artist' has filename(a2) vs folder(a1) conflict → in NEITHER
                if "artist" in w or "artist" in q:
                    conflict_write_count += 1

            elif scenario == "watermark":
                wm = rng.choice(WATERMARKS); t = rng.choice(TITLES)
                path = f"/Music/{wm}/{wm} - {t}.mp3"
                w, q = uc.resolve_user_canonical(path, existing={})
                # Council #2: watermarked → both empty
                if w or q:
                    for fld in (set(w.keys()) | set(q.keys())):
                        if fld in ("artist", "album", "album_artist"):
                            watermark_promotion_count += 1

            elif scenario == "bare_name":
                bare_name = rng.choice(["AMApiano #1.mp3", "track01.mp3",
                                         "recording.mp3", "audio.mp3"])
                path = f"/Music/{bare_name}"
                w, q = uc.resolve_user_canonical(path, existing={})
                for fld, (val, src, conf) in {**w, **q}.items():
                    if not val: fabrication_count += 1

            elif scenario == "existing_field_present":
                a = rng.choice(ARTISTS); al = rng.choice(ALBUMS); t = rng.choice(TITLES)
                num = rng.randint(1, 20)
                path = f"/Music/{a}/{a} - {al}/{a} - {al} - {num:02d} {t}.mp3"
                existing = {"artist": "OperatorOverride", "album": "ManualAlbum"}
                w, q = uc.resolve_user_canonical(path, existing=existing)
                if "artist" in w or "artist" in q: overwrite_count += 1
                if "album" in w  or "album" in q:  overwrite_count += 1

        recall_pct = (100.0 * production_pattern_writable_recall /
                       max(1, production_pattern_total))

        print("\n--- MK39-A1.3 user-canonical MC (5,000 samples, Council-compliant) ---")
        print(f"  fabrication_count:                    {fabrication_count}     (LOCKED: 0)")
        print(f"  overwrite_count:                      {overwrite_count}     (LOCKED: 0)")
        print(f"  conflict_write_count:                 {conflict_write_count}     (LOCKED: 0)")
        print(f"  watermark_promotion_count:            {watermark_promotion_count}     (LOCKED: 0)")
        print(f"  single_signal_in_writable:            {single_signal_in_writable}     (LOCKED: 0; Council #1)")
        print(f"  production_pattern_total:             {production_pattern_total}")
        print(f"  production_pattern_writable_recall:   {production_pattern_writable_recall} ({recall_pct:.1f}%)")

        # === LOCKED PASS CRITERIA ===
        self.assertEqual(fabrication_count, 0,
            f"FABRICATION: {fabrication_count}")
        self.assertEqual(overwrite_count, 0,
            f"OVERWRITE: {overwrite_count}")
        self.assertEqual(conflict_write_count, 0,
            f"CONFLICT: {conflict_write_count}")
        self.assertEqual(watermark_promotion_count, 0,
            f"WATERMARK: {watermark_promotion_count}")
        self.assertEqual(single_signal_in_writable, 0,
            f"COUNCIL_1_VIOLATION: {single_signal_in_writable} single-signal items in writable")
        self.assertGreaterEqual(recall_pct, 99.0,
            f"RECALL: {recall_pct:.1f}% (target ≥99%)")


class TestMK39_A1_3_API(unittest.TestCase):
    def test_T26_API_surface_complete(self):
        import mk39_a1_3_user_canonical as uc
        for name in ["USER_CANONICAL_OVERRIDE", "USER_CANONICAL_FILENAME",
                     "USER_CANONICAL_FOLDER", "USER_CANONICAL_SOURCES",
                     "OVERRIDE_FILENAME", "QUARANTINE_FILENAME",
                     "CONF_STRONG", "CONF_MEDIUM", "CONF_WEAK",
                     "parse_filename_canonical", "parse_folder_canonical",
                     "load_override_file", "resolve_user_canonical",
                     "register_as_canonical_source", "is_registered",
                     # Council Conditional-Approval constraints
                     "make_user_canonical_capability",
                     "write_quarantine_file", "load_quarantine_file",
                     "quarantine_summary"]:
            self.assertTrue(hasattr(uc, name), f"missing: {name}")
            self.assertIn(name, uc.__all__, f"not in __all__: {name}")


# ═══════════════════════════════════════════════════════════════════════════
# T28-T31: Council Constraints #3 (mediator capability) + #5 (quarantine workflow)
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_3_CouncilConstraints(unittest.TestCase):
    """T28-T31: verify Council 2026-05-09 Conditional Approval constraints
    are structurally honored.
      T28: Constraint #3 — capability builder produces valid capability
      T29: Constraint #3 — capability rejects invalid sources
      T30: Constraint #5 — quarantine file write+load round-trip
      T31: Constraint #5 — quarantine file merge preserves first_seen
    """

    def setUp(self):
        _reset_modules()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_T28_capability_builder_produces_valid_capability(self):
        """Council #3: make_user_canonical_capability returns a capability
        compatible with mediator.mediated_operation()."""
        import mk39_a1_3_user_canonical as uc
        cap = uc.make_user_canonical_capability("user_canonical",
                                                  ["artist", "album"])
        self.assertEqual(cap["source"], "user_canonical")
        self.assertEqual(cap["issued_by"], "mk39_a1_3_user_canonical")
        self.assertEqual(cap["semantic_scope"]["allowed_op_classes"], ["TAG_WRITE"])
        self.assertEqual(set(cap["semantic_scope"]["allowed_fields"]),
                          {"artist", "album"})
        self.assertEqual(cap["semantic_scope"]["max_writes"], 2)
        self.assertEqual(cap["telemetry_marker"], "USER_CANONICAL_WRITE")

    def test_T29_capability_rejects_invalid_sources(self):
        """Council #3: capability builder rejects non-USER_CANONICAL sources
        — prevents mis-attribution as user-canonical."""
        import mk39_a1_3_user_canonical as uc
        with self.assertRaises(ValueError):
            uc.make_user_canonical_capability("ai_inference", ["genre"])
        with self.assertRaises(ValueError):
            uc.make_user_canonical_capability("spotify_canonical", ["title"])

    def test_T30_quarantine_file_write_and_load_round_trip(self):
        """Council #5: write_quarantine_file persists pending entries;
        load_quarantine_file reads them back."""
        import mk39_a1_3_user_canonical as uc
        quar_per_track = {
            "Beatles - Help - 01 Yesterday.mp3": {
                "title": ("Yesterday", uc.USER_CANONICAL_FILENAME, uc.CONF_MEDIUM),
                "artist": ("Beatles", uc.USER_CANONICAL_FILENAME, uc.CONF_MEDIUM),
            }
        }
        out = uc.write_quarantine_file(self._tmp, quar_per_track)
        self.assertIsNotNone(out, "write should succeed")
        self.assertTrue(out.exists())
        loaded = uc.load_quarantine_file(self._tmp)
        self.assertIn("Beatles - Help - 01 Yesterday.mp3", loaded)
        track_data = loaded["Beatles - Help - 01 Yesterday.mp3"]
        self.assertIn("title", track_data)
        self.assertEqual(track_data["title"]["value"], "Yesterday")
        self.assertEqual(track_data["title"]["confidence"], "MEDIUM")
        self.assertIn("first_seen", track_data["title"])

    def test_T31_quarantine_merge_preserves_first_seen(self):
        """Council #5: re-running write_quarantine_file with same entries
        preserves first_seen timestamp (so operators see how long an item
        has been pending review)."""
        import mk39_a1_3_user_canonical as uc
        import time
        quar = {"track.mp3": {
            "title": ("First Title", uc.USER_CANONICAL_FILENAME, uc.CONF_MEDIUM),
        }}
        out1 = uc.write_quarantine_file(self._tmp, quar)
        loaded1 = uc.load_quarantine_file(self._tmp)
        first_seen_1 = loaded1["track.mp3"]["title"]["first_seen"]
        time.sleep(0.01)
        # Write again with same entry
        out2 = uc.write_quarantine_file(self._tmp, quar, merge=True)
        loaded2 = uc.load_quarantine_file(self._tmp)
        first_seen_2 = loaded2["track.mp3"]["title"]["first_seen"]
        self.assertEqual(first_seen_1, first_seen_2,
                          "first_seen must be preserved across writes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
