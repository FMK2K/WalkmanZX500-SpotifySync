"""
album_completeness.py — MK17 Phase B (M7)
==========================================
Album-completeness pass.

Apr 30 user direction: "WE ALSO WANNA MAKE SURE WE HAVE COMPLETE
ALBUMS ALWAYS".  Pre-MK17 the sync was driven primarily by individual
tracks the user had Liked/playlisted — partial albums (8 of 12 tracks)
stayed partial.  This module identifies the gaps and queues missing
tracks for download.

Operating modes:
  ONLINE: sp_client.album(album_id) returns the full tracklist; we
          compare against what's locally present and queue any missing
          tracks.
  OFFLINE: Spotify export's YourLibrary.json:albums[] records album IDs
          but NOT their full tracklists.  We can't enumerate gaps
          without an API call.  In offline mode, this module relies on
          the existing downloader's album-wise threshold mechanism
          (run_downloads → _build_album_strategies) and contributes
          nothing extra.

The function is intentionally thin: identify gaps, log them clearly,
return a list of "track-shaped" dicts suitable for handing to the
existing downloader waterfall.  It does NOT fetch tracks itself —
that's the downloader's job.
"""
from __future__ import annotations
import json
import logging
import re
import time
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# ===========================================================================
# 2026-09-02 — OFFLINE album metadata (tracklist + cover) via the public
# Spotify embed page. No auth, no API key. Used when OFFLINE_MODE=True so the
# a/b/c album-completeness rules below can still enumerate album gaps.
# ===========================================================================
_ALBUM_META_CACHE_FILE = "album_meta_cache.json"   # under BASE_DIR, prefixed "." at write
_ALBUM_META_TTL_S = 90 * 24 * 3600                 # 90 days
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_album_meta_mem: dict = {}


def _album_meta_path() -> Path:
    try:
        import config as _cfg
        return Path(getattr(_cfg, "BASE_DIR", ".")) / ("." + _ALBUM_META_CACHE_FILE)
    except Exception:
        return Path("." + _ALBUM_META_CACHE_FILE)


def _load_album_meta_cache() -> dict:
    global _album_meta_mem
    if _album_meta_mem:
        return _album_meta_mem
    p = _album_meta_path()
    if p.exists():
        try:
            _album_meta_mem = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _album_meta_mem = {}
    return _album_meta_mem


def _save_album_meta_cache() -> None:
    try:
        _album_meta_path().write_text(
            json.dumps(_album_meta_mem, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log.debug(f"  [Completeness] album_meta cache write failed: {e}")


def spotify_album_meta(album_id: str, *, force: bool = False) -> Optional[dict]:
    """
    Scrape https://open.spotify.com/embed/album/<id> for:
      {name, artists:[str], release_date, cover_url,
       tracks:[{title, artists:[str], uri, track_number}]}
    Cached (90d). Returns None on failure.
    Note: the embed does not expose disc numbers — track_number is the
    1-based position in the flat list (fine for single-disc; multi-disc
    deluxe editions get a flat 1..N which the reorganizer/audit will still
    place sensibly).
    """
    if not album_id:
        return None
    cache = _load_album_meta_cache()
    hit = cache.get(album_id)
    if hit and not force and (time.time() - hit.get("_ts", 0)) < _ALBUM_META_TTL_S:
        return hit
    try:
        import requests
        r = requests.get(
            f"https://open.spotify.com/embed/album/{album_id}",
            headers=_UA, timeout=15,
        )
        if r.status_code != 200:
            return hit or None
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            r.text, re.S,
        )
        if not m:
            return hit or None
        ent = (json.loads(m.group(1))["props"]["pageProps"]["state"]
               ["data"]["entity"])
        tl = ent.get("trackList", []) or []
        tracks = []
        for i, t in enumerate(tl, 1):
            sub = t.get("subtitle") or ""
            tracks.append({
                "title":        (t.get("title") or "").strip(),
                "artists":      [a.strip() for a in sub.split(",") if a.strip()] or [sub],
                "uri":          t.get("uri") or "",
                "track_number": i,
            })
        cover_url = ""
        for img in (ent.get("visualIdentity", {}).get("image", []) or []):
            u = img.get("url", "")
            if "ab67616d0000b273" in u:      # 640px standard
                cover_url = u; break
            cover_url = cover_url or u
        meta = {
            "name":         (ent.get("name") or ent.get("title") or "").strip(),
            "artists":      [a.strip() for a in (ent.get("subtitle") or "").split(",") if a.strip()],
            "release_date": ent.get("releaseDate", {}).get("isoString", "") if isinstance(ent.get("releaseDate"), dict) else (ent.get("releaseDate") or ""),
            "cover_url":    cover_url,
            "tracks":       tracks,
            "_ts":          time.time(),
        }
        cache[album_id] = meta
        _save_album_meta_cache()
        return meta
    except Exception as e:
        log.debug(f"  [Completeness] spotify_album_meta({album_id}) failed: {e}")
        return hit or None


def _norm(s: str) -> str:
    """
    2026-09-04: "&" was being stripped to nothing (same as any other
    punctuation), so a Spotify title like "Arnold & Danny" normalized to
    "arnold danny" while the same track tagged on-device as "Arnold and
    Danny" normalized to "arnold and danny" — different keys, so a track
    that was already present got reported/treated as missing. Confirmed
    live (Action Bronson - Lamb Over Rice). Expand "&" to "and" (both sides
    of every comparison run through this same function) before stripping
    the rest of the punctuation, so both spellings land on one key.
    """
    s = (s or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ===========================================================================
# 2026-09-04 — Followed-artist catalog completeness. Distinct from rules
# (a)/(b)/(c) below: those only catch an artist's albums if the user already
# has a specific liked-track/saved-album signal ON THAT ALBUM (or, for rule
# c, on ANY album by that artist). A followed artist with no saved album and
# too few liked tracks on any one release (e.g. followed Freddie Gibbs, but
# neither Alfredo, Alfredo 2, nor Piñata individually cleared the min-liked
# bar) falls through all three rules — confirmed live: Freddie Gibbs ranks
# #44/255 by engagement (5 liked, 2 saved albums) yet triggers none of them
# for his less-liked releases. This section closes that gap by working from
# the followed-artist list itself (Spotify Account Data/YourLibrary.json's
# "artists" key — the real followed-artist list; Follow.json is user-to-user
# social follows only, confirmed empty of artists).
#
# Ranking (rank_followed_artists) is pure offline arithmetic over the local
# export. Enumerating an artist's FULL discography has no Spotify-side
# offline equivalent — confirmed live 2026-09-04 that the public artist
# embed page (open.spotify.com/embed/artist/<id>), unlike the per-album
# embed spotify_album_meta() already scrapes, only exposes a 10-track "Top
# tracks" panel, not a discography — and the live Spotify Web API is
# currently 403'ing account-wide ("Active premium subscription required for
# the owner of the app"), a registered-app-level restriction, not something
# retries fix. So this uses Deezer's public API instead (api.deezer.com,
# confirmed live 2026-09-04: no auth/key needed at all for search, artist
# albums, or album tracks) for BOTH discography enumeration and per-album
# tracklists — already how downloader.py's Deezer fallback provider works,
# just reused here for metadata instead of audio.
# ===========================================================================

def rank_followed_artists(min_score: int = 0) -> list[dict]:
    """
    Rank the user's followed artists by local engagement signal (liked-track
    count + 3x saved-album count, saved albums weighted higher as a stronger
    per-artist signal) so a discography sweep can be prioritized instead of
    blindly hitting all ~255. Entirely offline (no API calls).

    Returns [{"artist","artist_uri","liked","saved_albums","score"}],
    sorted by score descending. artist_uri is the spotify:artist:<id> URI
    (present for essentially all real Spotify Follow entries), used by
    fetch_artist_albums().
    """
    import spotify_local_data as _sld
    if _sld._DATA_DIR is None:
        discovered = _sld._auto_discover()
        if discovered:
            _sld.install(discovered)
    lib = _sld._read_json("YourLibrary.json") or {}
    followed = [a for a in lib.get("artists", []) if a.get("name")]
    if not followed:
        return []
    from collections import Counter
    liked: Counter = Counter()
    for t in lib.get("tracks", []):
        liked[t.get("artist", "")] += 1
    saved: Counter = Counter()
    for a in lib.get("albums", []):
        saved[a.get("artist", "")] += 1

    out = []
    for a in followed:
        name = a["name"]
        L, A = liked.get(name, 0), saved.get(name, 0)
        score = L + A * 3
        if score >= min_score:
            out.append({"artist": name, "artist_uri": a.get("uri", ""),
                        "liked": L, "saved_albums": A, "score": score})
    out.sort(key=lambda e: -e["score"])
    return out


_DEEZER_API = "https://api.deezer.com"


def deezer_find_artist_id(artist_name: str) -> Optional[int]:
    """
    Resolve an artist name to a Deezer artist id. Deezer's search can return
    several same-named entries (stub/featured-artist pages with 0-2 albums
    alongside the real one) — among exact normalized-name matches, pick the
    one with the most albums (the real artist page). Falls back to the top
    search hit if no exact-name match. None on failure/no results.
    """
    if not artist_name:
        return None
    try:
        import requests
        r = requests.get(f"{_DEEZER_API}/search/artist",
                         params={"q": artist_name, "limit": 10}, timeout=15)
        data = r.json().get("data", []) or []
    except Exception as e:
        log.debug(f"  [ArtistCompleteness] deezer artist search({artist_name!r}) failed: {e}")
        return None
    if not data:
        return None
    target = _norm(artist_name)
    exact = [a for a in data if _norm(a.get("name", "")) == target]
    pool = exact or data
    best = max(pool, key=lambda a: a.get("nb_album", 0) or 0)
    return best.get("id")


def fetch_artist_albums(artist_name: str, *, include_types=("album", "ep")) -> list[dict]:
    """
    Full discography for one artist via Deezer's public API (no auth). See
    module note above for why Deezer instead of Spotify. Returns
    [{"name","id","album_type","release_date","total_tracks"}], filtered to
    record_type in include_types (default: real albums + EPs, excluding the
    much larger volume of loose singles/remixes/feature-appearances a
    prolific artist accumulates — pass include_types=("album","ep","single")
    to widen it), deduped by normalized name keeping the release with the
    most tracks (favors a deluxe/complete edition over a bare split of the
    same album). Returns [] on any failure or unresolved artist — callers
    should treat that as "could not check", not "artist has no albums".
    """
    artist_id = deezer_find_artist_id(artist_name)
    if not artist_id:
        return []
    items = []
    try:
        import requests
        url = f"{_DEEZER_API}/artist/{artist_id}/albums"
        params = {"limit": 100}
        while url:
            r = requests.get(url, params=params, timeout=15)
            d = r.json()
            items.extend(d.get("data", []) or [])
            url = d.get("next")
            params = {}  # `next` already carries its own query string
    except Exception as e:
        log.debug(f"  [ArtistCompleteness] deezer albums(artist_id={artist_id}) failed: {e}")
        return []

    by_name: dict = {}
    for a in items:
        if include_types and a.get("record_type") not in include_types:
            continue
        k = _norm(a.get("title", ""))
        if not k:
            continue
        nt = a.get("nb_tracks") or 0
        if k not in by_name or nt > (by_name[k].get("nb_tracks") or 0):
            by_name[k] = a
    return [
        {"name": a.get("title", ""), "id": a.get("id", ""),
         "album_type": a.get("record_type", ""), "release_date": a.get("release_date", ""),
         "total_tracks": a.get("nb_tracks", 0)}
        for a in by_name.values()
    ]


def fetch_album_tracklist(album_id) -> list[dict]:
    """
    Tracklist for one Deezer album id. Returns [{"title","artists",
    "track_number","duration_s"}] -- duration_s added 2026-09-04 for
    duration_verify.py's mislabeled-file sweep (Deezer's /tracks endpoint
    carries it directly, no extra call needed). The base fields are the
    same shape spotify_album_meta()["tracks"] uses so callers can treat the
    two interchangeably; duration_s is Deezer-only (0 if absent/failed).
    [] on failure.
    """
    if not album_id:
        return []
    try:
        import requests
        r = requests.get(f"{_DEEZER_API}/album/{album_id}/tracks", timeout=15)
        items = r.json().get("data", []) or []
    except Exception as e:
        log.debug(f"  [ArtistCompleteness] deezer tracks(album_id={album_id}) failed: {e}")
        return []
    return [
        {"title": t.get("title", ""),
         "artists": [t.get("artist", {}).get("name", "")] if t.get("artist") else [],
         "track_number": t.get("track_position", i),
         "duration_s": t.get("duration", 0) or 0}
        for i, t in enumerate(items, 1)
    ]


def build_completion_album_set(
    track_map: dict,
    saved_album_ids: Iterable[str],
    library_albums: Optional[list[dict]] = None,
    *,
    min_liked: int = 3,
    artist_has_saved: bool = True,
) -> list[dict]:
    """
    Decide which albums to complete IN FULL. Returns
      [{artist, album, album_id (or ""), reason}]
    where an album qualifies if ANY of:
      (a) it is a Spotify-saved album,
      (b) the user has >= min_liked liked tracks from it,
      (c) [when artist_has_saved] its artist has >= 1 saved album.

    library_albums: the YourLibrary.json `albums` list
      ([{artist, album, uri}]) — gives real Spotify album IDs + the
      artist set for rule (c). If None, rule (a)/(c) fall back to what
      can be inferred from track_map's own album_id / is_liked fields.
    """
    saved_ids = {s for s in (saved_album_ids or []) if s}
    lib = library_albums or []

    # (artist_norm, album_norm) -> {"artist","album","album_id","reasons":set}
    out: dict[tuple, dict] = {}

    def _add(artist, album, album_id, reason):
        k = (_norm(artist), _norm(album))
        if not k[0] and not k[1]:
            return
        e = out.setdefault(k, {"artist": artist, "album": album,
                               "album_id": album_id or "", "reasons": set()})
        e["reasons"].add(reason)
        if album_id and not e["album_id"]:
            e["album_id"] = album_id

    # rule (a): saved albums (from library_albums, which carries real IDs)
    saved_artist_norms: set = set()
    for a in lib:
        aid = (a.get("uri", "") or "").replace("spotify:album:", "")
        _add(a.get("artist", ""), a.get("album", ""), aid, "saved-album")
        saved_artist_norms.add(_norm(a.get("artist", "")))
    # also honour any raw saved_album_ids not in lib
    for sid in saved_ids:
        if not any(e["album_id"] == sid for e in out.values()):
            meta = spotify_album_meta(sid)
            if meta:
                _add(meta["artists"][0] if meta["artists"] else "",
                     meta["name"], sid, "saved-album")

    # scan track_map once for rules (b) and (c)
    from collections import Counter
    liked_by_album: Counter = Counter()
    album_display: dict = {}
    album_artist_norms: dict = {}
    for _pl, tracks in (track_map or {}).items():
        for t in tracks or []:
            if not isinstance(t, dict):
                continue
            art = (t.get("album_artist") or t.get("artist")
                   or (t.get("artists") or [""])[0] or "")
            alb = t.get("album") or ""
            k = (_norm(art), _norm(alb))
            if not k[1]:
                continue
            album_display[k] = (art, alb)
            album_artist_norms[k] = _norm(art)
            if t.get("is_liked"):
                liked_by_album[k] += 1

    # rule (b)
    for k, n in liked_by_album.items():
        if n >= min_liked:
            art, alb = album_display[k]
            _add(art, alb, "", f"{n}-liked-tracks")

    # rule (c)
    if artist_has_saved:
        for k, artn in album_artist_norms.items():
            if artn in saved_artist_norms:
                art, alb = album_display[k]
                _add(art, alb, "", "artist-has-saved-album")

    result = []
    for e in out.values():
        result.append({
            "artist":   e["artist"],
            "album":    e["album"],
            "album_id": e["album_id"],
            "reason":   ",".join(sorted(e["reasons"])),
        })
    log.info(
        f"  [Completeness] completion set: {len(result)} album(s) "
        f"(saved={sum('saved-album' in r['reason'] for r in result)}, "
        f">={min_liked}-liked={sum('liked-tracks' in r['reason'] for r in result)}, "
        f"artist-has-saved={sum('artist-has-saved' in r['reason'] for r in result)})"
    )
    return result


def find_missing_tracks_for_albums(
    saved_album_ids: Iterable[str],
    track_map: dict,
    sp_client,
    dev_lib=None,
    max_new_downloads: int = 200,
) -> list[dict]:
    """
    For each album in *saved_album_ids*, determine which tracks of that
    album are NOT already represented in *track_map* OR on the device
    (per *dev_lib* if provided), and return them as a list of track
    dicts in the same shape the downloader expects.

    MK18 #5: Strict OFFLINE_MODE gate.
    When OFFLINE_MODE is True, this function requires sp_client to make
    Spotify API calls (sp.album(album_id)) which fail with NoneType or
    403 errors.  The offline path previously fell through into the API
    branch, producing misleading "completed" status from partial data.
    Now we detect offline mode early, log SKIPPED_OFFLINE, and return
    an empty list so callers can differentiate between "no missing tracks"
    and "completeness check skipped because offline".

    Args:
        saved_album_ids: Set of Spotify album IDs the user has saved.
        track_map:       Existing track_map, keyed by playlist name.
        sp_client:       Online Spotify client (spotipy-style).  When
                         None, we silently return [] — caller is in
                         offline mode and this enrichment is unavailable.
        dev_lib:         Optional DeviceLibrary for cross-checking
                         what's already on the Walkman.
        max_new_downloads: Cap on returned items so a runaway result
                         doesn't blow up runtime.

    Returns:
        List of track dicts.  Each dict carries enough metadata
        (id, title, artist, artists, album, album_artist, album_id,
        track_number, disc_number) for the downloader to attempt it.
    """
    # MK18 #5: Strict OFFLINE_MODE gate — must come before any API access.
    # Previously, sp_client=None was the only offline guard, but a stale
    # spotipy token can pass the None check and then raise 403/NoneType
    # deep in the API call chain, producing misleading "completed" status.
    try:
        import config as _cfg
        _offline = getattr(_cfg, "OFFLINE_MODE", False)
    except Exception:
        _offline = sp_client is None
    if _offline or sp_client is None:
        log.info("  [Completeness] SKIPPED_OFFLINE — album completeness check "
                 "requires Spotify API (disabled in offline mode).")
        return []

    if not sp_client:
        log.debug("  [Completeness] Skipped: offline mode (no sp_client).")
        return []

    if not saved_album_ids:
        log.debug("  [Completeness] Skipped: no saved albums.")
        return []

    # Build a fast lookup of (artist_lower, title_lower) → True for what
    # we already have in track_map, so we don't re-queue duplicates.
    existing_keys: set[tuple[str, str]] = set()
    for plist, tracks in (track_map or {}).items():
        for t in tracks or []:
            if not isinstance(t, dict):
                continue
            artist = (t.get("artist") or (t.get("artists") or [""])[0] or "").strip().lower()
            title = (t.get("title") or "").strip().lower()
            if artist and title:
                existing_keys.add((artist, title))

    missing: list[dict] = []
    fetched = 0
    failed = 0

    log.info(
        f"  [Completeness] Checking {len(saved_album_ids)} saved album(s) "
        f"for tracklist gaps ..."
    )

    for album_id in list(saved_album_ids):
        if len(missing) >= max_new_downloads:
            log.info(
                f"  [Completeness] Hit cap of {max_new_downloads} new tracks; "
                f"deferring remaining albums to next sync."
            )
            break

        try:
            album_info = sp_client.album(album_id)
        except Exception as e:
            failed += 1
            log.debug(f"  [Completeness] sp.album({album_id}) failed: {e}")
            continue
        fetched += 1

        if not album_info:
            continue

        album_name = album_info.get("name", "Unknown")
        album_artists = [a.get("name", "") for a in album_info.get("artists", [])]
        album_artist  = album_artists[0] if album_artists else "Unknown"
        compilation   = album_info.get("album_type") == "compilation"

        for item in album_info.get("tracks", {}).get("items", []) or []:
            title         = item.get("name", "").strip()
            track_artists = [a.get("name", "") for a in item.get("artists", [])]
            primary       = track_artists[0] if track_artists else album_artist

            key = (primary.lower(), title.lower())
            if key in existing_keys:
                continue  # Already in our library

            if dev_lib is not None:
                try:
                    if dev_lib.has_track(
                        artist=album_artist, album=album_name, title=title
                    ):
                        continue  # Already on device
                except Exception:
                    pass

            track_dict = {
                "id":            item.get("id", ""),
                "title":         title,
                "artist":        primary,
                "artists":       track_artists,
                "album":         album_name,
                "album_artist":  album_artist,
                "album_id":      album_id,
                "track_number":  item.get("track_number", 1),
                "disc_number":   item.get("disc_number", 1),
                "compilation":   compilation,
                "is_liked":      True,  # the album is saved → tracks are liked
                "_completeness_fill": True,  # marker for the downloader
            }
            missing.append(track_dict)
            existing_keys.add(key)  # avoid duplicate within this pass

    log.info(
        f"  [Completeness] Done: {fetched} album(s) inspected, "
        f"{failed} API failure(s), {len(missing)} missing track(s) queued."
    )
    return missing


# ===========================================================================
# 2026-09-02 — run_completeness_pass: the single shared entry point used by
# BOTH walkman_sync.WalkmanSyncAgent (legacy) AND spiral_orchestrator
# (SpiralOrchestrator). Works online (sp_client) OR offline (Spotify embed
# scrape). Builds the a/b/c completion set, resolves each album's tracklist,
# diffs against what's already present, returns __completeness_fill__ tracks.
# ===========================================================================
def run_completeness_pass(
    track_map: dict,
    saved_album_ids: Iterable[str],
    *,
    sp_client=None,
    dev_lib=None,
    library_albums: Optional[list[dict]] = None,
    max_new_downloads: Optional[int] = None,
) -> list[dict]:
    """
    Returns a list of track dicts (shape the downloader expects) for tracks
    that are missing from albums the user wants completed in full.

    Album qualifies via build_completion_album_set()'s a/b/c rules
    (config: ALBUM_COMPLETE_MIN_LIKED, ALBUM_COMPLETE_ARTIST_HAS_SAVED).
    Cap: config.ALBUM_COMPLETENESS_MAX_NEW_DOWNLOADS (override via arg).
    """
    try:
        import config as _cfg
    except Exception:
        _cfg = None

    if _cfg is not None and not getattr(_cfg, "ALBUM_COMPLETENESS_ENABLED", True):
        log.info("  [Completeness] disabled (ALBUM_COMPLETENESS_ENABLED=False)")
        return []

    min_liked = int(getattr(_cfg, "ALBUM_COMPLETE_MIN_LIKED", 3)) if _cfg else 3
    artist_has_saved = bool(getattr(_cfg, "ALBUM_COMPLETE_ARTIST_HAS_SAVED", True)) if _cfg else True
    cap = max_new_downloads if max_new_downloads is not None else \
        int(getattr(_cfg, "ALBUM_COMPLETENESS_MAX_NEW_DOWNLOADS", 200)) if _cfg else 200
    offline = bool(getattr(_cfg, "OFFLINE_MODE", False)) if _cfg else (sp_client is None)

    # if offline and no library_albums passed, try to read the export directly
    if offline and library_albums is None:
        try:
            import spotify_local_data as _sld
            _lib = _sld._read_json("YourLibrary.json") or {}
            library_albums = _lib.get("albums", [])
        except Exception:
            library_albums = []

    album_set = build_completion_album_set(
        track_map, saved_album_ids, library_albums,
        min_liked=min_liked, artist_has_saved=artist_has_saved,
    )
    if not album_set:
        return []

    # what do we already have? (artist_norm, title_norm)
    have: set = set()
    for _pl, tracks in (track_map or {}).items():
        for t in tracks or []:
            if not isinstance(t, dict):
                continue
            art = (t.get("artist") or (t.get("artists") or [""])[0] or "")
            have.add((_norm(art), _norm(t.get("title", ""))))

    missing: list[dict] = []
    inspected = whole_album_seeds = 0

    for entry in album_set:
        if len(missing) >= cap:
            log.info(f"  [Completeness] hit cap {cap} — deferring rest to next run")
            break
        artist, album, album_id = entry["artist"], entry["album"], entry["album_id"]

        tracklist = None
        cover_url = ""
        if album_id:
            meta = spotify_album_meta(album_id)
            if meta and meta.get("tracks"):
                tracklist = meta["tracks"]
                cover_url = meta.get("cover_url", "")
        elif sp_client is not None and not offline:
            try:
                info = sp_client.album_tracks(f"spotify:album:{album_id}") if album_id else None
            except Exception:
                info = None
            if info:
                tracklist = [
                    {"title": it.get("name", ""),
                     "artists": [a.get("name", "") for a in it.get("artists", [])],
                     "track_number": it.get("track_number", 1)}
                    for it in info.get("items", [])
                ]
        inspected += 1

        if not tracklist:
            # no resolvable tracklist (rule b/c album, offline, no Spotify id):
            # emit a whole-album SEED and let the downloader's Tidal/Deezer
            # album-wise path enumerate + fill it.
            missing.append({
                "id": "", "title": "", "artist": artist, "artists": [artist],
                "album": album, "album_artist": artist, "album_id": album_id or "",
                "track_number": 1, "disc_number": 1, "is_liked": True,
                "album_image": cover_url,
                "_completeness_fill": True, "_complete_whole_album": True,
                "_reason": entry["reason"],
            })
            whole_album_seeds += 1
            continue

        for pos, tr in enumerate(tracklist, 1):
            title = (tr.get("title") or "").strip()
            primary = (tr.get("artists") or [artist])[0] or artist
            if not title:
                continue
            if (_norm(primary), _norm(title)) in have:
                continue
            if dev_lib is not None:
                try:
                    if dev_lib.has_track(artist=artist, album=album, title=title):
                        continue
                except Exception:
                    pass
            missing.append({
                "id": "", "title": title, "artist": primary, "artists": tr.get("artists") or [primary],
                "album": album, "album_artist": artist, "album_id": album_id or "",
                "track_number": tr.get("track_number", pos), "disc_number": 1,
                "is_liked": True, "album_image": cover_url,
                "_completeness_fill": True, "_reason": entry["reason"],
            })
            have.add((_norm(primary), _norm(title)))
            if len(missing) >= cap:
                break

    log.info(
        f"  [Completeness] {inspected} album(s) inspected -> {len(missing)} fill track(s) "
        f"({whole_album_seeds} whole-album seed(s), cap={cap}, "
        f"{'offline scrape' if offline else 'API'})"
    )
    return missing
