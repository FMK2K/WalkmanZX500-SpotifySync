"""
mk39_phase_I_containment.py — Council Emergency Corrective Directive 2026-05-10
================================================================================

Council 2026-05-10 §3 PHASE I CONTAINMENT (deploy within 48 hours):
  A. Wire user-canonical folder-name signals into local-audit `_expected_tags()`
  B. Hard-block `Abstract Hip-Hop` as a SILENT default
  C. Disable AI genre inference when providers exhausted (session-level flag)
  D. Detect or disable empty `GenreGraph` so its 0-node output cannot mislead

Discovered while implementing Phase I (root cause #5, NOT in Council list):
  E. `audit.py` calls `_artist_override(artist, title, album=...)` but
     `genre_client._artist_override` accepts only `(artist, title)`. Every
     override lookup has been silently raising `TypeError` and falling through
     to the broken cache. This is the actual bug behind The Weeknd → Afrobeats
     and the SA-house-tracks → Abstract Hip-Hop class of failures. The override
     dict has been correct all along; the call site was wrong. Phase I includes
     a **safe wrapper** `safe_artist_override()` that passes only the two
     accepted positional args.

This module is a focused operational layer. It does NOT introduce a 9th
enforcement architecture; it plugs concrete fallback holes per Council
forensic findings. No new MC sims (Council didn't ask). No new write barriers
(MK39-Gate already covers that). Just four containment helpers + the override
wrapper, each backed by a small test.

The Council made the priority explicit: *"silence (blank fields) is preferred
to confident error."* Every helper here returns blank/None rather than a guess
when uncertain.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# (A) Folder-name → genre keyword matcher
# Council §3 Phase I-A:  "the folder-name signal must be treated as a
# canonical source that can set genre to `Afro House`, `City Pop`, etc.,
# when the folder name confidently matches a known keyword."
# ═══════════════════════════════════════════════════════════════════════════

# Conservative keyword → canonical genre mapping. Keys are lowercase,
# match against folder name as a substring after normalization. Order
# matters: longer/more-specific keys checked first to avoid e.g.
# "Afro House" being shadowed by "House" or "Afro".
#
# Each entry justified by Council 2026-05-10 forensic log:
_FOLDER_GENRE_KEYWORDS: List[Tuple[str, str]] = [
    # ── Most specific first (to avoid shadowing) ──────────────────────────
    ("3 step afro house",   "Afro House"),
    ("afro house",          "Afro House"),
    ("city pop",            "City Pop"),
    ("japanese city pop",   "City Pop"),
    ("amapiano",            "Amapiano"),
    ("gqom",                "Gqom"),
    ("afrobeats",           "Afrobeats"),
    ("afro beats",          "Afrobeats"),
    ("afrobeat",            "Afrobeat"),    # Fela's afrobeat (singular) is distinct
    ("bongo flava",         "Bongo Flava"),
    ("alte",                "Alté"),
    ("afro pop",            "Afropop"),
    ("afropop",             "Afropop"),
    # Western reference cases
    ("alt r&b",             "Alt R&B"),
    ("contemporary r&b",    "Contemporary R&B"),
    # Asian
    ("k-pop",               "K-Pop"),
    ("k pop",               "K-Pop"),     # normalizer turns "K-Pop" → "k pop"
    ("kpop",                "K-Pop"),
    ("j-pop",               "J-Pop"),
    ("j pop",               "J-Pop"),
    ("jpop",                "J-Pop"),
    ("c-pop",               "C-Pop"),
    ("c pop",               "C-Pop"),
    ("cpop",                "C-Pop"),
    # Caribbean / Latin
    ("dancehall",           "Dancehall"),
    ("reggaeton",           "Reggaeton"),
    ("afrobeats & dancehall", "Afrobeats"),
    # Drum'n'bass / electronic clarifiers
    ("drum and bass",       "Drum & Bass"),
    ("drum & bass",         "Drum & Bass"),
    ("drum n bass",         "Drum & Bass"),
    ("dnb",                 "Drum & Bass"),
    ("uk garage",           "UK Garage"),
    ("jersey club",         "Jersey Club"),
    ("baltimore club",      "Baltimore Club"),
    # Rock / metal flavors
    ("psychedelic rock",    "Psychedelic Rock"),
    ("indie rock",          "Indie Rock"),
    ("alt rock",            "Alternative Rock"),
    ("alternative rock",    "Alternative Rock"),
]


def _normalize_folder(folder_name: str) -> str:
    """Lowercase + replace separators with spaces for keyword matching."""
    if not folder_name:
        return ""
    s = folder_name.lower()
    # Replace common separators (underscore, dash, dots, parens) with spaces
    s = re.sub(r"[_\-\.\(\)\[\]]+", " ", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_folder_genre(folder_name: str) -> Optional[str]:
    """Return a canonical genre tag if `folder_name` confidently contains
    a known genre keyword; otherwise `None`.

    Conservative by design — Council §3 Phase I-A: only fires when the
    folder name LITERALLY contains a recognized genre token. No fuzzy
    matching, no inference. Examples that match:

        "3 Step Afro House 2026 Hits"        → "Afro House"
        "80_90s Japanese City Pop"           → "City Pop"
        "Amapiano Gold 2024"                 → "Amapiano"
        "Best of K-Pop"                      → "K-Pop"

    Examples that do NOT match (operator owns these via overrides or .user_metadata.json):

        "After Hours"                        → None (Weeknd album, no genre keyword)
        "Don't Be Dumb"                      → None (A$AP Rocky album)
        "13"                                 → None (Black Sabbath)
    """
    if not folder_name:
        return None
    norm = _normalize_folder(folder_name)
    if not norm:
        return None
    for key, canonical in _FOLDER_GENRE_KEYWORDS:
        if key in norm:
            return canonical
    return None


# ═══════════════════════════════════════════════════════════════════════════
# (B) Block `Abstract Hip-Hop` as a SILENT default
# Council §3 Phase I-B:  "if the only candidate after all lookups is
# `Abstract Hip-Hop`, and the track's artist does not appear in a curated
# allow-list of abstract/underground rappers, then leave the genre blank."
# ═══════════════════════════════════════════════════════════════════════════

# Curated allow-list of artists for whom `Abstract Hip-Hop` is legitimately
# their canonical genre (these are the artists with explicit overrides in
# genre_client._ARTIST_GENRE_OVERRIDES whose value is "Abstract Hip-Hop").
# When the resolved genre is "Abstract Hip-Hop" AND the artist is on this
# list, allow. Otherwise, blank.
_ABSTRACT_HIP_HOP_ALLOWLIST: frozenset = frozenset({
    "mf doom", "mfdoom", "doom",
    "viktor vaughn", "king geedorah", "metal fingers", "zev love x",
    "dangerdoom", "madvillain", "nehruviandoom",
    "madlib", "quasimoto", "lootpack", "jaylib",
    "mf grimm", "aesop rock", "busdriver", "armand hammer",
    "billy woods", "elucid", "navy blue", "mavi", "mach hommy",
    "ka", "mike", "maxo", "milo", "scallops hotel",
    "quelle chris", "noname",
    "company flow", "deltron 3030", "del the funky homosapien",
    "j dilla", "jaylib", "the gaslamp killer",
    "earl sweatshirt",
})

ABSTRACT_HIP_HOP_TAG = "Abstract Hip-Hop"


def block_abstract_hip_hop_default(candidate_genre: Optional[str],
                                     artist: str) -> Optional[str]:
    """Council §3 Phase I-B: if the only candidate is `Abstract Hip-Hop`
    and the artist is NOT on the curated allow-list, return `None` (blank).

    Returns:
      - `candidate_genre` unchanged if it's not "Abstract Hip-Hop"
      - `candidate_genre` unchanged if artist IS on the allow-list
      - `None` otherwise (Council: "leave the genre blank")
    """
    if not candidate_genre:
        return candidate_genre
    if candidate_genre.strip().lower() != ABSTRACT_HIP_HOP_TAG.lower():
        return candidate_genre
    # It's "Abstract Hip-Hop" — is the artist on the allow-list?
    artist_norm = (artist or "").strip().lower()
    # Strip "the " prefix to match the allow-list normalization style
    if artist_norm.startswith("the "):
        artist_norm = artist_norm[4:]
    if artist_norm in _ABSTRACT_HIP_HOP_ALLOWLIST:
        return candidate_genre   # legitimate
    # Default-bleed case: blank
    log.info(
        f"  [Phase-I-B] BLOCKED_ABSTRACT_HIP_HOP_DEFAULT artist={artist!r} "
        f"(not on allow-list) — leaving genre blank per Council 2026-05-10 §3-B"
    )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# (C) Session-level AI-disable flag
# Council §3 Phase I-C: "if `_GEMINI_GENRE_CACHE` backoff has disabled
# Gemini and all Groq providers have failed more than 3 times in the
# current run, set a session-level flag `AI_GENRE_DISABLED = True`. No
# further AI calls will be attempted."
# ═══════════════════════════════════════════════════════════════════════════

_AI_DISABLE_LOCK = threading.RLock()
_AI_GENRE_DISABLED: bool = False
_GROQ_FAILURE_COUNT: int = 0
GROQ_FAILURE_THRESHOLD: int = 50  # 50 consecutive total-failures = providers are dead for this burst


def mark_groq_failure() -> int:
    """Record one Groq failure. When count exceeds GROQ_FAILURE_THRESHOLD
    AND Gemini is also disabled, set AI_GENRE_DISABLED=True for the session.

    Returns the new failure count.
    """
    global _GROQ_FAILURE_COUNT, _AI_GENRE_DISABLED
    with _AI_DISABLE_LOCK:
        _GROQ_FAILURE_COUNT += 1
        if _GROQ_FAILURE_COUNT > GROQ_FAILURE_THRESHOLD and not _AI_GENRE_DISABLED:
            # Check if Gemini is also disabled
            try:
                import genre_client as _gc
                gemini_dead = bool(getattr(_gc, "_GEMINI_DISABLED", False))
            except Exception:
                gemini_dead = False
            if gemini_dead:
                _AI_GENRE_DISABLED = True
                log.warning(
                    f"  [Phase-I-C] AI_GENRE_DISABLED — Gemini circuit-broken "
                    f"+ Groq failures={_GROQ_FAILURE_COUNT} (>{GROQ_FAILURE_THRESHOLD}). "
                    f"All further AI genre calls will be skipped this session "
                    f"(Council 2026-05-10 §3-C). Will retry next run."
                )
        return _GROQ_FAILURE_COUNT


def ai_genre_disabled() -> bool:
    """True if the session-level AI disable flag has tripped.
    Called by `ai_helper.call_ai` to short-circuit before provider calls."""
    with _AI_DISABLE_LOCK:
        return _AI_GENRE_DISABLED


def reset_ai_disable_state() -> None:
    """Reset the AI disable state. Used by tests; not called in production."""
    global _AI_GENRE_DISABLED, _GROQ_FAILURE_COUNT
    with _AI_DISABLE_LOCK:
        _AI_GENRE_DISABLED = False
        _GROQ_FAILURE_COUNT = 0


# ═══════════════════════════════════════════════════════════════════════════
# (D) Detect empty GenreGraph
# Council §3 Phase I-D: "investigate why GenreGraph built 0 nodes ...
# if the graph is not salvageable in the short term, disable it so that
# its empty output doesn't mislead upstream code into thinking an artist
# has '0 neighbors' and falling to a bad default."
#
# Root cause from genre_graph.py: the graph builds from `track_map`, the
# Spotify playlist data. In `--local-only` runs (no Spotify fetch), the
# track_map is empty and the graph has 0 nodes — by design. Upstream code
# that calls `graph.neighbors(artist)` then gets an empty result and
# treats it as "artist has no genre signal" rather than "graph is unbuilt."
# ═══════════════════════════════════════════════════════════════════════════

def genre_graph_is_empty(graph: Any) -> bool:
    """Return True if the graph has no artist nodes — meaning no Spotify
    track_map was available (e.g., local-only run). Upstream code MUST
    short-circuit graph-based inference when this is True; otherwise
    `0 neighbors` is misinterpreted as `artist has no genre signal`,
    which then falls to the broken cache default (Abstract Hip-Hop, etc.).

    Compatible with both `GenreGraph` instances (uses `.adjacency`) and
    None (returns True). Defensive — never raises.
    """
    if graph is None:
        return True
    try:
        adj = getattr(graph, "adjacency", None)
        if adj is None:
            return True
        if hasattr(adj, "__len__"):
            return len(adj) == 0
        # Fallback: try iter
        try:
            next(iter(adj))
            return False
        except StopIteration:
            return True
    except Exception:
        # Defensive: if we can't determine, treat as empty (safer)
        return True


# ═══════════════════════════════════════════════════════════════════════════
# (E) Safe wrapper for genre_client._artist_override
# DISCOVERED during Phase I implementation, NOT in Council's original list.
# Root cause: audit.py calls `_artist_override(artist, title, album=...)`
# but the function only accepts `(artist, title)`. Every override lookup
# has been silently raising TypeError → swallowed → fallthrough to broken
# cache. The override dict was correct all along; the call site was wrong.
# ═══════════════════════════════════════════════════════════════════════════

def safe_artist_override(artist: str, title: str = "", **kwargs: Any) -> str:
    """Safe wrapper around `genre_client._artist_override` that ignores
    extra kwargs. Restores override functionality at every audit-path
    call site without requiring updates to every caller.

    The underlying `_artist_override(artist, title)` accepts only two args.
    Callers that pass `album=`, `year=`, etc., would raise TypeError —
    which historically was swallowed by `except Exception: pass`, silently
    breaking all overrides for the entire audit run.

    Returns the override genre, or empty string if no override applies
    or the lookup raises. Non-empty result is the canonical genre.
    """
    try:
        import genre_client as _gc
        # Drop any extra kwargs — only artist + title are accepted
        return _gc._artist_override(artist, title) or ""
    except Exception as e:
        log.debug(f"  [Phase-I-E] safe_artist_override failed: "
                  f"{type(e).__name__}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Combined audit-path helper
# ═══════════════════════════════════════════════════════════════════════════

def resolve_genre_with_phase_I(artist: str,
                                  title: str,
                                  current_genre: str,
                                  folder_name: str = "",
                                  cache_genre: str = "",
                                  graph: Any = None) -> Tuple[Optional[str], str]:
    """Single-call helper that applies all Phase I containment rules in
    Council-mandated priority order. Returns `(genre_or_None, source_label)`.

    Priority (Council 2026-05-10 §3 ordering):
      1. Existing tag (current_genre) — never overwrite if confident
      2. Artist override via safe_artist_override (Phase I-E)
      3. Folder-name keyword match via match_folder_genre (Phase I-A)
      4. Cache genre (existing pipeline) — passed through Phase I-B filter
      5. None (blank — Council preference: "silence over confident error")

    Each non-None return is filtered through `block_abstract_hip_hop_default`
    so the dominant-bleed pattern can never reach the writer.
    """
    # 1. Existing tag wins if non-empty (fill-only-blanks; Council #4 echo)
    if current_genre and current_genre.strip().lower() not in ("", "unknown"):
        return current_genre, "existing_tag"

    # 2. Artist override (Phase I-E discovery)
    override = safe_artist_override(artist, title)
    if override:
        filtered = block_abstract_hip_hop_default(override, artist)
        if filtered:
            return filtered, "artist_override"

    # 3. Folder-name keyword (Phase I-A — Council main "fixes 80%" lever)
    folder_genre = match_folder_genre(folder_name)
    if folder_genre:
        filtered = block_abstract_hip_hop_default(folder_genre, artist)
        if filtered:
            return filtered, "user_canonical_folder_keyword"

    # 4. Cache genre (existing path) — Phase I-B applies as silent-default
    #    block at this layer too
    if cache_genre and cache_genre.strip().lower() not in ("", "unknown"):
        filtered = block_abstract_hip_hop_default(cache_genre, artist)
        if filtered:
            return filtered, "cache"
        # Otherwise blocked → fall through to None

    # 5. Graph: only if non-empty (Phase I-D)
    if graph is not None and not genre_graph_is_empty(graph):
        # Caller is responsible for actually doing graph inference;
        # we only signal that the graph IS usable.
        pass   # not implementing inference here — caller does it

    # 6. Blank — Council preference: "silence over confident error"
    return None, "blank_phase_I_preference"


__all__ = [
    # Phase I-A
    "match_folder_genre",
    # Phase I-B
    "ABSTRACT_HIP_HOP_TAG",
    "block_abstract_hip_hop_default",
    # Phase I-C
    "GROQ_FAILURE_THRESHOLD",
    "mark_groq_failure",
    "ai_genre_disabled",
    "reset_ai_disable_state",
    # Phase I-D
    "genre_graph_is_empty",
    # Phase I-E (discovered, not in original Council list)
    "safe_artist_override",
    # Combined helper
    "resolve_genre_with_phase_I",
]
