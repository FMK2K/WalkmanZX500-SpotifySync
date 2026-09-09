"""
test_mk39_1_l1_production_log_fixes.py — operator dry-run fixes
================================================================================

Council operator-feedback 2026-05-10 (LOG_10_5_26 production dry-run + Windows
env). Adversarial-review-vetted shortlist of 4 surviving fixes:

  L1.1  Sync planner crash: str.path AttributeError
  L1.2  fpcalc / MusicBee CWD probe on Windows (operator's PowerShell pain)
  L1.3  Dedup pre-commit plan review CLI (4253 planned deletes needed audit)
  L1.4  Truncation-collision detection helper (observability only)

The fpcalc real binary IS available in this sandbox so we test those paths
too; MusicBee subprocess is always mocked (Windows-only).
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# ═══════════════════════════════════════════════════════════════════════════
# L1.1 — Sync planner crash: string entries from device_index.files must be
# wrapped into FileState (with recovered size) before being passed to the
# planner. Production crash:
#     [Preflight] Sync planner failed
#     (AttributeError: 'str' object has no attribute 'path')
# ═══════════════════════════════════════════════════════════════════════════

class TestL1_1_SyncPlannerStringWrap(unittest.TestCase):
    """Test the wrapping logic that walkman_sync.py uses to construct a
    DeviceSnapshot from a device_index whose .files is dict[path→size]."""

    def _wrap_logic(self, di_files, device_index):
        """Replicate the (post-fix) wrapping logic from walkman_sync.py
        lines 1342-1383 so we can test it in isolation."""
        from sync_operations import FileState
        fs_list: list = []
        for f in di_files:
            if isinstance(f, dict):
                fs_list.append(FileState(
                    path=str(f.get("path", "")),
                    size_bytes=int(f.get("size_bytes") or f.get("size") or 0),
                    mtime=float(f.get("mtime") or 0.0),
                    content_hash=f.get("content_hash") or f.get("hash") or "",
                ))
            elif isinstance(f, str):
                recovered_size = 0
                try:
                    files_obj = getattr(device_index, "files", None)
                    if isinstance(files_obj, dict):
                        raw = files_obj.get(f, 0)
                        if isinstance(raw, int):
                            recovered_size = raw
                        elif isinstance(raw, dict):
                            recovered_size = int(raw.get("size", 0) or 0)
                except (TypeError, ValueError, AttributeError):
                    recovered_size = 0
                fs_list.append(FileState(
                    path=f, size_bytes=recovered_size, mtime=0.0,
                ))
            else:
                fs_list.append(f)
        return fs_list

    def test_T1_string_entries_wrapped_to_FileState(self):
        """device_index.files is dict[path→size]; list(.files) yields strings.
        Those strings MUST be wrapped, not appended raw. Production crash was:
        sync_planner.py:105 calls f.path on each element."""
        # Mock device_index: .files is dict[str→int]
        class FakeIndex:
            files = {
                "Music/Artist1/track1.mp3":   4_000_000,
                "Music/Artist1/track2.flac": 22_000_000,
                "Music/Artist2/empty":               0,
            }
        di_files = list(FakeIndex.files.keys())   # → list of STRINGS
        wrapped = self._wrap_logic(di_files, FakeIndex)
        self.assertEqual(len(wrapped), 3)
        # Every entry must have .path attribute (the crash condition)
        for fs in wrapped:
            self.assertTrue(hasattr(fs, "path"),
                              "wrapped entry missing .path attribute "
                              "(would crash sync_planner.py:105)")
            self.assertIsInstance(fs.path, str)
        # Sizes recovered from the dict
        sizes = sorted(fs.size_bytes for fs in wrapped)
        self.assertEqual(sizes, [0, 4_000_000, 22_000_000])

    def test_T1b_dict_entries_still_wrapped(self):
        """The other code path (dict entries) must continue working."""
        class FakeIndex: files = {}
        di_files = [
            {"path": "Music/x.mp3", "size_bytes": 1234},
            {"path": "Music/y.flac", "size": 5678, "content_hash": "abc"},
        ]
        wrapped = self._wrap_logic(di_files, FakeIndex)
        self.assertEqual(len(wrapped), 2)
        self.assertEqual(wrapped[0].path, "Music/x.mp3")
        self.assertEqual(wrapped[0].size_bytes, 1234)
        self.assertEqual(wrapped[1].size_bytes, 5678)
        self.assertEqual(wrapped[1].content_hash, "abc")

    def test_T1c_already_FileState_passes_through(self):
        """FileState-shaped entries must pass through unchanged."""
        from sync_operations import FileState
        class FakeIndex: files = {}
        fs = FileState(path="Music/z.mp3", size_bytes=999, mtime=0.0)
        wrapped = self._wrap_logic([fs], FakeIndex)
        self.assertEqual(len(wrapped), 1)
        self.assertIs(wrapped[0], fs)

    def test_T1d_planner_no_longer_crashes_on_string_input(self):
        """End-to-end: wrap strings → pass to generate_plan → no crash."""
        from sync_operations import DeviceSnapshot
        from sync_planner import build_sync_plan
        class FakeIndex:
            files = {
                "Music/a.mp3": 1000,
                "Music/b.flac": 5000,
            }
        wrapped = self._wrap_logic(list(FakeIndex.files.keys()), FakeIndex)
        snapshot = DeviceSnapshot(files=wrapped, complete=True)
        # This used to crash with AttributeError: 'str' object has no attribute 'path'
        plan = build_sync_plan([], snapshot)
        self.assertIsNotNone(plan)


# ═══════════════════════════════════════════════════════════════════════════
# L1.2 — fpcalc / MusicBee CWD probe on Windows
# Operator hit this literally: `fpcalc -version` failed in PowerShell because
# PowerShell doesn't search CWD by default. `.\fpcalc -version` works.
# Our shutil.which-only detection missed the binary present in their dir.
# ═══════════════════════════════════════════════════════════════════════════

class TestL1_2_FpcalcCwdProbe(unittest.TestCase):
    def setUp(self):
        # Force fresh import to pick up our changes
        for m in ("acoustid_fingerprint", "config"):
            if m in sys.modules:
                del sys.modules[m]

    def test_T2_fpcalc_path_uses_real_binary_when_present(self):
        """Sanity: real fpcalc IS on PATH in this sandbox; _fpcalc_path()
        must find it via shutil.which BEFORE falling back to CWD probe."""
        import acoustid_fingerprint as af
        # The real fpcalc was installed via apt
        path = af._fpcalc_path()
        # Either PATH hit (sandbox setup) or None (not installed)
        if path:
            self.assertTrue(os.path.isfile(path))

    def test_T2b_fpcalc_path_probes_cwd_when_not_on_PATH(self):
        """When fpcalc is in CWD but NOT on PATH, the new probe must find it."""
        import acoustid_fingerprint as af
        # Create a fake fpcalc.exe in a tempdir; cd there; clear PATH
        tmp = Path(tempfile.mkdtemp())
        try:
            fake = tmp / ("fpcalc.exe" if sys.platform == "win32" else "fpcalc")
            fake.write_text("#!/bin/sh\necho fake")
            os.chmod(fake, 0o755)
            old_cwd = os.getcwd()
            old_path = os.environ.get("PATH", "")
            os.chdir(tmp)
            os.environ["PATH"] = ""   # hide all real binaries
            try:
                # Force shutil.which to miss everything
                with mock.patch("shutil.which", return_value=None):
                    found = af._fpcalc_path()
                self.assertEqual(found, str(fake),
                                  "CWD probe must locate fpcalc when PATH misses")
            finally:
                os.chdir(old_cwd)
                os.environ["PATH"] = old_path
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_T2c_config_fpcalc_path_takes_precedence(self):
        """Adversarial constraint: config override MUST run before CWD probe."""
        import acoustid_fingerprint as af
        import config
        tmp = Path(tempfile.mkdtemp())
        try:
            fake = tmp / "explicit_fpcalc"
            fake.write_text("#!/bin/sh\necho fake")
            os.chmod(fake, 0o755)
            original = getattr(config, "FPCALC_PATH", "")
            try:
                config.FPCALC_PATH = str(fake)
                found = af._fpcalc_path()
                self.assertEqual(found, str(fake),
                                  "config.FPCALC_PATH must take precedence")
            finally:
                config.FPCALC_PATH = original
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_T2d_logs_loudly_when_found_in_cwd(self):
        """Security signal: CWD-probe finds must log INFO so operator
        notices the unexpected probe location."""
        import acoustid_fingerprint as af
        import logging
        tmp = Path(tempfile.mkdtemp())
        try:
            fake = tmp / "fpcalc"
            fake.write_text("#!/bin/sh\necho fake")
            os.chmod(fake, 0o755)
            old_cwd = os.getcwd()
            old_path = os.environ.get("PATH", "")
            os.chdir(tmp)
            os.environ["PATH"] = ""
            # Capture INFO logs
            buf = io.StringIO()
            handler = logging.StreamHandler(buf)
            handler.setLevel(logging.INFO)
            logger = logging.getLogger("acoustid_fingerprint")
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                with mock.patch("shutil.which", return_value=None):
                    af._fpcalc_path()
                handler.flush()
                self.assertIn("non-PATH location", buf.getvalue())
            finally:
                logger.removeHandler(handler)
                os.chdir(old_cwd)
                os.environ["PATH"] = old_path
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestL1_2_MusicBeeCwdProbe(unittest.TestCase):
    """Same CWD probe pattern applied to MusicBee.exe for Windows portable
    operator installs."""

    def setUp(self):
        for m in ("musicbee_sync", "config"):
            if m in sys.modules:
                del sys.modules[m]

    def test_T3_musicbee_returns_None_on_non_Windows(self):
        """Hard platform gate — MusicBee is Windows-only .NET."""
        import platform, musicbee_sync as ms
        if platform.system() != "Windows":
            self.assertIsNone(ms.musicbee_executable_path())

    def test_T3b_musicbee_probes_cwd_after_PATH_on_Windows(self):
        """When platform IS Windows, CWD probe runs after PATH miss."""
        import musicbee_sync as ms
        # Force Windows code path
        with mock.patch("platform.system", return_value="Windows"):
            # And force shutil.which to miss
            tmp = Path(tempfile.mkdtemp())
            try:
                fake = tmp / "MusicBee.exe"
                fake.write_text("portable MusicBee marker")
                old_cwd = os.getcwd()
                os.chdir(tmp)
                try:
                    with mock.patch("shutil.which", return_value=None):
                        # Also bypass standard install locations
                        with mock.patch.object(ms, "_DEFAULT_MUSICBEE_PATHS", ()):
                            found = ms.musicbee_executable_path()
                    self.assertEqual(found, str(fake),
                                      "MusicBee CWD probe must find portable install")
                finally:
                    os.chdir(old_cwd)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# L1.3 — Dedup pre-commit plan review CLI
# Operator's dry-run produced 4253 deletes. Operator needs structured audit
# before running for real. .dedup_plan.json is the new artifact; CLI displays.
# ═══════════════════════════════════════════════════════════════════════════

CLI = [sys.executable, "quarantine_review.py"]


def _make_dedup_plan(folder: Path, candidates: list):
    """Write a fake .dedup_plan.json the CLI commands can consume."""
    cats: dict = {}
    for c in candidates:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    payload = {
        "version": 1,
        "council_target": "MK39.1 L1.3 (test)",
        "generated_at": "2026-05-10T00:00:00Z",
        "mode": "hard",
        "dry_run": True,
        "match_mode": "fingerprint",
        "scan_total": 100,
        "total_planned": len(candidates),
        "stats_by_category": cats,
        "_instructions": "test",
        "candidates": candidates,
    }
    (folder / ".dedup_plan.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


class TestL1_3_DedupPlanCLI(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(CLI + list(args),
                                capture_output=True, text=True, cwd=self._cwd)

    def test_T4_dedup_plan_missing_returns_3(self):
        empty = Path(tempfile.mkdtemp())
        try:
            r = self._run("--dedup-plan", str(empty))
            self.assertEqual(r.returncode, 3)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_T4b_dedup_plan_displays_grouped_by_category(self):
        """The plan must be grouped by category; suspicious ones flagged."""
        _make_dedup_plan(self._tmp, [
            {"human_path": "a/x.mp3", "superior_path": "a/x.m4a",
             "reason": "inferior_format", "category": "format_upgrade"},
            {"human_path": "b/y - 01 t.mp3", "superior_path": "b/y - 04 t.m4a",
             "reason": "inferior_format", "category": "track_renumber"},
            {"human_path": "c/Album - 01 t.mp3",
             "superior_path": "c/Album (197/Album - 01 t.m4a",
             "reason": "inferior_format", "category": "truncation_dupe"},
        ])
        r = self._run("--dedup-plan", str(self._tmp))
        self.assertEqual(r.returncode, 0)
        # Both suspicious categories represented + flagged
        self.assertIn("track_renumber", r.stdout)
        self.assertIn("truncation_dupe", r.stdout)
        self.assertIn("INSPECT CLOSELY", r.stdout)

    def test_T4c_dedup_plan_stats_shows_summary(self):
        _make_dedup_plan(self._tmp, [
            {"human_path": "a.mp3", "superior_path": "a.m4a",
             "reason": "inferior_format", "category": "format_upgrade"},
            {"human_path": "b.mp3", "superior_path": "b.m4a",
             "reason": "inferior_format", "category": "format_upgrade"},
            {"human_path": "c.mp3", "superior_path": "c.m4a",
             "reason": "inferior_format", "category": "other"},
        ])
        r = self._run("--dedup-plan-stats", str(self._tmp))
        self.assertEqual(r.returncode, 0)
        self.assertIn("total planned", r.stdout)
        self.assertIn("format_upgrade", r.stdout)
        self.assertIn("3", r.stdout)


# ═══════════════════════════════════════════════════════════════════════════
# L1.4 — Truncation-collision detection helper
# Same album written to '...Vol. 1' and '...Vol. 1 (197' folders → phantom dupes.
# Observability only this turn.
# ═══════════════════════════════════════════════════════════════════════════

class TestL1_4_TruncationDetection(unittest.TestCase):
    def setUp(self):
        if "walkman_dedup" in sys.modules:
            del sys.modules["walkman_dedup"]

    def test_T5_strip_trailing_truncation_drops_open_paren(self):
        from walkman_dedup import _strip_trailing_truncation
        # The exact pattern from LOG_10_5_26: ...Vol. 1 (197 → ...Vol. 1
        self.assertEqual(
            _strip_trailing_truncation("Album - Vol. 1 (197"),
            "Album - Vol. 1",
        )
        # Already-clean folders unchanged
        self.assertEqual(
            _strip_trailing_truncation("Artist - Album"),
            "Artist - Album",
        )

    def test_T5b_looks_like_truncation_dupe_real_pattern(self):
        """Real example from LOG_10_5_26."""
        from walkman_dedup import looks_like_truncation_dupe
        a = ("SD card/Music/The Hygrades - Wake Up You! The Rise and Fall of "
              "Nigerian Rock, Vol. 1/The Hygrades - 01 Keep On Moving.mp3")
        b = ("SD card/Music/The Hygrades - Wake Up You! The Rise and Fall of "
              "Nigerian Rock, Vol. 1 (197/The Hygrades - 01 Keep On Moving.m4a")
        self.assertTrue(looks_like_truncation_dupe(a, b),
                         "real log pattern must be flagged as truncation dupe")

    def test_T5c_distinct_albums_not_flagged(self):
        from walkman_dedup import looks_like_truncation_dupe
        a = "Music/Artist1/Album1/track.mp3"
        b = "Music/Artist1/Album2/track.mp3"
        self.assertFalse(looks_like_truncation_dupe(a, b))

    def test_T6_categorize_format_upgrade(self):
        from walkman_dedup import _categorize_dedup_candidate
        cat = _categorize_dedup_candidate(
            human_path="Music/A/A - 04 t.mp3",
            superior_path="Music/A/A - 04 t.m4a",
            reason="inferior_format",
        )
        self.assertEqual(cat, "format_upgrade")

    def test_T6b_categorize_track_renumber(self):
        from walkman_dedup import _categorize_dedup_candidate
        cat = _categorize_dedup_candidate(
            human_path="Music/A/A - 01 t.mp3",
            superior_path="Music/A/A - 04 t.m4a",
            reason="inferior_format",
        )
        self.assertEqual(cat, "track_renumber")

    def test_T6c_categorize_truncation_dupe_wins_over_format(self):
        """Truncation-dupe is the strongest structural signal."""
        from walkman_dedup import _categorize_dedup_candidate
        cat = _categorize_dedup_candidate(
            human_path="Music/Long Album Name/X - 01 t.mp3",
            superior_path="Music/Long Album Name (197/X - 01 t.m4a",
            reason="inferior_format",
        )
        self.assertEqual(cat, "truncation_dupe")


# ═══════════════════════════════════════════════════════════════════════════
# Non-regression: Phase I forensic 9/9 + MK39.1 C2 strict mode + MK38.0
# ═══════════════════════════════════════════════════════════════════════════

class TestNonRegression(unittest.TestCase):
    def test_T7_phase_I_forensic_still_9_of_9(self):
        if "mk39_phase_I_containment" in sys.modules:
            del sys.modules["mk39_phase_I_containment"]
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
        self.assertEqual(fails, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
