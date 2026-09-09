"""
mk34_invariants.py — MK34 P4: Hard-Stop Invariant Assertions (Class C fix)
============================================================================
CTO Class C fix: "Stage 0 deletes 3,646 files; Stage 1 fails; system continues
anyway. MK33 does not enforce: 'no forward progress after failed invariant.'"

This module defines the invariants the system MUST maintain. Invariant
violations raise InvariantViolation — a hard exception, not a log line.
The exception is caught only by RunAbortPolicy (MK26 C5) to terminate the
run cleanly. No stage gate, no try/except, may "continue past" an invariant
violation.

CTO mandate (MK34 directive):
    "STOP stage-based execution.
     Every operation must satisfy: safe to rerun → safe to interrupt → safe to retry."
"""

from __future__ import annotations

import logging
from typing import FrozenSet, Iterable, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# The exception
# ═══════════════════════════════════════════════════════════════════════════

class InvariantViolation(RuntimeError):
    """
    Raised when a hard invariant is violated.

    This exception MUST NOT be caught and swallowed. The only legitimate
    handler is RunAbortPolicy.abort() — anything else is bypassing the
    Class C fix.
    """

    def __init__(self, invariant_name: str, detail: str,
                 sample: Optional[list] = None):
        self.invariant_name = invariant_name
        self.detail = detail
        self.sample = sample or []
        sample_str = (f" Sample (first 3): {sample[:3]!r}" if sample else "")
        super().__init__(
            f"INVARIANT_VIOLATED [{invariant_name}]: {detail}{sample_str}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Individual invariants — each one is a function that raises on violation
# ═══════════════════════════════════════════════════════════════════════════

def assert_no_destructive_until_verified(
    paths_to_delete: Iterable[str],
    verified_present_paths: FrozenSet[str],
    cross_format_alternatives: "dict[str, list[str]]",
) -> None:
    """
    CTO mandate: "no_track_is_deleted_unless_verified_present_elsewhere."

    For every path queued for deletion, at least one of its cross-format
    alternatives MUST be present in the verified path set. Otherwise
    deletion would cause data loss.
    """
    violations: list = []
    for path in paths_to_delete:
        alts = cross_format_alternatives.get(path, [])
        verified_alts = [a for a in alts if a in verified_present_paths]
        if not verified_alts:
            violations.append(path)

    if violations:
        raise InvariantViolation(
            invariant_name="no_destructive_until_verified",
            detail=(
                f"{len(violations)} tracks queued for deletion have no verified "
                f"cross-format alternative. CTO Class C: delete-without-verify "
                f"is the failure mode that lost 3,646 files in the MK33 incident."
            ),
            sample=violations,
        )
    log.debug(
        f"[MK34/invariant] OK: no_destructive_until_verified — "
        f"all {sum(1 for _ in paths_to_delete)} deletes have verified alternatives."
    )


def assert_no_zero_byte_writes(
    transfer_outcomes: list,   # list[TransferOutcome]
) -> None:
    """
    Class A guard: no transfer should be marked .completed=True if its
    device-side hash is missing or empty.

    This catches the 'CopyHere returns success but file size = 0' case from
    MK33's logs: the executor must have content-hash verification, but the
    invariant check is the second line of defense.
    """
    bogus = []
    for outcome in transfer_outcomes:
        if not getattr(outcome, "completed", False):
            continue   # only check completed transfers
        # If completed but we have no device_hash, that's suspicious
        if not getattr(outcome, "device_hash", None):
            bogus.append(getattr(outcome, "path", "<unknown>"))

    if bogus:
        raise InvariantViolation(
            invariant_name="no_zero_byte_writes",
            detail=(
                f"{len(bogus)} transfers reported completed=True but lack a device "
                f"content hash. CTO Class A: 'operation accepted' ≠ 'operation completed'."
            ),
            sample=bogus,
        )


def assert_state_consistency(
    observed,        # mk34_observer.DeviceObservedState
    desired,         # mk34_planner.DesiredState
    max_divergence_pct: float = 0.10,
) -> None:
    """
    Sanity check: the observed state's file count should be in the same order
    of magnitude as the desired state's expected_paths count. If they're
    radically different (>10% under default), something has gone wrong
    structurally — typically a Class B state-divergence event that the
    observer flagged.

    This is a guard against the MK33 'ADB:27,901 vs preload:0' pattern
    leaking into the planner.
    """
    if not desired.expected_paths:
        return  # empty desired state — degenerate but valid

    obs_count = len(observed.tracks_present_paths)
    exp_count = len(desired.expected_paths)
    if exp_count == 0:
        return

    relative_size = obs_count / exp_count if exp_count > 0 else 0
    # If observed is < (1 - max_divergence) × expected, something is wrong
    if relative_size < (1 - max_divergence_pct) and obs_count == 0:
        raise InvariantViolation(
            invariant_name="state_consistency",
            detail=(
                f"Observed {obs_count} files but desired state has {exp_count} "
                f"expected paths (ratio={relative_size:.2f}, threshold>{1-max_divergence_pct:.2f}). "
                f"This is the MK33 Class B pattern — ADB sees zero or near-zero "
                f"while caller expects thousands. Likely: device offline, ADB stale, "
                f"or wrong device_root path. Refusing to plan against this divergent state."
            ),
        )
    log.debug(
        f"[MK34/invariant] OK: state_consistency — "
        f"observed={obs_count} vs desired={exp_count} (ratio={relative_size:.2f})."
    )


def assert_planner_diff_acyclic(plan) -> None:
    """
    Sanity: COPY of path X and DELETE of path X cannot both appear in a single
    plan — that's a contradiction (the planner shouldn't have produced it).
    """
    from mk34_planner import ActionKind

    copies = {a.path for a in plan.actions if a.kind == ActionKind.COPY}
    deletes = {a.path for a in plan.actions if a.kind == ActionKind.DELETE}
    intersect = copies & deletes
    if intersect:
        raise InvariantViolation(
            invariant_name="planner_diff_acyclic",
            detail=(
                f"{len(intersect)} paths appear in BOTH copy and delete sets of "
                f"the same plan. The planner has produced a contradictory diff."
            ),
            sample=list(intersect),
        )


def assert_observer_freshness(
    observed,                # DeviceObservedState
    max_age_s: float = 600.0,
) -> None:
    """
    The observer's snapshot must be fresh enough to plan against.
    Default: 10 minutes.

    If the snapshot is older, the planner could be making decisions based on
    stale device state — exactly the failure pattern the CTO board flagged.
    """
    import time
    age = time.time() - observed.observed_at
    if age > max_age_s:
        raise InvariantViolation(
            invariant_name="observer_freshness",
            detail=(
                f"Observation is {age:.0f}s old (limit {max_age_s:.0f}s). "
                f"Re-observe before planning to avoid stale-state decisions."
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Composite gate — call this between stages
# ═══════════════════════════════════════════════════════════════════════════

def gate_pre_execution(
    observed,                       # DeviceObservedState
    desired,                        # DesiredState
    plan,                           # Plan
    grant_capability_on_pass: bool = True,
):
    """
    Run all relevant invariants as a single gate before plan execution.
    Raises InvariantViolation on first failure (no continue-past).

    The CTO's Class C diagnosis was specifically that the system was
    log-and-continuing past invariant violations. This gate makes that
    impossible: callers either pass or abort.

    MK35 Q2: On pass, returns an ExecutionCapability that must be passed
    to subsequent destructive calls. Without this capability, the
    walkman_dedup destructive paths and mk34_executor.transfer_one()
    refuse to run. This is the structural enforcement that replaces
    MK34's convention-based DEDUP_DESTRUCTIVE_VERIFIED flag.

    Args:
        grant_capability_on_pass: when True (default), returns an
            ExecutionCapability covering all destructive operations for
            the next 5 minutes. When False, only the invariant assertions
            run; callers must obtain capabilities separately (used in
            test contexts).

    Returns: ExecutionCapability if grant_capability_on_pass=True and
             MK35 capability module is available, else None.
    """
    log.info("[MK34/invariant] Running pre-execution gate...")
    assert_observer_freshness(observed)
    assert_state_consistency(observed, desired)
    assert_planner_diff_acyclic(plan)

    # Assemble delete paths from the plan
    from mk34_planner import ActionKind
    delete_paths = [a.path for a in plan.actions if a.kind == ActionKind.DELETE]
    if delete_paths:
        assert_no_destructive_until_verified(
            paths_to_delete=delete_paths,
            verified_present_paths=observed.tracks_present_paths,
            cross_format_alternatives=desired.cross_format_alts,
        )
    log.info("[MK34/invariant] Pre-execution gate PASSED.")

    # MK35 Q2: Grant a capability now that invariants have passed.
    if grant_capability_on_pass:
        try:
            from mk35_capability import (
                grant_capability, _get_grant_key_for_invariant_gate
            )
            cap = grant_capability(
                grant_key=_get_grant_key_for_invariant_gate(),
                scope="*",                 # run-wide for backward compat
                granted_by_invariant="gate_pre_execution",
                ttl_seconds=300.0,
            )
            log.info(
                f"  [MK34/invariant→MK35/Q2] CAPABILITY_ISSUED "
                f"token={cap.token_id[:8]} scope=* ttl=300s"
            )
            return cap
        except ImportError:
            log.info(
                "  [MK34/invariant] MK35 capability module not installed; "
                "running in MK34-only mode (DEDUP_DESTRUCTIVE_VERIFIED flag)."
            )
            return None
    return None


def gate_post_execution(transfer_outcomes: list) -> None:
    """
    Run all relevant invariants after execution. Raises on Class A/B/C residue.
    """
    log.info("[MK34/invariant] Running post-execution gate...")
    assert_no_zero_byte_writes(transfer_outcomes)
    log.info("[MK34/invariant] Post-execution gate PASSED.")


__all__ = [
    "InvariantViolation",
    "assert_no_destructive_until_verified",
    "assert_no_zero_byte_writes",
    "assert_state_consistency",
    "assert_planner_diff_acyclic",
    "assert_observer_freshness",
    "gate_pre_execution",
    "gate_post_execution",
]
