"""
resource_monitor.py  —  Windows handle-exhaustion watchdog  (Rev 14.5)
======================================================================
Driven by the 2026-04-18 screenshot showing:
  - Tk_GetPixmap: Error from CreateDIBSection — Not enough memory resources
  - [WinError 1450] Insufficient system resources exist to complete the
    requested service
  - Tkinter itself failing to allocate drawing surfaces

Neither of those is a disk-space error.  Both are Windows per-process or
per-session handle exhaustion: GDI objects (10,000 cap by default), USER
objects (~10,000 per process / 66,536 per session), or kernel pool
exhaustion from a large subprocess tree.

This module:
  1. Polls current GDI + USER + raw-handle counts every POLL_INTERVAL_S
     via GetGuiResources() and GetProcessHandleCount() (both Win32).
  2. Exposes a throttle Event — threads can check `should_throttle()`
     before spawning a new subprocess.  Throttle engages when any
     resource crosses the configured high-water mark, disengages when it
     drops back below the low-water mark.
  3. Hard-stops new spawns (and logs an ERROR) if a resource crosses the
     ABORT threshold.  This turns an opaque Tkinter crash into a clear,
     actionable log line.

On non-Windows platforms every function is a no-op — the module still
imports, `should_throttle()` always returns False, and `start()` logs
that monitoring is unavailable and returns.

Usage from main.py:
    import resource_monitor as _rm
    _rm.start()          # at session entry
    # ... agent runs normally ...
    _rm.stop()           # at session exit

Usage from downloader.py worker:
    import resource_monitor as _rm
    _rm.wait_if_throttled(timeout=60)
    # ... spawn subprocess ...

Zero dependencies beyond stdlib + ctypes (which is stdlib).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# =============================================================================
# Platform detection
# =============================================================================

import sys
_IS_WINDOWS = sys.platform.startswith("win")


# =============================================================================
# Windows handle-count probes via ctypes
# =============================================================================

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # GetGuiResources(HANDLE hProcess, DWORD uiFlags) → DWORD
    _user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _user32.GetGuiResources.restype = wintypes.DWORD

    # GetCurrentProcess() → HANDLE (pseudo-handle; no close needed)
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    # GetProcessHandleCount(HANDLE hProcess, PDWORD pdwHandleCount) → BOOL
    _kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    _GR_GDIOBJECTS = 0
    _GR_USEROBJECTS = 1

    def _current_gdi_count() -> int:
        h = _kernel32.GetCurrentProcess()
        return int(_user32.GetGuiResources(h, _GR_GDIOBJECTS))

    def _current_user_count() -> int:
        h = _kernel32.GetCurrentProcess()
        return int(_user32.GetGuiResources(h, _GR_USEROBJECTS))

    def _current_handle_count() -> int:
        h = _kernel32.GetCurrentProcess()
        count = wintypes.DWORD(0)
        ok = _kernel32.GetProcessHandleCount(h, ctypes.byref(count))
        if not ok:
            return -1
        return int(count.value)
else:
    def _current_gdi_count() -> int: return -1
    def _current_user_count() -> int: return -1
    def _current_handle_count() -> int: return -1


# =============================================================================
# Configurable thresholds
# =============================================================================

def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


# Windows limits (defaults):
#   GDI objects per process: 10,000 (HKLM\SOFTWARE\Microsoft\Windows NT\
#                                    CurrentVersion\Windows\GDIProcessHandleQuota)
#   USER objects per process: 10,000 (USERProcessHandleQuota)
#   Raw handles per process: ~16 million soft cap, but ~65,000 practical
#                            before kernel-pool pressure
#
# We set thresholds well below these so there's headroom for Tkinter /
# other library calls that haven't spawned yet when we detect the climb.

@dataclass
class _Thresholds:
    # Warn-only: log a WARNING at this level.  No behavioural change.
    gdi_warn: int = 6000
    user_warn: int = 6000
    handle_warn: int = 8000

    # Throttle: block new subprocess spawns until resource drops below
    # gdi_relief / user_relief / handle_relief.
    gdi_throttle: int = 8000
    user_throttle: int = 8000
    handle_throttle: int = 12000

    gdi_relief: int = 7000
    user_relief: int = 7000
    handle_relief: int = 10000

    # Abort: stop dispatching new work entirely.  Current in-flight work
    # finishes; no new work is scheduled.
    gdi_abort: int = 9500
    user_abort: int = 9500
    handle_abort: int = 15000


def _load_thresholds() -> _Thresholds:
    return _Thresholds(
        gdi_warn=_cfg("RESOURCE_GDI_WARN", 6000),
        user_warn=_cfg("RESOURCE_USER_WARN", 6000),
        handle_warn=_cfg("RESOURCE_HANDLE_WARN", 8000),
        gdi_throttle=_cfg("RESOURCE_GDI_THROTTLE", 8000),
        user_throttle=_cfg("RESOURCE_USER_THROTTLE", 8000),
        handle_throttle=_cfg("RESOURCE_HANDLE_THROTTLE", 12000),
        gdi_relief=_cfg("RESOURCE_GDI_RELIEF", 7000),
        user_relief=_cfg("RESOURCE_USER_RELIEF", 7000),
        handle_relief=_cfg("RESOURCE_HANDLE_RELIEF", 10000),
        gdi_abort=_cfg("RESOURCE_GDI_ABORT", 9500),
        user_abort=_cfg("RESOURCE_USER_ABORT", 9500),
        handle_abort=_cfg("RESOURCE_HANDLE_ABORT", 15000),
    )


# =============================================================================
# State — a single module-global monitor
# =============================================================================

@dataclass
class _State:
    started: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
    throttle_event: threading.Event = field(default_factory=threading.Event)
    abort_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    thresholds: _Thresholds = field(default_factory=_Thresholds)
    poll_interval_s: float = 30.0

    # Rate-limit WARN logs so we don't spam once/second when over threshold
    last_warn_at: float = 0.0
    warn_cooldown_s: float = 60.0

    # Telemetry
    samples_collected: int = 0
    throttle_engagements: int = 0
    throttle_wait_total_s: float = 0.0

_S = _State()


# =============================================================================
# Public API
# =============================================================================

def is_available() -> bool:
    """True on Windows.  False elsewhere (module is a no-op)."""
    return _IS_WINDOWS


def start(poll_interval_s: Optional[float] = None) -> None:
    """
    Begin monitoring in a daemon thread.  Idempotent — calling twice does
    nothing.  On non-Windows systems, logs a note and returns.
    """
    if not _IS_WINDOWS:
        log.info("[ResourceMonitor] Non-Windows platform — monitoring disabled")
        return

    if _S.started:
        return

    _S.thresholds = _load_thresholds()
    _S.poll_interval_s = (poll_interval_s
                          if poll_interval_s is not None
                          else _cfg("RESOURCE_POLL_INTERVAL_S", 30.0))
    _S.stop_event.clear()
    _S.throttle_event.clear()
    _S.abort_event.clear()
    _S.samples_collected = 0
    _S.throttle_engagements = 0
    _S.throttle_wait_total_s = 0.0

    thread = threading.Thread(
        target=_monitor_loop, name="WalkmanResourceMonitor", daemon=True
    )
    _S.thread = thread
    _S.started = True
    thread.start()

    # Log baseline
    try:
        gdi, usr, hnd = _current_gdi_count(), _current_user_count(), _current_handle_count()
        log.info(
            f"[ResourceMonitor] Started (poll={_S.poll_interval_s:.0f}s) — "
            f"baseline GDI={gdi}  USER={usr}  Handles={hnd}"
        )
    except Exception as e:
        log.info(f"[ResourceMonitor] Started (poll={_S.poll_interval_s:.0f}s) — "
                 f"baseline probe failed: {e}")


def stop() -> None:
    """Signal the monitor thread to exit and join briefly."""
    if not _S.started:
        return
    _S.stop_event.set()
    if _S.thread is not None:
        _S.thread.join(timeout=5.0)
    log.info(
        f"[ResourceMonitor] Stopped — {_S.samples_collected} samples, "
        f"throttle engaged {_S.throttle_engagements} time(s) "
        f"({_S.throttle_wait_total_s:.1f}s total worker wait)"
    )
    _S.started = False
    _S.thread = None


def should_throttle() -> bool:
    """Non-blocking check.  True if callers should delay new subprocess spawns."""
    return _S.throttle_event.is_set()


def should_abort() -> bool:
    """Non-blocking check.  True if resources have crossed abort thresholds
    and no new work should be scheduled."""
    return _S.abort_event.is_set()


def wait_if_throttled(timeout: float = 60.0) -> bool:
    """
    Block up to `timeout` seconds if throttle is engaged; otherwise return
    immediately.  Returns True if the throttle cleared within the timeout,
    False if it's still engaged after the timeout elapsed.  Aborts immediately
    (returns False) if abort_event is set.
    """
    if not _S.started or not _IS_WINDOWS:
        return True

    if _S.abort_event.is_set():
        return False

    if not _S.throttle_event.is_set():
        return True

    _S.throttle_engagements += 1
    t0 = time.time()
    # Poll rather than a single wait() so we pick up relief promptly
    end = t0 + timeout
    while time.time() < end:
        if _S.abort_event.is_set():
            _S.throttle_wait_total_s += time.time() - t0
            return False
        if not _S.throttle_event.is_set():
            _S.throttle_wait_total_s += time.time() - t0
            return True
        time.sleep(1.0)
    _S.throttle_wait_total_s += time.time() - t0
    return not _S.throttle_event.is_set()


def current_snapshot() -> dict:
    """Return the most recent probe values.  Blocks only long enough
    to take one fresh probe."""
    if not _IS_WINDOWS:
        return {"available": False}
    gdi = _current_gdi_count()
    usr = _current_user_count()
    hnd = _current_handle_count()
    t = _S.thresholds
    return {
        "available": True,
        "gdi": gdi,
        "user": usr,
        "handles": hnd,
        "gdi_pct_of_throttle": 100.0 * gdi / t.gdi_throttle if t.gdi_throttle else 0,
        "user_pct_of_throttle": 100.0 * usr / t.user_throttle if t.user_throttle else 0,
        "handle_pct_of_throttle": (
            100.0 * hnd / t.handle_throttle if t.handle_throttle and hnd >= 0 else 0
        ),
        "throttled": should_throttle(),
        "aborted": should_abort(),
    }


# =============================================================================
# MK37.2-5 — Pressure callback registry
# =============================================================================
# Minimal callback surface so external modules (mk37_2_resource_coupling) can
# react to pressure events without monkey-patching the monitor loop.
# Callbacks fire from the monitor thread when the warn threshold is crossed,
# rate-limited by the existing _S.warn_cooldown_s. They receive a dict with
# the fields {gdi, user, handles, throttled, aborted}.
# Exceptions in callbacks are caught and logged at DEBUG so they cannot
# disrupt monitor-loop hysteresis or abort logic.

_PRESSURE_CALLBACKS: list = []
_PRESSURE_CALLBACKS_LOCK = threading.RLock()


def register_pressure_callback(fn) -> None:
    """Register a callable invoked when pressure crosses warn threshold.

    Idempotent: registering the same callable twice is a no-op.
    Callable signature: fn(snapshot_dict) -> None.
    """
    if not callable(fn):
        raise TypeError("register_pressure_callback expects a callable")
    with _PRESSURE_CALLBACKS_LOCK:
        if fn not in _PRESSURE_CALLBACKS:
            _PRESSURE_CALLBACKS.append(fn)


def unregister_pressure_callback(fn) -> bool:
    """Remove a previously-registered callback. Returns True if removed."""
    with _PRESSURE_CALLBACKS_LOCK:
        try:
            _PRESSURE_CALLBACKS.remove(fn)
            return True
        except ValueError:
            return False


def _fire_pressure_callbacks(gdi: int, usr: int, hnd: int) -> None:
    """Internal: invoke registered callbacks. Called from _monitor_loop."""
    with _PRESSURE_CALLBACKS_LOCK:
        callbacks = list(_PRESSURE_CALLBACKS)
    if not callbacks:
        return
    snap = {"gdi": gdi, "user": usr, "handles": hnd,
            "throttled": should_throttle(), "aborted": should_abort()}
    for fn in callbacks:
        try:
            fn(snap)
        except Exception as e:
            log.debug(f"[ResourceMonitor] pressure callback {fn!r} raised: {e}")


# =============================================================================
# Monitor loop
# =============================================================================

def _monitor_loop() -> None:
    """Poll handle counts and flip throttle / abort events as thresholds cross."""
    t = _S.thresholds

    while not _S.stop_event.is_set():
        try:
            gdi = _current_gdi_count()
            usr = _current_user_count()
            hnd = _current_handle_count()
            _S.samples_collected += 1

            now = time.time()

            # Warn — rate-limited, never behavioural
            if (gdi >= t.gdi_warn or usr >= t.user_warn
                    or (hnd > 0 and hnd >= t.handle_warn)):
                if now - _S.last_warn_at >= _S.warn_cooldown_s:
                    log.warning(
                        f"[ResourceMonitor] Resource pressure climbing: "
                        f"GDI={gdi}/{t.gdi_throttle}  USER={usr}/{t.user_throttle}  "
                        f"Handles={hnd}/{t.handle_throttle}"
                    )
                    _S.last_warn_at = now

                    # MK17 Patch 9 (post-LOG_17 fix) — periodic forced GC
                    # when pressure crosses the warn line.  LOG_17 showed
                    # handle count climbing from 8013 → 15921 over 32h
                    # (~250/hr).  Most leaks come from transient win32com
                    # objects whose Python wrappers sit in cycles that
                    # only generational GC can break.  Forced collect
                    # here keeps us under the abort ceiling for longer
                    # without touching the actual leak source.
                    try:
                        import gc
                        before = hnd
                        n_collected = gc.collect()
                        # Re-probe after GC
                        hnd_after = _current_handle_count()
                        if hnd_after > 0 and hnd_after < before:
                            log.info(
                                f"[ResourceMonitor] Forced GC freed "
                                f"{before - hnd_after} handle(s) "
                                f"({n_collected} object(s) collected)."
                            )
                    except Exception as ge:
                        log.debug(f"[ResourceMonitor] forced GC failed: {ge}")

                    # MK37.2-5: fire registered pressure callbacks (e.g.
                    # capability-renewal coupling). Same rate-limit as the
                    # warn log itself, so callbacks don't churn the
                    # capability registry.
                    try:
                        _fire_pressure_callbacks(gdi, usr, hnd)
                    except Exception as cbe:
                        log.debug(f"[ResourceMonitor] callback fan-out failed: {cbe}")

            # Throttle engage / relieve (hysteresis — separate thresholds)
            if _S.throttle_event.is_set():
                # Currently throttled — release only when all resources below relief
                if (gdi < t.gdi_relief
                        and usr < t.user_relief
                        and (hnd < 0 or hnd < t.handle_relief)):
                    _S.throttle_event.clear()
                    log.info(
                        f"[ResourceMonitor] Throttle relieved — "
                        f"GDI={gdi}  USER={usr}  Handles={hnd}"
                    )
            else:
                # Not throttled — engage if any resource crosses throttle mark
                if (gdi >= t.gdi_throttle or usr >= t.user_throttle
                        or (hnd > 0 and hnd >= t.handle_throttle)):
                    _S.throttle_event.set()
                    log.warning(
                        f"[ResourceMonitor] Throttle ENGAGED — new subprocess "
                        f"spawns will pause.  GDI={gdi}  USER={usr}  Handles={hnd}"
                    )

            # Abort — terminal condition, do not clear
            if (gdi >= t.gdi_abort or usr >= t.user_abort
                    or (hnd > 0 and hnd >= t.handle_abort)):
                if not _S.abort_event.is_set():
                    _S.abort_event.set()
                    log.error(
                        f"[ResourceMonitor] ABORT — Windows resource ceiling "
                        f"reached.  GDI={gdi}  USER={usr}  Handles={hnd}.  "
                        "Current work will finish but no new work will be dispatched. "
                        "Restart Windows to reclaim leaked handles before the next run."
                    )

        except Exception as e:
            log.debug(f"[ResourceMonitor] Probe failed (non-fatal): {e}")

        _S.stop_event.wait(_S.poll_interval_s)
