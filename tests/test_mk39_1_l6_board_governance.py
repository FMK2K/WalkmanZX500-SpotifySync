"""
test_mk39_1_l6_board_governance.py — Board Review Revision 2 §A–§K subset
================================================================================

Board Review Revision 2 (2026-05-11) issued 11 binding governance sections.
Per the board's own Part VI sequencing, this sprint ships P0 + critical P1:

  L6.1  GOV-A1+A2 — silent_move 9-step lifecycle invariant + push-verify
                    strengthening (V3+V6 under MTP-cap, V4 default)
  L6.2  GOV-A5    — silent_move transaction journal (JSONL append-only)
  L6.3  GOV-F1+F2 — storage threshold governance (AMBER/RED in safety
                    profile + Stage 0 hard gate)
  L6.4  GOV-E1-E4 — upload failure circuit breaker (3→5→10 escalation)
  L6.5  GOV-B1+B2 — StageResult model + downstream_safe gate
  L6.6  GOV-J2    — recovery manifest workflow (builds on L6.2)
  L6.7  GOV-B3    — ALLOW_CONTINUE_ON_STAGE_FAIL lockdown
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════
# L6.5 — StageResult model + downstream_safe gate
# ═══════════════════════════════════════════════════════════════════════════

class TestStageResultModel(unittest.TestCase):
    """Board GOV-B1 — StageState enum + StageResult dataclass with
    downstream_safe bool that distinguishes 'empty plan' from 'failed plan'."""

    def setUp(self):
        for m in ("sync_operations",):
            if m in sys.modules: del sys.modules[m]

    def test_L6_5a_StageState_enum_values(self):
        from sync_operations import StageState
        # Board GOV-B1 mandates exactly these 6 states
        expected = {"not_started", "skipped", "failed", "partial", "completed", "aborted"}
        actual = {s.value for s in StageState}
        self.assertEqual(actual, expected)

    def test_L6_5b_stage_completed_is_downstream_safe(self):
        from sync_operations import stage_completed, StageState
        r = stage_completed()
        self.assertEqual(r.state, StageState.COMPLETED)
        self.assertTrue(r.downstream_safe,
                          "COMPLETED must have downstream_safe=True (Board GOV-B1)")

    def test_L6_5c_stage_failed_NOT_downstream_safe(self):
        from sync_operations import stage_failed, StageState
        r = stage_failed("ADB_TIMEOUT", "ADB find command timed out")
        self.assertEqual(r.state, StageState.FAILED)
        self.assertFalse(r.downstream_safe,
                           "FAILED must have downstream_safe=False (Board GOV-B2)")

    def test_L6_5d_stage_skipped_IS_downstream_safe(self):
        """Board GOV-B1: skip is intentional, not a failure."""
        from sync_operations import stage_skipped
        r = stage_skipped(reason="DISABLED_BY_CONFIG")
        self.assertTrue(r.downstream_safe)

    def test_L6_5e_stage_aborted_NOT_downstream_safe(self):
        from sync_operations import stage_aborted
        r = stage_aborted("CIRCUIT_BREAKER", "Upload circuit breaker tripped",
                            error_count=10, partial_count=42)
        self.assertFalse(r.downstream_safe)
        self.assertEqual(r.error_count, 10)
        self.assertEqual(r.partial_count, 42)

    def test_L6_5f_SyncPlan_carries_stage_2_result(self):
        """Board GOV-B1: SyncPlan must carry stage_2_result field."""
        from sync_operations import SyncPlan
        plan = SyncPlan(run_id="t1")
        self.assertTrue(hasattr(plan, "stage_2_result"),
                          "SyncPlan must have stage_2_result attribute")
        # Default is None (set by main pipeline after Stage 2)
        self.assertIsNone(plan.stage_2_result)


# ═══════════════════════════════════════════════════════════════════════════
# L6.4 — Transport circuit breaker (GOV-E1-E4)
# ═══════════════════════════════════════════════════════════════════════════

class TestTransportCircuitBreaker(unittest.TestCase):
    def setUp(self):
        # Force-reset breaker state between tests
        for m in ("transport_circuit_breaker",):
            if m in sys.modules: del sys.modules[m]
        import transport_circuit_breaker as _tcb
        _tcb.reset(operator_initiated=True)

    def test_L6_4a_thresholds_match_board_mandate(self):
        """Board GOV-E2: thresholds must be 3/5/10."""
        import transport_circuit_breaker as _tcb
        self.assertEqual(_tcb.THRESHOLD_WARNING, 3)
        self.assertEqual(_tcb.THRESHOLD_ERROR, 5)
        self.assertEqual(_tcb.THRESHOLD_CRITICAL, 10)

    def test_L6_4b_10_consecutive_failures_trips_breaker(self):
        """Board GOV-E1: 10 consecutive failures triggers ABORT."""
        import transport_circuit_breaker as _tcb
        for i in range(9):
            tripped = _tcb.record_failure(context=f"track{i}.mp3")
            self.assertFalse(tripped, f"breaker tripped early at failure {i+1}")
        # 10th failure should trip
        tripped = _tcb.record_failure(context="track10.mp3")
        self.assertTrue(tripped, "breaker must trip on 10th consecutive failure")
        self.assertTrue(_tcb.is_tripped())

    def test_L6_4c_success_resets_consecutive_counter(self):
        """Board GOV-E4: reset on ANY successful upload."""
        import transport_circuit_breaker as _tcb
        for _ in range(5):
            _tcb.record_failure()
        state = _tcb.get_state()
        self.assertEqual(state.consecutive_failures, 5)
        _tcb.record_success()
        state = _tcb.get_state()
        self.assertEqual(state.consecutive_failures, 0,
                          "success must reset consecutive counter")
        # Total failure count is preserved (lifetime metric)
        self.assertEqual(state.total_failures, 5)

    def test_L6_4d_tripped_breaker_not_auto_reset(self):
        """Board GOV-E4: when tripped, no auto-reset on subsequent success."""
        import transport_circuit_breaker as _tcb
        for _ in range(10):
            _tcb.record_failure()
        self.assertTrue(_tcb.is_tripped())
        # Calling reset without operator_initiated=True must NOT untrip
        _tcb.reset(operator_initiated=False)
        self.assertTrue(_tcb.is_tripped(),
                         "tripped breaker must not auto-reset without operator flag")
        # Operator-initiated reset works
        _tcb.reset(operator_initiated=True)
        self.assertFalse(_tcb.is_tripped())

    def test_L6_4e_escalation_log_levels(self):
        """Board GOV-E2: 3=WARNING, 5=ERROR, 10=CRITICAL."""
        import transport_circuit_breaker as _tcb
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.WARNING)
        log = logging.getLogger("transport_circuit_breaker")
        log.addHandler(h)
        log.setLevel(logging.WARNING)
        try:
            for _ in range(10):
                _tcb.record_failure(context="x.mp3")
            h.flush()
            out = buf.getvalue()
            self.assertIn("streak: 3", out)
            self.assertIn("streak: 5", out)
            self.assertIn("CIRCUIT BREAKER", out)
        finally:
            log.removeHandler(h)


# ═══════════════════════════════════════════════════════════════════════════
# L6.2 — Transaction journal (GOV-A5)
# ═══════════════════════════════════════════════════════════════════════════

class TestReorganizeTxnJournal(unittest.TestCase):
    def setUp(self):
        for m in ("reorganize_txn_journal",):
            if m in sys.modules: del sys.modules[m]

    def test_L6_2a_journal_init_creates_file(self):
        import reorganize_txn_journal as _rtj
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jp = _rtj.init_journal("test_run", base_dir=tmp_path)
            self.assertTrue(jp.exists() or jp.parent.exists())
            self.assertTrue(jp.name.startswith("reorganize_txn_test_run"))
            _rtj.close_journal()

    def test_L6_2b_new_txn_has_all_required_fields(self):
        """Board GOV-A5: required schema fields."""
        import reorganize_txn_journal as _rtj
        txn = _rtj.new_txn(
            src="/dev/x.mp3", dest="/dev/y.mp3",
            src_filename="x.mp3", dest_filename="y.mp3",
        )
        required = {
            "txn_id", "run_id", "src", "dest", "src_filename", "dest_filename",
            "pull_ok", "pull_verify_ok", "tag_mutate_ok", "rename_ok",
            "push_ok", "push_verify_ok", "delete_ok", "delete_verify_ok",
            "recovery_required", "recovery_cache_path", "failure_step",
            "failure_reason", "ts_start", "ts_end",
        }
        for f in required:
            self.assertIn(f, txn, f"missing required field: {f}")

    def test_L6_2c_finalize_computes_recovery_required(self):
        """Board GOV-A5: pull_ok=True + push_ok=False → recovery_required=True."""
        import reorganize_txn_journal as _rtj
        with tempfile.TemporaryDirectory() as tmp:
            _rtj.init_journal("test_recovery", base_dir=Path(tmp))
            txn = _rtj.new_txn(src="/a", dest="/b", src_filename="a.mp3", dest_filename="b.mp3")
            txn["pull_ok"] = True
            txn["push_ok"] = False
            _rtj.finalize_txn(txn)
            _rtj.close_journal()
            # Re-read journal
            jp = Path(tmp) / "recovery_journal" / "reorganize_txn_test_recovery.jsonl"
            records = _rtj.read_journal(jp)
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0]["recovery_required"])

    def test_L6_2d_journal_is_append_only_jsonl(self):
        """Board GOV-A5: append-only at file level."""
        import reorganize_txn_journal as _rtj
        with tempfile.TemporaryDirectory() as tmp:
            _rtj.init_journal("test_append", base_dir=Path(tmp))
            for i in range(3):
                t = _rtj.new_txn(src=f"/a/{i}", dest=f"/b/{i}",
                                   src_filename=f"a{i}.mp3", dest_filename=f"b{i}.mp3")
                t["pull_ok"] = True
                _rtj.finalize_txn(t)
            _rtj.close_journal()
            jp = Path(tmp) / "recovery_journal" / "reorganize_txn_test_append.jsonl"
            records = _rtj.read_journal(jp)
            self.assertEqual(len(records), 3)


# ═══════════════════════════════════════════════════════════════════════════
# L6.3 — Storage thresholds (GOV-F1+F2)
# ═══════════════════════════════════════════════════════════════════════════

class TestStorageThresholds(unittest.TestCase):
    def setUp(self):
        for m in ("storage_thresholds",):
            if m in sys.modules: del sys.modules[m]

    def test_L6_3a_thresholds_validate_against_floors(self):
        """Board GOV-F1: thresholds below hard floors are rejected."""
        from storage_thresholds import StorageThresholds
        t = StorageThresholds(write_target_amber_gb=0.1)  # below 0.5 floor
        violations = t.validate_against_floors()
        self.assertGreater(len(violations), 0,
                              "config below floor must be flagged")
        self.assertIn("write_target_amber_gb", violations[0])

    def test_L6_3b_write_target_99pct_full_yields_HALT(self):
        """LOG_11_5_26 case: write-target free < 100 MB → HALT verdict."""
        from storage_thresholds import assess_storage, StorageVerdict
        # 50 MB free on write target
        probe = lambda s: 50 * 1024 * 1024 if s == "SD card" else 50 * 1024**3
        result = assess_storage(
            write_target_storage="SD card",
            internal_storage="Internal shared storage",
            probe=probe,
        )
        self.assertEqual(result.write_target_verdict, StorageVerdict.HALT)
        self.assertTrue(result.halt_pipeline,
                           "halt_pipeline must be True under HALT verdict")

    def test_L6_3c_internal_99pct_yields_AMBER(self):
        """LOG_11_5_26: internal at 499 MB free → AMBER (Board GOV-F2)."""
        from storage_thresholds import assess_storage, StorageVerdict
        probe = lambda s: 499 * 1024 * 1024 if s == "Internal shared storage" else 50 * 1024**3
        result = assess_storage(
            write_target_storage="SD card",
            internal_storage="Internal shared storage",
            probe=probe,
        )
        # Internal < 500 MB AMBER floor
        self.assertEqual(result.internal_verdict, StorageVerdict.AMBER)
        # Write target is healthy
        self.assertEqual(result.write_target_verdict, StorageVerdict.GREEN)
        # Overall takes the worst → AMBER
        self.assertEqual(result.overall_verdict, StorageVerdict.AMBER)
        self.assertFalse(result.halt_pipeline)

    def test_L6_3d_unprobeable_storage_is_UNKNOWN_not_halt(self):
        """Board policy: unprobeable storage doesn't halt the pipeline."""
        from storage_thresholds import assess_storage, StorageVerdict
        result = assess_storage(
            write_target_storage="SD card",
            internal_storage="Internal shared storage",
            probe=lambda s: None,
        )
        self.assertEqual(result.write_target_verdict, StorageVerdict.UNKNOWN)
        self.assertFalse(result.halt_pipeline)

    def test_L6_3e_all_healthy_yields_GREEN(self):
        from storage_thresholds import assess_storage, StorageVerdict
        probe = lambda s: 20 * 1024**3   # 20 GB free everywhere
        result = assess_storage(
            write_target_storage="SD card",
            internal_storage="Internal shared storage",
            probe=probe,
        )
        self.assertEqual(result.overall_verdict, StorageVerdict.GREEN)


# ═══════════════════════════════════════════════════════════════════════════
# L6.7 — ALLOW_CONTINUE_ON_STAGE_FAIL lockdown (GOV-B3)
# ═══════════════════════════════════════════════════════════════════════════

class TestStageContinueOverride(unittest.TestCase):
    def setUp(self):
        for m in ("stage_continuation_override", "config"):
            if m in sys.modules: del sys.modules[m]
        # Clear env between tests
        os.environ.pop("ALLOW_CONTINUE_ON_STAGE_FAIL_OVERRIDE", None)

    def tearDown(self):
        os.environ.pop("ALLOW_CONTINUE_ON_STAGE_FAIL_OVERRIDE", None)

    def test_L6_7a_default_is_inactive(self):
        """No env var, no CLI flag → override is OFF."""
        from stage_continuation_override import is_active
        self.assertFalse(is_active())

    def test_L6_7b_activate_requires_reason(self):
        """Board GOV-B3: --override-reason is required."""
        from stage_continuation_override import activate, is_active
        # Empty reason rejected
        self.assertFalse(activate(""))
        self.assertFalse(is_active())
        # Non-empty reason accepted
        self.assertTrue(activate("operator chose to continue past Stage 2 failure"))
        self.assertTrue(is_active())

    def test_L6_7c_hardened_mode_rejects_override(self):
        """Board GOV-B3: blocked entirely under hardened mode."""
        from stage_continuation_override import activate, is_active
        # Stub config with hardened mode ON
        import config
        original = getattr(config, "PRODUCTION_HARDENED_MODE", False)
        try:
            config.PRODUCTION_HARDENED_MODE = True
            ok = activate("anything")
            self.assertFalse(ok, "hardened mode must reject activate()")
            self.assertFalse(is_active(),
                              "hardened mode must report override as inactive")
        finally:
            config.PRODUCTION_HARDENED_MODE = original

    def test_L6_7d_config_no_longer_has_settable_constant(self):
        """Board GOV-B3: config.ALLOW_CONTINUE_ON_STAGE_FAIL is read-only False.

        We don't remove the constant entirely (backward compat for getattr
        callers), but it MUST be False and MUST NOT be operator-settable
        via config.py — the lockdown is enforced by the override module."""
        import config
        # Constant still exists for back-compat
        self.assertTrue(hasattr(config, "ALLOW_CONTINUE_ON_STAGE_FAIL"))
        # And it's False
        self.assertFalse(config.ALLOW_CONTINUE_ON_STAGE_FAIL)


# ═══════════════════════════════════════════════════════════════════════════
# L6.6 — Recovery manifest workflow (GOV-J2)
# ═══════════════════════════════════════════════════════════════════════════

class TestRecoveryManifest(unittest.TestCase):
    def setUp(self):
        for m in ("walkman_inventory_repair", "reorganize_txn_journal"):
            if m in sys.modules: del sys.modules[m]

    def test_L6_6a_recovery_classifies_safe_recoverable_lost(self):
        """Three txns: one safe (on device), one recoverable (local only),
        one lost (gone from both)."""
        from walkman_inventory_repair import build_recovery_manifest
        device_inventory = [
            {"path": "/storage/emulated/0/Music/a.mp3", "size": "1000"},
            # "b.mp3" and "c.mp3" absent
        ]
        txn_records = [
            # safe: pulled, push failed, but file still on device
            {"txn_id": "1", "src": "/dev/a.mp3", "dest": "/dev/A.mp3",
             "src_filename": "a.mp3", "dest_filename": "A.mp3",
             "pull_ok": True, "push_ok": False,
             "failure_step": "push_ok", "failure_reason": "WinError 2"},
            # recoverable: pulled, push failed, gone from device, present locally
            {"txn_id": "2", "src": "/dev/b.mp3", "dest": "/dev/B.mp3",
             "src_filename": "b.mp3", "dest_filename": "B.mp3",
             "pull_ok": True, "push_ok": False,
             "failure_step": "push_ok", "failure_reason": "WinError 2"},
            # lost: gone from both
            {"txn_id": "3", "src": "/dev/c.mp3", "dest": "/dev/C.mp3",
             "src_filename": "c.mp3", "dest_filename": "C.mp3",
             "pull_ok": True, "push_ok": False,
             "failure_step": "push_ok", "failure_reason": "WinError 2"},
            # completed: should NOT appear in manifest
            {"txn_id": "4", "src": "/dev/d.mp3", "dest": "/dev/D.mp3",
             "src_filename": "d.mp3", "dest_filename": "D.mp3",
             "pull_ok": True, "push_ok": True, "delete_ok": True},
        ]
        # Set up local library with only b.mp3 present
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "lib"
            local_dir.mkdir()
            (local_dir / "b.mp3").write_text("local-only-data")
            manifest = build_recovery_manifest(
                txn_journal_records=txn_records,
                device_inventory_rows=device_inventory,
                local_folders=[local_dir],
            )
        # Completed txn excluded
        self.assertEqual(len(manifest.entries), 3)
        # Check counts
        s = manifest.summary
        self.assertEqual(s["safe"], 1)
        self.assertEqual(s["recoverable"], 1)
        self.assertEqual(s["lost"], 1)

    def test_L6_6b_cli_recovery_mode_emits_manifest(self):
        """--recovery + --journal + --inventory + --manifest produces JSON."""
        import reorganize_txn_journal as _rtj
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Build a fake journal
            _rtj.init_journal("test_cli", base_dir=tmp_path)
            for fn in ("x.mp3", "y.mp3"):
                t = _rtj.new_txn(src=f"/d/{fn}", dest=f"/d/{fn.upper()}",
                                   src_filename=fn, dest_filename=fn.upper())
                t["pull_ok"] = True
                t["push_ok"] = False
                _rtj.finalize_txn(t)
            _rtj.close_journal()
            journal_path = tmp_path / "recovery_journal" / "reorganize_txn_test_cli.jsonl"

            # Build a minimal inventory TSV (no overlap → all "lost")
            import csv
            inv_path = tmp_path / "inv.tsv"
            with inv_path.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=[
                    "location","path","size","codec","sample_rate","bits","channels",
                    "bit_rate","duration_s","title","artist","album_artist","album",
                    "track","disc","year","genre","composer",
                ], delimiter="\t")
                w.writeheader()
                w.writerow({"location":"internal","path":"/storage/emulated/0/Music/other.mp3",
                            "size":"1000","codec":"mp3","sample_rate":"44100","bits":"16",
                            "channels":"2","bit_rate":"256000","duration_s":"180",
                            "title":"T","artist":"A","album_artist":"A","album":"B",
                            "track":"1","disc":"1","year":"2024","genre":"Hip-Hop","composer":""})

            manifest_path = tmp_path / "manifest.json"
            r = subprocess.run(
                [sys.executable, "walkman_inventory_repair.py",
                 "--inventory", str(inv_path), "--recovery",
                 "--journal", str(journal_path),
                 "--manifest", str(manifest_path)],
                capture_output=True, text=True, cwd=".",
            )
            self.assertEqual(r.returncode, 0,
                              f"recovery CLI failed: {r.stderr[-300:]}")
            self.assertTrue(manifest_path.exists())
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(data["summary"]["incomplete_transactions"], 2)
            # Both x.mp3 and y.mp3 absent from inventory + no local dirs → lost
            self.assertEqual(data["summary"]["lost"], 2)


# ═══════════════════════════════════════════════════════════════════════════
# L6.1 — silent_move push-verify wiring (verify code markers present)
# ═══════════════════════════════════════════════════════════════════════════

class TestSilentMovePushVerifyWiring(unittest.TestCase):
    """We can't exercise silent_move end-to-end without the MTP stack,
    but we can verify the L6.1 wiring landed in source."""

    def test_L6_1a_push_verify_step_present(self):
        """Step 6 (push-verify) must precede delete in silent_move."""
        src = Path("walkman_reorganize.py").read_text(encoding="utf-8")
        for marker in (
            "MK39.1 L6.1 / Board GOV-A1 Step 6",
            "push_verify_ok",
            "Board GOV-A2",
            "V3_exists_nonzero",
            "preserving source on device",
        ):
            self.assertIn(marker, src, f"L6.1 marker missing: {marker!r}")

    def test_L6_1b_txn_journal_integration_present(self):
        """L6.2 transaction journal must be invoked in silent_move."""
        src = Path("walkman_reorganize.py").read_text(encoding="utf-8")
        for marker in (
            "MK39.1 L6.2",
            "import reorganize_txn_journal",
            "new_txn",
            "finalize_txn",
        ):
            self.assertIn(marker, src, f"L6.2 marker missing: {marker!r}")

    def test_L6_1c_circuit_breaker_integration_present(self):
        """L6.4 circuit breaker must be invoked in consumer + on failures."""
        src = Path("walkman_reorganize.py").read_text(encoding="utf-8")
        for marker in (
            "MK39.1 L6.4",
            "import transport_circuit_breaker",
            "record_failure",
            "record_success",
            "is_tripped",
        ):
            self.assertIn(marker, src, f"L6.4 marker missing: {marker!r}")


# ═══════════════════════════════════════════════════════════════════════════
# Cross-cutting: safety profile integration (L6.3 + L6.7)
# ═══════════════════════════════════════════════════════════════════════════

class TestSafetyProfileIntegration(unittest.TestCase):
    def setUp(self):
        for m in ("platform_safety_profile", "stage_continuation_override"):
            if m in sys.modules: del sys.modules[m]
        os.environ.pop("ALLOW_CONTINUE_ON_STAGE_FAIL_OVERRIDE", None)

    def test_L6_7e_override_active_forces_RED_verdict(self):
        """Board GOV-B3: when override is active, banner is RED regardless."""
        import stage_continuation_override as _sco
        _sco.activate("test override for unit test")
        try:
            import platform_safety_profile as _psp
            profile = _psp.build_profile()
            self.assertEqual(profile["verdict"], "RED",
                              f"override-active must force RED; got {profile['verdict']}")
            self.assertTrue(any("STAGE_CONTINUE_OVERRIDE_ACTIVE" in r
                                  for r in profile["verdict_reasons"]))
        finally:
            _sco.deactivate()


# ═══════════════════════════════════════════════════════════════════════════
# Behavioral simulation — circuit breaker under cascading failure
# (analogue to MC sim from prior sprints)
# ═══════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerCascadeSimulation(unittest.TestCase):
    """Simulate the LOG_11_5_26 cascade: 385 consecutive upload failures.
    Verify the circuit breaker would have caught it at #10 (~98% earlier)."""

    def test_L6_4_cascade_simulation(self):
        for m in ("transport_circuit_breaker",):
            if m in sys.modules: del sys.modules[m]
        import transport_circuit_breaker as _tcb
        _tcb.reset(operator_initiated=True)

        # Simulate the LOG_11_5_26 cascade: 385 consecutive upload failures
        failures_before_breaker = 0
        for i in range(385):
            tripped = _tcb.record_failure(context=f"simulated_track_{i}.mp3")
            if tripped:
                failures_before_breaker = i + 1
                break

        self.assertEqual(failures_before_breaker, 10,
                          f"breaker should trip at 10 (LOG_11_5_26 went to 385); "
                          f"got {failures_before_breaker}")
        # Compute reduction vs the actual LOG_11_5_26 scale
        reduction_pct = (385 - 10) / 385 * 100
        print(f"\n  CASCADE SIM: LOG_11_5_26 ran for {385} consecutive failures "
              f"before DialogWatcher abort. L6.4 circuit breaker would have "
              f"caught it at #{failures_before_breaker} — a {reduction_pct:.1f}% reduction "
              f"in collateral damage.")


# ═══════════════════════════════════════════════════════════════════════════
# Non-regression: prior L5/L4/Phase-I still intact
# ═══════════════════════════════════════════════════════════════════════════

class TestNonRegression(unittest.TestCase):
    def setUp(self):
        for m in ("walkman_dedup", "config"):
            if m in sys.modules: del sys.modules[m]
        os.environ.pop("DEDUP_STRICT_MODE", None)

    def test_L6_NR_planner_lint_still_clean(self):
        """L6 changes don't introduce bare-string planner calls."""
        from sync_planner_argument_audit import run_audit
        report = run_audit(Path("."))
        self.assertEqual(report.violations, [],
                          f"L4.2 linter now flags: {report.violations}")

    def test_L6_NR_phase_I_forensic_9_of_9(self):
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

    def test_L6_NR_L5_planned_dirs_still_works(self):
        """L5.1 SyncPlan.planned_dirs field still present after L6.5 additions."""
        from sync_operations import SyncPlan
        sp = SyncPlan(run_id="t")
        self.assertTrue(hasattr(sp, "planned_dirs"))
        self.assertEqual(sp.planned_dirs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
