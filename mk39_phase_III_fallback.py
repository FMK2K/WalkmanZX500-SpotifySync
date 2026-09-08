"""
mk39_phase_III_fallback.py — MK39 Phase III G.2: Phase-I-fallback caller
================================================================================

Council §G success criterion: "invoked on files where all other metadata
resolution fails (blank tags, no folder hint, no API hits)."

Adversarial-review constraint (locked): MUST NOT modify
mk39_phase_I_containment.py (Phase I is locked; verified resolving 9/9
forensic-log cases). The fingerprint fallback runs at the CALLER level —
audit.py / walkman_sync invoke Phase I as today, and ONLY when Phase I
returns blank AND audio is on disk, this module is invoked.

Module contract:
  attempt_fingerprint_fallback(audio_path, current_resolution) -> ResolvedIdentity | None

  Returns a ResolvedIdentity (provenance="acoustic_fingerprint") if the
  fingerprint resolves to a confident MusicBrainz match; otherwise None.
  Caller treats None as "still unresolved → write to .needs_review.json".
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def is_phase_I_resolution_blank(current_resolution: dict) -> bool:
    """Per Council §G: fingerprint fallback fires only when Phase I (and the
    rest of the metadata waterfall) leaves identity blank. Sharp definition:
    blank artist OR blank title (matches the .needs_review.json criterion
    locked in Phase II TARGET-I)."""
    if not isinstance(current_resolution, dict):
        return True
    artist = (current_resolution.get("artist") or "").strip()
    title  = (current_resolution.get("title") or "").strip()
    return not artist or not title


def attempt_fingerprint_fallback(
    audio_path: os.PathLike,
    current_resolution: dict,
    *,
    api_key: Optional[str] = None,
    cache_path: os.PathLike = ".acoustid_cache.json",
):
    """Try acoustic fingerprint resolution. Returns ResolvedIdentity or None.

    Conditions for invocation (all must hold):
      1. Phase I result is blank (artist OR title missing)
      2. audio_path exists on disk
      3. Fingerprint subsystem is configured (api_key + fpcalc available)

    Failure modes are silent (None return) per Council §G "graceful degrade".
    """
    if not is_phase_I_resolution_blank(current_resolution):
        # Identity already resolved — don't waste a fingerprint on it.
        return None

    audio = Path(str(audio_path))
    if not audio.is_file():
        log.debug(f"  [Phase-III/G.2] audio not on disk: {audio_path}")
        return None

    try:
        from acoustid_fingerprint import resolve_identity
    except ImportError:
        log.debug("  [Phase-III/G.2] acoustid_fingerprint module not available")
        return None

    try:
        identity = resolve_identity(
            audio,
            api_key=api_key,
            cache_path=cache_path,
        )
    except Exception as e:
        log.debug(f"  [Phase-III/G.2] resolve_identity raised: "
                   f"{type(e).__name__}: {e}")
        return None

    if identity is None:
        return None

    # Promotion to provenance source happens here only if at least one of the
    # blank fields was filled in. This avoids "false promotion" where the
    # fingerprint matches but adds no new information.
    if (current_resolution.get("artist") or "").strip() and not identity.artist:
        # Existing artist was set; fingerprint adds no value
        return None
    if (current_resolution.get("title") or "").strip() and not identity.title:
        return None

    log.info(
        f"  [Phase-III/G.2] FINGERPRINT_FALLBACK_RESOLVED {audio.name!r} → "
        f"artist={identity.artist!r} title={identity.title!r} "
        f"album={identity.album!r} score={identity.score:.2f}"
    )
    return identity


__all__ = ["is_phase_I_resolution_blank", "attempt_fingerprint_fallback"]
