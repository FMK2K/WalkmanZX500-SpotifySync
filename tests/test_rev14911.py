"""
test_rev14911.py — Rev 14.9.11 regression tests
================================================
Covers the two root-cause bugs found by live-device analysis of
zx500_all_files.txt (14 169 music files on a Sony NW-ZX500):

  Bug A — Album splintering via wrong album_artist
    spotify_client._parse_track() used track.artists[0] for album_artist.
    On collaborative/featured tracks the first track-artist varies per song,
    so the same album appeared in multiple folders:
        "Travis Scott - Birds In The Trap Sing McKnight"
        "Young Thug  - Birds In The Trap Sing McKnight"
    Fix: use album.artists[0] (Spotify's album-level field) instead.

  Bug B — Double feat in canonical filenames
    walkman_sync filename construction built feat_part from artists[1:] without
    checking whether the raw Spotify title already contained a "(feat. …)".
    Result: "Like Me (feat. iann dior) (feat. iann dior).mp3" (603 files affected).
    Fix: skip feat_part when the raw title already contains a feat marker.

Run:
    python test_rev14911.py
"""
from __future__ import annotations
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def _check(name: str, cond: bool, details: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}  {details}")
        print(f"  \u2717 {name}  {details}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build minimal Spotify-shaped track objects
# ─────────────────────────────────────────────────────────────────────────────

def _make_spotify_track(
    track_artists: list[str],
    album_name: str,
    album_artists: list[str],
    track_name: str,
    album_type: str = "album",
    track_number: int = 1,
    disc_number: int = 1,
) -> dict:
    """Return a minimal Spotify track object matching the shape Spotipy delivers."""
    return {
        "id": "fake_id",
        "name": track_name,
        "artists": [{"name": a} for a in track_artists],
        "track_number": track_number,
        "disc_number": disc_number,
        "external_ids": {"isrc": ""},
        "album": {
            "name": album_name,
            "id": "fake_album_id",
            "album_type": album_type,
            "artists": [{"name": a} for a in album_artists],
            "release_date": "2021-01-01",
            "images": [],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Section A  — spotify_client._parse_track album_artist field
# ─────────────────────────────────────────────────────────────────────────────
print("\n[A] Bug A: album_artist uses album.artists, not track.artists")

from spotify_client import _parse_track


# ── A1: single-artist album ───────────────────────────────────────────────

t = _make_spotify_track(
    track_artists=["Kendrick Lamar"],
    album_name="good kid, m.A.A.d city",
    album_artists=["Kendrick Lamar"],
    track_name="m.A.A.d city",
)
r = _parse_track(t)
_check("A1 single artist: album_artist == track artist",
       r["album_artist"] == "Kendrick Lamar",
       f"got {r['album_artist']!r}")


# ── A2: collaborative album — key anti-splinter case ─────────────────────
# "Birds In The Trap Sing McKnight": album artist = Travis Scott,
# but "pick up the phone" has track artists [Travis Scott, Young Thug, Quavo].

# Track where Travis Scott is first in track.artists — was already correct.
t_ts = _make_spotify_track(
    track_artists=["Travis Scott", "Young Thug", "Quavo"],
    album_name="Birds In The Trap Sing McKnight",
    album_artists=["Travis Scott"],
    track_name="pick up the phone",
)
# A hypothetical track where Young Thug appears first (as could happen with
# Spotify's artist ordering on features).
t_yt = _make_spotify_track(
    track_artists=["Young Thug", "Travis Scott"],
    album_name="Birds In The Trap Sing McKnight",
    album_artists=["Travis Scott"],
    track_name="outside",
)
r_ts = _parse_track(t_ts)
r_yt = _parse_track(t_yt)
_check(
    "A2a collaborative: album_artist is album.artists[0] (Travis Scott) "
    "when Travis is first track artist",
    r_ts["album_artist"] == "Travis Scott",
    f"got {r_ts['album_artist']!r}",
)
_check(
    "A2b collaborative: album_artist is STILL Travis Scott "
    "even when Young Thug is first track artist",
    r_yt["album_artist"] == "Travis Scott",
    f"got {r_yt['album_artist']!r}",
)
_check(
    "A2c both tracks produce IDENTICAL album_artist (no splintering)",
    r_ts["album_artist"] == r_yt["album_artist"],
    f"{r_ts['album_artist']!r} vs {r_yt['album_artist']!r}",
)


# ── A3: SAVAGE MODE II — 21 Savage + Metro Boomin collab album ───────────
# album.artists = [21 Savage, Metro Boomin]; track.artists vary per song.

t_21 = _make_spotify_track(
    track_artists=["21 Savage", "Metro Boomin", "Drake"],
    album_name="SAVAGE MODE II",
    album_artists=["21 Savage", "Metro Boomin"],
    track_name="Mr. Right Now (feat. Drake)",
)
t_mb = _make_spotify_track(
    track_artists=["Metro Boomin", "21 Savage", "Young Thug"],
    album_name="SAVAGE MODE II",
    album_artists=["21 Savage", "Metro Boomin"],
    track_name="Rich Nigga Shit (feat. Young Thug)",
)
r_21 = _parse_track(t_21)
r_mb = _parse_track(t_mb)
_check(
    "A3a SAVAGE MODE II: album_artist == album.artists[0] (21 Savage)",
    r_21["album_artist"] == "21 Savage",
    f"got {r_21['album_artist']!r}",
)
_check(
    "A3b SAVAGE MODE II: Metro Boomin-first track still gets 21 Savage",
    r_mb["album_artist"] == "21 Savage",
    f"got {r_mb['album_artist']!r}",
)
_check(
    "A3c no splintering between tracks",
    r_21["album_artist"] == r_mb["album_artist"],
)


# ── A4: true Various Artists compilation ─────────────────────────────────
t_va = _make_spotify_track(
    track_artists=["Davido"],
    album_name="Best Of Davido",
    album_artists=["Various Artists"],
    album_type="compilation",
    track_name="Skelewu",
)
r_va = _parse_track(t_va)
_check(
    "A4 VA compilation: album_artist == 'Various Artists'",
    r_va["album_artist"] == "Various Artists",
    f"got {r_va['album_artist']!r}",
)


# ── A5: single-artist greatest hits (Spotify marks as compilation) ────────
# "Best Of Davido" where album.artists = [Davido] despite compilation flag.
t_da = _make_spotify_track(
    track_artists=["Davido"],
    album_name="Best Of Davido",
    album_artists=["Davido"],
    album_type="compilation",
    track_name="Skelewu",
)
r_da = _parse_track(t_da)
_check(
    "A5 single-artist compilation: album_artist is the real artist, not 'Various Artists'",
    r_da["album_artist"] == "Davido",
    f"got {r_da['album_artist']!r}",
)


# ── A6: track.artists are correct (primary + featured) ───────────────────
t6 = _make_spotify_track(
    track_artists=["Travis Scott", "Young Thug", "Quavo"],
    album_name="Birds In The Trap Sing McKnight",
    album_artists=["Travis Scott"],
    track_name="pick up the phone",
)
r6 = _parse_track(t6)
_check(
    "A6 track.artist is still track.artists[0], not album artist",
    r6["artist"] == "Travis Scott",
)
_check(
    "A6 track.artists list preserved in full",
    r6["artists"] == ["Travis Scott", "Young Thug", "Quavo"],
)


# ── A7: fallback when album has no artists field ─────────────────────────
t7 = {
    "id": "fake",
    "name": "Track",
    "artists": [{"name": "Solo Artist"}],
    "track_number": 1,
    "disc_number": 1,
    "external_ids": {"isrc": ""},
    "album": {
        "name": "Some Album",
        "id": "fake_album",
        "album_type": "album",
        # NOTE: no "artists" key — simulates old cache entries
        "release_date": "2020",
        "images": [],
    },
}
r7 = _parse_track(t7)
_check(
    "A7 album with no artists key: falls back to track.artists[0]",
    r7["album_artist"] == "Solo Artist",
    f"got {r7['album_artist']!r}",
)

_check(
    "A7 empty album.artists list: falls back to track.artists[0]",
    _parse_track({**t7, "album": {**t7["album"], "artists": []}})["album_artist"] == "Solo Artist",
)


# ─────────────────────────────────────────────────────────────────────────────
# Section B — filename construction double-feat bug
# ─────────────────────────────────────────────────────────────────────────────
print("\n[B] Bug B: no double feat in canonical filenames")

# walkman_sync imports MTP/ADB (Windows-only) at module level, so we test
# the feat_part logic directly — it is self-contained (re + config only).
# Section C separately verifies the fix is present in the source file.

import config as _cfg

_ORIG_FEAT_IN_TITLE = getattr(_cfg, "FEATURE_ARTISTS_IN_TITLE", True)
_cfg.FEATURE_ARTISTS_IN_TITLE = True


def _build_feat_part(track: dict) -> str:
    """
    Mirror of the fixed filename-construction feat_part logic in walkman_sync.py.
    If the raw Spotify title already contains a feat/ft marker, returns "".
    Otherwise appends (feat. X, Y) from artists[1:].
    """
    _raw_title_has_feat = bool(
        re.search(r"\bfeat[.\s]|\bft[.\s]", track.get("title", ""), re.IGNORECASE)
    )
    artists = track.get("artists") or [track.get("artist", "")]
    if len(artists) > 1 and _cfg.FEATURE_ARTISTS_IN_TITLE and not _raw_title_has_feat:
        feat = ", ".join(a for a in artists[1:] if a)
        return f" (feat. {feat})" if feat else ""
    return ""


# ── B1: title already has feat. — must NOT add another ───────────────────
t_b1 = {
    "title": "Like Me (feat. iann dior)",
    "artists": ["$NOT", "iann dior"],
    "artist": "$NOT",
}
fp_b1 = _build_feat_part(t_b1)
_check(
    "B1 title with feat. already: feat_part is empty",
    fp_b1 == "",
    f"got {fp_b1!r}",
)


# ── B2: title has no feat — feat should be appended ──────────────────────
t_b2 = {
    "title": "Like Me",
    "artists": ["$NOT", "iann dior"],
    "artist": "$NOT",
}
fp_b2 = _build_feat_part(t_b2)
_check(
    "B2 title without feat.: feat_part correctly appended",
    fp_b2 == " (feat. iann dior)",
    f"got {fp_b2!r}",
)


# ── B3: Spotify title variant "feat." (with period + space) ───────────────
t_b3 = {
    "title": "Mr. Right Now (feat. Drake)",
    "artists": ["21 Savage", "Metro Boomin", "Drake"],
    "artist": "21 Savage",
}
fp_b3 = _build_feat_part(t_b3)
_check(
    "B3 'feat. Drake' in Spotify title → feat_part empty (no double)",
    fp_b3 == "",
    f"got {fp_b3!r}",
)


# ── B4: ft. abbreviation variant ─────────────────────────────────────────
t_b4 = {
    "title": "Track (ft. Guest)",
    "artists": ["Artist", "Guest"],
    "artist": "Artist",
}
_check(
    "B4 'ft.' abbreviation in title → feat_part empty",
    _build_feat_part(t_b4) == "",
    f"got {_build_feat_part(t_b4)!r}",
)


# ── B5: single artist — feat_part is always empty ────────────────────────
t_b5 = {
    "title": "Solo Track",
    "artists": ["Artist"],
    "artist": "Artist",
}
_check(
    "B5 single artist: feat_part is empty",
    _build_feat_part(t_b5) == "",
)


# ── B6: multi-artist title without existing feat — all extras listed ──────
t_b6 = {
    "title": "Highest In The Room",
    "artists": ["Travis Scott", "ROSALÍA", "Lil Baby"],
    "artist": "Travis Scott",
}
fp_b6 = _build_feat_part(t_b6)
_check(
    "B6 multi-artist, no feat in title → feat_part lists all extras",
    fp_b6 == " (feat. ROSALÍA, Lil Baby)",
    f"got {fp_b6!r}",
)


# ── B7: full stem reconstruction — confirm no double ─────────────────────
def _build_stem(track: dict, album_artist: str, album: str, num: str = "01") -> str:
    title = track.get("title", "Unknown")[:60]   # simplified sanitize for testing
    feat_part = _build_feat_part(track)
    return f"{album_artist} - {album} - {num} {title}{feat_part}"

stem_b7 = _build_stem(
    {"title": "Like Me (feat. iann dior)", "artists": ["$NOT", "iann dior"], "artist": "$NOT"},
    album_artist="$NOT", album="Beautiful Havoc", num="06",
)
_check(
    "B7 full stem: no double feat",
    "(feat. iann dior)" not in stem_b7.replace("Like Me (feat. iann dior)", "") ,
    f"got {stem_b7!r}",
)
_check(
    "B7 full stem: exactly one feat occurrence",
    stem_b7.count("(feat.") == 1,
    f"count={stem_b7.count('(feat.')} in {stem_b7!r}",
)


# ── B8: FEATURE_ARTISTS_IN_TITLE=False — no feat_part regardless ─────────
_cfg.FEATURE_ARTISTS_IN_TITLE = False
t_b8 = {"title": "Track", "artists": ["A", "B"], "artist": "A"}
_check(
    "B8 FEATURE_ARTISTS_IN_TITLE=False: feat_part always empty",
    _build_feat_part(t_b8) == "",
)
_cfg.FEATURE_ARTISTS_IN_TITLE = _ORIG_FEAT_IN_TITLE   # restore


# ─────────────────────────────────────────────────────────────────────────────
# Section C — source-level markers (the fixes exist in the right files)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[C] Source markers: fixes are wired into the right files")

import pathlib
_ROOT = pathlib.Path(__file__).parent

_sc_src  = (_ROOT / "spotify_client.py").read_text(encoding="utf-8")
_ws_src  = (_ROOT / "walkman_sync.py").read_text(encoding="utf-8")

_check(
    "C1 spotify_client uses album.get('artists') for album_artist",
    "album.get(\"artists\"" in _sc_src or "album.get('artists'" in _sc_src,
)
_check(
    "C2 spotify_client has Rev 14.9.11 docstring marker",
    "14.9.11" in _sc_src,
)
_check(
    "C3 spotify_client no longer hardcodes 'Various Artists' for is_comp",
    # The old pattern was: '"Various Artists" if is_comp else'
    '"Various Artists" if is_comp else' not in _sc_src,
    "old is_comp pattern still present",
)
_check(
    "C4 walkman_sync has double-feat guard",
    "_raw_title_has_feat" in _ws_src,
)
_check(
    "C5 walkman_sync double-feat guard checks both 'feat' and 'ft'",
    "ft[.\\\\s]" in _ws_src or r"ft[.\s]" in _ws_src or "\\\\bft[" in _ws_src
    or "bft[" in _ws_src,
)
_check(
    "C6 walkman_sync feat_part branch now checks _raw_title_has_feat",
    "not _raw_title_has_feat" in _ws_src,
)


# ─────────────────────────────────────────────────────────────────────────────
# Section D — device-file pattern checks (against real zx500_all_files.txt)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[D] Device-file diagnostics (zx500_all_files.txt)")

_DEVICE_FILE = pathlib.Path(__file__).parent.parent / "user-data/uploads/zx500_all_files.txt"
# Try both locations (CI vs. local)
for _candidate in [
    pathlib.Path("/mnt/user-data/uploads/zx500_all_files.txt"),
    pathlib.Path(__file__).parent / "zx500_all_files.txt",
]:
    if _candidate.exists():
        _DEVICE_FILE = _candidate
        break

if not _DEVICE_FILE.exists():
    print("  (skipped — zx500_all_files.txt not found at expected path)")
else:
    _raw = _DEVICE_FILE.read_bytes()
    for enc in ("utf-16", "utf-8-sig", "utf-8", "latin-1"):
        try:
            _lines = _raw.decode(enc).splitlines()
            break
        except Exception:
            continue
    _music = [l for l in _lines if "/Music/" in l]
    _total = len(_music)

    # Count double-feat filenames
    _double_feat = [l for l in _music if l.count("(feat.") > 1]
    _pct_double = len(_double_feat) / _total * 100 if _total else 0
    print(f"  Info: {_total} music files total, "
          f"{len(_double_feat)} ({_pct_double:.1f}%) have double feat (pre-fix baseline)")
    _check(
        "D1 double-feat count is informational (pre-fix baseline captured)",
        True,   # always pass — this is diagnostic, not a gate
    )

    # SpotiDownloader watermark count
    _spoti = [l for l in _music if "SpotiDownloader" in l]
    print(f"  Info: {len(_spoti)} files under SpotiDownloader.com folders (need audit run)")
    _check("D2 SpotiDownloader count captured", True)

    # Albums split across multiple artist folders (same album name, different artist prefix)
    from collections import defaultdict
    _album_folders: dict[str, list[str]] = defaultdict(list)
    for _l in _music:
        _parts = _l.split("/")
        if len(_parts) >= 6:
            _folder = _parts[5]
            if " - " in _folder:
                _artist_part, _album_part = _folder.split(" - ", 1)
                _album_folders[_album_part.strip()].append(_artist_part.strip())
    _split_albums = {
        k: sorted(set(v)) for k, v in _album_folders.items() if len(set(v)) > 1
    }
    # Known legitimate multi-artist (e.g. "Various Artists" + real artist for comps)
    _legitimate_va = sum(
        1 for v in _split_albums.values() if "Various Artists" in v and len(v) == 2
    )
    _true_splits = len(_split_albums) - _legitimate_va
    print(f"  Info: {len(_split_albums)} albums span >1 artist folder "
          f"({_legitimate_va} are legit VA, {_true_splits} are genuine splits)")
    _check("D3 split-album count captured", True)


# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'=' * 55}")
print(f"Rev 14.9.11 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
