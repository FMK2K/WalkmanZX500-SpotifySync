"""
lyrics_fetch.py  —  Synced lyrics via LRCLIB (Task 8)
======================================================
Populates the Walkman's native 3-tier lyrics display (embedded tag -> sidecar
.lrc -> sidecar .txt; confirmed against the decompiled Lyrics.java in this
repo's walkman_HiRes(95)/ source) by fetching from LRCLIB, a free, unauthenticated
synced-lyrics API.

Mirrors cover_art.py's shape: pure, fail-soft functions that never raise and
degrade to None/False on any network, parse, or server-side failure — a
lyrics miss must never interrupt the primary sync loop.

LRCLIB already separates synced vs. plain-text lyrics in its response, so no
LRC-timestamp parsing happens here — the raw `syncedLyrics` block is written
to the .lrc sidecar verbatim; the device's own Lyrics.java parses it.

Public API
----------
    fetch_synced_lyrics(title, artist, album="", duration_s=0) -> LyricsResult | None
    write_lrc_sidecar(audio_path, lyrics) -> bool
    embed_plain_lyrics(path, lyrics, is_mp3) -> bool

Config
------
    config.LYRICS_ENABLED = False           (opt-in)
    config.LYRICS_REQUEST_TIMEOUT_S = 10
"""
from __future__ import annotations

import dataclasses
import logging
import pathlib
from typing import Optional

import config

log = logging.getLogger(__name__)

LRCLIB_ENDPOINT = "https://lrclib.net/api/get"


@dataclasses.dataclass
class LyricsResult:
    synced: str = ""   # raw LRC-format block ("" if LRCLIB has none)
    plain: str = ""     # plain-text lyrics ("" if LRCLIB has none)
    source: str = "lrclib"

    @property
    def has_any(self) -> bool:
        return bool(self.synced or self.plain)


def _cfg(attr, default=None):
    return getattr(config, attr, default)


def fetch_synced_lyrics(
    title: str,
    artist: str,
    album: str = "",
    duration_s: int = 0,
) -> Optional[LyricsResult]:
    """Query LRCLIB for lyrics. Never raises; returns None on any failure or miss.

    duration_s is optional but recommended — LRCLIB's documented +/-2s
    tolerance uses it to disambiguate remasters/radio edits/album pressings.
    """
    if not _cfg("LYRICS_ENABLED", False):
        return None
    if not title or not artist:
        return None

    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration_s:
        params["duration"] = int(duration_s)

    timeout = float(_cfg("LYRICS_REQUEST_TIMEOUT_S", 10))
    try:
        import requests
        resp = requests.get(
            LRCLIB_ENDPOINT,
            params=params,
            timeout=timeout,
            headers={"User-Agent": "WalkmanSync/1.0 (+LRCLIB lyrics client)"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as e:
        log.debug(f"  [Lyrics] LRCLIB request failed ({artist!r} - {title!r}): {e}")
        return None

    synced = str(data.get("syncedLyrics") or "").strip()
    plain = str(data.get("plainLyrics") or "").strip()
    if not synced and not plain:
        return None
    return LyricsResult(synced=synced, plain=plain, source="lrclib")


def has_lyrics(audio_path, is_mp3: bool) -> bool:
    """Return True if *audio_path* already has a lyrics sidecar or embedded tag.
    Mirrors audit.py's _has_cover_art() gate — avoids re-fetching on every run."""
    try:
        p = pathlib.Path(audio_path)
        if p.with_suffix(".lrc").exists() or p.with_suffix(".txt").exists():
            return True
        if is_mp3:
            from mutagen.id3 import ID3
            return bool(ID3(p).getall("USLT"))
        else:
            from mutagen.flac import FLAC
            audio = FLAC(p)
            return bool(audio.get("LYRICS") or audio.get("UNSYNCEDLYRICS"))
    except Exception:
        return False


def write_lrc_sidecar(audio_path, lyrics: LyricsResult) -> bool:
    """Write lyrics.synced as a .lrc sidecar next to audio_path, atomically.
    Returns True on success; never raises."""
    if not lyrics or not lyrics.synced:
        return False
    try:
        import staging_atomic
        lrc_path = pathlib.Path(audio_path).with_suffix(".lrc")
        with staging_atomic.atomic_write(lrc_path, mode="w", encoding="utf-8") as f:
            f.write(lyrics.synced)
        return True
    except Exception as e:
        log.debug(f"  [Lyrics] .lrc sidecar write failed ({audio_path}): {e}")
        return False


def embed_plain_lyrics(path, lyrics: LyricsResult, is_mp3: bool) -> bool:
    """Embed lyrics.plain as a tag: USLT (lang=eng) for MP3, LYRICS Vorbis
    comment for FLAC. Routes through the same mediated save calls as every
    other tag write in this codebase (id3_helpers.save_v23_id3 / audio.save()).
    Returns True on success; never raises."""
    if not lyrics or not lyrics.plain:
        return False
    try:
        if is_mp3:
            from mutagen.id3 import ID3, USLT
            from id3_helpers import save_v23_id3
            id3 = ID3(path)
            id3.delall("USLT")
            id3.add(USLT(encoding=3, lang="eng", desc="", text=lyrics.plain))
            save_v23_id3(id3)
        else:
            from mutagen.flac import FLAC
            audio = FLAC(path)
            audio["LYRICS"] = lyrics.plain
            audio.save()
        return True
    except Exception as e:
        log.debug(f"  [Lyrics] Tag embed failed ({path}): {e}")
        return False


__all__ = [
    "LyricsResult", "fetch_synced_lyrics", "has_lyrics",
    "write_lrc_sidecar", "embed_plain_lyrics",
]
