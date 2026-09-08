"""
walkman_dedup.py  —  Device-side deduplication & flawed-file pruning  (Rev 14.4)
===============================================================================
Identifies and removes three classes of flawed / redundant files on the Walkman:

  1. Zero-length / corrupt       — file size < DEDUP_SMALL_FILE_THRESHOLD_BYTES
                                   (default 10 KB) or duration < 5 s on tag read.
                                   Unconditionally safe to delete — serves no
                                   purpose, usually a failed-download artifact.

  2. Inferior format duplicates  — when a FLAC and an MP3/M4A/OGG exist for the
                                   same canonical (artist, album, title), the
                                   lossy copy goes.  The lossless one is kept.

  3. Inferior metadata duplicates — when two files of the *same* format exist
                                   for the same canonical key, the one with
                                   fewer populated tag fields is dropped.
                                   Keeps the better-tagged copy.

Two delete modes:
  - HARD  (rm -f)                       — used pre-sync to actually free space
  - SOFT  (move to Music/.deleted/…)    — used in the backtrack audit / reorg
                                          so a human can review and restore

Three run modes (config.DEDUP_MODE):
  - report (default)                    — log only, change nothing
  - active                              — execute the plan in whichever delete
                                          mode the caller requested
  - off                                 — skip entirely

Two programmatic entry points:
  - prune_before_sync(mode="hard")       — called from _sync_thread at start
  - prune_during_audit(mode="soft")      — called from _reorganize_thread
                                          before a reorganize run

Backwards-compat: the original CLI (`python -m walkman_dedup --execute`) still
works and still does FLAC-beats-MP3 dedup.  New flags add the other passes.

Safety guardrails baked into every call:
  - Never delete if a match group has only 1 file
  - Never delete if deletion would leave 0 files for a canonical key
  - Always write .dedup_log.json recording every candidate action before doing it
  - Report mode is the default in config.py so an unconfigured install is safe
  - Title-only matches still route through the AI same-track resolver
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

import config
import mtp_transfer as mtp


# ═══════════════════════════════════════════════════════════════════════════
# MK34 P4-T3: Pre-deletion dedup blocked unless verified
# ═══════════════════════════════════════════════════════════════════════════
# CTO mandate (MK34 directive): "STOP pre-deletion dedup."
DEDUP_DESTRUCTIVE_VERIFIED = False


def _set_dedup_verified(value: bool = True) -> None:
    """Called by mk34_invariants.gate_pre_execution() after invariants pass."""
    global DEDUP_DESTRUCTIVE_VERIFIED
    DEDUP_DESTRUCTIVE_VERIFIED = bool(value)


def _check_destructive_verified(operation_name: str = "destructive_op",
                                 capability=None) -> bool:
    """
    MK35 Q2-T3 + Q5-T3: Capability-gated destructive check.

    Replaces the convention-based MK34 flag with a structural capability arg.
    When capability is provided and valid, the destructive operation may
    proceed. When None, falls back to the legacy MK34 flag with a deprecation
    warning (so in-flight rollouts don't break).

    Returns True if destructive op may proceed; False (with log) otherwise.
    """
    # MK35 Q2-T3: capability path (preferred)
    if capability is not None:
        try:
            from mk35_capability import require_capability
            require_capability(capability,
                                operation="dedup_destructive",
                                path=operation_name,
                                consume=True)
            return True
        except ImportError:
            pass
        except Exception as e:
            import logging as _log_p4t3
            _log_p4t3.getLogger(__name__).warning(
                f"  [Dedup/MK35-Q2] Capability check failed for "
                f"operation={operation_name!r}: {e}"
            )
            return False

    # MK34 P4-T3 fallback (legacy convention-based flag)
    if not DEDUP_DESTRUCTIVE_VERIFIED:
        import logging as _log_p4t3
        _log_p4t3.getLogger(__name__).warning(
            f"  [Dedup/MK34-P4-T3] DEDUP_DESTRUCTIVE_BLOCKED operation={operation_name!r} "
            f"— DEDUP_DESTRUCTIVE_VERIFIED=False. Run through mk34_invariants gate first. "
            f"CTO Class C: no destructive op without verified alternative."
        )
        return False

    # Convention-based path active — log a deprecation note so operators
    # know to migrate to capabilities (MK36+ may remove the flag entirely)
    import logging as _log_p4t3
    _log_p4t3.getLogger(__name__).debug(
        f"  [Dedup/MK35-Q2] DEDUP_LEGACY_FLAG_PATH operation={operation_name!r} "
        f"— consider passing capability= for structural enforcement."
    )
    return True

try:
    from device_library import _is_junk_prefix as _dev_is_junk_prefix
except ImportError:
    def _dev_is_junk_prefix(s: str) -> bool:
        return "spotidownloader" in s.lower() or "ytmp3" in s.lower()

try:
    import ai_helper as _ai_helper
    _AI_AVAILABLE = True
except ImportError:
    _AI_AVAILABLE = False

# MK22 B3.3: zombie dedup registry + dialog rate circuit breaker
# ROOT CAUSE (LOG_5_5_26): verification failure (remote size=0) on internal
# storage (99% full) caused dedup to re-queue the same file repeatedly.
# Result: 415 verify failures, 743 dialog clicks, 9+ hour Stage 2 loop.
import collections as _dd_collections
import threading as _dd_threading_b33
_ZOMBIE_DEDUP_REGISTRY: dict = {}    # {path: fail_count}
_ZOMBIE_LOCK = _dd_threading_b33.Lock()
_DIALOG_TIMESTAMPS: list = []        # timestamps of recent "Confirm File Delete" dialogs
_DIALOG_STORM_TRIPPED: bool = False  # True = abort mutating ops


def _record_dedup_failure(path: str) -> int:
    """MK22 B3.3: Record a dedup failure for a path. Returns new fail count."""
    with _ZOMBIE_LOCK:
        _ZOMBIE_DEDUP_REGISTRY[path] = _ZOMBIE_DEDUP_REGISTRY.get(path, 0) + 1
        return _ZOMBIE_DEDUP_REGISTRY[path]


def _is_zombie(path: str, max_attempts: int | None = None) -> bool:
    """Return True if this path has failed too many times this session."""
    import config as _cfg_b33
    _max = max_attempts or int(getattr(_cfg_b33, "ZOMBIE_DEDUP_MAX_ATTEMPTS", 3))
    with _ZOMBIE_LOCK:
        return _ZOMBIE_DEDUP_REGISTRY.get(path, 0) >= _max


def _record_dialog_click() -> bool:
    """
    MK26 C6 fix (A3): Dual threshold — rate/min OR cumulative session.
    Evidence (LOG_6_5_26): 621 dialogs over 12h = 0.86/min, under 20/min threshold.
    DIALOG_STORM_ABORT never fired. Run continued for 12.7 hours with 100% failure.
    Fix: trip breaker if EITHER rate > 20/min (burst storm) OR total > 50 this session.
    """
    global _DIALOG_STORM_TRIPPED
    import time as _t_b33
    import config as _cfg_b33
    _thresh_rate  = int(getattr(_cfg_b33, "DIALOG_RATE_THRESHOLD_PER_MIN", 20))
    _thresh_total = int(getattr(_cfg_b33, "DIALOG_SESSION_ABORT_THRESH", 50))
    now = _t_b33.monotonic()
    with _ZOMBIE_LOCK:
        _DIALOG_TIMESTAMPS.append(now)
        # Update session-level counter in config (visible across modules)
        try:
            _cfg_b33.DIALOG_SESSION_TOTAL = getattr(_cfg_b33, "DIALOG_SESSION_TOTAL", 0) + 1
            session_total = _cfg_b33.DIALOG_SESSION_TOTAL
        except Exception:
            session_total = len(_DIALOG_TIMESTAMPS)
        # Prune rate window (60s rolling)
        while _DIALOG_TIMESTAMPS and now - _DIALOG_TIMESTAMPS[0] > 60.0:
            _DIALOG_TIMESTAMPS.pop(0)
        rate = len(_DIALOG_TIMESTAMPS)
        if not _DIALOG_STORM_TRIPPED:
            _tripped_by = None
            if rate > _thresh_rate:
                _tripped_by = f"rate={rate}/min (threshold: {_thresh_rate}/min)"
            elif session_total > _thresh_total:
                _tripped_by = (f"session total={session_total} dialogs "
                               f"(threshold: {_thresh_total} — slow-drip storage-full cascade)")
            if _tripped_by:
                _DIALOG_STORM_TRIPPED = True
                import logging as _log_b33
                _log_b33.getLogger(__name__).critical(
                    f"[Dedup/Gate] DIALOG_STORM_ABORT — tripped by {_tripped_by}. "
                    f"Aborting mutating dedup. Root cause: likely full storage → "
                    f"size=0 COM placeholders → delete-confirm loop."
                )
        return _DIALOG_STORM_TRIPPED


# MK19 RC1: per-run AI call cap + stage breaker (RC3)
import threading as _dedup_threading
_DEDUP_AI_CALLS_THIS_RUN: int = 0
_DEDUP_AI_LOCK = _dedup_threading.Lock()
_DEDUP_STAGE_AI_DISABLED: bool = False    # RC3: stage breaker
_DEDUP_STAGE_AI_CONSEC_FAILS: int = 0    # RC3: consecutive failure counter

def _reset_dedup_ai_counters() -> None:
    """Reset per-run AI counters. Call at pipeline start."""
    global _DEDUP_AI_CALLS_THIS_RUN, _DEDUP_STAGE_AI_DISABLED, _DEDUP_STAGE_AI_CONSEC_FAILS
    with _DEDUP_AI_LOCK:
        _DEDUP_AI_CALLS_THIS_RUN   = 0
        _DEDUP_STAGE_AI_DISABLED   = False
        _DEDUP_STAGE_AI_CONSEC_FAILS = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# MK39.1 L1.3 + L1.4 — Dedup pre-commit review helpers
# Council operator-feedback 2026-05-10: production dry-run produced 4253
# planned deletes. Operator needs structured audit (L1.3) + visibility into
# truncation-collision dupes that are the structural root cause (L1.4).
# ─────────────────────────────────────────────────────────────────────────

# L1.4 — detect folders that differ only by truncation length suffix.
# Example from LOG_10_5_26:
#   'The Hygrades - Wake Up You! ... Vol. 1'
#   'The Hygrades - Wake Up You! ... Vol. 1 (197'    <-- truncated mid-year
# Same album written to two folders → 2× the dedup work and lost playlist
# integrity on the Walkman. Full structural fix deferred (walkman_reorganize
# refactor); this helper is OBSERVABILITY ONLY.

def _strip_trailing_truncation(folder: str) -> str:
    """Remove trailing fragments that look like mid-token truncation:
    open-paren with no close, trailing comma, trailing underscore, etc."""
    s = folder.rstrip(" /\\")
    # Drop trailing "(NNN" or "(N" or similar — open paren never closed
    s = re.sub(r"\s*\([^)]*$", "", s)
    # Drop trailing comma fragment
    s = re.sub(r"\s*,\s*[^,]*$", "", s) if s.count(",") >= 1 and not s.rstrip().endswith(",") else s
    return s.rstrip(" .,_-/\\")


def _prefix_normalize_truncated(name: str, prefix_len: int = 32) -> str:
    """
    Issue 3 / Council fix: collapse two independently-truncated variants of
    the same album name to one comparison key.

    `_strip_trailing_truncation` only catches an *unclosed* trailing paren —
    that's what downloader._safe()'s bracket-balancing produces, but
    path_utils._pu_safe() (pre-fix) did a raw byte-boundary cut with no
    balancing, so its output often ends mid-word or on a closed-but-unrelated
    fragment that the paren regex doesn't match at all. We can't recover the
    original untruncated string from either truncated variant, but both are
    guaranteed to share a common prefix out to at least `prefix_len` chars
    (both truncation implementations start from the same NFC-normalised
    input). Take that shared prefix, trimmed back to the last whole word, so
    two differently-truncated variants land on the identical key regardless
    of exactly where either one was cut.
    """
    s = (name or "")[:prefix_len]
    if len(name or "") > prefix_len and " " in s:
        s = s.rsplit(" ", 1)[0]
    return s.strip(" .,_-()[]{}")


def looks_like_truncation_dupe(human_path: str, superior_path: str) -> bool:
    """True when the dedup candidate and its 'superior' partner sit in
    folders that differ ONLY by truncation length (e.g., one is a strict
    prefix of the other after sanitizing trailing fragments)."""
    if not human_path or not superior_path:
        return False
    a_folder = os.path.dirname(human_path).replace("\\", "/")
    b_folder = os.path.dirname(superior_path).replace("\\", "/")
    if a_folder == b_folder:
        return False
    a_clean = _strip_trailing_truncation(a_folder)
    b_clean = _strip_trailing_truncation(b_folder)
    if not a_clean or not b_clean:
        return False
    # Same prefix once the truncated tail is removed?
    if a_clean == b_clean:
        return True
    # One is a prefix of the other (different lengths)
    if a_folder.startswith(b_folder) or b_folder.startswith(a_folder):
        return True
    return False


# L1.3 — Categorize a dedup candidate for operator review.
_TRACK_NUM_RE = re.compile(r"\s-\s(\d{2})\s")


def _extract_track_number(path: str) -> Optional[str]:
    """Pull '01', '02', ... from filename like 'Artist - Album - 04 Title.mp3'."""
    m = _TRACK_NUM_RE.search(path or "")
    return m.group(1) if m else None


def _categorize_dedup_candidate(*, human_path: str,
                                  superior_path: str,
                                  reason: str) -> str:
    """Return one of: format_upgrade | track_renumber | truncation_dupe | other.

    Order of checks matters: truncation is the strongest structural signal
    so it wins over format/track when both apply.
    """
    if looks_like_truncation_dupe(human_path, superior_path):
        return "truncation_dupe"

    a_track = _extract_track_number(human_path)
    b_track = _extract_track_number(superior_path)
    if a_track and b_track and a_track != b_track:
        return "track_renumber"

    # Format upgrade: extensions differ AND same folder + track
    a_ext = os.path.splitext(human_path)[1].lower()
    b_ext = os.path.splitext(superior_path)[1].lower()
    if a_ext != b_ext:
        # Same folder, same track number → clean format upgrade
        if (os.path.dirname(human_path) == os.path.dirname(superior_path)
                and a_track == b_track):
            return "format_upgrade"
        # Format differs but folder/track also differs — still primarily a
        # format reason, but flagged 'other' so operator inspects.
    return "other"


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class DedupCandidate:
    """A single file flagged for deletion with full provenance."""
    storage: str
    parts: list        # folder parts (list of strings)
    filename: str
    ext: str
    size: int          # bytes; -1 if unknown
    reason: str        # "zero_length" | "small_file" | "inferior_format" | "inferior_metadata"
    superior_path: str # human-readable "storage/parts/filename" of what we keep
    score: int = 0     # metadata-completeness score (for inferior_metadata only)

    @property
    def human_path(self) -> str:
        return f"{self.storage}/{'/'.join(self.parts)}/{self.filename}"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["human_path"] = self.human_path
        return d


@dataclass
class DedupPlan:
    """The full set of candidates plus summary counts and the source scan."""
    candidates: list = field(default_factory=list)
    scan_total: int = 0
    summary: dict = field(default_factory=dict)

    def by_reason(self, reason: str) -> list:
        return [c for c in self.candidates if c.reason == reason]

    def update_summary(self) -> None:
        s: dict = defaultdict(int)
        for c in self.candidates:
            s[c.reason] += 1
        s["total"] = len(self.candidates)
        s["scan_total"] = self.scan_total
        self.summary = dict(s)


# =============================================================================
# Helpers — canonical key construction
# =============================================================================

_JUNK_ARTIST_WORDS = {
    "spotidownloader", "spotifydownloader", "ytmp3", "y2mate",
    "mp3juice", "downloadify", "tubidy", "unknown",
}


def _norm(text: str) -> str:
    """ASCII-fold, lowercase, drop punctuation, strip leading 'the/a/an'.

    2026-09-04: expand "&" to "and" first — same fix as device_library._norm
    and album_completeness._norm — so "Arnold & Danny" and "Arnold and
    Danny" match instead of colliding into two different keys.
    """
    text = (text or "").replace("&", " and ")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    text = re.sub(r"^(the |a |an )", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


# ─────────────────────────────────────────────────────────────────────────
# MK39.1 L2 — Dedup safety: title-only fallback gate
# Council Verdict 2026-05-10 (Forensic Review of Dry-Run Dedup Log):
# "The dedup engine's title-only fallback is currently unsafe for execute
# mode. The Council directs the team to implement D1 (contextual artist/
# album overlap gate) and D3 (elevated risk warning) before any execute-
# mode dedup run."
#
# Real false positives caught by this gate (from LOG_10_5_26):
#   1. Oliver Nelson 'Stolen Moments' (composition, 1961) was being
#      paired with Ahmad Jamal Trio's COVER of the same composition.
#      Different recordings, different artists, different albums.
#   2. Pharrell Williams 'Best Friend' (In My Mind, 2006) was being
#      paired with Brandy 'Best Friend' (Brandy, 1994). Completely
#      unrelated songs that just happen to share a title.
# Both pairs had ZERO artist overlap AND ZERO album overlap; with the
# 50% Jaccard gate, both are now rejected.
# ─────────────────────────────────────────────────────────────────────────

_DEDUP_OVERLAP_STOPWORDS = frozenset({
    # Articles / prepositions
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "by",
    # Connectives
    "and", "or", "with",
    # 'feat' family (already partially stripped by _strip_feat but
    # belt-and-suspenders for folder names that contain it)
    "feat", "ft", "featuring",
    # Album-decoration noise
    "vol", "volume", "ep", "lp", "edition", "remastered",
    "deluxe", "expanded", "version", "single", "album",
    # Disc markers
    "disc", "cd",
})


def _tokens_for_overlap(s: str) -> set:
    """Tokens for Jaccard comparison, after _norm + stopword strip."""
    if not s:
        return set()
    raw = _norm(s)
    if not raw:
        return set()
    return {t for t in raw.split() if t and t not in _DEDUP_OVERLAP_STOPWORDS}


def _jaccard(a: set, b: set) -> float:
    """Set-Jaccard similarity in [0.0, 1.0]. Empty/empty → 0.0 (no signal)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def passes_title_only_gate(
    artist_a: str, album_a: str,
    artist_b: str, album_b: str,
    *, threshold: float = 0.5,
) -> tuple[bool, float, float]:
    """MK39.1 L2 / Council D1 — gate a title-only dedup match.

    Returns (passes, artist_jaccard, album_jaccard).

    Logic:
      - Compute Jaccard token-overlap for artist sides AND album sides.
      - If EITHER overlap >= threshold (default 50%) → pass.
      - If neither side has any tokens to compare (both empty), → reject:
        cannot vouch for the match without any context beyond the title.

    Either-condition rather than both-condition because folder truncation
    can wreck one of (artist | album) but the other usually survives —
    that legit case should still match. Both real false positives from
    LOG_10_5_26 had ZERO overlap on BOTH sides; either-condition is safe.
    """
    a_artist = _tokens_for_overlap(artist_a)
    b_artist = _tokens_for_overlap(artist_b)
    a_album  = _tokens_for_overlap(album_a)
    b_album  = _tokens_for_overlap(album_b)

    # Compute overlap only on sides where both have tokens; empty side
    # gets 0.0 (no signal) — falls through to the other dimension.
    artist_jac = _jaccard(a_artist, b_artist) if (a_artist and b_artist) else 0.0
    album_jac  = _jaccard(a_album,  b_album)  if (a_album  and b_album)  else 0.0

    # Pass if EITHER dimension shows enough overlap.
    passes = (artist_jac >= threshold) or (album_jac >= threshold)
    return passes, artist_jac, album_jac


def _strip_feat(title: str) -> str:
    # 2026-09-05: added "with" -- confirmed live that the download/completeness
    # pipeline grabs the same track twice for many albums, once with a
    # "(with X)" collaborator credit in the title and once without (JID's
    # "God Does Like Ugly" had ~26 files for a 15-track album this way:
    # "Sk8 (with Ciara and EARTHGANG)" + "Sk8", "What We On (with Don
    # Toliver)" + "What We On", etc.). Same format, both fully tagged, so
    # neither format-dominance nor metadata-completeness flagged them. ~721
    # folders / ~1356 extra files library-wide. Stripping "(with ...)" too
    # collapses these onto one canonical key.
    return re.sub(
        r"\s*[\(\[\{](?:feat|ft|featuring|with)\b[^\)\]\}]*[\)\]\}]",
        "", title or "", flags=re.I,
    ).strip()


def _parse_filename(filename: str) -> tuple:
    """Strip track number prefix and return (stem_parts, title_stripped_feat)."""
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"^\d{1,3}(?:-\d{1,2})?[\s\-\.]+", "", stem)
    parts = [p.strip() for p in stem.split(" - ")]
    if len(parts) >= 2:
        title = re.sub(r"^\d{1,3}[\s\-\.]+", "", parts[-1]).strip()
    elif len(parts) == 1:
        title = parts[0]
    else:
        title = stem
    return parts, _strip_feat(title)


def _parse_folder_artist_album(folder_parts: list) -> tuple:
    """MK39.1 L2 helper — extract (artist, album) from folder parts using the
    same logic as _track_keys but returning the strings directly. Used by the
    title-only fallback gate to evaluate token overlap on the two candidates'
    folder context independently of the matching key."""
    artist = ""
    album = ""
    if folder_parts:
        last_folder = folder_parts[-1]
        if _dev_is_junk_prefix(last_folder):
            if " - " in last_folder:
                last_folder = last_folder.split(" - ", 1)[1].strip()
            else:
                last_folder = ""
        if last_folder and " - " in last_folder:
            a, b = last_folder.split(" - ", 1)
            artist = a.strip()
            album = b.strip()
        elif last_folder:
            album = last_folder.strip()
            # Plain Artist/Album two-level layout: the album folder alone
            # carries no artist info, so fall back to the nearest parent
            # folder that isn't just a storage-subfolder root marker (every
            # device path starts with "Music", sometimes doubled). Without
            # this, artist is always "" for this (the common) layout, which
            # risks false-positive grouping collisions between different
            # artists sharing an album name/track number.
            candidates = [p for p in folder_parts[:-1] if p.strip().lower() != "music"]
            if candidates:
                artist = candidates[-1].strip()
    return artist, album


def _track_keys(folder_parts: list, filename: str) -> list:
    """
    Build canonical grouping keys (richest first) for a device file.
    Returns: [(artist, album, title), (artist, title), (album, title), (title,)]
    """
    artist = ""
    album = ""

    if folder_parts:
        last_folder = folder_parts[-1]
        if _dev_is_junk_prefix(last_folder):
            if " - " in last_folder:
                last_folder = last_folder.split(" - ", 1)[1].strip()
            else:
                last_folder = ""
        if last_folder and " - " in last_folder:
            a, b = last_folder.split(" - ", 1)
            artist = a.strip()
            album = b.strip()
        elif last_folder:
            album = last_folder.strip()

    if not artist or artist.lower() in _JUNK_ARTIST_WORDS or _dev_is_junk_prefix(artist):
        parts, _ = _parse_filename(filename)
        if len(parts) >= 2 and not artist:
            artist = parts[0]
        if len(parts) >= 3 and not album:
            album = parts[1]

    _, title = _parse_filename(filename)

    artist_n = _norm(artist)
    album_n = _norm(album)
    title_n = _norm(title)
    if not title_n:
        return []

    keys = []
    if artist_n and album_n:
        keys.append((artist_n, album_n, title_n))
        # MK39.1 L4.3 / Council L3 Patch Review §4.2 — truncation-tolerant
        # key construction. When the album name carries a folder-truncation
        # tail (e.g., 'Vol. 1 (197' from a FAT32 limit), also emit a rich
        # key built from the *stripped* album so this file groups with
        # its non-truncated sibling (e.g., 'Vol. 1'). Belt-and-suspenders:
        # the original key is preserved so non-truncation behavior is
        # unchanged; we only ADD an extra rich-key pairing when warranted.
        # This fixes the residual concern that strict mode mitigated but
        # non-strict cleanup runs would still surface.
        try:
            album_stripped = _strip_trailing_truncation(album)
            album_stripped_n = _norm(album_stripped)
            if album_stripped_n and album_stripped_n != album_n:
                keys.append((artist_n, album_stripped_n, title_n))
        except Exception:
            pass
        # Issue 3 / Council fix — catches truncation variants the paren-only
        # regex above misses (raw byte-cut with no unclosed bracket).
        try:
            if len(album) >= 32:
                album_prefix_n = _norm(_prefix_normalize_truncated(album))
                if album_prefix_n and album_prefix_n != album_n:
                    keys.append((artist_n, album_prefix_n, title_n))
        except Exception:
            pass
    if artist_n:
        keys.append((artist_n, title_n))
    if album_n:
        keys.append((album_n, title_n))
        # Same truncation tolerance for (album, title) fallback key
        try:
            album_stripped = _strip_trailing_truncation(album)
            album_stripped_n = _norm(album_stripped)
            if album_stripped_n and album_stripped_n != album_n:
                keys.append((album_stripped_n, title_n))
        except Exception:
            pass
        try:
            if len(album) >= 32:
                album_prefix_n = _norm(_prefix_normalize_truncated(album))
                if album_prefix_n and album_prefix_n != album_n:
                    keys.append((album_prefix_n, title_n))
        except Exception:
            pass
    keys.append((title_n,))
    return keys


# =============================================================================
# Device size probe (for zero-length detection without a pull)
# =============================================================================

def _stat_device_file(storage: str, parts: list, filename: str) -> int:
    """
    Return file size in bytes via ADB stat (fast path), falling back to a
    raw COM lookup for this one file if ADB can't answer — or -1 if neither
    can determine the size. Cheap — no file pull required.

    2026-07-26: previously ADB-only, silently returning -1 whenever ADB
    wasn't connected. _pass_zero_length treats size<0 as "skip, can't
    tell" — so whenever ADB happened to be flaky (this project's
    well-documented pattern) for an entire pass1 run, every file was
    silently skipped and pass1 reported "0 candidates," indistinguishable
    from a genuinely clean library. Confirmed live: an entire album (16
    tracks) of genuinely 0-byte corrupt stubs went completely undetected
    this way. The COM fallback here is a raw (non-isolated) call, not
    subprocess-isolated like this project's other COM primitives — pass1
    calls this once per file (up to ~1200+ times per run), so per-call
    subprocess spawning would be prohibitively slow. Expected to be rarely
    exercised in practice now that the caller also attempts an ADB
    reconnect before running pass1 (see build_plan()).

    2026-07-27: the ADB stat call itself was passing `remote` as a bare
    argv element to `adb shell` — adb joins trailing shell args with a
    single space before sending them to the device, so any path with a
    space (nearly every track in this library) got word-split into
    several bogus operands, matching this project's own documented
    `_shell_quote()` gotcha (adb_transfer.py). Depending on which
    fragment happened to exist, this could return a wrong size instead
    of failing cleanly — confirmed live on one track where stat reported
    0 while an actual adb_pull_head() read 40960 real bytes from the
    same path. Now routed through `sh -c` with `_shell_quote()`, same
    pattern as adb_pull_head()'s dd call.
    """
    try:
        import adb_transfer as _adb
        if getattr(_adb, "device_is_connected", lambda _n: True)(""):
            storage_path = _adb._get_storage_path(storage)
            folder_path = storage_path + "/" + "/".join(parts) if parts else storage_path
            remote = folder_path + "/" + filename
            out = _adb._adb_cmd(
                ["shell", "sh", "-c", "stat -c %s " + _adb._shell_quote(remote)],
                check=False, timeout=15,
            )
            try:
                return int((out or "").strip())
            except ValueError:
                pass  # unparseable — fall through to the COM fallback below
    except Exception:
        pass

    try:
        import mtp_transfer as _mtp
        if _mtp.COM_AVAILABLE:
            import mtp_transfer_com as _com
            storage_root = _com._get_storage_root(
                getattr(config, "DEVICE_NAME", "WALKMAN"), storage
            )
            folder = _com._navigate(storage_root, list(parts), create=False)
            item = _com._find_remote_item(folder, filename)
            if item is not None:
                return int(item.Size or 0)
    except Exception:
        pass
    return -1


# ls -l data line: perms links owner group size  YYYY-MM-DD HH:MM  name
_LSR_ENTRY = re.compile(
    r"^([-dl])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\d{4}-\d\d-\d\d\s+\d\d:\d\d\s+(.*)$"
)
_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav",
               ".aiff", ".aif", ".ape", ".wv", ".wma", ".dsf", ".dff"}


def _bulk_device_scan(storages: list) -> tuple:
    """
    Task 35 — one `adb shell ls -lR <mount>/<subdir>` per storage root
    (≈10-15s total for the whole ~23k-file library) instead of ~23k
    per-file `stat` calls that never finished. Same technique as
    scratchpad/fast_scan.py, folded into the engine.

    Returns (files, sizes):
      files  — [{"storage","parts","filename","ext","track_number"}, ...]
               (audio files only), the same shape mtp.scan_device_audio yields
      sizes  — {(storage, tuple(parts), filename): size_bytes} for every
               regular file, keyed as _pass_zero_length looks a file up
    Empty ([], {}) on failure — caller falls back to its slower paths.
    """
    import posixpath as _pp
    files: list = []
    sizes: dict = {}
    try:
        import adb_transfer as _adb
    except Exception:
        return files, sizes

    trash = getattr(config, "DEDUP_SOFT_DELETE_FOLDER", ".deleted")

    # config.STORAGE_ROOTS overlaps: e.g. ("SD card","") and ("SD card","Music")
    # both resolve under the same mount, so a naive loop `ls -lR`s every file
    # twice (once with parts=["Music",...], once with parts=[...]) — phantom
    # duplicates that poison Pass 2/3. Keep only the specific subdir roots per
    # storage (drop the bare "" root when any non-empty subdir exists for it),
    # and de-dupe identical resolved paths. `parts` then stays relative to
    # <mount>/<subdir>, matching what the rest of the engine expects.
    _by_storage: dict = {}
    for storage, subdir in storages:
        _by_storage.setdefault(storage, []).append(subdir or "")
    _roots: list = []          # (storage, subdir, mount)
    _seen_paths: set = set()
    for storage, subs in _by_storage.items():
        try:
            mount = _adb._get_storage_path(storage)
        except Exception:
            mount = None
        if not mount:
            continue
        _nonempty = [s for s in subs if s]
        _use = _nonempty if _nonempty else [""]
        for subdir in dict.fromkeys(_use):
            scan_root = _pp.join(mount, subdir) if subdir else mount
            if scan_root in _seen_paths:
                continue
            _seen_paths.add(scan_root)
            _roots.append((storage, subdir, mount))

    for storage, subdir, mount in _roots:
        scan_root = _pp.join(mount, subdir) if subdir else mount
        out = _adb._adb_cmd(
            ["shell", "ls", "-lR", scan_root], timeout=120, check=False
        ) or ""
        if not out.strip():
            continue
        cur_dir = None
        n0 = len(files)
        for raw in out.splitlines():
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.endswith(":") and line.startswith("/"):
                cur_dir = line[:-1]
                continue
            if line.startswith("total "):
                continue
            m = _LSR_ENTRY.match(line)
            if not m or cur_dir is None or m.group(1) != "-":
                continue
            size_s, name = m.group(2), m.group(3)
            rel = cur_dir[len(mount):].lstrip("/")   # e.g. "Music/Artist/Album"
            parts = rel.split("/") if rel else []
            sizes[(storage, tuple(parts), name)] = int(size_s)
            ext = _pp.splitext(name)[1].lower()
            if ext not in _AUDIO_EXTS or name.startswith("DEL_") or trash in parts:
                continue
            tn = ""
            tm = re.search(r" - (\d{1,3}) ", name) or re.match(r"^(\d{1,3})[\s.\-]", name)
            if tm:
                tn = tm.group(1)
            files.append({"storage": storage, "parts": parts, "filename": name,
                          "ext": ext, "track_number": tn})
        log.info(f"[Dedup] bulk ls -lR {storage}: {len(files) - n0} audio file(s), "
                 f"{len(sizes)} total size(s)")
    return files, sizes


# =============================================================================
# Metadata completeness scoring
# =============================================================================

def _score_tags_from_pull(local_path: Path, skip_duration: bool = False) -> tuple:
    """
    Score a locally-pulled file on tag completeness (0-10).
    Returns (score, details).  Used by the metadata-completeness pass.

    skip_duration: set True when local_path is a partial/truncated pull
    (see device_metadata_score_lookup). mutagen computes MP3 duration as
    local_file_size / bitrate for CBR files without a Xing VBR header —
    on a truncated local copy that yields a wrong (much shorter) duration,
    which would wrongly dock the <30s penalty. FLAC/most VBR-MP3 headers
    encode duration directly and are unaffected either way, but there's
    no cheap way to tell which case applies without parsing first, so the
    caller just omits the point uniformly for every partially-scored
    candidate — symmetric omission doesn't bias the comparison, and
    _pass_metadata_completeness already requires a >=2-point gap to act.
    """
    try:
        from mutagen import File as _MutagenFile
        audio = _MutagenFile(str(local_path), easy=True)
        if audio is None:
            return 0, {"error": "unreadable"}
    except Exception as e:
        return 0, {"error": str(e)}

    tags = {}
    for k in ("title", "artist", "album", "albumartist", "date",
              "genre", "tracknumber", "discnumber"):
        v = audio.get(k)
        if v:
            if isinstance(v, list):
                v = v[0] if v else ""
            tags[k] = str(v).strip()

    score = 0
    for key in ("title", "artist", "album", "albumartist", "date",
                "genre", "tracknumber", "discnumber"):
        if tags.get(key):
            score += 1

    # Cover art present?
    try:
        from mutagen import File as _MutagenFile
        raw = _MutagenFile(str(local_path))
        if raw is not None:
            if hasattr(raw, "tags") and raw.tags:
                pics = [k for k in raw.tags.keys() if str(k).upper().startswith("APIC")]
                if pics:
                    score += 1
            if hasattr(raw, "pictures") and raw.pictures:
                score += 1
    except Exception:
        pass

    # Duration sanity — <30s doesn't contribute; <5s flagged by Pass 1 already
    if not skip_duration:
        try:
            from mutagen import File as _MutagenFile
            info = _MutagenFile(str(local_path))
            dur = getattr(info.info, "length", 0) if info is not None else 0
            tags["_duration"] = dur
            if dur >= 30:
                score += 1
        except Exception:
            pass

    return score, tags


# =============================================================================
# AI ambiguity resolver (from Rev 13 CLI, preserved)
# =============================================================================

def _ai_same_track(a_artist, a_album, a_title,
                   b_artist, b_album, b_title) -> bool:
    """
    MK19 RC1+RC3: AI dedup comparison with guards.

    RC1 guard: skip AI when artist+album both empty (title_only candidates).
    Empty-metadata prompts produce unreliable AI responses and burn quota
    for zero decision value. Use deterministic fallback instead.

    RC3 guard: check stage-level AI breaker (_DEDUP_STAGE_AI_DISABLED).
    Once the stage breaker trips (5 consecutive provider failures), no
    further AI calls are made for the remainder of this dedup stage.

    Per-run cap: DEDUP_AI_MAX_CALLS_PER_RUN (default 200) prevents storms.
    """
    global _DEDUP_AI_CALLS_THIS_RUN, _DEDUP_STAGE_AI_DISABLED, _DEDUP_STAGE_AI_CONSEC_FAILS

    if not _AI_AVAILABLE:
        return True

    # RC1: guard — skip AI if both identity fields are below minimum length
    try:
        _min_len = int(getattr(config, "DEDUP_AI_MIN_FIELD_LENGTH", 2))
        _ai_enabled = bool(getattr(config, "DEDUP_AI_ENABLED", True))
        _max_calls  = int(getattr(config, "DEDUP_AI_MAX_CALLS_PER_RUN", 200))
        _max_consec = int(getattr(config, "MAX_CONSECUTIVE_PROVIDER_FAILURES", 5))
    except Exception:
        _min_len = 2; _ai_enabled = True; _max_calls = 200; _max_consec = 5

    if not _ai_enabled:
        return True

    a_id = (a_artist or "") + (a_album or "")
    b_id = (b_artist or "") + (b_album or "")
    if len(a_id.strip()) < _min_len and len(b_id.strip()) < _min_len:
        log.debug(
            "[Dedup/AI] SKIP_INVALID_CANDIDATE — artist+album both empty; "
            "using deterministic title-match only"
        )
        # Deterministic: same normalised title → treat as same (conservative)
        return True

    # RC3: stage breaker check
    with _DEDUP_AI_LOCK:
        if _DEDUP_STAGE_AI_DISABLED:
            log.debug("[Dedup/AI] SKIP — stage AI breaker is tripped")
            return True

        # Per-run cap
        if _DEDUP_AI_CALLS_THIS_RUN >= _max_calls:
            if _DEDUP_AI_CALLS_THIS_RUN == _max_calls:
                log.warning(
                    f"[Dedup/AI] Per-run cap reached ({_max_calls}): "
                    "disabling AI dedup for remainder of run"
                )
            return True
        _DEDUP_AI_CALLS_THIS_RUN += 1

    prompt = (
        "You are a music deduplication assistant.\n\n"
        f"File A:  artist={a_artist!r}, album={a_album!r}, title={a_title!r}\n"
        f"File B:  artist={b_artist!r}, album={b_album!r}, title={b_title!r}\n\n"
        "Are these the SAME song (same recording, possibly different releases)?\n"
        'Return ONLY: {"same": true/false, "confidence": "high"|"medium"|"low", "note": "brief"}'
    )
    try:
        result = _ai_helper.call_ai(prompt, max_tokens=100)
        if not result:
            # RC3: record failure toward stage breaker
            with _DEDUP_AI_LOCK:
                _DEDUP_STAGE_AI_CONSEC_FAILS += 1
                if _DEDUP_STAGE_AI_CONSEC_FAILS >= _max_consec:
                    _DEDUP_STAGE_AI_DISABLED = True
                    log.warning(
                        f"[Dedup/Breaker] STAGE_AI_DISABLED after "
                        f"{_DEDUP_STAGE_AI_CONSEC_FAILS} consecutive provider failures"
                    )
            return True
        # Success: reset consecutive fail counter
        with _DEDUP_AI_LOCK:
            _DEDUP_STAGE_AI_CONSEC_FAILS = 0
        same = result.get("same", True)
        conf = result.get("confidence", "medium")
        if not same:
            return False
        if conf == "low":
            return False
        return bool(same)
    except Exception:
        with _DEDUP_AI_LOCK:
            _DEDUP_STAGE_AI_CONSEC_FAILS += 1
            if _DEDUP_STAGE_AI_CONSEC_FAILS >= _max_consec:
                _DEDUP_STAGE_AI_DISABLED = True
                log.warning(
                    f"[Dedup/Breaker] STAGE_AI_DISABLED (exception path) after "
                    f"{_DEDUP_STAGE_AI_CONSEC_FAILS} consecutive failures"
                )
        return True


# =============================================================================
# Execution — delete / soft-delete primitives
# =============================================================================

# MK17/MK18 FIX-F10: just-written protection set
# ROOT CAUSE: Stage-2 dedup deleted files transferred in Stage-1 before
# their metakeys were stamped. Fix: protect newly written paths for entire session.
import threading as _dd_thread
_PROTECTED_PATHS: set = set()
_PROTECTED_LOCK = _dd_thread.Lock()


def protect_path(device_path: str) -> None:
    """Register a device path as protected from dedup for this pipeline session."""
    with _PROTECTED_LOCK:
        _PROTECTED_PATHS.add(device_path.replace("\\", "/").strip())


def clear_protected_paths() -> None:
    """Clear protection set at pipeline start (Stage 0 before any transfers)."""
    with _PROTECTED_LOCK:
        _PROTECTED_PATHS.clear()


def _load_favplaylist_protected_paths() -> set:
    """
    2026-09-04: Favorites (is_favorites) and Playlists (playlist_tracks) both
    live in the Walkman app's own DB, keyed to the specific on-device file
    (tracks._id). If dedup deletes/moves that exact file, the DB row gets
    garbage-collected on the next library scan (deleteUnlinkedDBEntry()) and
    a surviving duplicate rescans as a brand-new row with no favorite/
    playlist state -- silently losing work that (for playlists) took a
    genuine fix+rebuild to get right, and (for favorites) took ~17-19h of
    UI automation. See project_walkman_hires_player_playlists_favorites.md.

    Reads favorites_playlists_plan.json (if present) and returns every
    resolved favorite's and every resolved playlist-track's device path, in
    the same "storage/parts/filename" format DedupCandidate.human_path and
    _hard_delete's protection check use.
    """
    # mtime-keyed cache: audit.py calls this per-file (~9000x), and re-parsing
    # a ~2MB JSON each time added real GC pressure. Re-read only when the plan
    # file actually changes.
    global _favplaylist_prot_cache
    plan_path = getattr(config, "BASE_DIR", Path(".")) / "favorites_playlists_plan.json"
    try:
        _mt = plan_path.stat().st_mtime if plan_path.exists() else 0.0
    except OSError:
        _mt = 0.0
    _cached = globals().get("_favplaylist_prot_cache")
    if _cached is not None and _cached[0] == _mt:
        return _cached[1]

    protected: set = set()
    try:
        if not plan_path.exists():
            _favplaylist_prot_cache = (_mt, protected)
            return protected
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        entries = list(data.get("favorites", {}).get("resolved", []))
        for pl in data.get("playlists", {}).values():
            entries.extend(pl.get("resolved", []))
        for e in entries:
            d = e.get("device")
            if not d or not d.get("filename"):
                continue
            path = f"{d.get('storage', '')}/{'/'.join(d.get('parts', []))}/{d['filename']}"
            protected.add(path.replace("\\", "/").strip())
    except Exception as _e:
        log.debug(f"[Dedup] favorites/playlists protection load skipped: {_e}")
    _favplaylist_prot_cache = (_mt, protected)
    return protected


_favplaylist_prot_cache = None


def protect_favorited_and_playlisted_paths() -> int:
    """
    Seed the session protection set from favorites_playlists_plan.json.
    Idempotent and additive (does not clear existing protections) -- safe
    to call from both build_plan() and execute(), so protection holds
    whether a plan is built fresh or loaded from a saved --from-log run in
    a separate process. Returns the number of paths protected.
    """
    paths = _load_favplaylist_protected_paths()
    for p in paths:
        protect_path(p)
    if paths:
        log.info(f"[Dedup] Protected {len(paths)} favorited/playlisted path(s) "
                 f"from favorites_playlists_plan.json")
    return len(paths)


def _hard_delete(storage: str, parts: list, filename: str) -> bool:
    """
    Unconditional delete of a file on the device.  Returns True on success.

    MK10 ISSUE-F: trigger Android MediaScanner after every successful delete
    so the Walkman's MediaStore (and third-party apps) immediately reflect the
    removal.  Sony's own Music app requires a manual DB rebuild regardless, but
    this at least keeps the MediaStore consistent.
    """
    # MK17/MK18 FIX-F10: refuse delete if path is protected (just-written this session)
    _norm_path = str(storage) + "/" + "/".join(str(p) for p in parts) + "/" + str(filename)
    _norm_path = _norm_path.replace("\\", "/").strip()
    with _PROTECTED_LOCK:
        if _norm_path in _PROTECTED_PATHS:
            log.info(f"  [Dedup] PROTECTED (just-written): {_norm_path!r} — skip delete")
            return False

    deleted_path: str | None = None
    try:
        import adb_transfer as _adb
        if getattr(_adb, "device_is_connected", lambda _n: False)(""):
            storage_path = _adb._get_storage_path(storage)
            folder = storage_path + "/" + "/".join(parts) if parts else storage_path
            remote = folder + "/" + filename
            # MK37/S3: idempotent destructive op — replay-safe via UUID
            try:
                from mk37_wiring import idempotent_destructive_op
                _idem_result = idempotent_destructive_op(
                    path=remote, action="delete",
                    fn=lambda: _adb._adb_cmd(
                        ["shell", "sh", "-c", "rm -f " + _adb._shell_quote(remote)],
                        check=False,
                    ),
                )
                if _idem_result.get("duplicate"):
                    log.info(f"  [MK37/S3] dedup delete already executed for {filename!r}")
            except ImportError:
                _adb._adb_cmd(
                    ["shell", "sh", "-c", "rm -f " + _adb._shell_quote(remote)],
                    check=False,
                )
            deleted_path = remote
            # MK10 ISSUE-F: trigger MediaScanner after delete
            try:
                from walkman_sd_migrate import trigger_media_scanner
                trigger_media_scanner([remote])
            except Exception as _me:
                log.debug(f"  [Dedup/hard] MediaScanner trigger skipped: {_me}")

            # MK14 FIX-D: Remove orphan empty parent dir left by deletion.
            # 2,912 deletions in Stage 0 left empty album dirs → 160,706:3,387 ratio.
            # rmdir only removes EMPTY dirs — safe, no data loss risk.
            try:
                parent = remote.rsplit("/", 1)[0] if "/" in remote else ""
                if parent and parent not in ("/sdcard/Music", "/storage/emulated/0/Music"):
                    _adb._adb_cmd(
                        ["shell", "sh", "-c",
                         f"rmdir {_adb._shell_quote(parent)} 2>/dev/null || true"],
                        check=False,
                    )
            except Exception as _ode:
                log.debug(f"  [Dedup/hard] orphan rmdir skipped: {_ode}")

            return True
    except Exception as e:
        log.debug(f"  [Dedup/hard] ADB delete failed: {e}")

    if mtp.COM_AVAILABLE:
        # Prefer subprocess (no dialog) over in-process InvokeVerb
        try:
            import wpd_transfer as _wpd
            _wpd.delete_file_ps(
                device_name  = config.DEVICE_NAME,
                storage_name = storage,
                dest_parts   = parts,
                filename     = filename,
            )
            return True
        except ImportError:
            pass
        except Exception as _wpd_e:
            log.debug(f"  [Dedup/hard] wpd_transfer delete failed: {_wpd_e}")

        try:
            import mtp_transfer_com as _com
            storage_root = _com._get_storage_root(config.DEVICE_NAME, storage)
            folder = _com._navigate(storage_root, parts, create=False)
            if folder is None:
                return False
            item = _com._find_remote_item(folder, filename)
            if item is None:
                return False
            item.InvokeVerb("delete")   # last resort — dialog_watcher handles it
            time.sleep(0.3)
            return True
        except Exception as e:
            log.warning(f"  [Dedup/hard] COM delete failed: {e}")

    return False


def _soft_delete(storage: str, parts: list, filename: str,
                 trash_folder: str = ".deleted") -> bool:
    """
    Move a file to <storage>/<MUSIC_ROOT>/<trash_folder>/<original-subpath>/<filename>.
    Returns True on success.  Preserves the folder hierarchy underneath so the
    user can see where each file came from in the backtrack review.

    MK22 B3.2 CORRECTED: if _INTERNAL_REDIRECT_TO_SD is set (internal storage full),
    and the file is on internal storage, redirect the .deleted/ move to SD card instead.
    This prevents size=0 COM placeholder failures that caused the LOG_5_5_26 dialog storm.
    """
    # 2026-09-04: same protection _hard_delete already had (MK17/18 FIX-F10) --
    # _soft_delete had never gotten it, which mattered once favorites/playlists
    # started being tracked, since a move to .deleted/ is just as destructive
    # to the app's DB-side state as an outright delete (the file leaves its
    # original path either way).
    _norm_path = str(storage) + "/" + "/".join(str(p) for p in parts) + "/" + str(filename)
    _norm_path = _norm_path.replace("\\", "/").strip()
    with _PROTECTED_LOCK:
        if _norm_path in _PROTECTED_PATHS:
            log.info(f"  [Dedup] PROTECTED (just-written): {_norm_path!r} — skip move")
            return False

    music_root = getattr(config, "MUSIC_ROOT", "Music")

    # B3.2 redirect: when internal is full, .deleted/ goes to SD card
    try:
        import config as _cfg_soft
        if (
            getattr(_cfg_soft, "INTERNAL_REDIRECT_ACTIVE", False)
            and "internal" in storage.lower()
        ):
            _write_storage = getattr(_cfg_soft, "WRITE_STORAGE", "SD card")
            log.debug(
                f"  [Dedup/C1] Redirecting .deleted/ for {filename!r} "
                f"from '{storage}' to '{_write_storage}' (internal full, "
                f"config.INTERNAL_REDIRECT_ACTIVE=True)"
            )
            storage = _write_storage
    except Exception:
        pass
    rel_parts = list(parts)
    if rel_parts and rel_parts[0].lower() == music_root.lower():
        rel_parts = rel_parts[1:]
    dest_parts = [music_root, trash_folder] + rel_parts

    try:
        import adb_transfer as _adb
        if getattr(_adb, "device_is_connected", lambda _n: False)(""):
            storage_path = _adb._get_storage_path(storage)
            src_folder = storage_path + "/" + "/".join(parts) if parts else storage_path
            src = src_folder + "/" + filename

            dest_folder = storage_path + "/" + "/".join(dest_parts)
            dest = dest_folder + "/" + filename

            _adb._mkdir_p(dest_folder)
            _adb._adb_cmd(
                ["shell", "sh", "-c",
                 f"mv {_adb._shell_quote(src)} {_adb._shell_quote(dest)}"],
                check=True,
            )
            return True
    except Exception as e:
        log.debug(f"  [Dedup/soft] ADB mv failed: {e}")

    if mtp.COM_AVAILABLE:
        try:
            import mtp_transfer_com as _com
            import tempfile

            with tempfile.TemporaryDirectory() as tmpd:
                local = Path(tmpd) / filename
                ok = _com.read_file_from_device(
                    src_parts=parts, filename=filename, dest_local=local,
                    device_name=config.DEVICE_NAME, storage_name=storage,
                )
                if not ok:
                    return False
                # P4 CTO fix: Pre-delete stale dest to prevent CopyHere no-op.
                # If a previous attempt left a 0-byte placeholder at the destination,
                # CopyHere silently "succeeds" without writing — pre-delete prevents this.
                try:
                    import adb_transfer as _adb_p4
                    if getattr(_adb_p4, "device_is_connected", lambda _n: False)(""):
                        _storage_path = _adb_p4._get_storage_path(storage)
                        _dest_abs = (
                            _storage_path + "/" + "/".join(dest_parts) + "/" + filename
                        )
                        # 2026-07-25: _adb_cmd has no `capture` parameter — this
                        # call raised TypeError on every invocation, silently
                        # swallowed by the except below, so the pre-delete
                        # safety step never actually ran. Fixed.
                        _adb_p4._adb_cmd(
                            ["shell", f"rm -f '{_dest_abs}'"], check=False
                        )
                except Exception as _p4e:
                    pass  # non-fatal: proceed with copy even if pre-delete fails

                # MK37/S3a (board ticket P0-3): TRANSACTIONAL DEDUP.
                # Mandate: COPY → VERIFY → COMMIT → DELETE. Never DELETE BEFORE VERIFY.
                # Emits DEDUP_TRANSACTION_COMMITTED marker on success.
                _device_relpath = "/".join(dest_parts + [filename])

                def _do_copy_and_verify():
                    """COPY + VERIFY phase. Returns dict with 'completed' set per quorum."""
                    try:
                        from mk37_wiring import transfer_via_legacy_with_quorum
                        return transfer_via_legacy_with_quorum(
                            # 2026-07-17: routed through mtp.copy_file_to_device (was
                            # _com.copy_file_to_device directly) so this trash-folder
                            # write gets the same LocalSend-first / ADB / COM routing
                            # as every other pipeline write, instead of silently
                            # skipping straight to COM. Signature-compatible drop-in
                            # (both take src/dest_parts/device_name/storage_name/
                            # overwrite as kwargs).
                            mtp.copy_file_to_device,
                            src=local, dest_parts=dest_parts + [filename],
                            device_name=config.DEVICE_NAME, storage_name=storage,
                            overwrite=True,
                            source_local_path=str(local),
                            device_relpath=_device_relpath,
                        )
                    except ImportError:
                        # No MK37; fall back to legacy semantics
                        mtp.copy_file_to_device(
                            src=local, dest_parts=dest_parts + [filename],
                            device_name=config.DEVICE_NAME, storage_name=storage,
                            overwrite=True,
                        )
                        return {"completed": True, "accepted": True,
                                "effective_tier": "LEGACY_NO_QUORUM"}

                def _do_delete_source():
                    """DELETE phase — only runs after COMMIT."""
                    # Try subprocess paths first (no dialog) before in-process InvokeVerb
                    try:
                        import wpd_transfer as _wpd_ds
                        _wpd_ds.delete_file_ps(
                            device_name  = config.DEVICE_NAME,
                            storage_name = storage,
                            dest_parts   = parts,
                            filename     = filename,
                        )
                        return
                    except ImportError:
                        pass
                    except Exception:
                        pass
                    storage_root = _com._get_storage_root(config.DEVICE_NAME, storage)
                    folder = _com._navigate(storage_root, parts, create=False)
                    if folder is not None:
                        item = _com._find_remote_item(folder, filename)
                        if item is not None:
                            item.InvokeVerb("delete")   # last resort

                try:
                    from mk37_wiring import transactional_dedup
                    _txn = transactional_dedup(
                        source_local_path=str(local),
                        device_relpath=_device_relpath,
                        transfer_fn=_do_copy_and_verify,
                        delete_source_fn=_do_delete_source,
                    )
                    if not _txn["committed"]:
                        log.warning(
                            f"  [MK37/S3a/Dedup] DEDUP_ABORT_PRE_VERIFY for "
                            f"{filename!r}: tier={_txn.get('effective_tier')} "
                            f"reason={_txn.get('reason')} — source preserved"
                        )
                        return False
                    return True
                except ImportError:
                    # MK37 not installed — fall back to legacy in-order pattern
                    _result = _do_copy_and_verify()
                    if not _result.get("completed"):
                        return False
                    _do_delete_source()
                    return True
        except Exception as e:
            log.warning(f"  [Dedup/soft] COM move failed: {e}")

    return False


# =============================================================================
# Three detection passes
# =============================================================================

def _pass_zero_length(files: list, size_lookup: Callable,
                      small_threshold_bytes: int) -> list:
    """
    Detect files whose on-device size is below threshold.
    MK19 RC4: resource throttle checkpoint every DEDUP_SCAN_THROTTLE_INTERVAL files.
    """
    # RC4: load throttle interval from config
    try:
        _throttle_n = int(getattr(config, "DEDUP_SCAN_THROTTLE_INTERVAL", 500))
    except Exception:
        _throttle_n = 500

    out = []
    _unknown = 0
    for _scan_idx, entry in enumerate(files):
        # RC4: throttle checkpoint — call wait_if_throttled every N files
        if _throttle_n > 0 and _scan_idx > 0 and _scan_idx % _throttle_n == 0:
            try:
                import resource_monitor as _rm
                _rm.wait_if_throttled(timeout=5.0)
                import gc as _gc; _gc.collect()
                log.debug(f"[ResourceMonitor] THROTTLE_APPLIED — checkpoint at file {_scan_idx}")
            except Exception:
                pass

        size = size_lookup(entry)
        if size < 0:
            _unknown += 1
            continue
        if size < small_threshold_bytes:
            out.append(DedupCandidate(
                storage=entry["storage"],
                parts=list(entry["parts"]),
                filename=entry["filename"],
                ext=entry.get("ext", Path(entry["filename"]).suffix.lower()),
                size=size,
                reason="zero_length" if size < 1 else "small_file",
                superior_path="(corrupt — no replacement needed)",
            ))

    # 2026-07-26: make the silent-blind-spot failure mode loud, matching
    # build_plan()'s existing "scan returned ZERO audio files" precedent —
    # a run where most/all size lookups came back unknown (both ADB and COM
    # failed) must not look identical to "0 candidates, library is clean."
    if files and _unknown / len(files) > 0.5:
        log.warning(
            f"[Dedup/pass1] Could not determine size for {_unknown}/{len(files)} "
            f"file(s) — both ADB and COM lookups failed. Corrupt-file detection is "
            f"UNRELIABLE this run, not necessarily a clean library. Will retry "
            f"next run once the device is healthy."
        )
    return out


# MK14 FIX-F: Corrected codec hierarchy per CTO directive.
# BEFORE: FLAC > MP3 > AAC > M4A (wrong — was deleting M4A and keeping MP3)
# AFTER:  FLAC > M4A > AAC > OGG > OPUS > MP3 > WMA > WAV
# This means: when M4A+MP3 exist, M4A is kept (higher bitrate AAC container)
# and MP3 is flagged for deletion. 1,794 duplicate groups = ~14.4 GB recoverable.
_FORMAT_PREFERENCE = [".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp3", ".wma", ".wav"]


def _format_rank(ext: str) -> int:
    """Lower index = better (more preferred) format."""
    try:
        return _FORMAT_PREFERENCE.index(ext.lower())
    except ValueError:
        return len(_FORMAT_PREFERENCE)


def _pass_format_dominance(files: list, already_flagged: set,
                           match_mode: str = "artist_title_album",
                           use_ai: bool = True) -> list:
    """
    Group by canonical key.  Within each group, if a better-format file
    exists, flag the inferior-format files for deletion.
    """
    # MK39.1 L2 / Council D4 — strict mode (config + env opt-in/opt-out)
    # MK39.1 L3 / Council Unattended Operations Review 2026-05-10:
    # Default is now True (Council-endorsed safe-default profile). Env
    # override accepts both directions:
    #   DEDUP_STRICT_MODE=1 / true / yes  → force ON
    #   DEDUP_STRICT_MODE=0 / false / no  → force OFF (for manual periodic cleanup)
    try:
        import config as _cfg_l2
        strict_mode = bool(getattr(_cfg_l2, "DEDUP_STRICT_MODE", True))
        threshold = float(getattr(_cfg_l2, "DEDUP_TITLE_ONLY_MIN_OVERLAP", 0.6))
    except (ImportError, ValueError, TypeError):
        strict_mode = True
        threshold = 0.6
    _env_override = os.environ.get("DEDUP_STRICT_MODE", "").strip().lower()
    if _env_override in ("1", "true", "yes", "on"):
        strict_mode = True
    elif _env_override in ("0", "false", "no", "off"):
        strict_mode = False

    # MK39.1 L3.5 / Council Unattended Operations Review 2026-05-10:
    # One-line INFO banner so the operator can confirm which safety
    # profile is active on each run. No interaction required — purely
    # informational for unattended overnight syncs.
    try:
        import config as _cfg_l3
        _dedup_mode = getattr(_cfg_l3, "DEDUP_MODE", "active")
    except ImportError:
        _dedup_mode = "active"
    log.info(
        f"  [Dedup/L3] UNATTENDED_PROFILE mode={_dedup_mode} "
        f"strict_mode={strict_mode} threshold={threshold:.2f} "
        f"(Council unattended-default profile 2026-05-10). "
        f"Override via DEDUP_STRICT_MODE env (0/1) or config.py."
    )

    # MK39.1 L2 / Council D3 — counters for end-of-pass observability
    _title_only_survived = 0
    _title_only_rejected = 0
    _strict_skipped = 0

    key_entries: dict = defaultdict(list)
    key_strength: dict = {}

    for entry in files:
        ident = (entry["storage"], tuple(entry["parts"]), entry["filename"])
        if ident in already_flagged:
            continue
        keys = _track_keys(entry["parts"], entry["filename"])
        if not keys:
            continue
        e = dict(entry)
        e["keys"] = keys
        e["ext"] = e.get("ext") or Path(e["filename"]).suffix.lower()

        for k in keys:
            strength = "rich" if len(k) == 3 else ("artist_or_album" if len(k) == 2 else "title_only")
            if k not in key_strength or strength == "rich":
                key_strength[k] = strength
            key_entries[k].append(e)

    flagged: list = []
    ai_cache: dict = {}

    for key, entries in key_entries.items():
        if len(entries) < 2:
            continue

        by_ext: dict = defaultdict(list)
        for e in entries:
            by_ext[e["ext"]].append(e)
        best_ext = min(by_ext.keys(), key=_format_rank)
        best_rank = _format_rank(best_ext)

        # MK39.1 L3.1 / Council D5 — Keeper-folder cleanliness ranking.
        # When multiple candidates share the best format (e.g., both a
        # 'Vol. 1' folder and a 'Vol. 1 (197' truncation-dupe folder hold
        # the same album in .m4a), prefer the NON-truncated folder so the
        # surviving keeper path in the log is the canonical one.
        # Tie-break: longer path string (more context); then full-path
        # string for determinism across runs.
        def _keeper_sort_key(_e: dict) -> tuple:
            folder = "/".join(_e.get("parts", []))
            # _strip_trailing_truncation is the L1.4 helper; if folder ==
            # its stripped form, no truncation detected → 0 (preferred).
            try:
                stripped = _strip_trailing_truncation(folder)
                trunc_flag = 0 if stripped == folder else 1
            except Exception:
                trunc_flag = 0
            return (trunc_flag, -len(folder), folder, _e.get("filename", ""))
        by_ext[best_ext].sort(key=_keeper_sort_key)

        for e in entries:
            if _format_rank(e["ext"]) <= best_rank:
                continue
            ident = (e["storage"], tuple(e["parts"]), e["filename"])
            if ident in already_flagged:
                continue
            if len(entries) - 1 <= 0:
                continue

            strength = key_strength.get(key, "title_only")
            keep = by_ext[best_ext][0]

            # MK22 B2.1: Track number conflict guard.
            # ROOT CAUSE (LOG_5_5_26): Joey Bada$$ 1999 album had "Daily Routine"
            # stored as track 01 (MP3) and track 10 (M4A) — different track numbers
            # for the same title. Dedup went in circles trying to resolve this.
            # Fix: before deleting, check if candidates share title but differ in
            # track number. If TRACK_NUMBER_CONFLICT_GUARD=True, defer to report-only.
            try:
                import config as _cfg_b21
                if getattr(_cfg_b21, "TRACK_NUMBER_CONFLICT_GUARD", True):
                    _tracknums = set()
                    for _e in entries:
                        _tn = _e.get("track_number") or _e.get("disc_track") or ""
                        if str(_tn).strip(): _tracknums.add(str(_tn).strip())
                    if len(_tracknums) > 1:
                        log.warning(
                            f"  [Dedup/B2.1] TRACK_NUMBER_CONFLICT — "
                            f"'{key}' has conflicting track numbers {_tracknums}. "
                            f"Deferring to report-only to prevent wrong-file deletion."
                        )
                        for _e in entries[1:]:
                            plan.candidates.append(DedupCandidate(
                                storage=_e.get("storage",""),
                                parts=_e.get("parts",[]),
                                filename=_e.get("filename",""),
                                reason="track_number_conflict_deferred",
                                keep_path="",
                                confidence=0.0,
                            ))
                        continue
            except Exception as _e_b21:
                pass

            if strength == "title_only" and use_ai:
                cache_key = (e["filename"].lower(), keep["filename"].lower())
                if cache_key not in ai_cache:
                    ai_cache[cache_key] = _ai_same_track(
                        "", "", _parse_filename(e["filename"])[1],
                        "", "", _parse_filename(keep["filename"])[1],
                    )
                if not ai_cache[cache_key]:
                    log.info(f"  [Dedup/fmt] AI rejected title-only match: "
                             f"{e['filename']!r} vs {keep['filename']!r}")
                    continue

            # MK39.1 L2 / Council D4 — strict mode: only rich (artist+album+title)
            # matches survive. Skip everything else; operator opted in for safety.
            if strict_mode and strength != "rich":
                _strict_skipped += 1
                log.debug(f"  [Dedup/L2] STRICT_MODE skip: '{e['filename']}' "
                          f"(strength={strength})")
                continue

            # MK39.1 L2 / Council D1 — title-only fallback gate.
            # LOG_10_5_26 showed Oliver Nelson 'Stolen Moments' (1961) being
            # paired with Ahmad Jamal Trio's COVER, and Pharrell 'Best Friend'
            # being paired with Brandy 'Best Friend' — both data-loss-risk
            # false positives from title-only matching. Council mandated
            # Jaccard token-overlap gate (≥50% on EITHER artist or album).
            if strength == "title_only":
                # Parse artist/album from BOTH candidates' folder context
                a_keys = _track_keys(e["parts"], e["filename"])
                k_keys = _track_keys(keep["parts"], keep["filename"])
                # Richest key gives us (artist, album, title)
                a_artist, a_album = "", ""
                k_artist, k_album = "", ""
                for _k in a_keys:
                    if len(_k) == 3: a_artist, a_album, _ = _k; break
                    if len(_k) == 2 and not a_artist:
                        # 2-tuple is (artist|album, title) — ambiguous,
                        # so re-parse from folder for richer context.
                        pass
                for _k in k_keys:
                    if len(_k) == 3: k_artist, k_album, _ = _k; break

                # Fall back to direct folder parse if no rich key available
                if not a_artist and not a_album:
                    a_artist, a_album = _parse_folder_artist_album(e["parts"])
                if not k_artist and not k_album:
                    k_artist, k_album = _parse_folder_artist_album(keep["parts"])

                passes, art_jac, alb_jac = passes_title_only_gate(
                    a_artist, a_album, k_artist, k_album, threshold=threshold,
                )
                if not passes:
                    _title_only_rejected += 1
                    log.warning(
                        f"  [Dedup/L2] DEDUP_TITLE_ONLY_REJECTED — title-only "
                        f"match would have crossed artist+album boundary. "
                        f"Skipping '{e['filename']}' vs keep='{keep['filename']}' "
                        f"(artist_jaccard={art_jac:.2f} album_jaccard={alb_jac:.2f} "
                        f"threshold={threshold:.2f}). Council D1 protection."
                    )
                    continue
                _title_only_survived += 1
                log.info(
                    f"  [Dedup/L2] DEDUP_TITLE_ONLY_SURVIVED — '{e['filename']}' "
                    f"vs keep='{keep['filename']}' (artist_jaccard={art_jac:.2f} "
                    f"album_jaccard={alb_jac:.2f}). Match accepted."
                )

            superior = f"{keep['storage']}/{'/'.join(keep['parts'])}/{keep['filename']}"
            flagged.append(DedupCandidate(
                storage=e["storage"],
                parts=list(e["parts"]),
                filename=e["filename"],
                ext=e["ext"],
                size=-1,
                reason="inferior_format",
                superior_path=superior,
            ))
            already_flagged.add(ident)

    # MK39.1 L2 / Council D3 — end-of-pass summary at WARNING (high visibility).
    if _title_only_survived or _title_only_rejected or _strict_skipped:
        log.warning(
            f"[Dedup/L2] DEDUP_TITLE_ONLY_SUMMARY survived={_title_only_survived} "
            f"rejected={_title_only_rejected} strict_skipped={_strict_skipped} "
            f"threshold={threshold:.2f} strict_mode={strict_mode}. "
            f"Council D1+D3+D4 protection active."
        )

    return flagged


_HEADER_PULL_BYTES = 1_500_000  # 1.5MB — covers ID3v2 / FLAC-STREAMINFO+
                                 # VORBIS_COMMENT for the overwhelming
                                 # majority of tracks (see read_file_header
                                 # _from_device docstring in adb_transfer.py).


_duration_suspects_mem: set | None = None


def _load_duration_suspects() -> set:
    """
    2026-09-05: dedup's metadata-completeness scoring covers tag/cover-art
    completeness fine, but its own "duration sanity" check is only >=30s --
    that would happily score a full point for e.g. Brent Faiyaz's on-device
    "ROLLING STONE" (confirmed = The Weeknd's real track, 230s) since 230s
    passes the sanity floor even though the content is entirely wrong. If a
    duplicate SET ever contains both a correctly-labeled copy and a
    mislabeled one, scoring needs to know that -- not just "is this present
    and non-trivial" but "does duration_verify.py's sweep consider this
    exact file a suspect at all". Cached in-process; reloaded fresh per
    dedup run (the file only changes between runs, not mid-run).
    """
    global _duration_suspects_mem
    if _duration_suspects_mem is not None:
        return _duration_suspects_mem
    suspects: set = set()
    try:
        path = getattr(config, "BASE_DIR", Path(".")) / "duration_verify_report.json"
        if path.exists():
            report = json.loads(path.read_text(encoding="utf-8"))
            for e in report:
                human_path = f"{e['storage']}/{'/'.join(e['parts'])}/{e['filename']}"
                suspects.add(human_path.replace("\\", "/").strip())
    except Exception as ex:
        log.debug(f"[Dedup] duration-suspect load skipped: {ex}")
    _duration_suspects_mem = suspects
    return suspects


def is_duration_suspect(storage: str, parts: list, filename: str) -> bool:
    """True if duration_verify.py's sweep flagged this exact file."""
    human_path = f"{storage}/{'/'.join(parts)}/{filename}".replace("\\", "/").strip()
    return human_path in _load_duration_suspects()


def device_metadata_score_lookup(entry: dict) -> int:
    """
    Score a device file's tag completeness (0-10) for Pass 3.
    The score_lookup callable Pass 3 (_pass_metadata_completeness) needs —
    previously only ever built inline in main()'s --with-metadata CLI branch,
    so the pass never ran from any automatic pipeline hook. Extracted here
    so both the CLI and prune_during_audit() share one implementation.

    2026-08-27: was a full `adb pull` per candidate — observed live taking
    ~15s/file and making Pass 3 the dominant cost of a scan-duplicates run
    (multi-hour, for 20-30MB FLACs, just to read embedded tags). Metadata
    blocks live at the START of the file by format spec for FLAC/MP3, so a
    partial header pull covers scoring for the large majority of files at
    a fraction of the I/O. M4A/MP4 can place its moov atom at the file's
    END on non-"faststart" encodes — mutagen then can't parse the
    truncated header, so THAT SPECIFIC file falls back to a full pull
    rather than being silently mis-scored as 0. Duration is intentionally
    dropped from the header-scored path (see _score_tags_from_pull's
    skip_duration) since a truncated local file makes CBR-MP3 duration
    estimation wrong.
    """
    # 2026-09-05: check duration_verify.py's suspect list BEFORE doing any
    # header-pull work at all -- a file flagged there is known-wrong-content
    # (or credibly suspected), so it should never win a metadata-completeness
    # comparison against an unflagged rival regardless of how complete its
    # tags otherwise look. Scored at -100 rather than 0 so it also loses to
    # a rival that itself scored 0 (e.g. an unreadable header) -- being
    # confirmed-wrong-content is worse than being merely unscored.
    if is_duration_suspect(entry["storage"], entry["parts"], entry["filename"]):
        return -100

    import tempfile
    with tempfile.TemporaryDirectory() as tmpd:
        local = Path(tmpd) / entry["filename"]
        ok = mtp.read_file_header_from_device(
            src_parts=entry["parts"], filename=entry["filename"],
            dest_local=local, device_name=config.DEVICE_NAME,
            storage_name=entry["storage"], max_bytes=_HEADER_PULL_BYTES,
        )
        if not ok:
            return 0

        score, details = _score_tags_from_pull(local, skip_duration=True)
        if not details.get("error"):
            return score

        # Header pull wasn't parseable (typically M4A with a trailing moov
        # atom) — fall back to a full pull for just this one file rather
        # than scoring it 0 and letting it lose to a lower-quality rival.
        local.unlink(missing_ok=True)
        ok_full = mtp.read_file_from_device(
            src_parts=entry["parts"], filename=entry["filename"],
            dest_local=local, device_name=config.DEVICE_NAME,
            storage_name=entry["storage"],
        )
        if not ok_full:
            return 0
        score, _ = _score_tags_from_pull(local, skip_duration=False)
        return score


def _pass_metadata_completeness(files: list, already_flagged: set,
                                score_lookup: Optional[Callable] = None) -> list:
    """
    Within same-format groups sharing a canonical key, keep the highest-scoring
    file and flag the rest.  Requires a score_lookup; no-op without one.
    """
    if score_lookup is None:
        return []

    key_entries: dict = defaultdict(list)
    for entry in files:
        ident = (entry["storage"], tuple(entry["parts"]), entry["filename"])
        if ident in already_flagged:
            continue
        keys = _track_keys(entry["parts"], entry["filename"])
        if not keys:
            continue
        ext = entry.get("ext") or Path(entry["filename"]).suffix.lower()
        rich_key = None
        for k in keys:
            if len(k) >= 2:
                rich_key = k
                break
        if rich_key is None:
            continue
        key_entries[(ext, rich_key)].append(entry)

    # Only groups with 2+ same-extension files sharing a track key are
    # actual scoring candidates — everything else is a single occurrence
    # that never needed a device pull. Log the real workload up front and
    # check in periodically during the loop — previously this pass logged
    # nothing until fully done, so a multi-hour run gave no way to tell it
    # was working short of reaching in at the OS/process level.
    scorable_groups = {k: v for k, v in key_entries.items() if len(v) >= 2}
    total_to_score = sum(len(v) for v in scorable_groups.values())
    log.info(
        f"[Dedup/pass3] Scoring {total_to_score} file(s) across "
        f"{len(scorable_groups)} same-format candidate group(s)..."
    )

    flagged: list = []
    scored_so_far = 0
    _progress_every = 10
    _t_start = time.monotonic()
    for (ext, key), entries in scorable_groups.items():
        scored = []
        for e in entries:
            try:
                s = score_lookup(e)
            except Exception:
                s = 0
            scored.append((s, e))
            scored_so_far += 1
            if scored_so_far % _progress_every == 0 or scored_so_far == total_to_score:
                elapsed = time.monotonic() - _t_start
                log.info(
                    f"  [Dedup/pass3] scored {scored_so_far}/{total_to_score} "
                    f"file(s) ({elapsed:.0f}s elapsed)"
                )

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_entry = scored[0]
        for s, e in scored[1:]:
            if s >= best_score:
                continue
            # Require a meaningful 2-point gap — same-song files often differ
            # by a single field (e.g. disc number), which isn't enough signal
            # to warrant deletion of the "worse" one.
            if best_score - s < 2:
                continue
            ident = (e["storage"], tuple(e["parts"]), e["filename"])
            if ident in already_flagged:
                continue
            superior = (f"{best_entry['storage']}/{'/'.join(best_entry['parts'])}/"
                        f"{best_entry['filename']}")
            flagged.append(DedupCandidate(
                storage=e["storage"],
                parts=list(e["parts"]),
                filename=e["filename"],
                ext=ext,
                size=-1,
                reason="inferior_metadata",
                superior_path=superior,
                score=s,
            ))
            already_flagged.add(ident)

    return flagged


# =============================================================================
# Main DedupEngine class
# =============================================================================

class DedupEngine:
    """
    Orchestrates the three passes, executes the plan, and journals actions.
    Designed to be called from either a CLI or from the sync / reorganize
    thread hooks.  Stateless between runs except for the on-disk journal.
    """

    def __init__(self,
                 storages: Optional[list] = None,
                 match_mode: Optional[str] = None,
                 small_threshold_bytes: Optional[int] = None,
                 soft_delete_folder: Optional[str] = None,
                 journal_path: Optional[Path] = None,
                 use_ai: Optional[bool] = None):
        self.storages = storages or _default_storages()
        self.match_mode = match_mode or getattr(
            config, "DEDUP_MATCH_MODE", "artist_title_album"
        )
        self.small_threshold_bytes = (
            small_threshold_bytes
            if small_threshold_bytes is not None
            else getattr(config, "DEDUP_SMALL_FILE_THRESHOLD_BYTES", 10 * 1024)
        )
        self.soft_delete_folder = soft_delete_folder or getattr(
            config, "DEDUP_SOFT_DELETE_FOLDER", ".deleted"
        )
        self.journal_path = journal_path or (
            Path(getattr(config, "BASE_DIR", ".")) / ".dedup_log.json"
        )
        self.use_ai = (
            use_ai
            if use_ai is not None
            else getattr(config, "DEDUP_AMBIGUOUS_USE_AI", True)
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def build_plan(self,
                   files: Optional[list] = None,
                   include_metadata_check: bool = False,
                   metadata_score_lookup: Optional[Callable] = None,
                   size_lookup: Optional[Callable] = None) -> DedupPlan:
        """
        Scan the device (or use a pre-supplied file list) and produce a plan.
        Does NOT execute anything — safe to call in any mode.
        """
        plan = DedupPlan()

        # 2026-09-04: protect every favorited/playlisted file before scanning --
        # see protect_favorited_and_playlisted_paths(). Additive, so calling it
        # again in execute() (in case that runs in a separate --from-log process)
        # is safe.
        protect_favorited_and_playlisted_paths()

        # 2026-07-26: one ADB reconnect attempt before anything else — same
        # rationale as cli_runner._attempt_adb_reconnect_if_needed()
        # (mtp_transfer.ADB_AVAILABLE is only computed once, at import time;
        # this gives pass1's per-file _stat_device_file lookups their best
        # shot at the fast ADB path instead of falling straight to the raw
        # COM fallback for every one of potentially 1200+ files). Not
        # imported from cli_runner.py to avoid a circular import (that
        # module already imports this one); inlined instead, one attempt,
        # non-fatal.
        try:
            import mtp_transfer as _mtp_reconnect
            if not _mtp_reconnect.ADB_AVAILABLE:
                import adb_transfer as _adb_reconnect
                if _adb_reconnect.reconnect():
                    _mtp_reconnect.refresh_adb_availability()
        except Exception as _reconnect_err:
            log.debug(f"[Dedup] ADB reconnect attempt failed (non-fatal): {_reconnect_err}")

        # P2 CTO fix: Ensure .deleted/ folder exists before ANY move attempt.
        _trash_folder = getattr(config, "DEDUP_SOFT_DELETE_FOLDER", ".deleted")
        for _stor in dict.fromkeys([getattr(config, "WRITE_STORAGE", "SD card"),
                                    getattr(config, "SD_STORAGE", "SD card"),
                                    "Internal shared storage"]):
            _ensure_deleted_folder(_stor, _trash_folder)

        log.info("[Dedup] Scanning device audio files...")

        # Task 35 — primary path: one `ls -lR` per storage gives the full file
        # list AND every size in ~10-15s. This is what scratchpad/fast_scan.py
        # proved out after the old per-file-`stat` Pass 1 kept dying at 30-40
        # min. Feeds both `files` (when the caller didn't supply one) and the
        # size_lookup below. Falls through to the snapshot/COM/find paths only
        # if the bulk scan comes back empty (ADB down, etc.).
        _bulk_sizes: dict = {}
        try:
            _bulk_files, _bulk_sizes = _bulk_device_scan(self.storages)
        except Exception as _bse:
            log.warning(f"[Dedup] bulk ls -lR scan failed ({_bse})")
            _bulk_files, _bulk_sizes = [], {}
        if files is None and _bulk_files:
            files = _bulk_files
            log.info(f"[Dedup] using bulk ls -lR scan: {len(files)} audio file(s)")

        if files is None:
            # Fast path: reuse walkman_snapshot.json if <1 hr old (avoids long COM scan).
            # Threshold raised from 600 s → 3600 s; WPD batch scan now refreshes the
            # snapshot quickly when device is healthy, so 1 hr is still safe.
            try:
                import time as _t_snap, json as _j_snap, re as _re_snap, posixpath as _pp_snap
                _snap_path = getattr(config, "BASE_DIR", Path(".")) / "walkman_snapshot.json"
                _snap_age  = _t_snap.time() - _snap_path.stat().st_mtime if _snap_path.exists() else 9999
                if _snap_age < 3600:
                    _rows = _j_snap.loads(_snap_path.read_text(encoding="utf-8"))
                    _SD_MAP = {"sd": "SD card", "internal": "Internal shared storage"}
                    files = []
                    for _r in _rows:
                        _d = _r.get("_data") or ""
                        if not _d:
                            continue
                        _fn = _pp_snap.basename(_d)
                        _m  = _re_snap.match(r"^/storage/(?:emulated/\d+|[^/]+)/(.+)", _d)
                        if not _m:
                            continue
                        _rel   = _m.group(1)
                        _parts = _rel.split("/")[:-1]
                        _stor  = _SD_MAP.get(_r.get("storage", "sd"), "SD card")
                        files.append({"filename": _fn, "ext": _pp_snap.splitext(_fn)[1].lower(),
                                      "parts": _parts, "storage": _stor})
                    log.info(f"[Dedup] Loaded {len(files)} files from walkman_snapshot.json "
                             f"(age={_snap_age:.0f}s, skipped COM scan)")
            except Exception as _se:
                log.debug(f"[Dedup] Snapshot cache miss: {_se}")
                files = None

        if files is None:
            try:
                files = mtp.scan_device_audio(config.DEVICE_NAME, self.storages)
            except Exception as e:
                log.warning(f"[Dedup] Device scan failed — {e}.  "
                            "Skipping dedup pass.")
                plan.scan_total = 0
                plan.update_summary()
                return plan

            # MK9 ISSUE-5 FIX: when COM/MTP scan returns 0 files (common
            # when ADB is primary transport — the COM path sees no device),
            # fall back to a direct ADB find scan.  Pre-MK9 code logged
            # "0 planned, 0 moved" because it thought the library was empty.
            if not files:
                log.info("[Dedup] COM scan returned 0 files — trying ADB find fallback...")
                try:
                    import subprocess as _sp
                    import posixpath as _pp
                    _AUDIO_EXT = {".mp3",".flac",".m4a",".aac",".ogg",
                                  ".opus",".wav",".aiff",".ape",".wv",".wma",
                                  ".dsf",".dff"}
                    _adb_exe = str(getattr(config, "ADB_EXE", "adb"))
                    adb_files = []
                    for storage, subdir in self.storages:
                        is_internal = "internal" in storage.lower()
                        mount = "/storage/emulated/0" if is_internal else None
                        if not is_internal:
                            # Probe df for SD mount
                            try:
                                df_out = _sp.run(
                                    [_adb_exe, "shell", "df", "-k"],
                                    capture_output=True, text=True, timeout=20,
                                    encoding="utf-8", errors="replace",
                                ).stdout or ""
                                import re as _re
                                for m in _re.finditer(
                                    r"^\S+\s+\d+\s+\d+\s+\d+\s+\S+%\s+(/storage/[^\s]+)",
                                    df_out, _re.MULTILINE
                                ):
                                    mp = m.group(1)
                                    if not mp.startswith("/storage/emulated"):
                                        mount = mp
                                        break
                            except Exception:
                                pass
                        if not mount:
                            continue
                        scan_root = _pp.join(mount, subdir) if subdir else mount
                        try:
                            out = _sp.run(
                                [_adb_exe, "shell", f"find '{scan_root}' -type f"],
                                capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace",
                            ).stdout or ""
                            for line in out.splitlines():
                                p = line.strip()
                                ext = _pp.splitext(p)[1].lower()
                                if ext in _AUDIO_EXT:
                                    # Build a minimal dict compatible with mtp.scan_device_audio output
                                    # Storage mapping: use config names
                                    s_name = storage
                                    # parts = relative path split
                                    rel = _pp.relpath(p, mount).replace("\\", "/")
                                    parts = [x for x in rel.split("/") if x]
                                    fname = parts.pop() if parts else _pp.basename(p)
                                    if fname.startswith("DEL_"):
                                        continue
                                    adb_files.append({
                                        "storage": s_name,
                                        "parts":   parts,
                                        "filename": fname,
                                        "size":     0,  # size unknown from find; filled below
                                    })
                        except Exception as _fe:
                            log.debug(f"[Dedup/ADB] find on {scan_root}: {_fe}")

                    if adb_files:
                        _seen_adb: set = set()
                        _deduped_adb = []
                        for _f in adb_files:
                            _k = (_f["storage"], tuple(_f["parts"]), _f["filename"])
                            if _k not in _seen_adb:
                                _seen_adb.add(_k)
                                _deduped_adb.append(_f)
                        if len(_deduped_adb) < len(adb_files):
                            log.info(f"[Dedup] ADB dedup removed {len(adb_files) - len(_deduped_adb)} duplicate path(s)")
                        adb_files = _deduped_adb
                        log.info(f"[Dedup] ADB fallback found {len(adb_files)} audio files")
                        files = adb_files
                    else:
                        log.warning("[Dedup] ADB fallback also returned 0 files — "                                    "device may be disconnected or USB Debugging not enabled.")
                except Exception as _ae:
                    log.warning(f"[Dedup] ADB fallback scan failed: {_ae}")
        plan.scan_total = len(files)
        log.info(f"[Dedup] Scan: {plan.scan_total} audio file(s) across "
                 f"{len(self.storages)} storage root(s)")

        # MK17 Patch 11 (post-LOG_17 fix): warn loudly when scan returns 0.
        # LOG_17 had `Scan: 0 audio file(s) across 6 storage root(s)`
        # because ADB enumeration failed at the moment dedup ran; the
        # logs then said "Nothing to delete" which is misleading — it
        # wasn't that there were no dupes, it was that we couldn't see
        # the device at all.  297 cross-storage dupes survived as a
        # result.  This warning makes the failure mode explicit.
        if plan.scan_total == 0:
            log.warning(
                "[Dedup] Scan returned ZERO audio files across all "
                "storage roots.  This is almost certainly a device-scan "
                "failure (USB/MTP issue), not an empty library.  "
                "Cross-format and cross-storage dupes will NOT be "
                "detected this run.  Dedup will be retried on the next "
                "sync once the device is healthy."
            )

        # Skip files that already live under the soft-delete folder — we don't
        # want to dedup files that are themselves in the trash.
        trash = self.soft_delete_folder
        files = [f for f in files if trash not in f.get("parts", [])]
        files = [f for f in files if not f.get("filename", "").startswith("DEL_")]

        if size_lookup is None:
            # Task 35 — use the bulk ls -lR size map captured above; fall back
            # to a single per-file stat only for the rare miss (a file that
            # appeared between the ls -lR and now, or a parse edge case).
            if _bulk_sizes:
                log.info(f"[Dedup] size_lookup: {len(_bulk_sizes)} size(s) from "
                         "bulk ls -lR (Task 35 fast path)")
                def size_lookup(e, _m=_bulk_sizes):
                    hit = _m.get((e["storage"], tuple(e["parts"]), e["filename"]))
                    if hit is not None:
                        return hit
                    return _stat_device_file(e["storage"], e["parts"], e["filename"])
            else:
                log.warning("[Dedup] no bulk size map — Pass 1 will per-file "
                            "stat (slow; ADB may be degraded)")
                size_lookup = lambda e: _stat_device_file(
                    e["storage"], e["parts"], e["filename"]
                )

        flagged_idents: set = set()

        # Pass 1: zero-length / corrupt
        p1 = _pass_zero_length(files, size_lookup, self.small_threshold_bytes)
        for c in p1:
            flagged_idents.add((c.storage, tuple(c.parts), c.filename))
        plan.candidates.extend(p1)
        log.info(f"[Dedup/pass1] Zero-length / small-file: {len(p1)} candidate(s)")

        # Pass 2: format dominance
        p2 = _pass_format_dominance(files, flagged_idents,
                                    match_mode=self.match_mode,
                                    use_ai=self.use_ai)
        plan.candidates.extend(p2)
        log.info(f"[Dedup/pass2] Inferior-format duplicates: {len(p2)} candidate(s)")

        # Pass 3: metadata completeness (only if caller provides a scorer)
        if include_metadata_check and metadata_score_lookup is not None:
            p3 = _pass_metadata_completeness(files, flagged_idents,
                                             score_lookup=metadata_score_lookup)
            plan.candidates.extend(p3)
            log.info(f"[Dedup/pass3] Inferior-metadata duplicates: "
                     f"{len(p3)} candidate(s)")
        elif include_metadata_check:
            log.info("[Dedup/pass3] Metadata-completeness requested but "
                     "no score lookup supplied — skipping")

        plan.update_summary()
        return plan

    def execute(self, plan: DedupPlan, delete_mode: str = "hard",
                dry_run: bool = False) -> dict:
        """
        Carry out a plan.  delete_mode: "hard" or "soft".  dry_run=True logs
        everything but does no deletion.  Returns stats dict.
        """
        if delete_mode not in ("hard", "soft"):
            raise ValueError(f"delete_mode must be 'hard' or 'soft', got {delete_mode!r}")

        # 2026-09-04: re-seed here too (additive) -- covers `--from-log` runs
        # that load a plan.json and call execute() without ever calling
        # build_plan() in this process.
        protect_favorited_and_playlisted_paths()

        stats = {"planned": len(plan.candidates), "deleted": 0, "failed": 0,
                 "skipped": 0, "mode": delete_mode, "dry_run": dry_run}

        if not plan.candidates:
            log.info("[Dedup] Nothing to delete.")
            return stats

        self._write_journal(plan, delete_mode=delete_mode, dry_run=dry_run)

        # MK39.1 L1.3 — Pre-commit audit trail (Council operator-feedback path).
        # Operator's dry-run produced 4253 planned deletes in LOG_10_5_26;
        # the log was the only audit surface. Export structured JSON so
        # quarantine_review.py --dedup-plan / --dedup-plan-stats can show
        # the plan grouped + categorized (suspicious entries flagged).
        # Idempotent overwrite: each dry-run replaces the prior plan.
        try:
            self._write_dedup_plan_json(plan, delete_mode=delete_mode,
                                          dry_run=dry_run)
        except Exception as _e_plan:
            log.debug(f"  [Dedup] plan-json export skipped: "
                       f"{type(_e_plan).__name__}: {_e_plan}")

        _seen_exec: set = set()
        _deduped_cands = []
        for _c in plan.candidates:
            _ck = (_c.storage, tuple(_c.parts), _c.filename)
            if _ck not in _seen_exec:
                _seen_exec.add(_ck)
                _deduped_cands.append(_c)
        if len(_deduped_cands) < len(plan.candidates):
            log.info(f"[Dedup] Removed {len(plan.candidates) - len(_deduped_cands)} duplicate candidate(s) before execute")
        plan.candidates = _deduped_cands
        stats["planned"] = len(plan.candidates)

        mode_str = ("DRY-RUN " if dry_run else "") + delete_mode.upper()
        log.info(f"[Dedup] Executing plan ({mode_str}): "
                 f"{len(plan.candidates)} file(s) to process")

        verb = "DELETE" if delete_mode == "hard" else "MOVE-TO-TRASH"
        _dedup_attempted = 0   # P7: track for success rate check
        _dedup_succeeded = 0
        _total_cands = len(plan.candidates)
        for _idx, c in enumerate(plan.candidates, 1):
            action = "WOULD " if dry_run else ""
            log.info(f"  [Dedup {_idx}/{_total_cands}] {action}{verb}: {c.human_path}  "
                     f"(reason={c.reason}, keep={c.superior_path})")

            if dry_run:
                stats["skipped"] += 1
                continue

            if delete_mode == "hard":
                ok = _hard_delete(c.storage, c.parts, c.filename)
            else:
                ok = _soft_delete(c.storage, c.parts, c.filename,
                                  trash_folder=self.soft_delete_folder)

            if ok:
                stats["deleted"] += 1
            else:
                stats["failed"] += 1
                log.warning(f"  [Dedup] Failed to process: {c.human_path}")

        log.info(f"[Dedup] Done: {stats['deleted']} processed, "
                 f"{stats['failed']} failed, {stats['skipped']} skipped")
        return stats

    def _write_journal(self, plan: DedupPlan, *, delete_mode: str,
                       dry_run: bool) -> None:
        """Append a journal entry before execution so deletions are traceable."""
        try:
            record = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": delete_mode,
                "dry_run": dry_run,
                "match_mode": self.match_mode,
                "scan_total": plan.scan_total,
                "summary": dict(plan.summary),
                "candidates": [c.as_dict() for c in plan.candidates],
            }
            history = []
            if self.journal_path.exists():
                try:
                    history = json.loads(self.journal_path.read_text(encoding="utf-8"))
                    if not isinstance(history, list):
                        history = []
                except Exception:
                    history = []
            history.append(record)
            history = history[-50:]
            self.journal_path.write_text(
                json.dumps(history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"[Dedup] Could not write journal {self.journal_path}: {e}")

    def _write_dedup_plan_json(self, plan: "DedupPlan", *,
                                 delete_mode: str, dry_run: bool) -> None:
        """MK39.1 L1.3 — write a structured .dedup_plan.json that
        quarantine_review.py --dedup-plan / --dedup-plan-stats can consume.

        Different shape from the journal: the journal is append-only history
        (last 50 runs); this is the LATEST plan only, overwritten each run.
        Operator workflow: run dry-run → inspect .dedup_plan.json via
        quarantine_review --dedup-plan → if happy, re-run for real.

        Each candidate entry is annotated with a `category` field:
          - format_upgrade   — same path/track, just superior format on keep
          - track_renumber   — same title, different track numbers
          - truncation_dupe  — folders differ only by length suffix
          - other            — anything else (operator should inspect closely)
        """
        plan_path = Path("./.dedup_plan.json")
        entries = []
        for c in plan.candidates:
            try:
                entry = c.as_dict()
            except Exception:
                # Fallback if as_dict not available
                entry = {
                    "human_path":    getattr(c, "human_path", ""),
                    "superior_path": getattr(c, "superior_path", ""),
                    "reason":        getattr(c, "reason", ""),
                    "filename":      getattr(c, "filename", ""),
                }
            entry["category"] = _categorize_dedup_candidate(
                human_path=entry.get("human_path", ""),
                superior_path=entry.get("superior_path", ""),
                reason=entry.get("reason", ""),
            )
            entries.append(entry)

        # Stats grouped by category for at-a-glance summary
        stats_by_cat: dict = {}
        for e in entries:
            cat = e["category"]
            stats_by_cat[cat] = stats_by_cat.get(cat, 0) + 1

        payload = {
            "version":     1,
            "council_target": "MK39.1 L1.3 (Council operator-feedback 2026-05-10)",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode":         delete_mode,
            "dry_run":      dry_run,
            "match_mode":   self.match_mode,
            "scan_total":   plan.scan_total,
            "total_planned": len(entries),
            "stats_by_category": stats_by_cat,
            "_instructions": (
                "Operator pre-commit audit: review this file with "
                "quarantine_review.py --dedup-plan / --dedup-plan-stats "
                "before running the dedup for real. Suspicious categories "
                "('track_renumber', 'other') deserve close inspection — "
                "they may represent different songs with the same title."
            ),
            "candidates":   entries,
        }
        try:
            plan_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info(
                f"  [Dedup] PLAN_JSON_WRITTEN {plan_path} "
                f"({len(entries)} candidates; categories: {stats_by_cat}). "
                f"Operator: review with quarantine_review.py --dedup-plan"
            )
        except OSError as e:
            log.warning(f"[Dedup] Could not write {plan_path}: {e}")


# =============================================================================
# Public entry points for main.py thread hooks
# =============================================================================

def _default_storages() -> list:
    """Default storage roots to scan, from config.STORAGE_ROOTS."""
    return list(getattr(config, "STORAGE_ROOTS", [
        ("Internal shared storage", ""),
        ("Internal shared storage", "Music"),
        ("SD card", ""),
        ("SD card", "Music"),
    ]))


def snapshot_to_file_list(snap_path=None) -> list:
    """
    Convert walkman_snapshot.json to the file-list format DedupEngine.build_plan expects.
    Loads regardless of snapshot age — for use in deep-clean / emergency dedup runs.
    Returns [] if snapshot is missing or unparseable.
    """
    import json as _j, re as _re, posixpath as _pp
    if snap_path is None:
        snap_path = getattr(config, "BASE_DIR", Path(".")) / "walkman_snapshot.json"
    try:
        rows = _j.loads(Path(snap_path).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"[Dedup/Snapshot] Could not load {snap_path}: {e}")
        return []
    _SD_MAP = {"sd": "SD card", "internal": "Internal shared storage"}
    files = []
    for r in rows:
        d = r.get("_data") or ""
        if not d:
            continue
        fn = _pp.basename(d)
        m = _re.match(r"^/storage/(?:emulated/\d+|[^/]+)/(.+)", d)
        if not m:
            continue
        parts = m.group(1).split("/")[:-1]
        stor  = _SD_MAP.get(r.get("storage", "sd"), "SD card")
        files.append({
            "filename": fn,
            "ext":      _pp.splitext(fn)[1].lower(),
            "parts":    parts,
            "storage":  stor,
        })
    log.info(f"[Dedup/Snapshot] Loaded {len(files)} entries from {Path(snap_path).name}")
    return files


def build_delete_list_from_snapshot(snap_path=None) -> list:
    """
    Build a list of inferior/duplicate files directly from the snapshot.
    No MTP, no DedupEngine, no AI/Groq — completes in <1s.

    Detects (in priority order):
      1. Format pairs      — same artist|title, multiple formats: keep flac>wav>m4a>mp3
      2. Exact duplicates  — same artist|title|ext in multiple folders: keep one
      3. Cross-storage     — same artist|title on both SD card + Internal: keep SD copy

    Returns list of dicts: {_data, reason, ext, storage}
    """
    import json as _j, re as _re, posixpath as _pp
    from collections import defaultdict
    from pathlib import Path as _P

    if snap_path is None:
        snap_path = getattr(config, "BASE_DIR", _P(".")) / "walkman_snapshot.json"
    try:
        rows = _j.loads(_P(snap_path).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"[Dedup/Snapshot] Cannot load {snap_path}: {e}")
        return []

    FORMAT_RANK = {".flac": 5, ".wav": 4, ".m4a": 3, ".aac": 3, ".mp3": 2, ".ogg": 1}

    # Index by cache_key (artist|title)
    by_key: dict = defaultdict(list)
    for r in rows:
        key = (r.get("cache_key") or
               f"{r.get('artist','').strip()}|{r.get('title','').strip()}")
        if key and key != "|":
            by_key[key].append(r)

    to_delete: list = []
    seen: set = set()

    for key, entries in by_key.items():
        if len(entries) < 2:
            continue

        # Sort: best format first, then longest duration (most complete)
        entries.sort(key=lambda e: (
            FORMAT_RANK.get(e.get("ext", "").lower(), 0),
            int(e.get("duration") or 0),
        ), reverse=True)

        best = entries[0]
        best_path = best.get("_data", "")

        for e in entries[1:]:
            path = e.get("_data")
            if not path or path in seen:
                continue

            best_ext   = best.get("ext", "").lower()
            e_ext      = e.get("ext",  "").lower()
            best_stor  = best.get("storage", "sd")
            e_stor     = e.get("storage",    "sd")

            if FORMAT_RANK.get(e_ext, 0) < FORMAT_RANK.get(best_ext, 0):
                reason = f"inferior-format ({e_ext} < {best_ext})"
            elif e_ext == best_ext and best_stor != e_stor:
                reason = f"cross-storage dup (keeping sd-card copy)"
            elif e_ext == best_ext:
                reason = f"exact-dup of {_pp.basename(best_path)}"
            else:
                continue   # ambiguous — skip to be safe

            to_delete.append({
                "_data":   path,
                "reason":  reason,
                "ext":     e_ext,
                "storage": e_stor,
                "keep":    best_path,
            })
            seen.add(path)

    log.info(
        f"[Dedup/Snapshot] {len(to_delete)} files flagged from {len(rows)} entries "
        f"({len(by_key)} unique artist|title keys)"
    )
    return to_delete


def _is_enabled(flag_name: str, default: bool = False) -> bool:
    return bool(getattr(config, flag_name, default))


def _check_live_inventory() -> bool:
    """
    MK26 C3 fix (A3): Returns True ONLY via config.DEVICE_INVENTORY_IS_LIVE.

    REMOVED: secondary "adb devices" re-check that allowed the gate to be bypassed.
    Evidence (LOG_6_5_26): inventory=0 at startup, ADB connected 11s later.
    Old code re-checked ADB and returned True → RC2 gate passed on stale inventory
    → Stage 0 HARD DELETE ran against 0-file inventory (stale cache data).

    Now uses config.py flag (set by device_inventory.py after confirmed scan).
    config.py is already imported by all modules — no circular import risk.
    """
    try:
        import config as _cfg_rc2
        return bool(getattr(_cfg_rc2, "DEVICE_INVENTORY_IS_LIVE", False))
    except Exception:
        return False


def album_survivor_integrity_pass(
    surviving_tracks: list[dict],
    spotify_inventory: dict | None = None,
) -> dict:
    """
    MK22 FO-B: Album survivor integrity pass.
    CTO: "Compare surviving count vs expected total_tracks (where available).
    Auto-flag splinter risk and defer destructive operations for that album."

    Args:
        surviving_tracks: list of track dicts with keys: album, artist, filename, path
        spotify_inventory: dict mapping (artist, album) → total_tracks, or None

    Returns dict with:
        splinters: list of (artist, album, survivors, expected) tuples
        total_albums_checked: int
        ALBUM_SPLINTER_DETECTED: bool
    """
    import collections as _col
    _cfg = __import__("config")
    if not getattr(_cfg, "ALBUM_SURVIVOR_INTEGRITY", True):
        return {"splinters": [], "total_albums_checked": 0, "ALBUM_SPLINTER_DETECTED": False}

    # Group survivors by (artist, album)
    by_album: dict = _col.defaultdict(list)
    for t in surviving_tracks:
        artist = (t.get("artist") or "").strip()
        album  = (t.get("album") or "").strip()
        if artist or album:
            by_album[(artist, album)].append(t)

    splinters = []
    for (artist, album), tracks in by_album.items():
        survived_n = len(tracks)
        expected_n = None
        if spotify_inventory:
            expected_n = spotify_inventory.get((artist, album))

        # Splinter heuristic: single-track folder for a known multi-track album
        is_splinter = (
            survived_n == 1 and (
                (expected_n is not None and expected_n > 1)
                or album.lower() not in {"single", "ep", "promo"}
            )
        )
        if is_splinter:
            splinters.append({
                "artist":    artist,
                "album":     album,
                "survivors": survived_n,
                "expected":  expected_n,
            })

    result = {
        "splinters":               splinters,
        "total_albums_checked":    len(by_album),
        "ALBUM_SPLINTER_DETECTED": len(splinters) > 0,
    }

    if splinters:
        log.warning(
            f"[Dedup/AlbumIntegrity] ALBUM_SPLINTER_DETECTED — "
            f"{len(splinters)} album(s) with only 1 surviving track "
            f"(out of {len(by_album)} total albums checked). "
            f"Deferring further destructive operations for affected albums. "
            f"Run with ALBUM_SURVIVOR_INTEGRITY=False to override."
        )
        for s in splinters[:5]:   # log first 5 splinters
            log.info(
                f"  [Dedup/Splinter] '{s['artist']}' — '{s['album']}': "
                f"{s['survivors']} track(s) survived"
                + (f" (expected {s['expected']})" if s['expected'] else "")
            )
    else:
        log.debug(
            f"[Dedup/AlbumIntegrity] Album integrity OK — "
            f"{len(by_album)} album(s) checked, 0 splinters detected"
        )

    return result


def _ensure_deleted_folder(storage: str, trash_folder: str = ".deleted") -> bool:
    """
    P2 CTO fix (A4): Ensure the .deleted/ folder exists BEFORE any soft-delete.
    Evidence: 1,224 × "remote size is 0" — CopyHere to missing path → 0-byte stubs.
    Uses ADB mkdir (reliable) then COM FolderCreate fallback.
    """
    import os as _os_p2
    music_root = getattr(config, "MUSIC_ROOT", "Music")
    # ADB path — fastest and most reliable
    try:
        import adb_transfer as _adb_p2
        if getattr(_adb_p2, "device_is_connected", lambda _n: False)(""):
            storage_root = _adb_p2._get_storage_path(storage)
            full_path    = f"{storage_root}/{music_root}/{trash_folder}"
            _adb_p2._mkdir_p(full_path)
            # 2026-07-25: _adb_cmd has no `capture` parameter — this call
            # raised TypeError on every single invocation, silently swallowed
            # by the except Exception below, so the ADB path here ALWAYS
            # fell through to the raw, unguarded COM fallback (no subprocess
            # isolation, no timeout — the same class of hang Tasks 4-6 fixed
            # elsewhere in this project, never applied to this function).
            # Confirmed live: an isolated call to this function hung for
            # minutes with the device otherwise healthy and ADB confirmed
            # connected, immediately after this exact TypeError.
            r = _adb_p2._adb_cmd(
                ["shell", f"test -d '{full_path}' && echo 1 || echo 0"],
                check=False
            )
            # _adb_cmd returns the already-stripped stdout string directly,
            # not a CompletedProcess — `getattr(r, "stdout", ...)` on a str
            # always falls through to the "" default, so this check was
            # always false too, compounding the bug above.
            if r == "1":
                log.debug(f"  [Dedup/P2] .deleted/ confirmed via ADB: {full_path}")
                return True
    except Exception as _e_p2:
        log.debug(f"  [Dedup/P2] ADB folder check failed: {_e_p2}")
    # COM fallback — 2026-07-25: subprocess-isolated (mtp_transfer_com.
    # ensure_folder_isolated), replacing the raw, unguarded
    # _find_folder_by_name()/NewFolder() COM calls that used to live here
    # in-process with zero timeout — confirmed live to hang 90+s on this
    # device. Reuses the same _navigate(create=True) primitive
    # copy_file_to_device already relies on, just run in a supervised child
    # process with a dual-guard timeout like every other COM call site in
    # this project.
    try:
        import mtp_transfer as _mtp
        if _mtp.COM_AVAILABLE:
            import mtp_transfer_com as _com
            ok = _com.ensure_folder_isolated(
                getattr(config, "DEVICE_NAME", "WALKMAN"), storage,
                [music_root, trash_folder],
            )
            if ok:
                log.info(f"  [Dedup/P2] .deleted/ ready via COM: {storage}/{music_root}/{trash_folder}")
                return True
    except Exception as _e_p2b:
        log.debug(f"  [Dedup/P2] COM folder creation failed: {_e_p2b}")
    log.warning(f"  [Dedup/P2] Could not ensure .deleted/ for {storage!r} — "
                f"soft-delete may produce 0-byte placeholders")
    return False


def _mode_from_config() -> str:
    """Read DEDUP_MODE from config.  "report" | "active" | "off"."""
    m = str(getattr(config, "DEDUP_MODE", "report")).lower()
    if m not in ("report", "active", "off"):
        log.warning(f"[Dedup] DEDUP_MODE={m!r} is invalid — falling back to 'report'")
        return "report"
    return m


def prune_before_sync(mode: Optional[str] = None) -> dict:
    """
    Hard-delete pre-sync pruning hook.  Called from _sync_thread at start of run.

    mode override ("report" | "active" | "off") takes precedence over config.
    Default delete mode is HARD — this is the pre-sync aggressive pass that
    frees space before transfer.  Zero-length and inferior-format candidates
    only; metadata-completeness requires file pulls which we avoid at this
    stage (audit will catch those later via prune_during_audit).
    """
    effective_mode = (mode or _mode_from_config()).lower()
    if effective_mode == "off":
        log.info("[Dedup] Pre-sync pruning disabled (DEDUP_MODE=off)")
        return {"mode": "off", "planned": 0, "deleted": 0}

    if not _is_enabled("DEDUP_PRE_SYNC", default=True):
        log.info("[Dedup] Pre-sync pruning disabled (DEDUP_PRE_SYNC=False)")
        return {"mode": effective_mode, "planned": 0, "deleted": 0}

    # MK19 RC2: Live device gate.
    # ROOT CAUSE: 2026-05-04 — ADB not connected, yet HARD DELETE pass ran
    # against 27,895 files from stale MTP cache. Decisions based on stale
    # inventory can delete tracks that were added since the last scan.
    #
    # Gate logic:
    #   1. Check ADB inventory is fresh (live_inventory flag set by preflight).
    #   2. If stale: auto-downgrade to report-only UNLESS ALLOW_STALE_DEDUP=True.
    #   3. If ALLOW_STALE_DEDUP=True: proceed but log explicit WARNING.
    #
    # APPROACH SELECTION:
    #   A1 (ADB ping only): misses COM-path inventory staleness → rejected
    #   A2 (require non-zero scan): zero-device "succeeds" with cache → rejected
    #   A3 (is_live_inventory flag): checks preflight inventory freshness → SELECTED
    _live_inventory = _check_live_inventory()
    _allow_stale    = bool(getattr(config, "ALLOW_STALE_DEDUP", False))

    if not _live_inventory:
        if not _allow_stale:
            log.warning(
                "[Dedup/Gate] LIVE_DEVICE_REQUIRED — device inventory is stale. "
                "Skipping dedup entirely (no safe plan possible without live device scan). "
                "Set ALLOW_STALE_DEDUP=True in config to override (use with caution)."
            )
            return {"planned": 0, "moved": 0, "failed": 0, "mode": "skipped/stale"}
        else:
            log.warning(
                "[Dedup/Gate] ALLOW_STALE_DEDUP=True — proceeding with stale "
                "inventory. Decisions may be based on a prior scan."
            )

    log.info(f"[Dedup] === PRE-SYNC PRUNE ({effective_mode.upper()}, HARD DELETE) ===")
    engine = DedupEngine()
    plan = engine.build_plan(include_metadata_check=False)

    dry = (effective_mode == "report")
    stats = engine.execute(plan, delete_mode="hard", dry_run=dry)
    log.info(f"[Dedup] Pre-sync prune complete: {stats}")
    return stats


def prune_during_audit(mode: Optional[str] = None,
                       metadata_score_lookup: Optional[Callable] = None) -> dict:
    """
    Soft-delete pre-reorganize pruning hook.  Called from _reorganize_thread
    (and optionally _audit_thread) before reorganize moves files around.

    Uses SOFT delete — files are moved to <Music>/<DEDUP_SOFT_DELETE_FOLDER>/…
    so a human can review them before any permanent deletion.

    Includes the metadata-completeness pass when the caller supplies a
    score lookup (the audit has pulled files anyway and can reuse the data).
    """
    effective_mode = (mode or _mode_from_config()).lower()
    if effective_mode == "off":
        log.info("[Dedup] Pre-reorganize pruning disabled (DEDUP_MODE=off)")
        return {"mode": "off", "planned": 0, "deleted": 0}

    if not _is_enabled("DEDUP_PRE_REORGANIZE", default=True):
        log.info("[Dedup] Pre-reorganize pruning disabled (DEDUP_PRE_REORGANIZE=False)")
        return {"mode": effective_mode, "planned": 0, "deleted": 0}

    log.info(f"[Dedup] === PRE-REORGANIZE PRUNE ({effective_mode.upper()}, SOFT DELETE) ===")
    engine = DedupEngine()
    plan = engine.build_plan(
        include_metadata_check=(metadata_score_lookup is not None),
        metadata_score_lookup=metadata_score_lookup,
    )

    dry = (effective_mode == "report")
    stats = engine.execute(plan, delete_mode="soft", dry_run=dry)
    log.info(f"[Dedup] Pre-reorganize prune complete: {stats}")
    return stats


# =============================================================================
# Backwards-compatible CLI  (Rev 13 behaviour preserved)
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Walkman dedup — identify and remove flawed / duplicate files on the device."
    )
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete files (default: dry-run)")
    parser.add_argument("--soft", action="store_true",
                        help="Soft delete (move to .deleted/) instead of rm")
    parser.add_argument("--storage", choices=["internal", "sd", "both"],
                        default="both")
    parser.add_argument("--zero-length-only", action="store_true",
                        help="Only Pass 1 — detect zero-length / corrupt files")
    parser.add_argument("--format-only", action="store_true",
                        help="Only Pass 2 — FLAC-beats-MP3 dedup (Rev 13 default)")
    parser.add_argument("--with-metadata", action="store_true",
                        help="Include Pass 3 metadata-completeness (slow)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    mode_label = "EXECUTE" if args.execute else "DRY-RUN"
    delete_kind = "SOFT" if args.soft else "HARD"
    log.info("=" * 72)
    log.info(f"Walkman Dedup  —  {mode_label} / {delete_kind} DELETE")
    log.info("=" * 72)

    storages = [
        (name, sub) for name, sub in _default_storages()
        if (args.storage == "both"
            or (args.storage == "internal" and "internal" in name.lower())
            or (args.storage == "sd" and "sd" in name.lower()))
    ]

    engine = DedupEngine(storages=storages)

    if args.zero_length_only:
        # Task 35 — bulk ls -lR gives files + sizes in one shot.
        _bf, _bs = _bulk_device_scan(storages)
        files = _bf or mtp.scan_device_audio(config.DEVICE_NAME, storages)
        plan = DedupPlan(scan_total=len(files))
        _sl = ((lambda e: _bs.get((e["storage"], tuple(e["parts"]), e["filename"]),
                                  _stat_device_file(e["storage"], e["parts"], e["filename"])))
               if _bs else
               (lambda e: _stat_device_file(e["storage"], e["parts"], e["filename"])))
        p1 = _pass_zero_length(files, _sl, engine.small_threshold_bytes)
        plan.candidates = p1
        plan.update_summary()
    elif args.format_only:
        _bf, _bs = _bulk_device_scan(storages)
        files = _bf or mtp.scan_device_audio(config.DEVICE_NAME, storages)
        plan = DedupPlan(scan_total=len(files))
        p2 = _pass_format_dominance(files, set(), engine.match_mode,
                                    use_ai=engine.use_ai)
        plan.candidates = p2
        plan.update_summary()
    else:
        score_lookup = device_metadata_score_lookup if args.with_metadata else None

        plan = engine.build_plan(
            include_metadata_check=args.with_metadata,
            metadata_score_lookup=score_lookup,
        )

    if not plan.candidates:
        log.info("No deletion candidates found.")
        return

    log.info(f"\nPlan summary: {dict(plan.summary)}")
    stats = engine.execute(
        plan,
        delete_mode="soft" if args.soft else "hard",
        dry_run=(not args.execute),
    )
    log.info(f"\nResult: {stats}")


if __name__ == "__main__":
    main()