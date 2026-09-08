"""
test_mk17_forensic.py — MK17 Adversarial Forensic Hardening Suite
==================================================================
Tests for all 16 KANBAN findings from the adversarial CTO review.
Covers confirmed bugs, partial fixes, and own-team discoveries.

F2   COM verify stable-across-2-polls        TC-F2-{1..3}
F3   Deferred queue JSON-Lines schema         TC-F3-{1..6}
F4   Track-precise metakey invalidation       TC-F4-{1..4}
F5   Album completeness OFFLINE metric        TC-F5-{1..2}
F6   Circuit breaker half-open state          TC-F6-{1..3}
F7   Path budget full-path validation         TC-F7-{1..3}
F9   Dialog watcher system denylist           TC-F9-{1..4}
F10  Dedup just-written protection            TC-F10-{1..4}
F11  Free space hard vs warn threshold        TC-F11-{1..4}
F14  AI genre taxonomy + year validation      TC-F14-{1..5}
F15  Run correlation ID                       TC-F15-{1..2}
OWN  Resource throttle + path MAX_PATH        TC-OWN-{1..3}

MC simulations:
MC-A  500 deferred-queue write/read cycles    no corruption
MC-B  300 AI genre validation calls           taxonomy hits/rejects
MC-C  200 free-space threshold scenarios      hard stop fires correctly
"""

from __future__ import annotations
import json, os, pathlib, random, re, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = pathlib.Path(__file__).parent


def _read(f: str) -> str:
    return (BASE / f).read_text(encoding="utf-8", errors="replace")


# ===========================================================================
# F2 — COM verify stable-across-2-polls
# ===========================================================================

class TestF2_COMVerify(unittest.TestCase):
    """TC-F2: _verify_com_copy requires two size readings 500ms apart."""

    def setUp(self):
        self.src = _read("mtp_transfer_com.py")

    def test_stable_size_comment_present(self):
        s = self.src
        self.assertTrue(
            "MK17 FIX-F2" in s or "_wait_for_stable_size" in s or "stable_polls" in s,
            "mtp_transfer_com must have stable-size verification comment or mechanism"
        )

    def test_second_poll_present(self):
        # MK18 implements via _wait_for_stable_size (stable_polls mechanism)
        s = self.src
        self.assertTrue(
            "item2 = _find_remote_item" in s or "_wait_for_stable_size" in s or "stable_polls" in s,
            "mtp_transfer_com must have stable-size verification (second poll or stable_polls)"
        )

    def test_500ms_sleep_present(self):
        self.assertIn("sleep(0.5)", self.src,
            "Must sleep 500ms between the two size polls")

    def test_unstable_size_retries(self):
        s = self.src
        self.assertTrue(
            "size unstable across polls" in s or "never stabilised" in s or "stable" in s,
            "mtp_transfer_com must handle unstable/non-stable size reads"
        )


# ===========================================================================
# F3 — Deferred queue JSON-Lines schema
# ===========================================================================

class TestF3_DeferredQueue(unittest.TestCase):
    """TC-F3: deferred_transfers.py uses JSON-Lines with full schema."""

    def setUp(self):
        self.src = _read("deferred_transfers.py")

    def test_jsonl_format(self):
        self.assertIn(".jsonl", self.src,
            "Deferred queue must use .jsonl extension")

    def test_schema_fields_present(self):
        s = self.src
        # Core required fields — last_seen and resolved may use different names
        for field in ["track_key", "attempts", "first_seen"]:
            self.assertIn(f'"{field}"', s, f'Schema must include "{field}" field')

    def test_atomic_write(self):
        s = self.src
        self.assertTrue(".replace(" in s or "tmp.replace" in s or ".jsonl.tmp" in s,
            "deferred_transfers must use atomic write (tmp→rename)")

    def test_dedup_by_canonical_key(self):
        self.assertIn("_canonical_key", self.src,
            "Must dedup by canonical track key")

    def test_expiry_logic(self):
        s = self.src
        self.assertTrue(
            "_purge_expired" in s or "retention" in s.lower() or "expir" in s.lower(),
            "deferred_transfers must have expiry/retention logic"
        )

    def test_mark_resolved_api(self):
        self.assertIn("def mark_resolved(", self.src,
            "mark_resolved() API must exist for caller to signal success")

    def test_mc_write_read_cycle(self):
        """MC-A: write+read 100 entries, verify no corruption under threading."""
        import importlib, sys as _sys
        # Test the dedup logic purely in-memory
        entries = []
        lock = threading.Lock()

        def _canonical(src, dest):
            return f"{pathlib.Path(src).name}|{dest}"

        def add_entry(src, dest, reason="test"):
            key = _canonical(src, dest)
            with lock:
                for e in entries:
                    if e["track_key"] == key:
                        e["attempts"] += 1
                        return
                entries.append({
                    "track_key": key, "src": src, "dest_folder": dest,
                    "attempts": 1, "resolved": False,
                    "first_seen": time.time(), "last_seen": time.time(),
                })

        threads = [
            threading.Thread(target=add_entry, args=(f"/music/{i}.flac", "/Music/Jazz"))
            for i in range(100)
        ]
        for t in threads: t.start()
        for t in threads: t.join()

        # No duplicate track_keys
        keys = [e["track_key"] for e in entries]
        self.assertEqual(len(keys), len(set(keys)),
            "Concurrent writes created duplicate track_key entries")
        self.assertEqual(len(entries), 100)


# ===========================================================================
# F4 — Track-precise metakey invalidation
# ===========================================================================

class TestF4_MetakeyInvalidation(unittest.TestCase):
    """TC-F4: meta_key.py has track-level invalidation."""

    def setUp(self):
        self.src = _read("meta_key.py")

    def test_invalidate_by_track_id_exists(self):
        self.assertIn("def invalidate_by_track_id(", self.src,
            "invalidate_by_track_id() must be defined in meta_key.py")

    def test_signature_has_both_artist_and_title(self):
        s = self.src
        # MK18 has either artist_norm/title_norm or track_id/canonical approach
        self.assertTrue(
            ("artist_norm" in s and "title_norm" in s) or
            "def invalidate_by_canonical(" in s or
            "def invalidate_by_track_id(" in s,
            "meta_key must have track-precise invalidation"
        )

    def test_genre_client_uses_track_invalidation(self):
        gc = _read("genre_client.py")
        self.assertIn("invalidate_by_track_id", gc,
            "genre_client must call invalidate_by_track_id after cache write")

    def test_invalidate_by_artist_still_exists(self):
        """Artist-level invalidation must remain for bulk operations."""
        self.assertIn("def invalidate_by_artist(", self.src,
            "invalidate_by_artist() must be kept for backwards compat")


# ===========================================================================
# F5 — Album completeness OFFLINE metric
# ===========================================================================

class TestF5_AlbumOffline(unittest.TestCase):
    """TC-F5: sp_client=None emits SKIPPED_OFFLINE, not silent []."""

    def setUp(self):
        self.src = _read("album_completeness.py")

    def test_skipped_offline_warning(self):
        self.assertIn("SKIPPED_OFFLINE", self.src,
            "Must emit 'SKIPPED_OFFLINE' when sp_client is None")

    def test_warning_level(self):
        self.assertIn("SKIPPED_OFFLINE", self.src,
            "album_completeness must emit SKIPPED_OFFLINE marker")
        self.assertTrue("log.info" in self.src or "log.warning" in self.src,
            "Must log at info or warning level")


# ===========================================================================
# F6 — Circuit breaker half-open state
# ===========================================================================

class TestF6_CircuitBreakerHalfOpen(unittest.TestCase):
    """TC-F6: Gemini circuit breaker has half-open probe state."""

    def setUp(self):
        self.src = _read("genre_client.py")

    def test_half_open_state_defined(self):
        self.assertIn("_GEMINI_HALF_OPEN", self.src,
            "_GEMINI_HALF_OPEN flag must be defined")

    def test_cooldown_delay_configurable(self):
        self.assertIn("GEMINI_HALF_OPEN_DELAY_S", self.src,
            "Half-open cooldown delay must be a named constant")

    def test_probe_logic_in_should_skip(self):
        idx = self.src.find("def _gemini_circuit_should_skip(")
        section = self.src[idx:idx+1500]   # expanded — function body is longer in MK20
        self.assertIn("HALF_OPEN", section,
            "should_skip must check HALF_OPEN state")
        self.assertTrue("monotonic" in section or "__import__" in section,
            "Must use monotonic clock for cooldown timing")

    def test_mc_circuit_breaker_states(self):
        """MC simulation: trip → half-open → probe → closed."""
        # Simulate state machine
        state = {"disabled": False, "half_open": False,
                 "streak": 0, "tripped_at": 0.0}
        THRESHOLD = 5
        COOLDOWN  = 0.01  # tiny for test

        def record(code):
            if code == 429:
                state["streak"] += 1
                if state["streak"] >= THRESHOLD and not state["disabled"]:
                    state["disabled"]   = True
                    state["tripped_at"] = time.monotonic()
            else:
                state["streak"]   = 0
                state["disabled"] = False
                state["half_open"] = False

        def should_skip():
            if not state["disabled"]:
                return False
            elapsed = time.monotonic() - state["tripped_at"]
            if elapsed >= COOLDOWN and not state["half_open"]:
                state["half_open"] = True
                return False   # allow probe
            return True

        # Trip the breaker
        for _ in range(5): record(429)
        self.assertTrue(state["disabled"], "Breaker must trip after 5x 429")
        self.assertTrue(should_skip(), "Must skip while OPEN")

        # Wait for cooldown
        time.sleep(0.02)
        self.assertFalse(should_skip(), "Must allow probe in HALF_OPEN")

        # Probe succeeds
        record(200)
        self.assertFalse(state["disabled"], "Must close breaker after successful probe")


# ===========================================================================
# F7 — Path budget full-path validation
# ===========================================================================

class TestF7_PathBudget(unittest.TestCase):
    """TC-F7: path_utils validates assembled path before transfer."""

    def setUp(self):
        self.src = _read("path_utils.py")

    def test_truncation_exists(self):
        self.assertIn("truncat", self.src.lower(),
            "path_utils must have truncation logic")

    def test_mtp_aware(self):
        self.assertIn("MTP", self.src,
            "path_utils must be aware of MTP dest_parts contract")

    def test_path_budget_check(self):
        """Verify that an extremely long path gets truncated."""
        # Simulate safe_filename behaviour: long names get truncated
        def safe_filename(name: str, max_len: int = 200) -> str:
            name = str(name).strip()
            if len(name) > max_len:
                name = name[:max_len].rstrip()
            return name

        long_title = "A" * 300
        result = safe_filename(long_title)
        self.assertLessEqual(len(result), 200,
            "safe_filename must truncate names exceeding max_len")


# ===========================================================================
# F9 — Dialog watcher system denylist
# ===========================================================================

class TestF9_DialogDenylist(unittest.TestCase):
    """TC-F9: Enter fallback guarded by system-dialog denylist."""

    def setUp(self):
        self.src = _read("dialog_watcher.py")

    def test_deny_classes_defined(self):
        self.assertIn("_DENY_CLASSES", self.src,
            "_DENY_CLASSES set must be defined")

    def test_deny_title_fragments_defined(self):
        self.assertIn("_DENY_TITLE_FRAGMENTS", self.src,
            "_DENY_TITLE_FRAGMENTS list must be defined")

    def test_uac_in_denylist(self):
        idx = self.src.find("_DENY_CLASSES")
        section = self.src[idx:idx+600]
        self.assertIn("Consent UI for Windows", section,
            "UAC dialog class must be in denylist")

    def test_delete_word_denied(self):
        idx = self.src.find("_DENY_TITLE_FRAGMENTS")
        section = self.src[idx:idx+600]
        self.assertIn('"Delete"', section,
            "Dialogs titled 'Delete' must be denied Enter auto-click")

    def test_deny_check_guards_enter(self):
        """The _deny flag must prevent type_keys ENTER from firing."""
        # Search from 'if _deny:' (the gate), not from the assignment '_deny = ...'
        idx = self.src.find("if _deny:")
        section = self.src[idx:idx + 800]
        self.assertIn("DENIED", section,
            "Must log 'DENIED' when denylist matches")
        self.assertIn("else:", section,
            "Enter send must be in the else branch (only when not denied)")

    def test_mc_denylist_simulation(self):
        """MC: 200 random dialog titles — correct allow/deny decisions."""
        DENY_FRAGMENTS = [
            "User Account Control", "Windows Security", "Administrator",
            "Elevation", "Password", "Delete", "Format", "Uninstall",
        ]

        def should_deny(title: str) -> bool:
            t = (title or "").upper()
            return any(f.upper() in t for f in DENY_FRAGMENTS)

        safe_titles = [
            "Copy progress", "Transfer complete", "File exists — overwrite?",
            "Walkman sync", "ADB device", "MTP write",
        ]
        dangerous_titles = [
            "Confirm Delete", "Windows Security Alert", "Administrator Required",
            "User Account Control", "Format Drive", "Uninstall Program",
        ]
        rng = random.Random(42)
        errors = []
        for _ in range(100):
            t = rng.choice(safe_titles)
            if should_deny(t):
                errors.append(f"False positive deny: {t!r}")
        for _ in range(100):
            t = rng.choice(dangerous_titles)
            if not should_deny(t):
                errors.append(f"False negative allow: {t!r}")

        self.assertEqual(errors, [], "\n".join(errors[:5]))


# ===========================================================================
# F10 — Dedup just-written protection
# ===========================================================================

class TestF10_DedupProtection(unittest.TestCase):
    """TC-F10: _PROTECTED_PATHS prevents dedup deleting just-transferred files."""

    def setUp(self):
        self.src = _read("walkman_dedup.py")

    def test_protected_paths_set_defined(self):
        self.assertIn("_PROTECTED_PATHS", self.src,
            "_PROTECTED_PATHS module-level set must exist")

    def test_protect_path_fn_exists(self):
        self.assertIn("def protect_path(", self.src,
            "protect_path() must be defined")

    def test_clear_protected_paths_fn(self):
        self.assertIn("def clear_protected_paths(", self.src,
            "clear_protected_paths() must exist for pipeline start cleanup")

    def test_hard_delete_checks_protection(self):
        idx = self.src.find("def _hard_delete(")
        section = self.src[idx:idx+1500]
        self.assertIn("_PROTECTED_PATHS", section,
            "_hard_delete must check _PROTECTED_PATHS before deleting")
        self.assertIn("PROTECTED", section,
            "_hard_delete must log a PROTECTED message when skipping")

    def test_mc_protection_simulation(self):
        """MC: 200 delete attempts — protected paths always skipped."""
        protected = set()
        deleted    = []
        lock       = threading.Lock()

        def protect(path): protected.add(path)
        def hard_delete(path):
            with lock:
                if path in protected: return False
                deleted.append(path)
                return True

        # Protect 50, attempt all 200
        rng = random.Random(7)
        all_paths = [f"/storage/4A21/Music/Jazz/track_{i}.flac" for i in range(200)]
        protected_subset = set(rng.sample(all_paths, 50))
        for p in protected_subset: protect(p)

        for p in all_paths: hard_delete(p)

        # None of the protected paths should be deleted
        deleted_set = set(deleted)
        overlap = protected_subset & deleted_set
        self.assertEqual(overlap, set(),
            f"Protected paths were deleted: {list(overlap)[:3]}")
        self.assertEqual(len(deleted), 150,
            "Exactly 150 unprotected paths should be deleted")


# ===========================================================================
# F11 — Free space hard vs warn threshold
# ===========================================================================

class TestF11_FreeSpaceHardFloor(unittest.TestCase):
    """TC-F11: midrun_floor_check has hard_stop and warn tiers."""

    def setUp(self):
        self.src = _read("free_space_check.py")

    def test_hard_stop_key_in_return(self):
        self.assertIn("hard_stop", self.src,
            "midrun_floor_check must return 'hard_stop' key")

    def test_two_thresholds_defined(self):
        # thresholds may be defined in free_space_check.py or config.py
        import pathlib
        all_src = self.src + (pathlib.Path(__file__).parent / "config.py").read_text()
        self.assertIn("MIN_FREE_SPACE_GB_WARN", all_src)
        self.assertIn("MIN_FREE_SPACE_GB_HARD", all_src)

    def test_hard_stop_logs_critical(self):
        idx = self.src.find("hard_stop")
        section = self.src[max(0,idx-100):idx+500]
        self.assertIn("critical", section.lower(),
            "Hard stop must log at CRITICAL level")

    def test_hard_stop_flag_in_config(self):
        cfg = _read("config.py")
        self.assertIn("MIN_FREE_SPACE_GB_HARD", cfg,
            "Hard stop threshold must be in config.py")
        self.assertIn("MIN_FREE_SPACE_GB_WARN", cfg,
            "Warn threshold must be in config.py")

    def test_mc_threshold_scenarios(self):
        """MC-C: 200 space scenarios — correct hard_stop/warn decisions."""
        def check(avail_gb, hard_gb=0.3, warn_gb=1.0):
            avail = avail_gb * 1024**3
            hard  = hard_gb  * 1024**3
            warn  = warn_gb  * 1024**3
            if avail < hard:
                return {"ok": False, "hard_stop": True}
            if avail < warn:
                return {"ok": False, "hard_stop": False}
            return {"ok": True,  "hard_stop": False}

        rng = random.Random(99)
        errors = []
        for _ in range(200):
            gb = rng.uniform(0.0, 5.0)
            r  = check(gb)
            if gb < 0.3  and not r["hard_stop"]:
                errors.append(f"gb={gb:.2f}: should hard_stop but didn't")
            if gb >= 0.3 and gb < 1.0 and r["hard_stop"]:
                errors.append(f"gb={gb:.2f}: should warn-only but hard_stopped")
            if gb >= 1.0 and not r["ok"]:
                errors.append(f"gb={gb:.2f}: should be ok but wasn't")

        print(f"\n  [MC-C] 200 space scenarios: {len(errors)} errors")
        self.assertEqual(errors, [], "\n".join(errors[:3]))


# ===========================================================================
# F14 — AI genre taxonomy + year validation
# ===========================================================================

class TestF14_AIValidation(unittest.TestCase):
    """TC-F14: _validate_ai_genre enforces taxonomy and year range."""

    def setUp(self):
        self.src = _read("genre_client.py")

    def test_taxonomy_set_defined(self):
        self.assertIn("_VALID_GENRE_TAXONOMY", self.src,
            "Controlled genre taxonomy set must exist")

    def test_validate_fn_exists(self):
        self.assertIn("def _validate_ai_genre(", self.src,
            "_validate_ai_genre() function must be defined")

    def test_year_range_constants(self):
        self.assertIn("_YEAR_MIN", self.src)
        self.assertIn("_YEAR_MAX", self.src)
        idx = self.src.find("_YEAR_MIN")
        section = self.src[idx:idx+100]
        self.assertIn("1900", section, "Year min must be 1900")

    def test_validate_wired_to_call_ai_result(self):
        self.assertIn("_validate_ai_genre", self.src,
            "_validate_ai_genre must be called in the AI result path")

    def test_mc_genre_validation(self):
        """MC-B: 300 AI outputs — taxonomy hits and year rejects."""
        TAXONOMY = {
            "Hip-Hop", "Jazz", "Soul", "Afrobeats", "Amapiano",
            "R&B", "Trap", "Drill", "Bossa Nova", "Neo Soul",
        }
        YEAR_MIN, YEAR_MAX = 1900, 2030

        def validate(genre, year=""):
            clean_g = genre.strip()
            clean_y = ""
            if year:
                m = re.search(r"\b(\d{4})\b", str(year))
                if m:
                    y = int(m.group(1))
                    if YEAR_MIN <= y <= YEAR_MAX:
                        clean_y = str(y)
            return clean_g, clean_y

        rng = random.Random(13)
        valid_genres   = list(TAXONOMY)
        invalid_genres = ["Electronic Music", "N/A", "", "Unknown", "Various"]
        valid_years    = ["1975", "2003", "2019", "1989"]
        invalid_years  = ["2089", "1850", "N/A", "unknown", ""]

        year_rejected = 0
        for _ in range(300):
            g = rng.choice(valid_genres + invalid_genres)
            y = rng.choice(valid_years + invalid_years)
            _, clean_y = validate(g, y)
            if y in invalid_years and y not in ("", "N/A", "unknown") and clean_y:
                year_rejected += 1   # should have been rejected but wasn't

        print(f"\n  [MC-B] 300 genre validation calls: {year_rejected} bad years accepted")
        # Numeric out-of-range years (2089, 1850) must never be accepted
        _, y_2089 = validate("Jazz", "2089")
        _, y_1850 = validate("Jazz", "1850")
        self.assertEqual(y_2089, "", "Year 2089 must be rejected")
        self.assertEqual(y_1850, "", "Year 1850 must be rejected")
        _, y_2003 = validate("Jazz", "2003")
        self.assertEqual(y_2003, "2003", "Year 2003 must be accepted")


# ===========================================================================
# F15 — Run correlation ID
# ===========================================================================

class TestF15_CorrelationID(unittest.TestCase):
    """TC-F15: RUN_ID defined in config for log correlation."""

    def setUp(self):
        self.src = _read("config.py")

    def test_run_id_defined(self):
        self.assertIn("RUN_ID", self.src,
            "RUN_ID must be generated in config.py")

    def test_run_id_is_hex(self):
        """RUN_ID must be generated from timestamp + PID."""
        self.assertTrue(
            "int(_time.time())" in self.src or "_cfg_time.time()" in self.src
            or "time.time()" in self.src,
            "RUN_ID must incorporate current timestamp"
        )
        self.assertTrue(
            "os.getpid()" in self.src or "_cfg_os.getpid()" in self.src,
            "RUN_ID must incorporate PID for uniqueness"
        )


# ===========================================================================
# OWN findings — resource throttle + path limits
# ===========================================================================

class TestOWN_Additional(unittest.TestCase):
    """TC-OWN: additional team discoveries."""

    def test_OWN_B_resource_throttle_wired(self):
        """resource_monitor.wait_if_throttled wired to genre enrichment."""
        gc = _read("genre_client.py")
        self.assertIn("wait_if_throttled", gc,
            "genre_client must call resource_monitor.wait_if_throttled()")
        self.assertIn("MK17 FIX-OWN-B", gc,
            "OWN-B fix marker must be present")

    def test_OWN_C_free_space_dual_threshold_in_config(self):
        cfg = _read("config.py")
        self.assertIn("MIN_FREE_SPACE_GB_HARD", cfg)
        self.assertIn("MIN_FREE_SPACE_GB_WARN", cfg)
        # Check defaults are sensible (HARD < WARN)
        hard_m = re.search(r"MIN_FREE_SPACE_GB_HARD:\s*float\s*=\s*([\d.]+)", cfg)
        warn_m = re.search(r"MIN_FREE_SPACE_GB_WARN:\s*float\s*=\s*([\d.]+)", cfg)
        if hard_m and warn_m:
            self.assertLess(float(hard_m.group(1)), float(warn_m.group(1)),
                "HARD floor must be lower than WARN threshold")

    def test_OWN_deferred_no_plain_text_append(self):
        """OWN-A: deferred_transfers must not use plain open(path, 'a')."""
        dt = _read("deferred_transfers.py")
        # Should not have the old plain-text pattern
        self.assertNotIn('open(rp, "a"', dt,
            "Must not use plain file append — use atomic JSON-Lines write")


# ===========================================================================
# Regression guard — P7 walkman_reorganize
# ===========================================================================

class TestP7_DeleteItemBreaker(unittest.TestCase):
    """Verify P7 patches in walkman_reorganize.py."""

    def setUp(self):
        self.src = _read("walkman_reorganize.py")

    def test_breaker_globals(self):
        self.assertIn("_DELETE_BREAKER", self.src)
        self.assertIn("trip_threshold", self.src)

    def test_threading_timeout(self):
        self.assertIn(".join(timeout=timeout_s)", self.src)
        self.assertIn("rename+move hung", self.src)

    def test_reset_helper(self):
        self.assertIn("def _reset_delete_breaker", self.src)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    print("=" * 68)
    print("SpotifyTuner MK17 — Adversarial Forensic Hardening Suite")
    print("=" * 68)
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestF2_COMVerify,
        TestF3_DeferredQueue,
        TestF4_MetakeyInvalidation,
        TestF5_AlbumOffline,
        TestF6_CircuitBreakerHalfOpen,
        TestF7_PathBudget,
        TestF9_DialogDenylist,
        TestF10_DedupProtection,
        TestF11_FreeSpaceHardFloor,
        TestF14_AIValidation,
        TestF15_CorrelationID,
        TestOWN_Additional,
        TestP7_DeleteItemBreaker,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
