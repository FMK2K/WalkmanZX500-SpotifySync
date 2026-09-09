"""
test_mk39_1_l5_forensic_response.py — Council Forensic Review of LOG_11_5_26
================================================================================

Council Forensic Review 2026-05-11 identified three P0 issues after overnight run:
  §5.1 SyncPlan.planned_dirs attribute missing → Stage 2 completely skipped
  §5.2 silent_move pull/push asymmetry → 385 reorganize errors
  §5.3 Pre-existing inventory corruption (208 dupes, 289 blank metadata, etc.)

L5 subtickets:
  L5.1  SyncPlan.planned_dirs field + populator (§5.1)
  L5.2  silent_move pull-verify-push + degenerate-dest guard (§5.2)
  L5.3  walkman_inventory_repair.py — classify-and-report (§5.3)
  L5.4  CLI wrapper for inventory_repair
  L5.5  Caller-side getattr backstop for planned_dirs
"""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════
# L5.1 — SyncPlan.planned_dirs field exists + is populated
# ═══════════════════════════════════════════════════════════════════════════

class TestSyncPlanPlannedDirs(unittest.TestCase):
    def setUp(self):
        for m in ("sync_operations", "sync_planner"):
            if m in sys.modules: del sys.modules[m]

    def test_L5_1a_field_exists_with_default_empty_list(self):
        """SyncPlan must declare planned_dirs as a list field with
        default_factory=list (Council §5.1 mandate)."""
        import sync_operations as so
        sp = so.SyncPlan(run_id="t1")
        self.assertTrue(hasattr(sp, "planned_dirs"),
                          "SyncPlan must have planned_dirs attribute")
        self.assertEqual(sp.planned_dirs, [],
                          "planned_dirs must default to empty list")

    def test_L5_1b_planner_populates_planned_dirs_from_copies(self):
        """generate_plan must populate planned_dirs from copy destinations."""
        import sync_operations as so
        from sync_planner import build_sync_plan
        local = [
            so.FileState(path="Music/Artist1/Album1/01.mp3", size_bytes=1, mtime=0.0),
            so.FileState(path="Music/Artist1/Album1/02.mp3", size_bytes=1, mtime=0.0),
            so.FileState(path="Music/Artist2/Album2/01.mp3", size_bytes=1, mtime=0.0),
        ]
        plan = build_sync_plan(local, [])
        # All three are new files → 3 copy operations → 2 unique destination dirs
        self.assertGreater(len(plan.planned_dirs), 0,
                              "planned_dirs must be populated when copies exist")
        # Stored as sorted unique list
        self.assertEqual(plan.planned_dirs, sorted(set(plan.planned_dirs)))


# ═══════════════════════════════════════════════════════════════════════════
# L5.5 — caller-side getattr backstop
# ═══════════════════════════════════════════════════════════════════════════

class TestCallerBackstop(unittest.TestCase):
    """Even if a future SyncPlan subclass forgets the field, the caller
    must not crash with AttributeError as in LOG_11_5_26 line 178."""

    def test_L5_5_getattr_pattern_used_at_call_site(self):
        """The walkman_sync.py call-site uses getattr fallback."""
        wm_sync = Path("walkman_sync.py").read_text(encoding="utf-8")
        # The defensive read pattern must appear
        self.assertIn("getattr(plan, \"planned_dirs\", [])", wm_sync,
                       "walkman_sync.py must use getattr(...) for planned_dirs")
        # And the bare attribute access must NOT appear in the scrub block
        # (it's allowed in test code; we just check the production caller path)
        # Confirm the L5.5 comment landed:
        self.assertIn("MK39.1 L5.5", wm_sync,
                       "L5.5 sentinel comment missing from walkman_sync.py")


# ═══════════════════════════════════════════════════════════════════════════
# L5.2 — silent_move pull-verify-push + degenerate-dest guard
# ═══════════════════════════════════════════════════════════════════════════

class TestSilentMoveHardening(unittest.TestCase):
    """The actual silent_move can't be tested without the full pywin32 +
    MTP stack. We verify the L5.2 patches are present in the source via
    AST/text inspection, plus a unit test for the degenerate-dest pattern
    detection regex."""

    def test_L5_2a_degenerate_dest_regex_catches_terumasa_pattern(self):
        """The exact Terumasa Hino LOG_11_5_26 case must be detected by
        the degenerate-dest pattern."""
        import re
        # Pull the same regex used in silent_move (kept in sync via the
        # comment with the regex literal)
        pattern = re.compile(r"\s-\s+-\s+\d+\s*$|\s-\s+-\s*$")
        # The actual log evidence:
        deg = "Terumasa Hino - - 01"   # stem (no extension)
        self.assertIsNotNone(pattern.search(deg),
                                f"degenerate-dest regex must catch {deg!r}")
        # Non-degenerate cases must NOT match
        good_cases = [
            "Ebo Taylor - My Love and Music - 01 Will You Promise",
            "Pharrell Williams - In My Mind - 04 Best Friend",
            "Single Word",
            "Artist - Album - 12 Title with - dashes inside",
        ]
        for s in good_cases:
            self.assertIsNone(pattern.search(s),
                                f"regex must NOT match good name {s!r}")

    def test_L5_2b_silent_move_has_pull_verify_gate(self):
        """walkman_reorganize.py must include the pull-verify-size gate."""
        reorg = Path("walkman_reorganize.py").read_text(encoding="utf-8")
        # Required L5.2 markers
        for marker in (
            "MK39.1 L5.2",
            "Pull verification failed",
            "zero-byte",
            "DEGENERATE_DEST detected",
            "rename pulled→safe_path",
        ):
            self.assertIn(marker, reorg,
                           f"L5.2 marker missing from walkman_reorganize.py: {marker!r}")

    def test_L5_2c_pulled_stat_replaces_safe_path_stat(self):
        """The legacy `safe_path.stat()` call (which crashed when safe_path
        was a phantom path) must be replaced with `pulled.stat()`."""
        reorg = Path("walkman_reorganize.py").read_text(encoding="utf-8")
        # The previous bug was:
        #   _src_size = safe_path.stat().st_size
        # Fix uses pulled.stat() since pulled is always-real
        self.assertIn("_src_size = pulled.stat().st_size", reorg,
                       "L5.2: safe_path.stat → pulled.stat replacement missing")


# ═══════════════════════════════════════════════════════════════════════════
# L5.3 — Inventory repair classifier
# ═══════════════════════════════════════════════════════════════════════════

class TestInventoryRepairClassifier(unittest.TestCase):
    def setUp(self):
        if "walkman_inventory_repair" in sys.modules:
            del sys.modules["walkman_inventory_repair"]

    def _row(self, **overrides):
        """Build a row matching the operator's 18-column TSV schema."""
        base = {
            "location": "internal", "path": "/storage/emulated/0/Music/A/B/01.mp3",
            "size": "1000000", "codec": "mp3", "sample_rate": "44100",
            "bits": "16", "channels": "2", "bit_rate": "256000",
            "duration_s": "180.0", "title": "Song", "artist": "Artist",
            "album_artist": "Artist", "album": "Album", "track": "1",
            "disc": "1", "year": "2024", "genre": "Hip-Hop", "composer": "",
        }
        base.update(overrides)
        return base

    def test_L5_3a_classifies_exact_dupes(self):
        import walkman_inventory_repair as wir
        rows = [
            self._row(path="/storage/emulated/0/Music/A/01.mp3", size="1000"),
            self._row(path="/storage/4A21-0000/Music/A/01.mp3", size="1000"),
            self._row(path="/storage/emulated/0/Music/B/01.mp3", size="1000"),
        ]
        report = wir.classify_inventory(rows)
        # All three have basename='01.mp3' and size=1000 → one dupe group of 3
        self.assertEqual(len(report.exact_dupes), 1)
        g = report.exact_dupes[0]
        self.assertEqual(len(g.paths), 3)
        self.assertTrue(g.cross_storage,
                          "cross-storage detection must spot SD vs internal")

    def test_L5_3b_classifies_blank_metadata(self):
        import walkman_inventory_repair as wir
        rows = [
            self._row(title="", artist="X", album="X"),
            self._row(title="X", artist="", album=""),
            self._row(title="X", artist="X", album="X"),  # clean
        ]
        report = wir.classify_inventory(rows)
        self.assertEqual(len(report.blank_metadata), 2)
        self.assertIn("title", report.blank_metadata[0].missing_fields)

    def test_L5_3c_classifies_codec_ext_mismatch(self):
        """FLAC payload in .m4a container (LOG_11_5_26 — Ftquay YouTube
        Survey)."""
        import walkman_inventory_repair as wir
        rows = [
            self._row(path="x.m4a", codec="flac"),     # mismatch
            self._row(path="y.flac", codec="flac"),    # ok
            self._row(path="z.m4a", codec="aac"),       # ok (alias)
        ]
        report = wir.classify_inventory(rows)
        self.assertEqual(len(report.codec_ext_mismatch), 1)
        m = report.codec_ext_mismatch[0]
        self.assertEqual(m.extension, "m4a")
        self.assertEqual(m.declared_codec, "flac")

    def test_L5_3d_classifies_placeholder_year(self):
        import walkman_inventory_repair as wir
        rows = [
            self._row(year=""),         # placeholder
            self._row(year="1970"),      # placeholder
            self._row(year="0"),         # placeholder
            self._row(year="2024"),      # ok
        ]
        report = wir.classify_inventory(rows)
        self.assertEqual(len(report.placeholder_year), 3)

    def test_L5_3e_recommended_actions_populated(self):
        """Findings must produce recommended-action lines for the operator."""
        import walkman_inventory_repair as wir
        rows = [
            self._row(path="/a/01.mp3", size="1000"),
            self._row(path="/b/01.mp3", size="1000"),
            self._row(title="", artist="", album=""),
        ]
        report = wir.classify_inventory(rows)
        self.assertGreater(len(report.recommended_actions), 0)
        all_text = " ".join(report.recommended_actions)
        self.assertIn("DEDUP", all_text)
        self.assertIn("METADATA", all_text)

    def test_L5_3f_clean_inventory_no_actions(self):
        """A clean inventory must produce the 'CLEAN' recommendation."""
        import walkman_inventory_repair as wir
        report = wir.classify_inventory([self._row()])
        self.assertEqual(len(report.recommended_actions), 1)
        self.assertIn("CLEAN", report.recommended_actions[0])


# ═══════════════════════════════════════════════════════════════════════════
# L5.4 — CLI wrapper
# ═══════════════════════════════════════════════════════════════════════════

class TestInventoryRepairCLI(unittest.TestCase):
    def _write_tsv(self, rows: list, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter="\t")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_L5_4a_cli_runs_against_real_inventory(self):
        """CLI must successfully process the operator's actual inventory file."""
        r = subprocess.run(
            [sys.executable, "walkman_inventory_repair.py",
             "--inventory", "/mnt/user-data/uploads/walkman_inventory.tsv",
             "--summary-only"],
            capture_output=True, text=True, cwd=".",
        )
        self.assertEqual(r.returncode, 0,
                          f"CLI failed: {r.returncode}\nSTDOUT: {r.stdout[-300:]}\nSTDERR: {r.stderr[-300:]}")
        combined = r.stdout + r.stderr
        self.assertIn("WALKMAN INVENTORY REPAIR REPORT", combined)
        self.assertIn("Exact-dupe groups", combined)
        # Safety banner goes through logging → stderr
        self.assertIn("Council-endorsed unattended baseline", combined)

    def test_L5_4b_cli_emits_l4_safety_banner(self):
        """CLI must emit the L4 safety profile banner before doing anything."""
        r = subprocess.run(
            [sys.executable, "walkman_inventory_repair.py",
             "--inventory", "/mnt/user-data/uploads/walkman_inventory.tsv",
             "--summary-only"],
            capture_output=True, text=True, cwd=".",
        )
        # Banner is logged via platform_safety_profile.emit_banner() which
        # uses logging.* — logging defaults to stderr
        combined = r.stdout + r.stderr
        self.assertIn("[Safety/L4] PROFILE", combined)

    def test_L5_4c_cli_writes_json_plan(self):
        """--plan must produce a valid JSON repair plan."""
        with tempfile.TemporaryDirectory() as tmp:
            inv_path = Path(tmp) / "inv.tsv"
            self._write_tsv([{
                "location": "internal",
                "path": "/storage/emulated/0/Music/X/01.mp3",
                "size": "1000", "codec": "mp3", "sample_rate": "44100",
                "bits": "16", "channels": "2", "bit_rate": "256000",
                "duration_s": "180", "title": "T", "artist": "A",
                "album_artist": "A", "album": "B", "track": "1",
                "disc": "1", "year": "2024", "genre": "Hip-Hop", "composer": "",
            }], inv_path)
            plan_path = Path(tmp) / "plan.json"
            r = subprocess.run(
                [sys.executable, "walkman_inventory_repair.py",
                 "--inventory", str(inv_path), "--plan", str(plan_path),
                 "--summary-only"],
                capture_output=True, text=True, cwd=".",
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(plan_path.exists())
            data = json.loads(plan_path.read_text(encoding="utf-8"))
            for k in ("total_rows", "exact_dupes", "blank_metadata",
                      "codec_ext_mismatch", "placeholder_year",
                      "summary", "recommended_actions"):
                self.assertIn(k, data, f"JSON plan missing key: {k}")

    def test_L5_4d_cli_strict_mode_exits_2_on_findings(self):
        """--strict must exit 2 when findings exist."""
        r = subprocess.run(
            [sys.executable, "walkman_inventory_repair.py",
             "--inventory", "/mnt/user-data/uploads/walkman_inventory.tsv",
             "--summary-only", "--strict"],
            capture_output=True, text=True, cwd=".",
        )
        # The operator's inventory has 200+ dupe groups → findings exist → exit 2
        self.assertEqual(r.returncode, 2,
                          f"--strict should exit 2 when findings exist; got {r.returncode}")


# ═══════════════════════════════════════════════════════════════════════════
# L5 — Verification against actual operator inventory
# ═══════════════════════════════════════════════════════════════════════════

class TestActualInventoryFindings(unittest.TestCase):
    """Run the classifier against /mnt/user-data/uploads/walkman_inventory.tsv
    and confirm the findings match what the Council reported."""

    def test_L5_actual_inventory_matches_council_findings(self):
        if "walkman_inventory_repair" in sys.modules:
            del sys.modules["walkman_inventory_repair"]
        import walkman_inventory_repair as wir
        rows = wir.read_inventory_tsv(
            Path("/mnt/user-data/uploads/walkman_inventory.tsv"))
        report = wir.classify_inventory(rows)
        s = report.summary

        # Council §4.1: "hundreds of duplicate tracks" — must be ≥100
        self.assertGreater(s["exact_dupe_redundant"], 100,
                              f"expected ≥100 redundant dupes per Council §4.1; "
                              f"got {s['exact_dupe_redundant']}")
        # Council §4.2: "Thousands of tracks exhibit severe metadata deficiencies"
        # We classify by hard nulls only (not the soft criteria like 'Unknown'
        # artist), so blank_metadata may be smaller than thousands — but must
        # be >100 to confirm the issue exists at scale.
        self.assertGreater(s["blank_metadata"], 100,
                              f"expected ≥100 blank-metadata rows; "
                              f"got {s['blank_metadata']}")
        # Cross-storage dupes — the Council called out Nemzzz + Lil Uzi Vert
        # being on BOTH internal and SD with identical content.
        self.assertGreater(s["cross_storage_dupes"], 100,
                              f"expected ≥100 cross-storage dupes per "
                              f"Council §4.1; got {s['cross_storage_dupes']}")


# ═══════════════════════════════════════════════════════════════════════════
# Non-regression: prior L4 + Phase I still intact
# ═══════════════════════════════════════════════════════════════════════════

class TestNonRegression(unittest.TestCase):
    def setUp(self):
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]
        os.environ.pop("DEDUP_STRICT_MODE", None)

    def test_L5_NR_phase_I_forensic_9_of_9(self):
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

    def test_L5_NR_planner_repo_lint_still_clean(self):
        """L5 changes don't introduce bare-string planner calls."""
        from sync_planner_argument_audit import run_audit
        report = run_audit(Path("."))
        self.assertEqual(report.violations, [],
                          f"L4.2 linter now flags: {report.violations}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
