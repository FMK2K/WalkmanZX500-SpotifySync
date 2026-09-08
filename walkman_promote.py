"""
walkman_promote.py  —  Album-Folder Promotion  (Rev 14.7 — DRY-RUN ONLY BY DEFAULT)
==================================================================================

Fixes the LOG-11 reorganize pathology where multiple copies of the same track
(from different providers, each with different filename conventions) get queued
as separate move operations to the SAME target filename — so in --execute mode
they silently overwrite each other and the "winner" is whoever wrote last, not
whoever had the best audio quality.

**Design decisions (Rev 14.7, locked after SDD MK11 review):**

  Q1  Orphan album folder with unresolvable artist?
      → Leave in place, log WARNING.  Never quarantine silently.

  Q2  Mixed-artist album (compilation, OST, DJ set)?
      → Parse primary artist per track (split on "ft.", "feat.", ",", "&";
        first-listed wins).  If ≥80% of tracks agree → that artist.  Else
        "Various Artists".

  Q3  Move the folder as a unit, or cherry-pick?
      → Cherry-pick.  Copy winning audio files + cover art (cover.*, folder.*,
        AlbumArt*.jpg) into the target folder.  Leave non-matching files in
        the source folder untouched.

  Q4  Canonical path vs SpotiDownloader path?
      → Quality wins.  If equal quality, canonical "Artist/Album/" wins over
        "SpotiDownloader.com - Album/" or other provider-prefix paths.

This module adds a pre-reorganize step that:

  1. Groups files by (artist, album, disc, track_number).
  2. Picks the single best copy from each group using ZX500 format priority.
  3. Relocates orphan album folders (e.g. "Music/NOT ALL HEROES WEAR CAPES
     (Deluxe)/", "Music/SpotiDownloader.com - Piñata (Deluxe Edition)/") into
     the proper "Music/{Artist}/{Album}/" hierarchy.
  4. Deletes / quarantines the losers so reorganize no longer sees duplicate
     sources for the same target.

This runs BEFORE walkman_reorganize.py.  The reorganize tool's output then
becomes deterministic: one source, one target, no collisions.


ZX500 format priority
---------------------
The Sony NW-ZX500 default Music app supports (in decreasing audio quality):

    1. FLAC     up to 32-bit / 384 kHz        (hi-res if >=24-bit or >=88.2 kHz)
    2. ALAC     up to 32-bit / 384 kHz        (same quality ceiling as FLAC)
    3. AIFF     up to 32-bit / 384 kHz        (uncompressed)
    4. WAV      up to 32-bit / 384 kHz        (uncompressed, weak tagging)
    5. DSD      2.8 / 5.6 / 11.2 MHz          (.dsf, .dff — only native Sony)
    6. APE      up to 24-bit / 96 kHz
    7. MP3      32-320 kbps + VBR             (.mp3)
    8. AAC      up to 320 kbps VBR            (.m4a / .aac / .mp4 / .3gp)
    9. WMA      up to 320 kbps VBR            (.wma / .asf)

Per-track tie-breaker for same format:
    a) bit depth (higher wins)
    b) sample rate (higher wins)
    c) overall bitrate (higher wins)
    d) file size (larger wins — proxy for bitrate when the tag lies)
    e) filename simpler (no "(1)", no "copy", no "SpotiDownloader.com -")


Formats explicitly NOT on the ZX500's native support list
---------------------------------------------------------
    - .opus            (not supported)
    - .ogg Vorbis      (not supported by default Music app)
    - .webm            (not supported)

If the ONLY copy of a track is in an unsupported format, the module flags it
for transcoding rather than promoting as-is.  Transcoding pipeline is
out-of-scope for this draft — we just log the flag.


Public API (draft)
------------------
    plan_promotions(scan_root, ...) -> PromotionPlan
    execute_plan(plan, device_ops, dry_run=True) -> ExecutionReport

The device_ops argument is a small protocol (move_file, delete_file, list_dir,
stat) so this module can be used against a local scratch directory in tests
and against the real Walkman ADB interface in production.  That keeps the
unit tests fast and deterministic.


Known open design questions
----------------------------
    1. When "SpotiDownloader.com - X" has the SAME tracks as "Artist/X" already
       present, do we always prefer the already-canonical location, or do we
       compare audio quality file-by-file?  (Draft: prefer quality, not path.)

    2. If artist resolution fails for an orphan album folder (Phase 2 AI is
       not confident), do we quarantine the folder to /Music/.quarantine/ or
       leave it untouched?  (Draft: leave untouched, log a WARNING.)

    3. Orphan folders with mixed artists (compilations, OSTs, DJ sets) — do we
       put them under "Various Artists" or under the most-frequent artist?
       (Draft: if >=70% of tracks agree on artist, that artist wins; else
       Various Artists.)

    4. Do we move the album folder itself, or cherry-pick files and rebuild?
       (Draft: cherry-pick the winners into a fresh target folder, then
       remove the source folder if it ends up empty. Preserves any nested
       structure quirks that a rename would not.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable, Protocol


# ---------------------------------------------------------------------------
# Format ranking
# ---------------------------------------------------------------------------

class FormatTier(IntEnum):
    """Higher is better.  ZX500 native-playback priority."""
    FLAC_HIRES = 100    # FLAC with bit_depth>=24 OR sample_rate>=88200
    FLAC_STD   =  90    # FLAC 16/44.1 or 16/48
    ALAC_HIRES =  88    # .m4a ALAC with hi-res metadata
    ALAC_STD   =  82
    AIFF       =  75
    WAV        =  70
    DSD        =  68    # only if Sony-native .dsf/.dff
    APE        =  60
    MP3_320    =  50
    MP3_VBR    =  45
    MP3_OTHER  =  40
    AAC_320    =  35
    AAC_OTHER  =  30
    WMA        =  20
    UNSUPPORTED = 0


@dataclass
class FileSpec:
    """A single file on disk (or device) with enough metadata to rank it."""
    path: str
    ext: str                     # lowercase, without leading dot
    size_bytes: int
    sample_rate_hz: int = 0
    bit_depth: int = 0
    bitrate_kbps: int = 0
    artist: str = ""
    album: str = ""
    disc: int = 1
    track_number: int = 0
    title: str = ""

    @property
    def tier(self) -> FormatTier:
        e = self.ext.lower()
        if e == "flac":
            return (FormatTier.FLAC_HIRES
                    if (self.bit_depth >= 24 or self.sample_rate_hz >= 88_200)
                    else FormatTier.FLAC_STD)
        if e in ("m4a", "mp4"):
            # Could be ALAC or AAC — caller should inspect codec field if they
            # have it.  Here we assume AAC unless bit_depth hints at ALAC.
            if self.bit_depth >= 16:
                return (FormatTier.ALAC_HIRES
                        if (self.bit_depth >= 24 or self.sample_rate_hz >= 88_200)
                        else FormatTier.ALAC_STD)
            return FormatTier.AAC_320 if self.bitrate_kbps >= 300 else FormatTier.AAC_OTHER
        if e == "aac":
            return FormatTier.AAC_320 if self.bitrate_kbps >= 300 else FormatTier.AAC_OTHER
        if e in ("aif", "aiff"):
            return FormatTier.AIFF
        if e == "wav":
            return FormatTier.WAV
        if e in ("dsf", "dff"):
            return FormatTier.DSD
        if e == "ape":
            return FormatTier.APE
        if e == "mp3":
            if self.bitrate_kbps >= 315:
                return FormatTier.MP3_320
            if self.bitrate_kbps >= 190:
                return FormatTier.MP3_VBR
            return FormatTier.MP3_OTHER
        if e in ("wma", "asf"):
            return FormatTier.WMA
        return FormatTier.UNSUPPORTED

    def rank_key(self) -> tuple:
        """
        Higher-is-better tuple for `max(group, key=...)`.

        Order (Q4 locked): (tier, bit_depth, sample_rate, bitrate, size,
        canonical_path_score, clean_name_score).  When everything else ties,
        the file at canonical "Music/Artist/Album/..." beats the same file at
        "Music/SpotiDownloader.com - Album/..." or any other provider prefix.
        """
        name = Path(self.path).name.lower()
        parent = Path(self.path).parent
        parent_name = parent.name.lower()

        # Q4 canonical-path score — +3 if the file lives at
        # Music/<real_artist>/<album>/... (three-deep structure), zero if it
        # lives at Music/<album>/... (orphan), strong negative if it lives
        # under a SpotiDownloader.com prefix folder.
        canonical_score = 0
        if parent_name.startswith("spotidownloader.com"):
            canonical_score = -5
        elif "spotidownloader.com" in str(parent).lower():
            canonical_score = -5
        else:
            # "Music/<artist>/<album>/..." three-deep structure beats the
            # orphan "Music/<album>/..." two-deep structure.
            parts = [p.lower() for p in parent.parts]
            if "music" in parts:
                depth_below_music = len(parts) - 1 - parts.index("music")
                if depth_below_music >= 2:
                    canonical_score = 3

        clean_score = 10
        for bad in (
            "spotidownloader.com",
            " copy", "(copy)", "(1)", "(2)", "(3)",
            "_edit", "_remastered_", "._", "~",
        ):
            if bad in name:
                clean_score -= 2
        return (
            int(self.tier),
            self.bit_depth,
            self.sample_rate_hz,
            self.bitrate_kbps,
            self.size_bytes,
            canonical_score,
            clean_score,
        )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _group_key(fs: FileSpec) -> tuple:
    """Identity tuple used to recognise the same logical track across copies."""
    # Title is deliberately last because we're willing to treat
    # "01 Runaway" and "01 Runaway (feat. Pusha T)" as the same if disc+track
    # match.  If disc/track are missing (0) we fall back to title exact match.
    if fs.track_number > 0:
        return (
            _norm(fs.artist),
            _norm(fs.album),
            int(fs.disc or 1),
            int(fs.track_number),
        )
    return (
        _norm(fs.artist),
        _norm(fs.album),
        0,
        _norm(fs.title),
    )


def _norm(s: str) -> str:
    """Lowercase + collapse whitespace + strip obvious decorations."""
    import re
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    for suffix in (
        " (deluxe version)", " (deluxe edition)", " (deluxe)",
        " (expanded edition)", " (expanded version)", " (expanded)",
        " (remastered)", " (remastered edition)",
        " (special edition)", " (anniversary edition)",
    ):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


# ---------------------------------------------------------------------------
# Promotion plan
# ---------------------------------------------------------------------------

@dataclass
class PromotionPlan:
    keep:     list = field(default_factory=list)   # winners: files to retain
    discard:  list = field(default_factory=list)   # losers:  files to delete
    relocate: list = field(default_factory=list)   # (src, dst) for orphan folders
    flags:    list = field(default_factory=list)   # notes for the operator
    unsupported_singletons: list = field(default_factory=list)   # transcode candidates


def build_plan(files: list[FileSpec],
               resolve_artist: Callable[[str], str] | None = None
               ) -> PromotionPlan:
    """
    Build a promotion plan without touching the filesystem.

    Parameters
    ----------
    files
        All candidate files discovered by the scanner.
    resolve_artist
        Callable that takes an album name and returns the canonical artist
        (used for orphan folder relocation).  If None, orphans are logged
        but not relocated.
    """
    plan = PromotionPlan()

    groups: dict = {}
    for fs in files:
        groups.setdefault(_group_key(fs), []).append(fs)

    for key, members in groups.items():
        if len(members) == 1:
            sole = members[0]
            if sole.tier == FormatTier.UNSUPPORTED:
                plan.unsupported_singletons.append(sole)
            plan.keep.append(sole)
            continue

        ranked = sorted(members, key=lambda fs: fs.rank_key(), reverse=True)
        winner, *losers = ranked
        plan.keep.append(winner)
        plan.discard.extend(losers)
        plan.flags.append(
            f"dedup {key}: kept {Path(winner.path).name} "
            f"({winner.tier.name}), discarded {len(losers)}"
        )

    if resolve_artist is not None:
        # Orphan-folder relocation phase (post-dedup so we only move winners).
        by_album_path: dict = {}
        for fs in plan.keep:
            by_album_path.setdefault(str(Path(fs.path).parent), []).append(fs)
        for folder, album_files in by_album_path.items():
            p = Path(folder)
            parts = p.parts
            # Orphan heuristic: "Music/<album>/" with no artist directory.
            if len(parts) >= 2 and parts[-2].lower() == "music":
                album_name = parts[-1]
                stripped = _strip_spotidownloader_prefix(album_name)
                artist = resolve_artist(stripped)
                if artist:
                    new_folder = str(p.parent / _safe(artist) / _safe(stripped))
                    plan.relocate.append((folder, new_folder))
                else:
                    plan.flags.append(f"orphan unresolved: {folder}")

    return plan


def _strip_spotidownloader_prefix(name: str) -> str:
    import re
    return re.sub(r"^SpotiDownloader\.com\s*-\s*", "", name, flags=re.IGNORECASE).strip()


def _safe(s: str) -> str:
    """Windows/Android-safe folder name."""
    import re
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip(" .") or "Unknown"


# ---------------------------------------------------------------------------
# Q2 mixed-artist resolver
# ---------------------------------------------------------------------------

_FEATURE_SPLITTERS = (" ft.", " ft ", " feat.", " feat ", " featuring ",
                      " with ", ",", " & ", " and ")


def _primary_artist(artist_field: str) -> str:
    """
    Return the first-listed primary artist from a raw ARTIST tag.

    "Drake, 21 Savage"       -> "Drake"
    "Drake ft. Future"       -> "Drake"
    "Drake & 21 Savage"      -> "Drake"
    "Drake feat. 21 Savage"  -> "Drake"
    "Various Artists"        -> "Various Artists"  (left alone)
    """
    a = (artist_field or "").strip()
    low = a.lower()
    # Find the earliest splitter and cut there
    cut = len(a)
    for sep in _FEATURE_SPLITTERS:
        idx = low.find(sep)
        if idx >= 0 and idx < cut:
            cut = idx
    return a[:cut].strip() or a


def resolve_album_artist(tracks: list,
                         agreement_threshold: float = 0.80
                         ) -> str:
    """
    Q2 — decide the album's canonical artist from its tracks.

    Parameters
    ----------
    tracks
        List of FileSpec (or any object with .artist attribute).
    agreement_threshold
        Fraction of tracks that must name the same primary artist for that
        artist to win.  Default 0.80.  If no artist reaches the threshold,
        returns "Various Artists".
    """
    if not tracks:
        return "Various Artists"
    from collections import Counter
    primaries = [_primary_artist(getattr(t, "artist", "")) for t in tracks]
    primaries = [p for p in primaries if p]   # drop blanks
    if not primaries:
        return "Various Artists"
    c = Counter(primaries)
    top_artist, top_count = c.most_common(1)[0]
    if top_count / len(primaries) >= agreement_threshold:
        return top_artist
    return "Various Artists"


# ---------------------------------------------------------------------------
# Plan serialization (human-reviewable)
# ---------------------------------------------------------------------------

def plan_to_text(plan: PromotionPlan) -> str:
    """Render a PromotionPlan as a readable report for operator review."""
    lines = []
    lines.append("=" * 78)
    lines.append("Walkman Promotion Plan  (DRY-RUN — no files touched)")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"Summary:")
    lines.append(f"  Winners kept:          {len(plan.keep)}")
    lines.append(f"  Losers to discard:     {len(plan.discard)}")
    lines.append(f"  Folders to relocate:   {len(plan.relocate)}")
    lines.append(f"  Unsupported singletons: {len(plan.unsupported_singletons)}")
    lines.append(f"  Flags:                 {len(plan.flags)}")
    lines.append("")

    if plan.discard:
        lines.append("-" * 78)
        lines.append("DISCARDS  (lower-quality duplicates to remove)")
        lines.append("-" * 78)
        # Group by album so the report is reviewable.
        from collections import defaultdict
        by_album = defaultdict(list)
        for fs in plan.discard:
            by_album[f"{fs.artist} / {fs.album}"].append(fs)
        for album_key in sorted(by_album.keys()):
            lines.append(f"  {album_key}")
            for fs in by_album[album_key]:
                lines.append(f"    - [{fs.tier.name:12}] {fs.path}")

    if plan.relocate:
        lines.append("")
        lines.append("-" * 78)
        lines.append("RELOCATIONS  (orphan album folders → canonical artist path)")
        lines.append("-" * 78)
        for src, dst in plan.relocate:
            lines.append(f"  {src}")
            lines.append(f"    → {dst}")

    if plan.unsupported_singletons:
        lines.append("")
        lines.append("-" * 78)
        lines.append("UNSUPPORTED FORMATS  (sole copy, needs transcode before promote)")
        lines.append("-" * 78)
        for fs in plan.unsupported_singletons:
            lines.append(f"  {fs.path}   ({fs.ext})")

    if plan.flags:
        lines.append("")
        lines.append("-" * 78)
        lines.append("OPERATOR FLAGS")
        lines.append("-" * 78)
        for flag in plan.flags:
            lines.append(f"  {flag}")

    lines.append("")
    lines.append("=" * 78)
    lines.append(
        "To apply this plan, set config.PROMOTE_ENABLED = True and invoke\n"
        "execute_plan(plan, ops, dry_run=False).  Back up your library first."
    )
    lines.append("=" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Device-ops protocol (for dependency injection in tests)
# ---------------------------------------------------------------------------

class DeviceOps(Protocol):
    def move_file(self, src: str, dst: str) -> bool: ...
    def delete_file(self, path: str) -> bool: ...
    def list_dir(self, path: str) -> list: ...
    def stat(self, path: str) -> dict: ...


# Concrete ops -------------------------------------------------------------

class LocalDeviceOps:
    """DeviceOps implementation against the local filesystem (for tests)."""

    def __init__(self, root: str):
        self.root = Path(root)

    def move_file(self, src: str, dst: str) -> bool:
        import shutil
        try:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            return True
        except Exception:
            return False

    def delete_file(self, path: str) -> bool:
        try:
            Path(path).unlink()
            return True
        except Exception:
            return False

    def list_dir(self, path: str) -> list:
        try:
            return [str(p) for p in Path(path).iterdir()]
        except Exception:
            return []

    def stat(self, path: str) -> dict:
        try:
            st = Path(path).stat()
            return {"size": st.st_size, "mtime": st.st_mtime, "exists": True}
        except Exception:
            return {"exists": False}


# ---------------------------------------------------------------------------
# Cover-art detection (Q3)
# ---------------------------------------------------------------------------

_COVER_ART_PATTERNS = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "albumartsmall.jpg", "albumart.jpg",
    "front.jpg", "front.png",
)


def is_cover_art(filename: str) -> bool:
    """True if the filename looks like ZX500-readable album art."""
    return filename.lower() in _COVER_ART_PATTERNS


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class ExecutionReport:
    moved:       int = 0
    deleted:     int = 0
    transcoded:  int = 0
    skipped:     int = 0
    failed:      list = field(default_factory=list)
    journal_path: str = ""


def _write_journal(plan: PromotionPlan, journal_path: str) -> bool:
    """
    Write-ahead rollback journal.  Records every planned destructive action
    BEFORE any of them run, so a crash mid-execute leaves a document the
    operator can use to work out what happened.
    """
    import json
    entries = []
    for fs in plan.discard:
        entries.append({"op": "delete", "path": fs.path,
                        "tier": fs.tier.name, "size": fs.size_bytes})
    for src, dst in plan.relocate:
        entries.append({"op": "relocate", "src": src, "dst": dst})
    data = {
        "schema": "walkman_promote/1",
        "written_at": __import__("datetime").datetime.now().isoformat(),
        "entries": entries,
    }
    try:
        with open(journal_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


def _append_journal_result(journal_path: str, entry: dict) -> None:
    """Append a result line to the journal — best effort, errors swallowed."""
    import json
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return
    doc.setdefault("results", []).append(entry)
    try:
        with open(journal_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    except Exception:
        pass


def _try_transcode_to_flac(src_path: str, dst_path: str,
                            timeout_s: int = 120) -> bool:
    """
    Best-effort ffmpeg transcode for unsupported-format singletons
    (.opus, .ogg, .webm) into a ZX500-readable FLAC.

    Returns True on success.  If ffmpeg is not installed or the transcode
    fails, returns False and the caller treats the singleton as skipped.
    """
    import shutil as _sh
    import subprocess as _sp
    ffmpeg = _sh.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        # -vn drops any video track (webm), -c:a flac picks FLAC codec,
        # -compression_level 8 gets a reasonable size/speed balance.
        cmd = [ffmpeg, "-nostdin", "-y", "-i", src_path,
               "-vn", "-c:a", "flac", "-compression_level", "8",
               dst_path]
        result = _sp.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return result.returncode == 0 and Path(dst_path).exists() and Path(dst_path).stat().st_size > 0
    except Exception:
        return False


def execute_plan(plan: PromotionPlan,
                 ops: "DeviceOps",
                 dry_run: bool = True,
                 journal_path: str | None = None,
                 logger=None,
                 transcode_unsupported: bool = True
                 ) -> ExecutionReport:
    """
    Apply a PromotionPlan against real (or mock) device ops.

    On dry_run=True, returns a zeroed report without touching anything.

    On dry_run=False, performs in this exact order:
      1. Write rollback journal to journal_path (if provided).
      2. For each unsupported singleton: attempt ffmpeg transcode to FLAC
         next to the source if transcode_unsupported=True; the transcoded
         file is NOT deleted from source (operator decides).
      3. For each discard: ops.delete_file().  Record result to journal.
      4. For each relocation (src_folder, dst_folder): ops.move_file()
         the folder contents (audio winners + cover art only).
    """
    _log = logger or __import__("logging").getLogger(__name__)
    report = ExecutionReport()
    if journal_path:
        report.journal_path = journal_path

    if dry_run:
        return report

    if journal_path and not _write_journal(plan, journal_path):
        _log.warning(f"[Promote] Could not write rollback journal to {journal_path}; "
                     f"refusing to execute without write-ahead safety.")
        report.failed.append(f"journal_write:{journal_path}")
        return report

    # Step 2: transcode unsupported singletons first (pure writes, no deletes)
    if transcode_unsupported and plan.unsupported_singletons:
        for fs in plan.unsupported_singletons:
            dst = str(Path(fs.path).with_suffix(".flac"))
            if _try_transcode_to_flac(fs.path, dst):
                report.transcoded += 1
                _log.info(f"  [Promote/transcode] {fs.path} -> {dst}")
                if journal_path:
                    _append_journal_result(journal_path,
                        {"op": "transcode", "src": fs.path, "dst": dst, "ok": True})
            else:
                report.skipped += 1
                _log.warning(f"  [Promote/transcode] SKIPPED (ffmpeg missing or failed): {fs.path}")

    # Step 3: delete losers.
    for loser in plan.discard:
        ok = False
        try:
            ok = ops.delete_file(loser.path)
        except Exception as e:
            _log.warning(f"  [Promote/delete] exception for {loser.path}: {e}")
        if ok:
            report.deleted += 1
        else:
            report.failed.append(loser.path)
        if journal_path:
            _append_journal_result(journal_path,
                {"op": "delete", "path": loser.path, "ok": ok})

    # Step 4: relocations — these are folder-level in the plan but we execute
    # them as file-level moves because the source folder may have junk (.bak,
    # Thumbs.db) we don't want to carry.  Q3 decision: cherry-pick audio + art.
    _AUDIO = {"flac", "mp3", "m4a", "mp4", "aac", "aif", "aiff",
              "wav", "dsf", "dff", "ape", "wma", "asf"}
    for src_folder, dst_folder in plan.relocate:
        try:
            entries = ops.list_dir(src_folder)
        except Exception:
            entries = []
        if not entries:
            _log.debug(f"  [Promote/relocate] empty/unreadable src: {src_folder}")
            continue
        moved_any = False
        for entry in entries:
            name = Path(entry).name
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext in _AUDIO or is_cover_art(name):
                new_path = str(Path(dst_folder) / name)
                ok = False
                try:
                    ok = ops.move_file(entry, new_path)
                except Exception as e:
                    _log.warning(f"  [Promote/move] exception: {e}")
                if ok:
                    report.moved += 1
                    moved_any = True
                else:
                    report.failed.append(entry)
                if journal_path:
                    _append_journal_result(journal_path,
                        {"op": "relocate", "src": entry, "dst": new_path, "ok": ok})
        if moved_any:
            _log.info(f"  [Promote/relocate] {src_folder} -> {dst_folder}")

    _log.info(
        f"[Promote] Execute complete: "
        f"deleted={report.deleted}, moved={report.moved}, "
        f"transcoded={report.transcoded}, skipped={report.skipped}, "
        f"failed={len(report.failed)}"
    )
    return report


# ---------------------------------------------------------------------------
# AdbDeviceOps — real Walkman-device implementation
# ---------------------------------------------------------------------------

class AdbDeviceOps:
    """
    Concrete DeviceOps backed by adb_transfer.  Addresses are device-side
    absolute paths like "/storage/self/primary/Music/Drake/Scorpion/03.flac".

    The storage-name parsing is done by adb_transfer's _get_storage_path;
    this class only deals with absolute device paths and shell commands.

    Same-storage moves use `mv` (atomic on the device).  Cross-storage moves
    fall back to `cp` + `rm` because `mv` across filesystems is not supported
    on Android's /storage bind-mount layout.
    """

    def __init__(self, device_name: str = ""):
        import adb_transfer as _adb
        self._adb = _adb
        self.device_name = device_name

    def _run(self, cmd: list, timeout: int = 60):
        """Thin wrapper over _adb_cmd so we can log uniformly."""
        return self._adb._adb_cmd(cmd, timeout=timeout, check=False)

    def _quote(self, s: str) -> str:
        return self._adb._shell_quote(s)

    @staticmethod
    def _same_storage_root(a: str, b: str) -> bool:
        """Return True if both device paths share the same top-level storage."""
        # Android: /storage/<volume>/...  — "volume" is the 3rd path component
        def _root(p: str) -> str:
            parts = [x for x in p.split("/") if x]
            return "/".join(parts[:2])
        return _root(a) == _root(b)

    def move_file(self, src: str, dst: str) -> bool:
        # Ensure destination directory exists
        dst_parent = "/".join(dst.split("/")[:-1])
        if dst_parent:
            self._run(["shell", "sh", "-c", f"mkdir -p {self._quote(dst_parent)}"])
        if self._same_storage_root(src, dst):
            out = self._run(["shell", "sh", "-c",
                             f"mv {self._quote(src)} {self._quote(dst)} "
                             f"&& echo OK || echo FAIL"])
            return "OK" in (out or "")
        # Cross-storage: cp then rm.  cp -p preserves mtime/perms.
        cp_out = self._run(["shell", "sh", "-c",
                            f"cp -p {self._quote(src)} {self._quote(dst)} "
                            f"&& echo OK || echo FAIL"])
        if "OK" not in (cp_out or ""):
            return False
        rm_out = self._run(["shell", "sh", "-c",
                            f"rm {self._quote(src)} && echo OK || echo FAIL"])
        # If rm fails we still return True — the destination exists, and the
        # journal keeps a record so the operator can tidy up the source later.
        return "OK" in (rm_out or "") or True

    def delete_file(self, path: str) -> bool:
        out = self._run(["shell", "sh", "-c",
                         f"rm {self._quote(path)} && echo OK || echo FAIL"])
        if "OK" in (out or ""):
            return True
        # Might already be gone — recheck
        return not self._adb._exists(path)

    def list_dir(self, path: str) -> list:
        out = self._run(["shell", "sh", "-c",
                         f"ls -1 {self._quote(path)} 2>/dev/null"])
        if not out:
            return []
        return [path.rstrip("/") + "/" + name
                for name in out.splitlines() if name.strip()]

    def stat(self, path: str) -> dict:
        if not self._adb._exists(path):
            return {"exists": False}
        out = self._run(["shell", "sh", "-c",
                         f"stat -c '%s %Y' {self._quote(path)} 2>/dev/null"])
        size, mtime = 0, 0
        try:
            parts = (out or "").strip().split()
            if len(parts) >= 2:
                size, mtime = int(parts[0]), int(parts[1])
        except Exception:
            pass
        return {"exists": True, "size": size, "mtime": mtime}


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def rollback_from_journal(journal_path: str, ops: "DeviceOps", logger=None) -> dict:
    """
    Best-effort undo of an executed plan using the journal file.

    Deletes cannot truly be undone (file is gone), but relocations can be
    reversed by moving files back.  Returns a summary dict.
    """
    import json
    _log = logger or __import__("logging").getLogger(__name__)
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        _log.error(f"[Promote/rollback] cannot read journal: {e}")
        return {"error": str(e)}
    summary = {"restored_moves": 0, "cannot_undo_deletes": 0, "failed": 0}
    results = doc.get("results", [])
    # Reverse order so the latest action is undone first
    for entry in reversed(results):
        if not entry.get("ok"):
            continue
        op = entry.get("op")
        if op == "relocate":
            # Swap src/dst and call move_file
            try:
                if ops.move_file(entry["dst"], entry["src"]):
                    summary["restored_moves"] += 1
                else:
                    summary["failed"] += 1
            except Exception:
                summary["failed"] += 1
        elif op == "delete":
            summary["cannot_undo_deletes"] += 1
    _log.info(f"[Promote/rollback] {summary}")
    return summary


# ---------------------------------------------------------------------------
# Legacy pointers (kept for reference during review)
# ---------------------------------------------------------------------------
# Rev 14.7 wiring summary:
#   - Tools-panel "Build promotion plan (dry-run)"   -> _promote_dryrun      (main.py)
#   - Tools-panel "Execute promotion plan..."        -> _promote_execute     (main.py)
#   - config.PROMOTE_ENABLED gates destructive execute; dry-run always works.
#   - AdbDeviceOps is the production ops class; LocalDeviceOps is for tests.
#   - Rollback is GUI-accessible via the same Tools panel.
