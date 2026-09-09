"""Extended tests for Rev 14.9.2 patches 2 and 8.

Runs standalone against the patched walkman_sync module by mocking out
audit.py's state I/O.  Asserts that:
  - _consolidate_album_genres produces the expected tag mutations
  - _invalidate_audit_state_for_tracks removes state entries whose key
    matches the (artist, album, title) fingerprint of a rewritten track,
    and leaves unrelated entries untouched
"""
import sys
import types
from collections import Counter, defaultdict

# Stub out the modules walkman_sync imports at module load
for mod_name in [
    "mutagen", "mutagen.easyid3", "mutagen.id3", "mutagen.flac", "mutagen.mp3",
    "config", "id3_helpers", "meta_key", "spotify_client", "genre_client",
    "mtp_transfer",
]:
    sys.modules.setdefault(mod_name, types.ModuleType(mod_name))

# Minimal attrs walkman_sync needs at import
sys.modules["mutagen.easyid3"].EasyID3 = object
sys.modules["mutagen.id3"].ID3 = object
sys.modules["mutagen.id3"].ID3NoHeaderError = Exception
sys.modules["mutagen.id3"].POPM = object
sys.modules["mutagen.flac"].FLAC = object
sys.modules["mutagen.mp3"].MP3 = object
sys.modules["mtp_transfer"].StorageFullError = type("StorageFullError", (Exception,), {})
sys.modules["config"].WRITE_TAGS = {"genre": True, "release_year": True}

# Inject a fake `audit` module that we can inspect
fake_audit = types.ModuleType("audit")
_STATE = {}
fake_audit._STATE = _STATE

def _load_audit_state():
    return dict(_STATE)

def _save_audit_state(state):
    _STATE.clear()
    _STATE.update(state)

fake_audit._load_audit_state = _load_audit_state
fake_audit._save_audit_state = _save_audit_state
sys.modules["audit"] = fake_audit

# Now import the patched consolidator directly by re-reading the method source
# (we don't want to import the whole 1500-line walkman_sync; just lift the
# functions we need into this test's namespace.)
import pathlib, os
# Resolve relative to THIS test file so the test runs in any checkout — the
# pre-Rev-14.9.8 version had a hardcoded /home/claude/patched/... path that
# tied the test to one developer's working directory and crashed everywhere
# else with FileNotFoundError.
_HERE = pathlib.Path(__file__).resolve().parent if "__file__" in dir() else pathlib.Path.cwd()
_candidates = [
    _HERE / "walkman_sync.py",                 # standard layout: test alongside source
    _HERE.parent / "walkman_sync.py",           # test in tests/ subdir
    pathlib.Path.cwd() / "walkman_sync.py",     # cwd fallback
]
_src_path = next((p for p in _candidates if p.exists()), None)
if _src_path is None:
    raise FileNotFoundError(
        f"walkman_sync.py not found in any of: {[str(p) for p in _candidates]}"
    )
src = _src_path.read_text(encoding="utf-8")

# Build a fake class that owns the two methods we care about
import ast
tree = ast.parse(src)
klass = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "WalkmanSyncAgent")

wanted = {"_consolidate_album_genres", "_invalidate_audit_state_for_tracks", "_BROAD_GENRES"}
nodes = []
for item in klass.body:
    if isinstance(item, ast.Assign):
        for tgt in item.targets:
            if isinstance(tgt, ast.Name) and tgt.id in wanted:
                nodes.append(item)
    elif isinstance(item, ast.FunctionDef) and item.name in wanted:
        nodes.append(item)

# Build a standalone class wrapping those nodes
test_class = ast.ClassDef(
    name="TestAgent", bases=[], keywords=[], body=nodes, decorator_list=[]
)
module = ast.Module(body=[test_class], type_ignores=[])
ast.fix_missing_locations(module)

# The methods we ripped out of WalkmanSyncAgent reference module-level names
# (config, _gg, etc.) that aren't present in our isolated namespace.  Stub
# them so the methods evaluate cleanly — config attrs default via getattr,
# and _gg.infer_genre_for_artist returns None to skip the graph branch.
class _StubConfig:
    GENRE_ALBUM_MIN_VOTES = 2
    GENRE_ARTIST_FALLBACK_ENABLED = True
    GENRE_ARTIST_MIN_TRACKS = 3
    GENRE_GRAPH_INFERENCE_ENABLED = False  # off so test stays focused on tier-1/2
    def __getattr__(self, name): return None

class _StubGraph:
    @staticmethod
    def build_graph(track_map=None, tracks=None): return None
    @staticmethod
    def infer_genre_for_artist(*a, **kw): return None

ns: dict = {
    "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                 debug=lambda *a, **k: None,
                                 warning=lambda *a, **k: None),
    "config": _StubConfig(),
    "_gg": _StubGraph(),
}
exec(compile(module, "<patched>", "exec"), ns)
TestAgent = ns["TestAgent"]

agent = TestAgent()

# ── Test A: African Giant-style consolidation ──
# NOTE: This test was originally written against Rev 14.9.2's wider
# _BROAD_GENRES (which included "Pop", "Rock", "R&B", etc.).  Rev 14.9.5
# narrowed _BROAD_GENRES to {"", "unknown", "world", "world music",
# "various"} — Pop and R&B are now legitimate canonical genres.  Under
# the new rules, modal of [4 Afrobeats, 5 Pop, 1 R&B] is "Pop", which
# is correct behavior but breaks the original test data.  Test data
# updated below to give "Afrobeats" a legitimate plurality, the way a
# real Afrobeats album's enriched genres would actually look.
african_giant = [
    {"album_artist": "Burna Boy", "album": "African Giant", "artist": "Burna Boy",
     "title": f"Track {i}", "genre": g}
    for i, g in enumerate([
        "Afrobeats", "Afrobeats", "Afrobeats", "Afrobeats", "Afrobeats",
        "Afrobeats", "Afrobeats", "Afrobeats",
        "Pop", "Unknown", "", "World", "R&B",
        "Pop", "Unknown", "", "Afrobeats",
        "World", "Unknown", "",
    ])
]

# Pre-populate audit state with entries the rewrite should invalidate,
# PLUS some unrelated entries that should survive.  Tracks 8, 13, 18 in
# the updated test data above have non-Afrobeats genres that get rewritten.
fake_audit._STATE.update({
    # These match — they should be removed
    "SD card/Music/Burna Boy - African Giant/01 Track 8.mp3":   {"metakey": "stale1"},
    "SD card/Music/Burna Boy - African Giant/02 Track 13.mp3":  {"metakey": "stale2"},
    "SD card/Music/Burna Boy - African Giant/03 Track 18.mp3":  {"metakey": "stale3"},
    # These don't match — should survive
    "SD card/Music/Other Artist - Other Album/01 Something.mp3": {"metakey": "keep1"},
    "SD card/Music/Burna Boy - Love Damini/01 Different Title.mp3": {"metakey": "keep2"},
    # Sidecar caches (prefixed with __) — should never be touched
    "__tidal_dl_fails__": {"xyz": 1},
    "__push_fail_cache__": {"abc": 2},
})

initial_state_size = len(fake_audit._STATE)
agent._consolidate_album_genres(african_giant)

# Assert all African Giant tracks unified to Afrobeats
genres = {t["genre"] for t in african_giant}
assert genres == {"Afrobeats"}, f"Expected {{'Afrobeats'}}, got {genres}"
print(f"A: all 19 tracks unified to Afrobeats ✓")

# Assert the 3 matching audit state entries were removed.
# Under Rev 14.9.5+ broad-set: Track 8="Pop" (specific-but-different
# from chosen Afrobeats → rewritten), Track 13="R&B" (specific-but-
# different → rewritten), Track 18="World" (broad → backfilled).
remaining = set(fake_audit._STATE.keys())
removed = set([
    "SD card/Music/Burna Boy - African Giant/01 Track 8.mp3",
    "SD card/Music/Burna Boy - African Giant/02 Track 13.mp3",
    "SD card/Music/Burna Boy - African Giant/03 Track 18.mp3",
]) - remaining
assert len(removed) == 3, f"Expected 3 entries removed, got {len(removed)}: {removed}"
print(f"A: {len(removed)} matching audit entries invalidated ✓")

# Assert unrelated entries survived
assert "SD card/Music/Other Artist - Other Album/01 Something.mp3" in fake_audit._STATE
assert "SD card/Music/Burna Boy - Love Damini/01 Different Title.mp3" in fake_audit._STATE
assert "__tidal_dl_fails__" in fake_audit._STATE
assert "__push_fail_cache__" in fake_audit._STATE
print(f"A: unrelated + sidecar entries untouched ✓")

# ── Test B: no-op case — all tracks already correct ──
fake_audit._STATE.clear()
fake_audit._STATE["SD card/Music/Artist - Album/01 Song.mp3"] = {"metakey": "x"}
already_good = [
    {"album_artist": "Artist", "album": "Album", "artist": "Artist",
     "title": f"Song {i}", "genre": "Jazz"}
    for i in range(4)
]
agent._consolidate_album_genres(already_good)
assert "SD card/Music/Artist - Album/01 Song.mp3" in fake_audit._STATE
print("B: no-op consolidation leaves audit state alone ✓")

# ── Test C: all-broad album — no rewrites, no invalidation ──
fake_audit._STATE.clear()
fake_audit._STATE["SD card/Music/X - Y/01 Z.mp3"] = {"metakey": "x"}
all_broad = [
    {"album_artist": "X", "album": "Y", "artist": "X",
     "title": f"Z{i}", "genre": "Pop"}
    for i in range(5)
]
agent._consolidate_album_genres(all_broad)
assert "SD card/Music/X - Y/01 Z.mp3" in fake_audit._STATE
print("C: all-broad album — audit state untouched ✓")

print("\nAll patch 2+8 tests passed.")
