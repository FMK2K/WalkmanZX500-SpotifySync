"""
spotify_client.py  —  Spotify data layer
=========================================
Spotify is used EXCLUSIVELY for:
  - OAuth 2.0 authentication
  - Fetching owned playlist metadata and track lists
  - Fetching Liked Songs
  - Fetching saved (liked) albums (NEW)

Rate limit handling
-------------------
Spotify returns HTTP 429 with a Retry-After header (seconds to wait).
_sp_call() wraps every API call with:
  1. Honour the Retry-After header exactly
  2. Exponential backoff with jitter on repeated 429s
  3. Fall back to the structural cache (_metadata_cache.json) if rate
     limited for > MAX_WAIT_S — the sync continues on cached track data
     so downloads and transfers still run even when Spotify is throttling.

403 hardening
-------------
Non-owned playlists (Discover Weekly, Daily Mix, Radio) are filtered
BEFORE any track fetch by comparing playlist.owner.id to the current
user's ID.  This eliminates ~60-100 wasted API calls per run.
"""

import os
import json
import logging
import time
import random

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv

import config

load_dotenv()
log = logging.getLogger(__name__)

SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",        # for liked tracks and albums
    "user-library-read",        # for liked albums (same scope)
])

# If rate-limited for longer than this, give up live fetch and use cache
MAX_WAIT_S = 300   # 5 minutes max wait before falling back to cache


# ---------------------------------------------------------------------------
# Rate-limit-aware API call wrapper
# ---------------------------------------------------------------------------

def _sp_call(fn, *args, max_retries: int = 6, **kwargs):
    """
    Call a spotipy function with automatic retry on 429 rate limits.

    Respects the Retry-After header when present.
    Falls back to exponential backoff with jitter otherwise.
    Raises SpotifyException for non-429 errors after max_retries attempts.
    """
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except spotipy.exceptions.SpotifyException as exc:
            if exc.http_status == 429:
                # Spotify embeds the retry delay in the exception message
                # as "Retry will occur after: N s" or in headers
                retry_after = _parse_retry_after(exc)
                if retry_after and retry_after > MAX_WAIT_S:
                    log.warning(
                        f"  [Spotify] Rate limited for {retry_after}s "
                        f"(> {MAX_WAIT_S}s threshold) — will use cached data"
                    )
                    raise RateLimitExceeded(retry_after)

                wait = retry_after if retry_after else delay
                log.warning(
                    f"  [Spotify] Rate limited (429). "
                    f"Waiting {wait:.0f}s (attempt {attempt+1}/{max_retries})..."
                )
                time.sleep(wait + random.uniform(0, 1))
                delay = min(delay * 2, 60)  # exponential backoff, cap at 60s
            elif exc.http_status == 403:
                raise  # let callers handle 403
            else:
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                else:
                    raise
    return fn(*args, **kwargs)  # final attempt


def _parse_retry_after(exc: spotipy.exceptions.SpotifyException) -> int | None:
    """Extract Retry-After seconds from a 429 SpotifyException."""
    try:
        msg = str(exc)
        # spotipy embeds "Retry will occur after: N s" in the message
        import re
        m = re.search(r"Retry will occur after:\s*(\d+)", msg)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


class RateLimitExceeded(Exception):
    """Raised when Spotify rate limit exceeds MAX_WAIT_S threshold."""
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Spotify rate limit: retry after {retry_after}s")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_spotify_client() -> spotipy.Spotify:
    """
    Returns an authenticated Spotipy client.
    First run opens the browser for login; subsequent runs refresh silently.
    Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env.
    """
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=SCOPES,
        open_browser=True,
        cache_path=str(config.TOKEN_CACHE),
    ))


def get_current_user_id(sp: spotipy.Spotify) -> str:
    return _sp_call(sp.me)["id"]


# ---------------------------------------------------------------------------
# Saved albums (NEW)
# ---------------------------------------------------------------------------

def get_saved_albums(sp: spotipy.Spotify) -> set[str]:
    """
    Returns a set of album IDs that the user has saved (liked).
    Requires the user-library-read scope.
    """
    album_ids = set()
    results = _sp_call(sp.current_user_saved_albums, limit=50)
    while results:
        for item in results["items"]:
            album = item["album"]
            if album and album.get("id"):
                album_ids.add(album["id"])
        if results["next"]:
            results = _sp_call(sp.next, results)
        else:
            break
    log.info(f"Found {len(album_ids)} saved album(s).")
    return album_ids


# ---------------------------------------------------------------------------
# Playlist fetching
# ---------------------------------------------------------------------------

def get_all_playlists(sp: spotipy.Spotify) -> list[dict]:
    """
    Returns only playlists owned by the authenticated user.
    Spotify-curated playlists (Discover Weekly, Daily Mix, Radio) are
    filtered before any track fetch, preventing ~60-100 403 errors per run.
    """
    user_id  = get_current_user_id(sp)
    results  = []
    skipped  = 0
    response = _sp_call(sp.current_user_playlists, limit=50)

    while response:
        for pl in response["items"]:
            if pl and pl.get("owner", {}).get("id") == user_id:
                results.append(pl)
            else:
                skipped += 1
        response = _sp_call(sp.next, response) if response["next"] else None

    if skipped:
        log.info(
            f"Filtered {skipped} non-owned playlist(s) "
            f"(Discover Weekly, Daily Mix, Radio, etc.)"
        )
    return results


def get_playlist_tracks(sp: spotipy.Spotify, playlist_id: str) -> list[dict]:
    """
    Returns parsed track dicts from a playlist.
    Podcast episodes (track.id is None) are silently dropped.
    Returns [] if the playlist is inaccessible (403).
    """
    tracks = []
    try:
        response = _sp_call(sp.playlist_items,
            playlist_id,
            fields=(
                "next,items(track(id,name,track_number,disc_number,"
                "external_ids(isrc),"
                "artists(id,name),album(id,name,album_type,release_date,images)))"
            ),
            limit=100,
        )
        while response:
            for item in response["items"]:
                t = item.get("track")
                if t and t.get("id"):
                    tracks.append(_parse_track(t))
            response = _sp_call(sp.next, response) if response["next"] else None

    except spotipy.exceptions.SpotifyException as exc:
        if exc.http_status == 403:
            log.warning(f"Playlist {playlist_id} skipped (403 — permissions changed)")
        else:
            raise

    return tracks


def get_liked_songs(sp: spotipy.Spotify) -> list[dict]:
    """Returns parsed track dicts for all Liked Songs."""
    tracks   = []
    response = _sp_call(sp.current_user_saved_tracks, limit=50)
    while response:
        for item in response["items"]:
            t = item.get("track")
            if t and t.get("id"):
                tracks.append(_parse_track(t))
        response = _sp_call(sp.next, response) if response["next"] else None
    return tracks


def _parse_track(t: dict) -> dict:
    """
    Converts a raw Spotify track object into a clean dict.
    Includes ISRC (International Standard Recording Code) when available.
    ISRC is used as the primary TIDAL match key — it's a definitive identifier
    shared across Spotify, TIDAL, and MusicBrainz, bypassing all fuzzy matching.

    Rev 14.9.11 — album_artist fix (anti-splintering):
    Previously used track.artists[0] as album_artist.  On collaborative or
    featured tracks the first track-artist varies by song, causing the same
    album to appear under two different artist folders on the Walkman:
      "Travis Scott - Birds In The Trap…"  (most tracks)
      "Young Thug - Birds In The Trap…"    (tracks where Thug is listed first)
    The fix is to derive album_artist from album.artists — Spotify's album-level
    artist list — which is stable and consistent across every track on the album.
    For true various-artists compilations Spotify already sets album.artists to
    [{"name": "Various Artists", …}], so the old is_comp special-case is no
    longer needed; we rely on the field directly.
    """
    artist_names       = [a["name"] for a in t.get("artists", [])]
    album              = t.get("album", {})
    # album-level artists — stable across every track on the album
    album_artist_names = [a["name"] for a in album.get("artists", [])]
    raw_date           = album.get("release_date", "")
    is_comp            = album.get("album_type") == "compilation"
    isrc               = (t.get("external_ids") or {}).get("isrc", "")
    # Largest album image Spotify provides — used by audit as cover art fallback
    # when TIDAL is unavailable. images[] is sorted largest→smallest by Spotify.
    images             = album.get("images", [])
    album_image        = images[0]["url"] if images else ""

    # Resolve album_artist in priority order:
    #   1. album.artists[0]  — authoritative Spotify album-level field
    #   2. track.artists[0]  — fallback when album has no artists (rare edge case)
    #   3. "Unknown"         — absolute last resort
    _album_artist = (
        album_artist_names[0]
        if album_artist_names
        else (artist_names[0] if artist_names else "Unknown")
    )

    return {
        "id":           t["id"],
        "title":        t["name"],
        "artists":      artist_names,
        "artist":       artist_names[0] if artist_names else "Unknown",
        "album":        album.get("name", "Unknown"),
        "album_id":     album.get("id", ""),
        "album_artist": _album_artist,
        "spotify_year": raw_date[:4] if raw_date else "",
        "track_number": str(t.get("track_number", "1")),
        "disc_number":  str(t.get("disc_number",  "1")),
        "compilation":  is_comp,
        "genre":        "",
        "release_year": "",
        "is_liked":     False,
        "isrc":         isrc,
        "album_image":  album_image,
    }


# ---------------------------------------------------------------------------
# Cache fallback
# ---------------------------------------------------------------------------

def _load_from_cache() -> tuple[dict[str, list], set[str]] | None:
    """
    Load the last-known track list from _metadata_cache.json.
    Returns (track_map, liked_ids) or None if cache is empty/missing.

    This is used when Spotify rate-limits us beyond the MAX_WAIT_S threshold.
    The cache is updated on every successful live fetch, so it reflects the
    last known state of the library — good enough for downloads/transfers.
    """
    if not config.CACHE_FILE.exists():
        return None
    try:
        cache = json.loads(config.CACHE_FILE.read_text(encoding="utf-8"))
        if not cache:
            return None

        tracks = list(cache.values())
        for t in tracks:
            # Ensure all required fields exist (cache may be from older version)
            t.setdefault("genre", "")
            t.setdefault("release_year", "")
            t.setdefault("is_liked", False)
            t.setdefault("compilation", False)
            t.setdefault("album_image", "")

        liked_ids = {t["id"] for t in tracks if t.get("is_liked")}

        # Rebuild a minimal track_map — all tracks under LIKED_SONGS_NAME
        # so the downloader has something to work with
        track_map = {config.LIKED_SONGS_NAME: tracks}

        log.warning(
            f"  [Spotify] Using cached library: {len(tracks)} track(s) "
            f"from last successful fetch. Downloads will proceed on cached data."
        )
        return track_map, liked_ids
    except Exception as e:
        log.warning(f"  [Spotify] Cache load failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------

def fetch_and_build(sp: spotipy.Spotify) -> tuple[dict[str, list], set[str]]:
    """
    Fetches all owned playlists + Liked Songs and returns:
        track_map  — {playlist_name: [track_dict, ...]}
                     Liked Songs inserted first (highest transfer priority)
        liked_ids  — set of Spotify track IDs in Liked Songs

    On rate limit exceeding MAX_WAIT_S, falls back to cached track data
    so downloads and transfers still run.
    """
    try:
        log.info("Fetching playlists from Spotify ...")
        playlists = get_all_playlists(sp)
        log.info(f"Found {len(playlists)} owned playlist(s)")

        log.info("Fetching Liked Songs ...")
        liked_tracks = get_liked_songs(sp)
        liked_ids    = {t["id"] for t in liked_tracks}
        log.info(f"Found {len(liked_tracks)} Liked Song(s)")

        # Bug 7 fix: also fetch saved (liked) albums so that any track whose
        # album is saved gets is_liked=True — not just tracks that appear in
        # the Liked Songs list directly.  The downloader's album-wise path
        # checks t["is_liked"] but previously that flag was only set here for
        # Liked Songs tracks; tracks from liked albums in other playlists were
        # silently processed per-track even with PRIORITIZE_LIKED_ALBUMS=True.
        log.info("Fetching saved albums ...")
        try:
            saved_album_ids = get_saved_albums(sp)
            log.info(f"Found {len(saved_album_ids)} saved album(s).")
        except Exception as _sae:
            log.warning(f"Could not fetch saved albums: {_sae} — liked-album path may under-fire.")
            saved_album_ids = set()

        for t in liked_tracks:
            t["is_liked"] = True

        track_map: dict[str, list] = {}
        seen_ids:  dict[str, dict] = {}

        if liked_tracks:
            track_map[config.LIKED_SONGS_NAME] = liked_tracks
            for t in liked_tracks:
                seen_ids[t["id"]] = t

        for pl in playlists:
            try:
                pl_tracks = get_playlist_tracks(sp, pl["id"])
            except RateLimitExceeded:
                log.warning(
                    f"  [Spotify] Rate limited fetching playlist '{pl['name']}' "
                    f"— skipping remaining playlists, using what we have so far."
                )
                break
            track_map[pl["name"]] = pl_tracks
            for t in pl_tracks:
                if t["id"] not in seen_ids:
                    seen_ids[t["id"]] = t

        # Bug 7 fix continued: propagate is_liked=True to all tracks whose
        # album_id is in saved_album_ids, across all playlists.
        if saved_album_ids:
            marked = 0
            for t in seen_ids.values():
                if not t.get("is_liked") and t.get("album_id") in saved_album_ids:
                    t["is_liked"] = True
                    marked += 1
            if marked:
                log.info(f"  [Spotify] Marked {marked} additional track(s) is_liked=True via saved albums.")

        log.info(f"{len(seen_ids)} unique track(s) across all playlists")
        _save_structural_cache(list(seen_ids.values()))
        return track_map, liked_ids, saved_album_ids

    except RateLimitExceeded as exc:
        log.warning(
            f"  [Spotify] Rate limited for {exc.retry_after}s "
            f"(>{MAX_WAIT_S}s threshold). Falling back to cached track library."
        )
        cached = _load_from_cache()
        if cached:
            # Cache fallback returns (track_map, liked_ids) without saved_album_ids
            return cached[0], cached[1], set()
        raise RuntimeError(
            f"Spotify rate limited for {exc.retry_after}s and no cache available. "
            f"Wait ~{exc.retry_after // 3600:.0f}h or check your Spotify app usage."
        ) from exc


def _save_structural_cache(tracks: list[dict]):
    cache: dict[str, dict] = {}
    if config.CACHE_FILE.exists():
        try:
            cache = json.loads(config.CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    for t in tracks:
        cache[t["id"]] = {k: t[k] for k in (
            "id", "title", "artists", "artist", "album", "album_id",
            "album_artist", "spotify_year", "track_number",
            "disc_number", "compilation", "is_liked", "album_image",
        ) if k in t}

    config.CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )