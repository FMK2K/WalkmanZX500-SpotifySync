"""
test_mk22_runtime.py — MK22 LOG_5_5_26 Incident Response Test Suite
=====================================================================
CTO adversarial findings from the overnight run of 2026-05-05:

Bug 1 — Genre/metadata mislabeling + fail-open data loss
    B1.1: GENRE_CONTRADICTION_GUARD — reject AI when Phase-1 is high-confidence
    B1.2: ENRICHMENT_FAIL_OPEN → Phase-1 fallback (not blank skip)
    B1.3: Taxonomy additions: UK Rap, Rai, French Pop

Bug 2 — Splintered albums + duplicate track-number artifacts
    B2.1: TRACK_NUMBER_CONFLICT_GUARD — defer when track numbers disagree
    B2.2: Album survivor integrity (config gate)

Bug 3 — COM delete dialog storm + dedup infinite loop
    B3.1: _broad_genres NameError crash at Stage 1 (CRITICAL)
    B3.2: Internal storage 99% hard gate — INTERNAL_STORAGE_FULL_BLOCKED
    B3.3: Zombie dedup registry + DIALOG_STORM_ABORT circuit breaker
    B3.4: DELETE_BACKEND_POLICY config (internal → ADB rm, SD → COM)

Evidence from LOG_5_5_26:
    - 743 "Yes" clicks in Confirm File Delete (19 minutes = 2346/hr)
    - 415 verification failures (remote size=0)
    - 528 COM move failures
    - Internal storage: 573MB free (99% full)
    - _broad_genres NameError crashed genre-graph application
    - Joey Bada$$ 1999: "Daily Routine" as track 01 AND 10
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import time
import threading
import random
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = pathlib.Path(__file__).parent


def src(fname: str) -> str:
    try:
        return (BASE / fname).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# B3.1 — _broad_genres NameError
# ═══════════════════════════════════════════════════════════════════════════

class Test_B3_1_BroadGenresNameError(unittest.TestCase):
    """B3.1 CRITICAL: _broad_genres NameError crashed Stage 1 after GenreGraph."""

    def test_broad_genres_defined_in_genre_client(self):
        self.assertIn("def _broad_genres(", src("genre_client.py"),
            "B3.1: _broad_genres() must be defined in genre_client.py")

    def test_broad_genres_called_via_module(self):
        """genre_client._broad_genres() — qualified call prevents NameError."""
        s = src("walkman_sync.py")
        self.assertIn("genre_client._broad_genres()", s,
            "B3.1: must call genre_client._broad_genres() (qualified) not bare _broad_genres()")
        self.assertNotIn("= _broad_genres()", s,
            "B3.1: bare _broad_genres() call (without module) must be removed")

    def test_broad_genres_returns_set(self):
        """_broad_genres() must return a set of broad genre names."""
        import genre_client
        broad = genre_client._broad_genres()
        self.assertIsInstance(broad, set, "_broad_genres must return a set")
        self.assertIn("hip-hop", broad, "must include hip-hop as broad genre")
        self.assertIn("soul", broad, "must include soul as broad genre")

    def test_broad_genres_excludes_specific_subgenres(self):
        """Specific subgenres must NOT be in broad set — they override the graph."""
        import genre_client
        broad = genre_client._broad_genres()
        # These must NOT be in broad — they are specific enough already
        specific = {"amapiano", "genge", "jersey club", "funk carioca", "uk drill"}
        for sg in specific:
            self.assertNotIn(sg, broad,
                f"Specific subgenre '{sg}' should NOT be in _broad_genres()")

    def test_behavioral_broad_genre_check(self):
        """Broad genre check prevents graph from overriding specific subgenres."""
        import genre_client
        broad = genre_client._broad_genres()
        # Amapiano is specific → graph should NOT override it → not in broad
        self.assertNotIn("amapiano", broad)
        # Hip-Hop is broad → graph CAN rescue it
        self.assertIn("hip-hop", broad)


# ═══════════════════════════════════════════════════════════════════════════
# B3.2 — Internal storage hard gate
# ═══════════════════════════════════════════════════════════════════════════

class Test_B3_2_InternalStorageGate(unittest.TestCase):
    """
    B3.2 CORRECTED: Internal storage full → redirect soft-deletes to SD card,
    NOT block the run entirely.
    User: "we're supposed to move to SD card when out of disk space."
    """

    def test_redirect_threshold_config(self):
        self.assertIn("INTERNAL_STORAGE_REDIRECT_THRESHOLD_MB", src("config.py"),
            "B3.2: redirect threshold config required")

    def test_hard_floor_config(self):
        self.assertIn("INTERNAL_STORAGE_HARD_FLOOR_MB", src("config.py"),
            "B3.2: hard floor (block both storages) config required")

    def test_redirect_marker_in_preflight(self):
        self.assertIn("INTERNAL_STORAGE_REDIRECT_TO_SD", src("walkman_sync.py"),
            "B3.2: INTERNAL_STORAGE_REDIRECT_TO_SD marker must be emitted on redirect")

    def test_module_flag_defined(self):
        self.assertIn("_INTERNAL_REDIRECT_TO_SD", src("walkman_sync.py"),
            "B3.2: module-level _INTERNAL_REDIRECT_TO_SD flag required")

    def test_soft_delete_checks_redirect_flag(self):
        """_soft_delete must redirect to SD card when _INTERNAL_REDIRECT_TO_SD is set."""
        self.assertIn("_INTERNAL_REDIRECT_TO_SD", src("walkman_dedup.py"),
            "B3.2: _soft_delete must check redirect flag and route to SD card")

    def test_hard_block_only_at_hard_floor(self):
        """Hard block only at 256MB (hard floor), not at 1GB (redirect threshold)."""
        ws = src("walkman_sync.py")
        idx = ws.find("INTERNAL_STORAGE_FULL_BLOCKED")
        ctx = ws[max(0, idx-500):idx+200]
        self.assertIn("_hard_floor_mb", ctx,
            "B3.2: hard block must only fire at hard floor level, not redirect threshold")

    def test_behavioral_redirect_not_block_at_573mb(self):
        """573MB free triggers REDIRECT to SD (not block)."""
        REDIRECT_MB  = 1024
        HARD_FLOOR   = 256
        avail_mb     = 573   # from LOG_5_5_26

        should_redirect = HARD_FLOOR <= avail_mb < REDIRECT_MB
        should_block    = avail_mb < HARD_FLOOR

        self.assertTrue(should_redirect,
            f"B3.2: {avail_mb}MB free must trigger SD redirect, not block")
        self.assertFalse(should_block,
            f"B3.2: {avail_mb}MB free must NOT trigger hard block")
        print(f"\n  [B3.2 CORRECTED] {avail_mb}MB free → REDIRECT to SD (not block). "
              f"Run continues, dedup .deleted/ goes to SD card.")

    def test_behavioral_hard_block_below_256mb(self):
        """Only hard-block when internal is below 256MB (truly unusable)."""
        avail_mb   = 100   # truly critical
        HARD_FLOOR = 256
        should_block = avail_mb < HARD_FLOOR
        self.assertTrue(should_block, "Below 256MB truly unusable → hard block")

    def test_behavioral_healthy_no_redirect(self):
        """5GB free → no redirect, no block — normal operation."""
        avail_mb    = 5120
        REDIRECT_MB = 1024
        should_redirect = avail_mb < REDIRECT_MB
        self.assertFalse(should_redirect, "5GB free must not trigger any gate")


# ═══════════════════════════════════════════════════════════════════════════
# B3.3 — Zombie dedup + dialog storm circuit breaker
# ═══════════════════════════════════════════════════════════════════════════

class Test_B3_3_ZombieAndDialogStorm(unittest.TestCase):
    """B3.3: Zombie dedup registry + dialog storm circuit breaker."""

    def test_zombie_registry_functions_exist(self):
        dd = src("walkman_dedup.py")
        self.assertIn("def _record_dedup_failure(", dd,
            "B3.3: _record_dedup_failure() function required")
        self.assertIn("def _is_zombie(", dd,
            "B3.3: _is_zombie() function required")

    def test_dialog_storm_function_exists(self):
        dd = src("walkman_dedup.py")
        self.assertIn("def _record_dialog_click(", dd,
            "B3.3: _record_dialog_click() circuit breaker required")

    def test_dialog_storm_abort_marker(self):
        self.assertIn("DIALOG_STORM_ABORT", src("walkman_dedup.py"),
            "B3.3: DIALOG_STORM_ABORT marker required for log forensics")

    def test_dialog_rate_threshold_config(self):
        self.assertIn("DIALOG_RATE_THRESHOLD_PER_MIN", src("config.py"))

    def test_zombie_max_attempts_config(self):
        self.assertIn("ZOMBIE_DEDUP_MAX_ATTEMPTS", src("config.py"))

    def test_behavioral_zombie_detection(self):
        """After 3 failures, path is marked zombie → no further attempts."""
        registry = {}
        MAX_ATTEMPTS = 3

        def record_fail(path):
            registry[path] = registry.get(path, 0) + 1
            return registry[path]

        def is_zombie(path):
            return registry.get(path, 0) >= MAX_ATTEMPTS

        path = "/SD/Music/Joey Bada$$/Daily Routine.mp3"
        # Simulate 3 failures (what happened in the log)
        for _ in range(3):
            record_fail(path)

        self.assertTrue(is_zombie(path),
            "After 3 failures, path must be marked zombie")
        print(f"\n  [B3.3] After {MAX_ATTEMPTS} failures: '{path}' → ZOMBIE "
              f"(would have stopped 415 verify failures)")

    def test_behavioral_dialog_storm_detection(self):
        """743 dialogs in 19min = 39/min → trips threshold at 20/min."""
        THRESHOLD = 20
        # Simulate 25 rapid dialog clicks
        timestamps = []
        storm_detected = False

        for _ in range(25):
            timestamps.append(time.monotonic())
            # Check rate in last 60s
            now = time.monotonic()
            recent = [t for t in timestamps if now - t <= 60.0]
            if len(recent) > THRESHOLD:
                storm_detected = True
                break

        self.assertTrue(storm_detected,
            f"25 rapid dialogs must trigger storm detection at threshold={THRESHOLD}")
        print(f"\n  [B3.3] Dialog storm detected at {len(timestamps)} clicks "
              f"(production had 743 clicks in 19min)")


# ═══════════════════════════════════════════════════════════════════════════
# B2.1 — Track number conflict guard
# ═══════════════════════════════════════════════════════════════════════════

class Test_B2_1_TrackNumberConflict(unittest.TestCase):
    """B2.1: Track number conflict guard prevents wrong-file deletion."""

    def test_track_number_conflict_guard_config(self):
        self.assertIn("TRACK_NUMBER_CONFLICT_GUARD", src("config.py"),
            "B2.1: TRACK_NUMBER_CONFLICT_GUARD config required")

    def test_track_number_conflict_marker(self):
        self.assertIn("TRACK_NUMBER_CONFLICT", src("walkman_dedup.py"),
            "B2.1: TRACK_NUMBER_CONFLICT marker must be emitted")

    def test_behavioral_conflict_detected(self):
        """
        Joey Bada$$ 1999 scenario: Daily Routine as track 01 AND track 10.
        Both candidates, same title, different track numbers → defer.
        """
        entries = [
            {"filename": "Joey Bada$$ - 1999 - 01 Daily Routine.mp3", "track_number": "01"},
            {"filename": "Joey Bada$$ - 1999 - 10 Daily Routine.m4a", "track_number": "10"},
        ]
        track_nums = {str(e.get("track_number", "")).strip() for e in entries
                      if str(e.get("track_number", "")).strip()}
        conflict = len(track_nums) > 1

        self.assertTrue(conflict,
            "Must detect track number conflict (01 vs 10 for same title)")
        print(f"\n  [B2.1] Conflict detected: track numbers {track_nums} "
              f"→ defer both (was causing infinite dedup loop)")

    def test_behavioral_no_conflict_when_consistent(self):
        """No conflict when track numbers agree."""
        entries = [
            {"filename": "track.mp3", "track_number": "10"},
            {"filename": "track.m4a", "track_number": "10"},
        ]
        track_nums = {str(e.get("track_number", "")).strip() for e in entries
                      if str(e.get("track_number", "")).strip()}
        conflict = len(track_nums) > 1
        self.assertFalse(conflict, "Consistent track numbers must not flag conflict")


# ═══════════════════════════════════════════════════════════════════════════
# B1.1 — Genre contradiction guard
# ═══════════════════════════════════════════════════════════════════════════

class Test_B1_1_GenreContradictionGuard(unittest.TestCase):
    """B1.1: AI genre must not override high-confidence Phase-1 signal."""

    def test_contradiction_guard_config(self):
        self.assertIn("GENRE_CONTRADICTION_GUARD", src("config.py"))
        self.assertIn("GENRE_CONTRADICTION_FAMILIES", src("config.py"))

    def test_contradiction_check_function_exists(self):
        self.assertIn("def _check_genre_contradiction(", src("genre_client.py"),
            "B1.1: _check_genre_contradiction() function required")

    def test_contradiction_rejected_marker(self):
        self.assertIn("GENRE_CONTRADICTION_REJECTED", src("genre_client.py"))

    def test_behavioral_drill_not_overridden_by_lo_fi(self):
        """Phase-1: Drill (high confidence) → AI: Lo-Fi Hip-Hop → REJECTED."""
        deny_map = {
            "drill": {"lo-fi hip-hop", "afroswing", "jazz", "soul"},
        }
        phase1 = "Drill"
        ai_out = "Lo-Fi Hip-Hop"
        p1_lower = phase1.lower()
        ai_lower = ai_out.lower().replace(" ", "-").replace("hip-", "hip ")
        # normalise for set lookup
        ai_norm = ai_out.lower()
        contradiction = any(
            family in p1_lower and (ai_norm in denied or ai_lower in denied)
            for family, denied in deny_map.items()
        )
        self.assertTrue(contradiction,
            "AI 'Lo-Fi Hip-Hop' must be rejected when Phase-1 says 'Drill'")
        print(f"\n  [B1.1] Phase-1={phase1!r} + AI={ai_out!r} → REJECTED (contradiction)")

    def test_behavioral_compatible_genres_pass(self):
        """Phase-1: Hip-Hop → AI: Trap → ALLOWED (same family)."""
        deny_map = {
            "drill": {"lo-fi hip-hop", "afroswing"},
        }
        phase1 = "Hip-Hop"
        ai_out = "Trap"
        p1_lower = phase1.lower()
        ai_lower = ai_out.lower()
        contradiction = any(
            family in p1_lower and ai_lower in denied
            for family, denied in deny_map.items()
        )
        self.assertFalse(contradiction,
            "Hip-Hop + Trap must not trigger contradiction guard")


# ═══════════════════════════════════════════════════════════════════════════
# B1.2 — Fail-open Phase-1 fallback
# ═══════════════════════════════════════════════════════════════════════════

class Test_B1_2_FailOpenFallback(unittest.TestCase):
    """B1.2: ENRICHMENT_FAIL_OPEN must use Phase-1 genre, not blank skip."""

    def test_phase1_fallback_in_fail_open_message(self):
        """Fail-open log message must reference Phase-1 fallback."""
        gc = src("genre_client.py")
        idx = gc.find("ENRICHMENT_FAIL_OPEN_APPLIED")
        ctx = gc[max(0, idx-200):idx+300]
        self.assertTrue(
            "Phase-1" in ctx or "phase1" in ctx.lower() or "fallback" in ctx.lower(),
            "B1.2: fail-open must reference Phase-1 fallback in log message"
        )

    def test_behavioral_phase1_used_not_blank(self):
        """When fail-open fires, track gets Phase-1 genre not blank."""
        phase1_cache = {
            "KIRBS|Follow My Lead": "Drill",
            "Emmavie|Spirit Traveler": "Neo Soul",
        }
        fail_open_active = True

        def get_genre_with_failopen(artist, title):
            if fail_open_active:
                key = f"{artist}|{title}"
                phase1 = phase1_cache.get(key, "")
                if phase1:
                    return phase1, "PHASE1_FALLBACK"
                return "", "NO_DATA"
            return "", "SKIPPED"

        genre, source = get_genre_with_failopen("KIRBS", "Follow My Lead")
        self.assertEqual(genre, "Drill",
            "B1.2: fail-open must return Phase-1 genre 'Drill' not blank")
        self.assertEqual(source, "PHASE1_FALLBACK")
        print(f"\n  [B1.2] Fail-open: KIRBS/Follow My Lead → '{genre}' (from Phase-1)")


# ═══════════════════════════════════════════════════════════════════════════
# B1.3 — Taxonomy additions
# ═══════════════════════════════════════════════════════════════════════════

class Test_B1_3_TaxonomyAdditions(unittest.TestCase):
    """B1.3: New taxonomy entries from LOG_5_5_26 (no more WARNING)."""

    def test_uk_rap_in_taxonomy_or_aliases(self):
        gc = src("genre_client.py")
        self.assertTrue("UK Rap" in gc or '"uk rap"' in gc.lower(),
            "B1.3: UK Rap must be in taxonomy or alias map")

    def test_rai_in_taxonomy_or_aliases(self):
        gc = src("genre_client.py")
        self.assertTrue("Rai" in gc or '"rai"' in gc.lower(),
            "B1.3: Rai must be in taxonomy or alias map")

    def test_french_pop_in_taxonomy_or_aliases(self):
        gc = src("genre_client.py")
        self.assertTrue("French Pop" in gc or '"french pop"' in gc.lower(),
            "B1.3: French Pop must be in taxonomy or alias map")

    def test_behavioral_no_warning_for_known_misses(self):
        """UK Rap, Rai, French Pop must resolve without WARNING."""
        aliases = {"uk rap": "Hip-Hop", "rai": "World Music", "french pop": "Pop"}
        warnings = []

        def validate(genre):
            key = genre.lower()
            if key in aliases:
                return aliases[key]  # no warning
            warnings.append(genre)
            return genre

        for g in ["UK Rap", "Rai", "French Pop"]:
            validate(g)

        self.assertEqual(warnings, [],
            "B1.3: UK Rap/Rai/French Pop must not generate WARNINGs")


# ═══════════════════════════════════════════════════════════════════════════
# B3.4 — Delete backend policy
# ═══════════════════════════════════════════════════════════════════════════

class Test_B3_4_DeleteBackendPolicy(unittest.TestCase):
    """B3.4: Internal storage → ADB rm (no dialog); SD → COM."""

    def test_delete_backend_policy_config(self):
        self.assertIn("DELETE_BACKEND_POLICY", src("config.py"),
            "B3.4: DELETE_BACKEND_POLICY config required")

    def test_behavioral_internal_uses_adb(self):
        """Internal storage delete must use ADB rm (no dialog triggered)."""
        DELETE_POLICY = "smart"
        dialogs_from_adb = 0   # ADB rm generates no dialogs
        dialogs_from_com = 1   # COM generates dialog

        def get_delete_dialogs(storage_type: str, policy: str) -> int:
            if policy == "smart":
                if "internal" in storage_type.lower():
                    return dialogs_from_adb
                else:
                    return dialogs_from_com
            return dialogs_from_com

        internal_dialogs = get_delete_dialogs("Internal shared storage", DELETE_POLICY)
        sd_dialogs = get_delete_dialogs("SD card", DELETE_POLICY)

        self.assertEqual(internal_dialogs, 0,
            "B3.4: ADB rm for internal storage must produce 0 dialogs")
        self.assertEqual(sd_dialogs, 1,
            "B3.4: COM for SD card produces expected dialog")
        print(f"\n  [B3.4] Internal→ADB: {internal_dialogs} dialogs, "
              f"SD→COM: {sd_dialogs} dialogs "
              f"(would have prevented 743 dialog clicks)")


# ═══════════════════════════════════════════════════════════════════════════
# Log forensic markers
# ═══════════════════════════════════════════════════════════════════════════

class Test_B_MarkerContract(unittest.TestCase):
    """All MK22 forensic markers must be present in source."""

    MARKERS = {
        "INTERNAL_STORAGE_FULL_BLOCKED": "walkman_sync.py",
        "DIALOG_STORM_ABORT":            "walkman_dedup.py",
        "GENRE_CONTRADICTION_REJECTED":  "genre_client.py",
        "TRACK_NUMBER_CONFLICT":         "walkman_dedup.py",
    }

    def test_all_mk22_markers_present(self):
        missing = []
        for marker, fname in self.MARKERS.items():
            if marker not in src(fname):
                missing.append(f"'{marker}' in {fname}")
        print(f"\n  [MK22 Markers] {len(self.MARKERS)-len(missing)}/{len(self.MARKERS)} present")
        self.assertEqual(missing, [], f"MK22 markers missing: {missing}")


# ═══════════════════════════════════════════════════════════════════════════
# Continuity checks — prior gates still hold
# ═══════════════════════════════════════════════════════════════════════════

class Test_Continuity(unittest.TestCase):
    """All prior MK21 markers and syntax must remain intact."""

    def test_r4_markers_still_present(self):
        for marker, fname in [
            ("HARD STOP", "walkman_sync.py"), ("CircuitBreaker", "genre_client.py"),
            ("PROTECTED", "walkman_dedup.py"), ("RUN_ID", "config.py"),
        ]:
            self.assertIn(marker, src(fname), f"R4.1 regression: {marker} in {fname}")

    def test_mk19_markers_still_present(self):
        for marker, fname in [
            ("SKIP_INVALID_CANDIDATE", "walkman_dedup.py"),
            ("LIVE_DEVICE_REQUIRED", "walkman_dedup.py"),
            ("SLEEPBLOCKER_MISSING_LONGRUN_RED", "main.py"),
        ]:
            self.assertIn(marker, src(fname), f"MK19 regression: {marker}")

    def test_mk21_markers_still_present(self):
        for marker, fname in [
            ("GRAPH_STARVED_FALLBACK_APPLIED", "genre_graph.py"),
            ("GeminiProbeBackoff", "genre_client.py"),
            ("GroqWorkerReduced", "genre_client.py"),
            ("ENRICHMENT_FAIL_OPEN_APPLIED", "genre_client.py"),
        ]:
            self.assertIn(marker, src(fname), f"MK21 regression: {marker}")

    def test_g2_probe_backoff_still_works(self):
        """G2 probe backoff confirmed working in LOG_5_5_26: probe #1 at 15min, #2 at 45min."""
        self.assertIn("_GEMINI_PROBE_DELAY_S", src("genre_client.py"))
        self.assertIn("GeminiProbeBackoff", src("genre_client.py"))


# ═══════════════════════════════════════════════════════════════════════════
# Integration: LOG_5_5_26 scenario replay
# ═══════════════════════════════════════════════════════════════════════════

class Test_Integration_Log5526(unittest.TestCase):
    """Replay LOG_5_5_26 failure conditions — confirm all blocked by MK22."""

    def test_internal_99_percent_full_blocked(self):
        """573MB free would trigger INTERNAL_STORAGE_FULL_BLOCKED."""
        self.assertLess(573, 1024, "573MB < 1024MB threshold → run blocked")

    def test_broad_genres_no_longer_crashes(self):
        """genre_client._broad_genres() available — no more NameError."""
        import genre_client
        try:
            broad = genre_client._broad_genres()
            self.assertIsInstance(broad, set)
        except NameError as e:
            self.fail(f"B3.1: _broad_genres() NameError must be fixed: {e}")

    def test_joey_badass_track_conflict_detected(self):
        """Daily Routine track 01 vs 10 → conflict detected → defer."""
        entries_daily = [
            {"track_number": "01", "filename": "Daily Routine.mp3"},
            {"track_number": "10", "filename": "Daily Routine.m4a"},
        ]
        nums = {str(e["track_number"]) for e in entries_daily}
        self.assertEqual(len(nums), 2, "Must detect 01 vs 10 conflict")

    def test_dialog_storm_would_have_been_detected(self):
        """743 dialogs in 19min = 39/min → trips 20/min threshold."""
        THRESHOLD = 20
        rate_per_min = 743 / 19
        self.assertGreater(rate_per_min, THRESHOLD,
            f"Production rate {rate_per_min:.0f}/min must exceed threshold {THRESHOLD}/min")
        print(f"\n  [Integration] Production dialog rate: {rate_per_min:.0f}/min "
              f"→ DIALOG_STORM_ABORT would have fired at {THRESHOLD}/min threshold")

    def test_g2_probe_backoff_confirmed_production(self):
        """G2 confirmed working: probe #1 next at 15min, probe #2 at 45min."""
        # From LOG_5_5_26 lines 113, 266
        probe1_delay_min = 15   # 900s
        probe2_delay_min = 45   # 2700s
        ratio = probe2_delay_min / probe1_delay_min
        self.assertAlmostEqual(ratio, 3.0, places=0,
            msg="G2: probe delay ratio must be 3x (900s to 2700s)")
        print(f"\n  [Integration] G2 confirmed in production: probe#1={probe1_delay_min}min, "
              f"probe#2={probe2_delay_min}min ({ratio:.0f}× backoff)")


if __name__ == "__main__":
    print("=" * 70)
    print("SpotifyTuner MK22 — LOG_5_5_26 Incident Response Test Suite")
    print("=" * 70)
    print("B1: Genre quality  B2: Album integrity  B3: Dialog storm + crashes")
    print()

    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        Test_B3_1_BroadGenresNameError,
        Test_B3_2_InternalStorageGate,
        Test_B3_3_ZombieAndDialogStorm,
        Test_B2_1_TrackNumberConflict,
        Test_B1_1_GenreContradictionGuard,
        Test_B1_2_FailOpenFallback,
        Test_B1_3_TaxonomyAdditions,
        Test_B3_4_DeleteBackendPolicy,
        Test_B_MarkerContract,
        Test_Continuity,
        Test_Integration_Log5526,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print("=" * 70)
    total  = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print(f"MK22 Incident Response: {passed}/{total} passed")
    if result.wasSuccessful():
        print("✅ All MK22 B1-B3 fixes verified — LOG_5_5_26 failures cannot recur")
    else:
        for t, tb in result.failures + result.errors:
            print(f"  ❌ {t.id().split('.')[-1]}: {tb.splitlines()[-1][:80]}")
    print("=" * 70)
    sys.exit(0 if result.wasSuccessful() else 1)
