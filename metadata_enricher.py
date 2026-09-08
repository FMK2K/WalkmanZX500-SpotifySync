"""
metadata_enricher.py  —  Bulk AI metadata audit and enrichment
================================================================
Fixes the three persistent metadata problems that rule-based lookups miss:

  1. GENRE PROBLEMS
     - Artist names stamped as genre tags  ("21 Savage", "J. Cole", etc.)
     - Broad umbrella tags replacing niche subgenres  ("Hip-Hop" instead of "Drill")
     - Duplicate broad variants  ("Hip-Hop", "Rap/Hip-Hop", "Hip-Hop/Rap") all
       collapsed to the same canonical subgenre
     - Unknown / empty genre — filled by AI inference from artist + album context
     - Per-track genre drift inside the same album (one Kanye album gets four
       different genres on four tracks) — resolved to per-album consensus

  2. YEAR PROBLEMS
     - Unknown / 0 / 1900 / 2-digit year — looked up from Spotify cache or AI
     - Year-of-remaster vs year-of-original — prefers original release year

  3. ARTIST TAG PROBLEMS
     - ARTIST tag contains featured artists ("Tyler, The Creator feat. Pharrell")
       when FEATURE_ARTISTS_IN_TITLE is True — feat. goes in TITLE not ARTIST
     - ALBUM_ARTIST ≠ primary artist on single-artist albums
     - Slash-separated multi-artist strings left by download tools

Approach
--------
  Phase 1  (fast, rule-based, no API cost):
    - Load the entire .genre_cache.json
    - Detect and fix the clearly-wrong entries using the existing _GENRE_NORM map
      plus new heuristics (artist-name-as-genre detector, broad-tag upgrader)
    - Apply album-level consensus: take the mode genre across all tracks in an
      album and stamp it on every track in that album

  Phase 2  (AI batch, ~1 API call per 40 albums):
    - For entries that are still Unknown/empty/broad after Phase 1, group by
      artist+album and send batches of up to 40 albums to Claude claude-sonnet-4-6
    - Each batch returns a JSON mapping {artist||album: {genre, year}} for every
      album in the batch
    - Results written back to .genre_cache.json and used immediately by the next
      audit run

  Phase 3  (write-back to device files, optional):
    - Re-runs the existing audit.py sweep with the corrected cache
    - Only rewrites tags that actually changed — MetaKey fast-path skips the rest

Usage
-----
  Standalone (run after sync):
      python metadata_enricher.py
      python metadata_enricher.py --phase 1        # rule-based only (fast)
      python metadata_enricher.py --phase 2        # AI batch only
      python metadata_enricher.py --dry-run        # preview changes, don't write
      python metadata_enricher.py --album "Kanye West - MBDTF"  # one album

  Called from main.py after sync:
      from metadata_enricher import run_enrichment
      run_enrichment(phase=1)   # fast pass — always safe to call
      run_enrichment(phase=2)   # AI pass — costs API tokens

  Wired into audit button in GUI (add to main.py _audit_thread):
      from metadata_enricher import run_enrichment
      run_enrichment(phase=1, write_back=False)  # fix cache only, no MTP
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config & paths
# ---------------------------------------------------------------------------

def _get_config():
    try:
        import config as _c
        return _c
    except ImportError:
        return None

_cfg = _get_config()

GENRE_CACHE_FILE   = getattr(_cfg, "GENRE_CACHE_FILE",
                              pathlib.Path(".genre_cache.json"))
SPOTIFY_META_FILE  = pathlib.Path(getattr(_cfg, "BASE_DIR", pathlib.Path("."))) / "_spotify_metadata_cache.json"
CLAUDE_MODEL       = "claude-sonnet-4-20250514"
BATCH_SIZE         = 40     # albums per Claude API call
AI_TIMEOUT_S       = 30     # seconds per batch request
MAX_RETRIES        = 2

# ---------------------------------------------------------------------------
# Heuristics: detect genre strings that are actually artist / album names
# ---------------------------------------------------------------------------

# Minimum word length for an artist-name-as-genre to be detected
_ARTIST_NAME_MIN_LEN = 4

def _looks_like_artist_name(genre: str, artist: str = "", album: str = "") -> bool:
    """
    Return True if `genre` looks like an artist or album name rather than a genre.

    Signals:
      - Exact match against artist or album (case-insensitive)
      - Contains a possessive apostrophe  ("Kanye's")
      - All-caps initialism that isn't a known genre acronym  ("ASAP", "YNW")
      - Contains digits that aren't a decade  ("21", "6ix9ine")
      - Title Case phrase with no known genre words
    """
    g = genre.strip()
    if not g or len(g) < 2:
        return False

    g_lc = g.lower()

    # Exact match against artist or album
    if artist and g_lc == artist.lower().strip():
        return True
    if album and g_lc == album.lower().strip():
        return True

    # Contains digits that are NOT a decade pattern (80s, 90s, 2000s)
    if re.search(r"\d", g) and not re.search(r"\b\d0s\b|\b(19|20)\d{2}\b", g, re.I):
        return True

    # Possessive or slang punctuation typical of rap artist names
    if re.search(r"['\u2019]\s*s\b|\.com\b|\_", g):
        return True

    # Known genre words — if ANY appear, it's probably a genre
    _GENRE_WORDS = {
        "hip", "hop", "rap", "pop", "rock", "soul", "jazz", "r&b", "rnb",
        "funk", "disco", "house", "trap", "drill", "grime", "afro", "bongo",
        "kompa", "reggae", "dancehall", "electronic", "ambient", "classical",
        "metal", "punk", "folk", "country", "gospel", "blues", "latin",
        "salsa", "bachata", "kwaito", "gqom", "amapiano", "afrobeats",
        "alternative", "indie", "neo", "cloud", "emo", "phonk", "hyperpop",
    }
    words = set(re.findall(r"[a-z]+", g_lc))
    if words & _GENRE_WORDS:
        return False

    # All-caps (like an artist name) longer than 2 chars
    if g == g.upper() and len(g) > 2 and g.isalpha():
        return True

    return False


# ---------------------------------------------------------------------------
# Heuristics: upgrade broad genre to subgenre using artist context
# ---------------------------------------------------------------------------

# Map of (broad_genre → {artist_fragment → specific_genre})
# Applied when the cache has a broad genre and artist info is available.
_BROAD_TO_SPECIFIC: dict[str, list[tuple[str, str]]] = {
    "Hip-Hop": [
        ("central cee",       "UK Rap"),
        ("headie one",        "UK Drill"),
        ("dave",              "UK Rap"),
        ("j hus",             "Afroswing"),
        ("digga d",           "UK Drill"),
        ("young adz",         "UK Rap"),
        ("dutchavelli",       "UK Drill"),
        ("aitch",             "UK Rap"),
        ("little simz",       "UK Rap"),
        ("stormzy",           "Grime"),
        ("slowthai",          "UK Rap"),
        ("skepta",            "Grime"),
        ("giggs",             "UK Rap"),
        ("kano",              "Grime"),
        ("ghetts",            "Grime"),
        ("jpegmafia",         "Experimental Hip Hop"),
        ("billy woods",       "Abstract Hip Hop"),
        ("mach hommy",        "Abstract Hip Hop"),
        ("freddie gibbs",     "Gangsta Rap"),
        ("curren$y",          "Jazz Rap"),
        ("wiz khalifa",       "West Coast Rap"),
        ("mac miller",        "Jazz Rap"),
        ("joey badass",       "Boom Bap"),
        ("flatbush zombies",  "East Coast Rap"),
        ("flatbush",          "East Coast Rap"),
        ("grieves",           "Midwest Hip-Hop"),
        ("chance the rapper", "Conscious Hip Hop"),
        ("noname",            "Conscious Hip Hop"),
        ("saba",              "Conscious Hip Hop"),
        ("smino",             "Neo Soul"),
        ("serena isioma",     "Neo Soul"),
        ("aminé",             "West Coast Rap"),
        ("vince staples",     "West Coast Rap"),
        ("denzel curry",      "Southern Hip Hop"),
        ("j.i.d",             "Southern Hip Hop"),
        ("earthgang",         "Southern Hip Hop"),
        ("6lack",             "Alt R&B"),
        ("jidenna",           "Afrobeats"),
        ("migos",             "Trap"),
        ("young thug",        "Trap"),
        ("gunna",             "Trap"),
        ("lil baby",          "Trap"),
        ("dababy",            "Trap"),
        ("polo g",            "Melodic Trap"),
        ("nle choppa",        "Trap"),
        ("rod wave",          "Melodic Trap"),
        ("lil durk",          "Drill"),
        ("king von",          "Drill"),
        ("fivio foreign",     "Drill"),
        ("pop smoke",         "Drill"),
        ("sheff g",           "Drill"),
        ("sha ek",            "Drill"),
        ("sleepy hallow",     "Drill"),
        ("don toliver",       "Melodic Trap"),
        ("travis scott",      "Trap"),
        ("playboi carti",     "Trap"),
        ("ski mask",          "Emo Rap"),
        ("lil uzi vert",      "Emo Rap"),
        ("juice wrld",        "Emo Rap"),
        ("xxxtentacion",      "Emo Rap"),
        ("lil peep",          "Emo Rap"),
        ("kodak black",       "Trap"),
        ("rylo rodriguez",    "Trap"),
        ("nardo wick",        "Trap"),
        ("bandmanrill",       "Jersey Club"),
        ("sha ef",            "Drill"),
        ("kyle richh",        "Drill"),
        ("jenn carter",       "Drill"),
        ("41",                "Drill"),
        ("riverside",         "Drill"),
    ],
    "R&B": [
        ("frank ocean",       "Alt R&B"),
        ("the weeknd",        "Alt R&B"),
        ("bryson tiller",     "Trap Soul"),
        ("partynextdoor",     "Trap Soul"),
        ("giveon",            "R&B"),
        ("lucky daye",        "R&B"),
        ("brent faiyaz",      "Alt R&B"),
        ("sza",               "Alt R&B"),
        ("summer walker",     "R&B"),
        ("chloe x halle",     "R&B"),
        ("tinashe",           "Alt R&B"),
        ("mahalia",           "R&B"),
        ("jorja smith",       "R&B"),
        ("ravyn lenae",       "Alt R&B"),
        ("kali uchis",        "Alt R&B"),
        ("snoh aalegra",      "Neo Soul"),
        ("masego",            "Jazz Rap"),
        ("dvsn",              "Trap Soul"),
        ("mr eazi",           "Afrobeats"),
        ("wizkid",            "Afrobeats"),
    ],
    "Pop": [
        ("bad bunny",         "Latin Trap"),
        ("j balvin",         "Reggaeton"),
        ("maluma",            "Reggaeton"),
        ("rosalia",           "Latin Pop"),
        ("anuel aa",          "Latin Trap"),
        ("ozuna",             "Reggaeton"),
        ("jhay cortez",       "Latin Trap"),
    ],
}


def _upgrade_broad_genre(genre: str, artist: str) -> str:
    """
    If `genre` is a broad umbrella tag, try to upgrade it using artist name.
    Returns the upgraded genre, or the original if no upgrade found.
    """
    artist_lc = artist.lower().strip()
    candidates = _BROAD_TO_SPECIFIC.get(genre, [])
    for fragment, specific in candidates:
        if fragment in artist_lc:
            return specific
    return genre


# ---------------------------------------------------------------------------
# Album-level genre consensus
# ---------------------------------------------------------------------------

def _album_consensus_genre(track_genres: list[str]) -> str:
    """
    Given a list of genre strings for tracks in one album, return the
    single best genre to stamp on every track.

    Logic:
      1. Remove empty / Unknown entries
      2. Find the most common non-broad genre (mode)
      3. Fall back to most common broad genre if no specific found
      4. Return "" if all entries are empty/Unknown
    """
    clean = [g for g in track_genres if g and g.lower() not in ("unknown", "")]
    if not clean:
        return ""

    # Separate specific vs broad
    specific = [g for g in clean if not _is_broadly_hip_hop_or_rb(g)]
    broad    = [g for g in clean if _is_broadly_hip_hop_or_rb(g)]

    if specific:
        return Counter(specific).most_common(1)[0][0]
    if broad:
        return Counter(broad).most_common(1)[0][0]
    return ""


_BROAD_UMBRELLA = {"Hip-Hop", "R&B", "Pop", "Rock", "Electronic", "Soul",
                   "Jazz", "Latin", "Gospel", "Classical", "World Music",
                   "Rap/Hip Hop", "Hip-Hop/Rap", "Hip Hop",
                   # Added 2026-04-17: stale broad strings written by older
                   # enrichment passes before _ARTIST_GENRE_OVERRIDES was
                   # expanded (MF DOOM → "Alternative Rock", Cochise →
                   # "Rap/Hip Hop" already covered, DANGERDOOM → "Hardcore
                   # Hip-Hop").  See _is_broad_and_overridable() for how
                   # these are handled — the check is guarded so Radiohead
                   # and Nirvana, whose override genuinely IS "Alternative
                   # Rock", are NOT re-queried.
                   "Alternative Rock", "Hardcore Hip-Hop", "Hardcore Hip Hop",
                   "Hardcore Rap", "Underground Hip-Hop", "Underground Hip Hop",
                   "Underground Rap"}

def _is_broadly_hip_hop_or_rb(g: str) -> bool:
    return g in _BROAD_UMBRELLA


# Cache of the artist-override lookup — imported lazily so metadata_enricher
# stays usable standalone if genre_client is unavailable.
_ARTIST_OVERRIDE_FN = None

def _get_artist_override_fn():
    global _ARTIST_OVERRIDE_FN
    if _ARTIST_OVERRIDE_FN is None:
        try:
            from genre_client import _artist_override as _fn
            _ARTIST_OVERRIDE_FN = _fn
        except Exception as exc:
            log.debug(f"[Enricher] _artist_override unavailable: {exc}")
            # Sentinel so we don't retry every call
            _ARTIST_OVERRIDE_FN = lambda a, t="": ""
    return _ARTIST_OVERRIDE_FN


def _is_broad_and_overridable(genre: str, artist: str, title: str = "") -> bool:
    """
    Return True when the current genre should be treated as broad AND there
    is a better specific value available via _artist_override.

    This is the guard that prevents Phase-2 AI re-enrichment from clobbering
    correct-but-broad entries (e.g. Radiohead → "Alternative Rock", which
    is genuinely what the override dict says — it's broad by our _BROAD_GENRES
    definition, but it's the right answer for that artist, so we must not
    re-query).

    Behaviour:
      - genre not in _BROAD_UMBRELLA     → False (not broad, leave alone)
      - no override for this artist      → True  (broad + no better answer = retry)
      - override equals current genre    → False (current IS the override, keep it)
      - override is also broad           → False (no improvement available, don't cycle)
      - override is different & specific → True  (refetch, override will win)
    """
    if genre not in _BROAD_UMBRELLA:
        return False
    override = _get_artist_override_fn()(artist or "", title or "")
    if not override:
        return True
    if override == genre:
        return False
    if override in _BROAD_UMBRELLA:
        return False
    return True


# ---------------------------------------------------------------------------
# Phase 1: rule-based cache repair
# ---------------------------------------------------------------------------

def _load_cache(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"[Enricher] Cache load failed ({e}) — starting empty")
        return {}


def _save_cache(path: pathlib.Path, cache: dict):
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        log.info(f"[Enricher] Cache saved: {len(cache)} entries → {path.name}")
    except Exception as e:
        log.warning(f"[Enricher] Cache save failed: {e}")
        tmp.unlink(missing_ok=True)


def _parse_cache_key(key: str) -> tuple[str, str]:
    """Split "artist_norm|title_norm" cache key."""
    parts = key.split("|", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (key, "")


def _load_spotify_meta() -> dict:
    """Load _spotify_metadata_cache.json for artist/album/year cross-reference."""
    if not SPOTIFY_META_FILE.exists():
        return {}
    try:
        return json.loads(SPOTIFY_META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def run_phase1(
    cache: dict,
    spotify_meta: dict,
    dry_run: bool = False,
) -> tuple[dict, dict]:
    """
    Rule-based cache repair. Returns (updated_cache, stats_dict).

    Fixes applied per entry:
      A. Artist-name-as-genre → scrub to empty (triggers Phase 2 re-lookup)
      B. Broad umbrella genre + known artist → upgrade to subgenre
      C. Compound duplicate tags ("Hip-Hop/Rap") → canonical single tag
      D. Empty year → fill from Spotify metadata cache
      E. Bad year (0, 1900, 2-digit) → clear to empty (triggers re-lookup)

    Then applies album-level consensus:
      F. Group all tracks by artist+album, take mode genre, stamp all tracks
    """
    stats = {"artist_as_genre": 0, "upgraded": 0, "deduped": 0,
             "year_filled": 0, "year_cleared": 0, "consensus_applied": 0}

    # Build artist→album→[genre] index from Spotify meta for consensus
    album_genres: dict[str, list[str]] = defaultdict(list)   # "artist_norm|album_norm" → [genres]
    album_years:  dict[str, str]       = {}                   # "artist_norm|album_norm" → year

    # Build a fast artist lookup from Spotify meta
    sp_by_key: dict[str, dict] = {}
    for entry in spotify_meta.values():
        if not isinstance(entry, dict):
            continue
        try:
            import genre_client as _gc
            k = _gc._cache_key(entry.get("artist", ""), entry.get("title", ""))
            sp_by_key[k] = entry
        except ImportError:
            k = f"{entry.get('artist','').lower()}|{entry.get('title','').lower()}"
            sp_by_key[k] = entry

    updated = dict(cache)

    for key, entry in cache.items():
        if not isinstance(entry, dict):
            continue

        artist_norm, title_norm = _parse_cache_key(key)
        genre = (entry.get("genre") or "").strip()
        year  = (entry.get("year")  or "").strip()

        # Get artist name from Spotify meta for context
        sp = sp_by_key.get(key, {})
        artist_name  = sp.get("artist", artist_norm)
        album_name   = sp.get("album", "")
        spotify_year = str(sp.get("spotify_year") or "").strip()

        changed = False
        new_genre = genre
        new_year  = year

        # ── Fix A: artist name as genre ──────────────────────────────────────
        if genre and _looks_like_artist_name(genre, artist_name, album_name):
            log.debug(f"[Enricher/A] Artist-as-genre: '{genre}' for {artist_norm} — clearing")
            new_genre = ""
            stats["artist_as_genre"] += 1
            changed = True

        # ── Fix B: broad genre upgrade ────────────────────────────────────────
        if new_genre in _BROAD_UMBRELLA and artist_name:
            upgraded = _upgrade_broad_genre(new_genre, artist_name)
            if upgraded != new_genre:
                log.debug(f"[Enricher/B] Upgraded: '{new_genre}' → '{upgraded}' for {artist_norm}")
                new_genre = upgraded
                stats["upgraded"] += 1
                changed = True

        # ── Fix C: compound/duplicate tags ───────────────────────────────────
        _DEDUP = {
            "Rap/Hip Hop": "Hip-Hop",
            "Hip-Hop/Rap": "Hip-Hop",
            "Hip Hop":     "Hip-Hop",
            "Hip-Hop & Rap": "Hip-Hop",
            "R&B/Soul":    "R&B",
            "R&B & Soul":  "R&B",
            "Dance & Electronic": "Electronic",
            "Pop & Top 40": "Pop",
        }
        if new_genre in _DEDUP:
            new_genre = _DEDUP[new_genre]
            stats["deduped"] += 1
            changed = True

        # ── Fix D: fill missing year from Spotify cache ───────────────────────
        if (not new_year or new_year in ("0", "1900")) and spotify_year:
            if re.match(r"^(19|20)\d{2}$", spotify_year):
                new_year = spotify_year
                stats["year_filled"] += 1
                changed = True

        # ── Fix E: clear bad years ────────────────────────────────────────────
        if new_year and not re.match(r"^(19|20)\d{2}$", new_year):
            new_year = ""
            stats["year_cleared"] += 1
            changed = True

        if changed and not dry_run:
            updated[key] = {"genre": new_genre, "year": new_year}

        # Collect for album consensus
        al_key = f"{artist_norm}|{album_name.lower().strip()}" if album_name else None
        if al_key:
            album_genres[al_key].append(new_genre)
            if new_year and not album_years.get(al_key):
                album_years[al_key] = new_year

    # ── Fix F: album-level genre consensus ───────────────────────────────────
    # Group cache keys by album, compute consensus, stamp all tracks in album
    for al_key, genres in album_genres.items():
        consensus = _album_consensus_genre(genres)
        if not consensus:
            continue
        al_year = album_years.get(al_key, "")
        # Find all cache keys for this album
        for key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            artist_norm, _ = _parse_cache_key(key)
            sp = sp_by_key.get(key, {})
            album_name = sp.get("album", "")
            this_al_key = f"{artist_norm}|{album_name.lower().strip()}"
            if this_al_key != al_key:
                continue
            cur_genre = (updated.get(key, entry).get("genre") or "").strip()
            cur_year  = (updated.get(key, entry).get("year")  or "").strip()
            if cur_genre != consensus or (al_year and not cur_year):
                if not dry_run:
                    updated[key] = {
                        "genre": consensus,
                        "year":  al_year or cur_year,
                    }
                stats["consensus_applied"] += 1

    return updated, stats


# ---------------------------------------------------------------------------
# Phase 2: Claude AI batch enrichment
# ---------------------------------------------------------------------------

def _build_ai_prompt(albums: list[dict]) -> str:
    """
    Build a compact prompt asking Claude to return genre + year for each album.

    Each album dict has: artist, album, current_genre, current_year, track_sample
    """
    lines = [
        "You are a music metadata expert. For each album below, return the MOST SPECIFIC",
        "genre subgenre tag (e.g. 'UK Drill', 'Amapiano', 'Jazz Rap', 'Boom Bap',",
        "'Melodic Trap', 'Alt R&B', 'Neo Soul', 'Bongo Flava', 'Grime', 'UK Rap',",
        "'Conscious Hip Hop', 'Abstract Hip Hop', 'Detroit Rap', 'Phonk', 'Emo Rap',",
        "'Cloud Rap', 'Hyperpop', 'Lo-Fi', 'City Pop', 'J-R&B', 'Kompa', 'Afroswing',",
        "'Gangsta Rap', 'West Coast Rap', 'East Coast Rap', 'Southern Hip-Hop', etc.)",
        "and the ORIGINAL release year (not remaster year).",
        "",
        "Rules:",
        "- ONE genre per album (the most accurate subgenre, not a broad umbrella)",
        "- Do NOT use artist names as genre tags",
        "- Do NOT use: 'Hip-Hop', 'R&B', 'Pop', 'Rock' unless truly nothing more specific exists",
        "- If the album is clearly multi-genre, pick the genre of the majority of tracks",
        "- Year must be 4 digits (YYYY), original release (not streaming re-release)",
        "- If you genuinely don't know year, return empty string",
        "",
        "Return ONLY valid JSON, no markdown, no explanation:",
        '{"results": [{"id": N, "genre": "...", "year": "YYYY"}, ...]}',
        "",
        "Albums:",
    ]
    for i, alb in enumerate(albums):
        cur = f" (currently: {alb['current_genre']!r})" if alb.get("current_genre") else ""
        yr  = f", ~{alb['current_year']}" if alb.get("current_year") else ""
        sample = f" [tracks: {', '.join(alb['track_sample'][:3])}]" if alb.get("track_sample") else ""
        lines.append(f"{i+1}. {alb['artist']} — {alb['album']}{yr}{cur}{sample}")

    return "\n".join(lines)


def _call_claude(prompt: str, timeout_s: int = AI_TIMEOUT_S) -> Optional[dict]:
    """
    Call Claude claude-sonnet-4-6 via the Anthropic API.
    Returns parsed JSON dict or None on failure.
    """
    try:
        import urllib.request as _ur
        import urllib.error as _ue
    except ImportError:
        return None

    # Try to get API key from environment / config
    api_key = (
        (getattr(_cfg, "ANTHROPIC_API_KEY", None) or "")
        or __import__("os").environ.get("ANTHROPIC_API_KEY", "")
    )

    payload = json.dumps({
        "model":      CLAUDE_MODEL,
        "max_tokens": 2048,
        "messages":   [{"role": "user", "content": prompt}],
        "system": (
            "You are a precise music metadata expert. Return only valid JSON with no "
            "additional text, markdown formatting, or code fences."
        ),
    }).encode("utf-8")

    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
    }

    req = _ur.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload, headers=headers, method="POST",
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            with _ur.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                text = body["content"][0]["text"].strip()
                # Strip markdown fences if present
                text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
                return json.loads(text)
        except _ue.HTTPError as e:
            if e.code == 429:
                wait = 5 * (2 ** attempt)
                log.warning(f"[Enricher/AI] Rate limited — waiting {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"[Enricher/AI] HTTP {e.code}: {e.reason}")
                break
        except json.JSONDecodeError as e:
            log.warning(f"[Enricher/AI] JSON parse error: {e}")
            break
        except Exception as e:
            log.warning(f"[Enricher/AI] Request failed: {e}")
            break

    return None


def run_phase2(
    cache: dict,
    spotify_meta: dict,
    dry_run: bool = False,
    target_album: str = "",
) -> tuple[dict, dict]:
    """
    AI batch enrichment for albums that are still Unknown/empty/broad after Phase 1.

    Groups cache entries by artist+album, builds batches of BATCH_SIZE albums,
    sends each batch to Claude, and writes results back to the cache.
    """
    stats = {"albums_processed": 0, "genres_filled": 0, "years_filled": 0,
             "batches_sent": 0, "batches_failed": 0}

    # Build album-level view
    albums_map: dict[str, dict] = {}   # "Artist||Album" → metadata dict

    sp_by_key: dict[str, dict] = {}
    for entry in spotify_meta.values():
        if not isinstance(entry, dict): continue
        try:
            import genre_client as _gc
            k = _gc._cache_key(entry.get("artist", ""), entry.get("title", ""))
        except ImportError:
            k = f"{entry.get('artist','').lower()}|{entry.get('title','').lower()}"
        sp_by_key[k] = entry

    for key, entry in cache.items():
        if not isinstance(entry, dict): continue
        sp = sp_by_key.get(key, {})
        artist  = sp.get("artist", "").strip()
        album   = sp.get("album",  "").strip()
        title   = sp.get("title",  "").strip()
        year    = str(sp.get("spotify_year") or "").strip()
        if not artist or not album:
            continue

        alb_id = f"{artist}||{album}"

        # Filter to target album if specified
        if target_album and target_album.lower() not in alb_id.lower():
            continue

        genre = (entry.get("genre") or "").strip()
        cur_year = (entry.get("year") or "").strip()

        # A cached genre is considered "needs re-query" only when:
        #   - missing / Unknown, OR
        #   - broad AND the artist-override has a better specific answer.
        # This prevents re-querying broad-but-correct entries (e.g. Radiohead
        # → "Alternative Rock" is genuinely right for the artist).
        needs_genre = (
            not genre
            or genre.lower() == "unknown"
            or _is_broad_and_overridable(genre, artist, title)
        )
        needs_year  = not cur_year or cur_year in ("0", "1900")

        if not needs_genre and not needs_year:
            continue

        if alb_id not in albums_map:
            albums_map[alb_id] = {
                "artist":        artist,
                "album":         album,
                "current_genre": genre,
                "current_year":  cur_year or year,
                "track_sample":  [],
                "cache_keys":    [],
                "needs_genre":   False,
                "needs_year":    False,
            }

        a = albums_map[alb_id]
        if title and title not in a["track_sample"]:
            a["track_sample"].append(title)
        a["cache_keys"].append(key)
        if needs_genre: a["needs_genre"] = True
        if needs_year:  a["needs_year"]  = True

    albums_needing_work = [
        a for a in albums_map.values()
        if a["needs_genre"] or a["needs_year"]
    ]

    if not albums_needing_work:
        log.info("[Enricher/AI] No albums need AI enrichment — skipping Phase 2")
        return cache, stats

    log.info(
        f"[Enricher/AI] {len(albums_needing_work)} album(s) need AI enrichment "
        f"(genre: {sum(1 for a in albums_needing_work if a['needs_genre'])}, "
        f"year: {sum(1 for a in albums_needing_work if a['needs_year'])})"
    )

    updated = dict(cache)

    # Process in batches
    for batch_start in range(0, len(albums_needing_work), BATCH_SIZE):
        batch = albums_needing_work[batch_start: batch_start + BATCH_SIZE]
        prompt = _build_ai_prompt(batch)

        log.info(
            f"[Enricher/AI] Batch {batch_start//BATCH_SIZE + 1} — "
            f"{len(batch)} albums ..."
        )

        response = _call_claude(prompt)
        stats["batches_sent"] += 1

        if not response or "results" not in response:
            log.warning(f"[Enricher/AI] Batch failed or returned no results")
            stats["batches_failed"] += 1
            continue

        results = response["results"]
        for res_item in results:
            idx = res_item.get("id", 0) - 1
            if not (0 <= idx < len(batch)):
                continue
            alb = batch[idx]
            ai_genre = (res_item.get("genre") or "").strip()
            ai_year  = (res_item.get("year")  or "").strip()

            # Validate AI response
            if ai_genre and _looks_like_artist_name(ai_genre, alb["artist"], alb["album"]):
                log.debug(f"[Enricher/AI] Rejecting artist-as-genre from AI: '{ai_genre}'")
                ai_genre = ""
            if ai_year and not re.match(r"^(19|20)\d{2}$", ai_year):
                ai_year = ""

            if not ai_genre and not ai_year:
                continue

            # Stamp all cache keys for this album
            for ck in alb["cache_keys"]:
                cur = updated.get(ck, {})
                if not isinstance(cur, dict):
                    cur = {}
                new_entry = dict(cur)
                # Same guard as Site 2: overwrite only when current is
                # missing OR broad-with-better-override-available.  This
                # mirrors the Phase-2 filter so defense-in-depth is kept.
                _cur_genre = new_entry.get("genre", "")
                if ai_genre and (
                    not _cur_genre
                    or _is_broad_and_overridable(_cur_genre, alb["artist"], alb.get("album", ""))
                ):
                    new_entry["genre"] = ai_genre
                    stats["genres_filled"] += 1
                if ai_year and not new_entry.get("year"):
                    new_entry["year"] = ai_year
                    stats["years_filled"] += 1
                if not dry_run:
                    updated[ck] = new_entry

            stats["albums_processed"] += 1

        # Polite delay between batches
        if batch_start + BATCH_SIZE < len(albums_needing_work):
            time.sleep(1.0)

    return updated, stats


# ---------------------------------------------------------------------------
# Phase 3: write corrected tags back to device files via existing audit
# ---------------------------------------------------------------------------

def run_phase3(cancelled: "list[bool] | None" = None) -> dict:
    """
    Re-run the audit.py sweep with the corrected genre cache.
    Only writes tags that actually changed — MetaKey fast-path skips the rest.
    """
    stats = {"audited": 0, "changed": 0, "skipped": 0, "errors": 0}
    try:
        from audit import LibraryAuditor
        auditor = LibraryAuditor()
        # Force AUDIT_STALE_DAYS=0 for this pass so everything is re-checked
        import config as _c2
        orig_stale = _c2.AUDIT_STALE_DAYS
        _c2.AUDIT_STALE_DAYS = 0
        auditor.run()
        _c2.AUDIT_STALE_DAYS = orig_stale
        stats = auditor.stats
    except Exception as e:
        log.error(f"[Enricher/Phase3] Audit failed: {e}")
    return stats


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_enrichment(
    phase:       int  = 3,     # 1=rules only, 2=AI only, 3=both+writeback
    dry_run:     bool = False,
    target_album:str  = "",
    write_back:  bool = True,  # Phase 3 write to device
    cancelled:   "list[bool] | None" = None,
) -> dict:
    """
    Run the full enrichment pipeline.
    Returns combined stats dict.
    """
    _setup_logging()

    cache        = _load_cache(GENRE_CACHE_FILE)
    spotify_meta = _load_spotify_meta()

    log.info(
        f"[Enricher] Starting — {len(cache)} cache entries, "
        f"{len(spotify_meta)} Spotify meta entries"
    )

    all_stats: dict = {}

    if phase in (1, 3):
        log.info("[Enricher] ── Phase 1: rule-based repair ──")
        cache, stats1 = run_phase1(cache, spotify_meta, dry_run=dry_run)
        all_stats["phase1"] = stats1
        log.info(
            f"[Enricher] Phase 1 complete: "
            f"artist-as-genre={stats1['artist_as_genre']}, "
            f"upgraded={stats1['upgraded']}, "
            f"deduped={stats1['deduped']}, "
            f"year_filled={stats1['year_filled']}, "
            f"consensus={stats1['consensus_applied']}"
        )
        if not dry_run:
            _save_cache(GENRE_CACHE_FILE, cache)

    if phase in (2, 3):
        log.info("[Enricher] ── Phase 2: AI batch enrichment ──")
        cache, stats2 = run_phase2(cache, spotify_meta, dry_run=dry_run,
                                   target_album=target_album)
        all_stats["phase2"] = stats2
        log.info(
            f"[Enricher] Phase 2 complete: "
            f"albums={stats2['albums_processed']}, "
            f"genres={stats2['genres_filled']}, "
            f"years={stats2['years_filled']}, "
            f"batches={stats2['batches_sent']} ({stats2['batches_failed']} failed)"
        )
        if not dry_run:
            _save_cache(GENRE_CACHE_FILE, cache)

    if phase == 3 and write_back and not dry_run:
        log.info("[Enricher] ── Phase 3: write corrected tags to device ──")
        stats3 = run_phase3(cancelled=cancelled)
        all_stats["phase3"] = stats3
        log.info(
            f"[Enricher] Phase 3 complete: "
            f"audited={stats3.get('audited',0)}, "
            f"changed={stats3.get('changed',0)}, "
            f"skipped={stats3.get('skipped',0)}"
        )

    return all_stats


# ---------------------------------------------------------------------------
# Reporting helper: preview what would change
# ---------------------------------------------------------------------------

def preview_problems(cache: dict, spotify_meta: dict, limit: int = 50) -> list[dict]:
    """
    Scan the genre cache and return a list of problematic entries for review.
    Useful with --dry-run to see what would be fixed before committing.
    """
    problems = []

    sp_by_key: dict[str, dict] = {}
    for entry in spotify_meta.values():
        if not isinstance(entry, dict): continue
        try:
            import genre_client as _gc
            k = _gc._cache_key(entry.get("artist", ""), entry.get("title", ""))
        except ImportError:
            k = f"{entry.get('artist','').lower()}|{entry.get('title','').lower()}"
        sp_by_key[k] = entry

    for key, entry in cache.items():
        if not isinstance(entry, dict): continue
        genre = (entry.get("genre") or "").strip()
        year  = (entry.get("year")  or "").strip()
        sp    = sp_by_key.get(key, {})
        artist = sp.get("artist", key.split("|")[0] if "|" in key else key)
        album  = sp.get("album", "")
        title  = sp.get("title", "")

        issues = []
        if _looks_like_artist_name(genre, artist, album):
            issues.append(f"artist-as-genre: {genre!r}")
        # Only flag as a problem when an override exists that would give a
        # better specific answer; otherwise broad-but-correct entries like
        # Radiohead → "Alternative Rock" are noise in the report.
        if _is_broad_and_overridable(genre, artist, title):
            issues.append(f"broad: {genre!r}")
        if not genre or genre.lower() == "unknown":
            issues.append("missing genre")
        if not year or year in ("0", "1900"):
            issues.append("missing year")
        if not re.match(r"^(19|20)\d{2}$", year) and year:
            issues.append(f"bad year: {year!r}")

        if issues:
            problems.append({
                "artist": artist, "album": album, "title": title,
                "genre": genre, "year": year, "issues": issues,
            })

    problems.sort(key=lambda x: (x["artist"], x["album"]))
    return problems[:limit]


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging():
    if not logging.getLogger().handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        logging.basicConfig(level=logging.INFO, handlers=[h], force=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _setup_logging()
    parser = argparse.ArgumentParser(
        description="Bulk AI metadata audit and enrichment for the Walkman library",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python metadata_enricher.py                      # full pipeline (rules + AI + writeback)
  python metadata_enricher.py --phase 1            # rule-based repair only (fast, free)
  python metadata_enricher.py --phase 2            # AI batch only (costs tokens)
  python metadata_enricher.py --dry-run            # preview what would change
  python metadata_enricher.py --album "Kanye West - MBDTF"  # one album only
  python metadata_enricher.py --preview 100        # show top 100 problems
  python metadata_enricher.py --no-writeback       # update cache but don't push to device
        """,
    )
    parser.add_argument("--phase",   type=int, default=3, choices=[1,2,3],
                        help="1=rules only, 2=AI only, 3=both+writeback")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing to cache or device")
    parser.add_argument("--album",   default="",
                        help="Limit Phase 2 to a specific Artist||Album (substring match)")
    parser.add_argument("--no-writeback", action="store_true",
                        help="Update cache but skip Phase 3 device write-back")
    parser.add_argument("--preview", type=int, default=0, metavar="N",
                        help="Print top N problematic cache entries and exit")
    args = parser.parse_args()

    if args.preview > 0:
        cache        = _load_cache(GENRE_CACHE_FILE)
        spotify_meta = _load_spotify_meta()
        problems     = preview_problems(cache, spotify_meta, limit=args.preview)
        print(f"\nTop {len(problems)} metadata problems in {GENRE_CACHE_FILE.name}:\n")
        for p in problems:
            print(f"  {p['artist']} — {p['album']!r} / {p['title']!r}")
            print(f"    genre={p['genre']!r}  year={p['year']!r}")
            print(f"    issues: {', '.join(p['issues'])}")
        return

    stats = run_enrichment(
        phase=args.phase,
        dry_run=args.dry_run,
        target_album=args.album,
        write_back=not args.no_writeback,
    )

    print(f"\n{'─'*55}")
    print("Metadata enrichment complete")
    for phase_key, phase_stats in stats.items():
        print(f"\n  {phase_key}:")
        for k, v in phase_stats.items():
            print(f"    {k:<22}: {v}")


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────────────────
# Rev 14.7 — resolve_album_artist(album_name)
# ─────────────────────────────────────────────────────────────────────────
# Used by walkman_promote.py to decide which Artist/ folder an orphan album
# folder belongs under.  Strategy:
#   1) Spotify album-search: if the caller supplies a spotify_client, look
#      up the album name and take the top-match album's primary artist.
#      This is the most reliable path when it works (no hallucination).
#   2) AI fallback via ai_helper.call_ai with a narrow prompt asking "who
#      makes the album named X?".  Gemini returns None if it isn't sure.
# Returns the canonical artist name, or None if neither path is confident.
# Thread-safe: no module-level state is mutated.

def resolve_album_artist(album_name: str, spotify_client=None) -> str | None:
    """Resolve orphan album folder name → canonical artist name.

    This is the heavier-weight resolver Rev 14.7's walkman_promote.py prefers
    over the local-tag-agreement resolver in walkman_promote_dryrun.  It is
    especially important for orphan folders with ONE track (local agreement
    is trivially 100% but based on a single data point), and for folders
    whose track-level ARTIST tags are wrong or missing.

    Returns None on any failure — the caller is expected to fall back
    gracefully (leave the orphan folder in place, log a WARNING).
    """
    album_name = (album_name or "").strip()
    if not album_name:
        return None

    # Strip common provider prefixes before querying
    import re as _re
    stripped = _re.sub(r"^SpotiDownloader\.com\s*-\s*", "", album_name,
                       flags=_re.IGNORECASE).strip()
    # Strip common edition suffixes that confuse album search
    for suf in (" (Deluxe Edition)", " (Deluxe Version)", " (Deluxe)",
                " (Expanded Edition)", " (Expanded)",
                " (Remastered)", " (Special Edition)"):
        if stripped.endswith(suf):
            stripped = stripped[:-len(suf)].strip()

    # 1) Spotify album-search path
    if spotify_client is not None:
        try:
            res = spotify_client.search(q=f"album:{stripped}", type="album", limit=3)
            items = (res or {}).get("albums", {}).get("items", []) or []
            if items:
                top = items[0]
                artists = top.get("artists") or []
                if artists:
                    name = (artists[0] or {}).get("name", "").strip()
                    if name:
                        return name
        except Exception as _e:
            log.debug(f"[resolve_album_artist] Spotify search failed for "
                      f"{stripped!r}: {_e}")

    # 2) AI fallback
    try:
        import ai_helper as _ai
    except Exception:
        return None
    prompt = (
        "You are a music-metadata expert. Return JSON {\"artist\": \"name\"} "
        "with the PRIMARY artist who made the album named below. "
        "If it's a collaboration with a clear primary, give the primary. "
        "If it's a compilation, return \"Various Artists\". "
        "If you're not confident, return {\"artist\": \"\"}.\n\n"
        f"Album: {stripped}"
    )
    try:
        result = _ai.call_ai(prompt, max_tokens=64, temperature=0.1)
    except Exception as _e:
        log.debug(f"[resolve_album_artist] AI call failed: {_e}")
        return None
    if not result:
        return None
    artist = str((result or {}).get("artist", "")).strip()
    if not artist:
        return None
    # Guard against obvious hallucinations — single-token "Unknown" etc.
    if artist.lower() in ("unknown", "n/a", "none", "various"):
        return None if artist.lower() != "various" else "Various Artists"
    return artist
