"""
test_genre_phase2_failopen.py — isolated verification for the 2026-07-24 fix:
genre_client.enrich_metadata() must not crash the whole run when Phase 1.5+2
(_slow_lookup, e.g. MusicBrainz/Discogs blocked by OFFLINE_MODE) raises for a
single track. No network required — Phase 1 and Phase 2 are both mocked.
"""
from unittest import mock

import genre_client as gc

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


tracks = [
    {"track_id": "t1", "artist": "Artist One", "title": "Song One", "spotify_year": "2020"},
    {"track_id": "t2", "artist": "Artist Two", "title": "Song Two", "spotify_year": "2021"},
]

print("[1] _slow_lookup raising (simulating OfflineProviderViolation) does not crash enrich_metadata")
with mock.patch("genre_client._should_refetch", return_value=True), \
     mock.patch("genre_client._fast_lookup", return_value={}), \
     mock.patch("genre_client._slow_lookup", side_effect=RuntimeError("OFFLINE_PROVIDER_ACCESS_BLOCKED")), \
     mock.patch.object(gc, "_genre_cache_set", lambda *a, **k: None), \
     mock.patch.object(gc, "_get_genre_cache", return_value={}):
    try:
        result = gc.enrich_metadata(tracks)
        _check("enrich_metadata returns without raising", True)
        _check("returns a dict", isinstance(result, dict))
    except Exception as e:
        _check(f"enrich_metadata returns without raising (raised {e!r} instead)", False)

print("\n[2] second track still gets processed after the first one's lookup raised")
call_log = []


def _slow_lookup_side_effect(artist, title, need_genre, need_year, spotify_id=""):
    call_log.append(artist)
    if artist == "Artist One":
        raise RuntimeError("OFFLINE_PROVIDER_ACCESS_BLOCKED")
    return {"genre": "Rock", "year": "2021"}


with mock.patch("genre_client._should_refetch", return_value=True), \
     mock.patch("genre_client._fast_lookup", return_value={}), \
     mock.patch("genre_client._slow_lookup", side_effect=_slow_lookup_side_effect), \
     mock.patch.object(gc, "_genre_cache_set", lambda *a, **k: None), \
     mock.patch.object(gc, "_get_genre_cache", return_value={}):
    try:
        gc.enrich_metadata(tracks)
        _check("both tracks attempted despite first raising",
               call_log == ["Artist One", "Artist Two"])
    except Exception as e:
        _check(f"both tracks attempted despite first raising (raised {e!r} instead)", False)

print(f"\n{'=' * 60}")
print(f"genre_client Phase 1.5+2 fail-open: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
