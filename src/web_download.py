"""
web_download.py  —  Post-sync web agent for tracks that beat all other providers
=================================================================================
Runs AFTER the main sync completes and reads missing_tracks.txt to attempt
downloading tracks that TIDAL, spotiflac, and yt-dlp all failed on.

Provider waterfall (no browser auth needed):
  1. cobalt.tools      — clean JSON API, no JS needed, handles Spotify links
  2. spotifydown.com   — REST endpoint keyed by Spotify track ID
  3. spotilab.com      — Playwright browser automation (JS-heavy, Cloudflare)
  4. yt-dlp + isrc     — retry yt-dlp with ISRC search and extended timeout

All downloaded files are:
  - Tagged via the existing _tag_file() machinery (mutagen)
  - Transferred to the Walkman via the existing MTP stack
  - Stamped with a MetaKey so subsequent runs skip them

Usage
-----
  Standalone (after sync):
      python web_download.py
      python web_download.py --missing missing_tracks.txt --limit 50

  As a library (called from walkman_sync post-Stage-8):
      from web_download import run_web_downloads
      run_web_downloads(missing_tracks_file=config.REPORT_FILE)

  As a downloader waterfall provider ("webdl"):
      # Already wired into downloader._PROVIDERS when this module is importable.
      # Enable in config.py:  DOWNLOAD_PROVIDERS = {"webdl": True, ...}

Dependencies
------------
  Required:    requests (pip install requests)
  Optional:    playwright (pip install playwright && playwright install chromium)
               Only needed for spotilab.com fallback.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import shutil
import tempfile
import time
import threading
import unicodedata
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config defaults  (overridden by config.py values at runtime)
# ---------------------------------------------------------------------------

_DEFAULT_OUTPUT_DIR    = pathlib.Path(os.path.expanduser("~/Downloads")) / "_webdl_staging"
_DEFAULT_TIMEOUT_S     = 60
_DEFAULT_MAX_RETRIES   = 2
_COBALT_API            = "https://cobalt.tools/api/json"
_SPOTIFYDOWN_API       = "https://spotifydown.com/api/download"
_SPOTILAB_URL          = "https://spotilab.com/"
_YT_ISRC_TIMEOUT_S    = 120

# Tracks that have permanently failed web download (per-run in-memory cache)
_WEB_FAIL_CACHE: dict[str, int] = {}
_WEB_FAIL_LOCK  = threading.Lock()
_MAX_WEB_FAILS  = 2   # give up after 2 failed web attempts per track


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()


def _strip_feat(title: str) -> str:
    return re.sub(
        r"\s*[\(\[\{](?:feat|ft|featuring)[^\)\]\}]*[\)\]\}]",
        "", title, flags=re.I,
    ).strip()


def _parse_missing_line(line: str) -> dict:
    """
    Parse one line from missing_tracks.txt into a dict with keys:
      artist, title, spotify_url, spotify_id

    Handles both formats written by the sync agent:
      Stage 0 downloader:  "Artist — Title  [https://open.spotify.com/track/ID]"
      Stage 6 sync:        "Title by Artist (Not found — download required)"
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return {}

    result: dict = {"artist": "", "title": "", "spotify_url": "", "spotify_id": ""}

    # Extract Spotify URL if present
    url_m = re.search(r"https://open\.spotify\.com/track/([A-Za-z0-9]+)", line)
    if url_m:
        result["spotify_id"]  = url_m.group(1)
        result["spotify_url"] = f"https://open.spotify.com/track/{url_m.group(1)}"
        # Remove the URL bracket from line for further parsing
        line = re.sub(r"\s*\[https://open\.spotify\.com/track/[^\]]+\]", "", line).strip()

    # Format A: "Artist — Title" (Stage 0 downloader, em-dash)
    if " — " in line:
        parts = line.split(" — ", 1)
        result["artist"] = parts[0].strip()
        result["title"]  = parts[1].strip()
        return result

    # Format B: "Title by Artist (reason)" (Stage 6 sync)
    by_m = re.match(r"^(.+?)\s+by\s+(.+?)(?:\s+\(.+\))?$", line, re.I)
    if by_m:
        result["title"]  = by_m.group(1).strip()
        result["artist"] = by_m.group(2).strip()
        return result

    # Fallback: treat whole line as title
    result["title"] = line
    return result


def _collect_audio(directory: pathlib.Path) -> list[pathlib.Path]:
    """Return all audio files found under directory, preferring FLAC > M4A > MP3."""
    exts = {".flac": 0, ".m4a": 1, ".aac": 1, ".mp3": 2, ".ogg": 3}
    files = [
        f for f in directory.rglob("*")
        if f.is_file() and f.suffix.lower() in exts
    ]
    files.sort(key=lambda f: (exts.get(f.suffix.lower(), 9), f.name))
    return files


def _is_valid_audio(path: pathlib.Path, min_bytes: int = 80_000) -> bool:
    """Quick sanity check — file exists, non-trivial size, parseable by mutagen."""
    if not path.exists() or path.stat().st_size < min_bytes:
        return False
    try:
        from mutagen import File as _MF
        info = _MF(path)
        return info is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Provider 1 — cobalt.tools  (clean JSON API, no browser needed)
# ---------------------------------------------------------------------------

def _try_cobalt(spotify_url: str, output_dir: str, timeout_s: int = _DEFAULT_TIMEOUT_S) -> bool:
    """
    POST to cobalt.tools/api/json with the Spotify track URL.
    cobalt resolves it to a stream URL and returns a direct download link.
    Audio-only, MP3 output.
    """
    if not spotify_url:
        return False
    try:
        import requests as _req
    except ImportError:
        log.debug("  [WebDL/cobalt] requests not installed — skipping")
        return False

    out = pathlib.Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    payload = {
        "url":         spotify_url,
        "vCodec":      "none",
        "aFormat":     "mp3",
        "isAudioOnly": True,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "WalkmanSync/1.0 (+https://github.com/user/walkman-sync)",
    }

    try:
        resp = _req.post(
            _COBALT_API, json=payload, headers=headers,
            timeout=15, allow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug(f"  [WebDL/cobalt] API call failed: {e}")
        return False

    status = data.get("status", "")
    if status == "error":
        log.debug(f"  [WebDL/cobalt] API error: {data.get('text','unknown')}")
        return False

    # cobalt returns either a direct URL or a tunnel URL
    dl_url = data.get("url") or data.get("audio")
    if not dl_url:
        log.debug(f"  [WebDL/cobalt] No download URL in response: {list(data.keys())}")
        return False

    # Determine filename from URL or Content-Disposition
    filename = re.search(r"/([^/?#]+\.(mp3|flac|m4a|aac|ogg))", dl_url, re.I)
    out_name  = filename.group(1) if filename else "cobalt_track.mp3"
    out_path  = out / out_name

    try:
        dl_resp = _req.get(dl_url, headers=headers, timeout=timeout_s, stream=True)
        dl_resp.raise_for_status()
        # Respect Content-Disposition filename if present
        cd = dl_resp.headers.get("Content-Disposition", "")
        cd_m = re.search(r'filename="?([^";\n]+)', cd)
        if cd_m:
            out_path = out / cd_m.group(1).strip()
        with open(out_path, "wb") as fh:
            for chunk in dl_resp.iter_content(chunk_size=256 * 1024):
                fh.write(chunk)
        if _is_valid_audio(out_path):
            log.info(f"  [WebDL/cobalt] Downloaded: {out_path.name}")
            return True
        out_path.unlink(missing_ok=True)
        log.debug("  [WebDL/cobalt] Downloaded file is not valid audio")
        return False
    except Exception as e:
        log.debug(f"  [WebDL/cobalt] Download stream failed: {e}")
        out_path.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------------------
# Provider 2 — spotifydown.com  (REST, keyed by Spotify track ID)
# ---------------------------------------------------------------------------

def _try_spotifydown(spotify_id: str, output_dir: str, timeout_s: int = _DEFAULT_TIMEOUT_S) -> bool:
    """
    GET spotifydown.com/api/download?id=<track_id>
    Returns a JSON with a CDN download URL.  No auth required.
    """
    if not spotify_id:
        return False
    try:
        import requests as _req
    except ImportError:
        return False

    out = pathlib.Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://spotifydown.com/",
        "Origin":     "https://spotifydown.com",
    }

    try:
        api_resp = _req.get(
            _SPOTIFYDOWN_API,
            params={"id": spotify_id},
            headers=headers,
            timeout=20,
        )
        api_resp.raise_for_status()
        data = api_resp.json()
    except Exception as e:
        log.debug(f"  [WebDL/spotifydown] API call failed: {e}")
        return False

    if not data.get("success"):
        log.debug(f"  [WebDL/spotifydown] API returned failure: {data.get('message','?')}")
        return False

    dl_url = data.get("link") or data.get("url")
    if not dl_url:
        log.debug("  [WebDL/spotifydown] No download link in response")
        return False

    track_name = data.get("title", spotify_id)
    # NOTE: hoisted out of the f-string so we don't need PEP 701 nested
    # quotes (Python 3.12+) — the codebase targets 3.10 onwards.
    _safe_track_name = re.sub(r'[\\/:*?"<>|]', '_', track_name)
    out_path = out / f"{_safe_track_name}.mp3"

    try:
        dl_resp = _req.get(dl_url, headers=headers, timeout=timeout_s, stream=True)
        dl_resp.raise_for_status()
        with open(out_path, "wb") as fh:
            for chunk in dl_resp.iter_content(chunk_size=256 * 1024):
                fh.write(chunk)
        if _is_valid_audio(out_path):
            log.info(f"  [WebDL/spotifydown] Downloaded: {out_path.name}")
            return True
        out_path.unlink(missing_ok=True)
        return False
    except Exception as e:
        log.debug(f"  [WebDL/spotifydown] Stream failed: {e}")
        out_path.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------------------
# Provider 3 — spotilab.com  (Playwright browser automation)
# ---------------------------------------------------------------------------

def _try_spotilab(spotify_url: str, output_dir: str, timeout_s: int = 90) -> bool:
    """
    Automates spotilab.com using Playwright (headless Chromium).
    Pastes the Spotify URL into the search box, clicks Download,
    intercepts the XHR that returns the CDN download link, and saves the file.

    Requires:  pip install playwright && playwright install chromium
    """
    if not spotify_url:
        return False
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.debug("  [WebDL/spotilab] playwright not installed — skipping")
        return False

    out = pathlib.Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    dl_path: list[pathlib.Path] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                accept_downloads=True,
            )
            page = ctx.new_page()

            # Intercept the download trigger
            def _on_download(download):
                tmp = out / download.suggested_filename
                download.save_as(tmp)
                dl_path.append(tmp)

            page.on("download", _on_download)

            try:
                page.goto(_SPOTILAB_URL, timeout=30_000, wait_until="domcontentloaded")
                # Find the input field and paste the Spotify URL
                page.wait_for_selector("input[type='text'], input[placeholder*='spotify'], input[placeholder*='link']",
                                       timeout=10_000)
                page.fill("input[type='text']", spotify_url)
                page.keyboard.press("Enter")

                # Wait for download button to appear and click it
                dl_btn = page.wait_for_selector(
                    "a[href*='download'], button:has-text('Download'), a:has-text('Download MP3')",
                    timeout=20_000,
                )
                with page.expect_download(timeout=timeout_s * 1000) as dl_info:
                    dl_btn.click()
                download = dl_info.value
                save_path = out / download.suggested_filename
                download.save_as(save_path)
                dl_path.append(save_path)

            except PWTimeout:
                log.debug("  [WebDL/spotilab] Timed out waiting for page elements")
            except Exception as e:
                log.debug(f"  [WebDL/spotilab] Browser error: {e}")
            finally:
                browser.close()

    except Exception as e:
        log.debug(f"  [WebDL/spotilab] Playwright launch failed: {e}")
        return False

    valid = [p for p in dl_path if p.exists() and _is_valid_audio(p)]
    if valid:
        log.info(f"  [WebDL/spotilab] Downloaded: {valid[0].name}")
        return True
    return False


# ---------------------------------------------------------------------------
# Provider 4 — yt-dlp retry with ISRC + extended timeout
# ---------------------------------------------------------------------------

def _try_webdl_ytdlp(artist: str, title: str, isrc: str,
                      output_dir: str, timeout_s: int = _YT_ISRC_TIMEOUT_S) -> bool:
    """
    Last-resort yt-dlp retry with:
    - ISRC search query (most precise YouTube match)
    - Extended timeout (2× normal)
    - Looser duration validation (±30s instead of ±15s)
    - Explicit ffmpeg location from WinGet glob
    """
    try:
        import yt_dlp
    except ImportError:
        return False

    try:
        from downloader import _find_ffmpeg
        ffmpeg_dir = _find_ffmpeg()
    except ImportError:
        import shutil as _sh
        ff = _sh.which("ffmpeg")
        ffmpeg_dir = str(pathlib.Path(ff).parent) if ff else None

    out = pathlib.Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Build the most specific search query available
    clean = _strip_feat(title)
    if isrc:
        queries = [
            f"ytsearch3:{isrc}",                              # ISRC = exact match
            f"ytsearch3:{artist} {clean} official audio",
            f"ytsearch3:{artist} {clean} lyrics",
        ]
    else:
        queries = [
            f"ytsearch3:{artist} {clean} official audio",
            f"ytsearch3:{artist} {clean} topic",
        ]

    ydl_opts: dict = {
        "outtmpl":      str(out / "%(title)s.%(ext)s"),
        "quiet":        True,
        "no_warnings":  True,
        "noplaylist":   True,
    }
    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key":            "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }]
    else:
        ydl_opts["format"] = (
            "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio[!ext=webm]"
        )

    def _run(q: str):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([q])

    for query in queries:
        result_event = threading.Event()
        error_box: list = []

        def _worker(q=query):
            try:
                _run(q)
            except Exception as e:
                error_box.append(str(e))
            finally:
                result_event.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        finished = result_event.wait(timeout=timeout_s)
        if not finished:
            log.debug(f"  [WebDL/ytdlp-retry] Timed out on query: {query[:60]}")
            continue

        files = _collect_audio(out)
        if files and _is_valid_audio(files[0]):
            log.info(f"  [WebDL/ytdlp-retry] Downloaded: {files[0].name}")
            return True

    return False


# ---------------------------------------------------------------------------
# Main web download waterfall
# ---------------------------------------------------------------------------

def _web_download_track(
    artist:      str,
    title:       str,
    spotify_url: str = "",
    spotify_id:  str = "",
    isrc:        str = "",
    output_dir:  str = "",
    timeout_s:   int = _DEFAULT_TIMEOUT_S,
) -> Optional[pathlib.Path]:
    """
    Try all web providers in order and return the downloaded file path, or None.

    Provider order:
      1. cobalt.tools       (needs spotify_url)
      2. spotifydown.com    (needs spotify_id)
      3. spotilab.com       (needs spotify_url, needs Playwright)
      4. yt-dlp ISRC retry  (needs artist+title, uses isrc if available)
    """
    track_key = spotify_id or f"{_norm(artist)}|{_norm(title)}"

    with _WEB_FAIL_LOCK:
        if _WEB_FAIL_CACHE.get(track_key, 0) >= _MAX_WEB_FAILS:
            log.debug(f"  [WebDL] Permanently skipping '{title}' (failed {_MAX_WEB_FAILS}× already)")
            return None

    tmp_root = pathlib.Path(output_dir or tempfile.mkdtemp(prefix="webdl_"))
    tmp_root.mkdir(parents=True, exist_ok=True)

    providers = [
        ("cobalt",       lambda d: _try_cobalt(spotify_url, d, timeout_s)),
        ("spotifydown",  lambda d: _try_spotifydown(spotify_id, d, timeout_s)),
        ("spotilab",     lambda d: _try_spotilab(spotify_url, d, timeout_s)),
        ("ytdlp-retry",  lambda d: _try_webdl_ytdlp(artist, title, isrc, d, _YT_ISRC_TIMEOUT_S)),
    ]

    for name, fn in providers:
        prov_dir = tmp_root / name
        prov_dir.mkdir(exist_ok=True)
        try:
            ok = fn(str(prov_dir))
        except Exception as e:
            log.debug(f"  [WebDL/{name}] Uncaught exception: {e}")
            ok = False

        if ok:
            files = _collect_audio(prov_dir)
            if files and _is_valid_audio(files[0]):
                return files[0]
            log.debug(f"  [WebDL/{name}] Claimed success but no valid file found")

        shutil.rmtree(prov_dir, ignore_errors=True)

    # All providers failed
    with _WEB_FAIL_LOCK:
        _WEB_FAIL_CACHE[track_key] = _WEB_FAIL_CACHE.get(track_key, 0) + 1

    log.warning(f"  [WebDL] All web providers failed for '{title}' by {artist}")
    return None


# ---------------------------------------------------------------------------
# Integration: waterfall provider entry point
# (called by downloader._download_with_waterfall when provider == "webdl")
# ---------------------------------------------------------------------------

def try_webdl_provider(
    track:      dict,
    output_dir: str,
    timeout_s:  int = _DEFAULT_TIMEOUT_S,
) -> bool:
    """
    Drop-in provider function matching the downloader waterfall interface.
    Returns True if at least one valid audio file is in output_dir afterwards.
    """
    artist      = track.get("artist", "")
    title       = track.get("title",  "")
    spotify_id  = track.get("id",     "")
    isrc        = track.get("isrc",   "")
    spotify_url = (
        f"https://open.spotify.com/track/{spotify_id}" if spotify_id else ""
    )

    result = _web_download_track(
        artist=artist, title=title,
        spotify_url=spotify_url, spotify_id=spotify_id,
        isrc=isrc, output_dir=output_dir, timeout_s=timeout_s,
    )
    return result is not None


# ---------------------------------------------------------------------------
# Standalone sweep: read missing_tracks.txt and process all entries
# ---------------------------------------------------------------------------

def run_web_downloads(
    missing_tracks_file: "pathlib.Path | str | None" = None,
    output_dir:          "pathlib.Path | str | None" = None,
    limit:               int = 0,
    transfer_to_device:  bool = True,
    cancelled:           "list[bool] | None" = None,
) -> dict:
    """
    Read missing_tracks.txt, attempt web download for each entry,
    tag the files, and (optionally) transfer them to the Walkman.

    Returns stats dict: {downloaded, transferred, failed, skipped}
    """
    try:
        import config as _cfg
        _report_file = missing_tracks_file or _cfg.REPORT_FILE
        _out_dir = pathlib.Path(output_dir or _DEFAULT_OUTPUT_DIR)
        _device  = _cfg.DEVICE_NAME
    except ImportError:
        _report_file = missing_tracks_file or pathlib.Path("missing_tracks.txt")
        _out_dir = pathlib.Path(output_dir or _DEFAULT_OUTPUT_DIR)
        _device  = "WALKMAN"

    _report_file = pathlib.Path(_report_file)
    _out_dir.mkdir(parents=True, exist_ok=True)

    stats = {"downloaded": 0, "transferred": 0, "failed": 0, "skipped": 0}

    if not _report_file.exists():
        log.info(f"[WebDL] Missing tracks file not found: {_report_file}")
        return stats

    raw_lines = _report_file.read_text(encoding="utf-8", errors="replace").splitlines()
    tracks = [_parse_missing_line(l) for l in raw_lines]
    tracks = [t for t in tracks if t.get("title") or t.get("artist")]

    if limit > 0:
        tracks = tracks[:limit]

    log.info(
        f"\n[WebDL] Starting web download sweep — {len(tracks)} track(s) "
        f"from {_report_file.name}"
    )
    log.info(f"[WebDL] Provider order: cobalt.tools → spotifydown → spotilab → yt-dlp-retry")
    log.info(f"[WebDL] Output staging: {_out_dir}")

    for i, track_info in enumerate(tracks, 1):
        if cancelled and cancelled[0]:
            log.info("[WebDL] Cancelled.")
            break

        artist      = track_info.get("artist", "")
        title       = track_info.get("title", "")
        spotify_url = track_info.get("spotify_url", "")
        spotify_id  = track_info.get("spotify_id", "")

        if not title:
            stats["skipped"] += 1
            continue

        log.info(f"[WebDL] [{i}/{len(tracks)}] '{title}' by {artist or '?'}")

        track_tmp = _out_dir / f"track_{i:04d}"
        track_tmp.mkdir(exist_ok=True)

        file_path = _web_download_track(
            artist=artist, title=title,
            spotify_url=spotify_url, spotify_id=spotify_id,
            output_dir=str(track_tmp),
        )

        if not file_path:
            stats["failed"] += 1
            log.warning(f"[WebDL]   ✗  All providers failed — '{title}' remains missing")
            continue

        stats["downloaded"] += 1
        log.info(f"[WebDL]   ✓  Downloaded: {file_path.name}")

        # Tag the file using downloader._tag_file if available
        try:
            from downloader import _tag_file as _dt
            # Build a minimal track dict for tagging
            tag_track = {
                "title":        title,
                "artist":       artist,
                "album":        title,         # best-effort: single
                "album_artist": artist,
                "track_number": "1",
                "disc_number":  "1",
                "release_year": "",
                "genre":        "",
                "id":           spotify_id,
            }
            file_path = _dt(file_path, tag_track)
            log.debug(f"[WebDL]   Tagged: {file_path.name}")
        except Exception as e:
            log.debug(f"[WebDL]   Tagging skipped ({e})")

        # Transfer to device
        if transfer_to_device:
            try:
                import mtp_transfer as _mtp
                import config as _cfg2
                import meta_key as _mk

                album_artist = re.sub(r"[\\/:*?\"<>|]", "_", artist or "Unknown")[:40]
                album        = re.sub(r"[\\/:*?\"<>|]", "_", title  or "Unknown")[:60]
                folder_name  = f"{album_artist} - {album}"
                dest_parts   = [_cfg2.MUSIC_ROOT, folder_name]

                target_st = _mtp._pick_storage_for_file(file_path.stat().st_size)
                _mtp.copy_file_to_device(
                    src=file_path,
                    dest_parts=dest_parts,
                    device_name=_device,
                    storage_name=target_st,
                )

                # Stamp MetaKey so next run skips this file
                dev_key = _mk.canonical_device_key(
                    target_st, dest_parts, file_path.name
                )
                exp_mk = _mk.compute_metakey(
                    file_path.stem,
                    genre="", year="", track_number="1", disc_number="1",
                )
                _mk.stamp(dev_key, exp_mk)

                stats["transferred"] += 1
                log.info(
                    f"[WebDL]   ✓  Transferred → {target_st}/Music/{folder_name}/"
                )
            except Exception as e:
                log.warning(f"[WebDL]   Transfer failed: {e}")
                stats["failed"] += 1
        else:
            log.info(f"[WebDL]   Saved locally: {file_path}")

        # Clean up staging
        try:
            shutil.rmtree(track_tmp, ignore_errors=True)
        except Exception:
            pass

        # Polite delay between requests (avoid rate limiting)
        time.sleep(1.5)

    log.info(
        f"\n[WebDL] Sweep complete — "
        f"downloaded: {stats['downloaded']}, "
        f"transferred: {stats['transferred']}, "
        f"failed: {stats['failed']}, "
        f"skipped: {stats['skipped']}"
    )
    return stats


# ---------------------------------------------------------------------------
# Waterfall hook — called by downloader.py when provider == "webdl"
# ---------------------------------------------------------------------------
# To wire this into the existing waterfall, add to downloader.py:
#
#   In _PROVIDERS list, append "webdl":
#     _PROVIDERS = [..., "ytdlp", "spotdl", "webdl"]
#
#   In _download_with_waterfall, add the webdl branch:
#     elif provider == "webdl":
#         try:
#             from web_download import try_webdl_provider
#             completed = try_webdl_provider(track, str(provider_tmp), timeout_s)
#         except ImportError:
#             completed = False
#
#   In _dep_available, add:
#     if provider == "webdl":
#         try: from web_download import try_webdl_provider; return True
#         except ImportError: return False
#
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    h = logging.StreamHandler()
    h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[h], force=True)


def main():
    _setup_logging()
    parser = argparse.ArgumentParser(
        description="Web download agent for tracks missing from Walkman sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python web_download.py
  python web_download.py --missing missing_tracks.txt --limit 50
  python web_download.py --no-transfer --output ~/Downloads/webdl_staging
  python web_download.py --missing missing_tracks.txt --limit 100 --timeout 90
        """,
    )
    parser.add_argument(
        "--missing", "-m",
        default=None,
        help="Path to missing_tracks.txt (default: config.REPORT_FILE)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Local staging directory for downloads",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int, default=0,
        help="Max tracks to attempt (0 = all)",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=int, default=_DEFAULT_TIMEOUT_S,
        help=f"Per-provider timeout in seconds (default: {_DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--no-transfer",
        action="store_true",
        help="Download only — do not transfer files to the Walkman",
    )
    args = parser.parse_args()

    stats = run_web_downloads(
        missing_tracks_file = args.missing,
        output_dir          = args.output,
        limit               = args.limit,
        transfer_to_device  = not args.no_transfer,
    )

    print(f"\n{'─'*50}")
    print(f"  Downloaded : {stats['downloaded']}")
    print(f"  Transferred: {stats['transferred']}")
    print(f"  Failed     : {stats['failed']}")
    print(f"  Skipped    : {stats['skipped']}")


if __name__ == "__main__":
    main()
