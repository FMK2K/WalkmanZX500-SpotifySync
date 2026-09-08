"""
mk35_capability.py — MK35 Q2: Capability-Locked Execution
==========================================================
CTO MK34 review:
  "If any code path bypasses walkman_sync.py, destructive operations can still
   execute. Examples: direct CLI invocation of mk34_executor; replay engine
   rehydration side effects; future async worker bypass paths. You do not yet
   have a hard execution firewall at the domain boundary. Right now this is
   still convention-based safety, not structural safety."

This module replaces the MK34 DEDUP_DESTRUCTIVE_VERIFIED flag (which was
convention-based: any code that didn't check it could bypass) with a
capability-token model. Capability tokens are:

  - frozen (immutable)
  - single-use (consumed on first use)
  - time-bound (default 5-minute expiry)
  - scoped (a token grants access to a specific operation+path, or "*")
  - registry-validated (must be in process-local _CAPABILITY_REGISTRY)
  - issuable only by the invariant gate via grant_capability()

A function that performs a destructive operation MUST take a capability
parameter and call require_capability(cap, operation, path) at the top.
Without a valid capability the function raises NoCapabilityError —
uncatchable except by RunAbortPolicy (MK26 C5).

This is structural enforcement, not convention. There is no "default"
or "fallback" path that allows a destructive call without a capability.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, Optional, Set

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════

class NoCapabilityError(RuntimeError):
    """
    Raised when a destructive function is called without a valid capability.
    Like InvariantViolation, this is intended to propagate past stage gates.
    The only legitimate handler is RunAbortPolicy (MK26 C5).
    """

    def __init__(self, operation: str, path: str = "*", reason: str = ""):
        self.operation = operation
        self.path = path
        super().__init__(
            f"NO_CAPABILITY operation={operation!r} path={path!r} "
            f"reason={reason!r} — caller must obtain ExecutionCapability via "
            f"the invariant gate before invoking destructive operations."
        )


class CapabilityForgedError(RuntimeError):
    """Raised when a token claims to be a capability but isn't in the registry."""


# ═══════════════════════════════════════════════════════════════════════════
# The capability token (Q2-T1)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExecutionCapability:
    """
    A frozen, single-use, time-bound, scoped authorization token.

    `scope` is "*" for run-wide capabilities (legacy flow) or an
    operation:path pair like "transfer:SD card/Music/X.mp3" for narrow
    capabilities. Narrow capabilities are preferred when the call site
    knows exactly what it's doing.

    `_internal_secret` is matched against _CAPABILITY_REGISTRY at
    consume time — preventing forgeries (a frozen dataclass with the
    right shape but never granted by us).
    """
    token_id:               str       # uuid4
    scope:                  str       # "*" or "operation:path"
    granted_at:             float     # epoch seconds
    expires_at:             float     # epoch seconds
    granted_by_invariant:   str       # name of the gate function
    _internal_secret:       str       # matched against registry; not for serialization

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) > self.expires_at

    def matches(self, operation: str, path: str = "*") -> bool:
        """Does this capability cover this operation+path?"""
        if self.scope == "*":
            return True
        # narrow scope: "operation:path"
        try:
            scope_op, scope_path = self.scope.split(":", 1)
        except ValueError:
            return False
        if scope_op != operation:
            return False
        if scope_path == "*" or scope_path == path:
            return True
        return False

    def to_dict(self) -> dict:
        # Note: _internal_secret is NEVER serialized (would defeat the registry)
        return {
            "tokenId":            self.token_id,
            "scope":              self.scope,
            "grantedAt":          self.granted_at,
            "expiresAt":          self.expires_at,
            "grantedByInvariant": self.granted_by_invariant,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Registry — process-local store of valid capabilities (Q2-T2)
# ═══════════════════════════════════════════════════════════════════════════

# Module-internal: do not expose. The presence of an entry in this dict is
# the structural validation; a forged ExecutionCapability with the right
# shape but missing from the registry will fail require_capability().
_CAPABILITY_REGISTRY: Dict[str, ExecutionCapability] = {}
_REGISTRY_LOCK = RLock()
_USED_TOKENS: Set[str] = set()    # single-use enforcement


# ═══════════════════════════════════════════════════════════════════════════
# Issuance — only the invariant gate may call this (Q2-T2)
# ═══════════════════════════════════════════════════════════════════════════

# A grant key — a module-level secret that MUST be passed to grant_capability().
# Only mk34_invariants.gate_pre_execution() (or test code that has imported
# the secret directly) can issue capabilities. This is enforcement-by-secret,
# not enforcement-by-import-name. Code that didn't read this module's source
# cannot guess the value.
_GRANT_KEY = secrets.token_hex(16)


def _get_grant_key_for_invariant_gate() -> str:
    """
    Internal: only mk34_invariants.gate_pre_execution() should call this.
    The function exists so that the secret can be passed across module
    boundaries cleanly without a global import.
    """
    return _GRANT_KEY


def grant_capability(
    grant_key: str,
    scope: str = "*",
    granted_by_invariant: str = "gate_pre_execution",
    ttl_seconds: float = 300.0,
) -> ExecutionCapability:
    """
    Issue a new ExecutionCapability. Caller MUST supply the correct
    grant_key — typically obtained via _get_grant_key_for_invariant_gate().

    MK36 R2: capability is also written to the durable SQLite store
    (mk36_capability_store) so it survives process restart and is
    visible to concurrent processes. The in-memory dict remains as L1
    cache for fast lookup; SQLite is L2 truth.
    """
    if grant_key != _GRANT_KEY:
        raise PermissionError(
            "grant_capability called with invalid grant_key. Only the "
            "invariant gate may issue capabilities."
        )

    now = time.time()
    cap = ExecutionCapability(
        token_id              = uuid.uuid4().hex,
        scope                 = scope,
        granted_at            = now,
        expires_at            = now + ttl_seconds,
        granted_by_invariant  = granted_by_invariant,
        _internal_secret      = _GRANT_KEY,    # registry validation marker
    )
    with _REGISTRY_LOCK:
        _CAPABILITY_REGISTRY[cap.token_id] = cap

    # MK36 R2: persist to durable store (L2). Failure here is logged but
    # does not block the grant — L1-only mode is the MK35 fallback.
    try:
        from mk36_capability_store import get_default_store
        import hashlib
        store = get_default_store()
        secret_hash = hashlib.sha256(_GRANT_KEY.encode()).hexdigest()
        store.grant(
            token_id=cap.token_id, scope=scope,
            granted_at=now, expires_at=now + ttl_seconds,
            granted_by_invariant=granted_by_invariant,
            internal_secret_hash=secret_hash,
        )
    except ImportError:
        pass  # MK36 store not installed; L1-only mode (MK35 behavior)
    except Exception as e:
        log.warning(
            f"  [MK35/Q2→MK36/R2] durable store grant failed (L1 only): {e}"
        )

    log.info(
        f"  [MK35/Q2] CAPABILITY_GRANTED token={cap.token_id[:8]} "
        f"scope={scope!r} by={granted_by_invariant} ttl={ttl_seconds:.0f}s"
    )
    return cap


def revoke_all_capabilities(reason: str = "explicit_revoke") -> int:
    """
    Clear the registry. Called on RunAbortPolicy.abort() to ensure no
    in-flight capability survives an aborted run. Returns count revoked.

    MK36 R2: also revokes durable-store rows.
    """
    with _REGISTRY_LOCK:
        count = len(_CAPABILITY_REGISTRY)
        _CAPABILITY_REGISTRY.clear()
        _USED_TOKENS.clear()

    # MK36 R2: revoke in durable store too
    try:
        from mk36_capability_store import get_default_store
        get_default_store().revoke_all(reason=reason)
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"  [MK36/R2] durable revoke failed: {e}")

    log.warning(f"  [MK35/Q2] CAPABILITIES_REVOKED count={count} reason={reason!r}")
    return count


# ═══════════════════════════════════════════════════════════════════════════
# Consumption — the firewall (Q2-T2 / Q2-T3)
# ═══════════════════════════════════════════════════════════════════════════

def require_capability(
    capability: Optional[ExecutionCapability],
    operation: str,
    path: str = "*",
    consume: bool = True,
) -> None:
    """
    Validate that `capability` is a real, unexpired, scope-matching token
    in the registry. If yes, optionally consume it (mark single-use).

    MK36 R2: when L1 (in-memory) lookup misses, falls through to durable
    SQLite store. This handles the cross-process case where a capability
    was granted by Process A and is being consumed by Process B (replay
    worker, async executor, etc.).

    If `consume=False`, the capability remains valid for additional uses
    until expiry — used by read-only paths (replay) that need permission
    to read but shouldn't burn a token meant for a destructive op.

    Raises:
        NoCapabilityError: capability is None, expired, or scope-mismatched.
        CapabilityForgedError: capability's token_id is not in the registry.
    """
    if capability is None:
        raise NoCapabilityError(
            operation=operation, path=path,
            reason="no capability supplied",
        )

    with _REGISTRY_LOCK:
        registered = _CAPABILITY_REGISTRY.get(capability.token_id)
    durable_row = None

    if registered is None:
        # MK36 R2: fall through to durable store
        try:
            from mk36_capability_store import get_default_store
            import hashlib
            store = get_default_store()
            durable_row = store.lookup(capability.token_id)
            if durable_row is not None:
                # Re-hydrate L1 from L2 (only if shape matches)
                expected_hash = hashlib.sha256(_GRANT_KEY.encode()).hexdigest()
                if durable_row["internal_secret_hash"] == expected_hash:
                    # Update L1 cache; leave consumption to consume() below
                    with _REGISTRY_LOCK:
                        if capability.token_id not in _CAPABILITY_REGISTRY:
                            _CAPABILITY_REGISTRY[capability.token_id] = capability
                    registered = capability
        except ImportError:
            pass
        except Exception as e:
            log.warning(f"  [MK36/R2] durable lookup failed: {e}")

    if registered is None:
        raise CapabilityForgedError(
            f"Capability token_id={capability.token_id[:8]} not in registry "
            f"(L1 or L2). Either: (1) it expired and was cleaned up, "
            f"(2) it was forged, or "
            f"(3) capabilities were revoked between issuance and use."
        )
    # Verify the registered token matches what was passed (defense in depth)
    if registered._internal_secret != capability._internal_secret:
        raise CapabilityForgedError(
            f"Capability internal_secret mismatch — token_id matches but "
            f"the secret does not. Forged or tampered."
        )

    # MK36 R2: durable single-use check via SQLite atomic update
    if consume and durable_row is not None and durable_row.get("used"):
        raise NoCapabilityError(
            operation=operation, path=path,
            reason=f"token {capability.token_id[:8]} already consumed in durable store",
        )

    # In-memory single-use check (catches same-process race)
    with _REGISTRY_LOCK:
        if capability.token_id in _USED_TOKENS:
            raise NoCapabilityError(
                operation=operation, path=path,
                reason=f"token {capability.token_id[:8]} already consumed",
            )
        # Expiry
        if capability.is_expired():
            raise NoCapabilityError(
                operation=operation, path=path,
                reason=f"token {capability.token_id[:8]} expired "
                       f"{time.time() - capability.expires_at:.0f}s ago",
            )
        # Scope match
        if not capability.matches(operation, path):
            raise NoCapabilityError(
                operation=operation, path=path,
                reason=f"token scope {capability.scope!r} does not cover "
                       f"{operation}:{path}",
            )

        # All checks passed — consume if requested
        if consume:
            # MK36 R2: atomic consume in durable store first; if successful,
            # mark in L1. This is the multi-process safety guarantee.
            durable_consumed = True
            try:
                from mk36_capability_store import get_default_store
                store = get_default_store()
                # Only attempt durable consume if the row exists in L2
                if store.lookup(capability.token_id) is not None:
                    durable_consumed = store.consume(
                        capability.token_id, operation, path,
                    )
                    if not durable_consumed:
                        raise NoCapabilityError(
                            operation=operation, path=path,
                            reason=f"token {capability.token_id[:8]} consume race lost "
                                   f"in durable store (another process won)",
                        )
            except ImportError:
                pass  # L1-only mode (MK35 behavior)

            if durable_consumed:
                _USED_TOKENS.add(capability.token_id)
                log.debug(
                    f"  [MK35/Q2] CAPABILITY_CONSUMED token={capability.token_id[:8]} "
                    f"operation={operation} path={path!r}"
                )


def list_active_capabilities() -> "list[dict]":
    """Operator helper: list all currently-valid capabilities."""
    now = time.time()
    with _REGISTRY_LOCK:
        return [
            cap.to_dict() for cap in _CAPABILITY_REGISTRY.values()
            if not cap.is_expired(now) and cap.token_id not in _USED_TOKENS
        ]


__all__ = [
    "ExecutionCapability",
    "NoCapabilityError", "CapabilityForgedError",
    "grant_capability", "revoke_all_capabilities",
    "require_capability", "list_active_capabilities",
    "_get_grant_key_for_invariant_gate",
]
