#!/usr/bin/env python3
"""
quarantine_review.py — operator CLI for MK39-A1.3 quarantine workflow
================================================================================

Council 2026-05-09 §5 immediate action #2: *"Publish `quarantine_review.py`
alongside this release."*

The MK39-A1.3 user-canonical source recognition module quarantines single-
signal metadata extractions (filename-only OR folder-only) per Council
binding constraint #1. These quarantined items live in
`.user_canonical_pending.json` files inside album folders, awaiting
operator review.

This CLI provides the discoverable surface the Council §3 review identified
as missing: *"the quarantine file produced by the user-canonical source is
opaque to the operator. A simple CLI must allow listing, accepting,
rejecting, and inspecting quarantined metadata."*

USAGE:
  # List pending in a single folder
  python quarantine_review.py --list <album_folder>

  # List ALL pending across a tree (e.g., ~/Music)
  python quarantine_review.py --scan <root>

  # Show details for a specific track
  python quarantine_review.py --inspect <album_folder> <track_filename>

  # Accept a specific track (writes to .user_metadata.json with
  # _provenance: user_explicit, removes from pending)
  python quarantine_review.py --accept <album_folder> <track_filename>

  # Accept everything pending in a folder
  python quarantine_review.py --accept-all <album_folder>

  # Reject (remove from pending without accepting)
  python quarantine_review.py --reject <album_folder> <track_filename>

  # Clear all pending in a folder
  python quarantine_review.py --reject-all <album_folder>

  # Summary of quarantine state across a tree
  python quarantine_review.py --summary <root>

EXIT CODES:
  0  success
  1  general error
  2  invalid arguments
  3  no pending file found at given path
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import the user-canonical module for shared constants + load/parse helpers.
# Falls back to inline constants if the module isn't on sys.path (e.g.,
# operator copies just this CLI to a different directory).
try:
    from mk39_a1_3_user_canonical import (
        OVERRIDE_FILENAME, QUARANTINE_FILENAME,
        load_quarantine_file, load_override_file,
    )
    _MK39_AVAILABLE = True
except ImportError:
    OVERRIDE_FILENAME = ".user_metadata.json"
    QUARANTINE_FILENAME = ".user_canonical_pending.json"
    _MK39_AVAILABLE = False

    def load_quarantine_file(album_folder):
        p = Path(str(album_folder)) / QUARANTINE_FILENAME
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict) or data.get("version") != 1:
            return {}
        return data.get("pending", {}) if isinstance(data.get("pending"), dict) else {}

    def load_override_file(album_folder):
        p = Path(str(album_folder)) / OVERRIDE_FILENAME
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict) or data.get("version") != 1:
            return {}
        tracks = data.get("tracks", {})
        out = {}
        for fname, fields in (tracks.items() if isinstance(tracks, dict) else []):
            if isinstance(fields, dict) and fields.get("_provenance") == "user_explicit":
                out[str(fname)] = {k: v for k, v in fields.items() if not k.startswith("_")}
        return out


# ═══════════════════════════════════════════════════════════════════════════
# MK39 Phase II — TARGET-I "Needs Review" support
# Council Forum Suggestions 2026-05-10 (post-Phase-I-COMPLETE adversarial-
# vetted shortlist). The .needs_review.json file is a sibling marker that
# walkman_sync writes when a track has audio on disk but no resolved identity
# (blank artist OR blank title) after Phase I containment runs. The CLI
# surfaces these to the operator so they can fix with Picard / MP3Tag /
# Beets and have the platform ingest the corrections on the next run.
# ═══════════════════════════════════════════════════════════════════════════

NEEDS_REVIEW_FILENAME = ".needs_review.json"


def load_needs_review_file(album_folder):
    """Return list of needs-review items in folder, or empty list."""
    p = Path(str(album_folder)) / NEEDS_REVIEW_FILENAME
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict) or data.get("version") != 1:
        return []
    items = data.get("needs_review", [])
    return items if isinstance(items, list) else []


# ═══════════════════════════════════════════════════════════════════════════
# MK39.1 L1.3 — Dedup pre-commit plan review (operator-facing audit)
# Council operator-feedback path 2026-05-10. Production dry-run produced
# 4253 planned deletes (LOG_10_5_26). The dry-run log was the ONLY audit
# surface; this CLI gives operator a structured, categorized view they can
# inspect before re-running for real.
# ═══════════════════════════════════════════════════════════════════════════

DEDUP_PLAN_FILENAME = ".dedup_plan.json"


def load_dedup_plan(folder=None):
    """Load .dedup_plan.json from a folder (default: cwd). Returns the full
    payload dict, or None if the file doesn't exist / is invalid."""
    base = Path(str(folder)) if folder else Path.cwd()
    p = base / DEDUP_PLAN_FILENAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    return data


# ═══════════════════════════════════════════════════════════════════════════
# ANSI color helpers (no-op on non-tty)
# ═══════════════════════════════════════════════════════════════════════════

_TTY = sys.stdout.isatty()
def _c(code: str, s: Any) -> str:
    if not _TTY: return str(s)
    return f"\033[{code}m{s}\033[0m"
def _g(s):  return _c("32", s)   # green
def _y(s):  return _c("33", s)   # yellow
def _r(s):  return _c("31", s)   # red
def _b(s):  return _c("1", s)    # bold
def _d(s):  return _c("2", s)    # dim
def _cyan(s): return _c("36", s)


# ═══════════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════════

def cmd_list(album_folder: Path) -> int:
    """List pending entries in a single folder."""
    if not album_folder.is_dir():
        print(_r(f"error: not a directory: {album_folder}"), file=sys.stderr)
        return 2
    pending = load_quarantine_file(album_folder)
    if not pending:
        print(_d(f"no pending entries in {album_folder}"))
        return 3
    print(_b(f"\nPending in {album_folder}:"))
    print(_d(f"  ({len(pending)} track(s) awaiting operator review)\n"))
    for fname in sorted(pending.keys()):
        fields = pending[fname]
        print(f"  {_cyan(fname)}")
        for field_name in sorted(fields.keys()):
            entry = fields[field_name]
            val = entry.get("value", "")
            src = entry.get("source", "?")
            conf = entry.get("confidence", "?")
            first = entry.get("first_seen", "?")
            print(f"    {_b(field_name):20s} = {_y(repr(val))}")
            print(f"    {_d('source:'):20s} {src}    {_d('confidence:')} {conf}    {_d('first_seen:')} {first[:19]}")
        print()
    print(_d(f"To accept: python quarantine_review.py --accept-all {album_folder}"))
    print(_d(f"To inspect:  python quarantine_review.py --inspect {album_folder} <filename>\n"))
    return 0


def cmd_inspect(album_folder: Path, filename: str) -> int:
    """Show full details for one track."""
    pending = load_quarantine_file(album_folder)
    if filename not in pending:
        print(_r(f"error: {filename!r} not in pending for {album_folder}"), file=sys.stderr)
        return 1
    print(_b(f"\nTrack: {filename}"))
    print(_d(f"Folder: {album_folder}"))
    print(_d(f"Quarantine file: {album_folder / QUARANTINE_FILENAME}\n"))
    fields = pending[filename]
    for field_name in sorted(fields.keys()):
        entry = fields[field_name]
        print(f"  {_b(field_name)}:")
        for k, v in entry.items():
            print(f"    {k:14s} {v}")
        print()
    # Also show what the override file currently has for this track
    overrides = load_override_file(album_folder)
    if filename in overrides:
        print(_g(f"Already in override file ({OVERRIDE_FILENAME}):"))
        for k, v in sorted(overrides[filename].items()):
            print(f"  {k:14s} {v}")
        print()
    print(_d(f"To accept these:  python quarantine_review.py --accept {album_folder} {filename}"))
    print(_d(f"To reject these:  python quarantine_review.py --reject {album_folder} {filename}\n"))
    return 0


def cmd_accept(album_folder: Path, filename: str, accept_fields: Optional[List[str]] = None) -> int:
    """Accept pending entries — copy to .user_metadata.json with
    `_provenance: user_explicit`, remove from pending file."""
    pending = load_quarantine_file(album_folder)
    if filename not in pending:
        print(_r(f"error: {filename!r} not in pending for {album_folder}"), file=sys.stderr)
        return 1
    fields = pending[filename]
    if accept_fields:
        # Subset
        unknown = [f for f in accept_fields if f not in fields]
        if unknown:
            print(_r(f"error: fields not in pending: {unknown}"), file=sys.stderr)
            return 1
        accept_dict = {f: fields[f]["value"] for f in accept_fields}
    else:
        accept_dict = {f: e["value"] for f, e in fields.items()}

    # Load existing override file
    override_path = album_folder / OVERRIDE_FILENAME
    if override_path.exists():
        try:
            override_data = json.loads(override_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            override_data = {"version": 1, "tracks": {}}
    else:
        override_data = {"version": 1, "tracks": {}}
    if "tracks" not in override_data or not isinstance(override_data.get("tracks"), dict):
        override_data["tracks"] = {}

    # Merge — add accepted fields with _provenance marker
    track_entry = override_data["tracks"].get(filename, {})
    for f, v in accept_dict.items():
        track_entry[f] = v
    track_entry["_provenance"] = "user_explicit"
    override_data["tracks"][filename] = track_entry

    # Write override back
    override_path.write_text(json.dumps(override_data, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    # Remove accepted track from pending file
    quar_path = album_folder / QUARANTINE_FILENAME
    if quar_path.exists():
        try:
            quar_data = json.loads(quar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            quar_data = None
        if quar_data and isinstance(quar_data, dict):
            pending_dict = quar_data.get("pending", {})
            if accept_fields:
                # Only remove the accepted fields, keep others pending
                remaining = {k: v for k, v in pending_dict.get(filename, {}).items()
                              if k not in accept_fields}
                if remaining:
                    pending_dict[filename] = remaining
                else:
                    pending_dict.pop(filename, None)
            else:
                pending_dict.pop(filename, None)
            quar_data["pending"] = pending_dict
            if pending_dict:
                quar_path.write_text(json.dumps(quar_data, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
            else:
                # No more pending — remove the file
                quar_path.unlink()

    print(_g(f"\n✓ Accepted {len(accept_dict)} field(s) for {filename}:"))
    for k, v in accept_dict.items():
        print(f"    {_b(k):20s} = {_y(repr(v))}")
    print(_d(f"\n  Written to: {override_path}"))
    print(_d(f"  Provenance: user_explicit"))
    print(_d(f"  Next sync will treat these as canonical (STRONG, writable).\n"))
    return 0


def cmd_accept_all(album_folder: Path) -> int:
    """Accept ALL pending entries in a folder."""
    pending = load_quarantine_file(album_folder)
    if not pending:
        print(_d(f"no pending entries in {album_folder}"))
        return 3
    print(_b(f"Accepting all pending in {album_folder} ({len(pending)} track(s)):\n"))
    success = 0
    for filename in list(pending.keys()):
        rc = cmd_accept(album_folder, filename)
        if rc == 0:
            success += 1
    print(_g(f"\n✓ {success}/{len(pending)} tracks accepted.\n"))
    return 0 if success == len(pending) else 1


def cmd_reject(album_folder: Path, filename: str) -> int:
    """Remove a pending entry without accepting."""
    pending = load_quarantine_file(album_folder)
    if filename not in pending:
        print(_r(f"error: {filename!r} not in pending"), file=sys.stderr)
        return 1
    quar_path = album_folder / QUARANTINE_FILENAME
    if quar_path.exists():
        try:
            quar_data = json.loads(quar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 1
        quar_data.get("pending", {}).pop(filename, None)
        if quar_data.get("pending"):
            quar_path.write_text(json.dumps(quar_data, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        else:
            quar_path.unlink()
    print(_y(f"✗ Rejected (removed from pending): {filename}"))
    print(_d(f"  Note: the next sync may re-add it if signals are detected again."))
    print(_d(f"  To prevent re-adding, fix the filename/folder structure or add an"))
    print(_d(f"  explicit override with the correct value.\n"))
    return 0


def cmd_reject_all(album_folder: Path) -> int:
    pending = load_quarantine_file(album_folder)
    if not pending:
        print(_d(f"no pending entries in {album_folder}"))
        return 3
    quar_path = album_folder / QUARANTINE_FILENAME
    if quar_path.exists():
        quar_path.unlink()
    print(_y(f"✗ Rejected all {len(pending)} pending entries (file removed)."))
    return 0


def cmd_scan(root: Path) -> int:
    """Recursively find all .user_canonical_pending.json files under root."""
    if not root.is_dir():
        print(_r(f"error: not a directory: {root}"), file=sys.stderr)
        return 2
    found: List[Tuple[Path, int]] = []
    for p in root.rglob(QUARANTINE_FILENAME):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            count = len(data.get("pending", {})) if isinstance(data, dict) else 0
            found.append((p.parent, count))
        except (json.JSONDecodeError, OSError):
            found.append((p.parent, -1))
    if not found:
        print(_d(f"no pending files found under {root}"))
        return 0
    print(_b(f"\nQuarantine files under {root}:\n"))
    total_tracks = 0
    for folder, count in sorted(found):
        if count < 0:
            print(f"  {_r('CORRUPT'):10s}  {folder}")
        else:
            total_tracks += count
            print(f"  {_y(str(count) + ' track(s)'):20s}  {folder}")
    print(_d(f"\n  Total: {len(found)} folder(s), {total_tracks} pending track(s)\n"))
    return 0


def cmd_summary(root: Path) -> int:
    """Aggregate summary across a tree."""
    if not root.is_dir():
        print(_r(f"error: not a directory: {root}"), file=sys.stderr)
        return 2
    folders_found = 0
    tracks_pending = 0
    fields_by_source: Dict[str, int] = {}
    for p in root.rglob(QUARANTINE_FILENAME):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict): continue
        pending = data.get("pending", {})
        if not isinstance(pending, dict): continue
        folders_found += 1
        tracks_pending += len(pending)
        for fname, fields in pending.items():
            for fld_name, entry in (fields.items() if isinstance(fields, dict) else []):
                src = entry.get("source", "?") if isinstance(entry, dict) else "?"
                fields_by_source[src] = fields_by_source.get(src, 0) + 1
    print(_b(f"\nQuarantine summary under {root}:\n"))
    print(f"  {_b('folders with pending:'):28s}  {folders_found}")
    print(f"  {_b('total tracks pending:'):28s}  {tracks_pending}")
    if fields_by_source:
        print(f"\n  {_b('fields by source:')}")
        for src in sorted(fields_by_source.keys()):
            print(f"    {src:30s}  {fields_by_source[src]}")
    print()
    return 0


def cmd_needs_review(album_folder: Path) -> int:
    """List needs-review items in a folder. Council MK39.1 Target I:
    audio-on-disk-but-no-identity-resolved cases, presented to the
    operator with their best-guess so they can correct via Picard /
    MP3Tag / Beets and re-ingest on the next run."""
    if not album_folder.is_dir():
        print(_r(f"error: not a directory: {album_folder}"), file=sys.stderr)
        return 2
    items = load_needs_review_file(album_folder)
    if not items:
        print(_d(f"no .needs_review.json in {album_folder}"))
        return 3
    print(_b(f"\nNeeds-review items in {album_folder}:"))
    print(_d(f"  ({len(items)} track(s) require operator attention)\n"))
    for entry in items:
        if not isinstance(entry, dict):
            continue
        fname  = entry.get("filename", "?")
        reason = entry.get("reason", "(no reason given)")
        guess  = entry.get("best_guess", {}) or {}
        print(f"  {_cyan(fname)}")
        print(f"    {_d('reason:')}    {_y(reason)}")
        if guess:
            print(f"    {_d('best guess:')}")
            for k in sorted(guess.keys()):
                print(f"      {k:14s} {guess[k]!r}")
        print()
    print(_d(f"Open these files in Picard / MP3Tag / Beets to fix manually."))
    print(_d(f"After correcting tags, run quarantine_review.py --accept-all "
              f"{album_folder} to ingest.\n"))
    return 0


def cmd_needs_review_scan(root: Path) -> int:
    """Recursively find all .needs_review.json files under root and
    summarize. Council MK39.1 Target I (Forum Suggestions 2026-05-10)."""
    if not root.is_dir():
        print(_r(f"error: not a directory: {root}"), file=sys.stderr)
        return 2
    found: List[Tuple[Path, int, List[str]]] = []
    total_items = 0
    for p in root.rglob(NEEDS_REVIEW_FILENAME):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items = data.get("needs_review", []) if isinstance(data, dict) else []
            count = len(items) if isinstance(items, list) else 0
            sample_filenames = [
                (i.get("filename", "?") if isinstance(i, dict) else "?")
                for i in (items or [])[:3]
            ]
            found.append((p.parent, count, sample_filenames))
            total_items += count
        except (json.JSONDecodeError, OSError):
            found.append((p.parent, -1, []))
    if not found:
        print(_d(f"no .needs_review.json files under {root}"))
        return 0
    print(_b(f"\nNeeds-review files under {root}:\n"))
    for folder, count, samples in sorted(found):
        if count < 0:
            print(f"  {_r('CORRUPT'):14s}  {folder}")
        else:
            print(f"  {_y(str(count) + ' track(s)'):24s}  {folder}")
            for s in samples:
                print(f"    {_d('-')}  {s}")
    print(_d(f"\n  Total: {len(found)} folder(s), {total_items} track(s) "
              f"awaiting operator review\n"))
    return 0


def cmd_dedup_plan(folder: Path) -> int:
    """List the planned dedup deletes for operator review (MK39.1 L1.3).

    Council operator-feedback path 2026-05-10: production dry-run produced
    4253 planned deletes. This command displays the structured plan grouped
    by category (format_upgrade / truncation_dupe / track_renumber / other)
    so the operator can audit before running for real.
    """
    data = load_dedup_plan(folder)
    if data is None:
        print(_d(f"no .dedup_plan.json in {folder} (run dedup in dry-run mode first)"))
        return 3

    total = data.get("total_planned", 0)
    cats  = data.get("stats_by_category", {})
    generated = data.get("generated_at", "?")
    dry_run   = data.get("dry_run", True)
    mode      = data.get("mode", "?")

    print(_b(f"\nDedup pre-commit plan ({DEDUP_PLAN_FILENAME})"))
    print(_d(f"  generated: {generated}   mode: {mode}   dry-run: {dry_run}\n"))
    print(_b(f"  {total} planned delete(s):"))
    for cat in sorted(cats.keys()):
        line = f"    {cat:18s} {cats[cat]}"
        # Suspicious categories: flag in color
        if cat in ("track_renumber", "other"):
            line = _y(line)
        elif cat == "truncation_dupe":
            line = _cyan(line) + _d("  ← root cause of many dupes")
        else:
            line = _d(line)
        print(line)

    # Detailed list, grouped by category
    by_cat: Dict[str, List[dict]] = {}
    for c in data.get("candidates", []):
        if not isinstance(c, dict):
            continue
        by_cat.setdefault(c.get("category", "other"), []).append(c)

    # Lead with suspicious categories — those need closest inspection
    cat_order = ["other", "track_renumber", "truncation_dupe", "format_upgrade"]
    for cat in cat_order:
        items = by_cat.get(cat, [])
        if not items:
            continue
        heading = _b(f"\n  [{cat}]  ({len(items)} entries)")
        if cat in ("track_renumber", "other"):
            heading += _y("  ← INSPECT CLOSELY")
        print(heading)
        # Show up to 10 per category in concise form
        for c in items[:10]:
            hp = c.get("human_path", "")
            sp = c.get("superior_path", "")
            rs = c.get("reason", "?")
            print(f"    {_d('-')} {hp}")
            print(f"        {_d('keep:')} {sp}  {_d('(' + rs + ')')}")
        if len(items) > 10:
            print(_d(f"    ... and {len(items) - 10} more in this category\n"))
    print()
    print(_d(f"  To execute the plan for real: rerun walkman_dedup without --dry-run."))
    print(_d(f"  Suspicious categories ('other', 'track_renumber') deserve "
              f"close inspection — they may represent distinct songs."))
    print()
    return 0


def cmd_dedup_plan_stats(folder: Path) -> int:
    """Just print the stats_by_category summary (operator quick-scan)."""
    data = load_dedup_plan(folder)
    if data is None:
        print(_d(f"no .dedup_plan.json in {folder}"))
        return 3
    total = data.get("total_planned", 0)
    cats  = data.get("stats_by_category", {})
    print(_b(f"\nDedup plan stats ({DEDUP_PLAN_FILENAME}):"))
    print(f"  total planned: {_b(str(total))}")
    for cat in sorted(cats.keys()):
        bar = "█" * min(60, int(60 * cats[cat] / max(total, 1)))
        print(f"  {cat:18s} {cats[cat]:>6d}  {bar}")
    print()
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quarantine_review",
        description="Operator CLI for MK39-A1.3 user-canonical quarantine workflow.",
        epilog=(
            "Examples:\n"
            "  quarantine_review --list ~/Music/Beatles/Help\n"
            "  quarantine_review --scan ~/Music\n"
            "  quarantine_review --accept-all ~/Music/Beatles/Help\n"
            "  quarantine_review --inspect ~/Music/Beatles/Help track.mp3\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", metavar="FOLDER", help="list pending entries in a folder")
    g.add_argument("--scan", metavar="ROOT", help="recursively find quarantine files")
    g.add_argument("--summary", metavar="ROOT", help="aggregate summary across a tree")
    g.add_argument("--inspect", nargs=2, metavar=("FOLDER", "FILENAME"),
                    help="show full details for a track")
    g.add_argument("--accept", nargs=2, metavar=("FOLDER", "FILENAME"),
                    help="accept a track's pending fields (writes to .user_metadata.json)")
    g.add_argument("--accept-all", metavar="FOLDER",
                    help="accept everything pending in a folder")
    g.add_argument("--reject", nargs=2, metavar=("FOLDER", "FILENAME"),
                    help="reject (remove from pending without accepting)")
    g.add_argument("--reject-all", metavar="FOLDER",
                    help="clear all pending in a folder")
    # MK39 Phase II — TARGET-I (Council Forum 2026-05-10): operator path
    # for files that have audio on disk but no resolved identity. Output
    # is read-only: lists the items + their best-guess fields. Operator
    # uses Picard / MP3Tag / Beets to fix, then runs --accept-all to ingest.
    g.add_argument("--needs-review", metavar="FOLDER",
                    help="list items requiring manual operator review (audio "
                          "on disk but no identity resolved after Phase I)")
    g.add_argument("--needs-review-scan", metavar="ROOT",
                    help="recursively find .needs_review.json files across a tree")
    # MK39.1 L1.3 — Dedup pre-commit plan review (Council operator-feedback 2026-05-10).
    g.add_argument("--dedup-plan", metavar="FOLDER", nargs="?", const=".",
                    help="display the latest .dedup_plan.json grouped by "
                          "category (operator audit before running for real)")
    g.add_argument("--dedup-plan-stats", metavar="FOLDER", nargs="?", const=".",
                    help="just print the dedup-plan stats summary by category")
    p.add_argument("--field", action="append", metavar="NAME",
                    help="restrict --accept to specific fields (may repeat)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(Path(args.list))
    if args.scan:
        return cmd_scan(Path(args.scan))
    if args.summary:
        return cmd_summary(Path(args.summary))
    if args.inspect:
        return cmd_inspect(Path(args.inspect[0]), args.inspect[1])
    if args.accept:
        return cmd_accept(Path(args.accept[0]), args.accept[1], args.field)
    if args.accept_all:
        return cmd_accept_all(Path(args.accept_all))
    if args.reject:
        return cmd_reject(Path(args.reject[0]), args.reject[1])
    if args.reject_all:
        return cmd_reject_all(Path(args.reject_all))
    if args.needs_review:
        return cmd_needs_review(Path(args.needs_review))
    if args.needs_review_scan:
        return cmd_needs_review_scan(Path(args.needs_review_scan))
    if args.dedup_plan:
        return cmd_dedup_plan(Path(args.dedup_plan))
    if args.dedup_plan_stats:
        return cmd_dedup_plan_stats(Path(args.dedup_plan_stats))
    return 2


if __name__ == "__main__":
    sys.exit(main())
