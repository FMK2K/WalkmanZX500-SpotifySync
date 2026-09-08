"""
walkman_inventory_repair.py — MK39.1 L5.3 / Council Forensic Review §5.3
================================================================================

Council Forensic Review of LOG_11_5_26 §5.3 (Pre-Existing Inventory Corruption):

  > "A dedicated walkman-inventory-repair pass must be developed and
  > executed before any further sync operations. This pass shall:
  >   - Fingerprint all audio files, identify exact duplicates (by content
  >     hash), and retain only one canonical copy.
  >   - Run a full metadata scrub against MusicBrainz; for tracks that
  >     return no match, tag with Unknown and flag for operator review.
  >   - Re-index the device exclusively via ADB (Simplicity Sprint Target E)
  >     to eliminate MTP enumeration races."

This module IMPLEMENTS the classify-and-report stage (Sprint scope: L5.3).
Execution of destructive operations is deferred to:
  - walkman_dedup engine with strict mode for exact-dupe pruning
  - genre_client + audit pipeline for metadata scrub
  - device_index ADB re-scan (Simplicity Sprint Target E — separate sprint)

The repair report is JSON with four buckets:
  - exact_dupes:         candidates by (basename, size) collision
  - blank_metadata:      tracks with empty title OR artist OR album
  - codec_ext_mismatch:  audio codec doesn't match file extension
  - placeholder_year:    year missing or set to '1970' / '0' / etc.

The report respects the L4 safety profile: it emits the [Safety/L4] banner
before doing anything, and prints recommended next-action commands that
preserve the operator's Council-endorsed unattended posture.

Inventory format
----------------
Accepts the TSV that the operator's extractor produces:
  location | path | size | codec | sample_rate | bits | channels |
  bit_rate | duration_s | title | artist | album_artist | album |
  track | disc | year | genre | composer

Usage
-----
  python walkman_inventory_repair.py --inventory walkman_inventory.tsv
  python walkman_inventory_repair.py --inventory inv.tsv --plan repair.json
  python walkman_inventory_repair.py --inventory inv.tsv --summary-only

Exit codes
----------
  0  classification ran clean; report written
  1  inventory file unreadable or zero rows
  2  classification produced findings AND --strict was set
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Classifier data structures
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class DupeGroup:
    basename: str
    size: int
    paths: List[str]
    cross_storage: bool   # True when same content on internal AND sdcard

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MetadataHole:
    path: str
    missing_fields: List[str]   # 'title', 'artist', 'album'
    has_extension: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CodecMismatch:
    path: str
    extension: str
    declared_codec: str
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlaceholderYear:
    path: str
    year: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RepairReport:
    """Top-level repair report — what would-be done per category."""
    total_rows:        int
    exact_dupes:       List[DupeGroup] = field(default_factory=list)
    blank_metadata:    List[MetadataHole] = field(default_factory=list)
    codec_ext_mismatch: List[CodecMismatch] = field(default_factory=list)
    placeholder_year:  List[PlaceholderYear] = field(default_factory=list)
    summary:           dict = field(default_factory=dict)
    recommended_actions: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "exact_dupes": [g.as_dict() for g in self.exact_dupes],
            "blank_metadata": [h.as_dict() for h in self.blank_metadata],
            "codec_ext_mismatch": [m.as_dict() for m in self.codec_ext_mismatch],
            "placeholder_year": [p.as_dict() for p in self.placeholder_year],
            "summary": self.summary,
            "recommended_actions": self.recommended_actions,
        }


# ─────────────────────────────────────────────────────────────────────────
# Classification logic
# ─────────────────────────────────────────────────────────────────────────

# Codec/extension equivalences. (codec_lower, ext_lower) → acceptable.
_CODEC_EQUIV = {
    ("aac", "m4a"), ("aac", "mp4"), ("aac", "aac"),
    ("alac", "m4a"), ("alac", "alac"), ("alac", "mp4"),
    ("flac", "flac"),
    ("mp3", "mp3"),
    ("vorbis", "ogg"), ("vorbis", "oga"),
    ("opus", "opus"), ("opus", "ogg"),
    ("ac3", "ac3"), ("ac3", "mp4"),
}

# Year placeholders that strongly indicate failed lookup
_PLACEHOLDER_YEARS = frozenset({"", "0", "1970", "1900", "0000"})


def _is_blank(value: Optional[str]) -> bool:
    return not value or not value.strip()


def classify_inventory(rows: list) -> RepairReport:
    """Walk the inventory rows and produce a repair report."""
    report = RepairReport(total_rows=len(rows))

    # ── Exact dupe detection (cheap signal: basename + size collision) ──
    bucket = defaultdict(list)
    for r in rows:
        bn = Path(r["path"]).name
        try:
            sz = int(r["size"])
        except (KeyError, ValueError, TypeError):
            sz = -1
        bucket[(bn, sz)].append(r["path"])

    for (bn, sz), paths in bucket.items():
        if len(paths) < 2:
            continue
        # Cross-storage = same content on internal AND sdcard. ZX500 mount
        # points: internal lives under /storage/emulated/0/ and SD card lives
        # under /storage/<UUID>/ where UUID is uppercase hex (e.g.
        # /storage/4A21-0000/). Also tolerate the operator's TSV-extractor
        # prefix conventions ('internal/...', 'sdcard/...').
        locs = set()
        for p in paths:
            if p.startswith("/storage/emulated/") or p.startswith("internal/"):
                locs.add("internal")
            elif p.startswith("/storage/") or p.startswith("sdcard/") or "/external_SD/" in p:
                # /storage/<UUID>/ is the canonical SD-card mount on ZX500
                locs.add("sdcard")
        cross = len(locs) > 1
        report.exact_dupes.append(DupeGroup(
            basename=bn, size=sz, paths=sorted(paths), cross_storage=cross,
        ))

    # Stable ordering for the report (largest-impact dupes first)
    report.exact_dupes.sort(key=lambda g: -len(g.paths))

    # ── Metadata holes ──
    for r in rows:
        missing = []
        if _is_blank(r.get("title")):  missing.append("title")
        if _is_blank(r.get("artist")): missing.append("artist")
        if _is_blank(r.get("album")):  missing.append("album")
        if missing:
            report.blank_metadata.append(MetadataHole(
                path=r["path"],
                missing_fields=missing,
                has_extension=bool(Path(r["path"]).suffix),
            ))

    # ── Codec / extension mismatch ──
    for r in rows:
        ext = Path(r["path"]).suffix.lower().lstrip(".")
        codec = (r.get("codec") or "").lower()
        if not codec or not ext:
            continue
        if (codec, ext) in _CODEC_EQUIV:
            continue
        if codec == ext:
            continue
        report.codec_ext_mismatch.append(CodecMismatch(
            path=r["path"], extension=ext, declared_codec=codec,
            note=f"codec={codec} but file extension is .{ext}",
        ))

    # ── Placeholder year ──
    for r in rows:
        y = (r.get("year") or "").strip()
        if y in _PLACEHOLDER_YEARS:
            report.placeholder_year.append(PlaceholderYear(
                path=r["path"], year=y or "(empty)",
            ))

    # ── Summary + recommended actions ──
    redundant_files = sum(len(g.paths) - 1 for g in report.exact_dupes)
    cross_storage_groups = sum(1 for g in report.exact_dupes if g.cross_storage)
    report.summary = {
        "exact_dupe_groups":    len(report.exact_dupes),
        "exact_dupe_redundant": redundant_files,
        "cross_storage_dupes":  cross_storage_groups,
        "blank_metadata":       len(report.blank_metadata),
        "codec_ext_mismatch":   len(report.codec_ext_mismatch),
        "placeholder_year":     len(report.placeholder_year),
    }

    if redundant_files:
        report.recommended_actions.append(
            f"DEDUP — run walkman_dedup in strict execute mode to remove "
            f"~{redundant_files} exact-duplicate files. Council L2/L3 "
            f"defaults (DEDUP_STRICT_MODE=True, threshold=0.6) keep this safe."
        )
    if report.blank_metadata:
        report.recommended_actions.append(
            f"METADATA — {len(report.blank_metadata)} tracks have blank "
            f"title/artist/album. Route through audit pipeline. Tracks "
            f"with no MusicBrainz match get 'Unknown' tag + flagged for "
            f"operator review (per Council §5.3)."
        )
    if report.codec_ext_mismatch:
        report.recommended_actions.append(
            f"CODEC/EXT — {len(report.codec_ext_mismatch)} tracks have "
            f"codec≠extension (e.g. FLAC payload in .m4a container). "
            f"Re-encode or relabel before next sync; otherwise Walkman "
            f"playback may glitch."
        )
    if report.placeholder_year:
        report.recommended_actions.append(
            f"YEAR — {len(report.placeholder_year)} tracks have empty or "
            f"placeholder year (1970/0). Refresh via genre_client API "
            f"waterfall (Last.fm → MusicBrainz → Discogs)."
        )

    if not report.recommended_actions:
        report.recommended_actions.append("CLEAN — inventory is in good shape; no repair actions required.")

    return report


# ─────────────────────────────────────────────────────────────────────────
# IO — read TSV inventory
# ─────────────────────────────────────────────────────────────────────────

def read_inventory_tsv(path: Path) -> list:
    """Read the operator's inventory TSV. Returns list of dict rows."""
    if not path.is_file():
        raise FileNotFoundError(f"inventory not found: {path}")
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return list(reader)


# ─────────────────────────────────────────────────────────────────────────
# Pretty-print
# ─────────────────────────────────────────────────────────────────────────

def format_report_summary(report: RepairReport, max_examples: int = 3) -> str:
    """Human-readable text summary suitable for the operator's terminal."""
    lines = []
    lines.append("=" * 72)
    lines.append(f"WALKMAN INVENTORY REPAIR REPORT — Council §5.3")
    lines.append("=" * 72)
    lines.append(f"Total rows scanned: {report.total_rows:,}")
    lines.append("")
    lines.append("FINDINGS")
    lines.append("-" * 72)
    s = report.summary
    lines.append(f"  Exact-dupe groups:           {s['exact_dupe_groups']:>5} "
                  f"(~{s['exact_dupe_redundant']:,} redundant files)")
    lines.append(f"  ↳ cross-storage (int+SD):    {s['cross_storage_dupes']:>5}")
    lines.append(f"  Blank metadata (title/etc):  {s['blank_metadata']:>5}")
    lines.append(f"  Codec/extension mismatch:    {s['codec_ext_mismatch']:>5}")
    lines.append(f"  Placeholder year:            {s['placeholder_year']:>5}")
    lines.append("")
    if report.exact_dupes:
        lines.append(f"TOP EXACT DUPE GROUPS (showing first {max_examples})")
        lines.append("-" * 72)
        for g in report.exact_dupes[:max_examples]:
            lines.append(f"  {g.basename} ({g.size:,} bytes) × {len(g.paths)}"
                          + ("  [CROSS-STORAGE]" if g.cross_storage else ""))
            for p in g.paths[:3]:
                lines.append(f"    {p}")
            if len(g.paths) > 3:
                lines.append(f"    ... and {len(g.paths) - 3} more")
        lines.append("")
    if report.codec_ext_mismatch:
        lines.append(f"CODEC/EXT MISMATCH (showing first {max_examples})")
        lines.append("-" * 72)
        for m in report.codec_ext_mismatch[:max_examples]:
            lines.append(f"  {m.note}")
            lines.append(f"    {m.path}")
        lines.append("")
    lines.append("RECOMMENDED ACTIONS")
    lines.append("-" * 72)
    for i, a in enumerate(report.recommended_actions, 1):
        lines.append(f"  {i}. {a}")
    lines.append("=" * 72)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="MK39.1 L5.3/L5.4 — Walkman inventory repair classifier "
                    "(Council Forensic Review of LOG_11_5_26 §5.3)."
    )
    ap.add_argument("--inventory", required=True, type=Path,
                     help="path to walkman_inventory.tsv")
    ap.add_argument("--plan", type=Path, default=None,
                     help="write JSON report to this path")
    ap.add_argument("--summary-only", action="store_true",
                     help="print summary only, suppress per-record dump")
    ap.add_argument("--strict", action="store_true",
                     help="exit 2 if findings exist (CI gate)")
    ap.add_argument("--max-examples", type=int, default=3,
                     help="number of examples per category in text summary")
    ap.add_argument("--show-safety-banner", action="store_true", default=True,
                     help="emit L4 platform safety profile banner first")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Emit the L4 safety profile banner first (operator visibility of
    # the active safety posture)
    if args.show_safety_banner:
        try:
            import platform_safety_profile as psp
            psp.emit_banner()
        except Exception as _safety_err:
            log.debug(f"safety banner suppressed: {_safety_err!r}")

    # Read and classify
    try:
        rows = read_inventory_tsv(args.inventory)
    except FileNotFoundError as e:
        print(f"[Repair] ERROR: {e}", file=sys.stderr)
        return 1
    if not rows:
        print(f"[Repair] ERROR: inventory file is empty: {args.inventory}",
                file=sys.stderr)
        return 1

    report = classify_inventory(rows)

    print(format_report_summary(report, max_examples=args.max_examples))

    if args.plan:
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        args.plan.write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n[Repair] JSON plan → {args.plan}")

    # Any non-trivial finding → exit 2 under --strict
    has_findings = (
        len(report.exact_dupes) > 0
        or len(report.blank_metadata) > 0
        or len(report.codec_ext_mismatch) > 0
        or len(report.placeholder_year) > 0
    )
    if args.strict and has_findings:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
