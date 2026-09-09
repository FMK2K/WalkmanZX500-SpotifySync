"""
device_library.py  —  Shared device album/track index
======================================================
Builds and queries a structured index of everything currently on the Walkman.

Why this module exists
----------------------
The original code used a flat set of normalised title strings to answer
"is this track on the device?".  This breaks in two ways:

  1. Same-named albums by different artists (e.g. "Greatest Hits" appears
     hundreds of times; "Drunk" exists for both Thundercat and others).
     A flat title set cannot tell them apart.

  2. Same-named tracks on different albums ("Intro", "Interlude", "Outro",
     "Love", etc.) would produce false positives — the track is considered
     present even though it belongs to a completely different album.

Fix: index every device file as (norm_artist, norm_album) -> AlbumEntry,
where AlbumEntry holds the actual folder name AND the set of track titles
in that folder.  Album identity is confirmed by checking that the Spotify
track title matches a title already in the folder, not just that any file
with that title exists somewhere on the device.

Junk folder detection
---------------------
Folders with downloader-tool prefixes (SpotiDownloader.com, ytmp3, etc.)
are flagged at index time.  Callers receive both the junk folder name
(for renaming) and the canonical target (for routing new transfers).

Usage
-----
    from device_library import DeviceLibrary

    lib = DeviceLibrary.build(config.DEVICE_NAME, config.STORAGE_ROOTS)

    # Is a Spotify track present?
    present = lib.has_track(artist="Travis Scott", album="Rodeo",
                            title="Antidote")

    # What folder do I write into?
    folder_name, is_junk = lib.resolve_folder(artist="Travis Scott",
                                               album="Rodeo")

    # Raw album entry (for audit / reorganise)
    entry = lib.get_album(artist="Travis Scott", album="Rodeo")
"""

from __future__ import annotations

import re
import logging
import pathlib
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import mtp_transfer as mtp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Folder state machine  (DeviceLibrary v3)
# ---------------------------------------------------------------------------
# Every AlbumEntry has a FolderState that encodes what the sync engine should
# do with it.  Transitions are one-directional and idempotent — running the
# same sync twice always converges to CLEAN.
#
#   UNKNOWN  →  (scan)   →  CLEAN | JUNK | MISPLACED | PARTIAL
#   JUNK     →  (rename) →  CLEAN
#   MISPLACED→  (move)   →  CLEAN
#   PARTIAL  →  (copy)   →  CLEAN
#   CLEAN    →  (audit)  →  CLEAN  (no-op on subsequent runs)
#
# ---------------------------------------------------------------------------

class FolderState(Enum):
    UNKNOWN   = auto()   # not yet classified
    CLEAN     = auto()   # canonical name, correct storage, complete tracks
    JUNK      = auto()   # folder prefix is a download-tool watermark
    MISPLACED = auto()   # on wrong storage (e.g. should be on SD, is on internal)
    PARTIAL   = auto()   # album folder exists but tracks are missing

    def transition(self, action: str) -> "FolderState":
        """
        Apply an action and return the resulting state.
        Invalid transitions return the current state unchanged (idempotent).

        Valid transitions:
            UNKNOWN  + "classify_clean"     → CLEAN
            UNKNOWN  + "classify_junk"      → JUNK
            UNKNOWN  + "classify_misplaced" → MISPLACED
            UNKNOWN  + "classify_partial"   → PARTIAL
            JUNK     + "rename"             → CLEAN
            MISPLACED+ "move"               → CLEAN
            PARTIAL  + "fill"               → CLEAN
            CLEAN    + "audit"              → CLEAN   (no-op)
        """
        _TABLE = {
            (FolderState.UNKNOWN,   "classify_clean"):     FolderState.CLEAN,
            (FolderState.UNKNOWN,   "classify_junk"):      FolderState.JUNK,
            (FolderState.UNKNOWN,   "classify_misplaced"): FolderState.MISPLACED,
            (FolderState.UNKNOWN,   "classify_partial"):   FolderState.PARTIAL,
            (FolderState.JUNK,      "rename"):             FolderState.CLEAN,
            (FolderState.MISPLACED, "move"):               FolderState.CLEAN,
            (FolderState.PARTIAL,   "fill"):               FolderState.CLEAN,
            (FolderState.CLEAN,     "audit"):              FolderState.CLEAN,
        }
        result = _TABLE.get((self, action), self)
        if result is not self:
            log.debug(f"  [FolderState] {self.name} --{action}--> {result.name}")
        return result

    @property
    def needs_action(self) -> bool:
        """True if the sync engine must do work before this folder is usable."""
        return self not in (FolderState.CLEAN,)

    @property
    def recommended_action(self) -> Optional[str]:
        """Human-readable action the sync engine should take."""
        return {
            FolderState.JUNK:      "rename",
            FolderState.MISPLACED: "move",
            FolderState.PARTIAL:   "fill",
            FolderState.UNKNOWN:   "classify",
            FolderState.CLEAN:     None,
        }[self]



# Patterns in the folder prefix (the part before the first " - ") that indicate
# the folder was named by a download tool rather than a real artist name.
_JUNK_DOMAIN_RE  = re.compile(r"\.(com|net|org|io|fm|me|co)\b", re.I)
_JUNK_NAMES: set = {
    "spotidownloader", "spotifydownloader", "downloadify",
    "mp3juice", "ytmp3", "y2mate", "snapsave", "tubidy",
    "various artists", "various", "unknown artist", "unknown",
}


def _is_junk_prefix(prefix: str) -> bool:
    """Return True if the folder name prefix looks like a download-tool stamp."""
    p = prefix.strip().lower()
    return bool(_JUNK_DOMAIN_RE.search(p) or p in _JUNK_NAMES)


# ---------------------------------------------------------------------------
# Normalisation helpers  (mirrors genre_client and downloader conventions)
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Canonical comparison key: NFKD -> ASCII -> lower -> strip articles -> collapse ws.

    2026-09-04: expand "&" to "and" before the ASCII-strip so "Arnold &
    Danny" (Spotify's canonical title) and "Arnold and Danny" (as tagged
    on-device) land on the same key — "&" used to just vanish like any
    other punctuation, splitting one track into two different keys and
    making album_completeness report a present track as missing.
    """
    text = str(text).replace("&", " and ")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    text = re.sub(r"^(the |a |an )", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def _strip_feat(title: str) -> str:
    """Remove (feat. X) / [ft. X] / [featuring X] suffixes."""
    return re.sub(
        r"\s*[\(\[\{](?:feat|ft|featuring)[^\)\]\}]*[\)\]\}]",
        "", title, flags=re.I,
    ).strip()


_FZ_PARENS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FZ_TAIL = re.compile(
    r"\s*[-–]\s*(?:\d{4}\s*)?(?:remaster(?:ed)?|mono|stereo|live|radio\s*edit|"
    r"single\s*version|album\s*version|extended|edit|version|instrumental|"
    r"bonus\s*track|deluxe|explicit|clean)\s*$", re.I)


def _fuzzy_norm_title(t: str) -> str:
    t = re.sub(r"^\d{1,3}[\s\-\.]+", "", t or "").strip()
    t = _FZ_PARENS.sub("", t)
    prev = None
    while prev != t:
        prev = t
        t = _FZ_TAIL.sub("", t).strip()
    return _norm(_strip_feat(t))


def _fuzzy_title_in(title: str, title_keys: "set[str]") -> bool:
    """Same-album fuzzy title presence: exact after aggressive normalisation,
    prefix within 3 chars, or SequenceMatcher >= 0.90 (min 5 chars)."""
    a = _fuzzy_norm_title(title)
    if not a or len(a) < 3:
        return False
    import difflib
    for k in title_keys:
        b = _fuzzy_norm_title(k)
        if not b:
            continue
        if a == b:
            return True
        lo, hi = sorted((a, b), key=len)
        if hi.startswith(lo) and len(hi) - len(lo) <= 3 and (len(lo) >= 4 or lo.isalpha()):
            return True
        if len(lo) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.90:
            return True
    return False


def _title_keys(raw_stem: str) -> list[str]:
    """
    Return multiple normalised lookup keys for a filename stem so we can
    match regardless of whether the filename uses the full
    "Artist - Album - NN Title" format or just "Title".

    Keys generated (all normalised):
      - full stem after stripping leading track number
      - last segment after splitting on " - "  (bare title)
      - feat-stripped variants of both of the above
    """
    stem = re.sub(r"^\d{1,3}[\s\-\.]+", "", raw_stem).strip()
    parts = [p.strip() for p in stem.split(" - ")]
    candidates = [stem, parts[-1]] if len(parts) >= 2 else [stem]
    result = []
    for c in candidates:
        result.append(_norm(c))
        stripped = _norm(_strip_feat(c))
        if stripped != result[-1]:
            result.append(stripped)
    return [k for k in result if k]


# ---------------------------------------------------------------------------
# AlbumEntry
# ---------------------------------------------------------------------------

@dataclass
class AlbumEntry:
    """Represents one album folder on the device."""

    # The actual folder name as it appears on-device, e.g. "Rodeo" or
    # "SpotiDownloader.com - Drunk".
    folder_name:  str

    # Storage root this folder lives on, e.g. "Internal shared storage".
    storage:      str

    # Path components from the storage root to the album folder,
    # e.g. ["Music", "Rodeo"].
    parts:        list[str]

    # Normalised title keys for every track file in this folder.
    # Used to confirm album identity when artist+album names are ambiguous.
    title_keys:   set = field(default_factory=set)

    # True if the folder prefix looks like a download-tool stamp.
    is_junk:      bool = False

    # The canonical "Artist - Album" name this folder *should* have.
    # Populated by DeviceLibrary when the Spotify metadata is available.
    canonical_name: Optional[str] = None

    # Formal folder state — drives sync engine decisions idempotently.
    # Set to UNKNOWN at construction; DeviceLibrary.classify_all() resolves it.
    state: FolderState = field(default=FolderState.UNKNOWN)

    # Expected total track count from Spotify (set by caller after Spotify fetch).
    # When > 0, used by classify() to detect PARTIAL folders.
    expected_track_count: int = 0

    def classify(
        self,
        preferred_storage: str = "",
        spotify_track_count: int = 0,
    ) -> FolderState:
        """
        Classify this folder and advance its state machine.

        Rules applied in priority order:
          1. Junk prefix            → JUNK     (name must be fixed before use)
          2. Wrong storage          → MISPLACED (tracks on internal when should be SD)
          3. Track count too low    → PARTIAL   (album present but incomplete)
          4. Otherwise              → CLEAN

        The result is stored in self.state and returned.
        Calling classify() on an already-CLEAN entry is a no-op (idempotent).
        """
        if self.state == FolderState.CLEAN:
            return self.state

        action: str
        if self.is_junk:
            action = "classify_junk"
        elif preferred_storage and self.storage != preferred_storage:
            action = "classify_misplaced"
        elif spotify_track_count > 0 and len(self.title_keys) < spotify_track_count * 0.7:
            # Folder exists but is clearly incomplete (missing >30% of tracks)
            action = "classify_partial"
        else:
            action = "classify_clean"

        self.state = FolderState.UNKNOWN.transition(action)
        return self.state

    def mark_action_complete(self, action: str) -> FolderState:
        """
        Advance the state after the sync engine has performed an action.
        E.g. after renaming a JUNK folder: entry.mark_action_complete("rename") → CLEAN.
        """
        self.state = self.state.transition(action)
        return self.state

    def contains_title(self, title: str) -> bool:
        """Return True if any normalised key of 'title' matches a file in this folder."""
        for k in _title_keys(title):
            if k in self.title_keys:
                return True
        return False

    def match_score(self, artist: str, album: str) -> int:
        """
        Heuristic similarity score (higher = better match) against a
        Spotify (artist, album) pair.  Used to pick the best folder when
        multiple candidates exist for the same album name.

        Score components:
          +4  exact norm(artist) == norm(folder_artist_part)
          +3  exact norm(album)  == norm(folder_album_part)
          +2  norm(artist) in folder name (substring)
          +1  norm(album)  in folder name (substring)
          -2  folder is a junk prefix
        """
        score = 0
        fn = self.folder_name

        # Split folder into (prefix, album_part) on first " - "
        if " - " in fn:
            folder_prefix, folder_album = fn.split(" - ", 1)
        else:
            folder_prefix, folder_album = "", fn

        na = _norm(artist)
        nb = _norm(album)
        fp = _norm(folder_prefix)
        fa = _norm(folder_album)
        fn_norm = _norm(fn)

        if na and na == fp:                    score += 4
        if nb and nb == fa:                    score += 3
        if na and na in fn_norm:               score += 2
        if nb and nb in fn_norm:               score += 1
        if self.is_junk:                       score -= 2

        return score


# ---------------------------------------------------------------------------
# DeviceLibrary
# ---------------------------------------------------------------------------

class DeviceLibrary:
    """
    Structured index of every audio file on the Walkman.

    Internal structure:
        _by_album_norm:  {norm_album: [AlbumEntry, ...]}
            Primary lookup — find candidate folders by album name alone.

        _by_artist_album: {(norm_artist, norm_album): AlbumEntry}
            Secondary lookup — once we have a confirmed (artist, album) pair.

    Both are populated by build().  Callers use has_track() and
    resolve_folder() which apply the confirmation logic automatically.
    """

    def __init__(self):
        self._by_album_norm:    dict[str, list[AlbumEntry]] = {}
        self._by_artist_album:  dict[tuple, AlbumEntry]     = {}
        self._all_title_keys:   set[str]                    = set()   # global fast-miss check

    # -----------------------------------------------------------------------
    # Factory
    # -----------------------------------------------------------------------

    @classmethod
    def build(cls, device_name: str, storage_roots: list[tuple]) -> "DeviceLibrary":
        """
        Scan all storage roots on the device and build the full index.
        Silently skips storage roots that are absent (e.g. no SD card).
        Use from_scan() instead when you already have scan results to avoid
        a redundant MTP traversal.

        MK8 Fix — cache fallback on zero-scan.
        LOG_17 showed `[DeviceLibrary] Built: 0 album folder(s)` because the
        MTP scan returned empty (USB/power-save issue) at the exact moment
        Stage 0 ran.  An empty library makes find_track() return None for
        every track, which causes every song to be re-transferred as if it
        were new — resulting in 153+ duplicate transfers and no existing-file
        skips.  The fix: when the live scan returns 0 files, load from the
        persisted JSON cache (written at the end of each successful run).
        If the cache is also absent (true first-run), fall through to empty.
        """
        import pathlib
        # Store cache alongside the project files so it survives OS temp-dir
        # cleans and reboots between pipeline runs (the old tempdir path caused
        # "No usable cache found" on every new process, treating all 3,507
        # albums as missing and triggering a full wasteful re-download pass).
        _cache_path = (pathlib.Path(__file__).resolve().parent.parent / "state" / ".walkman_device_lib_cache.json")

        lib = cls()
        try:
            device_files = mtp.scan_device_audio(device_name, storage_roots)
        except Exception as e:
            log.warning(f"[DeviceLibrary] scan failed: {e} — attempting cache load")
            device_files = []

        if device_files:
            built = cls.from_scan(device_files)
            # Persist for next run / zero-scan fallback
            try:
                built.save(_cache_path)
            except Exception:
                pass
            return built

        # Scan returned 0 — almost certainly a USB/MTP glitch, not an empty device.
        log.warning(
            "[DeviceLibrary] Live scan returned 0 audio files — "
            "this is almost certainly a device-scan failure (USB/MTP issue), "
            "not an empty library.  Attempting to load from cache."
        )
        cached = cls.load(_cache_path)
        if cached is not None:
            log.warning(
                f"[DeviceLibrary] Using CACHED library — "
                f"cross-storage and newly-removed track detection may be stale.  "
                f"Eject and reconnect the Walkman to force a fresh scan next run."
            )
            return cached

        log.warning(
            "[DeviceLibrary] No usable cache found.  Returning empty library — "
            "all tracks will be treated as new and re-transferred.  "
            "Check USB connection, MTP mode, and that the device is unlocked."
        )
        return lib

    @classmethod
    def from_scan(cls, device_files: list[dict]) -> "DeviceLibrary":
        """
        Build a DeviceLibrary from an already-completed scan result list
        (as returned by mtp.scan_device_audio).  Use this when the caller
        has already scanned the device so we don't traverse MTP twice.
        """
        lib = cls()
        folders: dict[tuple, AlbumEntry] = {}

        for ef in device_files:
            parts_list = ef.get("parts", [])
            storage    = ef.get("storage", "")
            filename   = ef.get("filename", "")

            if len(parts_list) < 2:
                for k in _title_keys(filename):
                    lib._all_title_keys.add(k)
                continue

            folder_key    = (storage, tuple(parts_list))
            actual_folder = parts_list[-1]

            if folder_key not in folders:
                if " - " in actual_folder:
                    prefix = actual_folder.split(" - ", 1)[0]
                    junk   = _is_junk_prefix(prefix)
                else:
                    junk = False
                folders[folder_key] = AlbumEntry(
                    folder_name=actual_folder,
                    storage=storage,
                    parts=list(parts_list),
                    is_junk=junk,
                )

            entry = folders[folder_key]
            for k in _title_keys(pathlib.Path(filename).stem):
                entry.title_keys.add(k)
                lib._all_title_keys.add(k)

        return lib._finish_build(folders)

    def _finish_build(self, folders: dict) -> "DeviceLibrary":
        """Index the collected AlbumEntry objects into lookup dicts."""
        for entry in folders.values():
            fn = entry.folder_name
            if " - " in fn and not entry.is_junk:
                album_part = fn.split(" - ", 1)[1]
            else:
                album_part = fn

            album_norm = _norm(album_part)
            if album_norm:
                self._by_album_norm.setdefault(album_norm, []).append(entry)

            full_norm = _norm(fn)
            if full_norm != album_norm and full_norm:
                self._by_album_norm.setdefault(full_norm, []).append(entry)

        total_tracks  = len(self._all_title_keys)
        total_folders = len(folders)
        log.info(
            f"[DeviceLibrary] Built: {total_folders} album folder(s), "
            f"{total_tracks} track title key(s) indexed."
        )
        return self

    def classify_all(
        self,
        preferred_storage: str = "",
        spotify_track_counts: dict[str, int] | None = None,
    ) -> dict[FolderState, list[AlbumEntry]]:
        """
        Classify every AlbumEntry in one pass and return a dict grouping
        entries by resulting FolderState.

        Args:
            preferred_storage:    e.g. "SD card" — entries on a different
                                  storage are flagged MISPLACED.
            spotify_track_counts: {"norm_artist|norm_album": expected_count}
                                  Used to detect PARTIAL folders.  Build from
                                  the Spotify track_map after Stage 1.

        Returns:
            {
                FolderState.CLEAN:     [...],
                FolderState.JUNK:      [...],
                FolderState.MISPLACED: [...],
                FolderState.PARTIAL:   [...],
            }

        Typical call (after Stage 3 scan, before Stage 6 transfer):
            groups = dev_lib.classify_all(
                preferred_storage=config.WRITE_STORAGE,
                spotify_track_counts=track_counts,
            )
            for entry in groups[FolderState.JUNK]:
                mtp.rename_folder(...)
                entry.mark_action_complete("rename")
        """
        counts = spotify_track_counts or {}
        result: dict[FolderState, list[AlbumEntry]] = {s: [] for s in FolderState}

        for entry in self.all_entries():
            fn = entry.folder_name
            # Build lookup key for spotify_track_counts: "norm_artist|norm_album"
            if " - " in fn:
                a_part, b_part = fn.split(" - ", 1)
            else:
                a_part, b_part = "", fn
            sp_count = counts.get(f"{_norm(a_part)}|{_norm(b_part)}", 0)

            state = entry.classify(
                preferred_storage=preferred_storage,
                spotify_track_count=sp_count,
            )
            result[state].append(entry)

        # Summarise so it shows in the log without being spammy
        summary = ", ".join(
            f"{s.name}={len(v)}" for s, v in result.items() if v
        )
        log.info(f"[DeviceLibrary] classify_all: {summary}")
        return result

    def pending_actions(self) -> list[tuple[AlbumEntry, str]]:
        """
        Return [(entry, recommended_action), ...] for every entry that
        needs work, ordered by urgency: JUNK first, then MISPLACED, PARTIAL.
        Entries already CLEAN or UNKNOWN are excluded.
        """
        priority = {
            FolderState.JUNK:      0,
            FolderState.MISPLACED: 1,
            FolderState.PARTIAL:   2,
        }
        actions = [
            (e, e.state.recommended_action)
            for e in self.all_entries()
            if e.state in priority and e.state.recommended_action
        ]
        actions.sort(key=lambda x: priority.get(x[0].state, 99))
        return actions

    # -----------------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------------

    def _find_best_entry(self, artist: str, album: str) -> Optional[AlbumEntry]:
        """
        Find the best-matching AlbumEntry for (artist, album).

        Steps:
          1. Look up candidates by norm(album).
          2. If only one candidate: return it (no ambiguity).
          3. If multiple: score each against artist+album and return highest.
          4. Tie-break: prefer non-junk folders.
          5. Returns None if no candidates found.
        """
        album_norm = _norm(album)
        candidates = self._by_album_norm.get(album_norm, [])

        # Also try the full "Artist - Album" form in case the folder is indexed that way
        if not candidates:
            full_norm = _norm(f"{artist} - {album}")
            candidates = self._by_album_norm.get(full_norm, [])

        # Also try with article stripped from album
        if not candidates:
            stripped = _norm(re.sub(r"^(the |a |an )", "", album.strip().lower()))
            candidates = self._by_album_norm.get(stripped, [])

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        # Multiple candidates — score and sort
        scored = sorted(
            candidates,
            key=lambda e: (e.match_score(artist, album), not e.is_junk),
            reverse=True,
        )
        best = scored[0]

        # If top score is 0 or negative (no useful signal), return None rather
        # than a random wrong album
        if best.match_score(artist, album) <= 0:
            log.debug(
                f"[DeviceLibrary] No confident match for '{artist} - {album}' "
                f"among {len(candidates)} candidate(s) — treating as absent"
            )
            return None

        if len(scored) > 1 and scored[0].match_score(artist, album) == scored[1].match_score(artist, album):
            log.debug(
                f"[DeviceLibrary] Tied match for '{artist} - {album}': "
                f"'{scored[0].folder_name}' vs '{scored[1].folder_name}' — using first"
            )

        return best

    def has_track(self, artist: str, album: str, title: str) -> bool:
        """
        Return True only if:
          1. A folder matching (artist, album) exists on the device, AND
          2. That folder contains a file whose normalised title matches 'title'.

        This prevents false positives from same-named tracks on different albums.

        Falls back to a global title-only check ONLY when no album folder
        is found at all (handles flat-structured libraries with no album folders).
        """
        entry = self._find_best_entry(artist, album)
        if entry is not None:
            return entry.contains_title(title)

        # No album folder found — fall back to global title presence check.
        # This is intentionally conservative: it only fires when there is
        # genuinely no album-folder structure to confirm against.
        for k in _title_keys(title):
            if k in self._all_title_keys:
                # Encode to ASCII for cp1252 terminals (non-ASCII chars crash logging)
                safe_label = f"{artist} - {album}".encode("ascii", "replace").decode("ascii")
                safe_title = title.encode("ascii", "replace").decode("ascii")
                log.debug(
                    f"[DeviceLibrary] Album folder not found for '{safe_label}'; "
                    f"'{safe_title}' matched via global title index (may be false positive)"
                )
                return True
        return False

    def has_track_as_flac(self, artist: str, album: str, title: str) -> bool:
        """
        Return True only when the track is present AND its on-device filename
        ends with .flac.  Used by the album-wise downloader FLAC-upgrade path
        (I3) to decide whether to skip a track or replace an existing MP3.

        When the device's AlbumEntry doesn't store per-file extensions (older
        index) this safely falls back to has_track() — no upgrade is attempted.
        """
        entry = self._find_best_entry(artist, album)
        if entry is None:
            return False
        if not entry.contains_title(title):
            return False
        # Check per-file extension if stored; else treat as FLAC (conservative)
        nk = set(_title_keys(title))
        for tk, ext in getattr(entry, "title_exts", {}).items():
            if tk in nk:
                return ext.lower() == ".flac"
        # No extension info — assume present in correct format (skip upgrade)
        return True

    def add_track(
        self,
        artist: str,
        album: str,
        title: str,
        storage: str,
        parts: list,
        ext: str = "",
    ) -> None:
        """
        Register a newly transferred track so subsequent has_track() calls in
        the same run return True without a second MTP scan.

        Creates a minimal AlbumEntry if one doesn't exist yet for (artist, album).
        Used by salvage and album-wise transfer to keep dev_lib current.
        """
        folder_name = parts[-2] if len(parts) >= 2 else (parts[0] if parts else album)
        folder_parts = list(parts[:-1]) if len(parts) >= 2 else list(parts)

        entry = self._find_best_entry(artist, album)
        if entry is None:
            entry = AlbumEntry(
                folder_name=folder_name,
                storage=storage,
                parts=folder_parts,
                is_junk=False,
            )
            album_norm = _norm(album)
            self._by_album_norm.setdefault(album_norm, []).append(entry)
            full_norm = _norm(f"{artist} - {album}")
            if full_norm != album_norm:
                self._by_album_norm.setdefault(full_norm, []).append(entry)

        for k in _title_keys(title):
            entry.title_keys.add(k)
            self._all_title_keys.add(k)
        # Store extension for has_track_as_flac() — lazy init
        if ext:
            if not hasattr(entry, "title_exts"):
                entry.title_exts = {}
            for k in _title_keys(title):
                entry.title_exts[k] = ext

    def resolve_folder(self, artist: str, album: str) -> tuple[Optional[str], bool]:
        """
        Return (folder_name, is_junk) for the best matching album folder.

          folder_name — the actual on-device folder name to use as dest_parts,
                        or None if no folder exists yet (create fresh canonical).
          is_junk     — True if the folder has a poisoned name and should be
                        renamed to canonical before writing new files into it.

        Callers should:
          - If is_junk: call mtp.rename_folder_on_device() first, then use
            the canonical name as the destination.
          - If folder_name is None: create a fresh canonical folder.
          - Otherwise: use folder_name directly.
        """
        entry = self._find_best_entry(artist, album)
        if entry is None:
            return None, False
        return entry.folder_name, entry.is_junk

    def get_entry(self, artist: str, album: str) -> Optional[AlbumEntry]:
        """Return the raw AlbumEntry for (artist, album), or None."""
        return self._find_best_entry(artist, album)

    def all_entries(self) -> list[AlbumEntry]:
        """Return all indexed AlbumEntry objects (for audit / reorganise)."""
        seen = set()
        result = []
        for entries in self._by_album_norm.values():
            for e in entries:
                fid = (e.storage, tuple(e.parts))
                if fid not in seen:
                    seen.add(fid)
                    result.append(e)
        return result

    def junk_entries(self) -> list[AlbumEntry]:
        """Return all AlbumEntry objects with junk folder names."""
        return [e for e in self.all_entries() if e.is_junk]

    # -----------------------------------------------------------------------
    # Persistence  (cache across runs — avoids full ADB re-scan every time)
    # -----------------------------------------------------------------------

    def save(self, cache_path: "pathlib.Path") -> None:
        """
        Serialise the library to a JSON cache file.  Called at the end of
        run_downloads() so the next run can load() instead of relying on the
        ADB scan (which always returns 0 on NW-ZX507 in MTP mode).

        Uses an atomic write (write .tmp → rename) so a crash or USB
        disconnect mid-write never leaves a corrupt cache behind.
        """
        import json as _j
        records = []
        seen: set = set()
        for entries in self._by_album_norm.values():
            for entry in entries:
                fid = (entry.storage, tuple(entry.parts))
                if fid in seen:
                    continue
                seen.add(fid)
                title_exts = getattr(entry, "title_exts", {})
                for tk in entry.title_keys:
                    ext = title_exts.get(tk, ".m4a")
                    records.append({
                        "storage":  entry.storage,
                        "parts":    list(entry.parts),
                        "filename": entry.folder_name + "/" + tk + ext,
                        "ext":      ext,
                        "_folder":  entry.folder_name,
                        "_tk":      tk,
                    })
        dst = pathlib.Path(cache_path)
        tmp = dst.with_suffix(".tmp")
        try:
            tmp.write_text(
                _j.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(dst)   # atomic on same filesystem (POSIX + Windows)
            log.info(
                f"[DeviceLibrary] Cache saved: {len(records)} track record(s) "
                f"→ {dst.name}"
            )
        except Exception as e:
            tmp.unlink(missing_ok=True)
            log.warning(f"[DeviceLibrary] Cache save failed: {e}")

    @classmethod
    def load(cls, cache_path: "pathlib.Path") -> "DeviceLibrary | None":
        """
        Load a persisted library from the JSON cache.
        Returns None if the file is absent or corrupt — callers must handle
        None gracefully (fall back to live scan or empty library).

        Reconstructs via from_scan() so the full index (_by_album_norm,
        _all_title_keys, AlbumEntry.title_keys) is rebuilt cleanly from the
        saved records rather than being restored from raw JSON state.
        """
        import json as _j
        p = pathlib.Path(cache_path)
        if not p.exists():
            return None
        try:
            records = _j.loads(p.read_text(encoding="utf-8"))
            if not isinstance(records, list) or not records:
                return None
            # Reconstruct scan-format dicts — use _folder to preserve the
            # exact on-device folder name so resolve_folder() returns it
            # correctly rather than a normalised approximation.
            device_files = []
            for r in records:
                folder = r.get("_folder", "")
                tk     = r.get("_tk", "")
                ext    = r.get("ext", ".m4a")
                device_files.append({
                    "storage":  r.get("storage", ""),
                    "parts":    r.get("parts", []),
                    "filename": tk + ext,
                    "ext":      ext,
                })
            lib = cls.from_scan(device_files)
            log.info(
                f"[DeviceLibrary] Cache loaded: {len(records)} record(s) from "
                f"{p.name}  ({len(lib._all_title_keys)} title key(s))"
            )
            return lib
        except Exception as e:
            log.warning(f"[DeviceLibrary] Cache load failed ({e}) — starting fresh.")
            return None

    def merge(self, other: "DeviceLibrary") -> None:
        """
        Merge entries from a live scan result on top of cached data.

        Policy: cache wins on conflicts — cached entries have exact on-device
        folder names (e.g. "Aminé - Limbo") whereas a live ADB scan may
        return normalised or truncated names.  New folders seen only in the
        live scan are added freely.

        Also merges _all_title_keys so global fast-miss checks stay accurate.
        """
        for entries in other._by_album_norm.values():
            for entry in entries:
                album_norm = _norm(
                    entry.folder_name.split(" - ", 1)[-1]
                    if " - " in entry.folder_name
                    else entry.folder_name
                )
                existing_fids = {
                    (e.storage, tuple(e.parts))
                    for e in self._by_album_norm.get(album_norm, [])
                }
                if (entry.storage, tuple(entry.parts)) not in existing_fids:
                    self._by_album_norm.setdefault(album_norm, []).append(entry)
        self._all_title_keys.update(other._all_title_keys)
        log.debug(
            f"[DeviceLibrary] merge complete — "
            f"{len(self._all_title_keys)} total title key(s)"
        )



