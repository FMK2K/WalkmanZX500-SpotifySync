"""
walkman_deluxe_merge.py — collapse "<Album>" + "<Album> (Deluxe/Extended/
Expanded/…)" folder pairs into one folder, and renumber the tracks against
the canonical (deluxe) tracklist from Deezer.

Example: "The Forever Story" and "The Forever Story (Extended)" both sit on
the device, each half-present and mis-numbered. This merges them into
Music/JID/The Forever Story/ with every track named
"JID - The Forever Story - NN Title.ext" per Deezer's Extended tracklist.

Rules:
  - group device folders by (norm_artist, base_album) where base_album is
    the album name with a deluxe/edition suffix stripped
  - only act on a group that actually has a suffixed folder AND an
    unsuffixed (or differently-suffixed) sibling — pure splinters
    (Music/X vs Music/Artist/X, same name) are walkman_desplinter.py's job
  - target folder: Music/<AlbumArtist>/<base_album>/
  - fetch the most-complete Deezer edition's tracklist; match each file by
    normalised title -> canonical track number; rename in place / on move
  - within the merged folder, keep one copy per title (FLAC > MP3 > M4A,
    then larger); delete the rest
  - favorited / playlisted files are PINNED: never moved, renamed, or
    deleted (queue nothing — the desplinter/fav-upgrade tools own those)
  - --execute required; dry-run by default

Usage:
    python walkman_deluxe_merge.py
    python walkman_deluxe_merge.py --execute
    python walkman_deluxe_merge.py --artist "JID"        # limit to one artist
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import adb_transfer as adb
import walkman_dedup as wd
import album_completeness as ac

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MUSIC_ROOT = getattr(config, "MUSIC_ROOT", "Music")
_BAD = set('<>:"/\\|?*')

_SUFFIX = re.compile(
    r"\s*(?:"
    r"[\(\[][^)\]]*(?:deluxe|expanded|extended|anniversary|complete|"
    r"super\s*deluxe|bonus\s*track|remaster|reissue|collector|edition)[^)\]]*[\)\]]"
    r"|[-–]\s*(?:deluxe|expanded|extended|the\s+extended)(?:\s+(?:edition|version))?"
    r"|\+"
    r")\s*$",
    re.I,
)

_QUAL = {".flac": 3, ".wav": 3, ".aiff": 3, ".mp3": 2, ".m4a": 2, ".aac": 2, ".ogg": 1, ".opus": 1}


def _sanitize(s: str) -> str:
    return "".join(c for c in (s or "") if c not in _BAD).strip().rstrip(".")


def _base_album(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _SUFFIX.sub("", name).strip()
    return name


_NUM = re.compile(r"^\d{1,3}(?:-\d{1,3})?[\s.\-]+")


def _split_name(filename: str):
    stem = os.path.splitext(filename)[0]
    bits = [b.strip() for b in stem.split(" - ")]
    if len(bits) >= 3:
        # bits[2:] still carries the leading "NN " track number — strip it so
        # `title` is the bare song title.
        title = _NUM.sub("", " - ".join(bits[2:])).strip()
        return bits[0], bits[1], title
    return None, None, None


def _title_only(t: str) -> str:
    return wd._norm(wd._strip_feat(_NUM.sub("", t or "")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--artist", default="")
    args = ap.parse_args()

    protected = set()
    try:
        protected = wd._load_favplaylist_protected_paths()
    except Exception:
        pass

    files, _ = wd._bulk_device_scan(wd._default_storages())
    print(f"scanned {len(files)} file(s), {len(protected)} protected path(s)")

    # folder -> list of files;  (norm_artist, base_album) -> set of folders
    folder_files: dict = collections.defaultdict(list)
    for f in files:
        folder_files[(f["storage"], tuple(f["parts"]))].append(f)

    groups: dict = collections.defaultdict(list)   # (nartist, nbase) -> [(storage,parts,[files]), ...]
    raw: dict = collections.defaultdict(collections.Counter)
    for (storage, parts), gfiles in folder_files.items():
        if not parts:
            continue
        # derive artist/album from the files' names (reliable), fall back to folder
        arts = collections.Counter()
        albs = collections.Counter()
        for f in gfiles:
            a, al, _ = _split_name(f["filename"])
            if a and al:
                arts[a] += 1
                albs[al] += 1
        if not arts or not albs:
            continue
        artist = arts.most_common(1)[0][0]
        album = albs.most_common(1)[0][0]
        base = _base_album(album)
        key = (wd._norm(artist), wd._norm(base))
        groups[key].append((storage, parts, gfiles, album))
        raw[key][(artist, base)] += len(gfiles)

    # keep only groups with a genuine deluxe/base distinction
    targets = []
    for key, members in groups.items():
        if args.artist and key[0] != wd._norm(args.artist):
            continue
        album_names = {m[3] for m in members}
        has_suffixed = any(_SUFFIX.search(n) for n in album_names)
        if len(members) < 2 or not has_suffixed:
            continue
        if len({wd._norm(n) for n in album_names}) < 2:
            continue   # all the same normalized name -> a pure splinter, skip
        targets.append((key, members))

    print(f"deluxe/base folder groups to merge: {len(targets)}\n")

    n_ren = n_move = n_del = n_pin = n_nomatch = 0

    for (nartist, nbase), members in targets:
        (artist, base), _ = raw[(nartist, nbase)].most_common(1)[0]
        storage = collections.Counter(m[0] for m in members).most_common(1)[0][0]
        tgt_parts = [MUSIC_ROOT, _sanitize(artist), _sanitize(base)]
        tgt_dir = adb._get_storage_path(storage) + "/" + "/".join(tgt_parts)
        print(f"== {artist} — {base}")
        for m in members:
            print(f"     from {m[0]}/{'/'.join(m[1])}  ({len(m[2])} files, '{m[3]}')")
        print(f"     ->   {storage}/{'/'.join(tgt_parts)}")

        # canonical tracklist (prefer the most-complete edition)
        tl = []
        try:
            albums = ac.fetch_artist_albums(artist, include_types=("album", "ep"))
            cand = [a for a in albums if wd._norm(_base_album(a["name"])) == nbase]
            cand.sort(key=lambda a: -(a.get("total_tracks") or 0))
            if cand:
                tl = ac.fetch_album_tracklist(cand[0]["id"])
        except Exception as e:
            print(f"     ! tracklist fetch failed: {e}")
        num_by_title = {_title_only(t["title"]): t["track_number"] for t in tl}
        if tl:
            print(f"     canonical tracklist: {len(tl)} tracks (Deezer)")
        else:
            print(f"     ! no canonical tracklist — will keep existing numbers")

        # gather every file, choose best per title
        best: dict = {}   # title_key -> (score, storage, parts, filename, is_protected)
        for (mstor, mparts, mfiles, _alb) in members:
            for f in mfiles:
                a, al, title = _split_name(f["filename"])
                if not title:
                    continue
                tk = _title_only(title)
                hp = f"{mstor}/" + "/".join(list(mparts) + [f["filename"]])
                prot = hp.replace("\\", "/").strip() in protected
                ext = os.path.splitext(f["filename"])[1].lower()
                score = (_QUAL.get(ext, 0), f.get("size") or 0)
                cur = best.get(tk)
                # a protected file always wins its slot (we must not delete it)
                if cur is None or (prot and not cur[4]) or (prot == cur[4] and score > cur[0]):
                    best[tk] = (score, mstor, mparts, f["filename"], prot, title, ext)

        keep_paths = set()
        for tk, (score, mstor, mparts, fn, prot, title, ext) in best.items():
            num = num_by_title.get(tk)
            if num:
                new_fn = f"{_sanitize(artist)} - {_sanitize(base)} - {int(num):02d} {_sanitize(title)}{ext}"
            else:
                n_nomatch += 1
                new_fn = fn   # keep as-is
            src = adb._get_storage_path(mstor) + "/" + "/".join(list(mparts) + [fn])
            dst = tgt_dir + "/" + new_fn
            keep_paths.add(dst)
            if src == dst:
                continue
            # 2026-09-07 — a favorited/playlisted file IS moved/renamed now (was
            # PINned before, which left GKMC-style base+(Deluxe) splits
            # unmerged). A move keeps the content-matched favorite and favsync
            # rebuilds the M3U8s right after. Protection is deletion-only (the
            # delete loop below still skips protected files).
            ptag = "  [fav/pl]" if prot else ""
            if prot:
                n_pin += 1
            print(f"     {'REN ' if list(mparts)==tgt_parts else 'MOVE'} {fn}  ->  {new_fn}{ptag}")
            if args.execute:
                if adb._exists(dst) and dst not in keep_paths:
                    continue
                ok = adb._mv(src, dst)
                if ok:
                    if list(mparts) == tgt_parts:
                        n_ren += 1
                    else:
                        n_move += 1

        # delete leftover non-protected files in the source folders that aren't a "best"
        for (mstor, mparts, mfiles, _alb) in members:
            for f in mfiles:
                hp = f"{mstor}/" + "/".join(list(mparts) + [f["filename"]])
                if hp.replace("\\", "/").strip() in protected:
                    continue
                a, al, title = _split_name(f["filename"])
                if not title:
                    continue
                tk = _title_only(title)
                b = best.get(tk)
                if not b:
                    continue
                _, bstor, bparts, bfn, *_ = b
                if (mstor, tuple(mparts), f["filename"]) == (bstor, tuple(bparts), bfn):
                    continue   # this IS the best copy
                print(f"     DEL  {mstor}/{'/'.join(mparts)}/{f['filename']}  (dup of kept '{title}')")
                if args.execute:
                    p = adb._get_storage_path(mstor) + "/" + "/".join(list(mparts) + [f["filename"]])
                    adb._rm_f(p)
                    if not adb._exists(p):
                        n_del += 1
        print()

    verb = "done" if args.execute else "DRY-RUN"
    print(f"{verb}: renamed={n_ren} moved={n_move} deleted_dupes={n_del} "
          f"pinned={n_pin} unmatched_titles={n_nomatch}")
    if not args.execute:
        print("pass --execute to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
