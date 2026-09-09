"""
transport_circuit_breaker.py — MK39.1 L6.4 / Board GOV-E1-E4
================================================================================

Board Review Revision 2 — Governance §E (Upload Failure Circuit Breaker):

  GOV-E1 — 10 consecutive failures triggers ABORT (NOT a rolling window —
           the 385-error run in LOG_11_5_26 was systematic, every track
           of the same class failed; rolling would delay detection)

  GOV-E2 — Escalation ladder:
             3 consecutive  → WARNING  "Upload error streak: 3. Monitoring."
             5 consecutive  → ERROR    "Suspect systematic failure."
            10 consecutive  → CRITICAL "CIRCUIT BREAKER: stopping consumer."

  GOV-E3 — Counts upload failures only. Other failure classes (MTP
           enumerate, stale iterator, tag write) have their own monitoring.

  GOV-E4 — Reset on ANY successful upload. NOT reset by stage restart.
           When the breaker fires, no auto-reset — operator must pass
           --reset-transport-breaker at next run start.

The pre-L5 LOG_11_5_26 run produced 385 consecutive upload failures over
90 minutes before the DialogWatcher abort fired. This module is the
proactive gate that catches the same fault pattern within 10 errors
(~1-2 minutes at typical reorganize cadence).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass


log = logging.getLogger(__name__)


# Board GOV-E2 — Escalation thresholds (board-mandated constants).
THRESHOLD_WARNING  = 3
THRESHOLD_ERROR    = 5
THRESHOLD_CRITICAL = 10


@dataclass
class BreakerState:
    """Snapshot of the circuit breaker state for introspection."""
    consecutive_failures: int
    total_failures:       int
    total_successes:      int
    tripped:              bool
    trip_reason:          str = ""


# ─────────────────────────────────────────────────────────────────────────
# Singleton breaker — module-level state so callers from different
# subsystems (Stage 2 transfer + Stage 3 reorganize) share the same
# counter. Board GOV-E4: "Not reset by stage restart" → state must
# persist across stage boundaries within a single process.
# ─────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_consecutive_failures = 0
_total_failures = 0
_total_successes = 0
_tripped = False
_trip_reason = ""


def record_success() -> None:
    """Board GOV-E4 — any successful upload resets the consecutive counter.

    Successes after the breaker has tripped do NOT auto-untrip. Operator
    must explicitly pass --reset-transport-breaker at next run start.
    """
    global _consecutive_failures, _total_successes
    with _lock:
        _consecutive_failures = 0
        _total_successes += 1


def record_failure(*, context: str = "") -> bool:
    """Record a single upload failure. Returns True if the breaker has
    tripped (caller should stop further uploads). Board GOV-E2 logging
    ladder is applied here.

    `context` is a free-form string (filename, error class) included in
    log lines for diagnostic purposes.
    """
    global _consecutive_failures, _total_failures, _tripped, _trip_reason

    with _lock:
        _consecutive_failures += 1
        _total_failures += 1
        n = _consecutive_failures

    # Escalation ladder — Board GOV-E2
    if n == THRESHOLD_WARNING:
        log.warning(
            f"[Transport] Upload error streak: {n}. Monitoring. "
            f"(MK39.1 L6.4 / Board GOV-E2)"
            + (f" Last failure: {context!r}" if context else "")
        )
    elif n == THRESHOLD_ERROR:
        log.error(
            f"[Transport] Upload error streak: {n}. Suspect systematic failure. "
            f"(MK39.1 L6.4 / Board GOV-E2)"
            + (f" Last failure: {context!r}" if context else "")
        )
    elif n >= THRESHOLD_CRITICAL and not _tripped:
        with _lock:
            _tripped = True
            _trip_reason = (
                f"CIRCUIT_BREAKER_TRIPPED: {n} consecutive upload failures "
                f"(threshold={THRESHOLD_CRITICAL}). Last context: {context!r}."
            )
        log.critical(
            f"[Transport] CIRCUIT BREAKER: {n} consecutive upload failures. "
            f"Stopping consumer. Diagnostic: check MTP connection, device "
            f"storage, and transport log. Run with --reset-transport-breaker "
            f"after addressing root cause. (MK39.1 L6.4 / Board GOV-E1)"
        )

    return _tripped


def is_tripped() -> bool:
    """Cheap thread-safe check for use in inner loops before each upload."""
    return _tripped


def get_state() -> BreakerState:
    """Snapshot the current breaker state."""
    with _lock:
        return BreakerState(
            consecutive_failures=_consecutive_failures,
            total_failures=_total_failures,
            total_successes=_total_successes,
            tripped=_tripped,
            trip_reason=_trip_reason,
        )


def reset(*, operator_initiated: bool = False) -> None:
    """Reset the breaker. Board GOV-E4: only the operator may un-trip
    a tripped breaker (via CLI flag at next run start)."""
    global _consecutive_failures, _total_failures, _total_successes
    global _tripped, _trip_reason

    with _lock:
        if _tripped and not operator_initiated:
            log.warning(
                "[Transport] reset() called on tripped breaker without "
                "operator_initiated=True; ignoring. Board GOV-E4: "
                "operator must explicitly --reset-transport-breaker."
            )
            return
        _consecutive_failures = 0
        _total_failures = 0
        _total_successes = 0
        _tripped = False
        _trip_reason = ""

    if operator_initiated:
        log.info("[Transport] Circuit breaker reset by operator.")
