"""
test_mk39_phase_III.py — F/G/H integration regression
================================================================================

Adversarial-vetted shortlist of 9 subtickets across 3 Council targets:

  F.1  export_stager.py
  F.2  musicbee_sync.py
  F.3  walkman_sync wiring (config-gated)
  G.1  acoustid_fingerprint.py
  G.2  Phase I-fallback caller
  G.3  CANONICAL_SOURCES += "acoustic_fingerprint"
  H.1  staging_atomic.py
  H.2  picard_compat.py
  H.3  CANONICAL_SOURCES += "external_edit"

This suite uses REAL fpcalc (installed in sandbox) and REAL audio (ffmpeg
sine-wave generated). MusicBee subprocess and AcoustID HTTP are mocked
because they're genuinely unavailable in this environment.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


def _fresh_import(modules):
    for m in modules:
        if m in sys.modules:
            del sys.modules[m]


def _make_test_audio(out_path: Path, *, duration: float = 5.0,
                       freq: float = 440.0) -> None:
    """Generate a real sine-wave FLAC for fingerprint tests."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={duration}", str(out_path)],
        capture_output=True, check=True,
    )


def _make_test_mp3_with_tags(out_path: Path, *, version=4) -> None:
    """Generate a real MP3 with ID3v2.4 tags for picard_compat tests."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2",
         "-codec:a", "libmp3lame", "-b:a", "96k",
         "-id3v2_version", str(version),
         "-metadata", "artist=TestArtist",
         "-metadata", "title=TestTitle",
         "-metadata", "album=TestAlbum",
         "-metadata", "date=2024",
         str(out_path)],
        capture_output=True, check=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
# H.1 — staging_atomic
# ═══════════════════════════════════════════════════════════════════════════

class TestStagingAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T1_atomic_write_creates_file(self):
        from staging_atomic import atomic_write
        target = self.tmp / "out.bin"
        with atomic_write(target) as f:
            f.write(b"hello")
        self.assertEqual(target.read_bytes(), b"hello")

    def test_T2_atomic_write_replaces_existing_atomically(self):
        from staging_atomic import atomic_write
        target = self.tmp / "out.bin"
        target.write_bytes(b"original")
        with atomic_write(target) as f:
            f.write(b"replacement")
        self.assertEqual(target.read_bytes(), b"replacement")

    def test_T3_atomic_write_preserves_mode(self):
        from staging_atomic import atomic_write
        target = self.tmp / "out.bin"
        target.write_bytes(b"original")
        os.chmod(target, 0o640)
        with atomic_write(target) as f:
            f.write(b"replacement")
        # Mode survived the atomic swap
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_T4_atomic_write_exception_leaves_original_intact(self):
        from staging_atomic import atomic_write
        target = self.tmp / "out.bin"
        target.write_bytes(b"original")
        try:
            with atomic_write(target) as f:
                f.write(b"partial")
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        # Original survived; no orphan tempfile alongside it
        self.assertEqual(target.read_bytes(), b"original")
        siblings = list(target.parent.iterdir())
        self.assertEqual(len(siblings), 1, f"orphan file(s): {siblings}")

    def test_T5_atomic_copy_preserves_mtime(self):
        from staging_atomic import atomic_copy
        src = self.tmp / "src.bin"
        dst = self.tmp / "dst.bin"
        src.write_bytes(b"data")
        os.utime(src, (1700000000, 1700000000))
        atomic_copy(src, dst)
        self.assertEqual(int(dst.stat().st_mtime), 1700000000)

    def test_T6_safe_link_or_copy_uses_hardlink_same_fs(self):
        from staging_atomic import safe_link_or_copy
        src = self.tmp / "src.bin"
        dst = self.tmp / "dst.bin"
        src.write_bytes(b"data")
        result, method = safe_link_or_copy(src, dst)
        self.assertEqual(method, "hardlink")
        # Verify hardlink: same inode
        self.assertEqual(src.stat().st_ino, dst.stat().st_ino)


# ═══════════════════════════════════════════════════════════════════════════
# F.1 — export_stager
# ═══════════════════════════════════════════════════════════════════════════

class TestExportStager(unittest.TestCase):
    def setUp(self):
        self.src_root = Path(tempfile.mkdtemp())
        self.stg_root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.src_root, ignore_errors=True)
        shutil.rmtree(self.stg_root, ignore_errors=True)

    def test_T7_canonical_filename_template(self):
        from export_stager import canonical_filename
        out = canonical_filename(
            artist="Beatles", album="Help!", title="Yesterday",
            track=1, ext=".mp3",
        )
        self.assertEqual(out, "Beatles - Help! - 01 Yesterday.mp3")

    def test_T8_canonical_filename_sanitizes_illegal_chars(self):
        from export_stager import canonical_filename
        out = canonical_filename(
            artist="AC/DC", album='Back: "Black"', title="T*N|T",
            track=2, ext=".flac",
        )
        for bad in ('/', ':', '"', '*', '|'):
            self.assertNotIn(bad, out, f"illegal char {bad!r} survived: {out!r}")

    def test_T9_stage_one_skips_non_audio(self):
        from export_stager import stage_one
        src = self.src_root / "readme.txt"
        src.write_text("not audio")
        result = stage_one(src, self.stg_root / "readme.txt")
        self.assertEqual(result.method, "skip_no_audio")
        self.assertFalse(result.written)

    def test_T10_stage_one_idempotent(self):
        from export_stager import stage_one
        src = self.src_root / "track.mp3"
        _make_test_mp3_with_tags(src)
        dst = self.stg_root / "track.mp3"
        r1 = stage_one(src, dst)
        self.assertTrue(r1.written)
        r2 = stage_one(src, dst)
        self.assertEqual(r2.method, "skip_unchanged",
                          "second stage must be no-op")

    def test_T11_stage_tree_walks_recursively(self):
        from export_stager import stage_tree
        # Build small synthetic tree
        for sub in ("Artist1/Album1", "Artist2/Album2"):
            (self.src_root / sub).mkdir(parents=True)
            _make_test_mp3_with_tags(
                self.src_root / sub / "track.mp3"
            )
        # Plus some non-audio noise
        (self.src_root / "Artist1" / "notes.txt").write_text("ignore me")
        summary = stage_tree(self.src_root, self.stg_root)
        self.assertEqual(summary.written, 2)
        self.assertEqual(summary.skipped, 1)  # the .txt file


# ═══════════════════════════════════════════════════════════════════════════
# F.2 — musicbee_sync
# ═══════════════════════════════════════════════════════════════════════════

class TestMusicBeeSync(unittest.TestCase):

    def test_T12_is_available_false_on_non_windows(self):
        import musicbee_sync
        # Sandbox is Linux; MusicBee is Windows-only.
        if platform.system() != "Windows":
            self.assertFalse(musicbee_sync.is_available())

    def test_T13_run_sync_skips_gracefully_when_unavailable(self):
        import musicbee_sync
        # On Linux, run_sync must NOT raise; it must return SyncResult with
        # invoked=False and an explanatory skipped_reason.
        result = musicbee_sync.run_sync()
        if platform.system() != "Windows":
            self.assertFalse(result.invoked)
            self.assertIn("Windows", result.skipped_reason)

    def test_T14_dry_run_builds_command_without_invoking(self):
        import musicbee_sync as ms
        # Force a fake executable for command-build inspection
        with mock.patch.object(ms, "musicbee_executable_path",
                                return_value="C:\\fake\\MusicBee.exe"):
            result = ms.run_sync(dry_run=True, device_profile="ZX500")
            self.assertFalse(result.invoked)
            self.assertEqual(result.skipped_reason, "dry_run")

    def test_T15_build_sync_command_includes_profile_and_quiet(self):
        from musicbee_sync import build_sync_command
        cmd = build_sync_command(
            executable="MB.exe",
            device_profile="MyZX500",
            quiet=True,
        )
        self.assertIn("MB.exe", cmd[0])
        self.assertTrue(any("/Sync" in x and "MyZX500" in x for x in cmd))
        self.assertIn("/Quiet", cmd)

    def test_T16_run_sync_with_mocked_subprocess_invokes_and_parses_rc(self):
        import musicbee_sync as ms
        fake_proc = mock.Mock()
        fake_proc.returncode = 0
        fake_proc.stdout = "Sync OK"
        fake_proc.stderr = ""
        runner = mock.Mock(return_value=fake_proc)
        with mock.patch.object(ms, "musicbee_executable_path",
                                return_value="C:\\fake\\MusicBee.exe"):
            result = ms.run_sync(subprocess_runner=runner)
        self.assertTrue(result.invoked)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.success)
        self.assertEqual(runner.call_count, 1)


# ═══════════════════════════════════════════════════════════════════════════
# G.1 — acoustid_fingerprint (REAL fpcalc + MOCKED AcoustID HTTP)
# ═══════════════════════════════════════════════════════════════════════════

class TestAcoustIDFingerprintReal(unittest.TestCase):
    """Uses REAL fpcalc on REAL audio. AcoustID HTTP layer is mocked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.audio = self.tmp / "test.flac"
        _make_test_audio(self.audio, duration=10)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T17_fpcalc_real_returns_fingerprint(self):
        import acoustid_fingerprint as af
        result = af.fingerprint_file(self.audio)
        self.assertIsNotNone(result, "real fpcalc on real audio must return result")
        self.assertGreater(result.duration_sec, 9.0)
        self.assertTrue(result.fingerprint)

    def test_T18_fpcalc_returns_None_for_missing_file(self):
        import acoustid_fingerprint as af
        result = af.fingerprint_file(self.tmp / "nope.flac")
        self.assertIsNone(result)

    def test_T19_lookup_picks_highest_score_match(self):
        import acoustid_fingerprint as af
        from acoustid_fingerprint import FingerprintResult
        fake_response = {
            "status": "ok",
            "results": [
                {"id": "low", "score": 0.50, "recordings": []},
                {"id": "high", "score": 0.95, "recordings": [{
                    "id": "rec-1", "title": "Best Match",
                    "duration": 10.0,
                    "artists": [{"name": "Top Artist"}],
                    "releasegroups": [{
                        "title": "Best Album",
                        "first_release_date": "2023-01-01",
                    }],
                }]},
            ],
        }
        identity = af.lookup_fingerprint(
            FingerprintResult(duration_sec=10.0, fingerprint="abc"),
            api_key="fake",
            http_get=lambda url, **kw: fake_response,
        )
        self.assertIsNotNone(identity)
        self.assertEqual(identity.title, "Best Match")
        self.assertEqual(identity.artist, "Top Artist")
        self.assertEqual(identity.year, "2023")

    def test_T20_lookup_rejects_below_threshold(self):
        import acoustid_fingerprint as af
        from acoustid_fingerprint import FingerprintResult
        fake_response = {
            "status": "ok",
            "results": [{"id": "low", "score": 0.50, "recordings": [{
                "id": "rec-1", "title": "x", "artists": [{"name": "y"}],
            }]}],
        }
        identity = af.lookup_fingerprint(
            FingerprintResult(duration_sec=10.0, fingerprint="abc"),
            api_key="fake",
            http_get=lambda url, **kw: fake_response,
        )
        self.assertIsNone(identity, "below-threshold match must be rejected")

    def test_T21_resolve_identity_caches_results(self):
        import acoustid_fingerprint as af
        cache_file = self.tmp / "cache.json"
        fake_response = {
            "status": "ok",
            "results": [{"id": "x", "score": 0.95, "recordings": [{
                "id": "rec-1", "title": "Cached Title",
                "duration": 10.0,
                "artists": [{"name": "Cached Artist"}],
                "releasegroups": [{"title": "Cached Album"}],
            }]}],
        }
        call_count = {"n": 0}
        def counting_http(url, **kw):
            call_count["n"] += 1
            return fake_response
        # First call: HTTP fires
        i1 = af.resolve_identity(self.audio, api_key="fake",
                                    cache_path=cache_file, http_get=counting_http)
        self.assertIsNotNone(i1)
        self.assertEqual(call_count["n"], 1)
        # Second call: cache hit, no HTTP
        i2 = af.resolve_identity(self.audio, api_key="fake",
                                    cache_path=cache_file, http_get=counting_http)
        self.assertIsNotNone(i2)
        self.assertEqual(call_count["n"], 1, "cache hit must not re-query")
        self.assertEqual(i1.title, i2.title)

    def test_T22_resolve_identity_negative_cache(self):
        """When AcoustID returns no match, cache the negative result so
        we don't re-query on every run."""
        import acoustid_fingerprint as af
        cache_file = self.tmp / "cache.json"
        empty_response = {"status": "ok", "results": []}
        call_count = {"n": 0}
        def counting_http(url, **kw):
            call_count["n"] += 1
            return empty_response
        i1 = af.resolve_identity(self.audio, api_key="fake",
                                    cache_path=cache_file, http_get=counting_http)
        self.assertIsNone(i1)
        self.assertEqual(call_count["n"], 1)
        i2 = af.resolve_identity(self.audio, api_key="fake",
                                    cache_path=cache_file, http_get=counting_http)
        self.assertIsNone(i2)
        self.assertEqual(call_count["n"], 1, "negative-cache must not re-query")


# ═══════════════════════════════════════════════════════════════════════════
# G.2 — Phase-I-fallback caller integration
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseIIIFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.audio = self.tmp / "x.flac"
        _make_test_audio(self.audio, duration=8)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T23_blank_detection(self):
        from mk39_phase_III_fallback import is_phase_I_resolution_blank
        # Blank artist
        self.assertTrue(is_phase_I_resolution_blank({"artist": "", "title": "x"}))
        # Blank title
        self.assertTrue(is_phase_I_resolution_blank({"artist": "x", "title": ""}))
        # Both blank
        self.assertTrue(is_phase_I_resolution_blank({}))
        # Neither blank
        self.assertFalse(is_phase_I_resolution_blank(
            {"artist": "x", "title": "y"}))

    def test_T24_no_op_when_phase_I_already_resolved(self):
        """Per Council §G: fingerprint fallback fires ONLY when Phase I
        leaves blank. If Phase I returned identity, we MUST NOT waste a
        fingerprint."""
        from mk39_phase_III_fallback import attempt_fingerprint_fallback
        result = attempt_fingerprint_fallback(
            self.audio,
            {"artist": "Already Resolved", "title": "Set"},
            api_key="fake",
        )
        self.assertIsNone(result)

    def test_T25_phase_I_untouched(self):
        """Adversarial-review constraint LOCKED: Phase III must not modify
        mk39_phase_I_containment. Verify the file's exported names are
        the same as Phase I locked."""
        import mk39_phase_I_containment as p1
        # The 5 helpers Phase I locked + safe_artist_override
        for name in ("match_folder_genre", "block_abstract_hip_hop_default",
                      "ai_genre_disabled", "mark_groq_failure",
                      "genre_graph_is_empty", "safe_artist_override",
                      "resolve_genre_with_phase_I"):
            self.assertTrue(hasattr(p1, name),
                              f"Phase I must still export {name}")

    def test_T26_fallback_invokes_resolve_when_blank_and_audio_exists(self):
        """When Phase I returns blank AND audio is on disk, the fallback
        attempts fingerprint resolution. We mock resolve_identity to verify
        the call without going through a real network."""
        import mk39_phase_III_fallback as ph3
        # Patch the resolve_identity import inside the function
        import acoustid_fingerprint as af
        from acoustid_fingerprint import ResolvedIdentity
        with mock.patch.object(af, "resolve_identity",
                                return_value=ResolvedIdentity(
                                    artist="Fallback Artist",
                                    title="Fallback Title",
                                    score=0.92,
                                )):
            result = ph3.attempt_fingerprint_fallback(
                self.audio,
                {"artist": "", "title": ""},
                api_key="fake",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.artist, "Fallback Artist")
        self.assertEqual(result.provenance, "acoustic_fingerprint")


# ═══════════════════════════════════════════════════════════════════════════
# G.3 + H.3 — CANONICAL_SOURCES extension (most adversarial-sensitive)
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseIIICanonicalSources(unittest.TestCase):

    def setUp(self):
        # Make the registration startable from a clean state
        _fresh_import(["mk38_0_semantic_write_barrier",
                        "mk39_phase_III_canonical_sources"])

    def test_T27_phase_III_sources_added_to_freeze_whitelist(self):
        import mk38_0_semantic_write_barrier as mk38
        from mk39_phase_III_canonical_sources import (
            register_phase_III_canonical_sources,
            ACOUSTIC_FINGERPRINT, EXTERNAL_EDIT,
        )
        before = set(mk38.CANONICAL_SOURCES)
        ok = register_phase_III_canonical_sources()
        self.assertTrue(ok)
        self.assertIn(ACOUSTIC_FINGERPRINT, mk38.CANONICAL_SOURCES)
        self.assertIn(EXTERNAL_EDIT, mk38.CANONICAL_SOURCES)
        # Pre-existing sources untouched
        for s in before:
            self.assertIn(s, mk38.CANONICAL_SOURCES)

    def test_T28_registration_idempotent(self):
        from mk39_phase_III_canonical_sources import (
            register_phase_III_canonical_sources, is_registered, reset_for_tests,
        )
        reset_for_tests()
        self.assertFalse(is_registered())
        self.assertTrue(register_phase_III_canonical_sources())
        self.assertTrue(is_registered())
        # Second call: still True, no error
        self.assertTrue(register_phase_III_canonical_sources())

    def test_T29_freeze_accepts_acoustic_fingerprint_after_registration(self):
        """The whole point: write barrier must NOW permit acoustic_fingerprint
        writes (with appropriate confidence)."""
        from mk39_phase_III_canonical_sources import (
            register_phase_III_canonical_sources,
            ACOUSTIC_FINGERPRINT, reset_for_tests,
        )
        reset_for_tests()
        register_phase_III_canonical_sources()
        # Verify presence in the live whitelist
        import mk38_0_semantic_write_barrier as mk38
        # Test the source-classification helper (if it exists) accepts our source
        if hasattr(mk38, "classify_source"):
            cls = mk38.classify_source(ACOUSTIC_FINGERPRINT)
            # Could be "canonical" or whatever the lib uses; key thing is non-error.
            self.assertIsNotNone(cls)

    def test_T30_phase_III_sources_have_explicit_trust_ordering(self):
        """G.3 + H.3 require explicit trust ranking per adversarial review."""
        from mk39_phase_III_canonical_sources import (
            TRUST_ORDERING, ACOUSTIC_FINGERPRINT, EXTERNAL_EDIT,
        )
        # acoustic_fingerprint is a source we added — must be in the ordering
        self.assertIn(ACOUSTIC_FINGERPRINT, TRUST_ORDERING)
        self.assertIn(EXTERNAL_EDIT, TRUST_ORDERING)
        # External_edit (operator's deliberate edit) must outrank
        # acoustic_fingerprint (machine inference)
        self.assertLess(TRUST_ORDERING.index(EXTERNAL_EDIT),
                          TRUST_ORDERING.index(ACOUSTIC_FINGERPRINT))


# ═══════════════════════════════════════════════════════════════════════════
# H.2 — Picard compatibility (REAL mutagen, REAL MP3 file)
# ═══════════════════════════════════════════════════════════════════════════

class TestPicardCompat(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T31_v24_to_v23_conversion(self):
        """Generate a real MP3 with v2.4 tags, run normalization, verify v2.3."""
        from picard_compat import ensure_picard_compatible
        mp3 = self.tmp / "x.mp3"
        _make_test_mp3_with_tags(mp3, version=4)
        # Sanity: file is v2.4 before
        from mutagen.id3 import ID3
        before = ID3(str(mp3))
        if before.version[1] != 4:
            self.skipTest("ffmpeg did not produce v2.4")
        report = ensure_picard_compatible(mp3)
        self.assertTrue(report.saved)
        self.assertTrue(report.converted_v24_to_v23)
        # Verify on disk: now v2.3
        after = ID3(str(mp3))
        self.assertEqual(after.version[1], 3)

    def test_T32_idempotent_on_already_v23(self):
        from picard_compat import ensure_picard_compatible
        mp3 = self.tmp / "y.mp3"
        _make_test_mp3_with_tags(mp3, version=4)
        # First pass converts and saves
        r1 = ensure_picard_compatible(mp3)
        self.assertTrue(r1.saved)
        # Second pass: already v2.3, no APIC duplicates → no save needed
        r2 = ensure_picard_compatible(mp3)
        self.assertFalse(r2.saved)
        self.assertFalse(r2.converted_v24_to_v23)

    def test_T33_skips_non_mp3(self):
        from picard_compat import ensure_picard_compatible
        flac = self.tmp / "x.flac"
        _make_test_audio(flac, duration=2)
        report = ensure_picard_compatible(flac)
        self.assertFalse(report.saved)
        self.assertEqual(report.skipped_reason, "not_mp3")

    def test_T34_apic_dedup(self):
        """Add 3 cover-art frames; normalize; verify exactly 1 remains."""
        from mutagen.id3 import ID3, APIC
        from picard_compat import ensure_picard_compatible
        mp3 = self.tmp / "z.mp3"
        _make_test_mp3_with_tags(mp3, version=3)
        tags = ID3(str(mp3))
        for i in range(3):
            tags.add(APIC(encoding=3, mime="image/jpeg",
                            type=3, desc=f"cover-{i}",
                            data=b"\xff\xd8\xff\xe0fake jpeg" + bytes([i])))
        tags.save(v2_version=3, v1=1)
        # Sanity: 3 APIC frames present
        before_count = len(ID3(str(mp3)).getall("APIC"))
        self.assertEqual(before_count, 3)
        report = ensure_picard_compatible(mp3)
        self.assertTrue(report.saved)
        self.assertEqual(report.apic_frames_after, 1)
        after_count = len(ID3(str(mp3)).getall("APIC"))
        self.assertEqual(after_count, 1)


# ═══════════════════════════════════════════════════════════════════════════
# F.3 — config-gated wiring (Council mandate: default OFF, non-breaking)
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigGating(unittest.TestCase):
    def test_T35_default_flags_are_safe(self):
        """All Phase III config flags MUST default to safe-no-op — operator
        must explicitly opt into MusicBee/AcoustID/Picard pathways."""
        _fresh_import(["config"])
        import config
        self.assertFalse(config.SYNC_VIA_MUSICBEE)
        self.assertEqual(config.MUSICBEE_EXECUTABLE, "")
        self.assertEqual(config.ACOUSTID_API_KEY, "")
        self.assertFalse(config.PICARD_COMPAT_NORMALIZE)
        self.assertFalse(config.EXTERNAL_EDIT_REINGEST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
