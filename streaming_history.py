"""
streaming_history.py  —  Spotify Extended Streaming History integration
========================================================================
Parses the Spotify Extended Streaming History data export (the lifetime
JSON files, not the 1-year "recent" data) and makes it available to the
rest of the pipeline via a single lazy-loaded cache.

What this module provides
--------------------------
1. Priority scoring for the download queue
   Tracks you've played many times recently rise to the top of the
   missing-track download queue, replacing the current FIFO order.

2. POPM / star-rating seeding
   High-play-count + zero-skip-rate tracks get POPM=255 stamped in the
   audit pass even if they're not in the current Liked Songs list.
   (Liked Songs gives binary liked/not-liked; history gives real usage.)

3. Auto-generated M3U8 smart playlists written to the Walkman
   Four playlists are generated from history data and written alongside
   the standard Spotify playlist M3U8 files:
     • 🔥 Most Played Ever     — all-time top 50 by play count
     • 📅 Last 12 Months       — tracks with ≥5 genuine plays in the past year
     • ✅ Never Skipped         — played ≥10× with 0% skip rate
     • 🌀 Rediscovery           — 20+ plays all-time but 0 plays last 12 months

4. Streaming scores JSON cache (.streaming_scores_cache.json)
   Written to the project folder alongside the other cache files.
   Re-parsed from the raw export only when the export is newer.

Priority score formula
----------------------
  score = (all_time_plays × 1.0) + (last_12mo_plays × 2.0) + (last_6mo_plays × 3.0)
  Skip penalty: if skip_rate > 50%, score × 0.5

This weights recency strongly (a track you played 28× this month beats
one you played 118× back in 2019) while still honouring deep familiarity.

Usage
-----
  import streaming_history as sh

  # Load (or rebuild from raw export)
  scores = sh.load()

  # Download priority: sort missing track list by score descending
  missing_sorted = sh.sort_by_priority(missing_tracks, scores)

  # POPM candidates: tracks that deserve a star even if not in Liked Songs
  popm_uris = sh.popm_candidates(scores, min_plays=20, max_skip_rate=0.1)

  # Smart playlist track-URI lists for M3U8 generation
  playlists = sh.smart_playlists(scores)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HISTORY_DIR  = pathlib.Path(os.path.expanduser("~/Downloads")) / "Spotify Extended Streaming History"
_CACHE_FILE   = config.BASE_DIR / ".streaming_scores_cache.json"

# Also accept export directly in the project folder
_HISTORY_DIR_ALT = config.BASE_DIR / "Spotify Extended Streaming History"

# ---------------------------------------------------------------------------
# Score parameters
# ---------------------------------------------------------------------------
_GENUINE_MS       = 30_000       # ≥30 s = genuine play (Spotify's own definition)
_SKIP_PENALTY_THR = 0.50         # skip rate above this → halve the score
_W_ALL            = 1.0          # weight for all-time plays
_W_12M            = 2.0          # weight for last-12-month plays
_W_6M             = 3.0          # weight for last-6-month plays

# Smart playlist thresholds
_PL_MIN_PLAYS_MOST    = 30       # "Most Played": ≥30 all-time plays
_PL_MOST_LIMIT        = 50       # top N tracks
_PL_MIN_RECENT_12M    = 5        # "Last 12 Months": ≥5 plays in past year
_PL_RECENT_LIMIT      = 50
_PL_MIN_PLAYS_NEVER   = 10       # "Never Skipped": ≥10 plays, 0% skip
_PL_NEVER_LIMIT       = 100
_PL_MIN_PLAYS_REDISCO = 20       # "Rediscovery": ≥20 all-time, 0 in last 12 mo
_PL_REDISCO_LIMIT     = 30


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_history_dir() -> Optional[pathlib.Path]:
    """Return the first existing streaming-history folder, or None."""
    for candidate in (_HISTORY_DIR, _HISTORY_DIR_ALT, config.BASE_DIR):
        files = list(candidate.glob("Streaming_History_Audio_*.json")) if candidate.exists() else []
        if files:
            return candidate
    return None


def _cutoff(months: int) -> datetime:
    now = datetime.now(tz=timezone.utc)
    return now - timedelta(days=months * 30.44)


def _parse_raw(history_dir: pathlib.Path) -> dict:
    """
    Parse all Streaming_History_Audio_*.json files and return a raw
    aggregation dict keyed by Spotify URI.
    """
    cutoff_12m = _cutoff(12)
    cutoff_6m  = _cutoff(6)

    track_data: dict[str, dict] = defaultdict(lambda: {
        "artist":    "",
        "title":     "",
        "album":     "",
        "count":     0,      # genuine plays (≥30 s)
        "ms":        0,      # total ms of genuine plays
        "skips":     0,      # plays < 30 s
        "raw_plays": 0,      # total plays (genuine + skips)
        "recent_12": 0,
        "recent_6":  0,
    })

    files = sorted(history_dir.glob("Streaming_History_Audio_*.json"))
    if not files:
        return {}

    total_records = 0
    for f in files:
        try:
            records = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"  [StreamingHistory] Could not parse {f.name}: {e}")
            continue
        total_records += len(records)
        for r in records:
            # Skip podcasts / audiobooks (no track name)
            if not r.get("master_metadata_track_name"):
                continue
            uri = r.get("spotify_track_uri", "")
            if not uri:
                continue
            ms = r.get("ms_played", 0) or 0
            td = track_data[uri]
            td["artist"] = r.get("master_metadata_album_artist_name") or td["artist"]
            td["title"]  = r.get("master_metadata_track_name")        or td["title"]
            td["album"]  = r.get("master_metadata_album_album_name")  or td["album"]
            td["raw_plays"] += 1

            if ms < _GENUINE_MS:
                td["skips"] += 1
                continue  # do NOT count skips in play stats

            td["count"] += 1
            td["ms"]    += ms
            try:
                ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                if ts >= cutoff_12m:
                    td["recent_12"] += 1
                if ts >= cutoff_6m:
                    td["recent_6"]  += 1
            except Exception:
                pass

    log.info(
        f"  [StreamingHistory] Parsed {total_records:,} records "
        f"from {len(files)} file(s) → {len(track_data):,} unique tracks"
    )
    return dict(track_data)


def _build_scores(raw: dict) -> dict:
    """
    Convert raw aggregation into the finalised scores dict that callers use.
    Each entry:
      {
        "artist": str, "title": str, "album": str,
        "play_count": int, "total_ms": int,
        "skip_rate": float,
        "recent_12m": int, "recent_6m": int,
        "priority_score": float,
        "never_skipped": bool,
      }
    """
    out = {}
    for uri, td in raw.items():
        if td["count"] == 0:
            continue  # never genuinely played
        skip_rate = td["skips"] / max(td["raw_plays"], 1)
        base = (
            td["count"]    * _W_ALL +
            td["recent_12"]* _W_12M +
            td["recent_6"] * _W_6M
        )
        priority = base * (0.5 if skip_rate > _SKIP_PENALTY_THR else 1.0)
        out[uri] = {
            "artist":         td["artist"],
            "title":          td["title"],
            "album":          td["album"],
            "play_count":     td["count"],
            "total_ms":       td["ms"],
            "skip_rate":      round(skip_rate, 3),
            "recent_12m":     td["recent_12"],
            "recent_6m":      td["recent_6"],
            "priority_score": round(priority, 1),
            "never_skipped":  skip_rate == 0.0,
        }
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SCORES_CACHE: dict | None = None


def load(force_rebuild: bool = False) -> dict:
    """
    Return the scores dict.  Uses the JSON cache if present and newer than
    any raw history file; rebuilds from raw data otherwise.

    Returns {} (empty dict) gracefully if no history data is available.
    """
    global _SCORES_CACHE
    if _SCORES_CACHE is not None and not force_rebuild:
        return _SCORES_CACHE

    # Find raw data
    history_dir = _find_history_dir()

    # Try loading from cache file first
    if _CACHE_FILE.exists() and not force_rebuild:
        try:
            cache_mtime = _CACHE_FILE.stat().st_mtime
            # Check if any raw file is newer than the cache
            if history_dir:
                newest_raw = max(
                    (f.stat().st_mtime for f in history_dir.glob("Streaming_History_Audio_*.json")),
                    default=0,
                )
                if newest_raw <= cache_mtime:
                    _SCORES_CACHE = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                    log.info(
                        f"  [StreamingHistory] Loaded {len(_SCORES_CACHE):,} track scores "
                        f"from cache ({_CACHE_FILE.name})"
                    )
                    return _SCORES_CACHE
            else:
                # No raw data — use whatever cache we have
                _SCORES_CACHE = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                log.info(
                    f"  [StreamingHistory] Loaded {len(_SCORES_CACHE):,} track scores "
                    f"from cache (no raw data found)"
                )
                return _SCORES_CACHE
        except Exception as e:
            log.debug(f"  [StreamingHistory] Cache load failed: {e}")

    if not history_dir:
        log.debug(
            "  [StreamingHistory] No Extended Streaming History data found. "
            "Download your Spotify data export and place the "
            "'Spotify Extended Streaming History' folder in "
            f"~/Downloads or {config.BASE_DIR}"
        )
        _SCORES_CACHE = {}
        return _SCORES_CACHE

    # Rebuild from raw
    log.info(f"  [StreamingHistory] Building scores from {history_dir} ...")
    raw    = _parse_raw(history_dir)
    scores = _build_scores(raw)

    # Persist cache
    try:
        _CACHE_FILE.write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
        log.info(f"  [StreamingHistory] Cache written → {_CACHE_FILE.name} ({len(scores):,} entries)")
    except Exception as e:
        log.warning(f"  [StreamingHistory] Could not write cache: {e}")

    _SCORES_CACHE = scores
    return scores


def priority_score(uri: str, scores: dict | None = None) -> float:
    """Return the priority score for a Spotify URI (0.0 if not found)."""
    if scores is None:
        scores = load()
    return (scores.get(uri) or {}).get("priority_score", 0.0)


def sort_by_priority(tracks: list[dict], scores: dict | None = None) -> list[dict]:
    """
    Sort a list of track dicts (each with a 'spotify_uri' or 'id' key)
    by priority score descending.  Tracks not in history keep their
    original relative order at the bottom.
    """
    if scores is None:
        scores = load()
    if not scores:
        return tracks  # no history data — leave order unchanged

    def _key(t: dict) -> float:
        uri = t.get("spotify_uri") or t.get("uri") or ""
        if not uri and t.get("id"):
            uri = f"spotify:track:{t['id']}"
        return -priority_score(uri, scores)   # negative for descending sort

    return sorted(tracks, key=_key)


def popm_candidates(
    scores: dict | None = None,
    min_plays: int = 20,
    max_skip_rate: float = 0.15,
) -> set[str]:
    """
    Return a set of Spotify URIs that deserve a POPM=255 star rating based
    on listening history — regardless of whether they're in Liked Songs.

    Criteria (defaults):
      - ≥20 genuine plays
      - skip rate ≤15%
    """
    if scores is None:
        scores = load()
    return {
        uri for uri, v in scores.items()
        if v["play_count"] >= min_plays and v["skip_rate"] <= max_skip_rate
    }


def smart_playlists(scores: dict | None = None) -> dict[str, list[str]]:
    """
    Return a dict of smart playlist name → list of Spotify URIs.
    Intended for M3U8 generation: the caller resolves URIs to device paths.

    Playlists:
      "Most Played Ever"   — top plays all-time
      "Last 12 Months"     — top plays in the past year
      "Never Skipped"      — high plays + 0% skip rate
      "Rediscovery"        — old favourites with no recent plays
    """
    if scores is None:
        scores = load()
    if not scores:
        return {}

    by_score  = sorted(scores, key=lambda u: scores[u]["priority_score"],  reverse=True)
    by_recent = sorted(scores, key=lambda u: scores[u]["recent_12m"],       reverse=True)

    return {
        "Most Played Ever": [
            u for u in by_score
            if scores[u]["play_count"] >= _PL_MIN_PLAYS_MOST
        ][:_PL_MOST_LIMIT],

        "Last 12 Months": [
            u for u in by_recent
            if scores[u]["recent_12m"] >= _PL_MIN_RECENT_12M
        ][:_PL_RECENT_LIMIT],

        "Never Skipped": [
            u for u in by_score
            if scores[u]["never_skipped"] and scores[u]["play_count"] >= _PL_MIN_PLAYS_NEVER
        ][:_PL_NEVER_LIMIT],

        "Rediscovery": [
            u for u in by_score
            if scores[u]["play_count"] >= _PL_MIN_PLAYS_REDISCO
            and scores[u]["recent_12m"] == 0
        ][:_PL_REDISCO_LIMIT],
    }


def lifetime_stats(scores: dict | None = None) -> dict:
    """Return a summary dict of lifetime listening stats for display."""
    if scores is None:
        scores = load()
    if not scores:
        return {}

    total_plays = sum(v["play_count"] for v in scores.values())
    total_ms    = sum(v["total_ms"]   for v in scores.values())
    total_hrs   = total_ms / 3_600_000

    artists: dict[str, float] = defaultdict(float)
    for v in scores.values():
        artists[v["artist"]] += v["total_ms"]

    top_artists = sorted(artists.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "unique_tracks":  len(scores),
        "total_plays":    total_plays,
        "total_hours":    round(total_hrs, 1),
        "total_days":     round(total_hrs / 24, 1),
        "top_artists":    [{"artist": a, "hours": round(ms/3_600_000, 1)} for a, ms in top_artists],
    }
