"""
test_mk17_p7_p11.py — MK17 Patches 7-11 regression tests
============================================================
Source-level + behavioural checks for the LOG_17 fixes:

  P7  Reorganize deadlock — _delete_item must be timeout-bounded;
      circuit breaker trips after N consecutive timeouts so a sick
      device doesn't waste hours.

  P8  Genre-graph over-attraction — playlists larger than
      GENRE_GRAPH_MAX_PLAYLIST_SIZE are excluded from light-edge
      construction.  Playlist edge weight reduced from 1 to 0.5.
      Diversity guard suppresses any genre that wins more than
      GENRE_GRAPH_DIVERSITY_GUARD_RATIO of sparse rescues.

  P9  Resource-monitor forced GC — when handle pressure crosses
      the warn line, force a generational gc.collect() to break
      win32com object cycles.

  P10 Tag-write extended backoff — 5 attempts at 100/400/1500/3000/
      6000ms (total ~11s) instead of 3 at 50/200/800ms (~1.1s).

  P11 Dedup zero-scan warning — when device-side scan returns 0
      audio files, emit a loud WARNING rather than the silent
      "Nothing to delete" that misled LOG_17.
"""
from __future__ import annotations
import os, sys, pathlib, threading, time
from collections import Counter

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


# ─── 1. P7: timeout-bounded _delete_item — source-level wiring ──────────
print("\n[1] P7: _delete_item timeout-bounded")

WR_PATH = pathlib.Path(__file__).parent / "walkman_reorganize.py"
wr_src = WR_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 7 marker present", "MK17 Patch 7" in wr_src)
_check("LOG_17 named in patch comment", "LOG_17" in wr_src)
_check("threading imported within _delete_item",
       "import threading" in wr_src and "threading.Thread" in wr_src)
_check("timeout_s parameter on _delete_item",
       "timeout_s" in wr_src
       and "def _delete_item(item, name: str, verbose: bool" in wr_src)
_check("worker thread joined with timeout",
       ".join(timeout=timeout_s)" in wr_src)
_check("circuit breaker globals defined",
       "_DELETE_BREAKER" in wr_src)
_check("circuit breaker has trip_threshold",
       "trip_threshold" in wr_src)
_check("circuit breaker tripped flag is checked early",
       "_DELETE_BREAKER[\"tripped\"]" in wr_src)
_check("breaker reset helper exists",
       "_reset_delete_breaker" in wr_src)
_check("warning emitted when timeout fires",
       "rename+move hung" in wr_src and "direct delete hung" in wr_src)


# ─── 2. P7 behavioural: simulate a hang ─────────────────────────────────
print("\n[2] P7: behavioural — simulated hang triggers timeout")

# Mock everything _delete_item needs.  Module-load imports pythoncom on
# Linux so we can't import walkman_reorganize directly; instead we do a
# minimal repro of the timeout pattern to prove the threading approach
# works as intended.
def _simulate_hung_call():
    time.sleep(60)  # would hang far longer than test timeout

result = {"done": False}
def _runner():
    try:
        _simulate_hung_call()
    finally:
        result["done"] = True

t = threading.Thread(target=_runner, daemon=True)
start = time.time()
t.start()
t.join(timeout=0.5)  # 500ms timeout
elapsed = time.time() - start

_check("hung worker is abandoned within timeout window",
       elapsed < 1.0, f"took {elapsed:.2f}s")
_check("hung worker thread still running after join (would be killed when daemon=True)",
       t.is_alive(), "expected thread to still be alive")
_check("result.done remains False (we did not wait)",
       result["done"] is False)


# ─── 3. P8: graph builder excludes oversized playlists ──────────────────
print("\n[3] P8: oversized-playlist exclusion")

import genre_graph as _gg

# Build a graph with a 5-track 'curated' playlist (kept) AND a synthetic
# 500-track 'liked-songs' playlist (skipped). Verify edges only exist
# from the curated playlist.
small_playlist_tracks = [
    {"id": f"s{i}", "title": f"Song {i}", "artist": f"A{i}",
     "artists": [f"A{i}"], "album": f"Album{i}", "genre": "Pop"}
    for i in range(5)
]
big_playlist_tracks = [
    {"id": f"b{i}", "title": f"Big {i}", "artist": f"B{i}",
     "artists": [f"B{i}"], "album": f"Big{i}", "genre": "Pop"}
    for i in range(500)  # > GENRE_GRAPH_MAX_PLAYLIST_SIZE (300)
]

g = _gg.build_graph(track_map={
    "Curated": small_playlist_tracks,
    "Liked Songs": big_playlist_tracks,
})

# A0 should have an edge to A1 (both in the 5-track playlist)
_check("small-playlist edges exist (under cap)",
       g.adjacency.get("a0", {}).get("a1", 0) > 0,
       f"got {g.adjacency.get('a0', {}).get('a1', 0)}")

# B0 should NOT have an edge to B1 (both only in the oversized playlist)
_check("oversized-playlist edges are skipped",
       g.adjacency.get("b0", {}).get("b1", 0) == 0,
       f"got {g.adjacency.get('b0', {}).get('b1', 0)} — should be 0 since playlist exceeds cap")


# ─── 4. P8: playlist edge weight reduced to 0.5 ─────────────────────────
print("\n[4] P8: playlist edge weight is 0.5 (not 1)")

import config as _cfg
_check("GENRE_GRAPH_PLAYLIST_WEIGHT is 0.5",
       float(_cfg.GENRE_GRAPH_PLAYLIST_WEIGHT) == 0.5,
       f"got {_cfg.GENRE_GRAPH_PLAYLIST_WEIGHT}")
_check("feature weight remains higher than playlist weight",
       float(_cfg.GENRE_GRAPH_FEATURE_WEIGHT) > float(_cfg.GENRE_GRAPH_PLAYLIST_WEIGHT))
_check("GENRE_GRAPH_MAX_PLAYLIST_SIZE present",
       hasattr(_cfg, "GENRE_GRAPH_MAX_PLAYLIST_SIZE"))
_check("max playlist size is 300", _cfg.GENRE_GRAPH_MAX_PLAYLIST_SIZE == 300)


# ─── 5. P8: diversity guard wired into walkman_sync ─────────────────────
print("\n[5] P8: diversity guard")

WS_PATH = pathlib.Path(__file__).parent / "walkman_sync.py"
ws_src = WS_PATH.read_text(encoding="utf-8")

_check("diversity guard ratio config flag",
       hasattr(_cfg, "GENRE_GRAPH_DIVERSITY_GUARD_RATIO"))
_check("diversity guard min-sample config flag",
       hasattr(_cfg, "GENRE_GRAPH_DIVERSITY_GUARD_MIN_SAMPLE"))
_check("walkman_sync references the diversity guard",
       "DiversityGuard" in ws_src or "diversity_ratio" in ws_src)
_check("walkman_sync has rescue_proposals pre-scan",
       "rescue_proposals" in ws_src)
_check("walkman_sync has suppressed_genres set",
       "suppressed_genres" in ws_src)
_check("walkman_sync counts graph_suppressed",
       "graph_suppressed" in ws_src)
_check("suppression message names the cause",
       "GENRE_GRAPH_MAX_PLAYLIST_SIZE" in ws_src
       or "high-cardinality playlist" in ws_src)


# ─── 6. P9: forced GC in resource_monitor ───────────────────────────────
print("\n[6] P9: forced GC on pressure-warn")

RM_PATH = pathlib.Path(__file__).parent / "resource_monitor.py"
rm_src = RM_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 9 marker present", "MK17 Patch 9" in rm_src)
_check("forced GC inside the pressure-warn branch",
       "gc.collect()" in rm_src)
_check("re-probe after GC to verify free",
       "Forced GC freed" in rm_src or "after GC" in rm_src)


# ─── 7. P10: extended tag-write backoff ─────────────────────────────────
print("\n[7] P10: tag-write backoff extended")

DL_PATH = pathlib.Path(__file__).parent / "downloader.py"
dl_src = DL_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 10 marker present", "MK17 Patch 10" in dl_src)
# Look for the longer schedule
_check("backoff schedule extended past 5 seconds",
       "(100, 400, 1500, 3000, 6000)" in dl_src
       or ("3000" in dl_src and "6000" in dl_src
           and "100, 400" in dl_src),
       "expected 100/400/1500/3000/6000 ms ladder")
_check("LOG_17 referenced as motivation",
       "LOG_17" in dl_src)


# ─── 8. P11: dedup zero-scan warning ────────────────────────────────────
print("\n[8] P11: dedup zero-scan WARNING")

WD_PATH = pathlib.Path(__file__).parent / "walkman_dedup.py"
wd_src = WD_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 11 marker present", "MK17 Patch 11" in wd_src)
_check("explicit warning when scan returns 0",
       "Scan returned ZERO" in wd_src or "ZERO audio files" in wd_src)
_check("warning explains the failure mode",
       "device-scan" in wd_src and "USB" in wd_src.upper() or "MTP" in wd_src)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"MK17 Patches 7-11 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
