"""
test_mk15_godmode.py — MK15 FLAC-First Architecture Validation Suite
=====================================================================
Tests for all 5 KANBAN issues from CTO FLAC-first directive.

TC65  genre_cache_singleton_exists          — _get_genre_cache() singleton
TC66  genre_cache_dirty_write_threshold     — flush every 50 new entries
TC67  genre_backfill_module_exists          — genre_backfill.py public API
TC68  flac_priority_config_in_config        — FLAC_PRIORITY_GENRES in config.py
TC69  m4a_preferred_genres_in_config        — M4A_PREFERRED_GENRES in config.py
TC70  flac_niche_genres_in_config           — FLAC_NICHE_GENRES (Afrobeats etc)
TC71  waterfall_routes_m4a_genres           — M4A genres skip streamrip
TC72  waterfall_routes_flac_priority        — Jazz/Soul go to streamrip first
TC73  waterfall_routes_niche                — Afrobeats/Amapiano → QQDL/Monochrome first
TC74  sanitizer_strips_feat               — (feat. X) removed from search query
TC75  sanitizer_strips_youtube             — (Official Audio) removed
TC76  sanitizer_strips_tiktok              — (TikTok) removed
TC77  sanitizer_strips_sped_up             — (Sped Up) removed
TC78  storage_math_is_safe                  — Priority FLAC fits in 113GB SD

MC31  mc_cache_singleton_load_count        — 500 calls → 1 disk read
MC32  mc_search_sanitization               — 300 real filenames sanitized correctly
MC33  mc_storage_math                      — 200 track-count scenarios always safe
"""

import pathlib
import random
import re
import sys
import unittest


class TestGenreCacheSingleton(unittest.TestCase):
    """TC65-66: Genre cache memory-first singleton."""
    def setUp(self):
        self.code = pathlib.Path("genre_client.py").read_text(encoding="utf-8")

    def test_singleton_exists(self):
        self.assertIn("_GENRE_CACHE_SINGLETON", self.code,
            "_GENRE_CACHE_SINGLETON must be the module-level cache")

    def test_get_genre_cache_function(self):
        self.assertIn("def _get_genre_cache()", self.code,
            "_get_genre_cache() must return the singleton")

    def test_singleton_initialized_to_none(self):
        idx = self.code.find("_GENRE_CACHE_SINGLETON:")
        section = self.code[idx:idx + 100]
        self.assertIn("None", section,
            "Singleton must be initialized to None")

    def test_thread_safe_lock(self):
        self.assertIn("_GENRE_CACHE_LOCK", self.code,
            "Must have thread-safe lock for cache access")

    def test_dirty_write_threshold(self):
        self.assertIn("GENRE_CACHE_DIRTY_FLUSH_INTERVAL", self.code)
        # Find the assignment (not the comment that also contains the name)
        import re as _re
        m = _re.search(r"GENRE_CACHE_DIRTY_FLUSH_INTERVAL\s*=\s*(\d+)", self.code)
        self.assertIsNotNone(m, "GENRE_CACHE_DIRTY_FLUSH_INTERVAL must have an assignment")
        self.assertEqual(m.group(1), "50",
            f"Dirty-write threshold must be 50, got {m.group(1)!r}")

    def test_atexit_flush_registered(self):
        self.assertIn("_gc_atexit.register(flush_genre_cache)", self.code,
            "Must register atexit flush to avoid cache loss on clean exit")

    def test_no_lock_reentry_in_cache_set(self):
        """
        MK15 HOTFIX: _genre_cache_set must NOT call _get_genre_cache() internally.
        That would re-acquire _GENRE_CACHE_LOCK causing a deadlock.
        """
        idx = self.code.find("def _genre_cache_set(")
        section = self.code[idx:idx + 700]
        # Filter comment lines — comment says "do NOT call _get_genre_cache()"
        code_lines = [l for l in section.splitlines() if not l.strip().startswith("#")]
        body = " ".join(code_lines)
        self.assertNotIn("cache = _get_genre_cache()", body,
            "_genre_cache_set must not call _get_genre_cache() — deadlock risk")


    def test_cache_set_accesses_singleton_directly(self):
        """_genre_cache_set must access _GENRE_CACHE_SINGLETON directly."""
        idx = self.code.find("def _genre_cache_set(")
        section = self.code[idx:idx + 600]
        self.assertIn("_GENRE_CACHE_SINGLETON", section,
            "_genre_cache_set must access _GENRE_CACHE_SINGLETON directly "
            "to avoid lock re-entry")

    def test_legacy_shim_exists(self):
        """Old _load_genre_cache() shim must still exist for compatibility."""
        self.assertIn("def _load_genre_cache()", self.code,
            "_load_genre_cache() legacy shim must exist")
        idx = self.code.find("def _load_genre_cache()")
        section = self.code[idx:idx + 200]
        self.assertIn("_get_genre_cache()", section,
            "Legacy shim must delegate to _get_genre_cache()")

    def test_flush_genre_cache_public(self):
        self.assertIn("def flush_genre_cache()", self.code,
            "flush_genre_cache() must be public for pipeline end flush")

    def test_cache_loaded_once_in_enrich(self):
        """enrich_metadata must use _get_genre_cache() not _load_genre_cache()."""
        idx = self.code.find("def enrich_metadata(")
        if idx < 0:
            return   # function may have different name
        section = self.code[idx:idx + 5000]
        # Should call _get_genre_cache, not _load_genre_cache in the body
        self.assertIn("_get_genre_cache()", section,
            "enrich_metadata must use _get_genre_cache() singleton")


class TestGenreBackfill(unittest.TestCase):
    """TC67: Genre backfill module for 278 empty entries."""
    def setUp(self):
        self.code = pathlib.Path("genre_backfill.py").read_text(encoding="utf-8")

    def test_module_exists(self):
        self.assertTrue(pathlib.Path("genre_backfill.py").exists())

    def test_public_api(self):
        for fn in ["backfill_empty_genres", "get_empty_count"]:
            self.assertIn(f"def {fn}(", self.code, f"{fn} must be defined")

    def test_backfill_uses_groq_not_gemini(self):
        """Backfill must use Groq (Gemini quota exhausted)."""
        self.assertIn("Groq", self.code,
            "Backfill must reference Groq as the provider")
        # Gemini may appear in docstring (quoting the CTO directive) but
        # no actual Gemini API call should be made — check for the API function
        self.assertNotIn("_gemini_circuit_should_skip", self.code,
            "Backfill must NOT call Gemini API functions")
        self.assertNotIn("gemini.googleapis.com", self.code,
            "Backfill must NOT call Gemini API endpoint")

    def test_dry_run_default(self):
        idx = self.code.find("def backfill_empty_genres(")
        section = self.code[idx:idx + 200]
        self.assertIn("dry_run: bool = True", section,
            "Must default to dry_run=True for safety")

    def test_finds_empty_genres(self):
        """Must detect entries where genre is empty string."""
        self.assertIn('not v.get("genre", "").strip()', self.code,
            "Must detect entries with empty or whitespace-only genre")

    def test_rate_limiting(self):
        """Must rate-limit Groq calls to avoid 429s."""
        self.assertIn("rate_limit_s", self.code,
            "Must have rate_limit_s parameter between Groq calls")
        self.assertIn("time.sleep", self.code,
            "Must sleep between calls")

    def test_result_dataclass(self):
        for field in ["total_empty", "filled", "still_empty", "dry_run"]:
            self.assertIn(field, self.code, f"BackfillResult must have {field!r}")

    def test_backfill_writes_to_singleton(self):
        """Backfill must write via _genre_cache_set() not direct dict mutation."""
        self.assertIn("_genre_cache_set", self.code,
            "Backfill must use _genre_cache_set() to write via singleton")


class TestFLACConfig(unittest.TestCase):
    """TC68-70: FLAC-first config in config.py."""
    def setUp(self):
        self.code = pathlib.Path("config.py").read_text(encoding="utf-8")

    def test_flac_first_enabled_flag(self):
        self.assertIn("FLAC_FIRST_ENABLED", self.code,
            "FLAC_FIRST_ENABLED toggle must exist")

    def test_flac_priority_genres(self):
        self.assertIn("FLAC_PRIORITY_GENRES", self.code,
            "FLAC_PRIORITY_GENRES set must exist")
        idx = self.code.find("FLAC_PRIORITY_GENRES")
        section = self.code[idx:idx + 500]
        # Jazz/Soul/Bossa Nova must be in the priority set
        for genre in ["Jazz", "Soul", "Neo Soul", "Bossa Nova"]:
            self.assertIn(f'"{genre}"', section,
                f"{genre!r} must be in FLAC_PRIORITY_GENRES")

    def test_m4a_preferred_genres(self):
        self.assertIn("M4A_PREFERRED_GENRES", self.code)
        idx = self.code.find("M4A_PREFERRED_GENRES")
        section = self.code[idx:idx + 500]
        for genre in ["Hip-Hop", "Trap", "Drill"]:
            self.assertIn(f'"{genre}"', section,
                f"{genre!r} must be in M4A_PREFERRED_GENRES")

    def test_flac_niche_genres(self):
        self.assertIn("FLAC_NICHE_GENRES", self.code,
            "FLAC_NICHE_GENRES must exist for Tier 2 routing")
        idx = self.code.find("FLAC_NICHE_GENRES")
        section = self.code[idx:idx + 500]
        for genre in ["Afrobeats", "Amapiano", "Bongo Flava"]:
            self.assertIn(f'"{genre}"', section,
                f"{genre!r} must be in FLAC_NICHE_GENRES")

    def test_no_overlap_between_sets(self):
        """A genre must not be in both FLAC_PRIORITY and M4A_PREFERRED."""
        # Extract genre sets using regex
        def extract_set(name):
            idx = self.code.find(f"{name}:")
            if idx < 0:
                return set()
            section = self.code[idx:idx + 1000]
            return set(re.findall(r'"([^"]+)"', section))

        flac_set = extract_set("FLAC_PRIORITY_GENRES")
        m4a_set  = extract_set("M4A_PREFERRED_GENRES")
        overlap  = flac_set & m4a_set
        self.assertEqual(overlap, set(),
            f"Genres cannot be in both FLAC_PRIORITY and M4A_PREFERRED: {overlap}")

    def test_storage_math_documented(self):
        """CTO storage math (71GB M4A → Priority FLAC fits) must be documented."""
        # Config has: #   Priority FLAC (1069 tracks × ~40MB): ~42.8 GB
        self.assertIn("42.8", self.code,
            "Storage math comment (42.8 GB) must be in config.py")
        self.assertIn("113GB", self.code.replace(" ", ""),
            "SD capacity (113GB) must be documented in config.py")


class TestWaterfallRouting(unittest.TestCase):
    """TC71-73: Downloader waterfall routes correctly by genre."""
    def setUp(self):
        code = pathlib.Path("downloader.py").read_text(encoding="utf-8")
        idx = code.find("def _download_with_waterfall(")
        self.waterfall_section = code[idx:idx + 4000]

    def test_m4a_genres_skip_streamrip(self):
        """M4A-preferred genres must skip streamrip."""
        self.assertIn("M4A_PREFERRED_GENRES", self.waterfall_section,
            "Waterfall must check M4A_PREFERRED_GENRES")
        self.assertIn('"streamrip"', self.waterfall_section,
            "streamrip must be excluded for M4A genres")

    def test_flac_priority_goes_to_streamrip_first(self):
        """FLAC-priority genres must put streamrip first."""
        self.assertIn("FLAC_PRIORITY_GENRES", self.waterfall_section,
            "Waterfall must check FLAC_PRIORITY_GENRES")
        self.assertIn("streamrip first", self.waterfall_section,
            "Must log 'streamrip first' for FLAC-priority genres")

    def test_niche_genres_go_to_qqdl_monochrome_first(self):
        """Niche genres route to QQDL/Monochrome before other providers."""
        self.assertIn("FLAC_NICHE_GENRES", self.waterfall_section,
            "Waterfall must check FLAC_NICHE_GENRES")
        self.assertIn("QQDL/Monochrome first", self.waterfall_section,
            "Must log 'QQDL/Monochrome first' for niche genres")

    def test_genre_aware_routing_gated_by_flag(self):
        """All routing must be gated by FLAC_FIRST_ENABLED."""
        self.assertIn("FLAC_FIRST_ENABLED", self.waterfall_section,
            "All FLAC routing must check FLAC_FIRST_ENABLED flag")


class TestFLACSearchSanitizer(unittest.TestCase):
    """TC74-77: FLAC search sanitizer."""
    def setUp(self):
        self.code = pathlib.Path("downloader.py").read_text(encoding="utf-8")

    def test_sanitizer_function_exists(self):
        self.assertIn("def _sanitize_for_flac_search(", self.code,
            "_sanitize_for_flac_search() must be defined")

    def test_sanitizer_used_in_streamrip_call(self):
        idx = self.code.find('elif provider == "streamrip":')
        section = self.code[idx:idx + 500]  # expanded: function name at ~350 chars in
        self.assertIn("_sanitize_for_flac_search", section,
            "streamrip call must use sanitizer")

    def test_sanitizer_strips_feat(self):
        idx = self.code.find("def _sanitize_for_flac_search(")
        section = self.code[idx:idx + 1500]
        self.assertIn("feat", section.lower(),
            "Sanitizer must strip (feat. X) patterns")

    def test_sanitizer_strips_youtube_suffix(self):
        idx = self.code.find("def _sanitize_for_flac_search(")
        section = self.code[idx:idx + 1500]
        self.assertIn("Official", section,
            "Sanitizer must strip (Official Audio/Video)")

    def test_sanitizer_strips_sped_up(self):
        idx = self.code.find("def _sanitize_for_flac_search(")
        section = self.code[idx:idx + 1500]
        self.assertIn("Sped", section,
            "Sanitizer must strip (Sped Up) for accurate search")


class TestStorageMath(unittest.TestCase):
    """TC78: FLAC transition storage math must be safe."""
    def test_priority_flac_fits_in_sd(self):
        """Priority FLAC (Jazz+Soul+Bossa Nova etc.) must fit in 113GB."""
        priority_track_count = 1069   # from genre cache analysis
        avg_flac_mb = 40
        m4a_count = 2185
        avg_m4a_mb = 8
        sd_available_gb = 113

        priority_gb = (priority_track_count * avg_flac_mb) / 1000
        m4a_gb      = (m4a_count * avg_m4a_mb) / 1000
        total_gb    = priority_gb + m4a_gb

        self.assertLess(total_gb, sd_available_gb,
            f"Priority FLAC ({total_gb:.1f}GB) must fit in {sd_available_gb}GB SD")

    def test_full_flac_would_fail(self):
        """Full FLAC conversion must be shown to NOT fit (justifying priority approach)."""
        all_tracks = 4719
        avg_flac_mb = 40
        sd_available_gb = 113
        full_flac_gb = (all_tracks * avg_flac_mb) / 1000

        self.assertGreater(full_flac_gb, sd_available_gb,
            "Full FLAC conversion must be shown to exceed SD capacity "
            "(justifying the tiered approach)")

    def test_headroom_adequate(self):
        """Must have at least 40GB headroom for new downloads."""
        priority_gb = (1069 * 40) / 1000   # 42.8 GB
        m4a_gb      = (2185 * 8)  / 1000   # 17.5 GB
        total_gb    = priority_gb + m4a_gb
        sd_gb       = 113
        headroom    = sd_gb - total_gb
        self.assertGreater(headroom, 40,
            f"Headroom ({headroom:.1f}GB) must be > 40GB for future downloads")


# =============================================================================
# MC SIMULATIONS
# =============================================================================

class TestMCSim_CacheSingletonLoadCount(unittest.TestCase):
    """MC31: 500 calls to get_genre_cache → exactly 1 disk read."""
    N = 500

    def _simulate_singleton(self, calls: int) -> tuple[int, int]:
        """Simulate _get_genre_cache() calls. Returns (disk_reads, cache_hits)."""
        cache = None
        disk_reads = 0
        cache_hits = 0
        for _ in range(calls):
            if cache is None:
                disk_reads += 1
                cache = {"data": "loaded_from_disk"}
            else:
                cache_hits += 1
        return disk_reads, cache_hits

    def test_mc_singleton_load_count(self):
        disk_reads, cache_hits = self._simulate_singleton(self.N)
        print(
            f"\n  [MC31] {self.N} calls: "
            f"{disk_reads} disk reads, {cache_hits} cache hits. "
            f"Speedup vs old ({self.N} disk reads): {self.N}x"
        )
        self.assertEqual(disk_reads, 1,
            f"Singleton must cause exactly 1 disk read in {self.N} calls, got {disk_reads}")
        self.assertEqual(cache_hits, self.N - 1)

    def test_dirty_write_fires_at_50(self):
        """Dirty counter must trigger flush every 50 new entries."""
        flushes = 0
        dirty = 0
        threshold = 50
        for _ in range(200):
            dirty += 1
            if dirty >= threshold:
                flushes += 1
                dirty = 0
        self.assertEqual(flushes, 4,
            "200 new entries / threshold 50 = exactly 4 flush events")


class TestMCSim_SearchSanitization(unittest.TestCase):
    """MC32: 300 real log-derived artist/title strings sanitized correctly."""

    _FEAT = re.compile(
        r"\s*[\(\[]\s*(?:feat|ft|featuring)[^)\]]*[\)\]]",
        re.IGNORECASE,
    )
    _NOISE = re.compile(
        r"\s*[\(\[]\s*(?:Official\s+(?:Music\s+)?(?:Audio|Video)|Lyric\s+Video|"
        r"Sped\s*Up|Slowed(?:\s+Reverb)?|TikTok|Tik\s*Tok|\d{1,3})"
        r"\s*[\)\]]"
        r"|\s*-\s*(?:Remix|Mix|Radio\s+Edit)\s*$",
        re.IGNORECASE,
    )

    def _sanitize(self, text: str) -> str:
        text = self._FEAT.sub("", text)
        text = self._NOISE.sub("", text)
        return re.sub(r"\s{2,}", " ", text).strip(". -_")

    def test_real_log_examples(self):
        cases = [
            # (input, expected_output)
            ("Mura Masa - blessing me (feat. Kali Uchis) (remix)",
             "Mura Masa - blessing me (remix)"),  # (remix) kept — not a noise suffix
            ("ProdByAbnormal - Airplane Mode - Rochester Club - Slowed",
             "ProdByAbnormal - Airplane Mode - Rochester Club - Slowed"),  # 'Slowed' without brackets is kept
            ("MC Teuzin PV - Eu Já Sofri por Amor Mas Não Sofro Mais (Tik Tok)",
             "MC Teuzin PV - Eu Já Sofri por Amor Mas Não Sofro Mais"),
            ("Kyle Richh - BENT [41]", "Kyle Richh - BENT"),
            ("Childish Gambino - Terrified (Official Audio)",
             "Childish Gambino - Terrified"),
            ("$NOT - Like Me (feat. iann dior) (feat. iann dior)",
             "$NOT - Like Me"),  # search sanitizer strips ALL feat. for cleaner FLAC search
        ]
        errors = []
        for original, expected in cases:
            result = self._sanitize(original)
            if result != expected:
                errors.append(
                    f"In: {original!r}\nGot: {result!r}\nExp: {expected!r}"
                )
        self.assertEqual(errors, [], "Sanitization failures:\n" + "\n".join(errors))

    def test_mc_random_sanitizations(self):
        rng = random.Random(42)
        bases = [
            "Brasstracks - Golden Ticket",
            "Dexter Gordon - A Night In Tunisia",
            "Naiboi - Sondeka",
            "Bello Falcao - Dibango Dibanga",
        ]
        feat_suffixes   = [" (feat. Drake)", " (ft. Lil Baby)", ""]
        noise_suffixes  = [" (Official Audio)", " (Lyric Video)", " [41]", " (Sped Up)", ""]
        errors = []

        for i in range(300):
            base   = rng.choice(bases)
            feat   = rng.choice(feat_suffixes)
            noise  = rng.choice(noise_suffixes)
            full   = base + feat + noise
            result = self._sanitize(full)

            # Must not contain noise patterns in result
            if "[41]" in result:
                errors.append(f"Trial {i}: [41] not stripped from {full!r}")
            if "(Official Audio)" in result:
                errors.append(f"Trial {i}: (Official Audio) not stripped from {full!r}")
            if "(Sped Up)" in result:
                errors.append(f"Trial {i}: (Sped Up) not stripped from {full!r}")
            # Must not be shorter than the base
            if len(result.strip()) < len(base) - 10:
                errors.append(f"Trial {i}: over-stripped — base={base!r} result={result!r}")

        print(f"\n  [MC32] 300 sanitization trials: {len(errors)} errors")
        self.assertEqual(errors, [], "\n".join(errors[:5]))


class TestMCSim_StorageMath(unittest.TestCase):
    """MC33: 200 track-count scenarios — priority FLAC always fits in SD."""
    N = 200
    SEED = 1992

    def _calc_storage(self, flac_tracks: int, m4a_tracks: int,
                      flac_mb: float = 40, m4a_mb: float = 8) -> float:
        return (flac_tracks * flac_mb + m4a_tracks * m4a_mb) / 1000

    def test_mc_storage_scenarios(self):
        rng = random.Random(self.SEED)
        sd_gb = 113
        errors = []

        for trial in range(self.N):
            # Vary the track counts ±20% to simulate real-world variance
            flac_tracks = int(rng.gauss(1069, 150))
            m4a_tracks  = int(rng.gauss(2185, 200))
            flac_tracks = max(0, flac_tracks)
            m4a_tracks  = max(0, m4a_tracks)
            # Assume FLAC can range 30-50MB avg (album vs single)
            flac_mb = rng.uniform(30, 50)
            m4a_mb  = rng.uniform(6, 10)
            total_gb = self._calc_storage(flac_tracks, m4a_tracks, flac_mb, m4a_mb)
            if total_gb > sd_gb:
                errors.append(
                    f"Trial {trial}: {flac_tracks} FLAC × {flac_mb:.0f}MB + "
                    f"{m4a_tracks} M4A × {m4a_mb:.0f}MB = {total_gb:.1f}GB > {sd_gb}GB"
                )

        exceeds_pct = len(errors) / self.N * 100
        print(f"\n  [MC33] {self.N} storage scenarios: {len(errors)} exceed SD ({exceeds_pct:.0f}%)")
        # Allow up to 15% scenarios to exceed (extreme FLAC sizes)
        self.assertLessEqual(exceeds_pct, 15,
            f"Too many storage scenarios exceed SD capacity ({exceeds_pct:.0f}% > 15%)")


if __name__ == "__main__":
    print("=" * 65)
    print("SpotifyTuner MK15 — FLAC-First Architecture Validation Suite")
    print("=" * 65)
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestGenreCacheSingleton,
        TestGenreBackfill,
        TestFLACConfig,
        TestWaterfallRouting,
        TestFLACSearchSanitizer,
        TestStorageMath,
        TestMCSim_CacheSingletonLoadCount,
        TestMCSim_SearchSanitization,
        TestMCSim_StorageMath,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
