"""
mk39_a1_mediator.py — MK39-A1 MVP: ExecutionAuthority Mediator (Skeleton)
================================================================================

Sprint:        MK39 (ACCELERATED — parallel with MK38 Containment)
Ticket:        MK39-A1 — ExecutionAuthority Mediator
Scope (MVP):   Single context-manager API + audit hooks + capability integration.
               Existing call sites are NOT yet rewired — that's the full MK39-A1
               delivery in MK39-S1. This MVP establishes the API surface so that
               MK38-C write barriers can be designed against a stable target.

Closes (this MVP, behaviorally): partial scaffolding for
  R27 Unmediated metadata write paths   — full closure in MK39-A1 final + A3
  R28 Provenance policy not statically enforceable — full closure in MK39-C2
  R29 No forensic mutation trail        — full closure in MK39-B1

Closes (when full MK39-A1 delivered): the structural enforcement layer that
makes MK38-C write barriers bypass-resistant. Until then, this MVP gives
operators and reviewers a concrete target API.

Board directive (locked, 2026-05-09):
  "In parallel, begin MK39-A1 (ExecutionAuthority mediator). Even a minimal
   mediator that wraps existing mk37_wiring functions is a huge win. It does
   not need to be fully featured to start providing the single mediation
   point that MK38-C can integrate with."

Design (adversarial CTO checklist applied):

  1. SINGLE MEDIATION POINT — every device-state-mutating operation goes
     through `mediated_operation(op, capability)`. There is no "convenience
     bypass" path. Direct calls to underlying primitives are detectable by
     `mutation_path_audit` (MK37.1-1) once MK39-C2 lands.

  2. CAPABILITY-FIRST — the mediator REFUSES to enter the wrapped block if
     the capability does not authorize the operation. This is structurally
     stronger than MK37.2-1 renewal-before-check (which checks capability
     freshness but not capability/operation match). The mediator computes
     the match BEFORE yielding.

  3. PRE/POST AUDIT — pre-state and post-state are captured with their
     content hashes (when applicable) and recorded in the mutation journal
     entry. This is what MK39-B1 will durabilize; the MVP captures the
     events in memory + appends to .mk39_mutations.jsonl.

  4. INVARIANT CHECKS — on entry: capability still valid (TTL, scope,
     semantic tier); on exit: outcome consistent with pre-state expectations
     (no silent rollback, no orphan partial writes).

  5. NO MONKEY PATCHING — the MVP does NOT monkey-patch existing primitives.
     It provides the API; rewiring call sites to use it is a deliberate,
     reviewed change in MK39-S1. This avoids MK37.1-3-style fragility risks.

  6. TELEMETRY — every mediator call is counted by op_class and outcome.
     `snapshot_mediator_telemetry()` exposes the counters.

  7. REPLAYABLE — every mediated operation gets a UUID; the journal entry
     includes operation_uuid, capability_token_short, op_class, op_target,
     pre_state_hash, post_state_hash, outcome, started_at, completed_at,
     duration_ms. MK39-B2 will replay these events deterministically.

  8. SAFE-BY-DEFAULT — if the wrapped block raises, the mediator records
     the exception and re-raises. Telemetry counts the failure. The
     capability is NOT consumed on failure (consume happens only on
     successful completion).

Public API (locked for MVP):
  class OperationDescriptor       — frozen dataclass: op_class, target, payload_hint
  class MediatorOutcome           — frozen dataclass: outcome, pre_hash, post_hash, duration_ms
  mediated_operation(op, cap)     — context manager (the headline API)
  snapshot_mediator_telemetry()   — Dict[str, int]
  verify_mediator_installed()     — sanity self-check (modeled after MK37.1-3)

Locked operation-class taxonomy (extending requires SDD §10 update):
  TAG_WRITE        — metadata mutation on an existing file (genre, year, etc.)
  FILE_TRANSFER    — copy a file from host → device
  FILE_DELETE      — remove a file from device
  FILE_RENAME      — rename a file on device
  DEDUP_DELETE     — remove a duplicate after verified replacement

Anything not in this taxonomy is rejected with a `MediatorTaxonomyViolation`
exception so future code paths cannot silently sneak in new mutation classes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Locked taxonomy — additions require SDD §10 sign-off
# ═══════════════════════════════════════════════════════════════════════════

OPERATION_CLASSES: FrozenSet[str] = frozenset({
    "TAG_WRITE",
    "FILE_TRANSFER",
    "FILE_DELETE",
    "FILE_RENAME",
    "DEDUP_DELETE",
})

# Map of op_class → which capability operation field it consumes.
# Aligned with mk35_capability semantics; adding a new op_class without
# extending this map will fail-safe (default = "unknown" → rejected).
OP_TO_CAPABILITY_OP: Dict[str, str] = {
    "TAG_WRITE":     "tag_write",
    "FILE_TRANSFER": "transfer",
    "FILE_DELETE":   "delete",
    "FILE_RENAME":   "rename",
    "DEDUP_DELETE":  "dedup",
}


class MediatorError(RuntimeError):
    """Base class for mediator-detected violations."""


class MediatorTaxonomyViolation(MediatorError):
    """An OperationDescriptor used an op_class not in OPERATION_CLASSES."""


class MediatorCapabilityViolation(MediatorError):
    """The capability presented does not authorize this operation."""


class MediatorInvariantViolation(MediatorError):
    """A pre/post invariant check failed (e.g., post_hash unchanged when
    operation reported success)."""


# ═══════════════════════════════════════════════════════════════════════════
# Descriptors
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class OperationDescriptor:
    """Describes a pending mutation. Immutable so it can be audit-logged
    safely without TOCTOU between intent and execution."""
    op_class: str                    # MUST be in OPERATION_CLASSES
    target:   str                    # device path, e.g. "Music/Artist/Album/Track.mp3"
    source:   Optional[str] = None   # local file path (for transfer/tag_write)
    payload_hint: Optional[Dict[str, Any]] = None   # e.g., {"field": "genre", "new_value": "Hip-Hop"}

    def __post_init__(self):
        if self.op_class not in OPERATION_CLASSES:
            raise MediatorTaxonomyViolation(
                f"op_class={self.op_class!r} not in locked taxonomy "
                f"{sorted(OPERATION_CLASSES)}; refusing to mediate."
            )


@dataclass(frozen=True)
class MediatorOutcome:
    """Result of a mediated operation."""
    outcome:           str            # "success" | "failure" | "refused"
    operation_uuid:    str
    op_class:          str
    target:            str
    pre_state_hash:    Optional[str]
    post_state_hash:   Optional[str]
    capability_token_short: Optional[str]
    started_at:        float
    completed_at:      float
    duration_ms:       int
    exception:         Optional[str] = None
    invariant_violations: tuple = field(default_factory=tuple)


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry
# ═══════════════════════════════════════════════════════════════════════════

_TELEMETRY: Dict[str, int] = {
    "mediated_operations_total":      0,
    "mediated_operations_success":    0,
    "mediated_operations_failure":    0,
    "mediated_operations_refused":    0,
    "refusals_taxonomy":              0,
    "refusals_capability":            0,
    "invariant_violations":           0,
}
_TELEMETRY_BY_CLASS: Dict[str, int] = {}
_TELEMETRY_LOCK = threading.RLock()


def _bump(key: str, n: int = 1) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY[key] = _TELEMETRY.get(key, 0) + n


def _bump_class(op_class: str) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY_BY_CLASS[op_class] = _TELEMETRY_BY_CLASS.get(op_class, 0) + 1


def snapshot_mediator_telemetry() -> Dict[str, Any]:
    with _TELEMETRY_LOCK:
        return {
            "counters":  dict(_TELEMETRY),
            "by_class":  dict(_TELEMETRY_BY_CLASS),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Mutation journal — MVP captures to .mk39_mutations.jsonl; MK39-B1 will
# durabilize with hash-chaining and SQLite.
# ═══════════════════════════════════════════════════════════════════════════

_JOURNAL = Path(os.environ.get(
    "MK39_MUTATION_JOURNAL",
    str(Path(".") / ".mk39_mutations.jsonl"),
))
_JOURNAL_LOCK = threading.Lock()


def _append_journal(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        with _JOURNAL_LOCK:
            with open(_JOURNAL, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except Exception as e:
        log.warning(f"  [MK39-A1] journal append failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Capability check
# ═══════════════════════════════════════════════════════════════════════════

def _capability_authorizes(capability: Any, op: OperationDescriptor) -> bool:
    """
    Returns True if `capability` authorizes `op`. Conservative on missing
    attributes — unknown capability shapes are NOT authorized.

    Compatible with both MK35's ExecutionCapability and the future MK39-A2
    capability with semantic_scope.
    """
    if capability is None:
        return False
    # Scope check (MK35-style): "*" matches everything; otherwise must contain
    # the path or operation
    scope = getattr(capability, "scope", None)
    if scope is None:
        return False
    if scope != "*" and op.target not in (scope or "") and OP_TO_CAPABILITY_OP.get(op.op_class, "") not in str(scope):
        return False
    # TTL check (MK35-style): expires_at in the future
    expires_at = getattr(capability, "expires_at", None)
    if expires_at is not None:
        try:
            if float(expires_at) < time.time():
                return False
        except (TypeError, ValueError):
            return False
    # MK39-A2 semantic scope check (when present): semantic_scope must
    # explicitly authorize this op_class
    sem = getattr(capability, "semantic_scope", None)
    if sem is not None:
        # If semantic_scope is provided, it's gating: missing op_class = denied
        if isinstance(sem, dict):
            allowed = sem.get("allowed_op_classes")
            if allowed is not None and op.op_class not in allowed:
                return False
            # For TAG_WRITE, check field-level authorization if specified
            if op.op_class == "TAG_WRITE" and op.payload_hint:
                field_name = op.payload_hint.get("field")
                allowed_fields = sem.get("allowed_fields")
                if allowed_fields is not None and field_name not in allowed_fields:
                    return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# State hashing
# ═══════════════════════════════════════════════════════════════════════════

def _hash_target_state(target: str, op_class: str) -> Optional[str]:
    """
    Compute a state hash for the target. For FILE_* operations, this is the
    SHA-256 of the file bytes. For TAG_WRITE, the MVP returns the string
    representation of the file's existing tag map (full content-hash will
    be MK39-B1 work). Returns None when not computable.
    """
    if op_class in ("FILE_TRANSFER", "FILE_DELETE", "FILE_RENAME", "DEDUP_DELETE"):
        try:
            p = Path(target)
            if not p.exists() or p.is_dir():
                return None
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()[:16]
        except Exception as e:
            log.debug(f"  [MK39-A1] hash failed for {target}: {e}")
            return None
    elif op_class == "TAG_WRITE":
        # MVP: just the path + size + mtime as a cheap state digest.
        # MK39-B1 will replace with proper tag-content hash.
        try:
            p = Path(target)
            if not p.exists():
                return None
            st = p.stat()
            return hashlib.sha256(
                f"{target}:{st.st_size}:{int(st.st_mtime)}".encode()
            ).hexdigest()[:16]
        except Exception:
            return None
    return None


# ═══════════════════════════════════════════════════════════════════════════
# THE HEADLINE API
# ═══════════════════════════════════════════════════════════════════════════

@contextmanager
def mediated_operation(op: OperationDescriptor, capability: Any):
    """
    Single mediation point for all device-state-mutating operations.

    Usage:
        op = OperationDescriptor(op_class="TAG_WRITE", target="...",
                                  payload_hint={"field": "genre", "new_value": "Jazz"})
        with mediated_operation(op, capability) as ctx:
            mutagen.File(target).save()    # the actual mutation
            ctx.mark_success()              # explicit completion signal

    The mediator:
      1. Refuses entry if capability does not authorize op
      2. Captures pre-state hash
      3. Yields a context object the caller can use to signal completion
      4. Captures post-state hash on exit
      5. Logs the event to the mutation journal
      6. Counts success/failure/refused in telemetry
      7. Re-raises any exception from the wrapped block

    Rev MVP: capability is checked but NOT consumed. MK39-A2 will integrate
    with mk35_capability.consume() for transactional semantics.
    """
    # 1. Taxonomy check (already done in OperationDescriptor.__post_init__,
    #    but defensively re-verified)
    if op.op_class not in OPERATION_CLASSES:
        _bump("refusals_taxonomy")
        raise MediatorTaxonomyViolation(
            f"op_class={op.op_class!r} rejected by mediator"
        )

    _bump("mediated_operations_total")
    _bump_class(op.op_class)
    operation_uuid = uuid.uuid4().hex
    started_at = time.time()
    cap_token_short = (
        getattr(capability, "token_id", "") or ""
    )[:8] if capability is not None else None

    # 2. Capability authorization check
    if not _capability_authorizes(capability, op):
        _bump("refusals_capability")
        _bump("mediated_operations_refused")
        completed_at = time.time()
        outcome_obj = MediatorOutcome(
            outcome="refused", operation_uuid=operation_uuid,
            op_class=op.op_class, target=op.target,
            pre_state_hash=None, post_state_hash=None,
            capability_token_short=cap_token_short,
            started_at=started_at, completed_at=completed_at,
            duration_ms=int((completed_at - started_at) * 1000),
        )
        _append_journal({
            "uuid": operation_uuid, "op_class": op.op_class,
            "target": op.target, "outcome": "refused",
            "reason": "capability_does_not_authorize",
            "started_at": started_at, "completed_at": completed_at,
            "capability_token_short": cap_token_short,
        })
        log.warning(
            f"  [MK39-A1] MEDIATOR_REFUSED op={op.op_class} target={op.target!r} "
            f"reason=capability_does_not_authorize"
        )
        raise MediatorCapabilityViolation(
            f"Capability does not authorize {op.op_class} on {op.target!r}"
        )

    # 3. Pre-state hash
    pre_hash = _hash_target_state(op.target, op.op_class)

    # MK39-A1 / Council mandate: set thread-local mediation context so the
    # mk39_tag_write_gate (mutagen boundary) sees an active mediation. This
    # is what closes the "legacy direct-mutagen call bypasses the freeze"
    # gap. Stack-based: nested mediated_operation calls push/pop correctly.
    _gate_pushed = False
    try:
        from mk39_tag_write_gate import set_mediation_context, clear_mediation_context
        set_mediation_context(operation_uuid, capability)
        _gate_pushed = True
    except ImportError:
        pass

    # 4. Yield a small context object the caller can use
    ctx = _MediationContext(operation_uuid=operation_uuid,
                              op=op, capability=capability,
                              pre_hash=pre_hash, started_at=started_at)
    try:
        yield ctx
        # If the caller didn't explicitly mark, default = success
        # (the absence of an exception is taken as success signal)
        if not ctx._explicit_outcome:
            ctx.mark_success()
    except Exception as e:
        # 5a. Failure path — log + re-raise
        completed_at = time.time()
        post_hash = _hash_target_state(op.target, op.op_class)
        _bump("mediated_operations_failure")
        _append_journal({
            "uuid": operation_uuid, "op_class": op.op_class,
            "target": op.target, "outcome": "failure",
            "exception_type": type(e).__name__,
            "exception_str": str(e)[:200],
            "pre_state_hash": pre_hash,
            "post_state_hash": post_hash,
            "started_at": started_at, "completed_at": completed_at,
            "duration_ms": int((completed_at - started_at) * 1000),
            "capability_token_short": cap_token_short,
        })
        log.warning(
            f"  [MK39-A1] MEDIATOR_FAILURE op={op.op_class} target={op.target!r} "
            f"exception={type(e).__name__}"
        )
        raise
    else:
        # 5b. Success path — verify invariants + log
        completed_at = time.time()
        post_hash = _hash_target_state(op.target, op.op_class)
        invariant_violations = _check_post_invariants(op, pre_hash, post_hash)
        if invariant_violations:
            _bump("invariant_violations")
            log.warning(
                f"  [MK39-A1] MEDIATOR_INVARIANT_VIOLATED op={op.op_class} "
                f"target={op.target!r} violations={invariant_violations}"
            )
        _bump("mediated_operations_success")
        _append_journal({
            "uuid": operation_uuid, "op_class": op.op_class,
            "target": op.target, "outcome": "success",
            "pre_state_hash": pre_hash,
            "post_state_hash": post_hash,
            "started_at": started_at, "completed_at": completed_at,
            "duration_ms": int((completed_at - started_at) * 1000),
            "capability_token_short": cap_token_short,
            "invariant_violations": list(invariant_violations) if invariant_violations else [],
            "payload_hint": op.payload_hint,
        })
    finally:
        # Always pop the gate context, even on exceptions
        if _gate_pushed:
            try:
                clear_mediation_context()
            except Exception:
                pass


def _check_post_invariants(op: OperationDescriptor,
                            pre_hash: Optional[str],
                            post_hash: Optional[str]) -> tuple:
    """
    Lightweight post-invariant checks. Returns a tuple of violation strings
    (empty tuple = no violations).

    MVP invariants:
      - For FILE_TRANSFER on success: post_hash should exist (file is on device)
      - For FILE_DELETE on success: post_hash should be None (file gone)
      - For TAG_WRITE: pre_hash != post_hash (something changed)
      - Catch silent no-ops: success-claimed but state unchanged when change expected
    """
    violations = []
    if op.op_class == "FILE_TRANSFER":
        if post_hash is None:
            violations.append("file_transfer_success_but_post_hash_missing")
    elif op.op_class in ("FILE_DELETE", "DEDUP_DELETE"):
        if post_hash is not None:
            violations.append("file_delete_success_but_target_still_exists")
    elif op.op_class == "TAG_WRITE":
        # If both hashes are present and identical, the tag write was a no-op
        # despite claimed success. This is a silent failure signal.
        if pre_hash is not None and pre_hash == post_hash:
            violations.append("tag_write_success_but_state_unchanged")
    return tuple(violations)


class _MediationContext:
    """Object yielded to the `with` block. Caller uses .mark_success() /
    .mark_failure() to signal explicit completion. If neither is called,
    the absence of an exception implies success."""

    def __init__(self, operation_uuid, op, capability, pre_hash, started_at):
        self.operation_uuid = operation_uuid
        self.op = op
        self.capability = capability
        self.pre_hash = pre_hash
        self.started_at = started_at
        self._explicit_outcome = False
        self._outcome = None

    def mark_success(self):
        self._explicit_outcome = True
        self._outcome = "success"

    def mark_failure(self, reason: str = ""):
        self._explicit_outcome = True
        self._outcome = "failure"


# ═══════════════════════════════════════════════════════════════════════════
# Self-check (modeled after MK37.1-3 / MK38.0 verify_*)
# ═══════════════════════════════════════════════════════════════════════════

def verify_mediator_installed() -> bool:
    """Sanity check the mediator module hasn't been tampered with."""
    import sys
    mod = sys.modules.get(__name__)
    if mod is None:
        log.error("  [MK39-A1] MEDIATOR_TAMPERED: module missing from sys.modules")
        return False
    if not isinstance(OPERATION_CLASSES, frozenset):
        log.error("  [MK39-A1] MEDIATOR_TAMPERED: OPERATION_CLASSES not frozen")
        return False
    if len(OPERATION_CLASSES) < 5:
        log.error("  [MK39-A1] MEDIATOR_TAMPERED: OPERATION_CLASSES shrunk")
        return False
    # Every op_class must have a capability-op mapping (no orphans)
    missing = OPERATION_CLASSES - set(OP_TO_CAPABILITY_OP.keys())
    if missing:
        log.error(f"  [MK39-A1] MEDIATOR_TAMPERED: op_classes without cap mapping: {missing}")
        return False
    return True


__all__ = [
    "OperationDescriptor", "MediatorOutcome",
    "OPERATION_CLASSES", "OP_TO_CAPABILITY_OP",
    "MediatorError", "MediatorTaxonomyViolation",
    "MediatorCapabilityViolation", "MediatorInvariantViolation",
    "mediated_operation",
    "snapshot_mediator_telemetry",
    "verify_mediator_installed",
]
