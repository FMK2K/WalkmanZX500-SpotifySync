"""
test_task12_tunemymusic_csv.py — isolated verification for Task 12
(TuneMyMusic CSV fast-refresh supplement in spotify_local_data.py)

Stage 2 of this project's verification ladder: synthetic CSVs matching the
real confirmed header exactly. Stage 3 (partial): also runs once against the
user's actual real CSV file (read-only parse, no writes) as a live smoke
test, since that file already exists locally — no network/device required
either way.
"""
import pathlib
import sys
import tempfile
import time

import config
import spotify_local_data as sld

passed = 0
failed = 0


def _check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


def _reset_state():
    sld._CACHE = {}
    sld._PLAYLISTS = {}
    sld._LIKED_URIS = set()
    sld._ISRC_BY_URI = {}
    sld._LOADED = True  # pretend the GDPR export already loaded (empty here)


_HEADER = "Track name,Artist name,Album,Playlist name,Type,ISRC,Spotify - id\n"

tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task12_csv_test_"))

# ─── 1. Missing file — fail-soft, returns 0, no exception ──────────────────
print("\n[1] Missing CSV — fail-soft")
_reset_state()
missing = tmpdir / "does_not_exist.csv"
added = sld.augment_from_tunemymusic_csv(missing)
_check("returns 0", added == 0)
_check("no tracks added to cache", len(sld._CACHE) == 0)

# ─── 2. Basic parse — Favorite + Playlist rows ─────────────────────────────
print("\n[2] Basic parse — Favorite and Playlist rows handled correctly")
_reset_state()
csv1 = tmpdir / "basic.csv"
csv1.write_text(
    _HEADER +
    '"Song A","Artist A","Album A","Favorite Songs","Favorite","USVPR0612998","aaa111"\n'
    '"Song B","Artist B","Album B","My Playlist","Playlist","ZA56E2100247","bbb222"\n',
    encoding="utf-8",
)
added = sld.augment_from_tunemymusic_csv(csv1)
_check("2 new tracks added", added == 2)
_check("Favorite row added to liked_uris", "spotify:track:aaa111" in sld._LIKED_URIS)
_check("Playlist row NOT added to liked_uris", "spotify:track:bbb222" not in sld._LIKED_URIS)
_check("Playlist row added to _PLAYLISTS", "spotify:track:bbb222" in sld._PLAYLISTS.get("My Playlist", []))
_check("cache entry has correct title/artist", sld._CACHE["spotify:track:aaa111"]["track"] == "Song A")
_check("ISRC stashed for both", sld.get_isrc("spotify:track:aaa111") == "USVPR0612998" and sld.get_isrc("spotify:track:bbb222") == "ZA56E2100247")

# ─── 3. Purely additive — pre-existing URI never overwritten ───────────────
print("\n[3] Purely additive — existing (better) data is never overwritten")
_reset_state()
sld._CACHE["spotify:track:aaa111"] = {
    "track": "Canonical Title", "artist": "Canonical Artist", "album": "Canonical Album",
    "is_liked": True, "play_count_recent": 5, "skip_count_recent": 0,
    "play_count_lifetime": 12, "playlists": ["Existing Playlist"],
}
sld._LIKED_URIS.add("spotify:track:aaa111")
added = sld.augment_from_tunemymusic_csv(csv1)  # reuse csv1 from test 2 — contains aaa111
_check("existing URI not counted as newly added", added == 1)  # only bbb222 is new this time
_check("existing cache entry's title untouched", sld._CACHE["spotify:track:aaa111"]["track"] == "Canonical Title")
_check("existing play_count untouched", sld._CACHE["spotify:track:aaa111"]["play_count_recent"] == 5)

# ─── 4. Malformed rows — missing Spotify id / missing title skipped ────────
print("\n[4] Malformed rows are skipped, not crashed on")
_reset_state()
csv2 = tmpdir / "malformed.csv"
csv2.write_text(
    _HEADER +
    '"","","","Favorite Songs","Favorite","","  "\n'          # blank spotify id -> skip entirely
    '"","Artist Only","","Favorite Songs","Favorite","","ccc333"\n'  # blank title -> skip
    '"Valid Song","Valid Artist","","Favorite Songs","Favorite","","ddd444"\n',
    encoding="utf-8",
)
added = sld.augment_from_tunemymusic_csv(csv2)
_check("only the one fully-valid row was added", added == 1)
_check("valid row present", "spotify:track:ddd444" in sld._CACHE)
_check("blank-title row absent", "spotify:track:ccc333" not in sld._CACHE)

# ─── 5. Auto-discovery via config.TUNEMYMUSIC_CSV_PATH override ───────────
print("\n[5] Auto-discovery honors config.TUNEMYMUSIC_CSV_PATH")
_reset_state()
config.TUNEMYMUSIC_CSV_PATH = str(csv1)
added = sld.augment_from_tunemymusic_csv()  # no path given — must auto-discover
_check("auto-discovered via config override", added == 2)
config.TUNEMYMUSIC_CSV_PATH = ""

# ─── 6. Live smoke test against the user's REAL CSV (read-only, if present) ─
print("\n[6] Live smoke test against the real 'My Spotify Library.csv' (if present)")
real_csv = pathlib.Path.home() / "Downloads" / "My Spotify Library.csv"
if real_csv.exists():
    _reset_state()
    t0 = time.time()
    added = sld.augment_from_tunemymusic_csv(real_csv)
    elapsed = time.time() - t0
    _check("real CSV parsed without raising", True)
    _check("real CSV produced a plausible non-zero track count", added > 1000)
    _check(f"completed in reasonable time ({elapsed:.2f}s)", elapsed < 30)
    print(f"    -> {added} tracks added, {len(sld._PLAYLISTS)} playlists, "
          f"{len(sld._LIKED_URIS)} liked, {len(sld._ISRC_BY_URI)} ISRCs — {elapsed:.2f}s")
else:
    print("    (real CSV not found at ~/Downloads/My Spotify Library.csv — skipping live smoke test)")

_reset_state()
import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*60}\nTask 12 (TuneMyMusic CSV supplement): {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
