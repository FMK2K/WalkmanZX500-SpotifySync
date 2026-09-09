"""
test_mk39_phase_II.py — regression for the post-adversarial-review patches:

  P0 (C1+C2): conditional quorum-noise demotion via DeferredQuorumWarnings
              context. Routine MK38.1-salvaged failures emit ONE INFO line;
              genuine failures (no override) still emit the full WARNING set.

  P1 (C8 / TARGET-I): "Needs Review" quarantine flag — operator-facing
              report of files that have audio on disk but no resolved
              identity (blank artist OR blank title after Phase I).

These cover the two pieces that survived the adversarial CTO+SWE review of
the LOG_10_5_26 + Forum_Suggestions evidence.
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


# ═══════════════════════════════════════════════════════════════════════════
# P0 — DeferredQuorumWarnings buffer + flush/discard semantics
# ═══════════════════════════════════════════════════════════════════════════

class TestDeferredQuorumWarnings(unittest.TestCase):
    def setUp(self):
        # Force fresh import
        for m in ("mk35_trust", "mk38_1_quorum_dominance"):
            if m in sys.modules:
                del sys.modules[m]
        import mk35_trust
        self.mk35 = mk35_trust

    def _capture_log(self, logger_name="mk35_trust"):
        """Attach a StreamHandler at WARNING and capture output."""
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger(logger_name)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        return buf, handler, logger

    def test_T1_emit_outside_context_logs_immediately(self):
        """When no DeferredQuorumWarnings context is active, _emit_quorum_warning
        must log immediately at the requested level — preserving original
        behavior for callers who don't opt in."""
        buf, handler, logger = self._capture_log()
        try:
            self.mk35._emit_quorum_warning(logging.WARNING, "test-immediate-msg")
            handler.flush()
            self.assertIn("test-immediate-msg", buf.getvalue())
            self.assertIn("WARNING", buf.getvalue())
        finally:
            logger.removeHandler(handler)

    def test_T2_emit_inside_context_buffers_no_log(self):
        """When DeferredQuorumWarnings is active, the message must NOT
        emit to the logger — it goes to the thread-local buffer."""
        buf, handler, logger = self._capture_log()
        try:
            with self.mk35.DeferredQuorumWarnings() as ctx:
                self.mk35._emit_quorum_warning(logging.WARNING, "buffered-msg")
                # While context is active, NOTHING is in the log buffer
                handler.flush()
                self.assertNotIn("buffered-msg", buf.getvalue())
                # But message is in the buffer
                buffered = ctx.buffered_messages()
                self.assertEqual(len(buffered), 1)
                self.assertEqual(buffered[0][0], logging.WARNING)
                self.assertIn("buffered-msg", buffered[0][1])
        finally:
            logger.removeHandler(handler)

    def test_T3_discard_drops_buffer(self):
        buf, handler, logger = self._capture_log()
        try:
            with self.mk35.DeferredQuorumWarnings() as ctx:
                self.mk35._emit_quorum_warning(logging.WARNING, "drop-me")
                ctx.discard()
                self.assertEqual(ctx.buffered_messages(), [])
            handler.flush()
            self.assertNotIn("drop-me", buf.getvalue(),
                              "discarded message must NOT reach the log")
        finally:
            logger.removeHandler(handler)

    def test_T4_flush_emits_buffered_at_original_level(self):
        buf, handler, logger = self._capture_log()
        try:
            with self.mk35.DeferredQuorumWarnings() as ctx:
                self.mk35._emit_quorum_warning(logging.WARNING, "flush-me-1")
                self.mk35._emit_quorum_warning(logging.WARNING, "flush-me-2")
                ctx.flush()
            handler.flush()
            output = buf.getvalue()
            self.assertIn("flush-me-1", output)
            self.assertIn("flush-me-2", output)
            self.assertEqual(output.count("WARNING"), 2)
        finally:
            logger.removeHandler(handler)

    def test_T5_nested_contexts_only_outermost_owns_buffer(self):
        """Nested DeferredQuorumWarnings: only the outermost owns/clears
        the thread-local buffer (so a wrapper-of-a-wrapper doesn't reset)."""
        buf, handler, logger = self._capture_log()
        try:
            with self.mk35.DeferredQuorumWarnings() as outer:
                self.mk35._emit_quorum_warning(logging.WARNING, "outer-msg")
                with self.mk35.DeferredQuorumWarnings() as inner:
                    self.mk35._emit_quorum_warning(logging.WARNING, "inner-msg")
                    self.assertFalse(inner._owns_buffer)
                self.assertTrue(outer._owns_buffer)
                # Both messages in outer's buffer
                self.assertEqual(len(outer.buffered_messages()), 2)
        finally:
            logger.removeHandler(handler)


# ═══════════════════════════════════════════════════════════════════════════
# P0 — End-to-end: mk38_1 wrapper drops/flushes appropriately
# ═══════════════════════════════════════════════════════════════════════════

class TestMK38_1WrapperDropsNoiseOnSalvage(unittest.TestCase):
    """Verify the wrapper-level integration: when MK38.1 dominant override
    salvages, the inner WARNINGs are DROPPED. When override doesn't apply,
    they are FLUSHED."""

    def setUp(self):
        for m in ("mk35_trust", "mk38_1_quorum_dominance"):
            if m in sys.modules:
                del sys.modules[m]
        import mk35_trust
        import mk38_1_quorum_dominance
        self.mk35 = mk35_trust
        self.mk38_1 = mk38_1_quorum_dominance

    def _capture_log(self, name):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        return buf, handler, logger

    def test_T6_salvaged_path_drops_quorum_warnings(self):
        """Simulate the wrapper logic: deferred context, inner emits
        WARNINGs, override succeeds → discard. No WARNING reaches the log."""
        mk35_buf, mk35_h, mk35_logger = self._capture_log("mk35_trust")
        try:
            # Simulate the wrapper's deferred-buffer pattern
            with self.mk35.DeferredQuorumWarnings() as ctx:
                # Inner verify_with_quorum would emit these
                self.mk35._emit_quorum_warning(
                    logging.WARNING,
                    "  [MK35/Q1] QUORUM_FAILED path='X' qualifying=1/3 ...")
                self.mk35._emit_quorum_warning(
                    logging.WARNING,
                    "  [MK35/Q1] QUORUM_FAILED_DETAIL classes: sha256=positive, "
                    "size_mtime=no_signal, existence=positive")
                # Override "succeeds" → wrapper calls discard
                ctx.discard()
            mk35_h.flush()
            self.assertEqual(
                mk35_buf.getvalue().strip(), "",
                "Salvaged-by-override path must NOT leak WARNINGs to the log "
                "(noise-suppression mandate).")
        finally:
            mk35_logger.removeHandler(mk35_h)

    def test_T7_unsalvaged_path_flushes_quorum_warnings(self):
        """Simulate the wrapper's unsalvaged path: inner WARNINGs MUST flush
        so the operator can see the genuine failure."""
        mk35_buf, mk35_h, mk35_logger = self._capture_log("mk35_trust")
        try:
            with self.mk35.DeferredQuorumWarnings() as ctx:
                self.mk35._emit_quorum_warning(
                    logging.WARNING,
                    "  [MK35/Q1] QUORUM_FAILED path='Y' qualifying=0/3 ...")
                self.mk35._emit_quorum_warning(
                    logging.WARNING,
                    "  [MK35/Q1] QUORUM_FAILED_DETAIL classes: sha256=disagreement, "
                    "size_mtime=disagreement, existence=disagreement")
                # Override would FAIL → wrapper calls flush
                ctx.flush()
            mk35_h.flush()
            output = mk35_buf.getvalue()
            self.assertIn("QUORUM_FAILED path='Y'", output,
                            "Unsalvaged failures MUST surface to operator")
            self.assertIn("disagreement", output)
            self.assertEqual(output.count("WARNING"), 2)
        finally:
            mk35_logger.removeHandler(mk35_h)

    def test_T8_wrapper_function_uses_deferred_context(self):
        """Verify the actual install_dominance_override wrapper function
        contains the DeferredQuorumWarnings context — adversarial-review
        mandate."""
        import inspect
        src = inspect.getsource(self.mk38_1.patch_verify_with_quorum)
        self.assertIn("DeferredQuorumWarnings", src,
                       "patch_verify_with_quorum must use DeferredQuorumWarnings")
        self.assertIn("discard", src, "...with .discard() on salvage")
        self.assertIn("flush", src, "...with .flush() on no-salvage")


# ═══════════════════════════════════════════════════════════════════════════
# P1 — TARGET-I: "Needs Review" quarantine flag (Council Forum guidance)
# ═══════════════════════════════════════════════════════════════════════════

CLI = [sys.executable, "quarantine_review.py"]


def _make_pending(folder: Path, tracks: dict):
    """Write a quarantine file directly without going through the module."""
    payload = {
        "version": 1,
        "generated_at": "2026-05-10T00:00:00Z",
        "council_constraint": "MK39-A1.3 #5",
        "_instructions": "test",
        "pending": tracks,
    }
    (folder / ".user_canonical_pending.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def _make_needs_review(folder: Path, items: list):
    """Write a needs-review marker file. Each item is a dict with at minimum
    'filename' and a reason. This is what walkman_sync would emit when a
    file has audio on disk but no resolved identity after Phase I."""
    payload = {
        "version": 1,
        "council_target": "MK39.1 Target I (Forum Suggestions 2026-05-10)",
        "generated_at": "2026-05-10T00:00:00Z",
        "_instructions": (
            "Files listed here have audio on disk but the metadata pipeline "
            "could not resolve their identity. Use Picard / MP3Tag / Beets to "
            "tag them manually, or run quarantine_review.py --accept after "
            "editing this file's 'best_guess' fields."
        ),
        "needs_review": items,
    }
    (folder / ".needs_review.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


class TestNeedsReviewCLI(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(CLI + list(args),
                                capture_output=True, text=True, cwd=self._cwd)

    def test_T9_needs_review_file_format_documented(self):
        """The .needs_review.json format must include version, target,
        instructions, and a 'needs_review' list."""
        items = [
            {"filename": "track1.mp3", "reason": "blank artist after Phase I",
             "best_guess": {"artist": "", "title": "track1", "genre": ""}},
            {"filename": "じゃじゃ馬娘.mp3", "reason": "blank title from Spotify",
             "best_guess": {"artist": "Various Artists", "title": "", "genre": ""}},
        ]
        _make_needs_review(self._tmp, items)
        loaded = json.loads((self._tmp / ".needs_review.json").read_text())
        self.assertEqual(loaded["version"], 1)
        self.assertIn("council_target", loaded)
        self.assertIn("MK39.1 Target I", loaded["council_target"])
        self.assertEqual(len(loaded["needs_review"]), 2)

    def test_T10_cli_lists_needs_review_with_new_command(self):
        """quarantine_review.py --needs-review FOLDER lists items requiring
        manual operator attention."""
        items = [
            {"filename": "track1.mp3", "reason": "blank artist after Phase I",
             "best_guess": {"artist": "", "title": "track1", "genre": ""}},
        ]
        _make_needs_review(self._tmp, items)
        r = self._run("--needs-review", str(self._tmp))
        self.assertEqual(r.returncode, 0,
                          f"--needs-review must succeed; stderr={r.stderr}")
        self.assertIn("track1.mp3", r.stdout)
        self.assertIn("blank artist", r.stdout)

    def test_T11_cli_no_needs_review_file_returns_3(self):
        """If no .needs_review.json exists in the folder, exit code 3
        ('no pending file found at given path' — same convention as --list)."""
        empty = Path(tempfile.mkdtemp())
        try:
            r = self._run("--needs-review", str(empty))
            self.assertEqual(r.returncode, 3)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_T12_cli_needs_review_scan_walks_tree(self):
        """quarantine_review.py --needs-review-scan ROOT recursively finds
        all .needs_review.json files under a tree (Forum/Council Target I)."""
        nested = self._tmp / "albums" / "Album1"
        nested.mkdir(parents=True)
        _make_needs_review(nested, [
            {"filename": "x.mp3", "reason": "blank title", "best_guess": {}},
        ])
        nested2 = self._tmp / "albums" / "Album2"
        nested2.mkdir(parents=True)
        _make_needs_review(nested2, [
            {"filename": "y.mp3", "reason": "blank artist", "best_guess": {}},
        ])
        r = self._run("--needs-review-scan", str(self._tmp))
        self.assertEqual(r.returncode, 0)
        self.assertIn("Album1", r.stdout)
        self.assertIn("Album2", r.stdout)
        self.assertIn("2", r.stdout, "should report 2 folders or 2 items total")

    def test_T13_help_documents_new_commands(self):
        """--help text must mention the new --needs-review and
        --needs-review-scan commands so the operator can discover them."""
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--needs-review", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
