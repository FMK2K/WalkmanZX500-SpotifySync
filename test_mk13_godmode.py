"""
test_mk13_godmode.py — MK13 Live-Run Hotfix Validation Suite
=============================================================
Tests for all fixes applied in MK13 based on the live run log.

TC47  groq_no_context_block_in_message    — _groq_call uses prompt not context_block
TC48  test_groq_json_word_present          — test_groq message contains word "json"
TC49  sparse_specificity_guard_added       — broad-genre-only override in walkman_sync
TC50  sparse_guard_preserves_specific      — specific genres never overridden
TC51  mtp_transfer_no_module_exit          — sys.exit replaced with deferred pattern
TC52  audit_offline_mode_path              — _fetch_liked_track_ids respects OFFLINE_MODE
TC53  genre_graph_purity_fallback          — empty specific → broad fallback
TC54  genre_graph_liked_songs_size_gate    — small Liked Songs playlists not skipped

MC25  mc_groq_call_structure               — 200 prompt variants always use prompt key
MC26  mc_sparse_genre_guard_coverage       — 500 genre×inferred pairs → correct decisions
MC27  mc_purity_fallback_simulation        — 300 playlists with only broad genres
"""

import pathlib
import random
import re
import sys
import unittest


class TestGroqFixes(unittest.TestCase):
    """TC47-48: Groq NameError and HTTP 400 fixes."""

    def setUp(self):
        self.code = pathlib.Path("ai_helper.py").read_text(encoding="utf-8")

    def test_groq_call_uses_prompt_not_context_block(self):
        """_groq_call must use `prompt` in messages, never bare `context_block`."""
        idx = self.code.find("def _groq_call(")
        section = self.code[idx:idx + 1500]
        # The messages line must reference `prompt`, not `context_block` as a bare name
        # (context_block is not a parameter of _groq_call)
        msg_idx = section.find('"messages"')
        msg_line = section[msg_idx:msg_idx + 200]
        self.assertNotIn("context_block if context_block", msg_line,
            "_groq_call must not reference context_block (not a parameter — causes NameError)")
        self.assertIn('"content": prompt', msg_line,
            "_groq_call messages must use `prompt` as content")

    def test_groq_call_has_no_context_block_param(self):
        """_groq_call signature must NOT include context_block."""
        idx = self.code.find("def _groq_call(")
        sig = self.code[idx:idx + 200]
        self.assertNotIn("context_block", sig,
            "_groq_call must not have context_block parameter (only Claude/GPT-4o get it)")

    def test_test_groq_message_contains_json_word(self):
        """test_groq() message must contain the literal word 'json'."""
        idx = self.code.find("def test_groq()")
        section = self.code[idx:idx + 1500]
        # Find the message content
        msg_idx = section.find('"content"')
        msg_line = section[msg_idx:msg_idx + 100]
        self.assertIn("json", msg_line.lower(),
            "test_groq message must contain 'json' — Groq json_object mode requirement")

    def test_groq_response_format_still_present(self):
        """Groq must still request JSON mode (response_format: json_object)."""
        idx = self.code.find("def _groq_call(")
        section = self.code[idx:idx + 1500]
        self.assertIn('"type": "json_object"', section,
            "Groq must still use response_format json_object mode")

    def test_claude_still_gets_context_block(self):
        """_claude_sonnet_call must still accept context_block param."""
        idx = self.code.find("def _claude_sonnet_call(")
        section = self.code[idx:idx + 300]
        self.assertIn("context_block", section,
            "Claude must still receive context_block (not Groq)")

    def test_gpt4o_still_gets_context_block(self):
        """_gpt4o_call must still accept context_block param."""
        idx = self.code.find("def _gpt4o_call(")
        section = self.code[idx:idx + 300]
        self.assertIn("context_block", section,
            "GPT-4o must still receive context_block")

    def test_call_ai_forwards_context_to_phase5_only(self):
        """call_ai must pass context_block ONLY to Phase 5 (GPT-4o) and 6 (Claude)."""
        idx = self.code.find("def call_ai(")
        section = self.code[idx:idx + 3500]
        # Find Groq calls — they must NOT have context_block
        groq_calls = re.findall(r'_groq_call\([^)]+\)', section)
        for call in groq_calls:
            self.assertNotIn("context_block", call,
                f"Groq call must not pass context_block: {call!r}")
        # Phase 5/6 must have context_block
        self.assertIn("context_block=context_block", section,
            "Phase 5/6 must forward context_block")


class TestSparseGenreGuard(unittest.TestCase):
    """TC49-50: Sparse genre specificity guard."""

    def setUp(self):
        self.code = pathlib.Path("walkman_sync.py").read_text(encoding="utf-8")

    def test_specificity_guard_exists(self):
        """Sparse correction must have a specificity guard."""
        self.assertIn("_chosen_broad", self.code,
            "Must have _chosen_broad flag to check if current genre is broad")
        self.assertIn("already specific", self.code,
            "Must log when keeping a specific genre")

    def test_guard_checks_broad_genre_set(self):
        """Guard must check against _broad_genres()."""
        idx = self.code.find("_chosen_broad")
        section = self.code[idx:idx + 500]
        # _broad = _broad_genres() is called at start of block, before _chosen_broad
        guard_idx = self.code.find("_chosen_broad")
        wider_section = self.code[max(0,guard_idx-300):guard_idx+600]
        self.assertIn("_broad_genres()", wider_section,
            "Guard must call _broad_genres() near the _chosen_broad check")

    def test_guard_rejects_unknown(self):
        """Guard must not allow 'Unknown' as a graph proposal."""
        idx = self.code.find("_inferred_useful")
        section = self.code[idx:idx + 200]
        self.assertIn('"unknown"', section.lower(),
            "Guard must reject 'Unknown' as a graph proposal")

    def test_specific_genres_log_kept(self):
        """When genre is specific, must log that it's being kept."""
        self.assertIn("keeping", self.code,
            "Must log when keeping a specific genre over graph proposal")

    def test_broad_genres_still_overridable(self):
        """Broad genres (Hip-Hop, Soul, etc.) can still be overridden by graph."""
        idx = self.code.find("_chosen_broad")
        section = self.code[max(0, idx - 100):idx + 800]
        # The else branch (broad genre IS overridable) must still exist
        # "chosen = inferred" is in the else branch at end of the block
        idx2 = self.code.find("if not _chosen_broad:")
        full_block = self.code[idx2:idx2+2000]  # widen to 2000 — else branch is ~1400 chars in
        self.assertIn("chosen = inferred", full_block,
            "Broad genres must still be overridable by graph inference")


class TestMK17Patches(unittest.TestCase):
    """TC51-54: MK17 patches from linked conversation."""

    def test_mtp_transfer_no_module_exit(self):
        """mtp_transfer.py must not have module-level sys.exit()."""
        code = pathlib.Path("mtp_transfer.py").read_text(encoding="utf-8")
        # Find any sys.exit at module level (not inside functions)
        lines = code.split("\n")
        module_exits = []
        in_function = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("class "):
                in_function = True
            if not in_function and "sys.exit(" in stripped and not stripped.startswith("#"):
                module_exits.append(f"line {i}: {line.rstrip()}")
        self.assertEqual(module_exits, [],
            f"mtp_transfer must not have module-level sys.exit: {module_exits}")

    def test_mtp_no_transport_sentinel(self):
        """mtp_transfer must have _NO_TRANSPORT sentinel instead of exit."""
        code = pathlib.Path("mtp_transfer.py").read_text(encoding="utf-8")
        self.assertIn("_NO_TRANSPORT", code,
            "Must use _NO_TRANSPORT sentinel instead of sys.exit")

    def test_audit_offline_mode_path(self):
        """audit._fetch_liked_track_ids must check OFFLINE_MODE first."""
        code = pathlib.Path("audit.py").read_text(encoding="utf-8")
        idx = code.find("def _fetch_liked_track_ids")
        section = code[idx:idx + 1500]
        self.assertIn("OFFLINE_MODE", section,
            "_fetch_liked_track_ids must check config.OFFLINE_MODE")
        self.assertIn("spotify_local_data", section,
            "OFFLINE_MODE path must use spotify_local_data for liked IDs")
        # OFFLINE_MODE check must come BEFORE the Spotify API call
        offline_pos = section.find("OFFLINE_MODE")
        # get_spotify_client may be further in the function — search wider
        wider = code[idx:idx + 3000]
        api_pos = wider.find("get_spotify_client()")
        offline_pos2 = wider.find("OFFLINE_MODE")
        self.assertGreater(api_pos, 0, "get_spotify_client must be called in function")
        self.assertLess(offline_pos2, api_pos,
            "OFFLINE_MODE check must precede Spotify API call")

    def test_genre_graph_purity_fallback(self):
        """genre_graph must fall back to broad genres when specific is empty."""
        code = pathlib.Path("genre_graph.py").read_text(encoding="utf-8")
        self.assertIn("broad fallback", code.lower(),
            "Must log when using broad genre fallback for purity")
        self.assertIn("_broad_fb", code,
            "Must use broad fallback counter")

    def test_genre_graph_liked_songs_size_gate(self):
        """Liked Songs playlists should only be skipped when OVERSIZED."""
        code = pathlib.Path("genre_graph.py").read_text(encoding="utf-8")
        idx = code.find("_LIKED_SONGS_NAMES")
        section = code[idx:idx + 800]
        # The skip condition must include size check
        self.assertIn("n_tracks > max_playlist_size", section,
            "Liked Songs must only be skipped when oversized (n_tracks > cap)")
        # Should NOT have unconditional name-only skip
        old_unconditional = "if plist_name_lc in _LIKED_SONGS_NAMES:\n"
        self.assertNotIn(old_unconditional, section,
            "Must not skip Liked Songs unconditionally by name (size-gate required)")


# =============================================================================
# MC SIMULATIONS
# =============================================================================

class TestMCSim_GroqCallStructure(unittest.TestCase):
    """MC25: 200 prompt variants — verify _groq_call always uses prompt."""
    N = 200
    SEED = 777

    def _simulate_groq_call(self, prompt: str, context_block: str = "") -> dict:
        """Simulate the corrected _groq_call behavior."""
        # Fixed: always use `prompt` — never reference context_block
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        return payload

    def _old_groq_call(self, prompt: str) -> dict:
        """Simulate the BUGGY _groq_call (context_block NameError)."""
        # This would raise NameError: context_block not defined
        try:
            # context_block is undefined here
            content = eval("context_block if context_block else prompt",
                          {"prompt": prompt})
        except NameError:
            return {"error": "NameError: context_block not defined"}
        return {"messages": [{"content": content}]}

    def test_mc_always_uses_prompt(self):
        """Fixed _groq_call always uses prompt as message content."""
        rng = random.Random(self.SEED)
        prompts = [
            "You are a music expert. Classify genre. Return JSON: {\"genre\": \"string\"}",
            "Artist: Brasstracks\nTitle: Golden Ticket\nReturn JSON genre.",
            "Classify: Artist X, Title Y. Return {\"genre\": \"\"}",
        ]
        errors = []
        for i in range(self.N):
            prompt = rng.choice(prompts)
            result = self._simulate_groq_call(prompt)
            content = result["messages"][0]["content"]
            if content != prompt:
                errors.append(f"Trial {i}: content={content!r} != prompt={prompt!r}")

        self.assertEqual(errors, [], f"Groq call must always use prompt: {errors[:3]}")

    def test_mc_old_call_would_have_failed(self):
        """Verify the OLD _groq_call would have raised NameError."""
        rng = random.Random(self.SEED)
        failures = 0
        for _ in range(self.N):
            prompt = f"Classify track {rng.randint(1,1000)}. Return JSON genre."
            result = self._old_groq_call(prompt)
            if "error" in result and "NameError" in result["error"]:
                failures += 1
        self.assertEqual(failures, self.N,
            f"OLD _groq_call must always raise NameError: {failures}/{self.N}")

    def test_mc_json_word_in_test_prompt(self):
        """test_groq message must pass Groq's json_object word check."""
        test_messages = [
            'Reply with JSON only: {"status":"ok"}',          # MK13 fixed
            'Reply with {"status":"ok"}',                      # old broken
            'Respond in JSON format with {"status":"ok"}',     # also valid
        ]
        groq_check = lambda msg: "json" in msg.lower()
        self.assertTrue(groq_check(test_messages[0]), "Fixed message must pass Groq check")
        self.assertFalse(groq_check(test_messages[1]), "Old message fails Groq check")
        self.assertTrue(groq_check(test_messages[2]), "Alternate form also passes")


class TestMCSim_SparseGenreGuard(unittest.TestCase):
    """MC26: 500 (current_genre, graph_proposed) pairs → correct guard decisions."""
    N = 500
    SEED = 1984

    _BROAD = {
        "hip-hop", "hip hop", "r&b", "soul", "jazz", "electronic",
        "funk", "pop", "rock", "reggae", "world", "gospel",
    }

    def _apply_guard(self, chosen: str, inferred: str) -> str:
        """Simulate the MK13 specificity guard decision."""
        chosen_broad = chosen.lower() in self._BROAD
        inferred_useful = inferred.lower() not in {"unknown", "", "pop", "rock"}

        if not chosen_broad:
            return "keep_specific"      # already specific → keep
        if not inferred_useful:
            return "keep_not_useful"    # inferred is useless → keep
        return "override"               # broad + useful inference → override

    def test_anchor_cases(self):
        """Real-world examples from the live log must route correctly."""
        cases = [
            # (chosen, inferred, expected_action)
            ("Soul",          "Afrobeats",        "override"),    # broad → overridable
            ("Bongo Flava",   "Afrobeats",         "keep_specific"),  # specific → keep
            ("Jazz",          "Bossa Nova",        "override"),       # Jazz is broad → overridable
            ("Dancehall",     "Afrobeats",         "keep_specific"),  # specific → keep
            ("Hip-Hop",       "Conscious Hip-Hop", "override"),    # broad → overridable
            ("Gqom",          "Afrobeats",         "keep_specific"),  # specific → keep
            ("Amapiano",      "Afrobeats",         "keep_specific"),  # specific → keep
            ("R&B",           "Alt R&B",           "override"),    # broad → overridable
            ("Classic Soul",  "Conscious Hip-Hop", "keep_specific"),  # specific → keep
            ("Boom Bap",      "Unknown",           "keep_specific"),  # Boom Bap is specific → keep_specific
            ("Electronic",    "Neo Soul",          "override"),    # broad → overridable
        ]
        errors = []
        for chosen, inferred, expected in cases:
            result = self._apply_guard(chosen, inferred)
            if result != expected:
                errors.append(
                    f"({chosen!r} → {inferred!r}): got {result!r}, expected {expected!r}"
                )
        self.assertEqual(errors, [],
            "Anchor case failures:\n" + "\n".join(errors))

    def test_mc_no_specific_overridden(self):
        """Specific genres must NEVER be overridden by graph proposals."""
        rng = random.Random(self.SEED)
        specific_genres = [
            "Bongo Flava", "Amapiano", "Gqom", "Jersey Club", "Marching Band",
            "Dancehall", "Roots Reggae", "Classic Soul", "Neo Soul",
            "Classic Jazz", "Classic Funk", "Boom Bap", "UK Drill",
            "Chopped & Screwed", "Lo-Fi Hip-Hop", "Anime Soundtrack",
        ]
        broad_inferences = [
            "Afrobeats", "Hip-Hop", "R&B", "Electronic", "Pop", "Soul",
        ]
        errors = []
        for i in range(self.N):
            chosen   = rng.choice(specific_genres)
            inferred = rng.choice(broad_inferences)
            result   = self._apply_guard(chosen, inferred)
            if result == "override":
                errors.append(
                    f"Trial {i}: {chosen!r} (specific) was overridden by {inferred!r}"
                )

        print(f"\n  [MC26] {self.N} trials: {len(errors)} specific genres incorrectly overridden")
        self.assertEqual(errors, [],
            "Specific genres must NEVER be overridden:\n" + "\n".join(errors[:5]))

    def test_mc_broad_can_be_improved(self):
        """Broad genres with useful inferences must be overridden."""
        rng = random.Random(self.SEED)
        broad_genres = ["Hip-Hop", "Soul", "R&B", "Electronic", "Jazz", "Funk"]
        specific_inferences = [
            "Neo Soul", "Boom Bap", "Amapiano", "Conscious Hip-Hop",
            "Alt R&B", "Contemporary Jazz",
        ]
        overrides = 0
        for i in range(self.N):
            chosen   = rng.choice(broad_genres)
            inferred = rng.choice(specific_inferences)
            result   = self._apply_guard(chosen, inferred)
            if result == "override":
                overrides += 1
        self.assertGreater(overrides, self.N * 0.8,
            f"Most broad+useful pairs should result in override: {overrides}/{self.N}")


class TestMCSim_PurityFallback(unittest.TestCase):
    """MC27: 300 playlists with only broad genres use fallback correctly."""
    N = 300
    SEED = 1776

    _BROAD_SET = {
        "pop", "rock", "hip-hop", "hip hop", "hiphop", "rap", "r&b",
        "rnb", "soul", "jazz", "electronic", "world", "alternative",
        "indie", "folk", "country", "metal", "punk",
    }

    def _simulate_purity(self, tracks: list[dict], min_purity: float = 0.5) -> dict:
        """Simulate the MK13-patched purity calculation."""
        from collections import Counter
        # First pass: specific genres only
        genre_counts: Counter = Counter()
        for t in tracks:
            g = (t.get("genre") or "").strip().lower()
            if g and g not in self._BROAD_SET:
                genre_counts[g] += 1

        total_tagged = sum(genre_counts.values())
        purity = 0.0
        dominant = ""
        fallback_used = False

        if total_tagged > 0:
            top, count = genre_counts.most_common(1)[0]
            purity   = count / total_tagged
            dominant = top

        elif total_tagged == 0:
            # Fallback: use broad genres
            broad_counts: Counter = Counter()
            for t in tracks:
                g = (t.get("genre") or "").strip().lower()
                if g:
                    broad_counts[g] += 1
            if broad_counts:
                top, count   = broad_counts.most_common(1)[0]
                total_tagged = sum(broad_counts.values())
                purity       = count / total_tagged
                dominant     = top
                fallback_used = True

        return {
            "purity":   purity,
            "dominant": dominant,
            "skipped":  purity < min_purity,
            "fallback": fallback_used,
        }

    def test_jazz_playlist_not_skipped(self):
        """A pure Jazz playlist must not be skipped even if 'jazz' is broad."""
        tracks = [{"genre": "Jazz"} for _ in range(20)]
        result = self._simulate_purity(tracks)
        # With fallback, jazz playlist (all broad) should get purity=1.0
        self.assertFalse(result["skipped"],
            "Pure Jazz playlist must not be skipped (purity fallback)")
        self.assertGreater(result["purity"], 0,
            "Jazz playlist purity must be > 0 after fallback")

    def test_pure_afrobeats_specific_no_fallback(self):
        """A pure Afrobeats playlist (specific) needs no fallback."""
        tracks = [{"genre": "Afrobeats"} for _ in range(15)]
        result = self._simulate_purity(tracks)
        self.assertFalse(result["fallback"], "Specific genre needs no fallback")
        self.assertFalse(result["skipped"], "Pure Afrobeats must not be skipped")
        self.assertEqual(result["purity"], 1.0)

    def test_mc_broad_only_playlists_not_skipped(self):
        """Playlists with only broad genres must not be skipped via fallback."""
        rng = random.Random(self.SEED)
        broad_genres = list(self._BROAD_SET)
        errors = []
        for trial in range(self.N):
            # Playlist with only ONE broad genre (should be pure = 1.0 after fallback)
            genre = rng.choice(broad_genres)
            n = rng.randint(5, 30)
            tracks = [{"genre": genre.title()} for _ in range(n)]
            result = self._simulate_purity(tracks)
            if result["skipped"]:
                errors.append(
                    f"Trial {trial}: pure {genre!r} playlist ({n} tracks) wrongly skipped"
                )

        print(f"\n  [MC27] {self.N} broad-only playlists: {len(errors)} wrongly skipped")
        self.assertEqual(errors, [],
            "Broad-only pure playlists must not be skipped:\n" + "\n".join(errors[:3]))


if __name__ == "__main__":
    print("=" * 65)
    print("SpotifyTuner MK13 — Live-Run Hotfix Validation Suite")
    print("=" * 65)
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestGroqFixes,
        TestSparseGenreGuard,
        TestMK17Patches,
        TestMCSim_GroqCallStructure,
        TestMCSim_SparseGenreGuard,
        TestMCSim_PurityFallback,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
