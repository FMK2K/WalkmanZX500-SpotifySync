"""
walkman_incompleteness_scan.py — one read-only sweep of the device that
reports every axis of incompleteness in one place:

  1. missing embedded cover art + incomplete tags (artist/album/title/genre/
     track#/year)  — sampled header pulls
  2. incomplete albums + artist catalogs                — artist_completeness_report.json
  3. incomplete playlists                               — favorites_playlists_plan.json vs device
  4. un-organised folders + splintered albums           — folder-structure analysis
  5. storage distribution (% used, per-artist weight)   — bulk size scan

Writes `incompleteness_report.json` and prints a summary. No device writes.

Usage:
    python walkman_incompleteness_scan.py
    python walkman_incompleteness_scan.py --tag-sample 600   # header-pull N files for tag/art check
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
import walkman_dedup as wd
import adb_transfer as adb

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent / "state"   # <repo>/state (src/ reorg)
_ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}
_HDR = 300_000
MUSIC = getattr(config, "MUSIC_ROOT", "Music")


def _sd_root():
    return adb._get_storage_path("SD card")


def _int_root():
    return adb._get_storage_path("Internal shared storage")


def _df(path):
    try:
        out = subprocess.run(["adb", "shell", f"df {path}"], capture_output=True, text=True, env=_ENV).stdout
        for l in out.splitlines()[1:]:
            p = l.split()
            if len(p) >= 5:
                return int(p[1]) * 1024, int(p[2]) * 1024, int(p[3]) * 1024  # total, used, free (bytes)
    except Exception:
        pass
    return 0, 0, 0


def _tag_check(f):
    """header-pull one file, return dict of what's present/missing."""
    import tempfile, shutil
    from mutagen import File as MF
    tmp = Path(tempfile.mkdtemp())
    lp = tmp / f["filename"]
    try:
        ok = adb.read_file_header_from_device(
            src_parts=f["parts"], filename=f["filename"], dest_local=lp,
            device_name=config.DEVICE_NAME, storage_name=f["storage"], max_bytes=_HDR)
        if not (ok and lp.exists()):
            return None
        a = MF(lp)
        if a is None:
            return None
        t = a.tags or {}
        def g(*keys):
            for k in keys:
                v = t.get(k)
                if v:
                    return str(v[0] if isinstance(v, list) else v).strip()
            return ""
        has_art = bool(getattr(a, "pictures", None)) or any(str(k).startswith("APIC") or k == "covr" for k in t.keys())
        is_flac = f["filename"].lower().endswith(".flac")
        return {
            "art":   has_art,
            "artist": bool(g("artist", "TPE1", "\xa9ART")),
            "album":  bool(g("album", "TALB", "\xa9alb")),
            "albumartist": bool(g("albumartist", "TPE2", "aART")),
            "title":  bool(g("title", "TIT2", "\xa9nam")),
            "genre":  bool(g("genre", "TCON", "\xa9gen")),
            "track":  bool(g("tracknumber", "TRCK", "trkn")),
            "year":   bool(g("date", "TDRC", "year", "\xa9day")),
        }
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag-sample", type=int, default=500)
    args = ap.parse_args()

    rep: dict = {"generated": __import__("datetime").datetime.now().isoformat(timespec="seconds")}

    # ── bulk scan ───────────────────────────────────────────────────────────
    files, sizes = wd._bulk_device_scan(wd._default_storages())
    rep["device_files"] = len(files)
    print(f"device: {len(files)} audio files")

    # ── 5. storage distribution ─────────────────────────────────────────────
    sd_t, sd_u, sd_f = _df(_sd_root())
    in_t, in_u, in_f = _df(_int_root())
    by_storage_bytes = collections.Counter()
    by_artist_bytes = collections.Counter()
    for f in files:
        sz = sizes.get((f["storage"], tuple(f["parts"]), f["filename"]), 0)
        by_storage_bytes[f["storage"]] += sz
        parts = f["parts"]
        # artist = first path component under Music that isn't the album
        art = parts[1] if len(parts) >= 3 and parts[0].lower() == MUSIC.lower() else (parts[-1] if parts else "?")
        by_artist_bytes[art] += sz
    rep["storage"] = {
        "sd_card":  {"total_gb": round(sd_t/1e9, 1), "used_gb": round(sd_u/1e9, 1), "free_gb": round(sd_f/1e9, 1),
                     "pct_used": round(sd_u/sd_t*100, 1) if sd_t else 0,
                     "music_gb": round(by_storage_bytes.get("SD card", 0)/1e9, 1)},
        "internal": {"total_gb": round(in_t/1e9, 1), "used_gb": round(in_u/1e9, 1), "free_gb": round(in_f/1e9, 1),
                     "pct_used": round(in_u/in_t*100, 1) if in_t else 0,
                     "music_gb": round(by_storage_bytes.get("Internal shared storage", 0)/1e9, 1)},
        "music_split_pct": {
            "sd": round(by_storage_bytes.get("SD card", 0) / max(sum(by_storage_bytes.values()), 1) * 100, 1),
            "internal": round(by_storage_bytes.get("Internal shared storage", 0) / max(sum(by_storage_bytes.values()), 1) * 100, 1),
        },
        "heaviest_artists": [{"artist": a, "gb": round(b/1e9, 2)} for a, b in by_artist_bytes.most_common(15)],
    }

    # ── 4. folders / splintered albums ──────────────────────────────────────
    folders = {(f["storage"], tuple(f["parts"])) for f in files}
    top_level = set()
    nested = collections.defaultdict(set)
    music_music = 0
    for st, parts in folders:
        if not parts or parts[0].lower() != MUSIC.lower():
            continue
        rel = parts[1:]
        if len(rel) == 1:
            top_level.add(rel[0])
        elif len(rel) == 2:
            nested[rel[0]].add(rel[1])
        if len(rel) >= 2 and rel[0].lower() == MUSIC.lower():
            music_music += 1
    nested_albums = {b for albs in nested.values() for b in albs}
    split = top_level & nested_albums
    bare = [t for t in top_level if " - " not in t and t.lower() != MUSIC.lower()]
    # filename-keyed album -> set of distinct folders (the real splinter measure)
    alb_folders = collections.defaultdict(set)
    for f in files:
        stem = f["filename"].rsplit(".", 1)[0]
        bits = [b.strip() for b in stem.split(" - ")]
        if len(bits) >= 3:
            alb_folders[(wd._norm(bits[0]), wd._norm(bits[1]))].add((f["storage"], tuple(f["parts"])))
    splintered = {k: len(v) for k, v in alb_folders.items() if len(v) > 1}
    rep["folders"] = {
        "total_music_folders": len(folders),
        "top_level_bare_album_folders": len(bare),
        "album_names_split_between_bare_and_artist_folder": len(split),
        "music_music_nested_folders": music_music,
        "albums_across_multiple_folders": len(splintered),
        "worst_splinters": sorted(
            [{"artist_album": f"{a}/{al}", "folders": n} for (a, al), n in splintered.items()],
            key=lambda x: -x["folders"])[:20],
    }
    print(f"folders: {len(bare)} bare album folders, {len(split)} split names, "
          f"{music_music} under Music/Music/, {len(splintered)} albums across >1 folder")

    # ── 3. playlists ────────────────────────────────────────────────────────
    pl_rep = []
    try:
        plan = json.loads((ROOT / "favorites_playlists_plan.json").read_text(encoding="utf-8"))
        # device file set for a quick resolve check
        devset = set()
        for f in files:
            devset.add(f"{_sd_root()}/" + "/".join(f["parts"] + [f["filename"]]))
            devset.add(f"{_int_root()}/" + "/".join(f["parts"] + [f["filename"]]))
        for name, pd in plan.get("playlists", {}).items():
            res = pd.get("resolved", [])
            miss = pd.get("missing", [])
            on_dev = 0
            for e in res:
                d = e.get("device") or {}
                root = _sd_root() if d.get("storage") == "SD card" else _int_root()
                if f"{root}/" + "/".join(d.get("parts", []) + [d.get("filename", "")]) in devset:
                    on_dev += 1
            pl_rep.append({"playlist": name, "spotify_tracks": len(res) + len(miss),
                           "resolved": len(res), "on_device_now": on_dev, "unresolved": len(miss)})
        fav = plan.get("favorites", {})
        rep["favorites"] = {"spotify_liked": fav.get("resolved_count", 0) + fav.get("missing_count", 0),
                            "resolved": fav.get("resolved_count", 0), "unresolved": fav.get("missing_count", 0)}
    except Exception as e:
        print(f"  playlist check skipped: {e}")
    rep["playlists"] = sorted(pl_rep, key=lambda p: p["resolved"] - p["on_device_now"], reverse=True)
    if pl_rep:
        tot_gap = sum(p["spotify_tracks"] - p["on_device_now"] for p in pl_rep)
        print(f"playlists: {len(pl_rep)} playlists, {tot_gap} total (spotify - on-device) gap")

    # ── 2. incomplete albums / catalogs ─────────────────────────────────────
    try:
        acr = json.loads((ROOT / "artist_completeness_report.json").read_text(encoding="utf-8"))
        arts = acr["artists"] if isinstance(acr, dict) else acr
        cat = []
        for a in arts:
            albs = a.get("albums", [])
            inc = [x for x in albs if x.get("missing_count")]
            absent = [x for x in inc if x["expected"] - x["missing_count"] <= 0]
            cat.append({"artist": a["artist"], "liked": a.get("liked", 0), "saved_albums": a.get("saved_albums", 0),
                        "albums_checked": len(albs), "incomplete_albums": len(inc),
                        "absent_albums": len(absent),
                        "missing_tracks": sum(x["missing_count"] for x in inc)})
        rep["catalogs"] = sorted(cat, key=lambda c: -c["missing_tracks"])
        rep["catalog_totals"] = {
            "artists": len(cat),
            "incomplete_albums": sum(c["incomplete_albums"] for c in cat),
            "absent_albums": sum(c["absent_albums"] for c in cat),
            "missing_tracks": sum(c["missing_tracks"] for c in cat),
        }
        print(f"catalogs: {rep['catalog_totals']['incomplete_albums']} incomplete albums, "
              f"{rep['catalog_totals']['missing_tracks']} missing tracks across {len(cat)} artists")
    except Exception as e:
        print(f"  catalog check skipped ({e}) — run `cli_runner.py artist-completeness-report` first")

    # ── 1. cover art + tag completeness (sampled) ───────────────────────────
    import random
    random.seed(13)
    audio = [f for f in files if f["filename"].lower().endswith((".mp3", ".flac"))]
    sample = random.sample(audio, min(args.tag_sample, len(audio)))
    tallies = collections.Counter()
    checked = 0
    for f in sample:
        r = _tag_check(f)
        if r is None:
            continue
        checked += 1
        for k, present in r.items():
            if not present:
                tallies[k] += 1
        if checked % 100 == 0:
            print(f"  tag-checked {checked}/{len(sample)} ...")
    rep["tags"] = {
        "sampled": checked,
        "of_total": len(audio),
        "missing_pct": {k: round(v / checked * 100, 1) for k, v in tallies.items()} if checked else {},
        "est_missing_libwide": {k: int(v / checked * len(audio)) for k, v in tallies.items()} if checked else {},
    }
    if checked:
        print(f"tags (sample {checked}): " + ", ".join(
            f"{k} {rep['tags']['missing_pct'][k]}% missing (~{rep['tags']['est_missing_libwide'][k]})"
            for k in ("art", "genre", "year", "albumartist", "album", "artist", "title", "track")
            if rep['tags']['missing_pct'].get(k)))

    (ROOT / "incompleteness_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n-> incompleteness_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
