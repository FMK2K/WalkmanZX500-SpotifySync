"""
test_rev1497.py — Rev 14.9.7 regression tests
==============================================
Exercises the four LOG_16_1-driven fixes:

  * O1: audit._fetch_liked_track_ids honors OFFLINE_MODE
  * P1: tag-write retries with exponential backoff on Errno 13 / WinError 32
  * P2: _safe balances unmatched parentheses after truncation
  * P3: ADB push self-heals "daemon not running" with kill-server + start-server
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


# ─── 1. O1: audit honors OFFLINE_MODE ───────────────────────────────────
print("\n[1] O1: audit._fetch_liked_track_ids — OFFLINE_MODE source-level check")

with open(os.path.join(os.path.dirname(__file__), "audit.py"),
          "r", encoding="utf-8") as f:
    audit_src = f.read()

_check(
    "_fetch_liked_track_ids has OFFLINE_MODE branch",
    'getattr(_cfg, "OFFLINE_MODE"' in audit_src
    and "import spotify_local_data" in audit_src,
)
_check(
    "OFFLINE_MODE path uses get_liked_uris (not API)",
    "_sld.get_liked_uris()" in audit_src,
)
_check(
    "OFFLINE_MODE path returns BEFORE the API fallback",
    audit_src.find("return\n        except Exception as e:\n            log.debug")
    < audit_src.find("sp.current_user_saved_tracks")
    if "_sld.get_liked_uris()" in audit_src else False,
)
_check(
    "log line distinguishes OFFLINE_MODE source",
    "OFFLINE_MODE — sourced" in audit_src,
)


# ─── 2. P1: tag-write retry on PermissionError ──────────────────────────
print("\n[2] P1: tag-write retry on Errno 13 / WinError 32")

with open(os.path.join(os.path.dirname(__file__), "downloader.py"),
          "r", encoding="utf-8") as f:
    dl_src = f.read()

_check(
    "tag-write catches (PermissionError, OSError) as a separate branch",
    "except (PermissionError, OSError) as e:" in dl_src,
)
_check(
    "retry uses exponential backoff (50ms, 200ms, 800ms)",
    "(50, 200, 800)" in dl_src,
)
_check(
    "marks failure after retries (with optional copy-replace fallback)",
    "retries+copy" in dl_src or "after 3 retries" in dl_src,
)
_check(
    "preserves the original generic Exception handler",
    dl_src.count("except Exception as e:\n                log.warning(f\"  [Download] Tag write failed") >= 1,
)


# ─── 3. P2: _safe balances unmatched parentheses ────────────────────────
print("\n[3] P2: _safe balances unmatched parens after truncation")

# Extract _safe by reading source and execing — avoids importing downloader
# (which pulls in mtp_transfer that exits on Linux)
import unicodedata
ns = {"re": re, "unicodedata": unicodedata}
start = dl_src.find("def _safe(name: str, max_len: int = 60)")
end = dl_src.find("def _ascii_safe", start)
exec(dl_src[start:end], ns)
_safe = ns["_safe"]

# The actual LOG_16_1 truncation case
input_str = "Dali Nguwe (feat. Nkosazana Daughter, Basetsana and Obeey Am)"
# Length = 61 chars; truncated at 60 was "Dali Nguwe (feat. Nkosazana Daughter, Basetsana and Obeey Am" (no closing paren)
_check(
    f"the LOG_16_1 case strips orphan-open paren",
    _safe(input_str, 60) == "Dali Nguwe",
    f"got {_safe(input_str, 60)!r}",
)

# Other paren cases
cases = [
    ("Run the World (Girls)", 60, "Run the World (Girls)"),  # fits, no truncation
    ("Some Song (Live at Madison Square Garden, NYC)", 35, "Some Song"),  # truncates to "Some Song (Live at Madison Square G", strip parens
    ("Track [Bonus]", 60, "Track [Bonus]"),  # square-bracket fit
    ("Track [Bonus Track Edition]", 15, "Track"),  # square-bracket truncated
    ("Normal Title", 60, "Normal Title"),  # no parens
    ("ABC", 60, "ABC"),  # very short
    ("Track (feat. Drake & Future)", 60, "Track (feat. Drake and Future)"),  # & -> and
]
for input_, max_len, expected in cases:
    actual = _safe(input_, max_len)
    _check(
        f"_safe({input_!r}, {max_len}) -> {expected!r}",
        actual == expected,
        f"got {actual!r}",
    )

# Idempotent — applying _safe to its own output yields the same string
once = _safe("Title (feat. Some Long Featured Artist Name Here)", 30)
twice = _safe(once, 30)
_check("_safe idempotent on already-truncated input",
       once == twice, f"once={once!r} twice={twice!r}")

# Edge case: brackets not orphaned despite truncation
_check(
    "balanced parens within truncation are preserved",
    _safe("Album (Deluxe) Edition Track 1", 60)
    == "Album (Deluxe) Edition Track 1",
)


# ─── 4. P3: ADB daemon retry ────────────────────────────────────────────
print("\n[4] P3: ADB daemon-not-running retry")

with open(os.path.join(os.path.dirname(__file__), "adb_transfer.py"),
          "r", encoding="utf-8") as f:
    adb_src = f.read()

_check(
    "Recovery path detects 'daemon not running'",
    '"daemon not running" in stderr_text' in adb_src,
)
_check(
    "Recovery path detects 'cannot connect to daemon'",
    '"cannot connect to daemon" in stderr_text' in adb_src,
)
_check(
    "Recovery uses kill-server + start-server sequence",
    '"kill-server"' in adb_src and '"start-server"' in adb_src,
)
_check(
    "Recovery has 1.0s sleep between kill and start",
    "time.sleep(1.0)" in adb_src,
)
_n_utf8 = adb_src.count('encoding="utf-8"')
_check(
    "Recovery uses utf-8 encoding (Rev 14.9.6 U1 still applied)",
    _n_utf8 >= 5,  # was 5, now 7 with the 2 new ones
    f"got {_n_utf8} sites",
)
_check(
    "Recovery only fires on first attempt (not infinite loop)",
    "if attempts == 1 and (\n                \"daemon not running\"" in adb_src,
)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"Rev 14.9.7 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
