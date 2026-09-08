"""
walkman_desplinter.py — collapse split album folders into one canonical
Music/<AlbumArtist>/<Album>/ folder.

Why: after audit run #4 (2026-09-05) the SD card had ~2,616 album names
living as BOTH Music/<Album>/ AND Music/<Artist>/<Album>/, plus ~1,668
folders orphaned under a buggy Music/Music/. artist-completeness /
catalog-fill can't match a half-here / half-there album, so the fill
step re-downloads tracks that are already present and manufactures more
duplicates.

This is a narrow, predictable alternative to re-running the whole audit
(whose _consolidate_duplicate_folders groups by folder-NAME — so two
different albums that share a title get merged — and whose _cleanup pass
makes a DeepSeek call per folder).

How:
  - one bulk `adb ls -lR` scan of the library
  - group files by (norm_artist, norm_album) taken from the FILENAME
    ("Artist - Album - NN Title.ext"), never the folder name
  - for every group whose files span more than one folder, move each file
    to the canonical folder with `adb mv` (instant, same filesystem)
  - favorited / playlisted files ARE moved too (2026-09-07): a move is
    non-destructive, the Sony is_favorites row re-matches by content on the
    next DB rescan, and favsync regenerates the M3U8s afterward. (Was PINned
    before — that left albums permanently folder-split.)
  - if the destination filename already exists, the file is left in place
    and logged as a duplicate for the dedup pass to resolve — never
    overwritten
  - cross-storage members (SD vs internal) are left alone
  - after a real run, now-empty source folders are rmdir'd

Usage:
    python walkman_desplinter.py                # dry-run report
    python walkman_desplinter.py --execute
    python walkman_desplinter.py --execute --no-rmdir
"""
from __future__ import annotations

import os
import sys
import re
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import adb_transfer as adb
import walkman_dedup as wd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MUSIC_ROOT = getattr(config, "MUSIC_ROOT", "Music")
_BAD = set('<>:"/\\|?*')


def _sanitize(s: str) -> str:
    return "".join(c for c in (s or "") if c not in _BAD).strip().rstrip(".")


def _split_name(filename: str):
    """('Artist - Album - NN Title.ext') -> (artist, album) or (None, None)."""
    stem = os.path.splitext(filename)[0]
    bits = [b.strip() for b in stem.split(" - ")]
    if len(bits) < 3:
        return None, None
    artist, album = bits[0], bits[1]
    if not artist or not album:
        return None, None
    return artist, album


def _is_nested_music(parts: list) -> bool:
    return len(parts) >= 2 and parts[0].lower() == MUSIC_ROOT.lower() and parts[1].lower() == MUSIC_ROOT.lower()


def _human(storage: str, parts: list, filename: str) -> str:
    return f"{storage}/" + "/".join(list(parts) + [filename])


def main() -> int:
    execute = "--execute" in sys.argv
    do_rmdir = "--no-rmdir" not in sys.argv

    protected = set()
    try:
        protected = wd._load_favplaylist_protected_paths()
    except Exception as e:
        print(f"[warn] could not load favorites/playlists protection: {e}")
    print(f"protected favorited/playlisted paths: {len(protected)}")

    files, _sizes = wd._bulk_device_scan(wd._default_storages())
    print(f"scanned {len(files)} audio file(s)")
    if not files:
        print("no files — aborting")
        return 1

    # group by (norm_artist, norm_album)
    groups: dict = collections.defaultdict(list)
    raw_names: dict = collections.defaultdict(collections.Counter)
    for f in files:
        artist, album = _split_name(f["filename"])
        if artist is None:
            continue
        key = (wd._norm(artist), wd._norm(album))
        if not key[0] or not key[1]:
            continue
        groups[key].append(f)
        raw_names[key][(artist, album)] += 1

    n_move = n_pin = n_collision = n_crossstorage = 0
    moves: list = []          # (src_path, dest_path, human_src, human_dest)
    touched_src_folders: set = set()

    split_groups = 0
    for key, gfiles in groups.items():
        folders = {(f["storage"], tuple(f["parts"])) for f in gfiles}
        if len(folders) <= 1:
            continue
        split_groups += 1

        # storage with the most files in this group wins
        by_storage = collections.Counter(f["storage"] for f in gfiles)
        canon_storage = by_storage.most_common(1)[0][0]

        # canonical parts: prefer an existing 3-part Music/<A>/<B> folder on the
        # canon storage whose artist normalises to ours and isn't nested-Music;
        # otherwise construct one from the most common raw (artist, album).
        canon_parts = None
        cand_folders = sorted(
            (fp for (st, fp) in folders
             if st == canon_storage and len(fp) == 3
             and fp[0].lower() == MUSIC_ROOT.lower()
             and not _is_nested_music(list(fp))
             and wd._norm(fp[1]) == key[0]),
            key=lambda fp: -sum(1 for f in gfiles if f["storage"] == canon_storage and tuple(f["parts"]) == fp),
        )
        if cand_folders:
            canon_parts = list(cand_folders[0])
        else:
            (r_artist, r_album), _ = raw_names[key].most_common(1)[0]
            canon_parts = [MUSIC_ROOT, _sanitize(r_artist), _sanitize(r_album)]

        if _is_nested_music(canon_parts) or len(canon_parts) != 3 or not canon_parts[1] or not canon_parts[2]:
            continue

        canon_root = adb._get_storage_path(canon_storage)
        canon_dir = canon_root + "/" + "/".join(canon_parts)

        for f in gfiles:
            st, parts, fn = f["storage"], list(f["parts"]), f["filename"]
            if st != canon_storage:
                n_crossstorage += 1
                continue
            if tuple(parts) == tuple(canon_parts):
                continue  # already canonical
            hp = _human(st, parts, fn)
            # 2026-09-07 — a favorited/playlisted file IS moved now (was PINned,
            # which left albums folder-split whenever an old favorited copy sat
            # in a different folder than the rest). desplinter only MOVES (never
            # deletes), a move keeps the content-matched favorite, and favsync
            # rebuilds the M3U8s right after. Same change as walkman_deluxe_merge.
            if hp.replace("\\", "/").strip() in protected:
                n_pin += 1
                print(f"  MOVE (fav/pl) {hp}")
            src_root = adb._get_storage_path(st)
            src_path = src_root + "/" + "/".join(parts + [fn])
            dest_path = canon_dir + "/" + fn
            moves.append((src_path, dest_path, hp, _human(canon_storage, canon_parts, fn)))
            touched_src_folders.add((st, tuple(parts)))

    print(f"\nsplit album groups: {split_groups}")
    print(f"planned moves: {len(moves)}   pinned: {n_pin}   cross-storage skipped: {n_crossstorage}")

    if not execute:
        for src, dest, hs, hd in moves[:40]:
            print(f"  MOVE {hs}\n    -> {hd}")
        if len(moves) > 40:
            print(f"  ... and {len(moves) - 40} more")
        print("\nDRY-RUN — pass --execute to move")
        return 0

    # ── execute ──────────────────────────────────────────────────────────────
    import subprocess

    def _exists(path: str) -> bool:
        try:
            return adb._exists(path)
        except Exception:
            return False

    for i, (src, dest, hs, hd) in enumerate(moves, 1):
        if _exists(dest):
            n_collision += 1
            print(f"  COLLISION (left in place for dedup): {hs}")
            continue
        try:
            ok = adb._mv(src, dest)
        except Exception as e:
            print(f"  ERROR mv {hs}: {e}")
            continue
        if ok:
            n_move += 1
        else:
            print(f"  MV-FAILED {hs}")
        if i % 100 == 0:
            print(f"  ... {i}/{len(moves)}")

    print(f"\nmoved: {n_move}   collisions left for dedup: {n_collision}")

    if do_rmdir and touched_src_folders:
        n_rm = 0
        env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
        for st, parts in sorted(touched_src_folders, key=lambda x: -len(x[1])):
            if list(parts) == [MUSIC_ROOT]:
                continue
            d = adb._get_storage_path(st) + "/" + "/".join(parts)
            try:
                subprocess.run(["adb", "shell", "rmdir", d], capture_output=True, env=env, timeout=20)
                if not _exists(d):
                    n_rm += 1
            except Exception:
                pass
        print(f"removed {n_rm} now-empty source folder(s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
