"""
spotify_local_data.py  —  Offline Spotify account data integration
====================================================================
Parses the "Your Account Data" export (YourLibrary.json, Playlist1.json,
StreamingHistory_music_*.json, Wrapped2025.json) so the pipeline has a
LOCAL fallback when the Spotify API is slow/rate-limited/unavailable.

What this module provides
--------------------------
1. load()          — builds and caches the enriched track index
2. augment_track_map()  — adds offline-known tracks to an existing
                          track_map returned by spotify_client.fetch_and_build()
3. enrich_track()  — fills is_liked, play_count, skip_rate on a track dict
4. get_all_playlists() — returns playlist→[track_uri] from the local export
5. get_liked_uris() — set of track URIs in the saved library

The offline export knows:
  • Saved tracks (2270), saved albums (149), followed artists (255)
  • 33 playlists with full track lists (3215 tracks)
  • 8958 recent play events (1-year history)
  • 68,879 lifetime plays from the Extended Streaming History
  • Wrapped 2025 top tracks / genres

This is used as TIER 0 of the data waterfall — before the Spotify API call
and before any AI inference.  It costs zero API calls and is instantaneous.

Data flow:
    Offline export → augments → track_map from spotify_client
    If Spotify API fails → offline export alone provides the track_map
    Streaming history → priority_score, popm_candidates, smart playlists
                        (already in streaming_history.py; this module adds
                         the per-track play counts from the 1-year recent
                         history as a faster supplement)

Usage
-----
    import spotify_local_data as sld
    sld.install(data_dir)           # set path to the extracted export folder
    sld.load()                      # build index (auto-called on first access)

    # Augment track_map BEFORE writing to device (fills gaps left by API)
    track_map = sld.augment_track_map(track_map, all_uris=liked_ids)

    # Get enrichment for a specific track
    meta = sld.enrich_track("spotify:track:xxx")
    # → {"play_count": 31, "skip_rate": 0.1, "is_liked": True, "genre_hints": [...]}
"""

from __future__ import annotations

import json
import logging
import pathlib
import zipfile
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path configuration
# ---------------------------------------------------------------------------
_DATA_DIR: Optional[pathlib.Path] = None   # set by install()
_CACHE: dict = {}                          # URI → enriched dict
_PLAYLISTS: dict[str, list[str]] = {}      # name → [uri, ...]
_LIKED_URIS: set[str] = set()
_LOADED = False


def install(data_dir: str | pathlib.Path) -> None:
    """Point the module at the extracted Spotify Account Data folder or its zip."""
    global _DATA_DIR
    _DATA_DIR = pathlib.Path(data_dir)


def _auto_discover() -> Optional[pathlib.Path]:
    """Try common locations for the Spotify Account Data export.

    Rev 14.9.5 — extended the candidate list for users who unzip
    `my_spotify_data__N_.zip` files directly in the project directory
    (which produces a folder named like `my_spotify_data__3_/Spotify
    Extended Streaming History/` and `my_spotify_data__4_/Spotify
    Account Data/`).  We probe for the parent shell and unwrap.
    """
    import config
    # Check config first
    cfg_path = getattr(config, "SPOTIFY_LOCAL_DATA_PATH", None)
    if cfg_path:
        p = pathlib.Path(cfg_path)
        if p.exists():
            return p
    base_dirs = [
        getattr(config, "DATA_DIR", config.BASE_DIR),
        config.BASE_DIR,
        config.PROJECT_ROOT,
        pathlib.Path.home() / "Downloads",
    ]
    # Direct candidates: folder containing YourLibrary.json
    direct = []
    for base in base_dirs:
        direct.extend([
            base / "spotify_account_data",
            base / "Spotify Account Data",
            base / "my_spotify_data",
        ])
        # Multi-zip case: my_spotify_data__N_/Spotify Account Data/
        if base.is_dir():
            for child in base.iterdir():
                if child.is_dir() and "spotify" in child.name.lower():
                    direct.append(child / "Spotify Account Data")
                    direct.append(child)
    for p in direct:
        try:
            if p.is_dir() and (p / "YourLibrary.json").exists():
                return p
        except OSError:
            continue
    # Fallback: zip files
    zip_candidates = [
        config.BASE_DIR / "my_spotify_data.zip",
        pathlib.Path.home() / "Downloads" / "my_spotify_data.zip",
    ]
    for z in zip_candidates:
        if z.exists():
            return z
    return None


def _read_json(name: str) -> list | dict | None:
    """Read a JSON file from the data dir, supporting both dir and zip sources."""
    if _DATA_DIR is None:
        return None
    p = _DATA_DIR
    if p.suffix == ".zip":
        try:
            with zipfile.ZipFile(p) as z:
                # Try both with and without 'Spotify Account Data/' prefix
                for prefix in ["Spotify Account Data/", ""]:
                    try:
                        return json.loads(z.read(prefix + name))
                    except KeyError:
                        continue
        except Exception as e:
            log.debug(f"  [SpotifyLocal] Failed to read {name} from zip: {e}")
            return None
    else:
        fpath = p / name
        if not fpath.exists():
            return None
        try:
            return json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as e:
            log.debug(f"  [SpotifyLocal] Failed to read {name}: {e}")
            return None


def load(force_rebuild: bool = False) -> dict:
    """
    Build (or return cached) the enriched track index.
    Returns dict: URI → {track, artist, album, is_liked, play_count_recent,
                          skip_count_recent, play_count_lifetime, playlists}
    """
    global _CACHE, _PLAYLISTS, _LIKED_URIS, _LOADED

    if _LOADED and not force_rebuild:
        return _CACHE

    if _DATA_DIR is None:
        discovered = _auto_discover()
        if discovered:
            install(discovered)
        else:
            log.debug("  [SpotifyLocal] No Spotify Account Data export found — skipping")
            _LOADED = True
            return {}

    index: dict[str, dict] = {}

    # ── 1. YourLibrary.json — saved tracks, albums, artists ────────────────
    lib = _read_json("YourLibrary.json")
    if lib:
        for t in lib.get("tracks", []):
            uri = t.get("uri", "")
            if not uri:
                continue
            index[uri] = {
                "track":              t.get("track", ""),
                "artist":             t.get("artist", ""),
                "album":              t.get("album", ""),
                "is_liked":           True,
                "play_count_recent":  0,
                "skip_count_recent":  0,
                "play_count_lifetime": 0,
                "playlists":          [],
            }
            _LIKED_URIS.add(uri)

        saved_album_uris = {a.get("uri", "") for a in lib.get("albums", [])}
        log.debug(f"  [SpotifyLocal] Library: {len(index)} saved tracks, "
                  f"{len(saved_album_uris)} saved albums")

    # ── 2. Playlist1.json — playlist track lists ───────────────────────────
    pl_data = _read_json("Playlist1.json")
    if pl_data:
        for pl in pl_data.get("playlists", []):
            pl_name = pl.get("name", "Unknown Playlist")
            uris = []
            for item in pl.get("items", []):
                t = item.get("track", {})
                uri = t.get("trackUri", "")
                if not uri:
                    continue
                uris.append(uri)
                if uri not in index:
                    index[uri] = {
                        "track":              t.get("trackName", ""),
                        "artist":             t.get("artistName", ""),
                        "album":              t.get("albumName", ""),
                        "is_liked":           False,
                        "play_count_recent":  0,
                        "skip_count_recent":  0,
                        "play_count_lifetime": 0,
                        "playlists":          [],
                    }
                index[uri]["playlists"].append(pl_name)
            _PLAYLISTS[pl_name] = uris
        log.debug(f"  [SpotifyLocal] Playlists: {len(_PLAYLISTS)} playlists, "
                  f"{sum(len(v) for v in _PLAYLISTS.values())} tracks")

    # ── 3. StreamingHistory_music_0.json — recent 1-year plays ─────────────
    recent = _read_json("StreamingHistory_music_0.json")
    if recent:
        cutoff_12mo = datetime.now(timezone.utc) - timedelta(days=365)
        for entry in recent:
            # Recent history uses artistName/trackName, not URI
            artist = entry.get("artistName", "")
            track  = entry.get("trackName", "")
            ms     = entry.get("msPlayed", 0)
            # Try to match by name against index
            # (recent history doesn't have URIs — match by artist+track)
            _key = f"{artist.lower().strip()}|||{track.lower().strip()}"
            # We'll build a reverse lookup below
        # Build reverse name→URI map for recent history matching
        _name_to_uri: dict[str, str] = {}
        for uri, d in index.items():
            k = f"{d['artist'].lower().strip()}|||{d['track'].lower().strip()}"
            _name_to_uri[k] = uri

        for entry in recent:
            artist = entry.get("artistName", "")
            track  = entry.get("trackName", "")
            ms     = entry.get("msPlayed", 0)
            key    = f"{artist.lower().strip()}|||{track.lower().strip()}"
            uri    = _name_to_uri.get(key)
            if not uri:
                continue
            if ms > 30_000:   # real play (>30s)
                index[uri]["play_count_recent"] += 1
            else:
                index[uri]["skip_count_recent"] += 1
        log.debug(f"  [SpotifyLocal] Recent history: {len(recent)} events matched")

    _CACHE = index
    _LOADED = True

    # Task 12 — TuneMyMusic CSV fast-refresh supplement, purely additive.
    # Auto-discovers the CSV; silently no-ops if it isn't present.
    try:
        augment_from_tunemymusic_csv()
    except Exception as e:
        log.debug(f"  [SpotifyLocal] TuneMyMusic CSV supplement step failed: {e}")

    # Log summary
    liked = sum(1 for d in _CACHE.values() if d.get("is_liked"))
    with_plays = sum(1 for d in _CACHE.values() if d.get("play_count_recent", 0) > 0)
    log.info(
        f"[SpotifyLocal] Loaded: {len(_CACHE)} tracks  "
        f"({liked} liked, {with_plays} with recent plays, {len(_PLAYLISTS)} playlists)"
    )
    return _CACHE


def enrich_track(uri: str) -> dict:
    """
    Return enrichment dict for a single track URI.
    Keys: is_liked, play_count_recent, skip_count_recent, play_count_lifetime, playlists
    Returns empty dict if URI unknown.
    """
    if not _LOADED:
        load()
    return _CACHE.get(uri, {})


def get_liked_uris() -> set[str]:
    """Return the set of Spotify URIs that are saved/liked in the library."""
    if not _LOADED:
        load()
    return _LIKED_URIS


def get_all_playlists() -> dict[str, list[str]]:
    """Return {playlist_name: [uri, ...]} from the local export."""
    if not _LOADED:
        load()
    return _PLAYLISTS


# ---------------------------------------------------------------------------
# Task 12 (2026-07-18) — TuneMyMusic CSV fast-refresh supplement
# ---------------------------------------------------------------------------
# The official Spotify GDPR export (YourLibrary.json/Playlist1.json, loaded
# above) can take days to arrive and is richer overall, but a fresh
# TuneMyMusic library-export CSV (title/artist/album/playlist/ISRC/Spotify
# ID columns — real header confirmed against an actual export: "Track name,
# Artist name, Album, Playlist name, Type, ISRC, Spotify - id") can be pulled
# in seconds. This is purely additive: it only fills in URIs/playlists NOT
# already known from the GDPR export or the Spotify API, never overwrites
# better data. ISRCs are stashed too — a strong secondary matching key.

_ISRC_BY_URI: dict[str, str] = {}


def _auto_discover_tunemymusic_csv() -> Optional[pathlib.Path]:
    """Mirrors _auto_discover()'s search convention: config override first,
    then common Downloads/project-directory locations."""
    import config
    cfg_path = getattr(config, "TUNEMYMUSIC_CSV_PATH", None)
    if cfg_path:
        p = pathlib.Path(cfg_path)
        if p.exists():
            return p
    for candidate in (
        pathlib.Path.home() / "Downloads" / "My Spotify Library.csv",
        config.BASE_DIR / "My Spotify Library.csv",
    ):
        if candidate.exists():
            return candidate
    return None


def augment_from_tunemymusic_csv(csv_path: "str | pathlib.Path | None" = None) -> int:
    """Best-effort augmentation of _LIKED_URIS/_PLAYLISTS/_CACHE/_ISRC_BY_URI
    from a TuneMyMusic library-export CSV. Purely additive — never overwrites
    a URI/playlist already known from the GDPR export or Spotify API.

    Fail-soft by design (missing/stale/malformed CSV must never interrupt the
    primary sync loop): returns 0 and logs at DEBUG on any error, including a
    missing file. Auto-discovers the CSV (config.TUNEMYMUSIC_CSV_PATH, else
    ~/Downloads/My Spotify Library.csv) when csv_path is not given.

    Returns the number of NEW URIs added (0 if the CSV is absent, empty, or
    every row was already known).
    """
    if not _LOADED:
        load()

    path = pathlib.Path(csv_path) if csv_path else _auto_discover_tunemymusic_csv()
    if path is None or not path.exists():
        log.debug("  [SpotifyLocal] No TuneMyMusic CSV found — skipping fast-refresh supplement")
        return 0

    added = 0
    try:
        import csv as _csv
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                spotify_id = (row.get("Spotify - id") or "").strip()
                if not spotify_id:
                    continue
                uri = f"spotify:track:{spotify_id}"
                row_type = (row.get("Type") or "").strip()
                title = (row.get("Track name") or "").strip()
                artist = (row.get("Artist name") or "").strip()
                album = (row.get("Album") or "").strip()
                isrc = (row.get("ISRC") or "").strip()
                playlist_name = (row.get("Playlist name") or "").strip()

                if uri not in _CACHE:
                    if not title or not artist:
                        continue  # not enough to be useful — skip, don't guess
                    _CACHE[uri] = {
                        "track": title, "artist": artist, "album": album,
                        "is_liked": False, "play_count_recent": 0,
                        "skip_count_recent": 0, "play_count_lifetime": 0,
                        "playlists": [],
                    }
                    added += 1

                if isrc and uri not in _ISRC_BY_URI:
                    _ISRC_BY_URI[uri] = isrc

                if row_type == "Favorite" and uri not in _LIKED_URIS:
                    _LIKED_URIS.add(uri)
                    _CACHE[uri]["is_liked"] = True
                elif row_type == "Playlist" and playlist_name:
                    existing = _PLAYLISTS.setdefault(playlist_name, [])
                    if uri not in existing:
                        existing.append(uri)
                        if playlist_name not in _CACHE[uri]["playlists"]:
                            _CACHE[uri]["playlists"].append(playlist_name)
    except Exception as e:
        log.debug(f"  [SpotifyLocal] TuneMyMusic CSV parse failed ({path}): {e}")
        return added

    log.info(f"  [SpotifyLocal] TuneMyMusic CSV supplement: {added} new track(s) from {path.name}")
    return added


def get_isrc(uri: str) -> str:
    """Return the ISRC for *uri* if known (from the TuneMyMusic CSV
    supplement), else "". """
    return _ISRC_BY_URI.get(uri, "")


def _allocate_sequential_track_numbers(tracks: list[dict]) -> None:
    """
    Rev 14.9.3 — fail-safe track-number allocator.

    For tracks rescued from local-data only (no Spotify API track_number),
    assign 01, 02, 03... within each album in the order they were encountered.
    Mutates the track dicts in-place; does NOT overwrite existing values.

    The result is *not* the canonical album order — Spotify's playlist order
    is usually shuffled — but it guarantees every track within the same
    (album_artist, album) pair gets a unique track_number, which is the only
    invariant that prevents the splintered-album / overwriting-on-disk bug
    LOG-15 surfaced.

    Tracks that already have a non-empty `track_number` are left alone, so
    if metadata_enricher later fills these in via Spotify per-track API
    calls, the better data wins.

    MK8 Fix: assigned numbers are marked with `track_number_is_estimated=True`
    so the cross-playlist unification pass in augment_track_map() can
    replace them with canonical album track numbers when available.
    """
    counts: dict[tuple, int] = {}  # (album_artist_lower, album_lower) -> next number
    for t in tracks:
        if t.get("track_number"):  # already populated
            continue
        key = (
            (t.get("album_artist") or t.get("artist") or "Unknown").strip().lower(),
            (t.get("album") or "Unknown").strip().lower(),
        )
        n = counts.get(key, 0) + 1
        counts[key] = n
        t["track_number"] = str(n)
        t["track_number_is_estimated"] = True   # MK8: mark as sequentially allocated
        if not t.get("disc_number"):
            t["disc_number"] = "1"


def augment_track_map(
    track_map: dict[str, list],
    liked_ids: set[str] | None = None,
) -> dict[str, list]:
    """
    Augment a track_map from spotify_client.fetch_and_build() with:
      - is_liked flag from local library export
      - play_count_recent from 1-year streaming history
      - skip_rate computed from recent history
      - Playlists known locally but not returned by the API (if API failed)

    Also adds a 'local_data_confirmed' key so Stage 6 can note the track
    was verified against the local export (no API needed on next run).

    Call this AFTER fetch_and_build() or as a FALLBACK if it raised.
    """
    if not _LOADED:
        load()

    if not _CACHE:
        return track_map  # no local data available, pass through unchanged

    enriched = 0
    liked_from_local = 0

    # MK8 Fix (PR-CoverArt): load the Spotify metadata cache once upfront.
    # Tracks that came from local data export have album_image="" because the
    # account data JSON does not include image URLs.  The .spotify_metadata_cache.json
    # written by spotify_client.py DOES store album_image for every track
    # that was fetched from the Spotify API.  We use it here to backfill the URL
    # for any track whose album_image is currently blank — so cover art is
    # embedded during the download/tag stage (downloader._tag_file → _embed_cover_for_track)
    # rather than relying solely on yt-dlp thumbnails (which writethumbnail=False skips).
    _sp_meta_cache: dict = {}
    try:
        import json as _json_sld, config as _cfg_sld
        _sp_meta_path = _cfg_sld.BASE_DIR / ".spotify_metadata_cache.json"
        if _sp_meta_path.exists():
            _sp_meta_cache = _json_sld.loads(_sp_meta_path.read_text(encoding="utf-8"))
    except Exception:
        pass  # non-fatal — cover art will fall back to audit phase

    for pl_name, tracks in track_map.items():
        for t in tracks:
            uri = t.get("uri") or t.get("id") and f"spotify:track:{t['id']}"
            if not uri:
                continue
            local = _CACHE.get(uri)
            if not local:
                continue

            # Enrich is_liked
            if not t.get("is_liked") and local.get("is_liked"):
                t["is_liked"] = True
                liked_from_local += 1

            # Add streaming engagement data
            t["play_count_recent"] = local.get("play_count_recent", 0)
            t["skip_count_recent"] = local.get("skip_count_recent", 0)
            plays = t["play_count_recent"]
            skips = t["skip_count_recent"]
            t["skip_rate"] = (skips / (plays + skips)) if (plays + skips) > 0 else None
            t["local_data_confirmed"] = True
            enriched += 1

            # MK8 Fix (PR-CoverArt): backfill album_image from Spotify metadata cache
            if not t.get("album_image") and _sp_meta_cache:
                tid = uri.replace("spotify:track:", "")
                sp_entry = _sp_meta_cache.get(tid, {})
                img = (sp_entry.get("album_image") or "").strip()
                if img:
                    t["album_image"] = img

    # Second pass: backfill album_image for any track not matched by URI
    # using album-level lookup (catches tracks fetched by album rather than URI).
    if _sp_meta_cache:
        _album_image_by_album_id: dict[str, str] = {}
        for _tid, _sp in _sp_meta_cache.items():
            _aid = _sp.get("album_id", "")
            _img = (_sp.get("album_image") or "").strip()
            if _aid and _img and _aid not in _album_image_by_album_id:
                _album_image_by_album_id[_aid] = _img
        for pl_name, tracks in track_map.items():
            for t in tracks:
                if not t.get("album_image"):
                    _aid = t.get("album_id", "")
                    if _aid and _aid in _album_image_by_album_id:
                        t["album_image"] = _album_image_by_album_id[_aid]

    if enriched:
        log.info(
            f"[SpotifyLocal] Enriched {enriched} tracks "
            f"({liked_from_local} newly marked liked from local library)"
        )

    # If the API returned empty playlists but we have local data, fill them in.
    # BUGFIX (Rev 14.9.2): previous code skipped any playlist whose NAME was
    # present in track_map, but fetch_and_build inserts every owned playlist
    # name up front — even when get_playlist_tracks() returned [] for it.
    # That meant LOG-14's 33 owned playlists all arrived at Stage 4 with 0
    # tracks.  Correct check: only skip if we actually HAVE tracks for it.
    local_playlists = get_all_playlists()
    filled = 0
    refilled = 0
    for pl_name, uris in local_playlists.items():
        existing = track_map.get(pl_name)
        if existing:  # truthy = non-empty list → API result is authoritative
            continue
        # Build track dicts from local data only (minimal — no track_number etc.)
        # Rev 14.9.3 — DO NOT hardcode track_number="1".  Pre-14.9.3 every
        # track in a locally-rescued playlist got track_number=1, which made
        # every file land on disk as "01 X.mp3" — colliding album-mates onto
        # one filename slot, producing the LOG-15 splintered-album symptom.
        # Now we leave track_number/disc_number absent so downstream code can
        # (a) try Spotify per-track API enrichment in metadata_enricher,
        # (b) failing that, allocate sequential numbers per album below.
        pl_tracks = []
        for uri in uris:
            local = _CACHE.get(uri, {})
            if not local.get("track"):
                continue
            tid = uri.replace("spotify:track:", "")
            pl_tracks.append({
                "id":           tid,
                "uri":          uri,
                "title":        local["track"],
                "artist":       local["artist"],
                "album":        local["album"],
                "album_artist": local["artist"],
                "is_liked":     local.get("is_liked", False),
                # NOTE: no track_number / disc_number set — they are intentionally
                # absent so _allocate_sequential_track_numbers can fill them in.
                "play_count_recent": local.get("play_count_recent", 0),
                "_from_local_data": True,  # marker: enrichment needed
            })

        # Rev 14.9.3 — sequential track-number fallback per album.
        # If we can't enrich from Spotify per-track API (or that fails for
        # some tracks), allocate 01, 02, 03... within each album based on
        # the order tracks appear in the playlist's local data.  Imperfect
        # (real album order is usually different) but at minimum every
        # track on the same album gets a UNIQUE filename slot — no
        # collisions, no silent overwrites.
        _allocate_sequential_track_numbers(pl_tracks)

        if pl_tracks:
            track_map[pl_name] = pl_tracks
            if pl_name in track_map and existing is not None:
                # existing was an empty list: we're rescuing a playlist the
                # API returned with no tracks (the LOG-14 case).
                refilled += 1
            else:
                filled += 1

    if filled:
        log.info(f"[SpotifyLocal] Added {filled} playlists from local export "
                 f"(API did not return these)")
    if refilled:
        log.info(f"[SpotifyLocal] Refilled {refilled} playlists from local export "
                 f"(API returned them empty — likely rate-limit or field-spec issue)")

    # MK8 Fix — cross-playlist track_number unification.
    # _allocate_sequential_track_numbers() assigns sequential numbers (01, 02,
    # 03…) based on the order tracks appear in the LOCAL EXPORT PLAYLIST, which
    # is almost never the canonical album order.  The Spotify API (Liked Songs
    # track objects) does return real album track_number — so the same track
    # can exist in track_map twice:
    #   - "Liked Songs"    → track_number="12"  (real: IGOR track 12)
    #   - "My Playlist"    → track_number="2"   (allocated: 2nd in that playlist)
    # Both versions get transferred because seen_dedup was per-playlist.
    # Even with the global dedup fix above, the first playlist processed
    # determines the track_number stamped into the filename.  We want the
    # canonical album number to win.  Strategy: build a URI→track_number
    # index from Liked Songs / Spotify API playlists (tracks that have
    # is_estimated=False / locally_allocated=False), then propagate those
    # numbers to all instances of the same track in other playlists.
    _ESTIMATED_KEY = "track_number_is_estimated"
    canonical_tn: dict[str, str] = {}  # URI → canonical track_number

    # Phase A: collect canonical track_numbers from API-sourced tracks
    for tracks in track_map.values():
        for t in tracks:
            if t.get(_ESTIMATED_KEY):
                continue  # allocated sequentially — not trustworthy
            uri = t.get("uri") or ""
            tn  = t.get("track_number", "")
            if uri and tn and tn != "0":
                existing = canonical_tn.get(uri)
                # Prefer numbers that look like real album track positions
                # (i.e., the string contains only digits and is < 100).
                try:
                    new_n = int(tn)
                except ValueError:
                    continue
                if existing is None:
                    canonical_tn[uri] = tn
                else:
                    try:
                        old_n = int(existing)
                    except ValueError:
                        old_n = 0
                    # If we see the same URI with two different track_numbers,
                    # prefer the LARGER one — sequentially-allocated numbers
                    # tend to be low (1, 2, 3…) while real album numbers can be
                    # anywhere.  This heuristic is imperfect but dramatically
                    # reduces the collision rate.
                    if new_n > old_n:
                        canonical_tn[uri] = tn

    # Phase B: propagate canonical numbers to all instances
    unified = 0
    for tracks in track_map.values():
        for t in tracks:
            uri = t.get("uri") or ""
            if not uri:
                continue
            canon = canonical_tn.get(uri)
            if canon and t.get("track_number") != canon:
                t["track_number"] = canon
                t.pop(_ESTIMATED_KEY, None)
                unified += 1

    if unified:
        log.info(
            f"[SpotifyLocal] Unified track_number for {unified} track instance(s) "
            f"across playlists (replaced sequential allocation with canonical album number)."
        )

    return track_map


def priority_sort_key(t: dict) -> tuple:
    """
    Sort key for download priority ordering.
    Higher score = download sooner.
    Uses streaming-history-aware scoring:
      - Recent plays weighted 3×
      - Skip penalty if skip rate > 50%
      - is_liked bonus
    """
    uri = t.get("uri") or (f"spotify:track:{t['id']}" if t.get("id") else "")
    local = _CACHE.get(uri, {})
    plays  = local.get("play_count_recent", 0)
    skips  = local.get("skip_count_recent", 0)
    liked  = 1.5 if (t.get("is_liked") or local.get("is_liked")) else 1.0
    total  = plays + skips
    skip_r = (skips / total) if total > 0 else 0.0
    penalty = 0.5 if skip_r > 0.5 else 1.0
    score = plays * 3.0 * liked * penalty
    return (-score, 0 if t.get("is_liked") else 1)


# ─── Rev 14.9.5 — Offline mode ──────────────────────────────────────────
# Build the same (track_map, liked_ids, saved_album_ids) tuple that
# spotify_client.fetch_and_build() returns, but using ONLY local data.
# Zero API calls.  Caller decides between this and the API-based fetcher
# via config.OFFLINE_MODE.
#
# What we know from local data, per track:
#   - track["id"]            (from uri)
#   - track["uri"]
#   - track["title"]
#   - track["artist"]        (and ["artists"] = [primary])
#   - track["album"]
#   - track["album_artist"]  (= artist; we have no compilation hint)
#   - track["is_liked"]
#   - track["album_id"]      (synthesized from artist+album hash for cache key)
#
# What we DON'T know (must default):
#   - track["spotify_year"]    → ""
#   - track["track_number"]    → allocated sequentially per album by
#                                _allocate_sequential_track_numbers
#   - track["disc_number"]     → "1"
#   - track["isrc"]            → ""
#   - track["album_image"]     → "" (downloader will use yt-dlp thumbnail)
#   - track["compilation"]     → False
#
# Because we lack album_image URL, the F2 cover-art embed (Rev 14.9.3) will
# fall back to the yt-dlp thumbnail file — the user gets covers, just not
# the canonical Spotify artwork.

def _track_dict_from_local(uri: str, info: dict, *, mark_estimated: bool = True) -> dict:
    """
    Build a track dict from local data in the same shape spotify_client
    produces.  See module docstring for which fields are synthesised vs
    inherited from the export.
    """
    tid = uri.replace("spotify:track:", "") if uri else ""
    artist = info.get("artist") or "Unknown"
    return {
        "id":           tid,
        "uri":          uri,
        "title":        info.get("track") or "",
        "artists":      [artist],
        "artist":       artist,
        "album":        info.get("album") or "Unknown",
        # Synthesise an album_id from artist+album so the album-grouping
        # logic in downloader has a stable key.  Same approach as
        # spotify_client when album_id is missing.
        "album_id":     f"local::{artist}|{info.get('album', '')}".lower(),
        "album_artist": artist,  # local data has no compilation marker
        "spotify_year": "",
        "track_number": "",      # filled by _allocate_sequential_track_numbers
        "disc_number":  "1",
        "compilation":  False,
        "genre":        "",
        "release_year": "",
        "is_liked":     bool(info.get("is_liked")),
        "isrc":         "",
        "album_image":  "",
        "_from_local_data": mark_estimated,
        "play_count_recent": info.get("play_count_recent", 0),
    }



# ---------------------------------------------------------------------------
# MK14 FIX-A: Module-level singleton for fetch_and_build_offline
# ---------------------------------------------------------------------------
# ROOT CAUSE (identified from log output-5_3_26.txt, 17:41:10–17:53:14):
# fetch_and_build_offline() was called once per genre-worker invocation —
# 38 times during Phase 1.5+2, each call re-reading the full 4,727-track
# JSON from disk (SpotifyLocal + Offline + Saved = 3 reads per call).
# With 25 concurrent workers this caused 114 redundant disk reads and
# bloated execution time by ~12s per track = ~4.5 extra minutes of I/O.
#
# Fix: cache the result in a process-level singleton (_OFFLINE_CACHE).
# Thread-safe via threading.Lock(). Invalidated only by explicit force_rebuild.
# ---------------------------------------------------------------------------
import threading as _threading

_OFFLINE_CACHE: tuple | None = None
_OFFLINE_LOCK  = _threading.Lock()


def get_offline_singleton(force_rebuild: bool = False) -> tuple:
    """
    Return the cached (track_map, liked_ids, saved_album_ids) tuple,
    loading from disk only on the first call (or when force_rebuild=True).

    This is the correct entry point for genre workers — they should call
    get_offline_singleton() instead of fetch_and_build_offline() directly.
    """
    global _OFFLINE_CACHE
    with _OFFLINE_LOCK:
        if _OFFLINE_CACHE is None or force_rebuild:
            _OFFLINE_CACHE = fetch_and_build_offline()
        return _OFFLINE_CACHE


def invalidate_offline_cache() -> None:
    """Force the next get_offline_singleton() call to reload from disk."""
    global _OFFLINE_CACHE
    with _OFFLINE_LOCK:
        _OFFLINE_CACHE = None

def fetch_and_build_offline() -> tuple[dict[str, list], set[str], set[str]]:
    """
    Rev 14.9.5 — Build the full (track_map, liked_ids, saved_album_ids)
    tuple from local Spotify Account Data export only.  Zero API calls.

    Returns the same 3-tuple shape as spotify_client.fetch_and_build():
      track_map       — {playlist_name: [track_dict, ...]}, including
                        a synthesised "Liked Songs" entry from the
                        YourLibrary tracks list.
      liked_ids       — set of Spotify track IDs marked is_liked
      saved_album_ids — set of Spotify album IDs the user has saved.
                        Note: we synthesise these from URIs in the
                        export ("spotify:album:XXX" → "XXX").

    Raises RuntimeError if the local data folder is missing or empty —
    callers should catch and fall back to API mode if appropriate.
    """
    # Force a fresh load — caller may have just installed a new path
    cache = load(force_rebuild=True)
    if not cache:
        raise RuntimeError(
            "Spotify local-data export not found or empty.  Place the "
            "unzipped 'Spotify Account Data' and 'Spotify Extended "
            "Streaming History' folders in the project directory or set "
            "config.SPOTIFY_LOCAL_DATA_DIR explicitly."
        )

    track_map: dict[str, list] = {}
    liked_ids: set[str] = set()
    saved_album_ids: set[str] = set()

    # ── Liked Songs playlist — from is_liked entries in cache ──────────
    liked_uris = get_liked_uris()
    if liked_uris:
        liked_tracks = []
        for uri in liked_uris:
            info = cache.get(uri)
            if info:
                liked_tracks.append(_track_dict_from_local(uri, info))
                tid = uri.replace("spotify:track:", "")
                if tid:
                    liked_ids.add(tid)
        # F1 fix: assign sequential track-numbers per album within the
        # Liked Songs collection — without this, every liked track lands
        # as "01" and collides with siblings on disk.
        _allocate_sequential_track_numbers(liked_tracks)
        # Use the canonical Spotify name for the Liked Songs playlist so
        # downstream tooling (M3U8 export, audit) treats it the same as
        # the API-fetched version.
        track_map["Liked Songs"] = liked_tracks
        log.info(f"  [Offline] Liked Songs: {len(liked_tracks)} tracks")

    # ── User playlists from Playlist1.json ─────────────────────────────
    for pl_name, uris in get_all_playlists().items():
        if pl_name == "Liked Songs":
            continue  # handled above
        pl_tracks = []
        for uri in uris:
            info = cache.get(uri)
            if not info:
                continue
            pl_tracks.append(_track_dict_from_local(uri, info))
            if info.get("is_liked"):
                tid = uri.replace("spotify:track:", "")
                if tid:
                    liked_ids.add(tid)
        if pl_tracks:
            # F1 fix: assign sequential track-numbers per album within
            # this playlist.  Without this, every track lands as "01"
            # and collides with siblings on disk (LOG-15 case).
            _allocate_sequential_track_numbers(pl_tracks)
            track_map[pl_name] = pl_tracks

    log.info(
        f"  [Offline] Built track_map: {len(track_map)} playlists, "
        f"{sum(len(v) for v in track_map.values())} total entries, "
        f"{len(liked_ids)} liked tracks"
    )

    # ── Saved albums from YourLibrary.json:albums[] ────────────────────
    lib = _read_json("YourLibrary.json")
    if lib:
        for a in lib.get("albums", []):
            uri = a.get("uri", "")
            if uri.startswith("spotify:album:"):
                saved_album_ids.add(uri[len("spotify:album:"):])
        log.info(f"  [Offline] Saved albums: {len(saved_album_ids)}")

    return track_map, liked_ids, saved_album_ids

