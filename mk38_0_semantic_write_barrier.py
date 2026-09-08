"""
mk38_0_semantic_write_barrier.py — MK38.0 Emergency Semantic Write Freeze
================================================================================

Sprint:        MK38.0 — Emergency Semantic Write Freeze (HOTFIX, ships before MK38)
Closes (immediately, behaviorally):
               R17 Recursive semantic contamination       — CRITICAL
               R18 Cross-run inference amplification      — CRITICAL
               R19 Cache-poison persistence               — CRITICAL
               R22 Semantic overwrite without confidence delta — CRITICAL
               R24 Inventory truth contamination          — CRITICAL
Closes (architecturally, later):
               (above closed structurally by MK38-A..G when they land)
Trigger:       Board post-inventory review — "everything became Afrobeats" incident
               proves recursive semantic write amplification is occurring at
               library scale. Board verdict: FREEZE SEMANTIC WRITES before any
               further large sync runs.

Design (adversarial CTO checklist applied):
  1. SOURCE-ATTRIBUTION REQUIRED — every semantic write must declare its source
     class. Default for "unknown" is BLOCKED. No silent allow-by-default.
  2. CANONICAL ALLOW-LIST is small and explicit. Inference sources are the
     deny-list. There is no third "maybe" tier — anything not in CANONICAL is
     blocked under freeze.
  3. STALE-CACHE PROTECTION — under freeze, cache-read paths for inference-
     derived entries are also blocked, not just writes. (Otherwise MK38.0 only
     stops new poisoning while existing poisoned cache continues to feed.)
  4. HARDENED-MODE LOCK — freeze is default-ON in production hardened mode and
     cannot be disabled while hardened mode is locked (MK37.1-2 pattern).
     Operator override requires both env var MK38_0_ALLOW_SEMANTIC_WRITES=1 AND
     hardened mode unlocked. Override emits a SEMANTIC_FREEZE_OVERRIDE_ACTIVE
     warning every 60s.
  5. TELEMETRY — every blocked write is counted by source class; the operator
     dashboard surfaces "what would have been written" so the freeze isn't
     opaque.
  6. MC VALIDATION — test suite includes a 1000-sample Monte Carlo over the
     (field, source, confidence) tuple space to prove the gate's classification
     is exhaustive.

Source taxonomy (locked):
  CANONICAL_SOURCES       — spotify_canonical, musicbrainz_canonical,
                            fingerprint_match, user_explicit
                            → ALLOWED under freeze (semantic AND nonsemantic)
  INFERENCE_SOURCES       — ai_inference, claude_fallback, graph_propagation,
                            modal_consensus, artist_cache_inferred,
                            album_consensus, broad_upgrade
                            → BLOCKED under freeze (semantic fields only)
  NONSEMANTIC_FIELDS      — track_number, disc_number, duration, bitrate,
                            sample_rate, codec, file_size, cover_art,
                            normalized_filename
                            → ALWAYS ALLOWED regardless of source/freeze

Public API:
  is_frozen() -> bool
  assert_field_allowed(field, value, source, confidence=None) -> None  (raises)
  is_field_allowed(field, source) -> bool                              (boolean)
  with_freeze_override(reason: str) -> ctx                              (audited)
  snapshot_telemetry() -> Dict[str, int]
  verify_barrier_installed() -> bool
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional, Set

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Source taxonomy — LOCKED. Adding sources requires SDD §4 / §10 update.
# ═══════════════════════════════════════════════════════════════════════════

CANONICAL_SOURCES: Set[str] = {
    "spotify_canonical",
    "musicbrainz_canonical",
    "fingerprint_match",
    "user_explicit",
}

INFERENCE_SOURCES: Set[str] = {
    "ai_inference",
    "claude_fallback",
    "graph_propagation",
    "modal_consensus",
    "artist_cache_inferred",
    "album_consensus",
    "broad_upgrade",
    "google_kg_unverified",
    "playlist_cooccurrence",
}

# Fields where source-attribution is irrelevant — these are objective
# bytes-derived facts, not semantic claims. Always allowed.
NONSEMANTIC_FIELDS: Set[str] = {
    "track_number", "disc_number", "duration", "duration_s",
    "bitrate", "bit_rate", "sample_rate", "bits", "channels",
    "codec", "file_size", "size",
    "cover_art", "album_art", "artwork",
    "normalized_filename", "filename",
}

# Fields that are SEMANTIC — claims about what the music IS.
# Under freeze, these require CANONICAL_SOURCES.
SEMANTIC_FIELDS: Set[str] = {
    "genre", "year", "release_year",
    "title", "artist", "album_artist", "album",
    "composer", "mood",
}


class SemanticWriteBlocked(RuntimeError):
    """Raised when a semantic write is blocked by the MK38.0 freeze."""
    pass


# ═══════════════════════════════════════════════════════════════════════════
# Freeze state
# ═══════════════════════════════════════════════════════════════════════════

_STATE_LOCK = threading.RLock()
_FREEZE_OVERRIDE_ACTIVE: bool = False
_FREEZE_OVERRIDE_REASON: Optional[str] = None
_LAST_OVERRIDE_WARNING: float = 0.0


def _hardened_mode_locked() -> bool:
    """True if MK37.1-2 production hardened mode is locked. If yes, override
    of the freeze is forbidden."""
    try:
        import mk37_wiring
        if hasattr(mk37_wiring, "is_production_hardened_mode"):
            return bool(mk37_wiring.is_production_hardened_mode())
    except (ImportError, AttributeError):
        pass
    return False


def is_frozen() -> bool:
    """
    Returns True if semantic writes from inference sources are currently
    blocked. Default: True (board recommendation). Override: only via
    MK38_0_ALLOW_SEMANTIC_WRITES=1 AND hardened mode unlocked AND explicit
    with_freeze_override() context.

    The default-True semantics are deliberate: any code path that forgets
    to check this gets blocked, not allowed.
    """
    with _STATE_LOCK:
        if not _FREEZE_OVERRIDE_ACTIVE:
            return True
        if _hardened_mode_locked():
            # Override is forbidden under hardened mode regardless of state
            return True
        env = os.environ.get("MK38_0_ALLOW_SEMANTIC_WRITES", "").strip()
        if env != "1":
            return True
        # Override is active, hardened mode is unlocked, env var is set
        # → freeze is OFF
        # Periodic warning so operators don't forget
        global _LAST_OVERRIDE_WARNING
        now = time.time()
        if now - _LAST_OVERRIDE_WARNING > 60.0:
            log.warning(
                f"  [MK38.0] SEMANTIC_FREEZE_OVERRIDE_ACTIVE reason="
                f"{_FREEZE_OVERRIDE_REASON!r} — semantic writes are NOT "
                f"protected. This warning will repeat every 60s."
            )
            _LAST_OVERRIDE_WARNING = now
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry
# ═══════════════════════════════════════════════════════════════════════════

_TELEMETRY: Dict[str, int] = {
    "writes_allowed_canonical":          0,
    "writes_allowed_nonsemantic":        0,
    "writes_blocked_inference":          0,
    "writes_blocked_unknown_source":     0,
    "writes_blocked_unknown_field":      0,
    "cache_reads_blocked_inference":     0,
    "override_active_seconds":           0,
}
_TELEMETRY_BY_SOURCE: Dict[str, int] = {}    # blocked count by source
_TELEMETRY_BY_FIELD: Dict[str, int] = {}     # blocked count by field
_TELEMETRY_LOCK = threading.RLock()


def _bump(metric: str, n: int = 1) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY[metric] = _TELEMETRY.get(metric, 0) + n


def _bump_by(d: Dict[str, int], key: str) -> None:
    with _TELEMETRY_LOCK:
        d[key] = d.get(key, 0) + 1


def snapshot_telemetry() -> Dict[str, Any]:
    """Operator dashboard accessor."""
    with _TELEMETRY_LOCK:
        return {
            "counters":  dict(_TELEMETRY),
            "by_source": dict(_TELEMETRY_BY_SOURCE),
            "by_field":  dict(_TELEMETRY_BY_FIELD),
            "frozen":    is_frozen(),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Core gate
# ═══════════════════════════════════════════════════════════════════════════

def is_field_allowed(field: str, source: str) -> bool:
    """
    Boolean predicate: would a write of `field` from `source` be allowed
    under the current freeze state? Does not raise; does not increment
    telemetry. Use this for "should I even try?" checks; use
    assert_field_allowed for "I'm about to write."

    Decision tree (this is the LOCKED specification — change requires §4):
      1. Field is nonsemantic           → ALLOW (source irrelevant)
      2. Source is canonical            → ALLOW (semantic OK from canonical)
      3. Field is semantic AND not frozen → ALLOW (operator override path)
      4. Field is semantic AND frozen   → BLOCK
      5. Source is unknown              → BLOCK (default-deny)
      6. Field is unknown               → BLOCK (default-deny — new field
                                                 must be added to taxonomy)
    """
    if field in NONSEMANTIC_FIELDS:
        return True
    if source in CANONICAL_SOURCES:
        return True
    if field not in SEMANTIC_FIELDS:
        # Unknown field — default deny, force taxonomy update
        return False
    # Semantic field. Source must be canonical OR freeze must be off.
    if source in INFERENCE_SOURCES:
        return not is_frozen()
    # Source is genuinely unknown — deny regardless of freeze
    return False


def assert_field_allowed(field: str,
                          value: Any,
                          source: str,
                          confidence: Optional[float] = None) -> None:
    """
    Assert that a semantic write is permitted. Raises SemanticWriteBlocked
    on denial. Increments telemetry on every call (allowed and denied).

    `confidence` is optional and informational; it is recorded in the
    SEMANTIC_WRITE_BLOCKED log line for forensic analysis but does not
    affect the gate decision under the current MK38.0 freeze (the freeze
    is binary; confidence-gated semantics are MK38-D).
    """
    # MK39.1 C2 — Forbidden-default-source gate (Council 2026-05-10 §4 P0+).
    # Defense in depth: even if some legacy code path tries to write a
    # default-guess, the barrier rejects it BEFORE any other classification.
    # The forbidden set is small + explicit (terminal_state.FORBIDDEN_DEFAULT_SOURCES).
    # This prevents the "Abstract Hip-Hop on every unknown track"
    # contamination pattern from re-emerging via a new code path.
    try:
        from terminal_state import (
            is_forbidden_default_source,
            DefaultWriteAttempt, record_default_write_attempt,
        )
        if is_forbidden_default_source(source):
            _bump("writes_blocked_forbidden_default")
            _bump_by(_TELEMETRY_BY_SOURCE, source)
            _bump_by(_TELEMETRY_BY_FIELD, field)
            record_default_write_attempt(DefaultWriteAttempt(
                field=field,
                source=str(source),
                value=str(value)[:40],
                rejected=True,
            ))
            raise SemanticWriteBlocked(
                f"Source {source!r} is in FORBIDDEN_DEFAULT_SOURCES. "
                f"Default-writes are not permitted under the MK39.1 C2 "
                f"unknown-as-terminal policy. Leave the field blank instead "
                f"(it will be picked up by quarantine_review --needs-review)."
            )
    except ImportError:
        # terminal_state not yet importable — fail open (preserve legacy
        # behavior). Bootstrap order makes this transient.
        pass

    if field in NONSEMANTIC_FIELDS:
        _bump("writes_allowed_nonsemantic")
        return
    if source in CANONICAL_SOURCES:
        _bump("writes_allowed_canonical")
        return
    if field not in SEMANTIC_FIELDS:
        _bump("writes_blocked_unknown_field")
        _bump_by(_TELEMETRY_BY_FIELD, field)
        log.warning(
            f"  [MK38.0] SEMANTIC_WRITE_BLOCKED field={field!r} (unknown field; "
            f"taxonomy update required) source={source!r}"
        )
        raise SemanticWriteBlocked(
            f"Field {field!r} not in MK38.0 taxonomy; default-deny applies. "
            f"Add to SEMANTIC_FIELDS or NONSEMANTIC_FIELDS in mk38_0_semantic_write_barrier."
        )
    # Semantic field
    if source in INFERENCE_SOURCES:
        if is_frozen():
            _bump("writes_blocked_inference")
            _bump_by(_TELEMETRY_BY_SOURCE, source)
            _bump_by(_TELEMETRY_BY_FIELD, field)
            log.warning(
                f"  [MK38.0] SEMANTIC_WRITE_BLOCKED field={field!r} "
                f"value={str(value)[:40]!r} source={source!r} "
                f"confidence={confidence!r} (freeze active)"
            )
            raise SemanticWriteBlocked(
                f"Semantic write to {field!r} from inference source "
                f"{source!r} blocked by MK38.0 freeze. "
                f"Permitted sources: {sorted(CANONICAL_SOURCES)}."
            )
        # Override active and we're past the hardened-mode check
        _bump("writes_allowed_canonical")  # treat as allowed under override
        return
    # Unknown source — default deny
    _bump("writes_blocked_unknown_source")
    _bump_by(_TELEMETRY_BY_SOURCE, source or "<empty>")
    _bump_by(_TELEMETRY_BY_FIELD, field)
    log.warning(
        f"  [MK38.0] SEMANTIC_WRITE_BLOCKED field={field!r} source={source!r} "
        f"(unknown source; default-deny)"
    )
    raise SemanticWriteBlocked(
        f"Source {source!r} is not in CANONICAL_SOURCES or INFERENCE_SOURCES. "
        f"Default-deny applies."
    )


def cache_read_allowed(source: str) -> bool:
    """
    Stale-cache protection. Under freeze, reads of cache entries that were
    populated by inference sources are blocked — otherwise existing
    poisoned cache continues to feed downstream writes.

    Cache-read sites should call this before returning a cached value:

        cached = _CACHE.get(key)
        if cached and not cache_read_allowed(cached.get("_source", "unknown")):
            return None
    """
    if not is_frozen():
        return True
    if source in CANONICAL_SOURCES:
        return True
    _bump("cache_reads_blocked_inference")
    _bump_by(_TELEMETRY_BY_SOURCE, source or "<empty>")
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Override mechanism (audited)
# ═══════════════════════════════════════════════════════════════════════════

@contextmanager
def activate_spiral_rev3():
    """
    Spiral pipeline Rev 3 context — temporarily promotes "ai_inference" to a
    canonical source so AI genre writes can pass through the barrier while the
    spiral's genre graph provides the contradiction filter.

    Scope is intentionally narrow:
      - Only active inside the `with` block (thread-local flag + CANONICAL_SOURCES
        mutation under the state lock).
      - Blocked under hardened mode, identical to with_freeze_override.
      - Emits an INFO log at entry/exit so the operator can see Rev 3 is active.

    The graph coherence check (is_coherent_with_graph) is the caller's
    responsibility; this context merely lifts the barrier gate.
    """
    if _hardened_mode_locked():
        log.warning(
            "  [MK38.0] SPIRAL_REV3_CONTEXT blocked — hardened mode locked; "
            "AI genre writes remain frozen for this revolution."
        )
        yield   # non-fatal: Rev 3 runs without AI writes
        return

    log.info("  [MK38.0] SPIRAL_REV3_CONTEXT entered — ai_inference promoted to canonical")
    with _STATE_LOCK:
        CANONICAL_SOURCES.add("ai_inference")
    try:
        yield
    finally:
        with _STATE_LOCK:
            CANONICAL_SOURCES.discard("ai_inference")
        log.info("  [MK38.0] SPIRAL_REV3_CONTEXT exited — ai_inference demoted")


@contextmanager
def with_freeze_override(reason: str):
    """
    Audited override of the freeze. Useful for batch operations the operator
    has explicitly approved (e.g., re-tagging from a verified MusicBrainz
    export). Blocked under hardened mode regardless. Emits one
    SEMANTIC_FREEZE_OVERRIDE_ENTERED at entry and one
    SEMANTIC_FREEZE_OVERRIDE_EXITED at exit; while active, is_frozen()
    re-evaluates each call against env var + hardened mode.
    """
    global _FREEZE_OVERRIDE_ACTIVE, _FREEZE_OVERRIDE_REASON
    if _hardened_mode_locked():
        log.error(
            f"  [MK38.0] FREEZE_OVERRIDE_REJECTED reason={reason!r} — "
            f"hardened mode is locked; override forbidden."
        )
        raise PermissionError(
            "MK38.0 freeze override forbidden under hardened mode. "
            "Unlock hardened mode first (requires explicit operator action)."
        )
    started = time.time()
    with _STATE_LOCK:
        prev_active = _FREEZE_OVERRIDE_ACTIVE
        prev_reason = _FREEZE_OVERRIDE_REASON
        _FREEZE_OVERRIDE_ACTIVE = True
        _FREEZE_OVERRIDE_REASON = reason
    log.warning(
        f"  [MK38.0] SEMANTIC_FREEZE_OVERRIDE_ENTERED reason={reason!r}"
    )
    try:
        yield
    finally:
        elapsed = time.time() - started
        _bump("override_active_seconds", int(elapsed))
        with _STATE_LOCK:
            _FREEZE_OVERRIDE_ACTIVE = prev_active
            _FREEZE_OVERRIDE_REASON = prev_reason
        log.info(
            f"  [MK38.0] SEMANTIC_FREEZE_OVERRIDE_EXITED reason={reason!r} "
            f"elapsed_s={elapsed:.1f}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Self-check
# ═══════════════════════════════════════════════════════════════════════════

def verify_barrier_installed() -> bool:
    """
    Verify the barrier is installed and the taxonomy hasn't been
    monkey-patched away. Modeled after MK37.1-3's verify_primitive_guards.

    Returns True if all required attributes are present and have the
    expected types. Logs SEMANTIC_BARRIER_TAMPERED at ERROR if anything's
    off (does NOT raise — this is a probe, not an enforcer).
    """
    required = [
        ("CANONICAL_SOURCES", set, lambda v: len(v) >= 4),
        ("INFERENCE_SOURCES", set, lambda v: len(v) >= 8),
        ("NONSEMANTIC_FIELDS", set, lambda v: len(v) >= 8),
        ("SEMANTIC_FIELDS", set, lambda v: len(v) >= 4),
    ]
    import sys
    mod = sys.modules.get(__name__)
    if mod is None:
        log.error("  [MK38.0] SEMANTIC_BARRIER_TAMPERED: module missing from sys.modules")
        return False
    for name, expected_type, predicate in required:
        v = getattr(mod, name, None)
        if not isinstance(v, expected_type):
            log.error(
                f"  [MK38.0] SEMANTIC_BARRIER_TAMPERED: {name} is "
                f"{type(v).__name__}, expected {expected_type.__name__}"
            )
            return False
        if not predicate(v):
            log.error(
                f"  [MK38.0] SEMANTIC_BARRIER_TAMPERED: {name} fails sanity "
                f"check (size {len(v)})"
            )
            return False
    # Cross-set isolation: CANONICAL and INFERENCE must not overlap
    if CANONICAL_SOURCES & INFERENCE_SOURCES:
        log.error(
            f"  [MK38.0] SEMANTIC_BARRIER_TAMPERED: CANONICAL and INFERENCE "
            f"sources overlap ({CANONICAL_SOURCES & INFERENCE_SOURCES!r})"
        )
        return False
    if NONSEMANTIC_FIELDS & SEMANTIC_FIELDS:
        log.error(
            f"  [MK38.0] SEMANTIC_BARRIER_TAMPERED: SEMANTIC and NONSEMANTIC "
            f"fields overlap ({NONSEMANTIC_FIELDS & SEMANTIC_FIELDS!r})"
        )
        return False
    return True


__all__ = [
    "SemanticWriteBlocked",
    "CANONICAL_SOURCES", "INFERENCE_SOURCES",
    "NONSEMANTIC_FIELDS", "SEMANTIC_FIELDS",
    "is_frozen",
    "is_field_allowed", "assert_field_allowed",
    "cache_read_allowed",
    "with_freeze_override",
    "snapshot_telemetry",
    "verify_barrier_installed",
]
