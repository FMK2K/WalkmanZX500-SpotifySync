"""
mk37_2_renewal.py — MK37.2-1: Capability TTL renewal architecture
================================================================================

Sprint:        MK37.2 — Operational Endurance
Closes:        LOG_8_5_26 architectural defect — token=7d7e8348 expired 27,901s
               into the run with 5,001 CAPABILITY_REJECTED cascading and 0/2,688
               albums transferred.
Risk closed:   New risk #6 (Capability Lifetime Mismatch) discovered post-MK37.1.
Board cherry:  §11 of post-MK37.1 review — "long enrichment phases preceding
               destructive transfer must not leave the pass capability stale."

Design choice — STAGE-BOUND CAPABILITIES with DEMAND RENEWAL:
  - The pass capability remains issued at the start of run_sync, scope='*',
    ttl=2h.  Back-compat is preserved.
  - A new helper `renew_pass_capability_if_stale(min_remaining_s)` re-issues
    the pass capability when its remaining lifetime drops below the threshold.
    The renewal is gated on the same _GRANT_KEY invariant as initial issuance,
    so the security model is unchanged: only the invariant gate may issue.
  - `transfer_via_legacy_with_quorum` calls `renew_pass_capability_if_stale`
    before its capability check, with min_remaining=300s (5min) by default.
    This is the single mediation point — every transfer routes through it,
    so renewal coverage is guaranteed by MK37.1-1's authority graph.
  - `begin_destructive_stage(stage_name)` is a NEW API for explicit stage
    boundaries (Stage 0 download, Stage 6 transfer, Stage 8 audit re-tag).
    Calling it forces a fresh capability and binds a `stage_name` for
    telemetry — the marker `STAGE_CAPABILITY_BOUND stage=<name>` lets the
    operator correlate which stage was running when a rejection fired.
  - Renewal events are written to .mk37_2_renewals.jsonl for replay
    (one line per renewal: {ts, old_token_short, new_token_short,
    old_age_s, reason, stage}). Idempotent append, JSONL format consistent
    with .mk34_audit.jsonl and .mk37_idempotency.jsonl.

Why NOT renew-on-rejection:
  Renewing on rejection would defeat the security model — any caller that
  triggers a rejection would receive a fresh capability, making rejections
  unobservable. Rejections must remain meaningful failure signals. We renew
  PROACTIVELY based on remaining lifetime, BEFORE the check fires. This
  keeps rejections strictly out-of-band.

Why NOT raise the TTL to 12h or 24h:
  The board's MK37 §6 framing — "capabilities are not session tokens, they
  are transactional authorizations" — forbids unbounded TTL. A long TTL
  would also make crash-recovery reconciliation (MK37.1-4) less reliable:
  the older the capability, the wider the window in which a partial
  shutdown could leave a stranded grant. Stage-bound renewal preserves
  the transactional model.

Public API:
    renew_pass_capability_if_stale(min_remaining_s: float = 300.0) -> bool
    begin_destructive_stage(stage_name: str, ttl_seconds: float = 7200.0) -> Optional[Any]
    end_destructive_stage(stage_name: str) -> None
    current_stage() -> Optional[str]
    snapshot_renewal_telemetry() -> Dict[str, int]
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Telemetry — additive, separate dict from mk37_wiring._TELEMETRY
# ═══════════════════════════════════════════════════════════════════════════

_RENEWAL_TELEMETRY: Dict[str, int] = {
    "capability_renewals":              0,
    "capability_renewal_failures":      0,
    "capability_renewals_by_stage":     0,    # renewed because stage changed
    "capability_renewals_by_staleness": 0,    # renewed because TTL low
    "stage_bindings":                   0,
    "stage_unbindings":                 0,
}
_RENEWAL_TELEMETRY_LOCK = threading.RLock()


def _bump(metric: str, n: int = 1) -> None:
    with _RENEWAL_TELEMETRY_LOCK:
        _RENEWAL_TELEMETRY[metric] = _RENEWAL_TELEMETRY.get(metric, 0) + n


def snapshot_renewal_telemetry() -> Dict[str, int]:
    """Operations dashboard accessor — additive to snapshot_telemetry()."""
    with _RENEWAL_TELEMETRY_LOCK:
        return dict(_RENEWAL_TELEMETRY)


# ═══════════════════════════════════════════════════════════════════════════
# Stage tracker — current destructive stage name
# ═══════════════════════════════════════════════════════════════════════════

_STAGE_LOCK = threading.RLock()
_CURRENT_STAGE: Optional[str] = None
_STAGE_HISTORY: list = []   # [(stage_name, started_at, ended_at_or_none), ...]


def current_stage() -> Optional[str]:
    """Returns the active destructive stage name, or None."""
    with _STAGE_LOCK:
        return _CURRENT_STAGE


def _stage_history_snapshot() -> list:
    with _STAGE_LOCK:
        return list(_STAGE_HISTORY)


# ═══════════════════════════════════════════════════════════════════════════
# Renewal journal — JSONL replay log
# ═══════════════════════════════════════════════════════════════════════════

_RENEWAL_JOURNAL = Path(os.environ.get(
    "MK37_2_RENEWAL_JOURNAL",
    str(Path(__file__).resolve().parent.parent / "state" / ".mk37_2_renewals.jsonl"),
))
_JOURNAL_LOCK = threading.Lock()


def _append_renewal_event(event: dict) -> None:
    """Atomic-append a renewal event to the JSONL journal."""
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        with _JOURNAL_LOCK:
            with open(_RENEWAL_JOURNAL, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except Exception as e:
        # Journal write failure must not block the run; log and continue.
        log.warning(f"  [MK37.2-1] renewal journal append failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Core renewal — preserves _GRANT_KEY invariant
# ═══════════════════════════════════════════════════════════════════════════

# Renewal serialization: only one renewer at a time. mk37_wiring._PASS_LOCK
# also serializes begin_sync_pass/end_sync_pass; we acquire it transitively
# by calling those functions, but a separate lock here prevents two threads
# both deciding to renew at the same instant and racing for the new token.
_RENEWAL_LOCK = threading.RLock()


def renew_pass_capability_if_stale(min_remaining_s: float = 300.0,
                                    *,
                                    reason: str = "staleness_check",
                                    stage: Optional[str] = None) -> bool:
    """
    Re-issue the pass capability if its remaining lifetime is below threshold.

    Renewal goes through the same `_GRANT_KEY` invariant gate as initial
    issuance — the security model is preserved. Only the invariant gate may
    issue capabilities; this helper is the invariant gate's renewal entry
    point, NOT a way to mint capabilities outside the gate.

    Returns True if renewal occurred, False if the existing capability was
    still fresh enough.

    Markers:
        CAPABILITY_RENEWED old=<short> new=<short> elapsed=Xs reason=<r>
        CAPABILITY_RENEWAL_FAILED reason=<exception>
    """
    try:
        import mk37_wiring as _wiring
    except ImportError:
        log.debug("  [MK37.2-1] mk37_wiring unavailable; cannot renew")
        return False

    with _RENEWAL_LOCK:
        cap = _wiring.get_pass_capability()
        now = time.time()

        # No active capability — nothing to renew (caller should have called
        # begin_sync_pass first; we don't fabricate one out of thin air).
        if cap is None:
            log.debug("  [MK37.2-1] no active pass capability — skip renewal")
            return False

        # Check remaining lifetime
        remaining = cap.expires_at - now
        if remaining > min_remaining_s and reason == "staleness_check":
            # Fresh enough; no renewal needed
            return False

        old_token_short = cap.token_id[:8]
        old_granted_at  = cap.granted_at
        old_age_s       = now - old_granted_at

        # Re-issue. begin_sync_pass internally calls revoke_all_capabilities
        # then grant_capability with _GRANT_KEY — we route through it so the
        # invariant remains structurally enforced.
        try:
            new_cap = _wiring.begin_sync_pass(scope=cap.scope,
                                               ttl_seconds=cap.expires_at - cap.granted_at)
        except Exception as e:
            _bump("capability_renewal_failures")
            log.error(
                f"  [MK37.2-1] CAPABILITY_RENEWAL_FAILED "
                f"old_token={old_token_short} reason={type(e).__name__}: {e}"
            )
            _append_renewal_event({
                "ts":              now,
                "event":           "renewal_failed",
                "old_token_short": old_token_short,
                "old_age_s":       old_age_s,
                "reason":          f"{type(e).__name__}: {e}",
                "stage":           stage or current_stage(),
            })
            return False

        if new_cap is None:
            _bump("capability_renewal_failures")
            log.error(
                f"  [MK37.2-1] CAPABILITY_RENEWAL_FAILED old_token={old_token_short} "
                f"reason=begin_sync_pass returned None (mk35_capability missing?)"
            )
            return False

        new_token_short = new_cap.token_id[:8]
        _bump("capability_renewals")
        if reason == "stage_change":
            _bump("capability_renewals_by_stage")
        else:
            _bump("capability_renewals_by_staleness")

        log.info(
            f"  [MK37.2-1] CAPABILITY_RENEWED "
            f"old={old_token_short} new={new_token_short} "
            f"old_age_s={old_age_s:.0f} reason={reason} "
            f"stage={stage or current_stage() or '*'}"
        )
        _append_renewal_event({
            "ts":               now,
            "event":            "renewed",
            "old_token_short":  old_token_short,
            "new_token_short":  new_token_short,
            "old_age_s":        old_age_s,
            "reason":           reason,
            "stage":            stage or current_stage(),
        })
        return True


# ═══════════════════════════════════════════════════════════════════════════
# Stage binding — explicit stage boundaries
# ═══════════════════════════════════════════════════════════════════════════

def begin_destructive_stage(stage_name: str,
                             ttl_seconds: float = 7200.0,
                             *,
                             force_renewal: bool = True) -> Optional[Any]:
    """
    Declare entry into a destructive stage. Forces a fresh capability so
    the stage starts with full TTL — guarantees that stages preceded by
    long non-destructive phases (enrichment, ingest scan) don't carry
    stale capabilities into transfer.

    `force_renewal=False` skips renewal if the existing capability still
    has > min_remaining_s lifetime; useful for chained destructive stages
    (Stage 6 → Stage 7) where re-issuing every time is wasteful.

    Markers:
        STAGE_CAPABILITY_BOUND stage=<name> token=<short> ttl=<s>
    """
    global _CURRENT_STAGE

    try:
        import mk37_wiring as _wiring
    except ImportError:
        log.warning(f"  [MK37.2-1] cannot bind stage {stage_name!r}; mk37_wiring missing")
        return None

    with _STAGE_LOCK:
        prev_stage = _CURRENT_STAGE
        _CURRENT_STAGE = stage_name
        _STAGE_HISTORY.append((stage_name, time.time(), None))
        _bump("stage_bindings")

    if force_renewal:
        # Renew unconditionally for stage entry — full TTL window for the stage
        cap = _wiring.begin_sync_pass(scope="*", ttl_seconds=ttl_seconds)
    else:
        # Renew only if stale
        renewed = renew_pass_capability_if_stale(
            min_remaining_s=300.0,
            reason="stage_change",
            stage=stage_name,
        )
        cap = _wiring.get_pass_capability()

    if cap is None:
        log.warning(f"  [MK37.2-1] stage {stage_name!r} bound but no capability available")
        return None

    log.info(
        f"  [MK37.2-1] STAGE_CAPABILITY_BOUND stage={stage_name!r} "
        f"token={cap.token_id[:8]} ttl={ttl_seconds:.0f}s "
        f"prev_stage={prev_stage!r}"
    )
    return cap


def end_destructive_stage(stage_name: str) -> None:
    """
    Mark the end of a destructive stage. Does NOT revoke the capability —
    that's `end_sync_pass()`'s job at run completion. This just clears the
    stage-name binding for telemetry.

    Marker:
        STAGE_CAPABILITY_UNBOUND stage=<name>
    """
    global _CURRENT_STAGE

    with _STAGE_LOCK:
        if _CURRENT_STAGE != stage_name:
            log.debug(
                f"  [MK37.2-1] end_destructive_stage({stage_name!r}) but "
                f"current is {_CURRENT_STAGE!r}; ignoring"
            )
            return
        _CURRENT_STAGE = None
        # Stamp the end timestamp on the most recent matching history entry
        for i in range(len(_STAGE_HISTORY) - 1, -1, -1):
            name, started_at, ended_at = _STAGE_HISTORY[i]
            if name == stage_name and ended_at is None:
                _STAGE_HISTORY[i] = (name, started_at, time.time())
                break
        _bump("stage_unbindings")

    log.info(f"  [MK37.2-1] STAGE_CAPABILITY_UNBOUND stage={stage_name!r}")


# ═══════════════════════════════════════════════════════════════════════════
# Module exports
# ═══════════════════════════════════════════════════════════════════════════

__all__ = [
    "renew_pass_capability_if_stale",
    "begin_destructive_stage",
    "end_destructive_stage",
    "current_stage",
    "snapshot_renewal_telemetry",
]
