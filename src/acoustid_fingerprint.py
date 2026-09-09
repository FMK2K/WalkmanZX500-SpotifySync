"""
acoustid_fingerprint.py — MK39 Phase III G.1: acoustic fingerprint resolution
================================================================================

Council MK39.1 Target G success criterion: "Any track with a valid audio
fingerprint is matched to MusicBrainz and receives at minimum artist, album,
title, and year with 'acoustic_fingerprint' provenance."

Architecture (adversarial-review locked):
  1. fpcalc subprocess (Chromaprint) → (duration, fingerprint)
        - hard 15s timeout
        - empty fingerprint = silent or too-short audio → return None
        - fpcalc absent → log INFO once, return None (graceful degrade)
  2. AcoustID HTTP lookup → list of recordings with score
        - mockable via injected http_get callable for tests
        - rate-limited to 3 req/sec (free tier limit)
        - score threshold ≥ 0.85 (configurable)
        - tie-break by recording length proximity to fpcalc duration
  3. Return ResolvedIdentity dataclass with provenance="acoustic_fingerprint"

Caching:
  Fingerprints are cached by file SHA-256 in `.acoustid_cache.json` under
  the working directory. Re-running on the same audio is a no-op.

Operator setup:
  - Install Chromaprint:        apt install libchromaprint-tools  (Linux)
                                  brew install chromaprint           (macOS)
                                  Windows: bundled with platform
  - Install pyacoustid:         (optional; used as a fallback lookup path)
  - AcoustID API key:           free at https://acoustid.org/new-application
                                  set config.ACOUSTID_API_KEY
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

log = logging.getLogger(__name__)

# Hard limits per adversarial review
FPCALC_TIMEOUT_SEC = 15.0
ACOUSTID_TIMEOUT_SEC = 20.0
DEFAULT_SCORE_THRESHOLD = 0.85
RATE_LIMIT_GAP_SEC = 0.34  # 3 req/sec for free tier

ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"

# Module-level once-warned flag to avoid spamming the log on every track
# when fpcalc is absent in the environment.
_FPCALC_MISSING_WARNED = False
_FPCALC_MISSING_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class FingerprintResult:
    """Output of fpcalc on an audio file."""
    duration_sec: float
    fingerprint: str  # the Chromaprint base64-ish string


@dataclasses.dataclass(frozen=True)
class ResolvedIdentity:
    """High-confidence identity resolved via fingerprint + AcoustID lookup.

    All string fields default to "" (never None) so the consumer can rely on
    `.artist`, `.title`, etc. being safe to pass to write barriers.
    """
    artist: str = ""
    album: str = ""
    title: str = ""
    year: str = ""
    musicbrainz_recording_id: str = ""
    score: float = 0.0
    provenance: str = "acoustic_fingerprint"


# ─────────────────────────────────────────────────────────────────────────
# Layer 1 — Chromaprint subprocess wrapper
# ─────────────────────────────────────────────────────────────────────────

def _fpcalc_path() -> Optional[str]:
    """Locate fpcalc binary, honoring config.FPCALC_PATH override.
    Returns None if unavailable.

    Search order (each step explicitly logged on first session match):
      1. config.FPCALC_PATH (explicit operator override)
      2. shutil.which() — system PATH
      3. Current working directory + script directory — needed on Windows
         because PowerShell does NOT search CWD by default. Production
         LOG_10_5_26 showed operator hitting this:
             > fpcalc -version
             fpcalc: The term 'fpcalc' is not recognized...
             > .\fpcalc -version    # works
             fpcalc version 1.6.0 ...
    """
    # 1. config override
    try:
        import config  # type: ignore
        explicit = getattr(config, "FPCALC_PATH", "")
        if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
    except ImportError:
        pass

    # 2. PATH lookup
    found = shutil.which("fpcalc")
    if found:
        return found

    # 3. CWD + script-relative probe — Windows necessity
    candidates = []
    cwd = os.getcwd()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in (cwd, script_dir):
        for name in ("fpcalc.exe", "fpcalc"):
            candidates.append(os.path.join(base, name))
    for candidate in candidates:
        if os.path.isfile(candidate):
            # Loud INFO so operator sees the unexpected probe location.
            # If they didn't put it there, this is a security signal.
            log.info(
                f"  [AcoustID] fpcalc found in non-PATH location: {candidate} "
                f"(set config.FPCALC_PATH to suppress this probe message)"
            )
            return candidate

    return None


def fingerprint_file(audio_path: os.PathLike,
                       *, timeout: float = FPCALC_TIMEOUT_SEC
                       ) -> Optional[FingerprintResult]:
    """Run fpcalc on an audio file. Returns FingerprintResult or None.

    None outcomes (no exception):
        - fpcalc binary not present (logged INFO once per session)
        - audio file too short or silent (empty fingerprint)
        - subprocess timed out
        - any subprocess error
    """
    global _FPCALC_MISSING_WARNED
    audio_path = str(audio_path)
    if not os.path.isfile(audio_path):
        log.warning(f"  [AcoustID] fingerprint_file: not a file: {audio_path}")
        return None

    binary = _fpcalc_path()
    if binary is None:
        with _FPCALC_MISSING_LOCK:
            if not _FPCALC_MISSING_WARNED:
                log.info(
                    "  [AcoustID] fpcalc binary not found on PATH "
                    "(install: apt install libchromaprint-tools  /  "
                    "brew install chromaprint). Acoustic fingerprint "
                    "fallback is disabled for this session."
                )
                _FPCALC_MISSING_WARNED = True
        return None

    try:
        proc = subprocess.run(
            [binary, audio_path],
            capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning(f"  [AcoustID] fpcalc timed out on {audio_path}")
        return None
    except (OSError, FileNotFoundError) as e:
        log.debug(f"  [AcoustID] fpcalc subprocess failed: {e}")
        return None

    if proc.returncode != 0:
        # fpcalc emits "ERROR: Empty fingerprint" for silent/too-short audio
        # — that's expected and not actionable.
        if "Empty fingerprint" not in (proc.stderr or ""):
            log.debug(f"  [AcoustID] fpcalc rc={proc.returncode}: "
                       f"{(proc.stderr or '').strip()[:200]}")
        return None

    duration: Optional[float] = None
    fp: Optional[str] = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("DURATION="):
            try: duration = float(line.split("=", 1)[1])
            except ValueError: pass
        elif line.startswith("FINGERPRINT="):
            fp = line.split("=", 1)[1].strip()

    if not fp or duration is None or duration <= 0:
        return None
    return FingerprintResult(duration_sec=duration, fingerprint=fp)


# ─────────────────────────────────────────────────────────────────────────
# Layer 2 — AcoustID HTTP lookup
# ─────────────────────────────────────────────────────────────────────────

# Default HTTP getter — overridable for tests via dependency injection.
def _default_http_get(url: str, *, timeout: float) -> dict:
    """Default HTTP GET → parsed JSON. Tests inject a mock instead."""
    req = urllib.request.Request(url, headers={"User-Agent": "WalkmanSync/12.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Throttle state (module-level — covers all callers in process)
_THROTTLE_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0


def _throttle_acoustid() -> None:
    """Enforce ≤ 3 req/sec across all callers (free tier limit)."""
    global _LAST_REQUEST_TS
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_REQUEST_TS
        if elapsed < RATE_LIMIT_GAP_SEC:
            time.sleep(RATE_LIMIT_GAP_SEC - elapsed)
        _LAST_REQUEST_TS = time.monotonic()


def lookup_fingerprint(
    fp_result: FingerprintResult,
    *,
    api_key: str,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    http_get: Callable[..., dict] = _default_http_get,
    timeout: float = ACOUSTID_TIMEOUT_SEC,
) -> Optional[ResolvedIdentity]:
    """Look up a Chromaprint fingerprint via the AcoustID web service.

    Returns a ResolvedIdentity if a match scores ≥ score_threshold;
    otherwise None.

    The HTTP getter is injected to make this fully testable without
    network access (sandbox AcoustID egress is blocked).
    """
    if not api_key:
        log.debug("  [AcoustID] no api_key supplied; skipping lookup")
        return None

    params = {
        "client":      api_key,
        "duration":    int(round(fp_result.duration_sec)),
        "fingerprint": fp_result.fingerprint,
        "meta":        "recordings+releasegroups+compress",
        "format":      "json",
    }
    url = ACOUSTID_LOOKUP_URL + "?" + urllib.parse.urlencode(params)

    _throttle_acoustid()
    # 2026-07-24 — let HTTP/transport errors (bad API key, network failure,
    # rate limiting, etc.) propagate instead of swallowing them into a bare
    # None here. resolve_identity() needs to tell "the request itself failed"
    # apart from "the request succeeded and found nothing," because only the
    # latter is safe to cache as a permanent negative match — see its
    # docstring for the bug this fixes (a bad API key silently and
    # permanently negative-cached every file it ever touched).
    body = http_get(url, timeout=timeout)

    return _pick_best_match(body, fp_result.duration_sec, score_threshold)


def _pick_best_match(body: dict, fp_duration: float,
                       score_threshold: float) -> Optional[ResolvedIdentity]:
    """Parse AcoustID JSON, pick highest-score match above threshold.
    Tie-break by recording-duration proximity to fingerprint duration."""
    if not isinstance(body, dict) or body.get("status") != "ok":
        return None
    results = body.get("results", []) or []
    candidates: list[Tuple[float, float, dict]] = []

    for r in results:
        score = float(r.get("score", 0.0)) if isinstance(r, dict) else 0.0
        if score < score_threshold:
            continue
        for rec in (r.get("recordings", []) if isinstance(r, dict) else []) or []:
            if not isinstance(rec, dict):
                continue
            rec_dur = float(rec.get("duration", 0.0))
            dur_delta = abs(rec_dur - fp_duration) if rec_dur else 999.0
            candidates.append((score, dur_delta, rec))

    if not candidates:
        return None
    # Sort: highest score first; if score tied, smallest dur_delta first
    candidates.sort(key=lambda t: (-t[0], t[1]))
    score, _delta, rec = candidates[0]

    artists = rec.get("artists", []) or []
    artist  = (artists[0].get("name", "") if artists and isinstance(artists[0], dict) else "")
    title   = rec.get("title", "") or ""
    rgs     = rec.get("releasegroups", []) or []
    album   = ""
    year    = ""
    if rgs and isinstance(rgs[0], dict):
        album = rgs[0].get("title", "") or ""
        # Year may live under various release dates; keep simple
        first_release = rgs[0].get("first_release_date", "") or ""
        year = first_release[:4] if first_release else ""

    return ResolvedIdentity(
        artist=str(artist),
        album=str(album),
        title=str(title),
        year=str(year),
        musicbrainz_recording_id=str(rec.get("id", "")),
        score=score,
        provenance="acoustic_fingerprint",
    )


# ─────────────────────────────────────────────────────────────────────────
# Layer 3 — End-to-end resolver with caching
# ─────────────────────────────────────────────────────────────────────────

DEFAULT_CACHE_PATH = ".acoustid_cache.json"


def _file_sha256(path: os.PathLike, *, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of a file's contents — cache key for fingerprint results.

    Why SHA over inode: surviving a copy/move; surviving filesystem changes;
    cheap enough at ~1GB/sec on modern hardware."""
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for block in iter(lambda: f.read(chunk_size), b""):
            h.update(block)
    return h.hexdigest()


def _load_cache(cache_path: os.PathLike) -> dict:
    p = Path(str(cache_path))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache_path: os.PathLike, data: dict) -> None:
    p = Path(str(cache_path))
    try:
        # Use atomic_write so cache corruption can't happen on crash mid-write
        try:
            from staging_atomic import atomic_write
            with atomic_write(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except ImportError:
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        log.debug(f"  [AcoustID] cache save failed: {e}")


def resolve_identity(
    audio_path: os.PathLike,
    *,
    api_key: Optional[str] = None,
    cache_path: os.PathLike = DEFAULT_CACHE_PATH,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    http_get: Callable[..., dict] = _default_http_get,
) -> Optional[ResolvedIdentity]:
    """End-to-end: fingerprint + lookup + cache.

    Returns ResolvedIdentity on confident match, None otherwise. Never raises;
    failures are logged at DEBUG/INFO (per Council §G "graceful degradation").
    """
    if api_key is None:
        try:
            import config  # type: ignore
            api_key = (getattr(config, "ACOUSTID_API_KEY", "")
                        or os.environ.get("ACOUSTID_API_KEY", ""))
        except ImportError:
            api_key = os.environ.get("ACOUSTID_API_KEY", "")
    if not api_key:
        log.debug("  [AcoustID] no api_key configured; skipping resolution")
        return None

    audio_path = Path(str(audio_path))
    if not audio_path.is_file():
        return None

    # Cache lookup — avoid re-fingerprinting + re-HTTP on already-resolved files
    try:
        sha = _file_sha256(audio_path)
    except OSError as e:
        log.debug(f"  [AcoustID] sha256 failed for {audio_path}: {e}")
        return None
    cache = _load_cache(cache_path)
    if sha in cache:
        entry = cache[sha]
        if entry.get("status") == "match":
            return ResolvedIdentity(**entry["identity"])
        if entry.get("status") == "no_match":
            return None  # negative cache: don't re-query

    fp_result = fingerprint_file(audio_path)
    if fp_result is None:
        # Don't cache fpcalc failures (binary may become available later)
        return None

    # 2026-07-24 — found live: an invalid config.ACOUSTID_API_KEY made every
    # single lookup fail with HTTP 400 "invalid API key", and this was
    # previously indistinguishable from a genuine no-match — every failed
    # file got permanently written to the cache as "no_match", silently
    # masking the real (config) problem and preventing any retry even after
    # the key was fixed. Transport/auth errors are NOT cached: only a
    # completed lookup (whether it matched or not) is.
    try:
        identity = lookup_fingerprint(
            fp_result,
            api_key=api_key,
            score_threshold=score_threshold,
            http_get=http_get,
        )
    except Exception as e:
        log.warning(
            f"  [AcoustID] lookup failed for {audio_path.name!r} — NOT cached, "
            f"will retry next run: {type(e).__name__}: {e}"
        )
        return None

    if identity is not None:
        cache[sha] = {
            "status": "match",
            "identity": dataclasses.asdict(identity),
        }
        _save_cache(cache_path, cache)
        log.info(
            f"  [AcoustID] resolved {audio_path.name!r} → "
            f"{identity.artist!r} - {identity.title!r} "
            f"(score={identity.score:.2f})"
        )
    else:
        cache[sha] = {"status": "no_match"}
        _save_cache(cache_path, cache)

    return identity


__all__ = [
    "FingerprintResult", "ResolvedIdentity",
    "fingerprint_file", "lookup_fingerprint", "resolve_identity",
    "FPCALC_TIMEOUT_SEC", "DEFAULT_SCORE_THRESHOLD",
]
