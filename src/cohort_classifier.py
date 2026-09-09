"""
cohort_classifier.py — MK11 Spiral Pipeline Cohort Classifier

Assigns each item (Spotify track or device file) to a risk tier for the
spiral orchestrator.  No API calls are made; classification is based on
locally available data (tag snapshot, device snapshot, track metadata).

Risk tiers:
  skip        — item needs no work this run (already correct / already present)
  low         — high confidence path (FLAC, complete tags, known artist)
  medium      — moderate confidence (MP3→FLAC candidate, missing genre/art)
  high        — low confidence (unknown artist, sparse metadata, DSD, conflict)
  quarantine  — cannot be safely processed (zero-byte, no identity, unreadable)

Public API:
    classify(mode, track_map) -> dict[str, list]
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT = Path(__file__).parent
_SNAPSHOT_FILE    = _PROJECT / "walkman_snapshot.json"
_TAG_SNAP_FILE    = _PROJECT / "walkman_tag_snapshot.json"

# Extensions that are inherently higher-risk due to limited tooling support
_DSD_EXTS = {".dsf", ".dff"}
_LOSSLESS  = {".flac", ".dsf", ".dff", ".wav", ".aiff", ".aif"}


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def classify(mode: str, track_map: dict | None = None) -> dict[str, list]:
    """
    Return {low, medium, high, skip, quarantine} lists for the given mode.

    For "download" / "full": items are Spotify track dicts (from track_map).
    For "audit" / "reorganize": items are device file dicts (from snapshot).
    """
    track_map = track_map or {}

    if mode in ("download", "full"):
        return _classify_download(track_map)
    if mode == "audit":
        return _classify_audit()
    if mode == "reorganize":
        return _classify_reorganize()
    return {"low": [], "medium": [], "high": [], "skip": [], "quarantine": []}


# ──────────────────────────────────────────────────────────────────────────────
# Download / full mode — classify Spotify tracks
# ──────────────────────────────────────────────────────────────────────────────

def _classify_download(track_map: dict) -> dict[str, list]:
    buckets: dict[str, list] = {k: [] for k in ("low", "medium", "high", "skip", "quarantine")}

    # Build set of device file basenames for "already present" check
    on_device = _device_basenames()

    seen: set = set()
    for playlist, tracks in track_map.items():
        for t in tracks or []:
            if not isinstance(t, dict):
                continue
            tid = t.get("id") or ""
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)

            # Skip if already on device (fuzzy match by normalised title+artist)
            if _track_on_device(t, on_device):
                buckets["skip"].append(t)
                continue

            score = _download_risk(t)
            if score <= 1:
                buckets["low"].append(t)
            elif score == 2:
                buckets["medium"].append(t)
            else:
                buckets["high"].append(t)

    return buckets


def _download_risk(t: dict) -> int:
    """
    0 = skip, 1 = low, 2 = medium, 3 = high.
    Based on metadata completeness — proxy for download source confidence.
    """
    risk = 0
    if not t.get("artist") and not t.get("artists"):
        risk += 2
    if not t.get("album"):
        risk += 1
    if not t.get("name") and not t.get("title"):
        risk += 2
    if not t.get("isrc"):
        risk += 1    # no ISRC = harder TIDAL lookup
    return min(risk, 3)


# ──────────────────────────────────────────────────────────────────────────────
# Audit mode — classify device files by tag completeness
# ──────────────────────────────────────────────────────────────────────────────

def _classify_audit() -> dict[str, list]:
    """
    2026-07-28: this and _classify_reorganize()/_device_basenames() were reading
    f["device_path"]/f["path"], but walkman_snapshot.json's real per-file key is
    "_data" (see walkman_tag_scan.py, which already reads track["_data"]). Every
    lookup silently produced path="" — tag_snap.get("", {}) is always {} — and
    _audit_risk()'s `if not tags: return 3` then classified 100% of files as
    "high" regardless of their actual state, confirmed live: a 1420-file audit
    reported cohorts high=1420, everything else 0. Fixed to read "_data" first.
    """
    buckets: dict[str, list] = {k: [] for k in ("low", "medium", "high", "skip", "quarantine")}
    files = _load_device_files()
    tag_snap = _load_tag_snap()

    for f in files:
        path = f.get("_data") or f.get("device_path") or f.get("path") or ""
        ext  = Path(path).suffix.lower() if path else ""

        # Quarantine: zero-size, unknown extension
        size = f.get("size", -1)
        if size == 0:
            buckets["quarantine"].append(f)
            continue

        tags = tag_snap.get(path, {}) if path else {}
        score = _audit_risk(f, tags, ext)

        if score == 0:
            buckets["skip"].append(f)
        elif score == 1:
            buckets["low"].append(f)
        elif score == 2:
            buckets["medium"].append(f)
        elif score >= 3:
            buckets["high"].append(f)
        else:
            buckets["quarantine"].append(f)

    return buckets


def _audit_risk(f: dict, tags: dict, ext: str) -> int:
    """0=skip 1=low 2=medium 3=high -1=quarantine."""
    risk = 0

    # No tags at all → high
    if not tags:
        return 3

    tag_artist = tags.get("tag_artist") or f.get("artist") or ""
    tag_album  = tags.get("tag_album")  or f.get("album")  or ""
    tag_title  = tags.get("tag_title")  or f.get("title")  or ""
    tag_genre  = tags.get("tag_genre")  or ""

    if not tag_artist or not tag_album or not tag_title:
        risk += 2
    if not tag_genre:
        risk += 1
    if not tags.get("has_apic"):
        risk += 1
    if tags.get("id3_version") not in (0, 3) and ext == ".mp3":
        risk += 1   # wrong ID3 version → needs downgrade
    if ext in _DSD_EXTS:
        risk += 1   # DSD needs extra verification

    # Already verified recently → skip
    if tags.get("spiral_verified") and risk <= 1:
        return 0

    return min(risk, 3)


# ──────────────────────────────────────────────────────────────────────────────
# Reorganize mode — classify device files by path canonicality
# ──────────────────────────────────────────────────────────────────────────────

def _classify_reorganize() -> dict[str, list]:
    buckets: dict[str, list] = {k: [] for k in ("low", "medium", "high", "skip", "quarantine")}
    files  = _load_device_files()
    tag_snap = _load_tag_snap()

    for f in files:
        path = f.get("_data") or f.get("device_path") or f.get("path") or ""
        tags = tag_snap.get(path, {}) if path else {}
        score = _reorg_risk(f, tags)

        if score == 0:
            buckets["skip"].append(f)
        elif score == 1:
            buckets["low"].append(f)
        elif score == 2:
            buckets["medium"].append(f)
        else:
            buckets["high"].append(f)

    return buckets


def _reorg_risk(f: dict, tags: dict) -> int:
    """0=skip 1=low 2=medium 3=high."""
    parts = f.get("parts", [])
    # Heuristic: if the path already looks canonical (Music/Artist/Album/NN Title.ext)
    # it has ≥4 parts and the third-to-last contains a year-like token or is consistent.
    if len(parts) >= 4:
        filename = parts[-1] if parts else ""
        # Canonical filenames look like "NN Title.ext" or "Artist - Album - NN Title.ext"
        if filename and (filename[0].isdigit() or " - " in filename):
            return 0   # looks canonical → skip

    artist = (tags.get("tag_artist") or f.get("artist") or "").strip()
    album  = (tags.get("tag_album")  or f.get("album")  or "").strip()
    track  = (tags.get("tag_track")  or f.get("track_number") or "").strip()

    if not artist or not album:
        return 3   # can't determine canonical path
    if not track:
        return 2   # track number missing → ambiguous ordering
    return 1       # all needed fields present → low risk


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_device_files() -> list[dict]:
    try:
        if _SNAPSHOT_FILE.exists():
            data = json.loads(_SNAPSHOT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # Some snapshot formats nest files under storage keys
                out = []
                for v in data.values():
                    if isinstance(v, list):
                        out.extend(v)
                return out
    except Exception as e:
        log.debug(f"[Cohort] snapshot load error: {e}")
    return []


def _load_tag_snap() -> dict:
    try:
        if _TAG_SNAP_FILE.exists():
            return json.loads(_TAG_SNAP_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug(f"[Cohort] tag snapshot load error: {e}")
    return {}


def _device_basenames() -> set[str]:
    """Normalised basenames of all audio files currently on device."""
    files = _load_device_files()
    out: set[str] = set()
    for f in files:
        path = f.get("_data") or f.get("device_path") or f.get("path") or f.get("filename") or ""
        if path:
            out.add(Path(path).stem.lower())
    return out


def _track_on_device(t: dict, on_device: set[str]) -> bool:
    """Rough check: is this Spotify track already represented on device?"""
    name        = (t.get("name") or t.get("title") or "").lower().strip()
    artists_raw = t.get("artists")
    if artists_raw:
        first  = artists_raw[0]
        # offline mode stores artists as [str]; API mode stores as [{"name":...}]
        artist = first.get("name", "") if isinstance(first, dict) else str(first)
    else:
        artist = t.get("artist", "")
    artist = artist.lower().strip()
    if not name:
        return False
    # Check stem match: "Artist - Title" or just "Title"
    for candidate in (f"{artist} - {name}", name):
        norm = "".join(c for c in candidate if c.isalnum() or c == " ").strip()
        if norm in on_device:
            return True
    return False
