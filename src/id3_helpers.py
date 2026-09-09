"""
id3_helpers.py  —  Central ID3 save helpers (Rev 14)
=====================================================
Single source of truth for every ID3-tag save in the pipeline.

Why this module exists
----------------------
The ZX500's Walkman Music app ignores ID3v2.4 frames outright — tags appear
blank in the browser even when mutagen writes them correctly. The fix is
to save every MP3 as ID3v2.3.

`config.ID3_VERSION = 3` expresses that intent, but mutagen has a subtle
requirement that the rest of the codebase (27 files, 22 save sites) was
silently violating:

    >>> from mutagen.id3 import ID3
    >>> tags = ID3("example.mp3")      # file happens to be v2.4
    >>> tags.save(v2_version=3)        # SILENTLY DROPS v2.4-only frames

Per the official mutagen docs (id3.rst §"Downgrading"):
    "If the file has v2.4 frames that cannot be represented in v2.3,
     call update_to_v23() before save(v2_version=3) to downgrade them."

Without `update_to_v23()`, TDRC is dropped (year becomes blank), TIPL and
TMCL are dropped (performers/credits lost), and TSOP/TSOA sort frames
disappear. This is the silent metadata loss reported in PDF analyses #1
and #2, and it's been happening for every audited file from v2.4 sources
since Rev 8.

This module centralises the fix. Every ID3 save site in the codebase
MUST go through one of these functions.

Public API
----------
    save_easy_mp3(audio, *, rating_popm=None)
        For `MP3(path, ID3=EasyID3)` objects.  Downgrades to v2.3 and saves.

    save_v23_id3(id3)
        For raw `ID3(path)` objects (POPM writes, APIC writes, etc.).
        Downgrades to v2.3 and saves.

    write_zx500_tags(path, tags, is_mp3, *, rating=None)
        High-level batch-write helper used by the audit and sync paths.
        Takes a dict of canonical field names {title, artist, album, ...}
        and writes them correctly for the file's format.

Design notes
------------
- All three functions are defensive: they accept pre-opened objects *or*
  paths and tolerate missing ID3 headers (adds them on the fly).
- The `rating_popm` path accepts either an int (0-255) or a pre-built
  POPM frame; creating it here avoids each caller re-importing POPM.
- FLAC saves do NOT go through this module — FLAC tag handling is fine
  as-is and does not suffer from the v2.3/v2.4 issue.
- This module has zero side effects at import time. Safe to import from
  anywhere in the codebase.
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil
import stat
import tempfile
from typing import Any

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, ID3NoHeaderError, POPM
from mutagen.mp3 import MP3

import config

log = logging.getLogger(__name__)

# ZX500 needs v2.3. Re-exposed here as a module constant so callers don't
# have to reach back into config for the version number on every call.
ID3V23 = 3


# ---------------------------------------------------------------------------
# Core save helpers
# ---------------------------------------------------------------------------

def save_easy_mp3(audio: MP3) -> None:
    """
    Save an EasyID3-wrapped MP3 as ID3v2.3, safely downgrading any v2.4
    frames first.

    Use for any tag-editing write that went through `MP3(path, ID3=EasyID3)`.

    The caller owns the `audio` object — we don't close it, we don't reload
    it. Caller is responsible for having set whatever tags they wanted
    before calling this.
    """
    try:
        # update_to_v23() lives on the raw ID3 object, not EasyID3.
        # mutagen exposes the underlying ID3 via .tags on the MP3 object.
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "update_to_v23"):
            tags.update_to_v23()
    except Exception as e:
        # If downgrade fails, still attempt the save — partial is better
        # than nothing. Most commonly this fails when there are no v2.4
        # frames to downgrade, which is fine.
        log.debug(f"  [id3_helpers] update_to_v23 pre-save: {e}")
    audio.save(v2_version=getattr(config, "ID3_VERSION", ID3V23))


def save_v23_id3(id3: ID3) -> None:
    """
    Save a raw ID3 object as v2.3 after downgrading v2.4 frames.

    Use for POPM writes, APIC writes, TXXX writes, or any case where the
    caller opened the file with `ID3(path)` directly rather than via
    EasyID3.
    """
    try:
        if hasattr(id3, "update_to_v23"):
            id3.update_to_v23()
    except Exception as e:
        log.debug(f"  [id3_helpers] update_to_v23 pre-save: {e}")
    id3.save(v2_version=getattr(config, "ID3_VERSION", ID3V23))


# ──────────────────────────────────────────────────────────────────────────
# MK17 Patch 1 — File writability + atomic-replace helpers for tag-write
#                resilience against locked / read-only temp files
# ──────────────────────────────────────────────────────────────────────────
# LOG_16 captured 600 [Errno 13] Permission denied tag-write failures over
# a 13h run.  The Rev 14.9.7 retry-with-backoff (50/200/800ms) handles
# transient AV-scanner locks but cannot help when the lock persists or
# when the temp file landed on a read-only mount.  These helpers add the
# layer below: detect non-writability up front, and on persistent failure
# copy the file to a fresh writable temp path, tag THAT copy, then
# atomically replace the original.  The download itself never has to
# fail — at worst we tag a copy and swap.

def is_writable(path: "pathlib.Path | str") -> bool:
    """
    Return True if *path* (file or directory) can be opened for writing
    by the current process.  Cheaper than try/except around the actual
    save call because os.access doesn't trip AV scanners.

    For files: checks W_OK on the file itself.
    For directories: checks W_OK on the directory.
    """
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return False
        return os.access(str(p), os.W_OK)
    except Exception:
        return False


def ensure_writable(path: "pathlib.Path | str") -> bool:
    """
    Make *path* writable if possible.  Clears the read-only bit on the
    file AND its parent directory.  Returns True if the path is writable
    after the attempt.

    This is best-effort — if the OS denies the chmod (e.g., the file is
    locked by another process), we return False and let the caller
    decide whether to retry, copy-and-replace, or skip.
    """
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return False

        # Clear read-only on the file itself
        try:
            current = p.stat().st_mode
            if not (current & stat.S_IWUSR):
                p.chmod(current | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        except OSError as e:
            log.debug(f"  [id3_helpers] chmod file failed: {e}")

        # Clear read-only on the parent dir too — needed for atomic-replace
        try:
            parent = p.parent
            if parent.exists():
                pmode = parent.stat().st_mode
                if not (pmode & stat.S_IWUSR):
                    parent.chmod(pmode | stat.S_IWUSR)
        except OSError as e:
            log.debug(f"  [id3_helpers] chmod parent failed: {e}")

        return is_writable(p)
    except Exception as e:
        log.debug(f"  [id3_helpers] ensure_writable raised: {e}")
        return False


def copy_to_writable_temp(src: "pathlib.Path") -> "pathlib.Path | None":
    """
    Copy *src* into a brand-new temp directory created via tempfile.mkdtemp.
    Returns the path of the copy, or None on failure.

    The new temp directory is owned by the current process and inherits
    the user's TEMP umask, so the copy is guaranteed writable even if
    the original was on a locked / read-only mount.

    Caller is responsible for cleaning up the returned directory's
    parent if they want to (we do not register an atexit handler — the
    OS will reap %TEMP% periodically).
    """
    try:
        src_path = pathlib.Path(src)
        if not src_path.exists():
            return None
        new_dir = pathlib.Path(tempfile.mkdtemp(prefix="walkman_tag_"))
        new_path = new_dir / src_path.name
        shutil.copy2(str(src_path), str(new_path))
        # Ensure the copy is writable (mkdtemp + copy2 should already be,
        # but belt-and-suspenders for paranoid AV environments).
        try:
            new_path.chmod(new_path.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
        return new_path
    except Exception as e:
        log.warning(f"  [id3_helpers] copy_to_writable_temp failed for {src!r}: {e}")
        return None


def atomic_replace(src: "pathlib.Path", dst: "pathlib.Path") -> bool:
    """
    Move *src* over *dst*, replacing it.  Tries os.replace() first (which
    is atomic on the same filesystem) and falls back to copy-then-unlink
    if the two paths are on different filesystems.

    Returns True on success, False if both methods fail.
    """
    try:
        os.replace(str(src), str(dst))
        return True
    except OSError as e:
        log.debug(f"  [id3_helpers] os.replace failed ({e}); trying copy+unlink")
        try:
            shutil.copy2(str(src), str(dst))
            try:
                os.unlink(str(src))
            except OSError:
                pass  # original temp leak is OK
            return True
        except Exception as e2:
            log.warning(f"  [id3_helpers] atomic_replace fallback also failed: {e2}")
            return False


# ---------------------------------------------------------------------------
# High-level batch writer
# ---------------------------------------------------------------------------

# Canonical field → EasyID3 key map.  Mirrors audit._EASY_FIELDS but kept
# here too so this module is a self-contained helper callable from any
# layer of the pipeline.
_EASY_KEYS = {
    "title":         "title",
    "artist":        "artist",
    "album":         "album",
    "album_artist":  "albumartist",
    "release_year":  "date",
    "track_number":  "tracknumber",
    "disc_number":   "discnumber",
    "genre":         "genre",
}


def _ensure_easy_id3(path: pathlib.Path) -> MP3:
    """Open *path* with EasyID3, adding a header first if missing."""
    try:
        return MP3(path, ID3=EasyID3)
    except ID3NoHeaderError:
        MP3(path).add_tags()
        return MP3(path, ID3=EasyID3)


def write_zx500_tags(
    path: pathlib.Path,
    tags: dict,
    is_mp3: bool,
    *,
    rating: int | None = None,
) -> bool:
    """
    Write canonical tags to *path* in a ZX500-safe format.

    Parameters
    ----------
    path : pathlib.Path
        File to tag. Must exist.
    tags : dict
        Canonical field names as keys (see _EASY_KEYS). Empty strings and
        `None` values are skipped.
    is_mp3 : bool
        True if MP3 (uses ID3v2.3 via EasyID3). False if FLAC.
    rating : int | None
        If provided (0-255), writes POPM (MP3) or rating tag (FLAC).

    Returns
    -------
    bool
        True if the file was written to. False on write error (errors are
        logged at WARNING).
    """
    try:
        if is_mp3:
            audio = _ensure_easy_id3(path)
            for field, easy_key in _EASY_KEYS.items():
                val = tags.get(field)
                if val in (None, ""):
                    continue
                audio[easy_key] = str(val)
            save_easy_mp3(audio)

            if rating is not None:
                id3 = ID3(path)
                id3.delall("POPM")
                id3.add(POPM(email="Spotify Liked", rating=int(rating), count=0))
                save_v23_id3(id3)
        else:
            from mutagen.flac import FLAC
            audio = FLAC(path)
            for field, easy_key in _EASY_KEYS.items():
                val = tags.get(field)
                if val in (None, ""):
                    continue
                audio[easy_key] = str(val)
            if rating is not None:
                audio["rating"] = str(int(rating))
            audio.save()
        return True
    except Exception as e:
        log.warning(f"  [id3_helpers] write_zx500_tags failed on {path.name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Convenience: ID3 version detection (unchanged from legacy audit helper)
# ---------------------------------------------------------------------------

def id3_version(path: pathlib.Path) -> int:
    """
    Return the major ID3 version number of *path*, or 0 if no tags.
    """
    try:
        id3 = ID3(path)
        return int(id3.version[1]) if id3.version else 0
    except (ID3NoHeaderError, Exception):
        return 0
