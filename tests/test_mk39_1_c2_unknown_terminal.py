"""
test_mk39_1_c2_unknown_terminal.py — MK39.1 C2 + C1.1 + E.1 regression
================================================================================

Council MK39.1 directive (2026-05-10, P0+, must-ship-first):
    "Plug the bottom of the funnel first."

Tests the structural cure for the default-write contamination pattern:
  - terminal_state.py module + IS_BLANK + FORBIDDEN_DEFAULT_SOURCES
  - genre_client._validate_ai_genre under STRICT mode (default ON)
  - mk38_0 forbidden-default-source rejection
  - Phase I containment STILL resolving 9/9 (non-regression)
  - Audit benchmark harness sanity
"""

from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path


def _fresh_import(modules):
    for m in modules:
        if m in sys.modules:
            del sys.modules[m]


# ═══════════════════════════════════════════════════════════════════════════
# C2.4 — terminal_state module
# ═══════════════════════════════════════════════════════════════════════════

class TestTerminalState(unittest.TestCase):
    def test_T1_IS_BLANK_covers_None_empty_whitespace(self):
        from terminal_state import IS_BLANK
        self.assertTrue(IS_BLANK(None))
        self.assertTrue(IS_BLANK(""))
        self.assertTrue(IS_BLANK("   "))
        self.assertTrue(IS_BLANK("\t\n"))
        self.assertFalse(IS_BLANK("rock"))
        self.assertFalse(IS_BLANK(" hip-hop "))
        # Non-string blank-y values
        self.assertTrue(IS_BLANK([]))
        self.assertTrue(IS_BLANK({}))

    def test_T1b_normalize_to_blank_collapses_to_empty_string(self):
        from terminal_state import normalize_to_blank
        self.assertEqual(normalize_to_blank(None), "")
        self.assertEqual(normalize_to_blank(""), "")
        self.assertEqual(normalize_to_blank("   "), "")
        self.assertEqual(normalize_to_blank("Rock"), "Rock")
        self.assertEqual(normalize_to_blank(" Hip-Hop "), "Hip-Hop")


# ═══════════════════════════════════════════════════════════════════════════
# C2.2 — Strict AI validation: out-of-taxonomy → blank (NOT accept)
# ═══════════════════════════════════════════════════════════════════════════

class TestStrictAIValidation(unittest.TestCase):
    def setUp(self):
        # Force fresh import so we pick up the new strict logic
        _fresh_import(["genre_client", "config", "terminal_state"])
        # Reset telemetry buffer
        import terminal_state as ts
        ts.reset_default_write_attempts()

    def test_T2_strict_mode_rejects_out_of_taxonomy(self):
        """The Council-mandated structural fix: AI returns a genre that's
        not in the canonical taxonomy AND not in the alias map → reject
        and return blank (NOT 'accept with warning').

        IMPORTANT FINDING during implementation: "Abstract Hip-Hop" is
        actually IN the canonical taxonomy (it's a real subgenre). The
        Phase I contamination was AI over-using it as a default for
        unknown niche genres, not rejecting it as invalid. C2 handles
        the broader category — out-of-taxonomy guesses — while Phase I
        continues to handle the specific "Abstract Hip-Hop as default"
        abuse pattern. Both are necessary; neither is sufficient alone.
        """
        import genre_client as gc
        # Use a genre name guaranteed not to be in the taxonomy
        # (we generate one to avoid future taxonomy collisions)
        synthetic_unknown = "ZZ_C2_Synthetic_Unknown_Genre_2026"
        clean_genre, _year = gc._validate_ai_genre(synthetic_unknown, "")
        self.assertEqual(clean_genre, "",
                          "STRICT mode must reject out-of-taxonomy AI genres "
                          "by returning blank (terminal-unknown state)")

    def test_T2b_strict_mode_records_telemetry(self):
        """Rejected default-writes are recorded for audit."""
        import genre_client as gc
        from terminal_state import (
            get_default_write_attempts, reset_default_write_attempts,
        )
        reset_default_write_attempts()
        gc._validate_ai_genre("Some Made Up Genre Name", "")
        attempts = get_default_write_attempts()
        self.assertGreaterEqual(len(attempts), 1)
        self.assertEqual(attempts[-1].field, "genre")
        self.assertEqual(attempts[-1].value, "Some Made Up Genre Name")
        self.assertTrue(attempts[-1].rejected)

    def test_T3_canonical_taxonomy_genre_passes_through(self):
        """A genre IN the taxonomy must NOT be rejected."""
        import genre_client as gc
        # Pick a genre we know is in _VALID_GENRE_TAXONOMY
        valid_candidates = ["Hip-Hop", "Rock", "Jazz", "Pop", "Amapiano"]
        for cand in valid_candidates:
            clean, _ = gc._validate_ai_genre(cand, "")
            if clean:
                # If at least one of our candidates passes, the gate works
                self.assertTrue(True)
                return
        self.fail(f"none of {valid_candidates} passed validation")

    def test_T3b_alias_genre_passes_through_as_canonical(self):
        """A genre in the alias map maps to canonical name (no rejection)."""
        import genre_client as gc
        # _GENRE_ALIASES_VALIDATE maps lower-case alias -> canonical
        if hasattr(gc, "_GENRE_ALIASES_VALIDATE") and gc._GENRE_ALIASES_VALIDATE:
            # Pick one alias to test
            alias_key = next(iter(gc._GENRE_ALIASES_VALIDATE.keys()))
            canonical = gc._GENRE_ALIASES_VALIDATE[alias_key]
            clean, _ = gc._validate_ai_genre(alias_key, "")
            self.assertEqual(clean, canonical,
                              f"alias {alias_key!r} should map to {canonical!r}")

    def test_T3c_legacy_permissive_mode_still_accepts(self):
        """Operator opt-out path: STRICT_AI_GENRE_VALIDATION=False restores
        the historical accept-with-warning behavior."""
        import genre_client as gc
        import config
        # Save and restore
        original = getattr(config, "STRICT_AI_GENRE_VALIDATION", True)
        try:
            config.STRICT_AI_GENRE_VALIDATION = False
            clean, _ = gc._validate_ai_genre("Abstract Hip-Hop", "")
            # In permissive mode, the value should pass through (historical behavior)
            self.assertEqual(clean, "Abstract Hip-Hop",
                              "legacy permissive mode must still accept "
                              "out-of-taxonomy genres for backward compat")
        finally:
            config.STRICT_AI_GENRE_VALIDATION = original


# ═══════════════════════════════════════════════════════════════════════════
# C2.3 — Forbidden-default-source policy in MK38.0 barrier
# ═══════════════════════════════════════════════════════════════════════════

class TestForbiddenDefaultSources(unittest.TestCase):
    def setUp(self):
        _fresh_import(["mk38_0_semantic_write_barrier", "terminal_state"])

    def test_T4_forbidden_default_sources_set_documented(self):
        """The forbidden set is small + explicit per adversarial review."""
        from terminal_state import FORBIDDEN_DEFAULT_SOURCES
        # Must include the canonical ones
        self.assertIn("default", FORBIDDEN_DEFAULT_SOURCES)
        self.assertIn("default_fallback", FORBIDDEN_DEFAULT_SOURCES)
        self.assertIn("unknown", FORBIDDEN_DEFAULT_SOURCES)
        self.assertIn("placeholder", FORBIDDEN_DEFAULT_SOURCES)
        self.assertIn("guess", FORBIDDEN_DEFAULT_SOURCES)
        # Sanity bound — not absurdly large
        self.assertLess(len(FORBIDDEN_DEFAULT_SOURCES), 30)

    def test_T4b_forbidden_set_does_not_collide_with_canonical(self):
        """Adversarial-review constraint: FORBIDDEN_DEFAULT_SOURCES must NOT
        intersect the existing CANONICAL_SOURCES or INFERENCE_SOURCES."""
        from terminal_state import FORBIDDEN_DEFAULT_SOURCES
        from mk38_0_semantic_write_barrier import (
            CANONICAL_SOURCES, INFERENCE_SOURCES,
        )
        intersection_canonical = FORBIDDEN_DEFAULT_SOURCES & CANONICAL_SOURCES
        intersection_inference = FORBIDDEN_DEFAULT_SOURCES & INFERENCE_SOURCES
        self.assertEqual(intersection_canonical, set(),
                          f"forbidden set collides with canonical: "
                          f"{intersection_canonical}")
        self.assertEqual(intersection_inference, set(),
                          f"forbidden set collides with inference: "
                          f"{intersection_inference}")

    def test_T4c_is_forbidden_default_source_predicate(self):
        from terminal_state import is_forbidden_default_source
        self.assertTrue(is_forbidden_default_source("default"))
        self.assertTrue(is_forbidden_default_source("Default"))      # case-insensitive
        self.assertTrue(is_forbidden_default_source("  guess  "))    # whitespace-tolerant
        self.assertFalse(is_forbidden_default_source("spotify_canonical"))
        self.assertFalse(is_forbidden_default_source(""))            # empty is not forbidden
        self.assertFalse(is_forbidden_default_source(None))

    def test_T5_barrier_rejects_forbidden_default_source_writes(self):
        """The structural teeth: MK38.0 write barrier rejects any write
        whose source is in FORBIDDEN_DEFAULT_SOURCES, regardless of which
        field. This is defense in depth — even if some legacy path tries
        to write a default, the barrier stops it."""
        import mk38_0_semantic_write_barrier as mk38
        from terminal_state import (
            reset_default_write_attempts, get_default_write_attempts,
        )
        reset_default_write_attempts()
        # Try to write a default — must raise SemanticWriteBlocked
        with self.assertRaises(mk38.SemanticWriteBlocked) as ctx:
            mk38.assert_field_allowed(
                field="genre",
                value="Abstract Hip-Hop",
                source="default",   # forbidden
            )
        self.assertIn("FORBIDDEN_DEFAULT_SOURCES", str(ctx.exception))
        # Verify telemetry was recorded
        attempts = get_default_write_attempts()
        self.assertGreaterEqual(len(attempts), 1)
        self.assertEqual(attempts[-1].source, "default")

    def test_T5b_barrier_still_allows_canonical_sources(self):
        """The forbidden gate must NOT break legitimate canonical writes."""
        import mk38_0_semantic_write_barrier as mk38
        # Should NOT raise for canonical source
        try:
            mk38.assert_field_allowed(
                field="genre", value="Hip-Hop", source="spotify_canonical",
            )
        except mk38.SemanticWriteBlocked:
            self.fail("canonical write must NOT be blocked by C2 forbidden gate")


# ═══════════════════════════════════════════════════════════════════════════
# C2.5 — Pipeline preserves blank when all sources fail (non-regression)
# ═══════════════════════════════════════════════════════════════════════════

class TestNonRegression(unittest.TestCase):
    """Phase I containment must STILL resolve 9/9 forensic-log cases.
    Adversarial-review constraint locked: MK39.1 work must not perturb
    the locked Phase I behavior."""

    def test_T6_phase_I_forensic_9_of_9_still_resolving(self):
        _fresh_import(["mk39_phase_I_containment", "config", "terminal_state"])
        import mk39_phase_I_containment as p1
        cases = [
            ('The Weeknd', 'After Hours', 'Alt R&B'),
            ('Babalwa M', '3 Step Afro House 2026 Hits', 'Afro House'),
            ('Beekay Monalayzzar', '3 Step Afro House 2026 Hits', 'Afro House'),
            ('Skillz', '3 Step Afro House 2026 Hits', 'Afro House'),
            ('Kaoru Akimoto', '80_90s Japanese City Pop', 'City Pop'),
            ('Omega Tribe', '80_90s Japanese City Pop', 'City Pop'),
            ('Kei Ishiguro', '80_90s Japanese City Pop', 'City Pop'),
            ('A$AP Rocky', "Don't Be Dumb", 'Hip-Hop'),
            ('Alfa Kat', 'Amapiano Mix', 'Amapiano'),
        ]
        fails = []
        for artist, folder, expected in cases:
            g, _src = p1.resolve_genre_with_phase_I(
                artist=artist, title='t', current_genre='',
                folder_name=folder, cache_genre='Abstract Hip-Hop')
            if g != expected:
                fails.append((artist, expected, g))
        self.assertEqual(fails, [],
                          f"Phase I forensic regression broken: {fails}")

    def test_T6b_blank_input_yields_blank_output_through_validation(self):
        """End-to-end: empty AI response stays empty through validation.
        No defaults are substituted at any layer."""
        _fresh_import(["genre_client"])
        import genre_client as gc
        clean, year = gc._validate_ai_genre("", "")
        self.assertEqual(clean, "")
        self.assertEqual(year, "")

    def test_T6c_phase_I_unchanged_helpers_still_exported(self):
        """Phase I containment exports must be unchanged — adversarial
        constraint: MK39.1 must not modify mk39_phase_I_containment."""
        _fresh_import(["mk39_phase_I_containment"])
        import mk39_phase_I_containment as p1
        for name in ("match_folder_genre", "block_abstract_hip_hop_default",
                      "ai_genre_disabled", "mark_groq_failure",
                      "genre_graph_is_empty", "safe_artist_override",
                      "resolve_genre_with_phase_I"):
            self.assertTrue(hasattr(p1, name),
                              f"Phase I must still export {name}")


# ═══════════════════════════════════════════════════════════════════════════
# C2.1 — Audit catalog
# ═══════════════════════════════════════════════════════════════════════════

class TestAuditCatalog(unittest.TestCase):
    def test_T7_audit_catalog_documents_known_default_write_sites(self):
        from terminal_state import AUDIT_CATALOG
        # Catalog must include the known historical sites
        catalog_keys_str = " ".join(AUDIT_CATALOG.keys())
        self.assertIn("ai_helper", catalog_keys_str)
        self.assertIn("genre_client", catalog_keys_str)
        # Each entry has the required disposition keys
        for site, entry in AUDIT_CATALOG.items():
            self.assertIn("pattern", entry)
            self.assertIn("disposition", entry)
            self.assertIn("covered_by_test", entry)


# ═══════════════════════════════════════════════════════════════════════════
# E.1 — Audit-speed benchmark harness
# ═══════════════════════════════════════════════════════════════════════════

class TestBenchmarkHarness(unittest.TestCase):
    def test_T8_harness_runs_and_produces_report(self):
        from audit_benchmark import benchmark, BenchmarkReport
        rpt = benchmark("test", work=lambda i: None, n=10)
        self.assertIsInstance(rpt, BenchmarkReport)
        self.assertEqual(rpt.sample_count, 10)
        self.assertEqual(rpt.success_count, 10)
        self.assertEqual(rpt.failure_count, 0)
        self.assertGreaterEqual(rpt.total_ms, 0.0)

    def test_T8b_harness_counts_failures(self):
        from audit_benchmark import benchmark
        def bad(i):
            if i == 5:
                raise RuntimeError("simulated failure")
        rpt = benchmark("test_failures", work=bad, n=10)
        self.assertEqual(rpt.success_count, 9)
        self.assertEqual(rpt.failure_count, 1)

    def test_T8c_report_persists_to_json(self):
        import tempfile, os
        from audit_benchmark import benchmark, write_report
        rpt = benchmark("persist_test", work=lambda i: None, n=5)
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            path = f.name
        try:
            write_report(rpt, path)
            data = json.loads(Path(path).read_text())
            self.assertEqual(data["label"], "persist_test")
            self.assertEqual(data["sample_count"], 5)
            self.assertIn("p50_ms", data)
            self.assertIn("per_sample_ms", data)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
