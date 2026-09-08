"""
listenbrainz_client.py  —  Self-hosted discovery via ListenBrainz (Task 13)
============================================================================
Originally scoped as "explo_client.py" (talking to the Explo Docker
container's API). Investigation found Explo has NO documented REST API for
fetching its recommended track list — it's a self-contained tool that
downloads tracks itself (via yt-dlp) and writes directly into a music
server (Jellyfin/Navidrome/Plex/etc.), with no decoupled data export. Since
the user already has ListenBrainz listening history but no Explo instance
running, this talks directly to ListenBrainz's own public API instead —
real, documented, no-auth-required for public recommendation playlists,
and delivers the same "self-hosted Discover Weekly" value without requiring
Explo/Docker to exist first.

API used (https://listenbrainz.readthedocs.io/en/latest/users/api/):
    GET /1/user/{username}/playlists/createdfor
        Recommendation playlists ListenBrainz auto-generates for a user
        (Weekly Jams, Weekly Exploration, Daily Jams) — metadata only.
    GET /1/playlist/{playlist_mbid}
        Full playlist detail in JSPF format (JSON Playlist Format, an open
        standard — track.title/creator/album fields), including tracks.

Mirrors cover_art.py / genre_client.py's fail-soft shape: never raises,
degrades to an empty list on any network/parse failure so a discovery-feed
miss can never interrupt the primary sync loop.

Public API
----------
    fetch_recommendation_playlists(username) -> list[dict]
        [{"title": str, "mbid": str}, ...]

    fetch_playlist_tracks(playlist_mbid) -> list[dict]
        [{"title": str, "artist": str, "album": str}, ...] — same shape
        downloader.py's track dicts use for artist/title/album (no "id"
        key, since these aren't Spotify tracks — _download_with_waterfall
        already skips Spotify-URL-only providers gracefully when "id" is
        absent).

    fetch_discovery_queue(username) -> list[dict]
        Convenience: all tracks across all createdfor playlists, deduped
        by (artist, title).

Config
------
    config.LISTENBRAINZ_DISCOVERY_ENABLED = False   (opt-in)
    config.LISTENBRAINZ_USERNAME = ""                (required to enable)
    config.LISTENBRAINZ_REQUEST_TIMEOUT_S = 10
"""
from __future__ import annotations

import logging
from typing import Optional

import config

log = logging.getLogger(__name__)

LISTENBRAINZ_API_BASE = "https://api.listenbrainz.org"


def _cfg(attr, default=None):
    return getattr(config, attr, default)


def _get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    """Fail-soft GET + JSON parse. Returns None on any error."""
    timeout = float(_cfg("LISTENBRAINZ_REQUEST_TIMEOUT_S", 10))
    try:
        import requests
        resp = requests.get(
            url, params=params, timeout=timeout,
            headers={"User-Agent": "WalkmanSync/1.0 (+ListenBrainz discovery client)"},
        )
        if resp.status_code != 200:
            log.debug(f"  [ListenBrainz] {url} -> HTTP {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        log.debug(f"  [ListenBrainz] request failed ({url}): {e}")
        return None


def fetch_recommendation_playlists(username: str) -> list[dict]:
    """Fetch the list of ListenBrainz-generated recommendation playlists
    (Weekly Jams, Weekly Exploration, Daily Jams, etc.) for *username*.
    Public/no-auth. Returns [] on any failure or if none exist.

    Returns: [{"title": str, "mbid": str}, ...]
    """
    if not username:
        return []
    url = f"{LISTENBRAINZ_API_BASE}/1/user/{username}/playlists/createdfor"
    data = _get_json(url)
    if not data:
        return []

    out = []
    for entry in data.get("playlists", []):
        pl = entry.get("playlist", entry)  # tolerate either shape
        mbid = pl.get("identifier", "")
        # identifier is typically a URI like ".../playlist/<mbid>" — take the tail.
        if isinstance(mbid, str) and "/" in mbid:
            mbid = mbid.rstrip("/").rsplit("/", 1)[-1]
        title = pl.get("title", "")
        if mbid:
            out.append({"title": title, "mbid": mbid})
    return out


def fetch_playlist_tracks(playlist_mbid: str) -> list[dict]:
    """Fetch the full track list for a playlist MBID, in downloader.py's
    track-dict shape (title/artist/album, no Spotify "id"). Returns [] on
    any failure or an empty playlist."""
    if not playlist_mbid:
        return []
    url = f"{LISTENBRAINZ_API_BASE}/1/playlist/{playlist_mbid}"
    data = _get_json(url)
    if not data:
        return []

    tracks = []
    playlist = data.get("playlist", data)
    for t in playlist.get("track", []):
        title = (t.get("title") or "").strip()
        artist = (t.get("creator") or "").strip()
        album = (t.get("album") or "").strip()
        if not title or not artist:
            continue
        tracks.append({"title": title, "artist": artist, "album": album})
    return tracks


def fetch_discovery_queue(username: str) -> list[dict]:
    """Convenience: all tracks across every createdfor playlist for
    *username*, deduped by (artist, title). Returns [] on any failure —
    a discovery-feed miss must never interrupt the primary sync loop."""
    if not _cfg("LISTENBRAINZ_DISCOVERY_ENABLED", False):
        return []
    if not username:
        log.debug("  [ListenBrainz] no username configured — skipping discovery feed")
        return []

    playlists = fetch_recommendation_playlists(username)
    seen: set[tuple[str, str]] = set()
    queue: list[dict] = []
    for pl in playlists:
        for track in fetch_playlist_tracks(pl["mbid"]):
            key = (track["artist"].lower(), track["title"].lower())
            if key in seen:
                continue
            seen.add(key)
            queue.append(track)
    log.info(f"  [ListenBrainz] Discovery queue: {len(queue)} track(s) across {len(playlists)} playlist(s)")
    return queue


__all__ = [
    "fetch_recommendation_playlists", "fetch_playlist_tracks",
    "fetch_discovery_queue",
]
