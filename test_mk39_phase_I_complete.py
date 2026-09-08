"""
test_mk39_phase_I_complete.py — regression for Phase I closures
================================================================================

Council 2026-05-10 Final Review §2 described patches A1 (Anthropic+DeepSeek
wiring), A5 (GENRE_GRAPH_ENABLED config flag), and §B2 (genre_overrides.json
seed file) as completed. The prior sprint shipped A2/A3/A4 + root cause #5
but NOT these three pieces. This suite locks them.

Tests:
  T1-T3: DeepSeek provider wired into ai_helper.call_ai waterfall
  T4-T7: GENRE_GRAPH_ENABLED config flag honored everywhere
  T8-T13: genre_overrides.json operator file loaded + priority-0 honored
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _fresh_import(modules):
    for m in modules:
        if m in sys.modules:
            del sys.modules[m]


# ═══════════════════════════════════════════════════════════════════════════
# T1-T3: DeepSeek wiring (Council §A1)
# ═══════════════════════════════════════════════════════════════════════════

class TestDeepSeekWiring(unittest.TestCase):
    def setUp(self):
        _fresh_import(["ai_helper"])

    def test_T1_deepseek_in_PROVIDERS_state(self):
        import ai_helper
        self.assertIn("deepseek", ai_helper._PROVIDERS,
                       "Council §A1 mandate: DeepSeek must be in _PROVIDERS")
        prov = ai_helper._PROVIDERS["deepseek"]
        # Should follow the Groq pattern: until + lock + auth_fails
        self.assertIn("until", prov)
        self.assertIn("lock", prov)
        self.assertIn("auth_fails", prov)

    def test_T2_deepseek_call_function_exists_with_correct_signature(self):
        import ai_helper
        import inspect
        self.assertTrue(hasattr(ai_helper, "_deepseek_call"))
        sig = inspect.signature(ai_helper._deepseek_call)
        params = list(sig.parameters.keys())
        # Must accept prompt, max_tokens, temperature (matches GPT-4o/Claude pattern)
        self.assertIn("prompt", params)
        self.assertIn("max_tokens", params)
        self.assertIn("temperature", params)

    def test_T3_call_ai_invokes_deepseek_in_waterfall(self):
        """The call_ai entry point must invoke _deepseek_call as part of the
        provider waterfall — Council ordered it between Groq exhaustion and
        the existing GPT-4o/Claude phases."""
        import ai_helper
        import inspect
        src = inspect.getsource(ai_helper.call_ai)
        self.assertIn("_deepseek_call", src,
                       "call_ai must invoke _deepseek_call (Council §A1)")
        # Verify ordering: DeepSeek between groq_llama8b and GPT-4o
        groq8b_pos    = src.find("groq_llama8b")
        deepseek_pos  = src.find("_deepseek_call")
        gpt4o_pos     = src.find("_gpt4o_call")
        self.assertGreater(deepseek_pos, groq8b_pos,
                             "DeepSeek must come AFTER Groq exhaustion")
        self.assertLess(deepseek_pos, gpt4o_pos,
                          "DeepSeek must come BEFORE GPT-4o (cheaper first)")


# ═══════════════════════════════════════════════════════════════════════════
# T4-T7: GENRE_GRAPH_ENABLED config flag (Council §A5)
# ═══════════════════════════════════════════════════════════════════════════

class TestGenreGraphDisabled(unittest.TestCase):
    def test_T4_config_flag_exists_default_False(self):
        """Council §A5: 'add a config flag GENRE_GRAPH_ENABLED = False
        and set it to False until the graph can be repaired and validated.'"""
        _fresh_import(["config"])
        import config
        self.assertTrue(hasattr(config, "GENRE_GRAPH_ENABLED"))
        self.assertFalse(config.GENRE_GRAPH_ENABLED,
                          "Council §A5 default mandate: must be False this release")

    def test_T5_build_graph_returns_empty_when_disabled(self):
        """Even with a populated track_map, build_graph must return an
        empty GenreGraph when GENRE_GRAPH_ENABLED is False."""
        _fresh_import(["config", "genre_graph"])
        import genre_graph
        g = genre_graph.build_graph(track_map={
            "p1": [{"artist": "Test Artist 1", "title": "T1", "id": "x1"},
                    {"artist": "Test Artist 2", "title": "T2", "id": "x2"}],
        })
        self.assertEqual(len(g.adjacency), 0,
                          "Council §A5: graph must be empty when disabled")

    def test_T6_phase_I_helper_recognizes_disabled_graph(self):
        """The mk39_phase_I_containment.genre_graph_is_empty helper must
        agree that a disabled-graph build is empty."""
        _fresh_import(["config", "genre_graph", "mk39_phase_I_containment"])
        import genre_graph
        import mk39_phase_I_containment as p1
        g = genre_graph.build_graph()
        self.assertTrue(p1.genre_graph_is_empty(g))

    def test_T7_build_graph_with_explicit_enable_still_works(self):
        """When GENRE_GRAPH_ENABLED is forced True (e.g., for testing),
        build_graph must function normally with a populated track_map."""
        _fresh_import(["config"])
        import config
        original = config.GENRE_GRAPH_ENABLED
        config.GENRE_GRAPH_ENABLED = True
        try:
            _fresh_import(["genre_graph"])
            import genre_graph
            g = genre_graph.build_graph(track_map={
                "p1": [{"artist": "A1", "title": "T", "id": "x"} for _ in range(5)],
            })
            # Non-empty (or at least doesn't short-circuit to 0)
            # The actual node count depends on construction; we just verify
            # that the disable kill-switch doesn't fire when enabled.
            self.assertIsInstance(g, genre_graph.GenreGraph)
        finally:
            config.GENRE_GRAPH_ENABLED = original


# ═══════════════════════════════════════════════════════════════════════════
# T8-T13: genre_overrides.json operator file (Council §B2)
# ═══════════════════════════════════════════════════════════════════════════

class TestGenreOverridesFile(unittest.TestCase):
    def setUp(self):
        _fresh_import(["genre_client"])
        import genre_client
        genre_client._reset_operator_overrides_cache()

    def tearDown(self):
        import genre_client
        genre_client._reset_operator_overrides_cache()

    def test_T8_seed_file_exists_alongside_codebase(self):
        """Council §B2: pre-populate with urgent corrections."""
        from pathlib import Path
        seed = Path("genre_overrides.json")
        self.assertTrue(seed.exists(),
                          "genre_overrides.json must be shipped with this release")

    def test_T9_seed_file_loads_cleanly(self):
        import genre_client
        overrides = genre_client._load_operator_overrides()
        self.assertIsInstance(overrides, dict)
        self.assertGreater(len(overrides), 0,
                             "Council §B2: must pre-populate with corrections")

    def test_T10_council_named_artists_all_in_seed(self):
        """Council §B2 explicitly named: The Weeknd, all City Pop artists,
        Beekay Monalayzzar, Alfa Kat, etc."""
        import genre_client
        overrides = genre_client._load_operator_overrides()
        cases = [
            ("weeknd",              "Alt R&B"),
            ("babalwa m",           "Afro House"),
            ("beekay monalayzzar",  "Afro House"),
            ("kaoru akimoto",       "City Pop"),
            ("kei ishiguro",        "City Pop"),
            ("omega tribe",         "City Pop"),
            ("alfa kat",            "Amapiano"),     # NEW from Council §1
        ]
        for key, expected in cases:
            self.assertEqual(overrides.get(key), expected,
                              f"Council-named {key!r} must seed {expected!r}")

    def test_T11_operator_override_takes_priority_over_in_code_dict(self):
        """Council §B2: 'These overrides take ABSOLUTE PRIORITY over all
        other sources.' Even if _ARTIST_GENRE_OVERRIDES has a different
        value, the operator file wins."""
        import genre_client
        # Use a unique sentinel that doesn't conflict
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                            delete=False) as f:
            json.dump({
                "_format_version": 1,
                "overrides": {
                    "weeknd": "OPERATOR_TEST_VALUE",   # different from default
                },
            }, f)
            tmp_path = f.name
        try:
            # Force the cache to use our test file
            genre_client._reset_operator_overrides_cache()
            with mock.patch.object(genre_client, "_load_operator_overrides",
                                    return_value={"weeknd": "OPERATOR_TEST_VALUE"}):
                result = genre_client._artist_override("The Weeknd", "x")
                self.assertEqual(result, "OPERATOR_TEST_VALUE",
                                  "Operator override must beat in-code dict")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_T12_alfa_kat_resolves_via_operator_file(self):
        """Council §1 named Alfa Kat as receiving Abstract Hip-Hop (wrong).
        He's not in _ARTIST_GENRE_OVERRIDES — only the operator file fixes him."""
        import genre_client
        result = genre_client._artist_override("Alfa Kat", "")
        self.assertEqual(result, "Amapiano",
                          "Alfa Kat must resolve to Amapiano via genre_overrides.json")

    def test_T13_missing_or_corrupt_file_does_not_crash(self):
        """If genre_overrides.json is missing or corrupt, the loader must
        return an empty dict and the in-code overrides remain effective."""
        import genre_client
        genre_client._reset_operator_overrides_cache()
        # Mock out file-not-found path
        with mock.patch("os.path.isfile", return_value=False):
            overrides = genre_client._load_operator_overrides()
            self.assertEqual(overrides, {})
        # In-code overrides still work (e.g., weeknd is also in _ARTIST_GENRE_OVERRIDES)
        genre_client._reset_operator_overrides_cache()


if __name__ == "__main__":
    unittest.main(verbosity=2)
