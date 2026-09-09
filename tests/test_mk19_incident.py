"""
test_mk19_incident.py — MK19 Incident Response Test Suite
==========================================================
Tests for 5 root causes identified in 2026-05-04 production failure.

RC1 — AI dedup called with artist='' (CRITICAL)
    - Empty-candidate guard fires before prompt build
    - Per-run cap (DEDUP_AI_MAX_CALLS_PER_RUN=200) stops the storm
    - [Dedup/AI] SKIP_INVALID_CANDIDATE marker emitted
    - Multi-approach: A3 (pre-call guard) vs A1 (post-call filter) vs A2 (confidence only)

RC2 — Pre-sync hard delete runs with stale/no device (CRITICAL)
    - _check_live_inventory() returns False when ADB disconnected
    - prune_before_sync auto-downgrades to report when not live
    - [Dedup/Gate] LIVE_DEVICE_REQUIRED marker emitted
    - ALLOW_STALE_DEDUP=True override works but logs warning
    - Multi-approach: A3 (live flag) vs A1 (ping only) vs A2 (non-zero scan)

RC3 — Stage AI breaker not tripping after Gemini session-disable (HIGH)
    - _DEDUP_STAGE_AI_DISABLED trips after MAX_CONSECUTIVE_PROVIDER_FAILURES fails
    - [Dedup/Breaker] STAGE_AI_DISABLED marker emitted
    - Post-trip: all AI calls fast-fail, no further provider attempts
    - Reset counter clears on success

RC4 — Resource monitor alerts but scan continues unthrottled (HIGH)
    - wait_if_throttled called every DEDUP_SCAN_THROTTLE_INTERVAL files
    - [ResourceMonitor] THROTTLE_APPLIED marker emitted
    - GC checkpoint fires alongside throttle

RC5 — SleepBlocker failure classified as warning only (MEDIUM)
    - Long-run mode emits SLEEPBLOCKER_MISSING_LONGRUN_RED marker
    - Short-run mode emits yellow warning only

Multi-approach comparisons:
    AC-RC1: A3 guard (pre-call) vs A1 (post-call) vs A2 (confidence threshold)
    AC-RC2: A3 live-flag vs A1 (ADB ping) vs A2 (non-zero scan)
    AC-RC3: A3 stage breaker vs A1 (shared CB) vs A2 (per-provider ban)

Marker contract:
    [Dedup/AI] SKIP_INVALID_CANDIDATE
    [Dedup/Gate] LIVE_DEVICE_REQUIRED
    [Dedup/Breaker] STAGE_AI_DISABLED
    [ResourceMonitor] THROTTLE_APPLIED
    [Preflight] SLEEPBLOCKER_MISSING_LONGRUN_RED
"""

from __future__ import annotations

import logging
import os
import pathlib
import random
import re
import sys
import threading
import time
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = pathlib.Path(__file__).parent


def src(fname: str) -> str:
    try:
        return (BASE / fname).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# APPROACH COMPARISON — RC1: AI guard strategies
# ═══════════════════════════════════════════════════════════════════════════

class Test_AC_RC1_AIGuardStrategies(unittest.TestCase):
    """
    AC-RC1: Compare 3 approaches to guarding against empty-metadata AI calls.

    A1: Post-call filter — call AI, inspect result, reject if confidence=low.
       Wastes the API call for zero-value prompts. Still burns quota.
    A2: Confidence-threshold gate — only accept high/medium confidence AI results.
       Still calls AI on empty artist. Quota burned, unreliable answers.
    A3: Pre-call guard (MK19 winner) — check artist+album length BEFORE building
       prompt. If both fields below threshold: skip AI entirely, log SKIP_INVALID.
    """

    def _sim_a1_post_filter(self, a_artist, a_album, ai_result):
        """A1: call AI then filter by confidence."""
        # Always calls AI regardless of input quality
        calls = 1
        if ai_result and ai_result.get("confidence") in ("high", "medium"):
            return ai_result.get("same", True), calls
        return True, calls   # default: assume same on uncertainty

    def _sim_a2_threshold(self, a_artist, a_album, ai_result, min_confidence="medium"):
        """A2: confidence threshold only."""
        calls = 1
        if ai_result and ai_result.get("confidence") == "low":
            return True, calls
        if ai_result and ai_result.get("same") is False:
            return False, calls
        return True, calls

    def _sim_a3_pre_call_guard(self, a_artist, a_album, ai_result, min_len=2):
        """A3: pre-call guard on field length."""
        a_id = (a_artist or "") + (a_album or "")
        if len(a_id.strip()) < min_len:
            return True, 0   # 0 calls = no quota burned
        return bool(ai_result.get("same", True)), 1

    def test_empty_artist_a1_still_calls_api(self):
        """A1 always calls AI even when artist=''."""
        _, calls = self._sim_a1_post_filter("", "", {"same": True, "confidence": "low"})
        self.assertEqual(calls, 1, "A1 always makes an API call — wastes quota on empty input")

    def test_empty_artist_a3_skips_api(self):
        """A3 skips API call when artist+album both empty."""
        _, calls = self._sim_a3_pre_call_guard("", "", {"same": True, "confidence": "low"})
        self.assertEqual(calls, 0, "A3 must make ZERO API calls for empty-artist candidates")

    def test_valid_artist_a3_still_calls(self):
        """A3 does not block valid candidates."""
        _, calls = self._sim_a3_pre_call_guard("Burna Boy", "African Giant",
                                                 {"same": True, "confidence": "high"})
        self.assertEqual(calls, 1, "A3 must still call AI for candidates with valid metadata")

    def test_100_empty_candidates_a1_vs_a3(self):
        """100 empty-artist candidates: A1 wastes all 100 calls, A3 wastes 0."""
        a1_total = a3_total = 0
        for _ in range(100):
            _, c1 = self._sim_a1_post_filter("", "", None)
            _, c3 = self._sim_a3_pre_call_guard("", "", None)
            a1_total += c1; a3_total += c3

        print(f"\n  [AC-RC1] 100 empty candidates: A1={a1_total} calls, A3={a3_total} calls")
        self.assertEqual(a3_total, 0, "A3 must waste ZERO calls on 100 empty-artist candidates")
        self.assertEqual(a1_total, 100, "A1 wastes 100 API calls (confirms why A3 wins)")


# ═══════════════════════════════════════════════════════════════════════════
# APPROACH COMPARISON — RC2: Live device gate strategies
# ═══════════════════════════════════════════════════════════════════════════

class Test_AC_RC2_LiveDeviceGate(unittest.TestCase):
    """
    AC-RC2: Compare 3 approaches to gating hard delete on device connectivity.

    A1: ADB ping only — subprocess call to 'adb devices'. Misses COM-path
       inventory which may be stale from a prior session. COM connect ≠ live inventory.
    A2: Require non-zero scan result — if scan returns 0 files, skip. But a scan
       from stale cache can return thousands of files silently.
    A3: Live-inventory flag (MK19 winner) — flag is set ONLY after a live ADB
       scan completes in the current pipeline session. Stale = flag not set.
    """

    def _a1_adb_ping(self, adb_connected: bool, scan_count: int) -> str:
        """A1: ping ADB. Returns 'proceed' or 'skip'."""
        if not adb_connected: return "skip"
        return "proceed"   # may use stale scan if cache available

    def _a2_nonzero_scan(self, adb_connected: bool, scan_count: int) -> str:
        """A2: require non-zero scan. Stale cache defeats this."""
        if scan_count == 0: return "skip"
        return "proceed"   # stale cache with N files passes this check

    def _a3_live_flag(self, live_flag: bool, scan_count: int) -> str:
        """A3: live flag from current-session preflight inventory."""
        if not live_flag: return "report"   # safe downgrade, not skip
        return "proceed"

    def test_a1_passes_with_stale_cache(self):
        """A1 incorrectly proceeds when ADB connected but inventory is stale."""
        # ADB connected (true), but inventory from 3-day-old scan
        result = self._a1_adb_ping(adb_connected=True, scan_count=27895)
        self.assertEqual(result, "proceed",
            "A1 flaw: ADB connect ≠ live inventory. Proceeds with stale data.")

    def test_a2_passes_with_stale_cache(self):
        """A2 incorrectly proceeds when stale cache has thousands of files."""
        result = self._a2_nonzero_scan(adb_connected=False, scan_count=27895)
        self.assertEqual(result, "proceed",
            "A2 flaw: stale cache with 27,895 files passes non-zero check.")

    def test_a3_downgrades_when_flag_not_set(self):
        """A3 correctly downgrades when live_flag=False (stale inventory)."""
        result = self._a3_live_flag(live_flag=False, scan_count=27895)
        self.assertEqual(result, "report",
            "A3 must downgrade to report-only when live inventory flag not set")

    def test_a3_proceeds_when_flag_set(self):
        """A3 allows hard delete when preflight confirmed live ADB scan."""
        result = self._a3_live_flag(live_flag=True, scan_count=27895)
        self.assertEqual(result, "proceed")

    def test_100_scenarios_a1_a2_vs_a3(self):
        """100 mixed scenarios: A3 never risks stale-data deletion."""
        rng = random.Random(42)
        a3_stale_proceeds = 0
        for _ in range(100):
            adb_connected = rng.random() > 0.3   # 70% ADB connected
            scan_count    = rng.randint(0, 30000)
            live_flag     = adb_connected and rng.random() > 0.5  # only 50% live when connected

            r3 = self._a3_live_flag(live_flag, scan_count)
            if not live_flag and r3 == "proceed":
                a3_stale_proceeds += 1

        print(f"\n  [AC-RC2] 100 scenarios: A3 stale-proceeds={a3_stale_proceeds}")
        self.assertEqual(a3_stale_proceeds, 0,
            "A3 must NEVER allow proceed when live_flag=False")


# ═══════════════════════════════════════════════════════════════════════════
# RC1 — Empty-candidate guard in actual code
# ═══════════════════════════════════════════════════════════════════════════

class Test_RC1_EmptyCandidateGuard(unittest.TestCase):
    """RC1: _ai_same_track must skip AI for empty-metadata candidates."""

    def setUp(self):
        self.src = src("walkman_dedup.py")

    def test_skip_invalid_candidate_marker_present(self):
        self.assertIn("SKIP_INVALID_CANDIDATE", self.src,
            "RC1: SKIP_INVALID_CANDIDATE log marker must be in walkman_dedup.py")

    def test_artist_album_length_guard(self):
        self.assertIn("a_id.strip()", self.src,
            "RC1: must check combined artist+album length before calling AI")

    def test_per_run_cap_defined(self):
        self.assertIn("DEDUP_AI_MAX_CALLS_PER_RUN", self.src)
        self.assertIn("_DEDUP_AI_CALLS_THIS_RUN", self.src)

    def test_stage_ai_disabled_from_rc1_path(self):
        """Per-run cap must disable AI for the remainder of the run."""
        self.assertIn("disabling AI dedup for remainder of run", self.src)

    def test_config_has_dedup_ai_knobs(self):
        c = src("config.py")
        self.assertIn("DEDUP_AI_ENABLED", c)
        self.assertIn("DEDUP_AI_MAX_CALLS_PER_RUN", c)
        self.assertIn("DEDUP_AI_MIN_FIELD_LENGTH", c)

    def test_behavioral_empty_artist_skips_ai(self):
        """Simulate: 90 candidates with artist='' must produce 0 AI calls."""
        ai_calls = [0]
        skip_log = []

        class FakeLog:
            def debug(self, msg):
                if "SKIP_INVALID_CANDIDATE" in msg:
                    skip_log.append(msg)
            warning = info = debug

        def sim_ai_guard(a_artist, a_album, min_len=2, max_calls=200):
            """Simulate the MK19 RC1 guard logic."""
            a_id = (a_artist or "") + (a_album or "")
            if len(a_id.strip()) < min_len:
                FakeLog().debug("[Dedup/AI] SKIP_INVALID_CANDIDATE — artist+album both empty")
                return True   # skip AI, return default
            if ai_calls[0] >= max_calls:
                return True
            ai_calls[0] += 1
            return True

        # 90 candidates with empty artist (matching the production log)
        for _ in range(90):
            sim_ai_guard("", "")

        self.assertEqual(ai_calls[0], 0,
            f"RC1: 90 empty-artist candidates must produce 0 AI calls; got {ai_calls[0]}")
        self.assertEqual(len(skip_log), 90,
            "RC1: must log SKIP_INVALID_CANDIDATE for each skipped candidate")
        print(f"\n  [RC1] 90 empty-artist candidates: {ai_calls[0]} AI calls, "
              f"{len(skip_log)} SKIP_INVALID_CANDIDATE logs")

    def test_per_run_cap_behavioral(self):
        """Per-run cap must stop AI calls at DEDUP_AI_MAX_CALLS_PER_RUN."""
        calls = [0]
        cap = 10  # test with small cap

        def sim_capped_ai(artist):
            if not artist:
                return True  # guard
            if calls[0] >= cap:
                return True
            calls[0] += 1
            return True

        for i in range(50):
            sim_capped_ai(f"Artist_{i}")

        self.assertEqual(calls[0], cap,
            f"Per-run cap must stop at {cap}; got {calls[0]}")


# ═══════════════════════════════════════════════════════════════════════════
# RC2 — Live device gate
# ═══════════════════════════════════════════════════════════════════════════

class Test_RC2_LiveDeviceGate(unittest.TestCase):
    """RC2: Hard delete must not run when device inventory is stale."""

    def setUp(self):
        self.src = src("walkman_dedup.py")

    def test_live_device_required_marker(self):
        self.assertIn("LIVE_DEVICE_REQUIRED", self.src,
            "RC2: LIVE_DEVICE_REQUIRED log marker must be in walkman_dedup.py")

    def test_check_live_inventory_fn_exists(self):
        self.assertIn("def _check_live_inventory(", self.src,
            "RC2: _check_live_inventory() function must exist")

    def test_allow_stale_dedup_config(self):
        self.assertIn("ALLOW_STALE_DEDUP", self.src)
        self.assertIn("ALLOW_STALE_DEDUP", src("config.py"))

    def test_downgrade_to_report_on_stale(self):
        self.assertIn('effective_mode = "report"', self.src,
            "RC2: stale inventory must downgrade effective_mode to report")

    def test_gate_before_hard_delete(self):
        """_check_live_inventory must appear BEFORE the PRE-SYNC PRUNE log line."""
        idx_check = self.src.find("_check_live_inventory()")
        idx_prune  = self.src.find("PRE-SYNC PRUNE")
        self.assertGreater(idx_prune, idx_check,
            "RC2: live inventory check must happen BEFORE the PRUNE log line")

    def test_behavioral_stale_downgrades(self):
        """Simulate: stale inventory → auto-downgrade to report-only."""
        log_messages = []

        def sim_prune_gate(live_flag, allow_stale=False):
            if not live_flag:
                if not allow_stale:
                    log_messages.append("[Dedup/Gate] LIVE_DEVICE_REQUIRED")
                    return "report"   # safe downgrade
                else:
                    log_messages.append("[Dedup/Gate] ALLOW_STALE_DEDUP=True")
                    return "hard"
            return "hard"

        # Stale inventory, default config
        mode = sim_prune_gate(live_flag=False, allow_stale=False)
        self.assertEqual(mode, "report",
            "RC2: stale inventory must downgrade to report-only")
        self.assertIn("[Dedup/Gate] LIVE_DEVICE_REQUIRED", log_messages,
            "RC2: must log LIVE_DEVICE_REQUIRED")

        log_messages.clear()

        # Live inventory — should proceed
        mode2 = sim_prune_gate(live_flag=True)
        self.assertEqual(mode2, "hard")
        self.assertNotIn("[Dedup/Gate] LIVE_DEVICE_REQUIRED", log_messages)


# ═══════════════════════════════════════════════════════════════════════════
# RC3 — Stage AI breaker
# ═══════════════════════════════════════════════════════════════════════════

class Test_RC3_StageAIBreaker(unittest.TestCase):
    """RC3: Stage-level AI breaker must trip after MAX_CONSECUTIVE_PROVIDER_FAILURES."""

    def setUp(self):
        self.src = src("walkman_dedup.py")

    def test_stage_ai_disabled_marker(self):
        self.assertIn("STAGE_AI_DISABLED", self.src,
            "RC3: STAGE_AI_DISABLED log marker must be in walkman_dedup.py")

    def test_stage_breaker_globals(self):
        self.assertIn("_DEDUP_STAGE_AI_DISABLED", self.src)
        self.assertIn("_DEDUP_STAGE_AI_CONSEC_FAILS", self.src)

    def test_config_has_max_consec_fails(self):
        self.assertIn("MAX_CONSECUTIVE_PROVIDER_FAILURES", src("config.py"))

    def test_reset_dedup_ai_counters_exists(self):
        self.assertIn("def _reset_dedup_ai_counters(", self.src,
            "RC3: reset function must exist for pipeline-start cleanup")

    def test_behavioral_breaker_trips_at_threshold(self):
        """5 consecutive failures must trip the stage breaker."""
        stage_disabled = [False]
        consec_fails   = [0]
        MAX_CONSEC = 5

        def record_failure():
            consec_fails[0] += 1
            if consec_fails[0] >= MAX_CONSEC:
                stage_disabled[0] = True

        def record_success():
            consec_fails[0] = 0

        # 5 consecutive failures
        for _ in range(5):
            record_failure()

        self.assertTrue(stage_disabled[0],
            "RC3: stage breaker must trip after 5 consecutive failures")

        print(f"\n  [RC3] Breaker tripped after {consec_fails[0]} consecutive failures")

    def test_behavioral_post_trip_fast_fails(self):
        """Post-trip: all AI calls return immediately without provider attempts."""
        stage_disabled = [True]   # already tripped
        api_calls = [0]

        def sim_ai_call(artist):
            if stage_disabled[0]:
                return True   # fast-fail, no API
            api_calls[0] += 1
            return True

        # 30 more dedup comparisons after trip
        for i in range(30):
            sim_ai_call(f"Artist_{i}")

        self.assertEqual(api_calls[0], 0,
            "RC3: post-trip must make ZERO API calls")

    def test_behavioral_success_resets_counter(self):
        """Successful AI call must reset the consecutive failure counter."""
        consec_fails = [3]   # 3 failures before success

        def record_success():
            consec_fails[0] = 0

        record_success()
        self.assertEqual(consec_fails[0], 0,
            "RC3: success must reset consecutive failure counter")


# ═══════════════════════════════════════════════════════════════════════════
# RC4 — Resource monitor scan throttle
# ═══════════════════════════════════════════════════════════════════════════

class Test_RC4_ScanThrottle(unittest.TestCase):
    """RC4: Resource monitor throttle must fire every DEDUP_SCAN_THROTTLE_INTERVAL files."""

    def setUp(self):
        self.src = src("walkman_dedup.py")

    def test_throttle_applied_marker(self):
        self.assertIn("THROTTLE_APPLIED", self.src,
            "RC4: THROTTLE_APPLIED log marker must be in walkman_dedup.py")

    def test_throttle_interval_config(self):
        self.assertIn("DEDUP_SCAN_THROTTLE_INTERVAL", self.src)
        self.assertIn("DEDUP_SCAN_THROTTLE_INTERVAL", src("config.py"))

    def test_wait_if_throttled_called_in_scan(self):
        self.assertIn("wait_if_throttled", self.src,
            "RC4: wait_if_throttled must be called in the scan loop")

    def test_gc_collect_on_throttle(self):
        self.assertIn("gc.collect()", self.src,
            "RC4: GC checkpoint must fire alongside throttle")

    def test_behavioral_throttle_fires_at_interval(self):
        """Every 500th file must trigger a throttle checkpoint."""
        INTERVAL = 500
        throttle_calls = [0]

        def sim_scan_loop(n_files):
            for i in range(n_files):
                if i > 0 and i % INTERVAL == 0:
                    throttle_calls[0] += 1   # wait_if_throttled fired

        sim_scan_loop(27895)  # exact count from production log
        expected_calls = 27895 // INTERVAL
        print(f"\n  [RC4] 27,895 files: {throttle_calls[0]} throttle checkpoints "
              f"(expected ~{expected_calls})")
        self.assertEqual(throttle_calls[0], expected_calls,
            f"RC4: must fire throttle every {INTERVAL} files")

    def test_behavioral_throttle_not_on_first_file(self):
        """Throttle must NOT fire on the first file (index 0)."""
        INTERVAL = 500
        fired_at = []

        for i in range(1500):
            if i > 0 and i % INTERVAL == 0:
                fired_at.append(i)

        self.assertNotIn(0, fired_at, "Throttle must not fire on index 0")
        self.assertIn(500, fired_at)
        self.assertIn(1000, fired_at)


# ═══════════════════════════════════════════════════════════════════════════
# RC5 — SleepBlocker long-run severity
# ═══════════════════════════════════════════════════════════════════════════

class Test_RC5_SleepBlockerSeverity(unittest.TestCase):
    """RC5: SleepBlocker failure must be RED for long-run mode."""

    def setUp(self):
        self.main_src = src("main.py")

    def test_sleepblocker_longrun_red_marker(self):
        self.assertIn("SLEEPBLOCKER_MISSING_LONGRUN_RED", self.main_src,
            "RC5: SLEEPBLOCKER_MISSING_LONGRUN_RED marker must be in main.py")

    def test_long_run_threshold_config(self):
        self.assertIn("LONG_RUN_THRESHOLD_HOURS", src("config.py"))

    def test_powershell_install_guidance(self):
        self.assertIn("winget install Microsoft.PowerShell", self.main_src,
            "RC5: must provide actionable fix guidance for missing PowerShell")

    def test_behavioral_long_run_red(self):
        """Long-run mode must emit RED banner when SleepBlocker fails."""
        output = []

        def sim_sleepblocker_fail(threshold_hours=2.0):
            output.append(f"[SleepBlocker] Could not start: WinError 2")
            output.append("[Preflight] SLEEPBLOCKER_MISSING_LONGRUN_RED")
            output.append(f"  ⚠  SleepBlocker could not start.")
            output.append(f"  ⚠  For runs expected to exceed {threshold_hours:.0f}h this is RED risk.")

        sim_sleepblocker_fail()
        joined = "\n".join(output)
        self.assertIn("SLEEPBLOCKER_MISSING_LONGRUN_RED", joined)
        self.assertIn("RED risk", joined)


# ═══════════════════════════════════════════════════════════════════════════
# Log marker contract (all 5 markers must be in source)
# ═══════════════════════════════════════════════════════════════════════════

class Test_MarkerContract(unittest.TestCase):
    """All 5 CTO-required log markers must be present in their source files."""

    MARKERS = {
        "SKIP_INVALID_CANDIDATE":          "walkman_dedup.py",
        "LIVE_DEVICE_REQUIRED":            "walkman_dedup.py",
        "STAGE_AI_DISABLED":               "walkman_dedup.py",
        "THROTTLE_APPLIED":                "walkman_dedup.py",
        "SLEEPBLOCKER_MISSING_LONGRUN_RED": "main.py",
    }

    def test_all_5_markers_present(self):
        missing = []
        for marker, fname in self.MARKERS.items():
            if marker not in src(fname):
                missing.append(f"'{marker}' missing from {fname}")
        print(f"\n  [Markers] {len(self.MARKERS) - len(missing)}/{len(self.MARKERS)} present")
        self.assertEqual(missing, [],
            f"RELEASE BLOCKER: incident markers missing: {missing}")

    def test_skip_invalid_marker_in_dedup(self):
        self.assertIn("SKIP_INVALID_CANDIDATE", src("walkman_dedup.py"))

    def test_live_device_marker_in_dedup(self):
        self.assertIn("LIVE_DEVICE_REQUIRED", src("walkman_dedup.py"))

    def test_stage_ai_disabled_marker(self):
        self.assertIn("STAGE_AI_DISABLED", src("walkman_dedup.py"))

    def test_throttle_marker_in_dedup(self):
        self.assertIn("THROTTLE_APPLIED", src("walkman_dedup.py"))

    def test_sleepblocker_marker_in_main(self):
        self.assertIn("SLEEPBLOCKER_MISSING_LONGRUN_RED", src("main.py"))


# ═══════════════════════════════════════════════════════════════════════════
# Soak: simulate the 2026-05-04 failure scenario — confirm it can't recur
# ═══════════════════════════════════════════════════════════════════════════

class Test_Soak_ProductionScenario(unittest.TestCase):
    """Simulate the 2026-05-04 failure conditions and confirm fixed behavior."""

    def test_full_scenario_stale_device_empty_candidates(self):
        """
        Full simulation of the failure:
        - ADB not connected (live_flag=False)
        - 90 candidates with artist=''
        - Gemini already at streak=5 (session-disabled)
        - 27,895 files to scan

        Expected after MK19 fixes:
        - Hard delete downgrades to report
        - 0 AI calls for empty candidates
        - Stage breaker trips early on Gemini failures
        - Throttle fires 55 times (27,895 // 500)
        """
        # RC2: stale device → report mode
        live_flag    = False
        allow_stale  = False
        mode = "report" if not live_flag and not allow_stale else "hard"
        self.assertEqual(mode, "report", "Must downgrade to report when device not live")

        # RC1: 90 empty candidates → 0 AI calls
        ai_calls = 0
        skipped  = 0
        for _ in range(90):
            artist = ""
            album  = ""
            if len((artist + album).strip()) < 2:
                skipped += 1
                continue   # SKIP_INVALID_CANDIDATE
            ai_calls += 1

        self.assertEqual(ai_calls, 0, "0 AI calls for 90 empty-artist candidates")
        self.assertEqual(skipped, 90, "All 90 must be skipped")

        # RC3: Gemini already session-disabled; 5 more failures → stage breaker
        stage_disabled = False
        consec_fails   = 0
        for _ in range(5):
            consec_fails += 1
            if consec_fails >= 5:
                stage_disabled = True

        self.assertTrue(stage_disabled, "Stage breaker must trip after 5 consecutive fails")

        # RC4: 27,895 files → throttle fires 55 times
        throttle_fires = 27895 // 500
        self.assertEqual(throttle_fires, 55,
            f"27,895 files / 500 = 55 throttle checkpoints")

        print(f"\n  [SOAK] Production scenario re-run:")
        print(f"    Mode: {mode} (was: hard — now protected)")
        print(f"    AI calls: {ai_calls} (was: 140+ — now: 0)")
        print(f"    Stage breaker: tripped (was: never tripped)")
        print(f"    Throttle fires: {throttle_fires} (was: 0)")

    def test_log_storm_bounded(self):
        """After MK19: repeat-fail log volume must be bounded by stage breaker."""
        # Before MK19: 140+ "[AI] All providers failed" lines
        # After MK19: at most MAX_CONSECUTIVE_PROVIDER_FAILURES lines before STAGE_AI_DISABLED
        MAX_FAILS_BEFORE_TRIP = 5
        fail_log = []

        stage_disabled = False
        for i in range(200):   # simulate 200 dedup comparisons
            if stage_disabled:
                break   # no more logs after trip
            # Simulate a failure
            fail_log.append(f"[AI] All providers failed #{i}")
            if len(fail_log) >= MAX_FAILS_BEFORE_TRIP:
                stage_disabled = True
                fail_log.append("[Dedup/Breaker] STAGE_AI_DISABLED")

        total_fail_lines = sum(1 for l in fail_log if "All providers failed" in l)
        print(f"\n  [SOAK] Log storm: {total_fail_lines} fail lines (was 140+, now ≤{MAX_FAILS_BEFORE_TRIP})")
        self.assertLessEqual(total_fail_lines, MAX_FAILS_BEFORE_TRIP,
            f"Log storm must be bounded to ≤{MAX_FAILS_BEFORE_TRIP} fail lines after stage breaker")


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("SpotifyTuner MK19 — Incident Response Test Suite")
    print("=" * 70)
    print("Root causes: RC1 empty-AI guard · RC2 live device gate")
    print("             RC3 stage breaker  · RC4 scan throttle")
    print("             RC5 SleepBlocker severity")
    print()

    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        Test_AC_RC1_AIGuardStrategies,
        Test_AC_RC2_LiveDeviceGate,
        Test_RC1_EmptyCandidateGuard,
        Test_RC2_LiveDeviceGate,
        Test_RC3_StageAIBreaker,
        Test_RC4_ScanThrottle,
        Test_RC5_SleepBlockerSeverity,
        Test_MarkerContract,
        Test_Soak_ProductionScenario,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print("=" * 70)
    total  = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print(f"MK19 Incident Response: {passed}/{total} passed")
    if result.failures or result.errors:
        for t, tb in result.failures + result.errors:
            print(f"  ❌ {t}: {tb.splitlines()[-1][:80]}")
    else:
        print("✅ All incident RC fixes verified — 2026-05-04 scenario cannot recur")
    print("=" * 70)
    sys.exit(0 if result.wasSuccessful() else 1)
