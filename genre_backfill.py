"""
genre_backfill.py  —  Empty Genre Backfill Worker  (MK15)
==========================================================
CTO directive: "You have 278 empty entries. Since Gemini is hitting rate
limits (429 errors), we should switch the Backfill worker to use Groq
(Llama 3.1) exclusively for these specific gaps."

This module finds all tracks in .genre_cache.json with an empty genre
string and classifies them using the Groq genre waterfall that proved
effective for niche global genres in the live log:
  - MC Teuzin PV → Funk Carioca  ✓
  - Abu Abdul Malik → Arabic Music ✓
  - Bello Falcao → Soukous ✓
  - Ferre Gola → Soukous ✓
  - Naiboi → Genge ✓

Run modes:
  CLI:     python genre_backfill.py [--dry-run] [--limit N]
  Import:  backfill_empty_genres(dry_run=False, limit=None)

Results are written back via genre_client._genre_cache_set() (singleton)
and flushed to disk at completion.
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass, field

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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BackfillResult:
    total_empty:     int
    attempted:       int
    filled:          int
    still_empty:     int
    skipped:         int
    dry_run:         bool
    results:         list = field(default_factory=list)
    elapsed_s:       float = 0.0

    def summary(self) -> str:
        return (
            f"{'[DRY RUN] ' if self.dry_run else ''}"
            f"Empty genres: {self.total_empty} found, {self.attempted} attempted, "
            f"{self.filled} filled ({self.filled/max(1,self.attempted)*100:.0f}%), "
            f"{self.still_empty} still empty. "
            f"({self.elapsed_s:.1f}s)"
        )


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------

def _groq_classify_genre(artist: str, title: str) -> str:
    """
    Classify genre using Groq exclusively (bypasses Gemini/rate-limit trap).
    Uses the MK11 overhauled anti-Hip-Hop-default prompt.
    Returns genre string or "" on failure.
    """
    try:
        from genre_client import _claude_genre_fallback as _fb
        result = _fb(artist, title)
        return result.get("genre", "").strip()
    except Exception:
        pass

    # Direct Groq fallback if genre_client import fails
    try:
        import ai_helper as _ai
        prompt = (
            f"You are a world-class music genre expert with encyclopedic knowledge "
            f"of global music, including African subgenres, diaspora music, and "
            f"internet microgenres.\n\n"
            f"Artist: {artist}\nTitle: {title}\n\n"
            f"CRITICAL: NEVER default to Hip-Hop for unknowns. Return empty string "
            f"if not confident. African music: Afrobeats=Nigerian, Bongo Flava=Tanzanian, "
            f"Genge=Kenyan, Soukous=Congolese, Kizomba=Angolan.\n"
            f"Return ONLY JSON: {{\"genre\": \"string\"}}"
        )
        result = _ai.call_ai(prompt, max_tokens=64, temperature=0.1)
        return str(result.get("genre", "")).strip() if result else ""
    except Exception as e:
        log.debug(f"[Backfill] Groq call failed for {artist!r}: {e}")
        return ""


def backfill_empty_genres(
    dry_run: bool = True,
    limit: int | None = None,
    rate_limit_s: float = 0.4,
    cancelled_flag: list | None = None,
) -> BackfillResult:
    """
    Find all empty-genre entries in the genre cache and classify them via Groq.

    Parameters
    ----------
    dry_run      : if True, log what would be written but don't update cache
    limit        : max number of tracks to process (None = all)
    rate_limit_s : seconds between Groq calls (default 0.4 = ~2.5 req/s)
    cancelled_flag : [False] mutable flag; set True externally to stop

    Returns BackfillResult with per-track details.
    """
    from genre_client import _get_genre_cache, _genre_cache_set, flush_genre_cache

    t_start = time.time()
    cache = _get_genre_cache()

    # Find empty-genre entries (both "" and missing genre key)
    empty_keys = [
        k for k, v in cache.items()
        if isinstance(v, dict) and not v.get("genre", "").strip()
    ]

    log.info(
        f"[Backfill] Found {len(empty_keys)} empty-genre entries in cache "
        f"(from {len(cache)} total)"
    )

    result = BackfillResult(
        total_empty=len(empty_keys),
        attempted=0,
        filled=0,
        still_empty=0,
        skipped=0,
        dry_run=dry_run,
    )

    # Apply limit
    targets = empty_keys[:limit] if limit else empty_keys

    for i, key in enumerate(targets):
        if cancelled_flag and cancelled_flag[0]:
            log.info(f"[Backfill] Cancelled after {i} tracks")
            break

        # Parse artist|title from cache key
        if "|" not in key:
            result.skipped += 1
            continue
        artist, title = key.split("|", 1)
        if not artist or not title:
            result.skipped += 1
            continue

        result.attempted += 1

        genre = _groq_classify_genre(artist, title)

        log.info(
            f"  [Backfill] [{i+1}/{len(targets)}] "
            f"{artist!r} — {title!r} → {(genre if genre else '(still empty)')!r}"
        )

        if genre:
            if not dry_run:
                existing_year = cache.get(key, {}).get("year", "")
                _genre_cache_set(key, {"genre": genre, "year": existing_year})
            result.filled += 1
            result.results.append({
                "key": key, "genre": genre, "dry_run": dry_run
            })
        else:
            result.still_empty += 1

        # Rate limit between Groq calls
        if i < len(targets) - 1:
            time.sleep(rate_limit_s)

    if not dry_run:
        flush_genre_cache()

    result.elapsed_s = time.time() - t_start
    log.info(f"[Backfill] {result.summary()}")
    return result


def get_empty_count() -> int:
    """Quick check: how many empty-genre entries are in the cache?"""
    try:
        from genre_client import _get_genre_cache
        cache = _get_genre_cache()
        return sum(1 for v in cache.values()
                   if isinstance(v, dict) and not v.get("genre", "").strip())
    except Exception:
        # Fall back to direct file read
        cache_path = _cfg("GENRE_CACHE_FILE", pathlib.Path(".genre_cache.json"))
        if pathlib.Path(str(cache_path)).exists():
            data = json.loads(pathlib.Path(str(cache_path)).read_text(encoding="utf-8"))
            return sum(1 for v in data.values()
                       if isinstance(v, dict) and not v.get("genre", "").strip())
        return -1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Backfill empty genre entries in the genre cache using Groq"
    )
    parser.add_argument("--execute", action="store_true",
                        help="Actually write results (default: dry run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max tracks to process (default: all 278)")
    parser.add_argument("--rate", type=float, default=0.4,
                        help="Seconds between Groq calls (default: 0.4)")
    parser.add_argument("--count", action="store_true",
                        help="Just count empty entries and exit")
    args = parser.parse_args()

    if args.count:
        n = get_empty_count()
        print(f"Empty genre entries: {n}")
        sys.exit(0)

    result = backfill_empty_genres(
        dry_run=not args.execute,
        limit=args.limit,
        rate_limit_s=args.rate,
    )
    print(result.summary())
    if result.results:
        print(f"\nSample fills ({min(10, len(result.results))} shown):")
        for r in result.results[:10]:
            parts = r["key"].split("|", 1)
            print(f"  {parts[0]!r} — {parts[1]!r} → {r['genre']!r}")
    sys.exit(0 if result.still_empty == 0 else 1)
