"""
preflight.py  —  Session-start environmental cleanup (Rev 14)
==============================================================
Prevents MTP dialogs at the source rather than dismissing them after
they appear.  Runs exactly once, immediately before transfers begin.

Why prevention beats dismissal
-------------------------------
`dialog_watcher.py` auto-clicks "Copy and Replace", "Yes", "Replace the
files in the destination", etc. — and it does that well.  But the most
reliable copy-file dialog is the one that never renders.

Typical dialog-spawning causes, in order of frequency:

  1. Stray `adb.exe` processes from a previous run holding the MTP stack
     open, producing "Device is busy" and "Resource is in use" dialogs.
  2. Windows Explorer windows already open to the Walkman's Music folder.
     Every file written to that folder forces a shell refresh, and any
     refresh mid-transfer can reassert a locked handle and raise a
     dialog on the next file write.
  3. Stale WPD (Windows Portable Devices) service state — occurs after
     suspend/resume cycles or after a previous run was killed uncleanly.
  4. AutoPlay running on Walkman connect.  For the ZX500, AutoPlay opens
     File Explorer, which then holds the MTP handle; see #2.

This module addresses all four, with individual kill-switches in config
so users can opt out of anything that might touch their workflow.

Public API
----------
    run() -> dict
        Execute the configured preflight steps.  Returns a dict of
        step → {"ok": bool, "detail": str} for the transfer layer to log.

    ensure_run_once() -> None
        Idempotent wrapper — runs preflight the first time and no-ops
        on subsequent calls in the same Python process.

Safety notes
------------
- None of the steps require admin privileges in their default form.
- WPD cycling (PREFLIGHT_CYCLE_WPD) does require admin; opt-in only.
- AutoPlay disabling writes to HKCU, not HKLM.  Per-user, revocable.
"""
from __future__ import annotations

import logging
import subprocess
import threading

import config

log = logging.getLogger(__name__)

_ran_lock = threading.Lock()
_already_ran = False


# ---------------------------------------------------------------------------
# Step 1: Kill stray adb.exe processes
# ---------------------------------------------------------------------------

def _kill_stray_adb() -> tuple[bool, str]:
    """
    taskkill any orphaned adb.exe processes.  Safe: the next ADB call
    auto-respawns the server.  Failures are silent (no adb = nothing to
    kill = not an error).
    """
    try:
        # /F = force, /IM = image name.  Returns nonzero if process absent;
        # we treat that as success.
        r = subprocess.run(
            ["taskkill", "/F", "/IM", "adb.exe"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or "") + (r.stderr or "")
        if "not found" in out.lower() or r.returncode == 128:
            return True, "no stray adb.exe running"
        if r.returncode == 0:
            return True, "killed stray adb.exe"
        return True, f"taskkill exited {r.returncode} (non-fatal): {out.strip()[:120]}"
    except Exception as e:
        return False, f"taskkill error: {e}"


# ---------------------------------------------------------------------------
# Step 2: Close Explorer windows on the Walkman
# ---------------------------------------------------------------------------

def _close_walkman_explorer_windows() -> tuple[bool, str]:
    """
    Walk Shell.Windows() and close any whose path contains the device
    name.  COM-based; no extra deps beyond pywin32, which the rest of the
    pipeline already requires.

    If pywin32 is missing (unlikely given mtp_transfer_com.py uses it),
    this step is a silent no-op rather than a hard error.
    """
    try:
        import win32com.client as _w32
    except ImportError:
        return True, "pywin32 not installed — skipping"

    dev_name = (getattr(config, "DEVICE_NAME", "WALKMAN") or "WALKMAN").lower()
    closed = 0
    try:
        shell = _w32.Dispatch("Shell.Application")
        wins  = shell.Windows()
        # Iterate a snapshot — closing a window invalidates the live collection.
        snapshot = [wins.Item(i) for i in range(wins.Count)]
        for win in snapshot:
            try:
                loc = (getattr(win, "LocationURL", "") or "").lower()
                name = (getattr(win, "LocationName", "") or "").lower()
                if dev_name in loc or dev_name in name:
                    win.Quit()
                    closed += 1
            except Exception:
                continue
    except Exception as e:
        return False, f"Shell.Windows enumerate failed: {e}"

    return True, f"closed {closed} Explorer window(s)"


# ---------------------------------------------------------------------------
# Step 3: Cycle the WPD service  (opt-in, requires admin)
# ---------------------------------------------------------------------------

def _cycle_wpd() -> tuple[bool, str]:
    """
    Restart the WPDBusEnum ("Portable Device Enumerator Service") to clear
    stale MTP state. Requires admin; skipped unless config.PREFLIGHT_CYCLE_WPD
    is True.
    """
    if not getattr(config, "PREFLIGHT_CYCLE_WPD", False):
        return True, "skipped (opt-in)"

    # net stop requires admin; we do not self-elevate — if the user enabled
    # this flag they accept the requirement.
    def _run(cmd: list[str]) -> str:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (r.stdout or "") + (r.stderr or "")

    # "WpdMtp" is not a real Windows service name — confirmed live (2026-07-17)
    # via Get-Service that the actual service is WPDBusEnum ("Portable Device
    # Enumerator Service"). This function silently never worked before; fixed
    # to use the real name.
    try:
        out1 = _run(["net", "stop", "WPDBusEnum"])
        out2 = _run(["net", "start", "WPDBusEnum"])
        ok = "successfully" in (out1 + out2).lower()
        return ok, "cycled WPDBusEnum" if ok else f"WPD cycle did not confirm: {(out1+out2)[:200]}"
    except Exception as e:
        return False, f"WPD cycle error: {e}"


# ---------------------------------------------------------------------------
# Step 4: Disable AutoPlay for Walkman connection events
# ---------------------------------------------------------------------------

def _disable_autoplay() -> tuple[bool, str]:
    """
    Disable AutoPlay at HKCU scope so plugging in the Walkman (or any
    already-connected MTP device) does not pop File Explorer.  Per-user,
    per-session, revocable via Control Panel → AutoPlay.

    Skipped if config.PREFLIGHT_DISABLE_AUTOPLAY is False.
    """
    if not getattr(config, "PREFLIGHT_DISABLE_AUTOPLAY", True):
        return True, "skipped (disabled in config)"

    try:
        import winreg  # type: ignore
    except ImportError:
        return True, "winreg unavailable — skipping"

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0,
            winreg.KEY_SET_VALUE,
        ) as k:
            winreg.SetValueEx(k, "DisableAutoplay", 0, winreg.REG_DWORD, 1)
        return True, "DisableAutoplay=1 (HKCU)"
    except FileNotFoundError:
        # Key doesn't exist on this Windows build — accept silently.
        return True, "AutoplayHandlers key absent"
    except PermissionError as e:
        return False, f"registry permission denied: {e}"
    except Exception as e:
        return False, f"registry write error: {e}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run() -> dict:
    """
    Execute the configured preflight steps.  Each step is isolated — a
    failure in one does not block the others.

    Returns a summary dict that the caller can log.
    """
    results: dict[str, dict] = {}

    steps: list[tuple[str, bool, callable]] = [
        ("kill_stray_adb",    getattr(config, "PREFLIGHT_KILL_ADB",       True),  _kill_stray_adb),
        ("close_explorer",    getattr(config, "PREFLIGHT_CLOSE_EXPLORER", True),  _close_walkman_explorer_windows),
        ("cycle_wpd",         getattr(config, "PREFLIGHT_CYCLE_WPD",      False), _cycle_wpd),
        ("disable_autoplay",  getattr(config, "PREFLIGHT_DISABLE_AUTOPLAY", True), _disable_autoplay),
    ]

    for name, enabled, fn in steps:
        if not enabled:
            results[name] = {"ok": True, "detail": "disabled"}
            continue
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"exception: {e}"
        results[name] = {"ok": ok, "detail": detail}
        level = log.info if ok else log.warning
        level(f"  [Preflight/{name}] {detail}")

    return results


def ensure_run_once() -> None:
    """
    Call run() exactly once per Python process.  Safe to invoke multiple
    times — second and later calls no-op.
    """
    global _already_ran
    if not getattr(config, "ENABLE_PREFLIGHT", True):
        return
    with _ran_lock:
        if _already_ran:
            return
        log.info("  [Preflight] Starting environmental cleanup ...")
        run()
        _already_ran = True
        log.info("  [Preflight] Done.")
