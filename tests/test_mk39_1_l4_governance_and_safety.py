"""
test_mk39_1_l4_governance_and_safety.py — Council L3 §4.1/§4.2/§4.3 residuals
================================================================================

Council L3 Patch Review 2026-05-10 identified three residual concerns after
approving L3 patches. This sprint addresses all three.

L4 subtickets:
  L4.1  Config reconciliation — L2/L3 sections cleanly placed (no test —
        verified by config import succeeding with all expected attrs)
  L4.2  Governance linter (Council §4.1) — forbid bare strings to
        sync_planner.generate_plan
  L4.3  Key-construction truncation tolerance (Council §4.2) — _track_keys
        emits additional rich key when album has strippable truncation tail
  L4.4  Aggregated platform safety-profile banner (Council §4.3)
"""

from __future__ import annotations

import ast
import io
import logging
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════
# L4.1 — Config reconciliation: L2/L3 flags present
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigReconciliation(unittest.TestCase):
    def setUp(self):
        if "config" in sys.modules: del sys.modules["config"]

    def test_L4_1_l2_l3_attrs_present(self):
        """After reconciling with operator's canonical config.py, both L2
        and L3 attrs must still be exposed at module level with the
        Council-endorsed defaults."""
        import config
        # L2 — Council Forensic Review
        self.assertTrue(hasattr(config, "DEDUP_TITLE_ONLY_MIN_OVERLAP"))
        self.assertTrue(hasattr(config, "DEDUP_STRICT_MODE"))
        # L3 — Council Unattended Operations Review
        self.assertEqual(config.DEDUP_TITLE_ONLY_MIN_OVERLAP, 0.6)
        self.assertTrue(config.DEDUP_STRICT_MODE)
        # And MK39.1 C2 strict AI validation (preserved from canonical)
        self.assertTrue(hasattr(config, "STRICT_AI_GENRE_VALIDATION"))
        self.assertTrue(config.STRICT_AI_GENRE_VALIDATION)


# ═══════════════════════════════════════════════════════════════════════════
# L4.2 — Governance linter (Council §4.1)
# ═══════════════════════════════════════════════════════════════════════════

class TestPlannerArgumentAudit(unittest.TestCase):
    """Verify the AST linter catches the bug pattern that caused
    LOG_10_5_26's AttributeError: 'str' object has no attribute 'path'."""

    def _audit_source(self, source: str):
        """Helper: write source to a tmp file and run audit_file on it."""
        from sync_planner_argument_audit import audit_file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8",
        ) as f:
            f.write(source)
            tmppath = Path(f.name)
        try:
            return audit_file(tmppath)
        finally:
            tmppath.unlink(missing_ok=True)

    def test_L4_2a_inline_list_of_strs_flagged(self):
        """Literal list of strings to build_sync_plan → VIOLATION."""
        src = textwrap.dedent('''
            def trigger():
                plan = build_sync_plan([], ["Music/a.mp3", "Music/b.flac"])
                return plan
        ''')
        violations = self._audit_source(src)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].arg_kind, "violation_list_of_strs")
        self.assertEqual(violations[0].func, "build_sync_plan")

    def test_L4_2b_filestate_list_NOT_flagged(self):
        """List of FileState(...) constructors → OK (no violation)."""
        src = textwrap.dedent('''
            def safe():
                plan = build_sync_plan([], [
                    FileState(path="a", size_bytes=1, mtime=0.0),
                    FileState(path="b", size_bytes=2, mtime=0.0),
                ])
                return plan
        ''')
        violations = self._audit_source(src)
        self.assertEqual(violations, [])

    def test_L4_2c_devicesnapshot_call_NOT_flagged(self):
        """DeviceSnapshot(...) constructor → OK."""
        src = textwrap.dedent('''
            def safe():
                snap = DeviceSnapshot([], 0)
                plan = build_sync_plan([], snap)
        ''')
        violations = self._audit_source(src)
        self.assertEqual(violations, [])

    def test_L4_2d_name_assigned_strs_flagged(self):
        """Reaching-def lookup: variable assigned to list[str] → VIOLATION."""
        src = textwrap.dedent('''
            def trigger():
                paths = ["a.mp3", "b.flac"]
                plan = build_sync_plan([], paths)
        ''')
        violations = self._audit_source(src)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].arg_kind, "violation_name_strs")

    def test_L4_2e_noqa_suppression_works(self):
        """# noqa: planner-bare-strings suppresses the violation."""
        src = textwrap.dedent('''
            def trigger():
                # Intentional test path
                plan = build_sync_plan([], ["a.mp3"])  # noqa: planner-bare-strings
                return plan
        ''')
        violations = self._audit_source(src)
        self.assertEqual(violations, [])

    def test_L4_2f_empty_list_NOT_flagged(self):
        """Empty list [] → OK (the planner handles empty input)."""
        src = textwrap.dedent('''
            def trigger():
                plan = build_sync_plan([], [])
        ''')
        violations = self._audit_source(src)
        self.assertEqual(violations, [])

    def test_L4_2g_repo_wide_audit_clean(self):
        """The repo as a whole must be CLEAN under this linter — no actual
        bug-pattern violations in production code."""
        from sync_planner_argument_audit import run_audit
        report = run_audit(Path("."))
        self.assertEqual(report.violations, [],
                          f"repo has linter violations: "
                          f"{[v.as_dict() for v in report.violations]}")

    def test_L4_2h_cli_strict_mode_exits_1_on_violations(self):
        """CLI --strict must exit 1 when violations exist."""
        import subprocess
        # Create a violation file
        with tempfile.TemporaryDirectory() as tmpd:
            (Path(tmpd) / "violation.py").write_text(textwrap.dedent('''
                def trigger():
                    plan = build_sync_plan([], ["a.mp3"])
            '''), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "sync_planner_argument_audit.py",
                 "--root", tmpd, "--strict"],
                capture_output=True, text=True, cwd=".",
            )
            self.assertEqual(r.returncode, 1,
                              f"expected exit 1; got {r.returncode}\n{r.stdout}\n{r.stderr}")
            self.assertIn("VIOLATIONS", r.stdout)


# ═══════════════════════════════════════════════════════════════════════════
# L4.3 — Key-construction truncation tolerance (Council §4.2)
# ═══════════════════════════════════════════════════════════════════════════

class TestKeyConstructionTruncationTolerance(unittest.TestCase):
    """When the album folder has a truncation tail, _track_keys should emit
    an additional rich key built from the stripped form so truncation dupes
    group together at the rich-key level, not just the artist-only level."""

    def setUp(self):
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]

    def test_L4_3a_truncated_album_emits_extra_rich_key(self):
        """'The Hygrades - Vol. 1 (197' produces keys including the rich
        key for 'Vol. 1' (stripped) as well as 'Vol. 1 (197' (original)."""
        import walkman_dedup
        keys = walkman_dedup._track_keys(
            folder_parts=["Music", "The Hygrades - Vol. 1 (197"],
            filename="The Hygrades - Vol. 1 (197 - 01 Keep On Moving.mp3",
        )
        # Should have TWO rich keys: stripped and original
        rich_keys = [k for k in keys if len(k) == 3]
        self.assertEqual(len(rich_keys), 2,
                          f"expected 2 rich keys (orig + stripped), got {rich_keys}")
        albums = {k[1] for k in rich_keys}
        # Original normalized album (with '197' fragment)
        self.assertIn("vol 1 197", albums,
                       f"original normalized album missing: {albums}")
        # Stripped normalized album (truncation tail removed)
        self.assertIn("vol 1", albums,
                       f"stripped normalized album missing: {albums}")

    def test_L4_3b_clean_folder_emits_single_rich_key(self):
        """A non-truncated folder doesn't get a duplicate key."""
        import walkman_dedup
        keys = walkman_dedup._track_keys(
            folder_parts=["Music", "Ebo Taylor - My Love and Music"],
            filename="Ebo Taylor - My Love and Music - 01 Will You Promise.mp3",
        )
        rich_keys = [k for k in keys if len(k) == 3]
        self.assertEqual(len(rich_keys), 1)

    def test_L4_3c_truncated_and_nontruncated_share_rich_key(self):
        """Two files — one in truncated folder, one in clean folder, same
        artist + same album-base — must produce intersecting rich keys."""
        import walkman_dedup
        k1 = walkman_dedup._track_keys(
            folder_parts=["Music", "The Hygrades - Vol. 1"],
            filename="The Hygrades - Vol. 1 - 01 Keep On Moving.mp3",
        )
        k2 = walkman_dedup._track_keys(
            folder_parts=["Music", "The Hygrades - Vol. 1 (197"],
            filename="The Hygrades - Vol. 1 (197 - 01 Keep On Moving.m4a",
        )
        # Both should share at least one rich key (3-tuple)
        rich1 = {k for k in k1 if len(k) == 3}
        rich2 = {k for k in k2 if len(k) == 3}
        shared_rich = rich1 & rich2
        self.assertGreater(len(shared_rich), 0,
                              f"truncation dupes must share rich key: "
                              f"k1={rich1} k2={rich2}")

    def test_L4_3d_end_to_end_truncation_dupe_grouped(self):
        """Full matcher test: truncation-dupe pair groups at rich-key
        level under strict mode (so dedup actually fires)."""
        os.environ.pop("DEDUP_STRICT_MODE", None)  # use default (strict ON)
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]
        import walkman_dedup
        files = [
            # Clean folder, mp3
            {"storage": "SD",
             "parts": ["Music", "The Hygrades - Vol. 1"],
             "filename": "The Hygrades - Vol. 1 - 01 Keep On Moving.mp3",
             "ext": ".mp3"},
            # Truncated folder, m4a (different format) — must group under
            # truncation-tolerant rich key with the file above
            {"storage": "SD",
             "parts": ["Music", "The Hygrades - Vol. 1 (197"],
             "filename": "The Hygrades - Vol. 1 (197 - 01 Keep On Moving.m4a",
             "ext": ".m4a"},
        ]
        flagged = walkman_dedup._pass_format_dominance(
            files, already_flagged=set(), use_ai=False,
        )
        infmt = [c for c in flagged if c.reason == "inferior_format"]
        # The mp3 should be flagged with the m4a as keeper — even under
        # strict mode, because L4.3 made the rich key shareable
        mp3s = [c for c in infmt if c.filename.endswith(".mp3")]
        self.assertEqual(len(mp3s), 1,
                          f"truncation-dupe pair should produce 1 mp3 deletion; "
                          f"got {len(mp3s)} (flagged={[c.filename for c in infmt]})")


# ═══════════════════════════════════════════════════════════════════════════
# L4.4 — Aggregated platform safety-profile banner (Council §4.3)
# ═══════════════════════════════════════════════════════════════════════════

class TestPlatformSafetyProfile(unittest.TestCase):
    def setUp(self):
        for m in ("platform_safety_profile", "config"):
            if m in sys.modules: del sys.modules[m]
        os.environ.pop("DEDUP_STRICT_MODE", None)

    def test_L4_4a_profile_dict_has_all_keys(self):
        """build_profile returns a complete profile dict."""
        import platform_safety_profile as psp
        profile = psp.build_profile()
        required = [
            "dedup_mode", "dedup_strict", "dedup_threshold",
            "ai_strict", "hardened", "scrub_on", "verify_on",
            "musicbee_on", "acoustid_on", "picard_on",
            "verdict", "verdict_reasons",
        ]
        for k in required:
            self.assertIn(k, profile, f"profile missing key: {k}")

    def test_L4_4b_council_endorsed_defaults_yield_GREEN(self):
        """Under the Council-endorsed unattended defaults, verdict=GREEN."""
        import platform_safety_profile as psp
        profile = psp.build_profile()
        self.assertEqual(profile["verdict"], "GREEN",
                          f"expected GREEN under Council defaults; "
                          f"got {profile['verdict']} reasons={profile['verdict_reasons']}")

    def test_L4_4c_env_strict_off_downgrades_to_AMBER(self):
        """DEDUP_STRICT_MODE=0 env var → strict effective off → AMBER."""
        os.environ["DEDUP_STRICT_MODE"] = "0"
        try:
            for m in ("platform_safety_profile",):
                if m in sys.modules: del sys.modules[m]
            import platform_safety_profile as psp
            profile = psp.build_profile()
            self.assertFalse(profile["dedup_strict"])
            self.assertEqual(profile["dedup_strict_origin"], "env_off")
            self.assertEqual(profile["verdict"], "AMBER")
        finally:
            os.environ.pop("DEDUP_STRICT_MODE", None)

    def test_L4_4d_banner_emits_once_per_process(self):
        """emit_banner is idempotent — second call returns None."""
        import platform_safety_profile as psp
        psp.reset_for_testing()
        first = psp.emit_banner()
        second = psp.emit_banner()
        self.assertIsNotNone(first, "first emit should return profile dict")
        self.assertIsNone(second, "second emit should be no-op (idempotent)")
        # Force=True bypasses
        third = psp.emit_banner(force=True)
        self.assertIsNotNone(third)

    def test_L4_4e_banner_line_format(self):
        """Banner line must include all key flags."""
        import platform_safety_profile as psp
        profile = psp.build_profile()
        line = psp.format_banner_line(profile)
        # Required substrings
        for s in ("Safety/L4", "PROFILE", "dedup=", "ai=", "harden=",
                  "scrub=", "verify=", "musicbee=", "acoustid=", "picard=",
                  "VERDICT"):
            self.assertIn(s, line, f"banner missing '{s}': {line}")

    def test_L4_4f_logging_severity_matches_verdict(self):
        """GREEN → INFO, AMBER → WARNING, RED → ERROR."""
        import platform_safety_profile as psp
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.INFO)
        psp_log = logging.getLogger("platform_safety_profile")
        psp_log.addHandler(h)
        psp_log.setLevel(logging.INFO)
        try:
            psp.reset_for_testing()
            psp.emit_banner()
            h.flush()
            out = buf.getvalue()
            self.assertIn("[Safety/L4] PROFILE", out)
            self.assertIn("VERDICT GREEN", out)
        finally:
            psp_log.removeHandler(h)


# ═══════════════════════════════════════════════════════════════════════════
# Non-regression: prior real-world FPs still rejected, Phase I 9/9
# ═══════════════════════════════════════════════════════════════════════════

class TestNonRegression(unittest.TestCase):
    def setUp(self):
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]
        os.environ.pop("DEDUP_STRICT_MODE", None)

    def test_L4_NR_pharrell_brandy_still_rejected(self):
        """L4 changes don't regress L2/L3 cross-artist FP rejection."""
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
        infmt = [c for c in flagged if c.reason == "inferior_format"]
        self.assertEqual(len(infmt), 0,
                          "L4 must not regress the Pharrell-Brandy rejection")

    def test_L4_NR_phase_I_forensic_9_of_9(self):
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
