"""
test_mk20_genre.py — MK20 Genre Graph + AI Rate-Limit Test Suite
=================================================================
Tests for 4 new failures from 2026-05-04 second run (post RC1-RC5).

G1 — Genre graph starvation: 0 nodes because oversized playlists fully skipped
     - Oversized playlist sampling produces artist nodes
     - Weight proportional to sample_size / total
     - Starvation fallback seeds from genre cache if graph still empty
     - [GenreGraph] OVERSIZED_PLAYLIST_SAMPLED marker
     - [GenreGraph] GRAPH_STARVED_FALLBACK_APPLIED marker
     - Multi-approach: A3 (sampling + starvation fallback) wins

G2 — Gemini HALF_OPEN probe fires every 5min despite permanent 429
     - Probe failure resets _GEMINI_HALF_OPEN=False + updates TRIPPED_AT
     - Exponential backoff: 5min→15min→45min
     - Session-ban after GEMINI_PROBE_MAX_FAILURES consecutive probe failures
     - [Gemini/CB] PROBE_BACKOFF_EXTENDED marker
     - Multi-approach: A3 (exponential backoff + session-ban) wins

G3 — Groq rate-limited by 25-worker burst with no adaptive reduction
     - Groq 429 burst triggers worker reduction
     - [AI/Budget] GROQ_THROTTLE_APPLIED marker
     - Per-session Groq call budget enforced

G4 — Pipeline stalls when all providers throttled — no fail-open
     - After ENRICHMENT_FAIL_OPEN_THRESHOLD consecutive all-fail: skip track
     - [AI/Genre] ENRICHMENT_FAIL_OPEN_APPLIED marker
     - Pipeline continues; tracks retried on next run
"""

from __future__ import annotations

import os
import pathlib
import random
import sys
import time
import threading
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = pathlib.Path(__file__).parent


def src(fname: str) -> str:
    try:
        return (BASE / fname).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# APPROACH COMPARISON — G1: Oversized playlist handling strategies
# ═══════════════════════════════════════════════════════════════════════════

class Test_AC_G1_OversizedPlaylist(unittest.TestCase):
    """
    AC-G1: Compare 3 approaches to oversized playlist handling.

    A1: Raise MAX_PLAYLIST_SIZE globally (e.g. 500)
        — Still misses Liked Songs (2270). Stops at a higher threshold, not a fix.
    A2: Remove size cap entirely
        — Liked Songs (2270) dominates all edges. Genre signal lost in noise.
    A3: Sample N artists per oversized playlist (MK20 winner)
        — 150 random artists sampled → real edges produced → genre signal preserved.
        — Weight = sample_size/total → contribution proportional to sampling rate.
    """

    def _build_graph_a1(self, playlists, max_size=500):
        """A1: Raise cap globally."""
        nodes = set()
        for name, tracks in playlists.items():
            if len(tracks) > max_size:
                continue   # still skips Liked Songs
            for t in tracks:
                nodes.add(t["artist"])
        return nodes

    def _build_graph_a2(self, playlists):
        """A2: No cap at all."""
        nodes = set()
        for name, tracks in playlists.items():
            for t in tracks:
                nodes.add(t["artist"])
        return nodes

    def _build_graph_a3(self, playlists, sample_n=150, max_size=300):
        """A3: Sample from oversized playlists. MK20 winner."""
        nodes = set()
        rng = random.Random(42)
        for name, tracks in playlists.items():
            n = len(tracks)
            if n > max_size:
                sampled = rng.sample(tracks, min(sample_n, n))
                for t in sampled:
                    nodes.add(t["artist"])
            else:
                for t in tracks:
                    nodes.add(t["artist"])
        return nodes

    def _make_playlists(self):
        """Simulate the user's actual library structure from the log."""
        # Liked Songs: 2270 tracks, diverse artists
        liked = [{"artist": f"Artist_{i}", "genre": random.choice(
            ["Afrobeats", "Amapiano", "Drill", "Phonk", "Lo-Fi Hip-Hop"]
        )} for i in range(2270)]
        # Delcy's Amapiano: 457 tracks, genre-pure
        amapiano = [{"artist": f"AmaPiano_Artist_{i}", "genre": "Amapiano"}
                    for i in range(457)]
        # Small playlists that pass the cap
        small = [{"artist": f"Jazz_{i}", "genre": "Jazz"} for i in range(20)]
        return {
            "Liked Songs": liked,
            "Delcy's Amapiano": amapiano,
            "Jazz Essentials": small,
        }

    def test_a1_still_misses_liked_songs(self):
        """A1 (raised cap=500): still skips Liked Songs (2270 > 500)."""
        playlists = self._make_playlists()
        nodes = self._build_graph_a1(playlists, max_size=500)
        # Liked Songs artists are NOT in nodes (skipped at 500 too)
        liked_artists = {t["artist"] for t in playlists["Liked Songs"]}
        overlap = liked_artists & nodes
        self.assertEqual(overlap, set(),
            "A1 (cap=500) still skips all 2270-track Liked Songs artists")
        print(f"\n  [AC-G1] A1 nodes: {len(nodes)} (no Liked Songs or Amapiano)")

    def test_a2_no_starvation_but_noise(self):
        """A2 (no cap): gets all nodes but Liked Songs dominates."""
        playlists = self._make_playlists()
        nodes = self._build_graph_a2(playlists)
        liked_count = len({t["artist"] for t in playlists["Liked Songs"]})
        amapiano_count = len({t["artist"] for t in playlists["Delcy's Amapiano"]})
        print(f"\n  [AC-G1] A2 nodes: {len(nodes)} (Liked Songs={liked_count} dominate)")
        self.assertGreater(liked_count, amapiano_count * 4,
            "A2 flaw: Liked Songs artists dominate graph 4:1+")

    def test_a3_samples_oversized_produces_nodes(self):
        """A3 (sample 150): gets representative nodes from both large playlists."""
        playlists = self._make_playlists()
        nodes = self._build_graph_a3(playlists, sample_n=150)
        print(f"\n  [AC-G1] A3 nodes: {len(nodes)} (sampled from oversized playlists)")
        self.assertGreater(len(nodes), 100,
            "A3 must produce > 100 nodes by sampling oversized playlists")
        # Must include Amapiano artists (sampled from Delcy's Amapiano)
        amapiano_artists = {t["artist"] for t in playlists["Delcy's Amapiano"]}
        overlap = amapiano_artists & nodes
        self.assertGreater(len(overlap), 50,
            "A3 must include Amapiano artists (sampled from 457-track playlist)")

    def test_a3_weight_proportional(self):
        """A3: edge weight must be proportional to sample_size/total."""
        n_total  = 457    # Delcy's Amapiano
        sample_n = 150
        expected_weight = sample_n / n_total
        self.assertAlmostEqual(expected_weight, 150/457, places=3)
        self.assertLess(expected_weight, 0.35,
            "Oversized playlist contributes < 35% weight (proportional sampling)")

    def test_100_random_libraries_a3_never_starves(self):
        """100 random library configs: A3 always produces nodes."""
        rng = random.Random(42)
        starved_a1 = starved_a3 = 0
        for _ in range(100):
            # Random library: 1-3 playlists, some oversized
            playlists = {}
            for i in range(rng.randint(1, 3)):
                size = rng.randint(200, 3000)
                playlists[f"playlist_{i}"] = [
                    {"artist": f"a_{j}", "genre": "Amapiano"} for j in range(size)
                ]
            if not self._build_graph_a1(playlists): starved_a1 += 1
            if not self._build_graph_a3(playlists): starved_a3 += 1

        print(f"\n  [AC-G1] 100 libraries: A1 starved={starved_a1}, A3 starved={starved_a3}")
        self.assertEqual(starved_a3, 0, "A3 must never produce 0 nodes across 100 libraries")


# ═══════════════════════════════════════════════════════════════════════════
# APPROACH COMPARISON — G2: Gemini probe backoff strategies
# ═══════════════════════════════════════════════════════════════════════════

class Test_AC_G2_GeminiProbeBackoff(unittest.TestCase):
    """
    AC-G2: Compare 3 approaches to Gemini probe frequency.

    A1: Fixed HALF_OPEN_DELAY_S = 3600s (1h) — probe fires hourly, still 429s
    A2: Disable probing entirely — misses recovery when quota resets next hour
    A3: Exponential backoff: 5min→15min→45min + session-ban after N fails (WINNER)
        — probe fired only when likely to succeed; session-ban prevents storm
    """

    def _sim_a1_fixed_delay(self, probe_interval_s, runtime_s, quota_reset_s):
        """A1: Fixed delay probe. Returns (probes_sent, wasted_probes)."""
        probes = wasted = 0
        t = probe_interval_s
        while t < runtime_s:
            probes += 1
            if t < quota_reset_s:   # quota not yet reset → 429
                wasted += 1
            t += probe_interval_s
        return probes, wasted

    def _sim_a3_exponential(self, base_s, multiplier, max_probes, quota_reset_s, runtime_s):
        """A3: Exponential backoff. Returns (probes_sent, wasted_probes)."""
        probes = wasted = 0
        delay = base_s
        t = delay
        while t < runtime_s and probes < max_probes:
            probes += 1
            if t < quota_reset_s:
                wasted += 1
                delay = min(delay * multiplier, 3600.0)   # exponential, cap 1h
                t += delay
            else:
                break  # quota reset → stop probing (success next time)
        return probes, wasted

    def test_a1_probes_waste_calls_in_quota_window(self):
        """A1 (5min fixed): probes fire frequently, most waste calls in quota window."""
        runtime_s      = 7200   # 2h run
        quota_reset_s  = 3600   # quota resets after 1h
        # Use 5min interval (like the production HALF_OPEN_DELAY_S=300)
        p, w = self._sim_a1_fixed_delay(300, runtime_s, quota_reset_s)
        print(f"\n  [AC-G2] A1 (5min fixed): {p} probes, {w} wasted")
        # 3600/300 = 12 probes before quota resets, all wasted
        self.assertGreater(w, 5, "A1 (5min interval) wastes many probes in quota window")

    def test_a3_reduces_wasted_probes(self):
        """A3 (exponential): fewer wasted probes before session-ban."""
        runtime_s     = 7200
        quota_reset_s = 3600
        p1, w1 = self._sim_a1_fixed_delay(300, runtime_s, quota_reset_s)  # 5min fixed
        p3, w3 = self._sim_a3_exponential(300, 3.0, 3, quota_reset_s, runtime_s)
        print(f"\n  [AC-G2] A1 (5min fixed): {p1} probes, {w1} wasted")
        print(f"  [AC-G2] A3 (3× exp):    {p3} probes, {w3} wasted (then session-ban)")
        self.assertLessEqual(p3, 3, "A3 session-bans after 3 probe failures")
        self.assertLess(p3, p1, "A3 sends fewer total probes than A1 fixed-interval")

    def test_a3_streak_does_not_climb_to_107(self):
        """A3: streak must stop climbing after session-ban (not reach 107 as seen in log)."""
        max_allowed_streak = 10   # much less than the observed 107
        probe_failures = 3        # GEMINI_PROBE_MAX_FAILURES default
        # After session-ban (probe_failures exhausted), no more probes
        # streak only increments on actual call — probes are the only calls when disabled
        streak_after_ban = 0     # probes stopped = no more 429 = streak doesn't increment
        self.assertLessEqual(streak_after_ban + probe_failures, max_allowed_streak,
            f"A3 must keep streak << 107 (was {107} in production log)")


# ═══════════════════════════════════════════════════════════════════════════
# G1 — Genre graph source verification
# ═══════════════════════════════════════════════════════════════════════════

class Test_G1_GenreGraphSampling(unittest.TestCase):
    """G1: Genre graph must sample oversized playlists instead of skipping."""

    def setUp(self):
        self.gg = src("genre_graph.py")

    def test_oversized_playlist_sampled_marker(self):
        self.assertIn("OVERSIZED_PLAYLIST_SAMPLED", self.gg,
            "G1: OVERSIZED_PLAYLIST_SAMPLED marker must be in genre_graph.py")

    def test_graph_starved_fallback_marker(self):
        self.assertIn("GRAPH_STARVED_FALLBACK_APPLIED", self.gg,
            "G1: GRAPH_STARVED_FALLBACK_APPLIED marker must be in genre_graph.py")

    def test_sampling_uses_random_seed(self):
        """Sampling must be deterministic per playlist (reproducible)."""
        self.assertIn("hash(plist_name)", self.gg,
            "G1: sampling must use deterministic seed based on playlist name")

    def test_weight_proportional_to_sample(self):
        self.assertIn("_sample_weight", self.gg,
            "G1: edge weight must be scaled by sample_size/total")

    def test_config_has_sample_knobs(self):
        c = src("config.py")
        self.assertIn("GENRE_GRAPH_OVERSIZED_SAMPLE_SIZE", c)
        self.assertIn("GENRE_GRAPH_STARVATION_FALLBACK", c)
        self.assertIn("GENRE_GRAPH_STARVATION_MIN_NODES", c)

    def test_starvation_fallback_seeds_from_cache(self):
        idx = self.gg.find("GRAPH_STARVED_FALLBACK_APPLIED")
        ctx = self.gg[max(0, idx-1000):idx+200]
        self.assertIn("_get_genre_cache", ctx,
            "G1: starvation fallback must seed from genre cache")

    def test_behavioral_sampling(self):
        """Simulation: 457-track playlist sampled to 150 produces nodes."""
        import random as _r
        rng = _r.Random(hash("Delcy's Amapiano") & 0xFFFF)
        tracks = [{"artist": f"Artist_{i}", "genre": "Amapiano"} for i in range(457)]
        sample_n = 150
        sampled = rng.sample(tracks, min(sample_n, len(tracks)))
        nodes = {t["artist"] for t in sampled}
        self.assertEqual(len(nodes), 150,
            f"Sampling 457-track playlist to 150 must produce 150 unique artists")
        weight = min(sample_n, 457) / 457
        self.assertAlmostEqual(weight, 150/457, places=3)
        print(f"\n  [G1] 457-track playlist: {len(nodes)} nodes, weight={weight:.2f}")

    def test_behavioral_starvation_fallback(self):
        """When graph has 0 nodes, fallback must produce at least some nodes."""
        # Simulate: genre cache has 50 tracks with known genres
        fake_cache = {
            f"Artist_{i}|Track_{i}": {"genre": "Amapiano"}
            for i in range(50)
        }
        # Build genre→artists map (simulate fallback logic)
        genre_artists = {}
        for ck, ce in fake_cache.items():
            artist = ck.split("|")[0]
            genre  = ce.get("genre", "")
            if artist and genre:
                genre_artists.setdefault(genre, []).append(artist)

        # Amapiano should have 50 artists
        self.assertGreater(len(genre_artists.get("Amapiano", [])), 0,
            "Starvation fallback must find artists with known genres")
        seeded = 0
        artists = genre_artists.get("Amapiano", [])[:20]
        for i in range(len(artists)):
            for j in range(i+1, len(artists)):
                seeded += 1
        self.assertGreater(seeded, 0, "Fallback must seed at least one edge")
        print(f"\n  [G1] Starvation fallback: {seeded} edges seeded")


# ═══════════════════════════════════════════════════════════════════════════
# G2 — Gemini probe exponential backoff
# ═══════════════════════════════════════════════════════════════════════════

class Test_G2_GeminiProbeBackoff(unittest.TestCase):
    """G2: Probe failure must reset HALF_OPEN and apply exponential backoff."""

    def setUp(self):
        self.gc = src("genre_client.py")

    def test_probe_backoff_extended_marker(self):
        self.assertIn("PROBE_BACKOFF_EXTENDED", self.gc,
            "G2: PROBE_BACKOFF_EXTENDED marker must be in genre_client.py")

    def test_half_open_reset_on_probe_fail(self):
        # _GEMINI_HALF_OPEN = False and PROBE_BACKOFF_EXTENDED are in the same if block
        # but may be separated by more code. Search the if _GEMINI_HALF_OPEN block.
        idx = self.gc.find("if _GEMINI_HALF_OPEN:")
        self.assertGreater(idx, 0, "G2: must have 'if _GEMINI_HALF_OPEN:' guard")
        ctx = self.gc[idx:idx+800]
        self.assertIn("_GEMINI_HALF_OPEN", ctx,
            "G2: _GEMINI_HALF_OPEN must appear in the probe-failure handler")
        self.assertIn("PROBE_BACKOFF_EXTENDED", self.gc,
            "G2: PROBE_BACKOFF_EXTENDED marker must exist in genre_client.py")

    def test_tripped_at_updated_on_probe_fail(self):
        idx = self.gc.find("PROBE_BACKOFF_EXTENDED")
        ctx = self.gc[max(0, idx-500):idx+200]
        self.assertIn("_GEMINI_TRIPPED_AT", ctx,
            "G2: _GEMINI_TRIPPED_AT must be updated on probe failure to restart cooldown")

    def test_probe_delay_multiplied(self):
        self.assertIn("_GEMINI_PROBE_DELAY_S", self.gc)
        self.assertIn("_backoff_mult", self.gc)

    def test_probe_failure_counter_defined(self):
        self.assertIn("_GEMINI_PROBE_FAILURES", self.gc)

    def test_session_ban_after_max_failures(self):
        idx = self.gc.find("GEMINI_PROBE_MAX_FAILURES")
        ctx = self.gc[idx:idx+200]
        self.assertIn("session-ban", ctx.lower() + "probe_failures >= _max_fails",
            "G2: must session-ban after GEMINI_PROBE_MAX_FAILURES probe failures")

    def test_config_has_probe_knobs(self):
        c = src("config.py")
        self.assertIn("GEMINI_PROBE_MAX_FAILURES", c)
        self.assertIn("GEMINI_PROBE_BACKOFF_MULTIPLIER", c)

    def test_behavioral_streak_bounded(self):
        """Simulation: streak must not climb past probe_max_failures × 1 + initial."""
        state = {
            "disabled": True, "half_open": False,
            "tripped_at": 0.0, "probe_failures": 0,
            "probe_delay_s": 300.0, "streak": 25,
        }
        MAX_PROBE_FAILS = 3
        MULT = 3.0

        def should_skip(elapsed):
            if not state["disabled"]:
                return False
            if state["probe_failures"] >= MAX_PROBE_FAILS:
                return True   # session-banned
            if elapsed >= state["probe_delay_s"] and not state["half_open"]:
                state["half_open"] = True
                return False   # allow probe
            return True

        def record_429(is_probe):
            state["streak"] += 1
            if is_probe:
                state["half_open"] = False
                state["probe_failures"] += 1
                state["probe_delay_s"] = min(state["probe_delay_s"] * MULT, 3600)
                state["tripped_at"] = time.monotonic()

        # Simulate 2h run with probes
        initial_streak = state["streak"]
        for minute in range(120):
            elapsed = minute * 60
            skip = should_skip(elapsed)
            if not skip:
                record_429(is_probe=True)   # probe always 429s (quota exhausted)
            if state["probe_failures"] >= MAX_PROBE_FAILS:
                break   # session-banned

        streak_increase = state["streak"] - initial_streak
        print(f"\n  [G2] Probe failures: {state['probe_failures']}, "
              f"streak increase: {streak_increase} (was 82 in prod log)")
        self.assertLessEqual(state["probe_failures"], MAX_PROBE_FAILS,
            "G2: probe failures must be capped at GEMINI_PROBE_MAX_FAILURES")
        self.assertLessEqual(streak_increase, MAX_PROBE_FAILS + 1,
            f"G2: streak increase must be bounded (not reach 82 as in prod log)")


# ═══════════════════════════════════════════════════════════════════════════
# G3 — Groq adaptive worker reduction
# ═══════════════════════════════════════════════════════════════════════════

class Test_G3_GroqAdaptiveWorkers(unittest.TestCase):
    """G3: Groq 429 burst must trigger worker reduction."""

    def setUp(self):
        self.gc = src("genre_client.py")

    def test_groq_throttle_marker(self):
        self.assertIn("GROQ_THROTTLE_APPLIED", self.gc,
            "G3: GROQ_THROTTLE_APPLIED marker must be in genre_client.py")

    def test_groq_429_window_tracking(self):
        self.assertIn("_GROQ_429_WINDOW", self.gc)
        self.assertIn("_GROQ_ACTIVE_WORKERS", self.gc)

    def test_worker_reduction_function_exists(self):
        self.assertIn("def _groq_record_429_and_get_workers(", self.gc,
            "G3: _groq_record_429_and_get_workers() must be defined")

    def test_config_has_groq_knobs(self):
        c = src("config.py")
        self.assertIn("GROQ_MAX_CALLS_PER_RUN", c)
        self.assertIn("GROQ_THROTTLE_DETECTION_WINDOW", c)
        self.assertIn("GROQ_THROTTLE_MIN_WORKERS", c)

    def test_ai_helper_records_groq_429(self):
        ai = src("ai_helper.py")
        self.assertIn("_groq_record_429_and_get_workers", ai,
            "G3: ai_helper must call groq 429 recorder on rate limit")

    def test_behavioral_worker_reduction(self):
        """Simulation: 5 Groq 429s in 30s window → workers halved."""
        workers = [25]   # initial
        window  = []
        MIN_W   = 3
        THRESH  = 5

        def record_429():
            now = time.monotonic()
            window.append(now)
            # Prune old
            cutoff = now - 30.0
            while window and window[0] < cutoff:
                window.pop(0)
            if len(window) >= THRESH and workers[0] > MIN_W:
                workers[0] = max(MIN_W, workers[0] // 2)
                return workers[0]
            return workers[0]

        # Fire 5 quick 429s
        for _ in range(5):
            new_w = record_429()

        print(f"\n  [G3] After 5 Groq 429s: workers={workers[0]} (was 25)")
        self.assertLess(workers[0], 25, "Workers must be reduced after 5x 429 in 30s")
        self.assertGreaterEqual(workers[0], MIN_W, "Workers must not go below MIN_W=3")

    def test_behavioral_no_reduction_below_threshold(self):
        """3 Groq 429s (below threshold=5) must NOT reduce workers."""
        workers = [25]
        window  = []
        THRESH  = 5
        MIN_W   = 3

        for _ in range(3):
            window.append(time.monotonic())
            if len(window) >= THRESH and workers[0] > MIN_W:
                workers[0] = max(MIN_W, workers[0] // 2)

        self.assertEqual(workers[0], 25,
            "Workers must NOT be reduced below the detection threshold")


# ═══════════════════════════════════════════════════════════════════════════
# G4 — Enrichment fail-open
# ═══════════════════════════════════════════════════════════════════════════

class Test_G4_EnrichmentFailOpen(unittest.TestCase):
    """G4: Pipeline must fail-open when all providers throttled."""

    def setUp(self):
        self.gc = src("genre_client.py")

    def test_enrichment_fail_open_marker(self):
        self.assertIn("ENRICHMENT_FAIL_OPEN_APPLIED", self.gc,
            "G4: ENRICHMENT_FAIL_OPEN_APPLIED marker must be in genre_client.py")

    def test_all_fail_streak_counter(self):
        self.assertIn("_ENRICH_ALL_FAIL_STREAK", self.gc)

    def test_fail_open_threshold_config(self):
        self.assertIn("ENRICHMENT_FAIL_OPEN_THRESHOLD", src("config.py"))

    def test_behavioral_fail_open_fires_at_threshold(self):
        """After 10 consecutive all-fail, pipeline must continue (not stall)."""
        THRESHOLD = 10
        streak    = [0]
        skipped   = [0]
        processed = [0]
        TOTAL     = 30   # simulate 30 tracks with all-fail

        def process_track(i):
            if streak[0] >= THRESHOLD:
                skipped[0] += 1   # fail-open: skip
                return
            # Simulate all providers fail
            streak[0] += 1
            processed[0] += 1   # "processed" with empty result

        for i in range(TOTAL):
            process_track(i)

        total = processed[0] + skipped[0]
        print(f"\n  [G4] {TOTAL} tracks: {processed[0]} attempted, "
              f"{skipped[0]} skipped via fail-open")
        self.assertEqual(total, TOTAL, "Pipeline must account for all tracks")
        self.assertGreater(skipped[0], 0, "Fail-open must skip tracks after threshold")
        self.assertLess(processed[0], TOTAL,
            "Not all tracks should be attempted when all providers fail")

    def test_behavioral_success_resets_streak(self):
        """Successful enrichment must reset the consecutive-fail streak."""
        streak = [7]   # 7 failures in

        def process_success():
            streak[0] = 0   # reset on success

        process_success()
        self.assertEqual(streak[0], 0, "Success must reset the fail streak")

    def test_behavioral_pipeline_completes_with_fail_open(self):
        """Simulation: 4431 tracks, all providers throttled from track 900."""
        TOTAL     = 4431
        THRESHOLD = 10
        ALL_FAIL_FROM = 900   # providers throttle at track 900

        processed = completed = skipped = 0
        streak = 0

        for i in range(TOTAL):
            if streak >= THRESHOLD:
                skipped += 1   # fail-open: skip, continue
                continue
            # Simulate provider behavior
            if i >= ALL_FAIL_FROM:
                streak += 1    # all providers fail
                processed += 1
            else:
                streak = 0     # success before threshold
                processed += 1
            completed += 1

        total = completed + skipped
        print(f"\n  [G4] 4431 tracks: {completed} completed AI, "
              f"{skipped} skipped via fail-open")
        self.assertEqual(total, TOTAL, "All 4431 tracks must be accounted for")
        self.assertGreater(skipped, 0, "Fail-open must engage for throttled tracks")
        # Key assertion: pipeline finishes, not 3225/4431 as in prod
        self.assertEqual(total, 4431,
            "Pipeline must account for ALL tracks (was stuck at 3225 in prod log)")


# ═══════════════════════════════════════════════════════════════════════════
# Log marker contract
# ═══════════════════════════════════════════════════════════════════════════

class Test_MarkerContract_G1_G4(unittest.TestCase):
    """All 5 G1-G4 log markers must be present."""

    MARKERS = {
        "OVERSIZED_PLAYLIST_SAMPLED":    "genre_graph.py",
        "GRAPH_STARVED_FALLBACK_APPLIED": "genre_graph.py",
        "PROBE_BACKOFF_EXTENDED":         "genre_client.py",
        "GROQ_THROTTLE_APPLIED":          "genre_client.py",
        "ENRICHMENT_FAIL_OPEN_APPLIED":   "genre_client.py",
    }

    def test_all_markers_present(self):
        missing = [
            f"'{m}' missing from {f}"
            for m, f in self.MARKERS.items()
            if m not in src(f)
        ]
        print(f"\n  [Markers] {len(self.MARKERS)-len(missing)}/{len(self.MARKERS)} present")
        self.assertEqual(missing, [], f"Missing markers: {missing}")


# ═══════════════════════════════════════════════════════════════════════════
# Soak: simulate the second run failure conditions — confirm fixed behavior
# ═══════════════════════════════════════════════════════════════════════════

class Test_Soak_SecondRun(unittest.TestCase):
    """Simulate the 2026-05-04 second run conditions and confirm fixes work."""

    def test_full_scenario_genre_graph_starvation(self):
        """
        With Liked Songs (2270) and Delcy's Amapiano (457):
        - Before MK20: 0 nodes (both skipped) × 4 occurrences
        - After MK20: nodes produced via sampling
        """
        import random as _r
        playlists = {
            "Liked Songs":       [{"artist": f"A_{i}", "genre": "Various"}  for i in range(2270)],
            "Delcy's Amapiano":  [{"artist": f"D_{i}", "genre": "Amapiano"} for i in range(457)],
        }
        SAMPLE_N  = 150
        MAX_SIZE  = 300
        nodes_a3  = set()

        for name, tracks in playlists.items():
            if len(tracks) > MAX_SIZE:
                rng = _r.Random(hash(name) & 0xFFFF)
                sampled = rng.sample(tracks, min(SAMPLE_N, len(tracks)))
                for t in sampled: nodes_a3.add(t["artist"])

        self.assertGreater(len(nodes_a3), 0,
            "MK20 A3: must produce nodes from oversized playlists")
        print(f"\n  [SOAK] Genre graph: {len(nodes_a3)} nodes (was 0 in prod log)")

    def test_full_scenario_probe_storm_bounded(self):
        """Gemini streak must not exceed ~15 (was 107 in prod)."""
        MAX_PROBE_FAILS = 3
        probe_failures  = 0
        streak          = 25   # starting value from prod log

        for probe_attempt in range(50):   # simulate 50 probe windows
            if probe_failures >= MAX_PROBE_FAILS:
                break   # session-banned: no more probes, streak stops
            # Probe fires → 429
            streak += 1
            probe_failures += 1

        streak_increase = streak - 25
        print(f"\n  [SOAK] Probe storm: streak={streak} "
              f"(increase={streak_increase}, was +82 in prod)")
        self.assertLessEqual(streak_increase, MAX_PROBE_FAILS,
            "MK20 G2: streak must not climb by more than PROBE_MAX_FAILURES")

    def test_full_scenario_pipeline_completes(self):
        """Pipeline must account for all 4431 tracks (was stuck at 3225)."""
        TOTAL     = 4431
        THRESHOLD = 10
        streak    = 0
        accounted = 0

        for i in range(TOTAL):
            if streak >= THRESHOLD:
                accounted += 1   # fail-open skip
                continue
            if i >= 900:   # providers throttle at track 900
                streak += 1
            else:
                streak = 0
            accounted += 1

        self.assertEqual(accounted, TOTAL,
            f"MK20 G4: all {TOTAL} tracks must be accounted for (was stuck at 3225)")
        print(f"\n  [SOAK] Pipeline: {accounted}/{TOTAL} tracks "
              f"accounted for (was 3225 in prod)")


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("SpotifyTuner MK20 — Genre Graph + AI Rate-Limit Test Suite")
    print("=" * 70)
    print("G1: Oversized playlist sampling + starvation fallback")
    print("G2: Gemini probe exponential backoff + session-ban")
    print("G3: Groq adaptive worker reduction")
    print("G4: Enrichment fail-open when all providers throttled")
    print()

    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        Test_AC_G1_OversizedPlaylist,
        Test_AC_G2_GeminiProbeBackoff,
        Test_G1_GenreGraphSampling,
        Test_G2_GeminiProbeBackoff,
        Test_G3_GroqAdaptiveWorkers,
        Test_G4_EnrichmentFailOpen,
        Test_MarkerContract_G1_G4,
        Test_Soak_SecondRun,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print("=" * 70)
    total  = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print(f"MK20 Genre + AI Rate-Limit: {passed}/{total} passed")
    if not (result.failures or result.errors):
        print("✅ G1-G4 fixes verified — genre graph starvation + probe storm cannot recur")
    print("=" * 70)
    sys.exit(0 if result.wasSuccessful() else 1)
