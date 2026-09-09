"""
context_builder.py  —  Rich Context Assembly for AI Genre Classification  (MK12)
==================================================================================
Before asking Claude Sonnet (or GPT-4o) to classify a track's genre, we
assemble every piece of evidence we already have — from five data sources —
into a structured context block that the AI can reason over.

This transforms a bare "Artist: X, Title: Y → what's the genre?" prompt into
an evidence-rich brief like:

    Artist:  Brasstracks
    Title:   Golden Ticket (feat. Masego & Common)
    Album:   Golden Ticket (2017)
    Liked:   Yes   Priority: 18.4 (frequently played, never skipped)
    Device:  /storage/4A21-0000/Music/Brasstracks/01 Golden Ticket.flac [FLAC]

    Library context:
    • Genre cache:  no entry (this is why we're asking)
    • Masego  → Neo Soul       (12 library tracks, confident)
    • Common  → Conscious Hip-Hop (8 library tracks, confident)
    • Brasstracks → Jazz-Hop  (graph inferred, score 4.2)
    • Genre graph vote: Jazz-Hop (4 neighbors)

Without context, AI defaults to "Hip-Hop" for ~40% of edge cases.
With this context, it can infer "Jazz-Hop" from Masego+Common collaborations
even if Brasstracks itself is unknown.

Data sources used
-----------------
  1. genre_cache         (.genre_cache.json)            — existing genre/year per track
  2. spotify_metadata    (.spotify_metadata_cache.json) — album/year/liked/track_number
  3. streaming_scores    (.streaming_scores_cache.json) — plays/skip_rate/priority_score
  4. genre_graph         (genre_graph.py)               — neighbor-artist genre inference
  5. device_inventory    (device_inventory.py)          — format/path/duplicates on device

Public API
----------
    build_context(artist, title, album="", year="", spotify_id="") -> ContextBlock

    ContextBlock.as_prompt_block() -> str     # formatted evidence block for the prompt
    ContextBlock.to_dict()         -> dict    # structured dict for logging/caching
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg(attr, default=None):
    try:
        import config as _c
        return getattr(_c, attr, default)
    except Exception:
        return default


def _base_dir() -> pathlib.Path:
    return pathlib.Path(_cfg("BASE_DIR", pathlib.Path(".")))


# ---------------------------------------------------------------------------
# Lazy-loaded cache singletons (loaded once per process, then reused)
# ---------------------------------------------------------------------------

_genre_cache:      dict | None = None
_spotify_meta:     dict | None = None
_streaming_scores: dict | None = None
_graph_instance               = None   # GenreGraph | None
_graph_built_at: float        = 0.0


def _load_genre_cache() -> dict:
    global _genre_cache
    if _genre_cache is None:
        try:
            p = pathlib.Path(_cfg("GENRE_CACHE_FILE", _base_dir() / ".genre_cache.json"))
            _genre_cache = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _genre_cache = {}
    return _genre_cache


def _load_spotify_meta() -> dict:
    global _spotify_meta
    if _spotify_meta is None:
        try:
            p = _base_dir() / ".spotify_metadata_cache.json"
            _spotify_meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _spotify_meta = {}
    return _spotify_meta


def _load_streaming_scores() -> dict:
    global _streaming_scores
    if _streaming_scores is None:
        try:
            p = _base_dir() / ".streaming_scores_cache.json"
            _streaming_scores = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _streaming_scores = {}
    return _streaming_scores


def _get_graph():
    """Load/build the genre graph. Cached for 30 minutes."""
    global _graph_instance, _graph_built_at
    if _graph_instance is not None and (time.time() - _graph_built_at) < 1800:
        return _graph_instance
    try:
        import genre_graph as gg
        from spotify_client import get_spotify_client as _gsc
        import spotify_local_data as _sld
        # MK15-HF1: use the singleton — fetch_and_build_offline() was being
        # called here every time context_builder ran (once per worker),
        # causing the 39× SpotifyLocal reload pattern seen in the live log.
        try:
            track_map, _, _ = _sld.get_offline_singleton()
            _graph_instance = gg.build_graph(track_map)
            _graph_built_at = time.time()
        except Exception:
            pass
    except Exception as e:
        log.debug(f"[ContextBuilder] Genre graph unavailable: {e}")
    return _graph_instance


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _cache_key(artist: str, title: str) -> str:
    return f"{_norm(artist)}|{_norm(title)}"


def _norm_spotify_key(artist: str, title: str) -> str:
    """Match against spotify_metadata by (artist, title) pair."""
    a = _norm(artist)
    t = _norm(title)
    return f"{a}|{t}"


# ---------------------------------------------------------------------------
# Data-source lookup helpers
# ---------------------------------------------------------------------------

def _lookup_genre_cache(artist: str, title: str) -> dict:
    """Return {'genre': str, 'year': str} or {} if not cached."""
    cache = _load_genre_cache()
    key   = _cache_key(artist, title)
    return cache.get(key, {})


def _lookup_spotify_meta(artist: str, title: str, spotify_id: str = "") -> dict:
    """Return Spotify metadata dict or {}."""
    meta = _load_spotify_meta()
    # Try by ID first
    if spotify_id and spotify_id in meta:
        return meta[spotify_id]
    # Scan for artist+title match
    a_n = _norm(artist)
    t_n = _norm(title)
    for v in meta.values():
        if isinstance(v, dict):
            if _norm(v.get("artist", "")) == a_n and _norm(v.get("title", "")) == t_n:
                return v
    return {}


def _lookup_streaming_score(artist: str, title: str, spotify_id: str = "") -> dict:
    """Return streaming score dict or {}."""
    scores = _load_streaming_scores()
    # Try by spotify URI
    if spotify_id:
        uri = f"spotify:track:{spotify_id}"
        if uri in scores:
            return scores[uri]
    # Scan by artist+title
    a_n = _norm(artist)
    t_n = _norm(title)
    for v in scores.values():
        if isinstance(v, dict):
            if _norm(v.get("artist", "")) == a_n and _norm(v.get("title", "")) == t_n:
                return v
    return {}


def _lookup_genre_cache_for_artist(artist: str, n: int = 5) -> list[dict]:
    """Return up to n known tracks by this artist from the genre cache."""
    cache = _load_genre_cache()
    a_n   = _norm(artist)
    hits  = []
    for key, val in cache.items():
        if "|" in key and key.split("|", 1)[0] == a_n:
            hits.append({"title": key.split("|", 1)[1], **val})
    return hits[:n]


def _infer_from_graph(artist: str) -> tuple[str, float]:
    """
    Query the genre graph for artist's inferred genre.
    Returns (genre, confidence_score) or ("", 0.0).
    """
    try:
        graph = _get_graph()
        if graph is None:
            return "", 0.0
        import genre_graph as gg
        genre = gg.infer_genre_for_artist(graph, artist)
        if genre:
            return genre, 1.0   # graph score is already weighted internally
    except Exception as e:
        log.debug(f"[ContextBuilder] Graph inference failed: {e}")
    return "", 0.0


def _collaborator_genres(artist: str, title: str, n: int = 5) -> list[dict]:
    """
    Extract featuring artists from the title, look up their genres.
    Returns list of {artist, genre, track_count, source}.
    """
    # Extract feat. artists from title: "(feat. X, Y)" or "(ft. X)"
    feat_m = re.search(r"\((?:feat|ft)\.?\s+(.+?)\)", title, re.IGNORECASE)
    all_artists = [artist]
    if feat_m:
        feat_str = feat_m.group(1)
        all_artists += [a.strip() for a in re.split(r"[,&]", feat_str)]

    cache = _load_genre_cache()
    results = []
    for a in all_artists[:n]:
        a_n  = _norm(a)
        hits = [(k, v) for k, v in cache.items()
                if "|" in k and k.split("|")[0] == a_n]
        if hits:
            genres = [v.get("genre", "") for _, v in hits if v.get("genre")]
            if genres:
                from collections import Counter
                modal_genre, count = Counter(genres).most_common(1)[0]
                results.append({
                    "artist":      a,
                    "genre":       modal_genre,
                    "track_count": len(hits),
                    "source":      "genre_cache",
                })
    return results


def _device_file_info(artist: str, title: str, album: str = "") -> dict:
    """
    Look up device file info from the device inventory.
    Returns {found, path, storage, ext, duplicates, album_context}.

    MK14 FIX-B: NEVER call di.refresh() inside a genre worker thread.
    ROOT CAUSE: With 25 concurrent workers, each calling di.refresh() →
    subprocess.run('adb devices'), the ADB daemon is overwhelmed with
    concurrent subprocess calls and returns "no devices" (log shows 38
    "ADB device not connected" warnings despite the device being connected).
    Fix: workers only READ from the already-loaded in-memory index.
    di.refresh() must only be called at pipeline startup (in main.py preflight),
    never inside a worker thread.
    """
    try:
        import device_inventory as di
        # MK14: READ-ONLY — never call refresh() from a worker thread.
        # If the index isn't loaded (no preflight run), return not found
        # rather than hammering ADB from 25 concurrent subprocesses.
        if not di.is_refreshed():
            return {"found": False, "_reason": "index_not_preloaded"}

        hits = di.lookup(artist, title)
        if not hits:
            return {"found": False}

        best = hits[0]
        dups = di.get_duplicates(best.stem)

        album_ctx: list[str] = []
        if album:
            siblings = di.get_album_context(album, artist)
            album_ctx = [s.filename for s in siblings if s.path != best.path][:8]

        return {
            "found":         True,
            "path":          best.path,
            "storage":       best.storage,
            "ext":           best.ext,
            "copies":        len(dups),
            "all_formats":   sorted(set(d.ext for d in dups)),
            "album_context": album_ctx,
        }
    except Exception as e:
        log.debug(f"[ContextBuilder] Device lookup failed: {e}")
        return {"found": False}


# ---------------------------------------------------------------------------
# Context block
# ---------------------------------------------------------------------------

@dataclass
class ContextBlock:
    artist:              str
    title:               str
    album:               str   = ""
    year:                str   = ""
    spotify_id:          str   = ""
    cached_genre:        str   = ""          # from genre_cache (empty = unknown)
    cached_year:         str   = ""
    spotify_album:       str   = ""
    spotify_year:        str   = ""
    is_liked:            bool  = False
    track_number:        str   = ""
    play_count:          int   = 0
    skip_rate:           float = 0.0
    priority_score:      float = 0.0
    never_skipped:       bool  = False
    graph_genre:         str   = ""
    graph_score:         float = 0.0
    collaborators:       list  = field(default_factory=list)
    artist_known_tracks: list  = field(default_factory=list)
    device_found:        bool  = False
    device_path:         str   = ""
    device_storage:      str   = ""
    device_ext:          str   = ""
    device_copies:       int   = 0
    device_formats:      list  = field(default_factory=list)
    album_context:       list  = field(default_factory=list)

    def as_prompt_block(self) -> str:
        """
        Format all evidence as a structured text block for inclusion in a
        Claude / GPT-4o prompt.  Concise — fits in a few hundred tokens.
        """
        lines: list[str] = []

        # Track identity
        lines.append(f"Artist:  {self.artist}")
        lines.append(f"Title:   {self.title}")
        if self.album or self.spotify_album:
            lines.append(f"Album:   {self.album or self.spotify_album}")
        yr = self.year or self.cached_year or self.spotify_year
        if yr:
            lines.append(f"Year:    {yr}")

        # Listening behaviour
        behaviours: list[str] = []
        if self.is_liked:
            behaviours.append("Liked ♥")
        if self.play_count:
            behaviours.append(f"played {self.play_count}×")
        if self.skip_rate > 0:
            behaviours.append(f"skip rate {self.skip_rate:.0%}")
        if self.never_skipped and self.play_count > 1:
            behaviours.append("never skipped")
        if self.priority_score > 0:
            behaviours.append(f"priority {self.priority_score:.1f}")
        if behaviours:
            lines.append(f"Usage:   {' | '.join(behaviours)}")

        # Device presence
        if self.device_found:
            fmt_label = self.device_ext.lstrip(".").upper() if self.device_ext else "?"
            lines.append(
                f"Device:  {self.device_path} [{fmt_label}]"
                + (f" + {self.device_copies - 1} duplicate(s)" if self.device_copies > 1 else "")
            )
        else:
            lines.append("Device:  not found on Walkman")

        lines.append("")
        lines.append("── Library evidence ──────────────────────────────────────")

        # Cached genre
        if self.cached_genre:
            lines.append(f"• Already cached:  {self.cached_genre!r}"
                         + (" — verify/refine if possible" if self.cached_genre.lower() in
                            ("hip-hop", "hip hop", "r&b", "pop", "rock", "soul", "electronic")
                            else " (may be correct already)"))
        else:
            lines.append("• Genre cache:   not yet classified (this is why we're asking)")

        # Graph inference
        if self.graph_genre:
            lines.append(
                f"• Genre graph:   {self.graph_genre!r} "
                f"(inferred from co-artist network, score {self.graph_score:.1f})"
            )

        # Collaborators
        if self.collaborators:
            lines.append("• Collaborator genres from this library:")
            for c in self.collaborators:
                lines.append(
                    f"    {c['artist']:<22} → {c['genre']}"
                    f"  ({c['track_count']} tracks in library)"
                )

        # Artist's other known tracks
        if self.artist_known_tracks:
            genre_examples = [f"{t['genre']!r}" for t in self.artist_known_tracks if t.get("genre")][:4]
            if genre_examples:
                lines.append(f"• Artist in cache: {', '.join(genre_examples)}")

        # Album sibling tracks
        if self.album_context:
            lines.append(f"• Album siblings on device: {', '.join(self.album_context[:5])}")

        lines.append("")
        lines.append(
            "Given ALL this evidence, classify into the single most specific, "
            "accurate genre. Return only: {\"genre\": \"string\"}"
        )

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Structured dict for logging."""
        return {
            "artist": self.artist, "title": self.title,
            "cached_genre": self.cached_genre, "graph_genre": self.graph_genre,
            "collaborators": [c["artist"] for c in self.collaborators],
            "device_found": self.device_found, "device_ext": self.device_ext,
            "play_count": self.play_count, "is_liked": self.is_liked,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_context(
    artist:     str,
    title:      str,
    album:      str = "",
    year:       str = "",
    spotify_id: str = "",
) -> ContextBlock:
    """
    Assemble a ContextBlock for the given track by querying all 5 data sources.
    Fast (all data is in-memory after first load); safe to call for every track.
    """
    # 1. Genre cache
    gc = _lookup_genre_cache(artist, title)

    # 2. Spotify metadata
    sm = _lookup_spotify_meta(artist, title, spotify_id)

    # 3. Streaming score
    ss = _lookup_streaming_score(artist, title, spotify_id)

    # 4. Genre graph
    graph_genre, graph_score = _infer_from_graph(artist)

    # 5. Collaborator genres (from title feat. parsing)
    collabs = _collaborator_genres(artist, title)

    # 6. Artist's own cached tracks (context for unknown artists)
    artist_tracks = _lookup_genre_cache_for_artist(artist, n=4)

    # 7. Device inventory
    dev = _device_file_info(artist, title, album or sm.get("album", ""))

    ctx = ContextBlock(
        artist         = artist,
        title          = title,
        album          = album or sm.get("album", ""),
        year           = year,
        spotify_id     = spotify_id or sm.get("id", ""),
        cached_genre   = gc.get("genre", ""),
        cached_year    = gc.get("year", ""),
        spotify_album  = sm.get("album", ""),
        spotify_year   = sm.get("spotify_year", ""),
        is_liked       = bool(sm.get("is_liked", False)),
        track_number   = str(sm.get("track_number", "")),
        play_count     = int(ss.get("play_count", 0)),
        skip_rate      = float(ss.get("skip_rate", 0.0)),
        priority_score = float(ss.get("priority_score", 0.0)),
        never_skipped  = bool(ss.get("never_skipped", False)),
        graph_genre    = graph_genre,
        graph_score    = graph_score,
        collaborators  = collabs,
        artist_known_tracks = artist_tracks,
        device_found   = dev.get("found", False),
        device_path    = dev.get("path", ""),
        device_storage = dev.get("storage", ""),
        device_ext     = dev.get("ext", ""),
        device_copies  = dev.get("copies", 0),
        device_formats = dev.get("all_formats", []),
        album_context  = dev.get("album_context", []),
    )

    log.debug(
        f"[ContextBuilder] {artist!r} — {title!r}: "
        f"cached={ctx.cached_genre or '—'}, graph={ctx.graph_genre or '—'}, "
        f"collabs={len(ctx.collaborators)}, device={'✓' if ctx.device_found else '✗'}"
    )
    return ctx
