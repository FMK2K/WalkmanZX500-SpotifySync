"""
downloader.py  —  Stage 0: Download missing tracks, transfer to Walkman, clean up
==================================================================================
Downloads tracks that are in Spotify playlists but absent from the Walkman.

Rev 16 — Album‑wise download with liked album priority, free‑space threshold,
         TIDAL URL provider, QQDL, Monochrome, salvage failure cache, extended timeouts.
"""

from __future__ import annotations

import os
import re
import sys
import json
import shutil
import logging
import pathlib
import subprocess
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, ID3NoHeaderError, POPM
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

import id3_helpers as _idh   # REV 14: central ID3 v2.4→v2.3 save helpers
from mutagen import File as MutagenFile

import config
import mtp_transfer as mtp
import device_library
import spotify_client as sc

# Rev 14.9.3 F2 — initial-download cover art.  Pre-14.9.3, cover art was
# only embedded by the audit phase, which (a) added a multi-hour delay
# before the user saw covers, and (b) failed entirely when the audit
# couldn't enumerate the device (LOG-15 case).  Now we fetch and embed
# during _tag_file so the file lands on the device with cover already in
# place.
try:
    import cover_art as _cover_art
    _COVER_ART_AVAILABLE = True
except Exception as _ca_err:
    _COVER_ART_AVAILABLE = False
    _cover_art = None

import urllib.request as _urlreq
import urllib.error as _urlerr


# Module-level cover cache — keyed by album_image URL.  Multiple tracks
# from the same album reuse the same fetched bytes.  Capped to avoid
# unbounded growth on large libraries.
_COVER_CACHE: dict[str, bytes] = {}
_COVER_CACHE_LOCK = threading.Lock()
_COVER_CACHE_MAX_ENTRIES = 200


def _fetch_cover_bytes(url: str, timeout: float = 10.0) -> bytes | None:
    """
    Rev 14.9.3 — fetch raw image bytes from a URL with a per-process cache.
    Returns None on any failure (caller should treat as "no cover available"
    rather than failing the download).
    """
    if not url or not isinstance(url, str):
        return None

    with _COVER_CACHE_LOCK:
        if url in _COVER_CACHE:
            return _COVER_CACHE[url]

    try:
        req = _urlreq.Request(url, headers={"User-Agent": "WalkmanSyncAgent/14.9.2"})
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if data and len(data) > 100:  # sanity: cover is at least a few KB
                with _COVER_CACHE_LOCK:
                    if len(_COVER_CACHE) < _COVER_CACHE_MAX_ENTRIES:
                        _COVER_CACHE[url] = data
                return data
    except (_urlerr.URLError, TimeoutError, ConnectionError, OSError) as e:
        log.debug(f"  [Cover] fetch failed for {url[:80]!r}: {e}")
    except Exception as e:
        log.debug(f"  [Cover] unexpected error for {url[:80]!r}: {e}")
    return None


def _embed_cover_for_track(path: pathlib.Path, track: dict) -> bool:
    """
    Rev 14.9.3 F2 — embed cover art into the freshly-tagged audio file.
    Returns True if cover was successfully embedded, False otherwise.
    Idempotent: if the file already has a cover, this is a no-op (returns True).

    Source priority:
      1. track['album_image'] — Spotify CDN URL (preferred; consistent quality)
      2. yt-dlp thumbnail in same dir (ext .jpg/.png/.webp matching basename)

    File format support:
      * .mp3 — APIC frame via cover_art.build_apic
      * .flac — FLAC Picture via cover_art.build_flac_picture
      * .m4a — MP4Cover via mutagen
    """
    if not _COVER_ART_AVAILABLE:
        return False

    ext = path.suffix.lower()
    _ID3_EXTS  = {".mp3", ".wav", ".aiff", ".aif", ".dsf", ".dff"}
    _OGG_EXTS  = {".ogg", ".opus"}
    _SUPPORTED = _ID3_EXTS | _OGG_EXTS | {".flac", ".m4a", ".wma", ".ape", ".wv"}
    if ext not in _SUPPORTED:
        return False

    # ── Idempotence check ─────────────────────────────────────────────
    try:
        if ext in _ID3_EXTS and ext != ".mp3":
            from mutagen.id3 import ID3 as _ID3
            _f = _ID3(path)
            if any(k.startswith("APIC") for k in _f.keys()):
                return True
        elif ext == ".mp3":
            id3 = ID3(path)
            if any(k.startswith("APIC") for k in id3.keys()):
                return True
        elif ext == ".flac":
            f = FLAC(path)
            if f.pictures:
                return True
        elif ext == ".m4a":
            m = MP4(path)
            if "covr" in m.tags:
                return True
        elif ext in _OGG_EXTS:
            _mf = MutagenFile(path)
            if _mf and _mf.tags and "metadata_block_picture" in _mf.tags:
                return True
        elif ext == ".wma":
            from mutagen.asf import ASF as _ASF
            _af = _ASF(path)
            if "WM/Picture" in _af.tags:
                return True
        elif ext in (".ape", ".wv"):
            from mutagen.apev2 import APEv2 as _APEv2
            try:
                _av = _APEv2(path)
                if "Cover Art (Front)" in _av:
                    return True
            except Exception:
                pass
    except Exception:
        pass  # tags may be malformed; treat as "no cover"

    # ── Source 1: album_image URL ─────────────────────────────────────
    img_bytes: bytes | None = None
    url = track.get("album_image") or track.get("cover_url") or ""
    if url:
        img_bytes = _fetch_cover_bytes(url)
        if img_bytes is None:
            log.debug(f"  [Cover] album_image fetch failed for {path.name!r}")

    # ── Source 2: yt-dlp thumbnail file ───────────────────────────────
    if img_bytes is None:
        for thumb_ext in (".jpg", ".jpeg", ".png", ".webp"):
            cand = path.with_suffix(thumb_ext)
            if cand.exists() and cand.stat().st_size > 100:
                try:
                    img_bytes = cand.read_bytes()
                    break
                except Exception:
                    continue

    if img_bytes is None:
        # MK8 Fix (LOG_17_3): upgraded from silent return to WARNING so
        # "everything downloaded without a cover" is visible in logs.
        # Primary cause: album_image="" in track dict (TIDAL path didn't
        # set it) + no yt-dlp thumbnail present for TIDAL/SpotiFLAC streams.
        artist_hint = track.get("artist") or track.get("album_artist") or "?"
        title_hint  = track.get("title") or path.stem
        if not url:
            log.warning(
                f"  [Cover] No album_image URL in track dict for "
                f"{artist_hint!r} — {title_hint!r} ({path.name}). "
                f"Cover will not be embedded."
            )
        else:
            log.warning(
                f"  [Cover] All cover sources exhausted for {path.name!r} "
                f"(URL fetch failed, no thumbnail). Cover will not be embedded."
            )
        return False

    # ── Embed ─────────────────────────────────────────────────────────
    # MK37/S4: lock-aware tag write for cover art embed. Uses
    # tag_file_with_coherence to coordinate with concurrent AV/indexer
    # access. Falls back to contextlib.nullcontext when MK37 unavailable.
    try:
        from mk37_wiring import tag_file_with_coherence
        _lock_ctx = tag_file_with_coherence
    except ImportError:
        from contextlib import nullcontext
        _lock_ctx = lambda _p: nullcontext()  # noqa: E731

    try:
        if ext == ".mp3":
            apic = _cover_art.build_apic(img_bytes)
            id3 = ID3(path)
            id3.delall("APIC")
            id3.add(apic)
            with _lock_ctx(path):
                _idh.save_v23_id3(id3)
            return True

        if ext == ".flac":
            pic = _cover_art.build_flac_picture(img_bytes)
            f = FLAC(path)
            f.clear_pictures()
            f.add_picture(pic)
            with _lock_ctx(path):
                f.save()
            return True

        if ext == ".m4a":
            data, mime = _cover_art.normalize_bytes(img_bytes)
            fmt = MP4Cover.FORMAT_JPEG if "jpeg" in mime else MP4Cover.FORMAT_PNG
            m = MP4(path)
            m.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
            with _lock_ctx(path):
                m.save()
            return True

        if ext in (".wav", ".aiff", ".aif", ".dsf", ".dff"):
            from mutagen.id3 import ID3 as _ID3, ID3NoHeaderError as _NoHdr
            apic = _cover_art.build_apic(img_bytes)
            try:
                _id3 = _ID3(path)
            except _NoHdr:
                _id3 = _ID3()
            _id3.delall("APIC")
            _id3.add(apic)
            with _lock_ctx(path):
                _idh.save_v23_id3(_id3)
            return True

        if ext in (".ogg", ".opus"):
            import base64 as _b64
            from mutagen.flac import Picture as _Pic
            _mf = MutagenFile(path)
            if _mf is None:
                return False
            data, mime = _cover_art.normalize_bytes(img_bytes)
            pic = _Pic()
            pic.type, pic.mime, pic.width, pic.height, pic.depth, pic.data = (
                3, mime, 0, 0, 0, data
            )
            encoded = _b64.b64encode(pic.write()).decode("ascii")
            _mf.tags["metadata_block_picture"] = [encoded]
            with _lock_ctx(path):
                _mf.save()
            return True

        if ext == ".wma":
            from mutagen.asf import ASF as _ASF, ASFByteArrayAttribute as _ASFA
            data, mime = _cover_art.normalize_bytes(img_bytes)
            _af = _ASF(path)
            _af.tags["WM/Picture"] = [_ASFA(
                b"\x03" + mime.encode() + b"\x00" + b"\x00" + data
            )]
            with _lock_ctx(path):
                _af.save()
            return True

        if ext in (".ape", ".wv"):
            from mutagen.apev2 import APEv2 as _APEv2
            data, _ = _cover_art.normalize_bytes(img_bytes)
            try:
                _av = _APEv2(path)
            except Exception:
                _av = _APEv2()
            _av["Cover Art (Front)"] = data
            with _lock_ctx(path):
                _av.save(path)
            return True

    except Exception as e:
        log.debug(f"  [Cover] embed failed for {path.name!r}: {e}")

    return False


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ffmpeg availability — checked once at import time (Bug 3 fix)
# ---------------------------------------------------------------------------
# We need this before any download attempt so the waterfall can avoid
# requesting M4A-only outputs when ffmpeg is absent, and so _tag_file can
# skip the raw-AAC→MP3 conversion branch that silently produces untagged files.
# The result is cached here so _find_ffmpeg() calls inside worker threads
# don't each re-scan PATH.

def _find_ffmpeg() -> str | None:
    """
    Locate a working ffmpeg install.  Returns the directory containing
    ffmpeg (or ffmpeg.exe on Windows), or None if unfound.

    REV 14.9.1 (D1) — This function is now loud about where it looked.
    LOG-13 Run 2 showed ffmpeg on PATH but yt-dlp invoked with no
    postprocessor because this function returned None; we never knew which
    resolution step failed.  Now every probed location is logged at DEBUG,
    and a final FOUND/NOT FOUND at INFO/CRITICAL respectively.

    Resolution order:
      1. shutil.which("ffmpeg") — honors PATH, but occasionally fails on
         Windows due to PATHEXT weirdness or PATH containing UNC paths.
      2. shutil.which("ffmpeg.exe") — explicit-extension fallback for #1.
      3. config.FFMPEG_EXE if set.
      4. Python-adjacent candidates (Scripts/, Library/bin/, etc.) —
         covers conda-installed ffmpeg and pip's imageio-ffmpeg shim.
      5. Common Windows install locations (Chocolatey, Scoop, standard
         C:\\ffmpeg\\bin, Program Files, winget).
      6. Bundled location: %BASE_DIR%/tools/ffmpeg/bin/ffmpeg.exe so the
         agent can carry its own copy.
    """
    tried: list[str] = []

    # Step 1: PATH-based discovery
    for name in ("ffmpeg", "ffmpeg.exe"):
        ff = shutil.which(name)
        tried.append(f"shutil.which({name!r})")
        if ff:
            ff_dir = str(pathlib.Path(ff).parent)
            log.debug(f"[ffmpeg] Resolved via PATH: {ff} (dir={ff_dir})")
            return ff_dir

    # Step 2: config override
    try:
        cfg_exe = getattr(config, "FFMPEG_EXE", None)
        if cfg_exe:
            p = pathlib.Path(cfg_exe)
            tried.append(f"config.FFMPEG_EXE={cfg_exe!r}")
            if p.exists():
                log.debug(f"[ffmpeg] Resolved via config.FFMPEG_EXE: {p}")
                return str(p.parent)
    except Exception:
        pass

    # Step 3: Python-adjacent candidates
    py_dir = pathlib.Path(sys.executable).parent
    ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    py_candidates = [
        py_dir, py_dir / "Scripts", py_dir / "Library" / "bin",
        py_dir / "Library" / "mingw-w64" / "bin", py_dir.parent,
        py_dir.parent / "bin", py_dir.parent / "Library" / "bin",
        py_dir.parent / "Scripts",
    ]
    for candidate_dir in py_candidates:
        ff = candidate_dir / ffmpeg_name
        tried.append(str(ff))
        if ff.exists():
            log.debug(f"[ffmpeg] Resolved via Python-adjacent: {ff}")
            return str(candidate_dir)

    # Step 4: Common Windows install locations
    if sys.platform == "win32":
        import os as _os
        user_home = pathlib.Path(_os.path.expanduser("~"))
        standalone = [
            pathlib.Path("C:/ffmpeg/bin"),
            pathlib.Path("C:/Program Files/ffmpeg/bin"),
            pathlib.Path("C:/Program Files (x86)/ffmpeg/bin"),
            pathlib.Path("C:/tools/ffmpeg/bin"),
            pathlib.Path("C:/ProgramData/chocolatey/bin"),   # choco-installed
            pathlib.Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin"),
            user_home / "scoop" / "apps" / "ffmpeg" / "current" / "bin",
            user_home / "scoop" / "shims",                   # scoop-installed
            pathlib.Path("C:/msys64/mingw64/bin"),           # msys2
            user_home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages",
        ]
        for p in standalone:
            ff = p / "ffmpeg.exe"
            tried.append(str(ff))
            if ff.exists():
                log.debug(f"[ffmpeg] Resolved via Windows install: {ff}")
                return str(p)

    # Step 5: Bundled
    try:
        base_dir = pathlib.Path(getattr(config, "BASE_DIR", "."))
        bundled = base_dir / "tools" / "ffmpeg" / "bin" / ffmpeg_name
        tried.append(str(bundled))
        if bundled.exists():
            log.debug(f"[ffmpeg] Resolved via bundled: {bundled}")
            return str(bundled.parent)
    except Exception:
        pass

    # Loud, one-time diagnostic dump so the next log tells us exactly
    # what was probed.  At DEBUG so it doesn't spam, but ready to grep.
    log.debug(
        f"[ffmpeg] NOT FOUND after checking {len(tried)} locations: "
        + "; ".join(tried[:20]) + (" ..." if len(tried) > 20 else "")
    )
    return None


_FFMPEG_DIR: str | None = _find_ffmpeg()

if _FFMPEG_DIR:
    log.info(f"[ffmpeg] Found at: {_FFMPEG_DIR}")
else:
    # REV 14.9.1 (D1) — This message was previously WARNING, which got lost
    # in the log noise and the user didn't realize yt-dlp was dead.  In
    # LOG-13 Run 2, this caused 15/15 yt-dlp attempts to download .webm
    # files that were then discarded, tripping the circuit breaker and
    # removing yt-dlp from the waterfall entirely.  The MC sim says
    # fixing this alone (when paired with a fresh TIDAL auth) adds ~70
    # tracks to a 6h session; leaving it broken costs 3% of the library.
    log.critical(
        "[ffmpeg] NOT FOUND on PATH or in any common install location.\n"
        "  ║  ► yt-dlp downloads will FAIL for any track whose YouTube source "
        "is .webm/.opus only (most music videos are).\n"
        "  ║  ► M4A tag-fixing via transcoding is disabled.\n"
        "  ║  ► The circuit breaker WILL disable yt-dlp after 15 failures, "
        "killing a whole provider for the session.\n"
        "  ║  Fix one of these:\n"
        "  ║    • Install:  choco install ffmpeg   OR   scoop install ffmpeg\n"
        "  ║    • Or set config.FFMPEG_EXE to the full path to ffmpeg.exe\n"
        "  ║    • Or drop ffmpeg.exe into %BASE_DIR%/tools/ffmpeg/bin/\n"
        "  ║  To diagnose why shutil.which() didn't find it, check DEBUG log "
        "for '[ffmpeg] NOT FOUND after checking N locations'."
    )

# REV 14.5: DOWNLOAD_WORKERS lowered from 8 to 4 after 2026-04-18 handle
# exhaustion incident.  Each worker spawns a subprocess tree per download
# attempt (up to 7 providers × ffmpeg); 4 workers × 7 providers × (main +
# ffmpeg) = ~56 concurrent processes peak.  8 workers pushed that to ~112
# which exceeded Windows' kernel-pool budget on sustained runs.
# Config override: config.DOWNLOAD_WORKERS takes precedence if set.
DOWNLOAD_WORKERS      = getattr(config, "DOWNLOAD_WORKERS", 4)
PROVIDER_TIMEOUT_S    = getattr(config, "DOWNLOAD_TIMEOUT_S", 180)   # back-compat default

# REV 14.6 — per-provider adaptive timeouts.
#
# Rationale: flat 180s per provider was catastrophic for the waterfall, because
# most providers fail FAST (HTTP 403, connection reset, login-missing) but the
# flat budget meant every failure still had to burn 180s.  Worst-case doomed-track
# budget under flat-180 was ~1,085 s (18 min per track).
#
# New per-provider budgets reflect the real failure modes:
#   - Search providers (tidaldl, qqdl, monochrome): fail < 1s on "not found"
#   - Login-gated (tiddl):  exits non-zero in < 1s when login is missing
#   - Queue-based (spotiflac):  can legitimately take 60s
#   - YouTube-backed (ytdlp, spotdl): the only ones that LEGITIMATELY need >60s
#
# Callers use _timeout_for(provider, duration_ms) to get the effective budget,
# which applies duration scaling on top of these base values.
PROVIDER_TIMEOUTS: dict = getattr(config, "PROVIDER_TIMEOUTS", {
    "tidaldl":      30,
    "tidaldl_url":  45,
    "qqdl":         30,
    "monochrome":   45,
    "tiddl":        15,
    "spotiflac":    60,
    "streamrip":    30,   # SINGLE command now (see _try_streamrip fix)
    "ytdlp":       120,
    "spotdl":       90,
})

# Duration scaling.  Short tracks (interludes, skits) don't need full budget;
# long tracks (20min sets, live recordings) may legitimately need more.
DURATION_SCALE_LONG_THRESHOLD_S  = getattr(config, "DURATION_SCALE_LONG_THRESHOLD_S",  360)  # 6 min
DURATION_SCALE_LONG_MULT         = getattr(config, "DURATION_SCALE_LONG_MULT",         1.5)
DURATION_SCALE_SHORT_THRESHOLD_S = getattr(config, "DURATION_SCALE_SHORT_THRESHOLD_S",  60)
DURATION_SCALE_SHORT_MULT        = getattr(config, "DURATION_SCALE_SHORT_MULT",        0.6)
DURATION_SCALE_FLOOR_S           = getattr(config, "DURATION_SCALE_FLOOR_S",           10)


def _timeout_for(provider: str, duration_ms: int = 0) -> int:
    """Effective per-call timeout for (provider, track length).

    Applies PROVIDER_TIMEOUTS[provider] as the base, then scales by track
    duration if known. 10s hard floor so we never go below a viable budget.
    """
    base = PROVIDER_TIMEOUTS.get(provider, PROVIDER_TIMEOUT_S)
    if duration_ms and duration_ms > 0:
        mins = duration_ms / 60_000.0
        if mins * 60 > DURATION_SCALE_LONG_THRESHOLD_S:
            base = int(base * DURATION_SCALE_LONG_MULT)
        elif mins * 60 < DURATION_SCALE_SHORT_THRESHOLD_S:
            base = int(base * DURATION_SCALE_SHORT_MULT)
    return max(DURATION_SCALE_FLOOR_S, int(base))


# Circuit breaker — thread-safe provider disablement.
# After _CIRCUIT_THRESHOLD consecutive failures with ZERO successes on a given
# provider, we stop scheduling that provider for the rest of the session.
# This primarily catches cases like tiddl-with-no-login or a dead API host
# that would otherwise burn 15s × every_track × every_run.
import threading as _threading_cb
_CIRCUIT_LOCK           = _threading_cb.Lock()
_CIRCUIT_FAIL_COUNTS    : dict = {}   # provider -> consecutive-fail count
_CIRCUIT_SUCCESSES      : dict = {}   # provider -> success count this session
_CIRCUIT_DISABLED       : set  = set()
_CIRCUIT_THRESHOLD      = getattr(config, "PROVIDER_CIRCUIT_FAIL_THRESHOLD", 15)
_CIRCUIT_ENABLED        = getattr(config, "PROVIDER_CIRCUIT_BREAKER_ENABLED", True)


def _provider_record_success(provider: str) -> None:
    if not _CIRCUIT_ENABLED:
        return
    with _CIRCUIT_LOCK:
        _CIRCUIT_SUCCESSES[provider] = _CIRCUIT_SUCCESSES.get(provider, 0) + 1
        _CIRCUIT_FAIL_COUNTS[provider] = 0   # reset the streak


def _provider_record_failure(provider: str) -> None:
    if not _CIRCUIT_ENABLED:
        return
    with _CIRCUIT_LOCK:
        n = _CIRCUIT_FAIL_COUNTS.get(provider, 0) + 1
        _CIRCUIT_FAIL_COUNTS[provider] = n
        if (n >= _CIRCUIT_THRESHOLD
                and _CIRCUIT_SUCCESSES.get(provider, 0) == 0
                and provider not in _CIRCUIT_DISABLED):
            _CIRCUIT_DISABLED.add(provider)
            log.warning(
                f"  [CircuitBreaker] Disabling provider '{provider}' for the "
                f"rest of this session: {n} consecutive failures, 0 successes."
            )


def _provider_is_disabled(provider: str) -> bool:
    if not _CIRCUIT_ENABLED:
        return False
    with _CIRCUIT_LOCK:
        return provider in _CIRCUIT_DISABLED


def _circuit_summary() -> str:
    with _CIRCUIT_LOCK:
        parts = []
        for p, s in sorted(_CIRCUIT_SUCCESSES.items(), key=lambda x: -x[1]):
            f = _CIRCUIT_FAIL_COUNTS.get(p, 0)
            state = "DISABLED" if p in _CIRCUIT_DISABLED else "active"
            parts.append(f"{p}={s}ok/{f}fail[{state}]")
        for p in _CIRCUIT_DISABLED:
            if p not in _CIRCUIT_SUCCESSES:
                parts.append(f"{p}=0ok/?fail[DISABLED]")
        return "  ".join(parts) if parts else "(no activity)"
HEALTH_CHECK_INTERVAL = 25
MTP_TRANSFER_RETRIES  = 3
ALBUM_DOWNLOAD_THRESHOLD_MISSING = getattr(config, "ALBUM_DOWNLOAD_THRESHOLD_MISSING", 3)
PRIORITIZE_LIKED_ALBUMS = getattr(config, "PRIORITIZE_LIKED_ALBUMS", True)
ALBUM_DOWNLOAD_MIN_FREE_SPACE_GB = getattr(config, "ALBUM_DOWNLOAD_MIN_FREE_SPACE_GB", 50)
DOWNLOAD_STRATEGY = getattr(config, "DOWNLOAD_STRATEGY", "auto")

# Bug 5: SD free space is re-queried at most once per SD_FREE_RECHECK_INTERVAL
# albums.  Without this the threshold check runs once at process start and
# never sees space decrease — so full-album mode stays active even after the
# SD card drops below the 50 GB safety threshold mid-run.
SD_FREE_RECHECK_INTERVAL = 20   # re-query SD free space every N albums
_sd_free_cache: dict = {"gb": None, "albums_since_check": 0, "lock": None}


def _get_sd_free_gb_via_adb() -> float:
    """
    MK37.2-4: ADB-first SD free-space probe.
    LOG_8_5_26:447 showed adb shell df DID return correct numbers
    (`/mnt/media_rw/4A21-0000 244465664 127988736 116476928 53% /storage/4A21-0000`)
    while MTP enumeration returned UNKNOWN 135 times in the same run.
    Promote ADB to primary, fall back to MTP only if ADB itself errors.

    Returns free GB, or -1.0 if unable to determine.
    """
    try:
        import subprocess as _sp_dl
        # Attempt the SD card mount path; the volume label `4A21-0000` is
        # the device's current SD UUID (MK14-RC2 dynamic detection writes
        # this to config; if absent, try the literal path which is the
        # observed value across recent runs).
        sd_mount = getattr(config, "SD_CARD_MOUNT", None) or "/storage/4A21-0000"
        result = _sp_dl.run(
            ["adb", "shell", "df", "-k", sd_mount],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not result.stdout:
            return -1.0
        # Parse last data row. df output:
        #   Filesystem 1K-blocks Used Available Use% Mounted on
        #   /dev/...   244465664 127988736 116476928 53% /storage/4A21-0000
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4 and sd_mount in line:
                try:
                    avail_kb = int(parts[3])
                    return avail_kb / (1024 ** 2)   # KB → GB
                except (ValueError, IndexError):
                    continue
        return -1.0
    except Exception as _e:
        log.debug(f"  [SDFreeCache/ADB] probe failed: {_e}")
        return -1.0


def _get_sd_free_gb_cached() -> float:
    """
    Return cached SD card free space in GB.
    Refreshes the cache every SD_FREE_RECHECK_INTERVAL album calls.
    Thread-safe via a module-level lock initialised on first use.

    MK37.2-4: ADB-first probe (works in environments where MTP
    enumeration silently fails). Falls back to MTP only on ADB error.
    Logs marker SDFREE_ADB_OK / SDFREE_MTP_OK / SDFREE_BOTH_FAILED so
    the operator can see which path succeeded.

    Rev 14.9.3 — when the underlying query returns -1 ("can't determine"),
    we now distinguish that from 0.0 ("SD genuinely full") in the log so
    the operator can tell whether it's a query bug or a real-space issue.
    The cached value is still 0.0 (caller treats unknown as full = safe
    fallback to per-track) but the log is honest about why.
    """
    global _sd_free_cache
    if _sd_free_cache["lock"] is None:
        _sd_free_cache["lock"] = threading.Lock()

    with _sd_free_cache["lock"]:
        _sd_free_cache["albums_since_check"] += 1
        if (
            _sd_free_cache["gb"] is None
            or _sd_free_cache["albums_since_check"] >= SD_FREE_RECHECK_INTERVAL
        ):
            _sd_free_cache["albums_since_check"] = 0

            # MK37.2-4: ADB-primary
            adb_gb = _get_sd_free_gb_via_adb()
            if adb_gb >= 0:
                _sd_free_cache["gb"] = adb_gb
                if not _sd_free_cache.get("logged_adb_ok"):
                    log.info(
                        f"  [SDFreeCache] SDFREE_ADB_OK: {adb_gb:.1f} GB free on SD card "
                        f"(via adb shell df, MK37.2-4 primary path)"
                    )
                    _sd_free_cache["logged_adb_ok"] = True
                return _sd_free_cache["gb"]

            # MK37.2-4: ADB failed; fall back to MTP enumeration
            raw = mtp.get_device_free_space(config.DEVICE_NAME, config.SD_STORAGE)
            if raw == -1:
                _sd_free_cache["gb"] = 0.0
                if not _sd_free_cache.get("logged_both_failed"):
                    log.warning(
                        "  [SDFreeCache] SDFREE_BOTH_FAILED: SD card free-space "
                        "query returned UNKNOWN via both ADB and MTP. Treating "
                        "as 0 GB free → per-track strategy will be used. This "
                        "is a safe fallback but not optimal; check that the "
                        "SD card is properly mounted on the device. "
                        "This warning will not repeat."
                    )
                    _sd_free_cache["logged_both_failed"] = True
            else:
                _sd_free_cache["gb"] = raw / (1024 ** 3)
                if not _sd_free_cache.get("logged_mtp_ok"):
                    log.info(
                        f"  [SDFreeCache] SDFREE_MTP_OK: {_sd_free_cache['gb']:.1f} GB "
                        f"free on SD card (via MTP fallback; ADB df unavailable)"
                    )
                    _sd_free_cache["logged_mtp_ok"] = True
        return _sd_free_cache["gb"]

# Global cache for salvage failures (per‑run)
_SALVAGE_FAIL_CACHE: dict[str, int] = {}

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# MK15 FIX-D: FLAC search sanitizer
# ---------------------------------------------------------------------------
import re as _re_dl


def _sanitize_for_flac_search(text: str) -> str:
    """Strip noisy suffixes from artist/title before FLAC source searches."""
    # Featured artists
    text = _re_dl.sub(
        r"\s*[\(\[]\s*(?:feat|ft|featuring)[^)\]]*[\)\]]",
        "", text, flags=_re_dl.IGNORECASE
    )
    for pat in [
        r"\s*[\(\[]\s*Official\s+(?:Music\s+)?(?:Audio|Video)\s*[\)\]]",
        r"\s*[\(\[]\s*Lyric\s+Video\s*[\)\]]",
        r"\s*[\(\[]\s*Sped\s*Up\s*[\)\]]",
        r"\s*[\(\[]\s*Slowed(?:\s*\+\s*Reverb)?\s*[\)\]]",
        r"\s*[\(\[]\s*(?:Tik\s*Tok|TikTok)\s*[\)\]]",
        r"\s*[\(\[]\s*\d{1,3}\s*[\)\]]",
        r"\s*-\s*(?:Remix|Mix|Radio\s+Edit)\s*$",
    ]:
        text = _re_dl.sub(pat, "", text, flags=_re_dl.IGNORECASE)
    return _re_dl.sub(r"\s{2,}", " ", text).strip(". -_")


# TIDAL album search cache  (I2)
# ---------------------------------------------------------------------------
# Persisted to .tidal_album_cache.json alongside the download cache so that
# re-runs of the same liked albums skip the session.search() round-trip.
# Key: "<album_artist>||<album_name>" (lowercased)
# Value: {"tidal_id": int, "tidal_artist": str, "tidal_name": str}

_TIDAL_ALBUM_CACHE_FILE = pathlib.Path(
    getattr(config, "BASE_DIR", pathlib.Path("."))
) / ".tidal_album_cache.json"
_tidal_album_cache: dict = {}
_tidal_album_cache_dirty: bool = False
_tidal_album_cache_lock = threading.Lock()


# ─── REV 14.9 (D3) — Thread-safe transport outcome counters ──────────────
# The transfer sites (copy_file_to_device calls) are buried several function
# levels below run_downloads, and we don't want to plumb the stats dict
# through 6 intermediate function signatures.  Instead we use a module-level
# counter with a lock; run_downloads drains these into the stats dict each
# Progress cycle for inclusion in the [Progress] line (D1) and [Transport]
# summary line (D3).
_XFER_LOCK = threading.Lock()
_XFER_COUNTERS = {"ok": 0, "fail": 0, "skip": 0}
_XFER_FIRST_FAILURE_LOGGED = False  # REV 14.9 (D2) — one snapshot per session

# ──────────────────────────────────────────────────────────────────────────
# MK17 Patch 5 — run-level health counters
# ──────────────────────────────────────────────────────────────────────────
# Single-element lists used as cheap thread-safe counters (no lock needed
# for ints because of the GIL on simple assignment, but we're cautious
# anyway because some of these increment during error-path handling that
# may itself be racing).  Drained at end of run() into the health summary.
_XFER_COUNTERS_TAGFAIL       = [0]   # tag-write failures that exhausted ALL recovery
_XFER_COUNTERS_TAGFALLBACK   = [0]   # tag-write rescues via copy-tag-replace fallback
_XFER_COUNTERS_UNKNOWN_FREE  = [0]   # _pick_storage_for_file returned 'both unknown'
_XFER_COUNTERS_VERIFY_FAIL   = [0]   # post-transfer verification failures (ADB or COM)
_XFER_COUNTERS_COM_FALLBACK  = [0]   # transports that fell back from ADB to COM
_XFER_COUNTERS_PROVIDER_EXHAUST = [0]  # tracks for which all providers failed


def _health_reset() -> None:
    """Reset all health counters to zero.  Called at the top of run_downloads."""
    for c in (_XFER_COUNTERS_TAGFAIL, _XFER_COUNTERS_TAGFALLBACK,
              _XFER_COUNTERS_UNKNOWN_FREE, _XFER_COUNTERS_VERIFY_FAIL,
              _XFER_COUNTERS_COM_FALLBACK, _XFER_COUNTERS_PROVIDER_EXHAUST):
        c[0] = 0


def _health_summary() -> dict:
    """Return a snapshot of the current run-level health counters.
    Caller decides whether to log, tally, or fail-the-run based on values.
    """
    return {
        "tag_failures":          _XFER_COUNTERS_TAGFAIL[0],
        "tag_fallbacks":         _XFER_COUNTERS_TAGFALLBACK[0],
        "unknown_free_events":   _XFER_COUNTERS_UNKNOWN_FREE[0],
        "verify_failures":       _XFER_COUNTERS_VERIFY_FAIL[0],
        "com_fallbacks":         _XFER_COUNTERS_COM_FALLBACK[0],
        "provider_exhaustions":  _XFER_COUNTERS_PROVIDER_EXHAUST[0],
    }


# Rev 14.9.4 D2 — mid-run free-space watchdog state.
# Every MIDRUN_CHECK_INTERVAL successful transfers, peek at device free space.
# If below MIN_FREE_SPACE_GB, log CRITICAL and tell the user.  Does NOT halt
# the run automatically — the user might have a tolerance for the floor
# being crossed once near end-of-run.  But the line is loud enough to
# investigate.
_MIDRUN_TRANSFER_COUNT = 0
_MIDRUN_CHECK_LOCK = threading.Lock()
MIDRUN_CHECK_INTERVAL = 25  # check every N successful transfers


def _xfer_bump(kind: str, n: int = 1) -> None:
    """Thread-safe counter bump.  kind in {'ok','fail','skip'}."""
    global _MIDRUN_TRANSFER_COUNT
    with _XFER_LOCK:
        if kind in _XFER_COUNTERS:
            _XFER_COUNTERS[kind] += n
    # D2 mid-run check: only on success counts, throttled
    if kind == "ok":
        with _MIDRUN_CHECK_LOCK:
            _MIDRUN_TRANSFER_COUNT += n
            if _MIDRUN_TRANSFER_COUNT >= MIDRUN_CHECK_INTERVAL:
                _MIDRUN_TRANSFER_COUNT = 0
                # Run the check unsynchronized after release — we hold the
                # lock only for counter bookkeeping.
                _do_midrun = True
            else:
                _do_midrun = False
        if _do_midrun:
            try:
                import free_space_check as _fsc
                _fsc.midrun_floor_check()
            except Exception as e:
                log.debug(f"[FreeSpace] mid-run check raised: {e}")


def _xfer_drain() -> tuple[int, int, int]:
    """Atomically read-and-reset the counters.  Returns (ok, fail, skip)."""
    with _XFER_LOCK:
        ok = _XFER_COUNTERS["ok"]
        fail = _XFER_COUNTERS["fail"]
        skip = _XFER_COUNTERS["skip"]
        _XFER_COUNTERS["ok"] = 0
        _XFER_COUNTERS["fail"] = 0
        _XFER_COUNTERS["skip"] = 0
    return ok, fail, skip


def _xfer_first_failure_snapshot() -> None:
    """
    REV 14.9 (D2) — On the first transfer failure of a session, capture a
    device-state snapshot (df + Music/ ls head) to the log.  Subsequent
    failures do not re-log.  This gives LOG-13-style post-mortem analysis
    the forensic data at no ongoing cost.
    """
    global _XFER_FIRST_FAILURE_LOGGED
    with _XFER_LOCK:
        if _XFER_FIRST_FAILURE_LOGGED:
            return
        _XFER_FIRST_FAILURE_LOGGED = True

    try:
        from adb_transfer import _adb_cmd
    except Exception:
        return

    try:
        df_out = _adb_cmd(["shell", "df", "-k"], timeout=15, check=False)
        log.warning(
            "[Transport/snapshot] First transfer failure of session — "
            "capturing device df:\n%s",
            (df_out or "").strip()[:2000],
        )
    except Exception:
        pass

    try:
        ls_out = _adb_cmd(
            ["shell", "ls", "-la", "/storage/emulated/0/Music/"],
            timeout=30, check=False,
        )
        # Cap at ~50 lines
        lines = (ls_out or "").splitlines()[:50]
        log.warning(
            "[Transport/snapshot] /storage/emulated/0/Music/ (first 50 entries):\n%s",
            "\n".join(lines),
        )
    except Exception:
        pass


def _xfer_get_device_index():
    """
    REV 14.9 (C2) — Fetch the session-scoped device index if walkman_sync
    populated one.  Returns None if unavailable; callers must treat None
    as "no cache, do live probes" (equivalent to pre-14.9 behaviour).
    """
    try:
        import device_index as _di_mod
        return _di_mod.get_default_index()
    except Exception:
        return None


def _load_tidal_album_cache() -> None:
    global _tidal_album_cache
    try:
        if _TIDAL_ALBUM_CACHE_FILE.exists():
            _tidal_album_cache = json.loads(
                _TIDAL_ALBUM_CACHE_FILE.read_text(encoding="utf-8")
            )
    except Exception:
        _tidal_album_cache = {}


def _save_tidal_album_cache() -> None:
    global _tidal_album_cache_dirty
    try:
        _TIDAL_ALBUM_CACHE_FILE.write_text(
            json.dumps(_tidal_album_cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _tidal_album_cache_dirty = False
    except Exception as e:
        log.debug(f"  [TidalAlbumCache] Could not save: {e}")


# ---------------------------------------------------------------------------
# Liked-album processed set  (I4)
# ---------------------------------------------------------------------------
# On incremental runs, liked albums that were fully processed last run need
# their TIDAL search + track-enumerate + skip-loop to run again even though
# there is nothing new to download.  This set persists across runs (stored
# inside the download cache under key "__liked_processed__") and lets us
# skip the expensive TIDAL enumeration for albums where nothing changed.

_LIKED_PROCESSED_CACHE_KEY = "__liked_processed__"


def _get_liked_processed(dl_cache: dict) -> set:
    return set(dl_cache.get(_LIKED_PROCESSED_CACHE_KEY, []))


def _mark_liked_processed(dl_cache: dict, cache_lock: threading.Lock, album_key: str) -> None:
    with cache_lock:
        processed = dl_cache.get(_LIKED_PROCESSED_CACHE_KEY, [])
        if album_key not in processed:
            processed = list(processed) + [album_key]
            dl_cache[_LIKED_PROCESSED_CACHE_KEY] = processed


# ---------------------------------------------------------------------------
# Download result cache
# ---------------------------------------------------------------------------

def _load_download_cache() -> dict:
    try:
        p = getattr(config, "DOWNLOAD_CACHE_FILE", None)
        if p and pathlib.Path(p).exists():
            return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_download_cache(cache: dict) -> None:
    try:
        p = getattr(config, "DOWNLOAD_CACHE_FILE", None)
        if p:
            pathlib.Path(p).write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception as e:
        log.warning(f"[DownloadCache] Could not save: {e}")


# ---------------------------------------------------------------------------
# File validation helpers
# ---------------------------------------------------------------------------

def _is_valid(path: pathlib.Path) -> bool:
    try:
        with open(path, "rb") as f:
            h = f.read(12)
        if len(h) < 4:
            return False
        ext = path.suffix.lower()
        if ext == ".flac":
            return h[:4] == b"fLaC"
        if ext == ".mp3":
            return h[:3] == b"ID3" or (h[0] == 0xFF and (h[1] & 0xE0) == 0xE0)
        if ext in (".m4a", ".aac", ".mp4"):
            if len(h) >= 8 and h[4:8] == b"ftyp":
                return True
            if h[0] == 0xFF and (h[1] & 0xF0) == 0xF0:
                return True
            try:
                return path.stat().st_size > 1024
            except Exception:
                return len(h) > 0
        if ext == ".webm":
            return h[:4] == b"\x1a\x45\xdf\xa3"
        return len(h) > 0
    except Exception:
        return False


def _remediate_corrupt_flac(path: pathlib.Path) -> pathlib.Path | None:
    """
    I5: Attempt to fix a .flac file that fails the fLaC magic-byte check.

    Two strategies, tried in order:
      1. Header sniff — if the file is actually a valid MP3 or M4A, simply
         rename it to the correct extension and return the new path.
         This costs zero CPU and fixes the most common case (yt-dlp writing
         lossy audio into a .flac container).
      2. ffmpeg transcode — if the file has a recognisable audio codec that
         ffmpeg can decode (opus, webm, etc.), re-encode to MP3 and return
         the new .mp3 path.  The original is deleted on success.

    Returns the fixed path on success, or None if remediation was not possible.
    The caller is responsible for updating tags and re-transferring.
    """
    if not path.exists():
        return None
    try:
        with open(path, "rb") as fh:
            h = fh.read(12)
    except Exception:
        return None

    # Strategy 1: wrong extension
    if h[:3] == b"ID3" or (len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):
        new_path = path.with_suffix(".mp3")
        try:
            path.rename(new_path)
            log.info(f"  [I5/Remediate] Renamed corrupt .flac -> .mp3: {new_path.name}")
            return new_path
        except Exception as e:
            log.debug(f"  [I5/Remediate] Rename failed: {e}")
            return None

    if len(h) >= 8 and h[4:8] == b"ftyp":
        new_path = path.with_suffix(".m4a")
        try:
            path.rename(new_path)
            log.info(f"  [I5/Remediate] Renamed corrupt .flac -> .m4a: {new_path.name}")
            return new_path
        except Exception as e:
            log.debug(f"  [I5/Remediate] Rename failed: {e}")
            return None

    # Strategy 2: ffmpeg transcode
    if not _FFMPEG_DIR:
        log.debug(f"  [I5/Remediate] ffmpeg absent — cannot transcode {path.name}")
        return None

    mp3_path = path.with_suffix(".mp3")
    ffmpeg_bin = str(pathlib.Path(_FFMPEG_DIR) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-y", "-i", str(path),
             "-codec:a", "libmp3lame", "-qscale:a", "0", str(mp3_path)],
            capture_output=True, check=False, timeout=120,
        )
        if result.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 4096:
            path.unlink(missing_ok=True)
            log.info(f"  [I5/Remediate] ffmpeg transcoded corrupt .flac -> .mp3: {mp3_path.name}")
            return mp3_path
        else:
            log.debug(f"  [I5/Remediate] ffmpeg failed for {path.name}: {result.stderr[-200:]!r}")
    except Exception as e:
        log.debug(f"  [I5/Remediate] ffmpeg exception: {e}")

    return None


def _force_rmtree(path: pathlib.Path) -> None:
    import stat as _stat

    def _on_error(func, fpath, exc_info):
        try:
            pathlib.Path(fpath).chmod(
                pathlib.Path(fpath).stat().st_mode | _stat.S_IWRITE
            )
            func(fpath)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onerror=_on_error)
    except Exception:
        shutil.rmtree(path, ignore_errors=True)

    if path.exists():
        time.sleep(1.5)
        shutil.rmtree(path, ignore_errors=True)


def _collect_downloaded_files(directory: pathlib.Path) -> list:
    EXTS  = (".flac", ".mp3", ".m4a", ".ogg", ".aac", ".opus", ".webm")
    ORDER = {e: i for i, e in enumerate(EXTS)}
    files = [
        f for f in directory.rglob("*")
        if f.is_file()
        and f.suffix.lower() in EXTS
        and _is_valid(f)
    ]
    files.sort(key=lambda f: ORDER.get(f.suffix.lower(), 99))
    return files


# ---------------------------------------------------------------------------
# Name / tagging helpers
# ---------------------------------------------------------------------------

def _safe(name: str, max_len: int = 60) -> str:
    """
    Sanitise a string for use as a path component on FAT32/exFAT/NTFS.

    Rev 14.9.7 (P2) — balances unmatched parentheses after truncation.
    LOG_16_1 captured 5 ADB push failures where titles like
    "Dali Nguwe (feat. Nkosazana Daughter, Basetsana and Obeey Am)" got
    truncated at 60 chars to "Dali Nguwe (feat. Nkosazana Daughter,
    Basetsana and Obeey Am" — losing the closing ')' and producing an
    ugly orphan-open filename.  We now detect unmatched '(' '[' '{' and
    strip from the orphan onward, so the example becomes "Dali Nguwe".
    Also handles trailing connectors (", and Ob" -> "").
    """
    name = name.replace("&", "and")
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    if len(name) <= max_len:
        return name

    truncated = name[:max_len]

    # If truncation orphaned an opening bracket, strip from that bracket
    # onward.  Track each bracket type independently.
    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
        if truncated.count(opener) > truncated.count(closer):
            # Strip from the orphan opener and everything after
            idx = truncated.rfind(opener)
            truncated = truncated[:idx].rstrip(" ,;:-")

    return truncated.rstrip(". ")


def _ascii_safe(name: str) -> str:
    name = name.replace("&", "and")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return re.sub(r"\s+", " ", name).strip(". ") or "track"


def _track_filename(track: dict) -> str:
    """
    Build the canonical destination filename for a track.

    Rev 14.9.3 F6 — guards against empty-title tracks.  Pre-14.9.3 a track
    with title='' would silently produce filenames like
    'Dan Mason - Miami Virtual - 01.mp3' (no title), and two such tracks
    in the same album would push to the SAME path and overwrite each
    other (LOG-15 case).  Now we raise ValueError so callers can choose
    to skip vs synthesize.
    """
    album_artist = _safe(track.get("album_artist") or track.get("artist", "Unknown"), 40)
    album        = _safe(track.get("album", "Unknown"), 60)
    disc         = str(track.get("disc_number", "1"))
    num          = str(track.get("track_number", "1")).zfill(2)
    title_raw    = str(track.get("title") or "").strip()
    if not title_raw:
        # Rev 14.9.3 — caller MUST handle this.  Fail loudly rather than
        # produce a filename that collides with siblings.
        raise ValueError(
            f"_track_filename refused empty title for "
            f"{album_artist} - {album} (track {num})"
        )
    title        = _safe(title_raw, 60)

    artists = track.get("artists") or [track.get("artist", "")]
    if len(artists) > 1 and config.FEATURE_ARTISTS_IN_TITLE:
        feat = ", ".join(a for a in artists[1:] if a)
        feat_part = f" (feat. {feat})" if feat else ""
    else:
        feat_part = ""

    cd_part = f"CD{disc} - " if disc not in ("", "1") else ""

    return f"{album_artist} - {album} - {cd_part}{num} {title}{feat_part}"


def _album_folder_name(track: dict) -> str:
    artist = _safe(track.get("album_artist") or track.get("artist", "Unknown"), 40)
    album  = _safe(track.get("album", "Unknown"), 60)
    return f"{artist} - {album}"


def _strip_feat(title: str) -> str:
    return re.sub(
        r"\s*[\(\[\{](?:feat|ft|featuring)[^\)\]\}]*[\)\]\}]",
        "", title, flags=re.I,
    ).strip()


# Rev 14.9.4 D5 — yt-dlp title junk-stripper.
# Spotify and TIDAL deliver clean titles ("Daydreaming", "Down In A Hole").
# yt-dlp's source titles are scraped from YouTube descriptions and frequently
# contain "(Official Video)", "[HD 1080p]", "(Lyric Video)", "(Audio)",
# "(Live at X)", "[Music Video]", "feat. Drake" embedded in the title vs the
# title-credit, etc.  Most paths in this codebase set track["title"] from the
# clean Spotify source before yt-dlp runs, so the tag never sees junk — but
# album-wise (TIDAL) and the rare local-data-only case can still leak it.
# This function is conservative — it strips only well-known junk patterns and
# leaves anything else untouched.  The crucial property: bracketed content
# that contains a real word (not a junk keyword) like "(Girls)" stays.
# Idempotent.
_TITLE_JUNK_KEYWORDS = (
    r"official|hd|hq|4k|1080p|720p|2160p|uhd|stereo|mono|"
    r"music\s*video|lyric\s*video|lyrics|video|audio|version|release|"
    r"remaster(?:ed)?|explicit|"
    r"official\s*audio|official\s*video|official\s*music\s*video|"
    r"audio\s*only|hd\s*remaster(?:ed)?|hq\s*audio"
)
_TITLE_JUNK_PATTERNS = [
    # bracketed content composed entirely of junk keywords (with optional digits/whitespace)
    rf"\s*[\(\[\{{]\s*(?:(?:{_TITLE_JUNK_KEYWORDS})\s*\d*\s*)+[\)\]\}}]",
    # bare-year suffix "(2019)" at end
    r"\s*[\(\[\{]\s*(?:19|20)\d{2}\s*[\)\]\}]\s*$",
    # year+remaster like "(2022 Remaster)"
    r"\s*[\(\[\{]\s*\d{4}\s+remaster(?:ed)?\s*[\)\]\}]",
    # trailing " - Official Video"
    rf"\s*-\s*(?:{_TITLE_JUNK_KEYWORDS})(?:\s+(?:{_TITLE_JUNK_KEYWORDS}))*\s*$",
]
_TITLE_JUNK_RE = re.compile("|".join(_TITLE_JUNK_PATTERNS), flags=re.IGNORECASE)


def _clean_title(title: str) -> str:
    """
    Strip well-known yt-dlp / YouTube junk patterns from a title while
    preserving the actual song name.  Intended for titles that came from
    a non-Spotify source.  Spotify/TIDAL titles pass through unchanged
    (the patterns don't match cleanly-formatted titles).

    Crucial property: bracketed content with a non-junk word like
    "(Girls)" or "(Live at the Royal Albert Hall)" is preserved.  Only
    brackets composed entirely of junk keywords are stripped.
    """
    if not title or not isinstance(title, str):
        return title
    cleaned = _TITLE_JUNK_RE.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|｜")
    # Don't return empty — fall back to original if cleaning removed everything
    return cleaned or title.strip()


def _norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]", "", t.lower())
    t = re.sub(r"^(the |a |an )", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


# ──────────────────────────────────────────────────────────────────────────
# Rev 14.9.6 R2 — smart title-casing engine.
# Python's str.title() is famously bad at music titles: "Don't" becomes
# "Don'T", "feat." becomes "Feat.", proper-noun prefixes like "DJ" or
# "MC" lose their all-caps status, and short connecting words get capped
# when they shouldn't.  This engine applies the AP/Chicago Manual of Style
# convention: capitalize the first and last words always, and every word
# in between except articles, short prepositions, and conjunctions.
# Idempotent — applying it twice produces the same result.
#
# The user's request was "pixel-level control" applied programmatically.
# This is the pixel-level layer: exact, rule-based, deterministic.

# Words that stay lowercase when in the middle of a title.
_LOWERCASE_WORDS = frozenset({
    "a", "an", "the",                          # articles
    "and", "but", "for", "nor", "or", "so", "yet",  # coord conjunctions
    "as", "at", "by", "in", "of", "on", "to", "up", "via",  # short preps
    "vs", "vs.", "feat", "feat.",              # standard music abbreviations
    "ft", "ft.", "with",
})

# Tokens that should always be ALL-CAPS regardless of position.
_ALWAYS_UPPER = frozenset({
    "DJ", "MC", "OG", "II", "III", "IV", "VI", "VII", "VIII", "IX",
    "USA", "UK", "EP", "LP", "TV", "OK", "OG", "DMV",
    "AKA", "FYI", "NYC", "LA", "LDN", "ATL", "OG", "NWA",
})


def _smart_title_case(s: str) -> str:
    """
    Apply music-library title-case rules to a string.

    Idempotent.  Returns the string unchanged when the input is already
    correctly cased (so users who curate their own tags aren't fought
    against).
    """
    if not s or not isinstance(s, str):
        return s
    # Heuristic: only apply if the input looks ALL-LOWERCASE or ALL-UPPERCASE.
    # If it has any mixed case, assume the source already curated it
    # (e.g. Spotify, MusicBrainz, AC/DC, KIDS SEE GHOSTS — wait, that's
    # ALL-CAPS, see below).
    has_lower = any(c.islower() for c in s)
    has_upper = any(c.isupper() for c in s)

    # Special carve-out: if the string is ALL-CAPS and has no lowercase
    # letters, the user probably did not type it that way (yt-dlp /
    # YouTube descriptions love SHOUTING).  Exceptions:
    #   - short string ≤ 4 alpha chars (acronym like "MGMT", "$AP")
    #   - short string with internal punctuation / slashes (AC/DC, R.E.M.)
    # Cleanup ALL-CAPS unless one of those exceptions applies.
    alpha_chars = sum(1 for c in s if c.isalpha())
    has_internal_punct = bool(re.search(r"[\w][\.\-/&'][\w]", s))
    if has_upper and not has_lower:
        if alpha_chars <= 4:
            return s  # likely intentional acronym (MGMT, NWA)
        if has_internal_punct and alpha_chars <= 6:
            return s  # AC/DC, R.E.M., MF.DOOM, etc.
        # else fall through to titlecase rules

    elif has_upper and has_lower:
        # mixed case — already curated, leave alone
        return s

    # Tokenize on whitespace AND keep punctuation attached
    tokens = s.split()
    if not tokens:
        return s

    out = []
    for i, tok in enumerate(tokens):
        # Strip leading/trailing punctuation for keyword matching
        bare = re.sub(r"[^\w'.\-]", "", tok)  # keep apostrophes, dots, hyphens
        bare_lower = bare.lower()

        # First or last word always cased
        is_edge = (i == 0 or i == len(tokens) - 1)

        if bare in _ALWAYS_UPPER or bare.upper() in _ALWAYS_UPPER:
            out.append(tok.replace(bare, bare.upper()) if bare else tok.upper())
        elif (not is_edge) and bare_lower in _LOWERCASE_WORDS:
            out.append(tok.lower())
        else:
            # Capitalize first letter, preserve rest as-was (but don't
            # blindly call .title() which mangles "don't" → "Don'T").
            # Strategy: lowercase whole token, then uppercase first letter.
            lowered = tok.lower()
            # Find the first letter (skip leading quotes/punctuation)
            for j, ch in enumerate(lowered):
                if ch.isalpha():
                    out.append(lowered[:j] + ch.upper() + lowered[j + 1:])
                    break
            else:
                out.append(lowered)

    return " ".join(out)


def _convert_raw_aac_to_mp3(input_path: pathlib.Path, output_dir: pathlib.Path) -> pathlib.Path | None:
    ffmpeg_dir = _find_ffmpeg()
    if not ffmpeg_dir:
        return None

    ffmpeg_bin = str(pathlib.Path(ffmpeg_dir) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))
    mp3_path = output_dir / (input_path.stem + ".mp3")
    cmd = [
        ffmpeg_bin, "-y", "-i", str(input_path),
        "-codec:a", "libmp3lame", "-qscale:a", "0", str(mp3_path)
    ]
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=60)
    if result.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 0:
        log.info(f"  [Convert] Converted raw AAC to MP3: {mp3_path.name}")
        return mp3_path
    return None


def _tag_file(path: pathlib.Path, track: dict) -> pathlib.Path:
    """
    Write tags to a downloaded audio file.  Returns the path on disk after
    tagging — for .m4a inputs that fail MP4 validation and get rescued by
    _convert_raw_aac_to_mp3, the returned path is the new .mp3 file; for
    all other inputs it's the input path unchanged.

    BUGFIX (Rev 14.9.2): previous signature returned None and reassigned
    `path` locally during the m4a→mp3 rescue, but callers kept the old
    .m4a reference and then hit WinError 2 ("file not found") on transfer
    because the .m4a had been unlinked.  Every caller MUST now capture the
    return value and use that for subsequent operations.
    """
    ext = path.suffix.lower()
    # ZX500 requires a non-empty AlbumArtist to group album tracks correctly.
    # If album_artist is missing, fall back to artist.  Never write an empty string.
    aa = (track.get("album_artist") or track.get("artist") or "Unknown").strip()
    yr = track.get("release_year") or track.get("spotify_year", "")

    # Rev 14.9.4 D5 — strip yt-dlp junk patterns from title before tagging.
    # Idempotent: clean Spotify/TIDAL titles pass through unchanged.
    raw_title = track.get("title", "") or ""
    title = _clean_title(raw_title)
    if title != raw_title:
        log.debug(f"  [TitleClean] {raw_title!r} -> {title!r}")

    # Rev 14.9.4 D6 — NFC-normalise all string tags so the Walkman's
    # MediaScanner sees consistent character representation across tracks.
    # Without this, a NFD-encoded "Dan Mason ダン·メイソン" from one track
    # and a NFC-encoded one from another get different ALBUM_ID hashes
    # in MediaStore and the album splinters.  cheap, idempotent, no-op
    # for ASCII input.
    def _nfc(s: str) -> str:
        return unicodedata.normalize("NFC", str(s or ""))

    # Rev 14.9.6 R2 — apply smart title-case to title, artist, album, and
    # albumartist tags.  Only fires on inputs that look ALL-LOWERCASE or
    # ALL-UPPERCASE — already-curated mixed-case strings (Spotify, TIDAL)
    # pass through unchanged.  This is the "pixel-level control"
    # equivalent the user asked for: a deterministic, programmatic
    # rules engine, not a human in a GUI.
    def _normalize_text_tag(value: str) -> str:
        if not value:
            return value
        normalized = _smart_title_case(_nfc(value))
        return normalized

    artist_tag       = _normalize_text_tag(track.get("artist", ""))
    album_tag        = _normalize_text_tag(track.get("album", ""))
    albumartist_tag  = _normalize_text_tag(aa)
    title_tag        = _normalize_text_tag(title)
    genre_tag        = _nfc(track.get("genre", "Unknown"))  # genre already canonicalised by genre_client

    # Normalise track/disc numbers: strip "X/Y" total-track format the ZX500
    # does not parse cleanly — write only the integer track position.
    def _clean_num(val, default="1") -> str:
        s = str(val or default).strip()
        return s.split("/")[0].strip() or default

    track_num = _clean_num(track.get("track_number"), "1")
    disc_num  = _clean_num(track.get("disc_number"),  "1")

    if ext == ".flac":
        audio = FLAC(path)
        audio["title"]       = title_tag
        audio["artist"]      = artist_tag
        audio["album"]       = album_tag
        audio["albumartist"] = albumartist_tag
        audio["date"]        = yr
        audio["tracknumber"] = track_num
        audio["discnumber"]  = disc_num
        audio["genre"]       = genre_tag
        if track.get("is_liked"):
            audio["rating"] = "100"
        # MK37/S4: lock-aware tag write (board ticket P1-1).
        # Closes LOG_5_5_26 Permission denied class (AV / indexer races).
        try:
            from mk37_wiring import tag_file_with_coherence
            with tag_file_with_coherence(path):
                audio.save()
        except ImportError:
            audio.save()
        # Rev 14.9.3 F2 — embed cover art at download time
        try:
            _embed_cover_for_track(path, track)
        except Exception as _ce:
            log.debug(f"  [Cover] FLAC embed raised for {path.name!r}: {_ce}")
        return path

    if ext == ".mp3":
        try:
            audio = MP3(path, ID3=EasyID3)
        except ID3NoHeaderError:
            MP3(path).add_tags()
            audio = MP3(path, ID3=EasyID3)
        audio["title"]       = title_tag
        audio["artist"]      = artist_tag
        audio["album"]       = album_tag
        audio["albumartist"] = albumartist_tag
        audio["date"]        = yr
        audio["tracknumber"] = track_num
        audio["discnumber"]  = disc_num
        audio["genre"]       = genre_tag
        # MK37/S4: lock-aware tag write
        try:
            from mk37_wiring import tag_file_with_coherence
            with tag_file_with_coherence(path):
                _idh.save_easy_mp3(audio)
        except ImportError:
            _idh.save_easy_mp3(audio)

        if track.get("is_liked"):
            id3 = ID3(path)
            id3["POPM:Spotify Liked"] = POPM(
                email="Spotify Liked", rating=255, count=0
            )
            try:
                from mk37_wiring import tag_file_with_coherence
                with tag_file_with_coherence(path):
                    _idh.save_v23_id3(id3)
            except ImportError:
                _idh.save_v23_id3(id3)
        # Rev 14.9.3 F2 — embed cover art at download time
        try:
            _embed_cover_for_track(path, track)
        except Exception as _ce:
            log.debug(f"  [Cover] MP3 embed raised for {path.name!r}: {_ce}")
        return path

    if ext == ".m4a":
        try:
            audio = MP4(path)
            audio["\xa9nam"] = title_tag
            audio["\xa9ART"] = artist_tag
            audio["\xa9alb"] = album_tag
            audio["aART"]    = albumartist_tag
            audio["\xa9day"] = yr
            audio["trkn"]    = [(int(track_num), 0)]
            audio["disk"]    = [(int(disc_num),  0)]
            audio["\xa9gen"] = genre_tag
            # MK37/S4: lock-aware tag write
            try:
                from mk37_wiring import tag_file_with_coherence
                with tag_file_with_coherence(path):
                    audio.save()
            except ImportError:
                audio.save()
            # Rev 14.9.3 F2 — embed cover art at download time
            try:
                _embed_cover_for_track(path, track)
            except Exception as _ce:
                log.debug(f"  [Cover] M4A embed raised for {path.name!r}: {_ce}")
            return path
        except Exception as e:
            log.debug(f"  [Tag] Failed to tag as MP4: {e}")

        # Not a valid MP4 – try conversion to MP3
        log.info(f"  [Tag] File {path.name} not a valid MP4, attempting conversion to MP3")
        mp3_path = _convert_raw_aac_to_mp3(path, path.parent)
        if mp3_path:
            path.unlink()
            path = mp3_path
            try:
                audio = MP3(path, ID3=EasyID3)
            except ID3NoHeaderError:
                MP3(path).add_tags()
                audio = MP3(path, ID3=EasyID3)
            audio["title"]       = title_tag
            audio["artist"]      = artist_tag
            audio["album"]       = album_tag
            audio["albumartist"] = albumartist_tag
            audio["date"]        = yr
            audio["tracknumber"] = track_num
            audio["discnumber"]  = disc_num
            audio["genre"]       = genre_tag
            _idh.save_easy_mp3(audio)
            if track.get("is_liked"):
                id3 = ID3(path)
                id3["POPM:Spotify Liked"] = POPM(
                    email="Spotify Liked", rating=255, count=0
                )
                _idh.save_v23_id3(id3)
            # Rev 14.9.3 F2 — embed cover art after rescue
            try:
                _embed_cover_for_track(path, track)
            except Exception as _ce:
                log.debug(f"  [Cover] rescue-path embed raised: {_ce}")
            return path

    log.warning(
        f"  [Tag] Skipping enriched tags for {path.name} "
        f"(format {ext!r} not supported - file will play but may lack metadata)"
    )
    return path


# ---------------------------------------------------------------------------
# Shared thread-wrapper
# ---------------------------------------------------------------------------

def _run_in_thread(fn, timeout_s: int) -> tuple:
    result = {"done": False, "error": None}

    def _target():
        try:
            fn()
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            result["done"] = True

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    return result["done"], result["error"]


# ---------------------------------------------------------------------------
# Provider 1 — TIDAL (tidalapi)
# ---------------------------------------------------------------------------

_tidal_session_cache: object = None
_tidal_session_lock  = threading.Lock()


def _get_tidal_session():
    global _tidal_session_cache

    with _tidal_session_lock:
        if _tidal_session_cache == "failed":
            raise RuntimeError("TIDAL pre-auth already failed this run — skipping.")
        if _tidal_session_cache is not None:
            try:
                if _tidal_session_cache.check_login():
                    return _tidal_session_cache
                else:
                    log.warning("  [TIDAL] Session expired mid-run — marking failed")
                    _tidal_session_cache = "failed"
                    raise RuntimeError("TIDAL session expired mid-run.")
            except RuntimeError:
                raise
            except Exception:
                _tidal_session_cache = "failed"
                raise RuntimeError("TIDAL session check raised mid-run.")

    try:
        import tidalapi
    except ImportError:
        raise RuntimeError("tidalapi not installed — run: pip install tidalapi")

    import json as _json

    client_id     = getattr(config, "TIDAL_CLIENT_ID",     os.environ.get("TIDAL_CLIENT_ID",     ""))
    client_secret = getattr(config, "TIDAL_CLIENT_SECRET", os.environ.get("TIDAL_CLIENT_SECRET", ""))
    session_file  = pathlib.Path(getattr(config, "BASE_DIR", pathlib.Path("."))) / ".tidal_session.json"

    def get_quality(name):
        return getattr(tidalapi.Quality, name, None)

    quality_priority = [
        get_quality('hi_res_lossless'),
        get_quality('lossless'),
        get_quality('high_res'),
        get_quality('high'),
    ]
    target_qualities = [q for q in quality_priority if q is not None]

    session = None
    for q in target_qualities:
        try:
            current_cfg = None
            for use_creds in (True, False):
                try:
                    if use_creds and client_id and client_secret:
                        current_cfg = tidalapi.Config(
                            quality=q,
                            client_id=client_id,
                            client_secret=client_secret,
                        )
                    else:
                        current_cfg = tidalapi.Config(quality=q)
                    break
                except TypeError:
                    continue

            if not current_cfg:
                current_cfg = tidalapi.Config(quality=q)

            test_session = tidalapi.Session(current_cfg)

            if session_file.exists():
                saved = _json.loads(session_file.read_text(encoding="utf-8"))
                test_session.load_oauth_session(
                    saved["token_type"],
                    saved["access_token"],
                    saved["refresh_token"],
                    saved.get("expiry_time"),
                )
                if test_session.check_login():
                    log.info(f"  [TIDAL] Authenticated at {q} quality.")
                    session = test_session
                    break
            else:
                session = test_session
                break
        except Exception:
            continue

    if session is None:
        session = tidalapi.Session(tidalapi.Config())

    if session.check_login():
        # 2026-08-22: a successful check_login() here can mean tidalapi
        # silently refreshed an expired access_token using the still-valid
        # refresh_token — but the refreshed token was never written back to
        # session_file, only after a fresh interactive OAuth login (below).
        # So the on-disk file always looked stale/expired even when the
        # live session was working fine on every run. Persist here too.
        try:
            session_file.write_text(_json.dumps({
                "token_type":    session.token_type,
                "access_token":  session.access_token,
                "refresh_token": session.refresh_token,
                "expiry_time":   str(session.expiry_time) if session.expiry_time else None,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug(f"Could not persist refreshed TIDAL session: {e}")
        _tidal_session_cache = session
        return session

    if not client_id or not client_secret:
        raise RuntimeError("TIDAL credentials missing from .env")

    login_url = future = None
    try:
        link_login, future = session.login_oauth()
        login_url = link_login.verification_uri_complete
    except (TypeError, AttributeError, Exception):
        pass

    if login_url is None:
        for kwarg in ({}, {"client_id": client_id}):
            try:
                result = session.login_oauth_simple(**kwarg)
                if isinstance(result, tuple):
                    link_login, future = result
                    login_url = getattr(link_login, "verification_uri_complete",
                                        getattr(link_login, "href", str(link_login)))
                    break
            except TypeError:
                continue

    if login_url is None:
        raise RuntimeError("Could not initiate TIDAL OAuth.")

    device_code = login_url.rstrip("/").split("/")[-1].upper()
    print(f"\nLOGIN REQUIRED\nURL: {login_url}\nCODE: {device_code}\n", flush=True)

    confirmed = threading.Event()

    def _show_code_dialog():
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            root = _tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            _mb.showinfo("TIDAL Login", f"URL: {login_url}\nCode: {device_code}")
            root.destroy()
        except Exception:
            log.info(f"  [TIDAL] Login URL: {login_url}  Code: {device_code}")
            log.info("  [TIDAL] Waiting 240 s for device auth (unattended mode)...")
        confirmed.set()

    threading.Thread(target=_show_code_dialog, daemon=True).start()
    confirmed.wait(timeout=240)

    try:
        future.result(timeout=300)
    except Exception as e:
        raise RuntimeError(f"TIDAL OAuth failed: {e}")

    if not session.check_login():
        raise RuntimeError("TIDAL login check failed.")

    try:
        session_file.write_text(_json.dumps({
            "token_type":    session.token_type,
            "access_token":  session.access_token,
            "refresh_token": session.refresh_token,
            "expiry_time":   str(session.expiry_time) if session.expiry_time else None,
        }, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Could not save session: {e}")

    _tidal_session_cache = session
    return session


def _try_tidaldl(artist: str, title: str, output_dir: str, timeout_s: int, *, isrc: str = "") -> tuple[bool, str | None]:
    try:
        import tidalapi
        import requests as _requests
    except ImportError as e:
        log.warning(f"  [P1/TIDAL] missing dependency: {e}")
        return False, None

    query = f"{artist} {_strip_feat(title)}"
    tidal_id = None

    def _run():
        nonlocal tidal_id
        session = _get_tidal_session()

        best = None
        if isrc:
            try:
                isrc_results = session.search(isrc, models=[tidalapi.Track], limit=3)
                isrc_tracks = isrc_results.get("tracks", [])
                for t in isrc_tracks:
                    if (getattr(t, "isrc", "") or "").upper() == isrc.upper():
                        best = t
                        log.info(f"  [P1/TIDAL] ISRC match: {t.artist.name} - {t.name} (isrc={isrc})")
                        break
            except Exception as _isrc_e:
                log.debug(f"  [P1/TIDAL] ISRC lookup failed ({_isrc_e})")

        if best is None:
            results = session.search(query, models=[tidalapi.Track], limit=5)
            tracks = results.get("tracks", [])
            if not tracks:
                raise RuntimeError(f"No TIDAL results for: {query!r}")

            title_lc = _strip_feat(title).lower()
            artist_lc = artist.lower()
            best = tracks[0]
            for t in tracks:
                if t.name.lower() == title_lc and t.artist.name.lower() == artist_lc:
                    best = t
                    break
                if t.name.lower() == title_lc:
                    best = t

            log.info(f"  [P1/TIDAL] fuzzy match: {best.artist.name} - {best.name} (id={best.id})")

        tidal_id = str(best.id)

        import tidalapi as _tidalapi_dl
        _qualities = [
            getattr(_tidalapi_dl.Quality, 'hi_res_lossless', None),
            getattr(_tidalapi_dl.Quality, 'lossless', None),
            getattr(_tidalapi_dl.Quality, 'high', None),
        ]
        _qualities = [q for q in _qualities if q is not None]

        out_stem = _ascii_safe(_strip_feat(title))
        out_dir_p = pathlib.Path(output_dir)
        downloaded = False

        for _q in (_qualities or [None]):
            try:
                _stream = best.get_stream_info(quality=_q) if _q else best.get_stream()
                _stream_url = getattr(_stream, 'url', None)
                if not _stream_url and hasattr(_stream, 'get_stream_url'):
                    _stream_url = _stream.get_stream_url()
                if not _stream_url:
                    _seg_urls = getattr(_stream, 'urls', None) or getattr(_stream, 'segment_urls', None)
                    if _seg_urls:
                        out_path = out_dir_p / f"{out_stem}.flac"
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
                    ext = ".flac" if "flac" in _stream_url.lower() else ".m4a"
                    out_path = out_dir_p / f"{out_stem}{ext}"
                    resp = _requests.get(_stream_url, stream=True, timeout=60)
                    resp.raise_for_status()
                    with open(out_path, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            fh.write(chunk)
                    downloaded = True
                    break
            except Exception as _qe:
                log.debug(f"  [TIDAL] quality {_q} failed: {_qe}")
                continue

        if not downloaded:
            try:
                _stream_url = best.get_url()
                if _stream_url:
                    ext = ".m4a"
                    out_path = out_dir_p / f"{out_stem}{ext}"
                    resp = _requests.get(_stream_url, stream=True, timeout=60)
                    resp.raise_for_status()
                    with open(out_path, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            fh.write(chunk)
                    downloaded = True
            except Exception:
                pass

        if not downloaded:
            raise RuntimeError("All TIDAL stream URL methods exhausted")

        if out_path.suffix.lower() == ".flac" and shutil.which("ffmpeg"):
            remuxed = out_path.with_name(out_path.stem + "_remux.flac")
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(out_path), "-c:a", "copy", str(remuxed)],
                capture_output=True, check=False,
            )
            if r.returncode == 0 and remuxed.exists():
                out_path.unlink()  # mk37: staging
                remuxed.rename(out_path)  # mk37: staging

    completed, error = _run_in_thread(_run, timeout_s)
    if not completed:
        log.warning(f"  [P1/TIDAL] timed out after {timeout_s}s  '{artist} - {title}'")
        return False, None
    elif error:
        log.warning(f"  [P1/TIDAL] error: {error}  '{artist} - {title}'")
        return False, None
    return True, tidal_id


# ---------------------------------------------------------------------------
# Provider 1b — TIDAL URL (using yt-dlp with direct TIDAL track URL)
# ---------------------------------------------------------------------------

def _try_tidaldl_url(tidal_url: str, output_dir: str, timeout_s: int) -> bool:
    try:
        import yt_dlp
    except ImportError:
        return False

    ffmpeg_dir = _find_ffmpeg()
    out_dir = pathlib.Path(output_dir)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "flac",
            "preferredquality": "0",
        }]

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([tidal_url])

    completed, error = _run_in_thread(_run, timeout_s)
    if not completed:
        log.warning(f"  [P1b/TIDAL_URL] timed out after {timeout_s}s")
        return False
    elif error:
        log.warning(f"  [P1b/TIDAL_URL] error: {error}")
        return False

    files = _collect_downloaded_files(out_dir)
    if files:
        log.info(f"  [P1b/TIDAL_URL] downloaded: {files[0].name}")
        return True
    return False


# ---------------------------------------------------------------------------
# Provider 1c — QQDL (tidal.qqdl.site)
# ---------------------------------------------------------------------------

def _try_qqdl(spotify_url: str, tidal_id: str | None, output_dir: str, timeout_s: int) -> bool:
    import requests as _req
    from urllib.parse import urljoin

    base_url = "https://tidal.qqdl.site/"
    if tidal_id:
        track_url = f"https://tidal.com/browse/track/{tidal_id}"
    elif spotify_url:
        track_url = spotify_url
    else:
        return False

    try:
        resp = _req.get(base_url, params={"url": track_url}, timeout=timeout_s,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        if resp.status_code != 200:
            return False

        import re
        download_url = None

        # Look for direct .flac/.m4a/.mp3 link
        match = re.search(r'href="([^"]+\.(flac|m4a|mp3))"', resp.text)
        if match:
            download_url = urljoin(base_url, match.group(1))
        else:
            # Look for a button with data-url attribute
            match = re.search(r'data-url="([^"]+)"', resp.text)
            if match:
                download_url = urljoin(base_url, match.group(1))

        if not download_url:
            log.debug("  [P5/qqdl] No download URL found")
            return False

        out_dir = pathlib.Path(output_dir)
        filename = download_url.split("/")[-1].split("?")[0]
        if not filename:
            filename = "track" + (".flac" if ".flac" in download_url else ".m4a")
        out_path = out_dir / filename

        with _req.get(download_url, stream=True, timeout=timeout_s) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*256):
                    f.write(chunk)

        return out_path.exists() and out_path.stat().st_size > 0

    except Exception as e:
        log.debug(f"  [P5/qqdl] Error: {e}")
        return False


# ---------------------------------------------------------------------------
# Provider 1d — Monochrome (monochrome.samidy.com)
# ---------------------------------------------------------------------------
# Real flow (reverse-engineered from monochrome.samidy.com JS bundle):
#   1. GET {api_backend}/info?id={tidal_id}  → data.isrc
#   2. GET {qobuz_proxy}/api/get-music?q={isrc}&offset=0  → Qobuz track id
#   3. GET {qobuz_proxy}/api/download-music?track_id={id}&quality=27  → CDN URL
#   4. Stream-download the FLAC from the CDN URL.
#
# Backends tried in order; Qobuz proxy is qobuz.kennyy.com.br (Monochrome's
# own Qobuz relay — no credentials required, works in any region).

_MONOCHROME_API_BACKENDS = [
    "https://us-west.monochrome.tf",
    "https://api.monochrome.tf",
    "https://monochrome-api.samidy.com",
]
_QOBUZ_PROXY    = "https://qobuz.kennyy.com.br"
_MONO_HEADERS   = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://monochrome.samidy.com/",
    "Origin":  "https://monochrome.samidy.com",
}
# Qobuz quality IDs used by the Monochrome JS (extractStreamUrlFromManifest)
_QOBUZ_QUALITY  = {"HI_RES_LOSSLESS": "27", "LOSSLESS": "6", "HIGH": "5"}


def _monochrome_get(path: str, params: dict, timeout: int) -> dict | None:
    """Try each backend in priority order; return parsed JSON on first 200."""
    import requests as _req
    for base in _MONOCHROME_API_BACKENDS:
        try:
            r = _req.get(f"{base}{path}", params=params,
                         headers=_MONO_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


def _monochrome_isrc(tidal_id: str, timeout: int) -> str:
    """Return ISRC for a TIDAL track ID, or '' on failure."""
    j = _monochrome_get("/info", {"id": tidal_id}, timeout)
    return ((j or {}).get("data") or {}).get("isrc") or ""


def _qobuz_url_for_isrc(isrc: str, quality: str, timeout: int) -> str:
    """
    Search qobuz.kennyy.com.br by ISRC and return the direct CDN download URL.
    Returns '' on any failure.
    """
    import requests as _req
    q_num = _QOBUZ_QUALITY.get(quality, "6")
    try:
        r1 = _req.get(f"{_QOBUZ_PROXY}/api/get-music",
                      params={"q": isrc, "offset": 0},
                      headers=_MONO_HEADERS, timeout=timeout)
        if not r1.ok:
            return ""
        items = ((r1.json().get("data") or {}).get("tracks") or {}).get("items") or []
        match = (
            next((t for t in items if (t.get("isrc") or "").upper() == isrc.upper()), None)
            or (items[0] if items else None)
        )
        if not match:
            return ""
        r2 = _req.get(f"{_QOBUZ_PROXY}/api/download-music",
                      params={"track_id": match["id"], "quality": q_num},
                      headers=_MONO_HEADERS, timeout=timeout)
        if not r2.ok:
            return ""
        j2 = r2.json()
        return (j2.get("data") or {}).get("url") or "" if j2.get("success") else ""
    except Exception:
        return ""


def _qobuz_url_for_query(query: str, title: str, timeout: int) -> str:
    """Search Qobuz proxy by free-text query, fuzzy-match by title, return URL."""
    import requests as _req
    import unicodedata as _ud
    def _n(s): return _ud.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode()
    try:
        r1 = _req.get(f"{_QOBUZ_PROXY}/api/get-music",
                      params={"q": query, "offset": 0},
                      headers=_MONO_HEADERS, timeout=timeout)
        if not r1.ok:
            return ""
        items = ((r1.json().get("data") or {}).get("tracks") or {}).get("items") or []
        match = next((t for t in items if _n(t.get("title", "")) == _n(title)), None) or (items[0] if items else None)
        if not match:
            return ""
        for q_num in ("27", "6"):
            r2 = _req.get(f"{_QOBUZ_PROXY}/api/download-music",
                          params={"track_id": match["id"], "quality": q_num},
                          headers=_MONO_HEADERS, timeout=timeout)
            if r2.ok and r2.json().get("success"):
                return (r2.json().get("data") or {}).get("url") or ""
    except Exception:
        pass
    return ""


def _try_monochrome(
    spotify_url: str,
    tidal_id: str | None,
    output_dir: str,
    timeout_s: int,
    isrc: str = "",
    artist: str = "",
    title: str = "",
) -> bool:
    import requests as _req

    probe_t = min(timeout_s, 12)

    # ── Step 1: resolve ISRC ─────────────────────────────────────────────
    if not isrc and tidal_id:
        isrc = _monochrome_isrc(tidal_id, probe_t)
        if isrc:
            log.debug(f"  [P6/monochrome] resolved ISRC={isrc} for TIDAL id={tidal_id}")

    # ── Step 2: get CDN URL via Qobuz proxy ──────────────────────────────
    dl_url = ""
    if isrc:
        for quality in ("HI_RES_LOSSLESS", "LOSSLESS"):
            dl_url = _qobuz_url_for_isrc(isrc, quality, probe_t)
            if dl_url:
                log.debug(f"  [P6/monochrome] Qobuz URL ({quality}): {dl_url[:80]}…")
                break

    # ── Step 3: artist+title search fallback (no ISRC available) ────────
    if not dl_url and artist and title:
        dl_url = _qobuz_url_for_query(f"{artist} {title}", title, probe_t)
        if dl_url:
            log.debug(f"  [P6/monochrome] Qobuz URL via artist+title search")

    if not dl_url:
        log.debug("  [P6/monochrome] no download URL resolved")
        return False

    # ── Step 4: stream-download ──────────────────────────────────────────
    out_dir = pathlib.Path(output_dir)
    ext = ".flac"
    try:
        with _req.get(dl_url, stream=True, timeout=timeout_s,
                      headers=_MONO_HEADERS) as r:
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "mp4" in ct or "m4a" in ct:
                ext = ".m4a"
            elif "mpeg" in ct or "mp3" in ct:
                ext = ".mp3"
            out_path = out_dir / f"track_{tidal_id or 'mono'}{ext}"
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
        ok = out_path.exists() and out_path.stat().st_size > 10_000
        if not ok:
            log.debug(f"  [P6/monochrome] file too small: {out_path.stat().st_size if out_path.exists() else 0}B")
        return ok
    except Exception as e:
        log.debug(f"  [P6/monochrome] download failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Monochrome ALBUM resolver (2026-09-02) — get a full album's tracklist from
# the monochrome/TIDAL-proxy `/album/` endpoint, WITHOUT needing tidalapi or
# the user's TIDAL session. Audio for each track still comes via the per-track
# waterfall (monochrome -> Qobuz proxy -> ...). Used by _download_full_album so
# every completed album comes down whole and hi-res.
# ---------------------------------------------------------------------------
class _MonochromeAlbumOK(Exception):
    """Sentinel: monochrome /album/ already resolved the tracklist — skip tidalapi/streamrip."""


def _tidal_cover_url(cover_uuid: str, size: str = "1280x1280") -> str:
    if not cover_uuid:
        return ""
    return f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/{size}.jpg"


def _monochrome_search_album_id(artist: str, album: str, timeout: int) -> str:
    """Resolve a TIDAL album id for artist+album via monochrome /search/."""
    import unicodedata as _ud
    def _n(s): return _ud.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode().strip()
    j = _monochrome_get("/search/", {"s": f"{artist} {album}", "limit": 25}, timeout)
    if not j:
        return ""
    items = j.get("items") or j.get("data", {}).get("items") \
        or (j.get("data", {}).get("tracks", {}) or {}).get("items") or []
    want_al, want_ar = _n(album), _n(artist)
    best = ""
    for it in items:
        t = it.get("item", it)
        alb = t.get("album") or {}
        if not alb.get("id"):
            continue
        if _n(alb.get("title", "")) == want_al and want_ar in _n(t.get("artist", {}).get("name", "")):
            return str(alb["id"])
        if _n(alb.get("title", "")) == want_al and not best:
            best = str(alb["id"])
    return best


def _monochrome_album_tracklist(artist: str, album: str, timeout: int,
                                *, is_liked: bool = True) -> tuple[dict, list]:
    """
    Returns (album_meta, [track_dict, ...]) or ({}, []) on failure.
    track_dict shape matches what _download_full_album feeds to the waterfall.
    """
    aid = _monochrome_search_album_id(artist, album, timeout)
    if not aid:
        return {}, []
    j = _monochrome_get("/album/", {"id": aid, "limit": 100}, timeout)
    if not j:
        return {}, []
    items = j.get("items") or j.get("data", {}).get("items") or []
    if not items:
        return {}, []
    # album-level cover: from any track's album.cover, or the album payload
    cover_uuid = ""
    album_artist_name = artist
    release_year = ""
    tracks = []
    for i, it in enumerate(items, 1):
        t = it.get("item", it)
        alb = t.get("album") or {}
        cover_uuid = cover_uuid or alb.get("cover") or ""
        album_artist_name = (alb.get("artist") or {}).get("name") or album_artist_name
        tnum = t.get("trackNumber") or i
        disc = t.get("volumeNumber") or 1
        rd = t.get("streamStartDate") or t.get("dateAdded") or ""
        release_year = release_year or (rd[:4] if rd else "")
        tracks.append({
            "id":           f"mono_{t.get('id', '')}",
            "title":        (t.get("title") or "").strip(),
            "artists":      [(t.get("artist") or {}).get("name") or artist],
            "artist":       (t.get("artist") or {}).get("name") or artist,
            "album":        album,
            "album_artist": album_artist_name,
            "track_number": tnum,
            "disc_number":  disc,
            "isrc":         t.get("isrc") or "",
            "genre":        "",
            "release_year": release_year,
            "album_image":  _tidal_cover_url(cover_uuid),
            "is_liked":     is_liked,
            "_tidal_track_id": str(t.get("id", "")),
        })
    meta = {
        "album_id":     aid,
        "album_artist": album_artist_name,
        "album_name":   album,
        "cover_url":    _tidal_cover_url(cover_uuid),
        "release_year": release_year,
        "track_count":  len(tracks),
    }
    log.info(f"  [Monochrome/Album] resolved '{artist} - {album}' -> "
             f"TIDAL album {aid}, {len(tracks)} track(s)")
    return meta, tracks


# ---------------------------------------------------------------------------
# Provider 2 — tiddl
# ---------------------------------------------------------------------------

def _try_tiddl(artist: str, title: str, output_dir: str, timeout_s: int) -> bool:
    import shutil as _shutil, os as _os
    if not _shutil.which("tiddl"):
        return False

    query = f"{artist} {_strip_feat(title)}"
    tiddl_env = {**_os.environ, "TIDDL_PATH": output_dir}

    for quality in ("hi_res_lossless", "lossless"):
        cmd = ["tiddl", "search", query, "download", "--quality", quality]

        def _run(cmd=cmd, quality=quality, env=tiddl_env):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace",  # Rev 14.9.6 U1
                                        timeout=timeout_s, check=False, env=env, cwd=output_dir)
                out = (result.stdout or "") + (result.stderr or "")
                if result.returncode != 0:
                    log.debug(f"  [P1b/tiddl/{quality}] exit {result.returncode}: {out[:400]}")
            except subprocess.TimeoutExpired:
                pass

        completed, error = _run_in_thread(_run, timeout_s)
        if not completed or error:
            continue
        files = _collect_downloaded_files(pathlib.Path(output_dir))
        if files:
            log.info(f"  [P1b/tiddl/{quality}] downloaded: {files[0].name}")
            return True
    return False


# ---------------------------------------------------------------------------
# Provider 3 — SpotiFLAC
# ---------------------------------------------------------------------------

def _try_spotiflac(spotify_url: str, output_dir: str, timeout_s: int) -> bool:
    try:
        from SpotiFLAC import SpotiFLAC
    except ImportError:
        return False

    def _run():
        SpotiFLAC(url=spotify_url, output_dir=output_dir)

    completed, error = _run_in_thread(_run, timeout_s)
    if not completed:
        log.warning(f"  [P2/SpotiFLAC] timed out after {timeout_s}s")
    elif error:
        log.warning(f"  [P2/SpotiFLAC] error: {error}")
    return completed


# ---------------------------------------------------------------------------
# Provider 4 — streamrip
# ---------------------------------------------------------------------------

def _deezer_search_track_url(artist: str, title: str) -> str | None:
    try:
        import urllib.request, urllib.parse, json as _j
        q = urllib.parse.quote(f"{artist} {title}")
        req = urllib.request.Request(
            f"https://api.deezer.com/search/track?q={q}&limit=1",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _j.loads(r.read().decode())
        items = data.get("data", [])
        if items:
            return f"https://www.deezer.com/track/{items[0]['id']}"
    except Exception as e:
        log.debug(f"  [P2b/streamrip] Deezer API: {e}")
    return None


def _deezer_search_album_url(artist: str, album: str) -> str | None:
    try:
        import urllib.request, urllib.parse, json as _j
        q = urllib.parse.quote(f"{artist} {album}")
        req = urllib.request.Request(
            f"https://api.deezer.com/search/album?q={q}&limit=5",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _j.loads(r.read().decode())
        items = data.get("data", [])
        if not items:
            return None
        alb_low = album.lower().strip()
        art_low = artist.lower().strip()
        for item in items:
            if (item.get("artist", {}).get("name", "").lower() == art_low
                    and item.get("title", "").lower() == alb_low):
                return f"https://www.deezer.com/album/{item['id']}"
        return f"https://www.deezer.com/album/{items[0]['id']}"
    except Exception as e:
        log.debug(f"  [streamrip/album] Deezer API: {e}")
    return None


def _try_streamrip(artist: str, title: str, output_dir: str, timeout_s: int) -> bool:
    import shutil as _sh
    if not _sh.which("rip"):
        return False

    deezer_url = _deezer_search_track_url(artist, _strip_feat(title))
    if not deezer_url:
        return False

    # REV 14.6 fix: was a 3-command retry loop, each burning `timeout_s`
    # independently (so 30s → 90s, 180s → 540s in the old flat-timeout world).
    # Streamrip v2.x accepts `-f <path>` for the output folder; the other
    # two commands were speculative fallbacks that never worked in practice
    # (verified against the last 4 LOG files — zero successes from cmd 2 or 3).
    cmd = ["rip", "-f", output_dir, "--no-db", "url", deezer_url]

    def _run(cmd=cmd):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",  # Rev 14.9.6 U1
                                    timeout=timeout_s, cwd=output_dir, check=False)
            out = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                log.debug(f"  [P2b/streamrip] exit {result.returncode}: {out[:300]}")
        except subprocess.TimeoutExpired:
            pass

    completed, error = _run_in_thread(_run, timeout_s)
    if completed and not error:
        files = _collect_downloaded_files(pathlib.Path(output_dir))
        if files:
            log.info(f"  [P2b/streamrip] downloaded: {files[0].name}")
            return True
    return False


def _try_streamrip_album(artist: str, album: str, output_dir: str, timeout_s: int) -> list:
    """Download a whole album via streamrip/Deezer. Returns list of audio file Paths."""
    import shutil as _sh
    if not _sh.which("rip"):
        return []

    deezer_url = _deezer_search_album_url(artist, album)
    if not deezer_url:
        log.debug(f"  [streamrip/album] No Deezer album found for '{artist} - {album}'")
        return []

    log.info(f"  [streamrip/album] Downloading '{artist} - {album}' via {deezer_url}")
    cmd = ["rip", "-f", output_dir, "--no-db", "url", deezer_url]

    def _run(cmd=cmd):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=timeout_s, cwd=output_dir, check=False)
            out = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                log.debug(f"  [streamrip/album] exit {result.returncode}: {out[:300]}")
        except subprocess.TimeoutExpired:
            pass

    completed, error = _run_in_thread(_run, timeout_s)
    if completed and not error:
        files = _collect_downloaded_files(pathlib.Path(output_dir))
        if files:
            log.info(f"  [streamrip/album] Got {len(files)} tracks for '{artist} - {album}'")
            return files
    return []


def _read_streamrip_track_tags(path: pathlib.Path) -> dict:
    """Read per-track fields from a streamrip-downloaded file's existing tags."""
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return {}
        def _first(key: str, default: str = "") -> str:
            val = audio.get(key) or []
            return str(val[0]).split("/")[0].strip() if val else default
        return {
            "title":        _first("title") or path.stem,
            "artist":       _first("artist"),
            "track_number": _first("tracknumber", "1"),
            "disc_number":  _first("discnumber", "1"),
        }
    except Exception:
        return {"title": path.stem, "artist": "", "track_number": "1", "disc_number": "1"}


def _tag_streamrip_album_files(files: list, album_info: dict) -> list:
    """
    Apply album-coherent tags to all tracks downloaded by streamrip.
    Reads existing Deezer tags for per-track fields (title, artist,
    track_number), then overlays album-level fields so every track
    shares the same ALBUM_ARTIST, ALBUM, GENRE, YEAR, and cover art.
    Returns the list of paths after tagging (may differ for .m4a rescue).
    """
    album_artist = album_info.get("album_artist") or ""
    album_name   = album_info.get("album_name") or ""
    is_liked     = album_info.get("is_liked", False)

    ref_tracks   = album_info.get("missing_tracks") or []
    ref0         = ref_tracks[0] if ref_tracks else {}
    genre        = ref0.get("genre") or album_info.get("genre") or ""
    release_year = ref0.get("release_year") or album_info.get("release_year") or ""
    album_image  = ref0.get("album_image") or album_info.get("album_image") or ""

    out: list = []
    for f in files:
        per = _read_streamrip_track_tags(f)
        track = {
            "title":        per.get("title") or f.stem,
            "artist":       per.get("artist") or album_artist,
            "album":        album_name,
            "album_artist": album_artist,
            "release_year": release_year,
            "track_number": per.get("track_number") or "1",
            "disc_number":  per.get("disc_number") or "1",
            "genre":        genre,
            "album_image":  album_image,
            "is_liked":     is_liked,
        }
        try:
            _idh.ensure_writable(f)
            result = _tag_file(f, track)
            out.append(result if result is not None else f)
        except Exception as e:
            log.debug(f"  [streamrip/album] Tag failed for {f.name!r}: {e}")
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Provider 5 — yt-dlp
# ---------------------------------------------------------------------------

# _find_ffmpeg is defined at module level (above) and cached as _FFMPEG_DIR.
# The local alias below keeps all call-sites inside provider functions working
# without modification.
def _find_ffmpeg() -> str | None:  # noqa: F811  (intentional shadow of module-level def)
    return _FFMPEG_DIR


def _try_ytdlp(artist: str, title: str, output_dir: str, timeout_s: int,
               album: str = "", duration_ms: int = 0) -> bool:
    try:
        import yt_dlp
    except ImportError:
        return False

    ffmpeg_dir = _find_ffmpeg()
    clean_title = _strip_feat(title)
    if album:
        query = f"ytsearch1:{artist} {clean_title} {album}"
    else:
        query = f"ytsearch1:{artist} {clean_title}"
    out_dir = pathlib.Path(output_dir)

    # 2026-09: YouTube now 403s anonymous bestaudio requests. Pass cookies so
    # yt-dlp authenticates as a real signed-in session. Prefer an exported
    # cookie file; fall back to reading them straight from an installed browser.
    _cookie_opts: dict = {}
    _cf = getattr(config, "YTDLP_COOKIE_FILE", "") or ""
    _cfb = getattr(config, "YTDLP_COOKIES_FROM_BROWSER", "") or ""
    if _cf and os.path.exists(_cf):
        _cookie_opts["cookiefile"] = _cf
    elif _cfb:
        _cookie_opts["cookiesfrombrowser"] = (_cfb,)

    if ffmpeg_dir:
        bitrate = str(getattr(config, "YTDLP_MP3_BITRATE", "320"))
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
            "ffmpeg_location": ffmpeg_dir,
            # 2026-09-03: also pull + jpg-convert the video thumbnail. Tracks
            # from the offline Spotify export carry no album_image URL, so the
            # yt-dlp thumbnail is _embed_cover_for_track()'s only cover source
            # (Source 2). Without this the ffmpeg branch left ~all offline
            # per-track downloads coverless.
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                },
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            ],
            "keepvideo": False, "quiet": True, "no_warnings": True, "noplaylist": True,
            **_cookie_opts,
        }
    else:
        ydl_opts = {
            "format": (
                "bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio[ext=mp3]/"
                "bestaudio[ext=aac]/bestaudio[ext=ogg]/bestaudio[!ext=webm][!ext=opus]/bestaudio"
            ),
            "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noplaylist": True,
            # MK8 Fix (PR-CoverArt): enable thumbnail download as cover-art fallback.
            # _embed_cover_for_track() already reads Source 2 (yt-dlp thumbnail file),
            # but writethumbnail was False so the file never existed.  Now enabled so
            # tracks without a Spotify album_image URL (local-data tracks, tracks from
            # the account data export) still get a cover embedded.  The thumbnail is
            # a temporary artefact — _embed_cover_for_track() reads it then it stays
            # in the temp dir which is cleaned up by the caller.
            "postprocessors": [], "writethumbnail": True, "concurrent_fragment_downloads": 1,
            **_cookie_opts,
        }

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([query])

    completed, error = _run_in_thread(_run, timeout_s)
    if not completed:
        log.warning(f"  [P3/yt-dlp] timed out after {timeout_s}s")
        return False
    elif error:
        if "ffmpeg not found" in error or "ffprobe" in error:
            found_files = list(out_dir.rglob("*"))
            good_exts = {".m4a", ".mp4", ".ogg", ".aac", ".mp3", ".flac"}
            bad_exts = {".webm", ".opus"}
            good_files = [f for f in found_files if f.is_file() and f.suffix.lower() in good_exts]
            if good_files:
                log.info(f"  [P3/yt-dlp] ffmpeg missing but compatible download succeeded: {good_files[0].name}")
                return True
            bad_files = [f for f in found_files if f.is_file() and f.suffix.lower() in bad_exts]
            if bad_files:
                log.warning(f"  [P3/yt-dlp] Downloaded {bad_files[0].name} but ZX500 cannot play {bad_files[0].suffix} - install ffmpeg")
                for f in bad_files:
                    try: f.unlink()  # mk37: staging
                    except: pass
                return False
        log.warning(f"  [P3/yt-dlp] error: {error}")
        found = [f.name for f in out_dir.rglob("*") if f.is_file()]
        if found:
            log.warning(f"  [P3/yt-dlp] files left in tmp: {found}")

    # Duration validation
    if completed and duration_ms > 0:
        for _df in out_dir.rglob("*"):
            if not _df.is_file() or _df.suffix.lower() not in {".mp3", ".flac", ".m4a", ".ogg", ".aac"}:
                continue
            try:
                if _df.suffix.lower() == ".mp3":
                    from mutagen.mp3 import MP3 as _MP3_dur
                    dl_dur = _MP3_dur(_df).info.length
                elif _df.suffix.lower() == ".flac":
                    from mutagen.flac import FLAC as _FLAC_dur
                    dl_dur = _FLAC_dur(_df).info.length
                elif _df.suffix.lower() in (".m4a", ".aac"):
                    from mutagen.mp4 import MP4 as _MP4_dur
                    dl_dur = _MP4_dur(_df).info.length
                else:
                    continue
                sp_dur = duration_ms / 1000.0
                if abs(dl_dur - sp_dur) > 15:
                    log.warning(f"  [P3/yt-dlp] Duration mismatch for '{title}': {dl_dur:.0f}s vs Spotify {sp_dur:.0f}s - rejecting")
                    _df.unlink()  # mk37: staging
                    return False
            except Exception:
                pass
    return completed


# ---------------------------------------------------------------------------
# Provider 6 — spotdl
# ---------------------------------------------------------------------------

def _try_spotdl(spotify_url: str, output_dir: str, timeout_s: int) -> bool:
    if not shutil.which("spotdl"):
        return False

    cmd = [
        "spotdl", "download", spotify_url,
        "--output", output_dir, "--format", "mp3",
        "--bitrate", "320k", "--log-level", "CRITICAL",
    ]

    def _run():
        subprocess.run(cmd, capture_output=True, check=False)

    completed, error = _run_in_thread(_run, timeout_s)
    if not completed:
        log.warning(f"  [P4/spotdl] timed out after {timeout_s}s")
    elif error:
        log.warning(f"  [P4/spotdl] error: {error}")
    return completed


# ---------------------------------------------------------------------------
# Storage-aware FLAC/MP3 fallback (Task 9)
# ---------------------------------------------------------------------------
# Additive to the existing genre-tier FLAC-first waterfall below — that
# system still decides whether FLAC was attempted; this decision layer runs
# AFTER a download completes and only decides whether to KEEP it as FLAC
# once the device is running low on space. Liked Songs / saved-playlist
# tracks bypass the downgrade (config.FLAC_FORCE_FOR_LIKED_PLAYLISTS).

_STORAGE_FALLBACK_STATE = {"free_gb": None, "checked_at": 0.0}
_STORAGE_FALLBACK_RECHECK_S = 60.0  # don't hammer ADB — refresh at most once/min
_PLAYLIST_PROTECTED_URI_CACHE: set | None = None


def _current_device_free_gb() -> float | None:
    """Cached device free-space reading (GB), refreshed at most once per
    _STORAGE_FALLBACK_RECHECK_S. Returns None if unknown (adb unreachable,
    cold cache) — caller should treat None as "don't guess, keep FLAC"."""
    now = time.time()
    cached = _STORAGE_FALLBACK_STATE["free_gb"]
    if cached is not None and (now - _STORAGE_FALLBACK_STATE["checked_at"]) < _STORAGE_FALLBACK_RECHECK_S:
        return cached
    try:
        import free_space_check as _fsc
        free_bytes = _fsc.get_device_free_bytes()
    except Exception:
        free_bytes = None
    free_gb = (free_bytes / (1024 ** 3)) if free_bytes is not None else None
    _STORAGE_FALLBACK_STATE["free_gb"] = free_gb
    _STORAGE_FALLBACK_STATE["checked_at"] = now
    return free_gb


def _track_is_protected_from_downgrade(track: dict) -> bool:
    """True if this track should bypass the storage-aware FLAC downgrade:
    Liked Songs (track['is_liked'], already set by the existing pipeline —
    see the POPM-rating call sites above), or a member of any saved
    playlist (spotify_local_data.py's existing offline export)."""
    if track.get("is_liked"):
        return True
    spotify_id = track.get("id", "")
    if not spotify_id:
        return False
    global _PLAYLIST_PROTECTED_URI_CACHE
    if _PLAYLIST_PROTECTED_URI_CACHE is None:
        uris: set = set()
        try:
            import spotify_local_data as _sld
            uris |= set(_sld.get_liked_uris())
            for _track_uris in _sld.get_all_playlists().values():
                uris.update(_track_uris)
        except Exception:
            pass
        _PLAYLIST_PROTECTED_URI_CACHE = uris
    return f"spotify:track:{spotify_id}" in _PLAYLIST_PROTECTED_URI_CACHE


def apply_storage_aware_format_fallback(files: list, track: dict) -> list:
    """Downgrade a freshly-downloaded lossless file to MP3 (CBR 320kbps)
    when the device's effective free space is below
    config.FLAC_FALLBACK_MIN_FREE_GB, unless the track is protected (see
    _track_is_protected_from_downgrade). Never raises — returns `files`
    unchanged on any failure, when ffmpeg/free-space info is unavailable,
    or when the fallback simply doesn't apply."""
    if not files:
        return files
    if not getattr(config, "FLAC_FIRST_ENABLED", False):
        return files  # nothing to fall back FROM if FLAC-first isn't even on

    src = files[0]
    if src.suffix.lower() not in (".flac", ".wav", ".aiff"):
        return files  # already compressed — nothing to downgrade

    if getattr(config, "FLAC_FORCE_FOR_LIKED_PLAYLISTS", True) and _track_is_protected_from_downgrade(track):
        return files

    threshold_gb    = float(getattr(config, "FLAC_FALLBACK_MIN_FREE_GB", 5.0))
    system_floor_gb = float(getattr(config, "MIN_FREE_SPACE_GB", 0.5))
    free_gb = _current_device_free_gb()
    if free_gb is None:
        return files  # unknown — don't guess, keep FLAC

    effective_gb = free_gb - system_floor_gb
    if effective_gb >= threshold_gb:
        return files  # plenty of room

    if not _FFMPEG_DIR:
        log.debug("  [StorageFallback] ffmpeg absent — cannot downgrade, keeping FLAC")
        return files

    mp3_path = src.with_suffix(".mp3")
    ffmpeg_bin = str(pathlib.Path(_FFMPEG_DIR) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-y", "-i", str(src),
             "-codec:a", "libmp3lame", "-b:a", "320k", str(mp3_path)],
            capture_output=True, check=False, timeout=120,
        )
        if result.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 4096:
            src.unlink(missing_ok=True)
            log.info(
                f"  [StorageFallback] Low device space ({effective_gb:.1f} GB effective < "
                f"{threshold_gb:.1f} GB threshold) — downgraded to MP3 320kbps: {mp3_path.name}"
            )
            return [mp3_path] + files[1:]
        log.debug(f"  [StorageFallback] ffmpeg downgrade failed for {src.name}: {result.stderr[-200:]!r}")
    except Exception as e:
        log.debug(f"  [StorageFallback] ffmpeg exception: {e}")
    return files


# ---------------------------------------------------------------------------
# Waterfall coordinator (updated to use all providers)
# ---------------------------------------------------------------------------

_PROVIDERS = ["monochrome", "tidaldl", "tidaldl_url", "qqdl", "tiddl", "spotiflac", "streamrip", "ytdlp", "spotdl"]


def _download_with_waterfall(
    track:     dict,
    tmp_dir:   pathlib.Path,
    dl_cache:  dict,
    timeout_s: int = PROVIDER_TIMEOUT_S,
) -> tuple:
    spotify_id  = track.get("id", "")
    spotify_url = f"https://open.spotify.com/track/{spotify_id}" if spotify_id else ""
    artist      = track.get("artist", "Unknown")
    title       = track.get("title",  "Unknown")

    enabled = getattr(config, "DOWNLOAD_PROVIDERS", {})

    # MK15 FIX-C: Genre-aware provider ordering ("Tiered Download Waterfall").
    # CTO: "Tier 1 = Qobuz/streamrip (Jazz/Soul/Bossa Nova),
    #        Tier 2 = QQDL/Monochrome (Afrobeats/Amapiano/Bongo Flava),
    #        Tier 3 = Tidal/SpotiFLAC (Hip-Hop/Trap/Drill — keep as M4A)"
    genre = (track.get("genre") or "").strip()
    _flac_enabled = getattr(config, "FLAC_FIRST_ENABLED", False)
    _flac_priority = getattr(config, "FLAC_PRIORITY_GENRES", set())
    _flac_niche    = getattr(config, "FLAC_NICHE_GENRES", set())
    _m4a_pref      = getattr(config, "M4A_PREFERRED_GENRES", set())

    if _flac_enabled and genre in _m4a_pref:
        # Tier 3: M4A-preferred genre → skip FLAC sources entirely.
        # Saves ~60s per track of fruitless streamrip/Qobuz searching.
        order = [p for p in _PROVIDERS
                 if enabled.get(p, True)
                 and p not in ("streamrip",)]
        log.debug(f"  [Waterfall] M4A-preferred genre ({genre!r}) — skipping FLAC sources")

    elif _flac_enabled and genre in _flac_niche:
        # Tier 2: Niche African/global genre → Monochrome+QQDL first,
        # then Tidal fallback. These providers have deeper "World Music" licensing.
        order = []
        for p in ("monochrome", "qqdl"):
            if enabled.get(p, True):
                order.append(p)
        order += [p for p in _PROVIDERS
                  if enabled.get(p, True)
                  and p not in ("monochrome", "qqdl", "streamrip")]
        log.debug(f"  [Waterfall] Niche genre ({genre!r}) → Monochrome/QQDL first")

    elif _flac_enabled and genre in _flac_priority:
        # Tier 1: Audiophile genre → streamrip (Deezer/Qobuz) first,
        # then QQDL/Monochrome, then Tidal fallback.
        order = []
        if enabled.get("streamrip", True):
            order.append("streamrip")
        order += [p for p in _PROVIDERS
                  if enabled.get(p, True) and p != "streamrip"]
        log.debug(f"  [Waterfall] FLAC-priority genre ({genre!r}) → streamrip first")

    else:
        # Default: standard provider order (existing behaviour)
        order = [p for p in _PROVIDERS if enabled.get(p, True)]

    # 2026-09-02 (user direction) — monochrome is our PRIMARY music provider.
    # Whatever genre-tier logic above decided, force the configured primary
    # provider to the front of the waterfall (unless explicitly disabled).
    _primary = getattr(config, "PRIMARY_DOWNLOAD_PROVIDER", "monochrome")
    if _primary and _primary in _PROVIDERS and enabled.get(_primary, True):
        order = [_primary] + [p for p in order if p != _primary]

    def _dep_available(provider):
        if provider == "tidaldl":
            try: import tidalapi; return True
            except ImportError: return False
        if provider == "tidaldl_url":
            try: import yt_dlp; return True
            except ImportError: return False
        if provider == "ytdlp":
            try: import yt_dlp; return True
            except ImportError: return False
        if provider == "spotiflac":
            try: import SpotiFLAC; return True
            except ImportError: return False
        if provider == "tiddl":
            return shutil.which("tiddl") is not None
        if provider == "streamrip":
            return shutil.which("rip") is not None
        if provider in ("qqdl", "monochrome"):
            try: import requests; return True
            except ImportError: return False
        return True

    unavailable = [p for p in order if not _dep_available(p)]
    if unavailable:
        log.debug(f"  [Waterfall] skipping unavailable providers: {unavailable}")
    order = [p for p in order if _dep_available(p)]
    if not order:
        log.warning(f"  [Waterfall] no providers available for '{title}'")
        return [], None

    # REV 14.6 — filter out circuit-broken providers for this session.
    broken = [p for p in order if _provider_is_disabled(p)]
    if broken:
        order = [p for p in order if p not in broken]
        if not order:
            log.warning(
                f"  [Waterfall] all providers circuit-broken for '{title}' "
                f"(disabled: {broken})"
            )
            return [], None

    cached = dl_cache.get(spotify_id)
    if cached and cached in order:
        order = [cached] + [p for p in order if p != cached]

    tidal_id = None

    # Track duration for adaptive timeout scaling.
    _dur_ms = int(track.get("duration_ms") or track.get("spotify_duration_ms") or 0)

    for provider in order:
        if not spotify_url and provider in ("spotiflac", "spotdl"):
            continue

        # REV 14.6 — per-provider adaptive timeout (duration-scaled).
        eff_timeout = _timeout_for(provider, _dur_ms)

        provider_tmp = pathlib.Path(tempfile.mkdtemp(dir=tmp_dir))
        log.info(f"  [Waterfall] trying {provider} ({eff_timeout}s)  '{artist} - {title}'")

        if provider == "tidaldl":
            _isrc = track.get("isrc", "")
            completed, tidal_id = _try_tidaldl(artist, title, str(provider_tmp), eff_timeout, isrc=_isrc)
        elif provider == "tidaldl_url" and tidal_id:
            tidal_url = f"https://tidal.com/browse/track/{tidal_id}"
            completed = _try_tidaldl_url(tidal_url, str(provider_tmp), eff_timeout)
        elif provider == "qqdl":
            # MK15 FIX-D: sanitize search strings before QQDL lookup
            _q_artist = _sanitize_for_flac_search(artist)
            _q_title  = _sanitize_for_flac_search(title)
            if spotify_url or tidal_id:
                completed = _try_qqdl(spotify_url if spotify_url else None, tidal_id, str(provider_tmp), eff_timeout)
            else:
                completed = False
        elif provider == "monochrome":
            _m_isrc = track.get("isrc", "")
            if tidal_id or _m_isrc or (artist and title):
                completed = _try_monochrome(
                    spotify_url, tidal_id, str(provider_tmp), eff_timeout,
                    isrc=_m_isrc, artist=artist, title=title,
                )
            else:
                completed = False
        elif provider == "tiddl":
            completed = _try_tiddl(artist, title, str(provider_tmp), eff_timeout)
        elif provider == "spotiflac":
            completed = _try_spotiflac(spotify_url, str(provider_tmp), eff_timeout)
        elif provider == "streamrip":
            # MK15 FIX-D: Sanitize artist/title before FLAC source search.
            # CTO: "Niche Bongo Flava artists return zero results if metadata
            # isn't sanitized. Strip feat., Remix, TikTok, Sped Up before search."
            _s_artist = _sanitize_for_flac_search(artist)
            _s_title  = _sanitize_for_flac_search(title)
            completed = _try_streamrip(_s_artist, _s_title, str(provider_tmp), eff_timeout)
        elif provider == "ytdlp":
            _yt_album = track.get("album", "")
            completed = _try_ytdlp(artist, title, str(provider_tmp), eff_timeout,
                                   album=_yt_album, duration_ms=_dur_ms)
        else:  # spotdl
            completed = _try_spotdl(spotify_url, str(provider_tmp), eff_timeout)

        if completed:
            files = _collect_downloaded_files(provider_tmp)
            if files:
                log.info(f"  [Waterfall] {provider} succeeded  '{title}' -> {files[0].name}")
                _provider_record_success(provider)
                return files, provider
            found = [f.name for f in provider_tmp.rglob("*") if f.is_file()]
            log.warning(f"  [Waterfall] {provider} completed but no valid audio for '{title}'. Files: {found or '(none)'}")
            _provider_record_failure(provider)
        else:
            _provider_record_failure(provider)

        shutil.rmtree(provider_tmp, ignore_errors=True)  # mk37: staging

    return [], None


# ---------------------------------------------------------------------------
# Local library index helpers
# ---------------------------------------------------------------------------

def _build_local_index(folders: list) -> set:
    present = set()
    for folder in folders:
        folder = pathlib.Path(folder)
        if not folder.exists():
            continue
        for f in folder.rglob("*"):
            if f.suffix.lower() not in (".mp3", ".flac", ".m4a"):
                continue
            if not _is_valid(f):
                continue
            stem = re.sub(r"^\d{1,3}[\s\-\.]+", "", f.stem)
            present.add(_norm_title(stem))
    return present


def _build_device_index(device_name: str, storage_roots: list) -> "device_library.DeviceLibrary":
    # MK8 Fix — delegate to DeviceLibrary.build() which now handles the
    # zero-scan / cache-fallback case.  Previously this called from_scan()
    # directly, bypassing the cache logic added in device_library.py.
    try:
        return device_library.DeviceLibrary.build(device_name, storage_roots)
    except Exception as e:
        log.warning(f"Download stage: could not build device index ({e}) — using empty index.")
        return device_library.DeviceLibrary.from_scan([])


# ---------------------------------------------------------------------------
# Missing album collection (enhanced)
# ---------------------------------------------------------------------------

def _collect_missing_albums(
    track_map:       dict,
    local_index:     set,
    dev_lib:         "device_library.DeviceLibrary",
    liked_album_ids: set[str],
    sp_client,                  # kept for signature compatibility; no longer called
) -> dict:
    """
    Build the dict of albums that have at least one missing track.

    IMPORTANT: sp_client.album() has been removed from this function.
    The previous version called it once per unique album_id (~1490 calls),
    which consistently triggered Spotify's 24-hour rate limit (HTTP 429,
    Retry-After: 86400s).  total_tracks is now estimated from the number
    of tracks already present for that album_id in track_map, which is
    accurate enough for the threshold check (>= 3 missing) and avoids
    any additional API calls.
    """
    albums = {}

    # First pass: count total tracks per album_id across all playlists
    # so we can estimate total_tracks without hitting the Spotify API.
    album_total_tracks: dict[str, int] = {}
    album_is_comp:      dict[str, bool] = {}
    for _pl_name, tracks in track_map.items():
        for t in tracks:
            aid = t.get("album_id")
            if not aid:
                continue
            album_total_tracks[aid] = album_total_tracks.get(aid, 0) + 1
            if t.get("compilation"):
                album_is_comp[aid] = True

    for _pl_name, tracks in track_map.items():
        for t in tracks:
            artist = t.get("album_artist") or t.get("artist", "Unknown")
            album  = t.get("album", "Unknown")
            title  = t.get("title", "")
            album_id = t.get("album_id")

            if dev_lib.has_track(artist=artist, album=album, title=title):
                continue

            if not album_id:
                album_id = f"{artist} - {album}"
                total_tracks = 1
            else:
                # Use the count of tracks seen in track_map as a proxy for
                # total_tracks.  This is a lower bound (Spotify may have more
                # tracks on the album than are in the user's playlists), but
                # it is good enough for the >= ALBUM_DOWNLOAD_THRESHOLD_MISSING
                # check and avoids all Spotify API calls.
                total_tracks = album_total_tracks.get(album_id, 1)

            # Bug 6 fix: derive is_liked from the liked_album_ids set passed in
            # from spotify_client.get_saved_albums(), NOT from t["is_liked"].
            is_liked = bool(album_id) and (album_id in liked_album_ids)

            if album_id not in albums:
                folder_name, is_junk = dev_lib.resolve_folder(artist=artist, album=album)
                # B3: flag whether total_tracks is a playlist-proxy (lower bound).
                # When only 1-2 tracks from a long album appear in any playlist,
                # total_tracks == 1 or 2 — far below the real album length.
                # The strategy logic uses _tracks_estimated=True to avoid
                # triggering album-wise on albums where the threshold comparison
                # is meaningless (e.g. missing=2, total=2 → 100% but album has 20 tracks).
                tracks_estimated = bool(album_id) and album_id == t.get("album_id", "")
                albums[album_id] = {
                    "album_name":       album,
                    "album_artist":     artist,
                    "spotify_album_id": t.get("album_id", ""),
                    "existing_folder":  folder_name if not is_junk else None,
                    "junk_folder":      folder_name if is_junk else None,
                    "missing_tracks":   [],
                    "total_tracks":     total_tracks,
                    "is_liked":         is_liked,
                    "missing_count":    0,
                    "_tracks_estimated": True,   # B3: always treat as estimated
                }
                if folder_name and not is_junk:
                    log.debug(f"  [FolderMap] '{album}' -> existing folder '{folder_name}'")
                elif folder_name and is_junk:
                    log.info(f"  [FolderMap] '{album}' -> junk folder '{folder_name}' will be renamed")
            else:
                # Bug 6 fix: propagate is_liked if seen from another playlist context
                if is_liked and not albums[album_id]["is_liked"]:
                    albums[album_id]["is_liked"] = True

            # Add missing track
            track_entry = dict(t)
            title_norm = _norm_title(title)
            title_norm_nofeat = _norm_title(_strip_feat(title))
            track_entry["_local_only"] = title_norm in local_index or title_norm_nofeat in local_index
            existing_ids = {tr.get("id") for tr in albums[album_id]["missing_tracks"]}
            if t.get("id") not in existing_ids:
                albums[album_id]["missing_tracks"].append(track_entry)
                albums[album_id]["missing_count"] += 1

    total = sum(len(v["missing_tracks"]) for v in albums.values())
    log.info(f"Download stage: {total} missing track(s) across {len(albums)} album(s).")
    return albums


# ---------------------------------------------------------------------------
# Full album download from TIDAL (album‑wise)
# ---------------------------------------------------------------------------

def _download_full_album(
    album_info: dict,
    temp_root:  pathlib.Path,
    dl_cache:   dict,
    cache_lock: threading.Lock,
    mtp_lock:   threading.Lock,
    dev_lib:    "device_library.DeviceLibrary",
    sp_client,
) -> tuple[int, int]:
    """
    Download all tracks of an album from TIDAL (or fallback providers)
    and transfer the whole folder to the device.

    Fixes applied in this version
    ─────────────────────────────
    B1  When TIDAL album fetch fails, return a real failed count instead of
        (0, 0) so the caller's stats reflect the missed tracks.
    B2  Acquire mtp_lock around every CopyHere call — previously missing from
        the album-wise path while present in the per-track path, allowing 8
        concurrent MTP writes that race and produce ghost/corrupt files.
    B4  Use best_album.artist.name (album-level) for album_artist in every
        track dict, not t.artist.name (track-level) which may be a featured
        artist and causes ZX500 to split the album into multiple browser entries.
    I3  FLAC upgrade: if a track is already on device as MP3 and the TIDAL
        stream would deliver FLAC, download and replace it.  Uses the new
        dev_lib.has_track_as_flac() to distinguish MP3-present from FLAC-present.
    I2  TIDAL album search results are cached to .tidal_album_cache.json so
        re-runs skip the session.search() network round-trip.
    """
    transferred = 0
    failed      = 0

    album_artist = album_info["album_artist"]
    album_name   = album_info["album_name"]
    missing_count = album_info.get("missing_count", len(album_info.get("missing_tracks", [])))
    canonical_album_artist = album_artist   # default; overridden below if TIDAL resolves
    downloaded_files: list = []
    album_tmp_dir: "pathlib.Path | None" = None

    # ── 0. Monochrome album first (2026-09-02, user direction) ─────────────
    # Resolve the full tracklist from the monochrome /album/ proxy (no
    # tidalapi / TIDAL session needed). If it works, download each track
    # through the normal per-track waterfall (monochrome is provider #1) and
    # skip the tidalapi/streamrip path entirely.
    _mono_album_tracks: list = []
    _mono_meta: dict = {}
    if getattr(config, "MONOCHROME_ALBUM_FIRST", True):
        try:
            _mono_meta, _mono_album_tracks = _monochrome_album_tracklist(
                album_artist, album_name,
                int(getattr(config, "MONOCHROME_ALBUM_RESOLVE_TIMEOUT_S", 20)),
                is_liked=album_info.get("is_liked", False),
            )
        except Exception as _mae:
            log.debug(f"  [FullAlbum] monochrome album resolve error: {_mae}")
            _mono_album_tracks = []

    # ── 1. Resolve TIDAL album (with disk cache) — SKIPPED when monochrome
    #      already gave us the tracklist ─────────────────────────────────────
    best_album        = None
    tidal_album_tracks = []

    _cache_key = f"{album_artist.lower()}||{album_name.lower()}"
    try:
        if _mono_album_tracks:
            raise _MonochromeAlbumOK   # skip tidalapi + streamrip entirely
        import tidalapi
        session = _get_tidal_session()

        # Check disk cache first (I2)
        with _tidal_album_cache_lock:
            cached = _tidal_album_cache.get(_cache_key)

        if cached:
            try:
                best_album = session.album(cached["tidal_id"])
                tidal_album_tracks = best_album.tracks()
                log.debug(f"  [FullAlbum] TIDAL cache hit: {album_artist} - {album_name}")
            except Exception as _ce:
                log.debug(f"  [FullAlbum] Cached TIDAL id stale ({_ce}), re-searching")
                best_album = None
                tidal_album_tracks = []

        if best_album is None:
            query   = f"{album_artist} {album_name}"
            results = session.search(query, models=[tidalapi.Album], limit=5)
            albums  = results.get("albums", [])

            if not albums:
                log.warning(f"  [FullAlbum] No TIDAL album found for {album_artist} - {album_name}")
                raise LookupError(f"No TIDAL album found for {album_artist} - {album_name}")

            # Prefer exact match, fall back to best fuzzy
            for a in albums:
                if (a.artist.name.lower() == album_artist.lower() and
                        a.name.lower() == album_name.lower()):
                    best_album = a
                    break
            if not best_album:
                best_album = albums[0]
                log.info(f"  [FullAlbum] Using TIDAL album: {best_album.artist.name} - {best_album.name}")

            tidal_album_tracks = best_album.tracks()

            # Persist to disk cache (I2)
            with _tidal_album_cache_lock:
                global _tidal_album_cache_dirty
                _tidal_album_cache[_cache_key] = {
                    "tidal_id":     best_album.id,
                    "tidal_artist": best_album.artist.name,
                    "tidal_name":   best_album.name,
                }
                _tidal_album_cache_dirty = True

    except _MonochromeAlbumOK:
        # monochrome /album/ already gave us the tracklist — no fallback needed
        canonical_album_artist = _mono_meta.get("album_artist") or album_artist
        log.info(f"  [FullAlbum] Using monochrome album tracklist "
                 f"({len(_mono_album_tracks)} track(s)) for {album_artist} - {album_name}")
    except Exception as e:
        if _mono_album_tracks:
            canonical_album_artist = _mono_meta.get("album_artist") or album_artist
        else:
            log.warning(f"  [FullAlbum] Failed to fetch TIDAL album: {e}")
            # ── #2 streamrip whole-album fallback ────────────────────────────
            _sr_tmp = pathlib.Path(tempfile.mkdtemp(prefix="album_sr_", dir=temp_root))
            _sr_files = _try_streamrip_album(
                album_artist, album_name, str(_sr_tmp),
                getattr(config, "STREAMRIP_ALBUM_TIMEOUT_S", 600),
            )
            if _sr_files:
                downloaded_files = _tag_streamrip_album_files(_sr_files, album_info)
                album_tmp_dir = _sr_tmp
            else:
                shutil.rmtree(_sr_tmp, ignore_errors=True)
                return 0, missing_count   # B1: both TIDAL and streamrip failed

    # ── 2. Build track dicts — use album-level artist for album_artist (B4) ─
    # B4: best_album.artist.name is the canonical album artist (e.g. "Drake"),
    # whereas t.artist.name on individual tracks can be a featured artist
    # (e.g. "21 Savage"), which causes the ZX500 to split the album.
    if best_album is not None:
        canonical_album_artist = best_album.artist.name
        # tidalapi returns release_date as a datetime.datetime (not a str), so
        # slicing [:4] blows up with "'datetime.datetime' object is not
        # subscriptable" and fails the whole album. Handle both shapes.
        _rd = best_album.release_date
        if _rd:
            release_year = str(_rd.year) if hasattr(_rd, "year") else str(_rd)[:4]
        else:
            release_year = ""
    elif _mono_album_tracks:
        release_year = _mono_meta.get("release_year", "")
    else:
        release_year = ""
    disc_map: dict[int, int] = {}          # disc_number → running track index per disc
    tidal_tracks_list = list(tidal_album_tracks) if best_album is not None else []

    # MK8 Fix (LOG_17_3): populate album_image for TIDAL album downloads.
    # Previously the TIDAL track-dict had no album_image field, so
    # _embed_cover_for_track() found url="" and fell through to the yt-dlp
    # thumbnail source — which also doesn't exist for TIDAL streams.  Result:
    # every TIDAL album arrived on device with no cover art.
    #
    # Priority for album_image:
    #   1. TIDAL best_album.image(480) — the TIDAL CDN cover JPEG (480px)
    #   2. Spotify ref_track["album_image"] — from album_info["missing_tracks"][0]
    #      (set by spotify_client._parse_track() via the Spotify API)
    _tidal_album_image: str = ""
    if best_album is not None:
        try:
            # tidalapi Album.image(dimensions) returns a CDN URL for the cover art
            _tidal_album_image = best_album.image(480) or ""
        except Exception:
            pass
    if not _tidal_album_image and _mono_album_tracks:
        _tidal_album_image = _mono_meta.get("cover_url", "")
    if not _tidal_album_image:
        _ref_tracks = album_info.get("missing_tracks") or []
        _tidal_album_image = (_ref_tracks[0].get("album_image") or "") if _ref_tracks else ""
    if _tidal_album_image:
        log.debug(f"  [Cover] album_image resolved for '{album_name}': {_tidal_album_image[:60]!r}")
    else:
        log.debug(f"  [Cover] no album_image available for '{album_name}' — cover will be missing")

    album_tracks = []
    if best_album is None and _mono_album_tracks:
        # monochrome path — track dicts already in the right shape
        for _mt in _mono_album_tracks:
            _mt.setdefault("album_image", _tidal_album_image)
            _mt.setdefault("release_year", release_year)
            _mt["album_artist"] = canonical_album_artist
            album_tracks.append(_mt)
    if best_album is not None:
        for t in tidal_tracks_list:
            disc  = getattr(t, "volume_num", 1) or 1
            tnum  = getattr(t, "track_num",  None) or (len(album_tracks) + 1)
            album_tracks.append({
                "id":           f"tidal_{t.id}",
                "title":        t.name,
                "artists":      [t.artist.name],
                "artist":       t.artist.name,      # track-level (for display/tag)
                "album":        album_name,
                "album_artist": canonical_album_artist,   # B4: album-level always
                "track_number": tnum,
                "disc_number":  disc,
                "duration_ms":  t.duration * 1000 if hasattr(t, "duration") else 0,
                "isrc":         getattr(t, "isrc", ""),
                "genre":        "",
                "release_year": release_year,
                "album_image":  _tidal_album_image,  # MK8: cover art URL
                "is_liked":     album_info.get("is_liked", False),  # propagate liked status
            })

    # ── 3. Download each track ─────────────────────────────────────────────
    if best_album is not None or (_mono_album_tracks and album_tmp_dir is None):
        album_tmp_dir   = pathlib.Path(tempfile.mkdtemp(prefix="album_", dir=temp_root))
        downloaded_files = []

    for track in album_tracks:
        title  = track["title"]
        artist = track["artist"]
        album  = track["album"]

        # I3: skip only if already present as FLAC; download to upgrade MP3→FLAC
        already_flac = dev_lib.has_track_as_flac(
            artist=canonical_album_artist, album=album, title=title
        )
        if already_flac:
            log.debug(f"  [FullAlbum] Already FLAC on device: {title}")
            continue

        # Check if present as any format (for skip-vs-upgrade decision)
        already_any = dev_lib.has_track(
            artist=canonical_album_artist, album=album, title=title
        )

        files, provider = _download_with_waterfall(track, album_tmp_dir, dl_cache)
        if not files:
            if not already_any:
                log.warning(f"  [FullAlbum] Failed to download: {title}")
                failed += 1
            else:
                log.debug(f"  [FullAlbum] Already present (non-FLAC), download failed — keeping existing: {title}")
            continue

        files = apply_storage_aware_format_fallback(files, track)
        src_file = files[0]
        incoming_ext = src_file.suffix.lower()

        # I3: if already present and incoming is not FLAC, skip the upgrade
        if already_any and incoming_ext != ".flac":
            log.debug(f"  [FullAlbum] Already present, incoming is {incoming_ext} not .flac — skipping upgrade: {title}")
            src_file.unlink(missing_ok=True)  # mk37: staging
            continue

        if already_any:
            log.info(f"  [FullAlbum] Upgrading MP3 -> FLAC: {title}")

        try:
            # Rev 14.9.2: _tag_file may swap .m4a → .mp3 via ffmpeg rescue.
            # Capture the return value so downstream transfer sees the real path.
            # MK17 Patch 1: writability pre-check + retry + copy-tag-replace fallback.
            _idh.ensure_writable(src_file)
            tagged = _tag_file(src_file, track)
            if tagged is not None and tagged != src_file:
                src_file = tagged
                incoming_ext = src_file.suffix.lower()
        except (PermissionError, OSError) as e:
            # Same three-layer recovery as the per-track path.
            _retry_ok = False
            _last_err = e
            for _attempt, _delay_ms in enumerate((50, 200, 800), start=1):
                time.sleep(_delay_ms / 1000.0)
                try:
                    _idh.ensure_writable(src_file)
                    tagged = _tag_file(src_file, track)
                    if tagged is not None and tagged != src_file:
                        src_file = tagged
                        incoming_ext = src_file.suffix.lower()
                    _retry_ok = True
                    log.debug(
                        f"  [FullAlbum] Tag write retry {_attempt} succeeded "
                        f"for '{src_file.name}' after {_delay_ms}ms"
                    )
                    break
                except (PermissionError, OSError) as e2:
                    _last_err = e2

            if not _retry_ok:
                fallback_copy = _idh.copy_to_writable_temp(src_file)
                if fallback_copy is not None:
                    try:
                        tagged_copy = _tag_file(fallback_copy, track)
                        tag_target = tagged_copy if tagged_copy else fallback_copy
                        if _idh.atomic_replace(tag_target, src_file):
                            log.info(
                                f"  [FullAlbum] Tag write recovered via "
                                f"copy-tag-replace for '{title}' "
                                f"(original was locked: {_last_err})"
                            )
                        else:
                            log.info(
                                f"  [FullAlbum] Tag write recovered via copy "
                                f"(original locked, using copy): '{title}'"
                            )
                            src_file = tag_target
                            incoming_ext = src_file.suffix.lower()
                        _XFER_COUNTERS_TAGFALLBACK[0] += 1
                    except Exception as e3:
                        log.warning(f"  [FullAlbum] Tagging fully failed for {title}: {e3}")
                        _XFER_COUNTERS_TAGFAIL[0] += 1
                else:
                    log.warning(
                        f"  [FullAlbum] Tagging failed for {title} "
                        f"(retries+copy unavailable): {_last_err}"
                    )
                    _XFER_COUNTERS_TAGFAIL[0] += 1
        except Exception as e:
            log.warning(f"  [FullAlbum] Tagging failed for {title}: {e}")
            _XFER_COUNTERS_TAGFAIL[0] += 1

        # Rev 14.9.3 F6 — _track_filename now raises ValueError on empty
        # title; skip the track rather than overwrite a sibling.
        try:
            canonical_stem = _track_filename(track)
        except ValueError as ve:
            log.warning(f"  [FullAlbum] Skipping malformed track: {ve}")
            try:
                src_file.unlink()  # mk37: staging
            except Exception:
                pass
            continue
        canonical_ascii = _ascii_safe(canonical_stem) + incoming_ext
        renamed         = src_file.parent / canonical_ascii
        if src_file != renamed:
            try:
                src_file.rename(renamed)  # mk37: staging
                src_file = renamed
            except Exception:
                pass

        downloaded_files.append(src_file)

    # ── 4. Transfer all files — lock MTP for every write (B2) ─────────────
    existing_folder = album_info.get("existing_folder")
    if existing_folder:
        dest_folder = _safe(existing_folder, 100)
    else:
        ref_track = album_info["missing_tracks"][0] if album_info.get("missing_tracks") else None
        if ref_track:
            dest_folder = _safe(_album_folder_name(ref_track), 100)
        else:
            dest_folder = _safe(f"{canonical_album_artist} - {album_name}", 100)
    dest_parts = [config.MUSIC_ROOT, dest_folder]

    # Ensure folder exists (needs lock — folder creation races too)
    with mtp_lock:
        try:
            storage_root = mtp._get_storage_root(config.DEVICE_NAME, config.WRITE_STORAGE)
            mtp._navigate(storage_root, dest_parts, create=True)
        except Exception as e:
            log.warning(f"  [FullAlbum] Could not pre-create folder: {e}")

    for file_path in downloaded_files:
        try:
            target_storage = mtp._pick_storage_for_file(file_path.stat().st_size)
        except mtp.StorageFullError:
            raise

        file_ext = file_path.suffix.lower()
        for attempt in range(MTP_TRANSFER_RETRIES):
            try:
                # B2: acquire mtp_lock — prevents concurrent CopyHere races
                with mtp_lock:
                    log.info(
                        f"  [FullAlbum] Transferring '{file_path.name}'"
                        f" -> {target_storage}/{'/'.join(dest_parts)}/"
                    )
                    # MK37/S2: quorum-verified transfer (board P0-2)
                    try:
                        from mk37_wiring import transfer_via_legacy_with_quorum
                        _device_relpath = "/".join(dest_parts + [file_path.name])
                        _result = transfer_via_legacy_with_quorum(
                            mtp.copy_file_to_device,
                            src          = file_path,
                            dest_parts   = dest_parts,
                            device_name  = config.DEVICE_NAME,
                            storage_name = target_storage,
                            device_index = _xfer_get_device_index(),
                            source_local_path=str(file_path),
                            device_relpath=_device_relpath,
                        )
                        if not _result.get("completed"):
                            log.warning(
                                f"  [MK37/S2/FullAlbum] transfer not completed: "
                                f"{_result.get('reason')} tier={_result.get('effective_tier')}"
                            )
                    except ImportError:
                        mtp.copy_file_to_device(
                            src          = file_path,
                            dest_parts   = dest_parts,
                            device_name  = config.DEVICE_NAME,
                            storage_name = target_storage,
                            device_index = _xfer_get_device_index(),  # REV 14.9 (C2)
                        )
                transferred += 1
                _xfer_bump("ok")  # REV 14.9 (D3)
                # Update dev_lib so subsequent has_track / has_track_as_flac calls
                # in this run see the newly transferred file without re-scanning
                try:
                    track_title = pathlib.Path(file_path.stem).name
                    dev_lib.add_track(
                        artist  = canonical_album_artist,
                        album   = album_name,
                        title   = track_title,
                        storage = target_storage,
                        parts   = dest_parts + [file_path.name],
                        ext     = file_ext,
                    )
                except Exception:
                    pass
                break
            except Exception as e:
                log.warning(
                    f"  [FullAlbum] Transfer attempt {attempt + 1} failed"
                    f" for {file_path.name}: {e}"
                )
                _xfer_first_failure_snapshot()  # REV 14.9 (D2)
                if attempt < MTP_TRANSFER_RETRIES - 1:
                    time.sleep(5 * (2 ** attempt))
                else:
                    failed += 1
                    _xfer_bump("fail")  # REV 14.9 (D3)
        time.sleep(2.0)   # cooldown between transfers

    if album_tmp_dir is not None:
        shutil.rmtree(album_tmp_dir, ignore_errors=True)

    log.info(
        f"  [Album Done] {canonical_album_artist} - {album_name}: "
        f"{transferred} transferred, {failed} failed"
    )
    return transferred, failed


# ---------------------------------------------------------------------------
# Per‑album processing (hybrid)
# ---------------------------------------------------------------------------

def _download_and_transfer_album(
    album_info: dict,
    temp_root:  pathlib.Path,
    cancelled:  list,
    dl_cache:   dict,
    cache_lock: threading.Lock,
    mtp_lock:   threading.Lock,
    dev_lib:    "device_library.DeviceLibrary",
    sp_client,
) -> tuple[int, int]:
    if cancelled[0]:
        return 0, 0

    # FIX B1: mid-run guard — abort this album if PC disk is running low.
    _pc_min = getattr(config, "REBUILD_PC_MIN_FREE_GB", 10.0)
    _pc_free = _get_pc_free_gb()
    if _pc_free < _pc_min:
        log.warning(
            f"  [DiskGuard] PC only {_pc_free:.1f} GB free "
            f"(min {_pc_min} GB) — skipping {album_info.get('album_name', '?')}"
        )
        return 0, 0
    album_key = f"{album_info['album_artist']}||{album_info['album_name']}"
    if album_info.get("is_liked") and album_info.get("missing_count", 0) == 0:
        liked_done = _get_liked_processed(dl_cache)
        if album_key in liked_done:
            log.debug(
                f"  [I4/Skip] Liked album already fully processed: {album_info['album_name']}"
            )
            return 0, 0

    # Determine strategy
    strategy = "track"
    if DOWNLOAD_STRATEGY == "album":
        strategy = "album"
    elif DOWNLOAD_STRATEGY == "track":
        strategy = "track"
    else:  # auto
        # Bug 5 fix: use the periodically-refreshed SD free space cache instead
        # of a live MTP query per album (which was only called once at startup
        # in the old code, meaning the threshold never fired mid-run).
        sd_free_gb = _get_sd_free_gb_cached()

        if sd_free_gb >= ALBUM_DOWNLOAD_MIN_FREE_SPACE_GB:
            if PRIORITIZE_LIKED_ALBUMS and album_info.get("is_liked"):
                strategy = "album"
            else:
                missing = album_info.get("missing_count", 0)
                total   = album_info.get("total_tracks", 0)
                # B3: Only trigger album-wise on the missing-count threshold when
                # total_tracks is a real count (not a playlist-proxy undercount).
                # When _tracks_estimated is True, total_tracks equals the number
                # of playlist appearances — e.g. an album with 20 tracks where
                # only 2 appear in any playlist has total_tracks=2 and
                # missing_count=2, making missing/total look like 100% missing.
                # We require missing >= threshold AND total >= threshold to ensure
                # we're looking at a meaningfully incomplete album, not an album
                # that simply has few playlist-visible tracks.
                tracks_estimated = album_info.get("_tracks_estimated", False)
                if total and missing >= ALBUM_DOWNLOAD_THRESHOLD_MISSING:
                    if not tracks_estimated or total >= ALBUM_DOWNLOAD_THRESHOLD_MISSING:
                        strategy = "album"
                    else:
                        log.debug(
                            f"  [B3/Strategy] Suppressed album-wise for '{album_info['album_name']}' "
                            f"— total_tracks={total} is a playlist proxy (estimated), "
                            f"missing={missing}. Using per-track."
                        )
        else:
            log.debug(
                f"  [Strategy] SD free {sd_free_gb:.1f} GB < "
                f"{ALBUM_DOWNLOAD_MIN_FREE_SPACE_GB} GB threshold — "
                f"using per-track for {album_info['album_name']}"
            )

    if strategy == "album":
        log.info(f"  [Album-wise] Downloading full album: {album_info['album_artist']} - {album_info['album_name']}")
        transferred, failed = _download_full_album(
            album_info, temp_root, dl_cache, cache_lock, mtp_lock, dev_lib, sp_client
        )
        # I4: mark liked album as processed if it completed without failure
        if album_info.get("is_liked") and failed == 0:
            _mark_liked_processed(dl_cache, cache_lock, album_key)
        return transferred, failed

    # Per‑track processing (original)
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="walkman_dl_", dir=temp_root))
    transferred = 0
    failed = 0
    permanently_missing = []

    # Rev 14.9.4 D6 — Master Artist enforcement.  Within this album, force
    # every track's album_artist to the same canonical value.  Without this,
    # TIDAL-tagged tracks ("Akon") and yt-dlp-tagged tracks ("Akon feat.
    # Snoop Dogg") in the same album yield different ALBUM_ID hashes in
    # MediaStore, splintering the album into "Akon - Trouble Deluxe" with
    # 3 tracks and "Akon feat. Snoop - Trouble Deluxe" with 1 track.
    # Source priority for the canonical value:
    #   1. album_info["album_artist"] (set by the album-discovery pass in
    #      _build_track_index_for_strategy — the most authoritative source)
    #   2. The first track's album_artist or artist (best-effort fallback)
    canonical_album_artist_for_per_track = (
        album_info.get("album_artist")
        or (album_info["missing_tracks"][0].get("album_artist")
            if album_info.get("missing_tracks") else None)
        or (album_info["missing_tracks"][0].get("artist")
            if album_info.get("missing_tracks") else None)
        or "Unknown"
    )

    for track in album_info["missing_tracks"]:
        if cancelled[0]:
            break

        # Rev 14.9.4 D6 — overwrite album_artist with the canonical for this
        # album so all tracks tag identically.  We mutate the dict in place;
        # the original artist (track-level) is still preserved as track["artist"].
        track["album_artist"] = canonical_album_artist_for_per_track

        artist = track.get("artist", "Unknown")
        title  = track.get("title",  "Unknown")
        sp_id  = track.get("id", "")

        if not sp_id:
            log.warning(f"  [Download] No Spotify ID for '{title}' - skipping")
            failed += 1
            continue

        with cache_lock:
            if dl_cache.get(sp_id) == "failed":
                log.info(f"  [Download] '{title}' previously failed all providers - skipping")
                failed += 1
                continue

        log.info(f"  [Download] {artist} - {title}  ({album_info['album_name']})")

        try:
            # Local file lookup
            local_src_file = None
            title_norm = _norm_title(_strip_feat(title))
            artist_norm = _norm_title(artist)

            for folder in config.LOCAL_FOLDERS:
                folder = pathlib.Path(folder)
                if not folder.exists():
                    continue
                for f in folder.rglob("*"):
                    if f.suffix.lower() not in (".flac", ".mp3", ".m4a"):
                        continue
                    if not _is_valid(f):
                        continue
                    raw_stem = f.stem
                    stem_clean = re.sub(r"^\d{1,3}[\s\-\.]+", "", raw_stem).strip()
                    parts = [p.strip() for p in stem_clean.split(" - ")]
                    if len(parts) >= 3:
                        file_artist_norm = _norm_title(parts[0])
                        file_title_norm = _norm_title(re.sub(r"^\d{1,3}\s+", "", parts[-1]))
                    elif len(parts) == 2:
                        file_artist_norm = _norm_title(parts[0])
                        file_title_norm = _norm_title(parts[1])
                    else:
                        file_artist_norm = ""
                        file_title_norm = _norm_title(stem_clean)

                    title_match = (file_title_norm == title_norm or
                                   _norm_title(_strip_feat(raw_stem)) == title_norm)
                    artist_match = (not file_artist_norm or not artist_norm or
                                    file_artist_norm == artist_norm)

                    if title_match and artist_match:
                        if local_src_file is None or f.suffix.lower() == ".flac":
                            local_src_file = f

            if local_src_file is not None:
                log.info(f"  [Download] Using local file: {local_src_file.name}")
                winning_provider = "local"
                tmp_copy = tmp_dir / local_src_file.name
                shutil.copy2(local_src_file, tmp_copy)
                try:
                    import stat as _stat
                    tmp_copy.chmod(tmp_copy.stat().st_mode | _stat.S_IWRITE)
                except Exception:
                    pass
                # MK15-N1: 500ms wait after local-file copy so Windows AV/indexer
                # finishes scanning before mutagen writes tags. The inner 5-attempt
                # retry loop handles anything that still slips through.
                time.sleep(0.5)
                files = [tmp_copy]
            elif track.get("_local_only"):
                log.warning(f"  [Download] Local file not found for '{title}' - falling back to download")
                files, winning_provider = _download_with_waterfall(track, tmp_dir, dl_cache)
            else:
                files, winning_provider = _download_with_waterfall(track, tmp_dir, dl_cache)

            if files:
                files = apply_storage_aware_format_fallback(files, track)

            if not files:
                sp_url = f"https://open.spotify.com/track/{sp_id}"
                log.error(f"  [Download] All providers failed for '{title}' by {artist}. URL: {sp_url}")
                permanently_missing.append(f"{artist} — {title}  [{sp_url}]")
                with cache_lock:
                    dl_cache[sp_id] = "failed"
                failed += 1
                # MK17 Patch 5: track provider exhaustion for run-summary
                try:
                    _XFER_COUNTERS_PROVIDER_EXHAUST[0] += 1
                except Exception:
                    pass
                continue

            with cache_lock:
                dl_cache[sp_id] = winning_provider

            src_file = files[0]

            # Canonical rename
            # Rev 14.9.3 F6 — refuse empty-title tracks
            try:
                canonical_stem = _track_filename(track)
            except ValueError as ve:
                log.warning(f"  [Download] Skipping malformed track: {ve}")
                try:
                    src_file.unlink()
                except Exception:
                    pass
                continue
            canonical_ascii = _ascii_safe(canonical_stem) + src_file.suffix.lower()
            renamed = src_file.parent / canonical_ascii
            if src_file != renamed:
                _rename_ok = False
                for _rn_attempt, _rn_delay_ms in enumerate((50, 200, 800), start=1):
                    try:
                        src_file.rename(renamed)
                        src_file = renamed
                        _rename_ok = True
                        if _rn_attempt > 1:
                            log.debug(
                                f"  [Download] Rename succeeded on attempt {_rn_attempt} "
                                f"for '{canonical_ascii}'"
                            )
                        break
                    except (PermissionError, OSError) as _rn_err:
                        if _rn_attempt < 3:
                            time.sleep(_rn_delay_ms / 1000.0)
                        else:
                            log.warning(
                                f"  [Download] Rename failed for '{src_file.name}' "
                                f"after 3 attempts: {_rn_err}"
                            )

            # Tag file
            try:
                # Rev 14.9.2: _tag_file may swap .m4a → .mp3 via ffmpeg rescue.
                # Capture the return value so the subsequent _pick_storage_for_file
                # and copy_file_to_device calls see the real, on-disk path.
                # Without this, WinError 2 ("file not found") fires on transfer
                # because the old .m4a has been unlinked by the converter.
                #
                # MK17 Patch 1: ensure parent + file are writable before tagging.
                # Cheap pre-check that catches read-only temp dirs (some AV
                # quarantine paths land RO) before mutagen even tries to open.
                _idh.ensure_writable(src_file)
                tagged = _tag_file(src_file, track)
                if tagged is not None and tagged != src_file:
                    src_file = tagged
            except (PermissionError, OSError) as e:
                # MK17 Patch 1 — three-layer recovery for tag-write failures.
                #
                # Layer 1 (Rev 14.9.7): Windows AV or another process briefly
                # locks the temp file right after yt-dlp / tiddl writes it.
                # LOG_16 showed 600 Errno 13 Permission denied tag-write
                # failures over a 13h run.  Original retry was 3× with
                # 50ms / 200ms / 800ms backoff (total ~1.1s).
                #
                # MK17 Patch 10 (post-LOG_17 fix): extended backoff schedule
                # from 3 attempts (50/200/800ms = 1.1s total) to 5 attempts
                # (100/400/1500/3000/6000ms = ~11s total).  LOG_17 still
                # had 324 tag-write failures even with the original ladder
                # — Windows Defender's real-time scanning of newly-written
                # MP3s can hold the lock for several seconds on slower
                # disks.  Total budget of 11s is well below the ~30s/file
                # that yt-dlp itself spends on download.
                #
                # Layer 2 (MK17 Patch 1): if all retries fail, copy the file
                # to a brand-new writable temp dir, tag the copy, then
                # atomically replace the original.  Closes the case where
                # the original temp landed on a locked/RO mount that no
                # backoff would clear.  Track stats so the run-summary
                # surfaces this as a fallback event.
                _retry_ok = False
                _last_err = e
                for _attempt, _delay_ms in enumerate(
                        (100, 400, 1500, 3000, 6000), start=1):
                    time.sleep(_delay_ms / 1000.0)
                    try:
                        _idh.ensure_writable(src_file)
                        tagged = _tag_file(src_file, track)
                        if tagged is not None and tagged != src_file:
                            src_file = tagged
                        _retry_ok = True
                        log.debug(
                            f"  [Download] Tag write retry {_attempt} succeeded "
                            f"for '{src_file.name}' after {_delay_ms}ms"
                        )
                        break
                    except (PermissionError, OSError) as e2:
                        _last_err = e2

                if not _retry_ok:
                    # Layer 2 — copy-tag-replace fallback
                    fallback_copy = _idh.copy_to_writable_temp(src_file)
                    if fallback_copy is not None:
                        try:
                            tagged_copy = _tag_file(fallback_copy, track)
                            tag_target = tagged_copy if tagged_copy else fallback_copy
                            # Replace the original with the tagged copy.  If
                            # the original is held by another process, this
                            # will fail and we'll keep using the copy as src.
                            if _idh.atomic_replace(tag_target, src_file):
                                log.info(
                                    f"  [Download] Tag write recovered via "
                                    f"copy-tag-replace for '{src_file.name}' "
                                    f"(original was locked: {_last_err})"
                                )
                                _retry_ok = True
                            else:
                                # Original still locked — use the copy as
                                # the new src so the transfer at least gets
                                # a tagged file.
                                log.info(
                                    f"  [Download] Tag write recovered via "
                                    f"copy (original locked, using copy as "
                                    f"transfer source): '{src_file.name}'"
                                )
                                src_file = tag_target
                                _retry_ok = True
                            try:
                                _XFER_COUNTERS_TAGFALLBACK[0] += 1
                            except Exception:
                                pass
                        except Exception as e3:
                            log.warning(
                                f"  [Download] Tag write fallback FAILED for "
                                f"'{src_file.name}' after retries+copy: {e3}"
                            )
                            try:
                                _XFER_COUNTERS_TAGFAIL[0] += 1
                            except Exception:
                                pass
                    else:
                        log.warning(
                            f"  [Download] Tag write FAILED for "
                            f"'{src_file.name}' (all retries exhausted, "
                            f"copy-fallback unavailable): {_last_err}"
                        )
                        try:
                            _XFER_COUNTERS_TAGFAIL[0] += 1
                        except Exception:
                            pass
            except Exception as e:
                log.warning(f"  [Download] Tag write failed for '{src_file.name}': {e}")
                try:
                    _XFER_COUNTERS_TAGFAIL[0] += 1
                except Exception:
                    pass

            # Storage selection
            try:
                target_storage = mtp._pick_storage_for_file(src_file.stat().st_size)
            except mtp.StorageFullError:
                raise

            # Folder resolution
            existing_folder = album_info.get("existing_folder")
            junk_folder = album_info.get("junk_folder")
            canonical_folder = _safe(_album_folder_name(track), 100)

            if existing_folder:
                album_folder = existing_folder
            elif junk_folder:
                album_folder = canonical_folder
                if album_info.get("junk_folder"):
                    album_info["junk_folder"] = None
                    try:
                        ok = mtp.rename_folder_on_device(
                            device_name=config.DEVICE_NAME,
                            storage_name=target_storage,
                            parent_parts=[config.MUSIC_ROOT],
                            old_name=junk_folder,
                            new_name=canonical_folder,
                        )
                        if ok and junk_folder != canonical_folder:
                            log.info(f"  [Rename] Junk folder renamed: '{junk_folder}' -> '{canonical_folder}'")
                        elif ok:
                            log.debug(f"  [Rename] Junk folder already canonical: '{junk_folder}'")
                        else:
                            log.warning(f"  [Rename] Could not rename '{junk_folder}'")
                    except Exception as e:
                        log.warning(f"  [Rename] Junk folder rename failed: {e}")
            else:
                album_folder = canonical_folder

            dest_parts = [config.MUSIC_ROOT, _safe(album_folder, 100)]
            _lock = mtp_lock if mtp_lock is not None else threading.Lock()

            # Transfer with retries
            transfer_ok = False
            for attempt in range(MTP_TRANSFER_RETRIES):
                try:
                    with _lock:
                        log.info(f"  [Transfer] {src_file.name}  ->  {target_storage}/{'/'.join(dest_parts)}/")
                        # MK37/S2: quorum-verified transfer (board P0-2)
                        try:
                            from mk37_wiring import transfer_via_legacy_with_quorum
                            _device_relpath = "/".join(dest_parts + [src_file.name])
                            _result = transfer_via_legacy_with_quorum(
                                mtp.copy_file_to_device,
                                src=src_file,
                                dest_parts=dest_parts,
                                device_name=config.DEVICE_NAME,
                                storage_name=target_storage,
                                device_index=_xfer_get_device_index(),
                                source_local_path=str(src_file),
                                device_relpath=_device_relpath,
                            )
                            if not _result.get("completed"):
                                # Treat quorum failure as transfer failure for retry purposes
                                raise RuntimeError(
                                    f"MK37/S2 quorum failed: {_result.get('reason')} "
                                    f"tier={_result.get('effective_tier')}"
                                )
                        except ImportError:
                            mtp.copy_file_to_device(
                                src=src_file,
                                dest_parts=dest_parts,
                                device_name=config.DEVICE_NAME,
                                storage_name=target_storage,
                                # REV 14.9 (C2) — pass in-memory index if session has one
                                device_index=_xfer_get_device_index(),
                            )
                    transfer_ok = True
                    _xfer_bump("ok")  # REV 14.9 (D3)
                    break
                except Exception as e:
                    log.warning(f"  [Transfer] Attempt {attempt+1} failed: {e}")
                    # REV 14.9 (D2) — first-failure snapshot
                    _xfer_first_failure_snapshot()
                    if attempt < MTP_TRANSFER_RETRIES - 1:
                        time.sleep(5 * (2 ** attempt))
                    else:
                        _xfer_bump("fail")  # REV 14.9 (D3)
                        raise

            if not transfer_ok:
                raise RuntimeError(f"All {MTP_TRANSFER_RETRIES} transfer attempts failed")

            # Cooldown
            time.sleep(2.0)

            # FIX B2: update dev_lib immediately so subsequent has_track() calls
            # in this same run don't see this track as missing and re-transfer it.
            try:
                dev_lib.add_track(
                    artist  = artist,
                    album   = album_info.get("album_name", ""),
                    title   = _strip_feat(title),
                    storage = target_storage,
                    parts   = dest_parts + [src_file.name],
                    ext     = src_file.suffix.lower(),
                )
            except Exception:
                pass

            # Delete local temp file
            try:
                import stat as _stat
                src_file.chmod(src_file.stat().st_mode | _stat.S_IWRITE)
                src_file.unlink()
            except Exception:
                pass

            transferred += 1
            log.info(f"  [Done] {artist} - {title} (via {winning_provider})")

        except mtp.StorageFullError:
            raise
        except Exception as e:
            log.error(f"  [Failed] '{title}' by {artist}: {e}")
            failed += 1

    if permanently_missing:
        try:
            report = getattr(config, "REPORT_FILE", None)
            if report:
                with open(report, "a", encoding="utf-8") as fh:
                    for line in permanently_missing:
                        fh.write(line + "\n")
                log.info(f"  [{album_info['album_name']}] {len(permanently_missing)} track(s) appended to REPORT_FILE")
        except Exception as e:
            log.warning(f"[Download] Could not write to REPORT_FILE: {e}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    if tmp_dir.exists():
        _force_rmtree(tmp_dir)

    if transferred or failed:
        log.info(f"  [Album Done] {album_info['album_artist']} - {album_info['album_name']}: {transferred} transferred, {failed} failed")
    return transferred, failed


# ---------------------------------------------------------------------------
# Stale temp dir salvage with failure cache
# ---------------------------------------------------------------------------

def _parse_canonical_filename(stem: str) -> tuple[str, str, str]:
    stem = _strip_feat(stem).strip()
    parts = [p.strip() for p in stem.split(" - ")]
    if len(parts) >= 3:
        artist = parts[0]
        title_part = parts[-1]
        title = re.sub(r"^\d{1,3}\s+", "", title_part).strip() or title_part
        album = " - ".join(parts[1:-1])
        album = re.sub(r"\s*CD\d+\s*$", "", album).strip()
        return artist, album, title
    if len(parts) == 2:
        return parts[0], parts[0], parts[1]
    return "", "", stem


def _salvage_stale_downloads_with_lib(
    stale_dirs: list,
    dev_lib: "device_library.DeviceLibrary",
) -> int:
    global _SALVAGE_FAIL_CACHE
    audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".aac"}
    transferred = 0
    MAX_SALVAGE_ATTEMPTS = 3

    for stale_dir in stale_dirs:
        if not stale_dir.is_dir():
            continue

        log.info(f"  [Salvage] Processing stale directory: {stale_dir}")

        # I5: collect valid audio files; attempt to remediate corrupt .flac files
        # (e.g. yt-dlp writing MP3 audio into a .flac container) before discarding them.
        raw_candidates = [f for f in stale_dir.rglob("*") if f.is_file() and f.suffix.lower() in audio_exts]
        audio_files = []
        for f in raw_candidates:
            if _is_valid(f):
                audio_files.append(f)
            elif f.suffix.lower() == ".flac":
                fixed = _remediate_corrupt_flac(f)
                if fixed is not None and _is_valid(fixed):
                    log.info(f"  [Salvage/I5] Remediated corrupt FLAC: {f.name} -> {fixed.name}")
                    audio_files.append(fixed)
                else:
                    log.debug(f"  [Salvage/I5] Could not remediate {f.name} — discarding")
        if not audio_files:
            _force_rmtree(stale_dir)
            log.debug(f"  [Salvage] Removed empty/invalid stale dir: {stale_dir.name}")
            continue

        log.info(f"  [Salvage] {stale_dir.name}: found {len(audio_files)} audio file(s)")

        all_skipped = True
        for src_file in audio_files:
            try:
                size_mb = src_file.stat().st_size / (1024*1024)
                log.info(f"  [Salvage] File: {src_file.name} ({size_mb:.2f} MB) at {src_file.parent}")
            except Exception:
                pass

            file_key = str(src_file.absolute())
            if _SALVAGE_FAIL_CACHE.get(file_key, 0) >= MAX_SALVAGE_ATTEMPTS:
                log.debug(f"  [Salvage] Skipping {src_file.name} (failed {MAX_SALVAGE_ATTEMPTS} times already)")
                try:
                    src_file.unlink()
                    log.info(f"  [Salvage] Removed failing file: {src_file.name}")
                except Exception as e:
                    log.warning(f"  [Salvage] Could not remove {src_file.name}: {e}")
                continue

            try:
                stem = src_file.stem
                artist, album, title = _parse_canonical_filename(stem)

                if not title:
                    log.debug(f"  [Salvage] Skipping '{src_file.name}' - could not parse title")
                    continue

                if artist and album and dev_lib.has_track(artist=artist, album=album, title=title):
                    log.debug(f"  [Salvage] Already on device: '{title}' - skipping")
                    continue

                # Use the public has_track with title-only variants as fallback
                # (replaces the removed direct _all_title_keys attribute access)
                title_variants = [title]
                stripped_num = re.sub(r"^\d{1,3}[\s\-\.]+", "", title).strip()
                if stripped_num and stripped_num != title:
                    title_variants.append(stripped_num)

                skip = False
                for tv in title_variants:
                    if dev_lib.has_track(artist="", album="", title=tv):
                        log.debug(f"  [Salvage] Title already on device: '{tv}' - skipping")
                        skip = True
                        break
                if skip:
                    continue

                # M4A via CopyHere reliably times out on Internal shared storage
                # (the Walkman MTP stack rejects raw AAC containers from in-process
                # CopyHere; this is what caused the ~2000s timeout loops last night).
                # Convert to MP3 first if ffmpeg is available.
                if src_file.suffix.lower() == ".m4a":
                    if _FFMPEG_DIR:
                        converted = _convert_raw_aac_to_mp3(src_file, src_file.parent)
                        if converted and converted.exists():
                            log.info(f"  [Salvage] Converted M4A -> MP3: {converted.name}")
                            try:
                                src_file.unlink()  # mk37: staging
                            except Exception:
                                pass
                            src_file = converted
                            stem = converted.stem
                        else:
                            log.warning(
                                f"  [Salvage] M4A conversion failed for {src_file.name}; "
                                f"skipping to avoid multi-hour CopyHere timeout"
                            )
                            continue
                    else:
                        log.warning(
                            f"  [Salvage] Skipping M4A '{src_file.name}' — ffmpeg not found. "
                            f"Install ffmpeg (conda install -c conda-forge ffmpeg) to convert."
                        )
                        continue

                try:
                    target_storage = mtp._pick_storage_for_file(src_file.stat().st_size)
                except mtp.StorageFullError as e:
                    log.warning(f"  [Salvage] Storage full - stopping salvage: {e}")
                    return transferred

                if artist and album:
                    folder_name, is_junk = dev_lib.resolve_folder(artist=artist, album=album)
                    if folder_name and not is_junk:
                        dest_folder = _safe(folder_name, 100)
                    elif artist and album:
                        dest_folder = _safe(f"{artist} - {album}", 100)
                    else:
                        dest_folder = _safe(stem[:60], 100)
                else:
                    dest_folder = _safe(stem[:60], 100)

                dest_parts = [config.MUSIC_ROOT, dest_folder]

                # Convert webm/opus to mp3 if ffmpeg available
                if src_file.suffix.lower() in (".webm", ".opus"):
                    ffmpeg_dir = _find_ffmpeg()
                    if ffmpeg_dir:
                        ffmpeg_bin = str(pathlib.Path(ffmpeg_dir) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))
                        mp3_path = src_file.with_suffix(".mp3")
                        result = subprocess.run(
                            [ffmpeg_bin, "-y", "-i", str(src_file),
                             "-codec:a", "libmp3lame", "-qscale:a", "0", str(mp3_path)],
                            capture_output=True, check=False,
                        )
                        if result.returncode == 0 and mp3_path.exists():
                            log.info(f"  [Salvage] Converted {src_file.name} -> {mp3_path.name}")
                            src_file.unlink(missing_ok=True)  # mk37: staging
                            src_file = mp3_path
                            stem = mp3_path.stem
                        else:
                            log.warning(f"  [Salvage] ffmpeg conversion failed for {src_file.name} - skipping")
                            continue
                    else:
                        log.warning(f"  [Salvage] Skipping {src_file.name} - ffmpeg not found")
                        continue

                safe_name = _ascii_safe(stem) + src_file.suffix.lower()
                renamed = src_file.parent / safe_name
                if src_file.name != safe_name:
                    try:
                        src_file.rename(renamed)
                        src_file = renamed
                    except Exception:
                        pass

                # Transfer with retries
                transfer_ok = False
                for attempt in range(MTP_TRANSFER_RETRIES):
                    try:
                        log.info(f"  [Salvage] Transferring '{src_file.name}' -> {target_storage}/{'/'.join(dest_parts)}/")
                        # MK37/S2: quorum-verified transfer (board P0-2)
                        try:
                            from mk37_wiring import transfer_via_legacy_with_quorum
                            _device_relpath = "/".join(dest_parts + [src_file.name])
                            _result = transfer_via_legacy_with_quorum(
                                mtp.copy_file_to_device,
                                src=src_file,
                                dest_parts=dest_parts,
                                device_name=config.DEVICE_NAME,
                                storage_name=target_storage,
                                device_index=_xfer_get_device_index(),
                                source_local_path=str(src_file),
                                device_relpath=_device_relpath,
                            )
                            if not _result.get("completed"):
                                raise RuntimeError(
                                    f"MK37/S2 quorum failed: {_result.get('reason')}"
                                )
                        except ImportError:
                            mtp.copy_file_to_device(
                                src=src_file,
                                dest_parts=dest_parts,
                                device_name=config.DEVICE_NAME,
                                storage_name=target_storage,
                                device_index=_xfer_get_device_index(),  # REV 14.9 (C2)
                            )
                        transfer_ok = True
                        _xfer_bump("ok")  # REV 14.9 (D3)
                        break
                    except Exception as e:
                        log.warning(f"  [Salvage] Attempt {attempt+1} failed: {e}")
                        _xfer_first_failure_snapshot()  # REV 14.9 (D2)
                        if attempt < MTP_TRANSFER_RETRIES - 1:
                            time.sleep(5 * (2 ** attempt))
                        else:
                            _xfer_bump("fail")  # REV 14.9 (D3)
                            _SALVAGE_FAIL_CACHE[file_key] = _SALVAGE_FAIL_CACHE.get(file_key, 0) + 1
                            log.warning(f"  [Salvage] File {src_file.name} failed after {MTP_TRANSFER_RETRIES} attempts. Will skip.")
                            try:
                                src_file.unlink()  # mk37: staging
                                log.info(f"  [Salvage] Removed failing file: {src_file.name}")
                            except Exception:
                                pass
                            raise

                if not transfer_ok:
                    continue

                time.sleep(2.0)

                transferred += 1
                all_skipped = False
                log.info(f"  [Salvage] OK: '{title}'")

                _SALVAGE_FAIL_CACHE.pop(file_key, None)

                # Update dev_lib in-place so subsequent salvage iterations
                # don't re-transfer the same file.  Use the public API if
                # available; fall back silently if the version doesn't support it.
                try:
                    if artist and title and hasattr(dev_lib, "add_track"):
                        dev_lib.add_track(
                            artist=artist,
                            album=album if album else artist,
                            title=_strip_feat(title),
                            storage=target_storage,
                            parts=dest_parts + [src_file.name],
                        )
                    elif artist and title and hasattr(dev_lib, "_index"):
                        # Older DeviceLibrary: update the internal index dict directly
                        def _norm_sal(s):
                            return re.sub(r"\s+", " ",
                                re.sub(r"[^a-z0-9 ]", "", s.lower().strip())).strip()
                        _a_n = _norm_sal(artist)
                        _t_n = _norm_sal(_strip_feat(title))
                        dev_lib._index[f"{_a_n}|{_t_n}"] = {
                            "device_parts":   dest_parts + [src_file.name],
                            "device_storage": target_storage,
                        }
                except Exception as _li:
                    log.debug(f"  [Salvage] dev_lib update skipped: {_li}")

            except mtp.StorageFullError:
                return transferred
            except Exception as e:
                log.warning(f"  [Salvage] Failed '{src_file.name}': {e}")
                _SALVAGE_FAIL_CACHE[file_key] = _SALVAGE_FAIL_CACHE.get(file_key, 0) + 1
                try:
                    src_file.unlink()
                except Exception:
                    pass

        if all_skipped:
            try:
                _force_rmtree(stale_dir)
                log.info(f"  [Salvage] Cleaned up {stale_dir.name} (all files already on device)")
            except Exception as e:
                log.debug(f"  [Salvage] Could not remove {stale_dir.name}: {e}")

    return transferred


def _salvage_stale_downloads(stale_dirs: list) -> int:
    try:
        lib = _build_device_index(config.DEVICE_NAME, config.STORAGE_ROOTS)
        time.sleep(3.0)
    except Exception as e:
        log.warning(f"  [Salvage] Could not scan device: {e} - skipping salvage")
        return 0
    return _salvage_stale_downloads_with_lib(stale_dirs, lib)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_downloads(
    track_map:       dict,
    cancelled:       list,
    liked_album_ids: set[str] = None,
    sp_client=None,
    log_callback=None,
) -> dict:
    """
    Stage 0 entry point — called from walkman_sync.run_sync().

    liked_album_ids should come from spotify_client.fetch_and_build() which
    now returns a 3-tuple: (track_map, liked_track_ids, liked_album_ids).
    Pass the third element here.  If omitted or None, the liked-album priority
    path silently falls back to the missing-count threshold only.

    Progress is emitted to log at INFO level every CACHE_FLUSH_INTERVAL albums
    and to log_callback (GUI) if provided.
    """
    # REV 14.9 (D3) — transport-outcome counters for the progress log
    stats = {
        "downloaded": 0, "failed": 0, "albums": 0,
        "transfer_ok": 0, "transfer_fail": 0, "transfer_skip": 0,
    }
    liked_album_ids = liked_album_ids or set()

    # MK17 Patch 5: reset run-level health counters
    _health_reset()

    # MK17 Patch 2: reset router degraded-mode state for this run
    try:
        import adb_transfer as _adb
        if hasattr(_adb, "_reset_both_unknown_state"):
            _adb._reset_both_unknown_state()
    except Exception:
        pass

    temp_root = config.DOWNLOAD_DIR / ".walkman_tmp"
    temp_root.mkdir(parents=True, exist_ok=True)

    # FIX B1: fail fast if PC is already low on disk before any downloads start.
    # _get_pc_free_gb() uses shutil.disk_usage on config.DOWNLOAD_DIR.
    _pc_min_free = getattr(config, "REBUILD_PC_MIN_FREE_GB", 10.0)
    _pc_free_now = _get_pc_free_gb()
    if _pc_free_now < _pc_min_free:
        log.error(
            f"Download stage: PC has only {_pc_free_now:.1f} GB free on "
            f"{str(config.DOWNLOAD_DIR)[:3]} — need {_pc_min_free} GB minimum. "
            f"Aborting to avoid filling the drive. Free up space and retry."
        )
        return {"downloaded": 0, "failed": 0, "albums": 0}

    # Pre‑sweep: delete empty, non-audio, and very old stale dirs
    audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".aac"}
    pre_stale = list(temp_root.glob("walkman_dl_*"))
    for d in pre_stale:
        if not d.is_dir():
            continue
        all_files = [f for f in d.rglob("*") if f.is_file()]
        audio_files = [f for f in all_files if f.suffix.lower() in audio_exts]
        if not audio_files:
            _force_rmtree(d)
            log.debug(f"  [Pre-sweep] Removed empty/non-audio stale dir: {d.name}")
            continue
        dir_age_hours = (time.time() - d.stat().st_mtime) / 3600
        if dir_age_hours > 24:
            _force_rmtree(d)
            log.info(f"  [Pre-sweep] Removed stale dir older than 24h: {d.name} (age={dir_age_hours:.1f}h)")

    # Recover + sweep stale temp dirs
    stale = list(temp_root.glob("walkman_dl_*"))
    if stale:
        log.info(f"Download stage: found {len(stale)} stale temp dir(s) from prior run(s) — will salvage after device scan.")

    log.info("Download stage: indexing local library ...")
    local_index = _build_local_index(config.LOCAL_FOLDERS)
    log.info(f"Download stage: {len(local_index)} valid local track(s) indexed.")

    log.info("Download stage: scanning Walkman for existing tracks ...")
    dev_lib = _build_device_index(config.DEVICE_NAME, config.STORAGE_ROOTS)

    # Salvage after scan
    if stale:
        log.info(f"  [Salvage] Settling 3s after scan before writing to device...")
        time.sleep(3.0)
        salvaged = _salvage_stale_downloads_with_lib(stale, dev_lib)
        if salvaged:
            log.info(f"  [Salvage] {salvaged} file(s) transferred from stale temp dirs.")
        for d in stale:
            _force_rmtree(d)
            if d.exists():
                log.debug(f"  [Cleanup] Could not remove (still locked): {d.name}")

    missing_albums = _collect_missing_albums(
        track_map, local_index, dev_lib,
        liked_album_ids,
        sp_client,  # may be None in OFFLINE_MODE; _collect_missing_albums no longer uses it
    )
    if not missing_albums:
        log.info("Download stage: all tracks present - nothing to download.")
        shutil.rmtree(temp_root, ignore_errors=True)  # mk37: staging
        return stats

    stats["albums"] = len(missing_albums)

    # I2: load TIDAL album search cache so _download_full_album skips network
    # round-trips for albums already resolved in previous runs.
    _load_tidal_album_cache()

    enabled = getattr(config, "DOWNLOAD_PROVIDERS", {})
    active = [p for p in _PROVIDERS if enabled.get(p, True)]

    # REV 14.6 — banner shows per-provider adaptive timeouts so the user
    # can see at a glance what budget each provider gets.  Format is
    # compact: "tidaldl=30  tidaldl_url=45  qqdl=30 ..."
    timeout_brief = "  ".join(f"{p}={PROVIDER_TIMEOUTS.get(p, PROVIDER_TIMEOUT_S)}s"
                              for p in active)
    log.info(
        f"Download stage: {stats['albums']} album(s) with missing tracks  "
        f"({DOWNLOAD_WORKERS} workers, adaptive per-provider timeouts)  "
        f"providers: {active}"
    )
    log.info(f"  [Timeouts] {timeout_brief}")
    if _CIRCUIT_ENABLED:
        log.info(
            f"  [CircuitBreaker] enabled "
            f"(disable provider after {_CIRCUIT_THRESHOLD} consecutive failures "
            f"with 0 successes)"
        )

    dl_cache = _load_download_cache()
    cache_lock = threading.Lock()

    # TIDAL pre-authentication
    if getattr(config, "DOWNLOAD_PROVIDERS", {}).get("tidaldl", True):
        client_id = getattr(config, "TIDAL_CLIENT_ID", "")
        client_secret = getattr(config, "TIDAL_CLIENT_SECRET", "")
        if client_id and client_secret:
            log.info("\n" + "="*60 + "\n  TIDAL PRE-AUTHENTICATION\n  Checking TIDAL session before downloads start...\n" + "="*60)
            try:
                _get_tidal_session()
                log.info("  [TIDAL] Session OK - proceeding with downloads.")
            except Exception as e:
                global _tidal_session_cache
                _tidal_session_cache = "failed"
                log.warning(f"  [TIDAL] Pre-auth failed: {e}\n  TIDAL tracks will be skipped.")
        else:
            log.debug("  [TIDAL] Skipping pre-auth - credentials not configured.")

    mtp.log_health_check("Pre-download-transfer")

    # Bug 1 fix: reset the session-level internal-full flag at the start of
    # each download run so stale state from a previous run doesn't mis-route.
    mtp.reset_storage_cache()

    # Bug 5 fix: reset the SD free-space cache so it re-queries at the start
    # of each run rather than carrying over a stale reading from a prior session.
    global _sd_free_cache
    _sd_free_cache["gb"] = None
    _sd_free_cache["albums_since_check"] = 0

    # I1: Determine strategy for each album NOW (before submitting to pool)
    # and sort per-track albums first.
    #
    # Why this matters: With 8 concurrent workers and all 1490 albums submitted
    # simultaneously, as_completed() returns in finish order.  Album-wise albums
    # (each ~10–20 tracks) take far longer than per-track albums (~1 track each).
    # When album-wise albums happen to run first, the ETA projection sees only
    # slow completions and wildly overestimates remaining time.  Sorting per-track
    # first lets the fast completions dominate early, giving an accurate ETA from
    # the start and letting the overnight-friendly album-wise work finish last.
    #
    # Pre-classifying here also centralises the strategy logic so
    # _download_and_transfer_album doesn't need to re-evaluate SD free space
    # for every album — one SD free space query here is sufficient for the
    # initial sort; the per-album function still checks mid-run in case space
    # drops below the threshold.
    sd_free_gb_initial = _get_sd_free_gb_cached()

    def _initial_strategy(info: dict) -> str:
        if DOWNLOAD_STRATEGY == "album":
            return "album"
        if DOWNLOAD_STRATEGY == "track":
            return "track"
        if sd_free_gb_initial >= ALBUM_DOWNLOAD_MIN_FREE_SPACE_GB:
            if PRIORITIZE_LIKED_ALBUMS and info.get("is_liked"):
                return "album"
            missing = info.get("missing_count", 0)
            total   = info.get("total_tracks", 0)
            estimated = info.get("_tracks_estimated", False)
            if total and missing >= ALBUM_DOWNLOAD_THRESHOLD_MISSING:
                if not estimated or total >= ALBUM_DOWNLOAD_THRESHOLD_MISSING:
                    return "album"
        return "track"

    # Annotate and sort: per-track albums first, album-wise last
    album_items = list(missing_albums.items())
    album_items.sort(key=lambda kv: 0 if _initial_strategy(kv[1]) == "track" else 1)

    # ── Streaming-history secondary sort ─────────────────────────────────────
    # Within each strategy group, surface albums/tracks the user actually plays
    # most (by priority_score from streaming history) to the top of the queue.
    # Albums with no history data get score=0 and float to the bottom naturally.
    try:
        import streaming_history as _sh
        _sh_scores = _sh.load()
        if _sh_scores:
            def _album_sh_score(info: dict) -> float:
                """Max priority_score across all missing tracks in this album."""
                best = 0.0
                for t in info.get("missing_tracks", []):
                    uri = t.get("uri") or t.get("spotify_uri") or ""
                    if not uri and t.get("id"):
                        uri = f"spotify:track:{t['id']}"
                    s = (_sh_scores.get(uri) or {}).get("priority_score", 0.0)
                    if s > best:
                        best = s
                return best

            # Stable secondary sort within the two strategy buckets
            album_items.sort(key=lambda kv: (
                0 if _initial_strategy(kv[1]) == "track" else 1,  # strategy group (primary)
                -_album_sh_score(kv[1]),                           # history score (secondary, desc)
            ))
            log.info(
                f"Download stage: streaming-history priority applied "
                f"({len(_sh_scores):,} track scores loaded)"
            )
    except Exception as _sh_err:
        log.debug(f"  [StreamingHistory] Priority sort skipped: {_sh_err}")

    n_track  = sum(1 for _, v in album_items if _initial_strategy(v) == "track")
    n_album  = len(album_items) - n_track
    log.info(
        f"Download stage: strategy split — {n_track} per-track, {n_album} album-wise "
        f"(per-track submitted first for accurate ETA)"
    )

    albums_processed   = 0
    track_completed    = 0
    album_completed    = 0
    track_elapsed_sum  = 0.0
    album_elapsed_sum  = 0.0
    mtp_lock           = threading.Lock()
    _run_start         = time.time()

    # Bug 4: flush the download cache periodically so a cancel/crash mid-run
    # doesn't force a full re-download of everything already transferred.
    CACHE_FLUSH_INTERVAL = 10   # flush every N albums completed

    try:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            futures = {}

            # REV 14.5: wrap the album download call so every worker pauses
            # if the handle-count monitor has engaged its throttle.  Gives
            # Windows a chance to reclaim GDI/USER objects between subprocess
            # bursts rather than wedging on a Tk_GetPixmap failure mid-run.
            try:
                import resource_monitor as _rm
                _rm_available = _rm.is_available()
            except Exception:
                _rm = None
                _rm_available = False

            def _gated_download(info_arg):
                if _rm_available:
                    _rm.wait_if_throttled(
                        timeout=getattr(config, "RESOURCE_WAIT_TIMEOUT_S", 60.0)
                    )
                return _download_and_transfer_album(
                    info_arg, temp_root, cancelled, dl_cache, cache_lock,
                    mtp_lock, dev_lib, sp_client,
                )

            for name, info in album_items:
                # REV 14.5: hard abort check — stop scheduling new albums
                # once the monitor says resources have crossed the ceiling.
                if _rm_available and _rm.should_abort():
                    log.error(
                        "[Download] Resource monitor aborted — not scheduling "
                        "further albums.  Restart Windows to reclaim leaked "
                        "handles before re-running."
                    )
                    cancelled[0] = True
                    break
                strategy_hint = _initial_strategy(info)
                f = pool.submit(_gated_download, info)
                futures[f] = (name, strategy_hint)

            for future in as_completed(futures):
                if cancelled[0]:
                    break
                albums_processed += 1
                _name, _strategy = futures[future]

                t_now = time.time()

                # Periodic cache flush + split ETA progress report (B5/I1)
                if albums_processed % CACHE_FLUSH_INTERVAL == 0:
                    with cache_lock:
                        _save_download_cache(dl_cache)
                    # I2: flush TIDAL album cache too
                    with _tidal_album_cache_lock:
                        if _tidal_album_cache_dirty:
                            _save_tidal_album_cache()

                    elapsed_s = t_now - _run_start
                    frac = albums_processed / max(stats["albums"], 1)
                    # B5: ETA uses global fraction (pool is mixed, not strictly ordered)
                    eta_s  = int((elapsed_s / frac) - elapsed_s) if frac > 0 else 0

                    # REV 14.9 (D3) — Drain module-level transfer counters
                    # into stats so both the Progress line and the final
                    # summary see the same numbers.
                    _d_ok, _d_fail, _d_skip = _xfer_drain()
                    stats["transfer_ok"]   += _d_ok
                    stats["transfer_fail"] += _d_fail
                    stats["transfer_skip"] += _d_skip

                    # REV 14.9 (D1) — surface transport counters inline so
                    # "0 transfers succeeding" is visible within minutes
                    # instead of hidden under the album-level progress.
                    tok   = stats["transfer_ok"]
                    tfail = stats["transfer_fail"]
                    tskip = stats["transfer_skip"]
                    progress_msg = (
                        f"  [Progress] {albums_processed}/{stats['albums']} albums "
                        f"({track_completed} per-track, {album_completed} album-wise done) — "
                        f"transfers ok:{tok} fail:{tfail} skip:{tskip} — "
                        f"elapsed {int(elapsed_s//3600)}h{int((elapsed_s%3600)//60)}m, "
                        f"est. remaining {eta_s//3600}h{(eta_s%3600)//60}m"
                    )
                    log.info(progress_msg)
                    if log_callback:
                        try:
                            log_callback(progress_msg)
                        except Exception:
                            pass

                    # REV 14.9 (D3) — Transport summary with average time
                    if tok + tfail + tskip > 0:
                        avg_s = (elapsed_s / max(tok + tfail, 1))
                        log.info(
                            f"  [Transport] cumulative: ok:{tok} fail:{tfail} "
                            f"skip:{tskip}  (avg {avg_s:.1f}s per completed transfer)"
                        )

                if albums_processed % HEALTH_CHECK_INTERVAL == 0:
                    healthy = mtp.log_health_check(f"Health @ {albums_processed}/{stats['albums']} albums")
                    if not healthy:
                        log.error("Download stage: device unhealthy - pausing 30s for reconnect ...")
                        time.sleep(30)
                        mtp.log_health_check("Post-pause health check")

                try:
                    dl, fail = future.result()
                    stats["downloaded"] += dl
                    stats["failed"]     += fail
                    if _strategy == "track":
                        track_completed += 1
                    else:
                        album_completed += 1

                    # 2026-09-03 (user direction) — sweep local download staging
                    # every N album-wise albums OR every N per-track songs
                    # (counted separately, no combined trigger), plus whenever
                    # the PC drive runs low. Everything counted here is already
                    # transferred to the Walkman.
                    _sw_alb = int(getattr(config, "DOWNLOAD_TEMP_SWEEP_EVERY_ALBUMS", 5))
                    _sw_trk = int(getattr(config, "DOWNLOAD_TEMP_SWEEP_EVERY_TRACKS", 100))
                    _sw_low = float(getattr(config, "DOWNLOAD_TEMP_LOW_FREE_GB", 12.0))
                    _do_sweep = _sweep_reason = None
                    if _strategy != "track" and _sw_alb > 0 and album_completed % _sw_alb == 0:
                        _do_sweep, _sweep_reason = True, f"{album_completed} albums"
                    elif _strategy == "track" and _sw_trk > 0 and track_completed % _sw_trk == 0:
                        _do_sweep, _sweep_reason = True, f"{track_completed} tracks"
                    if _do_sweep:
                        try:
                            import temp_sweep as _ts
                            _ts.sweep_stale_staging(_sweep_reason, log_callback=log.info,
                                                    extra_roots=(str(temp_root),))
                        except Exception as _swe:
                            log.debug(f"  [TempSweep] skipped ({_swe})")
                    elif _sw_low > 0:
                        try:
                            import temp_sweep as _ts
                            if _ts.pc_free_gb() < _sw_low:
                                _ts.sweep_stale_staging("low disk", log_callback=log.info,
                                                        extra_roots=(str(temp_root),))
                        except Exception:
                            pass
                except mtp.StorageFullError as e:
                    log.error(f"Download stage stopped - storage full: {e}")
                    cancelled[0] = True
                    break
                except Exception as e:
                    log.error(f"Download stage album error: {e}")
                    stats["failed"] += 1

    finally:
        with cache_lock:
            _save_download_cache(dl_cache)
        # I2: persist TIDAL album search cache so next run skips network round-trips
        with _tidal_album_cache_lock:
            if _tidal_album_cache_dirty:
                _save_tidal_album_cache()
        _force_rmtree(temp_root)

        log.info("Download stage: sweeping empty device folders ...")
        for storage_name, subfolder in config.STORAGE_ROOTS:
            try:
                deleted = mtp.delete_empty_device_folders(
                    config.DEVICE_NAME, storage_name, [subfolder]
                )
                if deleted:
                    log.info(f"  Removed {deleted} empty folder(s) from {storage_name}/{subfolder}")
            except Exception as e:
                log.debug(f"  Empty folder sweep error ({storage_name}): {e}")

        # REV 14.6 — log circuit-breaker summary so the user can see which
        # providers actually succeeded, and which got disabled mid-run.
        if _CIRCUIT_ENABLED:
            log.info(f"  [CircuitBreaker] Stage 0 summary: {_circuit_summary()}")

        # REV 14.9 (D3) — final drain + transport summary
        _d_ok, _d_fail, _d_skip = _xfer_drain()
        stats["transfer_ok"]   += _d_ok
        stats["transfer_fail"] += _d_fail
        stats["transfer_skip"] += _d_skip
        log.info(
            f"  [Transport] Final: {stats['transfer_ok']} ok, "
            f"{stats['transfer_fail']} fail, {stats['transfer_skip']} skip"
        )

    return stats


# ---------------------------------------------------------------------------
# PC free-space guard (for batch rebuild staging area)
# ---------------------------------------------------------------------------

def _get_pc_free_gb(path: pathlib.Path | None = None) -> float:
    """
    Return free space in GiB on the drive containing *path*
    (defaults to config.DOWNLOAD_DIR).  Returns 0.0 on error.
    """
    check_path = path or config.DOWNLOAD_DIR
    try:
        return shutil.disk_usage(check_path).free / (1024 ** 3)
    except Exception as e:
        log.debug(f"  [PCFree] Could not query disk usage for {check_path}: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# Artist-normalisation helper (used only by run_batch_rebuild)
# ---------------------------------------------------------------------------

def _norm_artist(name: str) -> str:
    """
    Lowercase + strip punctuation for fuzzy artist matching.
    'OutKast' -> 'outkast',  'Jay-Z' -> 'jayz',  'Mos Def' -> 'mos def'
    """
    import unicodedata as _ucd
    name = _ucd.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


# ---------------------------------------------------------------------------
# Batch TIDAL library rebuild
# ---------------------------------------------------------------------------

def run_batch_rebuild(
    artist_names:    list[str],
    letter_prefixes: list[str],
    liked_track_ids: set[str],
    cancelled:       list[bool],
    sp_client=None,
    log_callback=None,
) -> dict:
    """
    Replace a subset of the device library with fresh TIDAL downloads.

    Selection criteria (union of both lists — either or both may be used):
      artist_names    -- exact artist names, matched case-insensitively after
                         punctuation normalisation.
      letter_prefixes -- first letter(s) of normalised artist name.
                         e.g. ["O"] matches OutKast, Omar Apollo, Frank Ocean.

    Workflow per matched album
    --------------------------
    1. Scan device; group files by album folder.
    2. For each folder whose album_artist matches the selection:
       a. PC free-space guard: skip album if < REBUILD_PC_MIN_FREE_GB.
       b. Download full album from TIDAL into a per-album temp folder.
       c. Stamp POPM=255 on any track whose Spotify ID is in liked_track_ids.
       d. Delete old device files for this album (silent PS delete).
          NOTE: _download_full_album already pushed the new files in step (b);
          the delete here removes the old copies left alongside them.
       e. Clean up temp folder immediately after push.
    3. Post-rebuild reorganise pass (rename-only, non-destructive) so any
       folder/file names left non-canonical are fixed without extra downloads.
    4. Sweep empty device folders created by the deletions.

    Space management
    ----------------
    Each album's temp folder is created, filled, pushed, and deleted before
    the next album starts.  Peak extra PC usage ~= size of the largest album.
    If PC free space drops below config.REBUILD_PC_MIN_FREE_GB, that album
    is skipped (not aborted) so the rest of the run continues.

    Returns {"rebuilt": int, "skipped": int, "failed": int, "tracks": int}.
    """
    stats = {"rebuilt": 0, "skipped": 0, "failed": 0, "tracks": 0}
    min_free_gb = getattr(config, "REBUILD_PC_MIN_FREE_GB", 10.0)

    # ── Normalise selection criteria ----------------------------------------
    norm_names   = {_norm_artist(a) for a in artist_names   if a.strip()}
    norm_letters = {l.strip().lower()[0] for l in letter_prefixes if l.strip()}

    if not norm_names and not norm_letters:
        log.warning("[Rebuild] No artists or letters specified — nothing to do.")
        return stats

    log.info(
        f"[Rebuild] Starting batch rebuild. "
        f"Artists: {sorted(norm_names) or '(none)'}  |  "
        f"Letters: {sorted(norm_letters) or '(none)'}"
    )

    # ── Scan device ---------------------------------------------------------
    log.info("[Rebuild] Scanning device library ...")
    try:
        device_files = mtp.scan_device_audio(config.DEVICE_NAME, config.STORAGE_ROOTS)
    except Exception as e:
        log.error(f"[Rebuild] Device scan failed: {e}")
        return stats

    if not device_files:
        log.info("[Rebuild] No audio files found on device.")
        return stats

    # Group by (storage, folder_parts)
    from collections import defaultdict as _dd
    folders: dict[tuple, list[dict]] = _dd(list)
    for ef in device_files:
        folders[(ef["storage"], tuple(ef["parts"]))].append(ef)

    log.info(f"[Rebuild] {len(device_files)} file(s) across {len(folders)} folder(s).")

    mtp_lock   = threading.Lock()
    cache_lock = threading.Lock()
    dl_cache:  dict = {}
    _load_tidal_album_cache()

    temp_root = config.DOWNLOAD_DIR / ".walkman_rebuild_tmp"
    temp_root.mkdir(parents=True, exist_ok=True)

    try:
        import wpd_transfer as _wpd
        _have_wpd = True
    except ImportError:
        _have_wpd = False

    for (storage, parts_tuple), files in folders.items():
        if cancelled[0]:
            log.info("[Rebuild] Cancelled by user.")
            break

        parts       = list(parts_tuple)
        folder_name = parts[-1] if parts else ""

        # ── Resolve album_artist / album from folder name or first filename --
        fp = [p.strip() for p in folder_name.split(" - ", 1)]
        if len(fp) == 2 and fp[0] and fp[1]:
            folder_artist, folder_album = fp
        else:
            folder_artist, folder_album = "", folder_name

        if not folder_artist:
            sp = [p.strip() for p in
                  pathlib.Path(files[0]["filename"]).stem.split(" - ")]
            if len(sp) >= 3:
                folder_artist = sp[0]
                folder_album  = folder_album or sp[1]

        norm_fa = _norm_artist(folder_artist)

        # ── Match against selection -----------------------------------------
        letter_match = bool(norm_letters and norm_fa and norm_fa[0] in norm_letters)
        name_match   = bool(norm_names   and norm_fa and norm_fa in norm_names)
        if not letter_match and not name_match:
            continue

        match_reason = "letter" if letter_match else "name"
        log.info(
            f"\n[Rebuild] ── {folder_artist} - {folder_album} "
            f"({match_reason} match, {len(files)} file(s)) ──"
        )

        # ── PC space guard --------------------------------------------------
        pc_free = _get_pc_free_gb()
        if pc_free < min_free_gb:
            log.warning(
                f"[Rebuild] SKIP '{folder_album}' — PC free {pc_free:.1f} GB "
                f"< {min_free_gb} GB minimum."
            )
            stats["skipped"] += 1
            continue

        # ── Build album_info ------------------------------------------------
        # missing_tracks=[] + missing_count=0 tells _download_full_album to
        # fetch the full TIDAL album (not just "missing" tracks).
        album_info = {
            "album_artist":    folder_artist,
            "album_name":      folder_album,
            "spotify_album_id": "",
            "existing_folder":  folder_name,
            "missing_tracks":   [],
            "missing_count":    0,
            "total_tracks":     0,
            "is_liked":         False,  # per-track POPM handled below
        }

        # ── Download fresh from TIDAL ---------------------------------------
        album_tmp = temp_root / re.sub(
            r"[^\w\-]", "_", f"{folder_artist}_{folder_album}"
        )[:80]
        album_tmp.mkdir(parents=True, exist_ok=True)

        log.info(
            f"[Rebuild] Downloading '{folder_artist} - {folder_album}' "
            f"(PC free: {pc_free:.1f} GB) ..."
        )
        try:
            transferred, failed_dl = _download_full_album(
                album_info = album_info,
                temp_root  = album_tmp,
                dl_cache   = dl_cache,
                cache_lock = cache_lock,
                mtp_lock   = mtp_lock,
                dev_lib    = _build_device_index(config.DEVICE_NAME, config.STORAGE_ROOTS),
                sp_client  = sp_client,  # may be None in OFFLINE_MODE
            )
        except Exception as e:
            log.error(f"[Rebuild] Download error for '{folder_album}': {e}")
            _force_rmtree(album_tmp)
            stats["failed"] += 1
            continue

        if transferred == 0:
            log.warning(
                f"[Rebuild] No tracks downloaded for '{folder_album}' — "
                f"TIDAL may not carry this album. Device copy left untouched."
            )
            _force_rmtree(album_tmp)
            stats["skipped"] += 1
            continue

        # ── Per-track liked POPM stamp --------------------------------------
        # _tag_file already stamps POPM when album_info["is_liked"] is True,
        # but we set that to False above to avoid false positives.  Instead we
        # check each downloaded file individually against liked_track_ids using
        # the TXXX:spotify_id frame that _tag_file embeds.
        if liked_track_ids:
            from mutagen.id3 import ID3 as _ID3, POPM as _POPM
            from mutagen.flac import FLAC as _FLAC_lk
            all_dl = list(album_tmp.rglob("*.flac")) + list(album_tmp.rglob("*.mp3"))
            for _f in all_dl:
                try:
                    _sp_id = ""
                    _is_mp3_f = _f.suffix.lower() == ".mp3"
                    if _is_mp3_f:
                        _id3f = _ID3(_f)
                        for _tx in _id3f.getall("TXXX"):
                            if (_tx.desc or "").lower() == "spotify_id":
                                _sp_id = (_tx.text or [""])[0]
                                break
                        if _sp_id and _sp_id in liked_track_ids:
                            if not _id3f.getall("POPM"):
                                _id3f.add(_POPM(email="Spotify Liked", rating=255, count=0))
                                # MK37/S4: lock-aware tag write
                                try:
                                    from mk37_wiring import tag_file_with_coherence
                                    with tag_file_with_coherence(_f):
                                        _idh.save_v23_id3(_id3f)
                                except ImportError:
                                    _idh.save_v23_id3(_id3f)
                                log.info(f"  [Rebuild/POPM] ★ {_f.name}")
                    else:
                        _fla = _FLAC_lk(_f)
                        # FLAC: spotify_id stored as a plain tag
                        _sp_id = (_fla.get("spotify_id") or [""])[0]
                        if _sp_id and _sp_id in liked_track_ids:
                            if not _fla.get("rating"):
                                _fla["rating"] = "255"
                                _fla.save()
                                log.info(f"  [Rebuild/POPM] ★ {_f.name}")
                except Exception as _pe:
                    log.debug(f"  [Rebuild/POPM] stamp error {_f.name}: {_pe}")

        # ── Delete old device files -----------------------------------------
        # The new files were already pushed by _download_full_album.
        # We now remove the old copies so the folder doesn't contain duplicates.
        log.info(
            f"[Rebuild] Removing {len(files)} old file(s) from device ..."
        )
        for ef in files:
            try:
                if _have_wpd:
                    _wpd.delete_file_ps(
                        config.DEVICE_NAME,
                        ef["storage"],
                        ef["parts"],
                        ef["filename"],
                    )
                else:
                    _sr   = mtp._get_storage_root(config.DEVICE_NAME, ef["storage"])
                    _fold = mtp._navigate(_sr, ef["parts"], create=False)
                    if _fold:
                        _fn_lc = ef["filename"].lower()
                        _st_lc = pathlib.Path(ef["filename"]).stem.lower()
                        for _item in list(_fold.Items()):
                            if (_item.Name or "").lower() in (_fn_lc, _st_lc):
                                _item.InvokeVerb("delete")
                                break
                log.debug(f"  [Rebuild] Deleted old: {ef['filename']}")
                time.sleep(0.3)
            except Exception as _de:
                log.warning(
                    f"  [Rebuild] Could not delete '{ef['filename']}': {_de}"
                )

        # 3 s grace for ZX500 DB to process the deletes before next album
        time.sleep(3.0)

        _force_rmtree(album_tmp)

        stats["tracks"]  += transferred
        stats["rebuilt"] += 1
        if failed_dl:
            stats["failed"] += failed_dl

        log.info(
            f"[Rebuild] [OK] '{folder_artist} - {folder_album}': "
            f"{transferred} track(s) pushed, {failed_dl} failed."
        )

    # ── Persist TIDAL album cache -------------------------------------------
    with _tidal_album_cache_lock:
        if _tidal_album_cache_dirty:
            _save_tidal_album_cache()

    # ── Post-rebuild reorganise pass ----------------------------------------
    # rename-only: fixes any folder/file names the rebuild left non-standard.
    # Safe to run even on partial rebuilds — it only renames, never deletes.
    if stats["rebuilt"] > 0 and not cancelled[0]:
        log.info("[Rebuild] Running post-rebuild reorganise pass ...")
        try:
            from walkman_sync import reorganize_device_library
            rstat = reorganize_device_library(
                log_callback   = log_callback,
                cancelled_flag = cancelled,
                dry_run        = False,
            )
            log.info(
                f"[Rebuild] Reorganise: "
                f"{rstat.get('folders_renamed', 0)} folder(s), "
                f"{rstat.get('files_renamed', 0)} file(s) renamed."
            )
        except Exception as _re:
            log.warning(f"[Rebuild] Reorganise pass error (non-fatal): {_re}")

    # ── Sweep empty folders -------------------------------------------------
    _force_rmtree(temp_root)
    log.info("[Rebuild] Sweeping empty device folders ...")
    for _sn, _sf in config.STORAGE_ROOTS:
        try:
            _nd = mtp.delete_empty_device_folders(config.DEVICE_NAME, _sn, [_sf])
            if _nd:
                log.info(f"  [Rebuild] Removed {_nd} empty folder(s) from {_sn}/{_sf}")
        except Exception as _efe:
            log.debug(f"  [Rebuild] Empty folder sweep error ({_sn}): {_efe}")

    log.info(
        f"\n[Rebuild] ══ Complete ══  "
        f"{stats['rebuilt']} album(s) rebuilt  |  "
        f"{stats['tracks']} track(s) pushed  |  "
        f"{stats['skipped']} skipped  |  "
        f"{stats['failed']} failed"
    )
    return stats