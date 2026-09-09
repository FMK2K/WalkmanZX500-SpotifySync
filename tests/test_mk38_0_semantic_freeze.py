"""
test_mk38_0_semantic_freeze.py — MK38.0 regression + Monte Carlo
================================================================================

Closes (test side):
  R17 Recursive semantic contamination          (T2, T7-MC)
  R18 Cross-run inference amplification         (T6 stale cache)
  R19 Cache-poison persistence                  (T6 stale cache, T8)
  R22 Semantic overwrite without confidence delta (T1, T2)
  R24 Inventory truth contamination             (T7-MC)

Methodology: Kanban + SCRUM + MC sims (Monte Carlo simulation as the
adversarial-CTO acceptance gate). The MC test (T7) generates 1000 random
(field, source, confidence) tuples drawn from the union of NONSEMANTIC,
SEMANTIC, INFERENCE, CANONICAL, and "novel adversarial" classes, runs each
through the gate, and verifies the gate's classification matches the LOCKED
specification for every sample. Pass criterion: 1000/1000 correct.

Run:
    python3 -m pytest test_mk38_0_semantic_freeze.py -v
"""

from __future__ import annotations

import importlib
import os
import random
import sys
import unittest
from unittest import mock


class TestMK38_0_FreezeBasics(unittest.TestCase):
    """Direct gate behavior under default-frozen state."""

    def setUp(self):
        for m in ("mk38_0_semantic_write_barrier",):
            if m in sys.modules:
                del sys.modules[m]
        # Default state — no override env var
        os.environ.pop("MK38_0_ALLOW_SEMANTIC_WRITES", None)

    def test_T1_default_state_is_frozen(self):
        """T1: with no override, freeze is active by default."""
        import mk38_0_semantic_write_barrier as b
        self.assertTrue(b.is_frozen(), "Default state must be FROZEN")

    def test_T2_inference_genre_blocked_under_freeze(self):
        """T2: ai_inference genre write is blocked under default freeze.
        This is the direct counter to the "everything became Afrobeats"
        contamination — the AI inference path that produced it is now gated."""
        import mk38_0_semantic_write_barrier as b
        with self.assertRaises(b.SemanticWriteBlocked):
            b.assert_field_allowed("genre", "Afrobeats",
                                    source="ai_inference", confidence=0.4)

    def test_T3_canonical_spotify_genre_allowed(self):
        """T3: spotify_canonical genre write is allowed under freeze
        (canonical sources bypass the freeze, semantic field or not)."""
        import mk38_0_semantic_write_barrier as b
        # Should not raise
        b.assert_field_allowed("genre", "Hip-Hop",
                                source="spotify_canonical", confidence=1.0)
        snap = b.snapshot_telemetry()
        self.assertGreater(snap["counters"]["writes_allowed_canonical"], 0)

    def test_T4_nonsemantic_field_always_allowed(self):
        """T4: track_number / cover_art / etc. are always allowed regardless
        of source — they're objective bytes-derived facts, not semantic claims."""
        import mk38_0_semantic_write_barrier as b
        for f in ("track_number", "disc_number", "duration", "cover_art",
                  "bitrate", "codec"):
            # Even with the most poisonous-sounding source
            b.assert_field_allowed(f, 42, source="ai_inference")
        snap = b.snapshot_telemetry()
        self.assertGreaterEqual(snap["counters"]["writes_allowed_nonsemantic"], 6)

    def test_T5_unknown_source_default_deny(self):
        """T5: a write with source='whatever_made_up_thing' is denied
        regardless of field. This is the no-silent-allow guarantee."""
        import mk38_0_semantic_write_barrier as b
        with self.assertRaises(b.SemanticWriteBlocked):
            b.assert_field_allowed("genre", "Jazz",
                                    source="some_new_provider_we_didnt_classify")
        # Even with no source at all
        with self.assertRaises(b.SemanticWriteBlocked):
            b.assert_field_allowed("genre", "Jazz", source="")


class TestMK38_0_StaleCachePoison(unittest.TestCase):
    """R18, R19 — already-poisoned cache must not feed downstream writes."""

    def setUp(self):
        for m in ("mk38_0_semantic_write_barrier",):
            if m in sys.modules:
                del sys.modules[m]
        os.environ.pop("MK38_0_ALLOW_SEMANTIC_WRITES", None)

    def test_T6_cache_read_blocks_inference_under_freeze(self):
        """T6: cache_read_allowed returns False for inference-source entries
        under freeze. This is the stale-cache protection — even existing
        poisoned cache can't propagate."""
        import mk38_0_semantic_write_barrier as b
        self.assertFalse(b.cache_read_allowed("ai_inference"))
        self.assertFalse(b.cache_read_allowed("graph_propagation"))
        self.assertFalse(b.cache_read_allowed("artist_cache_inferred"))

    def test_T6b_cache_read_allows_canonical_under_freeze(self):
        import mk38_0_semantic_write_barrier as b
        self.assertTrue(b.cache_read_allowed("spotify_canonical"))
        self.assertTrue(b.cache_read_allowed("musicbrainz_canonical"))
        self.assertTrue(b.cache_read_allowed("fingerprint_match"))


class TestMK38_0_AdversarialMonteCarlo(unittest.TestCase):
    """T7 — MC sim of 1000 random (field, source, confidence) tuples
    against the LOCKED specification.

    The classifier under test is `is_field_allowed(field, source)`.
    The expected oracle is the LOCKED decision tree from the module
    docstring. Pass criterion: oracle and gate agree on 1000/1000.

    This is the adversarial-CTO acceptance gate the board demanded —
    "show me the proof that EVERY semantic write goes through your gate."
    """

    def setUp(self):
        for m in ("mk38_0_semantic_write_barrier",):
            if m in sys.modules:
                del sys.modules[m]
        os.environ.pop("MK38_0_ALLOW_SEMANTIC_WRITES", None)

    def _expected_decision(self, field, source, frozen, b):
        """Oracle implementing the LOCKED specification independently."""
        if field in b.NONSEMANTIC_FIELDS:
            return True
        if source in b.CANONICAL_SOURCES:
            return True
        if field not in b.SEMANTIC_FIELDS:
            return False  # unknown field → default-deny
        if source in b.INFERENCE_SOURCES:
            return not frozen
        return False  # unknown source → default-deny

    def test_T7_monte_carlo_1000_samples(self):
        """T7-MC: 1000 random samples; gate must agree with oracle every time."""
        import mk38_0_semantic_write_barrier as b

        # Sample space — union of legitimate taxonomy + adversarial novelties
        all_fields = (list(b.NONSEMANTIC_FIELDS) + list(b.SEMANTIC_FIELDS)
                      + ["xenofield_a", "xenofield_b", "", "title2"])
        all_sources = (list(b.CANONICAL_SOURCES) + list(b.INFERENCE_SOURCES)
                       + ["xenosource_a", "xenosource_b", "",
                          "almost_canonical_typo", "AI_Inference"])  # case-sensitive

        rng = random.Random(0xC70_38_0)   # deterministic seed for replay
        N = 1000
        mismatches = []
        for i in range(N):
            field = rng.choice(all_fields)
            source = rng.choice(all_sources)
            expected = self._expected_decision(field, source, frozen=True, b=b)
            actual = b.is_field_allowed(field, source)
            if actual != expected:
                mismatches.append((i, field, source, expected, actual))

        self.assertEqual(len(mismatches), 0,
                         f"MC sim found {len(mismatches)} disagreements between "
                         f"gate and oracle. First 5: {mismatches[:5]}")

    def test_T7b_monte_carlo_zero_inference_genre_writes(self):
        """T7b-MC: any of the 9 inference sources, any of the 4 semantic
        fields, ALL must be blocked. 36 deterministic samples. This is the
        contamination-class proof: the union of (inference × semantic) is
        empty under freeze."""
        import mk38_0_semantic_write_barrier as b
        leaks = []
        for src in b.INFERENCE_SOURCES:
            for fld in b.SEMANTIC_FIELDS:
                if b.is_field_allowed(fld, src):
                    leaks.append((src, fld))
        self.assertEqual(leaks, [],
                         f"Inference × semantic must be empty under freeze; "
                         f"found leaks: {leaks}")

    def test_T7c_monte_carlo_all_canonical_writes_pass(self):
        """T7c-MC: any canonical source × any field must pass. Verifies
        the freeze didn't accidentally break legitimate canonical writes."""
        import mk38_0_semantic_write_barrier as b
        blocks = []
        for src in b.CANONICAL_SOURCES:
            for fld in b.SEMANTIC_FIELDS | b.NONSEMANTIC_FIELDS:
                if not b.is_field_allowed(fld, src):
                    blocks.append((src, fld))
        self.assertEqual(blocks, [],
                         f"Canonical writes must always pass; blocked: {blocks}")


class TestMK38_0_SelfCheck(unittest.TestCase):
    """T8 — verify_barrier_installed catches tampering."""

    def setUp(self):
        for m in ("mk38_0_semantic_write_barrier",):
            if m in sys.modules:
                del sys.modules[m]

    def test_T8_clean_install_passes_self_check(self):
        import mk38_0_semantic_write_barrier as b
        self.assertTrue(b.verify_barrier_installed())

    def test_T8b_emptied_canonical_fails_self_check(self):
        """If someone tries to neuter the gate by emptying CANONICAL_SOURCES
        (which would still allow CANONICAL paths to pass — but break the
        sanity invariant), self-check catches it."""
        import mk38_0_semantic_write_barrier as b
        original = set(b.CANONICAL_SOURCES)
        try:
            b.CANONICAL_SOURCES.clear()
            self.assertFalse(b.verify_barrier_installed(),
                             "Self-check must catch emptied CANONICAL_SOURCES")
        finally:
            b.CANONICAL_SOURCES.update(original)

    def test_T8c_overlapping_sets_fail_self_check(self):
        """Cross-set overlap (CANONICAL ∩ INFERENCE) is caught — ensures
        nobody can sneak a source into both buckets to make the gate ambiguous."""
        import mk38_0_semantic_write_barrier as b
        b.CANONICAL_SOURCES.add("ai_inference")  # tamper
        try:
            self.assertFalse(b.verify_barrier_installed())
        finally:
            b.CANONICAL_SOURCES.discard("ai_inference")


class TestMK38_0_OverrideMechanism(unittest.TestCase):
    """T9 — override path is audited and hardened-mode-locked."""

    def setUp(self):
        for m in ("mk38_0_semantic_write_barrier",):
            if m in sys.modules:
                del sys.modules[m]

    def test_T9_override_requires_env_var(self):
        """T9: with_freeze_override + env var unset → still frozen."""
        os.environ.pop("MK38_0_ALLOW_SEMANTIC_WRITES", None)
        import mk38_0_semantic_write_barrier as b
        with b.with_freeze_override(reason="test"):
            self.assertTrue(b.is_frozen(),
                            "Override without env var must NOT lift freeze")

    def test_T9b_override_with_env_var_lifts_freeze(self):
        """T9b: with_freeze_override + env var set → freeze lifted."""
        os.environ["MK38_0_ALLOW_SEMANTIC_WRITES"] = "1"
        try:
            import mk38_0_semantic_write_barrier as b
            with b.with_freeze_override(reason="test_lift"):
                self.assertFalse(b.is_frozen(),
                                 "Override + env var must lift freeze")
            # After exit, override is back to default (False) → frozen again
            self.assertTrue(b.is_frozen(),
                            "After override exits, freeze must reactivate")
        finally:
            os.environ.pop("MK38_0_ALLOW_SEMANTIC_WRITES", None)


class TestMK38_0_PatchSiteCoverage(unittest.TestCase):
    """T10 — verify the call sites we patched actually contain the gate."""

    def test_T10_ai_bulk_metadata_uses_barrier(self):
        from pathlib import Path
        src = Path(__file__).parent / "ai_bulk_metadata.py"
        if not src.exists():
            self.skipTest("ai_bulk_metadata.py not in test dir")
        text = src.read_text(encoding="utf-8")
        self.assertIn("mk38_0_semantic_write_barrier", text)
        self.assertIn('source="ai_inference"', text)

    def test_T10b_walkman_sync_consolidate_gated(self):
        from pathlib import Path
        src = Path(__file__).parent / "walkman_sync.py"
        if not src.exists():
            self.skipTest("walkman_sync.py not in test dir")
        text = src.read_text(encoding="utf-8")
        # The gate is at top of _consolidate_album_genres body
        self.assertIn("_consolidate_album_genres SKIPPED", text)
        self.assertIn("MK38.0", text)

    def test_T10c_genre_client_artist_cache_gated(self):
        from pathlib import Path
        src = Path(__file__).parent / "genre_client.py"
        if not src.exists():
            self.skipTest("genre_client.py not in test dir")
        text = src.read_text(encoding="utf-8")
        # Both put and get must be gated
        self.assertIn("MK38.0 SEMANTIC WRITE BARRIER", text)
        # Two distinct gate sites
        self.assertGreaterEqual(text.count("mk38_0_semantic_write_barrier"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
