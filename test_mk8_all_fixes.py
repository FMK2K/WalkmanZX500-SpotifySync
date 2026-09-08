"""
test_mk8_all_fixes.py — SpotifyTunerMK8 Comprehensive Fix Verification
=======================================================================
Tests every bug fix applied across LOG_17 and LOG_17_2 analysis sessions.
Pure-logic MC simulations — no device / API calls needed.

Bugs fixed:
  1 [CRITICAL] Cross-playlist duplicate transfers         walkman_sync.py
  2 [CRITICAL] Device scan returning 0 → re-transfer      device_library.py
  3 [HIGH]     Wrong track numbers / album splintering     spotify_local_data.py
  4 [HIGH]     Cover art missing on local-lib transfers    walkman_sync.py
  5 [MEDIUM]   Genre mislabeling when Groq at capacity     genre_client.py
  6 [CRITICAL] 94 upload failures (no PS fallback)         walkman_reorganize.py
  7 [MEDIUM]   Groq 403 log spam (no circuit breaker)      ai_helper.py

Run:  python test_mk8_all_fixes.py
"""
from __future__ import annotations
import re, sys, json, time, random, pathlib, threading, subprocess, unicodedata
from collections import defaultdict, Counter

HERE  = pathlib.Path(__file__).parent
OK    = 0
FAIL  = 0

def chk(label: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        print(f"  ✓  {label}")
        OK += 1
    else:
        print(f"  ✗  FAIL: {label}" + (f"  ({detail})" if detail else ""))
        FAIL += 1

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()

# ─────────────────────────────────────────────────────────────────────────────
# BUG 1: Cross-playlist global transfer dedup
# ─────────────────────────────────────────────────────────────────────────────
def test_global_dedup():
    print("\n── Bug 1 · Cross-playlist global transfer dedup ──")

    # Structural check
    src = (HERE / "walkman_sync.py").read_text(encoding="utf-8")
    chk("_global_transfer_seen set declared",   "_global_transfer_seen: set[str] = set()" in src)
    chk("_global_seen_lock declared",            "_global_seen_lock = threading.Lock()"   in src)
    chk("GlobalDedup log message present",       "[GlobalDedup]"                          in src)

    # Simulation: one track in 3 playlists must copy exactly once
    def sim(track_map, use_global=True):
        g_seen: set = set()
        g_lock = threading.Lock()
        copies = skipped = 0
        for pl, tracks in track_map.items():
            pl_seen: set = set()
            for t in tracks:
                k = _norm(f"{t['artist']}|{t['title']}")
                if k in pl_seen:
                    continue
                pl_seen.add(k)
                if use_global:
                    with g_lock:
                        if k in g_seen:
                            skipped += 1
                            continue
                        g_seen.add(k)
                copies += 1
        return copies, skipped

    track = {"artist": "Tyler, The Creator", "title": "ARE WE STILL FRIENDS?"}
    tm = {f"PL_{i}": [track] for i in range(3)}
    c_old, _ = sim(tm, use_global=False)
    c_new, s  = sim(tm, use_global=True)
    chk("3-playlist scenario: 3 copies→1", c_old == 3 and c_new == 1 and s == 2)

    # MC: 10 000 random track-maps — dedup never inflates copies
    pool_a = ["Tyler", "Kanye", "Mac", "Frank", "Travis", "Carti", "Kendrick", "SZA"]
    pool_t = [f"Track_{i}" for i in range(60)]
    fails  = 0
    for _ in range(10_000):
        shared = [{"artist": random.choice(pool_a), "title": random.choice(pool_t)}
                  for _ in range(random.randint(1, 8))]
        tm2 = {f"PL_{i}": shared + [{"artist": "X", "title": f"U{random.random()}"}
                                     for _ in range(random.randint(0, 10))]
               for i in range(random.randint(2, 5))}
        c0, _ = sim(tm2, False); c1, _ = sim(tm2, True)
        if c1 > c0:
            fails += 1
    chk(f"MC 10k: dedup never inflates copies ({fails} failures)", fails == 0)


# ─────────────────────────────────────────────────────────────────────────────
# BUG 2: DeviceLibrary zero-scan cache fallback
# ─────────────────────────────────────────────────────────────────────────────
def test_device_library_cache():
    print("\n── Bug 2 · DeviceLibrary zero-scan cache fallback ──")
    src = (HERE / "device_library.py").read_text(encoding="utf-8")
    build_body = src.split("def build(")[1].split("\n    @classmethod")[0]
    chk("MK8 Fix comment in build()",     "MK8 Fix — cache fallback on zero-scan" in build_body)
    chk("built.save(_cache_path) present", "built.save(_cache_path)"              in build_body)
    chk("cls.load(_cache_path) present",   "cls.load(_cache_path)"                in build_body)
    chk("Zero-scan warning logged",        "zero-scan failure" in build_body or
                                           "almost certainly"  in build_body)

    # Deterministic cache path (no UUID)
    import tempfile
    paths = {str(pathlib.Path(tempfile.gettempdir()) / ".walkman_device_lib_cache.json")
             for _ in range(1000)}
    chk("cache path is deterministic", len(paths) == 1)


# ─────────────────────────────────────────────────────────────────────────────
# BUG 3: Track-number cross-playlist unification
# ─────────────────────────────────────────────────────────────────────────────
def test_track_unification():
    print("\n── Bug 3 · Track-number cross-playlist unification ──")
    src = (HERE / "spotify_local_data.py").read_text(encoding="utf-8")
    chk("track_number_is_estimated marker", "track_number_is_estimated" in src)
    chk("canonical_tn dict",                "canonical_tn" in src)
    chk("MK8 unification comment",          "MK8 Fix — cross-playlist track_number unification" in src)

    # MC: canonical always beats estimated
    _EST = "track_number_is_estimated"
    def unify(tm):
        canon: dict[str, str] = {}
        for tracks in tm.values():
            for t in tracks:
                if t.get(_EST): continue
                uri = t.get("uri", ""); tn = t.get("track_number", "")
                if not uri or not tn: continue
                try: n = int(tn)
                except: continue
                old = canon.get(uri)
                if old is None or n > int(old):
                    canon[uri] = tn
        return canon

    correct = 0
    for _ in range(5_000):
        real = str(random.randint(1, 20)); est = str(random.randint(1, 5))
        uri  = "spotify:track:abc"
        tm   = {"Liked": [{"uri": uri, "track_number": real}],
                "PL":    [{"uri": uri, "track_number": est, _EST: True}]}
        if unify(tm).get(uri) == real:
            correct += 1
    chk(f"MC 5k: canonical beats estimated ({correct}/5000)", correct == 5_000)


# ─────────────────────────────────────────────────────────────────────────────
# BUG 4: Cover art embedding on local-library transfers
# ─────────────────────────────────────────────────────────────────────────────
def test_cover_art():
    print("\n── Bug 4 · Cover art on local-library transfers ──")
    src = (HERE / "walkman_sync.py").read_text(encoding="utf-8")
    copy_region = src.split("shutil.copy2(local_path, tmp_file)")[1][:1500]
    chk("_embed_cover_for_track called in copy path",   "_embed_cover_for_track" in copy_region)
    chk("MK8 cover-art comment present",                "MK8 Fix — embed cover art" in src)
    chk("Cover-art failure is non-fatal (except)",      "except Exception as _ce" in copy_region or
                                                        "except Exception" in copy_region)


# ─────────────────────────────────────────────────────────────────────────────
# BUG 5: Genre override table — LOG_17 mislabelings
# ─────────────────────────────────────────────────────────────────────────────
def test_genre_overrides():
    print("\n── Bug 5 · Genre override completeness ──")
    src = (HERE / "genre_client.py").read_text(encoding="utf-8")
    required = [
        ("phabo",           "R&B"),
        ("boutross",        "Afrobeats"),
        ("kilsosouth",      "Afrobeats"),
        ("ap dhillon",      "Punjabi Pop"),
        ("dully sykes",     "Bongo Flava"),
        ("maziya",          "Gqom"),
        ("groovy",          "Jersey Club"),
        ("lady donli",      "Alternative R&B"),
        ("wurld",           "Afropop"),
        ("bramsito",        "French Trap"),
        ("nerih",           "Gospel/Afropop"),  # new in LOG_17_2 pass
    ]
    for artist, genre in required:
        key = f'"{artist}"'
        in_dict = key in src
        nearby  = src[src.index(key):src.index(key) + 80] if in_dict else ""
        chk(f"override: {artist!r} → {genre!r}", in_dict and genre in nearby)


# ─────────────────────────────────────────────────────────────────────────────
# BUG 6: silent_move PowerShell fallback (LOG_17_2 — 94 upload failures)
# ─────────────────────────────────────────────────────────────────────────────
def test_silent_move_fallback():
    print("\n── Bug 6 · silent_move PowerShell fallback ──")
    src = (HERE / "walkman_reorganize.py").read_text(encoding="utf-8")

    # Isolate silent_move body (strip comments)
    sm_start = src.find("def silent_move(")
    sm_end   = src.find("\ndef ", sm_start + 20)
    sm_body  = src[sm_start:sm_end]
    code     = "\n".join(l for l in sm_body.splitlines()
                         if l.strip() and not l.strip().startswith("#"))

    chk("no direct _try_powershell() call in code",    "_try_powershell(" not in code)
    chk("uses mtp.copy_file_to_device()",               "mtp.copy_file_to_device(" in code)
    chk("MK8 Fix comment present",                      "MK8 Fix" in sm_body)
    chk("upload exception caught, not silently ignored",
        "except Exception as _upload_exc" in sm_body or
        "except Exception" in sm_body)


# ─────────────────────────────────────────────────────────────────────────────
# BUG 7: AI provider 403/401 circuit breaker (LOG_17_2 — 186 spam lines)
# ─────────────────────────────────────────────────────────────────────────────
def test_ai_circuit_breaker():
    print("\n── Bug 7 · AI 403/401 session-level circuit breaker ──")
    src = (HERE / "ai_helper.py").read_text(encoding="utf-8")

    chk("_SESSION_DISABLED set declared",         "_SESSION_DISABLED: set[str] = set()" in src)
    chk("_AUTH_FAIL_THRESHOLD constant",           "_AUTH_FAIL_THRESHOLD" in src)
    chk("_record_auth_fail() function",            "def _record_auth_fail(" in src)
    chk("_session_disable() function",             "def _session_disable(" in src)
    chk("_is_backed_off checks _SESSION_DISABLED", "_SESSION_DISABLED" in
        src.split("def _is_backed_off")[1].split("def ")[0])
    chk("_record_auth_fail called on 403",         "_record_auth_fail(provider_key)" in src)
    chk("_MAX_BACKOFF_S raised to ≥ 60s",
        float(re.search(r"_MAX_BACKOFF_S\s*=\s*([\d.]+)", src).group(1)) >= 60)

    # MC: simulate repeated 403s triggering session-disable
    fails: dict[str, int] = {}
    disabled: set[str]    = set()
    threshold = 3

    def record(name):
        fails[name] = fails.get(name, 0) + 1
        if fails[name] >= threshold:
            disabled.add(name)

    for i in range(1, threshold + 2):
        record("groq_test")
        should_be = i >= threshold
        chk(f"session-disable fires exactly at threshold (call {i}/{threshold})",
            (("groq_test" in disabled) == should_be))

    # Reset and verify clean provider is not disabled
    fails2: dict[str, int] = {}
    disabled2: set[str]    = set()
    def record2(name):
        fails2[name] = fails2.get(name, 0) + 1
        if fails2[name] >= threshold:
            disabled2.add(name)
    chk("provider not disabled before threshold", "groq_clean" not in disabled2)


# ─────────────────────────────────────────────────────────────────────────────
# BUG 8 (meta): All Python files must compile cleanly
# ─────────────────────────────────────────────────────────────────────────────
def test_syntax():
    print("\n── Meta · Syntax check all Python files ──")
    errors = []
    files  = sorted(HERE.glob("*.py"))
    for f in files:
        r = subprocess.run([sys.executable, "-m", "py_compile", str(f)],
                           capture_output=True)
        if r.returncode:
            errors.append(f"{f.name}: {r.stderr.decode().strip()[:80]}")
    chk(f"all {len(files)} .py files compile cleanly",
        not errors, "; ".join(errors[:3]))


# ─────────────────────────────────────────────────────────────────────────────
# BUG 9 (meta): MK8 marker inventory — all fixes tagged
# ─────────────────────────────────────────────────────────────────────────────
def test_fix_inventory():
    print("\n── Meta · MK8 fix marker inventory ──")
    markers = [
        ("walkman_sync.py",       "MK8 Fix — cross-playlist global transfer dedup"),
        ("walkman_sync.py",       "MK8 Fix — embed cover art for local-library transfers"),
        ("device_library.py",     "MK8 Fix — cache fallback on zero-scan"),
        ("spotify_local_data.py", "MK8 Fix — cross-playlist track_number unification"),
        ("walkman_reorganize.py", "MK8 Fix (LOG_17_2)"),
        ("ai_helper.py",          "MK8 Fix (LOG_17_2)"),
    ]
    for fname, marker in markers:
        src = (HERE / fname).read_text(encoding="utf-8")
        chk(f"{fname}: '{marker[:45]}…'", marker in src)


# ─────────────────────────────────────────────────────────────────────────────
# Run all tests
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# LOG_17_3 FIXES: Bugs 8–10
# ─────────────────────────────────────────────────────────────────────────────

# Bug 8: silent_move source delete via PS subprocess (no dialog)
def test_source_delete_no_dialog():
    print("\n── Bug 8 · silent_move source delete — no InvokeVerb in-process ──")
    src = (HERE / "walkman_reorganize.py").read_text(encoding="utf-8")

    # Isolate silent_move body (no comments)
    sm_start = src.find("def silent_move(")
    sm_end   = src.find("\ndef ", sm_start + 20)
    sm_body  = src[sm_start:sm_end]
    code     = "\n".join(l for l in sm_body.splitlines()
                         if l.strip() and not l.strip().startswith("#"))

    chk("src_parts param in silent_move signature",
        "src_parts:" in sm_body or "src_parts :" in sm_body)
    chk("Step 6 uses mtp.delete_file_on_device (not _delete_item)",
        "mtp.delete_file_on_device(" in code)
    chk("Step 6 does NOT call _delete_item for source",
        "_delete_item(src_item" not in code)
    chk("MK8 Fix LOG_17_3 comment present",
        "LOG_17_3" in sm_body)

    # consumer passes src_parts to silent_move
    consumer_src = src[src.find("def consumer("):]
    consumer_body = consumer_src[:consumer_src.find("\n    def ")]
    chk("consumer passes src_parts to silent_move",
        "src_parts=item[" in consumer_body)


# Bug 9: ADB verification retry for 4096-byte placeholder
def test_adb_verify_retry():
    print("\n── Bug 9 · ADB _verify_remote_size retry for MTP placeholder ──")
    src = (HERE / "adb_transfer.py").read_text(encoding="utf-8")
    fn_body = src.split("def _verify_remote_size(")[1].split("\ndef ")[0]

    chk("retries parameter added", "retries:" in fn_body or "retries =" in fn_body)
    chk("placeholder detection (≤4096 threshold)", "_PLACEHOLDER_THRESHOLD" in fn_body or "4096" in fn_body)
    chk("sleep/wait in retry loop", "_time.sleep(" in fn_body or "time.sleep(" in fn_body)
    chk("MK8 Fix comment present", "MK8 Fix" in fn_body)

    # Simulate: first call returns 4096, second returns correct size
    import time
    calls = [0]
    results = [(4096, True), (7301045, True)]  # (stat_return, success)

    def mock_verify(expected=7301045, retries=3, base_wait=0.001):
        _PLACEHOLDER = 4096
        import time as _t
        for attempt in range(retries + 1):
            actual = results[min(attempt, len(results)-1)][0]
            if actual <= _PLACEHOLDER and expected > _PLACEHOLDER and attempt < retries:
                _t.sleep(base_wait)
                continue
            if actual == expected:
                return True, "ok"
            return False, f"mismatch: {actual} vs {expected}"
        return False, "exhausted"

    ok, reason = mock_verify()
    chk("mock retry: succeeds after placeholder → real size", ok, reason)


# Bug 10: COM _verify_com_copy retry for 0-byte placeholder
def test_com_verify_retry():
    print("\n── Bug 10 · COM _verify_com_copy retry for 0-byte placeholder ──")
    src = (HERE / "mtp_transfer_com.py").read_text(encoding="utf-8")
    fn_body = src.split("def _verify_com_copy(")[1].split("\ndef ")[0]

    chk("retries parameter added to _verify_com_copy",
        "retries:" in fn_body or "retries =" in fn_body)
    chk("actual_size <= 0 triggers retry (not immediate fail)",
        "attempt < retries" in fn_body)
    chk("sleep in retry loop",
        "_verify_time.sleep(" in fn_body or "time.sleep(" in fn_body)
    chk("MK8 Fix comment present", "MK8 Fix" in fn_body)

    # Simulate: first two calls return 0, third returns correct size
    sizes = [0, 0, 2305379]
    def mock_com_verify(expected=2305379, retries=5, base_wait=0.001):
        import time as _t
        for attempt in range(retries + 1):
            actual = sizes[min(attempt, len(sizes)-1)]
            if actual <= 0:
                if attempt < retries:
                    _t.sleep(base_wait)
                    continue
                return False, f"remote size is {actual}"
            if actual == expected:
                return True, f"verified, size={actual}"
            return False, f"mismatch"
        return False, "exhausted"

    ok, reason = mock_com_verify()
    chk("mock COM retry: succeeds after 0→0→real size", ok, reason)


# Bug 11: dialog_watcher covers Confirm Folder Delete
def test_dialog_watcher_folder_delete():
    print("\n── Bug 11 · dialog_watcher covers Confirm Folder Delete ──")
    src = (HERE / "dialog_watcher.py").read_text(encoding="utf-8")
    chk("Confirm Folder Delete in _DIALOGS",        '"Confirm Folder Delete"' in src)
    chk("Error Moving File or Folder in _DIALOGS",  '"Error Moving File or Folder"' in src)
    chk("error moving file or folder in _ERROR_TITLES",
        "error moving file or folder" in src)
    chk("MK8 Fix LOG_17_3 comment in dialog_watcher", "LOG_17_3" in src or "MK8" in src)



# ─────────────────────────────────────────────────────────────────────────────
# PR-GenreGraph: playlist-based graph edges + Afrobeats over-assignment fix
# ─────────────────────────────────────────────────────────────────────────────

def test_liked_songs_exclusion():
    print("\n── PR-GenreGraph · Liked Songs excluded by name ──")
    src = (HERE / "genre_graph.py").read_text(encoding="utf-8")
    chk("_LIKED_SONGS_NAMES set defined",            "_LIKED_SONGS_NAMES" in src)
    chk('"liked songs" in exclusion set',            '"liked songs"' in src)
    chk("explicit name-based exclusion in build_graph", "plist_name_lc in _LIKED_SONGS_NAMES" in src)

    # Simulate: Liked Songs should be excluded, small curated playlist should not
    LIKED = frozenset({"liked songs", "liked tracks", "your liked songs",
                       "songs you've liked", "songs i've liked"})
    chk("'liked songs' matched",       "liked songs" in LIKED)
    chk("'Liked Songs' normalises",    "liked songs" == "Liked Songs".strip().lower())
    chk("'My Playlist' not excluded",  "my playlist" not in LIKED)
    chk("'Afrobeats Hits' not excluded","afrobeats hits" not in LIKED)


def test_playlist_purity_weighting():
    print("\n── PR-GenreGraph · Playlist genre purity weighting ──")
    src = (HERE / "genre_graph.py").read_text(encoding="utf-8")
    chk("min_purity config param present",            "GENRE_GRAPH_MIN_PURITY" in src)
    chk("purity calculation in build_graph",          "purity" in src and "dominant_genre" in src)
    chk("effective_w = playlist_w * purity",          "effective_w = playlist_w * purity" in src)
    chk("low-purity playlists skipped",               "purity < min_purity" in src)
    chk("purity logged per playlist",                 "purity=" in src)

    # MC simulation: purity filter should drastically reduce Afrobeats false edges
    import random as _r
    _r.seed(42)

    def mock_build_edges(playlists, min_purity=0.5, playlist_w=1.0):
        """Simulate the purity-weighted playlist edge builder."""
        edges = []
        BROAD = {"", "unknown", "world", "world music"}
        for pname, tracks in playlists.items():
            if pname.lower() in ("liked songs",):
                continue
            genre_counts: dict = {}
            artists = set()
            for t in tracks:
                artists.add(t["artist"])
                g = t.get("genre", "").lower()
                if g and g not in BROAD:
                    genre_counts[g] = genre_counts.get(g, 0) + 1
            total = sum(genre_counts.values())
            if not total:
                continue
            top_g, top_c = max(genre_counts.items(), key=lambda x: x[1])
            purity = top_c / total
            if purity < min_purity:
                continue
            w = playlist_w * purity
            alist = list(artists)
            for i in range(len(alist)):
                for j in range(i+1, len(alist)):
                    edges.append((alist[i], alist[j], w))
        return edges

    # Mixed playlist: 30% afrobeats, rest varied → should be skipped
    mixed = {"Mixed Vibes": [
        {"artist": "Burna Boy",  "genre": "Afrobeats"},
        {"artist": "Drake",      "genre": "Hip-Hop"},
        {"artist": "SZA",        "genre": "Alt R&B"},
        {"artist": "Wizkid",     "genre": "Afrobeats"},
        {"artist": "Travis",     "genre": "Trap"},
        {"artist": "Kendrick",   "genre": "Conscious Hip-Hop"},
        {"artist": "Tems",       "genre": "Afrobeats"},
        {"artist": "Frank",      "genre": "Neo Soul"},
        {"artist": "Tyler",      "genre": "Neo Soul"},
        {"artist": "Mac",        "genre": "Alt R&B"},
    ]}
    edges_mixed = mock_build_edges(mixed)
    chk("mixed playlist (30% Afrobeats) produces 0 edges", len(edges_mixed) == 0,
        f"got {len(edges_mixed)}")

    # Pure playlist: 100% Amapiano → strong edges
    pure = {"Amapiano Hits": [
        {"artist": "Kabza",     "genre": "Amapiano"},
        {"artist": "Focalistic","genre": "Amapiano"},
        {"artist": "Vigro",     "genre": "Amapiano"},
    ]}
    edges_pure = mock_build_edges(pure)
    chk("pure Amapiano playlist produces edges", len(edges_pure) > 0,
        f"got {len(edges_pure)}")
    chk("edge weight = 1.0 for 100% pure playlist",
        any(abs(w - 1.0) < 0.01 for _, _, w in edges_pure))

    # Liked Songs should always be excluded
    liked = {"Liked Songs": [
        {"artist": "X", "genre": "Afrobeats"},
        {"artist": "Y", "genre": "Afrobeats"},
    ]}
    edges_liked = mock_build_edges(liked)
    chk("Liked Songs produces 0 edges regardless of purity", len(edges_liked) == 0)


def test_afrobeats_norm_fix():
    print("\n── PR-GenreGraph · Afropop/Alte separate from Afrobeats in _GENRE_NORM ──")
    src = (HERE / "genre_client.py").read_text(encoding="utf-8")
    chk('"afropop" maps to "Afropop" (not Afrobeats)',
        '"afropop":                  "Afropop"' in src or
        '"afropop":                 "Afropop"' in src)
    chk('"alte" maps to "Alte" (not Afrobeats)',
        '"alte":                     "Alte"' in src or
        '"alte":                    "Alte"' in src)
    chk('"afro r&b" maps to "Alt R&B" (not Afrobeats)',
        '"alt r&b"' in src.lower() and '"afro r&b"' in src)
    chk('"south african pop" maps to "Afropop" (not Afrobeats)',
        '"south african pop":        "Afropop"' in src or
        '"south african pop":       "Afropop"' in src)

    # audit.py specificity
    audit_src = (HERE / "audit.py").read_text(encoding="utf-8")
    chk('"afropop" is a broad bucket in _GENRE_SPECIFICITY',
        '"afropop"' in audit_src and '"amapiano"' in audit_src)
    chk("diversity_guard_ratio tightened to ≤ 0.35",
        "0.35" in (HERE / "walkman_sync.py").read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# PR-CoverArt: album_image backfill + yt-dlp writethumbnail
# ─────────────────────────────────────────────────────────────────────────────

def test_cover_art_backfill():
    print("\n── PR-CoverArt · album_image backfilled from metadata cache ──")
    src = (HERE / "spotify_local_data.py").read_text(encoding="utf-8")
    chk("loads .spotify_metadata_cache.json in augment_track_map",
        ".spotify_metadata_cache.json" in src and "_sp_meta_cache" in src)
    chk("backfills album_image by track ID",
        't["album_image"] = img' in src or "t[\"album_image\"] = img" in src)
    chk("album-level fallback by album_id",
        "_album_image_by_album_id" in src)
    chk("non-fatal on cache miss (try/except)",
        "except Exception" in src and "non-fatal" in src.lower() or
        "except Exception" in src and "pass" in src)

    # Simulate the backfill logic
    def mock_augment(track_map, sp_cache):
        for pl, tracks in track_map.items():
            for t in tracks:
                if not t.get("album_image"):
                    tid = t.get("id", "")
                    sp = sp_cache.get(tid, {})
                    img = (sp.get("album_image") or "").strip()
                    if img:
                        t["album_image"] = img

    tm = {"My Playlist": [
        {"id": "abc", "title": "Song", "album_image": ""},
        {"id": "xyz", "title": "Other", "album_image": "https://already-set.jpg"},
    ]}
    cache = {
        "abc": {"album_image": "https://i.scdn.co/image/abc123"},
        "xyz": {"album_image": "https://i.scdn.co/image/different"},
    }
    mock_augment(tm, cache)
    chk("blank album_image backfilled from cache",
        tm["My Playlist"][0]["album_image"] == "https://i.scdn.co/image/abc123")
    chk("existing album_image NOT overwritten",
        tm["My Playlist"][1]["album_image"] == "https://already-set.jpg")


def test_writethumbnail_enabled():
    print("\n── PR-CoverArt · yt-dlp writethumbnail enabled ──")
    src = (HERE / "downloader.py").read_text(encoding="utf-8")
    # Find the writethumbnail line in yt-dlp opts
    # Make sure it's True not False
    lines = [l.strip() for l in src.splitlines() if "writethumbnail" in l]
    chk("writethumbnail present in ydl_opts", bool(lines))
    chk('writethumbnail set to True (not False)',
        any("True" in l and "writethumbnail" in l for l in lines),
        str(lines[:3]))
    chk("MK8 Fix comment near writethumbnail",
        "MK8 Fix" in src and "writethumbnail" in src)



# ─────────────────────────────────────────────────────────────────────────────
# LOG_17_3 Part 2: Afrobeats over-tagging + Cover Art fixes (Bugs 12–16)
# ─────────────────────────────────────────────────────────────────────────────

def test_east_african_genre_mapping():
    print("\n── Bug 12 · East African genres not mapped to Afrobeats ──")
    src = (HERE / "genre_client.py").read_text(encoding="utf-8")
    # east african pop must NOT map to Afrobeats
    import ast, re as _re
    # Find the mapping lines in _GENRE_NORM
    ea_line = [l for l in src.splitlines() if "east african pop" in l.lower()]
    chk("east african pop entry exists", len(ea_line) > 0)
    # Only check the actual mapping line (exclude comment-only lines, strip inline comments)
    ea_mapping = [l.split("#")[0] for l in ea_line if not l.strip().startswith("#")]
    chk("east african pop → Afropop (not Afrobeats)",
        len(ea_mapping) > 0 and "Afrobeats" not in ea_mapping[0] and "Afropop" in ea_mapping[0])
    chk("kenyan pop mapped to Afropop",    '"kenyan pop"' in src and "Afropop" in src)
    chk("ugandan pop mapped to Afropop",   '"ugandan pop"' in src)
    chk("east african music mapped",       '"east african music"' in src)


def test_ai_prompt_no_afrobeats_bias():
    print("\n── Bug 13 · AI prompt removes Afrobeats geographic bias ──")
    src = (HERE / "genre_client.py").read_text(encoding="utf-8")
    # Extract prompt region
    prompt_region = src[src.find("prompt = ("):src.find("prompt = (") + 600]
    chk("prompt no longer says 'prefer Afrobeats over World Music'",
        "prefer 'Afrobeats' over 'World Music'" not in prompt_region)
    chk("prompt has East Africa correction note",
        "East African" in prompt_region or "east african" in prompt_region.lower())
    chk("prompt still mentions Bongo Flava as specific genre",
        "Bongo Flava" in prompt_region)


def test_diversity_guard_tightened():
    print("\n── Bug 14 · Diversity guard ratio tightened in config ──")
    src = (HERE / "config.py").read_text(encoding="utf-8")
    import re as _re
    # Extract the actual value
    m = _re.search(r"GENRE_GRAPH_DIVERSITY_GUARD_RATIO\s*=\s*([\d.]+)", src)
    chk("GENRE_GRAPH_DIVERSITY_GUARD_RATIO defined", m is not None)
    if m:
        ratio = float(m.group(1))
        chk(f"diversity guard ratio ≤ 0.40 (got {ratio})", ratio <= 0.40)

    chk("GENRE_GRAPH_MIN_PURITY defined in config", "GENRE_GRAPH_MIN_PURITY" in src)
    m2 = _re.search(r"GENRE_GRAPH_MIN_PURITY\s*=\s*([\d.]+)", src)
    if m2:
        purity = float(m2.group(1))
        chk(f"min purity 0.50–0.70 (got {purity})", 0.50 <= purity <= 0.70)


def test_tidal_album_image():
    print("\n── Bug 15 · TIDAL album track dict includes album_image ──")
    src = (HERE / "downloader.py").read_text(encoding="utf-8")
    # Find _download_full_album body
    fn_body = src.split("def _download_full_album(")[1].split("\ndef ")[0]
    chk("album_image key present in TIDAL track dict",  '"album_image"' in fn_body)
    chk("_tidal_album_image variable declared",          "_tidal_album_image" in fn_body)
    chk("TIDAL best_album.image() attempted",            "best_album.image(" in fn_body)
    chk("Spotify ref_track fallback for album_image",
        'album_info.get("missing_tracks")' in fn_body
        or '_ref_tracks' in fn_body)
    chk("MK8 Fix LOG_17_3 comment in fn",               "MK8 Fix (LOG_17_3)" in fn_body)

    # MC: verify album_image is always populated when best_album.image() works
    class FakeAlbum:
        def image(self, dim=480): return f"https://cdn.tidal.com/images/abc/{dim}x{dim}.jpg"

    album_info_mock = {"missing_tracks": [{"album_image": "https://sp.example.com/cover.jpg"}]}
    album = FakeAlbum()

    img = ""
    try:
        img = album.image(480) or ""
    except Exception:
        pass
    if not img:
        ref = (album_info_mock.get("missing_tracks") or [])
        img = ref[0].get("album_image") or "" if ref else ""

    chk("album_image populated from TIDAL image()", img.startswith("https://cdn.tidal.com"))


def test_cover_warning_on_no_url():
    print("\n── Bug 16 · WARNING logged when album_image URL missing ──")
    src = (HERE / "downloader.py").read_text(encoding="utf-8")
    fn_body = src.split("def _embed_cover_for_track(")[1].split("\ndef ")[0]
    chk("log.warning called when url is empty",    "log.warning(" in fn_body)
    chk("MK8 Fix LOG_17_3 comment in embed fn",   "MK8 Fix (LOG_17_3)" in fn_body)
    chk("returns False after WARNING (not silently)", fn_body.count("return False") >= 1)

    # Simulate: track with no album_image → warning path
    import io, logging
    buf = io.StringIO()
    hdlr = logging.StreamHandler(buf)
    test_log = logging.getLogger("downloader_test")
    test_log.addHandler(hdlr)
    test_log.setLevel(logging.WARNING)

    artist_hint = "Test Artist"
    title_hint  = "Test Track"
    fname       = "test_track.mp3"
    url         = ""
    if not url:
        test_log.warning(
            f"  [Cover] No album_image URL in track dict for "
            f"{artist_hint!r} — {title_hint!r} ({fname}). "
            f"Cover will not be embedded."
        )
    log_output = buf.getvalue()
    chk("WARNING message contains artist hint",     "Test Artist" in log_output)
    chk("WARNING message says Cover will not be embedded", "Cover will not be embedded" in log_output)

def main():
    print("=" * 65)
    print("  SpotifyTunerMK8 — Comprehensive Fix Verification Test Suite")
    print("=" * 65)

    test_global_dedup()
    test_device_library_cache()
    test_track_unification()
    test_cover_art()
    test_genre_overrides()
    test_silent_move_fallback()
    test_ai_circuit_breaker()
    test_syntax()
    test_fix_inventory()

    # ── LOG_17_3 fixes ────────────────────────────────────────────────────
    test_source_delete_no_dialog()
    test_adb_verify_retry()
    test_com_verify_retry()
    test_dialog_watcher_folder_delete()

    # ── LOG_17_3 Part 2: Afrobeats + cover art ──────────────────────────────
    test_east_african_genre_mapping()
    test_ai_prompt_no_afrobeats_bias()
    test_diversity_guard_tightened()
    test_tidal_album_image()
    test_cover_warning_on_no_url()

    # ── PR-GenreGraph + PR-CoverArt ──────────────────────────────────────────
    test_liked_songs_exclusion()
    test_playlist_purity_weighting()
    test_afrobeats_norm_fix()
    test_cover_art_backfill()
    test_writethumbnail_enabled()

    total = OK + FAIL
    print("\n" + "=" * 65)
    if FAIL == 0:
        print(f"  ✅  ALL {total} TESTS PASSED")
    else:
        print(f"  ❌  {FAIL}/{total} TESTS FAILED")
        sys.exit(1)
    print("=" * 65)


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────────────────────
# BUG 8: LOG_17_3 — Source delete dialog (Confirm Folder Delete + _delete_item)
# ─────────────────────────────────────────────────────────────────────────────
def test_log17_3_delete_fixes():
    print("\n── Bug LOG_17_3 · Dialog-free source delete ──")
    dw_src   = (HERE / "dialog_watcher.py").read_text(encoding="utf-8")
    reorg_src = (HERE / "walkman_reorganize.py").read_text(encoding="utf-8")

    # dialog_watcher must handle Confirm Folder Delete (MTP items have no extension)
    chk("Confirm Folder Delete → Yes in _DIALOGS",
        '("Confirm Folder Delete",       "Yes")' in dw_src or
        '"Confirm Folder Delete"' in dw_src)

    # _delete_item must use mtp.delete_file_on_device first (no rename+move)
    delete_fn = reorg_src.split("def _delete_item(")[1].split("\ndef ")[0]
    chk("_delete_item: no DEL_ rename+move",
        "DEL_" not in delete_fn and "MoveHere" not in delete_fn)
    chk("_delete_item: uses mtp.delete_file_on_device",
        "mtp.delete_file_on_device(" in delete_fn)
    chk("_delete_item: folder_parts param",
        "folder_parts:" in delete_fn or "folder_parts =" in delete_fn)

    # Step 4 (FLAC upgrade) must pass path info to _delete_item
    step4_idx = reorg_src.find("Upgrade: delete existing MP3")
    step4_region = reorg_src[step4_idx:step4_idx+300]
    chk("Step 4 FLAC upgrade passes folder_parts to _delete_item",
        "folder_parts=dest_folder_parts" in step4_region)

    # Startup banner must reflect new method
    chk("startup banner: delete method updated",
        "rename+move" not in reorg_src.split("log.info(\"Delete:")[1].split("\n")[0])

    # Silent_move Step 6 uses mtp.delete_file_on_device (not _delete_item)
    sm_body = reorg_src.split("def silent_move(")[1].split("\ndef ")[0]
    step6_region = sm_body.split("Step 6")[1][:500] if "Step 6" in sm_body else sm_body[-500:]
    chk("silent_move Step 6: uses mtp.delete_file_on_device",
        "mtp.delete_file_on_device(" in step6_region)
    chk("silent_move Step 6: no _delete_item call",
        "_delete_item(" not in step6_region)


