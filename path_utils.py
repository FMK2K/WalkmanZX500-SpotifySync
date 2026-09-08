"""
path_utils.py — Single canonical path builder for WalkmanSyncAgent
===================================================================
ALL modules must import canonical_folder() and canonical_dest_parts()
and canonical_filename() from here instead of computing paths locally.

Do NOT use sanitize_name(), _safe(), sanitize(), or _device_folder_parts()
for device path construction — those remain for display/internal use only.

Structure enforced on device:
    Music / {Artist} - {Album} / {Artist} - {Album} - {NN} {Title}.flac
         ↑ MUSIC_ROOT           ↑ one flat folder        ↑ full filename

This matches what walkman_sync._device_folder_parts() has always produced
for ASCII-only artists. walkman_reorganize previously used a two-level
Artist / Album / structure — that is now retired in favour of this flat one.
"""
from __future__ import annotations
import re
import unicodedata


def _pu_safe(name: str, max_len: int = 60) -> str:
    """
    FAT32-safe name component.
    - Replaces & with 'and' (matches existing sync/downloader behaviour)
    - NFC normalisation (preserves é, é etc — does NOT strip to ASCII)
    - Illegal FAT32 chars → underscore
    - UTF-8 byte-boundary truncation (safe for any Unicode)
    - Bracket-balances the truncated tail (Issue 3 / Council fix): this must
      match downloader._safe()'s truncation exactly, or the two produce
      different folder names for the same source string and the same album
      splinters into two on-device folders (walkman_dedup._track_keys can
      then no longer recognize them as the same album). See downloader.py
      _safe() Rev 14.9.7 (P2) for the original of this block.
    """
    name = str(name).replace("&", "and")
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    encoded = name.encode("utf-8")
    if len(encoded) > max_len:
        encoded = encoded[:max_len]
        name = encoded.decode("utf-8", errors="ignore").rstrip()
        for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
            if name.count(opener) > name.count(closer):
                idx = name.rfind(opener)
                name = name[:idx].rstrip(" ,;:-")
        name = name.rstrip(". ")
    return name or "Unknown"


def canonical_folder(track: dict) -> str:
    """
    Return the flat folder name for a track: 'Artist - Album'
    (without the Music root prefix).

    This is the ONE function all modules use for folder naming.
    """
    artist = _pu_safe(track.get("album_artist") or track.get("artist") or "Unknown", 40)
    album  = _pu_safe(track.get("album") or "Unknown", 60)
    return f"{artist} - {album}"


def canonical_dest_parts(track: dict, music_root: str = "Music") -> list[str]:
    """
    Full MTP dest_parts list for a track: [music_root, 'Artist - Album']
    Pass this directly as dest_parts to mtp.copy_file_to_device().
    """
    return [music_root, canonical_folder(track)]


def canonical_filename(track: dict, ext: str) -> str:
    """
    Return the canonical filename for a track (no path prefix):
        'Artist - Album - [CDx - ]NN Title[ (feat. X, Y)].ext'

    Rules:
    - Track number zero-padded to 2 digits
    - Multi-disc albums prefixed with CDx
    - Feature artists appended as '(feat. X, Y)' when not already in title
      and FEATURE_ARTISTS_IN_TITLE is True (default)
    - ext must include the leading dot, e.g. '.flac'
    """
    artist = _pu_safe(track.get("album_artist") or track.get("artist") or "Unknown", 40)
    album  = _pu_safe(track.get("album") or "Unknown", 60)
    title  = _pu_safe(track.get("title") or "Unknown", 80)

    disc   = str(track.get("disc_number") or "1")
    num    = str(track.get("track_number") or "1").zfill(2)

    # Feature artists
    artists = track.get("artists") or [track.get("artist", "")]
    feat_part = ""
    if len(artists) > 1:
        others = [a for a in artists[1:] if a]
        if others and not re.search(r"\b(?:feat\.?|ft\.?|featuring|with)\b", title, re.I):
            feat_joined = _pu_safe(", ".join(others), 60)
            feat_part   = f" (feat. {feat_joined})"

    cd_part = f"CD{disc} - " if disc not in ("", "1") else ""
    return f"{artist} - {album} - {cd_part}{num} {title}{feat_part}{ext}"


# ==========================================================================
# MK18 #7 — Path budget allocator for Windows MAX_PATH safety
# ==========================================================================
# ROOT CAUSE: Windows MAX_PATH = 260 chars. MTP namespace prefix adds
# ~50 chars. With deep artist/album/title hierarchies and Unicode names,
# full paths silently exceed 260, causing intermittent create failures
# or silent skips on transfer.
#
# Solution: a weighted truncation policy that preserves the semantically
# critical components (track number + extension) and caps the variable-
# length components (artist, album, title) with a deterministic "…" token.
#
# Budget allocation (total budget 250 to leave margin for MTP prefix):
#   storage/music_root prefix  : ~40 chars (e.g. "SD card/Music")
#   folder separator chars     :  5 chars
#   artist component           : 40 chars max
#   album component            : 50 chars max
#   filename (NN Title.ext)    : 80 chars max
#     = track number (4)       :  4
#     = title                  : 70 chars max
#     = extension              :  6 (.flac)
# ==========================================================================

_PATH_MAX           = 250   # leave 10-char margin below Windows 260
_ARTIST_MAX         = 40
_ALBUM_MAX          = 50
_TITLE_MAX          = 70
_TRUNC_TOKEN        = "…"   # single Unicode ellipsis (1 char)


def _trunc(s: str, max_len: int) -> str:
    """Truncate string to max_len chars, appending ellipsis if cut."""
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[:max_len - 1].rstrip() + _TRUNC_TOKEN


def path_budget_check(full_path: str) -> tuple[bool, int]:
    """
    Validate a full device path is within Windows MAX_PATH budget.
    Returns (ok: bool, length: int).  Call before each transfer.
    """
    return len(full_path) <= _PATH_MAX, len(full_path)


def safe_artist(artist: str) -> str:
    """Return artist name truncated to path-budget-safe length."""
    return _trunc(artist, _ARTIST_MAX)


def safe_album(album: str) -> str:
    """Return album name truncated to path-budget-safe length."""
    return _trunc(album, _ALBUM_MAX)


def safe_title(title: str) -> str:
    """Return title truncated to path-budget-safe length."""
    return _trunc(title, _TITLE_MAX)


def safe_filename(track_num: str, title: str, ext: str) -> str:
    """
    Build a safe filename: '{NN} {title}{ext}' within budget.
    track_num is preserved in full; title is truncated if needed.
    """
    prefix = f"{track_num} " if track_num else ""
    avail  = _TITLE_MAX - len(prefix)
    return f"{prefix}{_trunc(title, avail)}{ext}"


def canonical_device_path(track: dict, ext: str, storage: str,
                           music_root: str = "Music") -> str:
    """
    Full human-readable path string (for logging and status checks).
    Not used as dest_parts — just for display and DeviceLibrary key lookup.
    """
    folder = canonical_folder(track)
    fname  = canonical_filename(track, ext)
    return f"{storage}/{music_root}/{folder}/{fname}"
