"""
walkman_promote_dryrun.py  —  Build a PromotionPlan against a local tree
=========================================================================

Standalone entry point the operator runs against a local copy / backup of the
Walkman library BEFORE enabling config.PROMOTE_ENABLED.

Usage
-----
    python walkman_promote_dryrun.py --root "C:/Users/FeisalK/WalkmanBackup/Music"
    python walkman_promote_dryrun.py --root "./testlib" --out plan.txt

Output
------
Writes a human-reviewable plan to walkman_promote_plan.txt (or --out path).
NEVER touches the filesystem; this is strictly read + analyse + report.

Dependencies: requires mutagen for tag extraction.  Gracefully degrades to
filename-parsing if mutagen is unavailable, but then several format tiers
(hi-res vs std FLAC) cannot be distinguished.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import walkman_promote as wp

log = logging.getLogger("walkman_promote_dryrun")


# ---------------------------------------------------------------------------
# Scanner: build FileSpec list from a local directory tree
# ---------------------------------------------------------------------------

_AUDIO_EXTS = {
    "flac", "mp3", "m4a", "mp4", "aac",
    "aif", "aiff", "wav", "dsf", "dff",
    "ape", "wma", "asf", "opus", "ogg", "webm",
}


def _tag_load(path: Path):
    """Best-effort tag read.  Returns dict or empty dict on any failure."""
    try:
        import mutagen
    except ImportError:
        return {}
    try:
        m = mutagen.File(str(path), easy=True)
        if m is None:
            return {}
        result = {}
        for key in ("artist", "albumartist", "album", "title",
                    "tracknumber", "discnumber"):
            v = m.get(key)
            if isinstance(v, list) and v:
                result[key] = str(v[0])
            elif isinstance(v, str):
                result[key] = v
        # Format-technical info (for tier ranking)
        info = getattr(m, "info", None)
        if info is not None:
            result["_sample_rate"] = getattr(info, "sample_rate", 0) or 0
            result["_bit_depth"]   = getattr(info, "bits_per_sample",
                                             getattr(info, "sample_size", 0)) or 0
            result["_bitrate"]     = int((getattr(info, "bitrate", 0) or 0) / 1000)
        return result
    except Exception:
        return {}


def _parse_track_number(s: str) -> int:
    """'3/12' -> 3;  '03' -> 3;  '' -> 0."""
    try:
        head = str(s).split("/")[0].strip()
        return int(head)
    except (ValueError, TypeError):
        return 0


def scan_local(root: str) -> list:
    """Walk `root` and return a list of FileSpec for every audio file."""
    specs = []
    root_p = Path(root)
    if not root_p.exists():
        log.error(f"Scan root does not exist: {root}")
        return specs

    count = 0
    for p in root_p.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lstrip(".").lower()
        if ext not in _AUDIO_EXTS:
            continue
        count += 1
        if count % 200 == 0:
            log.info(f"  scanned {count} audio files so far...")

        tags = _tag_load(p)
        try:
            size = p.stat().st_size
        except Exception:
            size = 0

        specs.append(wp.FileSpec(
            path=str(p),
            ext=ext,
            size_bytes=size,
            sample_rate_hz=int(tags.get("_sample_rate", 0)),
            bit_depth=int(tags.get("_bit_depth", 0)),
            bitrate_kbps=int(tags.get("_bitrate", 0)),
            artist=tags.get("artist", ""),
            album=tags.get("album", ""),
            disc=_parse_track_number(tags.get("discnumber", "1")) or 1,
            track_number=_parse_track_number(tags.get("tracknumber", "0")),
            title=tags.get("title", p.stem),
        ))
    log.info(f"Scan complete: {count} audio files indexed.")
    return specs


# ---------------------------------------------------------------------------
# Artist resolver callback for orphan album folders
# ---------------------------------------------------------------------------

def make_resolver(all_files: list):
    """
    Return a resolve_artist(album_name) callback.

    Uses the Q2 resolve_album_artist logic over all files whose album tag
    matches the given name.  Falls back to None (unresolved) if the match
    set is too small or the per-album agreement is weak.
    """
    from collections import defaultdict
    by_album = defaultdict(list)
    for fs in all_files:
        if fs.album:
            by_album[wp._norm(fs.album)].append(fs)

    def resolve(album_name: str) -> str | None:
        key = wp._norm(album_name)
        members = by_album.get(key, [])
        if len(members) < 3:
            # Too few tracks to trust — punt.
            return None
        artist = wp.resolve_album_artist(members)
        return artist if artist != "Various Artists" else None

    return resolve


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True,
                    help="Root directory to scan (e.g. backup of WALKMAN/Music)")
    ap.add_argument("--out", default="walkman_promote_plan.txt",
                    help="Output report path")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    log.info(f"Scanning: {args.root}")
    files = scan_local(args.root)
    if not files:
        log.error("No audio files found — aborting.")
        return 2

    resolver = make_resolver(files)
    log.info(f"Building plan for {len(files)} files ...")
    plan = wp.build_plan(files, resolve_artist=resolver)

    report = wp.plan_to_text(plan)
    out_p = Path(args.out)
    out_p.write_text(report, encoding="utf-8")
    log.info(f"Plan written to {out_p.resolve()}")

    # Short stdout summary
    print()
    print(f"  winners kept:           {len(plan.keep)}")
    print(f"  losers to discard:      {len(plan.discard)}")
    print(f"  folders to relocate:    {len(plan.relocate)}")
    print(f"  unsupported singletons: {len(plan.unsupported_singletons)}")
    print(f"  operator flags:         {len(plan.flags)}")
    print()
    print(f"Review {out_p.resolve()} before enabling config.PROMOTE_ENABLED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
