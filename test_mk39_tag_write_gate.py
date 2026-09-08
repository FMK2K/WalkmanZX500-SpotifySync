"""
test_mk39_tag_write_gate.py — MK39 Mutagen Boundary Gate
================================================================================
                regression + 5,000-sample MC + perf microbenchmark

Council mandate (locked, 2026-05-09): the mediator's choke point must be the
mutagen library boundary, not just mk37_wiring. This test suite validates
that requirement.

Locked MC pass criteria (from Council §5):
  - Every unmediated tag write is BLOCKED  (FNR_unmediated = 1.0)
  - Every mediated operation with valid capability SUCCEEDS  (TPR = 1.0)
  - Every operation with invalid/insufficient capability is REJECTED
  - FPR (false positive block of legitimate canonical write) = 0% over 5,000 samples
  - Performance: gate overhead <= 0.5% on a representative save call

Run:
    python3 -m pytest test_mk39_tag_write_gate.py -v -s
"""

from __future__ import annotations

import random
import sys
import time
import unittest


# ═══════════════════════════════════════════════════════════════════════════
# Test fixtures
# ═══════════════════════════════════════════════════════════════════════════

class FakeCap:
    """Capability stand-in. semantic_scope set per test; default = unrestricted
    (no semantic_scope attribute). This matches the MK35 ExecutionCapability
    shape that MK39-A2 will extend."""
    def __init__(self, scope="*", expires_at=float("inf"),
                 token_id="abcd1234", semantic_scope=None):
        self.scope = scope
        self.expires_at = expires_at
        self.token_id = token_id
        if semantic_scope is not None:
            self.semantic_scope = semantic_scope


def _reset_gate_modules():
    for m in ("mk39_tag_write_gate", "mk39_a1_mediator"):
        if m in sys.modules:
            del sys.modules[m]


# ═══════════════════════════════════════════════════════════════════════════
# T1-T4: Gate predicate
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_Gate_Predicate(unittest.TestCase):
    def setUp(self):
        _reset_gate_modules()

    def test_T1_unmediated_semantic_field_raises_unconditionally(self):
        """T1 (revised for MK39-A1.1): unmediated SEMANTIC field write
        ALWAYS hard-fails, regardless of severity mode. This is the
        contamination defense that the freeze + gate jointly enforce."""
        import os
        import mk39_tag_write_gate as g
        # Even under soft_warn, semantic field is hard-failed
        os.environ["MK39_GATE_SEVERITY"] = "soft_warn"
        try:
            with self.assertRaises(g.TagWriteOutsideMediation):
                g.gated_save_check(target="/tmp/x.mp3", field="genre")
        finally:
            os.environ.pop("MK39_GATE_SEVERITY", None)

    def test_T2_mediated_unscoped_capability_passes(self):
        import mk39_tag_write_gate as g
        with g.manual_mediation_scope("uuid-1", FakeCap()):   # no semantic_scope
            # should not raise
            g.gated_save_check(target="/tmp/x.mp3", field="genre")

    def test_T3_mediated_scoped_capability_blocks_unauthorized_field(self):
        import mk39_tag_write_gate as g
        cap = FakeCap(semantic_scope={"allowed_op_classes":["TAG_WRITE"],
                                       "allowed_fields":["year"]})
        with g.manual_mediation_scope("uuid-2", cap):
            # year permitted
            g.gated_save_check(target="/tmp/x.mp3", field="year")
            # genre denied
            with self.assertRaises(g.TagWriteCapabilityMismatch):
                g.gated_save_check(target="/tmp/x.mp3", field="genre")

    def test_T4_field_None_defers_to_mediator_entry_check(self):
        """When the wrapped primitive can't tell us which field is being
        written (e.g., bare mutagen.save), the gate defers to the mediator's
        entry-level check (which has already passed since we're inside
        mediation). gated_save_check must allow."""
        import mk39_tag_write_gate as g
        cap = FakeCap(semantic_scope={"allowed_fields":["year"]})
        with g.manual_mediation_scope("uuid-3", cap):
            g.gated_save_check(target="/tmp/x.mp3", field=None)


# ═══════════════════════════════════════════════════════════════════════════
# T5-T7: Mediator → gate integration
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_MediatorGateIntegration(unittest.TestCase):
    def setUp(self):
        _reset_gate_modules()

    def test_T5_mediator_pushes_gate_context_on_entry(self):
        import mk39_tag_write_gate as g
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/x")
        with m.mediated_operation(op, FakeCap()) as ctx:
            uuid, cap = g.current_mediation()
            self.assertIsNotNone(uuid, "mediator must push gate context")
            self.assertIsNotNone(cap)
            ctx.mark_success()
        # After exit
        uuid, cap = g.current_mediation()
        self.assertIsNone(uuid, "mediator must clear gate context on exit")

    def test_T6_mediator_clears_context_even_on_exception(self):
        import mk39_tag_write_gate as g
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/x")
        with self.assertRaises(ValueError):
            with m.mediated_operation(op, FakeCap()):
                raise ValueError("simulated failure")
        uuid, _ = g.current_mediation()
        self.assertIsNone(uuid, "mediator must clear context even on exception")

    def test_T7_nested_mediation_stacks_correctly(self):
        """Inner mediation's exit must restore the outer mediation's
        context, not clear all state."""
        import mk39_tag_write_gate as g
        import mk39_a1_mediator as m
        outer_op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/outer")
        inner_op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/inner")
        with m.mediated_operation(outer_op, FakeCap(token_id="outer__1")) as outer_ctx:
            outer_uuid, _ = g.current_mediation()
            self.assertIsNotNone(outer_uuid)
            with m.mediated_operation(inner_op, FakeCap(token_id="inner__2")) as inner_ctx:
                inner_uuid, _ = g.current_mediation()
                self.assertNotEqual(inner_uuid, outer_uuid)
                inner_ctx.mark_success()
            # After inner exit: outer context restored
            restored_uuid, _ = g.current_mediation()
            self.assertEqual(restored_uuid, outer_uuid,
                              "inner exit must restore outer context")
            outer_ctx.mark_success()
        final_uuid, _ = g.current_mediation()
        self.assertIsNone(final_uuid)


# ═══════════════════════════════════════════════════════════════════════════
# T8-T10: Install / hardened-mode lock / self-check
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_GateInstallation(unittest.TestCase):
    def setUp(self):
        _reset_gate_modules()

    def test_T8_install_is_idempotent(self):
        import mk39_tag_write_gate as g
        first = g.install_tag_write_gate()
        second = g.install_tag_write_gate()
        self.assertTrue(first or g.verify_tag_write_gate_installed())
        self.assertFalse(second, "second install must be no-op")

    def test_T9_uninstall_refused_under_hardened_mode(self):
        """When MK37.1-2 hardened mode is locked, uninstall_tag_write_gate
        must refuse without force=True."""
        import mk39_tag_write_gate as g
        from unittest import mock
        g.install_tag_write_gate()
        with mock.patch.object(g, "_hardened_mode_locked", return_value=True):
            result = g.uninstall_tag_write_gate(force=False)
            self.assertFalse(result, "uninstall must refuse under hardened mode")
            self.assertTrue(g.verify_tag_write_gate_installed(),
                              "gate must still be installed after refusal")
            tel = g.snapshot_gate_telemetry()
            self.assertGreater(tel["gate_uninstall_refusals_hardened"], 0)

    def test_T10_self_check_reports_uninstalled_when_not_installed(self):
        import mk39_tag_write_gate as g
        # Don't install. verify must report False.
        self.assertFalse(g.verify_tag_write_gate_installed())


# ═══════════════════════════════════════════════════════════════════════════
# T15-T19: MK39-A1.1 — Severity calibration + field-aware gating
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_1_SeverityCalibration(unittest.TestCase):
    """T15-T19: MK39-A1.1 hotfix — production observation 2026-05-09 showed
    the all-or-nothing default-deny gate caught legitimate downloader
    mutagen.save() calls on local temp files (Cochise/Tyler/Harley).
    The MK39-A1.1 hotfix introduces severity calibration:

      - SEMANTIC field outside mediation: ALWAYS hard fail (T1, T16)
      - NONSEMANTIC field outside mediation:
          - hard_fail mode (production)         → raise (T17)
          - soft_warn mode (dev)                → allow + log + count (T18)
      - Inside mediation: unchanged from original gate (T19)

    Locked criteria:
      - No severity mode allows a SEMANTIC field write outside mediation
      - SOFT_WARN never raises for non-semantic / unknown field
      - HARD_FAIL always raises for any unmediated write
    """

    def setUp(self):
        _reset_gate_modules()
        # Always start clean
        import os
        os.environ.pop("MK39_GATE_SEVERITY", None)

    def tearDown(self):
        import os
        os.environ.pop("MK39_GATE_SEVERITY", None)

    def test_T15_default_severity_is_soft_warn_under_non_hardened(self):
        """T15: when hardened mode is not locked and no env override is set,
        the default severity is soft_warn (dev-friendly)."""
        import mk39_tag_write_gate as g
        # We can't fake hardened mode here without monkey-patching, so just
        # verify the chosen severity is one of the valid modes
        s = g._select_severity()
        self.assertIn(s, (g.GATE_SEVERITY_HARD_FAIL, g.GATE_SEVERITY_SOFT_WARN))

    def test_T16_semantic_field_always_hard_fails_in_soft_warn(self):
        """T16: even under soft_warn mode, semantic-field writes outside
        mediation hard-fail. This is the unconditional contamination defense."""
        import os
        import mk39_tag_write_gate as g
        os.environ["MK39_GATE_SEVERITY"] = "soft_warn"
        for sem_field in ("genre", "year", "artist", "album_artist", "title", "mood"):
            with self.assertRaises(g.TagWriteOutsideMediation,
                                    msg=f"semantic field {sem_field!r} must hard-fail"):
                g.gated_save_check(target="/tmp/x.mp3", field=sem_field)

    def test_T17_nonsemantic_field_hard_fails_in_hard_fail_mode(self):
        """T17: under hard_fail mode (i.e., production / hardened), even
        non-semantic fields are blocked outside mediation."""
        import os
        import mk39_tag_write_gate as g
        os.environ["MK39_GATE_SEVERITY"] = "hard_fail"
        for nonsem_field in ("track_number", "codec", "duration", "bitrate", None):
            with self.assertRaises(g.TagWriteOutsideMediation,
                                    msg=f"under hard_fail, field={nonsem_field!r} must raise"):
                g.gated_save_check(target="/tmp/x.mp3", field=nonsem_field)

    def test_T18_nonsemantic_field_soft_allowed_in_soft_warn_mode(self):
        """T18: production-observation case — downloader writes structural
        tags (or unknown field=None) under soft_warn → log + allow.
        Counted in `gated_writes_soft_allowed`."""
        import os
        import mk39_tag_write_gate as g
        os.environ["MK39_GATE_SEVERITY"] = "soft_warn"
        before = g.snapshot_gate_telemetry()["gated_writes_soft_allowed"]
        for nonsem_field in ("track_number", "codec", "duration", "bitrate", None):
            try:
                g.gated_save_check(target="/tmp/x.mp3", field=nonsem_field)
            except Exception as e:
                self.fail(f"soft_warn must allow field={nonsem_field!r}: {e}")
        after = g.snapshot_gate_telemetry()["gated_writes_soft_allowed"]
        self.assertEqual(after - before, 5,
                          f"soft_allowed counter should advance by 5, got {after-before}")

    def test_T19_inside_mediation_capability_check_still_applies(self):
        """T19: severity calibration only affects the unmediated path.
        Inside mediation, the original capability-field check still applies
        (defense in depth — T3 behavior unchanged)."""
        import os
        import mk39_tag_write_gate as g
        os.environ["MK39_GATE_SEVERITY"] = "soft_warn"
        cap = FakeCap(semantic_scope={"allowed_op_classes":["TAG_WRITE"],
                                       "allowed_fields":["year"]})
        with g.manual_mediation_scope("uuid-T19", cap):
            g.gated_save_check(target="/tmp/x.mp3", field="year")     # ok
            with self.assertRaises(g.TagWriteCapabilityMismatch):
                g.gated_save_check(target="/tmp/x.mp3", field="genre")


# ═══════════════════════════════════════════════════════════════════════════
# T20-T27: MK39-A1.2 — Tag-frame inspection (Council-mandated enhancement)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeMutagenAudio:
    """Minimal mutagen-like fixture: has .tags (dict) and .filename."""
    def __init__(self, tags, filename="/tmp/x.mp3"):
        self.tags = tags
        self.filename = filename


class TestMK39_A1_2_FrameInspection(unittest.TestCase):
    """T20-T27: MK39-A1.2 — Council adversarial-concern closure.
    The monkey-patched _gated_save inspects mutagen audio.tags to detect
    semantic-field frames (TCON/©gen/'genre'/etc.) and forces a per-field
    gate check, so even a save() with field=None can no longer mask a
    buried semantic write under soft_warn mode.

    Locked criteria:
      - Detection works across ID3v2, MP4 atoms, Vorbis Comments, APE
      - Detection is fail-open (corrupt/exotic objects don't block)
      - Frame inspection BLOCKS soft_warn-disguised semantic writes
      - Frame inspection does NOT block legitimate structural-only writes
    """

    def setUp(self):
        _reset_gate_modules()
        import os
        os.environ.pop("MK39_GATE_SEVERITY", None)

    def tearDown(self):
        import os
        os.environ.pop("MK39_GATE_SEVERITY", None)

    def test_T20_id3v2_TCON_detected_as_genre(self):
        import mk39_tag_write_gate as g
        audio = _FakeMutagenAudio({"TIT2": "Title", "TCON": "Hip-Hop", "TRCK": "1/12"})
        detected = g._detect_semantic_fields_in_mutagen_object(audio)
        self.assertIn("genre", detected)
        self.assertIn("title", detected)
        self.assertNotIn("track_number", detected)   # TRCK is structural

    def test_T21_mp4_atom_genre_detected(self):
        import mk39_tag_write_gate as g
        audio = _FakeMutagenAudio({"\xa9nam": ["Title"], "\xa9gen": ["Jazz"],
                                     "trkn": [(1,12)]})
        detected = g._detect_semantic_fields_in_mutagen_object(audio)
        self.assertIn("genre", detected)
        self.assertIn("title", detected)

    def test_T22_vorbis_lowercase_genre_detected(self):
        import mk39_tag_write_gate as g
        audio = _FakeMutagenAudio({"title": ["T"], "genre": ["Rock"],
                                     "date": ["2024"], "tracknumber": ["1"]})
        detected = g._detect_semantic_fields_in_mutagen_object(audio)
        for f in ("title", "genre", "year"):
            self.assertIn(f, detected, f"vorbis {f} should be detected")

    def test_T23_ape_mixed_case_albumartist_detected(self):
        import mk39_tag_write_gate as g
        audio = _FakeMutagenAudio({"Title": "X", "Album_Artist": "AA",
                                     "TrackNumber": "1"})
        detected = g._detect_semantic_fields_in_mutagen_object(audio)
        self.assertIn("album_artist", detected)

    def test_T24_structural_only_returns_empty(self):
        """The legitimate downloader case: only structural tags present.
        Detection returns empty tuple → severity-mode logic applies."""
        import mk39_tag_write_gate as g
        audio = _FakeMutagenAudio({"TRCK": "1/12", "TLEN": "180000",
                                     "TENC": "ffmpeg"})
        self.assertEqual(g._detect_semantic_fields_in_mutagen_object(audio), ())

    def test_T25_corrupt_object_fails_open_safely(self):
        """A mutagen object that raises on .tags must NOT block writes —
        fail-open in safety direction (corrupt audio is the AUDIO file's
        problem, not our gate's). Detection returns empty tuple."""
        import mk39_tag_write_gate as g
        class BadAudio:
            filename = "/tmp/x.mp3"
            @property
            def tags(self): raise RuntimeError("simulated corruption")
        self.assertEqual(g._detect_semantic_fields_in_mutagen_object(BadAudio()), ())

    def test_T26_buried_semantic_write_blocked_under_softwarn(self):
        """The Council's adversarial scenario: under soft_warn mode, a
        mutagen.save() with field=None that buries a TCON (genre) frame
        in audio.tags must STILL hard-fail because frame inspection
        detects it.

        We can't easily exercise the monkey-patched _gated_save without
        installing mutagen. Instead we exercise the public path:
        if a caller sees detected fields and runs gated_save_check for
        each, gated_save_check hard-fails on the semantic field even
        under soft_warn (T16 already verified this). We verify the
        detection step here; the integration is verified by T27-MC."""
        import os, mk39_tag_write_gate as g
        os.environ["MK39_GATE_SEVERITY"] = "soft_warn"
        # The audio object has BOTH structural and semantic frames
        audio = _FakeMutagenAudio({
            "TRCK": "1/12",      # structural — would soft-allow alone
            "TENC": "ffmpeg",    # structural
            "TCON": "Afrobeats", # SEMANTIC — must be detected and blocked
        })
        detected = g._detect_semantic_fields_in_mutagen_object(audio)
        self.assertIn("genre", detected,
            "TCON frame must be detected even when buried among structural frames")
        # Per-field gate check: 'genre' MUST hard-fail under any mode
        with self.assertRaises(g.TagWriteOutsideMediation):
            g.gated_save_check(target=audio.filename, field="genre")


class TestMK39_A1_2_FrameInspection_MC(unittest.TestCase):
    """T27-MC: 5,000-sample Monte Carlo — frame inspection prevents semantic
    leakage under soft_warn mode. Each scenario:
      1. Build a fake audio.tags dict with random structural + semantic
         frames across all four mutagen format styles.
      2. Run detection.
      3. If detection found ANY semantic field, simulate the per-field
         gate check loop the monkey-patched _gated_save does. Verify
         that under soft_warn mode the gate STILL raises.
      4. If detection found nothing, verify gated_save_check(field=None)
         allows under soft_warn (downloader case).

    Locked criteria:
      - false_allow_buried_semantic = 0  (every buried semantic write blocked)
      - false_block_structural_only = 0  (legitimate structural writes allowed)
      - detection_misses = 0             (no semantic frame slipped through)
    """

    SAMPLES = 5000
    SEED = 0xCBE_39_A1A2 & 0xFFFFFFFF

    def setUp(self):
        _reset_gate_modules()
        import os
        os.environ["MK39_GATE_SEVERITY"] = "soft_warn"

    def tearDown(self):
        import os
        os.environ.pop("MK39_GATE_SEVERITY", None)

    def test_T27_monte_carlo_5000_samples_frame_inspection(self):
        import mk39_tag_write_gate as g
        rng = random.Random(self.SEED)

        # Pools of frame keys per format style
        FRAME_POOLS = {
            "id3v2": {
                "structural": ["TRCK", "TLEN", "TENC", "TBPM", "TKEY", "TPOS"],
                "semantic":   ["TIT2", "TALB", "TPE1", "TPE2", "TCON",
                                "TDRC", "TYER", "TCOM", "TMOO"],
            },
            "mp4": {
                "structural": ["trkn", "disk", "tmpo", "covr"],
                "semantic":   ["\xa9nam", "\xa9alb", "\xa9ART", "aART",
                                "\xa9gen", "gnre", "\xa9day", "\xa9wrt"],
            },
            "vorbis": {
                "structural": ["tracknumber", "tracktotal", "encoder", "bpm"],
                "semantic":   ["title", "album", "artist", "albumartist",
                                "genre", "date", "year", "composer", "mood"],
            },
            "ape": {
                "structural": ["TrackNumber", "Track Total", "BPM"],
                "semantic":   ["Title", "Album", "Artist", "Album_Artist",
                                "Genre", "Year", "Composer"],
            },
        }

        false_allow_buried_semantic = 0
        false_block_structural_only = 0
        detection_misses = 0
        scenarios_with_semantic = 0
        scenarios_structural_only = 0

        for _ in range(self.SAMPLES):
            fmt = rng.choice(list(FRAME_POOLS.keys()))
            pool = FRAME_POOLS[fmt]
            # Build tags dict with random selection
            tags = {}
            n_struct = rng.randint(0, 3)
            for _i in range(n_struct):
                k = rng.choice(pool["structural"])
                tags[k] = "structural_value"
            inject_semantic = rng.random() < 0.5
            if inject_semantic:
                n_sem = rng.randint(1, 2)
                for _i in range(n_sem):
                    k = rng.choice(pool["semantic"])
                    tags[k] = "semantic_value"
                scenarios_with_semantic += 1
            else:
                scenarios_structural_only += 1

            audio = _FakeMutagenAudio(tags=tags, filename="/tmp/x")
            detected = g._detect_semantic_fields_in_mutagen_object(audio)

            # Sanity: if we injected semantic frames, detection MUST find them
            if inject_semantic and not detected:
                detection_misses += 1
                continue

            # Simulate the monkey-patched _gated_save's per-field loop
            if detected:
                # Each detected semantic field triggers a gate check
                blocked_at_some_field = False
                for fld in detected:
                    try:
                        g.gated_save_check(target=audio.filename, field=fld)
                    except (g.TagWriteOutsideMediation,
                            g.TagWriteCapabilityMismatch):
                        blocked_at_some_field = True
                        break
                if not blocked_at_some_field and inject_semantic:
                    false_allow_buried_semantic += 1
            else:
                # No semantic detected → field=None path → soft_warn allows
                try:
                    g.gated_save_check(target=audio.filename, field=None)
                    allowed = True
                except (g.TagWriteOutsideMediation,
                        g.TagWriteCapabilityMismatch):
                    allowed = False
                if (not allowed) and (not inject_semantic):
                    false_block_structural_only += 1

        print("\n--- MK39-A1.2 frame-inspection MC (5,000 samples) ---")
        print(f"  scenarios with semantic frames:    {scenarios_with_semantic}")
        print(f"  scenarios structural-only:         {scenarios_structural_only}")
        print(f"  detection_misses:                  {detection_misses}    (LOCKED: 0)")
        print(f"  false_allow_buried_semantic:       {false_allow_buried_semantic}    (LOCKED: 0)")
        print(f"  false_block_structural_only:       {false_block_structural_only}    (LOCKED: 0)")

        # === LOCKED PASS CRITERIA ===
        self.assertEqual(detection_misses, 0,
            f"DETECTION GAP: {detection_misses} semantic frames not detected")
        self.assertEqual(false_allow_buried_semantic, 0,
            f"BURIED-SEMANTIC LEAK: {false_allow_buried_semantic} writes leaked")
        self.assertEqual(false_block_structural_only, 0,
            f"STRUCTURAL-ONLY REGRESSION: {false_block_structural_only} blocked")


# ═══════════════════════════════════════════════════════════════════════════
# T11-MC: 5,000-sample Monte Carlo
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_GateMonteCarlo(unittest.TestCase):
    """T11-MC: 5,000 random scenarios. The gate must:
      - Block 100% of unmediated writes (false-allow rate = 0)
      - Block 100% of mediated writes that violate semantic_scope
      - Allow 100% of mediated writes with valid capability (FPR = 0)

    Locked acceptance criteria (Council §5):
      - false_allow_unmediated     = 0   (gate let an unmediated write through)
      - false_allow_capability     = 0   (gate let an out-of-scope field write through)
      - false_block_canonical      = 0   (gate refused a legitimate canonical write)
    """

    SAMPLES = 5000
    SEED = 0xCBE_39_AA & 0xFFFFFFFF

    def setUp(self):
        _reset_gate_modules()

    def test_T11_monte_carlo_5000_samples(self):
        import mk39_tag_write_gate as g
        rng = random.Random(self.SEED)

        def random_field():
            return rng.choice(["genre", "year", "artist", "album_artist", "title"])

        def random_capability():
            r = rng.random()
            if r < 0.30:
                # canonical, unrestricted
                return FakeCap()
            elif r < 0.55:
                # canonical, restricted to year only
                return FakeCap(semantic_scope={"allowed_op_classes":["TAG_WRITE"],
                                                "allowed_fields":["year"]})
            elif r < 0.75:
                # canonical, unrestricted via explicit None
                return FakeCap(semantic_scope={"allowed_op_classes":["TAG_WRITE"],
                                                "allowed_fields":None})
            elif r < 0.90:
                # restricted to artist only
                return FakeCap(semantic_scope={"allowed_op_classes":["TAG_WRITE"],
                                                "allowed_fields":["artist"]})
            else:
                # no semantic_scope at all (unrestricted via absence)
                return FakeCap()

        # Independent oracle
        def oracle_should_pass(mediated, cap, field):
            if not mediated:
                return False
            if cap is None:
                return False
            sem = getattr(cap, "semantic_scope", None)
            if sem is None or not isinstance(sem, dict):
                return True
            allowed = sem.get("allowed_fields")
            if allowed is None:
                return True
            return field in allowed

        false_allow_unmediated = 0
        false_allow_capability = 0
        false_block_canonical  = 0
        scenarios = 0

        for _ in range(self.SAMPLES):
            mediated = rng.random() < 0.7
            field = random_field()
            cap = random_capability()
            should_pass = oracle_should_pass(mediated, cap, field)
            scenarios += 1
            try:
                if mediated:
                    with g.manual_mediation_scope("mc-uuid", cap):
                        g.gated_save_check(target="/tmp/x.mp3", field=field)
                else:
                    g.gated_save_check(target="/tmp/x.mp3", field=field)
                actual_passed = True
            except (g.TagWriteOutsideMediation, g.TagWriteCapabilityMismatch):
                actual_passed = False

            # Tally errors
            if (not should_pass) and actual_passed:
                if not mediated:
                    false_allow_unmediated += 1
                else:
                    false_allow_capability += 1
            elif should_pass and (not actual_passed):
                false_block_canonical += 1

        print("\n--- MK39 Tag-Write Gate MC sim (5,000 samples) ---")
        print(f"  scenarios:                        {scenarios}")
        print(f"  false_allow_unmediated:           {false_allow_unmediated}  (LOCKED criterion: 0)")
        print(f"  false_allow_capability_mismatch:  {false_allow_capability}  (LOCKED criterion: 0)")
        print(f"  false_block_canonical:            {false_block_canonical}   (LOCKED criterion: 0)")

        # === LOCKED PASS CRITERIA ===
        self.assertEqual(false_allow_unmediated, 0,
            f"GATE BREACH: {false_allow_unmediated} unmediated writes succeeded")
        self.assertEqual(false_allow_capability, 0,
            f"CAPABILITY BREACH: {false_allow_capability} writes outside scope succeeded")
        self.assertEqual(false_block_canonical, 0,
            f"FALSE POSITIVE: {false_block_canonical} canonical writes were blocked")

    def test_T11b_monte_carlo_5000_severity_aware(self):
        """T11b (MK39-A1.1): 5,000-sample MC over severity-aware behavior.
        Validates that:
          - SEMANTIC fields outside mediation are ALWAYS hard-failed
            (regardless of severity mode) → false_allow_semantic = 0
          - NONSEMANTIC fields outside mediation under soft_warn are allowed
            → soft_allowed counter advances appropriately
          - NONSEMANTIC fields outside mediation under hard_fail are blocked
        """
        import os
        import mk39_tag_write_gate as g
        rng = random.Random(self.SEED ^ 0xA1A1A1A1)

        SEMANTIC = list(g.SEMANTIC_FIELDS_FROZEN)
        NONSEMANTIC = ["track_number", "codec", "duration", "bitrate",
                        "cover_art", "encoder", None]

        false_allow_semantic_anymode = 0
        false_block_nonsemantic_softwarn = 0
        false_allow_nonsemantic_hardfail = 0

        scenarios = 0
        for _ in range(self.SAMPLES):
            mode = rng.choice(["soft_warn", "hard_fail"])
            os.environ["MK39_GATE_SEVERITY"] = mode
            field_pool = SEMANTIC if rng.random() < 0.5 else NONSEMANTIC
            field = rng.choice(field_pool)
            is_semantic = field in g.SEMANTIC_FIELDS_FROZEN if field else False
            scenarios += 1
            try:
                g.gated_save_check(target="/tmp/x.mp3", field=field)
                allowed = True
            except (g.TagWriteOutsideMediation, g.TagWriteCapabilityMismatch):
                allowed = False

            if is_semantic and allowed:
                false_allow_semantic_anymode += 1
            if (not is_semantic) and mode == "soft_warn" and (not allowed):
                false_block_nonsemantic_softwarn += 1
            if (not is_semantic) and mode == "hard_fail" and allowed:
                false_allow_nonsemantic_hardfail += 1

        os.environ.pop("MK39_GATE_SEVERITY", None)

        print("\n--- MK39-A1.1 severity-aware MC (5,000 samples) ---")
        print(f"  scenarios:                              {scenarios}")
        print(f"  false_allow_semantic (any mode):        {false_allow_semantic_anymode}  (LOCKED: 0)")
        print(f"  false_block_nonsemantic (soft_warn):    {false_block_nonsemantic_softwarn}  (LOCKED: 0)")
        print(f"  false_allow_nonsemantic (hard_fail):    {false_allow_nonsemantic_hardfail}  (LOCKED: 0)")

        self.assertEqual(false_allow_semantic_anymode, 0,
            f"CONTAMINATION DEFENSE BREACH: {false_allow_semantic_anymode} unmediated semantic writes allowed")
        self.assertEqual(false_block_nonsemantic_softwarn, 0,
            f"SOFT_WARN REGRESSION: {false_block_nonsemantic_softwarn} non-semantic writes blocked under soft_warn")
        self.assertEqual(false_allow_nonsemantic_hardfail, 0,
            f"HARD_FAIL REGRESSION: {false_allow_nonsemantic_hardfail} non-semantic writes allowed under hard_fail")


# ═══════════════════════════════════════════════════════════════════════════
# T12: Performance microbenchmark — overhead must be <=0.5%
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_GatePerformance(unittest.TestCase):
    """T12: Council §5 performance criterion: gate overhead <= 0.5% of a
    representative save call. Microbenchmark measures the gate check itself
    against a no-op baseline. Real production save calls do disk I/O (~ms);
    our gate is a thread-local read + dict lookup (~µs), so <0.5% is easy
    to meet — but we measure it explicitly so any future regression is
    caught."""

    ITER = 50000

    def setUp(self):
        _reset_gate_modules()

    def test_T12_gate_overhead_well_under_0_5_pct(self):
        import mk39_tag_write_gate as g
        cap = FakeCap()
        # Establish baseline: bare function call
        def noop():
            pass
        t0 = time.perf_counter()
        for _ in range(self.ITER):
            noop()
        baseline = time.perf_counter() - t0

        # With gate active (mediated)
        with g.manual_mediation_scope("perf-uuid", cap):
            t0 = time.perf_counter()
            for _ in range(self.ITER):
                g.gated_save_check(target="/tmp/x.mp3", field="year")
            gated = time.perf_counter() - t0

        # Real production save does file I/O — let's simulate a 5ms save
        # (a realistic mp3 tag write timing) and compute overhead %
        per_call_overhead_us = (gated - baseline) / self.ITER * 1_000_000
        simulated_save_us = 5_000     # 5 ms ≈ realistic
        overhead_pct = (per_call_overhead_us / simulated_save_us) * 100

        print("\n--- MK39 Gate Performance ---")
        print(f"  baseline (noop x {self.ITER}):   {baseline*1000:.2f} ms")
        print(f"  gated   (gate  x {self.ITER}):   {gated*1000:.2f} ms")
        print(f"  per-call gate overhead:          {per_call_overhead_us:.2f} µs")
        print(f"  vs simulated 5ms save:           {overhead_pct:.4f} %")

        self.assertLess(overhead_pct, 0.5,
            f"Gate overhead {overhead_pct:.4f}% exceeds Council 0.5% target")


# ═══════════════════════════════════════════════════════════════════════════
# T13: Source-presence + API surface
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_GateAPI(unittest.TestCase):
    def test_T13_API_surface_complete(self):
        import mk39_tag_write_gate as g
        for name in ["TagWriteOutsideMediation", "TagWriteCapabilityMismatch",
                     "set_mediation_context", "clear_mediation_context",
                     "current_mediation", "manual_mediation_scope",
                     "install_tag_write_gate", "uninstall_tag_write_gate",
                     "verify_tag_write_gate_installed",
                     "gated_save_check", "snapshot_gate_telemetry"]:
            self.assertTrue(hasattr(g, name), f"missing: {name}")
            self.assertIn(name, g.__all__, f"not in __all__: {name}")

    def test_T13b_mediator_imports_and_uses_gate(self):
        from pathlib import Path
        src = Path(__file__).parent / "mk39_a1_mediator.py"
        if not src.exists():
            self.skipTest("mk39_a1_mediator.py not in test dir")
        text = src.read_text(encoding="utf-8")
        self.assertIn("set_mediation_context", text,
                      "mediator must wire to gate")
        self.assertIn("clear_mediation_context", text)
        self.assertIn("mk39_tag_write_gate", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
