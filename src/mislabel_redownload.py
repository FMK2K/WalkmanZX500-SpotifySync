"""
mislabel_redownload.py — replace the files flagged by duration_verify.py as
mislabelled / oversized with a fresh clean copy.

mislabel_redownload_pending.json holds {artist, title, album, reason}:
  - "OVERSIZED FILE ..."          : local file is far longer than the real
    track (usually a full DJ mix / album rip misfiled as one song)
  - "HIGH-CONFIDENCE WRONG TRACK" : a different song entirely
  - "MANUAL ..."                  : user-confirmed

The downloader skips any track dev_lib.has_track() already finds, so a
plain re-feed does nothing — the bad file has to be deleted first. This:

  1. locates the bad file on the device (norm artist+title)
  2. for OVERSIZED, sanity-checks the real device file size (a genuine
     single is < ~18 MB at MP3-128 / < ~60 MB at FLAC) so a correctly
     large legit track isn't nuked
  3. deletes it (records whether it was favorited / playlisted)
  4. feeds every pending entry to downloader.run_downloads() for a fresh copy
  5. reports — LOUDLY if a delete succeeded but the re-download failed

Usage:
    python mislabel_redownload.py --dry-run
    python mislabel_redownload.py
    python mislabel_redownload.py --wrong-only     # skip the OVERSIZED bucket
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import adb_transfer as adb
import walkman_dedup as wd
import downloader
import catalog_fill_download as cf

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent / "state"   # <repo>/state (src/ reorg)
PENDING = ROOT / "mislabel_redownload_pending.json"
PLAN = ROOT / "favorites_playlists_plan.json"
RESTAR_OUT = ROOT / "mislabel_restar.json"

# a real single shouldn't exceed this on disk (bytes) — bigger => the
# "OVERSIZED" flag is trustworthy
_MAX_SINGLE = {".mp3": 22_000_000, ".m4a": 25_000_000, ".flac": 75_000_000,
               ".wav": 120_000_000}


def _norm_key(p: str) -> str:
    return p.replace("\\", "/").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wrong-only", action="store_true")
    args = ap.parse_args()

    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    if args.wrong_only:
        pending = [p for p in pending if "OVERSIZED" not in p.get("reason", "")]
    print(f"pending: {len(pending)}")

    protected = set()
    try:
        protected = wd._load_favplaylist_protected_paths()
    except Exception:
        pass
    plan = {}
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except Exception:
        pass
    fav_paths = set()
    for e in plan.get("favorites", {}).get("resolved", []):
        d = e.get("device")
        if d:
            fav_paths.add(_norm_key(f"{d['storage']}/" + "/".join(d["parts"] + [d["filename"]])))

    files, sizes = wd._bulk_device_scan(wd._default_storages())
    idx: dict = {}
    for f in files:
        stem = os.path.splitext(f["filename"])[0]
        bits = [b.strip() for b in stem.split(" - ")]
        if len(bits) >= 3:
            artist, title = bits[0], " - ".join(bits[2:])
        else:
            artist, title = "", bits[-1]
        import re as _re
        title = _re.sub(r"^\d{1,3}[\s.\-]+", "", title).strip()
        key = (wd._norm(artist), wd._norm(wd._strip_feat(title)))
        idx.setdefault(key, []).append(f)

    to_delete = []      # (device_path, human_path, is_protected, entry)
    no_match = []
    for e in pending:
        key = (wd._norm(e["artist"]), wd._norm(wd._strip_feat(e["title"])))
        hits = idx.get(key, [])
        if not hits:
            no_match.append(e)
            continue
        for f in hits:
            hp = f"{f['storage']}/" + "/".join(f["parts"] + [f["filename"]])
            dev = adb._get_storage_path(f["storage"]) + "/" + "/".join(f["parts"] + [f["filename"]])
            sz = sizes.get((f["storage"], tuple(f["parts"]), f["filename"]), 0)
            ext = os.path.splitext(f["filename"])[1].lower()
            if "OVERSIZED" in e.get("reason", "") and 0 < sz < _MAX_SINGLE.get(ext, 22_000_000):
                # flagged oversized but the file is a normal size — leave it,
                # the duration flag was likely a Deezer-reference mismatch
                continue
            to_delete.append((dev, _norm_key(hp), _norm_key(hp) in protected or _norm_key(hp) in fav_paths, e))

    print(f"bad files to delete: {len(to_delete)}   (no device match: {len(no_match)})")
    prot_hit = [d for d in to_delete if d[2]]
    if prot_hit:
        print(f"  {len(prot_hit)} are favorited/playlisted — will delete + queue re-star")
    for dev, hp, prot, e in to_delete[:30]:
        print(f"  {'[FAV] ' if prot else ''}{hp}   <- {e['reason'][:45]}")
    if len(to_delete) > 30:
        print(f"  ... and {len(to_delete) - 30} more")

    if args.dry_run:
        print("\nDRY-RUN — pass no --dry-run to delete + re-download")
        return 0

    n_del = 0
    restar = set()
    for dev, hp, prot, e in to_delete:
        if adb._exists(dev):
            adb._rm_f(dev)
            if not adb._exists(dev):
                n_del += 1
                if prot:
                    restar.add((e["artist"], e["title"]))
            else:
                print(f"  ! delete failed: {hp}")
    print(f"deleted {n_del} bad file(s)")

    if restar:
        RESTAR_OUT.write_text(json.dumps(
            {"favorites": {"resolved": [{"artist": a, "title": t} for a, t in sorted(restar)]},
             "playlists": {}}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"queued {len(restar)} favorited track(s) for re-star -> {RESTAR_OUT.name}")

    # ── re-download every pending entry ──────────────────────────────────────
    tracks = [cf._mk_track(e["artist"], e["title"], e["album"]) for e in pending]
    track_map = {"__mislabel_redownload__": tracks}
    cancelled = [False]
    stats = downloader.run_downloads(track_map, cancelled, liked_album_ids=set(), sp_client=None)
    print(f"[MislabelRedownload] Done — {stats}")

    # verify: which deleted files did NOT come back?
    files2, _ = wd._bulk_device_scan(wd._default_storages())
    have = set()
    for f in files2:
        stem = os.path.splitext(f["filename"])[0]
        bits = [b.strip() for b in stem.split(" - ")]
        t = " - ".join(bits[2:]) if len(bits) >= 3 else bits[-1]
        import re as _re
        t = _re.sub(r"^\d{1,3}[\s.\-]+", "", t).strip()
        a = bits[0] if len(bits) >= 3 else ""
        have.add((wd._norm(a), wd._norm(wd._strip_feat(t))))
    lost = [e for (_dev, _hp, _p, e) in to_delete
            if (wd._norm(e["artist"]), wd._norm(wd._strip_feat(e["title"]))) not in have]
    if lost:
        print(f"\n!!! {len(lost)} track(s) DELETED but re-download did not land — REVIEW:")
        for e in lost:
            print(f"    {e['artist']} - {e['title']}  ({e['album']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
