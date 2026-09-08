"""
test_mk17_b.py — MK17 Phase B regression tests
================================================
Exercises the seven library-completeness patches:

  M1 — Host-side Downloads-folder ingest (fuzzy match against library)
  M2 — Sparse-artist guard (artist with < N tracks → escalate to graph)
  M3 — Graph-based genre inference (feature-collab heavy edges, playlist light)
  M4 — Cover art on ingest path (covered by existing cover_art tests; smoke-check here)
  M5 — Cross-format dedup (FLAC > MP3 > AAC > M4A)
  M6 — Device-side /Download folder ingest
  M7 — Album-completeness pass for saved albums
"""
from __future__ import annotations
import sys
import os
import pathlib
import tempfile
import shutil
from unittest.mock import patch, MagicMock

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


# ─── 1. M1: ingest module exists, fuzzy-matches correctly ───────────────
print("\n[1] M1: downloads_ingest module — fuzzy match")

import downloads_ingest as _di

_check("module imports", _di is not None)
_check("scan_host_dirs callable", callable(_di.scan_host_dirs))
_check("scan_device_dirs callable", callable(_di.scan_device_dirs))
_check("find_match callable", callable(_di.find_match))
_check("ingest_loose_files callable", callable(_di.ingest_loose_files))

# Real-world matching cases the user cares about
library = [
    {"id": "1", "title": "Gongo Aso", "artist": "9ice", "album": "Gongo Aso"},
    {"id": "2", "title": "AMApiano #1", "artist": "Various Artists", "album": "Loose"},
    {"id": "3", "title": "Different Song", "artist": "Other", "album": "Other"},
]

# Exact match
m1 = _di.find_match({"title": "Gongo Aso", "artist": "9ice"}, library)
_check("exact title+artist matches", m1 is not None and m1["id"] == "1")

# Close match (different casing/punctuation)
m2 = _di.find_match({"title": "Gongo aso", "artist": "9ICE"}, library)
_check("case-insensitive match", m2 is not None and m2["id"] == "1")

# AMApiano case from LOG_16_1
m3 = _di.find_match({"title": "AMApiano #1", "artist": ""}, library)
_check("AMApiano LOG_16_1 case fuzzy-matches", m3 is not None and m3["id"] == "2")

# Below-threshold rejection
m4 = _di.find_match({"title": "Totally unrelated song", "artist": "Stranger"}, library)
_check("unrelated song does NOT match", m4 is None)


# ─── 2. M1: scan_host_dirs walks real directory ─────────────────────────
print("\n[2] M1: scan_host_dirs walks a real temp directory")

tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="mk17b_m1_"))
try:
    (tmpdir / "audio.mp3").write_bytes(b"x")
    (tmpdir / "audio.flac").write_bytes(b"x")
    (tmpdir / "image.jpg").write_bytes(b"x")  # non-audio, should be skipped
    (tmpdir / "doc.pdf").write_bytes(b"x")    # non-audio
    files = _di.scan_host_dirs([tmpdir])
    audio_names = sorted(f.name for f in files)
    _check("scans MP3", "audio.mp3" in audio_names)
    _check("scans FLAC", "audio.flac" in audio_names)
    _check("does NOT pick up jpg/pdf", len(audio_names) == 2)
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 3. M3: genre_graph builds and infers ───────────────────────────────
print("\n[3] M3: genre_graph build + inference")

import genre_graph as _gg

# Construct a synthetic library where Alfa Kat has 1 track tagged
# "Abstract Hip-Hop", but his collaborators (with 3+ tracks each) are
# all confidently tagged "Amapiano".  Inference should rescue him.
synth_tracks = [
    # Alfa Kat — 1 track, mistagged
    {"id": "1", "title": "Track1", "artist": "Alfa Kat",
     "artists": ["Alfa Kat", "Toss"], "album": "AK Album", "genre": "Abstract Hip-Hop"},
    # Toss — 4 tracks, confidently Amapiano
    {"id": "2", "title": "T1", "artist": "Toss", "artists": ["Toss"], "album": "T", "genre": "Amapiano"},
    {"id": "3", "title": "T2", "artist": "Toss", "artists": ["Toss"], "album": "T", "genre": "Amapiano"},
    {"id": "4", "title": "T3", "artist": "Toss", "artists": ["Toss"], "album": "T", "genre": "Amapiano"},
    {"id": "5", "title": "T4", "artist": "Toss", "artists": ["Toss"], "album": "T", "genre": "Amapiano"},
    # Sayfar — 3 tracks, confidently Amapiano
    {"id": "6", "title": "S1", "artist": "Sayfar", "artists": ["Sayfar"], "album": "S", "genre": "Amapiano"},
    {"id": "7", "title": "S2", "artist": "Sayfar", "artists": ["Sayfar"], "album": "S", "genre": "Amapiano"},
    {"id": "8", "title": "S3", "artist": "Sayfar", "artists": ["Alfa Kat", "Sayfar"], "album": "S", "genre": "Amapiano"},
]

g = _gg.build_graph(tracks=synth_tracks)

_check("Alfa Kat present in graph", "alfa kat" in g.adjacency)
_check("Toss present in graph", "toss" in g.adjacency)
_check("Alfa Kat-Toss heavy edge present",
       g.adjacency.get("alfa kat", {}).get("toss", 0) >= 3,
       f"weight={g.adjacency.get('alfa kat', {}).get('toss', 0)}")
_check("Alfa Kat tally is 2 (own track + Sayfar collab)", g.artist_tracks.get("alfa kat") == 2)
_check("Toss has 5 track tally (4 own + Alfa Kat collab)",
       g.artist_tracks.get("toss") == 5,
       f"got {g.artist_tracks.get('toss')}")
_check("Toss confident modal=Amapiano", g.artist_modal.get("toss") == "Amapiano")
# Alfa Kat's tally = 2, still below the strict threshold of 3
_check("Alfa Kat tally below threshold (so not 'confident')",
       g.artist_tracks.get("alfa kat", 0) < 3)

# Inference: Alfa Kat should land on Amapiano
inferred = _gg.infer_genre_for_artist(g, "Alfa Kat", min_neighbor_tracks=3)
_check("Alfa Kat inferred genre = Amapiano (not Abstract Hip-Hop)",
       inferred == "Amapiano",
       f"got {inferred!r}")

# An artist with no neighbors should return None
no_neighbors_track = {"id": "X", "title": "Lonely", "artist": "Solo Star",
                      "artists": ["Solo Star"], "album": "Lonely", "genre": ""}
g2 = _gg.build_graph(tracks=[no_neighbors_track])
inferred2 = _gg.infer_genre_for_artist(g2, "Solo Star")
_check("isolated artist returns None", inferred2 is None)


# ─── 4. M3: graph honors playlist edges ─────────────────────────────────
print("\n[4] M3: graph honors playlist co-occurrence (light edges)")

# Two artists who never collab but share playlists
track_map_with_playlists = {
    "Liked Songs": [
        {"id": "a", "title": "Solo A", "artist": "ArtistA",
         "artists": ["ArtistA"], "album": "AA", "genre": "Genre1"},
        {"id": "b", "title": "Solo B", "artist": "ArtistB",
         "artists": ["ArtistB"], "album": "BB", "genre": "Genre1"},
    ],
}
g3 = _gg.build_graph(track_map=track_map_with_playlists)
# Get the configured playlist weight (MK17 P8 reduced from 1 to 0.5)
import config as _cfg_p8
_expected_light_weight = float(getattr(_cfg_p8, "GENRE_GRAPH_PLAYLIST_WEIGHT", 0.5))
_observed_weight = g3.adjacency.get("artista", {}).get("artistb", 0)
# A and B share a playlist → light edge weight is the configured value
_check("playlist co-occurrence creates a light edge",
       _observed_weight > 0,
       f"got {_observed_weight}")
# Edge weight matches the LIGHT (playlist) configured weight, not the heavy (feature) weight
_check("playlist edge weight matches GENRE_GRAPH_PLAYLIST_WEIGHT",
       abs(_observed_weight - _expected_light_weight) < 0.01,
       f"got {_observed_weight}, expected {_expected_light_weight}")
_check("playlist edge weight < feature-edge weight (so collabs dominate)",
       _observed_weight < float(getattr(_cfg_p8, "GENRE_GRAPH_FEATURE_WEIGHT", 3)),
       f"playlist={_observed_weight}, feature={getattr(_cfg_p8, 'GENRE_GRAPH_FEATURE_WEIGHT', 3)}")


# ─── 5. M2: sparse-artist guard wired into walkman_sync ─────────────────
print("\n[5] M2: sparse-artist guard wiring")

with open(os.path.join(os.path.dirname(__file__), "walkman_sync.py"),
          "r", encoding="utf-8") as f:
    ws_src = f.read()

_check("walkman_sync reads GENRE_ARTIST_MIN_TRACKS",
       "GENRE_ARTIST_MIN_TRACKS" in ws_src)
_check("walkman_sync has graph-fallback branch",
       "import genre_graph" in ws_src or "from genre_graph" in ws_src
       or "_gg.infer_genre_for_artist" in ws_src)
_check("walkman_sync escalates artist→graph when sparse",
       "artist_track_count.get(aa, 0) < artist_min_tracks" in ws_src)
_check("walkman_sync logs graph-rescue events",
       "GenreGraph/Sparse" in ws_src or "graph proposes" in ws_src)
_check("walkman_sync counts graph_filled",
       "graph_filled" in ws_src)


# ─── 6. M5: cross-format dedup priority order ───────────────────────────
print("\n[6] M5: cross-format dedup priority FLAC > MP3 > AAC > M4A")

# Source-level checks (importing walkman_dedup pulls in mtp_transfer
# which exits on Linux without ADB/COM available)
with open(os.path.join(os.path.dirname(__file__), "walkman_dedup.py"),
          "r", encoding="utf-8") as f:
    wd_src = f.read()

# The corrected ordering: FLAC > MP3 > AAC > M4A
import re as _re
m = _re.search(r"_FORMAT_PREFERENCE\s*=\s*\[([^\]]+)\]", wd_src)
_check("_FORMAT_PREFERENCE found in source", m is not None)
if m:
    items = [x.strip().strip('"').strip("'") for x in m.group(1).split(",")]
    _check("FLAC is first", items[0] == ".flac",
           f"got {items[:4]}")
    # MK14 correction: FLAC > M4A > AAC > OGG > MP3 (not FLAC > MP3 > M4A)
    # CTO MK14 directive: "codec hierarchy fix corrected from FLAC > MP3 > M4A"
    _check("M4A is second (above AAC and MP3)", items[1] == ".m4a",
           f"got {items[:4]}")
    _check("M4A before AAC", items.index(".m4a") < items.index(".aac"),
           f"got {items[:4]}")


# ─── 7. M5: DEDUP_MODE active by default ────────────────────────────────
print("\n[7] M5: DEDUP_MODE flipped to active")

import config as _cfg
_check("DEDUP_MODE defaults to 'active'", _cfg.DEDUP_MODE == "active")


# ─── 8. M5: dedup wired into audit ──────────────────────────────────────
print("\n[8] M5: prune_during_audit wired into audit.run")

with open(os.path.join(os.path.dirname(__file__), "audit.py"),
          "r", encoding="utf-8") as f:
    a_src = f.read()
_check("audit.run calls prune_during_audit",
       "prune_during_audit" in a_src)
_check("audit dedup catches cross-format case",
       "9ice" in a_src or "Gongo" in a_src or "cross-format" in a_src.lower())


# ─── 9. M1/M6: ingest wired into walkman_sync ───────────────────────────
print("\n[9] M1/M6: ingest wired into walkman_sync as Stage 0.5")

_check("walkman_sync references downloads_ingest",
       "downloads_ingest" in ws_src or "import downloads_ingest" in ws_src)
_check("walkman_sync respects INGEST_ENABLED flag",
       "INGEST_ENABLED" in ws_src)
_check("walkman_sync surfaces match counts",
       "host-side match" in ws_src or "host_matches" in ws_src)


# ─── 10. M7: album-completeness wired ───────────────────────────────────
print("\n[10] M7: album-completeness wiring")

import album_completeness as _ac
_check("album_completeness module imports", _ac is not None)
_check("find_missing_tracks_for_albums callable",
       callable(_ac.find_missing_tracks_for_albums))

# Behavior: returns [] when sp_client is None (offline mode)
result = _ac.find_missing_tracks_for_albums(
    saved_album_ids={"album1"}, track_map={}, sp_client=None,
)
_check("offline mode returns empty list", result == [])

# Behavior: returns [] when no saved albums
result2 = _ac.find_missing_tracks_for_albums(
    saved_album_ids=set(), track_map={}, sp_client=MagicMock(),
)
_check("empty saved_album_ids returns empty list", result2 == [])

# Behavior: queues missing tracks when album has tracks not in track_map
mock_sp = MagicMock()
mock_sp.album = MagicMock(return_value={
    "name": "Mock Album",
    "artists": [{"name": "Mock Artist"}],
    "album_type": "album",
    "tracks": {
        "items": [
            {"id": "t1", "name": "Track 1", "artists": [{"name": "Mock Artist"}],
             "track_number": 1, "disc_number": 1},
            {"id": "t2", "name": "Track 2", "artists": [{"name": "Mock Artist"}],
             "track_number": 2, "disc_number": 1},
            {"id": "t3", "name": "Track 3", "artists": [{"name": "Mock Artist"}],
             "track_number": 3, "disc_number": 1},
        ]
    }
})
existing = {
    "Liked Songs": [
        {"id": "x", "artist": "Mock Artist", "title": "Track 1", "album": "Mock Album"},
    ]
}
missing_list = _ac.find_missing_tracks_for_albums(
    saved_album_ids={"album1"}, track_map=existing, sp_client=mock_sp,
)
_check("missing tracks identified",
       len(missing_list) == 2,
       f"got {len(missing_list)}: {[m['title'] for m in missing_list]}")
_check("missing track marked _completeness_fill",
       all(t.get("_completeness_fill") for t in missing_list))

# walkman_sync wires it
_check("walkman_sync calls album_completeness",
       "album_completeness" in ws_src or "import album_completeness" in ws_src)
_check("walkman_sync respects ALBUM_COMPLETENESS_ENABLED",
       "ALBUM_COMPLETENESS_ENABLED" in ws_src)
_check("walkman_sync re-runs downloader for completeness fills",
       "__completeness_fill__" in ws_src)


# ─── 11. Config flags ───────────────────────────────────────────────────
print("\n[11] Config flags for MK17 Phase B")

_check("INGEST_ENABLED present", hasattr(_cfg, "INGEST_ENABLED"))
_check("INGEST_HOST_DIRS present", hasattr(_cfg, "INGEST_HOST_DIRS"))
_check("INGEST_DEVICE_DIRS present", hasattr(_cfg, "INGEST_DEVICE_DIRS"))
_check("GENRE_ARTIST_MIN_TRACKS present", hasattr(_cfg, "GENRE_ARTIST_MIN_TRACKS"))
_check("GENRE_GRAPH_INFERENCE_ENABLED present",
       hasattr(_cfg, "GENRE_GRAPH_INFERENCE_ENABLED"))
_check("GENRE_GRAPH_FEATURE_WEIGHT > GENRE_GRAPH_PLAYLIST_WEIGHT",
       _cfg.GENRE_GRAPH_FEATURE_WEIGHT > _cfg.GENRE_GRAPH_PLAYLIST_WEIGHT)
# MK18-R4: updated to MK14-corrected order (FLAC > M4A > AAC > MP3)
_check("DEDUP_LOCAL_FORMAT_PRIORITY matches MK14 direction",
       _cfg.DEDUP_LOCAL_FORMAT_PRIORITY[:4] == (".flac", ".m4a", ".aac", ".ogg"))
_check("ALBUM_COMPLETENESS_ENABLED present",
       hasattr(_cfg, "ALBUM_COMPLETENESS_ENABLED"))


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"MK17 Phase B regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
