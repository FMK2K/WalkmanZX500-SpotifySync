"""
export_stager.py — MK39 Phase III F.1: clean staging directory producer
================================================================================

Council MK39.1 Target F success criterion: "Full library sync from staging
folder to Walkman via MusicBee, with zero custom device I/O."

This module produces the staging directory that MusicBee (or any external
sync tool) consumes. It does NOT itself touch the device — it ONLY mirrors
the user's tagged audio into a canonical, deduplicated, MusicBee-ready
directory tree.

Adversarial-review constraints honored:
  ✓ shutil.copy2 preserves mtime (never mutates source)
  ✓ Hardlinks where same filesystem (avoids 100GB×2 disk usage)
  ✓ Filename collision handling (sanitize → suffix on collision)
  ✓ Idempotent: re-staging unchanged files is a no-op (mtime+size compare)
  ✓ All writes through staging_atomic.atomic_copy / safe_link_or_copy
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple

log = logging.getLogger(__name__)

# Audio extensions we'll stage. Anything else is ignored.
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
                     ".wav", ".wma"}

# Characters illegal in filenames on Windows (most restrictive of common OSes).
# Replaced with "-" during canonical filename construction.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')

# Chosen filename template: "Artist - Album - NN Title.ext"
# Per Council §F: matches MusicBee's default device profile expectation.
DEFAULT_FILENAME_TEMPLATE = "{artist} - {album} - {track:02d} {title}{ext}"


@dataclasses.dataclass
class StageResult:
    """Outcome of staging a single file."""
    src: Path
    dst: Optional[Path]   # None if skipped
    method: str            # 'hardlink' | 'copy' | 'skip_unchanged' | 'skip_no_audio' | 'skip_collision'
    note: str = ""

    @property
    def written(self) -> bool:
        return self.method in ("hardlink", "copy")


@dataclasses.dataclass
class StageSummary:
    """Aggregate over a full stage_tree run."""
    total_seen: int = 0
    written:    int = 0  # newly hardlinked or copied
    unchanged:  int = 0  # already-present + matching mtime+size
    skipped:    int = 0  # non-audio or collision-rejected
    by_method: dict = dataclasses.field(default_factory=dict)

    def record(self, r: StageResult) -> None:
        self.total_seen += 1
        if r.written:
            self.written += 1
        elif r.method == "skip_unchanged":
            self.unchanged += 1
        else:
            self.skipped += 1
        self.by_method[r.method] = self.by_method.get(r.method, 0) + 1


def _sanitize(s: str, fallback: str = "_") -> str:
    """Strip filename-illegal chars; collapse whitespace; trim."""
    if not s:
        return fallback
    out = _ILLEGAL_FILENAME_CHARS.sub("-", s)
    out = re.sub(r"\s+", " ", out).strip()
    # Strip trailing dots/spaces (Windows filesystem quirk)
    out = out.rstrip(". ")
    return out or fallback


def canonical_filename(
    *,
    artist: str, album: str, title: str, track: int, ext: str,
    template: str = DEFAULT_FILENAME_TEMPLATE,
) -> str:
    """Build a canonical filename per the template, sanitizing each field.

    Track 0 or negative is treated as 'no track number' and the NN field is
    omitted; in that case the dash before NN is preserved so format stays
    parseable but the number slot is blank.
    """
    artist = _sanitize(artist, "Unknown Artist")
    album  = _sanitize(album,  "Unknown Album")
    title  = _sanitize(title,  "Untitled")
    if not ext.startswith("."):
        ext = "." + ext
    if track <= 0:
        # Replace "{track:02d} " with empty to keep filename clean
        nameless = template.replace("{track:02d} ", "")
        return nameless.format(artist=artist, album=album, title=title, ext=ext)
    return template.format(artist=artist, album=album, title=title,
                              track=track, ext=ext)


def _resolve_collision(target: Path) -> Path:
    """If target exists and the caller didn't choose to overwrite, append
    ' (2)', ' (3)', ... until we find an unused name. Bounded at 99 attempts."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for n in range(2, 100):
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many collisions for {target}")


def _is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def _unchanged(src: Path, dst: Path) -> bool:
    """True if dst exists and has same size+mtime (sec resolution) as src."""
    try:
        s_src = src.stat()
        s_dst = dst.stat()
    except OSError:
        return False
    if s_src.st_size != s_dst.st_size:
        return False
    # mtime equality with 1-second tolerance to survive cross-fs rounding
    return abs(s_src.st_mtime - s_dst.st_mtime) < 1.5


def stage_one(
    src: Path,
    dst: Path,
    *,
    overwrite: bool = False,
) -> StageResult:
    """Stage a single file: link-or-copy with collision + idempotency handling.

    Args:
        src: source audio file
        dst: target path inside the staging tree
        overwrite: if True, replace dst even when content matches; if False
                    (default), skip when dst is byte-equivalent
    """
    if not _is_audio(src):
        return StageResult(src=src, dst=None, method="skip_no_audio")

    if dst.exists() and not overwrite:
        if _unchanged(src, dst):
            return StageResult(src=src, dst=dst, method="skip_unchanged",
                                 note="identical size+mtime")
        # Different content at the target — apply collision-suffix policy
        dst_resolved = _resolve_collision(dst)
        if dst_resolved != dst:
            log.debug(f"  [Stager] collision: {dst.name} -> {dst_resolved.name}")
            dst = dst_resolved

    # Defer to staging_atomic for the actual file ops (hardlink-or-copy)
    from staging_atomic import safe_link_or_copy
    final, method = safe_link_or_copy(src, dst)
    return StageResult(src=src, dst=final, method=method)


def stage_tree(
    source_root: os.PathLike,
    staging_root: os.PathLike,
    *,
    naming: Optional[Iterable[Tuple[Path, dict]]] = None,
    progress: Optional[callable] = None,
) -> StageSummary:
    """Walk source_root, stage all audio into staging_root.

    Args:
        source_root: the user's Music directory (read-only contract)
        staging_root: the destination staging directory
        naming: optional iterable of (src_path, tags_dict) pairs that override
                  the canonical filename. Used by callers who already have
                  authoritative tags (post-Phase-I) and want them reflected
                  in the staged filename. tags_dict requires keys: artist,
                  album, title, track. If naming is None, the source
                  filename is preserved (useful for first-time dry-run).
        progress: optional callback(stage_result) invoked per file.

    Returns a StageSummary with counts by outcome.
    """
    src_root = Path(source_root)
    stg_root = Path(staging_root)
    if not src_root.exists() or not src_root.is_dir():
        raise FileNotFoundError(f"source root does not exist: {src_root}")
    stg_root.mkdir(parents=True, exist_ok=True)

    summary = StageSummary()
    naming_map = {Path(p): t for p, t in (naming or [])}

    for src_file in src_root.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(src_root)
        # Determine destination filename
        tags = naming_map.get(src_file)
        if tags:
            dst_name = canonical_filename(
                artist=tags.get("artist", ""),
                album=tags.get("album", ""),
                title=tags.get("title", ""),
                track=int(tags.get("track", 0) or 0),
                ext=src_file.suffix,
            )
            # Group by Artist/Album in the staging tree (Picard-friendly)
            artist_dir = _sanitize(tags.get("artist", "Unknown Artist"))
            album_dir  = _sanitize(tags.get("album",  "Unknown Album"))
            dst = stg_root / artist_dir / album_dir / dst_name
        else:
            # Mirror source path unchanged
            dst = stg_root / rel

        result = stage_one(src_file, dst)
        summary.record(result)
        if progress is not None:
            try: progress(result)
            except Exception: pass

    log.info(
        f"  [Stager] {src_root} -> {stg_root}: "
        f"{summary.written} written, {summary.unchanged} unchanged, "
        f"{summary.skipped} skipped (of {summary.total_seen} seen)"
    )
    return summary


__all__ = [
    "StageResult", "StageSummary", "AUDIO_EXTENSIONS",
    "canonical_filename", "stage_one", "stage_tree",
]
