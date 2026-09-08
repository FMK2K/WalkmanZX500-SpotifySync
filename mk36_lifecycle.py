"""
mk36_lifecycle.py — MK36 R4: Formal Event Lifecycle Model
============================================================
CTO MK35 review:
  "Verification only meaningful after transport acceptance. Capability
   issuance only meaningful after invariant evaluation. Those are still
   staged temporal relationships. Meaning the system is not fully
   state-native yet — it is state-oriented externally, temporally staged
   internally."

The MK35 `TransferOutcome` collapsed an inherently four-stage transfer
lifecycle into a single dataclass. The audit log only saw the final
outcome, not the lifecycle stages. MK36 splits this into four explicit
audit events with parent-child linkage:

    PRE_TRANSFER     — capability validated; transport about to begin
    TRANSFER_ACCEPTED — transport returned (COM CopyHere or adb push)
    VERIFICATION_RUN  — quorum verification completed
    TRANSFER_COMMITTED — final state recorded; outcome locked

Each event has a `lifecycle_id` (the same uuid for all four events of one
transfer) and a `parent_event_id` (the previous lifecycle event), so the
audit log can reconstruct the full lifecycle for any transfer.

Existing `TransferOutcome` is preserved as an external API; transfer_one()
emits the four events internally. Backward compatibility is total —
no existing consumer of TransferOutcome breaks.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle event types
# ═══════════════════════════════════════════════════════════════════════════

class LifecyclePhase(str, Enum):
    """The four phases of a transfer lifecycle. Each becomes one audit event."""
    PRE_TRANSFER       = "PRE_TRANSFER"
    TRANSFER_ACCEPTED  = "TRANSFER_ACCEPTED"
    VERIFICATION_RUN   = "VERIFICATION_RUN"
    TRANSFER_COMMITTED = "TRANSFER_COMMITTED"


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle event (frozen, audit-loggable)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LifecycleEvent:
    """
    One event in a transfer lifecycle. Frozen, JSON-serializable.

    `lifecycle_id` ties together the four events of one transfer.
    `parent_event_id` chains them in order. The first event (PRE_TRANSFER)
    has parent_event_id=None.
    """
    event_id:        str
    lifecycle_id:    str
    parent_event_id: Optional[str]
    phase:           LifecyclePhase
    path:            str
    timestamp:       float
    payload:         dict             # phase-specific fields

    def to_dict(self) -> dict:
        return {
            "eventId":       self.event_id,
            "lifecycleId":   self.lifecycle_id,
            "parentEventId": self.parent_event_id,
            "phase":         self.phase.value,
            "path":          self.path,
            "timestamp":     self.timestamp,
            "payload":       dict(self.payload),
        }


# ═══════════════════════════════════════════════════════════════════════════
# LifecycleTracker — produces the four events for one transfer
# ═══════════════════════════════════════════════════════════════════════════

class LifecycleTracker:
    """
    Produces a sequence of LifecycleEvent objects for a single transfer.
    Each call to .emit_X() advances the lifecycle and updates parent_event_id.

    Usage from transfer_one():
        tracker = LifecycleTracker(path=device_relpath)
        tracker.emit_pre(capability_token=cap.token_id)
        # ... transport ...
        tracker.emit_accepted(transport="com", success=accepted)
        # ... verify ...
        tracker.emit_verified(quorum_outcome=qo.to_dict())
        tracker.emit_committed(success=True, transport_used="com+quorum:STRONG")
        for event in tracker.events:
            audit.write(event)
    """

    def __init__(self, path: str, lifecycle_id: Optional[str] = None):
        self.path = path
        self.lifecycle_id = lifecycle_id or uuid.uuid4().hex
        self.events: list[LifecycleEvent] = []
        self._last_event_id: Optional[str] = None

    def _emit(self, phase: LifecyclePhase, payload: dict) -> LifecycleEvent:
        ev = LifecycleEvent(
            event_id        = uuid.uuid4().hex,
            lifecycle_id    = self.lifecycle_id,
            parent_event_id = self._last_event_id,
            phase           = phase,
            path            = self.path,
            timestamp       = time.time(),
            payload         = payload,
        )
        self.events.append(ev)
        self._last_event_id = ev.event_id
        log.debug(
            f"  [MK36/R4] lifecycle phase={phase.value} path={self.path!r} "
            f"event_id={ev.event_id[:8]} parent={self._last_event_id[:8] if self._last_event_id else None}"
        )
        return ev

    def emit_pre(self, *, capability_token: Optional[str] = None,
                 source_hash: Optional[str] = None) -> LifecycleEvent:
        """PRE_TRANSFER: capability validated; transport about to begin."""
        return self._emit(LifecyclePhase.PRE_TRANSFER, {
            "capabilityToken": capability_token,
            "sourceHash":      source_hash,
        })

    def emit_accepted(self, *, transport: str, success: bool,
                      reason: Optional[str] = None) -> LifecycleEvent:
        """TRANSFER_ACCEPTED: transport returned (success or failure)."""
        return self._emit(LifecyclePhase.TRANSFER_ACCEPTED, {
            "transport": transport,
            "success":   success,
            "reason":    reason,
        })

    def emit_verified(self, *, quorum_outcome: Optional[dict] = None,
                      device_hash: Optional[str] = None) -> LifecycleEvent:
        """VERIFICATION_RUN: quorum verification completed."""
        return self._emit(LifecyclePhase.VERIFICATION_RUN, {
            "quorumOutcome": quorum_outcome,
            "deviceHash":    device_hash,
        })

    def emit_committed(self, *, success: bool, transport_used: str,
                       failure_reason: Optional[str] = None,
                       elapsed_s: Optional[float] = None) -> LifecycleEvent:
        """TRANSFER_COMMITTED: final state locked; lifecycle complete."""
        return self._emit(LifecyclePhase.TRANSFER_COMMITTED, {
            "success":        success,
            "transportUsed":  transport_used,
            "failureReason":  failure_reason,
            "elapsedS":       elapsed_s,
        })


# ═══════════════════════════════════════════════════════════════════════════
# Audit-log writer (R4-T2 — emits to .mk34_audit.jsonl alongside engine events)
# ═══════════════════════════════════════════════════════════════════════════

def persist_lifecycle_events(events: list,
                              audit_log_path=None) -> None:
    """
    Append lifecycle events to the audit log as JSONL rows.
    Each row is a LifecycleEvent.to_dict() with a top-level "type": "LIFECYCLE".
    """
    from pathlib import Path
    p = Path(audit_log_path) if audit_log_path else Path(".mk34_audit.jsonl")

    try:
        # Use canonical JSON if MK36 R3 helper available; fall back to default
        try:
            from mk36_runtime import canonical_json
        except ImportError:
            import json
            def canonical_json(obj): return json.dumps(obj, sort_keys=True)

        with p.open("a", encoding="utf-8") as fh:
            for ev in events:
                row = {"type": "LIFECYCLE", **ev.to_dict()}
                fh.write(canonical_json(row) + "\n")
    except Exception as e:
        log.warning(f"  [MK36/R4] persist_lifecycle_events failed: {e}")


__all__ = [
    "LifecyclePhase", "LifecycleEvent", "LifecycleTracker",
    "persist_lifecycle_events",
]
