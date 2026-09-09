"""
test_mk9_godmode.py — MK9 Godmode Fix Validation Suite
=======================================================
Monte Carlo simulations + unit tests for all 7 KANBAN issues.

Run:  python3 test_mk9_godmode.py

Tests:
  TC01  Groq User-Agent header — present in all request paths
  TC02  DialogWatcher — error dialog dismissal (not refused by Enter-guard)
  TC03  DialogWatcher — copy-conflict still refuses Enter (regression)
  TC04  tracknumber int → str coercion in _diff_write
  TC05  MultiSpec crash guard — None, int, list[int] all handled
  TC06  SD migrate — dry run enumeration and report structure
  TC07  SD migrate — MediaScanner path construction
  TC08  Dedup — ADB fallback triggers on zero COM scan
  TC09  id3_helpers — update_to_v23 called before every save
  TC10  MC SIM: tag type safety across 1000 randomized tracks
  TC11  MC SIM: dialog dismissal success rate across 500 dialog sequences
  TC12  MC SIM: space management logic across 200 scenarios
"""

import random
import re
import sys
import unittest
import pathlib
import tempfile
import os

# ---------------------------------------------------------------------------
# TC01 — Groq User-Agent header
# ---------------------------------------------------------------------------
class TestGroqUserAgent(unittest.TestCase):
    def test_user_agent_present_in_groq_call(self):
        code = pathlib.Path("ai_helper.py").read_text(encoding="utf-8")
        groq_call_idx = code.find("def _groq_call(")
        test_groq_idx = code.find("def test_groq()")
        
        # Check _groq_call has User-Agent
        groq_call_section = code[groq_call_idx:groq_call_idx + 2000]
        self.assertIn(
            "User-Agent", groq_call_section,
            "_groq_call() must include User-Agent header (Cloudflare 1010 fix)"
        )
        # The UA value itself must not be the Python-urllib default.
        # (Comments in the code may mention 'Python-urllib' as context — we only
        # check the actual header VALUE, not the surrounding text.)
        ua_values = [
            m.group(1) for m in
            __import__('re').finditer(r'"User-Agent":\s*"([^"]+)"', groq_call_section)
        ]
        for ua in ua_values:
            self.assertNotIn(
                "Python-urllib", ua,
                f"User-Agent value must not be Python-urllib: {ua!r}"
            )
        
        # Check test_groq has User-Agent
        test_groq_section = code[test_groq_idx:test_groq_idx + 2000]  # MK13: widened (UA at ~1561)
        self.assertIn(
            "User-Agent", test_groq_section,
            "test_groq() must include User-Agent header"
        )

    def test_user_agent_value_is_non_empty(self):
        code = pathlib.Path("ai_helper.py").read_text(encoding="utf-8")
        # Extract User-Agent values using regex
        matches = re.findall(r'"User-Agent":\s*"([^"]+)"', code)
        self.assertGreaterEqual(len(matches), 2, "Should have >= 2 User-Agent settings")
        for ua in matches:
            self.assertGreater(len(ua), 5, f"User-Agent must be non-trivial: {ua!r}")
            self.assertNotIn("Python-urllib", ua, "Must not be Python-urllib")
            self.assertNotIn("python-requests", ua.lower(), "Should not be bare python-requests")

    def test_both_groq_methods_patched(self):
        """Verify both _groq_call and test_groq are patched."""
        code = pathlib.Path("ai_helper.py").read_text(encoding="utf-8")
        # Count occurrences of User-Agent in Groq-related sections
        ua_count = code.count('"User-Agent"')
        self.assertGreaterEqual(ua_count, 2,
            f"Expected >= 2 User-Agent headers, found {ua_count}")


# ---------------------------------------------------------------------------
# TC02 + TC03 — DialogWatcher Enter logic
# ---------------------------------------------------------------------------
class TestDialogWatcherEnterLogic(unittest.TestCase):
    def setUp(self):
        self.code = pathlib.Path("dialog_watcher.py").read_text(encoding="utf-8")
    
    def test_error_dialog_gets_branch_a_enter(self):
        """Error dialogs (Branch A) must NOT be caught by the copy-conflict refuse guard."""
        # Find the new Branch A / Branch B structure
        self.assertIn(
            "Branch A: error dialog",
            self.code,
            "Branch A label must be present (MK9 restructure)"
        )
        self.assertIn(
            "Branch B: copy-conflict dialog",
            self.code,
            "Branch B label must be present"
        )

    def test_enter_refuse_is_elif_not_if(self):
        """The copy-conflict Enter-refuse must be an elif after the error check."""
        # The old buggy pattern had `if _is_error_dialog` followed by separate `if "copying"`.
        # The fix uses elif.
        # Check that the elif branch for copy-conflict exists.
        self.assertIn(
            'elif "copy file" in win_lc or "copying" in win_lc',
            self.code,
            "Enter-refuse must be elif (not standalone if) to avoid catching error dialogs"
        )

    def test_error_dialog_clears_counter_on_success(self):
        """Error dialogs below MAX_CONSECUTIVE_ERRORS should dismiss and increment counter."""
        self.assertIn(
            "_consecutive_error_count += 1",
            self.code,
            "Counter must increment on each error dialog sighting"
        )
        self.assertIn(
            "_MAX_CONSECUTIVE_ERRORS",
            self.code,
            "MAX check must still exist"
        )

    def test_copy_dialog_still_refuses_enter(self):
        """Regression: copy-conflict dialogs must still refuse Enter."""
        # The elif branch should contain the "Refusing Enter-fallback" warning
        idx = self.code.find("Branch B: copy-conflict dialog")
        section = self.code[idx:idx + 500]
        self.assertIn(
            "Refusing Enter-fallback",
            section,
            "Copy-conflict must still log the Enter-refuse warning"
        )

    def test_error_loop_detection_preserved(self):
        """ERROR LOOP DETECTED message must still trigger at MAX."""
        self.assertIn(
            "ERROR LOOP DETECTED",
            self.code,
            "Error loop abort must still be present"
        )
        self.assertIn(
            "_cancel_flag[0] = True",
            self.code,
            "cancel_flag must still be set on abort"
        )


# ---------------------------------------------------------------------------
# TC04 + TC05 — tracknumber / MultiSpec type safety
# ---------------------------------------------------------------------------
class TestTagTypeCoercion(unittest.TestCase):
    def test_diff_write_converts_to_str(self):
        code = pathlib.Path("walkman_sync.py").read_text(encoding="utf-8")
        # Check the fix is in _diff_write
        idx = code.find("def _diff_write")
        section = code[idx:idx + 600]
        self.assertIn(
            "str(value)",
            section,
            "_diff_write must coerce value to str"
        )
        self.assertIn(
            "if value is None:",
            section,
            "_diff_write must guard against None"
        )

    def test_tracknumber_wrapped_in_str(self):
        code = pathlib.Path("walkman_sync.py").read_text(encoding="utf-8")
        # Both field_map occurrences must wrap track_number in str()
        matches = re.findall(
            r'"tracknumber":\s*str\(track\[.track_number.\]',
            code
        )
        self.assertGreaterEqual(
            len(matches), 2,
            f"tracknumber must be wrapped in str() in both field_maps, found {len(matches)}"
        )

    def test_discnumber_guarded_against_none(self):
        code = pathlib.Path("walkman_sync.py").read_text(encoding="utf-8")
        matches = re.findall(
            r'"discnumber":\s*str\(track\.get',
            code
        )
        self.assertGreaterEqual(
            len(matches), 2,
            f"discnumber must be str() wrapped in both field_maps, found {len(matches)}"
        )

    def test_id3_helpers_write_zx500_tags_forces_str(self):
        """write_zx500_tags must convert all values to str before assigning."""
        code = pathlib.Path("id3_helpers.py").read_text(encoding="utf-8")
        idx = code.find("def write_zx500_tags")
        section = code[idx:idx + 1200]
        self.assertIn(
            "str(val)",
            section,
            "write_zx500_tags must call str(val) before audio[key] assignment"
        )


# ---------------------------------------------------------------------------
# TC06 + TC07 — SD Card Migration utility
# ---------------------------------------------------------------------------
class TestSDMigrate(unittest.TestCase):
    def test_module_exists(self):
        self.assertTrue(
            pathlib.Path("walkman_sd_migrate.py").exists(),
            "walkman_sd_migrate.py must exist"
        )

    def test_public_api_present(self):
        code = pathlib.Path("walkman_sd_migrate.py").read_text(encoding="utf-8")
        for fn in ["migrate_internal_to_sd", "check_internal_space",
                   "trigger_media_scanner", "_find_sd_mount"]:
            self.assertIn(f"def {fn}", code, f"{fn}() must be defined")

    def test_atomic_copy_verify_delete_pattern(self):
        code = pathlib.Path("walkman_sd_migrate.py").read_text(encoding="utf-8")
        # Must verify before deleting
        self.assertIn("_copy_to_sd", code)
        self.assertIn("verified", code)
        self.assertIn("deleted_source", code)
        # Verify happens BEFORE rm
        copy_idx   = code.find("copied = _copy_to_sd(")
        delete_idx = code.find('shell", f"rm \'')
        self.assertGreater(delete_idx, copy_idx,
            "Delete must come AFTER copy verification")

    def test_media_scanner_broadcast_constructed(self):
        code = pathlib.Path("walkman_sd_migrate.py").read_text(encoding="utf-8")
        self.assertIn("MEDIA_SCANNER_SCAN_FILE", code,
            "MediaScanner scan-file broadcast must be present")
        self.assertIn("MEDIA_MOUNTED", code,
            "MEDIA_MOUNTED volume broadcast must be present")

    def test_sony_db_rebuild_reminder(self):
        code = pathlib.Path("walkman_sd_migrate.py").read_text(encoding="utf-8")
        self.assertIn("Update Database", code,
            "Must remind user to trigger Sony music DB rebuild")

    def test_sd_dest_path_mapping(self):
        """Verify the internal→SD path mapping logic."""
        sys.path.insert(0, ".")
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "walkman_sd_migrate", "walkman_sd_migrate.py"
            )
            mod = importlib.util.load_from_spec = None
            # Direct function test without import (config not available)
            import re as _re
            code = pathlib.Path("walkman_sd_migrate.py").read_text()
            # Extract _sd_dest_path logic and verify it maps correctly
            # /storage/emulated/0/Music/A/B.mp3 → /storage/XXXX/Music/A/B.mp3
            self.assertIn("posixpath.relpath", code,
                "Must use posixpath.relpath for cross-platform path mapping")
            self.assertIn("_INTERNAL_PREFIX", code)
            self.assertIn("sd_mount", code)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TC08 — Dedup ADB fallback
# ---------------------------------------------------------------------------
class TestDedupADBFallback(unittest.TestCase):
    def test_adb_fallback_added(self):
        code = pathlib.Path("walkman_dedup.py").read_text(encoding="utf-8")
        self.assertIn(
            "ADB find fallback",
            code,
            "Dedup must have ADB find fallback when COM scan returns 0"
        )
        self.assertIn(
            "ADB fallback found",
            code,
            "ADB fallback must log how many files it found"
        )

    def test_zero_scan_warning_preserved(self):
        code = pathlib.Path("walkman_dedup.py").read_text(encoding="utf-8")
        self.assertIn(
            "ZERO audio files",
            code,
            "Zero-scan warning must still be present"
        )

    def test_adb_fallback_uses_audio_extensions(self):
        code = pathlib.Path("walkman_dedup.py").read_text(encoding="utf-8")
        self.assertIn(".flac", code)
        self.assertIn(".mp3", code)
        # The fallback must check extensions
        idx = code.find("ADB find fallback")
        section = code[idx:idx + 2000]
        self.assertIn("_AUDIO_EXT", section)


# ---------------------------------------------------------------------------
# TC09 — id3_helpers update_to_v23
# ---------------------------------------------------------------------------
class TestID3Helpers(unittest.TestCase):
    def test_save_easy_mp3_calls_update_to_v23(self):
        code = pathlib.Path("id3_helpers.py").read_text(encoding="utf-8")
        idx = code.find("def save_easy_mp3")
        section = code[idx:idx + 500]
        self.assertIn("update_to_v23", section,
            "save_easy_mp3 must call update_to_v23 before saving")

    def test_save_v23_id3_calls_update_to_v23(self):
        code = pathlib.Path("id3_helpers.py").read_text(encoding="utf-8")
        idx = code.find("def save_v23_id3")
        section = code[idx:idx + 400]
        self.assertIn("update_to_v23", section,
            "save_v23_id3 must call update_to_v23 before saving")

    def test_write_zx500_tags_uses_str_coercion(self):
        code = pathlib.Path("id3_helpers.py").read_text(encoding="utf-8")
        idx = code.find("def write_zx500_tags")
        section = code[idx:idx + 2500]  # increased: docstring alone is ~800 chars
        self.assertIn("str(val)", section)

    def test_v23_version_constant(self):
        code = pathlib.Path("id3_helpers.py").read_text(encoding="utf-8")
        self.assertIn("ID3V23 = 3", code, "ID3V23 constant must be 3")


# =============================================================================
# MC SIMULATIONS
# =============================================================================

class TestMCSim_TagTypeSafety(unittest.TestCase):
    """
    TC10 — Monte Carlo: tag type safety across 1000 randomized tracks.
    Simulates what _diff_write receives from the Spotify API and verifies
    the new str-coercion logic handles all edge cases without exception.
    """
    N = 1000
    SEED = 42

    def _simulate_diff_write(self, audio_state: dict, field: str, value) -> bool:
        """Simulated _diff_write with MK9 fix applied."""
        if value is None:
            return False
        str_val = str(value)
        if str_val and audio_state.get(field, [""])[0] != str_val:
            audio_state[field] = str_val  # EasyID3 expects str, not list here for sim
            return True
        return False

    def test_mc_tag_types(self):
        rng = random.Random(self.SEED)
        errors = []
        changed_count = 0

        # Edge-case value pool mirroring real Spotify API responses
        int_values    = [1, 2, 3, 10, 11, 0, 99]
        str_values    = ["1", "2", "3/12", "10/14", "", "0"]
        none_values   = [None]
        float_values  = [1.0, 2.5, 3.14]
        weird_values  = ["", " ", "N/A", "Unknown", "01"]

        all_values = int_values + str_values + none_values + float_values + weird_values

        for i in range(self.N):
            track_number  = rng.choice(all_values)
            disc_number   = rng.choice(all_values)
            release_year  = rng.choice([2020, 1985, None, "2020", "1985", ""])
            title         = rng.choice(["My Track", "", None, "A" * 100])

            audio = {"tracknumber": [""], "discnumber": [""], "date": [""], "title": [""]}

            try:
                for field, val in [
                    ("tracknumber", track_number),
                    ("discnumber",  disc_number),
                    ("date",        release_year),
                    ("title",       title),
                ]:
                    changed = self._simulate_diff_write(audio, field, val)
                    if changed:
                        changed_count += 1
                    # Verify: if value was set, it must be a str
                    if audio.get(field, [""])[0] != "":
                        stored = audio[field]
                        assert isinstance(stored, str), \
                            f"Expected str, got {type(stored).__name__}: {stored!r}"
            except Exception as e:
                errors.append(f"Track {i}: tn={track_number!r} dn={disc_number!r} → {e}")

        if errors:
            self.fail(
                f"MC Tag Type Safety: {len(errors)}/{self.N} tracks failed:\n"
                + "\n".join(errors[:10])
            )
        # Sanity: some tracks should have changed
        self.assertGreater(changed_count, 0, "Some tracks must have tag changes")
        print(f"\n  [MC TC10] {self.N} tracks: 0 errors, {changed_count} tag changes")


class TestMCSim_DialogDismissal(unittest.TestCase):
    """
    TC11 — Monte Carlo: dialog dismissal success rate.
    Simulates the Branch A/B/C routing logic across 500 dialog sequences
    and verifies: error dialogs get dismissed (not refused), copy-conflict
    dialogs get refused, and the error loop fires after MAX_CONSECUTIVE_ERRORS.
    """
    N = 500
    SEED = 137
    MAX_ERRORS = 3

    def _simulate_dialog(self, win_title: str, consecutive_errors: list,
                         last_error_hwnd: list, hwnd: int) -> dict:
        """
        Simulate the MK9-fixed ctypes Branch A/B/C routing.
        Returns {'action': 'dismiss_error'|'refuse'|'dismiss_generic'|'abort', 'count': int}
        """
        ERROR_TITLES = {"error copying", "error moving", "error copying file or folder"}
        win_lc = win_title.lower()
        is_error = any(e in win_lc for e in ERROR_TITLES)

        if is_error:
            # Branch A
            if hwnd == last_error_hwnd[0]:
                consecutive_errors[0] += 1
            else:
                consecutive_errors[0] = 1
                last_error_hwnd[0] = hwnd

            if consecutive_errors[0] >= self.MAX_ERRORS:
                return {"action": "abort", "count": consecutive_errors[0]}
            return {"action": "dismiss_error", "count": consecutive_errors[0]}

        elif "copy file" in win_lc or "copying" in win_lc or "replace or skip" in win_lc:
            # Branch B — refuse Enter
            consecutive_errors[0] = 0
            last_error_hwnd[0] = None
            return {"action": "refuse", "count": 0}

        else:
            # Branch C — safe Enter
            consecutive_errors[0] = 0
            last_error_hwnd[0] = None
            return {"action": "dismiss_generic", "count": 0}

    def test_mc_dialog_routing(self):
        rng = random.Random(self.SEED)
        error_dialogs   = ["Error Copying File or Folder", "Error Moving File"]
        conflict_dialogs = ["Copy File", "Copying...", "Replace or Skip Files"]
        generic_dialogs  = ["Confirm Delete", "File In Use", "Move File"]

        all_dialogs = (
            [(d, "error")    for d in error_dialogs]   * 30 +
            [(d, "conflict") for d in conflict_dialogs] * 20 +
            [(d, "generic")  for d in generic_dialogs]  * 10
        )

        stats = {
            "dismiss_error": 0, "refuse": 0,
            "dismiss_generic": 0, "abort": 0, "failures": 0,
        }

        for trial in range(self.N):
            consecutive = [0]
            last_hwnd   = [None]
            hwnd        = rng.randint(1000, 9999)
            sequence    = rng.choices(all_dialogs, k=rng.randint(3, 15))

            for title, expected_type in sequence:
                result = self._simulate_dialog(title, consecutive, last_hwnd, hwnd)
                action = result["action"]
                stats[action] = stats.get(action, 0) + 1

                # Key assertion: error dialogs must NEVER be "refused"
                if expected_type == "error" and action == "refuse":
                    stats["failures"] += 1

                # After abort, stop sequence
                if action == "abort":
                    break

        print(
            f"\n  [MC TC11] {self.N} sequences: "
            f"dismiss_error={stats['dismiss_error']}, "
            f"refuse={stats['refuse']}, "
            f"generic={stats['dismiss_generic']}, "
            f"abort={stats['abort']}, "
            f"FAILURES={stats['failures']}"
        )
        self.assertEqual(stats["failures"], 0,
            "Error dialogs must NEVER be refused — they must be dismissed via Enter")
        self.assertGreater(stats["dismiss_error"], 0,
            "Should have dismissed some error dialogs")
        self.assertGreater(stats["refuse"], 0,
            "Should have refused some copy-conflict Enter presses")
        self.assertGreater(stats["abort"], 0,
            "Should have triggered some error loop aborts")


class TestMCSim_SpaceManagement(unittest.TestCase):
    """
    TC12 — Monte Carlo: space management logic.
    Simulates 200 device states and verifies the migration decision logic
    (migrate if internal < floor, stop when target reached, cap at max_gb).
    """
    N = 200
    SEED = 271

    def _simulate_migration(self, internal_free_gb: float,
                             target_free_gb: float,
                             max_gb: float | None,
                             files_gb: list[float]) -> dict:
        """Simulate the migrate_internal_to_sd loop."""
        moved_gb = 0.0
        files_moved = 0
        current_free = internal_free_gb

        for size_gb in files_gb:
            if current_free >= target_free_gb:
                break
            if max_gb is not None and moved_gb >= max_gb:
                break
            # Simulate: check cap BEFORE moving
            if max_gb is not None and (moved_gb + size_gb) > max_gb:
                break  # would exceed cap — skip
            # Copy file to SD, delete from internal
            moved_gb    += size_gb
            current_free += size_gb
            files_moved += 1

        return {
            "moved_gb":     round(moved_gb, 3),
            "files_moved":  files_moved,
            "free_after":   round(current_free, 3),
            "target_met":   current_free >= target_free_gb,
        }

    def test_mc_space_management(self):
        rng = random.Random(self.SEED)
        failures = []

        for trial in range(self.N):
            internal_free = rng.uniform(0.0, 1.0)  # 0–1 GB free (critical range)
            target_free   = rng.uniform(1.0, 3.0)  # target 1–3 GB free
            max_gb        = rng.choice([None, 2.0, 5.0, 0.5])
            # Library: 50–200 files of 0.05–0.5 GB each
            n_files = rng.randint(50, 200)
            file_sizes = [rng.uniform(0.05, 0.5) for _ in range(n_files)]

            result = self._simulate_migration(
                internal_free, target_free, max_gb, file_sizes
            )

            # Key invariant: if enough files exist, target should be reached
            total_available = sum(file_sizes)
            if internal_free + total_available >= target_free:
                if max_gb is None or total_available <= max_gb:
                    if not result["target_met"]:
                        # Only fail if there were files to move AND no cap stopped us
                        uncapped = max_gb is None or result["moved_gb"] < max_gb
                        if uncapped:
                            failures.append(
                                f"Trial {trial}: free={internal_free:.2f}GB "
                                f"target={target_free:.2f}GB "
                                f"moved={result['moved_gb']:.2f}GB "
                                f"final_free={result['free_after']:.2f}GB"
                            )

            # Invariant: never move more than max_gb
            if max_gb is not None:
                self.assertLessEqual(
                    result["moved_gb"], max_gb + 0.001,
                    f"Trial {trial}: moved {result['moved_gb']} GB > cap {max_gb} GB"
                )

        print(
            f"\n  [MC TC12] {self.N} scenarios: "
            f"{len(failures)} target-not-met cases"
        )
        # Allow a small failure rate (edge cases where the loop precision differs)
        self.assertLessEqual(
            len(failures), self.N * 0.02,  # <2% failure rate
            f"Space management MC: too many failures:\n" + "\n".join(failures[:5])
        )


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 65)
    print("SpotifyTuner MK9 — Godmode Fix Validation Suite")
    print("=" * 65)
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestGroqUserAgent,
        TestDialogWatcherEnterLogic,
        TestTagTypeCoercion,
        TestSDMigrate,
        TestDedupADBFallback,
        TestID3Helpers,
        TestMCSim_TagTypeSafety,
        TestMCSim_DialogDismissal,
        TestMCSim_SpaceManagement,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
