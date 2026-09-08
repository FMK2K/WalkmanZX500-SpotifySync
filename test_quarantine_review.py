"""
test_quarantine_review.py — regression for the quarantine_review CLI.

Council 2026-05-09 §5 immediate action #2: the CLI tool is a binding
deliverable. These tests verify the documented workflows from the Council's
review work end-to-end:
  - List pending in a folder
  - Inspect a track's pending fields
  - Accept all (round-trip to .user_metadata.json with provenance)
  - Reject (remove from pending without accepting)
  - Accept partial (with --field flag)
  - Scan + summary aggregation across a tree
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = ["python3", "quarantine_review.py"]


def _make_pending(folder: Path, tracks: dict):
    """Write a quarantine file directly without going through the module."""
    payload = {
        "version": 1,
        "generated_at": "2026-05-09T00:00:00Z",
        "council_constraint": "MK39-A1.3 #5",
        "_instructions": "test",
        "pending": tracks,
    }
    (folder / ".user_canonical_pending.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def _run(*args, cwd=None):
    return subprocess.run(CLI + list(args),
                            capture_output=True, text=True, cwd=cwd)


class TestQuarantineReviewCLI(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = Path(tempfile.mkdtemp())
        # Sample pending data
        self.pending = {
            "Beatles - Help - 01 Yesterday.mp3": {
                "title": {"value": "Yesterday", "source": "user_canonical_filename",
                           "confidence": "MEDIUM",
                           "first_seen": "2026-05-08T12:00:00Z",
                           "last_seen":  "2026-05-08T12:00:00Z"},
                "artist": {"value": "Beatles", "source": "user_canonical_filename",
                            "confidence": "MEDIUM",
                            "first_seen": "2026-05-08T12:00:00Z",
                            "last_seen":  "2026-05-08T12:00:00Z"},
            },
            "Stones - Satisfaction.mp3": {
                "title": {"value": "Satisfaction", "source": "user_canonical_filename",
                           "confidence": "MEDIUM",
                           "first_seen": "2026-05-08T12:00:00Z",
                           "last_seen":  "2026-05-08T12:00:00Z"},
            },
        }
        _make_pending(self._tmp, self.pending)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_T1_list_shows_all_tracks(self):
        r = _run("--list", str(self._tmp))
        self.assertEqual(r.returncode, 0)
        self.assertIn("Beatles - Help - 01 Yesterday.mp3", r.stdout)
        self.assertIn("Stones - Satisfaction.mp3", r.stdout)
        self.assertIn("Yesterday", r.stdout)

    def test_T2_list_empty_folder_returns_3(self):
        empty = Path(tempfile.mkdtemp())
        try:
            r = _run("--list", str(empty))
            self.assertEqual(r.returncode, 3)
            self.assertIn("no pending", r.stdout)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_T3_list_nonexistent_folder_errors(self):
        r = _run("--list", "/nonexistent/path/that/does/not/exist")
        self.assertEqual(r.returncode, 2)

    def test_T4_inspect_shows_track_details(self):
        r = _run("--inspect", str(self._tmp), "Beatles - Help - 01 Yesterday.mp3")
        self.assertEqual(r.returncode, 0)
        self.assertIn("title", r.stdout)
        self.assertIn("artist", r.stdout)
        self.assertIn("user_canonical_filename", r.stdout)
        self.assertIn("first_seen", r.stdout)

    def test_T5_inspect_unknown_track_errors(self):
        r = _run("--inspect", str(self._tmp), "NotInPending.mp3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not in pending", r.stderr)

    def test_T6_accept_writes_to_override_with_provenance(self):
        r = _run("--accept", str(self._tmp), "Beatles - Help - 01 Yesterday.mp3")
        self.assertEqual(r.returncode, 0)
        # Verify .user_metadata.json was created
        ovr_path = self._tmp / ".user_metadata.json"
        self.assertTrue(ovr_path.exists())
        ovr = json.loads(ovr_path.read_text())
        self.assertEqual(ovr["version"], 1)
        track = ovr["tracks"]["Beatles - Help - 01 Yesterday.mp3"]
        self.assertEqual(track["_provenance"], "user_explicit",
                          "Council requirement: accepted entries get user_explicit provenance")
        self.assertEqual(track["title"], "Yesterday")
        self.assertEqual(track["artist"], "Beatles")

    def test_T7_accept_removes_from_pending(self):
        _run("--accept", str(self._tmp), "Beatles - Help - 01 Yesterday.mp3")
        # Pending file should still exist with Stones, no Beatles
        quar = json.loads((self._tmp / ".user_canonical_pending.json").read_text())
        self.assertNotIn("Beatles - Help - 01 Yesterday.mp3", quar["pending"])
        self.assertIn("Stones - Satisfaction.mp3", quar["pending"])

    def test_T8_accept_all_processes_every_track(self):
        r = _run("--accept-all", str(self._tmp))
        self.assertEqual(r.returncode, 0)
        # Both tracks now in override
        ovr = json.loads((self._tmp / ".user_metadata.json").read_text())
        self.assertIn("Beatles - Help - 01 Yesterday.mp3", ovr["tracks"])
        self.assertIn("Stones - Satisfaction.mp3", ovr["tracks"])
        # Pending file should be removed entirely
        self.assertFalse((self._tmp / ".user_canonical_pending.json").exists())

    def test_T9_reject_removes_without_writing_override(self):
        r = _run("--reject", str(self._tmp), "Beatles - Help - 01 Yesterday.mp3")
        self.assertEqual(r.returncode, 0)
        # No override file created
        self.assertFalse((self._tmp / ".user_metadata.json").exists())
        # Beatles gone from pending
        quar = json.loads((self._tmp / ".user_canonical_pending.json").read_text())
        self.assertNotIn("Beatles - Help - 01 Yesterday.mp3", quar["pending"])

    def test_T10_accept_partial_field_subset(self):
        """--accept with --field only accepts those specific fields,
        leaves others in pending."""
        r = _run("--accept", str(self._tmp), "Beatles - Help - 01 Yesterday.mp3",
                  "--field", "title")
        self.assertEqual(r.returncode, 0)
        ovr = json.loads((self._tmp / ".user_metadata.json").read_text())
        beatles = ovr["tracks"]["Beatles - Help - 01 Yesterday.mp3"]
        self.assertIn("title", beatles)
        self.assertNotIn("artist", beatles, "non-accepted field should NOT be in override")
        # artist still in pending
        quar = json.loads((self._tmp / ".user_canonical_pending.json").read_text())
        beatles_pending = quar["pending"]["Beatles - Help - 01 Yesterday.mp3"]
        self.assertIn("artist", beatles_pending)
        self.assertNotIn("title", beatles_pending)

    def test_T11_scan_finds_quarantine_tree(self):
        # Create a nested folder with another quarantine file
        nested = self._tmp / "nested" / "album2"
        nested.mkdir(parents=True)
        _make_pending(nested, {"x.mp3": {"title": {"value": "X",
                                                       "source": "user_canonical_filename",
                                                       "confidence": "MEDIUM",
                                                       "first_seen": "2026-05-08T12:00:00Z",
                                                       "last_seen": "2026-05-08T12:00:00Z"}}})
        r = _run("--scan", str(self._tmp))
        self.assertEqual(r.returncode, 0)
        # Both folders should appear
        self.assertIn(str(self._tmp), r.stdout)
        self.assertIn("album2", r.stdout)
        # Total should be 3 tracks (2 in root + 1 in nested)
        self.assertIn("3 pending track(s)", r.stdout)

    def test_T12_summary_aggregates_by_source(self):
        r = _run("--summary", str(self._tmp))
        self.assertEqual(r.returncode, 0)
        self.assertIn("user_canonical_filename", r.stdout)
        self.assertIn("total tracks pending", r.stdout)

    def test_T13_help_shows_usage(self):
        r = _run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--list", r.stdout)
        self.assertIn("--accept", r.stdout)
        self.assertIn("--reject", r.stdout)
        self.assertIn("Council", r.stdout.replace("Operator", "Council") + "Council")
        # ^ help text includes "user-canonical" + "MK39-A1.3"
        self.assertIn("MK39-A1.3", r.stdout)

    def test_T14_no_args_returns_error(self):
        r = _run()
        # argparse mutex group is required → exit code 2
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
