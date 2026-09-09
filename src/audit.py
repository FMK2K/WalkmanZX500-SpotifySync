"""
audit.py  -  Post-sync full-library metadata audit  (Rev 13)
============================================================
... (full header as before, unchanged) ...
Uses AI (Gemini → DeepSeek) to infer missing track/disc numbers when
all other sources fail, reducing "SKIP (track=00)" during reorganise.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import pathlib
import subprocess
import tempfile
import threading
import unicodedata
import datetime
import logging
import time
from typing import Callable
from collections import defaultdict

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, ID3NoHeaderError, POPM, TXXX
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

import config
import genre_client as gc
import mtp_transfer as mtp
import device_library
from walkman_sync import normalize_key, format_title_with_features, sanitize_name

# AI helper (Gemini → DeepSeek waterfall) for missing track/disc inference
import ai_helper

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DeepSeek AI assistant — metadata verification & folder coherence
# ---------------------------------------------------------------------------
# Used in two places:
#   1. _deepseek_verify_metadata()  — called from backtrack pass to verify
#      that the tags we're about to write are plausible for a real track.
#   2. _deepseek_folder_coherence() — called from _cleanup_all_mixed_folders
#      to confirm that files in a folder really belong to the same album/artist.
#
# Both functions degrade gracefully to no-op when the API key is absent or
# when the API call fails (network issues, rate limits, etc.).
# ---------------------------------------------------------------------------

_DS_API_KEY  = getattr(config, "DEEPSEEK_API_KEY",
                        os.environ.get("DEEPSEEK_API_KEY", ""))
_DS_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
_DS_MODEL    = "deepseek-chat"
_DS_TIMEOUT  = 20      # seconds — audit must keep moving; never block on AI
_DS_MAX_TOK  = 256     # tiny response — we only need JSON yes/no + corrections
_DS_CALL_LOCK = threading.Lock()   # rate-limit: one call at a time
_DS_CALL_DELAY = 0.5   # seconds between calls to avoid 429

# Simple in-process LRU cache — avoid re-asking about the same track
import functools
@functools.lru_cache(maxsize=2048)
def _ds_cache_key(artist: str, album: str, title: str) -> str:
    return f"{artist}||{album}||{title}"


def _deepseek_call(prompt: str) -> dict | None:
    """
    Fire a single DeepSeek chat completion and return the parsed JSON dict,
    or None on any failure.  Never raises.
    """
    if not _DS_API_KEY:
        return None
    import urllib.request as _ur
    payload = json.dumps({
        "model":      _DS_MODEL,
        "max_tokens": _DS_MAX_TOK,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }).encode()
    req = _ur.Request(
        _DS_ENDPOINT,
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {_DS_API_KEY}",
        },
        method="POST",
    )
    with _DS_CALL_LOCK:
        time.sleep(_DS_CALL_DELAY)
        try:
            with _ur.urlopen(req, timeout=_DS_TIMEOUT) as resp:
                data    = json.loads(resp.read())
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as _e:
            log.debug(f"  [DeepSeek] call failed: {_e}")
            return None


def _deepseek_verify_metadata(artist: str, album: str, title: str,
                               genre: str, year: str) -> dict:
    """
    Ask DeepSeek whether the proposed metadata for a track looks plausible.

    Returns a dict:
      {
        "ok":             True/False,       # False = something looks wrong
        "artist":         str | None,       # corrected artist if wrong
        "album":          str | None,       # corrected album if wrong
        "title":          str | None,       # corrected title if wrong
        "genre":          str | None,       # corrected genre if wrong
        "year":           str | None,       # corrected year if wrong
        "confidence":     "high"|"medium"|"low",
        "note":           str               # human-readable explanation
      }

    Falls back to {"ok": True} (accept as-is) on API failure.
    """
    # Skip obviously incomplete records — no point asking
    if not artist or not title:
        return {"ok": True}
    # Cache check — avoid repeating the same question
    ck = _ds_cache_key(artist, album, title)
    cached = getattr(_deepseek_verify_metadata, "_cache", {}).get(ck)
    if cached is not None:
        return cached

    prompt = (
        "You are a music metadata expert. Given these tags for a song, "
        "check if they are plausible and correct. Return ONLY a JSON object.\n\n"
        f"Artist: {artist}\nAlbum: {album}\nTitle: {title}\n"
        f"Genre: {genre}\nYear: {year}\n\n"
        "Rules:\n"
        "- If everything looks correct, return {\"ok\": true, \"confidence\": \"high\", \"note\": \"\"}\n"
        "- If something is wrong or suspicious, return {\"ok\": false, \"artist\": corrected_or_null, "
        "\"album\": corrected_or_null, \"title\": corrected_or_null, "
        "\"genre\": corrected_or_null, \"year\": corrected_or_null, "
        "\"confidence\": \"high|medium|low\", \"note\": \"brief reason\"}\n"
        "- Only correct fields you are highly confident about.\n"
        "- null means keep the existing value.\n"
        "- Common issues: genre 'Unknown', wrong year, artist splintered by '/' or feat., "
        "album/artist swapped, track from wrong album.\n"
        "Respond with ONLY the JSON object, no prose."
    )
    result = _deepseek_call(prompt) or {"ok": True}
    # Cache result
    if not hasattr(_deepseek_verify_metadata, "_cache"):
        _deepseek_verify_metadata._cache = {}
    _deepseek_verify_metadata._cache[ck] = result
    return result


def _deepseek_folder_coherence(folder_name: str, filenames: list[str]) -> dict:
    """
    Ask DeepSeek whether the files in a device folder all belong to the same
    album and artist, and whether the folder name matches the content.

    Returns:
      {
        "coherent":      True/False,
        "expected_artist": str,      # what artist the folder should contain
        "expected_album":  str,      # what album it should be
        "outliers":        [str],    # filenames that look like they don't belong
        "rename_folder_to": str | None,  # suggested canonical folder name
        "note":            str
      }

    Falls back to {"coherent": True} on failure.
    """
    if not filenames:
        return {"coherent": True}
    # Only send first 8 filenames to keep prompt short
    sample = filenames[:8]
    prompt = (
        "You are a music library expert. A Sony Walkman folder contains these audio files:\n\n"
        f"Folder: {folder_name}\n"
        "Files:\n" + "\n".join(f"  - {fn}" for fn in sample) + "\n\n"
        "Determine:\n"
        "1. Do all files belong to the same album and album artist?\n"
        "2. Does the folder name match the content?\n"
        "3. Are there any outliers (files from a different album or artist)?\n\n"
        "Return ONLY a JSON object:\n"
        "{\"coherent\": bool, \"expected_artist\": str, \"expected_album\": str, "
        "\"outliers\": [filenames that don't belong], "
        "\"rename_folder_to\": \"Artist - Album\" or null if already correct, "
        "\"note\": \"brief explanation\"}\n"
        "Respond with ONLY the JSON object."
    )
    return _deepseek_call(prompt) or {"coherent": True}

# Tag fields the audit reads from a file, mapped to their mutagen EasyID3 keys
_EASY_FIELDS = {
    "title":        "title",
    "artist":       "artist",
    "album":        "album",
    "album_artist": "albumartist",
    "release_year": "date",
    "track_number": "tracknumber",
    "disc_number":  "discnumber",
    "genre":        "genre",
    "composer":     "composer",
}

# Values considered "empty" — the audit will try to fill these in
_EMPTY_VALUES = {"", "unknown", "unknown genre", "unknown artist",
                 "unknown album", "0", "0/0", "track", "title", "artist",
                 "album", "genre", "untitled"}

# ---------------------------------------------------------------------------
# Compilation / DJ-mix folder detection
# ---------------------------------------------------------------------------
# Folder names like "SoundCloud.app_DJ VIC PRESENTS_ CLUB CHOPS VOL. 1"
# carry the true album identity.  We detect these at tag-build time so that:
#   • album / album_artist come from the folder rather than the broken file tag
#   • per-track artist is resolved via swap detection + AI, not filename position-0
#   • the watermark prefix is stripped before the canonical name is derived
# ---------------------------------------------------------------------------

_DJ_COMPILATION_MARKERS = re.compile(
    r"\b(presents?|vol\.?\s*\d+|volume\s*\d+|mix(?:tape)?|club\s+chops?|"
    r"essentials?|sessions?|compil|anthology|collection|various)\b",
    re.IGNORECASE,
)

# Regex for detecting artist/title swap in club-edit / remix filenames.
# Pattern: "SONG - ARTIST REMIX_TYPE"  (title comes first, artist second)
_SWAP_PATTERN = re.compile(
    r"^(.+?)\s+-\s+(.+?)\s*"
    r"(CLUB\s+EDIT|REMIX|RADIO\s+EDIT|EXTENDED\s+MIX|VIP|REFIX|REWORK|"
    r"BOOTLEG|FLIP|MASHUP|EDIT|VERSION|MIX)\s*.*$",
    re.IGNORECASE,
)

_EDIT_KEYWORDS_RE = re.compile(
    r"\b(club\s+edit|remix|radio\s+edit|extended\s+mix|instrumental|vip|"
    r"refix|rework|bootleg|flip|mashup|edit|version|mix)\b",
    re.IGNORECASE,
)

_WATERMARK_TAG_FRAGMENTS = (
    "soundcloudaud.com", "spotidownloader", "spotifydown",
    "soundcloud.app", "ytmp3", "y2mate", "tubidy",
)


_WATERMARK_FOLDER_PREFIXES = (
    "SpotiDownloader.com - ", "SpotiDownloader.com_", "SoundCloud.app_",
    "spotifydown.com - ", "spotifymate.com - ",
    "ytmp3 - ", "y2mate - ", "tubidy - ", "mp3juice - ", "downloadify - ",
)


def _strip_watermark(folder_name: str) -> str:
    """Remove known download-tool prefix from a folder name."""
    for pfx in _WATERMARK_FOLDER_PREFIXES:
        if folder_name.startswith(pfx):
            return folder_name[len(pfx):]
    return folder_name


def _is_compilation_folder(folder_name: str) -> bool:
    """True when the folder name indicates a DJ mix, compilation, or presents-style release."""
    cleaned = folder_name
    # Strip watermark prefix first
    for pfx in _WATERMARK_FOLDER_PREFIXES:
        if cleaned.startswith(pfx):
            cleaned = cleaned[len(pfx):]
            break
    cleaned = cleaned.replace("_", " ")
    return bool(_DJ_COMPILATION_MARKERS.search(cleaned))


def _extract_compilation_meta(folder_name: str) -> dict:
    """
    For a folder like 'SoundCloud.app_DJ VIC PRESENTS_ CLUB CHOPS VOL. 1'
    return {"album_artist": "DJ Vic", "album": "DJ Vic Presents: Club Chops Vol. 1"}.
    Falls back to {"album_artist": "Various Artists", "album": cleaned_name}.
    """
    # Strip watermark
    cleaned = folder_name
    for pfx in _WATERMARK_FOLDER_PREFIXES:
        if cleaned.startswith(pfx):
            cleaned = cleaned[len(pfx):]
            break
    # Underscores used as spaces by SoundCloud.app
    cleaned = cleaned.replace("_", " ").strip()

    # "DJ NAME PRESENTS: ALBUM NAME"  or  "DJ NAME PRESENTS_ ALBUM NAME"
    m = re.match(
        r"^((?:DJ|MC|DJ'S?)\s+\S+(?:\s+\S+)?)\s+PRESENTS?[_:\s]+(.*)",
        cleaned, re.IGNORECASE,
    )
    if m:
        dj_name = m.group(1).strip().title()
        rest    = m.group(2).strip().title()
        album   = f"{dj_name} Presents: {rest}" if rest else dj_name
        return {"album_artist": dj_name, "album": album}

    if cleaned:
        return {"album_artist": "Various Artists", "album": cleaned.title()}
    return {}


def _detect_artist_title_swap(
    filename_stem: str,
    current_artist: str,
    current_title: str,
    folder_name: str = "",
) -> tuple[str, str]:
    """
    Detect 'SONG - ARTIST CLUB EDIT' (artist and title swapped in filename).
    Returns (corrected_artist, corrected_title), or ("", "") if no swap.

    Heuristics — all must be satisfied:
      1. File is in a compilation/club-edit folder OR filename has an edit keyword
      2. Filename matches the swap pattern: SONG - ARTIST EDIT_TYPE
      3. current_artist is blank or watermarked (i.e. currently wrong)
    """
    _watermark_in_artist = any(
        w in (current_artist or "").lower() for w in _WATERMARK_TAG_FRAGMENTS
    )
    _artist_is_blank = not current_artist or current_artist.lower() in _EMPTY_VALUES

    _in_comp_folder = _is_compilation_folder(folder_name)

    # Only fire when we have reason to suspect the tags are wrong
    if not (_watermark_in_artist or _artist_is_blank or _in_comp_folder):
        return "", ""
    # And the filename must contain an edit/remix keyword
    if not _EDIT_KEYWORDS_RE.search(filename_stem) and not _in_comp_folder:
        return "", ""

    m = _SWAP_PATTERN.match(filename_stem)
    if not m:
        return "", ""

    _parsed_song   = m.group(1).strip()
    _parsed_artist = m.group(2).strip()
    _edit_type     = m.group(3).strip()

    # Sanity: don't swap if song part looks like an artist (single short token)
    if len(_parsed_song.split()) == 0:
        return "", ""

    # Build title with edit suffix
    _song_title = _parsed_song.title()
    _edit_str   = _edit_type.title()
    if _edit_str and _edit_str.lower() not in _song_title.lower():
        _song_title = f"{_song_title} ({_edit_str})"

    _artist_clean = _parsed_artist.title()
    return _artist_clean, _song_title


# Subgenre specificity: maps a broad genre to more specific alternatives.
# When a file already has a broad genre and a lookup returns a specific subgenre,
# the subgenre wins. Also used to prefer specificity when multiple sources disagree.
# Format: broad_genre_lower -> {specific_subgenres_that_beat_it}
_GENRE_SPECIFICITY = {
    "hip hop":          {"trap", "uk grime", "grime", "drill", "uk drill", "afro trap",
                         "conscious hip hop", "alternative hip hop", "east coast hip hop",
                         "west coast hip hop", "southern hip hop", "gangsta rap",
                         "mumble rap", "cloud rap", "lo-fi hip hop", "jazz rap",
                         "boom bap", "crunk", "snap", "phonk"},
    "hip-hop":          {"trap", "uk grime", "grime", "drill", "uk drill", "afro trap",
                         "conscious hip hop", "alternative hip hop"},
    "rap":              {"trap", "grime", "drill", "gangsta rap", "boom bap"},
    "r&b":              {"neo soul", "contemporary r&b", "quiet storm", "new jack swing",
                         "alternative r&b"},
    "rnb":              {"neo soul", "contemporary r&b", "alternative r&b"},
    "pop":              {"indie pop", "art pop", "electropop", "synthpop", "bedroom pop",
                         "k-pop", "j-pop", "afropop"},
    "electronic":       {"house", "techno", "drum and bass", "dubstep", "ambient",
                         "trance", "jungle", "garage", "uk garage", "grime"},
    # MK8 Fix (PR-GenreGraph): Afropop is now a distinct tag (pan-African pop umbrella).
    # All of these subgenres are MORE SPECIFIC than Afropop, so when album consolidation
    # sees "Afropop" on some tracks and "Amapiano" on others, Amapiano wins.
    "afropop":          {"afrobeats", "amapiano", "bongo flava", "gqom", "alte",
                         "kwaito", "afro house", "gengetone", "genge", "singeli",
                         "afroswing", "afro trap", "soukous"},
    "african music":    {"afrobeats", "afropop", "afro-fusion", "highlife", "juju",
                         "fuji", "makossa", "amapiano", "bongo flava", "gengetone",
                         "alte", "gqom", "kwaito"},
    "world":            {"afrobeats", "afropop", "reggae", "dancehall", "latin",
                         "bossa nova", "samba", "highlife"},
    "rock":             {"indie rock", "alternative rock", "punk rock", "hard rock",
                         "metal", "prog rock", "post-rock", "grunge"},
    "jazz":             {"bebop", "cool jazz", "modal jazz", "free jazz", "fusion",
                         "smooth jazz", "nu jazz"},
    "soul":             {"neo soul", "funk", "gospel", "motown"},
}

def _is_more_specific(new_genre: str, current_genre: str) -> bool:
    """
    Returns True if new_genre is more specific than current_genre.
    e.g. "Afrobeats" beats "African Music", "Trap" beats "Hip Hop".
    """
    if not new_genre or not current_genre:
        return bool(new_genre)
    new_lc     = new_genre.lower().strip()
    current_lc = current_genre.lower().strip()
    if new_lc == current_lc:
        return False
    # Explicit subgenre mapping takes highest priority
    for broad, subgenres in _GENRE_SPECIFICITY.items():
        if current_lc in (broad, broad.replace("-", " ")):
            if new_lc in subgenres:
                return True
    # If current is a known broad/umbrella genre and new is not, always upgrade
    _BROAD = {
        "african music", "world music", "world", "pop", "hip hop", "hip-hop",
        "rap", "r&b", "rnb", "rock", "electronic", "latin", "dance", "reggae",
        "soul", "jazz", "classical", "country", "alternative", "indie", "metal",
        "folk", "blues", "funk", "punk", "films/games", "other", "unknown",
    }
    if current_lc in _BROAD and new_lc not in _BROAD:
        return True
    # Length heuristic as last resort
    return len(new_lc) > len(current_lc) + 3

def _best_genre(candidates: list[str], artist: str = "", album: str = "") -> str:
    """
    From a list of genre candidates, return the most specific non-empty one.
    Dynamically excludes any tag that matches the artist or album name —
    Last.fm crowdsourced tags frequently include artist names (e.g. "21 Savage")
    and album names (e.g. "Savage Mode II") which are NOT genres.
    """
    import re as _re_bg
    exclusions: set[str] = set()
    for val in [artist, album]:
        if not val:
            continue
        val_lc = val.lower().strip()
        exclusions.add(val_lc)
        # Split compound names: "21 Savage & Metro Boomin" → {"21 savage", "metro boomin"}
        for part in _re_bg.split(
            r"[,&]| x |\b(?:and|feat\.?|ft\.?)\b", val_lc
        ):
            p = part.strip()
            if len(p) > 2:
                exclusions.add(p)

    seen: set[str] = set()
    unique: list[str] = []
    for g in candidates:
        g = (g or "").strip()
        gl = g.lower()
        if g and gl not in seen and gl not in _EMPTY_VALUES and gl not in exclusions:
            seen.add(gl)
            unique.append(g)
    if not unique:
        return ""
    best = unique[0]
    for g in unique[1:]:
        if _is_more_specific(g, best):
            best = g
    return best


# ---------------------------------------------------------------------------
# Audit state helpers  (per-file last-audit timestamps)
# ---------------------------------------------------------------------------

def _load_audit_state() -> dict:
    """Delegates to meta_key so all modules share one in-process state cache."""
    try:
        import meta_key as _mk_io
        return _mk_io.load_state()  # returns the shared _state_cache dict
    except ImportError:
        pass
    if config.AUDIT_STATE_FILE.exists():
        try:
            return json.loads(config.AUDIT_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _compute_metakey(
    filename_stem: str,
    genre_hit: dict,
    album_meta: dict,
) -> str:
    """
    Compute a short hash of the tag values the audit WOULD write for this file.
    If this hash matches the stored metakey, the file is already up to date —
    no MTP pull needed.

    Inputs are all local (no device read required):
      filename_stem  — e.g. "Janet Jackson - All For You - 01-04 2wayforyou (Interlude)"
      genre_hit      — from _genre_cache_lookup()
      album_meta     — from _album_meta dict (pre-populated from Spotify cache)
    """
    import hashlib, re as _re

    # Parse filename to get the same values _expected_tags would produce
    parts = [p.strip() for p in filename_stem.split(" - ")]
    def _strip_num(s):
        return _re.sub(r"^\d{1,3}[\s\.\-]+", "", s).strip() or s

    if len(parts) >= 3:
        artist        = parts[0]
        album         = parts[1]
        title         = _strip_num(parts[-1])
        # "01-06 Title" → disc=1, track=6   |   "06 Title" → disc=1, track=6
        # Fix: differentiate by extracting disc prefix when present so that
        # "01-06 Save Me Pt 2" gets disc=1,track=6 but is DISTINCT from
        # "06 Save Me Pt 2" (disc=1,track=6) only if we also encode the raw prefix.
        # Strategy: include the full "DD-TT" or "TT" token literally in the key
        # so files with different prefix formats produce different hashes.
        _tn_raw_m = _re.match(r"^(\d{1,2}-\d{1,2}|\d{1,2})\s", parts[-1])
        _tn_raw   = _tn_raw_m.group(1) if _tn_raw_m else ""
        if "-" in _tn_raw:
            _disc_raw, _track_raw = _tn_raw.split("-", 1)
            disc_number  = str(int(_disc_raw))
            track_number = str(int(_track_raw))
        elif _tn_raw:
            disc_number  = "1"
            track_number = str(int(_tn_raw))
        else:
            disc_number  = "1"
            track_number = ""
    elif len(parts) == 2:
        artist        = _strip_num(parts[0])
        title         = _strip_num(parts[1])
        album         = ""
        track_number  = ""
        disc_number   = "1"
    else:
        artist        = ""
        title         = _strip_num(filename_stem)
        album         = ""
        track_number  = ""
        disc_number   = "1"

    genre = (genre_hit.get("genre") or "").strip()
    year  = (genre_hit.get("year")  or album_meta.get("year") or "").strip()

    # album_artist: same as artist for canonical filenames (single-artist albums)
    # For compilations or multi-artist albums, album_meta has the album_artist.
    album_artist = (album_meta.get("album_artist") or artist).strip()

    key_str = "|".join([artist, album_artist, album, title, genre, year, track_number, disc_number])
    return hashlib.md5(key_str.encode("utf-8")).hexdigest()[:12]


def _save_audit_state(state: dict):
    """Delegates flush to meta_key so all modules write to the same file atomically."""
    try:
        import meta_key as _mk_io
        # If state IS meta_key's cache, just flush it
        _mk_io._state_cache = state   # ensure cache is in sync
        _mk_io._flush()
        return
    except ImportError:
        pass
    config.AUDIT_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_stale(state: dict, file_key: str, expected_metakey: str | None = None) -> bool:
    """
    Returns True if the file needs re-auditing.

    Fast path: if expected_metakey is provided and matches the stored metakey,
    the file is up to date — skip the MTP pull entirely regardless of age.

    Slow path: falls back to timestamp comparison (AUDIT_STALE_DAYS).
    """
    if config.AUDIT_STALE_DAYS == 0:
        return True
    entry = state.get(file_key)
    if not entry:
        return True

    # New-format state: {"ts": "...", "mk": "abc123"}
    # Old-format state: "2026-03-25T..."  (plain timestamp string)
    if isinstance(entry, dict):
        # Fast metakey check — no MTP pull if tags are already correct
        if expected_metakey and entry.get("mk") == expected_metakey:
            # Also stale if cover art has never been checked (cv flag absent/False)
            # This is a one-time pass: once cv=True is stamped, this path is skipped.
            if not entry.get("cv", False):
                return True
            return False
        # Fall through to timestamp check
        ts = entry.get("ts", "")
    else:
        ts = entry   # legacy plain timestamp

    try:
        last_dt = datetime.datetime.fromisoformat(ts)
        age     = (datetime.datetime.now() - last_dt).days
        return age >= config.AUDIT_STALE_DAYS
    except Exception:
        return True


def _make_state_entry(metakey: str, has_cover: bool = False) -> dict:
    """Create a new state entry with timestamp, metakey, and cover art flag."""
    return {"ts": datetime.datetime.now().isoformat(), "mk": metakey, "cv": has_cover}


# ---------------------------------------------------------------------------
# Tag reading helpers
# ---------------------------------------------------------------------------

def _is_valid_audio(path: pathlib.Path) -> bool:
    """
    Quick validity check: can mutagen open the file without error?
    Returns False for truncated/corrupt downloads so we skip them entirely
    rather than burning API calls and producing a confusing write error.
    """
    try:
        if path.suffix.lower() == ".flac":
            FLAC(path)
        else:
            MP3(path)
        return True
    except Exception:
        return False


def _read_tags(path: pathlib.Path) -> dict:
    """
    Reads current tag values from a local file (temp copy from device).
    Returns dict with keys matching AUDIT_FIELDS.
    """
    try:
        if path.suffix.lower() == ".flac":
            audio = FLAC(path)
            return {
                field: audio.get(easy_key, [""])[0]
                for field, easy_key in _EASY_FIELDS.items()
            }
        else:
            try:
                audio = MP3(path, ID3=EasyID3)
            except ID3NoHeaderError:
                return {f: "" for f in _EASY_FIELDS}
            return {
                field: audio.get(easy_key, [""])[0]
                for field, easy_key in _EASY_FIELDS.items()
            }
    except Exception as e:
        log.debug(f"Tag read error for {path.name}: {e}")
        return {f: "" for f in _EASY_FIELDS}


def _id3_version(path: pathlib.Path) -> int:
    """Returns the major ID3 version of an MP3 file (2 or 3 or 4), or 0 on error."""
    try:
        id3 = ID3(path)
        return id3.version[0]
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Rating reading helper (for preserving rating during tag writes and upgrade)
# ---------------------------------------------------------------------------

def _read_rating(path: pathlib.Path) -> int | None:
    """
    Read rating from MP3 (POPM) or FLAC (rating tag). Returns 0-100 or None.
    """
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            id3 = ID3(path)
            popms = id3.getall("POPM")
            for popm in popms:
                if popm.email == "Spotify Liked":
                    return popm.rating
                # fallback: any POPM with rating >0
                if popm.rating > 0:
                    return popm.rating
        elif ext == ".flac":
            audio = FLAC(path)
            rating = audio.get("rating")
            if rating:
                try:
                    return int(rating[0])
                except:
                    pass
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Cover art helpers
# ---------------------------------------------------------------------------

def _has_cover_art(path: pathlib.Path, is_mp3: bool) -> bool:
    """Return True if the file already has embedded cover art."""
    try:
        if is_mp3:
            id3 = ID3(path)
            return bool(id3.getall("APIC"))
        else:
            audio = FLAC(path)
            return bool(audio.pictures)
    except Exception:
        return False


def _fetch_cover_art(url: str) -> bytes | None:
    """Download cover art from *url*. Returns raw bytes or None on failure."""
    if not url:
        return None
    try:
        import urllib.request as _ur
        req = _ur.Request(url, headers={"User-Agent": "WalkmanSync/1.0"})
        with _ur.urlopen(req, timeout=10) as resp:
            return resp.read()
    except Exception as e:
        log.debug(f"  [CoverArt] Fetch failed ({url!r}): {e}")
        return None


def _embed_cover_art(path: pathlib.Path, img_bytes: bytes, is_mp3: bool) -> bool:
    """
    Embed *img_bytes* as cover art into *path*.
    Routes through cover_art.py for baseline-JPEG normalisation (≤500px,
    ≤90 KB, non-progressive) and id3_helpers for v2.3-safe ID3 saves.
    Returns True on success.
    """
    if not img_bytes:
        return False
    try:
        import cover_art as _ca
        from id3_helpers import save_v23_id3
        if is_mp3:
            id3 = ID3(path)
            id3.delall("APIC")
            id3.add(_ca.build_apic(img_bytes))
            save_v23_id3(id3)
        else:
            audio = FLAC(path)
            audio.clear_pictures()
            audio.add_picture(_ca.build_flac_picture(img_bytes))
            audio.save()
        return True
    except Exception as e:
        log.debug(f"  [CoverArt] Embed failed ({path.name}): {e}")
        return False


def _reprocess_existing_art(path: pathlib.Path, is_mp3: bool) -> bool:
    """Re-embed existing art through the baseline-JPEG normaliser (progressive→baseline, PNG→JPEG, resize ≤500px/90KB)."""
    try:
        if is_mp3:
            frames = ID3(path).getall("APIC")
            if not frames:
                return False
            img_bytes = frames[0].data
        else:
            audio = FLAC(path)
            if not audio.pictures:
                return False
            img_bytes = audio.pictures[0].data
        return _embed_cover_art(path, img_bytes, is_mp3)
    except Exception as e:
        log.debug(f"  [HiResCompat] Art reprocess failed ({path.name}): {e}")
        return False


# ---------------------------------------------------------------------------
# Task 11 — AcoustID/MusicBrainz fingerprint fallback (opt-in)
# ---------------------------------------------------------------------------
# Wires up mk39_phase_III_fallback.attempt_fingerprint_fallback (fpcalc ->
# AcoustID -> MusicBrainz, registered in the MK38.0 write-barrier's trust
# ranking) which previously had zero production call sites. Fires only when
# `expected` is still blank (artist or title) after the full genre/filename/
# folder waterfall already run by _expected_tags — matching
# attempt_fingerprint_fallback's own documented contract. Only fills fields
# that are still blank; never overwrites an already-resolved value.

def _apply_acoustid_fallback(expected: dict, audio_path: "pathlib.Path") -> bool:
    """Mutates *expected* in place with any blank fields the fingerprint
    fallback can resolve. Returns True if anything was filled in. Never
    raises — a fingerprint-fallback failure must never break the primary
    tag-write loop."""
    try:
        import mk39_phase_III_fallback as _p3fb
        if not _p3fb.is_phase_I_resolution_blank(expected):
            return False
        identity = _p3fb.attempt_fingerprint_fallback(audio_path, expected)
        if identity is None:
            return False
        changed = False
        if not (expected.get("artist") or "").strip() and identity.artist:
            expected["artist"] = identity.artist
            changed = True
        if not (expected.get("title") or "").strip() and identity.title:
            expected["title"] = identity.title
            changed = True
        if not (expected.get("album") or "").strip() and identity.album:
            expected["album"] = identity.album
            changed = True
        if not (expected.get("release_year") or "").strip() and identity.year:
            expected["release_year"] = identity.year
            changed = True
        return changed
    except Exception as e:
        log.debug(f"  [AcoustID] Fingerprint fallback step failed for {audio_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Expected-value builder
# ---------------------------------------------------------------------------

def _expected_tags(
    current: dict,
    genre_hit: dict,
    filename_stem: str,
    cached_album_meta: dict | None = None,
    folder_parts: list[str] | None = None,
    folder_name: str = "",          # NEW: album folder name from device path parts
) -> dict:
    """
    Builds the expected tag values for a file given:
      current    — what's already in the file
      genre_hit  — {"genre": str, "year": str} from the genre cache (may be empty)
      filename_stem — used as title fallback when title is blank
      cached_album_meta — optional album-level metadata dict
      folder_parts — device path parts, used by AI fallback for track/disc inference
      folder_name — the immediate album folder name (last element of folder_parts)

    Logic per field:
      title       : keep current if non-blank, else use filename stem
      artist      : keep current; for compilations resolved via swap detection + AI
      album       : keep current; for watermarked/compilation folders inferred from folder_name
      album_artist: if blank, copy from artist; for compilations inferred from folder_name
      release_year: if blank/Unknown, use genre cache year
      genre       : if blank/Unknown, use genre cache genre
      track_number: if blank/0, infer from filename; AI fallback if still missing
      disc_number : if blank, default "1"

    Returns dict of expected values.  Any field the audit cannot improve
    is left equal to the current value so no change is flagged.
    """
    exp = dict(current)

    # ── Step 0: Compilation / DJ-mix folder context injection ────────────────
    # When a file lives in a folder like "SoundCloud.app_DJ VIC PRESENTS_ CLUB CHOPS VOL. 1"
    # the folder name is authoritative for album and album_artist.  The per-track artist
    # comes later from swap detection (if the filename encodes "SONG - ARTIST EDIT") or AI.
    _folder_name = folder_name or (folder_parts[-1] if folder_parts else "")
    _folder_is_compilation = _is_compilation_folder(_folder_name)
    if _folder_is_compilation:
        _comp_meta = _extract_compilation_meta(_folder_name)
        if _comp_meta.get("album"):
            _cur_album = (exp.get("album") or "").strip()
            if not _cur_album or _cur_album.lower() in _EMPTY_VALUES:
                exp["album"] = _comp_meta["album"]
        if _comp_meta.get("album_artist"):
            _cur_aa = (exp.get("album_artist") or "").strip()
            _aa_is_watermarked = any(
                w in _cur_aa.lower() for w in _WATERMARK_TAG_FRAGMENTS
            )
            if not _cur_aa or _cur_aa.lower() in _EMPTY_VALUES or _aa_is_watermarked:
                exp["album_artist"] = _comp_meta["album_artist"]

    current_title = (exp.get("title") or "").strip()
    # Detect titles that are actually full filename stems written as tags:
    #   "21 Savage - SAVAGE MODE II - 01-09 Snitches & Rats (feat"  <- truncated filename
    #   "Janet Jackson - All For You - 01-20 Son Of A Gun..."        <- full filename
    # Replace these with the properly parsed title from the filename stem.
    _title_looks_like_filename = bool(
        current_title.count(" - ") >= 2 or           # has Artist - Album - Title structure
        len(current_title) > 80 or                    # suspiciously long title
        current_title.endswith("(feat") or            # truncated mid-word
        current_title.endswith("(feat.") or
        current_title.endswith(" -") or
        (filename_stem and current_title.lower().startswith(
            filename_stem[:15].lower().replace("_", " ")  # starts with filename
        ))
    )
    if not current_title or current_title.lower() in _EMPTY_VALUES or _title_looks_like_filename:
        # Parse title from canonical filename stem "Artist - Album - NN Title"
        # Use only the last segment after " - " splitting, strip leading track number
        import re as _re
        raw = filename_stem.replace("_", " ").strip()
        parts = [p.strip() for p in raw.split(" - ")]
        # Known bare suffix words that are NOT a complete title by themselves.
        # If the last part is one of these, keep the full stem (or last two parts joined)
        # so "Nobles - Instrumental" stays "Nobles - Instrumental", not "Instrumental".
        _BARE_SUFFIXES = {
            "instrumental", "skit", "intro", "outro", "interlude",
            "bonus track", "bonus", "reprise", "remix", "version",
            "live", "acoustic", "a cappella", "acapella", "freestyle",
        }
        if len(parts) >= 3:
            # "Artist - Album - NN Title" -> strip track number from last part
            title_part = _re.sub(r"^\d{1,3}[\s\.\-]+", "", parts[-1]).strip()
            if title_part.lower() in _BARE_SUFFIXES:
                # e.g. "Artist - Album - 01 Instrumental" — keep last two parts
                prev = _re.sub(r"^\d{1,3}[\s\.\-]+", "", parts[-2]).strip()
                title_part = f"{prev} - {title_part}" if prev else title_part
            exp["title"] = title_part if title_part else parts[-1]
        elif len(parts) == 2:
            # "Artist - Title" format. Do NOT strip leading digits from the title part.
            # "4 Your Eyez Only" should not become "Your Eyez Only".
            # Only strip if the number is followed immediately by a dot or dash
            # (e.g. "01. Title" or "01 - Title") which clearly marks a track number.
            import re as _re2b
            title_part = parts[-1]
            # Only strip "01. " or "01 - " style prefixes (NOT "4 Title" or "16 Days")
            _track_stripped = _re2b.sub(r"^\d{1,2}[\.\-]\s*", "", title_part).strip()
            if _track_stripped != title_part and _track_stripped:
                title_part = _track_stripped
            if title_part.lower() in _BARE_SUFFIXES:
                exp["title"] = raw  # keep full stem
            else:
                exp["title"] = title_part if title_part else parts[-1]
        else:
            # No " - " separators — bare filename. Do NOT strip leading digits.
            # "16 Days No Sleep.mp3" → title = "16 Days No Sleep", not "Days No Sleep".
            # A bare filename has no album context to confirm a number is a track number.
            exp["title"] = raw

    # ── Artist/title swap detection ──────────────────────────────────────────
    # Catches filenames like "WHAT A DAY - TYLER THE CREATOR CLUB EDIT"
    # where the song name comes first and the artist name second (reversed from
    # canonical "Artist - Album - NN Title").  Only fires when:
    #   (a) the file is in a DJ compilation folder, OR
    #   (b) the current artist tag is watermarked / blank.
    _swap_artist, _swap_title = _detect_artist_title_swap(
        filename_stem,
        current_artist = (exp.get("artist") or "").strip(),
        current_title  = (exp.get("title")  or "").strip(),
        folder_name    = _folder_name,
    )
    if _swap_artist:
        exp["artist"] = _swap_artist
        exp["title"]  = _swap_title
        # In a compilation folder album_artist stays as the DJ/compiler,
        # not overwritten with the individual track artist.
        if not _folder_is_compilation:
            exp["album_artist"] = _swap_artist
        log.debug(
            f"  [Swap] {filename_stem!r}: "
            f"artist={_swap_artist!r} title={_swap_title!r}"
        )

    # album_artist: if blank, inherit primary artist only (never slash-joined list)
    # A slash-joined "Don Toliver/Travis Scott" in the artist tag would splinter
    # the ZX500 library — every slash-separated name becomes its own artist entry.
    if not exp.get("album_artist") or exp["album_artist"].lower() in _EMPTY_VALUES:
        _src_artist = (exp.get("artist") or "").strip()
        if _src_artist and _src_artist.lower() not in _EMPTY_VALUES:
            # Extract primary: take everything before the first / or ,
            # but keep the full name if it doesn't look like a multi-artist join
            import re as _re_aa2
            _aa_primary = _re_aa2.split(r"[/,;]", _src_artist)[0].strip()
            exp["album_artist"] = _aa_primary if _aa_primary else _src_artist

    # Also clean up album_artist if it already has slashes (SpotiDownloader residue)
    _raw_aa = (exp.get("album_artist") or "").strip()
    if "/" in _raw_aa or ";" in _raw_aa:
        import re as _re_aa3
        _aa_clean = _re_aa3.split(r"[/;]", _raw_aa)[0].strip()
        if _aa_clean and not _folder_is_compilation:
            exp["album_artist"] = _aa_clean

    # Known artist aliases — same person, different stage names.
    # If a slash-joined artist string contains ONLY aliases, treat as single artist
    # and use the primary (first) name without adding "(feat. X)" to the title.
    _ARTIST_ALIASES: dict[str, str] = {
        # key: normalised alias → value: canonical name to use
        "mf doom":       "MF DOOM",
        "doom":          "MF DOOM",
        "viktor vaughn": "MF DOOM",   # Viktor Vaughn = MF DOOM alter ego
        "metal fingers":  "MF DOOM",
        "king geedorah": "MF DOOM",
        "zev love x":    "MF DOOM",
        "daniel dumile":  "MF DOOM",
    }

    def _norm_aa(s): return s.lower().strip()

    def _resolve_aliases(parts: list[str]) -> tuple[str, list[str]]:
        """
        If all parts are aliases of the same canonical artist, return
        (canonical_name, []) — no feat. addition.
        Otherwise return (primary, feat_list) as normal.
        """
        canonical_set = set()
        for p in parts:
            n = _norm_aa(p)
            c = _ARTIST_ALIASES.get(n)
            if c:
                canonical_set.add(c)
            else:
                canonical_set.add(p)  # unknown — treat as distinct artist
        if len(canonical_set) == 1:
            return (list(canonical_set)[0], [])
        return (parts[0], parts[1:])

    # artist: clean slash-joined multi-artist values left by SpotiDownloader.
    # SpotiDownloader ALWAYS uses "/" as the multi-artist separator — never comma.
    # "Don Toliver/Travis Scott" → artist="Don Toliver", feat in title.
    # We ONLY split on "/" and explicit "feat."/"ft." markers.
    # We do NOT split on "," or "&" — those are often part of the artist name:
    #   "Tyler, the Creator", "Earth, Wind & Fire", "Ben & Ben".
    import re as _re_aa
    _current_artist = (exp.get("artist") or "").strip()
    _feat_sep = r"/|\s+(?:feat\.?|ft\.?)\s+"  # ONLY slash + explicit feat markers
    if _current_artist and ("/" in _current_artist or
            _re_aa.search(r"\s+(?:feat\.?|ft\.?)\s+", _current_artist, _re_aa.I)):
        _aa_parts = [p.strip() for p in _re_aa.split(_feat_sep, _current_artist, flags=_re_aa.I) if p.strip()]
        _primary_aa, _feat_others = _resolve_aliases(_aa_parts) if _aa_parts else (_current_artist, [])
        exp["artist"] = _primary_aa
        # album_artist must also be updated to match primary — do it here so
        # it's always in sync with exp["artist"] regardless of prior state.
        exp["album_artist"] = _primary_aa
        # Inject feat. into title if not already present
        if _feat_others and getattr(config, "FEATURE_ARTISTS_IN_TITLE", True):
            _cur_title = (exp.get("title") or "").strip()
            if _cur_title and not _re_aa.search(r"\bfeat\.?|\bft\.?", _cur_title, _re_aa.I):
                exp["title"] = f"{_cur_title} (feat. {', '.join(_feat_others)})"

    # artist: if blank, try album_meta first (most reliable for SpotiDownloader files
    # where folder name is album-only and no artist is in the filename)
    if not exp.get("artist") or exp["artist"].lower() in _EMPTY_VALUES:
        _aa_from_cache = ((cached_album_meta or {}).get("album_artist") or "").strip()
        if _aa_from_cache and _aa_from_cache.lower() not in _EMPTY_VALUES:
            exp["artist"]       = _aa_from_cache
            exp["album_artist"] = _aa_from_cache

    # artist: if still blank, try to infer from filename stem ("Artist - Album - Title")
    if not exp.get("artist") or exp["artist"].lower() in _EMPTY_VALUES:
        import re as _re3
        raw2 = filename_stem.replace("_", " ").strip()
        fp   = [p.strip() for p in raw2.split(" - ")]
        if len(fp) >= 2 and fp[0] and fp[0].lower() not in _EMPTY_VALUES:
            # Filename may have joined artists with underscore: "Don Toliver_Travis Scott"
            # Split on underscore only when it looks like multi-artist (contains space around it)
            _fp_artist = fp[0]
            _fp_parts  = [p.strip() for p in _fp_artist.split("_") if p.strip()]
            if len(_fp_parts) > 1 and all(len(p) > 1 for p in _fp_parts):
                # Looks like multi-artist ("Don Toliver_Travis Scott")
                # Use only first part as artist; rest become feat.
                exp["artist"] = _fp_parts[0]
                if getattr(config, "FEATURE_ARTISTS_IN_TITLE", True):
                    _cur_t = (exp.get("title") or "").strip()
                    import re as _re3b
                    if _cur_t and not _re3b.search(r"\bfeat\.?\b", _cur_t, _re3b.I):
                        _feat_str = ", ".join(_fp_parts[1:])
                        exp["title"] = f"{_cur_t} (feat. {_feat_str})"
            else:
                exp["artist"] = _fp_artist
            exp["album_artist"] = exp["artist"]

    # album: if blank, try to infer from filename stem
    if not exp.get("album") or exp["album"].lower() in _EMPTY_VALUES:
        import re as _re4
        raw3 = filename_stem.replace("_", " ").strip()
        fp2  = [p.strip() for p in raw3.split(" - ")]
        if len(fp2) >= 3 and fp2[1]:
            exp["album"] = fp2[1]

    # genre: use most specific genre from all available sources
    cache_genre   = (genre_hit.get("genre") or "").strip()
    current_genre = (current.get("genre")   or "").strip()

    # Step 0: check artist-level manual override from genre_client.
    # These are authoritative — e.g. Asake → Afrobeats, Diamond Platnumz → Bongo Flava.
    # Applied even when a genre is already present, UNLESS the current genre is already
    # more specific than the override (e.g. "Amapiano" is not overwritten by "Afrobeats").
    #
    # MK39 Phase I-E (Council emergency directive 2026-05-10): the previous call
    # passed `album=...` as a kwarg, but `_artist_override(artist, title)` accepts
    # only two positional args. The TypeError was silently swallowed by the broad
    # `except Exception: pass` below, making EVERY override lookup a no-op. The
    # `safe_artist_override` wrapper drops extra kwargs and restores the override
    # path. This is the actual root cause behind The Weeknd → Afrobeats and
    # similar Council-named failures.
    try:
        import mk39_phase_I_containment as _p1
        # Use only the primary artist (before any ";" separator) so that
        # featured artists like Tems or Burna Boy in "J. Cole; Tems" do not
        # bleed their genre override into a J. Cole album.
        _raw_artist_for_override = (current.get("artist") or exp.get("artist") or "").strip()
        _artist_for_override = _raw_artist_for_override.split(";")[0].strip()
        # Also use album_artist as the authority if available
        _aa_for_override = (current.get("album_artist") or exp.get("album_artist") or "").strip()
        if _aa_for_override:
            _artist_for_override = _aa_for_override.split(";")[0].strip()
        _title_for_override  = (current.get("title")  or exp.get("title")  or "").strip()
        # Phase I-E: safe_artist_override drops extra kwargs (album, year, ...)
        _manual_override = _p1.safe_artist_override(
            _artist_for_override, _title_for_override,
            album=(current.get("album") or exp.get("album") or "").strip(),
        )
        # Phase I-B: block Abstract Hip-Hop as silent default for non-allow-list artists
        _manual_override = _p1.block_abstract_hip_hop_default(
            _manual_override, _artist_for_override) or ""
        if _manual_override:
            # Artist-level overrides are authoritative curated data.
            # Apply unconditionally: they beat both the current ID3 tag
            # and the genre-client cache (the cache_genre reassignment
            # below prevents the subsequent cache block from regressing it).
            exp["genre"] = _manual_override
            cache_genre  = _manual_override
        else:
            # Phase I-A: folder-name keyword fallback. When no artist override
            # applies, check whether the album folder name explicitly contains
            # a genre keyword (e.g., "3 Step Afro House" → "Afro House",
            # "Japanese City Pop" → "City Pop"). Council 2026-05-10 §3-A:
            # this is the high-impact "fixes 80%" lever for folder-organized
            # personal libraries.
            _folder_genre = _p1.match_folder_genre(_folder_name)
            if _folder_genre:
                _folder_genre = _p1.block_abstract_hip_hop_default(
                    _folder_genre, _artist_for_override) or ""
                if _folder_genre:
                    _cur_genre = (exp.get("genre") or "").strip().lower()
                    if _cur_genre in _EMPTY_VALUES or _cur_genre == "unknown":
                        exp["genre"] = _folder_genre
                        cache_genre  = _folder_genre
    except Exception:
        pass

    if cache_genre and cache_genre.lower() not in ("unknown", ""):
        current_lc = (exp.get("genre") or current_genre).lower()
        if current_lc in _EMPTY_VALUES or current_lc == "unknown":
            # Blank slot — just fill it
            exp["genre"] = cache_genre
        elif _is_more_specific(cache_genre, exp.get("genre") or current_genre):
            # Cache has a more specific subgenre — upgrade
            exp["genre"] = cache_genre

    # release_year: fill blank OR wrong year with cache year
    # "Wrong" means a placeholder like "0", "1900", "1970" (Unix epoch zero),
    # or any year before 1950 (no commercial recordings on digital devices).
    _EPOCH_SENTINEL_YEARS = {"1970", "1969", "1900", "0000", "0"}
    cache_year = (genre_hit.get("year") or "").strip()
    if cache_year and re.match(r"^(19|20)\d{2}$", cache_year):
        current_year = (current.get("release_year") or "").strip()
        _year_bad = (
            not current_year
            or current_year.lower() in _EMPTY_VALUES
            or current_year in _EPOCH_SENTINEL_YEARS
            or not re.match(r"^(19|20)\d{2}$", current_year)
            or int(current_year) < 1950   # no pre-1950 recordings in a modern library
        )
        if _year_bad:
            exp["release_year"] = cache_year

    # disc_number: default to "1" if blank
    if not exp.get("disc_number") or exp["disc_number"].lower() in _EMPTY_VALUES:
        exp["disc_number"] = "1"

    # track_number + disc_number: infer from filename stem if blank/0
    # Canonical filename: "Artist - Album - NN Title" or "Artist - Album - DD-TT Title"
    # "01-06 Save Me Pt 2" → disc_number=1, track_number=6
    # "06 Save Me Pt 2"    → disc_number=1, track_number=6  (DIFFERENT from 01-06 in metakey)
    import re as _re5
    # Strip "N/total" format (e.g. "9/14" → "9") before emptiness check.
    # The ZX500 prefers bare track numbers; the /total part is noise.
    _raw_tn = (exp.get("track_number") or "").strip()
    if "/" in _raw_tn:
        _bare = _raw_tn.split("/")[0].strip()
        if _bare.isdigit():
            exp["track_number"] = str(int(_bare))   # "09/14" → "9", zero-padded later
            _raw_tn = exp["track_number"]
    current_tn = _raw_tn
    if not current_tn or current_tn in _EMPTY_VALUES or current_tn == "0":
        raw_tn = filename_stem.replace("_", " ").strip()
        fp_tn  = [p.strip() for p in raw_tn.split(" - ")]
        if len(fp_tn) >= 3:
            # Match "DD-TT Title" (disc-track) or "TT Title" (track only)
            m = _re5.match(r"^(\d{1,2})-(\d{1,2})\s", fp_tn[-1])
            if m:
                # DD-TT prefix: set both disc and track from filename
                _fn_disc  = str(int(m.group(1)))
                _fn_track = str(int(m.group(2)))
                exp["track_number"] = _fn_track
                # Only overwrite disc_number if it hasn't been set by tag already
                current_dn = (exp.get("disc_number") or "").strip()
                if not current_dn or current_dn in _EMPTY_VALUES or current_dn == "1":
                    exp["disc_number"] = _fn_disc
            else:
                m2 = _re5.match(r"^(\d{1,2})\s", fp_tn[-1])
                if m2:
                    exp["track_number"] = str(int(m2.group(1)))

    # ── AI fallback: infer track/disc if still missing after all filename parsing ──
    _tn_after_parse = (exp.get("track_number") or "").strip()
    if not _tn_after_parse or _tn_after_parse in _EMPTY_VALUES or _tn_after_parse == "0":
        _ai_track, _ai_disc = ai_helper.infer_track_number(
            filename=filename_stem,
            folder_parts=folder_parts or [],
            artist_hint=(exp.get("artist") or "").strip(),
            album_hint=(exp.get("album") or "").strip(),
        )
        if _ai_track:
            exp["track_number"] = _ai_track
            if _ai_disc and _ai_disc != "1":
                exp["disc_number"] = _ai_disc
            log.debug(f"  [AI] Inferred track={_ai_track}, disc={_ai_disc} for {filename_stem!r}")

    # ── AI fallback: infer artist / album / title when still blank ────────────
    # The audit traditionally left artist and album untouched ("no external source"),
    # but ai_helper.infer_artist_album_title CAN infer them from the filename stem
    # and folder path with high confidence for well-named files.  Only called when
    # a field is genuinely missing — never overwrites a valid existing value.
    _artist_now = (exp.get("artist") or "").strip()
    _artist_watermarked = any(w in _artist_now.lower() for w in _WATERMARK_TAG_FRAGMENTS)
    _needs_ai_meta = (
        not _artist_now
        or _artist_now.lower() in _EMPTY_VALUES
        or _artist_watermarked
        or not (exp.get("album") or "").strip()
        or (exp.get("album") or "").strip().lower() in _EMPTY_VALUES
        or not (exp.get("title") or "").strip()
        or (exp.get("title") or "").strip().lower() in _EMPTY_VALUES
    )
    if _needs_ai_meta:
        _ai_meta = ai_helper.infer_artist_album_title(
            filename=filename_stem,
            folder_parts=(folder_parts or []) + ([_folder_name] if _folder_name else []),
            existing_tags={
                "artist": exp.get("artist", ""),
                "album":  exp.get("album", ""),
                "title":  exp.get("title", ""),
            },
        )
        if _ai_meta:
            for _field in ("artist", "album", "title"):
                _val = _ai_meta.get(_field, "").strip()
                _cur = (exp.get(_field) or "").strip()
                # Accept AI value when: field is blank, OR artist is watermarked
                _cur_is_bad = (not _cur or _cur.lower() in _EMPTY_VALUES or
                               (_field == "artist" and _artist_watermarked))
                if _val and _cur_is_bad:
                    exp[_field] = _val
            # Keep album_artist in sync if artist was filled in
            # In compilation folders album_artist stays as the DJ — do not override
            if _ai_meta.get("artist") and not _folder_is_compilation:
                _aa = (exp.get("album_artist") or "").strip()
                if not _aa or _aa.lower() in _EMPTY_VALUES:
                    exp["album_artist"] = _ai_meta["artist"]
            log.debug(f"  [AI] Filled metadata from infer_artist_album_title: {_ai_meta} for {filename_stem!r}")

    return exp


# ---------------------------------------------------------------------------
# Tag writer (with rating preservation)
# ---------------------------------------------------------------------------

def _write_tags(path: pathlib.Path, expected: dict, is_mp3: bool) -> bool:
    """
    Writes expected tag values to path (local temp copy).
    Returns True if any field was actually changed.
    Also preserves existing rating (if any) during the write.
    """
    # Read existing rating before overwriting
    existing_rating = _read_rating(path) if path.exists() else None

    # 2026-09-04: `is_mp3=False` used to mean "assume FLAC" unconditionally,
    # so any non-mp3 file — .m4a above all (yt-dlp/TIDAL's usual container) —
    # got force-fed into FLAC(path) and raised "not a valid FLAC file" on
    # every write. Confirmed live: 1,479 .m4a files silently unaudited this
    # way in one run. Dispatch on the real extension instead.
    ext = path.suffix.lower()
    changed = False
    try:
        if not is_mp3 and ext == ".m4a":
            from mutagen.easymp4 import EasyMP4
            if "composer" not in EasyMP4.Get:
                EasyMP4.RegisterTextKey("composer", "\xa9wrt")
            audio = EasyMP4(path)
            for field, easy_key in _EASY_FIELDS.items():
                if not config.AUDIT_FIELDS.get(field):
                    continue
                new_val = expected.get(field, "")
                if new_val and audio.get(easy_key, [""])[0] != new_val:
                    audio[easy_key] = new_val
                    changed = True
            if changed:
                audio.save()
        elif not is_mp3 and ext != ".flac":
            # No writer for this container yet (.wav/.ogg/.dsf/.wma/...) —
            # skip cleanly instead of guessing FLAC and crashing.
            log.debug(f"  [Audit] No tag writer for {ext} — skipping {path.name}")
            return False
        elif not is_mp3:
            audio = FLAC(path)
            for field, easy_key in _EASY_FIELDS.items():
                if not config.AUDIT_FIELDS.get(field):
                    continue
                new_val = expected.get(field, "")
                if new_val and audio.get(easy_key, [""])[0] != new_val:
                    audio[easy_key] = new_val
                    changed = True
            if changed:
                audio.save()
        else:
            try:
                audio = MP3(path, ID3=EasyID3)
            except ID3NoHeaderError:
                MP3(path).add_tags()
                audio = MP3(path, ID3=EasyID3)

            for field, easy_key in _EASY_FIELDS.items():
                if not config.AUDIT_FIELDS.get(field):
                    continue
                new_val = expected.get(field, "")
                if new_val and audio.get(easy_key, [""])[0] != new_val:
                    audio[easy_key] = new_val
                    changed = True

            if changed:
                audio.save(v2_version=config.ID3_VERSION)

            # Also fix ID3 version even if no field changed
            elif path.suffix.lower() == ".mp3":
                ver = _id3_version(path)
                if ver not in (0, config.ID3_VERSION):
                    audio.save(v2_version=config.ID3_VERSION)
                    changed = True

        # Restore rating if it existed before the write
        if existing_rating is not None:
            if is_mp3:
                try:
                    id3 = ID3(path)
                    id3.delall("POPM")
                    id3.add(POPM(email="Spotify Liked", rating=existing_rating, count=0))
                    id3.save(v2_version=config.ID3_VERSION)
                except Exception as e:
                    log.debug(f"  [Rating] Failed to restore rating after tag write: {e}")
            else:
                audio = FLAC(path)
                audio["rating"] = str(existing_rating)
                audio.save()

    except Exception as e:
        log.warning(f"  [Audit Write Error] {path.name}: {e}")
        return False

    return changed


# ---------------------------------------------------------------------------
# Diff builder  (for the audit report)
# ---------------------------------------------------------------------------

def _build_diff(current: dict, expected: dict) -> list[tuple[str, str, str]]:
    """
    Returns list of (field, old_value, new_value) for fields that changed.
    Only includes fields enabled in AUDIT_FIELDS.
    """
    diffs = []
    for field in _EASY_FIELDS:
        if not config.AUDIT_FIELDS.get(field):
            continue
        old = (current.get(field) or "").strip()
        new = (expected.get(field) or "").strip()
        if old != new and new:
            diffs.append((field, old, new))
    return diffs


# ---------------------------------------------------------------------------
# Genre cache lookup helper
# ---------------------------------------------------------------------------

# ── Genre cache in-process layer ────────────────────────────────────────────
# _genre_cache_lookup() is called up to 7 times per file in audit batch mode:
# once in the prefetch worker metakey check, once in _audit_one_device_file,
# and once for each of the fallback lookups.  With AUDIT_BATCH_SIZE=200 that
# is 1,400+ json.loads() calls per batch against a potentially large cache file.
#
# Fix 16: add a module-level in-process cache that re-reads the file only when
# the mtime changes or after a 60-second TTL.  Cuts per-batch I/O from O(N)
# disk reads to O(1) with a single read per minute maximum.
_GENRE_CACHE_INMEM:      dict  = {}
_GENRE_CACHE_MTIME:      float = 0.0
_GENRE_CACHE_LOADED_AT:  float = 0.0
_GENRE_CACHE_TTL_S:      float = 60.0
_GENRE_CACHE_LOCK              = threading.Lock()


def _genre_cache_refresh() -> dict:
    """Return the in-memory genre cache, reloading from disk when stale."""
    global _GENRE_CACHE_INMEM, _GENRE_CACHE_MTIME, _GENRE_CACHE_LOADED_AT
    with _GENRE_CACHE_LOCK:
        now = time.monotonic()
        try:
            mtime = config.GENRE_CACHE_FILE.stat().st_mtime
        except FileNotFoundError:
            return {}
        needs_reload = (
            not _GENRE_CACHE_INMEM
            or mtime != _GENRE_CACHE_MTIME
            or (now - _GENRE_CACHE_LOADED_AT) > _GENRE_CACHE_TTL_S
        )
        if needs_reload:
            try:
                _GENRE_CACHE_INMEM     = json.loads(
                    config.GENRE_CACHE_FILE.read_text(encoding="utf-8")
                )
                _GENRE_CACHE_MTIME    = mtime
                _GENRE_CACHE_LOADED_AT = now
            except Exception:
                pass
        return _GENRE_CACHE_INMEM


def _normalize_genre_cache() -> int:
    """
    Walk .genre_cache.json and apply _GENRE_NORM + operator overrides to every
    stored genre value.  Entries that change will produce a different expected
    metakey on next audit → those device files are automatically queued for
    re-tagging without any manual intervention.

    Called at the start of LibraryAuditor.run() so the current audit's metakey
    computation already sees normalized genres, and again from the post-audit
    device scan for any entries written during the run.
    Returns the number of cache entries changed.
    """
    if not config.GENRE_CACHE_FILE.exists():
        return 0
    try:
        cache = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0

    op       = gc._load_operator_overrides()
    norm_map = gc._GENRE_NORM
    changed  = 0

    for key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        raw = (entry.get("genre") or "").strip()
        if not raw:
            continue
        new = raw
        # 1. Direct _GENRE_NORM lookup (collapses variants like "Rap/Hip Hop" → "Hip-Hop")
        nk = raw.lower().strip()
        if nk in norm_map:
            new = norm_map[nk]
        # 2. Operator override (absolute priority — "artist_norm" is part before "|")
        artist_norm = key.split("|")[0] if "|" in key else ""
        if artist_norm and artist_norm in op:
            new = op[artist_norm]
        if new != raw:
            entry["genre"] = new
            changed += 1

    if not changed:
        return 0
    global _GENRE_CACHE_MTIME
    try:
        with _GENRE_CACHE_LOCK:
            config.GENRE_CACHE_FILE.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _GENRE_CACHE_MTIME = 0.0   # force reload on next _genre_cache_refresh()
        log.info(f"  [GenreCacheNorm] Normalized {changed} genre cache entries")
    except Exception as e:
        log.warning(f"  [GenreCacheNorm] Write failed: {e}")
        changed = 0
    return changed


def _enforce_album_genre_consensus() -> int:
    """
    For each album represented in walkman_tag_snapshot.json, if ≥60% of tagged
    tracks share one genre, update .genre_cache.json for minority-genre tracks to
    match.  No ADB required — uses the existing snapshot files from the last
    walkman_tree_gen + walkman_tag_scan run.

    Fixing the cache causes metakey mismatches on next audit → those tracks get
    re-tagged to the album's majority genre automatically.
    Returns the number of cache entries updated.
    """
    snap_path     = config.BASE_DIR / "walkman_snapshot.json"
    tag_snap_path = config.BASE_DIR / "walkman_tag_snapshot.json"
    if not snap_path.exists() or not tag_snap_path.exists():
        return 0
    try:
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        tag_snap = json.loads(tag_snap_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    if not config.GENRE_CACHE_FILE.exists():
        return 0
    try:
        cache = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0

    op   = gc._load_operator_overrides()
    _BAD = {"unknown", "unknown genre", ""}

    # Group tracks by album, build {album: [{ck, genre}]}
    album_tracks: dict[str, list[dict]] = {}
    for track in snapshot:
        album = (track.get("album") or "").strip()
        if not album:
            continue
        dp     = track.get("_data", "")
        ts     = tag_snap.get(dp, {})
        genre  = (ts.get("tag_genre") or "").strip()
        artist = (ts.get("tag_artist") or track.get("artist") or "").strip()
        title  = (ts.get("tag_title")  or track.get("title")  or "").strip()
        ck = f"{gc._norm(artist)}|{gc._norm(title)}"
        album_tracks.setdefault(album, []).append({"ck": ck, "genre": genre})

    from collections import Counter as _Cnt
    changed = 0
    for album, tracks in album_tracks.items():
        tagged = [t for t in tracks if t["genre"].lower() not in _BAD]
        if not tagged:
            continue
        counts = _Cnt(t["genre"] for t in tagged)
        if len(counts) <= 1:
            continue
        majority, n = counts.most_common(1)[0]
        if n / sum(counts.values()) < 0.6:
            continue  # no clear majority — leave it alone
        for t in tagged:
            if t["genre"] == majority:
                continue
            entry = cache.get(t["ck"])
            if isinstance(entry, dict) and (entry.get("genre") or "").lower() not in _BAD:
                # Never overwrite a genre_overrides.json operator override
                _artist = t["ck"].split("|")[0] if "|" in t["ck"] else ""
                if _artist and _artist in op:
                    continue
                entry["genre"] = majority
                changed += 1

    if not changed:
        return 0
    global _GENRE_CACHE_MTIME
    try:
        with _GENRE_CACHE_LOCK:
            config.GENRE_CACHE_FILE.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _GENRE_CACHE_MTIME = 0.0
        log.info(f"  [AlbumConsensus] Updated {changed} cache entries to majority album genre")
    except Exception as e:
        log.warning(f"  [AlbumConsensus] Write failed: {e}")
        changed = 0
    return changed


def _genre_cache_lookup(artist: str, title: str) -> dict:
    """
    Looks up the genre cache for a (artist, title) pair.
    If year is missing, falls back to _spotify_metadata_cache.json.
    Returns {"genre": str, "year": str} or {} on miss.

    FIX 16: uses an in-process cache (refreshed from disk at most every
    _GENRE_CACHE_TTL_S seconds or when the file changes) instead of calling
    json.loads(file.read_text()) on every invocation.
    """
    cache = _genre_cache_refresh()
    if not cache:
        return {}

    key = f"{gc._norm(artist)}|{gc._norm(title)}"
    result = dict(cache.get(key, {}))

    # Scan same-artist entries for a more specific subgenre
    if result.get("genre"):
        artist_norm = gc._norm(artist)
        for k, v in cache.items():
            if k.startswith(artist_norm + "|") and v.get("genre"):
                if _is_more_specific(v["genre"], result["genre"]):
                    result["genre"] = v["genre"]
                    break

    # MK26 FIX: Apply artist override table AFTER raw cache lookup.
    # Root cause of persistent wrong tags: genre_cache.json can contain stale/wrong
    # values (e.g. "Afrobeats" for The Weeknd) that the audit was stamping and
    # perpetually skipping. The override table in genre_client.py has the correct
    # genre but _genre_cache_lookup never consulted it — so overrides were silently
    # bypassed. Now we apply the override as a post-correction step.
    if result.get("genre"):
        try:
            # Operator overrides (genre_overrides.json) have ABSOLUTE PRIORITY
            # per Council 2026-05-10 §B2 — bypass the specificity gate so a
            # specific-but-wrong cached genre (e.g. "Afrobeats" for a metal band)
            # cannot block a correct override (e.g. "Nu-Metal").
            _op = gc._load_operator_overrides()
            _an = gc._norm(artist)
            if _an in _op:
                result["genre"] = _op[_an]
            else:
                # In-code overrides still respect the specificity gate
                _override = gc._artist_override(artist, "")
                if _override and not _is_more_specific(result["genre"], _override):
                    result["genre"] = _override
        except Exception:
            pass  # non-fatal — use raw cache value

    # If year is still missing, check Spotify metadata cache
    if not result.get("year"):
        try:
            _sp_path = config.BASE_DIR / "_spotify_metadata_cache.json"
            if _sp_path.exists():
                _sp = json.loads(_sp_path.read_text(encoding="utf-8"))
                for _tv in _sp.values():
                    _sk = f"{gc._norm(_tv.get('artist',''))}|{gc._norm(_tv.get('title',''))}"
                    if _sk == key:
                        _y = str(_tv.get("spotify_year", "") or "").strip()
                        if _y:
                            result["year"] = _y
                        break
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# TIDAL helpers (used by audit MP3→FLAC upgrade and metadata enrichment)
# ---------------------------------------------------------------------------

def _norm_title(t: str) -> str:
    """Normalised title for fuzzy comparison."""
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]", "", t.lower())
    t = re.sub(r"^(the |a |an )", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def _strip_feat_audit(title: str) -> str:
    return re.sub(
        r"\s*[\(\[\{](?:feat|ft|featuring)[^\)\]\}]*[\)\]\}]",
        "", title, flags=re.I,
    ).strip()


def _tidal_fetch_track(artist: str, title: str) -> dict:
    """
    Search TIDAL for a track and return a metadata dict:
      {"genre": str, "year": str, "tidal_id": int, "quality": str}
    Returns {} on any failure (missing credentials, no results, no internet).
    Does NOT download audio — metadata only.
    """
    try:
        import tidalapi
    except ImportError:
        return {}

    try:
        from downloader import _get_tidal_session
        session = _get_tidal_session()
    except Exception:
        return {}

    try:
        query   = f"{artist} {_strip_feat_audit(title)}"
        results = session.search(query, models=[tidalapi.Track], limit=5)
        tracks  = results.get("tracks", [])
        if not tracks:
            return {}

        title_lc  = _strip_feat_audit(title).lower()
        artist_lc = artist.lower()
        best = tracks[0]
        for t in tracks:
            tname = t.name.lower()
            if tname == title_lc and t.artist.name.lower() == artist_lc:
                best = t
                break
            if tname == title_lc:
                best = t

        # Extract metadata from the TIDAL track object
        meta: dict = {"tidal_id": best.id, "quality": str(best.audio_quality), "matched_artist": best.artist.name}
        # Year from album release date
        try:
            rd = best.album.release_date
            if rd:
                meta["year"] = str(rd.year)
        except Exception:
            pass
        # Album cover art URL (largest available)
        try:
            cover_url = best.album.image(1280)  # 1280x1280 px
            if cover_url:
                meta["cover_url"] = cover_url
        except Exception:
            try:
                cover_url = best.album.image(640)
                if cover_url:
                    meta["cover_url"] = cover_url
            except Exception:
                pass
        # Genre — tidalapi 0.8 rarely exposes genres, try multiple paths
        try:
            genres = (getattr(best, "genres", None) or
                     getattr(best.album, "genres", None) or
                     getattr(best, "genre", None))
            if genres:
                if isinstance(genres, list) and genres:
                    meta["genre"] = genres[0] if isinstance(genres[0], str) else genres[0].name
                elif isinstance(genres, str) and genres:
                    meta["genre"] = genres
        except Exception:
            pass

        # If TIDAL had no genre, fall back to genre_client waterfall
        if "genre" not in meta:
            try:
                import genre_client as _gc
                cache = _gc._load_genre_cache()
                ck = _gc._cache_key(best.artist.name, best.name)
                if ck in cache and cache[ck].get("genre"):
                    meta["genre"] = cache[ck]["genre"]
                else:
                    # Phase 1: Last.fm → Deezer
                    hit = _gc._lastfm(best.artist.name, best.name)
                    if hit.get("genre") and not _gc._is_broad(hit["genre"]):
                        meta["genre"] = hit["genre"]
                    else:
                        hit2 = _gc._deezer(best.artist.name, best.name)
                        if hit2.get("genre") and not _gc._is_broad(hit2["genre"]):
                            meta["genre"] = hit2["genre"]
                        elif hit.get("genre"):
                            meta["genre"] = hit["genre"]  # broad is better than nothing
                        elif hit2.get("genre"):
                            meta["genre"] = hit2["genre"]
                    # Phase 3: Google scrape — only if still missing after API waterfall
                    if not meta.get("genre") or _gc._is_broad(meta.get("genre", "")):
                        try:
                            gg = _gc._google_genre(best.artist.name, best.name)
                            if gg.get("genre"):
                                meta["genre"] = gg["genre"]
                                log.debug(f"  [Google] genre={gg['genre']!r} for {best.artist.name} — {best.name}")
                        except Exception:
                            pass
            except Exception:
                pass

        return meta
    except Exception as e:
        log.debug(f"  [TIDAL meta] fetch failed: {e}")
        return {}


# In-process TIDAL download failure cache — persisted to .audit_state.json
# by LibraryAuditor.run() at the end of each run.
# Structure: {fail_key: {"fails": N, "skip_until": ISO}}
_TIDAL_FAIL_CACHE: dict = {}

_TIDAL_DL_FAIL_THRESHOLD = 2    # failures before cooldown
_TIDAL_DL_COOLDOWN_DAYS  = 30   # skip for this many days after threshold

# ── Push failure backoff cache ─────────────────────────────────────────────
# Tracks MTP push failures per device file key, inspired by spotify_to_tidal's
# persistent failure cache with exponential backoff.  After PUSH_FAIL_THRESHOLD
# consecutive failures, the file is skipped for an exponentially increasing
# number of runs so a single broken FLAC/MTP-degraded file doesn't burn
# 90s on every audit run.
# Structure: {file_key: {"fails": N, "backoff_runs": N, "runs_remaining": N}}
# Persisted inside .audit_state.json under key "push_fail_cache".
_PUSH_FAIL_CACHE: dict = {}          # loaded from state in LibraryAuditor.__init__
_PUSH_FAIL_THRESHOLD    = 3          # consecutive failures before backoff
_PUSH_FAIL_MAX_BACKOFF  = 8          # max runs to skip (caps at 2^3 = 8)

def _push_fail_record(file_key: str) -> None:
    """Record a push failure. After threshold, set exponential run-skip backoff."""
    entry = _PUSH_FAIL_CACHE.get(file_key, {"fails": 0, "backoff_runs": 0})
    entry["fails"] = entry.get("fails", 0) + 1
    if entry["fails"] >= _PUSH_FAIL_THRESHOLD:
        # Exponential backoff: 2^(fails - threshold), capped at _PUSH_FAIL_MAX_BACKOFF
        backoff = min(2 ** (entry["fails"] - _PUSH_FAIL_THRESHOLD + 1), _PUSH_FAIL_MAX_BACKOFF)
        entry["backoff_runs"] = backoff
        entry["runs_remaining"] = backoff
        log.debug(
            f"  [PushBackoff] {file_key!r}: {entry['fails']} failures — "
            f"skipping next {backoff} run(s)"
        )
    _PUSH_FAIL_CACHE[file_key] = entry

def _push_fail_clear(file_key: str) -> None:
    """Clear push failure record after a successful push."""
    if file_key in _PUSH_FAIL_CACHE:
        _PUSH_FAIL_CACHE.pop(file_key)

def _push_fail_should_skip(file_key: str) -> bool:
    """
    Return True if this file is currently in backoff and should not be pushed.
    Decrements runs_remaining each call so the backoff counts down run by run.
    """
    entry = _PUSH_FAIL_CACHE.get(file_key)
    if not entry:
        return False
    remaining = entry.get("runs_remaining", 0)
    if remaining > 0:
        entry["runs_remaining"] = remaining - 1
        _PUSH_FAIL_CACHE[file_key] = entry
        log.info(
            f"  [PushBackoff] Skipping push for {file_key!r} "
            f"({remaining} run(s) left in backoff after {entry.get('fails', 0)} failures)"
        )
        return True
    return False


def _tidal_dl_record_fail(fail_key: str) -> None:
    """Record a TIDAL download failure. After threshold, set a cooldown."""
    import datetime as _dt_rf
    entry = _TIDAL_FAIL_CACHE.get(fail_key, {"fails": 0})
    entry["fails"] = entry.get("fails", 0) + 1
    entry["last"]  = _dt_rf.datetime.now().isoformat()
    if entry["fails"] >= _TIDAL_DL_FAIL_THRESHOLD:
        entry["skip_until"] = (
            _dt_rf.datetime.now() + _dt_rf.timedelta(days=_TIDAL_DL_COOLDOWN_DAYS)
        ).isoformat()
        log.debug(
            f"  [TIDAL dl] cooldown set for {fail_key!r}: "
            f"{entry['fails']} failures, skipping until {entry['skip_until'][:10]}"
        )
    _TIDAL_FAIL_CACHE[fail_key] = entry


def _tidal_download_flac(artist: str, title: str, output_dir: str) -> pathlib.Path | None:
    """
    Download a HiRes FLAC from TIDAL for the given artist/title.
    Returns the path to the downloaded .flac file, or None on failure.
    """
    try:
        import tidalapi
        import requests as _requests
    except ImportError:
        return None

    try:
        from downloader import _get_tidal_session, _ascii_safe, _strip_feat
        session = _get_tidal_session()
    except Exception as e:
        log.warning(f"  [TIDAL dl] session error: {e}")
        return None

    # Check permanent-failure cache — skip tracks that have failed repeatedly
    _fail_key = f"tidal_dl_fail:{artist.lower().strip()}|{_strip_feat_audit(title).lower().strip()}"
    try:
        _fail_state = _TIDAL_FAIL_CACHE.get(_fail_key, {})
        if _fail_state.get("skip_until"):
            import datetime as _dt_fc
            _skip_until = _dt_fc.datetime.fromisoformat(_fail_state["skip_until"])
            if _dt_fc.datetime.now() < _skip_until:
                log.debug(f"  [TIDAL dl] skip (cooldown until {_fail_state['skip_until'][:10]}): {artist} — {title}")
                return None
    except Exception:
        pass

    try:
        query   = f"{artist} {_strip_feat_audit(title)}"
        results = session.search(query, models=[tidalapi.Track], limit=5)
        tracks  = results.get("tracks", [])
        if not tracks:
            log.warning(f"  [TIDAL dl] no results for: {query!r}")
            _tidal_dl_record_fail(_fail_key)
            return None

        title_lc  = _strip_feat_audit(title).lower()
        artist_lc = artist.lower()
        best = tracks[0]
        for t in tracks:
            if t.name.lower() == title_lc and t.artist.name.lower() == artist_lc:
                best = t; break
            if t.name.lower() == title_lc:
                best = t

        # Artist sanity check: if the best match has a very different artist,
        # we'd be downloading the wrong track (e.g. searching "Rockabye Baby!
        # SpottieOttieDopaliscious" finds the Outkast original, not the cover).
        # Skip if artist similarity is below threshold.
        import unicodedata as _ucd_dl
        def _norm_artist_dl(s):
            s = _ucd_dl.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
            return re.sub(r"[^a-z0-9 ]", "", s).strip()
        _req_artist  = _norm_artist_dl(artist)
        _found_artist = _norm_artist_dl(best.artist.name)
        # Allow if: one contains the other, or they share >50% of words
        _req_words   = set(_req_artist.split())
        _found_words = set(_found_artist.split())
        _shared      = _req_words & _found_words
        _artist_ok   = (
            _req_artist in _found_artist or
            _found_artist in _req_artist or
            (len(_req_words) > 0 and len(_shared) / len(_req_words) >= 0.5)
        )
        if not _artist_ok:
            log.debug(
                f"  [TIDAL dl] artist mismatch — want {artist!r}, got {best.artist.name!r}, skipping"
            )
            _tidal_dl_record_fail(_fail_key)
            return None

        log.info(f"  [TIDAL dl] {best.artist.name} — {best.name} ({best.audio_quality})")

        out_stem = re.sub(r'[\\/:*?"<>|]', "_",
                          unicodedata.normalize("NFKD", _strip_feat_audit(title))
                          .encode("ascii", "ignore").decode("ascii")).strip(". ")
        out_path = pathlib.Path(output_dir) / f"{out_stem}.flac"

        # Multi-fallback stream URL resolution — tidalapi API changed across versions:
        #   pre-0.8 : stream.get_stream_url()  (removed in newer versions → AttributeError)
        #   0.8.x   : stream.url               (direct attribute)
        #   0.8.x   : best.get_url()           (convenience method on Track)
        #   Some    : stream.urls / stream.segment_urls  (multi-segment manifest)
        # Also try quality waterfall: HI_RES_LOSSLESS → LOSSLESS → HIGH
        downloaded = False
        try:
            import tidalapi as _tidalapi_dl
            _qualities = [
                getattr(_tidalapi_dl.Quality, 'hi_res_lossless', None),
                getattr(_tidalapi_dl.Quality, 'lossless', None),
                getattr(_tidalapi_dl.Quality, 'high', None),
            ]
            _qualities = [q for q in _qualities if q is not None]
        except Exception:
            _qualities = [None]

        _stream_url = None
        for _q in (_qualities or [None]):
            try:
                _stream = best.get_stream_info(quality=_q) if _q else best.get_stream()
                # Try .url attribute (tidalapi >= 0.8)
                _stream_url = getattr(_stream, 'url', None)
                # Fallback: .get_stream_url() method (tidalapi < 0.8)
                if not _stream_url and hasattr(_stream, 'get_stream_url'):
                    _stream_url = _stream.get_stream_url()
                # Fallback: multi-segment manifest
                if not _stream_url:
                    _seg_urls = getattr(_stream, 'urls', None) or getattr(_stream, 'segment_urls', None)
                    if _seg_urls:
                        with open(out_path, "wb") as fh:
                            for _seg in _seg_urls:
                                _r = _requests.get(_seg, stream=True, timeout=60)
                                _r.raise_for_status()
                                for _chunk in _r.iter_content(chunk_size=1024 * 256):
                                    fh.write(_chunk)
                        downloaded = out_path.exists() and out_path.stat().st_size > 0
                        if downloaded:
                            break
                if _stream_url:
                    break
            except Exception as _qe:
                log.debug(f"  [TIDAL dl] quality {_q} failed: {_qe}")
                continue

        # Final fallback: Track.get_url() convenience method
        if not _stream_url and not downloaded:
            try:
                _stream_url = best.get_url()
            except Exception:
                pass

        if _stream_url and not downloaded:
            resp = _requests.get(_stream_url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    fh.write(chunk)
            downloaded = out_path.exists() and out_path.stat().st_size > 0

        if not downloaded:
            _tidal_dl_record_fail(_fail_key)
            raise RuntimeError("All TIDAL stream URL methods exhausted")

        # Remux with ffmpeg for a clean container
        if shutil.which("ffmpeg") and out_path.stat().st_size > 0:
            remuxed = out_path.with_name(out_path.stem + "_remux.flac")
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(out_path), "-c:a", "copy", str(remuxed)],
                capture_output=True, check=False,
            )
            if r.returncode == 0 and remuxed.exists() and remuxed.stat().st_size > 0:
                out_path.unlink()
                remuxed.rename(out_path)

        return out_path if out_path.exists() and out_path.stat().st_size > 0 else None

    except Exception as e:
        log.warning(f"  [TIDAL dl] download failed: {e}")
        return None


def _canonical_filename(tags: dict, suffix: str) -> str:
    """
    Build canonical device filename from tags dict:
    Artist - Album - CDx - NN Title (feat. X, Y).ext
    """
    aa    = sanitize_name(tags.get("album_artist") or tags.get("artist") or "Unknown", 40)
    alb   = sanitize_name(tags.get("album") or "Unknown", 60)
    disc  = str(tags.get("disc_number") or "1").split("/")[0]
    num   = str(tags.get("track_number") or "1").split("/")[0].zfill(2)
    title = sanitize_name(tags.get("title") or "Unknown", 60)

    # feat. artists from artist tag if it contains comma-separated names
    # (tags stored as single string — we infer feat. from the title tag if present)
    feat_part = ""
    if "(feat." in title.lower() or "(ft." in title.lower():
        feat_part = ""  # already in title
    cd_part = f"CD{disc} - " if disc not in ("", "0", "1") else ""

    # Compose stem - don't truncate title portion, only limit total length
    stem = f"{aa} - {alb} - {cd_part}{num} {title}{feat_part}"
    # ASCII-safe for Shell.Application
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r'[\\/:*?"<>|]', "_", stem).strip(". ")

    # Trim to 200 chars max but never cut in the middle of a word/parenthesis
    if len(stem) > 200:
        # Try to cut at a space before 200
        cut = stem[:200].rfind(" ")
        stem = stem[:cut if cut > 150 else 200].rstrip()

    return stem + suffix


def _primary_artist_folder(aa: str) -> str:
    """
    Extract the primary artist for use as a device folder name.
    Strips feat./slash collaborators so we don't create per-feat. artist folders.
      "Outkast/CeeLo Green"   → "Outkast"
      "Outkast feat. Raekwon" → "Outkast"
      "Tyler, the Creator"    → "Tyler, the Creator"  (comma preserved)
    """
    primary = aa.split("/")[0].strip()
    primary = re.split(r"\s+(?:feat\.|ft\.|featuring)\s+", primary, flags=re.IGNORECASE)[0].strip()
    return primary or aa


def _canonical_dest_parts(tags: dict) -> list[str] | None:
    """
    Compute the canonical Music/ sub-path for a file given its final tags.
    Returns folder parts list e.g. ["Music", "Outkast", "Aquemini"]
    Returns None if tags are insufficient (no artist/album).

    Used by the audit push so every file lands in the right folder as part of
    the push already happening — zero extra MTP operations (Option C).
    """
    aa    = (tags.get("album_artist") or tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()
    if not aa or not album:
        return None
    primary = _primary_artist_folder(aa)
    aa_s    = sanitize_name(primary, 50)
    alb_s   = sanitize_name(album,   60)
    if primary.lower() == "various artists":
        return [config.MUSIC_ROOT, alb_s]
    return [config.MUSIC_ROOT, aa_s, alb_s]


def _normalized_folder_name(folder_name: str) -> str:
    """
    Return a normalized key for a folder (e.g., "outkast|aquemini") used for
    grouping duplicate folders.
    """
    # Try to parse Artist - Album
    if " - " in folder_name:
        artist, album = folder_name.split(" - ", 1)
        norm_artist = normalize_key(artist)
        norm_album  = normalize_key(album)
        return f"{norm_artist}|{norm_album}"
    else:
        # Assume it's just an album name – use empty artist
        norm_album = normalize_key(folder_name)
        return f"|{norm_album}"


# ---------------------------------------------------------------------------
# Main audit runner
# ---------------------------------------------------------------------------

class LibraryAuditor:
    """
    Audits all audio files on the device (and local folders) after sync.
    Instantiate once and call run().
    """

    def __init__(self, log_callback: Callable[[str], None] | None = None,
                 dry_run: bool = False):
        import threading as _threading
        self._cb         = log_callback
        # 2026-08-20: previously only gated the cross-format-prune delete
        # path below — _audit_one_device_file()'s push/rename/canonical-move
        # writes were NOT gated at all, confirmed live to make real device
        # writes (100+ tracks) during `audit --dry-run`. Now also gates the
        # three real-write sites in _audit_one_device_file (search
        # "self.dry_run" in that method) — a genuine, real device write
        # should never happen when this is True.
        self.dry_run     = dry_run
        self.state       = _load_audit_state()
        # Load TIDAL download failure cache from persisted state
        _tidal_fails = self.state.get("__tidal_dl_fails__", {})
        if isinstance(_tidal_fails, dict):
            _TIDAL_FAIL_CACHE.update(_tidal_fails)
        # Load push failure backoff cache from persisted state
        _push_fails = self.state.get("__push_fail_cache__", {})
        if isinstance(_push_fails, dict):
            _PUSH_FAIL_CACHE.update(_push_fails)
        self.report      : list[str] = []
        self._stats_lock = _threading.Lock()
        self._state_lock = _threading.Lock()
        self._report_lock= _threading.Lock()
        # Album-level genre/year cache: {"Artist - Album" -> {"genre": str, "year": str}}
        # Pre-populated from _spotify_metadata_cache.json so every album has a year
        # from the start — no waiting for the first track of an album to be processed.
        self._album_meta : dict[str, dict] = {}
        self._album_lock = _threading.Lock()
        # Album-level genre cache: keyed by "album_artist|album" (primary artist only).
        # Populated on first track encounter; all subsequent tracks in the same album
        # reuse the stored genre without any API calls.  This ensures every track in
        # an album gets the same genre, regardless of per-track featured artists.
        self._album_genre_cache: dict[str, str] = {}
        self._prefill_album_meta_from_spotify()
        self._repair_genre_cache_overrides()
        # Liked track IDs from Spotify — fetched once at startup so the audit
        # can stamp POPM=255 on files that are liked but have no rating yet.
        # Populated by _fetch_liked_track_ids(); gracefully empty if Spotify
        # is unavailable or the call fails.
        self._liked_track_ids: set[str] = set()
        self._fetch_liked_track_ids()

        # ── Streaming-history POPM candidates ────────────────────────────────
        # Supplement Liked Songs with tracks the user plays heavily but may not
        # have explicitly "hearted". These get POPM=200 (4-star) rather than 255
        # (5-star) so Liked Songs tracks remain distinguishable.
        self._history_popm_uris: set[str] = set()    # URIs → POPM=200
        self._history_popm_scores: dict[str, float] = {}
        try:
            import streaming_history as _sh
            _sh_scores = _sh.load()
            if _sh_scores:
                # Heavy listeners (≥20 plays, ≤15% skip) that aren't already liked
                candidates = _sh.popm_candidates(_sh_scores, min_plays=20, max_skip_rate=0.15)
                # Extract just the track ID (strip "spotify:track:" prefix)
                for uri in candidates:
                    track_id = uri.split(":")[-1] if ":" in uri else uri
                    if track_id not in self._liked_track_ids:
                        self._history_popm_uris.add(track_id)
                        self._history_popm_scores[track_id] = _sh_scores[uri]["priority_score"]
                log.info(
                    f"  [StreamingHistory] {len(self._history_popm_uris)} additional "
                    f"POPM=200 candidates from listening history "
                    f"(not in Liked Songs)"
                )
        except Exception as _sh_e:
            log.debug(f"  [StreamingHistory] POPM candidates skipped: {_sh_e}")

        self.stats = {
            "audited":  0,
            "changed":  0,
            "skipped":  0,
            "errors":   0,
        }

    def _repair_genre_cache_overrides(self) -> None:
        """
        Scan the on-disk genre cache and correct any entries whose genre
        contradicts the authoritative artist override table.

        This handles the case where a previous audit run wrote a wrong genre
        (e.g. "Afrobeats" for Kali Uchis, "Pop Rap" for Travis Scott) into
        the cache, and that stale entry would otherwise persist indefinitely
        because the cache takes priority over live lookups.

        Only corrects entries where:
          a) the artist has a definitive override entry, AND
          b) the cached genre differs from the override, AND
          c) the override is more specific than the cached genre
             (we never downgrade a specific correct tag to a broader override)

        Runs at startup in O(cache_size) time — fast even for large caches.
        """
        try:
            cache = gc._load_genre_cache()
            if not cache:
                return
            changed = 0
            for cache_key, entry in cache.items():
                if not isinstance(entry, dict):
                    continue
                cached_genre = (entry.get("genre") or "").strip()
                if not cached_genre:
                    continue
                # Extract artist from cache key "norm_artist|norm_title"
                artist_norm = cache_key.split("|")[0] if "|" in cache_key else ""
                if not artist_norm:
                    continue
                # Check override table using the normalised artist name directly
                override = gc._ARTIST_GENRE_OVERRIDES.get(artist_norm, "")
                if not override:
                    continue
                if cached_genre == override:
                    continue
                # Only replace if override is at least as specific as current
                # (never replace "Amapiano" with "Afrobeats" for a DJ who sometimes does both)
                if _is_more_specific(cached_genre, override):
                    continue   # cached genre is more specific — keep it
                entry["genre"] = override
                changed += 1
            if changed:
                gc._save_genre_cache(cache)
                log.info(
                    f"  [GenreCache] Corrected {changed} stale genre entries "
                    f"against override table"
                )
        except Exception as e:
            log.debug(f"  [GenreCache] Override repair failed: {e}")

    def _prefill_album_meta_from_spotify(self):
        """
        Pre-populates _album_meta with year (and genre if available) from
        _spotify_metadata_cache.json so every album has data from the start,
        not just after its first track is audited.
        Covers all 1600+ albums in the library in <100ms.
        """
        try:
            sp_path = config.BASE_DIR / "_spotify_metadata_cache.json"
            if not sp_path.exists():
                return
            sp_data = json.loads(sp_path.read_text(encoding="utf-8"))
            filled = 0
            for tv in sp_data.values():
                artist    = (tv.get("artist") or "").strip()
                album     = (tv.get("album")  or "").strip()
                year      = str(tv.get("spotify_year", "") or "").strip()
                sp_cover  = (tv.get("album_image") or "").strip()
                if not (artist and album and year):
                    continue
                key = f"{artist} - {album}"
                if key not in self._album_meta:
                    self._album_meta[key] = {
                        "year": year, "genre": "", "album_artist": artist,
                        "spotify_cover_url": sp_cover,
                    }
                    filled += 1
                else:
                    if not self._album_meta[key].get("year"):
                        self._album_meta[key]["year"] = year
                    if not self._album_meta[key].get("album_artist"):
                        self._album_meta[key]["album_artist"] = artist
                    if sp_cover and not self._album_meta[key].get("spotify_cover_url"):
                        self._album_meta[key]["spotify_cover_url"] = sp_cover
            log.debug(f"  [AlbumCache] pre-filled {filled} album year(s) from Spotify metadata")

            # Also backfill genre cache year field from Spotify data (one-time catch-up)
            # The genre cache may have correct genre but year='' if it was built before
            # the spotify_year fallback was added. Fix this silently on every startup.
            try:
                if not config.GENRE_CACHE_FILE.exists():
                    return
                gcache = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
                sp_year_idx = {}
                for _tv in sp_data.values():
                    _a = (_tv.get("artist") or "").strip()
                    _t = (_tv.get("title")  or "").strip()
                    _y = str(_tv.get("spotify_year", "") or "").strip()
                    if _a and _t and _y:
                        import genre_client as _gc2
                        sp_year_idx[f"{_gc2._norm(_a)}|{_gc2._norm(_t)}"] = _y
                gc_filled = 0
                for _gk, _gv in gcache.items():
                    if not _gv.get("year") and _gk in sp_year_idx:
                        _gv["year"] = sp_year_idx[_gk]
                        gc_filled += 1
                if gc_filled:
                    config.GENRE_CACHE_FILE.write_text(
                        json.dumps(gcache, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    log.debug(f"  [GenreCache] backfilled {gc_filled} year(s) from Spotify metadata")
            except Exception as _ge:
                log.debug(f"  [GenreCache] year backfill failed: {_ge}")
        except Exception as e:
            log.debug(f"  [AlbumCache] prefill failed: {e}")

    def _fetch_liked_track_ids(self) -> None:
        """
        Fetch the user's Spotify liked-track IDs into self._liked_track_ids.

        MK13/MK17: Added OFFLINE_MODE fast-path.  When config.OFFLINE_MODE=True,
        reads liked track IDs from the local Spotify export (spotify_local_data)
        instead of calling the Spotify API.  This avoids unnecessary API calls
        during offline runs and respects the OFFLINE_MODE config flag.

        Called once during __init__.  Takes 2–5 s on first run (pagination);
        subsequent calls within the same Python process are near-instant because
        spotify_client caches the token.  Degrades gracefully — if Spotify is
        unavailable or credentials are absent, self._liked_track_ids stays empty
        and no POPM stamps are added (safe no-op).
        """
        # MK13/MK17 FIX: OFFLINE_MODE fast-path — no network needed
        try:
            import config as _cfg_mod
            if getattr(_cfg_mod, "OFFLINE_MODE", False):
                try:
                    import spotify_local_data as _sld
                    uris = _sld.get_liked_uris()
                    # Convert spotify:track:XXXX → XXXX
                    self._liked_track_ids = {
                        u.split(":")[-1] for u in (uris or []) if ":" in u
                    }
                    log.debug(
                        f"  [LikedTracks] OFFLINE_MODE: loaded "
                        f"{len(self._liked_track_ids)} liked IDs from local export"
                    )
                    return
                except Exception as _oe:
                    log.debug(f"  [LikedTracks] OFFLINE_MODE fallback failed: {_oe}")
        except Exception:
            pass

        try:
            import spotify_client as _sc
            sp = _sc.get_spotify_client()
            if sp is None:
                log.debug("  [LikedTracks] Spotify client unavailable — skipping liked fetch")
                return
            liked: set[str] = set()
            offset = 0
            limit  = 50
            while True:
                result = sp.current_user_saved_tracks(limit=limit, offset=offset)
                items  = (result or {}).get("items") or []
                for item in items:
                    track = (item or {}).get("track") or {}
                    tid   = track.get("id")
                    if tid:
                        liked.add(tid)
                if len(items) < limit:
                    break
                offset += limit
            self._liked_track_ids = liked
            log.info(f"  [LikedTracks] Fetched {len(liked)} liked track ID(s) from Spotify")
        except Exception as e:
            log.debug(f"  [LikedTracks] Fetch failed (non-fatal): {e}")

    def _log(self, msg: str):
        # 2026-08-22: this dropped every message on the floor as far as the
        # GUI is concerned — self._cb (the log_callback passed into
        # __init__, e.g. the GUI's _on_log chain via SpiralOrchestrator) was
        # stored but never called anywhere in this class. Every self._log()
        # call in LibraryAuditor (audit-start banner, file counts, every
        # per-file CHANGED block) went only to the standard `logging`
        # module, invisible to any GUI-triggered run — confirmed live: a
        # real audit progressed for minutes (verified via py-spy showing
        # real pushes/deletes happening) with the GUI log panel showing
        # nothing past "Audit/tag". CLI-launched runs never showed this
        # symptom because cli_runner.py redirects the standard logger
        # itself, so this was GUI-specific and easy to miss.
        log.info(msg)
        if self._cb:
            try:
                self._cb(msg)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Device file enumeration
    # -----------------------------------------------------------------------

    def build_device_library(self) -> "device_library.DeviceLibrary":
        """
        Build and return a DeviceLibrary for the current device state.
        Used by walkman_sync Stage 3/4 and any caller that needs album-identity
        aware presence checks (not just flat filename matching).
        """
        return device_library.DeviceLibrary.build(
            config.DEVICE_NAME,
            config.STORAGE_ROOTS,
        )

    def _enumerate_device(self) -> list[dict]:
        """
        Returns all audio files across ALL configured storage roots as dicts:
          {"filename": str, "parts": [str, ...], "storage": str, "key": str}
        key is "storage/parts/filename" used as the audit state key.
        Silently skips any storage root not present on the device (e.g. no SD card).
        """
        raw = mtp.scan_device_audio(
            config.DEVICE_NAME,
            config.STORAGE_ROOTS,
        )
        result = []
        for ef in raw:
            filename = ef["filename"]
            ext      = ef.get("ext", pathlib.Path(filename).suffix.lower())
            # Reconstruct full filename with real extension (Walkman strips ext from item.Name)
            full_filename = filename if pathlib.Path(filename).suffix else filename + ext
            # Include storage name in key so SD card and internal files don't collide
            key = ef["storage"] + "/" + "/".join(ef["parts"] + [full_filename])
            result.append({
                "filename": full_filename,
                "parts":    ef["parts"],
                "storage":  ef["storage"],
                "key":      key,
            })
        return result

    def _enumerate_local(self) -> list[pathlib.Path]:
        """Returns all local MP3/FLAC files across LOCAL_FOLDERS.

        Skips any path whose components include a hidden directory
        (starts with '.') or a known walkman staging directory, so that
        .walkman_tmp/walkman_dl_XXXX/tmpXXXX/ stream captures are never
        accidentally audited.
        """
        files = []
        for folder in config.LOCAL_FOLDERS:
            if not folder.exists():
                continue
            for f in folder.rglob("*"):
                if f.suffix.lower() not in (".mp3", ".flac"):
                    continue
                # Compute path components relative to the root folder so we
                # don't accidentally skip OS-level hidden dirs above the scan root.
                try:
                    rel_parts = f.relative_to(folder).parts
                except ValueError:
                    rel_parts = f.parts
                if any(
                    part.startswith(".")          # .walkman_tmp, .walkman_check, etc.
                    or part.startswith("walkman_dl_")
                    or part == "walkman_tmp"
                    for part in rel_parts
                ):
                    continue
                files.append(f)
        return files

    # -----------------------------------------------------------------------
    # Single-file audit
    # -----------------------------------------------------------------------
    def _audit_local_files(self, local_paths: list[pathlib.Path]) -> None:
        """Audit local files in-place (no MTP)."""
        for path in local_paths:
            if not path.exists():
                continue
            ext = path.suffix.lower()
            if ext not in (".mp3", ".flac"):
                continue
            state_key = str(path.absolute())
            self._audit_one(path, state_key, is_device=False, device_info=None)

    def _audit_one(
        self,
        path:        pathlib.Path,
        state_key:   str,
        is_device:   bool,
        device_info: dict | None = None,
    ) -> bool:
        """
        Audits a single file (already copied to a local temp path if device file).
        Reads current tags, computes expected tags, writes back if different.
        Updates audit state.  Returns True if the file was changed.
        """
        # Early validity check — skip corrupt/truncated files immediately.
        # Mutagen raises on write but not always on read, so we check before
        # burning any API calls or log lines on a file we can't fix anyway.
        if not _is_valid_audio(path):
            log.warning(
                f"  [Audit Skip] {path.name}: corrupt or unreadable audio file — "
                f"re-download and re-run audit to fix"
            )
            return False

        current  = _read_tags(path)
        stem     = path.stem

        # Guess artist/title from folder structure if tags are blank
        # Device layout: Music/ArtistFolder/AlbumFolder/file.mp3
        # parts[-2] = album folder, parts[-3] or similar = artist folder
        inferred_artist = current.get("artist", "")
        inferred_title  = current.get("title", "")
        if not inferred_artist or not inferred_title:
            # Try parsing "Artist - Album - NN Title" from the filename stem
            stem_parts = [p.strip() for p in stem.split(" - ")]
            if len(stem_parts) >= 3:
                if not inferred_artist:
                    inferred_artist = stem_parts[0]
                if not inferred_title:
                    import re as _re2
                    inferred_title = _re2.sub(r"^\d{1,3}[\s\.\-]+", "", stem_parts[-1]).strip() or stem_parts[-1]
            elif len(stem_parts) == 2:
                if not inferred_artist:
                    inferred_artist = stem_parts[0]
                if not inferred_title:
                    inferred_title = stem_parts[1]

        if device_info and (not inferred_artist or not inferred_title):
            parts = device_info.get("parts", [])
            # parts[0] = "Music", parts[1] = artist/album folder
            if len(parts) >= 2 and not inferred_artist:
                # Folder may be "Artist - Album" — take first segment
                folder_parts = parts[1].split(" - ", 1)
                inferred_artist = folder_parts[0].strip()
            if not inferred_title:
                inferred_title = stem

        # ── Album-level genre resolution ────────────────────────────────────
        # Genre is determined ONCE per album using the album_artist (not the
        # per-track artist, which may be a featured artist or collaborator).
        # Priority order:
        #   1. Live API  (Last.fm / Deezer) — always tried first
        #   2. Artist override table        — for well-known artists
        #   3. Per-track genre cache        — only if live API returns nothing
        #
        # The result is stored in self._album_genre_cache keyed by
        # "album_artist|album" so every subsequent track in the same album
        # reuses it with no further API calls.
        #
        # Producer albums (Metro Boomin, Kanye) and collabs (JPEGMAFIA &
        # Danny Brown) both work correctly because album_artist is always
        # the primary project owner, not the featured guests.

        # Derive the primary album_artist (strip features / collaborators)
        _aa_raw   = (current.get("album_artist") or current.get("artist") or inferred_artist or "").strip()
        _album_aa = _aa_raw.split(";")[0].split(" & ")[0].split(" x ")[0].split("/")[0].strip()
        _album_nm = (current.get("album") or "").strip()
        _album_genre_key = f"{gc._norm(_album_aa)}|{gc._norm(_album_nm)}" if _album_aa else ""

        # Check album-level in-memory cache first (no I/O)
        _cached_album_genre = self._album_genre_cache.get(_album_genre_key, "") if _album_genre_key else ""

        genre_hit: dict = {}

        if _cached_album_genre:
            # Already resolved this album — reuse without any API call
            genre_hit["genre"] = _cached_album_genre
            log.debug(f"  [Genre/Album] {_album_aa!r} / {_album_nm!r} → {_cached_album_genre!r} (album cache)")

        elif _album_aa and _album_aa != stem:
            # First track in this album — resolve genre live
            _resolved_genre = ""
            _resolved_year  = ""
            try:
                # Step 1: MusicFetch (URL-first, Apple Music genre taxonomy).
                # Requires a Spotify track ID — look it up from the local
                # Spotify metadata cache keyed by track ID.
                # Only attempted when MUSICFETCH_API_KEY is configured.
                _mf_key = (getattr(config, "MUSICFETCH_API_KEY", "")
                           or __import__("os").environ.get("MUSICFETCH_API_KEY", ""))
                if _mf_key and (inferred_title or stem):
                    _spotify_id_for_mf = ""
                    try:
                        _sp_cache_path = config.BASE_DIR / "_spotify_metadata_cache.json"
                        if _sp_cache_path.exists():
                            _sp_cache = json.loads(
                                _sp_cache_path.read_text(encoding="utf-8")
                            )
                            _query_artist = gc._norm(_album_aa)
                            _query_title  = gc._norm(inferred_title or stem)
                            for _sp_id, _sp_entry in _sp_cache.items():
                                if (gc._norm(_sp_entry.get("artist", "")) == _query_artist
                                        and gc._norm(_sp_entry.get("title", "")) == _query_title):
                                    _spotify_id_for_mf = _sp_id
                                    break
                    except Exception:
                        pass

                    if _spotify_id_for_mf:
                        try:
                            from genre_client import _musicfetch
                            _mf = _musicfetch(
                                _spotify_id_for_mf,
                                artist=_album_aa,
                                title=inferred_title or stem,
                            )
                            _mf_genre = (_mf.get("genre") or "").strip()
                            _mf_year  = (_mf.get("year")  or "").strip()
                            if _mf_genre and not gc._is_broad(_mf_genre):
                                _resolved_genre = _mf_genre
                                _resolved_year  = _mf_year
                                log.debug(
                                    f"  [Genre/MusicFetch] {_album_aa!r} → "
                                    f"{_resolved_genre!r} (Apple Music taxonomy)"
                                )
                        except Exception as _mfe:
                            log.debug(f"  [Genre/MusicFetch] failed: {_mfe}")

                # Step 2: Last.fm + Deezer (confidence-filtered) using album_artist + title
                if not _resolved_genre:
                    _live = gc._fast_lookup(_album_aa, inferred_title or stem)
                    _live_genre = (_live.get("genre") or "").strip()
                    _live_year  = (_live.get("year")  or "").strip()

                    if _live_genre and not gc._is_broad(_live_genre):
                        _resolved_genre = _live_genre
                        _resolved_year  = _live_year
                        log.debug(
                            f"  [Genre/Live] {_album_aa!r} → {_resolved_genre!r} "
                            f"(Last.fm/Deezer)"
                        )

                # Step 3: artist override table.
                # Applied when:
                #   a) live API returned nothing or a broad genre, OR
                #   b) live API returned something but the override is MORE specific
                #      (catches cases like Kali Uchis → Last.fm returns "Afrobeats"
                #      from a single crowdsourced tag on one track, while the override
                #      table knows she's Alt R&B/Neo Soul)
                _ov = gc._artist_override(_album_aa, inferred_title or stem)
                if _ov:
                    if (not _resolved_genre
                            or gc._is_broad(_resolved_genre)
                            or not gc._is_more_specific(_resolved_genre, _ov)):
                        _resolved_genre = _ov
                        log.debug(f"  [Genre/Override] {_album_aa!r} → {_resolved_genre!r}")

                # Step 4: fallback to per-track genre cache
                if not _resolved_genre:
                    _cache_hit = _genre_cache_lookup(
                        _album_aa,
                        inferred_title or stem,
                    )
                    _resolved_genre = (_cache_hit.get("genre") or "").strip()
                    if not _resolved_year:
                        _resolved_year = (_cache_hit.get("year") or "").strip()
                    if _resolved_genre:
                        log.debug(f"  [Genre/Cache] {_album_aa!r} → {_resolved_genre!r} (track cache fallback)")

                # Step 5: folder-name genre hint.
                # Playlist/compilation folders downloaded from YouTube/SoundCloud
                # often embed genre keywords in the folder name itself, e.g.
                # "3 Step Afro House 2026 Hits", "The Best Bachatas 💃🏻",
                # "French Afrobeats 🇫🇷🔥".  Parse these as a last resort before
                # burning an AI call — free, instant, surprisingly effective.
                if not _resolved_genre:
                    _folder_name = path.parent.name if path.parent else ""
                    _folder_norm = _folder_name.lower()
                    # Ordered from most-specific to most-broad so the first match wins
                    _FOLDER_GENRE_KEYWORDS: list[tuple[str, str]] = [
                        # ── African ──────────────────────────────────────────
                        ("afro house",      "Afro House"),
                        ("3 step",          "Afro House"),   # "3 Step Afro House"
                        ("amapiano",        "Amapiano"),
                        ("gqom",            "Gqom"),
                        ("kwaito",          "Kwaito"),
                        ("bongo flava",     "Bongo Flava"),
                        ("afrobeats",       "Afrobeats"),
                        ("afropop",         "Afrobeats"),
                        ("french afro",     "Afrobeats"),    # "French Afrobeats"
                        # ── Latin / Caribbean ─────────────────────────────────
                        ("bachata",         "Bachata"),
                        ("kompa",           "Kompa"),
                        ("reggaeton",       "Reggaeton"),
                        ("dancehall",       "Dancehall"),
                        # ── Hip-Hop / Rap (regional folders) ─────────────────
                        # Specific city/region names that imply Detroit Rap
                        ("detroit",         "Detroit Rap"),
                        ("flint",           "Detroit Rap"),
                        ("michigan rap",    "Detroit Rap"),
                        ("midwest rap",     "Midwest Hip-Hop"),
                        ("chicago rap",     "Drill"),
                        ("ny rap",          "East Coast Rap"),
                        ("new york rap",    "East Coast Rap"),
                        ("uk rap",          "UK Rap"),
                        ("uk drill",        "UK Drill"),
                        ("drill",           "Drill"),
                        ("trap",            "Trap"),
                        # Generic rap/hip-hop — less specific, comes after regional
                        ("rap 202",         "Hip-Hop"),      # "RAP 2024", "RAP 2025" etc.
                        (" rap",            "Hip-Hop"),      # trailing "rap" in folder name
                        ("hip hop",         "Hip-Hop"),
                        ("hip-hop",         "Hip-Hop"),
                        # ── R&B / Soul ────────────────────────────────────────
                        ("r&b",             "R&B"),
                        ("soul",            "Soul"),
                        ("funk",            "Funk"),
                        # ── Electronic ───────────────────────────────────────
                        ("phonk",           "Phonk"),
                        ("lo-fi",           "Lo-Fi"),
                        ("lofi",            "Lo-Fi"),
                        # ── Jazz ─────────────────────────────────────────────
                        ("jazz",            "Jazz"),
                        # ── Asian ────────────────────────────────────────────
                        ("city pop",        "City Pop"),
                        ("j-pop",           "J-Pop"),
                        ("k-pop",           "K-Pop"),
                    ]
                    for _kw, _genre_from_folder in _FOLDER_GENRE_KEYWORDS:
                        if _kw in _folder_norm:
                            _resolved_genre = _genre_from_folder
                            log.debug(
                                f"  [Genre/Folder] {_album_aa!r} → {_resolved_genre!r} "
                                f"(keyword {_kw!r} in folder {_folder_name!r})"
                            )
                            break

                # Step 6: AI genre inference for niche artists that no API knows.
                # Groq (llama-3.3-70b) has broad music knowledge and handles
                # regional/underground artists better than Last.fm or Deezer.
                # Only called when everything else has failed — this is the
                # true last resort.
                if not _resolved_genre and _album_aa:
                    try:
                        import ai_helper as _ai_h
                        _ai_result = _ai_h.infer_genre(
                            artist=_album_aa,
                            title=inferred_title or stem,
                            album=_album_nm,
                        )
                        _ai_genre = (_ai_result.get("genre") or "").strip()
                        _ai_year  = (_ai_result.get("year")  or "").strip()
                        if _ai_genre and not gc._is_broad(_ai_genre):
                            _resolved_genre = _ai_genre
                            if _ai_year and not _resolved_year:
                                _resolved_year = _ai_year
                            log.debug(
                                f"  [Genre/AI] {_album_aa!r} → {_resolved_genre!r} "
                                f"(Groq inference)"
                            )
                    except Exception as _aie:
                        log.debug(f"  [Genre/AI] inference failed for {_album_aa!r}: {_aie}")

            except Exception as _ge:
                log.debug(f"  [Genre/Live] lookup failed for {_album_aa!r}: {_ge}")

            # Store in album cache so remaining tracks skip all lookups
            if _album_genre_key and _resolved_genre:
                self._album_genre_cache[_album_genre_key] = _resolved_genre

            if _resolved_genre:
                genre_hit["genre"] = _resolved_genre
            if _resolved_year:
                genre_hit["year"] = _resolved_year

            # MK28 E1-T3 (A4): Confidence gate — do not write genre if confidence
            # is below threshold AND there was no Phase 1 (Last.fm/Deezer) hit.
            # CTO: "no confidence gating = silent wrong genre writes."
            # A4 winner: write only if confidence≥0.75 OR Phase1 hit exists.
            # MK39-X1 (audit-bug fix, 2026-05-09 Council directive):
            # This code path was missing the stem-parsing prologue that defines
            # `_mk_artist` (present in the metakey-validation block ~line 2799),
            # AND was referencing `_stem` which doesn't exist in this scope (the
            # variable here is named `stem`, no underscore). Both bugs traced to
            # a copy-paste from the lower block without name-translation.
            #
            # Council policy applied: per the MK38+MK39 architectural principle
            # *"absence of evidence is not evidence of absence, and not license to fabricate"*,
            # when artist context is unknown we initialize `_mk_artist` to
            # `_album_aa or ""`. With an empty artist key,
            # `_genre_cache_lookup()` correctly returns `{}` → downstream
            # `_has_source`/`_has_conf` gating naturally skips the write →
            # the field remains unknown, which is the desired behavior for
            # user-authored or no-metadata-source tracks.
            _mk_artist    = _album_aa or ""
            _cache_entry  = _genre_cache_lookup(_mk_artist, inferred_title or stem)
            # MK30 F2-T1 (A1): Legacy cache entries = unknown provenance artifacts, NOT Groq outputs.
            # CTO: "assigning 0.70 artificially biases toward LLM distrust and retrofits history."
            # Entries with no "source" field could be from LastFm, an old override, or a deleted pipeline.
            # Correct model: confidence=None + source_type="legacy_unknown" + re_evaluation_required.
            _has_source    = bool(_cache_entry.get("source")) if _cache_entry else False
            _has_conf      = "confidence" in (_cache_entry or {})
            _legacy_unknown = _cache_entry is not None and not _has_source and not _has_conf
            if _legacy_unknown:
                _entry_conf        = None  # truly unknown — don't pretend we know the probability
                _entry_type        = "legacy_unknown"
                _entry_evaluability = "unknown"
                # MK31 M2-T1 (A1): Reeval zombie prevention — check attempt count + backoff.
                # CTO: "bad entries become immortal queue zombies if Phase1 repeatedly fails."
                import time as _t_m2
                _reeval_attempts  = int((_cache_entry or {}).get("reeval_attempts", 0))
                _last_reeval_at   = float((_cache_entry or {}).get("last_reeval_at", 0))
                _backoff_schedule = [0, 86400, 259200, 604800, 1209600]  # 0, 1d, 3d, 7d, 14d
                _max_attempts     = 5
                if _reeval_attempts >= _max_attempts:
                    log.warning(
                        f"  [Audit/M2] REEVAL_ZOMBIE_PREVENTED: max attempts ({_max_attempts}) "
                        f"reached for legacy_unknown entry. Escalating to manual_review_required."
                    )
                    _entry_type = "manual_review_required"  # no auto-retry
                else:
                    # MK32 N2-T1 (A2): Multiplicative jitter ±20% on backoff intervals.
                    # CTO: "day-7 retry can become accidental DDoS against yourself when
                    # 4500 entries fail in the same outage window."
                    # Multiplicative jitter scales with backoff size; preserves 0 immediate.
                    import random as _rand_n2
                    _base_gap     = _backoff_schedule[min(_reeval_attempts, len(_backoff_schedule)-1)]
                    _jitter       = _rand_n2.uniform(0.8, 1.2)  # ±20%
                    _required_gap = _base_gap * _jitter
                    _elapsed      = _t_m2.time() - _last_reeval_at
                    if _base_gap > 0:
                        log.debug(
                            f"  [Audit/N2] JITTER_APPLIED: base={_base_gap}s jitter={_jitter:.2f} "
                            f"effective_gap={_required_gap:.0f}s"
                        )
                    if _elapsed < _required_gap:
                        # Still in backoff window — skip this reeval pass
                        _entry_type = "legacy_unknown_backoff"
                        log.debug(
                            f"  [Audit/M2] Reeval backoff: attempt {_reeval_attempts}, "
                            f"need {_required_gap:.0f}s gap, elapsed {_elapsed:.0f}s — skipping."
                        )
                    else:
                        log.debug(
                            f"  [Audit/F2-T1] LEGACY_UNKNOWN_REEVAL_QUEUED (attempt {_reeval_attempts+1}/{_max_attempts}) "
                            f"— no source/confidence, will re-evaluate on next Phase1 run."
                        )
            else:
                _entry_conf = float(_cache_entry.get("confidence") or 0.70) if _cache_entry else 0.0
            _entry_source = str(_cache_entry.get("source", "legacy")).lower() if _cache_entry else "legacy"
            _has_phase1   = any(s in _entry_source for s in ("lastfm","last.fm","deezer","phase1","legacy"))
            # MK31 M1-T1+M1-T2: Gate uses authority tier + evaluability.
            # CTO: "verifiable ≠ trustworthy — Last.fm crowd tags bypass gate incorrectly."
            # Tiered bypass: curated/community_curated → always bypass (editorial authority).
            # crowd_contributed → bypass only if Phase1 also confirms (crowd tags need corroboration).
            _entry_type        = str(_cache_entry.get("_source_type", "heuristic")).lower() if _cache_entry else "heuristic"
            _entry_evaluability= str(_cache_entry.get("evaluability", "assumed")).lower() if _cache_entry else "assumed"
            # MK32 N1-T1: Read authority policy from config (single source of truth).
            # CTO: "authority rules becoming implicit law — needs centralization."
            _entry_authority = str(_cache_entry.get("authority", "llm_generated")).lower() if _cache_entry else "llm_generated"
            try:
                _policy = __import__("config").get_authority_policy(_entry_authority)
                _bypass_needs_corroboration = bool(_policy["bypass_requires_corroboration"])
                _allows_auto_write           = bool(_policy["allows_auto_write"])
            except Exception:
                # Defensive: fall back to most restrictive policy
                _bypass_needs_corroboration = True
                _allows_auto_write           = False
            # External metadata can bypass IF policy allows it without corroboration,
            # OR if corroboration is required AND Phase1 hit confirms.
            _is_external = (
                _entry_type == "external_metadata"
                and _allows_auto_write
                and (not _bypass_needs_corroboration or _has_phase1)
            )
            _genre_blocked = (
                _resolved_genre
                and (_entry_conf or 0) < 0.75  # F2-T1: None = unknown, treat as 0
                and not _has_phase1
                and not _is_external
            )
            if _is_external and _resolved_genre:
                log.debug(
                    f"  [Audit/F1-T2] External metadata bypass: "
                    f"evaluability={_entry_evaluability!r} source={_entry_type!r} "
                    f"(not confidence={_entry_conf!r}) → writing {_resolved_genre!r}"
                )
            # MK33 O1-T2 (A3): Record decision_trace for reproducibility.
            # CTO: "given this evidence and this policy version, why was this decision made?"
            try:
                import config as _cfg_o1
                _decision_trace = {
                    "policy_version":  _cfg_o1.POLICY_VERSION,
                    "authority_used":  _entry_authority,
                    "rule_fired":      "blocked" if _genre_blocked else (
                                         "external_bypass" if _is_external else "passed_gate"
                                       ),
                    "evidence_count":  len((_cache_entry or {}).get("contested_genres") or [_resolved_genre]),
                    "ts":              int(__import__("time").time()),
                }
                if _cache_entry is not None:
                    _cache_entry["decision_trace"] = _decision_trace
            except Exception:
                pass

            # MK33 O5-T1 (A1): Build ResolvedView projection for contested entries.
            # CTO: "biggest remaining contradiction — collapse to one value loses
            # all the evidence we model upstream."
            # FOUNDATION step: write projection alongside chosen genre. Backward compat.
            try:
                if _cache_entry is not None and _resolved_genre:
                    import genre_client as _gc_o5
                    _is_contested = _cache_entry.get("_source_type") == "contested_external"
                    if _is_contested or _entry_type == "external_metadata":
                        _phase1_ev = {
                            "source":       _entry_source,
                            "value":        _resolved_genre,
                            "authority":    _entry_authority,
                            "confidence":   _entry_conf,
                            "evaluability": _entry_evaluability,
                        }
                        _ai_ev = None
                        if _is_contested:
                            _contested_genres = _cache_entry.get("contested_genres", [])
                            if len(_contested_genres) >= 2:
                                _ai_ev = {
                                    "source":       "secondary_external",
                                    "value":        _contested_genres[1],
                                    "authority":    "external_metadata",
                                    "confidence":   None,
                                    "evaluability": "verifiable",
                                }
                        _severity = _cache_entry.get("conflict_severity", "trivial")
                        _decision_path = (
                            "external_bypass" if _is_external
                            else "blocked" if _genre_blocked
                            else "passed_gate"
                        )
                        _rv = _gc_o5.build_resolved_view(
                            chosen_genre=_resolved_genre,
                            phase1_evidence=_phase1_ev,
                            ai_evidence=_ai_ev,
                            decision_path=_decision_path,
                            severity=_severity,
                        )
                        _cache_entry["resolved_view"] = _rv.to_dict()
            except Exception as _o5e:
                pass  # projection is additive; never block the audit pipeline

            if _genre_blocked:
                # MK39-X1 part B (audit-bug fix, 2026-05-09 Council directive):
                # Same copy-paste class as the line-2489 fix — `_stem` here
                # was wrong (the variable in `_audit_one`'s scope is `stem`,
                # defined at line 2210 as `stem = path.stem`). T4 AST regression
                # test caught this second occurrence. Fix matches the policy.
                log.info(
                    f"  [Audit/E1-T3] GENRE_WRITE_SKIPPED_LOW_CONFIDENCE "
                    f"{inferred_artist or stem!r} — "
                    f"confidence={_entry_conf if _entry_conf is not None else 'None'} "
                    f"source={_entry_source!r} (threshold=0.75). "
                    f"Leaving existing tag unchanged."
                )
                genre_hit.pop("genre", None)

            # Write back to the per-track genre cache so Spotify-sync runs
            # also benefit from this result in future.
            # Always apply the override correction before saving so stale
            # wrong genres are never persisted to disk.
            if _resolved_genre and inferred_title and inferred_title != stem:
                try:
                    _ck = gc._cache_key(_album_aa, inferred_title)
                    _gc_disk = gc._load_genre_cache()
                    _existing = _gc_disk.get(_ck, {})
                    _updated  = dict(_existing)
                    # Check override one more time before writing
                    _write_genre = _resolved_genre
                    _ov_check = gc._ARTIST_GENRE_OVERRIDES.get(
                        gc._norm(_album_aa), ""
                    )
                    if _ov_check and not _is_more_specific(_resolved_genre, _ov_check):
                        _write_genre = _ov_check
                    _updated["genre"] = _write_genre
                    if _resolved_year and not _updated.get("year"):
                        _updated["year"] = _resolved_year
                    _gc_disk[_ck] = _updated
                    gc._save_genre_cache(_gc_disk)
                except Exception:
                    pass
        else:
            # No artist name available — fall back to per-track cache only
            genre_hit = _genre_cache_lookup(
                inferred_artist or stem,
                inferred_title  or stem,
            )

        # Derive folder_name from the file's parent directory so compilation /
        # swap logic can fire on local files that live in named folders.
        # e.g. Downloads/DJ VIC PRESENTS_ CLUB CHOPS VOL. 1/WHAT A DAY - ...mp3
        _local_folder_name = path.parent.name if path.parent else ""

        # If the current artist tag contains a known watermark fragment, force
        # re-processing even if the state entry is fresh (the previous audit run
        # only fixed the ID3 version and stamped the state, leaving the bad tag).
        _cur_artist_local = (current.get("artist") or "").strip()
        _artist_is_watermarked_local = any(
            w in _cur_artist_local.lower() for w in _WATERMARK_TAG_FRAGMENTS
        )
        if _artist_is_watermarked_local:
            # Evict the stale state entry so _is_stale() returns True on next
            # run too, in case this run's write fails.
            self.state.pop(state_key, None)
            log.debug(
                f"  [WatermarkForce] {path.name}: artist tag is watermarked "
                f"({_cur_artist_local!r}), forcing re-audit"
            )

        expected = _expected_tags(current, genre_hit, stem, folder_parts=[], folder_name=_local_folder_name)
        diffs    = _build_diff(current, expected)

        # ID3 version mismatch counts as a needed fix even if no tag changed
        needs_version_fix = (
            path.suffix.lower() == ".mp3"
            and _id3_version(path) not in (0, config.ID3_VERSION)
        )

        # HiRes compat pass: re-process existing art through baseline-JPEG normaliser
        if config.HIRES_COMPAT_PASS and _has_cover_art(path, path.suffix.lower() == ".mp3"):
            if _reprocess_existing_art(path, path.suffix.lower() == ".mp3"):
                if not diffs and not needs_version_fix:
                    diffs = [("cover_art", "existing", "reprocessed (hires compat)")]

        if not diffs and not needs_version_fix:
            self.stats["skipped"] += 1
            self.state[state_key] = datetime.datetime.now().isoformat()
            return False

        # Apply changes
        ok = _write_tags(path, expected, is_mp3=path.suffix.lower() == ".mp3")
        if ok:
            self.stats["changed"] += 1
            # Build report entry — show all fields, changed in colour
            _RED   = "\033[31m"
            _GREEN = "\033[32m"
            _DIM   = "\033[2m"
            _RESET = "\033[0m"
            _changed_fields = {f for f, _, _ in diffs}
            _diff_map       = {f: (o, n) for f, o, n in diffs}
            lines = [f"CHANGED  {state_key}"]
            _ALL_DISPLAY_FIELDS = [
                "title", "artist", "album_artist", "album",
                "track_number", "disc_number", "release_year", "genre",
            ]
            for _fld in _ALL_DISPLAY_FIELDS:
                if not config.AUDIT_FIELDS.get(_fld):
                    continue
                if _fld in _changed_fields:
                    _o, _n = _diff_map[_fld]
                    lines.append(
                        f"  {_fld:<14}: {_RED}{repr(_o):<30}{_RESET} -> {_GREEN}{repr(_n)}{_RESET}"
                    )
                else:
                    _cur = (current.get(_fld) or "").strip()
                    lines.append(f"  {_DIM}{_fld:<14}: {repr(_cur)}{_RESET}")
            if needs_version_fix:
                lines.append(f"  {'id3_version':<14}: {_RED}non-v2.{config.ID3_VERSION}{_RESET} -> {_GREEN}v2.{config.ID3_VERSION}{_RESET}")
            self.report.append("\n".join(lines))
            self._log("\n".join(lines))
        else:
            self.stats["errors"] += 1

        # MK28 E3-T1 (A3): Only stamp state when is_device=True.
        # CTO: "processed ≠ correct." Local fix ≠ Walkman device fix.
        with self._state_lock:
            _stamp_local = getattr(__import__("config"), "AUDIT_STAMP_LOCAL_METAKEY", False)
            if not is_device and not _stamp_local:
                log.debug(
                    f"  [Audit/E3-T1] Metakey NOT stamped for local file "
                    f"(AUDIT_STAMP_LOCAL_METAKEY=False) — device copy still needs audit: "
                    f"{pathlib.Path(state_key).name}"
                )
                # MK29 G4-T2: Mark device_status=pending so next audit prioritises this track
                if ok:
                    try:
                        import sync_state as _ss_g4
                        _ss_g4.mark_local_fixed(state_key)
                        log.debug(
                            f"  [Audit/G4-T2] device_status=pending for {pathlib.Path(state_key).name}"
                        )
                    except Exception as _g4e:
                        log.debug(f"  [Audit/G4-T2] sync_state update failed: {_g4e}")
            else:
                self.state[state_key] = datetime.datetime.now().isoformat()
        return ok

    # -----------------------------------------------------------------------
    # Device batch audit (original mode)
    # -----------------------------------------------------------------------

    def _audit_one_device_file(
        self,
        ef: dict,
        prefetched_meta: dict | None = None,
        prefetched_flac: pathlib.Path | None = None,
        prefetched_tmp:  tuple | None = None,   # (tmp_dir, tmp_file) pre-read by pipeline
        cleanup_tmp:     bool = True,
    ) -> tuple[bool, bool, bool, pathlib.Path | None]:
        """
        Process one device file: enrich tags, push back if changed.
        If prefetched_tmp is provided, skips the MTP read (already done by prefetch thread).

        Returns (was_audited: bool, was_changed: bool, was_upgraded: bool, final_path: Path|None).
        final_path is the local temp file after any in-place renames (canonical rename may have
        moved it within tmp_dir).  Folder audit uses this to upload the correct file.

        Fast-path skip logic (no MTP pull needed):
          1. Compute expected metakey from local data (filename stem + genre cache + album cache).
          2. Look up the stored metakey in .audit_state.json.
          3. If they match → tags are known-correct → log the stored metakey and skip.
          4. If no metakey or mismatch → fall through to full audit.
        This avoids the MTP read + retag + write cycle for files we already know are clean.
        """
        # ── Metakey fast-path: skip if tags are already confirmed correct ─────
        _stem    = pathlib.Path(ef["filename"]).stem
        # Parse stem into artist / title for genre cache lookup.
        # Canonical format: "Artist - Album - NN Title"  (3+ parts)
        # Legacy format:    "NN Artist - Title"           (2 parts)
        import re as _re_mk
        def _strip_num_mk(s: str) -> str:
            return _re_mk.sub(r"^\d{1,3}[\s.\-]+", "", s).strip() or s
        _stem_parts = [p.strip() for p in _stem.split(" - ")]
        # MK39 defensive defaults (Council 2026-05-09 §2): initialize ALL
        # locals used downstream BEFORE any conditional. Production runs
        # showed `NameError: name '_mk_artist' is not defined` even though
        # all three if/elif/else branches set it — implies a stray
        # downstream reference outside this scope, OR a deployed copy that
        # diverges from this source. Defensive defaults make the variable
        # safe to reference regardless of which branch executed (or didn't).
        # This pattern is mandated by MK39-C2 governance linter for all
        # audit-path log parsing going forward.
        _mk_artist = ""
        _mk_title  = _strip_num_mk(_stem)
        if len(_stem_parts) >= 3:
            _mk_artist = _stem_parts[0]
            _mk_title  = _strip_num_mk(_stem_parts[-1])
        elif len(_stem_parts) == 2:
            _mk_artist = _strip_num_mk(_stem_parts[0])
            _mk_title  = _strip_num_mk(_stem_parts[1])
        # else: defaults already set above
        _gh      = _genre_cache_lookup(_mk_artist, _mk_title)
        _am      = self._album_meta.get(
            ef["parts"][-1] if ef.get("parts") else "", {}
        )
        _exp_mk  = _compute_metakey(_stem, _gh, _am)
        _stored  = self.state.get(ef["key"])
        _stored_mk = _stored.get("mk") if isinstance(_stored, dict) else None

        # ── Watermark / compilation eviction ─────────────────────────────────
        # If the device folder is a known compilation folder, or the filename
        # stem starts with a watermark-app prefix embedded in the tag (e.g.
        # "soundcloudaud.com 500..."), the stored metakey was computed from
        # bad tags and must NOT be trusted for a fast-path skip.
        # We detect this from the filename/folder name alone — no MTP pull needed.
        _ef_folder = ef["parts"][-1] if ef.get("parts") else ""
        _ef_stem_lc = _stem.lower()
        _is_comp_ef = _is_compilation_folder(_ef_folder)
        _stem_has_watermark = any(w in _ef_stem_lc for w in _WATERMARK_TAG_FRAGMENTS)
        if (_is_comp_ef or _stem_has_watermark) and _stored_mk:
            log.debug(
                f"  [WatermarkEvict] Evicting stored metakey for {ef['filename']!r} "
                f"(compilation={_is_comp_ef}, stem_watermark={_stem_has_watermark})"
            )
            _stored_mk = None
            self.state.pop(ef["key"], None)

        if _stored_mk and _stored_mk == _exp_mk:
            log.debug(
                f"  [MetaKey] SKIP {ef['filename']} | "
                f"stored_mk={_stored_mk!r} == expected_mk={_exp_mk!r} | "
                f"genre={(_gh.get('genre') or '?')!r} year={(_gh.get('year') or _am.get('year') or '?')!r} | "
                f"ts={_stored.get('ts','?')[:19]}"
            )
            with self._stats_lock:
                self.stats["skipped"] += 1
            if prefetched_tmp is not None:
                _ptd, _ = prefetched_tmp
                if _ptd and cleanup_tmp:
                    shutil.rmtree(_ptd, ignore_errors=True)
            return False, False, False, None

        if _stored_mk and _stored_mk != _exp_mk:
            log.debug(
                f"  [MetaKey] STALE {ef['filename']} | "
                f"stored_mk={_stored_mk!r} != expected_mk={_exp_mk!r} | "
                f"genre_now={(_gh.get('genre') or '?')!r} year_now={(_gh.get('year') or _am.get('year') or '?')!r}"
            )
        elif not _stored_mk:
            log.debug(
                f"  [MetaKey] NEW {ef['filename']} | "
                f"no stored metakey — will audit | expected_mk={_exp_mk!r}"
            )

        # Fall through to timestamp staleness check (secondary guard)
        if not _is_stale(self.state, ef["key"]):
            with self._stats_lock:
                self.stats["skipped"] += 1
            return False, False, False, None

        filename     = ef["filename"]
        parts        = ef["parts"]
        file_storage = ef.get("storage", config.WRITE_STORAGE)

        # Use pre-fetched file if available, otherwise read now
        if prefetched_tmp is not None:
            tmp_dir, tmp_file = prefetched_tmp
            if tmp_dir is None:
                # Prefetch failed
                with self._stats_lock:
                    self.stats["errors"] += 1
                return False, False, False, None
            found = tmp_file is not None and tmp_file.exists()
        else:
            tmp_dir  = pathlib.Path(tempfile.mkdtemp())
            tmp_file = tmp_dir / filename
            # Wrap the MTP pull in a thread with a hard timeout.
            # When the MTP stack degrades after hours of use, CopyHere can
            # block for the full 10-minute COPY_TIMEOUT_S.  A 120s cap keeps
            # the audit moving even on a degraded connection.
            _inline_found = [False]
            _inline_exc   = [None]
            def _inline_pull():
                try:
                    _inline_found[0] = mtp.read_file_from_device(
                        src_parts    = parts,
                        filename     = filename,
                        dest_local   = tmp_file,
                        device_name  = config.DEVICE_NAME,
                        storage_name = file_storage,
                    )
                except Exception as _exc:
                    _inline_exc[0] = _exc
            _pull_t = threading.Thread(target=_inline_pull, daemon=True)
            _pull_t.start()
            _pull_t.join(timeout=120)
            if _pull_t.is_alive():
                log.warning(f"  [Audit Pull Timeout] {filename}: MTP read took >120s, skipping")
                if cleanup_tmp:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return False, False, False, None
            if _inline_exc[0]:
                log.warning(f"  [Audit Pull Failed] {filename}: {_inline_exc[0]}")
                with self._stats_lock:
                    self.stats["errors"] += 1
                if cleanup_tmp:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return False, False, False, None
            found = _inline_found[0]

        # ── Resolve actual local path ─────────────────────────────────────────
        # ZX500 strips extensions from item.Name so the file may land without
        # extension. Always ensure the file has the correct extension before
        # passing to mutagen — mutagen uses the suffix to pick the right parser.
        actual_path = None
        expected_ext = pathlib.Path(filename).suffix.lower()

        if found and tmp_file is not None and tmp_file.exists() and tmp_file.stat().st_size > 0:
            actual_path = tmp_file
        elif tmp_file is not None:
            # Try stem-only (no extension) — rename it to include the extension
            stem_path = tmp_file.with_suffix("")
            if stem_path.exists() and stem_path.stat().st_size > 0:
                try:
                    stem_path.rename(tmp_file)
                    actual_path = tmp_file
                except Exception:
                    # Rename failed (e.g. cross-device) — add extension by copy
                    try:
                        shutil.copy2(stem_path, tmp_file)
                        stem_path.unlink()
                        actual_path = tmp_file
                    except Exception:
                        actual_path = stem_path

            # Scan the whole tmp dir for any audio file if still not found
            if actual_path is None:
                audio_exts = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav",
                              ".opus", ".wma", ".aiff", ".dsf", ".dff"}
                for f in sorted(tmp_dir.iterdir()):
                    if not f.is_file() or f.stat().st_size == 0:
                        continue
                    if f.suffix.lower() in audio_exts:
                        actual_path = f
                        break
                    if f.suffix == "" and expected_ext:
                        # Extension-stripped — rename to expected
                        target = f.parent / (f.name + expected_ext)
                        try:
                            f.rename(target)
                            actual_path = target
                        except Exception:
                            actual_path = f
                        break

        if actual_path is None or not actual_path.exists():
            log.warning(f"  [Audit Not Found] {filename}")
            with self._stats_lock:
                self.stats["errors"] += 1
            if cleanup_tmp:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return False, False, False, None

        # If the file has no extension but we know what it should be, fix it now
        if not actual_path.suffix and expected_ext:
            fixed = actual_path.parent / (actual_path.name + expected_ext)
            try:
                actual_path.rename(fixed)
                actual_path = fixed
            except Exception:
                pass

        with self._stats_lock:
            self.stats["audited"] += 1

        # ── Read current tags ─────────────────────────────────────────────────
        current_tags = _read_tags(actual_path)
        artist_tag   = (current_tags.get("artist")       or "").strip()
        title_tag    = (current_tags.get("title")        or actual_path.stem).strip()
        album_tag    = (current_tags.get("album")        or "").strip()
        genre_tag    = (current_tags.get("genre")        or "").strip().lower()
        year_tag     = (current_tags.get("release_year") or "").strip()
        is_mp3       = actual_path.suffix.lower() == ".mp3"
        missing_genre = not genre_tag or genre_tag in ("", "unknown", "unknown genre")
        missing_year  = not year_tag

        # ── Album-level genre/year cache lookup ───────────────────────────────
        # Derive album key from folder structure (most reliable) or tags.
        # Try multiple key formats — SpotiDownloader folders are named with just
        # the album name ("Life of a DON") while _album_meta may be keyed as
        # "Don Toliver - Life of a DON". Try all variants to maximise hit rate.
        album_folder = parts[-1] if len(parts) >= 1 else ""
        album_key    = album_folder or f"{artist_tag} - {album_tag}"
        with self._album_lock:
            cached_album_meta = dict(self._album_meta.get(album_key, {}))
            # Fallback: try "artist - folder" and "artist - album" variants
            if not cached_album_meta:
                _alt_keys = [
                    f"{artist_tag} - {album_folder}",
                    f"{artist_tag} - {album_tag}",
                    album_tag,
                    album_folder,
                ]
                for _ak in _alt_keys:
                    if _ak and _ak != album_key and _ak in self._album_meta:
                        cached_album_meta = dict(self._album_meta[_ak])
                        album_key = _ak
                        break
            # Fuzzy fallback: find album_meta entry whose key ENDS WITH the folder name
            # e.g. "Don Toliver - Life of a DON" matches folder "Life of a DON"
            if not cached_album_meta and album_folder:
                _af_lower = album_folder.lower()
                for _mk, _mv in self._album_meta.items():
                    if _mk.lower().endswith(_af_lower) or _mk.lower() == _af_lower:
                        cached_album_meta = dict(_mv)
                        album_key = _mk
                        # Also learn the album_artist from this match
                        if _mv.get("album_artist") and not artist_tag:
                            artist_tag = _mv["album_artist"]
                        break

        # Folder-name artist inference: "Outkast - Aquemini" → artist = "Outkast"
        # Many SpotiDownloader folder names follow "Artist - Album" format.
        # Only apply when artist_tag is still unknown after all lookups above.
        if not artist_tag and album_folder:
            import re as _re_fn
            _fn_parts = [p.strip() for p in album_folder.split(" - ")]
            if len(_fn_parts) >= 2:
                # Take the first segment as artist candidate — but only if it
                # doesn't look like a pure album name (no spaces = acronym/codename)
                _fn_artist_cand = _fn_parts[0]
                if _fn_artist_cand and len(_fn_artist_cand) > 1 and " " in _fn_artist_cand:
                    artist_tag = _fn_artist_cand
                    log.debug(f"  [FolderInfer] artist={artist_tag!r} from folder {album_folder!r}")

        if cached_album_meta.get("genre") and missing_genre:
            log.debug(f"  [AlbumCache] genre={cached_album_meta['genre']} from album '{album_key}'")
        if cached_album_meta.get("year") and missing_year:
            log.debug(f"  [AlbumCache] year={cached_album_meta['year']} from album '{album_key}'")

        # ── TIDAL metadata enrichment (genre / year) ──────────────────────────
        # ── Infer artist/title from filename stem for lookups ────────────────
        # Many files have blank/bad tags. Parse the canonical filename format:
        # "Artist - Album - NN Title" to get real values for genre/TIDAL lookups.
        import re as _re_gl
        _fstem  = actual_path.stem   # push_path not yet assigned here; actual_path always valid
        _fparts = [p.strip() for p in _fstem.split(" - ")]
        # Strip leading track numbers from any part (e.g. "03 Kanye West" -> "Kanye West")
        def _strip_num(s):
            return _re_gl.sub(r"^\d{1,3}[\s\.\-]+", "", s).strip() or s
        if len(_fparts) >= 3:
            _lookup_artist = artist_tag or _strip_num(_fparts[0])
            _lookup_title  = _strip_num(_fparts[-1])
        elif len(_fparts) == 2:
            _lookup_artist = artist_tag or _strip_num(_fparts[0])
            _raw_t2        = _fparts[1] if title_tag == actual_path.stem else title_tag
            _lookup_title  = _strip_num(_raw_t2) or _raw_t2
        else:
            _lookup_artist = artist_tag or _strip_num(_fstem)
            _lookup_title  = title_tag  if title_tag != actual_path.stem else _strip_num(_fstem)

        tidal_meta: dict = {}
        # Use filename-parsed artist/title as fallback when tags are blank
        _tidal_artist = artist_tag or _lookup_artist
        _tidal_title  = (title_tag if title_tag != actual_path.stem else "") or _lookup_title
        # Also fetch TIDAL when this file has no cover art stamped yet (cv flag absent).
        # Old-format state entries are plain strings — treat them as missing cv.
        _state_entry = self.state.get(ef["key"])
        _missing_cover_stamp = not (
            isinstance(_state_entry, dict) and _state_entry.get("cv", False)
        )
        if (missing_genre or missing_year or _missing_cover_stamp) and _tidal_artist and _tidal_title:
            if prefetched_meta:
                tidal_meta = prefetched_meta
            elif getattr(config, "TIDAL_CLIENT_ID", ""):
                tidal_meta = _tidal_fetch_track(_tidal_artist, _tidal_title)
            # Validate TIDAL meta: if the matched artist diverges from what we expect,
            # the metadata (especially year and genre) is from the wrong track.
            # Example: searching "Burden" without artist returns Rolling Stones data.
            if tidal_meta.get("matched_artist") and _tidal_artist:
                import re as _re_tm
                def _tm_norm(s):
                    return _re_tm.sub(r"[^a-z0-9 ]","",s.lower()).strip()
                _tm_want  = _tm_norm(_tidal_artist)
                _tm_got   = _tm_norm(tidal_meta.get("matched_artist",""))
                _tm_words = set(_tm_want.split())
                _tm_ok    = (_tm_want in _tm_got or _tm_got in _tm_want or
                             (len(_tm_words) > 0 and
                              len(set(_tm_got.split()) & _tm_words)/len(_tm_words) >= 0.5))
                if not _tm_ok:
                    log.debug(
                        f"  [TIDAL meta] discarding — artist mismatch "
                        f"(want {_tidal_artist!r}, got {tidal_meta['matched_artist']!r})"
                    )
                    tidal_meta = {}
            if tidal_meta.get("genre") and missing_genre:
                log.info(f"  [TIDAL meta] genre={tidal_meta['genre']}  '{_tidal_title}'")
            if tidal_meta.get("year") and missing_year:
                log.info(f"  [TIDAL meta] year={tidal_meta['year']}   '{_tidal_title}'")

        # ── MP3 → FLAC upgrade via TIDAL download ─────────────────────────────
        # Use the pre-downloaded FLAC from the batch pre-phase if available.
        # Only hit the network here if it wasn't pre-fetched (e.g. first file
        # after a restart, or a file the pre-phase didn't recognise as MP3).
        upgraded_flac: pathlib.Path | None = None
        if is_mp3 and getattr(config, "UPGRADE_MP3_TO_FLAC", True):
            if prefetched_flac and prefetched_flac.exists() and prefetched_flac.stat().st_size > 0:
                # Sanity-check: if we now know the artist (from album_meta resolution)
                # and the prefetched FLAC has a different artist embedded, it's a wrong
                # match (e.g. "Burden" prefetched as Rolling Stones for an Outkast folder).
                # Discard it and let the inline path re-download with the correct artist.
                _prefetch_ok = True
                if _tidal_artist:
                    try:
                        _pf_tags = _read_tags(prefetched_flac)
                        _pf_artist = (_pf_tags.get("artist") or "").strip().lower()
                        _want_artist = _tidal_artist.lower()
                        import unicodedata as _ucd_pf
                        def _pf_norm(s):
                            s = _ucd_pf.normalize("NFKD", s).encode("ascii","ignore").decode().lower()
                            return re.sub(r"[^a-z0-9 ]","",s).strip()
                        _pf_norm_got  = _pf_norm(_pf_artist)
                        _pf_norm_want = _pf_norm(_want_artist)
                        if _pf_artist and _pf_norm_got and _pf_norm_want:
                            _shared = set(_pf_norm_got.split()) & set(_pf_norm_want.split())
                            _ok = (_pf_norm_got in _pf_norm_want or
                                   _pf_norm_want in _pf_norm_got or
                                   (len(set(_pf_norm_want.split())) > 0 and
                                    len(_shared)/len(set(_pf_norm_want.split())) >= 0.5))
                            if not _ok:
                                log.warning(
                                    f"  [Upgrade] Discarding prefetch {prefetched_flac.name!r}: "
                                    f"artist mismatch (got {_pf_artist!r}, want {_tidal_artist!r})"
                                )
                                _prefetch_ok = False
                    except Exception:
                        pass
                if _prefetch_ok:
                    upgraded_flac = prefetched_flac
                    log.info(f"  [Upgrade] Using pre-downloaded FLAC: {prefetched_flac.name}")
            elif _tidal_artist and _tidal_title and getattr(config, "TIDAL_CLIENT_ID", ""):
                log.info(f"  [Upgrade] Downloading FLAC: '{_tidal_artist} — {_tidal_title}'")
                dl_dir = pathlib.Path(tempfile.mkdtemp())
                try:
                    flac_path = _tidal_download_flac(_tidal_artist, _tidal_title, str(dl_dir))
                    if flac_path and flac_path.exists() and flac_path.stat().st_size > 0:
                        upgraded_flac = flac_path
                        log.info(f"  [Upgrade] Downloaded: {flac_path.name}")
                    else:
                        log.warning(f"  [Upgrade] TIDAL failed — keeping MP3")
                        shutil.rmtree(dl_dir, ignore_errors=True)
                except Exception as e:
                    log.warning(f"  [Upgrade] Download error: {e}")
                    shutil.rmtree(dl_dir, ignore_errors=True)

        # ── Which file gets tagged and pushed? ────────────────────────────────
        push_path = upgraded_flac if upgraded_flac is not None else actual_path

        # ── Enrich tags: merge album cache + TIDAL + genre cache ─────────────
        # Priority: TIDAL (most accurate) > genre cache > album cache
        genre_hit = _genre_cache_lookup(_lookup_artist, _lookup_title)

        # Album-level cross-reference: genre/year established by sibling tracks
        _album_cross = self._album_meta.get(album_key, {})
        _album_xgenre = _album_cross.get("genre", "")
        _album_xyear  = _album_cross.get("year",  "")

        # Priority order:
        #   1. genre_cache  — Last.fm/Deezer curated (most reliable, especially world music)
        #   2. TIDAL        — gated: only used when cache is empty or broad. TIDAL frequently
        #                     returns wrong genres for non-Western artists (e.g. "Alternative Rock"
        #                     for Bongo Flava, "Hip-Hop" for early-2000s R&B, "Pop" for Neo Soul).
        #                     Once genre_cache has a specific value, TIDAL never overrides it.
        #   3. album cross-ref — consensus from sibling tracks in same album folder
        #   4. Spotify/album prefill — broadest, lowest confidence
        #   5. existing tag — what's already on the device
        _cache_genre_raw = genre_hit.get("genre", "")
        _tidal_genre_raw = tidal_meta.get("genre", "")
        # Broad genres that TIDAL is allowed to improve on (cache has nothing useful yet)
        _TIDAL_GATE_BROAD = {
            "", "unknown", "unknown genre", "pop", "r&b", "rnb", "hip hop", "hip-hop",
            "rap", "rock", "electronic", "latin", "dance", "soul", "jazz",
            "african music", "african", "world music", "world", "alternative", "other",
        }
        _allow_tidal = not _cache_genre_raw or _cache_genre_raw.lower() in _TIDAL_GATE_BROAD
        _tidal_genre_candidate = _tidal_genre_raw if _allow_tidal else ""

        genre_candidates = [
            _cache_genre_raw,               # genre_cache (always first — most reliable)
            _tidal_genre_candidate,         # TIDAL (only when cache is empty/broad)
            _album_xgenre,                  # sibling consensus
            cached_album_meta.get("genre", ""),
            current_tags.get("genre", ""),
        ]
        best_genre = _best_genre(genre_candidates, artist=_lookup_artist, album=album_tag)
        if best_genre:
            if best_genre == _album_xgenre and _album_xgenre:
                log.debug(f"  [AlbumCache] genre={_album_xgenre!r} (cross-ref) for {ef['filename']}")
            genre_hit["genre"] = best_genre

        # Year: prefer TIDAL > genre_cache > album_cache > sibling cross-ref
        if not genre_hit.get("year"):
            for src in [tidal_meta, cached_album_meta]:
                if src.get("year"):
                    genre_hit["year"] = src["year"]
                    break
        if not genre_hit.get("year") and _album_xyear:
            genre_hit["year"] = _album_xyear
            log.debug(f"  [AlbumCache] year={_album_xyear!r} (cross-ref) for {ef['filename']}")
        # Final fallback: Spotify metadata cache (has spotify_year for ~2268 tracks)
        if not genre_hit.get("year"):
            try:
                _sp_path = config.BASE_DIR / "_spotify_metadata_cache.json"
                if _sp_path.exists():
                    import genre_client as _gc2
                    _sp_data = json.loads(_sp_path.read_text(encoding="utf-8"))
                    _sp_key  = f"{_gc2._norm(_lookup_artist)}|{_gc2._norm(_lookup_title)}"
                    for _tv in _sp_data.values():
                        _tk = f"{_gc2._norm(_tv.get('artist',''))}|{_gc2._norm(_tv.get('title',''))}"
                        if _tk == _sp_key:
                            _y = str(_tv.get("spotify_year", "") or "").strip()
                            if _y:
                                genre_hit["year"] = _y
                            break
            except Exception:
                pass

        # Populate album-level cache for other tracks in same album
        with self._album_lock:
            existing = self._album_meta.get(album_key, {})
            updated  = dict(existing)
            # Only upgrade album genre with a more-specific value.
            # Exclude broad TIDAL genres (Pop, R&B etc from interludes) from
            # seeding the album cache — they would corrupt sibling track lookups.
            _ALBUM_BROAD = {
                "pop", "r&b", "rnb", "hip hop", "hip-hop", "rap", "rock",
                "electronic", "latin", "soul", "jazz", "other", "unknown",
            }
            candidate_genre = genre_hit.get("genre", "")
            if (candidate_genre
                    and candidate_genre.lower() not in _ALBUM_BROAD
                    and _is_more_specific(candidate_genre, existing.get("genre", ""))):
                updated["genre"] = candidate_genre
            elif candidate_genre and not existing.get("genre"):
                # Accept any genre (even broad) if album cache is empty
                updated["genre"] = candidate_genre
            if genre_hit.get("year") and not existing.get("year"):
                updated["year"] = genre_hit["year"]
            # Cross-reference: store cover URL at album level so all tracks share it
            if tidal_meta.get("cover_url") and not existing.get("cover_url"):
                updated["cover_url"] = tidal_meta["cover_url"]
            # Also store Spotify cover URL as a lower-priority fallback
            _sp_cover = cached_album_meta.get("spotify_cover_url", "")
            if _sp_cover and not existing.get("spotify_cover_url"):
                updated["spotify_cover_url"] = _sp_cover
            # Check for local folder.jpg (once per folder) and store its path
            if not updated.get("local_cover_path") and not existing.get("cover_url"):
                # Attempt to find folder.jpg in the device folder
                try:
                    _sr = mtp._get_storage_root(config.DEVICE_NAME, file_storage)
                    _folder = mtp._navigate(_sr, parts, create=False)
                    if _folder:
                        for _item in list(_folder.Items()):
                            if _item.Name.lower() in ("folder.jpg", "cover.jpg", "folder.jpeg", "cover.jpeg"):
                                # We could download and embed, but for now store the fact that it exists
                                # (actual embedding would require pulling the file, which is heavy)
                                updated["has_local_cover"] = True
                                break
                except Exception:
                    pass
            if updated != existing:
                self._album_meta[album_key] = updated
                log.debug(f"  [AlbumCache] stored {album_key!r}: {updated}")

        current_for_push = _read_tags(push_path)
        expected = _expected_tags(current_for_push, genre_hit, push_path.stem, cached_album_meta, folder_parts=parts, folder_name=parts[-1] if parts else "")

        # ── Task 11: AcoustID/MusicBrainz fingerprint fallback (opt-in) ──────
        if getattr(config, "ACOUSTID_AUTO_CORRECT_ENABLED", False):
            if _apply_acoustid_fallback(expected, push_path):
                log.info(f"  [AcoustID] Resolved blank identity for {ef['filename']} via fingerprint")

        diffs = _build_diff(current_for_push, expected)
        needs_id3_fix = (
            push_path.suffix.lower() == ".mp3"
            and _id3_version(push_path) not in (0, config.ID3_VERSION)
        )

        # ── Cover art: embed if missing ──────────────────────────────────────
        # Source priority: TIDAL (this track) → album cache TIDAL URL →
        #                  album cache Spotify URL (fallback when TIDAL unavailable)
        _album_cache_entry = self._album_meta.get(album_key, {})
        cover_url = (
            tidal_meta.get("cover_url")
            or _album_cache_entry.get("cover_url")
            or _album_cache_entry.get("spotify_cover_url")
        )
        needs_cover  = cover_url and not _has_cover_art(push_path, push_path.suffix.lower() == ".mp3")
        if needs_cover:
            img_bytes = _fetch_cover_art(cover_url)
            if img_bytes:
                embedded = _embed_cover_art(push_path, img_bytes, push_path.suffix.lower() == ".mp3")
                if embedded:
                    log.info(f"  [CoverArt] Embedded for {ef['filename']}")
                    if not diffs and not needs_id3_fix and upgraded_flac is None:
                        diffs = [("cover_art", "missing", "embedded")]

        # HiRes compat pass: re-process existing art through baseline-JPEG normaliser
        # Only fires when art wasn't just freshly embedded (needs_cover=False) and art is present.
        if config.HIRES_COMPAT_PASS and not needs_cover and _has_cover_art(push_path, push_path.suffix.lower() == ".mp3"):
            if _reprocess_existing_art(push_path, push_path.suffix.lower() == ".mp3"):
                if not diffs and not needs_id3_fix and upgraded_flac is None:
                    diffs = [("cover_art", "existing", "reprocessed (hires compat)")]

        # ── Synced lyrics (Task 8, opt-in via config.LYRICS_ENABLED) ─────────
        if getattr(config, "LYRICS_ENABLED", False):
            _is_mp3_lyrics = push_path.suffix.lower() == ".mp3"
            try:
                import lyrics_fetch as _lf
                if not _lf.has_lyrics(push_path, _is_mp3_lyrics):
                    _duration_s = 0
                    try:
                        import mutagen as _mg_dur
                        _info = _mg_dur.File(push_path)
                        if _info is not None and _info.info is not None:
                            _duration_s = int(_info.info.length)
                    except Exception:
                        pass
                    _lyrics = _lf.fetch_synced_lyrics(
                        title=expected.get("title", ""),
                        artist=expected.get("artist", ""),
                        album=expected.get("album", ""),
                        duration_s=_duration_s,
                    )
                    if _lyrics and _lyrics.has_any:
                        _wrote_lrc = _lf.write_lrc_sidecar(push_path, _lyrics)
                        _wrote_tag = _lf.embed_plain_lyrics(push_path, _lyrics, _is_mp3_lyrics)
                        if _wrote_lrc or _wrote_tag:
                            log.info(f"  [Lyrics] Fetched for {ef['filename']} (lrc={_wrote_lrc}, tag={_wrote_tag})")
                            if not diffs and not needs_id3_fix and upgraded_flac is None:
                                diffs = [("lyrics", "missing", "fetched")]
            except Exception as e:
                log.debug(f"  [Lyrics] Fetch/embed step failed for {ef['filename']}: {e}")

        # ── Liked-song POPM stamp ─────────────────────────────────────────────
        # If this track appears in the user's Spotify liked songs and the file
        # has no POPM frame yet, write one (rating=255 = 5 stars on Walkman).
        # We do this on the local temp copy (push_path) before the push, so the
        # freshly written POPM survives the MTP transfer intact.
        # Note: _read_rating returns None when no POPM is present, so checking
        # for None is the correct "no existing rating" test.
        _track_spotify_id = (
            self.state.get(ef["key"], {}) or {}
        ).get("spotify_id", "")
        _existing_rating = _read_rating(push_path)
        # 2026-08-20: defensive default (MK39-C2 pattern) — _is_liked_track is
        # referenced unconditionally below (line ~3684) but was only assigned
        # inside this if-block. Confirmed live: when self._liked_track_ids is
        # empty (e.g. the Liked-Songs Spotify API call fails, such as a 403
        # "premium subscription required" error), this block never runs and
        # every track hit `UnboundLocalError: cannot access local variable
        # '_is_liked_track'`, aborting per-track processing after that point.
        _is_liked_track = False
        if (
            self._liked_track_ids
            and _existing_rating is None
        ):
            # Check if this track's Spotify ID is in the liked set.
            # The spotify_id is stored in state when the track was first pushed
            # by the downloader.  Fall back to a title+artist match against the
            # _spotify_metadata_cache when the ID is absent.
            _is_liked_track = bool(
                _track_spotify_id and _track_spotify_id in self._liked_track_ids
            )
            if not _is_liked_track:
                # Fallback: match via metadata cache keyed by "artist|title"
                try:
                    import genre_client as _gc_lk
                    _lk_key = f"{_gc_lk._norm(final_tags.get('artist',''))}|{_gc_lk._norm(final_tags.get('title',''))}"
                    _sp_cache_path = config.BASE_DIR / "_spotify_metadata_cache.json"
                    if _sp_cache_path.exists():
                        _sp_cache = json.loads(_sp_cache_path.read_text(encoding="utf-8"))
                        _sp_entry = _sp_cache.get(_lk_key) or {}
                        _sp_id    = _sp_entry.get("id", "")
                        if _sp_id and _sp_id in self._liked_track_ids:
                            _is_liked_track = True
                except Exception:
                    pass

            if _is_liked_track:
                _is_push_mp3 = push_path.suffix.lower() == ".mp3"
                try:
                    if _is_push_mp3:
                        _lk_id3 = ID3(push_path)
                        _lk_id3.delall("POPM")
                        _lk_id3.add(POPM(email="Spotify Liked", rating=255, count=0))
                        _lk_id3.save(v2_version=config.ID3_VERSION)
                    else:
                        _lk_flac = FLAC(push_path)
                        _lk_flac["rating"] = "255"
                        _lk_flac.save()
                    log.info(f"  [LikedTracks] Stamped POPM=255 on {ef['filename']}")
                    if not diffs and not needs_id3_fix and upgraded_flac is None:
                        diffs = [("rating", "none", "255 (liked)")]
                except Exception as _lk_e:
                    log.debug(f"  [LikedTracks] POPM stamp failed ({ef['filename']}): {_lk_e}")

        # ── Streaming-history POPM=200 stamp (4-star for heavy listeners) ────
        # Tracks played ≥20× with ≤15% skip rate that are NOT in Liked Songs
        # get POPM=200 (≈4 stars). Only fires when no existing rating is present.
        if (
            self._history_popm_uris
            and _existing_rating is None
            and not _is_liked_track   # don't downgrade 5-star liked tracks
        ):
            _hist_id = _track_spotify_id
            if not _hist_id:
                # Same metadata-cache fallback as above
                try:
                    import genre_client as _gc_sh
                    _sh_key = f"{_gc_sh._norm(final_tags.get('artist',''))}|{_gc_sh._norm(final_tags.get('title',''))}"
                    _sp_cache_path2 = config.BASE_DIR / "_spotify_metadata_cache.json"
                    if _sp_cache_path2.exists():
                        _sp_c2 = json.loads(_sp_cache_path2.read_text(encoding="utf-8"))
                        _hist_id = (_sp_c2.get(_sh_key) or {}).get("id", "")
                except Exception:
                    pass

            if _hist_id and _hist_id in self._history_popm_uris:
                _is_hist_mp3 = push_path.suffix.lower() == ".mp3"
                _hist_score  = self._history_popm_scores.get(_hist_id, 0)
                try:
                    if _is_hist_mp3:
                        _hst_id3 = ID3(push_path)
                        _hst_id3.delall("POPM")
                        _hst_id3.add(POPM(email="Spotify Liked", rating=200, count=0))
                        _hst_id3.save(v2_version=config.ID3_VERSION)
                    else:
                        _hst_flac = FLAC(push_path)
                        _hst_flac["rating"] = "200"
                        _hst_flac.save()
                    log.debug(
                        f"  [StreamingHistory] Stamped POPM=200 on {ef['filename']} "
                        f"(score={_hist_score:.0f})"
                    )
                    if not diffs and not needs_id3_fix and upgraded_flac is None:
                        diffs = [("rating", "none", f"200 (history score={_hist_score:.0f})")]
                except Exception as _sh_e2:
                    log.debug(f"  [StreamingHistory] POPM stamp failed ({ef['filename']}): {_sh_e2}")

        # ── Copy rating from original MP3 when upgrading ────────────────────
        if upgraded_flac:
            rating_val = _read_rating(actual_path)
            if rating_val is not None:
                try:
                    flac_audio = FLAC(upgraded_flac)
                    flac_audio["rating"] = str(rating_val)
                    flac_audio.save()
                    log.debug(f"  [Rating] Copied rating {rating_val} to FLAC")
                except Exception as e:
                    log.debug(f"  [Rating] Failed to copy rating: {e}")

        if diffs or needs_id3_fix or upgraded_flac is not None:
            ok = _write_tags(push_path, expected, is_mp3=push_path.suffix.lower() == ".mp3")
            if not ok and upgraded_flac is None:
                # Tag write failed and not an upgrade — skip
                with self._stats_lock:
                    self.stats["errors"] += 1
                if cleanup_tmp:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return True, False, False, push_path

        # ── Canonical rename ───────────────────────────────────────────────────
        # Only rename if we have enough real tag data — never produce
        # "Unknown - Unknown - 01 Unknown" names for files with blank tags.
        final_tags = _read_tags(push_path)
        has_real_tags = (
            (final_tags.get("artist") or "").strip() not in ("", "Unknown")
            and (final_tags.get("title")  or "").strip() not in ("", "Unknown")
            and (final_tags.get("album")  or "").strip() not in ("", "Unknown")
        )
        if has_real_tags:
            canon_name = _canonical_filename(final_tags, push_path.suffix.lower())
            canon_path = push_path.parent / canon_name
            if push_path != canon_path:
                try:
                    push_path.rename(canon_path)
                    push_path = canon_path
                except Exception:
                    pass

        # ── Check if we can simply rename the file instead of pushing ────────
        # Only rename if we have enough tag data to produce a meaningful name.
        did_upgrade  = upgraded_flac is not None
        should_push  = did_upgrade or bool(diffs) or needs_id3_fix
        if not should_push and not needs_id3_fix and not did_upgrade:
            artist = (final_tags.get("artist") or final_tags.get("album_artist") or "").strip()
            album  = (final_tags.get("album") or "").strip()
            title  = (final_tags.get("title") or "").strip()
            if (artist and album and title and
                artist not in ("", "Unknown") and
                album  not in ("", "Unknown") and
                title  not in ("", "Unknown")):
                ext = push_path.suffix.lower() if push_path else pathlib.Path(ef["filename"]).suffix.lower()
                canon_name = _canonical_filename(final_tags, ext)
                if canon_name != ef["filename"]:
                    log.info(f"  [Rename] {ef['filename']} -> {canon_name}")
                    if self.dry_run:
                        log.info(f"  [DRY-RUN] Would rename on device: {ef['filename']!r} -> {canon_name!r} — no device write")
                        if cleanup_tmp:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        return True, False, False, push_path
                    success = mtp.rename_file_on_device(
                        device_name=config.DEVICE_NAME,
                        storage_name=file_storage,
                        folder_parts=parts,
                        old_filename=ef["filename"],
                        new_filename=canon_name,
                    )
                    if success:
                        with self._state_lock:
                            old_key = ef["key"]
                            new_key = file_storage + "/" + "/".join(parts + [canon_name])
                            entry = self.state.pop(old_key, None)
                            if entry:
                                self.state[new_key] = entry
                            # Also update the ef dict for later references (optional)
                            ef["filename"] = canon_name
                            ef["key"] = new_key
                        # No need to push anything else
                        if cleanup_tmp:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        return True, False, False, push_path
                    else:
                        log.warning(f"  [Rename] failed, will retry on next run")

        # ── Guard: don't push corrupt MP3s back to device ────────────────────
        # Files with 'can't sync to MPEG frame' errors have no valid MPEG header
        # and will silently fail to register on the Walkman's MTP layer.
        # This includes no-extension files on device (e.g. "WE SAW THE SUN!")
        # which are actually MP3s without a file extension.
        # Skip the push entirely and log a warning so the user can re-download.
        _ext = push_path.suffix.lower()
        if _ext in (".mp3", "") :   # no-extension files are often bare MP3s
            try:
                from mutagen.mp3 import MP3 as _MP3_chk
                _MP3_chk(push_path)   # raises if no valid MPEG frame
            except Exception as _mp3_err:
                err_str = str(_mp3_err).lower()
                if "sync" in err_str or "mpeg" in err_str or "frame" in err_str:
                    log.warning(
                        f"  [Audit Skip] {ef['filename']}: corrupt MP3 "
                        f"({_mp3_err}) — deleting from device and skipping. Re-download this track."
                    )
                    if cleanup_tmp:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    if self.dry_run:
                        log.info(f"  [DRY-RUN] Would delete corrupt file from device: {ef['filename']!r} — no device write")
                        return True, False, False, push_path
                    # Delete the corrupt file from device so it stops appearing
                    # in scans every run. The user must re-download a clean copy.
                    try:
                        import wpd_transfer as _wpd_del
                        _del_ok = _wpd_del.delete_file_ps(
                            config.DEVICE_NAME,
                            ef["storage"],
                            ef["parts"],
                            ef["filename"],
                        )
                        if _del_ok:
                            log.warning(f"  [Audit Delete] Corrupt file removed from device: {ef['filename']}")
                        else:
                            log.warning(f"  [Audit Delete] Could not remove corrupt file — delete manually: {ef['filename']}")
                    except Exception as _del_err:
                        log.warning(f"  [Audit Delete] Delete attempt failed ({_del_err}) — delete manually: {ef['filename']}")
                    return True, False, False, push_path

        # ── Push failure backoff guard ────────────────────────────────────────
        # If this file has failed to push repeatedly, skip it for this run.
        # Inspired by spotify_to_tidal's persistent failure cache — prevents a
        # single broken FLAC or degraded MTP state from burning 90s on every run.
        if should_push and _push_fail_should_skip(ef["key"]):
            if cleanup_tmp:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return True, False, False, push_path

        if should_push:
            # 2026-08-20: this whole block does real device writes (push +
            # canonical-path move/delete) — was NOT gated by self.dry_run at
            # all (see class docstring note at __init__), unlike the
            # cross-format-prune delete path elsewhere in this file. Found
            # live: an `audit --dry-run` run pushed 100+ real tag/reorg
            # writes to the actual device. Gated here the same way as the
            # rename-only and corrupt-MP3-delete branches above — log intent,
            # skip every real write (push, canonical move, state stamp),
            # return without touching the device at all.
            if self.dry_run:
                _canon_parts_preview = _canonical_dest_parts(final_tags)
                _would_move = bool(_canon_parts_preview and _canon_parts_preview != parts)
                _dest_desc = "/".join(_canon_parts_preview) if _would_move else "/".join(parts)
                log.info(
                    f"  [DRY-RUN] Would push {ef['filename']!r} -> {file_storage}/{_dest_desc}/"
                    f"{' (canonical move from ' + '/'.join(parts) + ')' if _would_move else ''} "
                    f"— no device write"
                )
                if cleanup_tmp:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return True, True, did_upgrade, push_path
            try:
                # ── Compute canonical destination (Option C) ──────────────────
                # If tags are complete, push to Artist/Album/ instead of the
                # original folder. Zero extra MTP ops — push was happening anyway.
                # If canonical dest == original → no delete needed.
                # If canonical dest != original → push to new path, delete old.
                _canon_parts = _canonical_dest_parts(final_tags)
                _push_parts  = _canon_parts if (_canon_parts and _canon_parts != parts) else parts

                # 2026-09-05: a canonical-path move changes the file's device
                # path, which orphans its is_favorites row and breaks the M3U8
                # playlists' relative paths on the next rescan (confirmed live:
                # an audit run moved 538 splinter-folder files and broke 203
                # favorites + 310 playlist entries). Favorites/playlists are
                # new this session and the move logic never had to know about
                # them. If this exact file is favorited or playlisted, pin it
                # to its current folder -- tags still get fixed in place, it
                # just doesn't relocate. (Same protection walkman_dedup already
                # applies to deletions.)
                try:
                    import walkman_dedup as _wd_prot
                    _cur_hp = f"{file_storage}/" + "/".join(parts) + "/" + ef["filename"]
                    if _cur_hp.replace("\\", "/").strip() in _wd_prot._load_favplaylist_protected_paths():
                        if _push_parts != parts:
                            log.info(f"  [Reorg] PINNED (favorited/playlisted): "
                                     f"{ef['filename']!r} stays in {'/'.join(parts)}/")
                        _push_parts = parts
                except Exception as _prot_e:
                    log.debug(f"  [Reorg] favplaylist protection check skipped: {_prot_e}")
                # 2026-08-22: was `_canon_moved = (_canon_parts != parts)` — folder-only
                # comparison. push_path's FILENAME may already have been canonically
                # renamed in-place above (push_path.rename(canon_path)) even when the
                # folder is unchanged. Pushing the new filename into the same folder
                # with the old filename's delete gated on "folder changed" left the
                # old-named file behind as an orphan every time a track needed a tag
                # fix but was already in its correct folder — confirmed live: 87
                # duplicate pairs (canonical-named + orig-named) across whole MF DOOM
                # albums. Compare the full old vs. new device path (folder+filename)
                # instead, so a filename-only change also triggers delete-of-original.
                _canon_moved = (tuple(_push_parts) + (push_path.name,)) != (tuple(parts) + (ef["filename"],))

                # Clear post-write event so prefetch worker waits during this push
                try:
                    _post_write_event.clear()
                except (NameError, Exception):
                    pass
                mtp.copy_file_to_device(
                    src          = push_path,
                    dest_parts   = _push_parts,
                    device_name  = config.DEVICE_NAME,
                    storage_name = file_storage,
                    overwrite    = True,
                )
                # ADDED: Cooldown to avoid overwhelming the MTP stack
                time.sleep(1.0)

                # If we pushed to a new canonical path, delete the original
                # 2026-08-20: was raw COM-only code (_navigate + .Items() +
                # InvokeVerb) — mtp._navigate() is a dispatcher that routes to
                # _adb._navigate under USE_ADB=True (this session's config),
                # whose return value has no .Items() method at all, causing
                # every one of these deletes to fail with 'str' object has no
                # attribute 'Items'' — confirmed live: 103 tracks pushed to a
                # canonical path with their original never cleaned up. Fixed
                # by routing through mtp.delete_file_on_device(), the
                # dispatcher-level delete that already correctly handles both
                # backends (ADB shell rm / PS-subprocess delete), same as
                # every other delete call site in this file.
                if _canon_moved:
                    try:
                        _del_ok = mtp.delete_file_on_device(
                            config.DEVICE_NAME, file_storage, parts, ef["filename"],
                        )
                        if _del_ok:
                            log.info(f"  [Reorg] Moved to canonical path: {'/'.join(_push_parts)}/")
                        else:
                            log.warning(f"  [Reorg] Could not delete original after canonical move: delete returned False")
                    except Exception as _del_e:
                        log.warning(f"  [Reorg] Could not delete original after canonical move: {_del_e}")
                # 3s grace for ZX500 internal DB update before prefetch resumes
                time.sleep(3.0)
                try:
                    _post_write_event.set()
                except (NameError, Exception):
                    pass
                with self._stats_lock:
                    self.stats["changed"] += 1
                    if did_upgrade:
                        self.stats.setdefault("upgrades", 0)
                        self.stats["upgrades"] += 1
                label = "Upgraded to FLAC" if did_upgrade else "Tags fixed"
                # ── Verbose metadata log ──────────────────────────────────────
                # Print every audited field so you can see full before/after.
                # Changed fields: show old (red) -> new (green).
                # Unchanged fields: show current value (dim/grey).
                # ANSI codes degrade gracefully in plain log files.
                _RED    = "\033[31m"
                _GREEN  = "\033[32m"
                _DIM    = "\033[2m"
                _RESET  = "\033[0m"

                _changed_fields = {f for f, _, _ in diffs}
                _diff_map       = {f: (o, n) for f, o, n in diffs}

                lines = [f"CHANGED  {ef['key']}"]

                _ALL_DISPLAY_FIELDS = [
                    "title", "artist", "album_artist", "album",
                    "track_number", "disc_number", "release_year", "genre",
                ]
                for _fld in _ALL_DISPLAY_FIELDS:
                    if not config.AUDIT_FIELDS.get(_fld):
                        continue
                    if _fld in _changed_fields:
                        old_v, new_v = _diff_map[_fld]
                        old_disp = repr(old_v) if old_v else "''"
                        new_disp = repr(new_v) if new_v else "''"
                        lines.append(
                            f"  {_fld:<14}: {_RED}{old_disp:<30}{_RESET} -> {_GREEN}{new_disp}{_RESET}"
                        )
                    else:
                        cur_v = (current_for_push.get(_fld) or "").strip()
                        lines.append(
                            f"  {_DIM}{_fld:<14}: {repr(cur_v)}{_RESET}"
                        )

                if needs_id3_fix:
                    lines.append(f"  {_fld:<14}  {_RED}id3_version: non-v2.{config.ID3_VERSION}{_RESET} -> {_GREEN}v2.{config.ID3_VERSION}{_RESET}")
                if did_upgrade:
                    lines.append(f"  {'format':<14}: {_RED}'mp3'{_RESET} -> {_GREEN}'flac (HiRes TIDAL)'{_RESET}")

                log.info("\n".join(lines))
                with self._report_lock:
                    self.report.append("\n".join(lines))
                with self._state_lock:
                    # Stamp with genre_hit (the enriched value actually written),
                    # not _gh (genre_cache only) — so next run SKIP uses same inputs.
                    _mk = _compute_metakey(
                        pathlib.Path(ef["filename"]).stem, genre_hit,
                        self._album_meta.get(album_key, {})
                    )
                    # cv=True means "cover art situation resolved":
                    #   • art is now embedded in the file, OR
                    #   • no URL was available from any source (TIDAL+Spotify) — don't
                    #     keep re-auditing forever hoping a URL magically appears.
                    # cv=False ONLY when we had a URL but fetch/embed failed (transient).
                    _actually_has_art = _has_cover_art(push_path, push_path.suffix.lower() == ".mp3")
                    _has_cv = _actually_has_art or not cover_url
                    # If we pushed to a canonical path, update the state key so
                    # the next audit scan finds the file at its new location.
                    _canon_filename = _canonical_filename(final_tags, push_path.suffix.lower())
                    _state_key_used = (
                        file_storage + "/" + "/".join(_push_parts + [_canon_filename])
                        if _canon_moved else ef["key"]
                    )
                    # Remove old key if we moved
                    if _canon_moved and ef["key"] != _state_key_used:
                        self.state.pop(ef["key"], None)
                    self.state[_state_key_used] = _make_state_entry(_mk, has_cover=_has_cv)
                    _push_fail_clear(ef["key"])  # clear any prior failure record on success
                    log.debug(f"  [MetaKey] Stamped | key={_state_key_used!r} | mk={_mk!r} | cover={_has_cv} | canon_moved={_canon_moved} | genre={(genre_hit.get('genre') or '–')!r} year={(genre_hit.get('year') or self._album_meta.get(album_key,{}).get('year') or '–')!r}")
            except Exception as e:
                log.error(f"  [Audit Push Failed] {push_path.name}: {e}")
                _push_fail_record(ef["key"])   # backoff tracking
                try:
                    _post_write_event.set()  # unblock prefetch even on failure
                except (NameError, Exception):
                    pass
                with self._stats_lock:
                    self.stats["errors"] += 1
                if cleanup_tmp:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return True, False, False, push_path
        else:
            with self._state_lock:
                # Stamp with genre_hit (enriched result) so the expected_mk
                # on the next run matches and the track correctly SKIPs.
                _mk = _compute_metakey(
                    pathlib.Path(ef["filename"]).stem, genre_hit,
                    self._album_meta.get(album_key, {})
                )
                # cv=True: art embedded OR no URL available (stop retrying).
                # cv=False ONLY when we had a URL but embed failed (transient).
                _actually_has_art = _has_cover_art(push_path, push_path.suffix.lower() == ".mp3")
                _has_cv = _actually_has_art or not cover_url
                self.state[ef["key"]] = _make_state_entry(_mk, has_cover=_has_cv)
                log.debug(f"  [MetaKey] Stamped | key={ef['key']!r} | mk={_mk!r} | cover={_has_cv} | genre={(genre_hit.get('genre') or '–')!r} year={(genre_hit.get('year') or self._album_meta.get(album_key,{}).get('year') or '–')!r}")

        if cleanup_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return True, should_push, did_upgrade, push_path

    # Known folder name watermarks from download tools
    _FOLDER_WATERMARKS = _WATERMARK_FOLDER_PREFIXES  # single source of truth

    def _normalize_folder_names(self, device_files: list[dict]) -> None:
        """
        Rename device folders whose names contain download-tool watermarks
        (e.g. "SpotiDownloader.com - Limbo") to the canonical "Artist - Album" format.

        Also renames bare album-only folders (e.g. "Aquemini") to "Outkast - Aquemini"
        when the tracks inside have a valid album_artist tag.

        Strategy per folder:
          1. Read tags from the first audio file inside via MTP
          2. Build canonical name: sanitize(album_artist) + " - " + sanitize(album)
          3. If folder name != canonical and canonical looks valid → rename
        """
        import re as _re_fn

        # Build unique folder set
        folders: set[tuple] = set()
        for ef in device_files:
            folders.add((ef.get("storage", config.WRITE_STORAGE), tuple(ef["parts"])))

        renamed = 0
        for storage, parts_tuple in folders:
            if not parts_tuple:
                continue
            folder_name = parts_tuple[-1]
            parent_parts = list(parts_tuple[:-1])

            # Check if rename is needed
            is_watermarked = any(folder_name.startswith(w) for w in self._FOLDER_WATERMARKS)
            # Also check bare album-only folders (no " - " = no artist prefix)
            # But don't touch single-word folders like "Downloads" or "Music"
            has_no_artist = (
                " - " not in folder_name and
                len(folder_name) > 3 and
                not folder_name.startswith(".")
            )
            if not is_watermarked and not has_no_artist:
                continue

            # Find one audio file in this folder from device_files to read its tags
            sample_ef = next(
                (ef for ef in device_files
                 if tuple(ef["parts"]) == parts_tuple
                 and pathlib.Path(ef["filename"]).suffix.lower() in (".mp3", ".flac")),
                None
            )
            if not sample_ef:
                continue

            # Pull the file to read its tags
            try:
                _tmp = pathlib.Path(tempfile.mkdtemp())
                _f   = _tmp / sample_ef["filename"]
                _ok  = mtp.read_file_from_device(
                    src_parts    = sample_ef["parts"],
                    filename     = sample_ef["filename"],
                    dest_local   = _f,
                    device_name  = config.DEVICE_NAME,
                    storage_name = storage,
                )
                if not _ok or not _f.exists():
                    shutil.rmtree(_tmp, ignore_errors=True)
                    continue
                tags = _read_tags(_f)
                shutil.rmtree(_tmp, ignore_errors=True)
            except Exception as e:
                log.debug(f"  [FolderRename] Could not read sample file in {folder_name!r}: {e}")
                continue

            aa  = (tags.get("album_artist") or tags.get("artist") or "").strip()
            alb = (tags.get("album") or "").strip()

            if not aa or not alb:
                log.debug(f"  [FolderRename] No artist/album tags in {folder_name!r}, skipping")
                continue

            canonical = (
                sanitize_name(aa, 40) + " - " + sanitize_name(alb, 60)
            )
            if canonical == folder_name:
                continue  # already correct

            # Sanity: canonical must look reasonable
            if len(canonical) < 5 or canonical.count(" - ") < 1:
                continue

            # Rename via MTP: navigate to parent, find the folder item, rename
            if self.dry_run:
                log.info(f"  [DRY-RUN] Would rename folder: {folder_name!r} -> {canonical!r} — no device write")
                continue
            try:
                _sr     = mtp._get_storage_root(config.DEVICE_NAME, storage)
                _parent = mtp._navigate(_sr, parent_parts, create=False) if parent_parts else _sr
                _found  = None
                for _item in _parent.Items():
                    if (_item.Name or "").lower() == folder_name.lower():
                        _found = _item
                        break
                if _found is None:
                    log.debug(f"  [FolderRename] Could not find folder item {folder_name!r}")
                    continue
                # Use Shell.Namespace rename — InvokeVerbEx("rename", new_name)
                try:
                    _found.InvokeVerbEx("rename", canonical)
                    log.info(f"  [FolderRename] {folder_name!r} → {canonical!r}")
                    renamed += 1
                    # Invalidate MTP cache so subsequent operations see the new name
                    mtp._invalidate_storage_cache()
                except Exception as _rv_e:
                    # Shell rename may not be supported — try a copy+delete approach
                    log.debug(f"  [FolderRename] InvokeVerbEx failed ({_rv_e}), skipping {folder_name!r}")
            except Exception as e:
                log.debug(f"  [FolderRename] Rename error for {folder_name!r}: {e}")

        if renamed:
            log.info(f"  [FolderRename] Renamed {renamed} folder(s) to canonical format")

    def _cleanup_all_mixed_folders(self, device_files: list[dict]) -> None:
        """
        Post-audit sweep: for every folder on the device, deduplicate files by
        normalised title. Priority: FLAC > MP3, larger file > smaller file,
        canonical name (Artist - Album - NN) > bare name.
        Runs once after all batches complete to catch cross-session duplicates
        that the per-batch cleanup misses.
        """
        import re as _re_cl2
        import walkman_dedup as _wd_prot

        def _norm_title_cl2(name: str) -> str:
            stem = pathlib.Path(name).stem
            pts  = [p.strip() for p in stem.split(" - ")]
            raw  = pts[-1] if len(pts) >= 2 else stem
            t    = _re_cl2.sub(r"^\d{1,2}-\d{1,2}\s+", "", raw)
            t    = _re_cl2.sub(r"^\d{1,3}[\s.\-]+", "", t)
            t    = _re_cl2.sub(r"[^a-z0-9 ]", "", t.lower())
            return _re_cl2.sub(r"\s+", " ", t).strip()

        def _is_canonical(name: str) -> bool:
            """True if filename has 'Artist - Album - NN Title' format."""
            parts = [p.strip() for p in pathlib.Path(name).stem.split(" - ")]
            return len(parts) >= 3

        # Build unique (storage, parts) folder set from device_files
        folders: set[tuple] = set()
        for ef in device_files:
            folders.add((ef.get("storage", config.WRITE_STORAGE), tuple(ef["parts"])))

        total_deleted = 0
        for storage, parts_tuple in folders:
            try:
                storage_root = mtp._get_storage_root(config.DEVICE_NAME, storage)
                folder = mtp._navigate(storage_root, list(parts_tuple), create=False)
                items  = list(folder.Items())
            except Exception:
                continue

            # ── DeepSeek folder coherence check ─────────────────────────────
            # For folders with enough files, ask DeepSeek whether all files
            # belong to the same album/artist, and whether the folder name is
            # correct. This catches the common case where SpotiDownloader
            # mixes tracks from different albums into one junk folder, or where
            # the folder was named after the wrong artist.
            # Only fires if we have ≥4 audio files (avoid false positives on
            # small folders) and the API key is set.
            if _DS_API_KEY and len(parts_tuple) >= 1:
                _folder_filenames = []
                for _ci in items:
                    try:
                        _cn = _ci.Name or ""
                        _ce = (mtp._item_audio_ext(_ci) or "").lower()
                        if _ce in (".mp3", ".flac", ".m4a", ".aac"):
                            _folder_filenames.append(_cn)
                    except Exception:
                        pass
                if len(_folder_filenames) >= 4:
                    _folder_display = parts_tuple[-1] if parts_tuple else "Music"
                    _coh = _deepseek_folder_coherence(_folder_display, _folder_filenames)
                    if not _coh.get("coherent", True):
                        _outliers = _coh.get("outliers", [])
                        _note     = _coh.get("note", "")
                        _exp_artist = _coh.get("expected_artist", "")
                        _exp_album  = _coh.get("expected_album", "")
                        log.warning(
                            f"  [DeepSeek] Folder coherence FAIL: {_folder_display!r}\n"
                            f"    Expected: {_exp_artist} — {_exp_album}\n"
                            f"    Outliers: {_outliers}\n"
                            f"    Note: {_note}"
                        )
                        # Log outliers but don't delete — user should review
                        # Mark them in the report so they appear in audit_report.txt
                        if _outliers:
                            with self._report_lock:
                                self.report.append(
                                    f"[DeepSeek Folder Coherence] {storage}/{'/'.join(parts_tuple)}\n"
                                    f"  Expected: {_exp_artist} — {_exp_album}\n"
                                    f"  Possible outliers (review manually):\n"
                                    + "\n".join(f"    • {o}" for o in _outliers[:6])
                                    + (f"\n  Note: {_note}" if _note else "")
                                )
                    elif _coh.get("rename_folder_to"):
                        _suggested = _coh["rename_folder_to"]
                        _current   = parts_tuple[-1] if parts_tuple else ""
                        if _suggested and _suggested != _current:
                            log.info(
                                f"  [DeepSeek] Folder rename suggested: "
                                f"{_current!r} → {_suggested!r}"
                            )

            # Group items by normalised title
            from collections import defaultdict
            groups: dict[str, list] = defaultdict(list)
            for item in items:
                nm  = item.Name or ""
                ext = (mtp._item_audio_ext(item) or pathlib.Path(nm).suffix).lower()
                if ext not in (".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg",
                               ".opus", ".wma", ".aiff", ".dsf", ".dff"):
                    continue
                nt = _norm_title_cl2(nm)
                if nt:
                    groups[nt].append((item, ext, nm))

            for nt, group in groups.items():
                if len(group) < 2:
                    continue
                # Score each: FLAC=3, MP3=2, other=1. Canonical bonus=+1.
                def _score(entry):
                    item, ext, nm = entry
                    q = 3 if ext == ".flac" else (2 if ext == ".mp3" else 1)
                    c = 1 if _is_canonical(nm) else 0
                    try:
                        sz = item.Size if hasattr(item, "Size") else 0
                    except Exception:
                        sz = 0
                    return (q, c, sz)

                group.sort(key=_score, reverse=True)
                keep = group[0]
                for loser in group[1:]:
                    l_item, l_ext, l_nm = loser
                    k_item, k_ext, k_nm = keep
                    # 2026-09-05: favorited/playlisted files live in the Sony
                    # app DB keyed to this exact path — deleting one orphans
                    # that state even though a "better" copy survives. The
                    # batch-phase Reorg already honours this pin; this
                    # post-batch dedup sweep was missing it (confirmed live:
                    # audit run #4 skipped it here and in _consolidate_*).
                    try:
                        _lhp = f"{storage}/" + "/".join(list(parts_tuple) + [l_nm])
                        if _lhp.replace("\\", "/").strip() in _wd_prot._load_favplaylist_protected_paths():
                            log.info(f"  [Dedup] PINNED (favorited/playlisted), not deleting: {l_nm!r}")
                            continue
                    except Exception:
                        pass
                    log.info(
                        f"  [Dedup] Removing {l_nm!r} (keep {k_nm!r}) "
                        f"in {storage}/{'/'.join(parts_tuple)}"
                    )
                    if self.dry_run:
                        log.info(f"  [DRY-RUN] Would delete duplicate: {l_nm!r} — no device write")
                        continue
                    try:
                        # FIX 14: route through mtp.delete_file_on_device instead
                        # of raw COM InvokeVerb — works on both ADB and COM transports.
                        _del_ok = mtp.delete_file_on_device(
                            config.DEVICE_NAME, storage, list(parts_tuple), l_nm
                        )
                        if _del_ok:
                            total_deleted += 1
                        else:
                            log.warning(f"  [Dedup] delete_file_on_device returned False for {l_nm!r}")
                    except Exception as e:
                        log.warning(f"  [Dedup] Delete failed for {l_nm!r}: {e}")

        if total_deleted:
            log.info(f"  [Dedup] Removed {total_deleted} duplicate file(s) across all folders")
            mtp._invalidate_storage_cache()

    def _cleanup_mp3_after_flac(
        self,
        storage:     str,
        parts:       list[str],
    ) -> int:
        """
        Scan a device album folder and delete any MP3 whose stem matches an
        existing FLAC (case-insensitive, ignoring extension).  Called after
        a batch that upgraded at least one file in this folder.
        Returns the number of MP3s deleted.
        """
        deleted = 0
        try:
            storage_root = mtp._get_storage_root(config.DEVICE_NAME, storage)
            folder       = mtp._navigate(storage_root, parts, create=False)
            items        = list(folder.Items())
        except Exception as e:
            log.warning(f"  [Cleanup] Could not enumerate {parts}: {e}")
            return 0

        def _norm_title_cleanup(name: str) -> str:
            """Normalise a filename to bare title for fuzzy duplicate detection."""
            import re as _re_cl
            stem = pathlib.Path(name).stem
            pts  = [p.strip() for p in stem.split(" - ")]
            raw  = pts[-1] if len(pts) >= 2 else stem
            t    = _re_cl.sub(r"^\d{1,2}-\d{1,2}\s+", "", raw)
            t    = _re_cl.sub(r"^\d{1,3}[\s.\-]+", "", t)
            t    = _re_cl.sub(r"[^a-z0-9 ]", "", t.lower())
            return _re_cl.sub(r"\s+", " ", t).strip()

        # Build sets of FLAC titles — exact stem AND fuzzy normalised title
        flac_stems        = set()
        flac_norm_titles  = set()
        for item in items:
            ext = mtp._item_audio_ext(item)
            nm  = item.Name or ""
            if nm.lower().endswith(".flac") or ext == ".flac":
                flac_stems.add(pathlib.Path(nm).stem.lower())
                flac_stems.add(nm.lower())
                flac_norm_titles.add(_norm_title_cleanup(nm))

        for item in items:
            try:
                ext = mtp._item_audio_ext(item)
                if ext != ".mp3":
                    continue
                nm   = item.Name or ""
                stem = pathlib.Path(nm).stem.lower()
                norm = _norm_title_cleanup(nm)
                if stem in flac_stems or nm.lower() in flac_stems or norm in flac_norm_titles:
                    log.info(f"  [Cleanup] Deleting MP3 (FLAC duplicate exists): {nm}")
                    if self.dry_run:
                        log.info(f"  [DRY-RUN] Would delete MP3 (FLAC duplicate): {nm!r} — no device write")
                        continue
                    try:
                        item.InvokeVerb("Delete")
                        deleted += 1
                    except Exception as e:
                        log.warning(f"  [Cleanup] Delete failed for {nm}: {e}")
            except Exception:
                continue

        if deleted:
            mtp._invalidate_storage_cache()
        return deleted

    # ── Network pre-fetch workers ─────────────────────────────────────────────
    TIDAL_META_WORKERS  = 16   # concurrent TIDAL metadata lookups (network I/O)
    TIDAL_DL_WORKERS    = 4    # concurrent TIDAL FLAC downloads   (network I/O)

    def _prefetch_tidal_meta(self, batch: list[dict]) -> dict[str, dict]:
        """
        Phase 1 of batch processing: fetch TIDAL metadata (genre/year) for all
        stale files in the batch that are missing genre or year, concurrently.
        Returns {ef_key: tidal_meta_dict}.
        Pure network I/O — no MTP, safe to parallelise.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not getattr(config, "TIDAL_CLIENT_ID", ""):
            return {}

        # Only fetch for stale files with missing metadata
        candidates = []
        for ef in batch:
            if not _is_stale(self.state, ef["key"]):
                continue
            # We don't have the tags yet (file not pulled), so use the filename
            # stem as a rough title proxy — TIDAL fetch will be confirmed later
            # against actual tags. This is a best-effort pre-warm of the cache.
            stem = pathlib.Path(ef["filename"]).stem
            # Strip leading track number (e.g. "01 ", "01 - ", "01. ")
            stem = re.sub(r"^\d{1,3}[\s\-\.]+", "", stem).strip()
            parts_split = [p.strip() for p in stem.split(" - ")]
            # Canonical format: "Artist - Album - NN Title"
            # SpotiDownloader format: "Title" (no dashes)
            # Old format: "NN Artist - Title"
            if len(parts_split) >= 3:
                # Canonical or old multi-part: first part = artist, last part = title
                artist_guess = parts_split[0]
                # Last part may start with track number — strip it
                title_guess  = re.sub(r"^\d{1,3}\s+", "", parts_split[-1]).strip()
            elif len(parts_split) == 2:
                # "Artist - Title" style
                artist_guess = parts_split[0]
                title_guess  = parts_split[1]
            else:
                # Single word / SpotiDownloader: use as title, no artist
                artist_guess = ""
                title_guess  = stem
            candidates.append((ef["key"], artist_guess, title_guess))

        if not candidates:
            return {}

        results: dict[str, dict] = {}
        results_lock = threading.Lock()

        def _fetch(key, artist, title):
            meta = _tidal_fetch_track(artist, title)
            if meta:
                with results_lock:
                    results[key] = meta

        with ThreadPoolExecutor(max_workers=self.TIDAL_META_WORKERS) as pool:
            futs = [pool.submit(_fetch, k, a, t) for k, a, t in candidates]
            for f in as_completed(futs):
                try: f.result()
                except Exception: pass

        hit_count = len(results)
        log.debug(f"  [Pre-fetch] TIDAL metadata: {hit_count}/{len(candidates)} hits")
        # If hit rate is very low and we had candidates, TIDAL session may have
        # expired mid-run. Attempt a session refresh so the next batch benefits.
        if len(candidates) >= 5 and hit_count == 0:
            log.warning("  [TIDAL] 0 hits on full batch — session may have expired. Attempting refresh.")
            try:
                from downloader import _get_tidal_session
                _get_tidal_session()  # re-establishes session if expired
                log.info("  [TIDAL] Session re-established.")
            except Exception as _te:
                log.warning(f"  [TIDAL] Session refresh failed: {_te}")
        return results

    def _prefetch_flac_downloads(self, batch: list[dict]) -> tuple[dict[str, pathlib.Path], pathlib.Path | None]:
        """
        Phase 2 of batch pre-fetch: for all stale MP3 files in the batch,
        download HiRes FLACs from TIDAL concurrently.
        Returns {ef_key: local_flac_path} and a base temp directory that should be cleaned up later.
        Pure network I/O — no MTP, safe to parallelise.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not getattr(config, "UPGRADE_MP3_TO_FLAC", True):
            return {}, None
        if not getattr(config, "TIDAL_CLIENT_ID", ""):
            return {}, None

        candidates = []
        for ef in batch:
            if not _is_stale(self.state, ef["key"]):
                continue
            if pathlib.Path(ef["filename"]).suffix.lower() != ".mp3":
                continue
            stem = pathlib.Path(ef["filename"]).stem
            stem_raw = stem
            stem = re.sub(r"^\d{1,3}[\s\-\.]+", "", stem)
            parts_split = [p.strip() for p in stem.split(" - ")]
            if len(parts_split) >= 3:
                artist_guess = parts_split[0]
                title_guess  = re.sub(r"^\d{1,3}\s+", "", parts_split[-1]).strip()
            elif len(parts_split) == 2:
                artist_guess = parts_split[0]
                title_guess  = parts_split[1]
            else:
                artist_guess = ""
                title_guess  = stem
            # CRITICAL: Skip prefetch when artist is unknown (bare title files like
            # "Burden.mp3", "Slump.mp3"). Without the artist, TIDAL will match the
            # wrong song (e.g. "Burden" → Rolling Stones "Beast of Burden").
            # These files will be handled by the inline download path in
            # _audit_one_device_file where album_meta has resolved the real artist.
            if not artist_guess:
                log.debug(f"  [Pre-fetch] Skipping {stem_raw!r} — no artist in filename, will resolve inline")
                continue
            candidates.append((ef["key"], artist_guess, title_guess))

        if not candidates:
            return {}, None

        log.info(f"  [Pre-fetch] Downloading {len(candidates)} FLAC(s) from TIDAL ...")
        results: dict[str, pathlib.Path] = {}
        results_lock = threading.Lock()
        dl_base = pathlib.Path(tempfile.mkdtemp(prefix="walkman_audit_dl_"))

        def _download(key, artist, title):
            dl_dir = dl_base / re.sub(r"[^a-z0-9]", "_", title.lower())[:40]
            dl_dir.mkdir(exist_ok=True)
            flac = _tidal_download_flac(artist, title, str(dl_dir))
            if flac and flac.exists() and flac.stat().st_size > 0:
                with results_lock:
                    results[key] = flac

        with ThreadPoolExecutor(max_workers=self.TIDAL_DL_WORKERS) as pool:
            futs = [pool.submit(_download, k, a, t) for k, a, t in candidates]
            for f in as_completed(futs):
                try: f.result()
                except Exception as e:
                    log.debug(f"  [Pre-fetch] FLAC download error: {e}")

        log.info(f"  [Pre-fetch] FLAC downloads complete: {len(results)}/{len(candidates)} succeeded")
        return results, dl_base

    def _audit_device_batch(self, batch: list[dict]) -> int:
        """
        Two-phase batch processing (original file‑batch mode).

        Phase A (parallel, network only):
          - TIDAL_META_WORKERS concurrent metadata lookups for files missing genre/year
          - TIDAL_DL_WORKERS concurrent FLAC downloads for all MP3 files in batch

        Phase B (serial, MTP only):
          - For each file: pull from device → tag with pre-fetched metadata/FLAC
            → push back if changed
          - MTP transfers are strictly serial (ZX500 single-threaded MTP stack)

        After the batch: sweep upgraded album folders to delete orphaned MP3s.
        Returns number of files changed in this batch.
        """
        # ── Phase A: parallel network pre-fetch ──────────────────────────────
        tidal_meta   = self._prefetch_tidal_meta(batch)
        dl_result    = self._prefetch_flac_downloads(batch)
        if isinstance(dl_result, tuple):
            flac_cache, dl_base = dl_result
        else:
            flac_cache, dl_base = dl_result, None

        # ── Phase B: serial MTP read→tag→push ────────────────────────────────
        # Pre-read the NEXT file from device while processing the current one.
        # This overlaps MTP read latency with CPU tag computation + MTP write,
        # cutting effective time per file from (read+write) to max(read, write).
        import queue as _queue
        READ_AHEAD = 2   # number of files to pre-read concurrently

        prefetch_q: _queue.Queue = _queue.Queue(maxsize=READ_AHEAD + 1)

        # Event set by consumer after each MTP write — prefetch waits on it
        # before starting the next pull, giving the ZX500 time to finish its
        # internal DB update before the next read is attempted.
        _post_write_event = threading.Event()
        _post_write_event.set()  # initially clear to proceed

        def _prefetch_worker():
            """Background thread: pull files from device into temp dirs ahead of time.
            Metakey fast path: compute expected metakey locally; skip MTP pull if it
            matches the stored metakey — file tags are already correct."""
            for _ef in batch:
                # Wait for any in-progress write to settle before reading
                _post_write_event.wait(timeout=30.0)
                _stem       = pathlib.Path(_ef["filename"]).stem
                _pf_parts   = [p.strip() for p in _stem.split(" - ")]
                import re as _re_pf
                def _sn(s): return _re_pf.sub(r"^\d{1,3}[\s.\-]+", "", s).strip() or s
                _pf_artist  = _pf_parts[0] if len(_pf_parts) >= 2 else ""
                _pf_title   = _sn(_pf_parts[-1]) if len(_pf_parts) >= 2 else _sn(_stem)
                _gh         = _genre_cache_lookup(_pf_artist, _pf_title)
                _am      = self._album_meta.get(
                    _ef["parts"][-1] if _ef.get("parts") else "", {}
                )
                _exp_mk  = _compute_metakey(_stem, _gh, _am)
                _pstored    = self.state.get(_ef["key"])
                _pstored_mk = _pstored.get("mk") if isinstance(_pstored, dict) else None
                if _pstored_mk and _pstored_mk == _exp_mk:
                    # Metakey matches — tags confirmed correct; skip MTP pull
                    prefetch_q.put((_ef, None, None))  # None signals: skip in consumer
                    continue
                # No metakey match — pull the file
                # Use a thread with a hard timeout so a slow/stuck MTP read
                # doesn't block the prefetch worker for up to 10 minutes.
                # If the pull times out we put (ef, None, None) so the consumer
                # falls back to a live pull (or skips if still not needed).
                _filename     = _ef["filename"]
                _file_storage = _ef.get("storage", config.WRITE_STORAGE)
                _tmp_dir      = pathlib.Path(tempfile.mkdtemp())
                _tmp_file     = _tmp_dir / _filename
                _pull_result  = [None]
                _pull_exc     = [None]
                def _do_pull():
                    try:
                        _pull_result[0] = mtp.read_file_from_device(
                            src_parts    = _ef["parts"],
                            filename     = _filename,
                            dest_local   = _tmp_file,
                            device_name  = config.DEVICE_NAME,
                            storage_name = _file_storage,
                        )
                    except Exception as _exc:
                        _pull_exc[0] = _exc
                _pull_thread = threading.Thread(target=_do_pull, daemon=True)
                _pull_thread.start()
                _pull_thread.join(timeout=90)  # 90s max per prefetch pull
                if _pull_thread.is_alive():
                    log.warning(f"  [Prefetch Timeout] {_filename}: MTP read took >90s, skipping prefetch")
                    shutil.rmtree(_tmp_dir, ignore_errors=True)
                    prefetch_q.put((_ef, None, None))
                elif _pull_exc[0]:
                    log.warning(f"  [Prefetch Pull Failed] {_filename}: {_pull_exc[0]}")
                    shutil.rmtree(_tmp_dir, ignore_errors=True)
                    prefetch_q.put((_ef, None, None))
                else:
                    prefetch_q.put((_ef, _tmp_dir, _tmp_file if _pull_result[0] else None))
            prefetch_q.put(None)  # sentinel

        prefetch_thread = threading.Thread(target=_prefetch_worker, daemon=True)
        prefetch_thread.start()

        changed_count    = 0
        upgraded_folders: set[tuple] = set()
        _changed_efs: list[dict]    = []   # files changed this batch — for backtrack pass

        while True:
            try:
                item = prefetch_q.get(timeout=150)  # 150s: 90s pull + 60s buffer
            except Exception:
                log.warning("  [Audit] Prefetch queue timeout — prefetch worker may be stuck. Moving on.")
                break
            if item is None:
                break
            ef, tmp_dir, tmp_file = item
            try:
                _, changed, upgraded, _final_path = self._audit_one_device_file(
                    ef,
                    prefetched_meta  = tidal_meta.get(ef["key"]),
                    prefetched_flac  = flac_cache.get(ef["key"]),
                    prefetched_tmp   = (tmp_dir, tmp_file),
                )
            except Exception as e:
                log.error(f"  [Batch error] {ef.get('filename','?')}: {e}")
                if tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                continue
            if changed:
                changed_count += 1
                _changed_efs.append(ef)
            if upgraded:
                upgraded_folders.add(
                    (ef.get("storage", config.WRITE_STORAGE), tuple(ef["parts"]))
                )

        prefetch_thread.join(timeout=5)

        # Cleanup pre-downloaded FLACs
        if dl_base and dl_base.exists():
            shutil.rmtree(dl_base, ignore_errors=True)

        # Post-batch: delete MP3s where a FLAC now exists
        for storage, parts_tuple in upgraded_folders:
            deleted = self._cleanup_mp3_after_flac(storage, list(parts_tuple))
            if deleted:
                log.info(
                    f"  [Cleanup] Removed {deleted} MP3(s) from "
                    f"{storage}/{'/'.join(parts_tuple)}"
                )

        self._batch_changed_efs = _changed_efs
        return changed_count

    # -----------------------------------------------------------------------
    # NEW: Folder‑wise audit method
    # -----------------------------------------------------------------------

    def _audit_device_folder(
        self,
        folder_key: tuple,
        folder_files: list[dict],
        prefetch_meta: dict,
        prefetch_flac: dict,
    ) -> int:
        """
        Process one album folder as a unit.
        Returns the number of files changed in this folder.
        """
        storage, parts_tuple = folder_key
        parts = list(parts_tuple)
        # 1. Determine which files are stale
        stale_files = [f for f in folder_files if _is_stale(self.state, f["key"])]
        if not stale_files:
            # No stale files → skip (folder already up‑to‑date)
            return 0

        log.info(
            f"[FolderAudit] Processing {storage}/{'/'.join(parts)} "
            f"({len(stale_files)} stale of {len(folder_files)} files)"
        )

        # 2. Create temp folder for the whole album
        tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="audit_folder_"))
        pulled_files = []

        try:
            # 3. Pull all files from the device (stale and clean).
            # On first failure, wait 3s and reconnect the MTP session before retrying.
            # This handles the common case where MTP drops after a long folder scan.
            _mtp_reconnected = False   # only reconnect once per folder
            for ef in folder_files:
                filename = ef["filename"]
                src_parts = ef["parts"]
                dest_path = tmp_root / filename
                ok = mtp.read_file_from_device(
                    src_parts, filename, dest_path,
                    config.DEVICE_NAME, storage
                )
                if ok and dest_path.exists():
                    pulled_files.append((ef, dest_path))
                else:
                    if not _mtp_reconnected:
                        # First failure in this folder: wait, reconnect, retry once.
                        log.warning(
                            f"  [FolderAudit] Failed to pull {filename!r} — "
                            f"waiting 3s and reconnecting MTP before retry"
                        )
                        time.sleep(3)
                        try:
                            mtp.reconnect()
                        except Exception as _re:
                            log.debug(f"  [FolderAudit] MTP reconnect error: {_re}")
                        _mtp_reconnected = True   # don't reconnect again this folder
                        dest_path.unlink(missing_ok=True)
                        ok2 = mtp.read_file_from_device(
                            src_parts, filename, dest_path,
                            config.DEVICE_NAME, storage
                        )
                        if ok2 and dest_path.exists():
                            pulled_files.append((ef, dest_path))
                            continue
                    # Still failed after reconnect (or reconnect already tried)
                    log.warning(f"  [FolderAudit] Failed to pull {filename!r}, skipping")

            if not pulled_files:
                return 0

            # 4. Process each file (tag, upgrade, etc.)
            changed_count = 0
            upgraded_folders = set()
            # Map ef["key"] -> (changed, final_path) so Step 7 can use the real local path
            audit_results: dict[str, tuple[bool, pathlib.Path | None]] = {}

            for ef, local_path in pulled_files:
                meta = prefetch_meta.get(ef["key"], {})
                flac = prefetch_flac.get(ef["key"])
                _, changed, upgraded, final_path = self._audit_one_device_file(
                    ef,
                    prefetched_meta=meta,
                    prefetched_flac=flac,
                    prefetched_tmp=(tmp_root, local_path),
                    cleanup_tmp=False,   # we'll clean up at the end
                )
                if changed:
                    changed_count += 1
                if upgraded:
                    upgraded_folders.add((storage, tuple(parts)))
                # final_path is the temp file after any in-place canonical rename.
                # If _audit_one_device_file already pushed this file (changed=True),
                # we mark it None so Step 7 skips it (it's already on the device).
                audit_results[ef["key"]] = (changed, final_path if not changed else None)

            # 5. Determine canonical destination for the folder
            # Use the first file that has valid tags to compute the new folder.
            # Use the final_path from audit (may have been canonically renamed in-place).
            canonical_parts = None
            for ef, local_path in pulled_files:
                # Prefer the post-audit final path (has corrected tags); fall back to original
                _, fp = audit_results.get(ef["key"], (False, None))
                read_path = fp if (fp is not None and fp.exists()) else local_path
                tags = _read_tags(read_path)
                if tags.get("album_artist") and tags.get("album"):
                    canonical_parts = _canonical_dest_parts(tags)
                    if canonical_parts:
                        break
            if not canonical_parts:
                log.warning(f"  [FolderAudit] No canonical folder derived, keeping original")
                canonical_parts = [config.MUSIC_ROOT] + parts

            # 6. Rename junk folder on device if needed
            old_folder_name = parts[-1] if parts else ""
            new_folder_name = sanitize_name(canonical_parts[-1], 100) if len(canonical_parts) > 1 else old_folder_name
            if new_folder_name != old_folder_name and old_folder_name:
                log.info(f"  [FolderAudit] Renaming folder: '{old_folder_name}' -> '{new_folder_name}'")
                if self.dry_run:
                    log.info(f"  [DRY-RUN] Would rename folder on device: '{old_folder_name}' -> '{new_folder_name}' — no device write")
                else:
                    mtp.rename_folder_on_device(
                        config.DEVICE_NAME,
                        storage,
                        parts[:-1] if len(parts) > 1 else [],
                        old_folder_name,
                        new_folder_name,
                    )
                # Update the parts list to the new name (kept even in dry-run so
                # the rest of this method's PREVIEW logic reflects what would
                # happen — no device write occurs either way when dry_run).
                if len(parts) > 0:
                    parts[-1] = new_folder_name
                canonical_parts = [config.MUSIC_ROOT] + parts

            # 7. Upload processed files to the canonical location.
            # IMPORTANT: _audit_one_device_file already pushed files where tags changed
            # (changed=True) directly to their canonical destination.  Do NOT re-upload
            # those — the temp file may have been renamed and the source path would be
            # wrong, burning retries on "Shell could not resolve source" errors.
            # Only upload files that were NOT changed (no tag diffs / no push needed)
            # but whose folder needs moving because the album folder name changed.
            folder_moved = (canonical_parts != [config.MUSIC_ROOT] + list(parts))
            for ef, local_path in pulled_files:
                changed, final_path = audit_results.get(ef["key"], (False, None))
                if changed:
                    # Already pushed by _audit_one_device_file — skip.
                    continue
                # Use the final (possibly renamed) local path; fall back to original.
                upload_path = final_path if (final_path is not None and final_path.exists()) else local_path
                if not upload_path.exists():
                    log.warning(f"  [FolderAudit] Local file missing, skipping upload: {upload_path.name}")
                    continue
                tags = _read_tags(upload_path)
                ext = upload_path.suffix.lower()
                new_filename = _canonical_filename(tags, ext) if tags.get("title") else upload_path.name
                if self.dry_run:
                    log.info(f"  [DRY-RUN] Would upload {new_filename!r} -> {storage}/{'/'.join(canonical_parts)}/ — no device write")
                    continue
                try:
                    mtp.copy_file_to_device(
                        src=upload_path,
                        dest_parts=canonical_parts,
                        device_name=config.DEVICE_NAME,
                        storage_name=storage,
                        overwrite=True,
                    )
                    log.info(f"  [FolderAudit] Uploaded: {new_filename}")
                except Exception as e:
                    log.error(f"  [FolderAudit] Upload failed for {new_filename}: {e}")

            # 8. Delete the old folder (if it's different from the new one)
            if canonical_parts != [config.MUSIC_ROOT] + parts:
                if self.dry_run:
                    log.info(f"  [DRY-RUN] Would delete old folder: {storage}/{'/'.join(parts)} — no device write")
                else:
                    try:
                        sr = mtp._get_storage_root(config.DEVICE_NAME, storage)
                        old_folder = mtp._navigate(sr, parts, create=False)
                        if old_folder:
                            log.info(f"  [FolderAudit] Deleting old folder: {storage}/{'/'.join(parts)}")
                            old_folder.InvokeVerb("delete")
                            time.sleep(1.0)
                    except Exception as e:
                        log.warning(f"  [FolderAudit] Could not delete old folder: {e}")

            # 9. Backtrack verification: re‑pull and verify tags for each changed file
            self._log(f"  [FolderAudit] Backtrack verifying {changed_count} file(s)...")
            for ef, local_path in pulled_files:
                tags = _read_tags(local_path)
                stem = pathlib.Path(ef["filename"]).stem
                gh = _genre_cache_lookup(tags.get("artist", ""), tags.get("title", ""))
                am = self._album_meta.get(canonical_parts[-1] if canonical_parts else "", {})
                exp_mk = _compute_metakey(stem, gh, am)
                state_entry = self.state.get(ef["key"])
                stored_mk = state_entry.get("mk") if isinstance(state_entry, dict) else None
                if stored_mk != exp_mk:
                    log.warning(f"  [FolderAudit] Metakey mismatch for {ef['filename']}: stored={stored_mk} expected={exp_mk}")
                else:
                    log.debug(f"  [FolderAudit] Verified OK: {ef['filename']}")

            return changed_count

        finally:
            # ignore_errors=True handles the case where tmp_root was already
            # cleaned up by an inner CopyHere timeout handler.
            if tmp_root.exists():
                shutil.rmtree(tmp_root, ignore_errors=True)

    # -----------------------------------------------------------------------
    # NEW: Folder consolidation (merges duplicate folders)
    # -----------------------------------------------------------------------

    def _consolidate_duplicate_folders(self, device_files: list[dict]) -> None:
        """
        Scan all Music/ folders across all storages, group them by normalized key,
        and merge all files from duplicate folders into one canonical folder.
        After merging, delete the empty source folders.
        """
        import walkman_dedup as _wd_prot
        try:
            _protected = _wd_prot._load_favplaylist_protected_paths()
        except Exception:
            _protected = set()

        def _is_nested_music(parts_tuple) -> bool:
            # A "Music/Music/..." path is the historical double-root bug — never
            # a valid consolidation target (audit run #4 merged good folders
            # INTO these, making the fragmentation worse).
            pl = [p for p in parts_tuple]
            return len(pl) >= 2 and pl[0].lower() == "music" and pl[1].lower() == "music"

        # Build a set of all folders (storage, parts_tuple)
        folders: dict[tuple, list[dict]] = defaultdict(list)
        for ef in device_files:
            key = (ef["storage"], tuple(ef["parts"]))
            folders[key].append(ef)

        # Group folders by normalized name
        norm_groups: dict[str, list[tuple]] = defaultdict(list)
        for (storage, parts_tuple), file_list in folders.items():
            if not parts_tuple:
                continue
            folder_name = parts_tuple[-1]
            norm_key = _normalized_folder_name(folder_name)
            norm_groups[norm_key].append((storage, parts_tuple, file_list))

        # For each group, pick a canonical folder and merge others into it
        total_merged = 0
        for norm_key, group in norm_groups.items():
            if len(group) <= 1:
                continue

            # Sort by number of files (desc), then by name quality
            def folder_score(entry):
                storage, parts_tuple, file_list = entry
                name = parts_tuple[-1]
                # Prefer folders that already look canonical (have " - ")
                canonical_score = 1 if " - " in name else 0
                # Prefer folders without watermarks
                watermarked = any(name.startswith(w) for w in self._FOLDER_WATERMARKS)
                watermark_score = 0 if watermarked else 1
                # Prefer longer names (likely full artist+album)
                length_score = min(len(name), 100) / 100
                # Never let a "Music/Music/..." path win canonical.
                not_nested = 0 if _is_nested_music(parts_tuple) else 1
                return (not_nested, len(file_list), canonical_score, watermark_score, length_score)

            group.sort(key=folder_score, reverse=True)
            canonical_entry = group[0]
            canonical_storage, canonical_parts_tuple, canonical_files = canonical_entry
            canonical_parts = list(canonical_parts_tuple)
            if _is_nested_music(canonical_parts_tuple):
                # every candidate in this group is a Music/Music/ path — leave
                # it alone rather than pick a bad target.
                log.debug(f"  [Consolidate] all candidates nested under Music/Music/ — skipping {norm_key!r}")
                continue

            # Process each other folder
            for other_storage, other_parts_tuple, other_files in group[1:]:
                other_parts = list(other_parts_tuple)
                other_folder_name = other_parts[-1]

                # Skip folders that hold a favorited/playlisted file — merging
                # renames every path inside, orphaning the Sony app DB rows and
                # breaking the M3U8 relative paths (audit run #4 incident).
                if _protected:
                    _pf = [
                        ef["filename"] for ef in other_files
                        if f"{other_storage}/" + "/".join(other_parts + [ef["filename"]])
                        in _protected
                    ]
                    if _pf:
                        log.info(
                            f"  [Consolidate] PINNED (favorited/playlisted) — not merging "
                            f"{other_storage}/{'/'.join(other_parts)} ({len(_pf)} protected file(s))"
                        )
                        continue

                log.info(
                    f"  [Consolidate] Merging {other_storage}/{'/'.join(other_parts)} "
                    f"into {canonical_storage}/{'/'.join(canonical_parts)}"
                )
                if self.dry_run:
                    log.info(
                        f"  [DRY-RUN] Would merge {len(other_files)} file(s) from "
                        f"{other_storage}/{'/'.join(other_parts)} into "
                        f"{canonical_storage}/{'/'.join(canonical_parts)} and delete the "
                        f"source folder — no device write"
                    )
                    continue

                # Move each audio file from other to canonical
                for ef in other_files:
                    filename = ef["filename"]
                    # Pull the file to a temporary location
                    tmp_dir = pathlib.Path(tempfile.mkdtemp())
                    try:
                        tmp_file = tmp_dir / filename
                        ok = mtp.read_file_from_device(
                            src_parts=other_parts,
                            filename=filename,
                            dest_local=tmp_file,
                            device_name=config.DEVICE_NAME,
                            storage_name=other_storage,
                        )
                        if not ok or not tmp_file.exists():
                            log.warning(f"  [Consolidate] Failed to read {filename}")
                            continue

                        # Upload to canonical folder
                        mtp.copy_file_to_device(
                            src=tmp_file,
                            dest_parts=canonical_parts,
                            device_name=config.DEVICE_NAME,
                            storage_name=canonical_storage,
                            overwrite=True,
                        )
                        log.debug(f"  [Consolidate] Moved {filename}")

                        # Update state: remove old key, add new key
                        old_key = ef["key"]
                        new_key = canonical_storage + "/" + "/".join(canonical_parts + [filename])
                        with self._state_lock:
                            entry = self.state.pop(old_key, None)
                            if entry:
                                self.state[new_key] = entry
                    except Exception as e:
                        log.warning(f"  [Consolidate] Error moving {filename}: {e}")
                    finally:
                        shutil.rmtree(tmp_dir, ignore_errors=True)

                # After moving all files, delete the now-empty source folder
                try:
                    sr = mtp._get_storage_root(config.DEVICE_NAME, other_storage)
                    parent_parts = other_parts[:-1]
                    folder_item = mtp._navigate(sr, other_parts, create=False)
                    if folder_item:
                        # Check if folder is empty (no files, no subfolders)
                        try:
                            remaining = list(folder_item.Items())
                            if not remaining:
                                log.info(f"  [Consolidate] Deleting empty folder: {other_storage}/{'/'.join(other_parts)}")
                                folder_item.InvokeVerb("delete")
                                total_merged += 1
                            else:
                                log.warning(f"  [Consolidate] Folder not empty after move: {other_storage}/{'/'.join(other_parts)}")
                        except Exception:
                            pass
                except Exception as e:
                    log.warning(f"  [Consolidate] Could not delete source folder: {e}")

        if total_merged:
            log.info(f"  [Consolidate] Merged {total_merged} duplicate folder(s)")
            mtp._invalidate_storage_cache()

    # -----------------------------------------------------------------------
    # Local-temp sweep (2026-09-03) — keep a long audit from filling the PC
    # drive with staging copies of files that are already back on the device.
    # -----------------------------------------------------------------------

    def _pc_free_gb(self, path: "str | None" = None) -> float:
        import temp_sweep as _ts
        return _ts.pc_free_gb(path)

    def _sweep_local_temp(self, reason: str = "") -> None:
        """
        Delete this session's stale staging dirs/files from %TEMP% (shared
        impl in temp_sweep.py). By the time this fires, every earlier
        batch/folder has already pushed its files to the Walkman — those
        local copies are dead weight. Best-effort; a locked file is skipped.
        """
        try:
            import temp_sweep as _ts
            _ts.sweep_stale_staging(
                reason,
                log_callback=self._log,
                min_age_s=float(getattr(config, "AUDIT_TEMP_SWEEP_MIN_AGE_S", 90)),
            )
        except Exception as e:
            log.debug(f"  [TempSweep] skipped ({e})")

    # -----------------------------------------------------------------------
    # Main run
    # -----------------------------------------------------------------------

    def run(self, cancelled_flag: list[bool] | None = None) -> dict:
        """
        Full audit entry point.
        If config.AUDIT_FOLDER_MODE is True, processes folders as units.
        After the main audit, performs folder consolidation to eliminate
        duplicate folders.
        After the dedup pass, prune_during_audit() removes cross-format
        duplicates (e.g. FLAC+MP3 pairs) discovered during the audit scan.
        """
        # Prevent Windows from sleeping or turning off the display while the
        # audit runs — long runs (3-8h) were being interrupted by system sleep.
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        _sleep_guard_active = False
        try:
            import ctypes as _ct
            _ES_CONTINUOUS        = 0x80000000
            _ES_SYSTEM_REQUIRED   = 0x00000001
            _ES_DISPLAY_REQUIRED  = 0x00000002
            _ct.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
            )
            _sleep_guard_active = True
            log.info("  [Sleep Guard] Windows sleep/shutdown prevention active.")
        except Exception as _sg_err:
            log.debug(f"  [Sleep Guard] Could not activate: {_sg_err}")

        def _release_sleep_guard():
            if _sleep_guard_active:
                try:
                    import ctypes as _ct2
                    _ct2.windll.kernel32.SetThreadExecutionState(0x80000000)
                    log.info("  [Sleep Guard] Windows sleep prevention released.")
                except Exception:
                    pass

        self._log("\n=== METADATA AUDIT STARTED ===")
        self._log(
            f"Stale threshold: {config.AUDIT_STALE_DAYS} day(s)  |  "
            f"Batch size: {config.AUDIT_BATCH_SIZE}"
        )

        # Clear stale staging cruft from any earlier (crashed / killed) run
        # before we start pulling files.
        self._sweep_local_temp("startup")
        self._files_since_sweep = 0

        # Normalize genre cache before computing metakeys — collapses variants
        # like "Alternative R&B"→"Alt R&B", applies operator overrides, etc.
        # Changes here cause metakey mismatches → stale files re-tagged this run.
        _n = _normalize_genre_cache()
        if _n:
            self._log(f"  [GenreCacheNorm] Pre-audit: {_n} genre entries normalized")

        # ── 1. Audit local files ─────────────────────────────────────────
        if getattr(config, "AUDIT_DEVICE_ONLY", False):
            self._log("  [AuditScope] AUDIT_DEVICE_ONLY=True — skipping local-file phase")
            local_files = []
        else:
            local_files = self._enumerate_local()
        self._log(f"Local files found: {len(local_files)}")
        self._audit_local_files(local_files)

        if cancelled_flag and cancelled_flag[0]:
            self._log("Audit cancelled after local phase.")
            self._write_report()
            return self.stats

        # ── 2. Enumerate device ──────────────────────────────────────────
        self._log("Enumerating device files for audit ...")
        device_files = self._enumerate_device()
        total = len(device_files)
        self.stats["device_files_total"] = total
        self._log(f"Device files found: {total}")

        if total == 0:
            self._log("No device files found — skipping device audit phase.")
        else:
            log.info("  [Device] Waiting 5s for MTP stack to settle after scan...")
            time.sleep(5.0)

            # -----------------------------------------------------------------
            # Branch: folder‑wise mode vs. file‑batch mode
            # -----------------------------------------------------------------
            if config.AUDIT_FOLDER_MODE:
                # Group files by (storage, tuple(parts))
                from collections import defaultdict
                folders: dict[tuple, list[dict]] = defaultdict(list)
                for ef in device_files:
                    folders[(ef["storage"], tuple(ef["parts"]))].append(ef)

                self._log(f"Folder‑wise audit: {len(folders)} album folder(s) to process")
                processed = 0
                for folder_key, folder_files in folders.items():
                    if cancelled_flag and cancelled_flag[0]:
                        break

                    # Pre‑fetch TIDAL metadata and FLACs for stale files in this folder
                    stale_efs = [f for f in folder_files if _is_stale(self.state, f["key"])]
                    if not stale_efs:
                        continue

                    tidal_meta = self._prefetch_tidal_meta(stale_efs)
                    dl_result  = self._prefetch_flac_downloads(stale_efs)
                    if isinstance(dl_result, tuple):
                        flac_cache, dl_base = dl_result
                    else:
                        flac_cache, dl_base = dl_result, None

                    changed = self._audit_device_folder(folder_key, folder_files, tidal_meta, flac_cache)
                    processed += 1
                    if processed % 10 == 0:
                        self._log(f"  Folder progress: {processed}/{len(folders)} folders, {changed} changed in this folder")

                    if dl_base and dl_base.exists():
                        shutil.rmtree(dl_base, ignore_errors=True)

                    # 2026-09-03 — sweep local staging every N albums (or sooner
                    # if the drive is running low). Files from prior folders are
                    # already back on the Walkman.
                    _every = int(getattr(config, "AUDIT_TEMP_SWEEP_EVERY_FOLDERS", 5))
                    _lowgb = float(getattr(config, "AUDIT_TEMP_LOW_FREE_GB", 12.0))
                    if (_every > 0 and processed % _every == 0):
                        self._sweep_local_temp(f"{processed} albums")
                    elif self._pc_free_gb() < _lowgb:
                        self._sweep_local_temp("low disk")

                # After all folders, run a final cleanup (duplicate removal)
                self._cleanup_all_mixed_folders(device_files)

            else:
                # Original file‑batch mode
                batch_size = config.AUDIT_BATCH_SIZE
                n_batches  = (total + batch_size - 1) // batch_size
                self._all_changed_efs_run = []
                for batch_idx in range(n_batches):
                    if cancelled_flag and cancelled_flag[0]:
                        break
                    batch = device_files[
                        batch_idx * batch_size : (batch_idx + 1) * batch_size
                    ]
                    changed = self._audit_device_batch(batch)
                    if hasattr(self, "_batch_changed_efs"):
                        self._all_changed_efs_run.extend(self._batch_changed_efs)

                    # Save state after every batch so a cancel doesn't lose progress
                    _save_audit_state(self.state)

                    processed = min((batch_idx + 1) * batch_size, total)
                    self._log(
                        f"  Audit batch {batch_idx + 1}/{n_batches}: "
                        f"{processed}/{total} files processed, "
                        f"{changed} changed in this batch"
                    )

                    # 2026-09-03 — sweep local staging every N tracks (or sooner
                    # if the drive is running low). Prior batches are already
                    # pushed back to the Walkman.
                    self._files_since_sweep = getattr(self, "_files_since_sweep", 0) + len(batch)
                    _every = int(getattr(config, "AUDIT_TEMP_SWEEP_EVERY_FILES", 100))
                    _lowgb = float(getattr(config, "AUDIT_TEMP_LOW_FREE_GB", 12.0))
                    if _every > 0 and self._files_since_sweep >= _every:
                        self._sweep_local_temp(f"{processed} tracks")
                        self._files_since_sweep = 0
                    elif self._pc_free_gb() < _lowgb:
                        self._sweep_local_temp("low disk")
                        self._files_since_sweep = 0

        # ── Folder rename pass: fix SpotiDownloader watermark folder names ───
        # Renames folders like "SpotiDownloader.com - Limbo" → "Aminé - Limbo"
        # by reading one track's tags to get the real album_artist.
        if device_files:
            try:
                self._normalize_folder_names(device_files)
            except Exception as _rfn_e:
                log.warning(f"  [FolderRename] Pass error: {_rfn_e}")

        # ── Full dedup sweep across all folders ─────────────────────────────
        # Removes duplicate audio files in every device folder:
        # FLAC beats MP3, canonical filename beats bare title, larger file wins ties.
        # Catches cross-session duplicates (e.g. "Slump.mp3" vs "Outkast - Aquemini - 01 Slump.flac")
        # and FLAC-vs-FLAC duplicates (SpotiDownloader FLAC + upgraded TIDAL FLAC).
        if device_files:
            try:
                self._cleanup_all_mixed_folders(device_files)
            except Exception as _ded_e:
                log.warning(f"  [Dedup] Sweep error: {_ded_e}")

        # ── NEW: Consolidate duplicate folders ─────────────────────────────
        if device_files:
            try:
                self._consolidate_duplicate_folders(device_files)
            except Exception as _con_e:
                log.warning(f"  [Consolidate] Error: {_con_e}")

        # ── Backtrack pass: verify + reorganise changed files ────────────────
        # 1. Re-reads every changed file from device, checks genre+album_artist
        #    were actually written. Un-stamps failures for retry next run.
        # 2. For files with correct tags but wrong folder path, silently moves
        #    them to canonical Artist/Album/ structure using the same
        #    CopyHere → verify → InvokeVerb(Delete) approach as the reorganizer.
        #    No MoveHere = no dialogs.
        try:
            _all_changed = getattr(self, "_all_changed_efs_run", [])
            if _all_changed:
                self._log(
                    "\n── Backtrack Verification + Reorganise: "
                    + str(len(_all_changed)) + " changed file(s) ──"
                )
                _bt_ok = 0
                _bt_fail = 0
                _bt_moved = 0
                for _bef in _all_changed:
                    _b_tmp = pathlib.Path(tempfile.mkdtemp())
                    try:
                        _b_storage = _bef.get("storage", config.WRITE_STORAGE)
                        _b_file    = _b_tmp / _bef["filename"]
                        _pulled    = mtp.read_file_from_device(
                            src_parts    = _bef["parts"],
                            filename     = _bef["filename"],
                            dest_local   = _b_file,
                            device_name  = config.DEVICE_NAME,
                            storage_name = _b_storage,
                        )
                        if not (_pulled and _b_file.exists()):
                            # File may have already been relocated by the push step
                            log.debug("  [Backtrack] not at original path (already moved?): " + _bef["filename"])
                            _bt_ok += 1
                            continue

                        # ── Step 1: verify tags ────────────────────────────────
                        _bt = _read_tags(_b_file)
                        _issues = []
                        if not (_bt.get("genre") or "").strip():
                            _issues.append("genre blank")
                        if not (_bt.get("album_artist") or "").strip():
                            _issues.append("album_artist blank")
                        if _issues:
                            log.warning(
                                "  [Backtrack] FAIL " + _bef["filename"]
                                + ": " + ", ".join(_issues)
                            )
                            _bt_fail += 1
                            self.state.pop(_bef["key"], None)
                            continue   # don't try to move a badly-tagged file

                        # ── Step 1b: DeepSeek metadata verification ─────────────
                        # Ask DeepSeek whether the tags we just wrote look plausible.
                        # This catches: wrong album attribution, genre "Unknown" that
                        # should have been filled, year that looks impossible, artist
                        # splintering that slipped through the heuristics.
                        # Only runs when the API key is configured — degrades to no-op.
                        _ds_artist = (_bt.get("artist") or "").strip()
                        _ds_album  = (_bt.get("album")  or "").strip()
                        _ds_title  = (_bt.get("title")  or "").strip()
                        _ds_genre  = (_bt.get("genre")  or "").strip()
                        _ds_year   = (_bt.get("release_year") or "").strip()

                        if _DS_API_KEY and _ds_title:
                            _ds_result = _deepseek_verify_metadata(
                                _ds_artist, _ds_album, _ds_title, _ds_genre, _ds_year
                            )
                            if not _ds_result.get("ok", True):
                                _note = _ds_result.get("note", "")
                                log.info(
                                    f"  [DeepSeek] Metadata correction for {_bef['filename']!r}: {_note}"
                                )
                                # Apply any high-confidence corrections
                                _ds_changed = False
                                for _field, _easy_key in [
                                    ("genre",   "genre"),
                                    ("year",    "release_year"),
                                    ("artist",  "artist"),
                                    ("album",   "album"),
                                    ("title",   "title"),
                                ]:
                                    _ds_val = _ds_result.get(_field)
                                    if _ds_val and _ds_val != _bt.get(_field if _field != "year" else "release_year"):
                                        _bt[_field if _field != "year" else "release_year"] = _ds_val
                                        _ds_changed = True
                                if _ds_changed:
                                    # Write corrected tags to the local temp file
                                    try:
                                        _b_ext_for_tag = pathlib.Path(_bef["filename"]).suffix.lower()
                                        if _b_ext_for_tag == ".flac":
                                            _audio_ds = FLAC(_b_file)
                                            for _f, _ek in [("title","title"),("artist","artist"),
                                                            ("album","album"),("genre","genre"),
                                                            ("release_year","date"),("album_artist","albumartist")]:
                                                if _bt.get(_f):
                                                    _audio_ds[_ek] = _bt[_f]
                                            _audio_ds.save()
                                        else:
                                            try:
                                                _audio_ds = MP3(_b_file, ID3=EasyID3)
                                            except ID3NoHeaderError:
                                                MP3(_b_file).add_tags()
                                                _audio_ds = MP3(_b_file, ID3=EasyID3)
                                            for _f, _ek in [("title","title"),("artist","artist"),
                                                            ("album","album"),("genre","genre"),
                                                            ("release_year","date"),("album_artist","albumartist")]:
                                                if _bt.get(_f):
                                                    _audio_ds[_ek] = _bt[_f]
                                            _audio_ds.save(v2_version=config.ID3_VERSION)
                                        log.info(f"  [DeepSeek] Applied corrections to {_bef['filename']!r}")

                                        # Re-upload the corrected file to canonical path.
                                        # FIX B6: _canonical_dest_parts returns a list of folder
                                        # parts already including MUSIC_ROOT — do NOT prepend it again
                                        # (old code: dest_rel, dest_fn = ... then [MUSIC_ROOT] + dest_rel
                                        # caused double "Music/Music/..." path on 3-part albums and
                                        # ValueError: too many values to unpack on any album).
                                        _dest_rel_ds = _canonical_dest_parts(_bt)
                                        if _dest_rel_ds is None:
                                            log.debug(
                                                f"  [DeepSeek] No canonical path for "
                                                f"{_bef['filename']!r} — skipping re-upload"
                                            )
                                        elif self.dry_run:
                                            log.info(
                                                f"  [DRY-RUN] Would re-upload DeepSeek-corrected "
                                                f"tags for {_bef['filename']!r} -> "
                                                f"{'/'.join(_dest_rel_ds)}/ — no device write"
                                            )
                                        else:
                                            # FIX F3: route through mtp.copy_file_to_device
                                            # (ADB push or PS CopyHere FolderItem) instead of
                                            # calling CopyHere(str(path)) which silently does
                                            # nothing on Win11 non-interactive.
                                            _upload_ok = mtp.copy_file_to_device(
                                                src          = _b_file,
                                                dest_parts   = _dest_rel_ds,
                                                device_name  = config.DEVICE_NAME,
                                                storage_name = _b_storage,
                                                overwrite    = True,
                                            )
                                            if not _upload_ok:
                                                log.warning(
                                                    f"  [DeepSeek] Could not upload corrected "
                                                    f"tags for {_bef['filename']!r}"
                                                )
                                    except Exception as _ds_e:
                                        log.debug(f"  [DeepSeek] Tag re-write failed: {_ds_e}")

                        _bt_ok += 1

                        # ── Step 2: check folder structure ─────────────────────
                        _b_ext    = pathlib.Path(_bef["filename"]).suffix.lower() or _bef.get("ext", ".mp3")
                        _dest_rel = _canonical_dest_parts(_bt)

                        if _dest_rel is None or _dest_rel == _bef["parts"]:
                            continue   # already in canonical location

                        # 2026-09-05: favorited/playlisted files are pinned to
                        # their current path in every other move path (batch
                        # Reorg, _cleanup_all_mixed_folders, _consolidate_*) —
                        # this backtrack reorg was the last one missing it.
                        try:
                            import walkman_dedup as _wd_bt
                            _bt_hp = f"{_b_storage}/" + "/".join(_bef["parts"] + [_bef["filename"]])
                            if _bt_hp.replace("\\", "/").strip() in _wd_bt._load_favplaylist_protected_paths():
                                log.info(
                                    f"  [Backtrack Reorg] PINNED (favorited/playlisted): "
                                    f"{_bef['filename']!r} stays in {'/'.join(_bef['parts'])}/"
                                )
                                continue
                        except Exception:
                            pass

                        _canon_fn = _canonical_filename(_bt, _b_ext)
                        log.info(
                            "  [Backtrack Reorg] " + _bef["filename"]
                            + " -> " + "/".join(_dest_rel + [_canon_fn])
                        )
                        if self.dry_run:
                            log.info(
                                "  [DRY-RUN] Would move (upload + delete original) — no device write: "
                                + _bef["filename"] + " -> " + "/".join(_dest_rel + [_canon_fn])
                            )
                            continue

                        # ── Step 3: move to canonical path ─────────────────────
                        try:
                            # FIX B5: transport-safe existence check.
                            # The old code called mtp._get_storage_root() then
                            # _dst_fld.Items() on the result — which is a COM folder
                            # object on the COM path but a plain PATH STRING on ADB.
                            # Calling .Items() on a string raises AttributeError.
                            # Use dev_lib if available for a zero-MTP existence check;
                            # otherwise let copy_file_to_device overwrite=True handle it.
                            _already = False
                            _dev_lib_ref = getattr(self, "_dev_lib", None)
                            if _dev_lib_ref is not None:
                                try:
                                    _already = _dev_lib_ref.has_track(
                                        artist = (_bt.get("album_artist") or _bt.get("artist") or ""),
                                        album  = (_bt.get("album") or ""),
                                        title  = (_bt.get("title") or ""),
                                    )
                                except Exception:
                                    _already = False

                            if _already:
                                log.debug("  [Backtrack Reorg] dest already exists: " + _canon_fn)
                                continue

                            # FIX F3: route through mtp.copy_file_to_device instead of
                            # _dst_fld.CopyHere(str(path)).
                            # CopyHere() requires a Shell FolderItem object — passing a bare
                            # path string silently does nothing on Win11 non-interactive.
                            # mtp.copy_file_to_device uses ADB push (primary) or PowerShell
                            # CopyHere with a proper FolderItem (COM fallback), and handles
                            # the wait-for-file poll internally.
                            _upload_ok = mtp.copy_file_to_device(
                                src          = _b_file,
                                dest_parts   = _dest_rel,
                                device_name  = config.DEVICE_NAME,
                                storage_name = _b_storage,
                                overwrite    = True,
                            )
                            if not _upload_ok:
                                log.warning("  [Backtrack Reorg] Upload failed: " + _canon_fn)
                                continue

                            # FIX F2: delete original via mtp.delete_file_on_device.
                            # InvokeVerb("Delete") in-process triggers Win11 dialogs.
                            # ADB path: adb shell rm. COM path: PS subprocess (hwnd=0,
                            # no dialog attachment possible).
                            try:
                                mtp.delete_file_on_device(
                                    device_name  = config.DEVICE_NAME,
                                    storage_name = _b_storage,
                                    folder_parts = _bef["parts"],
                                    filename     = _bef["filename"],
                                )
                                time.sleep(0.5)
                            except Exception as _de:
                                log.warning("  [Backtrack Reorg] Original delete failed: " + str(_de))

                            # Migrate state key to new canonical path
                            _new_key = _b_storage + "/" + "/".join(_dest_rel + [_canon_fn])
                            with self._state_lock:
                                _entry = self.state.pop(_bef["key"], None)
                                if _entry:
                                    self.state[_new_key] = _entry
                            _bt_moved += 1

                        except Exception as _re:
                            log.warning("  [Backtrack Reorg] Move error: " + str(_re))

                    except Exception as _be:
                        log.debug("  [Backtrack] skip " + str(_bef.get("filename", "?")) + ": " + str(_be))
                    finally:
                        shutil.rmtree(_b_tmp, ignore_errors=True)

                self._log(
                    "  Backtrack: " + str(_bt_ok) + " OK, "
                    + str(_bt_fail) + " failed (will retry next run), "
                    + str(_bt_moved) + " reorganised into canonical path"
                )
        except Exception as _btp:
            log.debug("  [Backtrack] pass error: " + str(_btp))

        self.stats["device_files_reached"] = (
            self.stats.get("audited", 0)
            + self.stats.get("skipped", 0)
            + self.stats.get("errors", 0)
        )
        self._write_report()
        _total  = self.stats.get("device_files_total", 0)
        _reached = self.stats["device_files_reached"]
        _pct = f"{100*_reached//_total}%" if _total else "n/a"
        self._log(
            f"=== AUDIT COMPLETE ===  "
            f"Audited: {self.stats['audited']}  "
            f"Changed: {self.stats['changed']}  "
            f"Skipped: {self.stats['skipped']}  "
            f"Errors: {self.stats['errors']}  "
            f"Reached: {_reached}/{_total} ({_pct})"
        )
        # Persist TIDAL download failure cache back into state
        if _TIDAL_FAIL_CACHE:
            self.state["__tidal_dl_fails__"] = dict(_TIDAL_FAIL_CACHE)
            self.state["__push_fail_cache__"] = dict(_PUSH_FAIL_CACHE)
            self._write_state()

        # Release sleep prevention — also released in finally block if exception occurs
        # MK17 B: cross-format dedup pass (prune_during_audit)
        self.prune_during_audit()
        _release_sleep_guard()

        # Notify the Walkman app so it rescans its private DB.
        # Only fires when files were actually changed or reorganised — zero-change
        # runs skip this to avoid waking the screen unnecessarily.
        if self.stats.get("changed", 0) > 0:
            try:
                import walkman_app_refresh as _war
                _war.refresh()
            except Exception as _wr_e:
                log.debug(f"  [Audit] Walkman refresh failed (non-fatal): {_wr_e}")

        return self.stats

    def prune_during_audit(self) -> int:
        """
        MK17 B: cross-format dedup pass run after the main audit scan.
        Finds tracks present as both FLAC+MP3 or M4A+MP3 and removes the
        inferior format. Cross-format case: "9ice" or "Gongo" track present
        in two codecs → delete the lower-ranked one per _FORMAT_PREFERENCE.
        Returns count of files pruned.
        """
        import collections as _col, os as _os
        pruned = 0
        try:
            from walkman_dedup import _FORMAT_PREFERENCE
            stem_map: dict = _col.defaultdict(list)
            for entry in getattr(self, "report", []):
                path = getattr(entry, "device_path", None) or (
                    entry.get("device_path") if isinstance(entry, dict) else None
                )
                if not path:
                    continue
                stem = _os.path.splitext(_os.path.basename(path))[0].lower().strip()
                ext  = _os.path.splitext(path)[1].lower()
                stem_map[stem].append((ext, path))
            for stem, variants in stem_map.items():
                if len({v[0] for v in variants}) < 2:
                    continue
                ranked = sorted(
                    variants,
                    key=lambda v: _FORMAT_PREFERENCE.index(v[0])
                    if v[0] in _FORMAT_PREFERENCE else 99
                )
                best_ext = ranked[0][0]
                for ext, path in ranked[1:]:
                    # MK18-R3 FIX-F15: actually delete, don't just log
                    # Approach chosen: ADB shell rm — storage-agnostic, respects _PROTECTED_PATHS
                    norm = path.replace("\\", "/").strip()
                    try:
                        from walkman_dedup import _PROTECTED_PATHS as _PP, _PROTECTED_LOCK as _PL
                        with _PL:
                            if norm in _PP:
                                log.info(f"  [Audit/CrossFormat] PROTECTED (just-written): {_os.path.basename(path)!r}")
                                continue
                    except Exception:
                        pass
                    log.info(
                        f"  [Audit/CrossFormat] pruning {ext} "
                        f"(keeping {best_ext}): {_os.path.basename(path)!r}"
                    )
                    if getattr(self, "dry_run", False):
                        pruned += 1
                        continue
                    import subprocess as _sp18, pathlib as _pl18
                    _adb18 = str(_pl18.Path(__file__).parent / "adb.exe") \
                        if (_pl18.Path(__file__).parent / "adb.exe").exists() else "adb"
                    try:
                        _r = _sp18.run(
                            [_adb18, "shell", f"rm -f '{path}'"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=15, check=False,
                        )
                        if _r.returncode == 0:
                            pruned += 1
                        else:
                            log.warning(
                                f"  [Audit/CrossFormat] rm failed: "
                                f"{_r.stderr.strip()[:80]}"
                            )
                    except Exception as _ae:
                        log.warning(f"  [Audit/CrossFormat] ADB error: {_ae}")
        except Exception as e:
            log.debug(f"[Audit/prune_during_audit] {e}")
        if pruned:
            log.info(f"[Audit/CrossFormat] Pruned {pruned} inferior-format file(s)")
        return pruned

    # -----------------------------------------------------------------------
    # Report writer
    # -----------------------------------------------------------------------

    def _write_report(self):
        if not self.report:
            self._log("Audit report: no changes needed.")
            return
        content = (
            f"Walkman Metadata Audit Report\n"
            f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'=' * 60}\n\n"
            + "\n\n".join(self.report)
            + f"\n\n{'=' * 60}\n"
            f"Total changed: {self.stats['changed']}\n"
        )
        config.AUDIT_REPORT_FILE.write_text(content, encoding="utf-8")
        log.info(f"Audit report -> {config.AUDIT_REPORT_FILE}")

    def _write_state(self):
        """Persist the full state (including TIDAL/push failure caches)."""
        _save_audit_state(self.state)