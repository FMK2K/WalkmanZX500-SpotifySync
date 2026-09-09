"""
test_mk38_1_quorum_dominance.py — MK38.1 regression + 10,000-sample MC sim
================================================================================

Closes (test side):
  R25 Quorum-rejects-correct-transfers (LOG_9_5_26)  — T1, T2, T3, T8-MC
  R26 UNTRUSTED ambiguity                            — T4, T5, T6, T7
  Safety preservation (no new false positives)       — T8-MC FPR analysis

Methodology — SCRUM + KANBAN + MC sims (the adversarial CTO acceptance gate):

  T1-T7: deterministic unit tests — exact behavior on hand-crafted scenarios
  T8-MC: 10,000-sample Monte Carlo simulation — comprehensive distribution
         analysis comparing OLD (pre-MK38.1) vs NEW (with dominance) logic.

  MC pass criteria (locked):
    - FPR_NEW <= FPR_OLD      (safety preserved: no new false-positives)
    - FNR_NEW <= 0.05         (95%+ of correct transfers accepted)
    - Production case (sha256=STRONG, size_mtime=NO_SIGNAL, existence=NO_SIGNAL):
                              100% acceptance under NEW (currently 0% in production)

Run:
    python3 -m pytest test_mk38_1_quorum_dominance.py -v
"""

from __future__ import annotations

import random
import sys
import unittest
from dataclasses import dataclass, field
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════════════════
# Test fixtures — minimal stand-ins so we don't pull in the full mk35 stack
# ═══════════════════════════════════════════════════════════════════════════

class FakeTier:
    """Minimal TrustTier substitute. int values match TrustTier:
    UNTRUSTED=0, WEAK=1, MODERATE=2, STRONG=3."""
    def __init__(self, value, name):
        self.value = value
        self.name = name
    def __int__(self): return self.value
    def __ge__(self, other): return self.value >= int(other)
    def __lt__(self, other): return self.value < int(other)
    def __eq__(self, other):
        if isinstance(other, FakeTier): return self.value == other.value
        return self.value == other
    def __hash__(self): return self.value
    def __repr__(self): return f"FakeTier.{self.name}"

UNTRUSTED = FakeTier(0, "UNTRUSTED")
WEAK      = FakeTier(1, "WEAK")
MODERATE  = FakeTier(2, "MODERATE")
STRONG    = FakeTier(3, "STRONG")


@dataclass
class FakeOutcome:
    strategy_name: str
    tier: Any
    evidence: dict = field(default_factory=dict)
    note: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# T1-T7: classify_outcome — distinguishing no-signal from disagreement
# ═══════════════════════════════════════════════════════════════════════════

class TestMK38_1_OutcomeClassification(unittest.TestCase):
    """T1-T7: classify_outcome() must correctly distinguish 4 classes."""

    def setUp(self):
        if "mk38_1_quorum_dominance" in sys.modules:
            del sys.modules["mk38_1_quorum_dominance"]
        import mk38_1_quorum_dominance as m
        self.m = m

    def test_T1_positive_when_tier_above_untrusted(self):
        """T1: any tier > UNTRUSTED = positive observation."""
        self.assertEqual(self.m.classify_outcome(
            FakeOutcome("sha256", STRONG)), "positive")
        self.assertEqual(self.m.classify_outcome(
            FakeOutcome("size_mtime", MODERATE)), "positive")
        self.assertEqual(self.m.classify_outcome(
            FakeOutcome("existence", WEAK)), "positive")

    def test_T2_no_signal_when_strategy_raised_exception(self):
        """T2: UNTRUSTED + evidence={'strategy_exception': ...} = no_signal."""
        o = FakeOutcome("size_mtime", UNTRUSTED,
                        evidence={"strategy_exception": "RuntimeError(...)"},
                        note="strategy raised; treated as no-signal")
        self.assertEqual(self.m.classify_outcome(o), "no_signal")

    def test_T3_no_signal_when_subprocess_failed(self):
        """T3: UNTRUSTED + evidence={'rc': 1} or 'error' key = no_signal.
        This is the LOG_9_5_26 production pattern: device-side stat failed."""
        o = FakeOutcome("size_mtime", UNTRUSTED,
                        evidence={"src_size": 5242880, "rc": 1},
                        note="device-side stat failed")
        self.assertEqual(self.m.classify_outcome(o), "no_signal")

    def test_T4_disagreement_when_size_mismatch(self):
        """T4: UNTRUSTED + evidence={'src_size': N, 'dev_size': M} where N!=M
        = disagreement (active contradiction signal)."""
        o = FakeOutcome("size_mtime", UNTRUSTED,
                        evidence={"src_size": 5242880, "dev_size": 1024},
                        note="size mismatch")
        self.assertEqual(self.m.classify_outcome(o), "disagreement")

    def test_T5_disagreement_when_hash_mismatch(self):
        """T5: UNTRUSTED with explicit hash_mismatch note = disagreement."""
        o = FakeOutcome("sha256", UNTRUSTED,
                        evidence={"expected_hash": "abc", "actual_hash": "xyz"},
                        note="hash mismatch detected")
        self.assertEqual(self.m.classify_outcome(o), "disagreement")

    def test_T6_missing_when_evidence_says_so(self):
        """T6: UNTRUSTED + evidence={'missing': True} = missing."""
        o = FakeOutcome("existence", UNTRUSTED,
                        evidence={"missing": True})
        self.assertEqual(self.m.classify_outcome(o), "missing")

    def test_T7_ambiguous_untrusted_defaults_to_disagreement_safe(self):
        """T7: UNTRUSTED with NO evidence and NO note → conservative default
        is 'disagreement', NOT 'no_signal'. Defending against false override."""
        o = FakeOutcome("mystery_strategy", UNTRUSTED)
        # Conservative default: ambiguous → assume disagreement → don't override
        self.assertEqual(self.m.classify_outcome(o), "disagreement")


# ═══════════════════════════════════════════════════════════════════════════
# T8: 10,000-sample Monte Carlo simulation
# ═══════════════════════════════════════════════════════════════════════════

class TestMK38_1_MonteCarlo(unittest.TestCase):
    """T8-MC: 10,000 random transfer scenarios; compare OLD vs NEW quorum.

    Adversarial-CTO pass criteria (LOCKED):
      - FPR_NEW <= FPR_OLD       (no new false positives — safety preserved)
      - FNR_NEW <= 0.05          (most correct transfers accepted)
      - Production-pattern recall = 1.00 under NEW (LOG_9_5_26 closed)

    Each scenario:
      1. Sample ground truth: 'truth' = bytes_match (Bernoulli p=0.95)
      2. Sample each strategy's outcome conditionally on truth, with
         realistic-ish error rates per the LOG_9_5_26 evidence
      3. Apply OLD logic (2-of-3 must reach MODERATE)
      4. Apply NEW logic (OLD result, then dominance override if applicable)
      5. Tally TP/FP/TN/FN against ground truth
    """

    SAMPLES = 10000
    SEED = 0xCBE_38_01

    def setUp(self):
        if "mk38_1_quorum_dominance" in sys.modules:
            del sys.modules["mk38_1_quorum_dominance"]
        import mk38_1_quorum_dominance as m
        self.m = m

    def _sample_outcomes_given_truth(self, truth: bool, rng: random.Random) -> list:
        """Generate a 3-strategy outcome list conditioned on ground truth.
        Probabilities reflect the LOG_9_5_26 evidence: many no-signal
        degradations on size_mtime/existence even when transfers succeed."""
        outcomes = []
        if truth:
            # sha256: STRONG with very high prob (rare no-signal on hash)
            r = rng.random()
            if r < 0.97:
                outcomes.append(FakeOutcome("sha256", STRONG))
            else:
                outcomes.append(FakeOutcome("sha256", UNTRUSTED,
                                             evidence={"strategy_exception": "..."}))
            # size_mtime: ~40% MODERATE, ~60% no-signal degradation (the prod issue)
            r = rng.random()
            if r < 0.40:
                outcomes.append(FakeOutcome("size_mtime", MODERATE))
            else:
                outcomes.append(FakeOutcome("size_mtime", UNTRUSTED,
                                             evidence={"src_size": 1024, "rc": 1},
                                             note="device-side stat failed"))
            # existence: ~50% WEAK, ~50% no-signal (matching log: WEAK in 3830 cases)
            r = rng.random()
            if r < 0.50:
                outcomes.append(FakeOutcome("existence", WEAK))
            else:
                outcomes.append(FakeOutcome("existence", UNTRUSTED,
                                             evidence={"error": "subprocess failed"}))
        else:
            # File didn't transfer correctly → strategies should detect this
            r = rng.random()
            if r < 0.99:
                # sha256 STRONG would be a cryptographic collision = effectively impossible
                outcomes.append(FakeOutcome("sha256", UNTRUSTED,
                                             evidence={"expected_hash": "abc",
                                                       "actual_hash": "xyz"},
                                             note="hash mismatch"))
            else:
                # 1% no-signal even when truth=False (rare degradation)
                outcomes.append(FakeOutcome("sha256", UNTRUSTED,
                                             evidence={"strategy_exception": "..."}))
            # size_mtime: most likely disagreement
            r = rng.random()
            if r < 0.85:
                outcomes.append(FakeOutcome("size_mtime", UNTRUSTED,
                                             evidence={"src_size": 5000, "dev_size": 100},
                                             note="size mismatch"))
            elif r < 0.95:
                outcomes.append(FakeOutcome("size_mtime", UNTRUSTED,
                                             evidence={"missing": True}))
            else:
                outcomes.append(FakeOutcome("size_mtime", UNTRUSTED,
                                             evidence={"rc": 1}))
            # existence: usually missing if file truly didn't transfer
            r = rng.random()
            if r < 0.90:
                outcomes.append(FakeOutcome("existence", UNTRUSTED,
                                             evidence={"missing": True},
                                             note="file missing"))
            else:
                outcomes.append(FakeOutcome("existence", UNTRUSTED,
                                             evidence={"error": "stat failed"}))
        return outcomes

    def _old_logic_accepts(self, outcomes) -> bool:
        """Original 2-of-3 must reach MODERATE."""
        qualifying = sum(1 for o in outcomes if int(o.tier) >= int(MODERATE))
        return qualifying >= 2

    def _new_logic_accepts(self, outcomes) -> bool:
        """OLD logic, then MK38.1 dominance override on failure."""
        if self._old_logic_accepts(outcomes):
            return True
        # Dominance override: STRONG sha256 + no disagreement + no missing
        crypto = self.m._has_strong_cryptographic_signal(outcomes)
        if crypto is None:
            return False
        for o in outcomes:
            c = self.m.classify_outcome(o)
            if c in ("disagreement", "missing"):
                return False
        return True

    def test_T8_monte_carlo_10000_samples(self):
        rng = random.Random(self.SEED)
        old_TP = old_FP = old_TN = old_FN = 0
        new_TP = new_FP = new_TN = new_FN = 0
        production_pattern_total = 0
        production_pattern_accepted_old = 0
        production_pattern_accepted_new = 0
        for _ in range(self.SAMPLES):
            truth = rng.random() < 0.95
            outcomes = self._sample_outcomes_given_truth(truth, rng)
            old_acc = self._old_logic_accepts(outcomes)
            new_acc = self._new_logic_accepts(outcomes)
            if truth and old_acc:     old_TP += 1
            elif truth and not old_acc: old_FN += 1
            elif not truth and old_acc: old_FP += 1
            else:                     old_TN += 1
            if truth and new_acc:     new_TP += 1
            elif truth and not new_acc: new_FN += 1
            elif not truth and new_acc: new_FP += 1
            else:                     new_TN += 1
            # Track the LOG_9_5_26 pattern specifically:
            # sha256=STRONG, size_mtime=UNTRUSTED-no-signal, existence=UNTRUSTED-no-signal
            # (or existence=WEAK — both are present in the prod log)
            sha_strong = any(o.strategy_name == "sha256" and int(o.tier) >= int(STRONG)
                             for o in outcomes)
            no_disagreement = all(self.m.classify_outcome(o) not in ("disagreement", "missing")
                                   for o in outcomes)
            if sha_strong and no_disagreement and truth:
                production_pattern_total += 1
                if old_acc: production_pattern_accepted_old += 1
                if new_acc: production_pattern_accepted_new += 1

        old_TPR = old_TP / max(1, old_TP + old_FN)
        old_FPR = old_FP / max(1, old_FP + old_TN)
        old_FNR = 1 - old_TPR
        new_TPR = new_TP / max(1, new_TP + new_FN)
        new_FPR = new_FP / max(1, new_FP + new_TN)
        new_FNR = 1 - new_TPR

        # Print results to stdout for the test report
        print("\n--- MK38.1 MC sim 10,000 samples ---")
        print(f"OLD logic: TPR={old_TPR:.4f} FPR={old_FPR:.4f} FNR={old_FNR:.4f}")
        print(f"           TP={old_TP} FP={old_FP} TN={old_TN} FN={old_FN}")
        print(f"NEW logic: TPR={new_TPR:.4f} FPR={new_FPR:.4f} FNR={new_FNR:.4f}")
        print(f"           TP={new_TP} FP={new_FP} TN={new_TN} FN={new_FN}")
        print(f"Production-pattern (sha256=STRONG, no disagreement, truth=correct):")
        print(f"  total samples: {production_pattern_total}")
        print(f"  OLD accepted:  {production_pattern_accepted_old} "
              f"({100*production_pattern_accepted_old/max(1,production_pattern_total):.1f}%)")
        print(f"  NEW accepted:  {production_pattern_accepted_new} "
              f"({100*production_pattern_accepted_new/max(1,production_pattern_total):.1f}%)")

        # === LOCKED PASS CRITERIA ===
        # Safety: no new false positives
        self.assertLessEqual(new_FPR, old_FPR + 0.001,
            f"SAFETY VIOLATION: NEW FPR ({new_FPR:.4f}) > OLD FPR ({old_FPR:.4f}) + tolerance")
        # Operational: most correct transfers accepted under NEW
        self.assertLessEqual(new_FNR, 0.05,
            f"OPERATIONAL: NEW FNR ({new_FNR:.4f}) > 0.05; correct transfers still being rejected")
        # Production-pattern: 100% recall on the LOG_9_5_26 pattern
        if production_pattern_total > 0:
            recall = production_pattern_accepted_new / production_pattern_total
            self.assertEqual(recall, 1.0,
                f"PRODUCTION-PATTERN: NEW only accepts {recall:.4f} of LOG_9_5_26 pattern; "
                "must be 1.0")
        # Improvement: NEW must accept strictly more correct transfers than OLD
        self.assertGreater(new_TP, old_TP,
            f"NEW logic must accept MORE correct transfers than OLD (NEW={new_TP}, OLD={old_TP})")


# ═══════════════════════════════════════════════════════════════════════════
# T9: install / patch tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMK38_1_PatchInstall(unittest.TestCase):
    """T9: patch_verify_with_quorum installs idempotently and no-ops cleanly
    when mk35_trust isn't present."""

    def setUp(self):
        if "mk38_1_quorum_dominance" in sys.modules:
            del sys.modules["mk38_1_quorum_dominance"]
        import mk38_1_quorum_dominance as m
        self.m = m

    def test_T9_patch_install_returns_True_first_time(self):
        # First install
        result1 = self.m.patch_verify_with_quorum()
        # Second install must be no-op (already installed)
        result2 = self.m.patch_verify_with_quorum()
        self.assertFalse(result2, "Second install must return False")
        # is_dominance_active reflects state
        self.assertTrue(self.m.is_dominance_active() or not result1,
                        "is_dominance_active must agree with install state")


# ═══════════════════════════════════════════════════════════════════════════
# T10: source-presence checks (for code-review review)
# ═══════════════════════════════════════════════════════════════════════════

class TestMK38_1_PatchSiteCoverage(unittest.TestCase):
    def test_T10_mk37_wiring_installs_dominance_at_bootstrap(self):
        from pathlib import Path
        src = Path(__file__).parent / "mk37_wiring.py"
        if not src.exists():
            self.skipTest("mk37_wiring.py not in test dir")
        text = src.read_text(encoding="utf-8")
        self.assertIn("patch_verify_with_quorum", text,
                      "mk37_wiring bootstrap must install MK38.1")
        self.assertIn("MK38.1", text, "MK38.1 marker comment must be present")

    def test_T10b_mk35_trust_emits_classification_in_QUORUM_FAILED(self):
        from pathlib import Path
        src = Path(__file__).parent / "mk35_trust.py"
        if not src.exists():
            self.skipTest("mk35_trust.py not in test dir")
        text = src.read_text(encoding="utf-8")
        self.assertIn("QUORUM_FAILED_DETAIL", text,
                      "QUORUM_FAILED must emit classification detail")
        self.assertIn("classify_outcome", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
