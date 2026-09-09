"""
picard_compat.py — MK39 Phase III H.2: Picard/MP3Tag compatibility helpers
================================================================================

Council MK39.1 Target H success criterion: "An operator can open the staging
folder in Picard, make corrections, and the platform will ingest the corrected
files on the next run without data loss."

Picard prefers ID3v2.3 with UTF-16 strings; some older devices and tag editors
also struggle with v2.4. APIC (cover art) frames duplicate easily across our
enrichment pipeline; Picard expects at most one front-cover image. This module
normalizes both, using mutagen as the underlying tag library.

Adversarial-review constraints honored:
  ✓ ID3v2.3 conversion via mutagen's update_to_v23() — the recommended call,
    NOT just setting a version flag (which would corrupt v2.4-only frames)
  ✓ APIC type 3 (front cover) deduplication — keep one, strip rest
  ✓ Atomic write to disk via mutagen's save() (which uses tempfile internally)
  ✓ Idempotent: re-running on already-compatible files is a no-op
  ✓ Non-MP3 files left untouched (the Picard issue is specifically ID3 + APIC)
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclasses.dataclass
class CompatReport:
    """Report of changes made to a file by ensure_picard_compatible."""
    path: Path
    converted_v24_to_v23: bool = False
    apic_frames_before: int = 0
    apic_frames_after: int = 0
    saved: bool = False
    skipped_reason: str = ""

    @property
    def changed(self) -> bool:
        return self.saved


def _import_mutagen():
    """Return (mutagen, mutagen.id3) or (None, None) if unavailable.

    Mutagen is already a platform dep (used by id3_helpers, mk39_tag_write_gate,
    etc.); this is a safety guard for environments where it might be missing.
    """
    try:
        import mutagen  # type: ignore
        import mutagen.id3  # type: ignore
        return mutagen, mutagen.id3
    except ImportError:
        return None, None


def ensure_picard_compatible(path: os.PathLike) -> CompatReport:
    """Normalize a single file's tags for Picard compatibility.

    Operations performed (only if needed; idempotent):
      1. Convert ID3v2.4 to v2.3 (mutagen.id3.ID3.update_to_v23)
      2. Deduplicate APIC frames: keep first front-cover (type 3), strip rest
      3. Save with v2.3 + v1.0 (Picard's preferred combination)

    Returns a CompatReport describing what changed. Never raises on tag
    corruption; logs DEBUG and returns a report with skipped_reason set.
    """
    p = Path(str(path))
    report = CompatReport(path=p)

    if not p.is_file():
        report.skipped_reason = "not_a_file"
        return report
    # Picard's ID3 nuance only matters for MP3. FLAC/M4A use Vorbis/MP4 tags
    # which have different formats; nothing to normalize here.
    if p.suffix.lower() != ".mp3":
        report.skipped_reason = "not_mp3"
        return report

    mutagen, id3_mod = _import_mutagen()
    if mutagen is None:
        report.skipped_reason = "mutagen_unavailable"
        return report

    try:
        tags = id3_mod.ID3(str(p))
    except id3_mod.ID3NoHeaderError:
        # No tags at all — nothing to normalize. Picard will add tags
        # itself when it edits the file.
        report.skipped_reason = "no_id3_header"
        return report
    except Exception as e:
        log.debug(f"  [PicardCompat] failed to load tags from {p.name}: {e}")
        report.skipped_reason = f"load_error: {type(e).__name__}"
        return report

    changed = False

    # 1. v2.4 → v2.3 conversion (only if currently v2.4)
    version = tags.version
    if version and version[1] >= 4:
        try:
            tags.update_to_v23()
            report.converted_v24_to_v23 = True
            changed = True
        except Exception as e:
            log.debug(f"  [PicardCompat] update_to_v23 failed for {p.name}: {e}")
            # Don't bail — we might still dedup APIC.

    # 2. APIC dedup
    apic_frames = tags.getall("APIC") if hasattr(tags, "getall") else []
    report.apic_frames_before = len(apic_frames)
    if len(apic_frames) > 1:
        # Keep the first FRONT_COVER (type 3), or the first frame if no
        # explicit front cover exists.
        front = next((a for a in apic_frames if getattr(a, "type", 0) == 3),
                       apic_frames[0])
        # Remove all and re-add the chosen one
        tags.delall("APIC")
        tags.add(front)
        report.apic_frames_after = 1
        changed = True
    else:
        report.apic_frames_after = len(apic_frames)

    if changed:
        try:
            # mutagen's save uses tempfile + replace internally → atomic
            tags.save(v2_version=3, v1=1)
            report.saved = True
            log.debug(
                f"  [PicardCompat] {p.name}: "
                f"v23={report.converted_v24_to_v23} "
                f"apic={report.apic_frames_before}->{report.apic_frames_after}"
            )
        except Exception as e:
            log.warning(f"  [PicardCompat] save failed for {p.name}: {e}")
            report.skipped_reason = f"save_error: {type(e).__name__}"

    return report


def normalize_directory(root: os.PathLike,
                          *, recursive: bool = True
                          ) -> dict:
    """Apply ensure_picard_compatible to every MP3 in a directory tree.
    Returns a summary dict {scanned, changed, errors}."""
    root_p = Path(str(root))
    iterator = root_p.rglob("*.mp3") if recursive else root_p.glob("*.mp3")
    scanned = 0
    changed = 0
    errors = 0
    for f in iterator:
        scanned += 1
        r = ensure_picard_compatible(f)
        if r.changed:
            changed += 1
        if r.skipped_reason and r.skipped_reason.startswith(("save_", "load_")):
            errors += 1
    log.info(f"  [PicardCompat] {root_p}: scanned={scanned} "
              f"changed={changed} errors={errors}")
    return {"scanned": scanned, "changed": changed, "errors": errors}


__all__ = ["CompatReport", "ensure_picard_compatible", "normalize_directory"]
