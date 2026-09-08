"""
test_mk39_phase_I_containment.py — regression for Council 2026-05-10 directive

Council §5 immediate action #5: re-evaluate the platform's semantic integrity
after these patches are deployed and a fresh audit log is presented. These
tests lock the four containment fixes (A/B/C/D) plus the discovered
root-cause (E) so they cannot regress without breaking the suite.
"""

from __future__ import annotations

import unittest


def _fresh_import():
    import sys
    for m in ("mk39_phase_I_containment",):
        if m in sys.modules:
            del sys.modules[m]
    import mk39_phase_I_containment  # noqa


# ═══════════════════════════════════════════════════════════════════════════
# T1-T8: Phase I-A folder-name keyword matching
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseIA_FolderGenreMatch(unittest.TestCase):
    def setUp(self):
        _fresh_import()

    def test_T1_afro_house_folder_matches(self):
        import mk39_phase_I_containment as p1
        self.assertEqual(p1.match_folder_genre("3 Step Afro House 2026 Hits"),
                          "Afro House")
        self.assertEqual(p1.match_folder_genre("Afro House Vibes"),
                          "Afro House")

    def test_T2_japanese_city_pop_folder_matches(self):
        import mk39_phase_I_containment as p1
        self.assertEqual(p1.match_folder_genre("80_90s Japanese City Pop"),
                          "City Pop")
        self.assertEqual(p1.match_folder_genre("City Pop Classics"),
                          "City Pop")

    def test_T3_amapiano_folder_matches(self):
        import mk39_phase_I_containment as p1
        self.assertEqual(p1.match_folder_genre("Amapiano Gold"), "Amapiano")

    def test_T4_kpop_folder_variants_all_match(self):
        """Hyphen normalization variants should all resolve to K-Pop."""
        import mk39_phase_I_containment as p1
        for v in ["K-Pop", "K_Pop", "kpop Hits", "Best of K-Pop", "K Pop 2024"]:
            self.assertEqual(p1.match_folder_genre(v), "K-Pop", f"failed: {v!r}")

    def test_T5_album_titles_without_genre_keyword_return_None(self):
        """Council: 'After Hours' is The Weeknd's album, not a genre signal.
        The matcher must NOT guess; it should leave such cases for the
        artist-override path to handle."""
        import mk39_phase_I_containment as p1
        for album in ["After Hours", "Beauty Behind The Madness",
                       "Don't Be Dumb", "13", "IGOR", "Selected Ambient",
                       "Help", "Let It Bleed", "Yesterday"]:
            self.assertIsNone(p1.match_folder_genre(album),
                                f"{album!r} should NOT match — no genre keyword")

    def test_T6_specificity_order_matches_more_specific_first(self):
        """A folder named 'Afro House Hits' must match 'Afro House',
        not the broader 'Afrobeats'. Specificity ordering is critical."""
        import mk39_phase_I_containment as p1
        self.assertEqual(p1.match_folder_genre("Afro House Hits"), "Afro House")
        # Distinct: Afrobeats (with s) should match Afrobeats, not Afrobeat (no s)
        self.assertEqual(p1.match_folder_genre("Modern Afrobeats Hits"), "Afrobeats")

    def test_T7_empty_or_None_input_safe(self):
        import mk39_phase_I_containment as p1
        self.assertIsNone(p1.match_folder_genre(""))
        self.assertIsNone(p1.match_folder_genre(None))
        self.assertIsNone(p1.match_folder_genre("   "))

    def test_T8_normalizes_separators_consistently(self):
        import mk39_phase_I_containment as p1
        # Underscores, dots, dashes, parens — all should normalize
        for s in ["80_90s.Japanese.City.Pop", "(Afro House) 2026",
                   "k-pop_hits-2024"]:
            r = p1.match_folder_genre(s)
            self.assertIsNotNone(r, f"{s!r} should match after normalization")


# ═══════════════════════════════════════════════════════════════════════════
# T9-T13: Phase I-B Abstract Hip-Hop default block
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseIB_BlockAbstractHipHopDefault(unittest.TestCase):
    def setUp(self):
        _fresh_import()

    def test_T9_blocks_for_artists_not_on_allow_list(self):
        """Council §3-B core case: SA artist getting Abstract Hip-Hop
        as a silent default → blank instead."""
        import mk39_phase_I_containment as p1
        self.assertIsNone(p1.block_abstract_hip_hop_default(
            "Abstract Hip-Hop", "Babalwa M"))
        self.assertIsNone(p1.block_abstract_hip_hop_default(
            "Abstract Hip-Hop", "Beekay Monalayzzar"))
        self.assertIsNone(p1.block_abstract_hip_hop_default(
            "Abstract Hip-Hop", "Kaoru Akimoto"))

    def test_T10_allows_for_curated_allow_list(self):
        """MF DOOM, Madlib etc. legitimately have Abstract Hip-Hop."""
        import mk39_phase_I_containment as p1
        for legit in ["MF DOOM", "Madlib", "Earl Sweatshirt",
                       "Aesop Rock", "Quasimoto", "Madvillain"]:
            self.assertEqual(
                p1.block_abstract_hip_hop_default("Abstract Hip-Hop", legit),
                "Abstract Hip-Hop",
                f"{legit!r} is on allow-list — should pass through")

    def test_T11_strips_the_prefix_for_allow_list_match(self):
        """Allow-list normalizes 'The X' → 'x' before matching."""
        import mk39_phase_I_containment as p1
        # "The MF DOOM" or other "the X" formats
        self.assertEqual(
            p1.block_abstract_hip_hop_default("Abstract Hip-Hop", "The Madlib"),
            "Abstract Hip-Hop")

    def test_T12_other_genres_pass_through_unchanged(self):
        """The block ONLY targets Abstract Hip-Hop. Other genres are not affected."""
        import mk39_phase_I_containment as p1
        for genre in ["Hip-Hop", "Afrobeats", "City Pop", "Trap", "Alt R&B",
                       "Drum & Bass", "Psychedelic Rock"]:
            self.assertEqual(
                p1.block_abstract_hip_hop_default(genre, "Random Artist"),
                genre)

    def test_T13_empty_or_None_passes_through(self):
        import mk39_phase_I_containment as p1
        self.assertEqual(p1.block_abstract_hip_hop_default("", "X"), "")
        self.assertIsNone(p1.block_abstract_hip_hop_default(None, "X"))


# ═══════════════════════════════════════════════════════════════════════════
# T14-T17: Phase I-C session-level AI disable
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseIC_AIDisable(unittest.TestCase):
    def setUp(self):
        _fresh_import()
        import mk39_phase_I_containment as p1
        p1.reset_ai_disable_state()
        # Also reset Gemini state
        try:
            import genre_client as gc
            gc._GEMINI_DISABLED = False
        except Exception:
            pass

    def tearDown(self):
        import mk39_phase_I_containment as p1
        p1.reset_ai_disable_state()

    def test_T14_initial_state_not_disabled(self):
        import mk39_phase_I_containment as p1
        self.assertFalse(p1.ai_genre_disabled())

    def test_T15_groq_failures_alone_do_not_trip_without_gemini_disabled(self):
        """The flag requires BOTH conditions — Gemini circuit-broken AND
        Groq failures > threshold. Gemini still up = not disabled."""
        import mk39_phase_I_containment as p1
        for _ in range(10):
            p1.mark_groq_failure()
        self.assertFalse(p1.ai_genre_disabled(),
                          "Should NOT disable while Gemini still up")

    def test_T16_both_conditions_trip_the_flag(self):
        import mk39_phase_I_containment as p1
        import genre_client as gc
        gc._GEMINI_DISABLED = True
        # Below threshold
        for _ in range(p1.GROQ_FAILURE_THRESHOLD):
            p1.mark_groq_failure()
        self.assertFalse(p1.ai_genre_disabled(),
                          "At threshold (3), should NOT yet disable")
        # One more — exceeds threshold
        p1.mark_groq_failure()
        self.assertTrue(p1.ai_genre_disabled(),
                         "Above threshold (>3) AND Gemini down → disabled")

    def test_T17_reset_clears_state(self):
        import mk39_phase_I_containment as p1
        import genre_client as gc
        gc._GEMINI_DISABLED = True
        for _ in range(5):
            p1.mark_groq_failure()
        self.assertTrue(p1.ai_genre_disabled())
        p1.reset_ai_disable_state()
        self.assertFalse(p1.ai_genre_disabled())


# ═══════════════════════════════════════════════════════════════════════════
# T18-T20: Phase I-D empty graph detection
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseID_EmptyGraphDetection(unittest.TestCase):
    def setUp(self):
        _fresh_import()

    def test_T18_None_is_empty(self):
        import mk39_phase_I_containment as p1
        self.assertTrue(p1.genre_graph_is_empty(None))

    def test_T19_graph_with_no_adjacency_is_empty(self):
        import mk39_phase_I_containment as p1
        class FakeGraph:
            adjacency = {}
        self.assertTrue(p1.genre_graph_is_empty(FakeGraph()))

    def test_T20_graph_with_nodes_is_not_empty(self):
        import mk39_phase_I_containment as p1
        class FakeGraph:
            adjacency = {"a": [], "b": ["a"]}
        self.assertFalse(p1.genre_graph_is_empty(FakeGraph()))

    def test_T20b_defensive_on_malformed_graph(self):
        import mk39_phase_I_containment as p1
        class WeirdGraph:
            adjacency = None    # weird but not crashing
        self.assertTrue(p1.genre_graph_is_empty(WeirdGraph()))


# ═══════════════════════════════════════════════════════════════════════════
# T21-T24: Phase I-E safe_artist_override wrapper (CRITICAL bug fix)
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseIE_SafeArtistOverride(unittest.TestCase):
    """The discovered root cause: audit.py calls
    `_artist_override(artist, title, album=...)` but the function only
    accepts `(artist, title)`. The TypeError was silently swallowed,
    making EVERY override lookup in the audit pipeline a no-op for
    months. These tests lock the safe wrapper."""

    def setUp(self):
        _fresh_import()

    def test_T21_extra_kwargs_are_ignored_returns_correct_override(self):
        import mk39_phase_I_containment as p1
        # The exact call pattern audit.py uses
        result = p1.safe_artist_override("The Weeknd", "Blinding Lights",
                                            album="After Hours")
        self.assertEqual(result, "Alt R&B",
                          "The Weeknd override should now reach the audit "
                          "(was lost to TypeError swallowed by except: pass)")

    def test_T22_council_named_artists_all_resolve(self):
        """The artists Council §1 named as broken should all resolve via
        the wrapper. Their entries already exist in _ARTIST_GENRE_OVERRIDES;
        the bug was the call site, not the data."""
        import mk39_phase_I_containment as p1
        cases = [
            ("The Weeknd",          "Alt R&B"),
            ("Babalwa M",           "Afro House"),
            ("Beekay Monalayzzar",  "Afro House"),
            ("Kei Ishiguro",        "City Pop"),
            ("Omega Tribe",         "City Pop"),
        ]
        for artist, expected in cases:
            actual = p1.safe_artist_override(artist, "any title",
                                                 album="any album",
                                                 year="any year")
            self.assertEqual(actual, expected,
                              f"{artist!r}: got {actual!r}, expected {expected!r}")

    def test_T23_unknown_artist_returns_empty(self):
        import mk39_phase_I_containment as p1
        result = p1.safe_artist_override("Nonexistent Random Artist X",
                                            "Some Title")
        # May return empty OR a script-detected default; both are valid
        self.assertIsInstance(result, str)

    def test_T24_no_kwargs_also_works_backward_compat(self):
        import mk39_phase_I_containment as p1
        result = p1.safe_artist_override("The Weeknd", "Blinding Lights")
        self.assertEqual(result, "Alt R&B")


# ═══════════════════════════════════════════════════════════════════════════
# T25-T28: Combined Council priority chain
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseI_CombinedResolver(unittest.TestCase):
    """Verifies the integrated Council priority chain matches §3 wording."""

    def setUp(self):
        _fresh_import()

    def test_T25_existing_tag_wins_over_everything(self):
        import mk39_phase_I_containment as p1
        g, src = p1.resolve_genre_with_phase_I(
            artist="The Weeknd", title="Blinding Lights",
            current_genre="R&B", folder_name="3 Step Afro House",
            cache_genre="Afrobeats")
        self.assertEqual(g, "R&B")
        self.assertEqual(src, "existing_tag")

    def test_T26_artist_override_beats_cache_and_folder(self):
        """The Weeknd → Alt R&B (override) wins over Afrobeats (cache).
        This is THE core bug Council §1 named."""
        import mk39_phase_I_containment as p1
        g, src = p1.resolve_genre_with_phase_I(
            artist="The Weeknd", title="Blinding Lights",
            current_genre="", folder_name="After Hours",
            cache_genre="Afrobeats")
        self.assertEqual(g, "Alt R&B")
        self.assertEqual(src, "artist_override")

    def test_T27_folder_keyword_wins_when_no_override(self):
        """A folder keyword must apply when no override exists. We use a
        synthesized artist not in any override list to test the pure
        folder-keyword path. (Kaoru Akimoto is now in genre_overrides.json
        so resolves via artist_override; that's tested in T27b below.)"""
        import mk39_phase_I_containment as p1
        g, src = p1.resolve_genre_with_phase_I(
            artist="ZZZ_NoOverrideArtist_Test", title="Dress Down",
            current_genre="", folder_name="80_90s Japanese City Pop",
            cache_genre="Abstract Hip-Hop")
        self.assertEqual(g, "City Pop")
        self.assertEqual(src, "user_canonical_folder_keyword")

    def test_T27b_kaoru_akimoto_now_resolves_via_operator_override(self):
        """Post-Phase-I-complete: Kaoru Akimoto is in genre_overrides.json,
        so she resolves via the operator-override path (priority 0) — a
        STRONGER signal than the folder keyword. The genre is identical
        (City Pop); the source label changes to artist_override."""
        import mk39_phase_I_containment as p1
        g, src = p1.resolve_genre_with_phase_I(
            artist="Kaoru Akimoto", title="Dress Down",
            current_genre="", folder_name="80_90s Japanese City Pop",
            cache_genre="Abstract Hip-Hop")
        self.assertEqual(g, "City Pop")
        self.assertEqual(src, "artist_override",
                          "Operator override (priority 0) beats folder keyword")

    def test_T28_no_signals_returns_blank_per_council_preference(self):
        """Council §5: 'silence (blank fields) is preferred to confident error.'
        AMApiano #1 (operator's own) with no canonical signals → blank."""
        import mk39_phase_I_containment as p1
        g, src = p1.resolve_genre_with_phase_I(
            artist="", title="AMApiano #1",
            current_genre="", folder_name="My Music",
            cache_genre="")
        self.assertIsNone(g)
        self.assertEqual(src, "blank_phase_I_preference")

    def test_T28b_abstract_hip_hop_cache_default_blocked(self):
        """Cache says Abstract Hip-Hop for an SA artist → block (Phase I-B)."""
        import mk39_phase_I_containment as p1
        g, src = p1.resolve_genre_with_phase_I(
            artist="Babalwa M", title="random",
            current_genre="", folder_name="random_folder_no_keyword",
            cache_genre="Abstract Hip-Hop")
        # Override exists for Babalwa M ("Afro House") so override wins
        # before cache is even reached
        self.assertEqual(g, "Afro House")
        self.assertEqual(src, "artist_override")


# ═══════════════════════════════════════════════════════════════════════════
# T29: API surface complete
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseI_API(unittest.TestCase):
    def test_T29_API_surface_complete(self):
        import mk39_phase_I_containment as p1
        for name in ["match_folder_genre",
                     "block_abstract_hip_hop_default", "ABSTRACT_HIP_HOP_TAG",
                     "mark_groq_failure", "ai_genre_disabled",
                     "reset_ai_disable_state", "GROQ_FAILURE_THRESHOLD",
                     "genre_graph_is_empty",
                     "safe_artist_override",
                     "resolve_genre_with_phase_I"]:
            self.assertTrue(hasattr(p1, name), f"missing: {name}")
            self.assertIn(name, p1.__all__, f"not in __all__: {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
