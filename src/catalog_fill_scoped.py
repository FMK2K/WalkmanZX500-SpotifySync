"""
catalog_fill_scoped.py — fill every incomplete album for a followed artist,
absent OR partial, without manufacturing duplicates.

catalog_fill_download.py trusts artist_completeness_actionable.json's
"X% missing" flags directly, which are noisy (title typos, feat-credit
variants, misfiled tracks, Deezer-vs-real tracklist drift). This variant
adds a real per-track device check on top:

  For each album by a followed artist (from artist_completeness_report.json,
  Deezer discography x device scan):
    - drop deluxe / live / remix / compilation / soundtrack editions
    - drop < 5-track releases
    - for every track the completeness matcher marked missing, do a direct
      fuzzy filesystem check (album folder + title, both fuzzy-normalised).
      A track that IS found is dropped from the queue — the "missing" flag
      was a norm mismatch, not a real gap.
    - queue only the genuinely-absent tracks.

The downloader's own dev_lib.has_track() still skips anything it finds
present, and the pipeline's desplinter + dedup + fav_upgrade stages clean
up anything that slips through — so this is layered, not fragile.

Usage:
    python catalog_fill_scoped.py --dry-run
    python catalog_fill_scoped.py
    python catalog_fill_scoped.py --absent-only     # old behaviour
    python catalog_fill_scoped.py --min-tracks 5 --max-queue 4000
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import downloader
import walkman_dedup as wd
import catalog_fill_download as cf

ROOT = Path(__file__).resolve().parent.parent / "state"   # <repo>/state (src/ reorg)
REPORT = ROOT / "artist_completeness_report.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BAD_ALBUM = re.compile(
    r"(deluxe|expanded|extended|instrumental|remix|a ?cappella|chopped|screwed|"
    r"\blive\b|session|karaoke|tribute|\bmix\b|commentary|reprise|\bedit\b|"
    r"\+$|\(.*version\)|anniversary|soundtrack|\bscore\b|best of|greatest hits|"
    r"\bcollection\b|compilation|b-sides|\bdemos?\b|\bmono\b|\bpack\b|reissue|"
    r"remaster|\bvol\.?\s*\d|\bpt\.?\s*\d|\bep\b.*remix|freestyle|screwed|"
    r"presents|and friends|meets|highlights|collectors?\s*edition|\bredux\b|"
    r"bonus\s*edition|alc\s*edition|preluxe|immortal|gangsta\s*grillz|"
    r"\bmixtape\b|the\s*cutting\s*room|russian\s*roulettte|"
    # 2026-09-07 — more greatest-hits / non-studio patterns that leaked through
    r"world\s*tour|\btour\b|\bhits\b|number\s*ones?|\bthis\s*is\s*it\b|"
    r"love\s*songs|\bmotown\b|\bicon\b|\bessentials?\b|\bduets?\b|\bthe\s*best\b|"
    r"looking\s*back|\banthology\b|\bplaylist\b|\bunplugged\b|\brarities\b|"
    r"\bthe\s*remixes\b|\bremixed\b|\bfrom\s*the\s*vault\b|"
    r"director'?s\s*cut|\be-?single\b|chop\s*not\s*slop|chopnotslop|"
    r"stripped\s*mix|\bslowed\b|\bsped\s*up\b|\bacoustic\b\s*(?:ep|version|sessions?)?$)",
    re.I,
)
_NUM = re.compile(r"^\d{1,3}[\s.\-]+")
_PARENS = re.compile(r"[\(\[\{].*?[\)\]\}]")


def _tnorm(t: str) -> str:
    t = _NUM.sub("", t or "")
    t = _PARENS.sub("", t)
    return wd._norm(wd._strip_feat(t))


def build_tracklist(*, absent_only=False, min_tracks=5, max_queue=6000):
    if not REPORT.exists():
        print("no artist_completeness_report.json — run "
              "`python cli_runner.py artist-completeness-report` first")
        return []
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    arts = rep["artists"] if isinstance(rep, dict) else rep
    # Re-check every "missing" track against the device with the SAME flat
    # per-album fuzzy index the report uses (unions split folders; fuzzy title
    # match) — a true safety net that also catches anything grabbed since the
    # report ran.
    import device_library as _devlib
    import walkman_deluxe_merge as _dm
    _fz = _devlib._fuzzy_norm_title
    # strip "(Deluxe)/(Extended)/…" AND ": Deluxe Edition" / " Ultra" / " Anniversary"
    _EXP = re.compile(r"\s*(?::\s*)?(?:deluxe(?:\s*edition)?|ultra|expanded|extended|"
                      r"anniversary(?:\s*edition)?|complete\s*edition|the\s*extended)\s*$", re.I)
    def _basealb(name: str) -> str:
        prev = None
        while prev != name:
            prev = name
            name = _EXP.sub("", _dm._base_album(name)).strip()
        return name

    files, _ = wd._bulk_device_scan(wd._default_storages())
    by_album: dict = {}
    for f in files:
        stem = f["filename"].rsplit(".", 1)[0]
        bits = [b.strip() for b in stem.split(" - ")]
        title = bits[-1] if bits else stem
        alb = bits[1] if len(bits) >= 3 else (f["parts"][-1] if f["parts"] else "")
        fld = f["parts"][-1] if f["parts"] else ""
        # index each title under the full name, the folder name, AND the
        # base-album name — so "Rave & Roses" gaps are satisfied by
        # "Rave & Roses Ultra" files (user has one edition, not both).
        keys = {wd._norm(alb), wd._norm(fld), wd._norm(_basealb(alb)), wd._norm(_basealb(fld))}
        for a in keys:
            if a:
                by_album.setdefault(a, set()).add(_fz(title))
    print(f"device: {len(files)} file(s), {len(by_album)} album key(s)")

    def _present(_artist, album, title):
        dev = by_album.get(wd._norm(album)) or by_album.get(wd._norm(_basealb(album)))
        return bool(dev) and _devlib._fuzzy_title_in(title, dev)

    # 1st pass classify every album, 2nd pass queue partials first (album folder
    # exists = highest-confidence intent), then deep-artist absent albums, so a
    # --max-queue cap keeps the best.
    classified = []   # (artist, name, really_missing, exp, status)
    for a in arts:
        artist = a["artist"]
        liked = a.get("liked", 0)
        saved = a.get("saved_albums", 0)
        if saved < 1 and liked < 8:
            continue
        deep = saved >= 2 or liked >= 15
        for g in a.get("albums", a.get("gaps", [])):
            name = g["album"]
            exp = g.get("expected", 0)
            if exp < min_tracks or _BAD_ALBUM.search(name):
                continue
            really_missing = [t for t in g.get("missing", [])
                              if len(_fz(t)) >= 2 and not _present(artist, name, t)]
            present_ct = exp - len(really_missing)
            status = "ABSENT" if present_ct <= 0 else ("COMPLETE" if not really_missing else "PARTIAL")
            if status == "COMPLETE":
                continue
            if absent_only and status != "ABSENT":
                continue
            if status == "ABSENT" and not (deep and exp >= 6):
                continue
            classified.append((artist, name, really_missing, exp, status))

    tracks, seen = [], set()
    per_album = []
    for want in ("PARTIAL", "ABSENT"):
        for artist, name, really_missing, exp, status in classified:
            if status != want:
                continue
            n0 = len(tracks)
            for t in really_missing:
                k = (artist.strip().lower(), _tnorm(t))
                if not k[1] or k in seen or len(tracks) >= max_queue:
                    continue
                seen.add(k)
                tracks.append(cf._mk_track(artist, t, name))
            per_album.append((artist, name, len(tracks) - n0, exp, status))
    if len(tracks) >= max_queue:
        print(f"\n! hit max-queue cap ({max_queue})")

    absent = [x for x in per_album if x[4] == "ABSENT"]
    partial = [x for x in per_album if x[4] == "PARTIAL"]
    print(f"\n{len(absent)} absent album(s), {len(partial)} partial album(s) "
          f"-> {len(tracks)} track(s) to fetch\n")
    for tag, rows in (("ABSENT", absent), ("PARTIAL", partial)):
        for artist, name, q, exp, _ in sorted(rows, key=lambda r: -r[2])[:60]:
            print(f"   {tag:7} +{q:2}/{exp:2}  {artist} — {name}")
        if len(rows) > 60:
            print(f"   ... and {len(rows) - 60} more {tag.lower()} albums")
    return tracks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--absent-only", action="store_true")
    ap.add_argument("--min-tracks", type=int, default=5)
    ap.add_argument("--max-queue", type=int, default=6000)
    args = ap.parse_args()

    tracks = build_tracklist(absent_only=args.absent_only,
                             min_tracks=args.min_tracks, max_queue=args.max_queue)
    if args.dry_run or not tracks:
        print(f"\n[dry-run] {len(tracks)} track(s) — pass no --dry-run to fetch")
        return 0

    track_map = {"__catalog_fill_scoped__": tracks}
    cancelled = [False]
    stats = downloader.run_downloads(track_map, cancelled, liked_album_ids=set(), sp_client=None)
    print(f"[CatalogFillScoped] Done — {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
