"""
ai_bulk_metadata.py  —  Bulk metadata resolution for WalkmanSyncAgent
======================================================================
Replaces the per-file AI call pattern (1 call per MP3 → instant 429s)
with a batched approach: collect ALL tracks needing metadata, send them
in batches of 50 to the LLM, parse one JSON response per batch.

Also provides:
  - corrupt_file_scan(): detect 00:00 / zero-byte / tag-only MP3s before
    they get transferred to the Walkman (the "unplayable files" problem)
  - openrouter_confirm(): one-shot test that OpenRouter is live

Provider waterfall (uses existing ai_helper.py — all backoff state shared):
  1. Gemini direct (your key)
  2. OpenRouter free models (gemini-flash, deepseek-free, llama-3.1-8b:free)
  3. Returns empty if all fail (never crashes the pipeline)

Usage:
  from ai_bulk_metadata import pre_resolve, apply_resolved, corrupt_file_scan

  # Before the tagging loop — one batch call for all files
  resolved = pre_resolve(file_list)

  # Inside the loop — instant dict lookup, zero API calls
  for f in file_list:
      apply_resolved(f, resolved)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import struct
import time
import unicodedata
from typing import Any

import config

log = logging.getLogger(__name__)

try:
    import config as _cfg
    _CACHE_PATH = _cfg.BASE_DIR / ".bulk_metadata_cache.json"
except Exception:
    _CACHE_PATH = pathlib.Path(__file__).parent / ".bulk_metadata_cache.json"

BATCH_SIZE            = 50
INTER_BATCH_DELAY_S   = 1.5
CROSS_CHECK_ENABLED   = True

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache: dict[str, dict] = {}
_cache_loaded = False


def _load_cache() -> None:
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if _CACHE_PATH.exists():
            _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug(f"  [BulkMeta] Cache load failed: {e}")
        _cache = {}


def _save_cache() -> None:
    try:
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as e:
        log.debug(f"  [BulkMeta] Cache save failed: {e}")


def _cache_key(title: str, artist: str, album: str) -> str:
    def _n(s):
        return unicodedata.normalize("NFKD", str(s or "")).encode("ascii","ignore").decode().lower().strip()
    return f"{_n(artist)}|||{_n(title)}|||{_n(album)}"


# ---------------------------------------------------------------------------
# OpenRouter confirmation (one-time liveness check)
# ---------------------------------------------------------------------------
def openrouter_confirm() -> dict:
    """
    Send a minimal test request to OpenRouter.
    Returns {"ok": True, "model": str} on success or {"ok": False, "error": str}.
    """
    import urllib.request, urllib.error
    # Check both config (set at import time) and live os.environ (set after .env load)
    or_key = (getattr(config, "OPENROUTER_API_KEY", "") or
              os.environ.get("OPENROUTER_API_KEY", ""))
    if not or_key:
        return {"ok": False, "error": "OPENROUTER_API_KEY not set — add it to your .env file"}

    model = "google/gemini-flash-1.5"
    payload = json.dumps({
        "model": model,
        "max_tokens": 20,
        "messages": [{"role": "user", "content": 'Reply with just: {"status": "ok"}'}],
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {or_key}",
            "HTTP-Referer": "https://github.com/walkman-sync",
            "X-Title": "WalkmanSync",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            return {"ok": True, "model": model, "response": content[:100]}
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode(errors="ignore")[:200]
        except: pass
        return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Batch prompt builder
# ---------------------------------------------------------------------------
def _build_prompt(batch: list[dict]) -> str:
    items = []
    for i, t in enumerate(batch):
        ti = t.get("title","") or pathlib.Path(t.get("filename","")).stem or "Unknown"
        items.append(
            f"{i+1}. title={ti!r} "
            f"artist={t.get('artist','')!r} "
            f"album={t.get('album','')!r} "
            f"filename={t.get('filename','')!r}"
        )
    tracks_text = "\n".join(items)
    return (
        "You are a music metadata expert. For each track below, return a JSON array "
        "of objects with these fields:\n"
        "  track_number (integer, 1-based), disc_number (integer, default 1),\n"
        "  genre (string, specific e.g. \'Hip-Hop\', \'Afrobeats\'), "
        "release_year (4-digit integer or null)\n\n"
        "IMPORTANT: Return ONLY a JSON array with exactly one object per track, "
        "in the same order. No markdown, no explanation.\n\n"
        "Tracks:\n" + tracks_text
    )



# ---------------------------------------------------------------------------
# LLM call (delegates to existing ai_helper waterfall)
# ---------------------------------------------------------------------------
def _call_llm(prompt: str) -> list[dict] | None:
    """Call the AI waterfall and return a parsed list of metadata dicts."""
    try:
        import ai_helper
        result = ai_helper.call_ai(prompt, max_tokens=2048, temperature=0.1)
        if result is None:
            return None
        # call_ai returns a dict; for batch we expect a list inside it
        if isinstance(result, list):
            return result
        # Sometimes the model wraps the array in a key
        for key in ("tracks", "results", "items", "data"):
            if key in result and isinstance(result[key], list):
                return result[key]
        # If it returned a dict with integer keys, convert
        if all(str(k).isdigit() or str(k).startswith(str(i)) for i,k in enumerate(result.keys(), 1)):
            return list(result.values())
        return None
    except Exception as e:
        log.debug(f"  [BulkMeta] LLM call failed: {e}")
        return None


def _normalise_item(raw: Any) -> dict:
    """Normalise a single LLM result item into our standard format."""
    if not isinstance(raw, dict):
        return {}
    return {
        "track_number":  int(raw["track_number"])  if str(raw.get("track_number","")).isdigit() else None,
        "disc_number":   int(raw["disc_number"])    if str(raw.get("disc_number","")).isdigit()  else 1,
        "genre":         str(raw["genre"]).strip()  if raw.get("genre") else None,
        "release_year":  int(raw["release_year"])   if str(raw.get("release_year","")).isdigit() else None,
    }


# ---------------------------------------------------------------------------
# Core resolve
# ---------------------------------------------------------------------------
def resolve_batch(tracks: list[dict]) -> dict[str, dict]:
    """
    Resolve metadata for a list of tracks in bulk.

    Each track dict must have: key (unique id), title, artist, album, filename
    Returns: {key: {track_number, disc_number, genre, release_year}}

    Results are cached; subsequent calls for the same track are instant.
    """
    _load_cache()

    # Check which are already cached
    needed  = []
    results = {}
    for t in tracks:
        ck = _cache_key(t.get("title",""), t.get("artist",""), t.get("album",""))
        if ck in _cache:
            results[t["key"]] = _cache[ck]
        else:
            needed.append(t)

    if not needed:
        return results

    log.info(
        f"[BulkMeta] Resolving {len(needed)} tracks in "
        f"{(len(needed) + BATCH_SIZE - 1) // BATCH_SIZE} batch(es) ..."
    )

    # Process in batches
    for batch_start in range(0, len(needed), BATCH_SIZE):
        batch = needed[batch_start:batch_start + BATCH_SIZE]
        prompt = _build_prompt(batch)

        raw_list = _call_llm(prompt)

        if raw_list and len(raw_list) == len(batch):
            for t, raw in zip(batch, raw_list):
                meta = _normalise_item(raw)
                ck   = _cache_key(t.get("title",""), t.get("artist",""), t.get("album",""))
                _cache[ck] = meta
                results[t["key"]] = meta

            # Cross-check low-confidence results with a second call
            if CROSS_CHECK_ENABLED:
                low_conf = [
                    t for t in batch
                    if not results.get(t["key"], {}).get("track_number")
                    or not results.get(t["key"], {}).get("genre")
                ]
                if low_conf:
                    log.debug(f"  [BulkMeta] Cross-checking {len(low_conf)} low-confidence results")
                    xc_prompt = _build_prompt(low_conf)
                    xc_list   = _call_llm(xc_prompt)
                    if xc_list and len(xc_list) == len(low_conf):
                        for t, raw in zip(low_conf, xc_list):
                            xc_meta = _normalise_item(raw)
                            ck = _cache_key(t.get("title",""), t.get("artist",""), t.get("album",""))
                            # Merge: fill gaps only, don't overwrite good data
                            existing = _cache.get(ck, {})
                            for field in ("track_number", "disc_number", "genre", "release_year"):
                                if not existing.get(field) and xc_meta.get(field):
                                    existing[field] = xc_meta[field]
                            _cache[ck] = existing
                            results[t["key"]] = existing
        else:
            # Batch failed — mark as empty so we don't re-try in this run
            for t in batch:
                ck = _cache_key(t.get("title",""), t.get("artist",""), t.get("album",""))
                if ck not in _cache:
                    _cache[ck] = {}
            log.warning(
                f"  [BulkMeta] Batch {batch_start//BATCH_SIZE + 1} failed "
                f"(LLM returned {len(raw_list) if raw_list else 0} items, expected {len(batch)})"
            )

        if batch_start + BATCH_SIZE < len(needed):
            time.sleep(INTER_BATCH_DELAY_S)

    _save_cache()
    return results


def resolve_single(title: str, artist: str, album: str, filename: str = "") -> dict:
    """Resolve a single track — convenience wrapper around resolve_batch."""
    key = _cache_key(title, artist, album)
    result = resolve_batch([{"key": key, "title": title, "artist": artist,
                              "album": album, "filename": filename}])
    return result.get(key, {})


# ---------------------------------------------------------------------------
# Integration helpers (drop-in replacements for per-file AI calls)
# ---------------------------------------------------------------------------
def pre_resolve(files: list) -> dict:
    """
    Pre-resolve metadata for a list of audio file objects.

    Accepts any object with .title, .artist, .album, .path attributes
    (or a plain dict with those keys). Returns {path_str: metadata_dict}.
    """
    _load_cache()

    batch = []
    for f in files:
        if isinstance(f, dict):
            title  = f.get("title","")
            artist = f.get("artist","")
            album  = f.get("album","")
            fname  = f.get("filename","") or f.get("path","")
            key    = str(f.get("path", fname))
        else:
            title  = getattr(f, "title",  "") or ""
            artist = getattr(f, "artist", "") or ""
            album  = getattr(f, "album",  "") or ""
            fname  = getattr(f, "path",   pathlib.Path("")).name
            key    = str(getattr(f, "path", fname))

        # Skip if already cached
        ck = _cache_key(title, artist, album)
        if ck in _cache:
            continue

        # Skip if not missing any important metadata
        if title and artist and album:
            # Only skip if the AI call is unlikely to help
            # (all three fields present means the file is probably OK)
            pass

        batch.append({
            "key":      key,
            "title":    title,
            "artist":   artist,
            "album":    album,
            "filename": str(fname),
        })

    if not batch:
        return {}

    resolved = resolve_batch(batch)
    # Re-key by path string
    return {b["key"]: resolved.get(b["key"], {}) for b in batch}


def apply_resolved(f, resolved: dict) -> bool:
    """
    Apply pre-resolved metadata to a file object or dict.
    Returns True if any field was filled.
    """
    if isinstance(f, dict):
        key = str(f.get("path", f.get("filename","")))
    else:
        key = str(getattr(f, "path", ""))

    meta = resolved.get(key, {})
    if not meta:
        return False

    changed = False
    _set = (lambda obj, field, val: setattr(obj, field, val)) if not isinstance(f, dict) else (lambda obj, field, val: obj.update({field: val}))
    _get = (lambda obj, field: getattr(obj, field, None)) if not isinstance(f, dict) else (lambda obj, field: obj.get(field))

    if not _get(f, "track_number") and meta.get("track_number"):
        _set(f, "track_number", str(meta["track_number"]))
        changed = True
    if not _get(f, "genre") and meta.get("genre"):
        # MK38.0: SEMANTIC WRITE BARRIER. Bulk-AI genre writes are inference-
        # derived; under the default freeze they're blocked. We skip silently
        # (logging at debug only) because this code path is invoked in tight
        # loops over thousands of files; the barrier itself logs at WARNING
        # the first time per source/field combination via its telemetry.
        try:
            from mk38_0_semantic_write_barrier import (
                assert_field_allowed, SemanticWriteBlocked,
            )
            # Source classification: this function is the AI-bulk metadata
            # path, so the source is ai_inference. Confidence is whatever
            # the upstream provider attached, defaulting to None (unknown).
            assert_field_allowed(
                "genre", meta["genre"],
                source="ai_inference",
                confidence=meta.get("genre_confidence"),
            )
            _set(f, "genre", meta["genre"])
            changed = True
        except SemanticWriteBlocked:
            # Barrier blocked the write; do not propagate to tags.
            pass
        except ImportError:
            # Barrier not present (very old codebase) — preserve legacy behavior
            _set(f, "genre", meta["genre"])
            changed = True
    if not _get(f, "release_year") and meta.get("release_year"):
        _set(f, "release_year", str(meta["release_year"]))
        changed = True
    if meta.get("disc_number", 1) != 1 and not _get(f, "disc_number"):
        _set(f, "disc_number", str(meta["disc_number"]))
        changed = True

    return changed


# ---------------------------------------------------------------------------
# Corrupt / zero-duration file detector (fixes the unplayable MP3 issue)
# ---------------------------------------------------------------------------
def _is_corrupt_mp3(path: pathlib.Path) -> tuple[bool, str]:
    """
    Check if an MP3/M4A file is unplayable:
      - Zero bytes or near-zero (< 4 KB)
      - Pure ID3 tag with no audio frames (00:00 duration)
      - Valid header magic but no audio data after the tag
    Returns (is_corrupt, reason).
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return True, "zero bytes"
        if size < 4096:
            return True, f"suspiciously small ({size} bytes)"

        with open(path, "rb") as fh:
            header = fh.read(10)

        ext = path.suffix.lower()

        if ext == ".mp3":
            # Check for ID3 tag
            if header[:3] == b"ID3":
                # Parse ID3 size (4 bytes, each 7 bits)
                raw_size = header[6:10]
                tag_size = (
                    ((raw_size[0] & 0x7F) << 21) |
                    ((raw_size[1] & 0x7F) << 14) |
                    ((raw_size[2] & 0x7F) << 7)  |
                    (raw_size[3] & 0x7F)
                ) + 10  # +10 for the header itself
                if tag_size >= size - 128:
                    # Tag takes up virtually the entire file → tag-only file
                    return True, f"tag-only (ID3 tag {tag_size}B, file {size}B)"
            elif not (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
                # No ID3 and no sync bytes
                return True, "no MP3 sync bytes and no ID3 tag"

        elif ext in (".m4a", ".aac"):
            if len(header) >= 8 and header[4:8] == b"ftyp":
                return False, ""  # valid M4A
            if header[0] == 0xFF and (header[1] & 0xF0) == 0xF0:
                return False, ""  # ADTS AAC
            if size < 8192:
                return True, f"M4A too small ({size} bytes)"

        return False, ""

    except Exception as e:
        return False, ""  # can't determine, assume OK


def corrupt_file_scan(
    folder: str | pathlib.Path,
    extensions: tuple = (".mp3", ".m4a", ".flac", ".aac"),
) -> list[dict]:
    """
    Scan a folder for unplayable audio files.

    Returns list of dicts:
      {path, size, reason, ext}

    Use this BEFORE transferring to the Walkman — catching corrupt files
    locally is much faster than discovering them on the device.

    Example:
      problems = corrupt_file_scan("C:/Users/FeisalK/Downloads")
      for p in problems:
          print(f"CORRUPT: {p['path']} — {p['reason']}")
    """
    folder = pathlib.Path(folder)
    if not folder.is_dir():
        return []

    problems = []
    checked  = 0

    for ext in extensions:
        for fpath in folder.rglob(f"*{ext}"):
            if not fpath.is_file():
                continue
            checked += 1
            if ext in (".mp3", ".m4a", ".aac"):
                bad, reason = _is_corrupt_mp3(fpath)
                if bad:
                    problems.append({
                        "path":   str(fpath),
                        "size":   fpath.stat().st_size,
                        "reason": reason,
                        "ext":    ext,
                    })
            elif ext == ".flac":
                try:
                    with open(fpath, "rb") as fh:
                        magic = fh.read(4)
                    if magic != b"fLaC":
                        size = fpath.stat().st_size
                        problems.append({
                            "path":   str(fpath),
                            "size":   size,
                            "reason": f"not a valid FLAC (magic={magic!r})",
                            "ext":    ext,
                        })
                except Exception:
                    pass

    log.info(
        f"[CorruptScan] Checked {checked} files in {folder}: "
        f"{len(problems)} suspect file(s) found"
    )
    return problems


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, argparse

    # Load .env so the key is available when run from the command line directly
    # (config.py's dotenv loader only fires when config is imported as part of the app)
    try:
        import pathlib as _pl
        _env = _pl.Path(__file__).parent / ".env"
        if _env.exists():
            for _line in _env.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="ai_bulk_metadata CLI")
    parser.add_argument("--scan-corrupt", metavar="FOLDER",
                        help="Scan a folder for unplayable audio files")
    parser.add_argument("--test-openrouter", action="store_true",
                        help="Confirm OpenRouter API key is working")
    parser.add_argument("--batch", metavar="JSON_FILE",
                        help="Resolve metadata for tracks listed in a JSON file")
    args = parser.parse_args()

    if args.test_openrouter:
        result = openrouter_confirm()
        if result["ok"]:
            print(f"✅ OpenRouter OK  model={result['model']}  response={result.get('response','')}") 
        else:
            print(f"❌ OpenRouter FAILED: {result['error']}")
            sys.exit(1)

    if args.scan_corrupt:
        problems = corrupt_file_scan(args.scan_corrupt)
        if not problems:
            print(f"✅ No corrupt files found in {args.scan_corrupt}")
        else:
            print(f"\n⚠️  {len(problems)} suspect file(s):")
            for p in sorted(problems, key=lambda x: x["size"]):
                print(f"  [{p['ext']}] {p['size']:>10,} bytes  {p['reason']:30}  {p['path']}")

    if args.batch:
        with open(args.batch) as f:
            tracks = json.load(f)
        results = resolve_batch(tracks)
        print(json.dumps(results, indent=2))
