"""
device_inventory.py  —  Walkman Device File Index  (MK12)
==========================================================
Maintains a fast, searchable in-memory index of every file on the Walkman,
built from:

    adb shell "find /storage/emulated/0 /storage/4A21-0000 \\
               -maxdepth 6 -type f 2>/dev/null | sort"

The output is cached to .device_file_index.json (UTF-8, 24h TTL) so
subsequent pipeline runs don't re-query the device.  The cache is
invalidated on:
  - Manual force-refresh (refresh(force=True))
  - Cache age > CACHE_TTL_HOURS
  - SD card UUID change (detected from adb shell df output)

Why this matters for genre classification
-----------------------------------------
When asking Claude Sonnet (or GPT-4o) to classify a track's genre, we
can tell it:
  - Whether the track exists on device at all
  - Its format (FLAC > M4A > MP3 signals source quality)
  - How many copies exist (duplicates hint at messy provenance)
  - What other tracks share its album folder (genre context from siblings)
  - Whether it's on SD (permanent) vs Internal (probably temp/new)

This gives the AI evidence it can't get from the track name alone, and
prevents hallucinated genres for obscure tracks where it would otherwise
default to "Hip-Hop".

Public API
----------
    refresh(force=False) -> int
        Populate/update the index via ADB.  Returns file count.

    lookup(artist, title) -> list[DeviceFile]
        Fuzzy-match a track by artist+title stem.  Returns all hits.

    get_album_context(album_name, artist_name="") -> list[DeviceFile]
        Return sibling tracks in the same album folder for context.

    get_duplicates(stem) -> list[DeviceFile]
        All device files whose name stem matches (multi-format duplicates).

    get_device_summary() -> dict
        Counts by storage / format / duplicate groups for logging.

    is_stale() -> bool
        True if cache is older than CACHE_TTL_HOURS.

    age_hours() -> float
        Cache age in hours (float).

    is_refreshed() -> bool
        True if the index is populated (either from ADB or from cache).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import posixpath
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════
# MK34 P2-T2: ADB-first STATE_DIVERGENCE_DETECTED check
# ═══════════════════════════════════════════════════════════════════════════
# CTO Class B fix: when ADB count and preload count diverge significantly,
# log STATE_DIVERGENCE_DETECTED and treat ADB as canonical. preload becomes
# diagnostic-only.
def check_state_divergence(adb_count: int, preload_count: int,
                            threshold_pct: float = 0.05) -> bool:
    """
    Return True iff divergence between ADB and preload counts exceeds threshold.
    Logs STATE_DIVERGENCE_DETECTED on divergence; ADB is always authoritative.
    """
    import logging as _log_p2
    _log = _log_p2.getLogger(__name__)
    if adb_count == 0 and preload_count == 0:
        return False
    biggest = max(adb_count, preload_count, 1)
    div = abs(adb_count - preload_count) / biggest
    if div > threshold_pct:
        _log.warning(
            f"  [DeviceInventory/MK34-P2-T2] STATE_DIVERGENCE_DETECTED "
            f"adb={adb_count} preload={preload_count} ({div*100:.1f}% diff). "
            f"ADB is canonical."
        )
        return True
    return False


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

_ADB_EXE = str(pathlib.Path(__file__).parent / "adb.exe") if (
    pathlib.Path(__file__).parent / "adb.exe"
).exists() else "adb"

CACHE_TTL_HOURS = 24.0   # re-query device once per day
CACHE_PATH = pathlib.Path(_cfg("BASE_DIR", pathlib.Path(".")) or ".") / ".device_file_index.json"
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wav", ".aiff", ".aif", ".ape", ".wv", ".wma", ".dsf", ".dff",
})
# Storages to scan — internal + SD (UUID discovered dynamically)
_SCAN_ROOTS = ["/storage/emulated/0", "/storage/4A21-0000"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DeviceFile:
    path: str           # full /storage/... path
    storage: str        # "internal" | "sd" | "unknown"
    ext: str            # ".mp3", ".flac", etc.
    folder: str         # parent directory path
    album_folder: str   # folder name (artist - album convention)
    filename: str       # basename with extension
    stem: str           # basename without extension, lowercased


@dataclass
class DeviceInventory:
    files: list[DeviceFile] = field(default_factory=list)
    timestamp: float        = 0.0    # Unix time of last refresh
    sd_mount: str           = ""     # /storage/XXXX-XXXX
    file_count: int         = 0
    audio_count: int        = 0
    _populated: bool        = field(default=False, repr=False)

    # Fast-lookup indices built after load
    _by_stem: dict          = field(default_factory=dict, repr=False)
    _by_folder: dict        = field(default_factory=dict, repr=False)

    def build_indices(self) -> None:
        """Build fast-lookup dicts from the file list."""
        self._by_stem   = {}
        self._by_folder = {}
        for f in self.files:
            self._by_stem.setdefault(f.stem, []).append(f)
            self._by_folder.setdefault(f.folder, []).append(f)
        self._populated = True

    def is_refreshed(self) -> bool:
        return self._populated and bool(self.files)

    def age_hours(self) -> float:
        if not self.timestamp:
            return float("inf")
        return (time.time() - self.timestamp) / 3600.0

    def is_stale(self) -> bool:
        return self.age_hours() > CACHE_TTL_HOURS


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_inventory: DeviceInventory = DeviceInventory()


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

_ADB_TIMED_OUT = object()   # sentinel: distinguish timeout from empty output

def _run_adb(*args, timeout: int = 120):
    """
    Run an adb command and return stdout as UTF-8 string.
    Returns _ADB_TIMED_OUT sentinel on timeout so callers can skip retries.
    """
    cmd = [_ADB_EXE] + list(args)
    serial = _cfg("ADB_DEVICE_SERIAL", None)
    if serial:
        cmd = [_ADB_EXE, "-s", serial] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",    # non-Latin filenames safe
            timeout=timeout,
            check=False,
        )
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        log.warning(f"[DeviceInventory] ADB command timed out: {' '.join(args[:3])}")
        return _ADB_TIMED_OUT
    except FileNotFoundError:
        log.warning(f"[DeviceInventory] ADB not found at {_ADB_EXE!r}")
        return _ADB_TIMED_OUT
    except Exception as e:
        log.debug(f"[DeviceInventory] ADB error: {e}")
        return ""


def _detect_sd_mount() -> str:
    """Return the /storage/XXXX-XXXX mount for the SD card from df output."""
    df_out = _run_adb("shell", "df", "-k", timeout=20)
    if df_out is _ADB_TIMED_OUT:
        return ""
    for line in df_out.splitlines():
        m = re.search(r"(/storage/[A-F0-9]{4}-[A-F0-9]{4})\s*$", line, re.IGNORECASE)
        if m:
            mp = m.group(1)
            if not mp.startswith("/storage/emulated") and not mp.startswith("/storage/self"):
                return mp
    return ""


def _device_connected(retries: int = 3, retry_delay_s: float = 3.0) -> bool:
    """
    MK26 C3+C4 fix (A3): Retry loop for USB auth race condition.
    Evidence (LOG_6_5_26): inventory=0 at t=0, ADB found 27901 files at t=11s.
    USB debugging IS enabled; the device just wasn't enumerated yet.
    Tries 3× at 3-second intervals before declaring not-connected.

    Retries only on "no device yet" (fast response) — stops immediately on
    timeout or FileNotFoundError because those mean ADB itself is unavailable,
    not that the device is still authenticating.
    """
    import time as _t_di
    for attempt in range(retries):
        out = _run_adb("devices", timeout=4)
        if out is _ADB_TIMED_OUT:
            # ADB server not responding — retrying won't help
            log.warning("[DeviceInventory] ADB not available (timed out) — "
                        "Enable USB Debugging on the Walkman and retry.")
            return False
        lines = [l for l in out.splitlines()
                 if l.strip() and "daemon" not in l.lower()
                 and "attached" not in l.lower()]
        if len(lines) > 1:
            if attempt > 0:
                log.info(f"[DeviceInventory] ADB connected on retry #{attempt + 1} "
                         f"(USB auth completed)")
            return True
        if attempt < retries - 1:
            log.debug(
                f"[DeviceInventory] ADB not ready (attempt {attempt+1}/{retries}) "
                f"— retrying in {retry_delay_s:.0f}s (USB auth may still be completing)"
            )
            _t_di.sleep(retry_delay_s)
    elapsed = retries * retry_delay_s + retries * 4
    log.warning("[DeviceInventory] ADB device not connected after "
                f"{retries} attempts (~{elapsed:.0f}s total). "
                "Enable USB Debugging on the Walkman and retry.")
    return False


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------

def _classify_storage(path: str, sd_mount: str) -> str:
    if "/storage/emulated" in path:
        return "internal"
    if sd_mount and sd_mount in path:
        return "sd"
    if re.search(r"/storage/[A-F0-9]{4}-[A-F0-9]{4}", path, re.IGNORECASE):
        return "sd"
    return "unknown"


def _parse_paths(raw_output: str, sd_mount: str) -> list[DeviceFile]:
    """Convert raw find output into DeviceFile objects (audio only)."""
    files: list[DeviceFile] = []
    for line in raw_output.splitlines():
        path = line.strip()
        if not path or not path.startswith("/storage/"):
            continue
        ext = posixpath.splitext(path)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            continue
        folder   = posixpath.dirname(path)
        filename = posixpath.basename(path)
        stem     = posixpath.splitext(filename)[0].lower().strip()
        # Normalize stem: strip NFKD → ASCII → collapse whitespace
        stem_norm = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
        stem_norm = re.sub(r"\s+", " ", stem_norm).strip()
        album_folder = posixpath.basename(folder)
        storage  = _classify_storage(path, sd_mount)
        files.append(DeviceFile(
            path=path,
            storage=storage,
            ext=ext,
            folder=folder,
            album_folder=album_folder,
            filename=filename,
            stem=stem_norm,
        ))
    return files


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------

def _save_cache(inv: DeviceInventory) -> None:
    try:
        data = {
            "timestamp":   inv.timestamp,
            "sd_mount":    inv.sd_mount,
            "file_count":  inv.file_count,
            "audio_count": inv.audio_count,
            "files":       [asdict(f) for f in inv.files],
        }
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log.info(f"[DeviceInventory] Cache saved: {len(inv.files)} audio files → {CACHE_PATH.name}")
    except Exception as e:
        log.warning(f"[DeviceInventory] Cache save failed: {e}")


def _load_cache() -> DeviceInventory | None:
    try:
        if not CACHE_PATH.exists():
            return None
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        inv  = DeviceInventory(
            timestamp   = float(data.get("timestamp", 0)),
            sd_mount    = data.get("sd_mount", ""),
            file_count  = int(data.get("file_count", 0)),
            audio_count = int(data.get("audio_count", 0)),
        )
        for fd in data.get("files", []):
            inv.files.append(DeviceFile(**fd))
        inv.build_indices()
        log.info(
            f"[DeviceInventory] Cache loaded: {len(inv.files)} audio files "
            f"(age {inv.age_hours():.1f}h)"
        )
        return inv
    except Exception as e:
        log.warning(f"[DeviceInventory] Cache load failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refresh(force: bool = False) -> int:
    """
    Populate/update the device file index via ADB.

    If force=False and the cache is fresh (< 24h old), loads from cache.
    If force=True or cache is stale, re-queries the device.

    Returns number of audio files indexed.
    """
    global _inventory

    # Try loading from cache first
    if not force:
        cached = _load_cache()
        if cached and not cached.is_stale():
            _inventory = cached
            return len(_inventory.files)
        if cached and cached.is_stale():
            log.info(
                f"[DeviceInventory] Cache is {cached.age_hours():.1f}h old "
                f"(TTL={CACHE_TTL_HOURS}h) — re-querying device"
            )

    # ADB query
    if not _device_connected():
        log.warning(
            "[DeviceInventory] ADB device not connected after all retries. "
            "Enable USB Debugging on the Walkman and retry. "
            "Inventory state: UNKNOWN (not empty). "
            "Falling back to stale cache if available."
        )
        if cached := _load_cache():
            _inventory = cached
            return len(_inventory.files)
        return 0

    # Detect SD card mount (dynamic UUID)
    sd_mount = _detect_sd_mount() or "/storage/4A21-0000"   # fallback to known UUID
    log.info(f"[DeviceInventory] SD mount detected: {sd_mount!r}")

    # Build scan roots — always include internal + SD
    scan_roots = ["/storage/emulated/0"]
    if sd_mount and sd_mount not in scan_roots:
        scan_roots.append(sd_mount)

    # Run the find command
    # -maxdepth 6 keeps it fast (Artist/Album/Track is depth 3 from Music/)
    # 2>/dev/null suppresses permission errors on system folders
    find_cmd = (
        "find " + " ".join(scan_roots) +
        " -maxdepth 6 -type f 2>/dev/null | sort"
    )
    log.info(f"[DeviceInventory] Running: adb shell \"{find_cmd[:80]}...\"")
    # MK16-I1 + MK37.2-3: retry loop — race condition where ADB shell is ready
    # but Android filesystem not yet mounted (observed: 0 files at t=0, N files
    # at t=11s).
    #
    # MK37.2-3 enhancement: emit explicit markers so operators can correlate
    # downstream halt-gate behavior with inventory race detection. The
    # existing 3×5s retry is preserved as the inner loop; this layer adds:
    #   - INVENTORY_RACE_DETECTED on attempt 1 returning empty (after USB ready)
    #   - INVENTORY_RACE_RECOVERED if attempts 2 or 3 yield non-empty
    #   - INVENTORY_RACE_PERSISTED if all 3 still return empty
    #
    # Closes LOG_8_5_26 case where line 30 ("Device inventory: 0 audio files
    # indexed") and line 439 ("Live scan returned 0 audio files") both fired
    # without correlation telemetry.
    raw = ""
    race_detected = False
    for _attempt in range(3):
        raw = _run_adb("shell", find_cmd, timeout=180)
        if raw is _ADB_TIMED_OUT:
            raw = ""
            break
        if raw:
            if race_detected:
                log.info(
                    f"[DeviceInventory] INVENTORY_RACE_RECOVERED on attempt "
                    f"{_attempt+1}/3 (USB/MTP filesystem mount completed)"
                )
            break
        # Empty result on this attempt
        if _attempt == 0:
            race_detected = True
            log.warning(
                f"[DeviceInventory] INVENTORY_RACE_DETECTED — first ADB find "
                f"returned no output despite USB device present. "
                f"Will retry up to 2 more times (filesystem mount race)."
            )
        if _attempt < 2:
            log.warning(
                f"[DeviceInventory] ADB find returned no output "
                f"(attempt {_attempt+1}/3) — retrying in 5s "
                f"(USB auth / filesystem mount race condition)"
            )
            import time as _t_inv
            _t_inv.sleep(5)

    if not raw and race_detected:
        log.error(
            f"[DeviceInventory] INVENTORY_RACE_PERSISTED — all 3 ADB find "
            f"attempts returned empty. Stage 1 halt gate (MK37/S1) will "
            f"evaluate via MTP quorum before proceeding."
        )

    if not raw:
        log.warning("[DeviceInventory] ADB find returned no output after 3 attempts")
        if cached := _load_cache():
            _inventory = cached
            log.info("[DeviceInventory] Using stale cache as fallback")
            return len(_inventory.files)
        log.warning(
            "[DeviceInventory] No cache and ADB returned nothing. "
            "Inventory unknown — halt gate will fire. "
            "Check USB Debugging, unlock device, and retry."
        )
        return 0

    total_lines = len(raw.splitlines())
    files       = _parse_paths(raw, sd_mount)

    inv = DeviceInventory(
        files       = files,
        timestamp   = time.time(),
        sd_mount    = sd_mount,
        file_count  = total_lines,
        audio_count = len(files),
    )
    inv.build_indices()
    _inventory = inv

    # MK26 C3 fix (A3): Set the live inventory flag in config.py.
    # Using config avoids the circular import / dynamic-attribute-write that
    # failed silently in production (LOG_6_5_26: flag never set → RC2 always
    # downgraded to report-only → 3646 stale-cache deletions from inventory=0).
    try:
        import config as _cfg_di
        _cfg_di.DEVICE_INVENTORY_IS_LIVE = True
        log.debug("[DeviceInventory] config.DEVICE_INVENTORY_IS_LIVE = True")
    except Exception as _e_di:
        log.debug(f"[DeviceInventory] Could not set live flag: {_e_di}")

    log.info(
        f"[DeviceInventory] Indexed {total_lines} total paths, "
        f"{len(files)} audio files "
        f"(internal: {sum(1 for f in files if f.storage=='internal')}, "
        f"SD: {sum(1 for f in files if f.storage=='sd')})"
    )

    # MK37/S1: enforce observation halt gate. Closes LOG_5_7_5_26 line 18+2373
    # case where 0 audio files indexed but Stage 1 proceeded anyway, causing
    # 3,646 stale-cache deletions. The gate raises InvariantViolation on
    # collapse — production caller MUST NOT catch it.
    try:
        from mk37_wiring import enforce_observation_halt
        cache_count = None
        try:
            _cached_for_halt = _load_cache()
            if _cached_for_halt is not None:
                cache_count = len(_cached_for_halt.files)
        except Exception:
            cache_count = None
        enforce_observation_halt(
            adb_count=len(files),
            cache_count=cache_count,
            sd_mount=sd_mount,
            raise_on_collapse=getattr(__import__("config"), "MK37_HALT_ON_OBSERVATION_COLLAPSE", True),
        )
    except ImportError:
        log.debug("[DeviceInventory] mk37_wiring not installed; halt gate skipped")
    except Exception as _halt_e:
        # Do NOT swallow InvariantViolation — re-raise after logging
        if type(_halt_e).__name__ == "InvariantViolation":
            log.error(f"[DeviceInventory] HALT_GATE_FIRED — propagating: {_halt_e}")
            raise
        log.warning(f"[DeviceInventory] halt gate non-fatal error: {_halt_e}")

    _save_cache(inv)
    return len(files)


def _norm_for_lookup(text: str) -> str:
    """Canonical form for fuzzy matching."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def lookup(artist: str, title: str) -> list[DeviceFile]:
    """
    Find device files matching artist + title by stem overlap.

    Matching strategy (loosest first):
      1. Exact stem match of "artist - album - NN title" patterns
      2. Title token overlap >= 0.6 with artist prefix check
      3. Title substring (last resort for short titles)

    Returns list of matches sorted: FLAC first, then M4A, then MP3.
    """
    if not _inventory.is_refreshed():
        refresh()

    title_n  = _norm_for_lookup(title)
    artist_n = _norm_for_lookup(artist)

    if not title_n:
        return []

    title_tokens  = set(title_n.split())
    artist_tokens = set(artist_n.split())

    hits: list[tuple[float, DeviceFile]] = []
    FORMAT_SCORE = {".flac": 3, ".m4a": 2, ".aac": 2, ".mp3": 1, ".wma": 0}

    for stem, df_list in _inventory._by_stem.items():
        stem_tokens = set(stem.split())
        # Title token overlap (Jaccard)
        if not stem_tokens or not title_tokens:
            continue
        jaccard = len(title_tokens & stem_tokens) / len(title_tokens | stem_tokens)
        if jaccard < 0.5:
            continue
        # Artist presence bonus
        artist_bonus = 0.2 if any(t in stem for t in artist_tokens if len(t) > 3) else 0.0
        score = jaccard + artist_bonus
        for df in df_list:
            fmt_bonus = FORMAT_SCORE.get(df.ext, 0) * 0.05
            hits.append((score + fmt_bonus, df))

    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:10]]


def get_album_context(album_name: str, artist_name: str = "") -> list[DeviceFile]:
    """
    Return all audio files in folders whose name contains the album name.
    Used to give the AI sibling-track context ("what else is in this album?").
    """
    if not _inventory.is_refreshed():
        refresh()

    album_n  = _norm_for_lookup(album_name)
    artist_n = _norm_for_lookup(artist_name)
    results: list[DeviceFile] = []

    for folder, files in _inventory._by_folder.items():
        folder_n = _norm_for_lookup(posixpath.basename(folder))
        if album_n and album_n in folder_n:
            if artist_n and artist_n not in folder_n:
                # Check parent folder too
                parent_n = _norm_for_lookup(posixpath.basename(posixpath.dirname(folder)))
                if artist_n not in parent_n:
                    continue
            results.extend(files)

    # Sort by filename (track number order)
    results.sort(key=lambda f: f.filename)
    return results[:20]   # cap at 20 sibling tracks


def get_duplicates(stem: str) -> list[DeviceFile]:
    """Return all device files whose stem matches (catches FLAC+MP3 dupes)."""
    if not _inventory.is_refreshed():
        refresh()
    stem_n = _norm_for_lookup(stem)
    return _inventory._by_stem.get(stem_n, [])


def get_device_summary() -> dict:
    """
    Return a summary dict for logging/reporting:
    {total, internal, sd, by_format, duplicate_groups, stale, age_hours}
    """
    inv = _inventory
    by_format = {}
    for f in inv.files:
        by_format[f.ext] = by_format.get(f.ext, 0) + 1

    # Duplicate groups = stems with > 1 file
    dup_groups = sum(1 for files in inv._by_stem.values() if len(files) > 1)

    return {
        "total":            len(inv.files),
        "internal":         sum(1 for f in inv.files if f.storage == "internal"),
        "sd":               sum(1 for f in inv.files if f.storage == "sd"),
        "by_format":        by_format,
        "duplicate_groups": dup_groups,
        "stale":            inv.is_stale(),
        "age_hours":        round(inv.age_hours(), 1),
        "sd_mount":         inv.sd_mount,
    }


def is_stale() -> bool:
    return _inventory.is_stale()


def age_hours() -> float:
    return _inventory.age_hours()


def is_refreshed() -> bool:
    return _inventory.is_refreshed()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    force = "--force" in sys.argv or "-f" in sys.argv
    count = refresh(force=force)
    summary = get_device_summary()
    print(f"\nDevice inventory: {count} audio files")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    if "--lookup" in sys.argv:
        idx = sys.argv.index("--lookup")
        artist = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        title  = sys.argv[idx + 2] if idx + 2 < len(sys.argv) else ""
        hits   = lookup(artist, title)
        print(f"\nLookup '{artist}' — '{title}': {len(hits)} hit(s)")
        for h in hits:
            print(f"  [{h.storage}] {h.path}")
