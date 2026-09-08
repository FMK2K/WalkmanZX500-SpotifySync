"""
test_task13_listenbrainz.py — isolated verification for listenbrainz_client.py
(Task 13, pivoted from Explo to direct ListenBrainz API integration)

Stage 2 of this project's verification ladder. No network required — mocks
requests.get with realistic ListenBrainz API response shapes (createdfor
playlist metadata + JSPF playlist detail).
"""
import sys
from unittest import mock

import config
import listenbrainz_client as lbc

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


class _Resp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


# ─── 1. fetch_recommendation_playlists — happy path ─────────────────────────
print("\n[1] fetch_recommendation_playlists — parses createdfor response")
_CREATEDFOR = {
    "playlists": [
        {"playlist": {"title": "Weekly Jams", "identifier": "https://listenbrainz.org/playlist/aaa-111"}},
        {"playlist": {"title": "Weekly Exploration", "identifier": "https://listenbrainz.org/playlist/bbb-222"}},
    ]
}
with mock.patch("requests.get", return_value=_Resp(200, _CREATEDFOR)):
    playlists = lbc.fetch_recommendation_playlists("testuser")
_check("returns 2 playlists", len(playlists) == 2)
_check("mbid extracted from identifier URI tail", playlists[0]["mbid"] == "aaa-111")
_check("title preserved", playlists[0]["title"] == "Weekly Jams")

# ─── 2. fetch_recommendation_playlists — no username, no network call ──────
print("\n[2] fetch_recommendation_playlists — empty username short-circuits")
with mock.patch("requests.get") as m_get:
    playlists = lbc.fetch_recommendation_playlists("")
_check("returns empty list", playlists == [])
_check("no network call made", not m_get.called)

# ─── 3. fetch_playlist_tracks — JSPF parsing ────────────────────────────────
print("\n[3] fetch_playlist_tracks — parses JSPF track list")
_JSPF = {
    "playlist": {
        "title": "Weekly Jams",
        "track": [
            {"title": "Song One", "creator": "Artist A", "album": "Album A"},
            {"title": "Song Two", "creator": "Artist B", "album": ""},
        ],
    }
}
with mock.patch("requests.get", return_value=_Resp(200, _JSPF)):
    tracks = lbc.fetch_playlist_tracks("aaa-111")
_check("returns 2 tracks", len(tracks) == 2)
_check("track dict shape matches downloader.py convention", set(tracks[0].keys()) == {"title", "artist", "album"})
_check("artist mapped from JSPF 'creator' field", tracks[0]["artist"] == "Artist A")
_check("blank album tolerated", tracks[1]["album"] == "")

# ─── 4. fetch_playlist_tracks — skips malformed entries ─────────────────────
print("\n[4] fetch_playlist_tracks — skips tracks missing title/artist")
_JSPF_PARTIAL = {"playlist": {"track": [
    {"title": "", "creator": "Artist A"},
    {"title": "Valid Song", "creator": "Artist B"},
]}}
with mock.patch("requests.get", return_value=_Resp(200, _JSPF_PARTIAL)):
    tracks = lbc.fetch_playlist_tracks("ccc-333")
_check("blank-title entry skipped, only valid track kept", len(tracks) == 1 and tracks[0]["title"] == "Valid Song")

# ─── 5. HTTP failure degrades to empty list, never raises ──────────────────
print("\n[5] Non-200 response degrades to []")
with mock.patch("requests.get", return_value=_Resp(500, {})):
    playlists = lbc.fetch_recommendation_playlists("testuser")
    tracks = lbc.fetch_playlist_tracks("aaa-111")
_check("playlists empty on HTTP error", playlists == [])
_check("tracks empty on HTTP error", tracks == [])

# ─── 6. Network exception degrades to empty list, never raises ─────────────
print("\n[6] Exception during request degrades to [] without raising")
with mock.patch("requests.get", side_effect=RuntimeError("network down")):
    try:
        playlists = lbc.fetch_recommendation_playlists("testuser")
        _check("no exception raised", True)
        _check("returns [] on exception", playlists == [])
    except Exception:
        _check("no exception raised", False)

# ─── 7. fetch_discovery_queue — disabled by default, dedup, aggregation ────
print("\n[7] fetch_discovery_queue — disabled short-circuit + end-to-end aggregation")
config.LISTENBRAINZ_DISCOVERY_ENABLED = False
with mock.patch("requests.get") as m_get:
    queue = lbc.fetch_discovery_queue("testuser")
_check("disabled by default returns []", queue == [])
_check("no network call when disabled", not m_get.called)

config.LISTENBRAINZ_DISCOVERY_ENABLED = True
_DUP_JSPF_1 = {"playlist": {"track": [{"title": "Shared Song", "creator": "Same Artist", "album": ""}]}}
_DUP_JSPF_2 = {"playlist": {"track": [
    {"title": "Shared Song", "creator": "Same Artist", "album": ""},   # duplicate across playlists
    {"title": "Unique Song", "creator": "Other Artist", "album": ""},
]}}

def _fake_get(url, params=None, timeout=None, headers=None):
    if "createdfor" in url:
        return _Resp(200, _CREATEDFOR)
    if "aaa-111" in url:
        return _Resp(200, _DUP_JSPF_1)
    if "bbb-222" in url:
        return _Resp(200, _DUP_JSPF_2)
    return _Resp(404, {})

with mock.patch("requests.get", side_effect=_fake_get):
    queue = lbc.fetch_discovery_queue("testuser")
_check("aggregated across 2 playlists with dedup", len(queue) == 2)
_check("dedup kept exactly one 'Shared Song'", sum(1 for t in queue if t["title"] == "Shared Song") == 1)

config.LISTENBRAINZ_DISCOVERY_ENABLED = False

print(f"\n{'='*60}\nTask 13 (ListenBrainz discovery client): {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
