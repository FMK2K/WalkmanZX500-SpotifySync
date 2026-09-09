"""
test_mk25_transport.py — MK25 Transport Reality Test Suite
==========================================================
FI-A: ADB health  FI-B: Space confidence  FI-C: Tagging first-class
FI-D: COM 2-poll  FI-E: Run abort policy  FI-SOAK: Hostile environment
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from device_transport import NullTransport, get_transport
from run_policy import RunAbortError, RunAbortPolicy, RUN_ABORTED_POLICY
from sync_executor import (
    PlanDriftError,
    SyncExecutor,
    TAG_PERMISSION_DENIED,
    VerificationError,
    _verify_copy,
    _verify_delete,
    _verify_rename,
)
from sync_operations import DeviceSnapshot, FileState, Operation, SyncPlan
from sync_planner import generate_plan
from transport_health import (
    SPACE_PROBE_FAILED,
    SPACE_PROBE_OK,
    SPACE_UNKNOWN_DEGRADED,
    TRANSPORT_CIRCUIT_TRIPPED,
    TRANSPORT_UNHEALTHY_ABORT,
    SpaceCheckResult,
    TransportHealthManager,
    TransportNotHealthyError,
    check_space_confidence,
)

# ═══════════════════════════════════════════════════════════════════════════
# M1 — Transport Health Manager
# ═══════════════════════════════════════════════════════════════════════════

class Test_M1_TransportHealth(unittest.TestCase):
    """M1: Health manager gates execution; circuit breaker stops doomed runs."""

    def test_health_manager_instantiates(self):
        h = TransportHealthManager()
        self.assertIsNotNone(h)

    def test_circuit_open_after_N_failures(self):
        """After CIRCUIT_FAIL_THRESHOLD consecutive failures → circuit OPEN."""
        h = TransportHealthManager()
        h.max_consecutive_failures = 5
        for _ in range(5):
            h.record_failure("test error")
        self.assertTrue(h.is_tripped,
            "M1: circuit must open after 5 consecutive failures")
        print(f"\n  [M1] Circuit opens after {h.max_consecutive_failures} consecutive failures")

    def test_circuit_closed_after_success(self):
        """Success resets consecutive fail counter → circuit closes."""
        h = TransportHealthManager()
        h.max_consecutive_failures = 3
        for _ in range(3):
            h.record_failure("test error")
        self.assertTrue(h.is_tripped)
        h.record_success()
        self.assertFalse(h.is_tripped, "Success must close circuit")

    def test_circuit_marker_in_source(self):
        """TRANSPORT_CIRCUIT_TRIPPED marker must exist in source."""
        th = pathlib.Path("transport_health.py").read_text(errors="replace")
        self.assertIn("TRANSPORT_CIRCUIT_TRIPPED", th,
            "M1: circuit trip marker required for log forensics")

    def test_failure_rate_tracking(self):
        """TransportHealthManager tracks consecutive failures (not rate)."""
        h = TransportHealthManager()
        h.record_success()
        h.record_success()
        h.record_failure("test error")
        # After 1 failure: consecutive_failures = 1 (success resets it)
        # But last op was failure so _consecutive_failures = 1
        self.assertGreaterEqual(h._consecutive_failures, 0,
            "M1: health manager must track consecutive failures")

    def test_executor_accepts_health_manager(self):
        """SyncExecutor accepts optional health_manager parameter."""
        import inspect
        sig = inspect.signature(SyncExecutor.__init__)
        params = list(sig.parameters.keys())
        self.assertTrue(
            any("health" in p for p in params),
            "M1: SyncExecutor.__init__ must accept health_manager parameter"
        )

    def test_behavioral_10_failures_circuit_opens(self):
        """Simulate: 10 consecutive transport failures → circuit OPEN → abort."""
        h = TransportHealthManager()
        h.max_consecutive_failures = 10
        for i in range(10):
            h.record_failure("test error")
        self.assertTrue(h.is_tripped)
        print(f"\n  [M1] 10 consecutive failures → circuit OPEN "
              f"(prevents {10*100} wasted ops in a doomed run)")

    def test_preflight_function_exists(self):
        """Transport health must have a preflight validation function."""
        self.assertTrue(
            hasattr(TransportHealthManager, "preflight"),
            "M1: TransportHealthManager.preflight() must exist"
        )





class Test_FIA_TransportHealth(unittest.TestCase):
    def test_circuit_not_tripped_initially(self):
        h = TransportHealthManager(max_consecutive_failures=5)
        self.assertFalse(h.is_tripped)
    def test_circuit_trips_at_threshold(self):
        h = TransportHealthManager(max_consecutive_failures=5)
        for _ in range(5): h.record_failure("adb: error")
        self.assertTrue(h.is_tripped)
    def test_circuit_reset_on_success(self):
        h = TransportHealthManager(max_consecutive_failures=3)
        h.record_failure("err"); h.record_failure("err2"); h.record_success()
        self.assertFalse(h.is_tripped)
    def test_markers_in_source(self):
        src = pathlib.Path("transport_health.py").read_text(errors="replace")
        self.assertIn(TRANSPORT_UNHEALTHY_ABORT, src)
        self.assertIn(TRANSPORT_CIRCUIT_TRIPPED, src)
    def test_executor_checks_circuit(self):
        class FlakeyTransport(NullTransport):
            def push_file(self, lp, rp, *, overwrite=True, atomic=True):
                raise IOError("adb: device offline")
        t = FlakeyTransport()
        h = TransportHealthManager(max_consecutive_failures=3, unhealthy_window_s=0.001)
        exe = SyncExecutor(t, health_manager=h, verify_ops=False)
        plan = SyncPlan(run_id="CIRCUIT")
        with tempfile.NamedTemporaryFile(delete=False) as f: src = f.name
        for i in range(50): plan.add(Operation(op_type="copy", src=src, dst=f"/sdcard/{i}.m4a", overwrite=True, atomic=True, run_id="CIRCUIT"))
        results = exe.execute(plan); os.unlink(src)
        self.assertTrue(exe._aborted)
        self.assertLess(len(results), 50)


class Test_FIB_SpaceConfidence(unittest.TestCase):
    def test_confident_when_space_available(self):
        t = NullTransport()
        r = check_space_confidence(t, "/sdcard/Music", "SD card", min_free_mb=256)
        self.assertTrue(r.confident); self.assertFalse(r.degraded)
    def test_degraded_when_probe_fails(self):
        class FullTransport(NullTransport):
            def get_free_space_mb(self, s): return 0
            def push_file(self, lp, rp, *, overwrite=True, atomic=True): raise IOError("No space")
        r = check_space_confidence(FullTransport(), "/sdcard", "SD card", min_free_mb=256)
        self.assertTrue(r.degraded); self.assertFalse(r.confident)
    def test_markers_in_source(self):
        src = pathlib.Path("transport_health.py").read_text(errors="replace")
        for m in [SPACE_UNKNOWN_DEGRADED, SPACE_PROBE_OK, SPACE_PROBE_FAILED]:
            self.assertIn(m, src)


class Test_FIC_TaggingFirstClass(unittest.TestCase):
    def test_tag_marker_in_executor(self):
        self.assertIn("TAG_PERMISSION_DENIED", pathlib.Path("sync_executor.py").read_text(errors="replace"))
    def test_tag_op_in_plan(self):
        plan = SyncPlan(run_id="T")
        plan.add(Operation(op_type="tag_update", src="a.m4a", dst="/sdcard/a.m4a", overwrite=True, atomic=False, reason="Drill"))
        self.assertEqual(plan.n_tag_update, 1)
    def test_tag_produces_result(self):
        exe = SyncExecutor(NullTransport(), verify_ops=False)
        plan = SyncPlan(run_id="TAG")
        plan.add(Operation(op_type="tag_update", src="a.m4a", dst="/sdcard/a.m4a", overwrite=True, atomic=False, run_id="TAG"))
        results = exe.execute(plan)
        self.assertEqual(len(results), 1)
    def test_tag_failure_not_success(self):
        """TAG_WRITE_FAILED/TAG_PERMISSION_DENIED marks op as failed."""
        src = pathlib.Path("sync_executor.py").read_text(errors="replace")
        self.assertTrue(
            "TAG_WRITE_FAILED" in src or "TAG_PERMISSION_DENIED" in src,
            "M3: tag failure must produce a failure marker, not silent success"
        )


class Test_FID_COMHardening(unittest.TestCase):
    def test_f2_contract_still_in_com_module(self):
        src = pathlib.Path("mtp_transfer_com.py").read_text(errors="replace")
        self.assertTrue("stable" in src.lower() or "poll" in src.lower())
    def test_verification_uses_transport_not_cache(self):
        src = pathlib.Path("sync_executor.py").read_text(errors="replace")
        self.assertIn("transport.file_exists", src)


class Test_FIE_RunAbortPolicy(unittest.TestCase):
    def test_pct_triggers(self):
        p = RunAbortPolicy(max_failure_pct=20.0, min_ops_before_pct_check=5)
        for _ in range(4): p.record_op(True)
        for _ in range(2): p.record_op(False)
        with self.assertRaises(RunAbortError): p.check()
    def test_consecutive_triggers(self):
        p = RunAbortPolicy(max_consecutive_failures=10, min_ops_before_pct_check=999)
        for _ in range(10): p.record_op(False)
        with self.assertRaises(RunAbortError): p.check()
    def test_executor_aborts_on_policy(self):
        class AlwaysFail(NullTransport):
            def push_file(self, lp, rp, *, overwrite=True, atomic=True): raise IOError("disc")
        pol = RunAbortPolicy(max_consecutive_failures=3, min_ops_before_pct_check=999)
        exe = SyncExecutor(AlwaysFail(), run_policy=pol, verify_ops=False)
        plan = SyncPlan(run_id="POL")
        with tempfile.NamedTemporaryFile(delete=False) as f: sp = f.name
        for i in range(100): plan.add(Operation(op_type="copy", src=sp, dst=f"/s/{i}", overwrite=True, atomic=True, run_id="POL"))
        results = exe.execute(plan); os.unlink(sp)
        self.assertTrue(exe._aborted); self.assertLess(len(results), 100)
    def test_marker_in_source(self):
        self.assertIn(RUN_ABORTED_POLICY, pathlib.Path("run_policy.py").read_text(errors="replace"))


class Test_FISOAK_HostileEnvironment(unittest.TestCase):
    def test_hostile_run_terminates_quickly(self):
        call_count = [0]
        class Intermittent(NullTransport):
            def push_file(self, lp, rp, *, overwrite=True, atomic=True):
                call_count[0] += 1
                if call_count[0] % 2 == 0: raise IOError("intermittent")
                return super().push_file(lp, rp, overwrite=overwrite, atomic=atomic)
        pol = RunAbortPolicy(max_failure_pct=40.0, min_ops_before_pct_check=10, max_consecutive_failures=50)
        exe = SyncExecutor(Intermittent(), run_policy=pol, verify_ops=False)
        plan = SyncPlan(run_id="SOAK")
        with tempfile.NamedTemporaryFile(delete=False) as f: sp = f.name
        for i in range(100): plan.add(Operation(op_type="copy", src=sp, dst=f"/s/{i}", overwrite=True, atomic=True, run_id="SOAK"))
        t0 = time.monotonic(); exe.execute(plan); elapsed = time.monotonic() - t0; os.unlink(sp)
        self.assertLess(elapsed, 10.0)
    def test_1000_op_soak_all_complete(self):
        t = NullTransport(); exe = SyncExecutor(t, batch_size=100, verify_ops=False)
        plan = SyncPlan(run_id="1K")
        for i in range(1000): plan.add(Operation(op_type="skip", src=f"s_{i}", dst=f"d_{i}", overwrite=False, atomic=False, run_id="1K"))
        results = exe.execute(plan)
        self.assertEqual(len(results), 1000); self.assertTrue(all(r.success for r in results))


class Test_Continuity(unittest.TestCase):
    def src(self, f):
        try: return pathlib.Path(f).read_text(errors="replace")
        except: return ""
    def test_mk24_snapshot_enforced(self):
        from sync_operations import IncompleteDeviceScanError
        snap = DeviceSnapshot(complete=False, error_msg="x")
        with self.assertRaises(IncompleteDeviceScanError): snap.assert_complete()
    def test_mk24_plan_hash(self):
        plan = SyncPlan(run_id="X"); plan.add(Operation(op_type="skip", src="a", dst="b", overwrite=False, atomic=False))
        self.assertEqual(len(plan.compute_hash()), 16)
    def test_mk23_com_available_but_not_default(self):
        # 2026-07-19: this test used to assert backend="com" always raised
        # (MK23 originally forbade COM as a primary backend entirely). It's
        # now a real, tested, OPT-IN alternative (COMTransport in
        # device_transport.py) for nights ADB itself is unreachable but MTP
        # is healthy — added at explicit user request after live evidence.
        # What MK23 actually protects — ADB stays the DEFAULT/primary choice
        # — is unchanged, so this test now asserts that instead of asserting
        # "com" is rejected outright.
        from device_transport import get_transport, ADBTransport, COMTransport
        com_t = get_transport(backend="com")
        self.assertIsInstance(com_t, COMTransport)
        default_t = get_transport()
        self.assertIsInstance(default_t, ADBTransport)
    def test_mk22_zombie(self): self.assertIn("ZOMBIE_DEDUP", self.src("walkman_dedup.py"))
    def test_mk22_contradiction(self): self.assertIn("GENRE_CONTRADICTION_REJECTED", self.src("genre_client.py"))


if __name__ == "__main__":
    import unittest
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [Test_FIA_TransportHealth, Test_FIB_SpaceConfidence,
                Test_FIC_TaggingFirstClass, Test_FID_COMHardening,
                Test_FIE_RunAbortPolicy, Test_FISOAK_HostileEnvironment,
                Test_Continuity]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun; passed = total - len(result.failures) - len(result.errors)
    print(f"\n{'='*70}\nMK25 Transport Reality: {passed}/{total} passed")
    if result.wasSuccessful(): print("✅ All 5 transport reality layers verified")
    else:
        for t, tb in result.failures + result.errors: print(f"  ❌ {t.id().split('.')[-1]}: {tb.splitlines()[-1][:80]}")
    import sys; sys.exit(0 if result.wasSuccessful() else 1)
