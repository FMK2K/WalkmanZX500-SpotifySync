"""
mk39_phase_III_canonical_sources.py — MK39 Phase III G.3 + H.3
================================================================================

Two new canonical sources extend the MK38.0 freeze whitelist for Phase III:

  G.3  acoustic_fingerprint  — identity resolved by Chromaprint+AcoustID+MusicBrainz
                                (Council §G success criterion)

  H.3  external_edit          — operator edited tags directly with Picard/MP3Tag/Beets
                                between platform runs; mtime-based detection with
                                a sticky 'external_authority' bit suppresses re-enrich
                                (Council §H success criterion)

Adversarial-review constraints honored:
  ✓ Two-source addition documented loudly (Council nervous about CANONICAL_SOURCES growth)
  ✓ Trust ranking explicit: acoustic_fingerprint < user_explicit; external_edit > most
  ✓ Registration is one-shot at bootstrap (idempotent via lock)
  ✓ Extensive test (suite test_mk39_phase_III.py)
"""

from __future__ import annotations

import logging
import threading
from typing import FrozenSet

log = logging.getLogger(__name__)

# Phase III source labels
ACOUSTIC_FINGERPRINT = "acoustic_fingerprint"
EXTERNAL_EDIT        = "external_edit"

PHASE_III_SOURCES: FrozenSet[str] = frozenset({
    ACOUSTIC_FINGERPRINT,
    EXTERNAL_EDIT,
})

# Trust ordering (highest authority first). Used by callers wanting to know
# whether to overwrite an existing tag. Higher in the list = wins.
TRUST_ORDERING = (
    "user_explicit",          # operator typed/accepted via quarantine_review
    "external_edit",          # operator edited file directly with Picard/etc.
    "user_canonical",         # MK39-A1.3 cross-validated user signal
    "musicbrainz_canonical",
    "spotify_canonical",
    "fingerprint_match",      # legacy MK37 fingerprint
    "acoustic_fingerprint",   # NEW Phase III G.3 (high but below operator)
)


_REGISTERED = False
_REG_LOCK = threading.Lock()


def register_phase_III_canonical_sources() -> bool:
    """Idempotently extend mk38_0_semantic_write_barrier.CANONICAL_SOURCES
    with the Phase III sources. Returns True if registration occurred or was
    already done; False on environmental error (e.g., MK38.0 not importable)."""
    global _REGISTERED
    with _REG_LOCK:
        if _REGISTERED:
            return True
        try:
            import mk38_0_semantic_write_barrier as mk38
        except ImportError:
            log.warning(
                "  [MK39-Phase-III] mk38_0_semantic_write_barrier unavailable; "
                "Phase III sources NOT registered. Acoustic fingerprint and "
                "external-edit writes will be REJECTED by the write barrier."
            )
            return False
        try:
            before = len(mk38.CANONICAL_SOURCES)
            mk38.CANONICAL_SOURCES.update(PHASE_III_SOURCES)
            after = len(mk38.CANONICAL_SOURCES)
            _REGISTERED = True
            log.info(
                f"  [MK39-Phase-III] CANONICAL_SOURCES_EXTENDED — added "
                f"{sorted(PHASE_III_SOURCES)} to MK38.0 freeze whitelist; "
                f"sources before={before} after={after}"
            )
            return True
        except Exception as e:
            log.error(f"  [MK39-Phase-III] registration failed: "
                       f"{type(e).__name__}: {e}")
            return False


def is_registered() -> bool:
    return _REGISTERED


def reset_for_tests() -> None:
    """Test helper — DO NOT call from production code."""
    global _REGISTERED
    with _REG_LOCK:
        _REGISTERED = False


__all__ = [
    "ACOUSTIC_FINGERPRINT", "EXTERNAL_EDIT", "PHASE_III_SOURCES",
    "TRUST_ORDERING",
    "register_phase_III_canonical_sources", "is_registered",
]
