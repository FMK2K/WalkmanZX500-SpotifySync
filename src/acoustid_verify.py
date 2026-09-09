"""
acoustid_verify.py  —  AcoustID / Chromaprint download verification  (MK10)
============================================================================
Inspired by SoulSync's audio fingerprint verify step.

Every downloaded file passes through this module before tagging.  We:
  1. Run `fpcalc` (Chromaprint) on the file to generate an acoustic fingerprint
  2. Submit the fingerprint to the AcoustID API (free, no quota for fair use)
  3. Compare the returned MusicBrainz recording IDs against what Spotify says
     we downloaded

This catches:
  - Wrong track (YouTube source returned the wrong song)
  - Cover versions (live cover of "Redbone" ≠ the original)
  - Instrumental vs vocal mix
  - Regional/version mismatches (radio edit returned instead of album cut)

If fingerprint matches with confidence ≥ ACOUSTID_MIN_CONFIDENCE, we also
extract the canonical MusicBrainz recording ID and first-release-date for use
in metadata enrichment (ISSUE-B).

Public API
----------
    verify_download(path, expected_title, expected_artist, spotify_id=None)
        → VerifyResult (dataclass)

    fingerprint_file(path)
        → (duration_s: float, fingerprint: str) | (None, None)

    is_fpcalc_available() -> bool

Config
------
    ACOUSTID_API_KEY  in .env  — free key at https://acoustid.org/login
    ACOUSTID_MIN_CONFIDENCE = 0.80  (reject below this threshold)
    ACOUSTID_ENABLED = True   (set False to skip fingerprinting)

Dependencies
------------
    fpcalc  — Chromaprint CLI binary; bundled or installed via:
              Windows: https://acoustid.org/chromaprint
              Linux:   apt install libchromaprint-tools
              macOS:   brew install chromaprint
    requests — already in requirements.txt
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import requests

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

_ACOUSTID_ENDPOINT = "https://api.acoustid.org/v2/lookup"
_FPCALC_CANDIDATES = ["fpcalc", "fpcalc.exe",
                       str(pathlib.Path(__file__).parent / "fpcalc.exe"),
                       str(pathlib.Path(__file__).parent / "fpcalc")]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AcoustIDMatch:
    recording_id: str          # MusicBrainz recording MBID
    title: str
    artist: str
    score: float               # AcoustID confidence 0.0–1.0
    mb_first_release_date: str  # "YYYY", "YYYY-MM", or "YYYY-MM-DD"
    releases: list = field(default_factory=list)


@dataclass
class VerifyResult:
    path: str
    ok: bool                    # True if fingerprint matches expected track
    confidence: float           # best match score 0.0–1.0
    matches: list               # list[AcoustIDMatch]
    expected_title: str
    expected_artist: str
    matched_title: str = ""
    matched_artist: str = ""
    mismatch_reason: str = ""
    mb_recording_id: str = ""
    mb_first_release_date: str = ""
    duration_s: float = 0.0
    skipped: bool = False       # True if fpcalc unavailable or ACOUSTID_ENABLED=False
    error: str = ""


# ---------------------------------------------------------------------------
# fpcalc wrapper
# ---------------------------------------------------------------------------

def _find_fpcalc() -> str | None:
    """Locate the fpcalc binary; return its path or None."""
    for candidate in _FPCALC_CANDIDATES:
        try:
            r = subprocess.run(
                [candidate, "-version"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None

_fpcalc_path: str | None | bool = False  # False = not yet probed


def is_fpcalc_available() -> bool:
    global _fpcalc_path
    if _fpcalc_path is False:
        _fpcalc_path = _find_fpcalc()
    return _fpcalc_path is not None


def fingerprint_file(path: pathlib.Path) -> tuple[float | None, str | None]:
    """
    Run fpcalc on *path*.  Returns (duration_seconds, fingerprint_string).
    Returns (None, None) on failure.
    """
    global _fpcalc_path
    if _fpcalc_path is False:
        _fpcalc_path = _find_fpcalc()
    if _fpcalc_path is None:
        return None, None
    try:
        result = subprocess.run(
            [_fpcalc_path, "-json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.debug(f"[AcoustID] fpcalc error: {result.stderr[:100]}")
            return None, None
        data = json.loads(result.stdout)
        return float(data.get("duration", 0)), data.get("fingerprint", "")
    except Exception as e:
        log.debug(f"[AcoustID] fingerprint_file failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# AcoustID API call
# ---------------------------------------------------------------------------

_LAST_CALL_AT: float = 0.0
_MIN_INTERVAL: float = 0.34   # ~3 req/s max (AcoustID guideline)


def _acoustid_lookup(duration: float, fingerprint: str) -> list[dict]:
    """
    Query AcoustID and return list of recording dicts sorted by score desc.
    Returns [] on failure.
    """
    api_key = _cfg("ACOUSTID_API_KEY", os.environ.get("ACOUSTID_API_KEY", ""))
    if not api_key:
        log.debug("[AcoustID] No ACOUSTID_API_KEY — skipping lookup")
        return []

    global _LAST_CALL_AT
    elapsed = time.time() - _LAST_CALL_AT
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    try:
        resp = requests.post(
            _ACOUSTID_ENDPOINT,
            data={
                "client":      api_key,
                "duration":    int(duration),
                "fingerprint": fingerprint,
                "meta":        "recordings releasegroups",
            },
            timeout=12,
        )
        _LAST_CALL_AT = time.time()
        if resp.status_code != 200:
            log.debug(f"[AcoustID] HTTP {resp.status_code}")
            return []
        return resp.json().get("results", [])
    except Exception as e:
        log.debug(f"[AcoustID] lookup failed: {e}")
        return []


# ---------------------------------------------------------------------------
# String similarity helpers (avoid a dep on difflib for speed)
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", "", text).strip()


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity on word tokens."""
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------

def verify_download(
    path: pathlib.Path,
    expected_title: str,
    expected_artist: str,
    spotify_id: str | None = None,
) -> VerifyResult:
    """
    Fingerprint *path* and check whether it matches the expected track.

    The result's `.mb_recording_id` and `.mb_first_release_date` are
    always populated from the best match so callers can use them for
    metadata enrichment even when `ok=True`.
    """
    result = VerifyResult(
        path=str(path),
        ok=False,
        confidence=0.0,
        matches=[],
        expected_title=expected_title,
        expected_artist=expected_artist,
    )

    if not _cfg("ACOUSTID_ENABLED", True):
        result.ok      = True
        result.skipped = True
        result.error   = "ACOUSTID_ENABLED=False in config"
        return result

    if not is_fpcalc_available():
        result.ok      = True   # can't verify → assume ok (don't block pipeline)
        result.skipped = True
        result.error   = "fpcalc not found — install Chromaprint to enable fingerprinting"
        log.debug(f"[AcoustID] {result.error}")
        return result

    # Fingerprint
    duration, fingerprint = fingerprint_file(path)
    if not duration or not fingerprint:
        result.ok      = True   # corrupt fingerprint → don't block; log is enough
        result.skipped = True
        result.error   = "fpcalc returned no data (file may be too short)"
        return result

    result.duration_s = duration

    # Query AcoustID
    raw_results = _acoustid_lookup(duration, fingerprint)
    if not raw_results:
        result.ok      = True
        result.skipped = True
        result.error   = "AcoustID returned no results (key missing or network)"
        return result

    min_confidence = float(_cfg("ACOUSTID_MIN_CONFIDENCE", 0.80))

    parsed_matches: list[AcoustIDMatch] = []
    for hit in raw_results:
        score = float(hit.get("score", 0.0))
        for rec in hit.get("recordings", []):
            artist_str = ", ".join(
                a.get("name", "") for a in rec.get("artists", [])
            )
            # Extract earliest first-release-date across release groups
            earliest_date = ""
            for rg in rec.get("releasegroups", []):
                rd = rg.get("releases", [{}])[0].get("date", {}) if rg.get("releases") else {}
                yr = str(rd.get("year", ""))
                if re.match(r"(19|20)\d{2}", yr):
                    if not earliest_date or yr < earliest_date:
                        earliest_date = yr

            m = AcoustIDMatch(
                recording_id=rec.get("id", ""),
                title=rec.get("title", ""),
                artist=artist_str,
                score=score,
                mb_first_release_date=earliest_date,
                releases=rec.get("releasegroups", []),
            )
            parsed_matches.append(m)

    parsed_matches.sort(key=lambda m: m.score, reverse=True)
    result.matches = parsed_matches

    if not parsed_matches:
        result.ok    = True
        result.error = "AcoustID returned hits but no recording data"
        result.skipped = True
        return result

    best = parsed_matches[0]
    result.confidence          = best.score
    result.mb_recording_id     = best.recording_id
    result.mb_first_release_date = best.mb_first_release_date
    result.matched_title       = best.title
    result.matched_artist      = best.artist

    # Decide: does the best match correspond to what we expected?
    if best.score < min_confidence:
        result.ok             = False
        result.mismatch_reason = (
            f"Best match score {best.score:.2f} < threshold {min_confidence:.2f}"
        )
        log.warning(
            f"  [AcoustID] LOW CONFIDENCE ({best.score:.2f}) for "
            f"{expected_artist!r} — {expected_title!r}: "
            f"best match is {best.artist!r} — {best.title!r}"
        )
        return result

    # Check title + artist overlap against expected
    title_sim  = _token_overlap(expected_title, best.title)
    artist_sim = _token_overlap(expected_artist, best.artist)

    if title_sim >= 0.5 and artist_sim >= 0.4:
        result.ok = True
        log.debug(
            f"  [AcoustID] ✓ {expected_artist!r} — {expected_title!r} "
            f"(score={best.score:.2f}, title_sim={title_sim:.2f}, "
            f"artist_sim={artist_sim:.2f}, mbid={best.recording_id})"
        )
    else:
        result.ok = False
        result.mismatch_reason = (
            f"Title overlap {title_sim:.2f}<0.5 or artist overlap {artist_sim:.2f}<0.4. "
            f"AcoustID says: {best.artist!r} — {best.title!r}"
        )
        log.warning(
            f"  [AcoustID] MISMATCH: expected {expected_artist!r}—{expected_title!r}, "
            f"fingerprint matched {best.artist!r}—{best.title!r} "
            f"(score={best.score:.2f}, title_sim={title_sim:.2f})"
        )

    return result


# ---------------------------------------------------------------------------
# Batch convenience wrapper
# ---------------------------------------------------------------------------

def verify_downloads_batch(
    downloads: list[dict],
    cancelled_flag: list | None = None,
) -> dict:
    """
    Verify a batch of downloads.  Each dict must have keys:
      path, title, artist, (optional) spotify_id.

    Returns:
      {
        "total":     int,
        "verified":  int,   # passed
        "mismatches": int,  # flagged but not blocked
        "skipped":   int,   # fpcalc unavailable / ACOUSTID_ENABLED=False
        "results":   list[VerifyResult],
      }
    """
    summary = {"total": len(downloads), "verified": 0,
               "mismatches": 0, "skipped": 0, "results": []}

    for dl in downloads:
        if cancelled_flag and cancelled_flag[0]:
            break
        r = verify_download(
            pathlib.Path(dl["path"]),
            dl.get("title", ""),
            dl.get("artist", ""),
            dl.get("spotify_id"),
        )
        summary["results"].append(r)
        if r.skipped:
            summary["skipped"] += 1
        elif r.ok:
            summary["verified"] += 1
        else:
            summary["mismatches"] += 1

    log.info(
        f"[AcoustID] Batch complete: {summary['verified']} verified, "
        f"{summary['mismatches']} mismatches, {summary['skipped']} skipped "
        f"of {summary['total']} files"
    )
    return summary
