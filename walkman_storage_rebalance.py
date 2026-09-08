"""
walkman_storage_rebalance.py — move whole album folders SD -> internal until
the music split matches the two storages' capacity proportion.

Current state (2026-09-06): music is ~99 % on the SD card and ~1 % internal,
while internal sits ~14 % full with tens of GB free. A capacity-proportional
split (SD ~82 % / internal ~18 % of total music bytes, minus the internal
reserve) balances wear and headroom.

Moves are WHOLE ALBUM FOLDERS (not loose files), on-device (`adb shell cp`
across the mount boundary + verify + `rm` source — no PC round-trip), and
favorited/playlisted-aware only in the *ordering* sense: a moved favorited
file keeps its star (the Sony DB re-matches by content on rescan), but a
moved playlisted file breaks its M3U8 line, so albums with the FEWEST
playlist tracks are moved first and the caller must regenerate the M3U8s
afterward (step 4 below). Nothing is deleted; a move that can't verify is
rolled back.

After --execute:
    python cli_runner.py build-favorites-playlists-plan
    python walkman_playlist_import.py
    # then trigger a device "Update database" rescan
    # favorites need NO re-star (moves, not deletes)

Usage:
    python walkman_storage_rebalance.py                       # dry-run plan
    python walkman_storage_rebalance.py --execute
    python walkman_storage_rebalance.py --execute --max-gb 25
    python walkman_storage_rebalance.py --target-internal-pct 18
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import subprocess
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import adb_transfer as adb
import walkman_dedup as wd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).parent
_ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}
MUSIC = getattr(config, "MUSIC_ROOT", "Music")
GB = 1024 ** 3


def _cap_and_free(storage: str):
    """(total_bytes, free_bytes) via `adb shell df`. (0, 0) if the device /
    storage isn't reachable — main() turns that into a clean abort message."""
    try:
        root = adb._get_storage_path(storage)
    except Exception:
        return 0, 0
    try:
        out = subprocess.run(["adb", "shell", f"df {root}"], capture_output=True, text=True, env=_ENV).stdout
        for l in out.splitlines()[1:]:
            p = l.split()
            if len(p) >= 4:
                return int(p[1]) * 1024, int(p[3]) * 1024
    except Exception:
        pass
    return 0, 0


class _R:
    """Stand-in result for a timed-out / failed adb shell call."""
    def __init__(self): self.stdout = ""; self.stderr = "timeout"; self.returncode = 124


def _sh(cmd: str, timeout=120, _retry=1):
    """adb shell, tolerant of the ZX507's periodic daemon drops: on a timeout
    or transport error, restart the daemon once and retry, then give up with a
    dummy result instead of raising (a raise here used to kill the whole run
    mid-move — 2026-09-07)."""
    for attempt in range(_retry + 1):
        try:
            return subprocess.run(["adb", "shell", cmd], capture_output=True,
                                  text=True, env=_ENV, timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt < _retry:
                try:
                    subprocess.run(["adb", "kill-server"], capture_output=True, timeout=10)
                    subprocess.run(["adb", "start-server"], capture_output=True, timeout=15)
                except Exception:
                    pass
                continue
        except Exception:
            break
    return _R()


def _size(path: str) -> int:
    try:
        r = _sh(f'stat -c %s {json.dumps(path)}', timeout=20)
        return int(r.stdout.strip())
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--max-gb", type=float, default=0.0, help="cap the move volume this run")
    ap.add_argument("--target-internal-pct", type=float, default=0.0,
                    help="override the capacity-proportional target")
    ap.add_argument("--min-mb", type=float, default=8.0, help="skip folders smaller than this")
    args = ap.parse_args()

    sd_cap, sd_free = _cap_and_free("SD card")
    in_cap, in_free = _cap_and_free("Internal shared storage")
    reserve = getattr(config, "INTERNAL_STORAGE_MIN_FREE_MB", 1024) * 1024 * 1024
    if not (sd_cap and in_cap):
        print("! could not read df for both storages — aborting")
        return 1

    files, sizes = wd._bulk_device_scan(wd._default_storages())
    # whole-folder byte totals + playlist-track counts
    folder_bytes = collections.Counter()
    folder_files = collections.defaultdict(list)
    for f in files:
        k = (f["storage"], tuple(f["parts"]))
        folder_bytes[k] += sizes.get((f["storage"], tuple(f["parts"]), f["filename"]), 0)
        folder_files[k].append(f)

    music_sd = sum(b for (st, _), b in folder_bytes.items() if st == "SD card")
    music_in = sum(b for (st, _), b in folder_bytes.items() if st == "Internal shared storage")
    music_total = music_sd + music_in

    if args.target_internal_pct > 0:
        target_in_frac = args.target_internal_pct / 100.0
    else:
        # capacity-proportional, but don't target more than internal can hold w/ reserve
        target_in_frac = in_cap / (in_cap + sd_cap)
        max_holdable = max(in_free - reserve + music_in, 0) / max(music_total, 1)
        target_in_frac = min(target_in_frac, max_holdable)

    target_in_bytes = target_in_frac * music_total
    to_move = target_in_bytes - music_in

    print(f"SD card:  {sd_cap/GB:.0f} GB cap, {sd_free/GB:.1f} GB free, {music_sd/GB:.1f} GB music")
    print(f"internal: {in_cap/GB:.0f} GB cap, {in_free/GB:.1f} GB free, {music_in/GB:.1f} GB music")
    print(f"music split now: SD {music_sd/music_total*100:.1f}% / internal {music_in/music_total*100:.1f}%")
    print(f"target internal: {target_in_frac*100:.1f}%  ->  move ~{to_move/GB:.1f} GB SD -> internal")
    if to_move <= args.min_mb * 1024 * 1024:
        print("already balanced (or internal can't take more) — nothing to do")
        return 0

    cap = args.max_gb * GB if args.max_gb else to_move

    # playlist-track count per folder (move the least-disruptive first)
    pl_paths = set()
    try:
        plan = json.loads((ROOT / "favorites_playlists_plan.json").read_text(encoding="utf-8"))
        for pd in plan.get("playlists", {}).values():
            for e in pd.get("resolved", []):
                d = e.get("device") or {}
                if d.get("storage") == "SD card":
                    pl_paths.add((tuple(d.get("parts", [])), d.get("filename", "")))
    except Exception:
        pass

    # album-folder count per (norm_artist, norm_album) — prefer non-splintered
    alb_folders = collections.defaultdict(set)
    for f in files:
        bits = [b.strip() for b in f["filename"].rsplit(".", 1)[0].split(" - ")]
        if len(bits) >= 3:
            alb_folders[(wd._norm(bits[0]), wd._norm(bits[1]))].add((f["storage"], tuple(f["parts"])))

    cands = []
    for (st, parts), b in folder_bytes.items():
        if st != "SD card" or not parts or parts[0].lower() != MUSIC.lower() or len(parts) < 2:
            continue
        if b < args.min_mb * 1024 * 1024 or b > cap:
            continue
        fs = folder_files[(st, parts)]
        n_pl = sum(1 for f in fs if (tuple(parts), f["filename"]) in pl_paths)
        bits = [x.strip() for x in fs[0]["filename"].rsplit(".", 1)[0].split(" - ")]
        split = 2 if (len(bits) >= 3 and len(alb_folders.get((wd._norm(bits[0]), wd._norm(bits[1])), ())) > 1) else 0
        cands.append((n_pl + split, b, st, parts, fs))
    cands.sort(key=lambda c: (c[0], -c[1]))   # fewest playlist tracks first, then largest

    moved_bytes = 0
    plan_moves = []
    for score, b, st, parts, fs in cands:
        if moved_bytes + b > cap:
            continue
        plan_moves.append((parts, fs, b, score))
        moved_bytes += b
        if moved_bytes >= to_move:
            break

    print(f"\nplan: {len(plan_moves)} album folder(s), {moved_bytes/GB:.1f} GB")
    for parts, fs, b, score in plan_moves[:40]:
        print(f"  {b/1024/1024:6.0f} MB  {'/'.join(parts)}   ({len(fs)} files, {score} pl/split)")
    if len(plan_moves) > 40:
        print(f"  ... and {len(plan_moves) - 40} more")

    if not args.execute:
        print("\nDRY-RUN — pass --execute to move")
        return 0

    sd_root = adb._get_storage_path("SD card")
    in_root = adb._get_storage_path("Internal shared storage")
    n_folders = n_files = n_fail = n_skip = 0
    for parts, fs, b, score in plan_moves:
        rel = "/".join(parts)
        dst_dir = f"{in_root}/{rel}"
        _sh(f'mkdir -p {json.dumps(dst_dir)}', timeout=30)
        moved_all = True
        for f in fs:
            src = f"{sd_root}/{rel}/{f['filename']}"
            dst = f"{dst_dir}/{f['filename']}"
            src_sz = _size(src)
            # RESUMABLE: a prior crashed run may have already moved this file.
            if src_sz < 0:
                if _size(dst) > 0:
                    n_skip += 1                     # already on internal, gone from SD
                    continue
                moved_all = False; n_fail += 1
                print(f"  ! source unreadable, skipping: {rel}/{f['filename']}")
                continue
            if _size(dst) == src_sz:                # copy already there — just drop the source
                _sh(f'rm -f {json.dumps(src)}', timeout=25)
                n_skip += 1
                continue
            _sh(f'cp {json.dumps(src)} {json.dumps(dst)}', timeout=180)
            if _size(dst) != src_sz:
                print(f"  ! verify failed, rolling back: {rel}/{f['filename']}")
                _sh(f'rm -f {json.dumps(dst)}', timeout=25)
                moved_all = False
                n_fail += 1
                continue                           # don't abort the folder/run — just this file
            _sh(f'rm -f {json.dumps(src)}', timeout=25)
            n_files += 1
        if moved_all:
            _sh(f'rmdir {json.dumps(f"{sd_root}/{rel}")} 2>/dev/null', timeout=20)
            n_folders += 1
            print(f"  moved {rel}  ({len(fs)} files, {b/1024/1024:.0f} MB)")

    print(f"\nmoved {n_folders} folder(s) / {n_files} file(s), {n_skip} already-done, {n_fail} verify-fail")
    print("NEXT: rebuild the plan + M3U8s, then rescan:")
    print("  python cli_runner.py build-favorites-playlists-plan")
    print("  python walkman_playlist_import.py")
    print("  # then a device 'Update database' rescan; favorites need NO re-star (moves, not deletes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
