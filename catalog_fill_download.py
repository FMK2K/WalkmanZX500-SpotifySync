"""
catalog_fill_download.py — download the artist-catalog gap identified by
artist-completeness-report + its filter.

Feeds two lists into downloader.run_downloads():
  - catalog_partial_albums.json : 87 core albums the user has clearly
    started (25-85% present) -> finish them. Vetted, clean.
  - artist_completeness_actionable.json : entirely-absent core albums for
    deep-engagement artists (saved_albums>=2), release year >= 2018, with
    deluxe/compilation/reissue/edition markers dropped.

Each missing track becomes a minimal track dict (artist/title/album,
_completeness_fill=True); the downloader waterfall resolves it by search.

Usage: python catalog_fill_download.py [--dry-run]
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import downloader

ROOT = Path(__file__).parent

# noise markers for the absent-album list (partials are already vetted)
_BAD_ALBUM = re.compile(
    r"(deluxe|expanded|collector|edition\.|\(clean\)|\bredux\b|preluxe|"
    r"alternate version|extended version|\(.*version\)|trilogy|highlights|"
    r"black hippy|the mix|book i|dance floor|parte 2|\bp\.s\.\b|alternate world|"
    r"remaster|\bpack\b|carnival|director|grillz|forest hills|nitro|intl|"
    r"chopnotslop|thriller 40|thriller 25|bad 25|super deluxe|anniversary)",
    re.I,
)


def _mk_track(artist: str, title: str, album: str) -> dict:
    # The downloader skips any track with no `id` (it's the dl_cache key --
    # see downloader.py ~L3662). Our catalog-gap data is ID-less (Deezer
    # title strings only), and the provider waterfall matches by artist/title
    # string anyway, so a stable synthetic id is enough to get past the gate.
    syn = "cat_" + hashlib.sha1(f"{artist}|{title}|{album}".lower().encode()).hexdigest()[:20]
    return {
        "id": syn, "title": title, "artist": artist, "artists": [artist],
        "album": album, "album_artist": artist, "album_id": "",
        "track_number": 1, "disc_number": 1, "is_liked": False,
        "_completeness_fill": True, "_reason": "artist-catalog-gap",
    }


def build_tracklist() -> list:
    tracks: list = []
    seen: set = set()

    def _add(artist, title, album):
        k = (artist.strip().lower(), title.strip().lower())
        if not k[0] or not k[1] or k in seen:
            return
        seen.add(k)
        tracks.append(_mk_track(artist, title, album))

    # 1) partial albums (clean)
    partial = json.loads((ROOT / "catalog_partial_albums.json").read_text(encoding="utf-8"))
    for e in partial:
        for t in e.get("missing", []):
            _add(e["artist"], t, e["album"])
    n_partial = len(tracks)

    # 2) deep-artist absent core albums, filtered
    act = json.loads((ROOT / "artist_completeness_actionable.json").read_text(encoding="utf-8"))
    for a in act:
        if (a.get("saved_albums") or 0) < 2:
            continue
        for g in a.get("gaps", []):
            if not g.get("absent"):
                continue
            if _BAD_ALBUM.search(g["album"]):
                continue
            yr = (g.get("release_date") or "0000")[:4]
            if yr < "2018":
                continue
            if (g.get("expected") or 0) < 4:
                continue
            for t in g.get("missing", []):
                _add(a["artist"], t, g["album"])
    n_absent = len(tracks) - n_partial

    print(f"partial-album tracks: {n_partial}")
    print(f"absent-album tracks (deep artists, filtered): {n_absent}")
    print(f"total (deduped): {len(tracks)}")
    return tracks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    tracks = build_tracklist()
    if args.dry_run:
        for t in tracks[:40]:
            print(f"  {t['artist']} - {t['title']}  ({t['album']})")
        print(f"  ... ({len(tracks)} total)")
        return 0

    track_map = {"__catalog_fill__": tracks}
    cancelled: list = [False]   # downloader checks cancelled[0] as the abort flag
    stats = downloader.run_downloads(track_map, cancelled, liked_album_ids=set(),
                                     sp_client=None)
    print(f"[CatalogFill] Done — {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
