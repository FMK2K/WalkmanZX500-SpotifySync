"""
genre_graph.py — MK17 Phase B (M3)
====================================
Graph-based genre inference for sparse-data artists.

The problem (LOG_16_1 + user direction Apr 30):
    Alfa Kat is an Amapiano/Bacardi artist.  His single track in the
    user's library was tagged "Abstract Hip-Hop" by Last.fm.  The
    Rev 14.9.5 album-then-artist consolidation has only one data point,
    so modal-of-1 picks "Abstract Hip-Hop" — and the entire Alfa Kat
    album lands on the Walkman tagged with the wrong subgenre.

The fix: when an artist has fewer than GENRE_ARTIST_MIN_TRACKS in the
library AND has no confident album-level signal, treat their genre as
unknown and infer it from the GRAPH.

Graph construction:
    Nodes = artists.
    Heavy edges (weight = GENRE_GRAPH_FEATURE_WEIGHT, default 3) between
        every pair of artists who appear together in a track's `artists`
        list.  This captures features/collabs, which are the strongest
        genre signal — Alfa Kat's collabs are with Toss, Sayfar, Kabza
        de Small, etc., all firmly Amapiano.
    Light edges (weight = GENRE_GRAPH_PLAYLIST_WEIGHT, default 1)
        between artists who appear in the same playlist.  Captures
        curatorial signal — a Liked Songs entry placed near other
        Amapiano tracks votes weakly for Amapiano.

Inference:
    For each query artist, walk the graph up to GENRE_GRAPH_HOPS hops.
    Tally weighted votes from CONFIDENT neighbors only — a neighbor is
    confident when it has >= GENRE_ARTIST_MIN_TRACKS in the library AND
    a non-broad modal genre.  This is the "strict" setting per the
    user's Apr 30 confirmation; permissive variants are commented out.
    Pick the highest-voted genre as the inferred result.

This module is pure stdlib (no NetworkX dependency) — the graphs
involved are small (a few thousand artists) and we only walk 1-2 hops,
so a hand-rolled adjacency dict is faster than the import overhead.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from typing import Iterable

import logging
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Module configuration helpers (read fresh on each call so config edits
# during dev don't require a restart)
# ──────────────────────────────────────────────────────────────────────────
def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _norm_artist(name: str) -> str:
    """Lowercase + strip for use as graph node key."""
    return (name or "").strip().lower()


import re as _re_feat

_FEAT_BLOCK_RE = _re_feat.compile(
    r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]+)[\)\]]"
    r"|(?:^|\s)(?:feat\.?|ft\.?|featuring)\s+(.+)$",
    _re_feat.IGNORECASE,
)
_FEAT_SPLIT_RE = _re_feat.compile(r"\s*(?:,|&|\band\b|\bx\b|/)\s*", _re_feat.IGNORECASE)


def _extract_feat_artists(title: str) -> list[str]:
    """
    Pull featured-artist names out of a track title's "(feat. X, Y)" /
    "(with X)" / "ft. X" / "X feat. Y" block, since not every track dict
    carries a real multi-artist list. Best-effort string parsing — good
    enough to seed graph edges, not meant to be authoritative metadata.
    """
    if not title:
        return []
    m = _FEAT_BLOCK_RE.search(title)
    if not m:
        return []
    block = m.group(1) or m.group(2) or ""
    names = [n.strip(" .") for n in _FEAT_SPLIT_RE.split(block) if n.strip(" .")]
    return names


# Broad-genre set used to filter "low-information" votes.  Mirrors the
# walkman_sync._BROAD_GENRES list — re-imported lazily to avoid a hard
# circular dep, but with a defensible default.
_DEFAULT_BROAD_GENRES = frozenset({
    "pop", "rock", "hip-hop", "hip hop", "hiphop", "rap", "r&b",
    "rnb", "soul", "jazz", "electronic", "world", "alternative",
    "indie", "folk", "country", "metal", "punk",
})


def _broad_genres() -> frozenset:
    try:
        from walkman_sync import WalkmanSync as _WS
        return frozenset(getattr(_WS, "_BROAD_GENRES", _DEFAULT_BROAD_GENRES))
    except Exception:
        return _DEFAULT_BROAD_GENRES


# ══════════════════════════════════════════════════════════════════════════
# Graph builder
# ══════════════════════════════════════════════════════════════════════════
class GenreGraph:
    """
    Adjacency-dict graph keyed on normalised artist names.

    self.adjacency[artist_a] = {artist_b: weight_a_to_b, ...}

    The graph is undirected — we always add both (a -> b) and (b -> a)
    edges with the same weight.  When the same artist pair appears in
    multiple sources (a feature AND shared playlists), weights ADD
    (so a pair that features together AND shares 2 playlists has
    weight 3 + 1 + 1 = 5).  Self-loops are skipped.
    """

    def __init__(self) -> None:
        self.adjacency: dict[str, dict[str, int]] = defaultdict(dict)
        # Per-artist track count and modal genre (computed once at build time).
        self.artist_tracks: dict[str, int] = defaultdict(int)
        self.artist_modal: dict[str, str] = {}

    # ─── builder API ──────────────────────────────────────────────────
    def add_edge(self, a: str, b: str, weight: int) -> None:
        a_norm = _norm_artist(a)
        b_norm = _norm_artist(b)
        if not a_norm or not b_norm or a_norm == b_norm:
            return
        self.adjacency[a_norm][b_norm] = (
            self.adjacency[a_norm].get(b_norm, 0) + weight
        )
        self.adjacency[b_norm][a_norm] = (
            self.adjacency[b_norm].get(a_norm, 0) + weight
        )

    def neighbors(self, artist: str) -> dict[str, int]:
        """Return dict of {neighbor_artist: weight}."""
        return dict(self.adjacency.get(_norm_artist(artist), {}))


# ──────────────────────────────────────────────────────────────────────────
# Public builder — call this once per sync after enrichment
# ──────────────────────────────────────────────────────────────────────────
def build_graph(track_map: dict | None = None,
                tracks: Iterable[dict] | None = None,
                _override_purity: "float | None" = None) -> GenreGraph:
    # MK20 NG1: _override_purity for starvation-fallback purity relaxation
    """
    Build a co-occurrence graph from either:
      - a track_map (dict[playlist_name → list[track_dict]]) — the shape
        produced by walkman_sync._fetch_from_spotify; OR
      - a flat tracks iterable.

    Pass track_map when you have it (so playlist-edges can be added);
    pass tracks alone if you only have the flat list (then only feature
    edges will be inferred — playlist-edges will be empty).

    MK39 Phase I-A5 (Council Emergency Directive 2026-05-10): if
    config.GENRE_GRAPH_ENABLED is False, return an empty GenreGraph
    immediately. Council disabled the graph by default until its
    construction logic can be fixed and validated. Downstream code
    already handles empty graphs gracefully via
    mk39_phase_I_containment.genre_graph_is_empty.
    """
    # Phase I-A5 master kill-switch
    try:
        import config as _cfg_p1
        if not getattr(_cfg_p1, "GENRE_GRAPH_ENABLED", False):
            log.debug("  [GenreGraph] Disabled via config.GENRE_GRAPH_ENABLED=False "
                       "(Council 2026-05-10 §A5). Returning empty graph.")
            return GenreGraph()
    except Exception:
        pass

    feature_w  = float(_cfg("GENRE_GRAPH_FEATURE_WEIGHT", 3))
    playlist_w = float(_cfg("GENRE_GRAPH_PLAYLIST_WEIGHT", 1))

    g = GenreGraph()
    broad = _broad_genres()

    # Flatten track_map if given
    flat_tracks: list[dict] = []
    if track_map:
        seen: set = set()
        for plist_name, plist_tracks in (track_map or {}).items():
            for t in plist_tracks or []:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id") or (t.get("artist", ""), t.get("title", ""))
                if tid in seen:
                    continue
                seen.add(tid)
                flat_tracks.append(t)
    if tracks is not None:
        flat_tracks.extend([t for t in tracks if isinstance(t, dict)])

    # ── Feature edges + per-artist tally ──────────────────────────────
    # 2026-09-04: offline-mode track dicts (spotify_local_data.py, built
    # from the GDPR export) carry only a single primary artist —
    # confirmed live: 0 of 5,484 offline tracks had a multi-artist
    # "artists" list, so this pass produced 0 feature edges every time
    # under OFFLINE_MODE, no matter how many confident cache entries fed
    # in. Fall back to parsing "(feat. X, Y)" / "(with X)" / "ft. X" out
    # of the title when the track itself only names one artist — the
    # collab signal is still there, just embedded in the title string
    # rather than a separate field.
    for t in flat_tracks:
        artists_list = t.get("artists") or [t.get("artist", "")]
        clean_artists = [a for a in artists_list if a]
        if len(clean_artists) <= 1:
            clean_artists = clean_artists + _extract_feat_artists(t.get("title", ""))

        # Per-artist tally — every artist in the list gets +1 track count
        for a in clean_artists:
            g.artist_tracks[_norm_artist(a)] += 1

        # All-pairs heavy edges between feature collaborators
        for i in range(len(clean_artists)):
            for j in range(i + 1, len(clean_artists)):
                g.add_edge(clean_artists[i], clean_artists[j], feature_w)

    # ── Compute per-artist modal genre across their tracks ────────────
    by_artist: dict[str, list[str]] = defaultdict(list)
    for t in flat_tracks:
        primary = t.get("artist") or (t.get("artists") or [""])[0]
        primary_norm = _norm_artist(primary)
        genre = (t.get("genre") or "").strip()
        if primary_norm and genre and genre.lower() not in broad:
            by_artist[primary_norm].append(genre)

    for a, gs in by_artist.items():
        if gs:
            g.artist_modal[a] = Counter(gs).most_common(1)[0][0]

    # ── Playlist-shared light edges ───────────────────────────────────
    # MK17 Patch 8 (post-LOG_17 fix): skip playlists with more than
    # max_plist_size tracks.  "Liked Songs" with 2271 tracks tells the
    # graph nothing about genre similarity — every artist appears in it,
    # so the playlist generates ~5.1M weight-1 edges that drown out the
    # targeted weight-3 collab signal.  LOG_17 showed 776/805 sparse
    # rescues collapsing to "Afrobeats" because of this.  After the
    # cap, only smaller curated playlists contribute genre signal.
    #
    # MK8 Fix (PR-GenreGraph): THREE additional changes:
    #
    # 1. EXPLICIT Liked Songs exclusion by NAME (in addition to the size cap).
    #    User direction: "excluding the liked songs".  Liked Songs is excluded
    #    unconditionally regardless of size — even if someone has only 50 liked
    #    tracks, it's a personal "heard it" list, not a genre-curated playlist.
    #
    # 2. GENRE PURITY WEIGHTING: instead of applying playlist_w uniformly to
    #    every playlist, scale the edge weight by the genre purity of the
    #    playlist.  A playlist that is 90% Afrobeats gets purity=0.9 and adds
    #    playlist_w * 0.9 edges between all artists in it.  A mixed playlist
    #    (30% Afrobeats, 25% Hip-Hop, 20% R&B, ...) gets purity≈0.30 for
    #    Afrobeats — weak signal that barely moves any artist's genre vote.
    #    This is the core request: "edges based on common features AND playlists".
    #    Only playlists with a clear genre majority produce strong playlist edges.
    #    Minimum purity threshold (GENRE_GRAPH_MIN_PURITY, default 0.5):
    #    playlists below this are skipped entirely — too mixed to add signal.
    #
    # 3. TIGHTER DIVERSITY GUARD default: combined with the purity filter,
    #    the diversity_guard_ratio in walkman_sync.py is also tightened from
    #    0.5 → 0.35 so a genre needs < 35% (was 50%) of rescues to get through.

    max_plist_size  = int(_cfg("GENRE_GRAPH_MAX_PLAYLIST_SIZE", 300))
    min_purity = (
        _override_purity
        if _override_purity is not None
        else float(_cfg("GENRE_GRAPH_MIN_PURITY", 0.50))
    )

    # Names that identify the "Liked Songs" playlist across locales
    _LIKED_SONGS_NAMES = frozenset({
        "liked songs", "liked tracks", "your liked songs",
        "songs you've liked", "songs i've liked",
    })

    skipped_playlists: list[tuple[str, int]] = []
    used_playlists:    int = 0

    # 2026-09-04: playlist-purity (below) reads t.get("genre") — but offline
    # track dicts (spotify_local_data.py, built from the GDPR export) NEVER
    # carry a genre at all (confirmed live: 0 of 5,484 offline tracks tagged).
    # Every one of the user's 34 playlists therefore computed purity=0.0 and
    # got skipped — "0 playlist(s) contributed" on every run, regardless of
    # min_purity. Fall back to the genre cache (the same lookup that already
    # powers artist_modal) keyed on (artist, title) when the track itself has
    # no genre.
    _genre_cache: dict = {}
    _cache_key_fn = None
    try:
        from genre_client import _get_genre_cache as _ggc, _cache_key as _gck
        _genre_cache = _ggc()
        _cache_key_fn = _gck
    except Exception:
        pass

    def _track_genre(t: dict) -> str:
        g_tag = (t.get("genre") or "").strip()
        if g_tag:
            return g_tag
        if _cache_key_fn is None:
            return ""
        primary = t.get("artist") or (t.get("artists") or [""])[0]
        title = t.get("title") or ""
        if not primary or not title:
            return ""
        hit = _genre_cache.get(_cache_key_fn(primary, title))
        return (hit or {}).get("genre", "") if isinstance(hit, dict) else ""

    # 2026-09-04 (user direction) — "use all the playlists except Rizz": named
    # playlists are hard-excluded regardless of purity; everything else is no
    # longer dropped for being too mixed (min_purity below), since the
    # existing purity*weight scaling already makes a mixed playlist
    # contribute proportionally little on its own — a hard cutoff was extra.
    _excluded_playlists = {
        p.strip().lower() for p in _cfg("GENRE_GRAPH_EXCLUDED_PLAYLISTS", {"Rizz"})
    }

    if track_map:
        for plist_name, plist_tracks in track_map.items():
            n_tracks = len(plist_tracks or [])

            if (plist_name or "").strip().lower() in _excluded_playlists:
                log.debug(f"  [GenreGraph] Excluding playlist '{plist_name}' (user exclusion list)")
                continue

            # MK20 G1: Initialize sampling defaults (must be set before any continue)
            _sampled_tracks = plist_tracks   # default: use all
            _sample_weight  = 1.0            # default: full weight
            _is_oversized   = n_tracks > max_plist_size

            # Liked Songs: apply sampling too (don't skip entirely)
            # MK13/MK17: the issue was their massive size dominating edges,
            # not the name itself. With sampling, they contribute proportionally.
            plist_name_lc = (plist_name or "").strip().lower()
            if plist_name_lc in _LIKED_SONGS_NAMES and n_tracks > max_plist_size:
                log.debug(
                    f"  [GenreGraph] Liked Songs oversized — applying sampling: "
                    f"'{plist_name}' ({n_tracks} tracks)"
                )
                # Fall through to oversized sampling below (don't continue)

            # MK20 G1: Sample artists from oversized playlists instead of skipping.
            # ROOT CAUSE: Liked Songs (2270) and genre-focused large playlists like
            # 'Delcy's Amapiano' (457) were BOTH skipped entirely because n_tracks > 300.
            # This caused graph starvation (0 nodes) on every run.
            #
            # APPROACH SELECTION:
            #   A1: Raise size cap globally → still misses 2000+ playlists
            #   A2: Remove cap → Liked Songs dominates, noise overwhelms signal
            #   A3: Random sample of GENRE_GRAPH_OVERSIZED_SAMPLE_SIZE artists ← WINNER
            #       Weight edges by (sample_size / n_tracks) so contribution is proportional.
            #
            try:
                _sample_n = int(getattr(config, "GENRE_GRAPH_OVERSIZED_SAMPLE_SIZE", 150))
            except Exception:
                _sample_n = 150

            if _is_oversized:
                import random as _rand
                _seed = hash(plist_name) & 0xFFFF   # deterministic per playlist name
                _rng  = _rand.Random(_seed)
                _sampled_tracks = _rng.sample(plist_tracks, min(_sample_n, n_tracks))
                _sample_weight  = min(_sample_n, n_tracks) / n_tracks
                log.info(
                    f"  [GenreGraph] OVERSIZED_PLAYLIST_SAMPLED '{plist_name}' "
                    f"({n_tracks} tracks → {len(_sampled_tracks)} sampled, "
                    f"weight={_sample_weight:.2f})"
                )

            # ── Genre purity calculation ───────────────────────────────
            # MK13/MK17 Fix: Count specific genres first; if none found,
            # fall back to all genres (including broad ones).
            # Pre-fix: pure Jazz/Classical playlists had 0 specific tags
            # → purity=0 → playlist skipped → lost all Jazz graph edges.
            genre_counts: Counter = Counter()
            artist_set:   set     = set()
            for t in _sampled_tracks or []:
                if not isinstance(t, dict):
                    continue
                primary = t.get("artist") or (t.get("artists") or [""])[0]
                if primary:
                    artist_set.add(primary)
                g_tag = _track_genre(t).lower()
                if g_tag and g_tag not in broad:
                    genre_counts[g_tag] += 1

            total_tagged = sum(genre_counts.values())
            purity = 0.0
            dominant_genre = ""
            if total_tagged > 0:
                top_genre, top_count = genre_counts.most_common(1)[0]
                purity         = top_count / total_tagged
                dominant_genre = top_genre
            elif total_tagged == 0:
                # MK13/MK17: fallback — include broad genres when no specific
                # genre tags were found.  Prevents pure Jazz/Classical playlists
                # from being skipped entirely due to purity=0.
                _broad_fb: Counter = Counter()
                for t in _sampled_tracks or []:
                    if isinstance(t, dict):
                        _genre_str = _track_genre(t)
                        if _genre_str:
                            _broad_fb[_genre_str.lower()] += 1
                if _broad_fb:
                    top_genre, top_count = _broad_fb.most_common(1)[0]
                    total_tagged   = sum(_broad_fb.values())
                    purity         = top_count / total_tagged
                    dominant_genre = top_genre.title()
                    genre_counts   = _broad_fb
                    log.debug(
                        f"  [GenreGraph] Playlist '{plist_name}': no specific "
                        f"genres — broad fallback: '{dominant_genre}' ({purity:.0%})"
                    )

            # 2026-09-04 (user direction): no longer hard-skip mixed
            # playlists — "use all the playlists except Rizz" (excluded by
            # name above). purity==0 means literally no genre signal could
            # be found at all (not even a broad fallback), so there's
            # nothing to weight; anything with SOME purity now contributes,
            # scaled down proportionally by the weighting right below rather
            # than dropped outright.
            if purity <= 0:
                log.debug(
                    f"  [GenreGraph] No genre signal for playlist '{plist_name}' "
                    f"(n={n_tracks}) — nothing to contribute"
                )
                continue

            # Scale edge weight by purity — strong signal for genre-focused
            # playlists, weak signal for mildly-skewed playlists.
            effective_w = playlist_w * purity * _sample_weight   # G1: proportional weight

            artist_list = list(artist_set)
            for i in range(len(artist_list)):
                for j in range(i + 1, len(artist_list)):
                    g.add_edge(artist_list[i], artist_list[j], effective_w)
            used_playlists += 1
            log.debug(
                f"  [GenreGraph] Playlist '{plist_name}': "
                f"purity={purity:.0%} ({dominant_genre}), "
                f"edge_w={effective_w:.2f}, artists={len(artist_list)}"
            )

    if skipped_playlists:
        # Log just the size of each skipped playlist; this is informational.
        skip_summary = ", ".join(
            f"'{n}'={c}" for n, c in skipped_playlists[:5]
        )
        if len(skipped_playlists) > 5:
            skip_summary += f", ... +{len(skipped_playlists) - 5} more"
        log.info(
            f"  [GenreGraph] Skipped {len(skipped_playlists)} playlist(s) "
            f"(oversized or Liked Songs): {skip_summary}"
        )

    log.info(
        f"  [GenreGraph] Built: {len(g.adjacency)} artist node(s), "
        f"{sum(len(v) for v in g.adjacency.values()) // 2} edge(s), "
        f"{len(g.artist_modal)} artist(s) with confident modal genre, "
        f"{used_playlists} playlist(s) contributed (min_purity={min_purity:.0%})."
    )

    # MK20 G1: Starvation fallback — seed from genre cache if graph empty.
    # ROOT CAUSE: When all playlists skipped (oversized/low-purity), graph has 0 nodes.
    # APPROACH A3 WINNER: seed weak same-genre edges from cache — provides seed signal
    # without distorting genre inference when real playlist data is available.
    try:
        _min_nodes = int(getattr(config, "GENRE_GRAPH_STARVATION_MIN_NODES", 5))
        _fb_enabled = bool(getattr(config, "GENRE_GRAPH_STARVATION_FALLBACK", True))
        if _fb_enabled and len(g.adjacency) < _min_nodes:
            _cache = _get_genre_cache()
            _genre_artists: dict = {}
            _broad = _broad_genres()
            for _ck, _ce in _cache.items():
                if "|" in _ck and isinstance(_ce, dict):
                    _a  = _ck.split("|")[0].strip()
                    _gn = (_ce.get("genre") or "").strip()
                    if _a and _gn and _gn.lower() not in _broad:
                        _genre_artists.setdefault(_gn, []).append(_a)
            _seeded = 0
            for _gn, _alist in sorted(_genre_artists.items(), key=lambda x: -len(x[1]))[:10]:
                _sub = _alist[:20]
                for _i in range(len(_sub)):
                    for _j in range(_i + 1, len(_sub)):
                        g.add_edge(_sub[_i], _sub[_j], 0.1)
                        _seeded += 1
            if _seeded > 0:
                log.warning(
                    f"  [GenreGraph] GRAPH_STARVED_FALLBACK_APPLIED — "
                    f"seeded {len(g.adjacency)} nodes from genre cache "
                    f"({_seeded} weak edges)"
                )
    except Exception as _sf_e:
        log.debug(f"  [GenreGraph] Starvation fallback error: {_sf_e}")

    return g


# ──────────────────────────────────────────────────────────────────────────
# Public inference — call this for sparse-artist genre rescue
# ──────────────────────────────────────────────────────────────────────────
def infer_genre_for_artist(graph: GenreGraph, artist: str,
                            hops: int | None = None,
                            min_neighbor_tracks: int | None = None
                            ) -> str | None:
    """
    Return the inferred genre for *artist* via weighted-vote walk on
    *graph*, or None if no confident neighbor was found.

    Strict mode (current default per Apr 30 user direction): only count
    votes from neighbors with >= min_neighbor_tracks in the library AND
    a non-broad modal genre.

    Walk up to *hops* (default GENRE_GRAPH_HOPS).  Vote weights are
    edge-weight × 1/hop_distance, so direct collaborators count more
    than 2-hop neighbors.
    """
    if hops is None:
        hops = int(_cfg("GENRE_GRAPH_HOPS", 2))
    if min_neighbor_tracks is None:
        min_neighbor_tracks = int(_cfg("GENRE_ARTIST_MIN_TRACKS", 3))

    a_norm = _norm_artist(artist)
    if not a_norm or a_norm not in graph.adjacency:
        return None

    # BFS with per-hop attenuation
    visited: dict[str, int] = {a_norm: 0}     # node -> hop distance
    frontier: list[str] = [a_norm]
    votes: Counter = Counter()

    for hop in range(1, hops + 1):
        next_frontier: list[str] = []
        for node in frontier:
            for neighbor, weight in graph.adjacency.get(node, {}).items():
                if neighbor in visited:
                    continue
                visited[neighbor] = hop
                next_frontier.append(neighbor)

                # Tally vote if neighbor is confident
                if (graph.artist_tracks.get(neighbor, 0) >= min_neighbor_tracks
                        and neighbor in graph.artist_modal):
                    weighted_vote = weight / hop
                    votes[graph.artist_modal[neighbor]] += weighted_vote

        frontier = next_frontier
        if not frontier:
            break

    if not votes:
        return None

    winner, score = votes.most_common(1)[0]
    log.debug(
        f"  [GenreGraph] Inferred '{winner}' for '{artist}' "
        f"(score={score:.1f}, votes={dict(votes)})"
    )
    return winner


def is_coherent_with_graph(genre: str, artist: str, graph: "GenreGraph") -> bool:
    """
    Return True if *genre* is coherent with what the genre graph knows.

    Coherence rules (in order):
      1. If the graph has no nodes at all → True (empty graph can't contradict).
      2. If *artist* is in the graph and has a confident modal genre that
         contradicts *genre* → False.
      3. If *artist* has no graph neighbors → check whether *genre* appears as
         the modal genre of ANY confident node.  If it's completely absent from
         the library's genre space → False.
      4. Otherwise → True.

    "Confident" = artist has ≥ GENRE_ARTIST_MIN_TRACKS tracks in the graph.
    """
    if not genre or graph is None:
        return True
    if not getattr(graph, "adjacency", None):
        return True

    min_tracks = int(_cfg("GENRE_ARTIST_MIN_TRACKS", 3))
    broad      = _broad_genres()
    g_norm     = genre.lower().strip()

    # Rule 2: artist has a known modal genre that contradicts
    if artist:
        a_norm = _norm_artist(artist)
        modal  = graph.artist_modal.get(a_norm, "")
        if modal and graph.artist_tracks.get(a_norm, 0) >= min_tracks:
            if modal.lower().strip() != g_norm:
                return False

    # Rule 3: genre must appear somewhere in the graph's modal genres
    known_modals = {v.lower().strip() for v in graph.artist_modal.values()
                    if v and v.lower() not in broad}
    if known_modals and g_norm not in known_modals:
        return False


# ──────────────────────────────────────────────────────────────────────────
# 2026-09-04 — Persistence + visualization
# ──────────────────────────────────────────────────────────────────────────
# The graph was previously built fresh every spiral run and held only on the
# SpiralOrchestrator instance — gone the moment the run ended, no way to
# inspect it standalone. These let `cli_runner.py rebuild-genre-graph` /
# `visualize-genre-graph` (and the matching GUI buttons) build it, persist
# it, and render it without a full sync.

def _graph_path():
    import config
    return config.BASE_DIR / "genre_graph.json"


def save_graph(g: "GenreGraph", path=None) -> "object":
    import json, time
    p = path or _graph_path()
    data = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "adjacency": {a: dict(nb) for a, nb in g.adjacency.items()},
        "artist_tracks": dict(g.artist_tracks),
        "artist_modal": dict(g.artist_modal),
    }
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log.info(f"  [GenreGraph] Saved {len(g.adjacency)} artist node(s) -> {p.name}")
    return p


def load_graph(path=None) -> "GenreGraph":
    import json
    p = path or _graph_path()
    g = GenreGraph()
    if not p.exists():
        return g
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"  [GenreGraph] Could not load {p.name}: {e}")
        return g
    for a, nb in data.get("adjacency", {}).items():
        g.adjacency[a] = dict(nb)
    g.artist_tracks.update(data.get("artist_tracks", {}))
    g.artist_modal.update(data.get("artist_modal", {}))
    return g


def render_graph(g: "GenreGraph", out_path=None, max_nodes: int = 250) -> "object":
    """
    Render the artist co-occurrence graph to a PNG: nodes = artists (colored
    by modal genre, sized by library track count), edges = collab/playlist
    weight. Capped to the `max_nodes` most-connected artists — the full
    graph has thousands of nodes and would render as an unreadable hairball.
    """
    import networkx as nx
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import config

    out_path = out_path or (config.BASE_DIR / "genre_graph.png")

    if not g.adjacency:
        raise RuntimeError("Genre graph is empty — run 'rebuild-genre-graph' first")

    degree = {a: len(nb) for a, nb in g.adjacency.items()}
    top = sorted(degree, key=degree.get, reverse=True)[:max_nodes]
    top_set = set(top)

    G = nx.Graph()
    G.add_nodes_from(top)
    for a in top:
        for b, w in g.adjacency[a].items():
            if b in top_set and not G.has_edge(a, b):
                G.add_edge(a, b, weight=w)

    genres = sorted({g.artist_modal.get(a, "Unknown") or "Unknown" for a in top})
    cmap = plt.get_cmap("tab20", max(len(genres), 1))
    genre_color = {gname: cmap(i) for i, gname in enumerate(genres)}
    node_colors = [genre_color[g.artist_modal.get(a, "Unknown") or "Unknown"] for a in top]
    node_sizes = [80 + 12 * g.artist_tracks.get(a, 1) for a in top]

    pos = nx.spring_layout(G, k=0.6, seed=42, weight="weight")
    fig, ax = plt.subplots(figsize=(20, 20))
    edge_widths = [0.2 + 0.15 * G[u][v]["weight"] for u, v in G.edges()]
    nx.draw_networkx_edges(G, pos, ax=ax, width=edge_widths, alpha=0.25, edge_color="#888888")
    nx.draw_networkx_nodes(G, pos, ax=ax, node_color=node_colors, node_size=node_sizes,
                            linewidths=0.5, edgecolors="black")
    label_nodes = sorted(top, key=degree.get, reverse=True)[:60]
    nx.draw_networkx_labels(G, pos, labels={a: a for a in label_nodes}, ax=ax, font_size=7)

    handles = [
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=genre_color[gname],
                   markersize=8, label=gname)
        for gname in genres[:30]
    ]
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.0, 1.0),
              fontsize=7, title="Modal genre")
    ax.set_title(f"Artist genre graph — {len(top)} of {len(g.adjacency)} artists shown "
                 f"(top by connections)")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info(f"  [GenreGraph] Rendered {len(top)} node(s) -> {out_path}")
    return out_path

    return True
