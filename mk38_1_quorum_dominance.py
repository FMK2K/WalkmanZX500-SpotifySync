"""
mk38_1_quorum_dominance.py — MK38.1: SHA-256 Cryptographic Dominance Hotfix
================================================================================

Sprint:        MK38.1 — Quorum Dominance Hotfix (HOTFIX, ships immediately)
Closes:        LOG_9_5_26 production regression — 100% transfer failure
               (1963/1963 albums; 3,876 TRANSFER_QUORUM_FAILED events; 0 successful
               transfers) with dominant signature `sha256=STRONG, size_mtime=UNTRUSTED,
               existence=WEAK` in 3,830/3,876 cases (98.8%).

Root cause analysis (locked):
  The production failure cascade has TWO architectural causes that compound:

  1. AMBIGUOUS UNTRUSTED — TrustTier.UNTRUSTED conflates two semantically
     opposite states:
       • "no signal" — strategy couldn't compute a result (subprocess failure,
         stat call returned nothing, COM marshalling error). Evidence: {"error": ...}
         or {"rc": ...} or {"strategy_exception": ...}. Note: this is DEGRADATION,
         not disagreement.
       • "disagreement" — strategy computed a result that contradicts the source
         (e.g., dev_size != src_size). Evidence: {"src_size": N, "dev_size": M}
         with note="size mismatch". This is an ACTIVE contradiction.
     The quorum logic treats both as "below threshold" → both fail. But they
     have completely different safety implications.

  2. MISSED CRYPTOGRAPHIC DOMINANCE — when sha256 returns STRONG, the bytes on
     the device are byte-identical to the source. This is mathematically
     equivalent to "the transfer succeeded." The size_mtime and existence
     strategies are CHEAPER signals that are REDUNDANT with sha256:
       • size_mtime STRONG implies sha256 STRONG would pass (nothing more)
       • existence WEAK implies file exists (nothing more)
     They are not independent observations of bytes-match; they are degraded
     observations of the same thing sha256 already verified at the strongest
     level.

Design (adversarial CTO checklist applied):

  The MK38.1 hotfix introduces TWO complementary rules to the quorum logic,
  while preserving every safety property MK35/MK36/MK37 established:

  RULE A — Disagreement vs No-Signal Classification:
    Every UNTRUSTED outcome is now classified as either NO_SIGNAL or
    DISAGREEMENT based on its evidence dict. This is a DIAGNOSTIC enrichment;
    the existing quorum decision still applies.

  RULE B — Cryptographic Dominance Override:
    If ALL of the following hold:
      (1) At least one strategy returned STRONG with cryptographic content match
      (2) NO strategy returned DISAGREEMENT (i.e., no UNTRUSTED-with-mismatch
          evidence; UNTRUSTED-no-signal is OK)
      (3) NO strategy returned a result claiming the file is MISSING
    Then quorum is REACHED via "cryptographic dominance" with effective_tier=STRONG.

    This rule fires INSTEAD OF (not in addition to) the standard 2-of-N rule
    when its preconditions hold. The standard rule continues to apply when
    sha256 is not STRONG.

Why this is safe (independence / dominance argument):
  - SHA-256 is a 256-bit cryptographic hash with collision resistance ≈ 2^128
  - For SHA-256 STRONG to be a false positive, the device file would need to
    be a deliberate adversarial collision against the source — not an
    accidental partial-transfer artifact (partial files have different hashes)
  - Independence: a single STRONG cryptographic signal IS independent of the
    other strategies BY CONSTRUCTION. The other strategies are derived signals
    (size, mtime, existence) that are STRICTLY DOMINATED by content hash
  - Disagreement guard: if any other strategy reports an active contradiction
    (e.g., device-side size doesn't match source), we DO NOT override — we
    fail the quorum and flag the inconsistency. This catches the (impossible
    but defensively guarded) case of sha collision plus size mismatch.

Why this is empirically necessary:
  - LOG_9_5_26: 3,830 / 3,876 TRANSFER_QUORUM_FAILED events have signature
    sha256=STRONG with size_mtime=UNTRUSTED (degraded). All would have been
    accepted under MK38.1 dominance.
  - 0 successful transfers across 1,963 albums in 6+ hours means the system
    is operationally broken under the current threshold.

Public API:
  classify_outcome(outcome) -> Literal["positive", "no_signal", "disagreement", "missing"]
  apply_dominance_override(outcomes, threshold_tier, threshold_count, ...) -> QuorumOutcome
  patch_verify_with_quorum() -> None      # idempotent monkey-patch; preferred install path
  is_dominance_active() -> bool
  snapshot_dominance_telemetry() -> Dict[str, int]
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Outcome classification — distinguish no-signal from disagreement
# ═══════════════════════════════════════════════════════════════════════════

# Evidence keys / note substrings that indicate the strategy COULDN'T COMPUTE
# a result. These are degradations, not contradictions.
_NO_SIGNAL_EVIDENCE_KEYS = frozenset({
    "error", "rc", "strategy_exception", "raw", "timeout",
})
_NO_SIGNAL_NOTE_SUBSTRINGS = (
    "stat failed", "stat output parse failed", "raised", "no signal",
    "treated as no-signal", "src_stat_failed",
)

# Evidence keys / note substrings that indicate the strategy DID compute a
# result and the result is "the file is missing on device."
_MISSING_EVIDENCE_KEYS = frozenset({"missing", "not_found", "enoent"})
_MISSING_NOTE_SUBSTRINGS = (
    "file missing", "does not exist", "not found",
)

# Evidence keys / note substrings that indicate the strategy DID compute a
# result and the result is "active disagreement" (e.g., sizes differ).
_DISAGREEMENT_NOTE_SUBSTRINGS = (
    "size mismatch", "hash mismatch", "content mismatch", "mtime mismatch",
)


def classify_outcome(outcome: Any) -> str:
    """
    Classify a VerificationOutcome into one of:
      "positive"      — tier > UNTRUSTED (strategy succeeded with some confidence)
      "no_signal"     — UNTRUSTED because of degradation (couldn't compute)
      "disagreement"  — UNTRUSTED because of an actively-detected mismatch
      "missing"       — UNTRUSTED because the device-side file is reported missing

    Critical: "no_signal" and "disagreement" both report tier=UNTRUSTED in the
    legacy code, but they have OPPOSITE safety implications. This function
    teases them apart by examining evidence + notes.

    The classification is conservative: ambiguous cases default to
    "disagreement" (the safer interpretation) so a borderline case never
    triggers the dominance override falsely.
    """
    tier_val = getattr(outcome, "tier", None)
    if tier_val is None:
        return "no_signal"
    # tier may be a TrustTier enum; compare by int value to avoid coupling
    try:
        tier_int = int(tier_val)
    except (TypeError, ValueError):
        return "no_signal"

    # tier > 0 means at least WEAK — this is a positive observation
    if tier_int > 0:
        return "positive"

    # tier == 0 (UNTRUSTED). Disambiguate via evidence + note.
    evidence = getattr(outcome, "evidence", None) or {}
    note = (getattr(outcome, "note", None) or "").lower()

    # MISSING beats DISAGREEMENT beats NO_SIGNAL when multiple match
    if any(k in evidence for k in _MISSING_EVIDENCE_KEYS):
        return "missing"
    if any(s in note for s in _MISSING_NOTE_SUBSTRINGS):
        return "missing"
    if any(s in note for s in _DISAGREEMENT_NOTE_SUBSTRINGS):
        return "disagreement"
    # Look for size/hash mismatch shape in evidence
    # (e.g., evidence={"src_size": N, "dev_size": M} with N != M)
    if "src_size" in evidence and "dev_size" in evidence:
        try:
            if int(evidence["src_size"]) != int(evidence["dev_size"]):
                return "disagreement"
        except (TypeError, ValueError):
            pass
    if "expected_hash" in evidence and "actual_hash" in evidence:
        if evidence["expected_hash"] != evidence["actual_hash"]:
            return "disagreement"
    if any(k in evidence for k in _NO_SIGNAL_EVIDENCE_KEYS):
        return "no_signal"
    if any(s in note for s in _NO_SIGNAL_NOTE_SUBSTRINGS):
        return "no_signal"
    # Default: ambiguous UNTRUSTED with no evidence → assume disagreement
    # (conservative: never override on ambiguous evidence)
    return "disagreement"


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry
# ═══════════════════════════════════════════════════════════════════════════

_TELEMETRY: Dict[str, int] = {
    "dominance_overrides_applied":     0,
    "dominance_overrides_skipped_disagreement": 0,
    "dominance_overrides_skipped_missing":      0,
    "dominance_overrides_unavailable_no_strong_sha": 0,
    "outcomes_classified_positive":    0,
    "outcomes_classified_no_signal":   0,
    "outcomes_classified_disagreement":0,
    "outcomes_classified_missing":     0,
}
_TELEMETRY_LOCK = threading.RLock()
_INSTALLED = False


def _bump(metric: str, n: int = 1) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY[metric] = _TELEMETRY.get(metric, 0) + n


def snapshot_dominance_telemetry() -> Dict[str, int]:
    with _TELEMETRY_LOCK:
        return dict(_TELEMETRY)


def is_dominance_active() -> bool:
    with _TELEMETRY_LOCK:
        return _INSTALLED


# ═══════════════════════════════════════════════════════════════════════════
# Dominance rule
# ═══════════════════════════════════════════════════════════════════════════

def _has_strong_cryptographic_signal(outcomes: Sequence[Any]) -> Optional[Any]:
    """
    Return the first outcome that constitutes a strong cryptographic signal,
    or None if none exists. "Strong cryptographic" means the strategy is named
    in a way that identifies it as content-hash-based AND its tier is STRONG.

    Strategy name allowlist for cryptographic dominance:
      sha256, blake2b, blake2s, blake3, content_hash
    """
    crypto_strategy_names = frozenset({"sha256", "blake2b", "blake2s",
                                         "blake3", "content_hash"})
    for o in outcomes:
        name = getattr(o, "strategy_name", "")
        tier_val = getattr(o, "tier", None)
        try:
            tier_int = int(tier_val) if tier_val is not None else 0
        except (TypeError, ValueError):
            tier_int = 0
        # Tier value 3 is STRONG in the canonical TrustTier enum
        if name in crypto_strategy_names and tier_int >= 3:
            return o
    return None


def apply_dominance_override(outcomes: Sequence[Any],
                              quorum_outcome_factory,
                              threshold_tier: Any,
                              threshold_count: int,
                              independence_report_dict: Optional[dict] = None) -> Optional[Any]:
    """
    Determine whether the cryptographic dominance rule applies. If yes,
    return a QuorumOutcome representing dominance-verified state. If no,
    return None (caller falls through to standard quorum logic).

    Preconditions for dominance (ALL must hold):
      (1) At least one outcome is a STRONG cryptographic signal
      (2) NO outcome is classified as "disagreement"
      (3) NO outcome is classified as "missing"

    The classification of every outcome is also tallied into telemetry.
    """
    # Tally classifications
    classifications = []
    has_disagreement = False
    has_missing = False
    for o in outcomes:
        c = classify_outcome(o)
        classifications.append(c)
        _bump(f"outcomes_classified_{c}")
        if c == "disagreement":
            has_disagreement = True
        elif c == "missing":
            has_missing = True

    crypto = _has_strong_cryptographic_signal(outcomes)
    if crypto is None:
        _bump("dominance_overrides_unavailable_no_strong_sha")
        return None
    if has_disagreement:
        _bump("dominance_overrides_skipped_disagreement")
        log.warning(
            f"  [MK38.1] DOMINANCE_SKIPPED_DISAGREEMENT — sha256=STRONG present "
            f"but at least one strategy reports active disagreement; "
            f"falling through to standard 2-of-N quorum (which will fail safe)."
        )
        return None
    if has_missing:
        _bump("dominance_overrides_skipped_missing")
        log.warning(
            f"  [MK38.1] DOMINANCE_SKIPPED_MISSING — sha256=STRONG present "
            f"but at least one strategy reports the file is MISSING; "
            f"this is structurally impossible (sha256 verified bytes exist) "
            f"so falling through to standard quorum to flag the inconsistency."
        )
        return None

    # Override applies. Build a QuorumOutcome with effective_tier=STRONG.
    _bump("dominance_overrides_applied")
    log.info(
        f"  [MK38.1] QUORUM_DOMINANT_SHA256_OVERRIDE strategy={crypto.strategy_name!r} "
        f"tier=STRONG; other strategies: " +
        ", ".join(f"{getattr(o,'strategy_name','?')}={c}" for o, c in zip(outcomes, classifications))
    )

    # Construct outcome via the factory the caller passed (decoupling).
    # The factory is `QuorumOutcome` — we replicate the shape of the
    # success branch in verify_with_quorum.
    try:
        from mk35_trust import TrustTier
        effective = TrustTier.STRONG
    except ImportError:
        effective = getattr(crypto, "tier", None)

    return quorum_outcome_factory(
        quorum_reached=True,
        effective_tier=effective,
        threshold_tier=threshold_tier,
        threshold_count=threshold_count,
        outcomes=tuple(outcomes),
        note="cryptographic_dominance_mk38_1",
        independence=independence_report_dict,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Install — monkey-patch verify_with_quorum to add the dominance check
# ═══════════════════════════════════════════════════════════════════════════

def patch_verify_with_quorum() -> bool:
    """
    Install the MK38.1 dominance rule into mk35_trust.verify_with_quorum.
    Idempotent: subsequent calls return False without re-patching.

    The patched function runs the original quorum logic, then:
      - If the original returned quorum_reached=True, returns it unchanged
        (no behavioral change for already-passing transfers).
      - If the original returned quorum_reached=False, attempts the
        dominance override. If dominance applies, returns the override.
        Otherwise returns the original failure.

    This wrap-then-override pattern ensures:
      - No regression in safety: the original quorum still runs and its
        independence check still applies.
      - No regression in observability: a dominance override emits its own
        log line and is counted in telemetry.
    """
    global _INSTALLED
    with _TELEMETRY_LOCK:
        if _INSTALLED:
            return False

    try:
        import mk35_trust
    except ImportError:
        log.error("  [MK38.1] cannot install: mk35_trust unavailable")
        return False

    if not hasattr(mk35_trust, "verify_with_quorum"):
        log.error("  [MK38.1] cannot install: mk35_trust.verify_with_quorum missing")
        return False

    original_verify = mk35_trust.verify_with_quorum
    try:
        QuorumOutcome = mk35_trust.QuorumOutcome
    except AttributeError:
        log.error("  [MK38.1] cannot install: mk35_trust.QuorumOutcome missing")
        return False

    def _wrapped(*args, **kwargs):
        # MK39 Phase II — Adversarial-review noise demotion (SWE concern):
        # buffer the inner verify's QUORUM_FAILED WARNINGs so we can decide
        # whether to flush them (no salvage) or drop them with a single INFO
        # summary (salvage applied). 330+ noise WARNINGs/run was the trigger.
        _DeferredCtx = getattr(mk35_trust, "DeferredQuorumWarnings", None)
        if _DeferredCtx is None:
            # Older mk35_trust without the deferred pattern — fall back to
            # the original behavior (warnings emit immediately).
            result = original_verify(*args, **kwargs)
            if getattr(result, "quorum_reached", False):
                return result
            outcomes = getattr(result, "outcomes", ())
            threshold_tier = getattr(result, "threshold_tier", None)
            threshold_count = getattr(result, "threshold_count", 2)
            independence = getattr(result, "independence", None)
            override = apply_dominance_override(
                outcomes,
                quorum_outcome_factory=QuorumOutcome,
                threshold_tier=threshold_tier,
                threshold_count=threshold_count,
                independence_report_dict=independence,
            )
            return override if override is not None else result

        with _DeferredCtx() as _ctx:
            result = original_verify(*args, **kwargs)
            # Original passed → no warnings would have been emitted; nothing
            # to flush, nothing to drop.
            if getattr(result, "quorum_reached", False):
                return result
            # Original failed → try dominance override
            outcomes = getattr(result, "outcomes", ())
            threshold_tier = getattr(result, "threshold_tier", None)
            threshold_count = getattr(result, "threshold_count", 2)
            independence = getattr(result, "independence", None)
            override = apply_dominance_override(
                outcomes,
                quorum_outcome_factory=QuorumOutcome,
                threshold_tier=threshold_tier,
                threshold_count=threshold_count,
                independence_report_dict=independence,
            )
            if override is not None:
                # Salvage applied. Drop buffered WARNINGs — the INFO line
                # emitted inside apply_dominance_override is sufficient.
                _ctx.discard()
                return override
            # Salvage NOT applied. Flush the buffered WARNINGs so the
            # operator can diagnose the genuine failure.
            _ctx.flush()
            return result

    mk35_trust.verify_with_quorum = _wrapped
    with _TELEMETRY_LOCK:
        _INSTALLED = True
    log.info("  [MK38.1] DOMINANCE_OVERRIDE_INSTALLED — wrapping mk35_trust.verify_with_quorum")
    return True


__all__ = [
    "classify_outcome",
    "apply_dominance_override",
    "patch_verify_with_quorum",
    "is_dominance_active",
    "snapshot_dominance_telemetry",
]
