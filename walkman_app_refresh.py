"""
walkman_app_refresh.py  —  Sony Walkman Music app DB refresh (Rev 14)
======================================================================
End-of-session fix for "files are on the device but invisible in the
Walkman Music app" — the Hypothesis A defect.

Why this module exists
----------------------
The Sony ZX500 Walkman Music app maintains its own SQLite database of
tracks, ratings, playlists, Favorites, and bookmarks.  It does NOT query
Android's MediaStore — the two databases are completely independent.

This means every single `MEDIA_SCANNER_SCAN_FILE` broadcast we've been
firing does exactly nothing for the Walkman app's visibility.  Those
broadcasts only help third-party apps that query MediaStore.

Per Sony's own ZX500 Help Guide (§"Music transfer ... doesn't appear"):
    "When you transfer music content, … the content may not appear in
     the library of 'Music player'.  On the 'Music player' screen, tap
     the menu and then 'Update database'."

There is no silent / API-level way to trigger that "Update database" tap
from an ADB intent broadcast — the Walkman app exposes no receiver for
it.  Two practical options exist:

  1. `adb shell pm clear <walkman-pkg>`  (NUCLEAR)
     Wipes the Walkman app's state and forces a full rebuild on next
     launch.  ALSO WIPES Favorites, bookmarks, play counts, and EQ
     presets.  Not safe to default-on.

  2. `adb shell input tap X Y` into the Music app's "Update database"
     menu item.  Non-destructive.  Requires discovering tap coordinates
     once per firmware.  Needs the screen on and unlocked.

Both require the screen on to succeed reliably.  The default mode is
UI-tap ("ui_tap") with pm_clear available behind an explicit flag for
power users who don't care about Favorites.

Public API
----------
    refresh(mode: str | None = None) -> bool
        Run the end-of-session refresh.  *mode* overrides
        config.WALKMAN_DB_REFRESH_MODE.  Returns True on apparent success.

    is_configured() -> bool
        True if a mode is enabled and the Sony package can be located.

Tuning
------
Edit config.WALKMAN_DB_REFRESH_MODE to switch default behaviour:
    "off"       disable entirely
    "ui_tap"    press menu → Update database via `input tap` (default)
    "pm_clear"  wipe app state (destructive)
"""
from __future__ import annotations

import logging
import time

import config
from adb_transfer import _adb_cmd, device_is_connected

log = logging.getLogger(__name__)

# Candidate package names for the Walkman Music app, in priority order.
# Different firmware revisions use different package names — we probe the
# device for whichever one is installed.  The legacy W.Music app used
# com.sony.songpal; the current Music app (Rev 13+) uses
# com.sony.nwz.walkmanplayer or com.sony.nwz.a3.walkmanplayer on most EU
# and US ZX500 firmware.
#
# 2026-07-18 (Task 10 investigation): confirmed live via `pm list packages` +
# `dumpsys package` against a real connected device that the ZX507's actual
# player is `com.sony.walkman.highresmediaplayer` (matches the decompiled
# HighResMediaPlayerApp source exactly) — NOT any of the pre-existing
# candidates below, none of which were installed on that device. Added as the
# highest-priority candidate. Its PlayerMediaProvider ContentProvider is
# confirmed NOT exported (SecurityException from ADB shell), so it cannot be
# used for a direct ContentProvider read/write path — but ui_tap/pm_clear
# still need the correct package name to target the right app at all.
_WALKMAN_PKG_CANDIDATES = [
    "com.sony.walkman.highresmediaplayer",
    "com.sony.nwz.walkmanplayer",
    "com.sony.nwz.a3.walkmanplayer",
    "com.sony.songpal.mdr.vim",
    "com.sony.songpal",
]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_resolved_pkg: str | None = None


def _detect_walkman_pkg() -> str | None:
    """
    Probe the device via `adb shell pm list packages` for whichever
    candidate is actually installed.  Cached after first call.
    """
    global _resolved_pkg
    if _resolved_pkg is not None:
        return _resolved_pkg

    # Respect an explicit override if the user pinned one in config.
    override = getattr(config, "SONY_WALKMAN_PKG", "") or ""
    if override:
        _resolved_pkg = override
        return _resolved_pkg

    try:
        out = _adb_cmd(["shell", "pm", "list", "packages"], check=False, timeout=10)
    except Exception as e:
        log.debug(f"  [WalkmanRefresh] pm list packages failed: {e}")
        return None

    # 2026-07-18 bugfix (Task 10 investigation): this used to check
    # `f"package:{cand}" in out`, a SUBSTRING containment test — which
    # incorrectly matched candidate "com.sony.songpal" against the installed
    # (and unrelated) "com.sony.songpal.mdr" Bluetooth-headphone-companion
    # app on a real device, silently resolving to the wrong app entirely.
    # `pm list packages` emits one exact "package:<name>" token per line;
    # compare each line's package name for exact equality instead.
    installed = {
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("package:")
    }
    for cand in _WALKMAN_PKG_CANDIDATES:
        if cand in installed:
            _resolved_pkg = cand
            log.info(f"  [WalkmanRefresh] Detected Walkman app: {cand}")
            return cand

    log.warning(
        "  [WalkmanRefresh] No known Walkman app package found on device. "
        "End-of-session refresh will be skipped. "
        "Set config.SONY_WALKMAN_PKG explicitly to override."
    )
    return None


def _ensure_screen_on() -> None:
    """
    Wake the device screen if asleep.  KEYCODE_WAKEUP (224) is a no-op if
    already awake, so this is safe to always call.
    """
    try:
        _adb_cmd(["shell", "input", "keyevent", "224"], check=False, timeout=5)
        time.sleep(0.3)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Mode 1: pm clear  (destructive)
# ---------------------------------------------------------------------------

def _refresh_via_pm_clear() -> bool:
    """
    Force the Walkman app to rebuild its database by wiping its state.
    DESTRUCTIVE: loses Favorites, bookmarks, play counts, EQ presets.
    """
    pkg = _detect_walkman_pkg()
    if not pkg:
        return False

    log.warning(
        f"  [WalkmanRefresh] Running destructive pm clear on {pkg} — "
        "Favorites and play counts will be lost."
    )
    try:
        out = _adb_cmd(["shell", "pm", "clear", pkg], check=False, timeout=30)
        if "Success" in out:
            log.info(f"  [WalkmanRefresh] pm clear succeeded on {pkg}")
            return True
        log.warning(f"  [WalkmanRefresh] pm clear returned: {out.strip()[:200]}")
        return False
    except Exception as e:
        log.warning(f"  [WalkmanRefresh] pm clear error: {e}")
        return False


# ---------------------------------------------------------------------------
# Mode 2: UI tap automation  (non-destructive, default)
# ---------------------------------------------------------------------------

def _refresh_via_ui_tap() -> bool:
    """
    Open the Walkman Music app and tap through menu → Update database.

    Coordinates for the ZX500 3.6" 1280x720 display are pinned in
    config.WALKMAN_UI_TAP_COORDS as a dict of named points.  Defaults
    are provided for the stock firmware; adjust once per firmware if
    Sony changes the menu layout.
    """
    pkg = _detect_walkman_pkg()
    if not pkg:
        return False

    coords = getattr(config, "WALKMAN_UI_TAP_COORDS", None) or {
        # Default ZX500 stock firmware layout.  If these are wrong for
        # your firmware, run once with the screen visible, take note of
        # the menu button's pixel position, and override in config.py.
        "menu_button":      (690, 60),      # top-right overflow menu
        "update_database":  (540, 420),     # "Update database" menu item
        "confirm_ok":       (540, 610),     # confirmation dialog OK
    }

    _ensure_screen_on()

    # Launch the Music app via monkey — cheap and reliable across firmware.
    try:
        _adb_cmd(
            ["shell", "monkey", "-p", pkg, "-c",
             "android.intent.category.LAUNCHER", "1"],
            check=False, timeout=10,
        )
    except Exception as e:
        log.debug(f"  [WalkmanRefresh] monkey launch failed: {e}")
    time.sleep(2.0)  # let the app settle

    def _tap(x: int, y: int) -> None:
        _adb_cmd(["shell", "input", "tap", str(x), str(y)], check=False, timeout=5)

    try:
        # 1. Open the menu
        _tap(*coords["menu_button"])
        time.sleep(0.6)
        # 2. Update database
        _tap(*coords["update_database"])
        time.sleep(0.6)
        # 3. Confirm the "Update now?" prompt
        _tap(*coords["confirm_ok"])
        time.sleep(0.3)
    except Exception as e:
        log.warning(f"  [WalkmanRefresh] UI tap sequence failed: {e}")
        return False

    log.info(
        f"  [WalkmanRefresh] UI tap sequence sent to {pkg}. "
        "Scan will run in background on device."
    )
    # We cannot verify completion without a full UIAutomator — we trust
    # the tap sequence was received and return True.  The on-device scan
    # typically finishes within 30-60 s for a ~2 GB library.
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Return True if refresh is enabled and the app package resolves."""
    mode = getattr(config, "WALKMAN_DB_REFRESH_MODE", "ui_tap")
    if mode == "off":
        return False
    return _detect_walkman_pkg() is not None


def verify_db_update(
    timeout: int = 90,
    use_content_provider: bool = False,
) -> dict:
    """
    Verify the Walkman Music app's private DB has processed recent file writes.

    Strategy (two-layer):

    Layer 1 — ContentProvider query (only when use_content_provider=True and the
    canary confirmed the URI is exported by the device firmware).  Queries
    content://com.sony.nwz.walkmanplayer.provider/audio for the total track
    count and polls until it stabilises, up to `timeout` seconds.

    Layer 2 — Android MediaStore count delta.  Polls
    content://media/external/audio/media for total count.  A stabilised count
    (no change across two consecutive 5-second polls) signals the MediaStore
    scan has settled.  This is a proxy for PlayerMediaProvider completion since
    both scans are triggered by the same "Update database" tap.

    Returns dict:
        verified        bool   — True if either layer confirmed completion
        count_before    int    — track count before polling started
        count_after     int    — track count after stabilisation (or timeout)
        elapsed         float  — seconds spent polling
        method          str    — "content_provider" | "mediastore_delta" | "timeout"
    """
    import time as _time

    result = {
        "verified":     False,
        "count_before": 0,
        "count_after":  0,
        "elapsed":      0.0,
        "method":       "timeout",
    }

    try:
        from adb_transfer import _adb_cmd

        def _query_count(uri: str) -> int:
            try:
                out = _adb_cmd(
                    ["shell", "content", "query", "--uri", uri,
                     "--projection", "_id"],
                    timeout=10, check=False,
                ) or ""
                return out.count("Row:")
            except Exception:
                return -1

        uri = (
            getattr(config, "WALKMAN_CONTENT_PROVIDER_URI",
                    "content://com.sony.nwz.walkmanplayer.provider/audio")
            if use_content_provider
            else "content://media/external/audio/media"
        )

        t0      = _time.time()
        prev    = _query_count(uri)
        result["count_before"] = max(prev, 0)
        stable  = 0

        while (_time.time() - t0) < timeout:
            _time.sleep(5)
            curr = _query_count(uri)
            if curr >= 0 and curr == prev:
                stable += 1
                if stable >= 2:          # two consecutive identical counts → settled
                    result.update({
                        "verified":    True,
                        "count_after": curr,
                        "elapsed":     _time.time() - t0,
                        "method":      "content_provider" if use_content_provider
                                       else "mediastore_delta",
                    })
                    return result
            else:
                stable = 0
            prev = curr if curr >= 0 else prev

        # Timed out — report what we have
        result["count_after"] = prev
        result["elapsed"]     = _time.time() - t0
        log.warning(
            f"  [WalkmanRefresh/Verify] Timed out after {result['elapsed']:.0f}s — "
            f"count={prev} (may still be scanning)"
        )
    except Exception as e:
        log.debug(f"  [WalkmanRefresh/Verify] verify_db_update error (non-fatal): {e}")

    return result


def refresh(mode: str | None = None) -> bool:
    """
    Run the configured end-of-session refresh.

    Returns True if the refresh command(s) executed without error.  Does
    NOT verify the library has actually re-scanned — that happens
    asynchronously on device.
    """
    # 2026-07-18: this used to gate on config.USE_ADB — but USE_ADB governs
    # whether ADB is the PRIMARY BULK FILE-TRANSFER backend (default False on
    # this device, because sustained ADB use for hundreds of pushes is
    # unreliable). Refresh has no non-ADB path at all (there is no COM/MTP
    # equivalent of "tap a menu item" or "clear app data"), so it was silently
    # never running on the recommended default config — meaning newly synced
    # content, including playlists, could go invisible in the Walkman's own
    # browser with no operator-visible warning. A single occasional command
    # here is a different risk profile than hundreds of bulk pushes, so this
    # now checks LIVE ADB reachability instead of the static transfer-mode
    # preference.
    if not device_is_connected(""):
        log.warning(
            "  [WalkmanRefresh] Skipped — ADB not reachable right now, so the Walkman's "
            "own database cannot be told to rescan. Newly synced content (including "
            "playlists) may not appear in the Walkman's Music app until you manually "
            "tap Menu -> Update database on the device, or ADB becomes reachable on a "
            "future run."
        )
        return False

    effective = mode or getattr(config, "WALKMAN_DB_REFRESH_MODE", "ui_tap")
    if effective == "off":
        log.debug("  [WalkmanRefresh] Disabled (mode=off)")
        return False

    if effective == "pm_clear":
        return _refresh_via_pm_clear()

    if effective == "ui_tap":
        return _refresh_via_ui_tap()

    log.warning(f"  [WalkmanRefresh] Unknown mode {effective!r}; skipping")
    return False
