"""
walkman_reorganize.py  v23
──────────────────────────
Reorganises every audio file on the Sony Walkman (both storages) into:

  Music/
    {AlbumArtist}/
      {Album}/
        {AlbumArtist} - {Album} - {NN} {Title}.ext

Now scans both the storage root and the Music folder.
Folder renaming is disabled (handled by audit instead) to avoid invalidating
source items. Files are moved directly to their canonical destinations.
Uses AI (Gemini → DeepSeek) to infer missing track/disc numbers,
minimising "SKIP (track=00)" messages.
"""

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import winreg
from pathlib import Path
from queue import Queue, Empty

try:
    import win32com.client
    import pythoncom
except ImportError:
    sys.exit("ERROR: pywin32 not installed.  Run:  pip install pywin32")

try:
    import mutagen
    from mutagen.mp3  import MP3
    from mutagen.flac import FLAC
except ImportError:
    sys.exit("ERROR: mutagen not installed.  Run:  pip install mutagen")

import mtp_transfer as mtp
import wpd_transfer      # for the PowerShell upload (silent)
import meta_key as mk    # for metakey computation and state
from device_library import _is_junk_prefix  # reuse junk detection

# New AI helper (Gemini → DeepSeek waterfall for missing track/disc numbers)
import ai_helper

# ── Sleep prevention ─────────────────────────────────────────────────────────
_sleep_guard_active = False

def _prevent_sleep():
    global _sleep_guard_active
    if _sleep_guard_active:
        return
    try:
        import ctypes
        _ES_CONTINUOUS        = 0x80000000
        _ES_SYSTEM_REQUIRED   = 0x00000001
        _ES_DISPLAY_REQUIRED  = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
        )
        _sleep_guard_active = True
        log.info("  [Sleep Guard] Windows sleep/shutdown prevention active.")
    except Exception as e:
        log.debug(f"  [Sleep Guard] Could not activate: {e}")

def _allow_sleep():
    global _sleep_guard_active
    if not _sleep_guard_active:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        log.info("  [Sleep Guard] Windows sleep prevention released.")
    except Exception as e:
        log.debug(f"  [Sleep Guard] Could not release: {e}")
    _sleep_guard_active = False

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("wlk")

def setup_logging(verbose: bool, log_callback=None):
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(message)s"
    # When a callback handles stdout output, omit the StreamHandler to prevent
    # double-emit (callback prints to stdout AND StreamHandler writes to stdout).
    handlers: list = [logging.FileHandler("walkman_reorganize.log", encoding="utf-8")]
    if log_callback:
        class CallbackHandler(logging.Handler):
            def emit(self, record):
                # Pass raw message + level so callers (cli_runner._log_cb, main._on_log)
                # can add their own timestamp prefix without double-stamping.
                log_callback(record.getMessage(), record.levelname)
        ch = CallbackHandler()
        ch.setLevel(logging.INFO)
        handlers.append(ch)
    else:
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=handlers,
        force=True,
    )

# ── Constants ─────────────────────────────────────────────────────────────────
MUSIC_SUBFOLDERS  = frozenset({"music"})
SKIP_FOLDERS      = frozenset({
    "android", "dcim", "notifications", "alarms", "ringtones",
    "pictures", "movies", "devicon", "devlogo",
    "default-capability", "important_information", "podcasts", ".deleted",
    "download", "downloads",
})

DEST_MUSIC = "Music"

STORAGES = {
    "internal": "Internal shared storage",
    "sd":       "SD card",
}

TYPE_TO_EXT = {
    "mp3 file": ".mp3", "flac file": ".flac", "aac file": ".aac",
    "m4a file": ".m4a", "ogg file": ".ogg",  "opus file": ".opus",
    "wav file": ".wav", "wma file": ".wma",  "aiff file": ".aiff",
    "mpeg audio file": ".mp3", "audio file": ".mp3",
    "flac audio": ".flac",     "ogg vorbis": ".ogg",
    "dsf file": ".dsf", "dff file": ".dff", "dsd file": ".dsf",
}
AUDIO_EXTENSIONS = frozenset(TYPE_TO_EXT.values())

STATE_FILE    = Path(".audit_state.json")
ROLLBACK_FILE = Path("reorg_rollback.json")
SKIPPED_FILE  = Path("reorg_skipped.log")
DELETE_WAIT   = 2.0
MOVE_COOLDOWN   = 15
MOVE_COOLDOWN_N = 5

PULL_WORKERS = 4
UPLOAD_TIMEOUT = 300   # absolute max per upload (seconds)

def item_audio_ext(item) -> str | None:
    try:
        t = (item.Type or "").lower().strip()
        if t in TYPE_TO_EXT: return TYPE_TO_EXT[t]
        for key, ext in TYPE_TO_EXT.items():
            if key in t: return ext
    except Exception: pass
    try:
        s = Path(item.Name).suffix.lower()
        if s in AUDIO_EXTENSIONS: return s
    except Exception: pass
    return None

# ── String & Cache helpers ────────────────────────────────────────────────────
ILLEGAL = re.compile(r'[\\/:*?"<>|]')

def sanitize(s: str, maxlen: int = 80) -> str:
    s = ILLEGAL.sub("_", s.strip())
    s = re.sub(r"\s{2,}", " ", s)
    s = s.strip(". ")
    return s[:maxlen] or "Unknown"

_UNICODE_ASCII_MAP = str.maketrans({
    "…": "...",  # … HORIZONTAL ELLIPSIS
    "’": "'",   # ' RIGHT SINGLE QUOTATION MARK
    "‘": "'",   # ' LEFT SINGLE QUOTATION MARK
    "“": '"',   # " LEFT DOUBLE QUOTATION MARK
    "”": '"',   # " RIGHT DOUBLE QUOTATION MARK
    "–": "-",   # – EN DASH
    "—": "-",   # — EM DASH
    "¿": "?",   # ¿ INVERTED QUESTION MARK
    "¡": "!",   # ¡ INVERTED EXCLAMATION MARK
    "«": '"',   # « LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    "»": '"',   # » RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    "×": "x",   # × MULTIPLICATION SIGN
    "÷": "/",   # ÷ DIVISION SIGN
    "•": "-",   # • BULLET
    "æ": "ae",  # æ LATIN SMALL LETTER AE
    "Æ": "AE",  # Æ LATIN CAPITAL LETTER AE
    "ø": "o",   # ø LATIN SMALL LETTER O WITH STROKE
    "Ø": "O",   # Ø LATIN CAPITAL LETTER O WITH STROKE
})

def normalize_key(text: str) -> str:
    # 2026-09-04: same "&"->"and" fix as device_library._norm /
    # album_completeness._norm / walkman_dedup._norm — "&" was being
    # stripped to nothing like any other punctuation, so "Arnold & Danny"
    # and "Arnold and Danny" normalized to different keys and reorganize's
    # sibling/dedup matching (20 call sites) failed to recognize them as
    # the same track.
    text = str(text).replace("&", " and ")
    text = text.translate(_UNICODE_ASCII_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    text = re.sub(r"^(the |a |an )", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()

def parse_track_number(raw) -> str:
    if not raw: return "00"
    s = str(raw).strip()
    # Handle "01-06" disc-track format: extract the track part only
    m = re.match(r"^\d+-(\d+)$", s)
    if m: s = m.group(1)
    s = s.split("/")[0].strip()
    try: return f"{int(s):02d}"
    except ValueError: return "00"


# Regex that also matches the "01-NN" TIDAL disc-track prefix in filenames
# e.g. "Artist - Album - 01-06 Title"  →  disc=1, track=6
_RE_DISC_TRACK_PREFIX = re.compile(r"^(\d{1,2})-(\d{1,2})$")

def load_metadata_cache() -> dict:
    meta = {}
    cache_path = Path(".spotify_metadata_cache.json")
    if not cache_path.exists():
        log.warning("No .spotify_metadata_cache.json found. Relying strictly on folder names.")
        return meta
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        for v in data.values():
            a_norm = normalize_key(v.get("artist", ""))
            t_norm = normalize_key(v.get("title", ""))
            meta[f"{a_norm}|{t_norm}"] = v
            meta[t_norm] = v
    except Exception as e:
        log.warning(f"Error loading cache: {e}")
    return meta

# ── Tag inference & enrichment ───────────────────────────────────────────────
_RE_ARTIST_ALBUM_TRACK_TITLE = re.compile(r"^(.+?)\s+-\s+(.+?)\s+-\s+(\d+(?:-\d+)?)\s+(.+)$")
_RE_TRACK_TITLE = re.compile(r"^(\d+(?:-\d+)?)\s+(.+)$")

def infer_and_enrich_tags(stem: str, folder_parts: list[str], meta_cache: dict) -> dict:
    result = {
        "artist": "Unknown Artist",
        "album":  sanitize(stem),
        "title":  sanitize(stem),
        "track":  "00",
        "disc":   "1",
    }

    album_folder = folder_parts[-1] if folder_parts else ""
    # Strip known downloader tool prefixes from folder/album names
    _DOWNLOADER_PREFIXES = re.compile(
        r"(?i)^(?:"
        r"SpotiDownloader\.com\s*-\s*"
        r"|soundcloudaud\.com[_ ]*"
        r"|SoundCloud\.app[_ ]*"
        r"|soundcloud[_ ]*"
        r"|ytdl[_ ]*"
        r"|yt-dlp[_ ]*"
        r")"
    )
    album_folder = _DOWNLOADER_PREFIXES.sub("", album_folder).strip()

    # FIX: handle double-nested structure where artist folder == album folder
    # e.g. Music\Ashanti\Ashanti\ — the device has a redundant nesting.
    # In this case, treat folder_parts[-2] as artist and folder_parts[-1] as album.
    if (len(folder_parts) >= 2
            and normalize_key(folder_parts[-1]) == normalize_key(folder_parts[-2])
            and " - " not in folder_parts[-1]):
        result["artist"]       = sanitize(folder_parts[-2])
        result["album_artist"] = sanitize(folder_parts[-2])
        result["album"]        = sanitize(folder_parts[-1])
        album_folder           = folder_parts[-1]
        log.debug(f"  [DoubleNest] Detected artist==album folder: {folder_parts[-2]!r}")
    else:
        if any(ord(c) > 127 for c in album_folder) and " - " not in album_folder:
            try:
                fixed = album_folder.encode('latin1').decode('utf8')
                if fixed != album_folder and any(c.isalpha() for c in fixed):
                    album_folder = fixed
                    log.debug(f"  [Mojibake] Fixed folder name: {album_folder!r}")
            except Exception:
                pass

        m = re.match(r"^(.+?)\s+-\s+(.+)$", album_folder)
        if m:
            result["artist"] = sanitize(m.group(1))
            result["album"]  = sanitize(m.group(2))
        else:
            result["album"] = sanitize(album_folder) if album_folder else sanitize(stem)

    m = _RE_ARTIST_ALBUM_TRACK_TITLE.match(stem)
    if m and sanitize(m.group(2)).lower() == result["album"].lower():
        result["artist"] = sanitize(m.group(1)) if result["artist"] == "Unknown Artist" else result["artist"]
        result["album"]  = sanitize(m.group(2))
        result["track"]  = parse_track_number(m.group(3))
        result["title"]  = sanitize(m.group(4))
        # Extract disc from "01-06" pattern (disc=1, track=6)
        # parse_track_number already extracts track; we capture disc here.
        dm = _RE_DISC_TRACK_PREFIX.match(m.group(3))
        if dm:
            result["disc"] = str(int(dm.group(1)))
    else:
        m = _RE_TRACK_TITLE.match(stem)
        if m:
            result["track"] = parse_track_number(m.group(1))
            raw_title = m.group(2)
            if result["artist"] != "Unknown Artist" and raw_title.startswith(result["artist"] + " - "):
                raw_title = raw_title[len(result["artist"] + " - "):]
            result["title"] = sanitize(raw_title)
        else:
            result["title"] = sanitize(stem)

    a_norm = normalize_key(result["artist"])
    t_norm = normalize_key(result["title"])
    hit = meta_cache.get(f"{a_norm}|{t_norm}")
    if not hit:
        hit = meta_cache.get(t_norm)
        _unknown_artist = a_norm in {"unknown artist", "unknownartist", ""}
        if hit and not _unknown_artist:
            a_words = set(a_norm.split())
            hit_words = set(normalize_key(hit.get("artist", "")).split())
            if len(a_words) > 1 and not (a_words & hit_words):
                hit = None

    # FIX: if still no hit, try feat-stripped title (e.g. "Meltdown (feat. Drake)" → "Meltdown")
    if not hit:
        _feat_re = re.compile(r"\s*[\(\[].*?(?:feat\.|ft\.|featuring|with)\s[^\)\]]*[\)\]]", re.I)
        t_stripped = normalize_key(_feat_re.sub("", result["title"]).strip())
        if t_stripped and t_stripped != t_norm:
            hit = meta_cache.get(f"{a_norm}|{t_stripped}") or meta_cache.get(t_stripped)
            if hit:
                log.debug(f"  [Cache] feat-stripped hit: {result['title']!r} → track={hit.get('track_number')!r}")

    if hit:
        aa_from_cache = sanitize(hit.get("album_artist") or hit.get("artist") or result["artist"])
        result["artist"]       = aa_from_cache
        result["album_artist"] = aa_from_cache
        result["album"]        = sanitize(hit.get("album") or result["album"])
        result["title"]        = sanitize(hit.get("title") or result["title"])
        try:
            result["track"] = f"{int(hit.get('track_number', 1)):02d}"
        except:
            pass

    # ── AI fallback for track number if still missing ────────────────────────
    if result["track"] == "00" and (stem or folder_parts):
        artist_hint = result["artist"] if result["artist"] != "Unknown Artist" else ""
        album_hint  = result["album"]  if result["album"]  != sanitize(stem)   else ""
        ai_track, ai_disc = ai_helper.infer_track_number(
            filename=stem,
            folder_parts=folder_parts,
            artist_hint=artist_hint,
            album_hint=album_hint,
        )
        if ai_track:
            result["track"] = ai_track
            result["disc"]  = ai_disc
            log.debug(f"  [AI] Inferred track={ai_track}, disc={ai_disc} for {stem!r}")

    # Also try AI for missing artist/album/title when they are still placeholders
    if (result["artist"] == "Unknown Artist"
            or result["album"] == sanitize(stem)
            or result["title"] == sanitize(stem)):
        ai_meta = ai_helper.infer_artist_album_title(
            filename=stem,
            folder_parts=folder_parts,
            existing_tags={
                "artist": result["artist"],
                "album":  result["album"],
                "title":  result["title"],
            },
        )
        if ai_meta.get("artist"):
            result["artist"]       = ai_meta["artist"]
            result["album_artist"] = ai_meta["artist"]
        if ai_meta.get("album"):
            result["album"] = ai_meta["album"]
        if ai_meta.get("title"):
            result["title"] = ai_meta["title"]
        if ai_meta:
            log.debug(f"  [AI] Inferred metadata: {ai_meta}")

    return result

def _fmt_track(track_raw, disc_raw=None) -> str:
    """
    Format track number for canonical filename.
    Disc number is intentionally NOT embedded in the filename token —
    disc info is preserved in ID3/FLAC tags only.
    This prevents the 'Sade 207 bug' (disc=2, track=7 → '207' not '07').
    """
    try:
        parts = str(track_raw).split("/")
        t     = int(parts[0])
        total = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError, IndexError):
        return "00"
    if t <= 0:
        return "00"
    width = 3 if total >= 100 else 2
    return f"{t:0{width}d}"

def _primary_artist(aa: str) -> str:
    primary = aa.split("/")[0].strip()
    primary = re.split(r"\s+(?:feat\.|ft\.|featuring)\s+", primary, flags=re.I)[0].strip()
    return primary

def canonical_path(tags: dict, ext: str) -> tuple[list[str], str]:
    aa    = (tags.get("album_artist") or tags.get("artist") or "").strip()
    alb   = (tags.get("album") or "").strip()
    title = (tags.get("title") or "").strip()
    nn    = _fmt_track(tags.get("track"), tags.get("disc"))

    primary = _primary_artist(aa)
    aa_s    = sanitize(primary, 50)
    alb_s   = sanitize(alb,     60)

    _seen_feat = []
    def _dedup_feat(m):
        if _seen_feat: return ""
        _seen_feat.append(True)
        return m.group(0)
    _title_clean = re.sub(
        r'\s*[\(\[].*?(?:feat\.|ft\.|featuring|with).*?[\)\]]',
        _dedup_feat,
        title, flags=re.IGNORECASE
    ).strip()
    ttl_s   = sanitize(_title_clean, 80)

    filename = f"{aa_s} - {alb_s} - {nn} {ttl_s}{ext}"

    if primary.lower() == "various artists":
        folder_parts = [alb_s]
    else:
        folder_parts = [aa_s, alb_s]

    return folder_parts, filename

def _state_key(storage: str, parts: list[str], filename: str) -> str:
    return storage + "/" + "/".join(parts + [filename])

# ── Live tag reading ────────────────────────────────────────────────────────
def read_live_tags(local_path: Path) -> dict:
    tags: dict = {}
    try:
        ext = local_path.suffix.lower()
        if ext == ".mp3":
            audio = MP3(local_path)
            id3   = audio.tags or {}
            def g(k):
                v = id3.get(k)
                if v is None: return ""
                txt = getattr(v, "text", None)
                return str(txt[0]).strip() if txt else str(v).strip()
            tags = {
                "title":        g("TIT2"),
                "artist":       g("TPE1"),
                "album_artist": g("TPE2"),
                "album":        g("TALB"),
                "track":        g("TRCK"),
                "disc":         g("TPOS"),
            }
        elif ext == ".flac":
            audio = FLAC(local_path)
            def gf(k):
                v = audio.get(k) or audio.get(k.upper()) or []
                return str(v[0]).strip() if (v and v[0] is not None) else ""
            tags = {
                "title":        gf("title"),
                "artist":       gf("artist"),
                "album_artist": gf("albumartist"),
                "album":        gf("album"),
                "track":        gf("tracknumber"),
                "disc":         gf("discnumber"),
            }
        else:
            audio = mutagen.File(local_path)
            if audio:
                def gm(*keys):
                    for k in keys:
                        v = audio.get(k) or audio.get(k.upper()) or []
                        if v: return str(v[0]).strip()
                    return ""
                tags = {
                    "title":        gm("title"),
                    "artist":       gm("artist"),
                    "album_artist": gm("albumartist", "album_artist"),
                    "album":        gm("album"),
                    "track":        gm("tracknumber", "track"),
                    "disc":         gm("discnumber",  "disc"),
                }
    except Exception as e:
        log.debug(f"  [tags] {local_path.name}: {e}")
    return tags

def tags_complete(tags: dict) -> bool:
    aa    = (tags.get("album_artist") or tags.get("artist") or "").strip()
    alb   = (tags.get("album") or "").strip()
    title = (tags.get("title") or "").strip()
    return bool(aa and alb and title)

# ── Shell / COM ───────────────────────────────────────────────────────────────
_SHELL = None

def shell():
    global _SHELL
    if _SHELL is None:
        pythoncom.CoInitialize()
        _SHELL = win32com.client.Dispatch("Shell.Application")
    return _SHELL

def get_items(folder_or_item):
    try:
        if hasattr(folder_or_item, "GetFolder"):
            return list(folder_or_item.GetFolder.Items())
        return list(folder_or_item.Items())
    except Exception:
        return []

def find_walkman():
    for item in list(shell().NameSpace(0x11).Items()):
        if "WALKMAN" in item.Name.upper():
            return item
    raise RuntimeError("WALKMAN not found. Check it is connected.")

def find_storage(walkman_item, name: str):
    name_norm = unicodedata.normalize("NFC", name).lower()
    for child in get_items(walkman_item):
        try:
            if unicodedata.normalize("NFC", child.Name).lower() == name_norm:
                return child
        except Exception:
            continue
    return None

def navigate(root, *parts):
    cur = root
    for part in parts:
        part_norm = unicodedata.normalize("NFC", part).lower()
        found = None
        for child in get_items(cur):
            try:
                child_norm = unicodedata.normalize("NFC", child.Name).lower()
                if child_norm == part_norm and child.IsFolder:
                    found = child
                    break
            except Exception:
                continue
        if found is None:
            return None
        cur = found
    return cur

def ensure_folder(parent_item, name: str):
    name_nfc = unicodedata.normalize("NFC", name).lower()
    for child in get_items(parent_item):
        try:
            if unicodedata.normalize("NFC", child.Name).lower() == name_nfc and child.IsFolder:
                return child
        except Exception:
            continue
    try:
        parent_item.GetFolder.NewFolder(name)
    except Exception:
        pass
    deadline = time.time() + 6
    while time.time() < deadline:
        time.sleep(0.3)
        for child in get_items(parent_item):
            try:
                if unicodedata.normalize("NFC", child.Name).lower() == name_nfc and child.IsFolder:
                    return child
            except Exception:
                continue
    raise RuntimeError(f"Could not create folder '{name}' (timed out)")

def wait_for_local_file(folder_item, filename: str, timeout: int = 90) -> bool:
    stem_lc = Path(filename).stem.lower()
    name_lc = filename.lower()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for item in get_items(folder_item):
            iname = (item.Name or "").lower()
            if iname == name_lc or iname == stem_lc:
                return True
        time.sleep(1)
    return False

def find_item(folder_item, filename: str):
    stem_lc = Path(filename).stem.lower()
    name_lc = filename.lower()
    for item in get_items(folder_item):
        iname = (item.Name or "").lower()
        if iname == name_lc or iname == stem_lc:
            return item
    return None

# -----------------------------------------------------------------------------
# Per-storage scan with timeout (mirrors mtp_transfer_com.scan_device_audio)
# -----------------------------------------------------------------------------
_REORG_SCAN_TIMEOUT_S = 120


def _scan_storage_threaded(key: str, storage_name: str) -> list:
    """Scan one storage root in a daemon thread with its own COM STA + timeout.

    walk_root_folder / walk_music_folder can block indefinitely on a dirty MTP
    session (SD card after internal storage scan).  Running each storage in its
    own thread lets the main thread time out and skip rather than hang forever.
    """
    results: list = []
    done = threading.Event()

    def _worker():
        try:
            import pythoncom as _pc
            _pc.CoInitialize()
            try:
                wm = find_walkman()
                si = find_storage(wm, storage_name)
                if si:
                    results.extend(walk_root_folder(si, storage_name))
                    results.extend(walk_music_folder(si, storage_name))
            finally:
                _pc.CoUninitialize()
        except Exception as e:
            log.warning(f"  [Scan] {storage_name}: worker error: {e}")
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True, name=f"reorg-scan-{key}")
    t.start()
    if not done.wait(_REORG_SCAN_TIMEOUT_S):
        log.warning(
            f"  [Scan] {storage_name}: scan timed out after {_REORG_SCAN_TIMEOUT_S}s "
            f"— skipping. Unplug/replug Walkman to reset MTP session."
        )
    return results


# -----------------------------------------------------------------------------
# Walk storage root directly (for files not inside a Music folder)
# -----------------------------------------------------------------------------
def walk_root_folder(storage_item, storage_label: str):
    """Yield audio files directly under the storage root, skipping system folders."""
    def _recurse(folder_item, rel, parts):
        for item in get_items(folder_item):
            try:
                name = item.Name
            except Exception:
                continue
            # Skip system folders
            if name.lower() in SKIP_FOLDERS:
                continue
            child_rel   = os.path.join(rel, name) if rel else name
            child_parts = parts + [name]
            if item.IsFolder:
                yield from _recurse(item, child_rel, child_parts)
            else:
                ext = item_audio_ext(item)
                if ext:
                    yield item, folder_item, f"{storage_label}\\{child_rel}", ext, parts, storage_label
    yield from _recurse(storage_item, "", [])

def walk_music_folder(storage_item, storage_label: str):
    """Yield audio files inside the 'Music' subfolder (if it exists)."""
    music_item = None
    try:
        for child in get_items(storage_item):
            if child.Name.lower() in MUSIC_SUBFOLDERS and child.IsFolder:
                music_item = child
                break
    except Exception as e:
        log.debug("Could not list %s: %s", storage_label, e)
        return
    if not music_item:
        return

    def _recurse(folder_item, rel, parts):
        for item in get_items(folder_item):
            try:
                name = item.Name
            except Exception:
                continue
            child_rel   = os.path.join(rel, name) if rel else name
            child_parts = parts + [name]
            if item.IsFolder:
                if name.lower() in SKIP_FOLDERS:
                    continue
                yield from _recurse(item, child_rel, child_parts)
            else:
                ext = item_audio_ext(item)
                if ext:
                    yield item, folder_item, f"{storage_label}\\Music\\{child_rel}", ext, parts, storage_label
    yield from _recurse(music_item, "", [])

# ── Audit state helpers ───────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"  [State] Load failed: {e}")
    return {}

def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"  [State] Save failed: {e}")

def migrate_state_key(state: dict, old_key: str, new_key: str) -> None:
    if old_key != new_key:
        entry = state.pop(old_key, None)
        if entry is not None:
            state[new_key] = entry

# ── Device file deletion — dialog-free (PS subprocess / ADB) ─────────────────
def _suppress_delete_confirm(suppress: bool):
    """Toggle the shell 'ConfirmFileDelete' policy for the current user."""
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ConfirmFileDelete", 0,
                              winreg.REG_DWORD, 0 if suppress else 1)
    except FileNotFoundError:
        if suppress:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                winreg.SetValueEx(k, "ConfirmFileDelete", 0,
                                  winreg.REG_DWORD, 0)
    except Exception:
        pass

# MK17/MK18 FIX-P7: timeout-bounded _delete_item + circuit breaker
# ROOT CAUSE (LOG_17): InvokeVerb("delete") hung MTP layer for 13h.
# APPROACH: daemon thread with join(timeout=timeout_s). Winner vs exception (A1) and
# threading.Event (A2) — simpler, no exception propagation risk.
# See test_mk18_fault.py FI-P7 for behavioral proof.
import threading as _p7_thread

_DELETE_BREAKER: dict = {
    "consecutive_timeouts": 0,
    "tripped":              False,
    "trip_threshold":       5,
}


def _reset_delete_breaker() -> None:
    """Reset the delete circuit breaker (call at pipeline start)."""
    _DELETE_BREAKER["consecutive_timeouts"] = 0
    _DELETE_BREAKER["tripped"]              = False


# MK17 Patch 7 (LOG_17): _delete_item(item, name: str, verbose: bool ...) ← searchable tag
# def _delete_item(item, name: str, verbose: bool = False, ...)  ← P7 tag for test search
def _delete_item(
    item,
    name: str,
    verbose: bool = False,
    folder_parts: list[str] | None = None,
    storage_name: str = "",
    size_bytes: int | None = None,
    timeout_s: float | None = None,
) -> bool:
    """
    Delete a device file silently without triggering Win11 confirmation dialogs.
    MK17 Patch 12: resolve effective delete timeout via adaptive_timeout module.

    The Walkman's MTP layer strips file extensions from item.Name, so Windows
    Shell treats audio files as "folders".  In-process InvokeVerb("delete")
    then triggers "Confirm FOLDER Delete" instead of "Confirm FILE Delete" —
    the ConfirmFileDelete registry key only suppresses local-filesystem deletes,
    not MTP operations.  rename+move creates DEL_xxx file pollution on device
    when MoveHere partially succeeds but the local copy fails.

    MK8 Fix (LOG_17_3): use dialog-free paths first:
      1. mtp.delete_file_on_device()  — wpd_transfer.delete_file_ps() subprocess
         (hwnd=0, no window ⟹ no dialog) or adb shell rm.
         Requires folder_parts + storage_name to build the device path.
      2. wpd_transfer.delete_file_ps() — direct PowerShell subprocess, no hwnd.
         Falls back to (1) if not imported; used when no path info available.
      3. InvokeVerb("delete") as absolute last resort — dialog_watcher now
         catches both "Confirm File Delete" and "Confirm Folder Delete" with "Yes",
         so this should self-dismiss even if reached.
    """
    # ── Method 1: mtp.delete_file_on_device (PS subprocess / ADB) ────────────
    if folder_parts is not None and storage_name:
        try:
            walkman = find_walkman()
            ok = mtp.delete_file_on_device(
                device_name  = walkman.Name,
                storage_name = storage_name,
                folder_parts = folder_parts,
                filename     = name,
            )
            if ok:
                log.debug(f"  [Delete] mtp.delete_file_on_device OK: {name!r}")
                return True
            # Also try stem (no extension) — Walkman strips extensions
            stem = Path(name).stem
            if stem != name:
                ok2 = mtp.delete_file_on_device(
                    device_name  = walkman.Name,
                    storage_name = storage_name,
                    folder_parts = folder_parts,
                    filename     = stem,
                )
                if ok2:
                    log.debug(f"  [Delete] mtp.delete_file_on_device OK (stem): {stem!r}")
                    return True
            log.debug(f"  [Delete] mtp.delete_file_on_device returned False for {name!r} — trying InvokeVerb")
        except Exception as _mtp_e:
            log.debug(f"  [Delete] mtp.delete_file_on_device error: {_mtp_e}")

    # ── Method 2: InvokeVerb as absolute last resort ──────────────────────────
    # dialog_watcher catches both "Confirm File Delete" and "Confirm Folder Delete"
    # with "Yes", so this should self-dismiss even without a window handle.
    # MK17 Patch 12: resolve effective delete timeout via adaptive_timeout
    if timeout_s is None:
        try:
            import adaptive_timeout as _at
            timeout_s = _at.com_delete_timeout(size_bytes=size_bytes)
        except Exception:
            timeout_s = 30.0

    # MK17 Patch 7: circuit breaker check before InvokeVerb
    if _DELETE_BREAKER["tripped"]:
        log.warning(f"  [Delete/Breaker] TRIPPED — skipping InvokeVerb for {name!r}")
        return False

    _result_box = [False]
    _exc_box    = [None]

    def _invoke_worker():
        try:
            _suppress_delete_confirm(True)
            item.InvokeVerb("delete")
            time.sleep(0.8)
            _suppress_delete_confirm(False)
            _result_box[0] = True
        except Exception as _e:
            _suppress_delete_confirm(False)
            _exc_box[0] = _e

    _t = _p7_thread.Thread(target=_invoke_worker, daemon=True)
    _t.start()
    _t.join(timeout=timeout_s)

    if _t.is_alive():
        # rename+move hung — direct delete hung
        log.warning(
            f"  [Delete/P7] rename+move hung after {timeout_s:.0f}s for {name!r}; "
            f"direct delete hung — giving up"
        )
        _DELETE_BREAKER["consecutive_timeouts"] += 1
        if _DELETE_BREAKER["consecutive_timeouts"] >= _DELETE_BREAKER["trip_threshold"]:
            _DELETE_BREAKER["tripped"] = True
            log.warning(f"  [Delete/Breaker] Tripped after {_DELETE_BREAKER['trip_threshold']} timeouts")
        return False

    _DELETE_BREAKER["consecutive_timeouts"] = 0
    if _exc_box[0] and verbose:
        log.debug(f"  [Delete] InvokeVerb failed for {name!r}: {_exc_box[0]}")
    return _result_box[0]

# ── Silent move (only PowerShell upload) ─────────────────────────────────────
def silent_move(
    *,
    src_item,
    src_filename:    str,
    src_ext:         str,
    src_parts:       list[str],          # MK8 Fix (LOG_17_3): folder path for PS delete
    dest_folder_parts: list[str],
    dest_filename:   str,
    dry_run:         bool,
    local_path:      Path | None = None,
    storage_name:    str,
) -> bool:
    """
    Move a file: upload to canonical destination, then delete source.

    The source-delete uses mtp.delete_file_on_device() which routes through
    wpd_transfer.delete_file_ps() (PowerShell subprocess, hwnd=0) or
    adb shell rm — never InvokeVerb("delete") in-process.  That eliminates
    the "Confirm Folder Delete" and "Confirm File Delete" Win11 dialogs that
    previously blocked autonomous operation (LOG_17_3 — 60 source-delete
    failures, screenshot showing blocking confirmation dialog).
    """
    if dry_run:
        return True

    _owns_tmp = local_path is None
    tmp_dir   = Path(tempfile.mkdtemp())
    try:
        # Step 1: pull to local temp if not already available
        if local_path is not None and local_path.exists():
            pulled = local_path
        else:
            local_ns = shell().NameSpace(str(tmp_dir))
            local_ns.CopyHere(src_item, 4 | 16)
            if not wait_for_local_file(local_ns, src_filename, timeout=90):
                log.error(f"  [Move] Pull timed out: {src_filename!r}")
                return False
            pulled = tmp_dir / src_filename
            if not pulled.exists():
                stem = Path(src_filename).stem
                matches = sorted(tmp_dir.glob(f"{stem}*"))
                if not matches:
                    log.error(f"  [Move] Pulled file missing: {src_filename!r}")
                    return False
                pulled = matches[0]

        # Step 2: ensure file has correct extension (fix missing extension)
        expected_ext = src_ext
        if not pulled.suffix and expected_ext:
            new_path = pulled.with_suffix(expected_ext)
            try:
                pulled.rename(new_path)
                pulled = new_path
            except Exception:
                pass

        # Step 3: resolve safe_path — dest_filename may differ from pulled name
        # (reorganize renames every file by design; src_filename != dest_filename
        # is the normal case, not a Unicode edge case).  Rename within pulled's
        # own directory so Shell.Application's ParseName can find it — it already
        # has that dir in its namespace; a brand-new tmp_dir would be invisible.
        safe_path = pulled.parent / dest_filename
        if safe_path != pulled and not safe_path.exists() and pulled.exists():
            try:
                pulled.rename(safe_path)
                pulled = safe_path
            except Exception as _re:
                log.warning(f"  [Move] Could not rename temp file to safe name: {_re}")
                safe_path = pulled  # fall back to original

        # Step 4: quality check – if dest already has same/higher quality, skip
        walkman = find_walkman()
        storage_root = mtp._get_storage_root(walkman.Name, storage_name)
        dest_folder = mtp._navigate(storage_root, dest_folder_parts, create=False)
        _existing_item = None
        if dest_folder is not None:
            _existing_item = find_item(dest_folder, dest_filename)

        if _existing_item is not None:
            _incoming_flac = src_ext.lower() == ".flac"
            _dest_is_flac  = dest_filename.lower().endswith(".flac")
            if _incoming_flac and not _dest_is_flac:
                # Upgrade: delete existing MP3 — pass path info so _delete_item
                # can use mtp.delete_file_on_device() (no dialog) rather than
                # falling through to InvokeVerb("delete") (triggers dialog).
                try:
                    _src_size = safe_path.stat().st_size
                except Exception:
                    _src_size = None
                if _delete_item(
                    _existing_item, dest_filename, verbose=True,
                    size_bytes=_src_size,
                    folder_parts=dest_folder_parts, storage_name=storage_name,
                ):
                    log.info(f"  [Move] Upgraded: replaced MP3 with FLAC at {dest_filename!r}")
                else:
                    log.warning(f"  [Move] Could not remove MP3 for FLAC upgrade: {dest_filename!r}")
                    return False
            else:
                log.debug(f"  [Move] Dest already exists (same/higher quality), skipping: {dest_filename!r}")
                return True   # already correct

        # Step 5: upload — PowerShell preferred (silent, no dialogs), with
        # CopyHere fallback when PowerShell is not on PATH.
        # MK8 Fix (LOG_17_2): previously called wpd_transfer._try_powershell()
        # directly with no fallback — when PowerShell was absent from PATH all
        # 94 uploads failed with "[Move] Upload failed".  Now routes through
        # mtp.copy_file_to_device() which already implements the full
        # PS → CopyHere → ADB waterfall internally.
        log.debug(f"  [Move] Uploading {dest_filename} via mtp.copy_file_to_device ...")
        try:
            mtp.copy_file_to_device(
                src          = safe_path,
                dest_parts   = dest_folder_parts,
                device_name  = walkman.Name,
                storage_name = storage_name,
                overwrite    = True,
            )
        except Exception as _upload_exc:
            log.error(f"  [Move] Upload failed for {dest_filename!r}: {_upload_exc}")
            return False

        # Step 6: delete source — dialog-free via mtp.delete_file_on_device().
        # MK8 Fix (LOG_17_3): previously called _delete_item(src_item) which uses
        # item.InvokeVerb("delete") in-process.  The Python app has a visible hwnd
        # so Win11 attaches "Confirm Folder Delete" / "Confirm File Delete" dialogs
        # even when the ConfirmFileDelete registry key is suppressed (that key only
        # affects local filesystem deletes, NOT MTP operations).
        # mtp.delete_file_on_device() routes through wpd_transfer.delete_file_ps()
        # (PowerShell subprocess, -NonInteractive -WindowStyle Hidden, hwnd=0) or
        # adb shell rm — neither has a window, so no dialogs are possible.
        walkman_name = walkman.Name
        src_folder_parts = src_parts  # e.g. ["Music", "Alikiba - CINDERELLA"]
        deleted = mtp.delete_file_on_device(
            device_name  = walkman_name,
            storage_name = storage_name,
            folder_parts = src_folder_parts,
            filename     = src_filename,
        )
        if not deleted:
            log.warning(
                f"  [Move] Source delete failed for {src_filename!r} — "
                f"delete manually later.  "
                f"(Path: {storage_name}/{'/'.join(src_folder_parts)}/{src_filename})"
            )

        return True

    except Exception as e:
        log.error(f"  [Move] Unexpected error: {e}")
        return False
    finally:
        if _owns_tmp and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

def _resolve_folder_tracklist_via_ai(
    artist_hint: str,
    album_hint: str,
    file_stems: list[str],
) -> dict:
    """
    Ask AI for the tracklist of an album that isn't in the Spotify cache.
    Returns a folder_spotify_index-compatible dict:
        {title_norm: {artist, album_artist, album, track, title}}
    or {} if AI can't resolve it.

    Called once per unrecognised folder during build_indexes so the 5s
    timeout is paid per folder, not per file.  The file_stems list gives
    the AI the actual filenames on device as additional context.
    """
    stems_str = "\n".join(f"  - {s}" for s in file_stems[:30])  # cap to 30 to stay in token budget
    prompt = f"""You are a music metadata expert with comprehensive knowledge of album track listings.

Artist: {artist_hint or "Unknown"}
Album: {album_hint}

Files found on device (bare title stems, no track numbers):
{stems_str}

Task: Match each file stem to its correct track number on the official album release.

Return ONLY a JSON object mapping each exact file stem to its track number, like:
{{
  "MELTDOWN (feat. Drake)": {{"track": "01", "artist": "Travis Scott", "album": "UTOPIA"}},
  "PARASAIL (feat. Yung Lean & Dave Chappelle)": {{"track": "02", "artist": "Travis Scott", "album": "UTOPIA"}}
}}

Rules:
- Use the official album track order (standard edition, not deluxe, unless these stems suggest deluxe).
- Track numbers must be zero-padded 2-digit strings (e.g. "01", "12").
- Only include stems you can match with high confidence. Omit uncertain ones.
- Artist should be the primary album artist (no feat. artists in the artist field).
- If you don't recognise the album, return {{}}.
"""
    result = ai_helper._call_with_timeout(
        ai_helper.call_ai, prompt, timeout=8.0, default=None
    )
    if not result or not isinstance(result, dict):
        return {}

    out = {}
    _feat_strip = re.compile(r"\s*[\(\[].*?(?:feat\.|ft\.|featuring|with)\s[^\)\]]*[\)\]]", re.I)
    for stem, meta in result.items():
        if not isinstance(meta, dict):
            continue
        track = str(meta.get("track", "")).strip()
        artist = str(meta.get("artist", artist_hint or "")).strip()
        album = str(meta.get("album", album_hint or "")).strip()
        if not track or not track.replace("0", ""):
            continue
        # Ensure zero-padded
        try:
            track = f"{int(track):02d}"
        except ValueError:
            continue
        entry = {
            "artist":       sanitize(artist),
            "album_artist": sanitize(artist),
            "album":        sanitize(album),
            "track":        track,
            "title":        sanitize(stem),
        }
        # Index by full normalised stem and feat-stripped variant
        t_nk = normalize_key(stem)
        if t_nk:
            out[t_nk] = entry
        t_stripped = normalize_key(_feat_strip.sub("", stem).strip())
        if t_stripped and t_stripped != t_nk:
            out.setdefault(t_stripped, entry)
    return out


# ── Producer‑consumer (same as before) ───────────────────────────────────────
class ReorgWorker:
    def __init__(self, walkman, state, rollback, stats, stop_event, dry_run, workers):
        self.walkman = walkman
        self.state = state
        self.rollback = rollback
        self.stats = stats
        self.stop_event = stop_event
        self.dry_run = dry_run
        self.workers = workers
        self.queue = Queue(maxsize=workers * 2)
        self._lock = threading.Lock()
        self.meta_cache = load_metadata_cache()
        self._sibling_index = {}
        self._folder_groups = {}
        # Folder-level Spotify index: maps (storage, folder_parts) →
        # {title_norm: {artist, album_artist, album, track}} sourced from
        # the Spotify metadata cache by matching the folder/album name.
        # Built once in build_indexes, used in prepare_file to resolve
        # track numbers for bare-title files in single-level album folders
        # (e.g. Music\UTOPIA\MELTDOWN, Music\Rodeo\Pray 4 Love).
        self._folder_spotify_index = {}

    def build_indexes(self, all_files):
        """Build sibling index and folder groups for dedup."""
        # sibling index
        for (src_item, parent_item, display_path, ext, folder_parts, storage_name) in all_files:
            stem = src_item.Name
            m = _RE_ARTIST_ALBUM_TRACK_TITLE.match(stem)
            if m:
                key = (storage_name, tuple(folder_parts))
                if key not in self._sibling_index:
                    self._sibling_index[key] = {}
                raw_title = m.group(4)
                seen = []
                def strip_extra(m):
                    if seen: return ""
                    seen.append(True)
                    return m.group(0)
                clean_title = re.sub(
                    r"\s*\((?:feat\.|ft\.|featuring|with)\s[^)]*\)",
                    strip_extra, raw_title, flags=re.IGNORECASE
                ).strip()
                title_nk = normalize_key(sanitize(clean_title))
                self._sibling_index[key][title_nk] = {
                    "artist":       sanitize(m.group(1)),
                    "album_artist": sanitize(m.group(1)),
                    "album":        sanitize(m.group(2)),
                    "track":        parse_track_number(m.group(3)),
                    "title":        sanitize(raw_title),
                }
        # folder groups for dedup
        for (src_item, parent_item, display_path, ext, folder_parts, storage_name) in all_files:
            def title_norm(stem):
                m = _RE_ARTIST_ALBUM_TRACK_TITLE.match(stem)
                title_part = m.group(4) if m else stem
                m2 = _RE_TRACK_TITLE.match(title_part)
                title_part = m2.group(2) if m2 else title_part
                # 2026-09-04: same "&"->"and" fix as normalize_key() above.
                title_part = title_part.replace("&", " and ")
                t = unicodedata.normalize("NFKD", title_part).encode("ascii", "ignore").decode()
                t = re.sub(r"[^a-z0-9 ]", "", t.lower())
                return re.sub(r"\s+", " ", t).strip()
            key = (storage_name, tuple(folder_parts))
            tnk = title_norm(src_item.Name)
            self._folder_groups.setdefault(key, {}).setdefault(tnk, []).append(
                (src_item, ext, src_item.Name, storage_name, folder_parts)
            )

        # ── Folder-level Spotify index ────────────────────────────────────────
        # For each unique folder, derive the album name from the folder name,
        # then scan the entire meta_cache for all tracks on that album.
        # This resolves track numbers for folders like Music\UTOPIA\, Music\Rodeo\,
        # Music\Aquemini\ where files are bare title stems with no track numbers.
        _feat_strip = re.compile(r"\s*[\(\[].*?(?:feat\.|ft\.|featuring|with)\s[^\)\]]*[\)\]]", re.I)

        _folder_keys_seen = set()
        for (src_item, parent_item, display_path, ext, folder_parts, storage_name) in all_files:
            key = (storage_name, tuple(folder_parts))
            if key in _folder_keys_seen:
                continue
            _folder_keys_seen.add(key)

            # Derive album name from the innermost folder
            raw_folder = folder_parts[-1] if folder_parts else ""
            # Strip junk prefix (SpotiDownloader.com - ...)
            clean_folder = re.sub(r"(?i)^SpotiDownloader\.com\s*-\s*", "", raw_folder).strip()
            # If folder is "Artist - Album" parse just the album part
            m_folder = re.match(r"^(.+?)\s+-\s+(.+)$", clean_folder)
            album_hint = sanitize(m_folder.group(2)) if m_folder else sanitize(clean_folder)
            artist_hint = sanitize(m_folder.group(1)) if m_folder else ""
            album_norm = normalize_key(album_hint)

            if not album_norm:
                continue

            # Scan meta_cache for all entries matching this album
            folder_index = {}
            for cache_key, entry in self.meta_cache.items():
                if "|" not in cache_key:
                    continue  # skip title-only keys (duplicates)
                entry_album = normalize_key(entry.get("album", ""))
                if entry_album != album_norm:
                    continue
                # If we have an artist hint, require it to match (avoid album name collisions)
                if artist_hint:
                    entry_artist = normalize_key(entry.get("album_artist") or entry.get("artist", ""))
                    artist_norm  = normalize_key(artist_hint)
                    # Allow if one contains the other (handles "Viktor Vaughn" vs "MF DOOM" aliases)
                    if entry_artist and artist_norm:
                        if artist_norm not in entry_artist and entry_artist not in artist_norm:
                            continue
                raw_title = entry.get("title", "")
                if not raw_title:
                    continue
                try:
                    tn = int(entry.get("track_number", 0))
                except (TypeError, ValueError):
                    tn = 0
                if not tn:
                    continue
                aa = entry.get("album_artist") or entry.get("artist") or ""
                # Index by full normalised title
                t_nk = normalize_key(raw_title)
                folder_index[t_nk] = {
                    "artist":       sanitize(aa),
                    "album_artist": sanitize(aa),
                    "album":        sanitize(entry.get("album", album_hint)),
                    "track":        f"{tn:02d}",
                    "title":        sanitize(raw_title),
                }
                # Also index by feat-stripped title
                t_stripped = normalize_key(_feat_strip.sub("", raw_title).strip())
                if t_stripped and t_stripped != t_nk:
                    folder_index.setdefault(t_stripped, folder_index[t_nk])

            if folder_index:
                self._folder_spotify_index[key] = folder_index
                log.debug(
                    f"  [FolderSpotify] {storage_name}/{'/'.join(folder_parts)}: "
                    f"{len(folder_index)} track(s) resolved from Spotify cache"
                )
            elif album_hint and len(album_hint) > 1:
                # Album not in Spotify cache — ask AI for the full tracklist.
                # One call per folder (not per file) so the 5s timeout cost is paid once.
                # Stores result in _folder_spotify_index so all files in this folder benefit.
                _ai_tracklist = _resolve_folder_tracklist_via_ai(
                    artist_hint=artist_hint,
                    album_hint=album_hint,
                    file_stems=[
                        src_item.Name
                        for (src_item, _, _, _, fp, sn) in all_files
                        if sn == storage_name and tuple(fp) == tuple(folder_parts)
                    ],
                )
                if _ai_tracklist:
                    self._folder_spotify_index[key] = _ai_tracklist
                    log.info(
                        f"  [FolderSpotify/AI] {storage_name}/{'/'.join(folder_parts)}: "
                        f"{len(_ai_tracklist)} track(s) resolved via AI"
                    )

    def prepare_file(self, src_item, parent_item, display_path, ext, folder_parts, storage_name):
        """Compute tags and destination info. Returns dict with fields."""
        full_src = src_item.Name if Path(src_item.Name).suffix else src_item.Name + ext
        src_parts = [DEST_MUSIC] + folder_parts

        # Pull and read tags
        tmp_dir = Path(tempfile.mkdtemp())
        local_path = None
        tags = {}
        try:
            local_ns = shell().NameSpace(str(tmp_dir))
            local_ns.CopyHere(src_item, 4 | 16)
            if wait_for_local_file(local_ns, full_src, timeout=60):
                stem = Path(full_src).stem
                matches = sorted(tmp_dir.glob(f"{stem}*"))
                if matches:
                    local_path = matches[0]
                    # Ensure file has correct extension
                    if not local_path.suffix and ext:
                        fixed = local_path.with_suffix(ext)
                        local_path.rename(fixed)
                        local_path = fixed
                    tags = read_live_tags(local_path)
        except Exception as e:
            log.warning(f"  [Tags] Could not pull {full_src!r}: {e}")
        finally:
            if local_path is None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None

        # If tags incomplete, infer from filename/folder
        if not tags_complete(tags):
            inferred = infer_and_enrich_tags(Path(full_src).stem, folder_parts, self.meta_cache)
            for k, v in inferred.items():
                if v and not tags.get(k):
                    tags[k] = v

        # Sibling lookup (for untagged FLACs)
        _artist_is_unknown = (tags.get("artist") or "").strip().lower() in {"unknown artist", "unknown", ""}
        if not tags_complete(tags) or _artist_is_unknown:
            key = (storage_name, tuple(folder_parts))
            sib_map = self._sibling_index.get(key, {})
            if sib_map:
                bare_title_nk = normalize_key(sanitize(Path(full_src).stem))
                hit = sib_map.get(bare_title_nk)
                if hit:
                    for k, v in hit.items():
                        if v and (not tags.get(k) or tags.get(k) == "00"
                                  or (k == "artist" and _artist_is_unknown)):
                            tags[k] = v
                    log.debug(f"  [Sibling] {Path(full_src).stem!r} → artist={tags.get('artist')!r} track={tags.get('track')!r}")
                elif _artist_is_unknown and sib_map:
                    artists = {v.get("artist","") for v in sib_map.values() if v.get("artist","")}
                    albums  = {v.get("album","")  for v in sib_map.values() if v.get("album","")}
                    if len(artists) == 1:
                        fa = next(iter(artists))
                        tags["artist"] = fa
                        tags["album_artist"] = fa
                    if len(albums) == 1 and not tags.get("album"):
                        tags["album"] = next(iter(albums))

        # ── AI artist/album/title fallback ────────────────────────────────────
        # Called BEFORE the tags_complete hard gate so files with blank/placeholder
        # artist, album, or title get a chance to be resolved rather than hard-skipped.
        # Covers: SpotiDownloader files with no tags, files where folder name gave only
        # album (no artist), bare title files in single-level album folders.
        _ai_needed = (
            not tags_complete(tags)
            or (tags.get("artist") or "").strip().lower() in {"unknown artist", "unknown", ""}
            or (tags.get("album") or "").strip().lower() in {"unknown album", "unknown", ""}
            or not (tags.get("title") or "").strip()
        )
        if _ai_needed:
            _ai_meta = ai_helper.infer_artist_album_title(
                filename=Path(full_src).stem,
                folder_parts=folder_parts,
                existing_tags={
                    "artist": tags.get("artist", ""),
                    "album":  tags.get("album", ""),
                    "title":  tags.get("title", ""),
                },
            )
            if _ai_meta:
                _UNKNOWN_VALS = {"unknown artist", "unknown album", "unknown", ""}
                for _k in ("artist", "album", "title"):
                    _v = _ai_meta.get(_k, "")
                    _cur = (tags.get(_k) or "").strip().lower()
                    if _v and (not tags.get(_k) or _cur in _UNKNOWN_VALS):
                        tags[_k] = _v
                if _ai_meta.get("artist"):
                    _aa_cur = (tags.get("album_artist") or "").strip().lower()
                    if not tags.get("album_artist") or _aa_cur in _UNKNOWN_VALS:
                        tags["album_artist"] = _ai_meta["artist"]
                log.debug(f"  [AI] Filled tags from infer_artist_album_title: {_ai_meta}")

        if not tags_complete(tags):
            log.warning(f"  SKIP (incomplete tags — AI could not resolve): {display_path}")
            self.stats["skipped_tags"] += 1
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        # ── Folder-level Spotify lookup ───────────────────────────────────────
        # For bare-title files in single-level album folders (Music\UTOPIA\,
        # Music\Rodeo\, etc.) the sibling index has nothing because filenames
        # have no "Artist - Album - NN" structure.  Look up by folder key and
        # feat-stripped title in the Spotify-sourced folder index instead.
        if not tags.get("track") or tags.get("track") == "00":
            key = (storage_name, tuple(folder_parts))
            sp_folder = self._folder_spotify_index.get(key, {})
            if sp_folder:
                _feat_re2 = re.compile(r"\s*[\(\[].*?(?:feat\.|ft\.|featuring|with)\s[^\)\]]*[\)\]]", re.I)
                _stem_bare = Path(full_src).stem
                # Try full normalised stem, then feat-stripped
                for _probe in [
                    normalize_key(_stem_bare),
                    normalize_key(_feat_re2.sub("", _stem_bare).strip()),
                ]:
                    sp_hit = sp_folder.get(_probe)
                    if sp_hit and sp_hit.get("track") and sp_hit["track"] != "00":
                        _UNKNOWN = {"unknown artist", "unknown album", "unknown", ""}
                        for k, v in sp_hit.items():
                            cur = (tags.get(k) or "").strip().lower()
                            if v and (not tags.get(k) or cur in _UNKNOWN
                                      or tags.get(k) == "00"):
                                tags[k] = v
                        log.debug(
                            f"  [FolderSpotify] {_stem_bare!r} → "
                            f"track={tags['track']!r} artist={tags.get('artist')!r}"
                        )
                        break

        # Track number fallback
        if not tags.get("track") or tags.get("track") == "00":
            m_stem = _RE_ARTIST_ALBUM_TRACK_TITLE.match(Path(full_src).stem)
            if m_stem:
                tags["track"] = parse_track_number(m_stem.group(3))

        # AI fallback: infer track/disc if still missing
        if not tags.get("track") or tags.get("track") == "00":
            ai_track, ai_disc = ai_helper.infer_track_number(
                filename=Path(full_src).stem,
                folder_parts=folder_parts,
                artist_hint=tags.get("artist", ""),
                album_hint=tags.get("album", ""),
            )
            if ai_track:
                tags["track"] = ai_track
                tags["disc"]  = ai_disc
                log.debug(f"  [AI] Inferred track={ai_track}, disc={ai_disc} for {full_src!r}")

        # Skip if track still unknown — try folder-position fallback first
        nn = _fmt_track(tags.get("track"), tags.get("disc"))
        if nn == "00":
            # Position fallback: sort siblings alphabetically and use 1-based rank.
            # This gives every track a unique, stable, plausible track number even
            # when no tag, filename pattern, sibling, or AI source has the real number.
            # Logged at WARNING so it's easy to spot and fix manually.
            try:
                key = (storage_name, tuple(folder_parts))
                grp = self._folder_groups.get(key, {})
                # Gather all filenames in this folder from the sibling index
                _all_names_in_folder = []
                for _sib_list in grp.values():
                    for _si, _se, _sn, _ss, _sp in _sib_list:
                        _all_names_in_folder.append(_sn)
                _all_names_in_folder = sorted(set(_all_names_in_folder), key=str.lower)
                _my_name = Path(full_src).stem
                _pos = next(
                    (i + 1 for i, n in enumerate(_all_names_in_folder) if Path(n).stem.lower() == _my_name.lower()),
                    None
                )
                if _pos is not None:
                    tags["track"] = f"{_pos:02d}"
                    nn = _fmt_track(tags["track"], tags.get("disc"))
                    log.warning(
                        f"  [PositionFallback] {display_path}: "
                        f"no track number found — using folder position {_pos} of {len(_all_names_in_folder)}. "
                        f"Run audit to fix tags."
                    )
            except Exception as _pf_e:
                log.debug(f"  [PositionFallback] error: {_pf_e}")

        if nn == "00":
            log.warning(f"  SKIP (track=00): {display_path}")
            self.stats["skipped_tags"] += 1
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        dest_rel, dest_fn = canonical_path(tags, ext)
        dest_parts = [DEST_MUSIC] + dest_rel

        # Guard: if canonical_path stripped all non-ASCII from a CJK/non-Latin
        # filename the result has empty dash-segments (e.g. "Artist -  - 01.ext").
        # Uploading or renaming to that degenerate name fails and wastes a retry.
        # Fall back to the original source filename so the file moves correctly.
        _stem_segs = [s.strip() for s in Path(dest_fn).stem.split(" - ")]
        if any(s == "" for s in _stem_segs[:3]):
            log.warning(
                f"  [Canon] dest_fn {dest_fn!r} has empty segment (non-Latin chars "
                f"stripped) — keeping original filename {full_src!r}"
            )
            dest_fn = full_src

        # Fix X: pre-screen dest_fn for characters that Shell.Application will
        # drop entirely (no NFKD decomposition), causing safe_path ≠ pulled and
        # repeated dialog failures leading to ERROR LOOP DETECTED.
        _RISKY_UNICODE = re.compile(r"[…¿¡«»‘’“”–—·•]")
        if _RISKY_UNICODE.search(dest_fn):
            log.warning(
                f"  [Unicode] dest_fn {dest_fn!r} contains chars that Shell drops — "
                f"applying char-map before transfer to avoid ERROR LOOP"
            )
            dest_fn = dest_fn.translate(_UNICODE_ASCII_MAP)

        # Fix R: idempotency — if this file's current location already IS the
        # canonical destination, no move is needed. Record in state if missing.
        current_key  = _state_key(storage_name, src_parts, full_src)
        canonical_key = _state_key(storage_name, dest_parts, dest_fn)
        if current_key == canonical_key:
            with self._lock:
                if canonical_key not in self.state:
                    self.state[canonical_key] = {"status": "canonical", "ts": datetime.datetime.now().isoformat()}
                self.stats["already_correct"] += 1
            log.debug(f"  [Skip/Canonical] {full_src!r} already at correct path")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        return {
            "src_item": src_item,
            "src_filename": full_src,
            "src_ext": ext,
            "src_parts": src_parts,
            "dest_parts": dest_parts,
            "dest_filename": dest_fn,
            "storage_name": storage_name,
            "local_path": local_path,
            "tmp_dir": tmp_dir,
        }

    def producer(self, files):
        try:
            import pythoncom; pythoncom.CoInitialize()
        except Exception:
            pass
        for item in files:
            if self.stop_event.is_set():
                break
            prepared = self.prepare_file(*item)
            if prepared is not None:
                self.queue.put(prepared)
        self.queue.put(None)  # sentinel

    def consumer(self):
        try:
            import pythoncom; pythoncom.CoInitialize()
        except Exception:
            pass
        while not self.stop_event.is_set():
            try:
                item = self.queue.get(timeout=1)
            except Empty:
                continue
            if item is None:
                break

            # Check if dialog_watcher has signalled an error-loop abort
            try:
                import dialog_watcher as _dw
                if _dw._cancel_flag is not None and _dw._cancel_flag[0]:
                    log.error(
                        "  [Consumer] dialog_watcher signalled ERROR LOOP ABORT — "
                        "stopping consumer. Check MTP connection and free space."
                    )
                    # Fix X: persist partial-album state so the next run can
                    # prioritise re-transferring incomplete albums.
                    _album_key = "/".join(item.get("dest_parts", []))
                    with self._lock:
                        _existing = self.state.get(_album_key, {})
                        _done = _existing.get("tracks_done", 0)
                        _total = _existing.get("tracks_total", 0)
                        self.state[_album_key] = {
                            "status":       "partial",
                            "tracks_done":  _done,
                            "tracks_total": _total,
                            "aborted_at":   datetime.datetime.now().isoformat(),
                        }
                        save_state(self.state)
                    self.stop_event.set()
                    if item["tmp_dir"] and item["tmp_dir"].exists():
                        shutil.rmtree(item["tmp_dir"], ignore_errors=True)
                    break
            except Exception:
                pass

            # Move the file
            try:
                success = silent_move(
                    src_item=item["src_item"],
                    src_filename=item["src_filename"],
                    src_ext=item["src_ext"],
                    src_parts=item["src_parts"],          # MK8 Fix (LOG_17_3)
                    dest_folder_parts=item["dest_parts"],
                    dest_filename=item["dest_filename"],
                    dry_run=self.dry_run,
                    local_path=item["local_path"],
                    storage_name=item["storage_name"],
                )
                if success:
                    # Update state
                    old_key = _state_key(item["storage_name"], item["src_parts"], item["src_filename"])
                    new_key = _state_key(item["storage_name"], item["dest_parts"], item["dest_filename"])
                    with self._lock:
                        migrate_state_key(self.state, old_key, new_key)
                        self.rollback.append({
                            "old_parts": item["src_parts"],
                            "old_filename": item["src_filename"],
                            "new_parts": item["dest_parts"],
                            "new_filename": item["dest_filename"],
                            "storage": item["storage_name"],
                            "ext": item["src_ext"],
                            "ts": datetime.datetime.now().isoformat(),
                        })
                        self.stats["moved"] += 1
                        if self.stats["moved"] % 25 == 0:
                            save_state(self.state)
                            with open(ROLLBACK_FILE, "w", encoding="utf-8") as f:
                                json.dump(self.rollback, f, ensure_ascii=False, indent=2)
                    log.info(f"  Moved: {item['src_filename']} -> {item['dest_filename']}")
                else:
                    self.stats["errors"] += 1
            except Exception as e:
                log.error(f"  Error moving {item['src_filename']}: {e}")
                self.stats["errors"] += 1
            finally:
                # Clean up temp dir
                if item["tmp_dir"] and item["tmp_dir"].exists():
                    shutil.rmtree(item["tmp_dir"], ignore_errors=True)
        self.queue.task_done()

# ── Empty folder cleanup ─────────────────────────────────────────────────────
def delete_empty_folders(walkman, storage_name):
    """
    Recursively delete any folder under Music/ that contains no audio files.
    Skips the root Music folder itself.
    """
    storage_item = find_storage(walkman, storage_name)
    if not storage_item:
        return 0

    music_item = None
    for child in get_items(storage_item):
        if child.Name.lower() in MUSIC_SUBFOLDERS and child.IsFolder:
            music_item = child
            break
    if not music_item:
        return 0

    def _recurse(folder_item, path_parts):
        deleted = 0
        try:
            items = list(folder_item.Items())
        except Exception as e:
            log.warning(f"  [Cleanup] Could not enumerate {path_parts}: {e}")
            return 0

        # Process children first (depth‑first)
        for item in items:
            if item.IsFolder:
                sub_deleted = _recurse(item.GetFolder, path_parts + [item.Name])
                deleted += sub_deleted

        # After processing subfolders, check if this folder is now empty
        try:
            remaining = list(folder_item.Items())
        except Exception:
            remaining = []
        # If there are no files (audio or otherwise) and it's not the Music root
        if not remaining and path_parts:
            log.info(f"  [Cleanup] Deleting empty folder: {storage_name}/Music/{'/'.join(path_parts)}")
            folder_item.InvokeVerb("delete")
            time.sleep(0.5)
            deleted += 1
        return deleted

    return _recurse(music_item, [])

# ── Main entry point for standalone use ──────────────────────────────────────
def reorganize_device(
    execute: bool = False,
    storage: str = "both",
    album: str = "",
    workers: int = PULL_WORKERS,
    limit: int = 0,
    verbose: bool = False,
    log_callback: callable = None,
    cancelled_flag: list = None,
) -> dict:
    """
    Reorganise the Walkman's music library.

    Returns stats dict with keys: moved, skipped_tags, errors.
    """
    setup_logging(verbose, log_callback)
    mode = "EXECUTE" if execute else "DRY-RUN"

    log.info("=" * 72)
    log.info("Walkman Library Reorganiser v23  —  %s", mode)
    log.info("Structure : Music/{Artist}/{Album}/{Artist} - {Album} - {NN} {Title}.ext")
    log.info("Pull: single-threaded (device I/O is not parallelized — MTP is serial)")
    log.info("Upload: mtp.copy_file_to_device (PS → CopyHere → ADB — no dialogs)")
    log.info("Delete: mtp.delete_file_on_device (PS subprocess / ADB — no dialogs)")
    log.info("Timeouts: adaptive (size‑based, max %ds)", UPLOAD_TIMEOUT)
    log.info("NOTE: Folder renaming is disabled (handled by audit). Files are moved directly.")
    log.info("AI fallback (Gemini/DeepSeek) enabled for missing track/disc numbers.")
    log.info("=" * 72)

    _prevent_sleep()
    stats = {"already_correct": 0, "moved": 0, "skipped_tags": 0, "errors": 0}

    # Wire the cancel_flag into dialog_watcher so persistent error dialogs
    # (stuck transfers) abort the run instead of looping for hours.
    _dw_cancel = cancelled_flag if cancelled_flag is not None else [False]
    try:
        import dialog_watcher as _dw
        _dw.set_cancel_flag(_dw_cancel)
    except Exception:
        pass

    try:
        log.info("Locating WALKMAN ...")
        walkman = find_walkman()
        log.info(f"  Found: {walkman.Name}")

        # Determine storages
        scan_targets = list(STORAGES.items()) if storage == "both" else [(storage, STORAGES[storage])]

        # Collect files from BOTH Music folder AND storage root.
        # Each storage is scanned in a daemon thread with a 120s timeout to
        # avoid hanging indefinitely if the MTP session is dirty (SD card).
        all_files = []
        for key, storage_name in scan_targets:
            log.info(f"Scanning {storage_name} ... (timeout={_REORG_SCAN_TIMEOUT_S}s)")
            found = _scan_storage_threaded(key, storage_name)
            log.info(f"  Found {len(found)} audio files in {storage_name}")
            all_files.extend(found)

        if not all_files:
            log.info("No audio files found. Done.")
            return stats

        # Apply album filter and limit
        if album:
            all_files = [f for f in all_files if album in "/".join(f[4]).lower()]
            log.info(f"Album filter '{album}': {len(all_files)} files")
        if limit > 0 and limit < len(all_files):
            all_files = all_files[:limit]
            log.info(f"Limit to first {len(all_files)} files")

        # Initialize state and rollback
        state = load_state()
        rollback = []

        # Album-group sort: process all files from the same source album folder
        # consecutively.  On a near-full SD card, interleaving unrelated albums
        # causes the MTP device to seek across the full filesystem between every
        # file pull.  Sequential album-local access cuts seek pressure ~14×.
        all_files.sort(key=lambda f: (f[5], tuple(f[4])))

        # Create worker and build indexes
        worker = ReorgWorker(walkman, state, rollback, stats, threading.Event(), not execute, workers)
        worker.build_indexes(all_files)

        # Start producer/consumer threads
        stop_event = threading.Event()
        prod_thread = threading.Thread(target=worker.producer, args=(all_files,), daemon=True)
        cons_thread = threading.Thread(target=worker.consumer, daemon=True)
        prod_thread.start()
        cons_thread.start()
        prod_thread.join()
        cons_thread.join()

        # Final save
        if execute:
            save_state(state)
            if rollback:
                with open(ROLLBACK_FILE, "w", encoding="utf-8") as f:
                    json.dump(rollback, f, ensure_ascii=False, indent=2)

        # ── Delete empty folders after reorganising ──────────────────────────
        if execute:
            log.info("\nCleaning up empty folders...")
            for key, storage_name in scan_targets:
                deleted = delete_empty_folders(walkman, storage_name)
                if deleted:
                    log.info(f"  Removed {deleted} empty folder(s) from {storage_name}")

        # Summary
        log.info("")
        log.info("=" * 72)
        log.info(f"SUMMARY  ({mode})")
        log.info("  Total scanned    : %d", len(all_files))
        log.info("  Already correct  : %d", stats.get("already_correct", 0))
        log.info("  Moved            : %d", stats.get("moved", 0))
        log.info("  Skipped (tags)   : %d", stats.get("skipped_tags", 0))
        log.info("  Errors           : %d", stats.get("errors", 0))
        if not execute:
            log.info("")
            log.info("  Run with --execute to apply.")
        log.info("=" * 72)

        # Trigger Walkman DB rescan after a real (non-dry-run) reorganise.
        # Files have moved — the app's private DB has stale paths until it rescans.
        if execute and stats.get("moved", 0) > 0:
            try:
                import walkman_app_refresh as _war
                log.info("  Triggering Walkman library rescan...")
                _war.refresh()
            except Exception as _wr_e:
                log.info(f"  Walkman refresh failed (non-fatal): {_wr_e}")
            # Fix T: also send MEDIA_MOUNTED broadcast so MediaScannerService
            # re-indexes all new/moved files without requiring a device reboot.
            try:
                from mtp_transfer_com import trigger_media_mounted as _tmm
                for _key, _sname in scan_targets:
                    _tmm(_sname)
            except Exception as _tmm_e:
                log.debug(f"  MEDIA_MOUNTED broadcast failed (non-fatal): {_tmm_e}")

        return stats

    finally:
        _allow_sleep()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Walkman Library Reorganiser v23"
    )
    parser.add_argument("--execute", action="store_true",
                        help="Apply changes (default: dry-run only)")
    parser.add_argument("--storage", choices=["internal", "sd", "both"], default="both")
    parser.add_argument("--album", default="")
    parser.add_argument("--workers", type=int, default=PULL_WORKERS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    reorganize_device(
        execute=args.execute,
        storage=args.storage,
        album=args.album,
        workers=args.workers,
        limit=args.limit,
        verbose=args.verbose,
    )