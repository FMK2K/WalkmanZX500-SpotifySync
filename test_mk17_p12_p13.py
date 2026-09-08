"""
test_mk17_p12_p13.py — MK17 Patches 12-13 regression tests
=============================================================
P12 — Adaptive size-and-format-aware timeouts replacing fixed 60/120s.
P13 — Dialog watcher tighter polling + Enter-key fallback so dialogs
      don't slip through to require manual OK clicks (LOG_17_1 case:
      "The requested resource is in use" error dialog).
"""
from __future__ import annotations
import os, sys, pathlib

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


# ─── 1. P12: adaptive_timeout module exists and behaves ─────────────────
print("\n[1] P12: adaptive_timeout module — basic behaviour")

import adaptive_timeout as _at

_check("module imports", _at is not None)
_check("timeout_for callable", callable(_at.timeout_for))
_check("adb_push_timeout callable", callable(_at.adb_push_timeout))
_check("com_delete_timeout callable", callable(_at.com_delete_timeout))
_check("ps_upload_timeout callable", callable(_at.ps_upload_timeout))


# ─── 2. P12: size scaling ───────────────────────────────────────────────
print("\n[2] P12: timeouts scale with file size")

# Small file (1 MB MP3)
small = _at.adb_push_timeout(size_bytes=1 * 1024 * 1024, ext=".mp3")
# Medium file (10 MB MP3)
medium = _at.adb_push_timeout(size_bytes=10 * 1024 * 1024, ext=".mp3")
# Large file (50 MB MP3)
large = _at.adb_push_timeout(size_bytes=50 * 1024 * 1024, ext=".mp3")

_check("small file timeout < medium file timeout",
       small < medium, f"small={small:.1f} medium={medium:.1f}")
_check("medium file timeout < large file timeout",
       medium < large, f"medium={medium:.1f} large={large:.1f}")
_check("small MP3 has reasonable timeout (between 30s and 60s)",
       30 <= small <= 60, f"got {small:.1f}")
_check("large MP3 has reasonable timeout (>= 90s)",
       large >= 90, f"got {large:.1f}")
_check("timeouts have a ceiling (huge file caps out)",
       _at.adb_push_timeout(size_bytes=10 * 1024 * 1024 * 1024) <= 700,
       f"got {_at.adb_push_timeout(size_bytes=10 * 1024 * 1024 * 1024)}")


# ─── 3. P12: format multiplier ──────────────────────────────────────────
print("\n[3] P12: FLAC takes longer than MP3 of same size")

mp3_t = _at.adb_push_timeout(size_bytes=10 * 1024 * 1024, ext=".mp3")
flac_t = _at.adb_push_timeout(size_bytes=10 * 1024 * 1024, ext=".flac")
m4a_t = _at.adb_push_timeout(size_bytes=10 * 1024 * 1024, ext=".m4a")

_check("FLAC timeout > MP3 timeout for same size",
       flac_t > mp3_t, f"FLAC={flac_t:.1f} MP3={mp3_t:.1f}")
_check("M4A roughly equal to MP3 (within 20%)",
       0.8 <= (m4a_t / mp3_t) <= 1.3, f"M4A={m4a_t:.1f} MP3={mp3_t:.1f}")


# ─── 4. P12: defaults / unknown size ────────────────────────────────────
print("\n[4] P12: graceful defaults")

_check("None size returns the base timeout",
       _at.adb_push_timeout(size_bytes=None, ext=".mp3") > 0)
_check("None ext doesn't crash",
       _at.adb_push_timeout(size_bytes=1024 * 1024, ext=None) > 0)
_check("Unknown ext uses default multiplier",
       _at.adb_push_timeout(size_bytes=1024 * 1024, ext=".weird") > 0)


# ─── 5. P12: per-operation differs ──────────────────────────────────────
print("\n[5] P12: different operations have different scales")

# At 10MB MP3:
adb_t  = _at.adb_push_timeout(size_bytes=10*1024*1024, ext=".mp3")
com_t  = _at.com_copy_timeout(size_bytes=10*1024*1024, ext=".mp3")
del_t  = _at.com_delete_timeout(size_bytes=10*1024*1024, ext=".mp3")
ver_t  = _at.verify_timeout(size_bytes=10*1024*1024, ext=".mp3")

_check("verify is faster than push (just metadata round-trip)",
       ver_t < adb_t, f"verify={ver_t:.1f} push={adb_t:.1f}")
_check("delete is bounded (doesn't transfer data)",
       del_t < 100, f"got {del_t:.1f}")
_check("COM copy is slower than ADB push (Shell overhead)",
       com_t > adb_t, f"COM={com_t:.1f} ADB={adb_t:.1f}")


# ─── 6. P12: wired into walkman_reorganize._delete_item ─────────────────
print("\n[6] P12: _delete_item wires adaptive timeout")

WR_PATH = pathlib.Path(__file__).parent / "walkman_reorganize.py"
wr_src = WR_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 12 marker in walkman_reorganize.py",
       "MK17 Patch 12" in wr_src)
_check("_delete_item accepts size_bytes parameter",
       "size_bytes:" in wr_src
       and ("size_bytes: int | None = None" in wr_src
            or "size_bytes=None" in wr_src))
_check("imports adaptive_timeout when timeout_s is None",
       "import adaptive_timeout" in wr_src)
_check("call site passes size_bytes",
       "_src_size = safe_path.stat().st_size" in wr_src
       and "size_bytes=_src_size" in wr_src)


# ─── 7. P12: wired into adb_transfer push ───────────────────────────────
print("\n[7] P12: ADB push uses adaptive timeout")

ADB_PATH = pathlib.Path(__file__).parent / "adb_transfer.py"
adb_src = ADB_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 12 marker in adb_transfer.py",
       "MK17 Patch 12" in adb_src)
_check("ADB push imports adaptive_timeout",
       "import adaptive_timeout" in adb_src)
_check("ADB push uses _push_timeout variable",
       "_push_timeout" in adb_src and "timeout=_push_timeout" in adb_src)
_check("ADB push falls back to ADB_TIMEOUT on import failure",
       "_push_timeout = ADB_TIMEOUT" in adb_src)


# ─── 8. P12: wired into wpd_transfer PowerShell ─────────────────────────
print("\n[8] P12: PowerShell upload uses adaptive timeout")

WPD_PATH = pathlib.Path(__file__).parent / "wpd_transfer.py"
wpd_src = WPD_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 12 marker in wpd_transfer.py",
       "MK17 Patch 12" in wpd_src)
_check("_try_powershell signature accepts timeout_s=None",
       "timeout_s:    int | None = None" in wpd_src
       or "timeout_s: int | None = None" in wpd_src)
_check("PS upload imports adaptive_timeout when None",
       "import adaptive_timeout" in wpd_src
       and "ps_upload_timeout" in wpd_src)


# ─── 9. P13: dialog watcher tightened ───────────────────────────────────
print("\n[9] P13: dialog watcher poll + cooldown tightened")

DW_PATH = pathlib.Path(__file__).parent / "dialog_watcher.py"
dw_src = DW_PATH.read_text(encoding="utf-8")

_check("MK17 Patch 13 marker present",
       "MK17 Patch 13" in dw_src)
# Find the actual numeric values
import re
poll_m = re.search(r"^POLL_INTERVAL_S\s*=\s*([\d.]+)", dw_src, re.M)
cool_m = re.search(r"^_DISMISS_COOLDOWN_S\s*=\s*([\d.]+)", dw_src, re.M)
_check("POLL_INTERVAL_S found", poll_m is not None)
_check("_DISMISS_COOLDOWN_S found", cool_m is not None)
if poll_m:
    poll_val = float(poll_m.group(1))
    _check("poll interval reduced (<=0.15s)",
           poll_val <= 0.15, f"got {poll_val}")
if cool_m:
    cool_val = float(cool_m.group(1))
    _check("dismiss cooldown reduced (<=0.5s)",
           cool_val <= 0.5, f"got {cool_val}")
    _check("cooldown still positive (no double-dismiss within 100ms)",
           cool_val > 0)


# ─── 10. P13: Enter-key fallback wired ──────────────────────────────────
print("\n[10] P13: Enter-key fallback for missed error dialogs")

_check("EnterFallback log marker present",
       "EnterFallback" in dw_src)
_check("Enter fallback only runs for error dialogs",
       "_is_error_dialog(win_title)" in dw_src)
_check("type_keys('{ENTER}') is sent",
       'type_keys("{ENTER}"' in dw_src)
_check("Enter fallback only fires when named-button match missed",
       "if not clicked and _is_error_dialog" in dw_src)


# ─── 11. P12 importable on all platforms (no Windows deps) ──────────────
print("\n[11] P12: adaptive_timeout has no platform-specific deps")

# Re-import in a fresh namespace to make sure
import importlib
mod = importlib.reload(_at)
_check("module reload succeeds", mod is not None)
_check("computes a value with no config available",
       mod.timeout_for("adb_push", size_bytes=1024 * 1024) > 0)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"MK17 Patches 12-13 regression results: {PASS} passed, {FAIL} failed")
if ERRORS:
    print("Failures:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(0 if FAIL == 0 else 1)
