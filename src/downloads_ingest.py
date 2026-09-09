"""
downloads_ingest.py — MK17 Phase B (M1 + M6)
==============================================
Ingestion of loose audio files from:
  M1: host-side Downloads folder (e.g. C:\\Users\\FeisalK\\Downloads)
  M6: device-side internal Download folder (e.g. /storage/emulated/0/Download)

LOG_16_1 captured the user's Pre-sync stage running over a file at
`C:\\Users\\FeisalK\\Downloads\\AMApiano #1.mp3` — a loose track the
user dropped into Downloads that was being audited but not actually
ingested into the canonical Music tree.

Per Apr 30 user direction (match-only):
    Ingest only files that fuzzy-match a Spotify track in the user's
    library.  Unmatched files are LEFT ALONE — we don't move or rename
    them, and we don't push them to the device.

Workflow:
    1. Scan all configured ingest dirs for audio files.
    2. For each audio file, read existing tags via mutagen.
    3. Build a fuzzy-match candidate set from the user's track_map.
    4. If we get a confident match (SequenceMatcher ratio >= threshold),
       hand the file off to the normal downloader pipeline as if it had
       just been downloaded — same _tag_file path, same cover-art path,
       same transfer path.
    5. If no match, leave the file alone.

The ingest is purely additive: an ingested file replaces a download
attempt, not a new artifact on the user's disk.  When ingest finds a
match for a Liked track, that track is added to the
`already_have_locally` set so the downloader doesn't try to re-download it.
"""
from __future__ import annotations
import logging
import pathlib
import re
import shutil
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable

log = logging.getLogger(__name__)

_AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav")

_DOWNLOADER_PREFIX_RE = re.compile(
    r"(?i)^(?:"
    r"SpotiDownloader\.com\s*-\s*"
    r"|soundcloudaud\.com[_ ]*"
    r"|SoundCloud\.app[_ ]*"
    r"|soundcloud[_ ]*"
    r"|ytdl[_ ]*"
    r"|yt-dlp[_ ]*"
    r")"
)

def _strip_dl_prefix(s: str) -> str:
    """Remove known downloader tool prefixes from a folder/album/artist string."""
    return _DOWNLOADER_PREFIX_RE.sub("", s).strip(" -_")

# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _norm_for_match(s: str) -> str:
    """Aggressive normalisation for fuzzy-matching: lowercase, NFKD,
    drop punctuation, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)        # drop parenthesised qualifiers
    s = re.sub(r"\bfeat\.?\b|\bft\.?\b", " ", s)  # drop feat./ft. markers
    s = re.sub(r"[^a-z0-9 ]+", " ", s)            # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _read_tags(path: pathlib.Path) -> dict:
    """Return {title, artist, album} from the file's existing tags.
    Returns empty strings on read errors — caller should fall back to
    parsing the filename."""
    out = {"title": "", "artist": "", "album": ""}
    try:
        from mutagen import File as MutagenFile
        m = MutagenFile(str(path), easy=True)
        if m is not None and m.tags is not None:
            out["title"]  = (m.tags.get("title", [""]) or [""])[0]
            out["artist"] = (m.tags.get("artist", [""]) or [""])[0]
            out["album"]  = (m.tags.get("album", [""]) or [""])[0]
    except Exception as e:
        log.debug(f"  [Ingest] tag-read failed for {path.name!r}: {e}")
    return out


def _parse_filename(path: pathlib.Path) -> dict:
    """Best-effort parse of `Artist - Album - 01 Title.ext` style filenames.
    Returns {title, artist, album} with empty strings if a field can't
    be inferred."""
    stem = path.stem
    parts = [p.strip() for p in re.split(r"\s+-\s+", stem)]
    out = {"title": "", "artist": "", "album": ""}
    if len(parts) >= 4:
        # Artist - Album - NN Title  (or)  Artist - Album - CDx - NN Title
        out["artist"] = parts[0]
        out["album"]  = parts[1]
        # Last segment usually has 'NN Title' format
        title_seg = parts[-1]
        title_seg = re.sub(r"^\d+\s+", "", title_seg)
        out["title"] = title_seg
    elif len(parts) == 3:
        out["artist"] = parts[0]
        out["album"]  = parts[1]
        out["title"]  = re.sub(r"^\d+\s+", "", parts[2])
    elif len(parts) == 2:
        out["artist"] = parts[0]
        out["title"]  = re.sub(r"^\d+\s+", "", parts[1])
    else:
        out["title"] = re.sub(r"^\d+\s+", "", stem)
    return out


def _extract_signal(path: pathlib.Path) -> dict:
    """Combine tag-read with filename-parse, prefer non-empty tag fields."""
    sig = _parse_filename(path)
    tags = _read_tags(path)
    for k in ("title", "artist", "album"):
        if tags.get(k):
            sig[k] = tags[k]
    # Strip downloader tool prefixes from album/artist that leaked into tags or filenames
    for k in ("album", "artist"):
        if sig.get(k):
            sig[k] = _strip_dl_prefix(sig[k])
    # Also strip from parent folder name used as album fallback
    parent = path.parent.name
    parent_clean = _strip_dl_prefix(parent)
    if parent_clean and parent_clean != parent and not sig.get("album"):
        sig["album"] = parent_clean
    return sig


# ──────────────────────────────────────────────────────────────────────────
# Match against library
# ──────────────────────────────────────────────────────────────────────────
def find_match(file_signal: dict, library_tracks: list[dict],
               threshold: float = 0.85) -> dict | None:
    """
    Return the best-matching library track for *file_signal*, or None
    if no candidate scores above *threshold*.

    Scoring: SequenceMatcher ratio on normalised "artist title" string,
    with a fallback to title-only when one side has no usable artist
    field (covers the LOG_16_1 'AMApiano #1.mp3' case where the file
    had no embedded artist tag).
    Album bonus: +0.05 if the album name also matches.
    """
    f_title  = _norm_for_match(file_signal.get("title", ""))
    f_artist = _norm_for_match(file_signal.get("artist", ""))
    f_album  = _norm_for_match(file_signal.get("album", ""))

    if not f_title:
        return None  # can't match without at least a title

    f_key = f"{f_artist} {f_title}".strip()

    best_track  = None
    best_score  = 0.0

    for t in library_tracks:
        t_title  = _norm_for_match(t.get("title", ""))
        t_artist = _norm_for_match(t.get("artist", "")
                                   or (t.get("artists") or [""])[0])
        t_album  = _norm_for_match(t.get("album", ""))

        if not t_title:
            continue

        # Compute both the full artist+title score AND a title-only score,
        # take the max.  The title-only path rescues files with empty
        # tag.artist where the library entry's "Various Artists" would
        # otherwise drag the ratio below threshold.
        t_key = f"{t_artist} {t_title}".strip()
        score_full     = SequenceMatcher(None, f_key, t_key).ratio()
        score_title    = SequenceMatcher(None, f_title, t_title).ratio()
        score = max(score_full, score_title)

        # Album bonus
        if f_album and t_album:
            album_ratio = SequenceMatcher(None, f_album, t_album).ratio()
            if album_ratio > 0.85:
                score += 0.05

        if score > best_score:
            best_score = score
            best_track = t

    if best_score >= threshold:
        return best_track
    return None


# ──────────────────────────────────────────────────────────────────────────
# Ingest scan — host side (M1)
# ──────────────────────────────────────────────────────────────────────────
def scan_host_dirs(dirs: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    """Walk each dir looking for audio files (one level deep)."""
    found: list[pathlib.Path] = []
    for d in dirs:
        try:
            d = pathlib.Path(d)
            if not d.exists() or not d.is_dir():
                continue
            for entry in d.iterdir():
                if entry.is_file() and entry.suffix.lower() in _AUDIO_EXTS:
                    found.append(entry)
        except Exception as e:
            log.debug(f"  [Ingest] scan error on {d!r}: {e}")
    return found


# ──────────────────────────────────────────────────────────────────────────
# Ingest scan — device side (M6)
# ──────────────────────────────────────────────────────────────────────────
def scan_device_dirs(dirs: Iterable[str]) -> list[str]:
    """List audio files in each device dir via `adb shell find`.
    Returns absolute device paths (strings, not pathlib.Path because
    the device uses POSIX paths regardless of host OS)."""
    found: list[str] = []
    try:
        from adb_transfer import _adb_cmd
    except Exception as e:
        log.debug(f"  [Ingest/device] adb_transfer unavailable: {e}")
        return found

    ext_pattern = "|".join(e.lstrip(".") for e in _AUDIO_EXTS)
    for d in dirs:
        try:
            # `find` rather than `ls`, to handle non-existent dirs gracefully
            script = f"find {d!r} -maxdepth 2 -type f 2>/dev/null"
            out = _adb_cmd(["shell", "sh", "-c", script],
                          timeout=30, check=False)
            if not out:
                continue
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                ext = line.rsplit(".", 1)[-1].lower() if "." in line else ""
                if f".{ext}" in _AUDIO_EXTS:
                    found.append(line)
        except Exception as e:
            log.debug(f"  [Ingest/device] scan error on {d!r}: {e}")
    return found


# ──────────────────────────────────────────────────────────────────────────
# Public entry point — called from walkman_sync as Stage 0.5
# ──────────────────────────────────────────────────────────────────────────
def ingest_loose_files(track_map: dict, threshold: float = 0.85,
                        host_dirs: Iterable[pathlib.Path] | None = None,
                        device_dirs: Iterable[str] | None = None
                        ) -> dict:
    """
    Walk all configured ingest dirs and return:
        {
          "host_matches":    [(host_path, matched_track_dict), ...],
          "device_matches":  [(device_path, matched_track_dict), ...],
          "host_unmatched":  [host_path, ...],
          "device_unmatched":[device_path, ...],
        }

    track_map is the same shape produced by walkman_sync._fetch_from_spotify
    (dict[playlist_name -> list[track_dict]]).

    Caller is responsible for actually moving/copying the matched files
    into the canonical Music tree — this function only IDENTIFIES
    matches.  This separation lets the caller decide whether to use the
    full _tag_file + transfer pipeline (production) or just log the
    plan (dry-run).
    """
    try:
        import config
        if host_dirs is None:
            host_dirs = getattr(config, "INGEST_HOST_DIRS", []) or []
        if device_dirs is None:
            device_dirs = getattr(config, "INGEST_DEVICE_DIRS", []) or []
    except Exception:
        host_dirs = host_dirs or []
        device_dirs = device_dirs or []

    # Flatten library
    library_tracks: list[dict] = []
    seen_ids: set = set()
    for tracks in (track_map or {}).values():
        for t in tracks or []:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            if tid and tid in seen_ids:
                continue
            if tid:
                seen_ids.add(tid)
            library_tracks.append(t)

    # ── Host scan ─────────────────────────────────────────────────────
    host_files = scan_host_dirs(host_dirs)
    host_matches: list = []
    host_unmatched: list = []
    for f in host_files:
        sig = _extract_signal(f)
        match = find_match(sig, library_tracks, threshold=threshold)
        if match:
            host_matches.append((f, match))
        else:
            host_unmatched.append(f)

    # ── Device scan ───────────────────────────────────────────────────
    device_files = scan_device_dirs(device_dirs)
    device_matches: list = []
    device_unmatched: list = []
    for path_str in device_files:
        # Extract title from filename only (we can't read tags over MTP
        # without pulling the whole file; matching by filename is the
        # only viable cheap option for device-side ingest)
        p = pathlib.Path(path_str)
        sig = _parse_filename(p)
        match = find_match(sig, library_tracks, threshold=threshold)
        if match:
            device_matches.append((path_str, match))
        else:
            device_unmatched.append(path_str)

    log.info(
        f"  [Ingest] Scan complete: "
        f"host={len(host_matches)}/{len(host_files)} matched, "
        f"device={len(device_matches)}/{len(device_files)} matched"
    )
    return {
        "host_matches":     host_matches,
        "device_matches":   device_matches,
        "host_unmatched":   host_unmatched,
        "device_unmatched": device_unmatched,
    }
