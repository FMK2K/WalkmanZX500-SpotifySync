"""
meta_key.py  —  Shared metakey utilities for the Walkman Sync Agent
====================================================================
A "metakey" is a 12-char MD5 hash of the tag values that SHOULD be written
to a device file:  artist | album | title | genre | year | track | disc

It is stored in .audit_state.json alongside the last-audit timestamp.

Any module that writes tags to device files (walkman_sync, downloader, audit)
should stamp a metakey after writing.  Any module that checks whether tags are
current (audit prefetch, walkman_sync update check) should compute the expected
metakey first — if it matches the stored one, skip the expensive MTP pull.

Public API
----------
compute_metakey(filename_stem, genre, year, track_number="", disc_number="1") -> str
    Compute the metakey for a file from its expected tag values.

stamp(device_key, metakey)
    Persist a metakey in the audit state file.

load_state() -> dict
    Load the full audit state dict (metakeys + timestamps).

is_current(device_key, expected_metakey) -> bool
    True if the stored metakey matches the expected one (file is up to date).

invalidate_by_artist(artist_norm)
    Clear stored metakeys for all files by an artist when genre cache updates.
"""

import hashlib
import json
import re
import datetime
import threading
import unicodedata

import config

_state_lock = threading.Lock()
_state_cache: dict | None = None   # in-process cache, flushed on stamp


# ---------------------------------------------------------------------------
# Core hash
# ---------------------------------------------------------------------------

def compute_metakey(
    filename_stem: str,
    genre:         str  = "",
    year:          str  = "",
    track_number:  str  = "",
    disc_number:   str  = "1",
    album_artist:  str  = "",
) -> str:
    """
    Compute a 12-char metakey from expected tag values.
    Parses artist/album/title from the canonical filename stem when needed.

    Canonical stem format: "Artist - Album - NN Title"
    Legacy format:         "NN Artist - Title"

    All callers (walkman_sync, audit, downloader) must use this function —
    never build the hash inline.  The field order and casing are fixed here
    so stamps and lookups always agree:

        artist | album_artist | album | title | genre | year | track | disc

    album_artist defaults to artist when omitted (single-artist albums).
    All fields are lower-cased before hashing.
    """
    def _strip_num(s: str) -> str:
        return re.sub(r"^\d{1,3}[\s\.\-]+", "", s).strip() or s

    parts = [p.strip() for p in filename_stem.split(" - ")]

    if len(parts) >= 3:
        artist       = _strip_num(parts[0])
        album        = parts[1]
        title        = _strip_num(parts[-1])
        m            = re.match(r"^(?:\d{1,2}-)?(\d{1,2})\s", parts[-1])
        track_number = track_number or (str(int(m.group(1))) if m else "")
    elif len(parts) == 2:
        artist       = _strip_num(parts[0])
        album        = ""
        title        = _strip_num(parts[1])
    else:
        artist       = ""
        album        = ""
        title        = _strip_num(filename_stem)

    # album_artist defaults to artist so single-artist files hash identically
    # whether the caller supplies album_artist or not.
    resolved_aa = (album_artist.strip() or artist).lower().strip()

    # MK28 E2-T1 (A4): Structural identity only — genre/year stripped from hash.
    # CTO: "genre in metakey = freeze system." Wrong inference becomes permanent.
    # METAKEY_SCHEMA_VERSION bump auto-invalidates all prior stored keys on deploy.
    # genre/year params kept for API backward compat but deliberately NOT hashed.
    _METAKEY_SCHEMA_VERSION = "mk28"
    key_str = "|".join([
        artist.lower().strip(),
        resolved_aa,
        album.lower().strip(),
        title.lower().strip(),
        str(track_number).strip(),
        str(disc_number).strip(),
        _METAKEY_SCHEMA_VERSION,
    ])
    return hashlib.md5(key_str.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Canonical device key construction  — single source of truth
# ---------------------------------------------------------------------------

def canonical_device_key(storage: str, folder_parts: list, filename: str) -> str:
    """
    Build the canonical audit-state key for a device file.

    All modules (walkman_sync, audit, downloader) MUST use this function
    instead of constructing keys manually.  It guarantees the key format:
        "<storage>/<folder_parts[0]>/.../<folder_parts[-1]>/<filename>"

    The caller provides folder_parts exactly as returned by
    _device_folder_parts() — i.e. already containing config.MUSIC_ROOT
    ("Music") as the first element.  This function does NOT add an extra
    "Music/" prefix.

    Example:
        canonical_device_key(
            "Internal shared storage",
            ["Music", "Aminé - Limbo"],
            "Aminé - Limbo - 03 Compensating (feat. Young Thug).mp3"
        )
        → "Internal shared storage/Music/Aminé - Limbo/Aminé - Limbo - 03 ..."
    """
    parts = [storage] + list(folder_parts) + [filename]
    return "/".join(parts)


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def load_state() -> dict:
    global _state_cache
    with _state_lock:
        if _state_cache is not None:
            return _state_cache
        try:
            if config.AUDIT_STATE_FILE.exists():
                _state_cache = json.loads(
                    config.AUDIT_STATE_FILE.read_text(encoding="utf-8")
                )
            else:
                _state_cache = {}
        except Exception:
            _state_cache = {}
        return _state_cache


def _flush():
    """Write the in-process state cache to disk."""
    with _state_lock:
        if _state_cache is not None:
            config.AUDIT_STATE_FILE.write_text(
                json.dumps(_state_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def stamp(device_key: str, metakey: str):
    """
    Persist {ts, mk} for a device file path after tags have been written.
    device_key — canonical device path string, e.g.
        "SD card/Music/Janet Jackson - All For You/Janet Jackson - All For You - 01-04 2wayforyou.flac"
    """
    import logging as _log_mk
    global _state_cache
    ts    = datetime.datetime.now().isoformat()
    entry = {"ts": ts, "mk": metakey}
    with _state_lock:
        if _state_cache is None:
            load_state()
        _state_cache[device_key] = entry
    _flush()
    # Log confirmation so we can verify metakeys are being written
    _log_mk.getLogger(__name__).debug(
        f"  [MetaKey] Stamped | key={device_key!r} | mk={metakey!r} | ts={ts[:19]}"
    )


def is_current(device_key: str, expected_metakey: str) -> bool:
    """
    Returns True if the stored metakey matches the expected one.
    When True, the file's tags are already correct — no MTP pull needed.
    """
    import logging as _log_mk
    state = load_state()
    entry = state.get(device_key)
    if not entry:
        _log_mk.getLogger(__name__).debug(
            f"  [MetaKey] MISS (no entry) | key={device_key!r} | expected={expected_metakey!r}"
        )
        return False
    if isinstance(entry, dict):
        stored = entry.get("mk")
        match  = (stored == expected_metakey)
        if match:
            _log_mk.getLogger(__name__).debug(
                f"  [MetaKey] HIT | key={device_key!r} | mk={stored!r}"
            )
        else:
            _log_mk.getLogger(__name__).debug(
                f"  [MetaKey] STALE | key={device_key!r} | stored={stored!r} | expected={expected_metakey!r}"
            )
        return match
    # legacy plain-timestamp entry — always re-audit
    _log_mk.getLogger(__name__).debug(
        f"  [MetaKey] MISS (legacy entry) | key={device_key!r}"
    )
    return False


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------

def invalidate_by_artist(artist_norm: str):
    """
    Clear metakeys for all device files whose key contains the normalised
    artist name.  Called by genre_client after updating genre/year for an artist
    so the audit will re-check those files on the next run.
    """
    global _state_cache
    state = load_state()
    artist_lc = artist_norm.lower().strip()
    cleared   = 0
    with _state_lock:
        for key in list(state.keys()):
            if artist_lc in key.lower():
                entry = state[key]
                if isinstance(entry, dict):
                    entry.pop("mk", None)   # remove metakey, keep timestamp
                    cleared += 1
    if cleared:
        _flush()
    return cleared


def invalidate_by_track_id(track_id: str) -> int:
    """
    MK18 #4 — Exact-match invalidation by Spotify track ID.

    invalidate_by_artist() does fuzzy substring matching across all device
    keys, which can clear unrelated tracks that share a common artist
    substring (e.g. invalidating 'Drake' also hits 'Drake Bell', 'Childish
    Gambino ft. Drake', etc.).  This function targets only the specific
    track whose upstream metadata changed.

    Called by genre_client._genre_cache_set() after writing a new genre
    for a track that has a known Spotify ID.
    """
    state = load_state()
    cleared = 0
    tid_lc  = track_id.lower().strip()
    with _state_lock:
        for key in list(state.keys()):
            # Device keys contain the track_id when stamped via stamp_with_id()
            if f":{tid_lc}" in key.lower() or key.lower().startswith(tid_lc):
                entry = state[key]
                if isinstance(entry, dict):
                    entry.pop("mk", None)
                    cleared += 1
    if cleared:
        _flush()
    return cleared


def invalidate_by_canonical(artist: str, album: str, title: str) -> int:
    """
    MK18 #4 — Exact-match invalidation by (artist, album, title) triple.

    More precise than invalidate_by_artist() — only clears entries that
    match all three fields simultaneously.  Handles mixed-artist albums
    correctly where a per-album genre change should only affect tracks on
    that album, not all tracks by that artist.
    """
    import unicodedata as _ud

    def _n(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "",
            _ud.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())

    a_n, al_n, t_n = _n(artist), _n(album), _n(title)
    state   = load_state()
    cleared = 0
    with _state_lock:
        for key in list(state.keys()):
            parts = [_n(p) for p in key.split("|")]
            # Key format: artist|album|title  or  device_path|artist|title
            if len(parts) >= 3:
                if (a_n in parts[0] or a_n in parts[-2]) and t_n in parts[-1]:
                    if not al_n or al_n in " ".join(parts):
                        entry = state[key]
                        if isinstance(entry, dict):
                            entry.pop("mk", None)
                            cleared += 1
    if cleared:
        _flush()
    return cleared


def reconcile_sample(sample_fraction: float = 0.05) -> dict:
    """
    MK18 #4 — Reconciliation sampler: verify stored metakeys are still
    valid for a random subset of cache entries each run.

    Computes the expected metakey from stored tag values and compares
    against the stored metakey.  Mismatches indicate a genre/year update
    whose invalidation cascade was missed.

    Returns {checked, mismatches, invalidated}.
    Mismatched entries are invalidated so the next audit re-validates them.
    """
    import random, math
    state    = load_state()
    keys     = [k for k, v in state.items() if isinstance(v, dict) and "mk" in v]
    n_sample = max(1, math.ceil(len(keys) * sample_fraction))
    sample   = random.sample(keys, min(n_sample, len(keys)))

    mismatches  = 0
    invalidated = 0
    for key in sample:
        entry = state.get(key, {})
        if not isinstance(entry, dict):
            continue
        stored_mk   = entry.get("mk", "")
        # Recompute from stored tag fields if available
        genre  = entry.get("genre", "")
        year   = entry.get("year", "")
        track  = entry.get("track", "")
        disc   = entry.get("disc", "1")
        # Split key to get stem
        stem = key.split("|")[-1] if "|" in key else key
        expected_mk = compute_metakey(stem, genre, year, track, disc)
        if stored_mk and stored_mk != expected_mk:
            mismatches += 1
            with _state_lock:
                entry.pop("mk", None)
            invalidated += 1

    if invalidated:
        _flush()

    log_fn = __import__("logging").getLogger(__name__).info
    log_fn(
        f"[MetaKey/Reconcile] Sampled {len(sample)}/{len(keys)} entries: "
        f"{mismatches} mismatches invalidated"
    )
    return {"checked": len(sample), "mismatches": mismatches, "invalidated": invalidated}


# MK29 G3-T1 (A3): Lazy invalidation on schema bump.
# CTO: "O(n) full recomputation storm." 27,901 files × MTP round-trip = hours.
# Only invalidate entries that NEED re-audit: low-confidence, legacy, no-source.
# Skip entries with type=external_metadata (human-curated, structurally correct).
_METAKEY_SCHEMA_VERSION_KEY = "__metakey_schema_version__"
_METAKEY_SCHEMA_MIGRATED_KEY = "__metakey_schema_migrated__"
_CURRENT_SCHEMA             = "mk28"


def lazy_invalidate_for_schema_bump(
    current_schema: str = _CURRENT_SCHEMA,
    confidence_threshold: float = 0.75,
) -> dict:
    """
    MK29 G3-T1 (A3): Invalidate only entries that benefit from re-audit.
    Called once on schema bump, guarded by METAKEY_SCHEMA_MIGRATED flag.

    Skips entries with:
      - _source_type = "external_metadata" (always correct, no need to re-audit)
      - confidence >= confidence_threshold (already confident)

    Invalidates entries with:
      - No confidence field (legacy pre-MK28, Groq-written)
      - confidence < confidence_threshold
      - _source_type = "legacy" or "heuristic"

    Returns {"checked": N, "invalidated": N, "skipped_external": N}
    """
    import logging as _log_g3
    _log = _log_g3.getLogger(__name__)

    state = load_state()

    # Check if already migrated — one-time cost, not per-restart
    # G3-T2: METAKEY_SCHEMA_MIGRATED guard
    migrated_key = f"{_METAKEY_SCHEMA_MIGRATED_KEY}:{current_schema}"
    if state.get(migrated_key):
        _log.debug(
            f"[MetaKey/G3-T2] Schema migration already completed "
            f"({current_schema}) — skipping lazy invalidation."
        )
        return {"checked": 0, "invalidated": 0, "skipped_external": 0, "already_migrated": True}

    _log.info(
        f"[MetaKey/G3-T1] Starting lazy schema migration → {current_schema}. "
        f"Only low-confidence/legacy entries will be re-audited."
    )

    checked             = 0
    invalidated         = 0
    skipped_external    = 0

    with _state_lock:
        for key, entry in state.items():
            if not isinstance(entry, dict) or "mk" not in entry:
                continue
            checked += 1

            # Skip external_metadata entries — human-curated, always correct
            src_type = str(entry.get("_source_type", "legacy")).lower()
            if src_type == "external_metadata":
                skipped_external += 1
                continue

            # Skip high-confidence entries (already have good data)
            conf = float(entry.get("confidence", 0.70))  # G2-T1: legacy default 0.70
            if conf >= confidence_threshold and src_type not in ("legacy", "heuristic"):
                continue

            # Invalidate: remove mk so next audit re-validates
            entry.pop("mk", None)
            invalidated += 1

        # G3-T2: stamp the migration as complete
        state[migrated_key] = True

    _flush()

    _log.info(
        f"[MetaKey/G3-T1] Schema migration complete: "
        f"checked={checked}, invalidated={invalidated}, "
        f"skipped_external={skipped_external}. "
        f"~{100*skipped_external/max(checked,1):.0f}% of entries preserved (external_metadata). "
        f"METAKEY_SCHEMA_MIGRATED stamped — will not re-run."
    )
    return {
        "checked": checked, "invalidated": invalidated,
        "skipped_external": skipped_external, "already_migrated": False,
    }


def invalidate_all():
    """
    Fully reset the in-process state cache AND delete the state file.

    FIX B8: the previous version only cleared metakeys but kept timestamps,
    so the stale-days check still skipped recently-audited files after a nuke.
    A true nuke must wipe _state_cache entirely so every file is re-examined
    by the next audit run — no timestamps, no metakeys, clean slate.

    Called by:
      - main._delete_audit_state() when "Nuke audit" is checked
      - main._start_sync() / _start_audit_only() when "Force re-audit" is checked
    """
    global _state_cache
    with _state_lock:
        _state_cache = {}
    try:
        if config.AUDIT_STATE_FILE.exists():
            config.AUDIT_STATE_FILE.unlink()
    except Exception as e:
        import logging as _lmk
        _lmk.getLogger(__name__).warning(
            f"  [MetaKey] Could not delete state file: {e}"
        )


# ---------------------------------------------------------------------------
# Playlist metakey  (ZX500 .m3u8 skip optimisation)
# ---------------------------------------------------------------------------
# The ZX500 reads .m3u8 files from Music root. Writing the same playlist
# every sync wastes ~2s per playlist on MTP text file writes.
# Solution: hash the playlist content; skip the write if unchanged.
#
# Key format: "PLAYLIST/<safe_playlist_name>"
# Hash input: joined sorted track paths + "\n" + count
# The ZX500 reads paths relative to the Music root — that's what we hash.
# ---------------------------------------------------------------------------

def compute_playlist_metakey(track_paths: list[str]) -> str:
    """
    Compute a metakey for a playlist from its ordered list of device paths.
    Paths should be relative to the Music root (same as what goes in the .m3u8).
    Order matters — the ZX500 plays tracks in the order listed.
    """
    content = "\n".join(track_paths)
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


def playlist_key(playlist_name: str) -> str:
    """Canonical state dict key for a playlist."""
    # Normalise: lowercase, collapse spaces
    safe = re.sub(r"\s+", " ", playlist_name.strip()).lower()
    return f"PLAYLIST/{safe}"


def is_playlist_current(playlist_name: str, track_paths: list[str]) -> bool:
    """
    Returns True if the playlist m3u8 on device already matches this track list.
    When True, skip the MTP write entirely.
    """
    key        = playlist_key(playlist_name)
    exp_mk     = compute_playlist_metakey(track_paths)
    state      = load_state()
    entry      = state.get(key)
    if not entry:
        return False
    if isinstance(entry, dict):
        return entry.get("mk") == exp_mk
    return False


def stamp_playlist(playlist_name: str, track_paths: list[str]):
    """Persist the playlist metakey after successfully writing the m3u8."""
    key = playlist_key(playlist_name)
    mk  = compute_playlist_metakey(track_paths)
    stamp(key, mk)


def invalidate_playlist(playlist_name: str):
    """Force next sync to rewrite this playlist's m3u8."""
    global _state_cache
    key   = playlist_key(playlist_name)
    state = load_state()
    with _state_lock:
        if key in state and isinstance(state[key], dict):
            state[key].pop("mk", None)
    _flush()


def invalidate_all_playlists():
    """Force next sync to rewrite all playlist m3u8 files."""
    global _state_cache
    state = load_state()
    with _state_lock:
        for key, entry in state.items():
            if key.startswith("PLAYLIST/") and isinstance(entry, dict):
                entry.pop("mk", None)
    _flush()


# ---------------------------------------------------------------------------
# Transfer resume  — partial file detection
# ---------------------------------------------------------------------------
# When the computer shuts down mid-CopyHere, a partial (truncated) audio file
# is left on the device. It causes "can't sync to MPEG frame" errors until
# deleted. We track in-flight transfers in the state dict so a restart can
# detect and clean them up.
#
# Key format: "IN_FLIGHT/<storage>/<folder>/<filename>"
# Value:      {"ts": "...", "size_bytes": N}  (expected final size)
# ---------------------------------------------------------------------------

def mark_in_flight(storage: str, folder_parts: list, filename: str, size_bytes: int = 0):
    """
    Record that a transfer is about to start.
    Must be called BEFORE CopyHere fires.
    """
    key   = f"IN_FLIGHT/{storage}/{'/'.join(folder_parts)}/{filename}"
    entry = {"ts": datetime.datetime.now().isoformat(), "size_bytes": size_bytes}
    global _state_cache
    state = load_state()
    with _state_lock:
        state[key] = entry
    _flush()


def mark_transfer_complete(storage: str, folder_parts: list, filename: str):
    """Remove the in-flight marker after a successful transfer."""
    key   = f"IN_FLIGHT/{storage}/{'/'.join(folder_parts)}/{filename}"
    global _state_cache
    state = load_state()
    with _state_lock:
        state.pop(key, None)
    _flush()


def get_incomplete_transfers() -> list[dict]:
    """
    Returns all transfers that were in-flight when the process last stopped.
    Each entry: {"storage": str, "folder_parts": list, "filename": str, "ts": str}
    Call at startup to find partial files that need cleanup.
    """
    state      = load_state()
    incomplete = []
    for key, entry in state.items():
        if not key.startswith("IN_FLIGHT/"):
            continue
        # key format: IN_FLIGHT/<storage>/<folder1>/<folder2>/<filename>
        rest  = key[len("IN_FLIGHT/"):]
        parts = rest.split("/")
        if len(parts) < 2:
            continue
        incomplete.append({
            "storage":      parts[0],
            "folder_parts": parts[1:-1],
            "filename":     parts[-1],
            "ts":           entry.get("ts", ""),
            "size_bytes":   entry.get("size_bytes", 0),
        })
    return incomplete


def clear_in_flight_markers():
    """Remove all in-flight markers (call after cleanup sweep)."""
    global _state_cache
    state = load_state()
    with _state_lock:
        for key in [k for k in state if k.startswith("IN_FLIGHT/")]:
            del state[key]
    _flush()