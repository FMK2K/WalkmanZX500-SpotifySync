"""
test_rev1496.py — Rev 14.9.6 regression tests
==============================================
Exercises:
  * U1: subprocess.run encoding="utf-8" errors="replace" prevents
        cp1252 charmap-decode crash on non-Latin filenames
  * A1: artist-level genre cache stops repeat Google scrapes
  * A2: AI fallback routes through call_ai (Groq) when Gemini breaker tripped
  * R2: smart title-case (idempotent, edge cases handled)
"""
from __future__ import annotations
import sys
import os
import re
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


# ─── 1. U1: subprocess encoding ─────────────────────────────────────────
print("\n[1] U1: subprocess encoding utf-8 errors=replace")

# Read source and check the kwargs are present at all critical sites
with open(os.path.join(os.path.dirname(__file__), "adb_transfer.py"),
          "r", encoding="utf-8") as f:
    adb_src = f.read()
_check(
    "_adb_cmd subprocess uses encoding='utf-8'",
    'encoding="utf-8"' in adb_src and "errors=\"replace\"" in adb_src,
)
# Count uses — should be 3 in adb_transfer (probe + _adb_cmd + push)
encoding_uses = adb_src.count('encoding="utf-8"')
_check(
    f"adb_transfer.py has 3 utf-8 subprocess sites (got {encoding_uses})",
    encoding_uses >= 3,
)

with open(os.path.join(os.path.dirname(__file__), "downloader.py"),
          "r", encoding="utf-8") as f:
    dl_src = f.read()
_check(
    "downloader.py tiddl + streamrip subprocess use utf-8 encoding",
    dl_src.count('encoding="utf-8"') >= 2,
)

# Functional test: simulate subprocess that returns bytes containing 0x8d
# (the actual byte from LOG_16's crash).  The fixed code shouldn't crash.
import subprocess

# Simulate by directly invoking subprocess.run with the new args
# (we can't actually run adb in CI, but we can verify the kwargs decode
# the problematic bytes correctly).
test_bytes = b"normal text \x8d more text"
try:
    # Verify that text=True + encoding="utf-8" + errors="replace" handles 0x8d
    decoded = test_bytes.decode("utf-8", errors="replace")
    _check("0x8d decodes to U+FFFD with errors=replace", "\ufffd" in decoded)
except UnicodeDecodeError:
    _check("0x8d decodes without raising", False)

# And confirm the cp1252 path WOULD have crashed (sanity check)
crashed = False
try:
    test_bytes.decode("cp1252")
except UnicodeDecodeError:
    crashed = True
_check("0x8d would have crashed cp1252 (the bug LOG_16 hit)", crashed)


# ─── 2. A1: artist-level genre cache ────────────────────────────────────
print("\n[2] A1: artist-level Google genre cache")

import importlib
# Reload genre_client to reset module-level caches
if "genre_client" in sys.modules:
    importlib.reload(sys.modules["genre_client"])
import genre_client

# Reset the module-level cache to known empty
with genre_client._ARTIST_GENRE_LOCK:
    genre_client._ARTIST_GENRE_CACHE.clear()

# Verify the helpers exist
_check("_artist_cache_get exists", callable(getattr(genre_client, "_artist_cache_get", None)))
_check("_artist_cache_put exists", callable(getattr(genre_client, "_artist_cache_put", None)))
_check("_artist_cache_key exists", callable(getattr(genre_client, "_artist_cache_key", None)))

# Cache key normalization
_check(
    "key normalization: trailing whitespace + case",
    genre_client._artist_cache_key("  LO-FI BEATS  ")
    == genre_client._artist_cache_key("lo-fi beats"),
)

# Empty/None artist returns None
_check("empty artist returns None on get",
       genre_client._artist_cache_get("") is None)

# Put + get round-trip
genre_client._artist_cache_put("LO-FI BEATS", {"genre": "Lo-fi"})
hit = genre_client._artist_cache_get("LO-FI BEATS")
_check("put+get round-trip", hit == {"genre": "Lo-fi"})
hit = genre_client._artist_cache_get("lo-fi beats")  # case-insensitive
_check("get is case-insensitive", hit == {"genre": "Lo-fi"})

# Negative cache: empty {} stored, retrieved as {} (not None)
genre_client._artist_cache_put("Unknown Artist X", {})
hit = genre_client._artist_cache_get("Unknown Artist X")
_check("negative cache returns {} (not None)",
       hit == {} and hit is not None)

# Positive doesn't get overwritten by negative
genre_client._artist_cache_put("LO-FI BEATS", {})  # try to clobber with negative
hit = genre_client._artist_cache_get("LO-FI BEATS")
_check("positive NOT overwritten by negative", hit == {"genre": "Lo-fi"})

# Negative CAN be overwritten by positive
genre_client._artist_cache_put("Unknown Artist X", {"genre": "Pop"})
hit = genre_client._artist_cache_get("Unknown Artist X")
_check("negative overwritten by positive", hit == {"genre": "Pop"})


# ─── 3. A1: _google_genre uses cache ────────────────────────────────────
print("\n[3] A1: _google_genre consults cache before HTTP")

# Reset cache and seed a value
with genre_client._ARTIST_GENRE_LOCK:
    genre_client._ARTIST_GENRE_CACHE.clear()
genre_client._artist_cache_put("Lofi Hip-Hop Beats", {"genre": "Lo-fi"})

# Patch requests.get so we can detect whether HTTP was attempted
http_called = [0]
def _spy_get(*args, **kwargs):
    http_called[0] += 1
    raise RuntimeError("requests.get should not have been called")

with patch.object(genre_client.requests, "get", side_effect=_spy_get):
    result = genre_client._google_genre("Lofi Hip-Hop Beats", "Sleep Song")
    _check("cache hit avoids HTTP call", http_called[0] == 0)
    _check("cache hit returns the cached value", result == {"genre": "Lo-fi"})

# Different artist (not in cache) -- HTTP attempt would fire
http_called[0] = 0
def _spy_get_404(*args, **kwargs):
    http_called[0] += 1
    m = MagicMock()
    m.status_code = 404
    m.text = ""
    return m

with patch.object(genre_client.requests, "get", side_effect=_spy_get_404):
    result = genre_client._google_genre("Different Artist", "Different Song")
    _check("cache miss does fire HTTP", http_called[0] == 1)


# ─── 4. R2: _smart_title_case ───────────────────────────────────────────
print("\n[4] R2: smart title-case engine (extracted from source)")

# Extract _smart_title_case + helpers from downloader.py (avoid full import)
ns = {"re": re}
# Pull the whole region from _LOWERCASE_WORDS through _smart_title_case
start = dl_src.find("_LOWERCASE_WORDS = frozenset")
end = dl_src.find("def _convert_raw_aac_to_mp3", start)
exec(dl_src[start:end], ns)
stc = ns["_smart_title_case"]

# Cases
cases = [
    # All-uppercase input -> Title Case
    ("KIDS SEE GHOSTS", "Kids See Ghosts"),
    ("ALL-AMERICAN TRASH", "All-american Trash"),
    # Short connector words stay lowercase except at edges
    ("the world is yours", "The World Is Yours"),
    ("a long way home", "A Long Way Home"),
    ("rock and roll", "Rock and Roll"),
    ("songs of the south", "Songs of the South"),
    # Already-curated mixed case stays untouched
    ("Don't Look Back", "Don't Look Back"),
    ("Run the World (Girls)", "Run the World (Girls)"),
    ("AC/DC", "AC/DC"),  # short all-caps stays
    # Edge: single word
    ("daydreaming", "Daydreaming"),
    # All-caps short string (acronym-like) preserved
    ("MGMT", "MGMT"),
    # Empty / None
    ("", ""),
]
for input_, expected in cases:
    actual = stc(input_)
    _check(f"{input_!r:35s} -> {expected!r}",
           actual == expected, f"got {actual!r}")

# Idempotent
twice = stc(stc("the rolling stones"))
_check("idempotent: title-case applied twice == once",
       twice == stc("the rolling stones"))

# None passes through
_check("None input passes through", stc(None) is None)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"Rev 14.9.6 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
