"""
walkman_sync.py  —  Walkman Sync Agent pipeline orchestrator
=============================================================
This module coordinates the full nine-stage sync pipeline.
It imports from all other modules but contains no COM/MTP code itself.

... (header unchanged) ...
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════════════
# MK34 P4-T2: Stage → State Invariant Gate
# ═══════════════════════════════════════════════════════════════════════════
# CTO Class C fix: "no forward progress after failed invariant."
# Stage 0 (delete) is blocked until invariants verify Stage 1 is safe to run.
# Sets walkman_dedup.DEDUP_DESTRUCTIVE_VERIFIED=True after invariants pass.
# Hard exception path; no continue-past.
def _mk34_p4t2_run_invariant_gate(observed_state=None, desired_state=None,
                                   plan=None) -> bool:
    """
    Returns True if invariants pass (stage may proceed); raises InvariantViolation
    via mk34_invariants.gate_pre_execution() if any invariant fails.

    On pass, sets walkman_dedup.DEDUP_DESTRUCTIVE_VERIFIED=True so destructive
    paths can run.
    """
    import logging as _ws_log
    log = _ws_log.getLogger(__name__)

    # If MK34 modules aren't installed yet, fail-open with WARNING (graceful
    # degradation during incremental rollout). Production: install all modules.
    try:
        from mk34_invariants import gate_pre_execution, InvariantViolation
        from walkman_dedup import _set_dedup_verified
    except ImportError as e:
        log.warning(
            f"  [WalkmanSync/MK34-P4-T2] Invariant gate not wired: {e}. "
            f"Falling back to pre-MK34 behavior. Install mk34_invariants for "
            f"the Class C fix to take effect."
        )
        return True

    # If caller hasn't provided state objects yet (incremental rollout), skip
    # the gate but log loudly so it's visible.
    if observed_state is None or desired_state is None or plan is None:
        log.info(
            f"  [WalkmanSync/MK34-P4-T2] Invariant gate inputs not provided "
            f"(observed/desired/plan = None). Stage proceeding without gate."
        )
        return True

    try:
        gate_pre_execution(observed_state, desired_state, plan)
        _set_dedup_verified(True)
        log.info(
            f"  [WalkmanSync/MK34-P4-T2] Invariant gate PASSED. "
            f"DEDUP_DESTRUCTIVE_VERIFIED=True. Stage may proceed."
        )
        return True
    except InvariantViolation as iv:
        log.error(
            f"  [WalkmanSync/MK34-P4-T2] INVARIANT_VIOLATED: {iv}. "
            f"Stage aborted via RunAbortPolicy (MK26 C5)."
        )
        # Try to use existing RunAbortPolicy
        try:
            from run_policy import RunAbortPolicy
            RunAbortPolicy.get().abort(reason=str(iv))
        except Exception:
            pass
        # Re-raise so the calling stage can't continue silently
        raise


_DEVICE_INVENTORY_IS_LIVE: bool = False  # MK19-RC2: set True after live ADB scan

import re
import sys
import shutil
import pathlib
import queue
import tempfile
import time
import unicodedata
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, ID3NoHeaderError, POPM
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

import config
import id3_helpers as _idh   # REV 14: central ID3 v2.4→v2.3 save helpers
try:
    import meta_key as _mk
except ImportError:
    _mk = None
import spotify_client as sc
import genre_client   as gc
import genre_client          # P10: bare name needed for genre_client._broad_genres() call
import mtp_transfer   as mtp
from mtp_transfer import StorageFullError


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _make_utf8_safe_stream(stream):
    """
    Return a stream that tolerates non-ASCII characters on Windows cp1252 consoles.

    Rev 14.6 hardening: the previous implementation relied on ``hasattr(stream, "buffer")``
    which returns False when stdout has been redirected (VS Code code-runner, pythonw,
    pipe-to-file, ...). When that check failed, the original cp1252 stream was used
    unchanged and any log message containing an em-dash or arrow crashed the logger
    (LOG 2026-04-19 ai_helper.py:95 UnicodeEncodeError).  Now we try three strategies
    in order: reconfigure(), TextIOWrapper(buffer), and finally a no-op wrapper that
    catches encode errors at write time.
    """
    # Strategy 1: Python 3.7+ supports .reconfigure on the standard streams directly
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
            return stream
    except Exception:
        pass
    # Strategy 2: wrap .buffer in a UTF-8 TextIOWrapper
    try:
        if hasattr(stream, "buffer"):
            import io as _io
            return _io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
    except Exception:
        pass
    # Strategy 3: last-resort wrapper that swallows encode errors on write
    class _ReplaceStream:
        def __init__(self, inner):
            self._inner = inner
        def write(self, s):
            try:
                return self._inner.write(s)
            except UnicodeEncodeError:
                enc = getattr(self._inner, "encoding", "ascii") or "ascii"
                return self._inner.write(s.encode(enc, errors="replace").decode(enc, errors="replace"))
        def flush(self):
            try: return self._inner.flush()
            except Exception: pass
        def __getattr__(self, name):
            return getattr(self._inner, name)
    return _ReplaceStream(stream)


def setup_logging(log_callback: Callable[[str], None] | None = None):
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # File handler — INFO and above only, keeps log files readable
    file_h = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_h.setLevel(logging.INFO)
    file_h.setFormatter(fmt)

    # stdout — DEBUG for diagnosis when running interactively.
    # Rev 14.6: robust UTF-8 setup that works under VS Code code-runner and pipes.
    _utf8_stdout = _make_utf8_safe_stream(sys.stdout)
    stdout_h = logging.StreamHandler(_utf8_stdout)
    stdout_h.setLevel(logging.DEBUG)
    stdout_h.setFormatter(fmt)

    handlers: list[logging.Handler] = [file_h, stdout_h]

    if log_callback:
        # GUI callback — INFO and above (keeps the log panel clean)
        cb_h = _CallbackHandler(log_callback)
        cb_h.setLevel(logging.INFO)
        cb_h.setFormatter(fmt)
        handlers.append(cb_h)

    logging.basicConfig(
        level=logging.DEBUG,   # root level DEBUG so stdout sees everything
        handlers=handlers,
        force=True,
    )
    # Suppress noisy third-party debug output
    for noisy in ("urllib3", "spotipy", "requests", "mutagen", "tidalapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # MK39.1 L4.4 / Council §4.3 — emit one-line aggregated safety profile
    # at session start. Idempotent: only fires once per process.
    try:
        import platform_safety_profile
        platform_safety_profile.emit_banner()
    except Exception as _safety_err:  # never block startup on this
        logging.getLogger(__name__).debug(
            f"[Safety/L4] banner suppressed: {_safety_err!r}"
        )


log = logging.getLogger(__name__)


class _CallbackHandler(logging.Handler):
    def __init__(self, cb: Callable[[str], None]):
        super().__init__()
        self.cb = cb

    def emit(self, record: logging.LogRecord):
        try:
            self.cb(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

_FAT32_ILLEGAL = r'[\\/:*?"<>|]'


def sanitize_name(name: str, max_len: int | None = None) -> str:
    """FAT32-safe filename component.  NFC-normalised, illegal chars → '_'."""
    max_len = max_len or config.MAX_NAME_LEN
    name = name.replace("&", "and")   # & confuses Windows Shell accelerator parsing
    name = unicodedata.normalize("NFC", name)
    name = re.sub(_FAT32_ILLEGAL, "_", name)
    name = name.strip(". ")
    return name[:max_len]


def normalize_key(text: str) -> str:
    """Canonical match key: NFKD→ASCII→lower→strip articles→collapse ws."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    text = re.sub(r"^(the |a |an )", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def format_title_with_features(track: dict) -> str:
    """
    If FEATURE_ARTISTS_IN_TITLE is True and the track has multiple artists,
    appends '(feat. X, Y)' to the title — but only when the title doesn't
    already contain a feat. marker.  Prevents ZX500 artist-splintering.
    """
    title   = track["title"]
    artists = track.get("artists", [track["artist"]])
    if not config.FEATURE_ARTISTS_IN_TITLE or len(artists) <= 1:
        return title
    if re.search(r"\bfeat\.?\b", title, re.IGNORECASE):
        return title
    feat = ", ".join(artists[1:])
    return f"{title} (feat. {feat})"


def _is_valid_audio(path: pathlib.Path) -> bool:
    """
    Magic-byte check — reads only the first 12 bytes of the file.
    Returns False for corrupt files, partial downloads, or files with the
    wrong extension, without invoking mutagen (which is slow and noisy).
    """
    try:
        with open(path, "rb") as f:
            header = f.read(12)
        if not header:
            return False
        ext = path.suffix.lower()
        if ext == ".flac":
            return header[:4] == b"fLaC"
        if ext == ".mp3":
            if header[:3] == b"ID3":
                return True
            if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
                return True
            return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sync agent
# ---------------------------------------------------------------------------

TAG_WORKERS = 32  # parallel temp-file taggers (CPU+disk, pure I/O wait during transfer)
                  # 32 workers on i5 with HT: each thread sleeps while disk reads;
                  # keeps the transfer thread's queue permanently full so it never waits

# 2026-07-22 — cap on how many times a device_status='pending' file (left by
# mtp_transfer_com.copy_file_to_device's ambiguous-WPD-cache-staleness path)
# gets a cheap recheck before giving up and falling back to a real re-copy.
# Reuses sync_state's existing failure_count column rather than a new one.
_PENDING_MAX_RECHECKS = 5


class WalkmanSyncAgent:
    """
    Orchestrates the full pipeline.
    Call cancel() from another thread to stop gracefully after the current
    track or audit batch.
    """

    def __init__(self, log_callback: Callable[[str], None] | None = None):
        setup_logging(log_callback)
        self.library:     dict = {}
        self.missing:     list = []
        self._cancelled:  bool = False
        self._cancel_flag: list = [False]  # mutable ref for worker threads
        self._lib_lock         = threading.Lock()
        self._stats_lock       = threading.Lock()
        self.stats = {
            "downloaded":   0,
            "copied":       0,
            "updated":      0,
            "skipped":      0,
            "missing":      0,
            # MK10 ISSUE-G: space housekeeping counters
            "dedup":        {},    # populated by prune_before_sync
            "sd_migration": {},    # populated by migrate_internal_to_sd
        }
        # Tracks successfully transferred this session — used by smart playlists
        # to map Spotify URIs to device paths without a full re-scan.
        self.tracks_transferred: list[dict] = []
        self._hard_stop_transfers: bool = False  # MK18-F11: set by midrun hard floor
        self._device_inventory_is_live: bool = False  # MK19-RC2: set after live ADB scan

    def cancel(self):
        self._cancelled = True
        self._cancel_flag[0] = True
        log.info("Cancellation requested — stopping after current track.")

    def _inc_stat(self, key: str, n: int = 1):
        with self._stats_lock:
            self.stats[key] += n

    # -----------------------------------------------------------------------
    # Stage 1: Spotify fetch
    # -----------------------------------------------------------------------

    def _fetch_from_spotify(self, sp) -> tuple[dict[str, list], set[str], set[str]]:
        """
        Wraps spotify_client.fetch_and_build() OR (Rev 14.9.5) the offline
        loader spotify_local_data.fetch_and_build_offline() based on
        config.OFFLINE_MODE.

        Returns a 3-tuple:
            track_map       — {playlist_name: [track_dict, ...]}
            liked_track_ids — set of Spotify track IDs in Liked Songs
            saved_album_ids — set of Spotify album IDs the user has saved/liked

        The third element is passed directly to dl.run_downloads() so the
        album-wise download path correctly fires on all 148 liked albums, not
        just albums whose tracks appear in the Liked Songs playlist.
        """
        # Rev 14.9.5 — offline mode skips the API entirely
        if getattr(config, "OFFLINE_MODE", False):
            log.info("[Sync] OFFLINE_MODE=True — building track_map from local "
                     "Spotify export only (no API calls)")
            try:
                import spotify_local_data as _sld
                track_map, liked_ids, saved_album_ids = _sld.fetch_and_build_offline()
                return track_map, liked_ids, saved_album_ids
            except Exception as e:
                log.critical(
                    f"[Sync] OFFLINE_MODE failed: {e}.  "
                    f"To fix: ensure 'Spotify Account Data' folder exists in "
                    f"the project directory, or set OFFLINE_MODE=False to use "
                    f"the API instead."
                )
                raise

        # Online mode — original Rev 14.9.4 behaviour
        result = sc.fetch_and_build(sp)
        # fetch_and_build now always returns 3 values; guard against an older
        # cached version returning 2 in case of a partial upgrade.
        if len(result) == 3:
            track_map, liked_ids, saved_album_ids = result
        else:
            track_map, liked_ids = result
            saved_album_ids = set()

        # TIER 0: augment with offline Spotify account data export.
        # Enriches is_liked flags, play counts, and fills playlists the
        # API may have returned empty (rate limit) from local data — zero API calls.
        try:
            import spotify_local_data as _sld
            track_map = _sld.augment_track_map(track_map, liked_ids=liked_ids)
            # Also augment liked_ids with anything in the local library export
            _local_liked = _sld.get_liked_uris()
            for uri in _local_liked:
                tid = uri.replace("spotify:track:", "")
                liked_ids.add(tid)
        except Exception as _sld_err:
            log.debug(f"  [SpotifyLocal] Augmentation skipped: {_sld_err}")

        return track_map, liked_ids, saved_album_ids

    # -----------------------------------------------------------------------
    # Stage 2: Genre/year enrichment
    # -----------------------------------------------------------------------

    def _enrich_genres(self, track_map: dict[str, list]) -> dict[str, list]:
        all_tracks: dict[str, dict] = {}
        for tracks in track_map.values():
            for t in tracks:
                t.setdefault("track_id", t["id"])
                all_tracks[t["id"]] = t

        meta = gc.enrich_metadata(list(all_tracks.values()))

        for tracks in track_map.values():
            for t in tracks:
                hit = meta.get(t["id"], {})
                if config.WRITE_TAGS.get("genre"):
                    t["genre"] = hit.get("genre", "Unknown")
                if config.WRITE_TAGS.get("release_year"):
                    t["release_year"] = hit.get("year") or t.get("spotify_year", "")

        # ── Rev 14.9.2: Album-level genre consensus pass ───────────────────
        # Without this, per-track lookups against MusicBrainz/Last.fm/Deezer
        # return different genres for different tracks on the same album —
        # e.g. LOG-14's "African Giant" getting "Afrobeats" on 3 tracks and
        # "Unknown" / "Pop" / "R&B" on the rest.  We unify by taking the
        # modal specific (non-Unknown, non-broad) genre per album and
        # backfilling any track that disagrees or is blank.
        #
        # Runs on the same in-memory track dicts, so downstream stages
        # (tagging, M3U8 writing, device updates) see the unified value
        # with zero extra network calls.
        if config.WRITE_TAGS.get("genre"):
            # MK17 Phase B (M3): hand the track_map to the consolidator so
            # it can build a playlist-co-occurrence graph for sparse-artist
            # rescue.  We stash on self because _consolidate_album_genres
            # already takes only the flat tracks iterable.
            self._track_map_for_graph = track_map
            self._consolidate_album_genres(all_tracks.values())
            self._track_map_for_graph = None

        return track_map

    def preview_album_genre_consolidation(self, track_map: dict[str, list]) -> list[dict]:
        """
        Dry-run version of the genre-consolidation step above: enriches genres
        the same way, then shows what _consolidate_album_genres would change
        WITHOUT writing anything or needing the MK38.0 freeze touched at all.
        Returns a list of {artist, album, title, genre_from, genre_to, tier}.
        """
        all_tracks: dict[str, dict] = {}
        for tracks in track_map.values():
            for t in tracks:
                t.setdefault("track_id", t["id"])
                all_tracks[t["id"]] = t

        meta = gc.enrich_metadata(list(all_tracks.values()))
        for t in all_tracks.values():
            hit = meta.get(t["id"], {})
            t["genre"] = hit.get("genre", "Unknown")

        self._track_map_for_graph = track_map
        try:
            return self._consolidate_album_genres(all_tracks.values(), dry_run=True)
        finally:
            self._track_map_for_graph = None

    # Genres that are too broad to be useful as the canonical album tag
    # *only when a more specific alternative also appears on the album*.
    # Rev 14.9.5 — narrowed this list dramatically.  Pre-14.9.5 included
    # "rock", "pop", "indie", "hip hop", "r&b", "electronic", which meant
    # an album of 8 hip-hop tracks where 7 returned "Hip-Hop" and 1 returned
    # "Cloud Rap" would have all 8 forced to "Cloud Rap" — over-specific.
    # Now we only block the genuinely-uninformative ones; "Hip-Hop" can be
    # the canonical album genre when it's the actual mode.
    _BROAD_GENRES = frozenset({
        "", "unknown", "world", "world music", "various",
    })

    def _consolidate_album_genres(self, tracks, dry_run: bool = False) -> list[dict]:
        """
        Group tracks by (album_artist, album) and set every track's genre
        to the album's modal genre.  Tracks already carrying that value
        are untouched.

        dry_run=True: compute everything (including graph inference) but never
        mutate a track dict. Bypasses the MK38.0 early-return so the preview
        reflects what consolidation WOULD propose if unfrozen — the freeze still
        blocks the actual write, this only blocks it earlier with no output.
        Returns a list of {artist, album, title, genre_from, genre_to, tier}
        dicts describing every proposed change (empty when dry_run=False).

        Rev 14.9.5 — two-tier consolidation:
          Tier 1: album-level mode if the album has GENRE_ALBUM_MIN_VOTES+
                  specific (non-broad) genres.
          Tier 2: artist-level mode (computed once across ALL albums by the
                  same artist in the library) as fallback for albums where
                  Tier 1 doesn't trigger.

        MK17 Phase B (M2/M3) — sparse-artist guard + graph fallback:
          Tier 2.5: when the artist's library footprint is below
                    GENRE_ARTIST_MIN_TRACKS, modal-of-1 isn't trustworthy.
                    LOG_16_1 case: Alfa Kat is an Amapiano/Bacardi artist,
                    but his single track in the library got tagged
                    "Abstract Hip-Hop" by Last.fm.  Modal-of-1 picked
                    the wrong subgenre for the entire album.
          Tier 3:  for sparse artists, use genre_graph to infer from
                   weighted-vote walk over feature-collaboration and
                   shared-playlist edges.  Strict confidence threshold
                   (>= GENRE_ARTIST_MIN_TRACKS confident neighbors) per
                   Apr 30 user direction.

        Mutates each track dict in place.  Expects tracks to be the iterable
        of dicts already enriched by gc.enrich_metadata — do NOT call this
        before genre enrichment runs.

        Rev 14.9.2 (Patch 8): when a track's genre is rewritten here, we
        also invalidate the audit state entry for any on-device file that
        matches it.  Without this, Stage 8's MetaKey fast-path would skip
        those files (the cached metakey still reflects the OLD splintered
        genre, matches the on-device file's metakey, audit concludes "no
        change needed") and leave them splintered forever.  The stage-8
        auditor will recompute the metakey from fresh, detect the diff,
        and re-tag.

        MK38.0 SEMANTIC WRITE BARRIER: This function performs *modal_consensus*
        and *graph_propagation* — both inference sources. Under freeze
        (default-ON), the entire function short-circuits and logs once. The
        log_16_1 "Alfa Kat → Abstract Hip-Hop" pattern documented above is
        precisely the contamination class the freeze blocks; the
        "everything became Afrobeats" incident is the catastrophic
        amplification of the same mechanism. Under freeze we don't propagate.
        """
        # MK38.0: Hard short-circuit — no semantic writes from inference under freeze.
        # dry_run bypasses only this early-return so preview mode can show what
        # WOULD be proposed; the write loop below is separately dry_run-gated, so
        # no track dict is ever mutated while dry_run=True regardless of freeze state.
        try:
            from mk38_0_semantic_write_barrier import is_frozen as _frozen
            if _frozen():
                if dry_run:
                    log.info(
                        "  [MK38.0] _consolidate_album_genres PREVIEW — freeze is "
                        "active (a real run would be blocked); showing what modal/"
                        "graph consolidation would propose."
                    )
                else:
                    if not getattr(self, "_consolidation_freeze_logged", False):
                        log.warning(
                            "  [MK38.0] _consolidate_album_genres SKIPPED — semantic "
                            "write freeze active. Modal/graph genre consolidation is an "
                            "inference source and is blocked under MK38.0. To re-enable "
                            "(NOT recommended), unlock hardened mode and set "
                            "MK38_0_ALLOW_SEMANTIC_WRITES=1. Logged once per run."
                        )
                        self._consolidation_freeze_logged = True
                    return []
        except ImportError:
            pass

        preview: list[dict] = []

        from collections import Counter, defaultdict

        # Materialise once — we iterate twice
        tracks = list(tracks)

        # Min votes for album-level confidence (default 2)
        min_votes = int(getattr(config, "GENRE_ALBUM_MIN_VOTES", 2) or 2)
        artist_fallback = bool(
            getattr(config, "GENRE_ARTIST_FALLBACK_ENABLED", True)
        )
        # MK17 Phase B (M2): sparse-artist threshold
        artist_min_tracks = int(getattr(config, "GENRE_ARTIST_MIN_TRACKS", 3) or 3)
        # MK17 Phase B (M3): graph-inference toggle
        graph_enabled = bool(
            getattr(config, "GENRE_GRAPH_INFERENCE_ENABLED", True)
        )

        # Group by (artist, album) AND by artist alone (for tier-2 fallback)
        albums: dict[tuple, list[dict]] = defaultdict(list)
        by_artist: dict[str, list[dict]] = defaultdict(list)
        for t in tracks:
            aa = (t.get("album_artist") or t.get("artist") or "").strip().lower()
            al = (t.get("album") or "").strip().lower()
            if aa:
                by_artist[aa].append(t)
            if aa and al:
                albums[(aa, al)].append(t)

        # Tier 2 prep: compute each artist's modal genre across all albums
        # by that artist, ignoring broad/blank values.  Used only as fallback.
        artist_modal: dict[str, str] = {}
        artist_track_count: dict[str, int] = {}
        if artist_fallback:
            for aa, group in by_artist.items():
                artist_track_count[aa] = len(group)
                specific = [
                    g for g in (t.get("genre", "") for t in group)
                    if g and g.strip().lower() not in self._BROAD_GENRES
                ]
                if specific:
                    artist_modal[aa] = Counter(specific).most_common(1)[0][0]

        # MK17 Phase B (M3) — build the genre graph once, used for sparse-
        # artist tier-3 inference below.  Cheap to build (a few thousand
        # nodes max for a 2000-track library) and only happens once per run.
        graph = None
        if graph_enabled:
            try:
                import genre_graph as _gg
                # If we have track_map on self, pass it for playlist-edges
                track_map = getattr(self, "_track_map_for_graph", None)
                graph = _gg.build_graph(track_map=track_map, tracks=tracks)
            except Exception as e:
                log.debug(f"  [GenreConsolidate] graph build failed: {e}")
                graph = None

        # MK17 Patch 8 (post-LOG_17 fix) — diversity guard.  LOG_17 showed
        # 776 of 805 sparse rescues collapsing to "Afrobeats" because the
        # 2271-track Liked Songs playlist created a near-complete graph
        # that drowned out the targeted feature-edge signal.  We do a
        # PRE-SCAN here to count what genre each rescue WOULD propose,
        # and if the same genre wins more than diversity_guard_ratio of
        # the time, we suppress that genre's rescues entirely (the rest
        # of the rescues still apply).
        diversity_ratio = getattr(
            config, "GENRE_GRAPH_DIVERSITY_GUARD_RATIO", None
        )
        # MK8 Fix (PR-GenreGraph): tightened from 0.5 → 0.35.
        # Combined with the playlist purity filter in genre_graph.build_graph(),
        # this means a genre needs to win <35% (was 50%) of sparse-artist rescues
        # to survive the diversity guard.  The purity filter reduces Afrobeats
        # proposals at source; this guard catches any that still slip through.
        diversity_ratio = float(diversity_ratio) if diversity_ratio is not None else 0.35
        diversity_min_sample = getattr(
            config, "GENRE_GRAPH_DIVERSITY_GUARD_MIN_SAMPLE", None
        )
        diversity_min_sample = int(diversity_min_sample) if diversity_min_sample is not None else 10
        suppressed_genres: set = set()  # genres the guard refuses to apply
        if graph is not None:
            rescue_proposals: Counter = Counter()
            for (aa, al), group in albums.items():
                # Repeat the tier-1/2 logic just enough to know whether
                # we WOULD rescue this album.  This duplicates work but
                # keeps the change local and easy to revert.
                specific = [
                    g for g in (t.get("genre", "") for t in group)
                    if g and g.strip().lower() not in self._BROAD_GENRES
                ]
                would_pre_chose: str | None = None
                pre_tier = ""
                if len(specific) >= min_votes:
                    would_pre_chose = Counter(specific).most_common(1)[0][0]
                    pre_tier = "album"
                elif len(group) >= 2 and len(specific) >= 1:
                    artist_choice = artist_modal.get(aa)
                    if artist_choice and specific[0].lower() == artist_choice.lower():
                        would_pre_chose = specific[0]
                        pre_tier = "album-confirmed"
                    else:
                        would_pre_chose = artist_choice or specific[0]
                        pre_tier = "artist" if artist_choice else "album-weak"
                elif artist_fallback and aa in artist_modal:
                    would_pre_chose = artist_modal[aa]
                    pre_tier = "artist"

                if (would_pre_chose
                        and pre_tier in ("artist", "album-weak")
                        and artist_track_count.get(aa, 0) < artist_min_tracks):
                    primary_artist = (
                        group[0].get("album_artist") or group[0].get("artist") or aa
                    )
                    proposed = _gg.infer_genre_for_artist(
                        graph, primary_artist,
                        min_neighbor_tracks=artist_min_tracks,
                    )
                    if proposed and proposed.lower() != would_pre_chose.lower():
                        rescue_proposals[proposed] += 1

            total_proposed = sum(rescue_proposals.values())
            if total_proposed >= diversity_min_sample:
                for genre, count in rescue_proposals.items():
                    fraction = count / total_proposed
                    if fraction > diversity_ratio:
                        suppressed_genres.add(genre.lower())
                        log.warning(
                            f"  [GenreGraph/DiversityGuard] '{genre}' would "
                            f"win {count}/{total_proposed} ({fraction:.0%}) "
                            f"of sparse-artist rescues — that's too uniform "
                            f"to be real signal.  Suppressing '{genre}' "
                            f"rescues for this run.  Likely cause: a "
                            f"high-cardinality playlist created spurious "
                            f"edges; see GENRE_GRAPH_MAX_PLAYLIST_SIZE."
                        )

        # Collect tracks whose genre was rewritten, for audit-state invalidation
        rewritten: list[dict] = []
        unified = 0
        backfilled = 0
        artist_filled = 0
        graph_filled = 0  # MK17 M3 — count graph-rescued tracks
        graph_suppressed = 0  # MK17 P8 — count suppressed-by-diversity-guard

        for (aa, al), group in albums.items():
            # Tier 1: album-level mode
            specific = [
                g for g in (t.get("genre", "") for t in group)
                if g and g.strip().lower() not in self._BROAD_GENRES
            ]
            chosen: str | None = None
            tier = ""
            if len(specific) >= min_votes:
                chosen = Counter(specific).most_common(1)[0][0]
                tier = "album"
            elif len(group) >= 2 and len(specific) >= 1:
                # Album has weak signal — one specific track.  Trust it
                # only if it agrees with the artist mode, else use artist.
                album_one = specific[0]
                artist_choice = artist_modal.get(aa)
                if artist_choice and artist_choice.lower() == album_one.lower():
                    chosen = album_one
                    tier = "album-confirmed"
                else:
                    chosen = artist_choice or album_one
                    tier = "artist" if artist_choice else "album-weak"
            elif artist_fallback and aa in artist_modal:
                # Tier 2: pure artist fallback — album had zero specific genres.
                chosen = artist_modal[aa]
                tier = "artist"

            # MK17 Phase B (M2/M3) — sparse-artist guard.  If we landed
            # on the artist-modal but that modal is itself based on
            # < artist_min_tracks data points, escalate to the graph.
            # This is the Alfa Kat fix: 1 track tagged "Abstract Hip-Hop"
            # produces artist_modal = "Abstract Hip-Hop", but the graph
            # (with neighbors Toss, Sayfar, Kabza de Small at weight 3
            # each) propagates "Amapiano" instead.
            if (chosen
                    and tier in ("artist", "album-weak")
                    and graph is not None
                    and artist_track_count.get(aa, 0) < artist_min_tracks):
                # Get the album's primary artist (case-preserved) for
                # nicer logging
                primary_artist = (
                    group[0].get("album_artist") or group[0].get("artist") or aa
                )
                inferred = _gg.infer_genre_for_artist(
                    graph, primary_artist,
                    min_neighbor_tracks=artist_min_tracks,
                )
                if inferred and inferred.lower() != chosen.lower():
                    # MK13 FIX-C: Specificity guard — only apply graph inference
                    # when the current modal genre is BROAD (Hip-Hop, Soul, R&B,
                    # Electronic, etc.) AND the inferred genre is not "Unknown".
                    # Prevents valid specific genres from being overridden by
                    # graph noise from oversized playlists:
                    #   D'Angelo: Soul → Afrobeats  (WRONG — Soul is correct)
                    #   Bongo Flava artist → Afrobeats (WRONG — Bongo Flava more specific)
                    #   Jazz pianist → Bossa Nova    (WRONG — Jazz is correct)
                    #   Dancehall → Afrobeats        (WRONG — Dancehall is specific)
                    # The fix: check if chosen is in the broad genre set.
                    # Specific subgenres are NEVER overridden by graph inference.
                    _broad = genre_client._broad_genres()
                    _chosen_broad = chosen.lower() in _broad or chosen.lower() in {
                        "hip-hop", "hip hop", "r&b", "soul", "jazz", "electronic",
                        "funk", "pop", "rock", "reggae", "world", "gospel",
                    }
                    _inferred_useful = (
                        inferred.lower() not in {"unknown", ""}
                        and inferred.lower() not in {"pop", "rock"}  # don't degrade
                    )
                    if not _chosen_broad:
                        log.debug(
                            f"  [GenreGraph/Sparse] '{primary_artist}': "
                            f"keeping '{chosen}' (already specific) over "
                            f"graph proposal '{inferred}'"
                        )
                    elif not _inferred_useful:
                        log.debug(
                            f"  [GenreGraph/Sparse] '{primary_artist}': "
                            f"graph proposed '{inferred}' — not useful, keeping '{chosen}'"
                        )
                    elif inferred.lower() in suppressed_genres:
                        # MK17 Patch 8: diversity guard vetoed this rescue
                        graph_suppressed += 1
                        log.debug(
                            f"  [GenreGraph/Sparse] '{primary_artist}' would "
                            f"have rescued to '{inferred}' but diversity "
                            f"guard is suppressing that genre this run."
                        )
                    else:
                        log.info(
                            f"  [GenreGraph/Sparse] '{primary_artist}' has only "
                            f"{artist_track_count.get(aa, 0)} track(s); modal "
                            f"'{chosen}' (broad) — graph proposes "
                            f"'{inferred}'.  Using graph result."
                        )
                        chosen = inferred
                        tier = "graph"

            if not chosen:
                continue  # nothing to apply

            for t in group:
                current = (t.get("genre") or "").strip()
                if current == chosen:
                    continue
                if current.lower() in self._BROAD_GENRES:
                    if dry_run:
                        preview.append({
                            "artist": t.get("artist") or aa, "album": t.get("album") or al,
                            "title": t.get("title") or "", "genre_from": current,
                            "genre_to": chosen, "tier": tier,
                        })
                    else:
                        t["genre"] = chosen
                        rewritten.append(t)
                    if current:
                        unified += 1
                    else:
                        backfilled += 1
                # Specific-but-different genres on the same album — coerce
                # to the canonical chosen genre.
                elif current.lower() != chosen.lower():
                    if dry_run:
                        preview.append({
                            "artist": t.get("artist") or aa, "album": t.get("album") or al,
                            "title": t.get("title") or "", "genre_from": current,
                            "genre_to": chosen, "tier": tier,
                        })
                    else:
                        t["genre"] = chosen
                        rewritten.append(t)
                    unified += 1

            if tier == "artist":
                artist_filled += len(group)
            elif tier == "graph":
                graph_filled += len(group)

        if unified or backfilled or artist_filled or graph_filled or graph_suppressed:
            log.info(
                f"  [AlbumGenre] Consolidated: "
                f"{unified} aligned to album mode, "
                f"{backfilled} blanks backfilled, "
                f"{artist_filled} tracks filled from artist-level mode, "
                f"{graph_filled} tracks rescued via graph inference, "
                f"{graph_suppressed} graph rescues suppressed by diversity guard"
            )

        # ── Patch 8: invalidate audit state for every rewritten track ──────
        # The audit cache is keyed by "storage/Music/Artist - Album/filename"
        # which we don't know at this stage (the file may not even be on
        # device yet).  So we invalidate by artist+album+title match, which
        # is what the auditor's MetaKey check uses internally.
        if rewritten:
            self._invalidate_audit_state_for_tracks(rewritten)

        return preview

    def _invalidate_audit_state_for_tracks(self, tracks: list[dict]) -> None:
        """
        Remove audit-state entries for the given tracks so the next audit
        pass treats them as fresh (not cache-hit) and recomputes tags.

        Matches by comparing the cached metakey's (artist, album, title)
        fingerprint against the rewritten track.  This is coarse — it may
        invalidate more entries than strictly needed if two tracks share
        a title across albums — but the cost is a re-tag pass, not data
        loss, and the audit's MetaKey check will short-circuit entries
        that are genuinely unchanged after the rewrite is applied.
        """
        try:
            from audit import _load_audit_state, _save_audit_state
        except Exception as e:
            log.debug(f"  [AlbumGenre] Could not load audit state for invalidation: {e}")
            return

        try:
            state = _load_audit_state()
        except Exception as e:
            log.debug(f"  [AlbumGenre] Audit state load failed: {e}")
            return

        if not isinstance(state, dict):
            return

        # Build a fingerprint set of (artist_lower, album_lower, title_lower)
        # for every track whose genre we rewrote.
        targets: set[tuple[str, str, str]] = set()
        for t in tracks:
            artist = (t.get("artist") or "").strip().lower()
            album  = (t.get("album") or "").strip().lower()
            title  = (t.get("title") or "").strip().lower()
            if artist and album and title:
                targets.add((artist, album, title))

        if not targets:
            return

        invalidated = 0
        # Iterate over a snapshot of keys — we're mutating the dict
        for key in list(state.keys()):
            if key.startswith("__"):  # skip sidecar caches like __tidal_dl_fails__
                continue
            entry = state.get(key)
            if not isinstance(entry, dict):
                continue
            # Key format: "storage/Music/Artist - Album/NN Title.mp3" — we
            # do a coarse substring check for each target fingerprint.
            key_lower = key.lower()
            for artist, album, title in targets:
                if artist in key_lower and album in key_lower and title in key_lower:
                    state.pop(key, None)
                    invalidated += 1
                    break

        if invalidated:
            try:
                _save_audit_state(state)
                log.info(
                    f"  [AlbumGenre] Invalidated {invalidated} audit state "
                    f"entries so Stage 8 will re-tag consolidated tracks"
                )
            except Exception as e:
                log.warning(f"  [AlbumGenre] Audit state save failed: {e}")

    # -----------------------------------------------------------------------
    # Stage 3: Scan (local + all device storage roots)
    # -----------------------------------------------------------------------

    def scan_storage(self):
        log.info("Scanning local folders ...")
        corrupt: list[str] = []

        for folder in config.LOCAL_FOLDERS:
            if not folder.exists():
                continue
            for file in folder.rglob("*"):
                if file.suffix.lower() not in (".mp3", ".flac"):
                    continue
                if not _is_valid_audio(file):
                    corrupt.append(file.name)
                    continue
                try:
                    meta   = self._read_local_tags(file)
                    t_norm = normalize_key(meta.get("title",  file.stem))
                    a_norm = normalize_key(meta.get("artist", ""))
                    entry  = {
                        "local_path":     file,
                        "device_parts":   None,
                        "device_storage": None,
                        "artist_norm":    a_norm,
                        "title_norm":     t_norm,
                    }
                    self.library[f"{a_norm}|{t_norm}"] = entry
                    self.library.setdefault(t_norm, entry)
                except Exception as e:
                    corrupt.append(f"{file.name} ({e})")

        if corrupt:
            log.warning(
                f"Scan skipped {len(corrupt)} unreadable local file(s). "
                f"First few: {', '.join(corrupt[:5])}"
                + (f" ... and {len(corrupt)-5} more" if len(corrupt) > 5 else "")
            )

        log.info(
            "Scanning Walkman via MTP across all storage roots "
            "(may take 60-180 s for large libraries). "
            "If the scan returns 0 files, eject and reconnect the Walkman."
        )
        device_files = mtp.scan_device_audio(config.DEVICE_NAME, config.STORAGE_ROOTS)

        for ef in device_files:
            filename = ef["filename"]
            ext      = ef.get("ext", pathlib.Path(filename).suffix.lower())  # real ext from item.Type
            parts    = ef["parts"]
            storage  = ef["storage"]
            # item.Name on Walkman has no extension — stem IS the full Name
            stem     = filename if not pathlib.Path(filename).suffix else pathlib.Path(filename).stem
            t_norm   = normalize_key(stem)
            a_norm   = normalize_key(parts[-1]) if parts else ""
            # Reconstruct the full filename with its real extension for device_parts
            full_filename = filename if pathlib.Path(filename).suffix else filename + ext
            dp       = parts + [full_filename]
            primary  = f"{a_norm}|{t_norm}"
            if primary in self.library:
                self.library[primary]["device_parts"]   = dp
                self.library[primary]["device_storage"] = storage
            else:
                entry = {
                    "local_path":     None,
                    "device_parts":   dp,
                    "device_storage": storage,
                    "artist_norm":    a_norm,
                    "title_norm":     t_norm,
                }
                self.library[primary] = entry
                self.library.setdefault(t_norm, entry)

        log.info(f"Library: {len(self.library)} entries indexed")

    def _read_local_tags(self, path: pathlib.Path) -> dict:
        if path.suffix.lower() == ".flac":
            a = FLAC(path)
        else:
            a = MP3(path, ID3=EasyID3)
        return {k: a.get(k, [""])[0] for k in
                ["title", "artist", "album", "albumartist", "date", "genre", "tracknumber"]}

    # -----------------------------------------------------------------------
    # Stage 4: Match  (two-pass fuzzy)
    # -----------------------------------------------------------------------

    def find_track(self, title: str, artist: str) -> dict | None:
        t_n = normalize_key(title)
        a_n = normalize_key(artist)
        # Pass 1: exact composite key
        entry = self.library.get(f"{a_n}|{t_n}")
        if entry:
            return entry
        # Pass 2: title-only with artist word-overlap guard
        entry = self.library.get(t_n)
        if entry:
            shared = set(a_n.split()) & set(entry["artist_norm"].split())
            if not entry["artist_norm"] or shared:
                return entry
        return None

    # -----------------------------------------------------------------------
    # Stage 5: Tag
    # -----------------------------------------------------------------------

    def tag_local_file(self, path: pathlib.Path, track: dict):
        """Writes enriched metadata to path (must be a writable local temp copy)."""
        display_title  = format_title_with_features(track)
        primary_artist = track["artist"]
        if path.suffix.lower() == ".flac":
            self._tag_flac(path, track, display_title, primary_artist)
        else:
            self._tag_mp3(path, track, display_title, primary_artist)

    def _diff_write(self, audio, field: str, value) -> bool:
        # MK9 ISSUE-3 FIX: EasyID3 requires str values (list-of-str internally).
        # Spotify API returns track_number/disc_number as int → mutagen raises
        # "X needs to be str for key tracknumber" / "Invalid MultiSpec data: N".
        # Coerce to str here as the central enforcement point so all 22+ call
        # sites are covered without touching each individually.
        if value is None:
            return False
        str_val = str(value)
        if str_val and audio.get(field, [""])[0] != str_val:
            audio[field] = str_val
            return True
        return False

    def _tag_mp3(self, path, track, display_title, primary_artist):
        try:
            audio = MP3(path, ID3=EasyID3)
        except ID3NoHeaderError:
            MP3(path).add_tags()
            audio = MP3(path, ID3=EasyID3)

        field_map = {
            "title":       display_title                  if config.WRITE_TAGS["title"]        else None,
            "artist":      primary_artist                 if config.WRITE_TAGS["artist"]       else None,
            "album":       track["album"]                 if config.WRITE_TAGS["album"]        else None,
            "albumartist": track["album_artist"]          if config.WRITE_TAGS["album_artist"] else None,
            "date":        track.get("release_year", "")  if config.WRITE_TAGS["release_year"] else None,
            "tracknumber": str(track["track_number"]) if (config.WRITE_TAGS["track_number"] and track["track_number"] not in (None, "")) else None,
            "discnumber":  str(track.get("disc_number", "1") or "1") if config.WRITE_TAGS["disc_number"] else None,
            "genre":       track.get("genre", "Unknown")  if config.WRITE_TAGS["genre"]        else None,
        }
        changed = any(self._diff_write(audio, f, v) for f, v in field_map.items() if v)
        if changed:
            # MK17 Patch 1: ensure path is writable before save.  The local
            # library files are usually writable, but if the user has
            # mirrored their library to a synced cloud folder (Dropbox /
            # OneDrive) parts of it can become RO during sync.
            _idh.ensure_writable(path)
            # MK37/S4: lock-aware tag write (board P1-1).
            try:
                from mk37_wiring import tag_file_with_coherence
                with tag_file_with_coherence(path):
                    try:
                        _idh.save_easy_mp3(audio)
                    except (PermissionError, OSError) as e:
                        time.sleep(0.2)
                        try:
                            _idh.ensure_writable(path)
                            _idh.save_easy_mp3(audio)
                        except (PermissionError, OSError):
                            log.warning(
                                f"  [LocalFix] Could not save MP3 tags for {path.name!r}: "
                                f"{e}.  File may be open in another app or read-only."
                            )
            except ImportError:
                try:
                    _idh.save_easy_mp3(audio)
                except (PermissionError, OSError) as e:
                    time.sleep(0.2)
                    try:
                        _idh.ensure_writable(path)
                        _idh.save_easy_mp3(audio)
                    except (PermissionError, OSError):
                        log.warning(
                            f"  [LocalFix] Could not save MP3 tags for {path.name!r}: "
                            f"{e}.  File may be open in another app or read-only."
                        )

        if track.get("is_liked"):
            id3 = ID3(path)
            id3["POPM:Spotify Liked"] = POPM(
                email="Spotify Liked", rating=255, count=0
            )
            _idh.ensure_writable(path)
            # MK37/S4: lock-aware tag write
            try:
                from mk37_wiring import tag_file_with_coherence
                with tag_file_with_coherence(path):
                    try:
                        _idh.save_v23_id3(id3)
                    except (PermissionError, OSError) as e:
                        log.debug(f"  [LocalFix] POPM save skipped for {path.name!r}: {e}")
            except ImportError:
                try:
                    _idh.save_v23_id3(id3)
                except (PermissionError, OSError) as e:
                    log.debug(f"  [LocalFix] POPM save skipped for {path.name!r}: {e}")

    def _tag_flac(self, path, track, display_title, primary_artist):
        audio = FLAC(path)
        field_map = {
            "title":       display_title                  if config.WRITE_TAGS["title"]        else None,
            "artist":      primary_artist                 if config.WRITE_TAGS["artist"]       else None,
            "album":       track["album"]                 if config.WRITE_TAGS["album"]        else None,
            "albumartist": track["album_artist"]          if config.WRITE_TAGS["album_artist"] else None,
            "date":        track.get("release_year", "")  if config.WRITE_TAGS["release_year"] else None,
            "tracknumber": str(track["track_number"]) if (config.WRITE_TAGS["track_number"] and track["track_number"] not in (None, "")) else None,
            "discnumber":  str(track.get("disc_number", "1") or "1") if config.WRITE_TAGS["disc_number"] else None,
            "genre":       track.get("genre", "Unknown")  if config.WRITE_TAGS["genre"]        else None,
        }
        changed = any(self._diff_write(audio, f, v) for f, v in field_map.items() if v)
        if track.get("is_liked"):
            if audio.get("rating", [""])[0] != "100":
                audio["rating"] = "100"
                changed = True
        if changed:
            _idh.ensure_writable(path)
            # MK37/S4: lock-aware FLAC tag write
            try:
                from mk37_wiring import tag_file_with_coherence
                with tag_file_with_coherence(path):
                    try:
                        audio.save()
                    except (PermissionError, OSError) as e:
                        time.sleep(0.2)
                        try:
                            _idh.ensure_writable(path)
                            audio.save()
                        except (PermissionError, OSError):
                            log.warning(
                                f"  [LocalFix] Could not save FLAC tags for {path.name!r}: "
                                f"{e}.  File may be open in another app or read-only."
                            )
            except ImportError:
                try:
                    audio.save()
                except (PermissionError, OSError) as e:
                    time.sleep(0.2)
                    try:
                        _idh.ensure_writable(path)
                        audio.save()
                    except (PermissionError, OSError):
                        log.warning(
                            f"  [LocalFix] Could not save FLAC tags for {path.name!r}: "
                            f"{e}.  File may be open in another app or read-only."
                        )

    # -----------------------------------------------------------------------
    # Stage 6: MTP transfer (serial) with storage fallback
    # -----------------------------------------------------------------------

    def _device_folder_parts(self, track: dict) -> list[str]:
        """
        Returns the folder path on the Walkman for a track, relative to the
        storage root.  Flat two-level structure:
            Music / Artist - Album Name /
        e.g.  Music / Aminé - Limbo /
        """
        album_artist = sanitize_name(track.get("album_artist") or track.get("artist", "Unknown"), 40)
        album        = sanitize_name(track.get("album", "Unknown"), 60)
        folder_name  = f"{album_artist} - {album}"
        return [config.MUSIC_ROOT, sanitize_name(folder_name, 100)]

    def _update_tags_on_device(self, entry: dict, track: dict):
        """MTP read → retag temp copy → MTP write back.
        Skipped if metakey shows tags are already current."""
        parts    = entry["device_parts"]
        storage  = entry.get("device_storage") or config.WRITE_STORAGE
        filename = parts[-1]
        folders  = parts[:-1]
        stem     = pathlib.Path(filename).stem

        # Metakey fast-path: skip MTP round-trip if tags are already correct
        if _mk:
            device_key   = f"{storage}/Music/{'/'.join(folders)}/{filename}"
            exp_metakey  = _mk.compute_metakey(
                stem,
                genre        = track.get("genre", ""),
                year         = track.get("release_year", "") or track.get("spotify_year", ""),
                track_number = str(track.get("track_number", "")),
                disc_number  = str(track.get("disc_number", "1")),
            )
            if _mk.is_current(device_key, exp_metakey):
                log.debug(f"  [Tag Skip] {filename} — metakey current")
                self._inc_stat("skipped")
                return

        tmp_dir  = pathlib.Path(tempfile.mkdtemp())
        tmp_file = tmp_dir / filename
        try:
            found = mtp.read_file_from_device(
                src_parts=folders,
                filename=filename,
                dest_local=tmp_file,
                device_name=config.DEVICE_NAME,
                storage_name=storage,
            )
            if not found:
                log.warning(f"  [Tag Skip] '{filename}' not readable from device")
                return
            self.tag_local_file(tmp_file, track)
            # MK37/S2: route through quorum-verified transfer (capability-gated,
            # post-transfer 3-strategy verification, lifecycle events).
            # Falls back to direct call if mk37_wiring unavailable.
            try:
                from mk37_wiring import transfer_via_legacy_with_quorum
                _device_relpath = "/".join(folders + [tmp_file.name])
                _result = transfer_via_legacy_with_quorum(
                    mtp.copy_file_to_device,
                    src=tmp_file,
                    dest_parts=folders,
                    device_name=config.DEVICE_NAME,
                    storage_name=storage,
                    device_index=getattr(self, "_device_index", None),
                    source_local_path=str(tmp_file),
                    device_relpath=_device_relpath,
                )
                if not _result.get("completed"):
                    log.warning(
                        f"  [MK37/S2] re-tag transfer not completed: "
                        f"{_result.get('reason')} tier={_result.get('effective_tier')}"
                    )
            except ImportError:
                mtp.copy_file_to_device(
                    src=tmp_file,
                    dest_parts=folders,
                    device_name=config.DEVICE_NAME,
                    storage_name=storage,
                    # REV 14.9 (C2) — reuse the session-scoped device index
                    device_index=getattr(self, "_device_index", None),
                )
            self._inc_stat("updated")
            log.info(f"  [Tags Updated] {track['title']}")
            # Stamp metakey so next run skips this file
            if _mk:
                _mk.stamp(device_key, exp_metakey)
        except Exception as e:
            log.error(f"  [Re-tag Failed] {filename}: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # Stage 7: M3U8 (with cross-storage fix)
    # -----------------------------------------------------------------------

    def _write_m3u8(self, playlist_name: str, entries: list[tuple[str, list[str]]]):
        """
        Write playlist files, one per storage that has tracks.
        entries: list of (storage, device_parts)
        """
        if not entries:
            log.warning(f"  [M3U8] No tracks for '{playlist_name}', skipping")
            return

        # Group by storage
        by_storage: dict[str, list[list[str]]] = {}
        for storage, parts in entries:
            by_storage.setdefault(storage, []).append(parts)

        for storage, parts_list in by_storage.items():
            # Build safe storage name for filename
            safe_storage = sanitize_name(storage.replace(" ", "_"), 40)
            safe_name = sanitize_name(playlist_name) + (f"_{safe_storage}" if safe_storage != "Internal_shared_storage" else "") + ".m3u8"
            lines = ["#EXTM3U"]
            for parts in parts_list:
                # Relative path from Music root
                rel = parts[1:] if parts and parts[0].lower() == config.MUSIC_ROOT.lower() else parts
                lines.append("/".join(rel))
            content = "\n".join(lines) + "\n"
            try:
                mtp.write_text_file_to_device(
                    content=content,
                    dest_parts=[config.MUSIC_ROOT],
                    filename=safe_name,
                    device_name=config.DEVICE_NAME,
                    storage_name=storage,  # write to the same storage as the tracks
                )
                log.info(f"  [M3U8] {safe_name}  ({len(parts_list)} tracks) on {storage}")
            except Exception as e:
                log.error(f"  [M3U8 Error] {safe_name}: {e}")

    # -----------------------------------------------------------------------
    # Helper
    # -----------------------------------------------------------------------

    def _record_missing(self, track: dict, reason: str):
        with self._stats_lock:
            self.missing.append(f"{track['title']} by {track['artist']} ({reason})")
            self.stats["missing"] += 1

    # -----------------------------------------------------------------------
    # Main run
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Rev 14.9 preflight (index + scrub + free-space)
    # -----------------------------------------------------------------------

    def _rev149_preflight(self, track_map):
        """
        Populate the in-memory device index, build a sync plan, scrub
        legacy corruption, and warn about low free space.

        Returns the populated DeviceIndex (or None if disabled/unavailable)
        so downstream code can pass it to transport calls.

        This method is intentionally fail-soft: any step that errors logs
        a warning and continues.  The sync proceeds in all cases.
        """
        device_index = None

        # ── Step 1: Refresh device index (C1) ──────────────────────────
        if getattr(config, "DEVICE_INDEX_ENABLED", True):
            try:
                import device_index as _di_mod
                device_index = _di_mod.DeviceIndex()
                max_depth = int(getattr(config, "SCRUB_MAX_DEPTH", 3))
                ok = device_index.refresh(max_depth=max_depth)
                if not ok:
                    # MK17 Patch 3 — emit ONE clear degraded-mode warning that
                    # explains the operational impact.  The previous wording
                    # ("continuing without in-memory cache") understated the
                    # cost: scrub doesn't run, fast-path _exists is off,
                    # every transfer falls back to live ADB probes.  Each
                    # individual probe is fast but at scale they add up
                    # (~150 ms per file × 4665 files = ~12 minutes wasted
                    # in the LOG_16_1 13h run).
                    log.warning(
                        "  [Preflight] DEVICE INDEX DISABLED for this session — "
                        "the deep-scan + 2× retry + adb-restart + shallow-fallback "
                        "all returned no usable output.  Run will continue with "
                        "live ADB probes (slower but correct).  This warning "
                        "will not repeat per-file."
                    )
                    device_index = None
                else:
                    _di_mod.set_default_index(device_index)
            except Exception as e:
                log.warning(f"  [Preflight] Device index unavailable ({type(e).__name__}: {e})")
                device_index = None

        # ── Step 2: Build sync plan (B2) ───────────────────────────────
        # MK37.2-2: build_sync_plan requires (local_files, device_snapshot)
        # but the preflight stage doesn't have a verified DeviceSnapshot yet
        # (device_index is built but snapshot.assert_complete() hasn't been
        # called). LOG_8_5_26:425 showed:
        #   [Preflight] Sync planner failed (TypeError: generate_plan()
        #   missing 1 required positional argument: 'device_snapshot')
        # Fix: build a best-effort snapshot from device_index if available,
        # else explicitly skip planning with a clear marker. Pre-flight scrub
        # will then no-op cleanly instead of crashing.
        plan = None
        try:
            import sync_planner as _sp
            # Flatten track_map (dict keyed by playlist) to a unique track list
            seen_ids: set[str] = set()
            all_tracks: list[dict] = []
            for tracks in (track_map or {}).values():
                for t in tracks or []:
                    if not isinstance(t, dict):
                        continue
                    tid = t.get("id") or ""
                    if tid and tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    all_tracks.append(t)

            # Try to build a DeviceSnapshot from the available device_index.
            # If we can't, skip planning rather than crashing.
            device_snapshot = None
            if device_index is not None:
                try:
                    from sync_planner import DeviceSnapshot, FileState
                    di_files: list = []
                    # device_index may expose .iter_all() / .all_files() / .files
                    iter_fn = (getattr(device_index, "iter_all", None)
                               or getattr(device_index, "all_files", None))
                    if callable(iter_fn):
                        di_files = list(iter_fn())
                    elif hasattr(device_index, "files"):
                        di_files = list(device_index.files or [])
                    fs_list: list = []
                    for f in di_files:
                        # device_index entries may be dicts, FileState-shaped,
                        # or — in the very common case where device_index.files
                        # is dict[path→size] — bare path STRINGS that fall out
                        # of list(device_index.files). Production LOG_10_5_26
                        # showed this exact crash:
                        #   [Preflight] Sync planner failed
                        #   (AttributeError: 'str' object has no attribute 'path')
                        # because sync_planner.py:105 does `f.path` on each
                        # element. Wrap any bare string into a FileState with
                        # the size recovered from device_index.files (if the
                        # underlying dict is still indexable by that path).
                        if isinstance(f, dict):
                            fs_list.append(FileState(
                                path=str(f.get("path", "")),
                                size_bytes=int(f.get("size_bytes") or f.get("size") or 0),
                                mtime=float(f.get("mtime") or 0.0),
                                content_hash=f.get("content_hash") or f.get("hash") or "",
                            ))
                        elif isinstance(f, str):
                            # Recover size from device_index.files if it's a dict.
                            recovered_size = 0
                            try:
                                files_obj = getattr(device_index, "files", None)
                                if isinstance(files_obj, dict):
                                    raw = files_obj.get(f, 0)
                                    if isinstance(raw, int):
                                        recovered_size = raw
                                    elif isinstance(raw, dict):
                                        recovered_size = int(raw.get("size", 0) or 0)
                            except (TypeError, ValueError, AttributeError):
                                recovered_size = 0
                            fs_list.append(FileState(
                                path=f,
                                size_bytes=recovered_size,
                                mtime=0.0,
                            ))
                        else:
                            # Already a FileState-like (has .path attribute)
                            fs_list.append(f)
                    device_snapshot = DeviceSnapshot(files=fs_list, complete=True)
                except Exception as _e_snap:
                    log.debug(
                        f"  [Preflight] DeviceSnapshot construction skipped "
                        f"({type(_e_snap).__name__}: {_e_snap})"
                    )
                    device_snapshot = None

            if device_snapshot is not None:
                # Convert all_tracks (dicts) to a FileState list for local_files.
                # Pre-flight planner doesn't actually need source files; pass
                # an empty list to get the device-only view.
                plan = _sp.build_sync_plan([], device_snapshot)
            else:
                log.info(
                    "  [Preflight] PLAN_SKIPPED_NO_SNAPSHOT — device snapshot "
                    "unavailable; pre-flight scrub will be skipped (run continues)."
                )
                plan = None
        except Exception as e:
            log.warning(f"  [Preflight] Sync planner failed ({type(e).__name__}: {e})")
            plan = None

        # ── Step 3: Pre-flight scrub (B1) ──────────────────────────────
        if (
            getattr(config, "SCRUB_ENABLED", True)
            and device_index is not None
            and plan is not None
            and getattr(plan, "planned_dirs", [])  # MK39.1 L5.5
        ):
            try:
                import device_scrub as _ds
                _ds.scrub_device(device_index, getattr(plan, "planned_dirs", []))
            except Exception as e:
                log.warning(f"  [Preflight] Scrub failed ({type(e).__name__}: {e})")

        # ── Step 3.5: Internal storage check (MK22 B3.2 CORRECTED) ─────────
        # ROOT CAUSE (LOG_5_5_26): internal storage at 99% full (573MB free).
        # ORIGINAL FIX (WRONG): blocked entire run with _preflight_halt.
        # CORRECTED FIX: when internal is full, redirect soft-deletes to SD card.
        #   The WRITE target is already SD card (config.WRITE_STORAGE = 'SD card').
        #   Dedup .deleted/ moves should go to SD card's Music root, not internal.
        #   Hard block ONLY if BOTH internal AND SD card are critically full.
        #
        # APPROACH A3: ADB query internal free bytes.
        #   < REDIRECT_THRESHOLD: warn + set INTERNAL_REDIRECT_TO_SD = True
        #   < HARD_FLOOR (and SD also low): hard block
        try:
            _redirect_mb  = int(getattr(config, "INTERNAL_STORAGE_REDIRECT_THRESHOLD_MB", 1024))
            _hard_floor_mb = int(getattr(config, "INTERNAL_STORAGE_HARD_FLOOR_MB", 256))
            import subprocess as _sp22
            _df_r = _sp22.run(
                ["adb", "shell", "df", "/storage/emulated"],
                capture_output=True, text=True, timeout=15, check=False
            )
            if _df_r.returncode == 0:
                import re as _re22
                _m2 = _re22.search(r"(\d+)\s+(\d+)\s+(\d+)", _df_r.stdout)
                _avail_mb = int(_m2.group(3)) // 1024 if _m2 else 9999

                if _avail_mb < _hard_floor_mb:
                    # Critical — hard block (internal has < 256MB; writes will always fail)
                    self._preflight_halt = True
                    self._preflight_halt_reason = (
                        f"INTERNAL_STORAGE_FULL_BLOCKED: only {_avail_mb}MB free "
                        f"— below hard floor of {_hard_floor_mb}MB. "
                        f"Free space before running: delete thumbnails and .deleted/ folder."
                    )
                    log.critical(f"[Preflight] {self._preflight_halt_reason}")
                elif _avail_mb < _redirect_mb:
                    # Low but usable — redirect soft-deletes to SD card
                    import config as _cfg_c1
                    _cfg_c1.INTERNAL_REDIRECT_ACTIVE = True
                    log.warning(
                        f"[Preflight] INTERNAL_STORAGE_REDIRECT_TO_SD — "
                        f"internal has only {_avail_mb}MB free (threshold: {_redirect_mb}MB). "
                        f"Dedup soft-deletes (.deleted/) will target SD card instead. "
                        f"Run continues normally — write target already is SD card."
                    )
        except Exception as _e22:
            log.debug(f"[Preflight] Internal storage check failed: {_e22}")

        # ── Step 4: Free-space check (B4 + Rev 14.9.3 F5 floor) ──────────
        # F5 — if the device is below MIN_FREE_SPACE_GB, halt the run.
        # Below the floor, the Android system's MediaStore index can become
        # corrupt and the Walkman app freezes; sync would just make it worse.
        # Set self._preflight_halt so the caller knows to abort cleanly.
        if (
            getattr(config, "FREE_SPACE_CHECK_ENABLED", True)
            and plan is not None
            and plan.estimated_bytes > 0
        ):
            try:
                import free_space_check as _fsc
                fs_result = _fsc.check_free_space(plan.estimated_bytes)
                if fs_result.get("below_floor"):
                    self._preflight_halt = True
                    self._preflight_halt_reason = fs_result.get("message",
                        "Device below MIN_FREE_SPACE_GB safety floor.")
                    log.critical(
                        f"  [Preflight] HALT — device below {getattr(config, 'MIN_FREE_SPACE_GB', 0.5):.1f} "
                        f"GB free-space floor.  Sync will not proceed."
                    )
            except Exception as e:
                log.warning(f"  [Preflight] Free-space check failed ({type(e).__name__}: {e})")

        # ── Step 5: TIDAL auth pre-check (D4) ──────────────────────────
        # In LOG-13 Run 2 the TIDAL session was stale: tidaldl managed 69%
        # success (vs the usual 92%+ on a fresh login), tidaldl_url got
        # 0/15 because URL-mode requires a fresh auth handshake.  This
        # cost the run ~1200 tracks according to the MC simulation.  A
        # 500ms check at startup saves hours of wasted provider burn.
        if getattr(config, "TIDAL_AUTH_PRECHECK_ENABLED", True):
            self._rev149_tidal_auth_check()

        return device_index

    def _rev149_tidal_auth_check(self):
        """
        REV 14.9.1 (D4) — Quick TIDAL session sanity check at startup.
        Loud CRITICAL log line if stale, with exact fix command.  Does NOT
        block the run (the user may know about it already and intend to
        sync from yt-dlp alone), but makes the cost visible up-front.
        """
        try:
            import json as _json
            import pathlib as _pl
            session_file = _pl.Path(
                getattr(config, "BASE_DIR", _pl.Path("."))
            ) / ".tidal_session.json"

            if not session_file.exists():
                log.critical(
                    "[TIDAL/auth] No .tidal_session.json found at "
                    f"{session_file}.  tidaldl will fail for every track.  "
                    "Log in once with:  python -m tidal_dl  (then follow "
                    "the OAuth prompt in your browser).  Without this, "
                    "the session will attempt OAuth but time out headlessly."
                )
                return

            # Check expiry timestamp if present
            try:
                saved = _json.loads(session_file.read_text(encoding="utf-8"))
                expiry = saved.get("expiry_time")
                import time as _time
                if expiry and isinstance(expiry, (int, float)) and expiry < _time.time():
                    log.critical(
                        f"[TIDAL/auth] Session token EXPIRED "
                        f"({int(_time.time() - expiry)}s ago).  "
                        "tidaldl will fall back to OAuth and time out.  "
                        "Re-authenticate with:  python -m tidal_dl  "
                        "(or delete .tidal_session.json and re-run)."
                    )
                    return
            except Exception:
                pass  # malformed session file — let downloader complain later

            # Perform an active check_login() — costs ~500ms but catches
            # the more common case where the token is structurally valid
            # but revoked server-side.
            try:
                import tidalapi
                import time as _time
                _t0 = _time.time()
                cfg = tidalapi.Config()
                sess = tidalapi.Session(cfg)
                sess.load_oauth_session(
                    saved["token_type"],
                    saved["access_token"],
                    saved["refresh_token"],
                    saved.get("expiry_time"),
                )
                ok = sess.check_login()
                elapsed = (_time.time() - _t0) * 1000
                if ok:
                    log.info(f"  [TIDAL/auth] Session OK ({elapsed:.0f}ms check).")
                else:
                    log.critical(
                        f"[TIDAL/auth] Session token present but REJECTED "
                        f"by TIDAL ({elapsed:.0f}ms check).  This is most "
                        "often a revoked token or a server-side session "
                        "timeout.  Re-authenticate with:  "
                        "python -m tidal_dl  (or delete "
                        ".tidal_session.json and re-run)."
                    )
            except ImportError:
                log.warning(
                    "  [TIDAL/auth] tidalapi not installed; skipping pre-check."
                )
            except Exception as e:
                log.warning(
                    f"  [TIDAL/auth] Pre-check raised ({type(e).__name__}: {e}); "
                    "will let the downloader attempt auth later."
                )

        except Exception as e:
            log.warning(f"  [Preflight] TIDAL auth pre-check error: {e}")

    # -----------------------------------------------------------------------
    # Health check helpers (Rev 10)
    # -----------------------------------------------------------------------

    def _check_device_health(self, label: str = "Health check") -> bool:
        """
        Wrapper around mtp.log_health_check().
        Returns True if the device is connected and the write storage is OK.
        Logs a clear error and sets cancelled if the device is gone.

        REV 14.6 — up to 3 reconnect attempts at 15 s intervals, with an explicit
        adb reconnect between each attempt.  The 2026-04-19 log lost the tail of
        a 5 h sync because the post-scan health check had only ONE 15 s retry
        window; the device was re-enumerating after Stage 0 and missed it.
        """
        healthy = mtp.log_health_check(label)
        if healthy:
            return True

        import time
        max_retries = getattr(config, "DEVICE_HEALTH_RETRY_COUNT", 3)
        wait_s      = getattr(config, "DEVICE_HEALTH_RETRY_WAIT_S", 15)

        # Try to import the ADB reconnect helper — fail open if unavailable
        try:
            import adb_transfer as _adb
            _reconnect = getattr(_adb, "reconnect", None)
        except Exception:
            _reconnect = None

        for attempt in range(1, max_retries + 1):
            log.error(
                f"  [{label}] Device health check failed "
                f"(attempt {attempt}/{max_retries}). "
                f"Ensure Walkman is connected via USB (File Transfer / MTP mode) "
                f"and its screen is not locked/sleeping. "
                f"Waiting {wait_s}s..."
            )
            time.sleep(wait_s)

            # Rev 14.6: explicitly nudge adb before the next poll.  On the ZX500
            # post-Stage-0 re-enumeration this single extra call tips the balance.
            if _reconnect is not None:
                try:
                    _reconnect()
                except Exception as _e:
                    log.debug(f"  [{label}] adb_transfer.reconnect() raised: {_e}")

            healthy = mtp.log_health_check(f"{label} retry {attempt}/{max_retries}")
            if healthy:
                log.info(f"  [{label}] Reconnected on attempt {attempt}.")
                return True

        log.error(f"  [{label}] Reconnect failed after {max_retries} attempts — stopping sync.")
        return False

    def run_sync(self):
        """Full pipeline entry point, called by the GUI thread."""
        # REV 14: declare workload — bulk pushes use ADB as default, per
        # config.BULK_BACKEND, with COM as fallback.
        try:
            mtp.set_workload("bulk")
        except Exception:
            pass

        # MK37/S2-T3: issue a single pass-wide capability so all transfers
        # this run are capability-gated. Cleanup is registered via atexit
        # AND called at end of run_sync (belt-and-suspenders).
        try:
            from mk37_wiring import begin_sync_pass, end_sync_pass
            import atexit as _atexit_mk37
            begin_sync_pass(scope="*", ttl_seconds=7200.0)  # 2h max for long syncs
            _atexit_mk37.register(end_sync_pass)
        except ImportError:
            pass
        except Exception as _e_pass:
            log.warning(f"  [MK37/S2-T3] begin_sync_pass failed (continuing): {_e_pass}")

        # Pre-flight health check (Rev 10)
        if not self._check_device_health("Pre-sync"):
            return

        # Stage 1: Spotify fetch
        # fetch_and_build now returns (track_map, liked_track_ids, saved_album_ids).
        # saved_album_ids is used by the album-wise downloader (Stage 0) so all
        # 148 liked albums trigger full-album mode, not just albums whose tracks
        # happen to appear in the Liked Songs playlist.
        sp = sc.get_spotify_client()
        track_map, _liked_ids, saved_album_ids = self._fetch_from_spotify(sp)

        # Stage 2: Genre/year enrichment
        track_map = self._enrich_genres(track_map)

        # ─── REV 14.9 PREFLIGHT ────────────────────────────────────────
        # Before Stage 0 downloads/transfers begin, build an in-memory
        # snapshot of the device's /Music tree, compute the set of album
        # folders we'll need, scrub any blockers, and warn about low
        # free space.  Each step degrades gracefully if something fails:
        # the sync always proceeds; worst case, it runs without the
        # benefits (which is equivalent to the pre-14.9 behaviour).
        # Rev 14.9.3 F5 — preflight may set self._preflight_halt = True
        # if the device is below MIN_FREE_SPACE_GB; we honor it here.
        self._preflight_halt = False
        self._preflight_halt_reason = ""
        self._device_index = self._rev149_preflight(track_map)
        if self._preflight_halt:
            log.critical(
                "Sync HALTED by preflight check.  Reason: "
                f"{self._preflight_halt_reason}"
            )
            self._inc_stat("errors")
            return  # do not proceed to Stage 0
        # ───────────────────────────────────────────────────────────────

        # Stage 0: Download missing tracks
        # (Runs after Stage 2 so we know the full track list)
        if config.DOWNLOAD_MISSING and not self._cancelled:
            # ── MK17 Phase B (M1 + M6) — Stage 0.5: Downloads-folder ingest
            # Scan host-side and device-side Downloads folders for loose
            # audio files; promote files that fuzzy-match a Spotify track
            # in the user's library.  Match-only mode (per Apr 30 user
            # direction): unmatched files are left alone.
            if getattr(config, "INGEST_ENABLED", False) and not self._cancelled:
                log.info("\nStage 0.5: scanning Downloads folder(s) for loose audio files ...")
                try:
                    import downloads_ingest as _ingest
                    threshold = float(getattr(config, "INGEST_FUZZY_THRESHOLD", 0.85))
                    plan = _ingest.ingest_loose_files(
                        track_map=track_map,
                        threshold=threshold,
                    )
                    # Match-only: just log what we found.  Actual file
                    # promotion would require either copying into the
                    # canonical local Music tree (and the user wants
                    # original Downloads files left alone) or pushing
                    # straight to device.  For safety in this first cut
                    # we surface the matches as a report and the next
                    # release can wire in the move-into-canonical-tree
                    # path once the user confirms the matching is correct.
                    h_match = plan["host_matches"]
                    d_match = plan["device_matches"]
                    if h_match:
                        log.info(
                            f"  [Ingest] {len(h_match)} host-side match(es) "
                            f"identified.  Sample: "
                            f"{[str(p.name) for p, _ in h_match[:3]]}"
                        )
                        # Mark these tracks as already-have-locally so the
                        # downloader doesn't waste effort re-fetching them.
                        for _path, t in h_match:
                            t["_ingest_local_path"] = str(_path)
                    if d_match:
                        log.info(
                            f"  [Ingest] {len(d_match)} device-side match(es) "
                            f"identified (already on Walkman in /Download)."
                        )
                        # Mark these so the audit downstream knows to
                        # consider them present.
                        for _path, t in d_match:
                            t["_ingest_device_path"] = _path
                    if not (h_match or d_match):
                        log.info("  [Ingest] No loose audio files matched the library.")
                except Exception as _ie:
                    log.warning(f"  [Ingest] Skipped due to error: {_ie}")

            log.info("\nStage 0: downloading missing tracks ...")
            # MK37.2-1: Stage 0 is the first destructive stage after the
            # 2-3 hour enrichment + ingest scan window. Force a fresh
            # capability so transfers triggered during download have a
            # full TTL window. Without this, LOG_8_5_26 saw token=7d7e8348
            # expire 3,116s before the first transfer fired.
            try:
                from mk37_2_renewal import begin_destructive_stage
                begin_destructive_stage("stage_0_download", ttl_seconds=7200.0,
                                          force_renewal=True)
            except ImportError:
                pass
            except Exception as _e_stage:
                log.warning(f"  [MK37.2-1] stage 0 bind failed (continuing): {_e_stage}")
            import downloader as dl

            dl_stats = dl.run_downloads(
                track_map=track_map,
                cancelled=self._cancel_flag,
                liked_album_ids=saved_album_ids,  # from fetch_and_build — no extra API call needed
                sp_client=sp,
            )
            self._inc_stat("downloaded", dl_stats["downloaded"])
            log.info(
                f"Stage 0 complete: {dl_stats['downloaded']} downloaded, "
                f"{dl_stats['failed']} failed."
            )

            # MK17 Patch 5 — single run-level health summary.  Pre-MK17
            # the log had useful per-event signals (verify failures, com
            # fallbacks, tag failures) but no single final verdict.
            # Operators had to grep the log to know whether the session
            # was clean.  Now we surface the totals once, at INFO if
            # everything passed and at WARNING if anything notable
            # happened.  The verdict lets the caller decide what's
            # acceptable — we don't fail the run automatically because
            # some events (com_fallbacks, unknown_free_events) are
            # informational rather than error.
            try:
                import downloader as _dl
                health = _dl._health_summary()
                # Determine verdict
                self._stage0_health = health
                hard_fail_count = (
                    health["verify_failures"] + health["tag_failures"]
                )
                soft_warn_count = (
                    health["unknown_free_events"]
                    + health["com_fallbacks"]
                    + health["tag_fallbacks"]
                )
                if hard_fail_count > 0:
                    log.warning(
                        f"\n[Health] Stage 0 verdict: ATTENTION REQUIRED — "
                        f"{health['verify_failures']} transfer-verification "
                        f"failure(s), {health['tag_failures']} tag-write "
                        f"failure(s).  Some files may be on the device with "
                        f"stale or corrupted state."
                    )
                elif soft_warn_count > 0:
                    log.warning(
                        f"\n[Health] Stage 0 verdict: COMPLETED WITH WARNINGS — "
                        f"{health['tag_fallbacks']} tag rescues via copy-replace, "
                        f"{health['unknown_free_events']} unknown-free-space "
                        f"events, {health['com_fallbacks']} ADB→COM fallback(s), "
                        f"{health['provider_exhaustions']} tracks where every "
                        f"download provider failed."
                    )
                else:
                    log.info(
                        f"\n[Health] Stage 0 verdict: CLEAN — no tag failures, "
                        f"no verification failures, no transport fallbacks, "
                        f"no unknown-free-space events.  "
                        f"({health['provider_exhaustions']} provider exhaustion(s) "
                        f"for tracks not findable anywhere — typically DMCA / "
                        f"region restrictions, not a bug.)"
                    )
            except Exception as _hs_err:
                log.debug(f"[Health] summary skipped: {_hs_err}")

            # ── MK17 Phase B (M7) — Album-completeness pass ─────────────
            # 2026-09-02: rewritten to call the shared
            # album_completeness.run_completeness_pass() (a/b/c rule) so the
            # legacy and SpiralOrchestrator paths behave identically. Works
            # online (sp) OR offline (Spotify embed scrape) — no longer
            # "online-mode only".
            if (getattr(config, "ALBUM_COMPLETENESS_ENABLED", True)
                    and not self._cancelled):
                log.info("\nStage 0.7: checking for incomplete albums (a/b/c) ...")
                try:
                    import album_completeness as _ac
                    missing = _ac.run_completeness_pass(
                        track_map,
                        saved_album_ids or set(),
                        sp_client=sp,
                        dev_lib=getattr(self, "_device_lib", None),
                    )
                    if missing:
                        # Inject into track_map under a synthetic playlist
                        # name so the downloader picks them up on a second
                        # pass.  Using the same dict reference means
                        # downstream stages (audit, tagging) see them too.
                        track_map.setdefault("__completeness_fill__", []).extend(missing)

                        log.info(
                            f"  [Completeness] Running follow-up download "
                            f"for {len(missing)} missing track(s) ..."
                        )
                        completion_stats = dl.run_downloads(
                            track_map={"__completeness_fill__": missing},
                            cancelled=self._cancel_flag,
                            liked_album_ids=saved_album_ids,
                            sp_client=sp,
                        )
                        self._inc_stat("downloaded", completion_stats["downloaded"])
                        log.info(
                            f"  [Completeness] Follow-up done: "
                            f"{completion_stats['downloaded']} new track(s), "
                            f"{completion_stats['failed']} unfindable."
                        )
                    else:
                        log.info("  [Completeness] All saved albums are complete.")
                except Exception as _ace:
                    log.warning(f"  [Completeness] Skipped due to error: {_ace}")

        # Stage 2.5: Pre-sync local tag fix
        # Fix local source files BEFORE scanning so they arrive on the Walkman
        # already correctly tagged — avoids expensive MTP pull/retag/push cycles.
        if not self._cancelled:
            log.info("\nPre-sync: fixing local file tags ...")
            from audit import LibraryAuditor
            pre_aud = LibraryAuditor()
            pre_aud._audit_local_files(pre_aud._enumerate_local())
            log.info(
                f"Pre-sync local fix complete: "
                f"{pre_aud.stats['changed']} changed, "
                f"{pre_aud.stats['skipped']} already correct."
            )

        # Stage 3: Scan
        self.scan_storage()

        # Post-scan health check (Rev 10) — MTP scan is the most likely
        # point at which the device enters sleep mode due to the long scan time.
        if not self._check_device_health("Post-scan"):
            return

        _HEALTH_CHECK_EVERY_N_TRACKS = 50   # device health check every N copied tracks
        _tracks_since_health_check   = [0]  # list so inner closures can mutate it

        # MK8 Fix — cross-playlist global transfer dedup.
        # LOG_17 showed 153+ exact duplicate transfers and 244 track-number
        # collision instances because `seen_dedup` was reset for every
        # playlist iteration below.  The same song appearing in both
        # "Liked Songs" and a saved album playlist was thus enqueued and
        # transferred once per playlist.  This global set, combined with
        # `_global_seen_lock`, gates the actual file-copy decision across
        # ALL playlists.  The per-playlist `seen_dedup` is kept for
        # M3U8-entry dedup (each playlist file should still list its tracks).
        _global_transfer_seen: set[str] = set()
        _global_seen_lock = threading.Lock()

        # Stages 4-7: producer-consumer pipeline per playlist
        for pl_name, tracks in track_map.items():
            if self._cancelled:
                log.info("Sync cancelled.")
                break

            log.info(
                f"\nProcessing: {pl_name}  "
                f"({len(tracks)} tracks | {TAG_WORKERS} tag workers + 1 transfer thread)"
            )
            m3u8_entries: list[tuple[str, list[str]]] = []  # (storage, device_parts)
            seen_dedup:   set[str]        = set()
            seen_lock     = threading.Lock()
            SENTINEL      = object()
            tagged_queue: queue.Queue = queue.Queue()  # unbounded — tag everything upfront

            def _tag_worker(track: dict) -> tuple | None:
                if self._cancelled:
                    return None

                dedup = normalize_key(f"{track['artist']}|{track['title']}")
                with seen_lock:
                    if dedup in seen_dedup:
                        self._inc_stat("skipped")
                        return None
                    seen_dedup.add(dedup)

                with self._lib_lock:
                    entry = self.find_track(track["title"], track["artist"])

                if not entry:
                    log.warning(
                        f"  [Missing] {track['title']} by {track['artist']} "
                        f"(Not found — download required)"
                    )
                    self._record_missing(track, "Not found — download required")
                    return None

                if entry["device_parts"]:
                    return ("update", entry, track)

                # MK8 Fix — global cross-playlist transfer dedup guard.
                # If another playlist already queued or copied this exact
                # track this session, skip the copy and return an update
                # so the M3U8 for THIS playlist still gets an entry.
                with _global_seen_lock:
                    if dedup in _global_transfer_seen:
                        log.debug(
                            f"  [GlobalDedup] '{track['title']}' already transferred "
                            f"this session (seen in a previous playlist) — skipping copy."
                        )
                        return ("update", entry, track)
                    _global_transfer_seen.add(dedup)

                # New file — determine storage BEFORE tagging
                local_path = entry["local_path"]
                try:
                    # Rev 14.4: pass the local filename for verbose router logs
                    target_storage = mtp._pick_storage_for_file(
                        local_path.stat().st_size,
                        filename=local_path.name,
                    )
                except StorageFullError as e:
                    log.error(f"  [Storage Full] {e}")
                    raise   # propagates to consumer which stops the run

                tmp_dir  = pathlib.Path(tempfile.mkdtemp())
                # Canonical filename: Artist - Album - CDx - NN Title (feat. X).ext
                album_artist = sanitize_name(track.get("album_artist") or track.get("artist", "Unknown"), 40)
                album        = sanitize_name(track.get("album", "Unknown"), 60)
                disc         = str(track.get("disc_number", "1"))
                num          = str(track.get("track_number", "1")).zfill(2)
                title        = sanitize_name(track.get("title", "Unknown"), 60)
                ext          = local_path.suffix.lower()
                # feat. artists — Rev 14.9.11 double-feat fix:
                # Only append feat_part when the Spotify title doesn't already
                # contain a feat. marker.  Previously this check was absent,
                # so titles like "Like Me (feat. iann dior)" (where Spotify
                # embeds the feat. in the track name) got a second
                # "(feat. iann dior)" appended → filenames like
                # "Like Me (feat. iann dior) (feat. iann dior).mp3".
                # format_title_with_features() guards the ID3 tag correctly;
                # we mirror that same guard here for the canonical filename.
                _raw_title_has_feat = bool(
                    re.search(r"\bfeat[.\s]|\bft[.\s]", track.get("title", ""), re.IGNORECASE)
                )
                artists = track.get("artists") or [track.get("artist", "")]
                if len(artists) > 1 and config.FEATURE_ARTISTS_IN_TITLE and not _raw_title_has_feat:
                    feat = ", ".join(a for a in artists[1:] if a)
                    feat_part = f" (feat. {feat})" if feat else ""
                else:
                    feat_part = ""
                cd_part  = f"CD{disc} - " if disc not in ("", "1") else ""
                stem     = f"{album_artist} - {album} - {cd_part}{num} {title}{feat_part}"
                # ASCII-safe for Shell.Application CopyHere
                import unicodedata as _ud
                safe_stem = _ud.normalize("NFKD", stem).encode("ascii","ignore").decode("ascii")
                safe_stem = re.sub(r'[\\/:*?"<>|]', "_", safe_stem).strip(". ")[:120]
                canonical = safe_stem + ext

                # 2026-07-22 — deferred recheck for files left device_status=
                # 'pending' by mtp_transfer_com.copy_file_to_device's ambiguous-
                # WPD-cache-staleness path (see task_plan.md). Before doing a
                # full re-copy of a track that already has a prior write in
                # flight, do a cheap isolated recheck first — folder_parts/
                # canonical/target_storage are all deterministic from `track`
                # alone (no persisted schema needed), matching the confirmed
                # scope decision to re-derive rather than add sync_state
                # columns. Skips the (wasteful, and possibly overwrite=False-
                # unsafe) re-copy entirely when the file turns out to already
                # be correct on-device.
                folder_parts = self._device_folder_parts(track)
                _pending_relpath = str(local_path.absolute())
                try:
                    import sync_state as _pending_ss
                    _pending_state = _pending_ss.get_state(_pending_relpath)
                except Exception:
                    _pending_ss, _pending_state = None, None
                if _pending_state and _pending_state.get("device_status") == "pending":
                    if _pending_state.get("failure_count", 0) >= _PENDING_MAX_RECHECKS:
                        try:
                            _pending_ss.transition_to(_pending_relpath, "device_status", "retryable_error")
                        except Exception:
                            pass
                        # fall through — treat as needing a real re-copy below
                    else:
                        try:
                            import mtp_transfer_com as _pending_mtpcom
                            _recheck_ok, _recheck_reason = _pending_mtpcom.verify_com_copy_isolated(
                                config.DEVICE_NAME, target_storage, folder_parts, canonical,
                                expected_size=local_path.stat().st_size,
                            )
                        except Exception as _recheck_err:
                            _recheck_ok, _recheck_reason = False, str(_recheck_err)
                        if _recheck_ok:
                            try:
                                _pending_ss.transition_to(_pending_relpath, "device_status", "synced")
                            except Exception:
                                pass
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                            return ("confirmed_pending", folder_parts, canonical, target_storage)
                        else:
                            try:
                                _pending_ss.mark_failed(_pending_relpath, "device_verify", _recheck_reason or "recheck failed")
                            except Exception:
                                pass
                            # fall through — treat as needing a real re-copy below

                tmp_file  = tmp_dir / canonical
                try:
                    shutil.copy2(local_path, tmp_file)
                    self.tag_local_file(tmp_file, track)
                    # MK8 Fix — embed cover art for local-library transfers.
                    # Previously only downloader.py embedded covers, so tracks
                    # transferred directly from the local library (not downloaded
                    # fresh) arrived on the device with no cover art.  This was
                    # the root cause of only 30/3307 folders having covers on the
                    # ZX500.  We call _embed_cover_for_track from downloader.py
                    # (which is already imported) using the same album_image URL
                    # that Spotify supplies in the track dict.
                    try:
                        from downloader import _embed_cover_for_track as _ect
                        _ect(tmp_file, track)
                    except Exception as _ce:
                        log.debug(f"  [Cover] embed skipped for {track.get('title','?')!r}: {_ce}")
                    return (
                        "copy", tmp_dir, tmp_file, track,
                        folder_parts, target_storage,
                    )
                except Exception as e:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    log.error(f"  [Tag Failed] {track['title']}: {e}")
                    self._record_missing(track, f"Tag error: {e}")
                    return None

            def _producer():
                try:
                    with ThreadPoolExecutor(max_workers=TAG_WORKERS) as pool:
                        futures = [pool.submit(_tag_worker, t) for t in tracks]
                        for future in as_completed(futures):
                            if self._cancelled:
                                break
                            result = future.result()
                            if result is not None:
                                tagged_queue.put(result)
                except StorageFullError:
                    tagged_queue.put("STORAGE_FULL")
                finally:
                    tagged_queue.put(SENTINEL)

            def _consumer():
                while True:
                    item = tagged_queue.get()
                    if item is SENTINEL:
                        break
                    if item == "STORAGE_FULL":
                        self._cancelled = True
                        break
                    if self._cancelled:
                        if isinstance(item, tuple) and item[0] == "copy":
                            shutil.rmtree(item[1], ignore_errors=True)
                        continue

                    try:
                        if item[0] == "update":
                            _, entry, track = item
                            dp = entry["device_parts"]
                            # FIX (dead _update_tags_on_device): call the method here
                            # so existing device files have their tags refreshed when
                            # genre, year, or track metadata has changed in Spotify/Last.fm.
                            # The metakey fast-path inside _update_tags_on_device means
                            # files whose tags are already current skip the MTP round-trip
                            # with zero cost. Without this call the "update" branch only
                            # wrote M3U8 entries and incremented "skipped" — tags were
                            # never updated for already-present tracks.
                            self._update_tags_on_device(entry, track)
                            # Add to m3u8 entries with storage
                            storage = entry.get("device_storage") or config.WRITE_STORAGE
                            m3u8_entries.append((storage, dp))

                        elif item[0] == "confirmed_pending":
                            # 2026-07-22 — _tag_worker's pending-recheck gate already
                            # confirmed this file is correctly on-device (via ADB
                            # cross-check or a settled COM re-verify); no copy needed,
                            # just record the M3U8 entry.
                            _, folder_parts, canonical, target_storage = item
                            self._inc_stat("skipped")
                            m3u8_entries.append((target_storage, folder_parts + [canonical]))

                        elif item[0] == "copy":
                            _, tmp_dir, tmp_file, track, folder_parts, target_storage = item
                            try:
                                # MK37/S2: quorum-verified transfer
                                try:
                                    from mk37_wiring import transfer_via_legacy_with_quorum
                                    _device_relpath = "/".join(folder_parts + [tmp_file.name])
                                    _result = transfer_via_legacy_with_quorum(
                                        mtp.copy_file_to_device,
                                        src=tmp_file,
                                        dest_parts=folder_parts,
                                        device_name=config.DEVICE_NAME,
                                        storage_name=target_storage,
                                        device_index=getattr(self, "_device_index", None),
                                        source_local_path=str(tmp_file),
                                        device_relpath=_device_relpath,
                                    )
                                    if not _result.get("completed"):
                                        log.warning(
                                            f"  [MK37/S2] copy not completed: "
                                            f"{_result.get('reason')} "
                                            f"tier={_result.get('effective_tier')}"
                                        )
                                except ImportError:
                                    mtp.copy_file_to_device(
                                        src=tmp_file,
                                        dest_parts=folder_parts,
                                        device_name=config.DEVICE_NAME,
                                        storage_name=target_storage,
                                        # REV 14.9 (C2)
                                        device_index=getattr(self, "_device_index", None),
                                    )
                                device_parts = folder_parts + [tmp_file.name]
                                self._inc_stat("copied")
                                # MK18 FIX-F11: check hard floor every 10 copies
                                if self.stats.get("copied", 0) % 10 == 0:
                                    try:
                                        import free_space_check as _fsc18
                                        _fl = _fsc18.midrun_floor_check()
                                        if _fl.get("hard_stop"):
                                            log.critical(f"[FreeSpace] HARD STOP: {_fl['message']}")
                                            self._hard_stop_transfers = True

                                        # MK26 C5 (A4): RunAbortPolicy — check after every transfer
                                        # Evidence: 12.7h run, 100% verify-fail, never self-terminated
                                        try:
                                            from run_policy import RunAbortPolicy as _RAP, RunAbortError as _RAE
                                            if not hasattr(self, "_run_abort_policy"):
                                                self._run_abort_policy = _RAP(
                                                    max_failure_pct=20.0,
                                                    max_consecutive_failures=10,
                                                    min_ops_before_pct_check=20,
                                                )
                                            # Record this transfer's outcome
                                            _xfer_ok = not _fl.get("hard_stop")
                                            self._run_abort_policy.record_op(_xfer_ok)
                                            self._run_abort_policy.check()
                                        except ImportError:
                                            pass  # run_policy.py not available — skip
                                        except Exception as _rap_e:
                                            if "RUN_ABORTED_POLICY" in str(_rap_e):
                                                log.critical(
                                                    f"[RunPolicy] RUN_ABORTED_POLICY: {_rap_e} "
                                                    f"— aborting run to prevent overnight waste."
                                                )
                                                self._hard_stop_transfers = True
                                            # else: other exception — ignore, don't crash transfer
                                    except Exception:
                                        pass
                                log.info(f"  [Copied] {track['title']} → {target_storage}/{'/'.join(folder_parts)}/")
                                # Stamp metakey — file just arrived with correct tags
                                if _mk:
                                    _dev_key = f"{target_storage}/Music/{'/'.join(folder_parts)}/{tmp_file.name}"
                                    _exp_mk  = _mk.compute_metakey(
                                        tmp_file.stem,
                                        genre        = track.get("genre", ""),
                                        year         = track.get("release_year", "") or track.get("spotify_year", ""),
                                        track_number = str(track.get("track_number", "")),
                                        disc_number  = str(track.get("disc_number", "1")),
                                    )
                                    _mk.stamp(_dev_key, _exp_mk)
                                # Store for M3U8
                                m3u8_entries.append((target_storage, device_parts))

                                # Periodic health check (Rev 10) — every N copies
                                _tracks_since_health_check[0] += 1
                                if _tracks_since_health_check[0] >= _HEALTH_CHECK_EVERY_N_TRACKS:
                                    _tracks_since_health_check[0] = 0
                                    copied_so_far = self.stats.get("copied", 0)
                                    mtp.log_health_check(
                                        f"Health @ {copied_so_far} tracks copied"
                                    )

                                t_n = normalize_key(track["title"])
                                a_n = normalize_key(track["artist"])
                                with self._lib_lock:
                                    self.library[f"{a_n}|{t_n}"] = {
                                        "local_path":     pathlib.Path(tmp_file),
                                        "device_parts":   device_parts,
                                        "device_storage": target_storage,
                                        "artist_norm":    a_n,
                                        "title_norm":     t_n,
                                    }
                            except StorageFullError as e:
                                log.error(f"  [Storage Full] {e}")
                                self._cancelled = True
                            except Exception as e:
                                log.error(f"  [Transfer Failed] {track['title']}: {e}")
                                self._record_missing(track, f"Transfer error: {e}")
                            finally:
                                shutil.rmtree(tmp_dir, ignore_errors=True)

                    except Exception as e:
                        log.error(f"  [Consumer Error] {e}")

            producer_thread = threading.Thread(target=_producer, daemon=True)
            consumer_thread = threading.Thread(target=_consumer, daemon=True)
            producer_thread.start()
            consumer_thread.start()
            producer_thread.join()
            consumer_thread.join()

            if self._cancelled:
                log.info("Sync stopped (storage full or cancellation).")
                break

            self._write_m3u8(pl_name, m3u8_entries)

        self._write_report()

        # ── Streaming-history smart playlists (Stage 7b) ─────────────────────
        # Generate 4 extra M3U8 playlists from the user's listening history:
        #   🔥 Most Played Ever, 📅 Last 12 Months, ✅ Never Skipped, 🌀 Rediscovery
        # Each playlist is written only for tracks that are actually on the device.
        # Runs after all standard playlists so the device index is fully populated.
        if not self._cancelled:
            try:
                import streaming_history as _sh
                _sh_scores = _sh.load()
                if _sh_scores:
                    _smart_pls = _sh.smart_playlists(_sh_scores)
                    # Build a URI → device path index from what we transferred this run
                    # plus whatever was already on the device from prior runs.
                    _uri_to_parts: dict[str, list[tuple[str, list[str]]]] = {}
                    for track in self.tracks_transferred:
                        uri = track.get("uri") or track.get("spotify_uri") or ""
                        if not uri and track.get("id"):
                            uri = f"spotify:track:{track['id']}"
                        if uri and track.get("device_parts"):
                            _uri_to_parts.setdefault(uri, []).append(
                                (track.get("storage", config.WRITE_STORAGE), track["device_parts"])
                            )

                    for pl_name, uri_list in _smart_pls.items():
                        entries = []
                        for uri in uri_list:
                            for storage, parts in _uri_to_parts.get(uri, []):
                                entries.append((storage, parts))
                        if entries:
                            # Prefix with emoji-safe ASCII name for M3U8 file
                            safe_name = pl_name.replace("🔥", "Top").replace("📅", "Recent") \
                                               .replace("✅", "NeverSkipped").replace("🌀", "Rediscovery") \
                                               .strip()
                            self._write_m3u8(safe_name, entries)
                            log.info(
                                f"  [SmartPlaylist] '{safe_name}': {len(entries)} track(s) on device"
                            )
                        else:
                            log.debug(f"  [SmartPlaylist] '{pl_name}': 0 tracks on device — skipping")
            except Exception as _sh_e:
                log.debug(f"  [SmartPlaylist] Skipped: {_sh_e}")

        # Stage 8: post-sync device audit (device files only)
        if config.AUDIT_AFTER_SYNC and not self._cancelled:
            log.info("\nStage 8: post-sync device audit ...")
            # ── Mojibake folder repair ────────────────────────────────────────
            # Rename any folders with Latin-1/UTF-8 encoding corruption on the
            # device (e.g. "AminÃ©" → "Aminé") BEFORE scanning, so that
            # _canonical_dest_parts paths match actual device folder names and
            # wpd_transfer navigation succeeds. Without this, every audit run
            # re-pushes all affected tracks and hits the 90s CopyHere timeout.
            import mtp_transfer as _mtp_pre
            _mtp_pre.rename_mojibake_folders(
                config.DEVICE_NAME,
                config.STORAGE_ROOTS,
            )
            # ─────────────────────────────────────────────────────────────────
            from audit import LibraryAuditor, _save_audit_state
            auditor = LibraryAuditor()
            device_files = auditor._enumerate_device()
            log.info(f"Device files to audit: {len(device_files)}")
            if device_files:
                batch_size = config.AUDIT_BATCH_SIZE
                n_batches  = (len(device_files) + batch_size - 1) // batch_size
                for batch_idx in range(n_batches):
                    if self._cancelled:
                        break
                    batch   = device_files[batch_idx*batch_size:(batch_idx+1)*batch_size]
                    changed = auditor._audit_device_batch(batch)
                    _save_audit_state(auditor.state)
                    processed = min((batch_idx+1)*batch_size, len(device_files))
                    log.info(
                        f"  Device audit {batch_idx+1}/{n_batches}: "
                        f"{processed}/{len(device_files)} processed, {changed} changed"
                    )
                    # Health check between audit batches (Rev 10)
                    if batch_idx % 5 == 0 and batch_idx > 0:
                        mtp.log_health_check(f"Audit health @ batch {batch_idx+1}/{n_batches}")
                auditor._write_report()

    # -----------------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------------

    def _write_report(self):
        log.info("\n--- SYNC COMPLETE -------------------------------------------")
        for k, v in self.stats.items():
            log.info(f"  {k.capitalize():<12}: {v}")

        # MK10 ISSUE-G: space housekeeping summary
        # Collect migration + dedup stats if they were run this session
        space_lines = []
        try:
            dedup_stats = self.stats.get("dedup", {})
            if dedup_stats.get("deleted", 0) > 0:
                space_lines.append(
                    f"  Dedup deleted : {dedup_stats['deleted']} file(s) "
                    f"({dedup_stats.get('bytes_freed', 0) // (1024**2)} MB freed)"
                )
        except Exception:
            pass
        try:
            migrate_stats = self.stats.get("sd_migration", {})
            if migrate_stats.get("files_migrated", 0) > 0:
                space_lines.append(
                    f"  → SD migrated : {migrate_stats['files_migrated']} file(s) "
                    f"({migrate_stats.get('bytes_moved', 0) // (1024**2)} MB moved)"
                )
                int_before = migrate_stats.get("internal_free_before", -1)
                int_after  = migrate_stats.get("internal_free_after",  -1)
                if int_before >= 0 and int_after >= 0:
                    freed = (int_after - int_before) // (1024**2)
                    space_lines.append(
                        f"  Internal freed: +{freed} MB "
                        f"({int_before//(1024**3):.2f} GB → {int_after//(1024**3):.2f} GB free)"
                    )
        except Exception:
            pass
        if space_lines:
            log.info("  --- Space housekeeping ---")
            for line in space_lines:
                log.info(line)

        if self.missing:
            config.REPORT_FILE.write_text("\n".join(self.missing), encoding="utf-8")
            log.info(f"Missing report → {config.REPORT_FILE}")

        # MK37/S2-T3: revoke the pass capability now that run_sync is done.
        # Belt-and-suspenders — the atexit registered at start also covers it.
        try:
            from mk37_wiring import end_sync_pass
            end_sync_pass()
        except ImportError:
            pass
        except Exception as _e_end:
            log.debug(f"  [MK37/S2-T3] end_sync_pass error (non-fatal): {_e_end}")


# =============================================================================
# REORGANISER (fixed: delegates to walkman_reorganize.py v22)
# =============================================================================

def reorganize_device_library(
    log_callback: Callable[[str], None] | None = None,
    cancelled_flag: list[bool] | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Reorganises every audio file on the Sony Walkman (both storages) into:

      Music/
        {AlbumArtist}/
          {Album}/
            {AlbumArtist} - {Album} - {NN} {Title}.ext

    This version delegates to the standalone reorganiser (walkman_reorganize.py)
    which does NOT rename folders before moving files.  Empty source folders
    are deleted afterwards.

    Returns {"folders_renamed": 0, "files_renamed": int, "errors": int}.
    """
    # Delegate to the standalone reorganiser (v22)
    from walkman_reorganize import reorganize_device
    return reorganize_device(
        execute=not dry_run,
        storage="both",
        album="",
        workers=4,
        limit=0,
        verbose=False,
        log_callback=log_callback,
        cancelled_flag=cancelled_flag,
    )


def run_audit(
    log_callback: Callable[[str], None] | None = None,
    cancelled_flag: list[bool] | None = None,
) -> dict:
    """
    Standalone audit entry point (called from GUI 'Audit Only' button).
    Runs both pre-sync local fix AND post-sync device audit in sequence.

    Respects config.AUDIT_FOLDER_MODE:
      False (default) — file-batch mode  (_audit_device_batch)
      True            — folder-wise mode (_audit_device_folder, via auditor.run())

    The within-run MTP wedge counter (_MTP_CONSECUTIVE_PUSH_FAILS) is reset
    after the audit phase so a wedged batch doesn't block the reorganise pass
    that main.py optionally runs immediately afterwards.
    """
    from audit import LibraryAuditor, _save_audit_state
    import audit as _audit_mod

    # MK33 O1-T1: Stamp policy snapshot at audit startup for reproducibility.
    # CTO: "policy snapshots + evidence snapshots = deterministic replay."
    try:
        import config as _cfg_o1s, meta_key as _mk_o1s
        _snap = _cfg_o1s.freeze_policy_snapshot()
        _state = _mk_o1s.load_state()
        _state["__policy_snapshot__"] = _snap
        _mk_o1s._flush()
        log.info(f"[MK33/O1] Policy snapshot stamped: version={_snap['version']!r}")
    except Exception as _o1e:
        log.debug(f"[MK33/O1] Policy snapshot skipped: {_o1e}")

    # MK29 G3-T1: Lazy schema migration on audit startup.
    try:
        from meta_key import lazy_invalidate_for_schema_bump as _lazy_inv
        _mig = _lazy_inv()
        if not _mig.get("already_migrated"):
            log.info(
                f"[MK29/G3] Schema migration: {_mig['invalidated']} entries invalidated, "
                f"{_mig['skipped_external']} external_metadata entries preserved."
            )
    except Exception as _mig_e:
        log.debug(f"[MK29/G3] Schema migration skipped: {_mig_e}")
    setup_logging(log_callback)

    auditor     = LibraryAuditor(log_callback=log_callback)
    local_files = auditor._enumerate_local()
    auditor._audit_local_files(local_files)

    if cancelled_flag and cancelled_flag[0]:
        return auditor.stats

    device_files = auditor._enumerate_device()
    log.info(f"Device files to audit: {len(device_files)}")

    if device_files:
        if config.AUDIT_FOLDER_MODE:
            # Folder-wise mode: delegate entirely to auditor.run() which
            # handles folder grouping, TIDAL prefetch and dedup sweeps.
            # Pass cancelled_flag so the GUI Cancel button works.
            auditor.run(cancelled_flag=cancelled_flag)
        else:
            # File-batch mode (original behaviour)
            batch_size = config.AUDIT_BATCH_SIZE
            n_batches  = (len(device_files) + batch_size - 1) // batch_size
            for batch_idx in range(n_batches):
                if cancelled_flag and cancelled_flag[0]:
                    break
                batch   = device_files[batch_idx * batch_size:(batch_idx + 1) * batch_size]
                changed = auditor._audit_device_batch(batch)
                _save_audit_state(auditor.state)
                processed = min((batch_idx + 1) * batch_size, len(device_files))
                log.info(
                    f"  Audit {batch_idx + 1}/{n_batches}: "
                    f"{processed}/{len(device_files)} processed, {changed} changed"
                )
            auditor._write_report()

    # FIX 13: clear the push-fail backoff cache so transient MTP wedge
    # failures recorded during this audit run don't carry into the
    # subsequent reorganise pass.  The old code tried to reset a module
    # attribute (_MTP_CONSECUTIVE_PUSH_FAILS) that never existed in audit.py;
    # audit.py uses _PUSH_FAIL_CACHE (a per-file dict) instead.
    # Clearing it here is safe: the backoff only protects against chronic
    # failures (3+ runs), and a reorganise pass is a fresh operation.
    try:
        if hasattr(_audit_mod, "_PUSH_FAIL_CACHE"):
            _audit_mod._PUSH_FAIL_CACHE.clear()
            log.debug("  [run_audit] Cleared push-fail backoff cache after audit.")
    except Exception:
        pass

    return auditor.stats


# ---------------------------------------------------------------------------
# Compatibility alias — main.py imports this name from walkman_sync
# ---------------------------------------------------------------------------
run_reorganize = reorganize_device_library