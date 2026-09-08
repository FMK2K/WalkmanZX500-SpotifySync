"""
test_rev1495.py — Rev 14.9.5 regression tests
==============================================
Exercises:
  * Offline mode: fetch_and_build_offline() returns a valid 3-tuple
  * Offline mode: track_dict has all expected fields
  * Offline mode: F1 allocator runs on Liked Songs and per-playlist
  * Genre tier-2: artist-fallback fills albums with no specific signal
  * Genre tier-2: narrowed _BROAD_GENRES allows Hip-Hop / R&B / etc.
  * Genre tier-2: GENRE_ALBUM_MIN_VOTES gates album-mode confidence
"""
from __future__ import annotations
import sys
import os
import json
import tempfile
import shutil

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


# ─── 1. Offline-mode end-to-end against a synthetic export ──────────────
print("\n[1] Offline mode: fetch_and_build_offline()")

# Build a tiny synthetic export
tmpdir = tempfile.mkdtemp(prefix="walkman_test_")
data_dir = os.path.join(tmpdir, "Spotify Account Data")
os.makedirs(data_dir)

# YourLibrary.json — 3 liked tracks across 2 albums + 1 saved album
your_library = {
    "tracks": [
        {"uri": "spotify:track:t1", "track": "Song A1", "artist": "Artist X", "album": "Album X1"},
        {"uri": "spotify:track:t2", "track": "Song A2", "artist": "Artist X", "album": "Album X1"},
        {"uri": "spotify:track:t3", "track": "Song A3", "artist": "Artist X", "album": "Album X1"},
        {"uri": "spotify:track:t4", "track": "Song B1", "artist": "Artist Y", "album": "Album Y1"},
    ],
    "albums": [
        {"uri": "spotify:album:a1", "artist": "Artist X", "album": "Album X1"},
    ],
    "shows": [], "episodes": [], "bannedTracks": [], "artists": [],
    "bannedArtists": [], "pageMatch": [], "kallaxes": [], "other": [],
    "parentAllowedTracks": [], "parentAllowedArtists": [], "podcastChapters": [],
}
with open(os.path.join(data_dir, "YourLibrary.json"), "w", encoding="utf-8") as f:
    json.dump(your_library, f)

# Playlist1.json — 1 playlist with 3 tracks
playlist_data = {
    "playlists": [
        {
            "name": "Test Playlist",
            "items": [
                {"track": {"trackUri": "spotify:track:t5", "trackName": "Song C1",
                           "artistName": "Artist Z", "albumName": "Album Z1"}},
                {"track": {"trackUri": "spotify:track:t6", "trackName": "Song C2",
                           "artistName": "Artist Z", "albumName": "Album Z1"}},
                {"track": {"trackUri": "spotify:track:t1", "trackName": "Song A1",  # already in liked
                           "artistName": "Artist X", "albumName": "Album X1"}},
            ],
        },
    ],
}
with open(os.path.join(data_dir, "Playlist1.json"), "w", encoding="utf-8") as f:
    json.dump(playlist_data, f)

# Empty streaming history
with open(os.path.join(data_dir, "StreamingHistory_music_0.json"), "w", encoding="utf-8") as f:
    json.dump([], f)

import config
import spotify_local_data as sld

# Reset module-level state
sld._CACHE = {}
sld._PLAYLISTS = {}
sld._LIKED_URIS = set()
sld._LOADED = False
sld._DATA_DIR = None
sld.install(data_dir)

try:
    track_map, liked_ids, saved_album_ids = sld.fetch_and_build_offline()

    _check("returns 3-tuple",
           isinstance(track_map, dict) and isinstance(liked_ids, set)
           and isinstance(saved_album_ids, set))
    _check("Liked Songs key present", "Liked Songs" in track_map)
    _check("Liked Songs has 4 tracks (4 liked URIs)",
           len(track_map.get("Liked Songs", [])) == 4,
           f"got {len(track_map.get('Liked Songs', []))}")
    _check("Test Playlist key present", "Test Playlist" in track_map)
    _check("Test Playlist has 3 tracks",
           len(track_map.get("Test Playlist", [])) == 3,
           f"got {len(track_map.get('Test Playlist', []))}")
    _check("liked_ids has 4 IDs", len(liked_ids) == 4)
    _check("saved_album_ids contains a1",
           "a1" in saved_album_ids)

    # Track-dict shape
    sample = track_map["Liked Songs"][0]
    required = ("id", "uri", "title", "artist", "artists", "album",
                "album_id", "album_artist", "is_liked", "track_number",
                "disc_number", "_from_local_data")
    for field in required:
        _check(f"sample has {field}", field in sample)
    _check("sample is_liked=True (Liked Songs)", sample["is_liked"] == True)
    _check("sample _from_local_data=True", sample["_from_local_data"] == True)

    # F1: tracks within an album get sequential numbers
    artist_x_tracks = [t for t in track_map["Liked Songs"]
                       if t["album"] == "Album X1"]
    nums = sorted(t["track_number"] for t in artist_x_tracks)
    _check("Album X1 (3 tracks) gets sequential 1,2,3",
           nums == ["1", "2", "3"], f"got {nums}")

    # Singleton album gets "1"
    artist_y_tracks = [t for t in track_map["Liked Songs"]
                       if t["album"] == "Album Y1"]
    _check("Album Y1 (1 track) gets '1'",
           artist_y_tracks and artist_y_tracks[0]["track_number"] == "1")

    # Empty title rejected — no test track has empty title in our fixture,
    # but the F6 ValueError is exercised by test_rev1493.

finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
    # Reset module state so other tests aren't polluted
    sld._CACHE = {}
    sld._PLAYLISTS = {}
    sld._LIKED_URIS = set()
    sld._LOADED = False
    sld._DATA_DIR = None


# ─── 2. Empty export raises RuntimeError ────────────────────────────────
print("\n[2] Offline mode: empty export raises")
empty_dir = tempfile.mkdtemp(prefix="walkman_empty_")
try:
    sld._CACHE = {}
    sld._PLAYLISTS = {}
    sld._LIKED_URIS = set()
    sld._LOADED = False
    sld._DATA_DIR = None
    sld.install(empty_dir)
    raised = False
    try:
        sld.fetch_and_build_offline()
    except RuntimeError:
        raised = True
    _check("missing/empty export → RuntimeError", raised)
finally:
    shutil.rmtree(empty_dir, ignore_errors=True)


# ─── 3. Genre consolidation: tier-2 artist fallback ─────────────────────
print("\n[3] Genre tier-2 artist fallback")

# walkman_sync depends on spotipy which isn't installed in CI sandboxes.
# Extract _consolidate_album_genres + _BROAD_GENRES directly from source
# and exec them in a minimal namespace.
import re
with open(os.path.join(os.path.dirname(__file__), "walkman_sync.py"), "r", encoding="utf-8") as f:
    src = f.read()

# Pull out _BROAD_GENRES frozenset (multi-line literal)
m_bg = re.search(
    r"_BROAD_GENRES = frozenset\(\{(.*?)\}\)",
    src, re.DOTALL,
)
broad_literal = m_bg.group(1) if m_bg else ""
ns = {"config": config, "log": __import__("logging").getLogger("test")}
exec("from collections import Counter, defaultdict", ns)
exec(f"BROAD_GENRES = frozenset({{{broad_literal}}})", ns)

# Pull the body of _consolidate_album_genres and adapt as a free function
m_cons = re.search(
    r"def _consolidate_album_genres\(self, tracks\) -> None:(.*?)(?=\n    def |\nclass )",
    src, re.DOTALL,
)
if not m_cons:
    _check("source extraction of _consolidate_album_genres", False)
else:
    body = m_cons.group(1)
    # Make it a free function: replace `self._BROAD_GENRES` with `BROAD_GENRES`
    # and `self._invalidate_audit_state_for_tracks` with a no-op.
    body = body.replace("self._BROAD_GENRES", "BROAD_GENRES")
    body = body.replace(
        "self._invalidate_audit_state_for_tracks(rewritten)",
        "pass"
    )
    func_src = "def consolidate(tracks):\n" + body
    exec(func_src, ns)
    consolidate = ns["consolidate"]

    # Scenario A: artist with 2 albums; one clear, one all blank
    tracks = [
        {"album_artist": "Kendrick Lamar", "album": "DAMN.", "title": "DNA", "genre": "Hip-Hop"},
        {"album_artist": "Kendrick Lamar", "album": "DAMN.", "title": "Pride", "genre": "Hip-Hop"},
        {"album_artist": "Kendrick Lamar", "album": "DAMN.", "title": "Loyalty", "genre": "Hip-Hop"},
        {"album_artist": "Kendrick Lamar", "album": "Section.80", "title": "Hol' Up", "genre": ""},
        {"album_artist": "Kendrick Lamar", "album": "Section.80", "title": "Tammy's Song", "genre": ""},
        {"album_artist": "Kendrick Lamar", "album": "Section.80", "title": "Chapter Six", "genre": ""},
    ]
    consolidate(tracks)
    damn_genres = {t["genre"] for t in tracks if t["album"] == "DAMN."}
    _check("Hip-Hop is now allowed as canonical (not blocked as broad)",
           damn_genres == {"Hip-Hop"}, f"got {damn_genres}")
    section_genres = {t["genre"] for t in tracks if t["album"] == "Section.80"}
    _check("Section.80 (all blank) gets Hip-Hop from artist tier",
           section_genres == {"Hip-Hop"}, f"got {section_genres}")

    # Scenario B: noisy album, modal pick
    tracks2 = [
        {"album_artist": "Burna Boy", "album": "African Giant", "title": "Anybody", "genre": "Afrobeats"},
        {"album_artist": "Burna Boy", "album": "African Giant", "title": "Killin Dem", "genre": "Afrobeats"},
        {"album_artist": "Burna Boy", "album": "African Giant", "title": "Dangote", "genre": "Afrobeats"},
        {"album_artist": "Burna Boy", "album": "African Giant", "title": "Pull Up", "genre": "Pop"},
        {"album_artist": "Burna Boy", "album": "African Giant", "title": "Anybody Else", "genre": ""},
    ]
    consolidate(tracks2)
    af_genres = {t["genre"] for t in tracks2}
    _check("African Giant: outlier Pop and blank both rewritten to Afrobeats",
           af_genres == {"Afrobeats"}, f"got {af_genres}")

    # Scenario C: weak album with artist-confirmed signal
    tracks3 = [
        {"album_artist": "Some Artist", "album": "Album X", "title": "T1", "genre": ""},
        {"album_artist": "Some Artist", "album": "Album X", "title": "T2", "genre": "Trap"},
        {"album_artist": "Some Artist", "album": "Album Y", "title": "Y1", "genre": "Trap"},
        {"album_artist": "Some Artist", "album": "Album Y", "title": "Y2", "genre": "Trap"},
    ]
    consolidate(tracks3)
    album_x_genres = {t["genre"] for t in tracks3 if t["album"] == "Album X"}
    _check("Album X (1 specific=Trap + 1 blank) backfills with artist-confirmed Trap",
           album_x_genres == {"Trap"}, f"got {album_x_genres}")


# ─── 4. _BROAD_GENRES narrowed correctly ────────────────────────────────
print("\n[4] _BROAD_GENRES narrowed (Rev 14.9.5)")
# Use the broad set we extracted above
broad = ns.get("BROAD_GENRES", frozenset())
for kept in ("", "unknown", "world", "world music", "various"):
    _check(f"{kept!r} still treated as broad", kept.lower() in broad)
for now_specific in ("hip hop", "hip-hop", "r&b", "pop", "rock",
                     "indie", "electronic", "alternative"):
    _check(f"{now_specific!r} no longer broad (allowed as canonical)",
           now_specific.lower() not in broad)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"Rev 14.9.5 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
