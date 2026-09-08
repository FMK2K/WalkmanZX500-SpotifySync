"""
test_mk39_1_l3_council_deferred.py — Council guidance 2026-05-10 (two docs)
================================================================================

Doc 1: "Review of Dedup Safety Patches (D1, D3, D4) & Deferred-Item Status"
  - L2 D1/D3/D4 APPROVED for execute mode.
  - D5 (keeper-path folder truncation in logs) upgraded LATER → P2.
  - Sync planner crash remains P1 (root-cause inside planner).

Doc 2: "Adjustments for Fully Unattended Operation"
  - Operator confirmed fully unattended overnight runs.
  - Council endorses safe-default profile:
      DEDUP_STRICT_MODE = True   (was False)
      DEDUP_TITLE_ONLY_MIN_OVERLAP = 0.6   (was 0.5)
  - One-line startup banner showing current safety profile.

L3 subtickets:
  L3.1  D5 — Keeper-folder cleanliness ranking (prefer non-truncated)
  L3.2  Default flip — DEDUP_STRICT_MODE=True
  L3.3  Threshold raise — DEDUP_TITLE_ONLY_MIN_OVERLAP=0.6
  L3.4  P1 — sync_planner defensive coercion at planner boundary
  L3.5  Unattended-mode startup banner
"""

from __future__ import annotations

import io
import logging
import os
import random
import sys
import unittest
from unittest import mock


# ═══════════════════════════════════════════════════════════════════════════
# L3.2 + L3.3 — New Council-endorsed defaults
# ═══════════════════════════════════════════════════════════════════════════

class TestUnattendedDefaults(unittest.TestCase):
    """Council Unattended Operations Review 2026-05-10 endorsed defaults."""

    def setUp(self):
        for m in ("config",):
            if m in sys.modules: del sys.modules[m]

    def test_L3_2_strict_mode_default_is_True(self):
        """Council mandated: DEDUP_STRICT_MODE must default to True."""
        import config
        self.assertTrue(config.DEDUP_STRICT_MODE,
                          "Council unattended-profile: DEDUP_STRICT_MODE must default True")

    def test_L3_3_threshold_default_is_06(self):
        """Council mandated: DEDUP_TITLE_ONLY_MIN_OVERLAP raised to 0.6."""
        import config
        self.assertEqual(config.DEDUP_TITLE_ONLY_MIN_OVERLAP, 0.6,
                          "Council unattended-profile: threshold must be 0.6 (was 0.5)")

    def test_L3_2b_dedup_mode_remains_active(self):
        """DEDUP_MODE = 'active' for unattended runs (was already so)."""
        import config
        self.assertEqual(config.DEDUP_MODE, "active")


# ═══════════════════════════════════════════════════════════════════════════
# L3.5 — Startup banner emits at scan start
# ═══════════════════════════════════════════════════════════════════════════

class TestUnattendedStartupBanner(unittest.TestCase):
    def setUp(self):
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setLevel(logging.INFO)
        logging.getLogger("walkman_dedup").addHandler(self.h)
        logging.getLogger("walkman_dedup").setLevel(logging.INFO)

    def tearDown(self):
        logging.getLogger("walkman_dedup").removeHandler(self.h)

    def test_L3_5_banner_emits_with_safety_profile(self):
        """Single INFO line should fire each pass: mode/strict/threshold."""
        import walkman_dedup
        # Trigger _pass_format_dominance — empty files is fine for banner test
        walkman_dedup._pass_format_dominance([], already_flagged=set(), use_ai=False)
        self.h.flush()
        out = self.buf.getvalue()
        self.assertIn("UNATTENDED_PROFILE", out)
        self.assertIn("strict_mode=True", out)
        self.assertIn("threshold=0.60", out)
        self.assertIn("mode=active", out)


# ═══════════════════════════════════════════════════════════════════════════
# L3.1 — Keeper-folder cleanliness ranking (D5, P2)
# ═══════════════════════════════════════════════════════════════════════════

class TestKeeperFolderRanking(unittest.TestCase):
    """When multiple same-format keepers exist, prefer non-truncated folder."""

    def setUp(self):
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]
        # Disable strict mode for these specific tests (we want the matcher
        # to actually run the format-dominance path).
        os.environ["DEDUP_STRICT_MODE"] = "0"

    def tearDown(self):
        os.environ.pop("DEDUP_STRICT_MODE", None)

    def test_L3_1_truncated_folder_NOT_preferred_as_keeper(self):
        """Two .m4a keepers exist: one in clean 'Vol. 1', one in truncated
        'Vol. 1 (197'. Keeper should be the CLEAN one. Truncated one should
        be flagged for deletion (same format, just messy folder)."""
        import walkman_dedup
        files = [
            # Clean folder keeper
            {"storage": "SD",
             "parts": ["Music", "The Hygrades - Vol. 1"],
             "filename": "The Hygrades - Vol. 1 - 01 Keep On Moving.m4a",
             "ext": ".m4a"},
            # Truncated folder same format — should NOT be the preferred keeper
            {"storage": "SD",
             "parts": ["Music", "The Hygrades - Vol. 1 (197"],
             "filename": "The Hygrades - Vol. 1 (197 - 01 Keep On Moving.m4a",
             "ext": ".m4a"},
            # The mp3 in the clean folder
            {"storage": "SD",
             "parts": ["Music", "The Hygrades - Vol. 1"],
             "filename": "The Hygrades - Vol. 1 - 01 Keep On Moving.mp3",
             "ext": ".mp3"},
        ]
        flagged = walkman_dedup._pass_format_dominance(
            files, already_flagged=set(), use_ai=False,
        )
        infmt = [c for c in flagged if getattr(c, "reason", "") == "inferior_format"]
        # The mp3 should be flagged with the CLEAN folder keeper, not the truncated one
        mp3_candidates = [c for c in infmt if c.filename.endswith(".mp3")]
        self.assertEqual(len(mp3_candidates), 1,
                          f"expected 1 mp3 flagged, got {len(mp3_candidates)}")
        keep_path = mp3_candidates[0].superior_path
        # Critical assertion: keeper path must reference the CLEAN folder
        self.assertIn("Vol. 1/", keep_path,
                       f"keeper must use clean folder; got {keep_path}")
        self.assertNotIn("Vol. 1 (197/", keep_path,
                          f"keeper must NOT use truncated folder; got {keep_path}")


# ═══════════════════════════════════════════════════════════════════════════
# L3.4 — Sync planner defensive coercion (P1, root-cause inside planner)
# ═══════════════════════════════════════════════════════════════════════════

class TestSyncPlannerDefensiveCoercion(unittest.TestCase):
    """Council P1: bug should be fixed INSIDE the planner, not just at caller."""

    def setUp(self):
        for m in ("sync_planner", "sync_operations"):
            if m in sys.modules: del sys.modules[m]
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setLevel(logging.WARNING)
        logging.getLogger("sync_planner").addHandler(self.h)
        logging.getLogger("sync_planner").setLevel(logging.WARNING)

    def tearDown(self):
        logging.getLogger("sync_planner").removeHandler(self.h)

    def test_L3_4_planner_coerces_bare_strings_with_warning(self):
        """Pass a DeviceSnapshot containing bare strings (the bug scenario).
        Planner must NOT crash; must coerce + emit WARNING."""
        from sync_operations import DeviceSnapshot
        from sync_planner import build_sync_plan
        # Simulate the LOG_10_5_26 scenario: device_files contains strings
        # (because someone passed list(device_index.files) where files is
        # dict[path→size]).
        # We use a list directly (which the planner accepts as raw list
        # for backward compat), but containing strings.
        snapshot = ["Music/a.mp3", "Music/b.flac", "Music/c.m4a"]
        # Must NOT raise AttributeError
        plan = build_sync_plan([], snapshot)
        self.assertIsNotNone(plan)
        # And the WARNING must fire
        self.h.flush()
        out = self.buf.getvalue()
        self.assertIn("DEFENSIVE_COERCION", out)
        self.assertIn("3 bare path string", out)

    def test_L3_4b_mixed_list_works(self):
        """Mixed FileState + strings — both paths handled."""
        from sync_operations import DeviceSnapshot, FileState
        from sync_planner import build_sync_plan
        snapshot = [
            FileState(path="Music/a.mp3", size_bytes=1000, mtime=0.0),
            "Music/b.flac",
            FileState(path="Music/c.m4a", size_bytes=5000, mtime=0.0),
        ]
        plan = build_sync_plan([], snapshot)
        self.assertIsNotNone(plan)


# ═══════════════════════════════════════════════════════════════════════════
# L3-INTEGRATION — Unattended mode + strict gate: real false positives
# stay rejected; legit format upgrades still fire.
# ═══════════════════════════════════════════════════════════════════════════

class TestUnattendedIntegration(unittest.TestCase):
    """Verify L3 defaults end-to-end: strict mode ON catches FPs AND
    still cleans legit same-album format upgrades."""

    def setUp(self):
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]
        os.environ.pop("DEDUP_STRICT_MODE", None)

    def test_L3_int_pharrell_brandy_still_rejected_under_strict(self):
        """Strict mode keeps the Pharrell-vs-Brandy FP rejection working."""
        import walkman_dedup
        files = [
            {"storage": "SD",
             "parts": ["Music", "Brandy - Brandy"],
             "filename": "Brandy - Brandy - 01 Best Friend.m4a", "ext": ".m4a"},
            {"storage": "SD",
             "parts": ["Music", "Pharrell Williams - In My Mind (Deluxe Edition)"],
             "filename": "Pharrell Williams - In My Mind (Deluxe Edition) - 04 Best Friend.mp3", "ext": ".mp3"},
        ]
        flagged = walkman_dedup._pass_format_dominance(files, set(), use_ai=False)
        infmt = [c for c in flagged if getattr(c, "reason", "") == "inferior_format"]
        self.assertEqual(len(infmt), 0,
                          "strict mode must still reject Pharrell-vs-Brandy FP")

    def test_L3_int_legit_same_album_upgrade_still_works_under_strict(self):
        """Legit Ebo Taylor mp3→m4a in same album folder: rich key match
        (artist+album+title) so strict mode DOES pass."""
        import walkman_dedup
        files = [
            {"storage": "SD",
             "parts": ["Music", "Ebo Taylor - My Love and Music"],
             "filename": "Ebo Taylor - My Love and Music - 01 Will You Promise.mp3",
             "ext": ".mp3"},
            {"storage": "SD",
             "parts": ["Music", "Ebo Taylor - My Love and Music"],
             "filename": "Ebo Taylor - My Love and Music - 01 Will You Promise.m4a",
             "ext": ".m4a"},
        ]
        flagged = walkman_dedup._pass_format_dominance(files, set(), use_ai=False)
        infmt = [c for c in flagged if getattr(c, "reason", "") == "inferior_format"]
        self.assertEqual(len(infmt), 1,
                          "rich-key format upgrade must still fire under strict mode")
        self.assertTrue(infmt[0].filename.endswith(".mp3"))


# ═══════════════════════════════════════════════════════════════════════════
# MC SIM (L3 update) — verify FP/TP rates under new default profile
# ═══════════════════════════════════════════════════════════════════════════

ARTISTS = [
    "The Weeknd", "Pharrell Williams", "Brandy", "Oliver Nelson",
    "Ahmad Jamal Trio", "Bow Wow", "Chick Corea", "Joe Henderson",
    "Donald Byrd", "Bobbi Humphrey", "Ebo Taylor", "William Onyeabor",
    "Orlando Julius", "The Funkees", "Marijata", "Peter King",
    "USHER", "Jhené Aiko", "Drake", "Frank Ocean", "Kendrick Lamar",
    "Solange", "Erykah Badu", "Sade", "Marvin Gaye", "Stevie Wonder",
    "Miles Davis", "John Coltrane", "Thelonious Monk", "Charles Mingus",
]
ALBUMS = [
    "After Hours", "Heart Break", "Cosmic Spheres", "Stolen Truths",
    "Midnight Suite", "Vol. 1", "Vol. 2", "Live At The Vanguard",
    "The Awakening", "The Blueprint", "Greatest Hits", "Self-Titled",
]


class TestL3MCSimulation(unittest.TestCase):
    def test_L3_mc_sim_threshold_06(self):
        """With threshold 0.6 (raised from 0.5), false-positive rate must
        be ≤ previous run. Same-artist same-album still 100% TP."""
        from walkman_dedup import passes_title_only_gate
        rng = random.Random(0xC0FFEE)
        N = 5000

        fp = 0
        for _ in range(N):
            a1, a2 = rng.sample(ARTISTS, 2)
            b1, b2 = rng.sample(ALBUMS, 2)
            passes, _, _ = passes_title_only_gate(a1, b1, a2, b2, threshold=0.6)
            if passes: fp += 1

        tp = 0
        for _ in range(N):
            a = rng.choice(ARTISTS)
            b = rng.choice(ALBUMS)
            passes, _, _ = passes_title_only_gate(a, b, a, b, threshold=0.6)
            if passes: tp += 1

        fp_rate = fp / N
        tp_rate = tp / N
        print(f"\n  MC SIM L3 (threshold=0.6): "
              f"FP={fp}/{N} ({fp_rate:.4%}) "
              f"TP={tp}/{N} ({tp_rate:.4%})")

        self.assertLess(fp_rate, 0.005,
                          f"FP rate {fp_rate:.4%} exceeds 0.5%")
        self.assertEqual(tp_rate, 1.0,
                          f"same-artist TP rate {tp_rate:.4%} != 100%")


# ═══════════════════════════════════════════════════════════════════════════
# T7 — Non-regression: Phase I forensic 9/9 still resolves
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseINonRegression(unittest.TestCase):
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
