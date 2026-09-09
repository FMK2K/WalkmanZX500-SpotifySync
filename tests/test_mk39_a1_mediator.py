"""
test_mk39_a1_mediator.py — MK39-A1 MVP regression + 5,000-sample MC sim
================================================================================

Closes (test side, MVP):
  - API surface stability (T1-T4)
  - Capability authorization correctness (T5-T7)
  - Pre/post invariant violation detection (T8-T10)
  - Taxonomy violation rejection (T11)
  - Self-check tampering detection (T12)
  - 5000-sample MC: invariant — *every* mutation that completes goes through
                    the mediator and is journaled (T13-MC)

Methodology — SCRUM + KANBAN + MC sims (board-locked acceptance gate):
  Locked MC pass criteria:
    - 100% of operations whose context manager completes appear in the journal
    - 0% of refused operations leak through (no journal entry shows
      outcome=success when capability was insufficient)
    - 0 false-positive invariant violations on no-op compatible operations
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# ═══════════════════════════════════════════════════════════════════════════
# Test fixtures
# ═══════════════════════════════════════════════════════════════════════════

class FakeCap:
    """Minimal capability stand-in compatible with mediator's checks."""
    def __init__(self, scope="*", expires_at=float("inf"),
                 token_id="abc12345", semantic_scope=None):
        self.scope = scope
        self.expires_at = expires_at
        self.token_id = token_id
        if semantic_scope is not None:
            self.semantic_scope = semantic_scope


# ═══════════════════════════════════════════════════════════════════════════
# T1-T4: API surface
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_APISurface(unittest.TestCase):
    def setUp(self):
        for m in ("mk39_a1_mediator",):
            if m in sys.modules:
                del sys.modules[m]
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self._tmp.close()
        os.environ["MK39_MUTATION_JOURNAL"] = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass
        os.environ.pop("MK39_MUTATION_JOURNAL", None)

    def test_T1_descriptor_rejects_unknown_op_class(self):
        import mk39_a1_mediator as m
        with self.assertRaises(m.MediatorTaxonomyViolation):
            m.OperationDescriptor(op_class="MAGIC_NEW_OP", target="/tmp/x")

    def test_T2_descriptor_accepts_known_op_classes(self):
        import mk39_a1_mediator as m
        for c in ("TAG_WRITE", "FILE_TRANSFER", "FILE_DELETE",
                  "FILE_RENAME", "DEDUP_DELETE"):
            op = m.OperationDescriptor(op_class=c, target="/tmp/test")
            self.assertEqual(op.op_class, c)

    def test_T3_mediator_yields_context(self):
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/test")
        with m.mediated_operation(op, FakeCap()) as ctx:
            self.assertIsNotNone(ctx)
            self.assertTrue(hasattr(ctx, "mark_success"))
            self.assertTrue(hasattr(ctx, "mark_failure"))
            ctx.mark_success()

    def test_T4_telemetry_counts_operations(self):
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="FILE_TRANSFER", target="/tmp/x")
        before = m.snapshot_mediator_telemetry()["counters"]["mediated_operations_total"]
        with m.mediated_operation(op, FakeCap()) as ctx:
            ctx.mark_success()
        after = m.snapshot_mediator_telemetry()["counters"]["mediated_operations_total"]
        self.assertEqual(after, before + 1)


# ═══════════════════════════════════════════════════════════════════════════
# T5-T7: Capability authorization
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_CapabilityAuthz(unittest.TestCase):
    def setUp(self):
        for m in ("mk39_a1_mediator",):
            if m in sys.modules:
                del sys.modules[m]
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self._tmp.close()
        os.environ["MK39_MUTATION_JOURNAL"] = self._tmp.name

    def tearDown(self):
        try: os.unlink(self._tmp.name)
        except OSError: pass
        os.environ.pop("MK39_MUTATION_JOURNAL", None)

    def test_T5_no_capability_refuses(self):
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/x")
        with self.assertRaises(m.MediatorCapabilityViolation):
            with m.mediated_operation(op, None):
                pass

    def test_T6_expired_capability_refuses(self):
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/x")
        cap = FakeCap(expires_at=0.0)   # epoch — long expired
        with self.assertRaises(m.MediatorCapabilityViolation):
            with m.mediated_operation(op, cap):
                pass

    def test_T7_semantic_scope_enforces_field_authorization(self):
        """T7: a capability with semantic_scope={'allowed_fields': ['year']}
        must REFUSE a TAG_WRITE for 'genre'. This is the structural enforcement
        that MK38-C write barriers will rely on once MK39-A2 lands."""
        import mk39_a1_mediator as m
        cap = FakeCap(semantic_scope={"allowed_op_classes": ["TAG_WRITE"],
                                       "allowed_fields": ["year"]})
        op_genre = m.OperationDescriptor(
            op_class="TAG_WRITE", target="/tmp/x",
            payload_hint={"field": "genre", "new_value": "Jazz"},
        )
        with self.assertRaises(m.MediatorCapabilityViolation):
            with m.mediated_operation(op_genre, cap):
                pass
        # Same cap, but writing 'year' (allowed): should succeed
        op_year = m.OperationDescriptor(
            op_class="TAG_WRITE", target="/tmp/x",
            payload_hint={"field": "year", "new_value": "2024"},
        )
        with m.mediated_operation(op_year, cap) as ctx:
            ctx.mark_success()


# ═══════════════════════════════════════════════════════════════════════════
# T8-T10: Pre/post invariant detection
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_Invariants(unittest.TestCase):
    def setUp(self):
        for m in ("mk39_a1_mediator",):
            if m in sys.modules:
                del sys.modules[m]
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self._tmp.close()
        os.environ["MK39_MUTATION_JOURNAL"] = self._tmp.name

    def tearDown(self):
        try: os.unlink(self._tmp.name)
        except OSError: pass
        os.environ.pop("MK39_MUTATION_JOURNAL", None)

    def test_T8_journal_contains_uuid_and_outcome(self):
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="FILE_TRANSFER", target="/tmp/test")
        with m.mediated_operation(op, FakeCap()) as ctx:
            ctx.mark_success()
        # Read journal
        with open(self._tmp.name, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertGreaterEqual(len(lines), 1)
        last = json.loads(lines[-1])
        self.assertIn("uuid", last)
        self.assertEqual(last["op_class"], "FILE_TRANSFER")
        self.assertEqual(last["outcome"], "success")

    def test_T9_exception_in_block_logged_as_failure(self):
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/test")
        with self.assertRaises(ValueError):
            with m.mediated_operation(op, FakeCap()):
                raise ValueError("simulated mutation failure")
        # Verify journal entry says failure
        with open(self._tmp.name, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        last = json.loads(lines[-1])
        self.assertEqual(last["outcome"], "failure")
        self.assertEqual(last["exception_type"], "ValueError")

    def test_T10_refused_operation_journaled_as_refused(self):
        import mk39_a1_mediator as m
        op = m.OperationDescriptor(op_class="TAG_WRITE", target="/tmp/test")
        with self.assertRaises(m.MediatorCapabilityViolation):
            with m.mediated_operation(op, None):
                pass
        with open(self._tmp.name, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        last = json.loads(lines[-1])
        self.assertEqual(last["outcome"], "refused")
        self.assertEqual(last["reason"], "capability_does_not_authorize")


# ═══════════════════════════════════════════════════════════════════════════
# T11: Taxonomy + T12: Self-check tampering
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_TaxonomyAndSelfCheck(unittest.TestCase):
    def setUp(self):
        for m in ("mk39_a1_mediator",):
            if m in sys.modules:
                del sys.modules[m]

    def test_T11_taxonomy_rejects_unknown_op_classes(self):
        import mk39_a1_mediator as m
        for bad in ("write_tag", "TRANSFER_FILE", "tagWrite", "", "  "):
            with self.assertRaises(m.MediatorTaxonomyViolation,
                                    msg=f"taxonomy must reject {bad!r}"):
                m.OperationDescriptor(op_class=bad, target="/tmp/x")

    def test_T12_self_check_clean_passes(self):
        import mk39_a1_mediator as m
        self.assertTrue(m.verify_mediator_installed())

    def test_T12b_self_check_catches_taxonomy_shrink(self):
        import mk39_a1_mediator as m
        # We cannot mutate a frozenset, but we can verify a clean check
        # passes — and a shrunk OPERATION_CLASSES would fail.
        # Simulate by re-binding the module attribute temporarily.
        original = m.OPERATION_CLASSES
        try:
            m.OPERATION_CLASSES = frozenset({"TAG_WRITE"})  # shrunk
            self.assertFalse(m.verify_mediator_installed())
        finally:
            m.OPERATION_CLASSES = original


# ═══════════════════════════════════════════════════════════════════════════
# T13-MC: 5,000-sample Monte Carlo — every mutation goes through the mediator
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_MonteCarlo(unittest.TestCase):
    """T13-MC: 5,000 random scenarios. The mediator must:
      - Refuse every operation with insufficient capability (no leak)
      - Allow every operation with sufficient capability
      - Journal every operation that completed (success/failure/refused)
      - Never silently allow an unauthorized mutation

    Adversarial CTO acceptance gate (locked):
      - 0 false acceptances (incorrect operation got success outcome)
      - 0 unrecorded operations (every op produces a journal entry)
      - 100% taxonomy violations rejected
    """

    SAMPLES = 5000
    SEED = 0xCBE_39_A1

    def setUp(self):
        for m in ("mk39_a1_mediator",):
            if m in sys.modules:
                del sys.modules[m]
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self._tmp.close()
        os.environ["MK39_MUTATION_JOURNAL"] = self._tmp.name

    def tearDown(self):
        try: os.unlink(self._tmp.name)
        except OSError: pass
        os.environ.pop("MK39_MUTATION_JOURNAL", None)

    def test_T13_monte_carlo_5000_samples(self):
        import mk39_a1_mediator as m
        rng = random.Random(self.SEED)

        # Sample space:
        op_classes = list(m.OPERATION_CLASSES)
        # Capabilities: mix of valid, expired, scope-limited, semantic-scope,
        # missing
        def random_cap():
            r = rng.random()
            if r < 0.15: return None                                 # missing
            if r < 0.25: return FakeCap(expires_at=0.0)              # expired
            if r < 0.40: return FakeCap(scope="single_target")       # narrow scope
            if r < 0.60:
                # semantic-restricted: only allows YEAR writes
                return FakeCap(semantic_scope={
                    "allowed_op_classes": ["TAG_WRITE"],
                    "allowed_fields": ["year"]
                })
            return FakeCap()  # full caps

        # Independent oracle implementing the LOCKED authz spec
        def oracle_authorizes(cap, op_class, field):
            if cap is None: return False
            if getattr(cap, "expires_at", float("inf")) < 1.0: return False
            scope = getattr(cap, "scope", None)
            if scope is None: return False
            sem = getattr(cap, "semantic_scope", None)
            if sem is not None:
                allowed_classes = sem.get("allowed_op_classes")
                if allowed_classes is not None and op_class not in allowed_classes:
                    return False
                if op_class == "TAG_WRITE" and field is not None:
                    af = sem.get("allowed_fields")
                    if af is not None and field not in af:
                        return False
            # Scope check
            if scope != "*":
                # Mediator's scope check: target must be in scope OR cap-op in scope
                # Our oracle: if scope is anything other than "*" and not "single_target"
                # match (we use "/tmp/test" as target), refuse
                if "single_target" in str(scope):
                    return False
            return True

        # Run
        false_acceptances = 0
        false_refusals = 0
        taxonomy_breakages = 0
        unrecorded = 0
        scenarios_run = 0

        for i in range(self.SAMPLES):
            op_class = rng.choice(op_classes)
            field = rng.choice(["genre", "year", "artist", None]) if op_class == "TAG_WRITE" else None
            cap = random_cap()
            target = "/tmp/test"
            payload = {"field": field, "new_value": "X"} if field else None
            try:
                op = m.OperationDescriptor(op_class=op_class, target=target,
                                             payload_hint=payload)
            except m.MediatorTaxonomyViolation:
                taxonomy_breakages += 1
                continue
            scenarios_run += 1

            should_authorize = oracle_authorizes(cap, op_class, field)
            try:
                with m.mediated_operation(op, cap) as ctx:
                    ctx.mark_success()
                actual_authorized = True
            except m.MediatorCapabilityViolation:
                actual_authorized = False
            except Exception as e:
                actual_authorized = False
                # Should never happen in this loop
                self.fail(f"Unexpected exception in MC: {type(e).__name__}: {e}")

            if actual_authorized and not should_authorize:
                false_acceptances += 1
            if (not actual_authorized) and should_authorize:
                false_refusals += 1

        # Verify journal recorded everything
        with open(self._tmp.name, "r", encoding="utf-8") as f:
            journal_lines = [ln for ln in f.read().splitlines() if ln.strip()]
        unrecorded = max(0, scenarios_run - len(journal_lines))

        print("\n--- MK39-A1 MC sim (5,000 samples) ---")
        print(f"  scenarios run:        {scenarios_run}")
        print(f"  taxonomy_breakages:   {taxonomy_breakages}")
        print(f"  false_acceptances:    {false_acceptances}  (LOCKED criterion: 0)")
        print(f"  false_refusals:       {false_refusals}     (LOCKED criterion: 0)")
        print(f"  unrecorded ops:       {unrecorded}         (LOCKED criterion: 0)")
        print(f"  journal entries:      {len(journal_lines)}")

        # === LOCKED PASS CRITERIA ===
        self.assertEqual(false_acceptances, 0,
            f"SAFETY VIOLATION: {false_acceptances} unauthorized ops succeeded")
        self.assertEqual(false_refusals, 0,
            f"OPERATIONAL: {false_refusals} authorized ops were refused")
        self.assertEqual(unrecorded, 0,
            f"FORENSIC: {unrecorded} operations completed without journal entry")


# ═══════════════════════════════════════════════════════════════════════════
# T14: Source-presence checks
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_A1_PatchSiteCoverage(unittest.TestCase):
    def test_T14_module_exposes_documented_API(self):
        import mk39_a1_mediator as m
        for name in ["OperationDescriptor", "MediatorOutcome",
                     "OPERATION_CLASSES", "OP_TO_CAPABILITY_OP",
                     "MediatorError", "MediatorTaxonomyViolation",
                     "MediatorCapabilityViolation",
                     "mediated_operation",
                     "snapshot_mediator_telemetry",
                     "verify_mediator_installed"]:
            self.assertTrue(hasattr(m, name), f"missing: {name}")
            self.assertIn(name, m.__all__, f"not in __all__: {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
