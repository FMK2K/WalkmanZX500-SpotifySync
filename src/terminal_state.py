"""
terminal_state.py — MK39.1 C2: "Unknown is a terminal state" policy
================================================================================

Council MK39.1 directive (2026-05-10, P0+, must ship FIRST):

    "Plug the bottom of the funnel first. The hard-block on Abstract Hip-Hop
    is a band-aid; structural None-as-terminal-state is the cure."

This module codifies the empty-string-as-terminal-blank convention used
throughout the codebase. The convention: when a semantic field (genre,
artist, album, title, year) cannot be resolved with confidence, the field
is left blank ("" — empty string), and that blank propagates through the
pipeline to be picked up by quarantine_review --needs-review.

Default-writes (substituting a guessed value when the truth is unknown) are
the contamination root cause that produced the "Abstract Hip-Hop on every
unknown track" failure mode in LOG_9_5_26.

This module provides:
  - IS_BLANK(s): canonical predicate for "this field is the terminal-blank state"
  - FORBIDDEN_DEFAULT_SOURCES: source labels that must NEVER reach the write barrier
  - is_forbidden_default_source(source): runtime check used by MK38.0 barrier
  - DefaultWriteAttempt: dataclass for runtime detector telemetry
  - audit_catalog: documentation of historical default-write sites and how
                    each was either fixed or proven safe
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, FrozenSet, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# C2.4 — Terminal-blank predicate
# ─────────────────────────────────────────────────────────────────────────

def IS_BLANK(value: Any) -> bool:
    """True iff `value` represents the terminal "unknown" state.

    The codebase uses empty string ("") as the canonical blank. This helper
    accepts both None and "" (and whitespace-only strings) so callers can
    use the predicate without first normalizing — but they SHOULD normalize
    to "" (not None) when storing/passing along.

    Rationale: introducing a separate None sentinel alongside "" creates
    dual-state ambiguity (does None mean "not yet checked" or "checked and
    unknown"?). Empty string consistently means "checked and unknown".
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    # For non-string values, blank means "missing"
    return value in (0, [], {}, ())


def normalize_to_blank(value: Any) -> str:
    """Coerce any blank-y value to the canonical "" so downstream code only
    has to check one form. Non-blank values are returned as str(value).strip()."""
    if IS_BLANK(value):
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


# ─────────────────────────────────────────────────────────────────────────
# C2.3 — Forbidden default-source policy
# ─────────────────────────────────────────────────────────────────────────
# These are source labels that must NEVER reach the MK38.0 write barrier.
# Any code path that tries to write a semantic field with these sources is
# attempting a default-write — exactly the contamination pattern Council §4
# locked as the C2 root cause. The barrier rejects the write outright.
#
# Adversarial-review scope (locked): keep this set small + explicit. False
# positives = legitimate writes blocked. The list below was vetted against
# the existing CANONICAL_SOURCES + INFERENCE_SOURCES sets and confirmed not
# to collide.
FORBIDDEN_DEFAULT_SOURCES: FrozenSet[str] = frozenset({
    # Generic placeholders any code path might use as a "fill it with anything" guess
    "default",
    "default_fallback",
    "default_genre",
    "default_artist",
    "default_album",
    "fallback",
    "fallback_default",

    # "I don't know but I have to write something" patterns
    "unknown",
    "unknown_genre",
    "placeholder",
    "auto_fill",
    "guess",
    "guessed",
    "speculation",
})


def is_forbidden_default_source(source: Any) -> bool:
    """True iff the given source label is in the forbidden set.

    Used by MK38.0 write barrier to reject default-writes regardless of
    whether the source is also in CANONICAL_SOURCES (defense in depth).
    """
    if not isinstance(source, str):
        return False
    return source.strip().lower() in FORBIDDEN_DEFAULT_SOURCES


# ─────────────────────────────────────────────────────────────────────────
# C2.1 — Runtime default-write detector (telemetry)
# ─────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class DefaultWriteAttempt:
    """Telemetry record for a default-write that the C2 policy intercepted."""
    field:      str
    source:     str
    value:      str
    file_path:  str = ""
    rejected:   bool = True   # True = barrier rejected; False = warning-only mode


# Module-level counter (incremented on each interception).
_DEFAULT_WRITE_ATTEMPTS: list[DefaultWriteAttempt] = []
import threading as _threading
_DEFAULT_WRITE_LOCK = _threading.Lock()


def record_default_write_attempt(attempt: DefaultWriteAttempt) -> None:
    """Append to the in-memory record. Tests and audit tools can read this
    via get_default_write_attempts(); production code logs WARNING."""
    with _DEFAULT_WRITE_LOCK:
        _DEFAULT_WRITE_ATTEMPTS.append(attempt)
    log.warning(
        f"  [C2/Detector] DEFAULT_WRITE_INTERCEPTED field={attempt.field!r} "
        f"source={attempt.source!r} value={attempt.value!r} "
        f"file={attempt.file_path!r} rejected={attempt.rejected}"
    )


def get_default_write_attempts() -> list[DefaultWriteAttempt]:
    """Snapshot of intercepted attempts. Used by tests + audit reporting."""
    with _DEFAULT_WRITE_LOCK:
        return list(_DEFAULT_WRITE_ATTEMPTS)


def reset_default_write_attempts() -> None:
    """Test helper — clear the buffer."""
    with _DEFAULT_WRITE_LOCK:
        _DEFAULT_WRITE_ATTEMPTS.clear()


# ─────────────────────────────────────────────────────────────────────────
# C2.1 — Audit catalog (historical default-write sites + their disposition)
# ─────────────────────────────────────────────────────────────────────────
# This catalog documents the audit performed during MK39.1 C2 work. Each
# entry: where the historical default-write existed, what category, and
# what fix was applied. Reviewers (and future maintainers) consult this to
# confirm coverage.

AUDIT_CATALOG = {
    "ai_helper.py:infer_genre": {
        "pattern": "AI returns guess for out-of-taxonomy genre",
        "disposition": "FIXED — strict mode in genre_client._validate_ai_genre rejects "
                          "out-of-taxonomy genres when STRICT_AI_GENRE_VALIDATION=True.",
        "covered_by_test": "test_T2_strict_mode_rejects_out_of_taxonomy",
    },
    "genre_client.py:_validate_ai_genre line 753 (historical)": {
        "pattern": "log.warning(...not in taxonomy; accepting) — accepted any AI guess",
        "disposition": "FIXED — under STRICT mode (default ON), returns blank + records "
                          "DefaultWriteAttempt for telemetry instead of accepting.",
        "covered_by_test": "test_T2_strict_mode_rejects_out_of_taxonomy",
    },
    "ai_helper.py: 'Abstract Hip-Hop' overuse pattern": {
        "pattern": "AI providers were returning 'Abstract Hip-Hop' as a default for "
                      "many unknown niche genres. Note: 'Abstract Hip-Hop' IS a valid "
                      "genre in our taxonomy — the contamination was BEHAVIORAL "
                      "(over-using a real value as a guess), not VALIDATIONAL.",
        "disposition": "STILL HANDLED BY PHASE I (mk39_phase_I_containment.block_"
                          "abstract_hip_hop_default). C2 does NOT subsume this; C2 "
                          "handles a different category (out-of-taxonomy guesses). "
                          "Both layers are necessary; neither alone suffices.",
        "covered_by_test": "test_T6_phase_I_forensic_9_of_9_still_resolving "
                              "(Phase I containment test)",
    },
    "audit.py:multiple `(.get('field') or '').strip()` patterns": {
        "pattern": "Field defaults to '' when missing — already correct convention",
        "disposition": "VERIFIED SAFE — these are READS not WRITES; the empty default "
                          "is exactly the terminal-blank we want propagating downstream.",
        "covered_by_test": "test_T6b_blank_input_yields_blank_output_through_validation",
    },
    "audit.py:1791 `tags.get('title') or 'Unknown'`": {
        "pattern": "Filename sanitization fallback to literal 'Unknown'",
        "disposition": "ACCEPTABLE — this is a FILENAME segment, not a semantic tag "
                          "field. Filenames need a placeholder; tags do not. Filename "
                          "writes are NONSEMANTIC_FIELDS in MK38.0.",
        "covered_by_test": "(no test needed — nonsemantic field, write barrier "
                              "doesn't apply)",
    },
    "Future audit": {
        "pattern": "Any new code path that wants to write a default value",
        "disposition": "BLOCKED — MK38.0 write barrier rejects sources in "
                          "FORBIDDEN_DEFAULT_SOURCES. Runtime detector logs WARNING.",
        "covered_by_test": "test_T5_barrier_rejects_forbidden_default_source_writes",
    },
}


def print_audit_catalog() -> None:
    """Operator-facing pretty-printer for the audit catalog."""
    print("MK39.1 C2 — Default-Write Audit Catalog")
    print("=" * 78)
    for site, entry in AUDIT_CATALOG.items():
        print(f"\n{site}")
        print(f"  Pattern:     {entry['pattern']}")
        print(f"  Disposition: {entry['disposition']}")
        print(f"  Test:        {entry['covered_by_test']}")
    print()


__all__ = [
    "IS_BLANK", "normalize_to_blank",
    "FORBIDDEN_DEFAULT_SOURCES", "is_forbidden_default_source",
    "DefaultWriteAttempt", "record_default_write_attempt",
    "get_default_write_attempts", "reset_default_write_attempts",
    "AUDIT_CATALOG", "print_audit_catalog",
]
