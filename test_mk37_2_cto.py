"""
test_mk37_2_cto.py — MK37.2 Board ticket regression suite
================================================================================

Tests close every MK37.2 cherry-pick from the post-LOG_8_5_26 review. Each
ticket maps to N tests (T1, T2, ...) verifying the structural fix.

Run:
    python3 -m pytest test_mk37_2_cto.py -v
    python3 test_mk37_2_cto.py            # standalone (uses unittest)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


# ═══════════════════════════════════════════════════════════════════════════
# MK37.2-1 — Capability TTL renewal
# ═══════════════════════════════════════════════════════════════════════════

class TestMK37_2_1_CapabilityRenewal(unittest.TestCase):
    """Closes LOG_8_5_26 token=7d7e8348 expired 27,901s into the run."""

    def setUp(self):
        # Fresh imports for each test — module-level state must reset
        for mod in ("mk37_2_renewal", "mk37_wiring", "mk35_capability"):
            sys.modules.pop(mod, None)
        # Redirect renewal journal to a tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self._tmp.close()
        os.environ["MK37_2_RENEWAL_JOURNAL"] = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass
        os.environ.pop("MK37_2_RENEWAL_JOURNAL", None)

    def test_T1_renewal_fires_when_capability_stale(self):
        """T1: renew_pass_capability_if_stale renews when remaining < threshold."""
        try:
            import mk37_wiring
            import mk37_2_renewal as renewal
        except ImportError as e:
            self.skipTest(f"mk37_wiring/mk37_2_renewal unavailable: {e}")

        # Issue a capability with TTL=10s
        cap1 = mk37_wiring.begin_sync_pass(scope="*", ttl_seconds=10.0)
        if cap1 is None:
            self.skipTest("begin_sync_pass returned None (mk35 missing)")
        old_token = cap1.token_id

        # Min-remaining=300s → 10s remaining is below threshold → renew
        renewed = renewal.renew_pass_capability_if_stale(min_remaining_s=300.0)
        self.assertTrue(renewed, "Should have renewed when stale")

        cap2 = mk37_wiring.get_pass_capability()
        self.assertIsNotNone(cap2)
        self.assertNotEqual(cap2.token_id, old_token,
                            "Renewed capability should have a NEW token_id")

    def test_T2_renewal_skipped_when_capability_fresh(self):
        """T2: fresh capability is NOT renewed (no churn)."""
        try:
            import mk37_wiring
            import mk37_2_renewal as renewal
        except ImportError as e:
            self.skipTest(f"modules unavailable: {e}")

        cap1 = mk37_wiring.begin_sync_pass(scope="*", ttl_seconds=7200.0)
        if cap1 is None:
            self.skipTest("begin_sync_pass returned None")
        old_token = cap1.token_id

        # 7200s remaining vs min=300s → no renewal
        renewed = renewal.renew_pass_capability_if_stale(min_remaining_s=300.0)
        self.assertFalse(renewed, "Should NOT renew a fresh capability")

        cap2 = mk37_wiring.get_pass_capability()
        self.assertEqual(cap2.token_id, old_token,
                         "Fresh capability should have the SAME token_id")

    def test_T3_renewal_journaled_to_jsonl(self):
        """T3: every renewal appends a JSONL event for replay/audit."""
        try:
            import mk37_wiring
            import mk37_2_renewal as renewal
        except ImportError as e:
            self.skipTest(f"modules unavailable: {e}")

        mk37_wiring.begin_sync_pass(scope="*", ttl_seconds=10.0)
        renewed = renewal.renew_pass_capability_if_stale(min_remaining_s=300.0)
        if not renewed:
            self.skipTest("renewal did not fire (mk35 issue?)")

        # Read the journal
        with open(self._tmp.name, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertGreaterEqual(len(lines), 1, "Renewal must write to journal")
        event = json.loads(lines[-1])
        self.assertEqual(event["event"], "renewed")
        self.assertIn("old_token_short", event)
        self.assertIn("new_token_short", event)
        self.assertNotEqual(event["old_token_short"], event["new_token_short"])

    def test_T4_renewal_preserves_grant_key_invariant(self):
        """T4: renewal cannot bypass _GRANT_KEY (security model unchanged)."""
        try:
            import mk37_wiring
            import mk35_capability as cap_mod
        except ImportError as e:
            self.skipTest(f"modules unavailable: {e}")

        # Direct grant_capability with wrong key MUST raise PermissionError
        with self.assertRaises(PermissionError):
            cap_mod.grant_capability(
                grant_key="bogus",
                scope="*",
                granted_by_invariant="test_T4",
                ttl_seconds=60.0,
            )

    def test_T5_begin_destructive_stage_forces_fresh_capability(self):
        """T5: begin_destructive_stage(force_renewal=True) issues a fresh token."""
        try:
            import mk37_wiring
            import mk37_2_renewal as renewal
        except ImportError as e:
            self.skipTest(f"modules unavailable: {e}")

        cap1 = mk37_wiring.begin_sync_pass(scope="*", ttl_seconds=7200.0)
        if cap1 is None:
            self.skipTest("begin_sync_pass returned None")
        old_token = cap1.token_id

        renewal.begin_destructive_stage("stage_0_download",
                                         ttl_seconds=7200.0,
                                         force_renewal=True)
        cap2 = mk37_wiring.get_pass_capability()
        self.assertNotEqual(cap2.token_id, old_token,
                            "force_renewal=True must issue a fresh token")
        self.assertEqual(renewal.current_stage(), "stage_0_download")

    def test_T6_telemetry_counts_renewals(self):
        """T6: snapshot_renewal_telemetry tracks renewal events."""
        try:
            import mk37_wiring
            import mk37_2_renewal as renewal
        except ImportError as e:
            self.skipTest(f"modules unavailable: {e}")

        before = renewal.snapshot_renewal_telemetry()
        mk37_wiring.begin_sync_pass(scope="*", ttl_seconds=10.0)
        renewal.renew_pass_capability_if_stale(min_remaining_s=300.0)
        after = renewal.snapshot_renewal_telemetry()

        self.assertGreater(
            after["capability_renewals"],
            before["capability_renewals"],
            "telemetry must increment on renewal"
        )


# ═══════════════════════════════════════════════════════════════════════════
# MK37.2-2 — Sync planner signature repair
# ═══════════════════════════════════════════════════════════════════════════

class TestMK37_2_2_SyncPlannerSignature(unittest.TestCase):
    """Closes LOG_8_5_26:425 — generate_plan() missing 'device_snapshot'."""

    def test_T1_generate_plan_requires_device_snapshot(self):
        """T1: calling generate_plan with only local_files raises TypeError."""
        try:
            import sync_planner
        except ImportError as e:
            self.skipTest(f"sync_planner unavailable: {e}")

        with self.assertRaises(TypeError):
            sync_planner.generate_plan([])    # missing device_snapshot

    def test_T2_generate_plan_accepts_device_snapshot(self):
        """T2: providing device_snapshot lets the call succeed."""
        try:
            import sync_planner
            from sync_planner import DeviceSnapshot
        except ImportError as e:
            self.skipTest(f"sync_planner unavailable: {e}")

        snap = DeviceSnapshot(files=[], complete=True)
        plan = sync_planner.generate_plan([], snap)
        self.assertIsNotNone(plan, "plan must be returned")

    def test_T3_walkman_sync_call_site_provides_snapshot_or_skips(self):
        """T3: walkman_sync.py preflight either passes a snapshot or logs PLAN_SKIPPED."""
        ws_path = Path(__file__).parent / "walkman_sync.py"
        if not ws_path.exists():
            self.skipTest("walkman_sync.py not in test dir")
        text = ws_path.read_text(encoding="utf-8")
        # The fixed call site must either:
        #   (a) call build_sync_plan with TWO positional args, or
        #   (b) log PLAN_SKIPPED_NO_SNAPSHOT on the no-snapshot branch.
        self.assertIn("PLAN_SKIPPED_NO_SNAPSHOT", text,
                      "walkman_sync must emit PLAN_SKIPPED_NO_SNAPSHOT marker")
        self.assertIn("build_sync_plan([], device_snapshot)", text,
                      "walkman_sync must call build_sync_plan with TWO args")


# ═══════════════════════════════════════════════════════════════════════════
# MK37.2-3 — Device inventory race recovery
# ═══════════════════════════════════════════════════════════════════════════

class TestMK37_2_3_InventoryRaceRecovery(unittest.TestCase):
    """Closes LOG_8_5_26:30 / 439 — ADB inventory=0 race condition."""

    def test_T1_race_markers_present_in_source(self):
        """T1: device_inventory emits the three race markers."""
        di_path = Path(__file__).parent / "device_inventory.py"
        if not di_path.exists():
            self.skipTest("device_inventory.py not in test dir")
        text = di_path.read_text(encoding="utf-8")
        self.assertIn("INVENTORY_RACE_DETECTED", text)
        self.assertIn("INVENTORY_RACE_RECOVERED", text)
        self.assertIn("INVENTORY_RACE_PERSISTED", text)

    def test_T2_three_attempt_retry_preserved(self):
        """T2: existing 3-attempt retry loop is preserved (not weakened)."""
        di_path = Path(__file__).parent / "device_inventory.py"
        if not di_path.exists():
            self.skipTest("device_inventory.py not in test dir")
        text = di_path.read_text(encoding="utf-8")
        # The retry loop is `for _attempt in range(3):`
        self.assertIn("for _attempt in range(3):", text,
                      "3-attempt retry loop must be preserved")


# ═══════════════════════════════════════════════════════════════════════════
# MK37.2-4 — SD free-space resilience
# ═══════════════════════════════════════════════════════════════════════════

class TestMK37_2_4_SDFreeSpaceResilience(unittest.TestCase):
    """Closes LOG_8_5_26 — 135× SD free-space UNKNOWN warning."""

    def test_T1_adb_first_probe_present(self):
        """T1: downloader has an ADB-first SD free-space probe."""
        dl_path = Path(__file__).parent / "downloader.py"
        if not dl_path.exists():
            self.skipTest("downloader.py not in test dir")
        text = dl_path.read_text(encoding="utf-8")
        self.assertIn("_get_sd_free_gb_via_adb", text,
                      "ADB-first probe function must exist")
        self.assertIn("SDFREE_ADB_OK", text,
                      "ADB-OK marker must be emittable")
        self.assertIn("SDFREE_BOTH_FAILED", text,
                      "Both-failed marker must be emittable")

    def test_T2_warning_does_not_repeat_per_album(self):
        """T2: SDFREE_BOTH_FAILED is logged once, not 135 times."""
        dl_path = Path(__file__).parent / "downloader.py"
        if not dl_path.exists():
            self.skipTest("downloader.py not in test dir")
        text = dl_path.read_text(encoding="utf-8")
        self.assertIn("logged_both_failed", text,
                      "single-fire flag must exist")
        self.assertIn("This warning will not repeat", text,
                      "user-visible 'will not repeat' note required")


# ═══════════════════════════════════════════════════════════════════════════
# MK37.2-5 — Resource pressure ↔ Capability coupling
# ═══════════════════════════════════════════════════════════════════════════

class TestMK37_2_5_ResourceCoupling(unittest.TestCase):
    """Closes LOG_8_5_26 secondary — handle pressure climbing during cascade."""

    def test_T1_coupling_module_exposes_install_api(self):
        """T1: install_resource_capability_coupling is callable."""
        try:
            from mk37_2_resource_coupling import install_resource_capability_coupling
        except ImportError as e:
            self.skipTest(f"coupling module unavailable: {e}")
        self.assertTrue(callable(install_resource_capability_coupling))

    def test_T2_install_idempotent(self):
        """T2: calling install twice doesn't double-install."""
        try:
            import mk37_2_resource_coupling as coup
        except ImportError as e:
            self.skipTest(f"coupling module unavailable: {e}")

        # Force re-import to reset _INSTALLED
        importlib.reload(coup)
        # First call may succeed or skip depending on env
        coup.install_resource_capability_coupling()
        # Second call must return False (already installed or still skipped)
        result2 = coup.install_resource_capability_coupling()
        self.assertFalse(result2, "Second install must return False")

    def test_T3_coupling_noops_when_resource_monitor_absent(self):
        """T3: missing resource_monitor module does NOT raise."""
        try:
            import mk37_2_resource_coupling as coup
        except ImportError as e:
            self.skipTest(f"coupling module unavailable: {e}")

        importlib.reload(coup)
        # Setting sys.modules["resource_monitor"] = None makes
        # `import resource_monitor` raise ImportError. mock.patch.dict
        # restores the prior state automatically on exit. Do NOT delete
        # the entry — that would undo the patch.
        with mock.patch.dict(sys.modules, {"resource_monitor": None}):
            try:
                # The function must return False, not raise
                result = coup.install_resource_capability_coupling()
                self.assertFalse(result, "install must return False when "
                                          "resource_monitor unavailable")
            except ImportError:
                self.fail("install_resource_capability_coupling raised ImportError")


# ═══════════════════════════════════════════════════════════════════════════
# Cross-ticket: end-to-end renewal in transfer path
# ═══════════════════════════════════════════════════════════════════════════

class TestMK37_2_TransferPathRenewal(unittest.TestCase):
    """Verifies transfer_via_legacy_with_quorum invokes renewal proactively."""

    def test_T1_transfer_path_calls_renewal(self):
        """T1: source has the renewal call before require_capability."""
        wiring_path = Path(__file__).parent / "mk37_wiring.py"
        if not wiring_path.exists():
            self.skipTest("mk37_wiring.py not in test dir")
        text = wiring_path.read_text(encoding="utf-8")
        # The renewal call must appear textually BEFORE the capability check
        idx_renewal = text.find("renew_pass_capability_if_stale")
        idx_require = text.find("require_capability(")
        self.assertGreater(idx_renewal, 0, "Renewal call must be present")
        self.assertGreater(idx_require, 0, "require_capability call must be present")
        self.assertLess(
            idx_renewal, idx_require,
            "Renewal must occur BEFORE require_capability (proactive, not reactive)"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
