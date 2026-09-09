"""
adb_transfer.py  —  ADB-based file operations for Android Walkman devices
==========================================================================
Provides the same public interface as mtp_transfer_com.py but uses adb
commands instead of Windows Shell COM / MTP.

Fixes vs. the first draft
--------------------------
* _run_adb_cmd was called but never defined — consolidated into _adb_cmd.
* copy_file_to_device: tmp_src was referenced in the finally block before
  it was conditionally assigned → NameError on safe filenames.  Fixed with
  own_tmp flag pattern.
* _adb_cmd default check= changed from True to False so non-critical probes
  (test -e, df, devices) do not raise.  Callers that need hard failure pass
  check=True explicitly.
* _mkdir_p used undefined _run_adb_cmd; replaced with _adb_cmd.
* _rm_rf likewise.
* _exists likewise.
* delete_empty_device_folders: compound shell command passed as a single
  string arg now wrapped via ["shell", "sh", "-c", cmd] so adb parses it
  correctly.
* scan_device_audio: find(...) args list built with proper shell quoting
  via ["shell", "sh", "-c", cmd_str] instead of a broken positional list.
* reset_storage_cache: clears the function-level attribute properly.
* ADB_SERIAL support wired into _adb_cmd.
"""

import subprocess
import re
import logging
import tempfile
import shutil
import sys
import time
import os
from pathlib import Path
from typing import List, Tuple, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (overridden at startup by config.py / mtp_transfer.py)
# ---------------------------------------------------------------------------
ADB_TIMEOUT = 120      # seconds per adb command
ADB_RETRIES = 3
ADB_SERIAL: Optional[str] = None   # set to device serial if multiple devices


# ---------------------------------------------------------------------------
# adb executable resolution
# ---------------------------------------------------------------------------
# PowerShell does not look in the current directory by default, so a plain
# "adb" call fails even when adb.exe sits right in the project folder.
# We probe candidate paths at import time and use the first one that works.

def _find_adb() -> str:
    """Return the full path to a working adb executable, or 'adb' as fallback."""
    candidates = [
        "adb",   # already on PATH (normal PowerShell / CMD)
        str(Path(__file__).parent / "adb.exe"),                          # project folder
        str(Path(__file__).parent / "tools" / "platform-tools" / "adb.exe"),  # bundled sdk
        r"C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
        r"C:\Program Files\Android\android-sdk\platform-tools\adb.exe",
        str(Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe"),
    ]
    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "version"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",  # Rev 14.9.6 U1
            )
            if result.returncode == 0:
                log.debug(f"  [ADB] Using executable: {candidate}")
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    log.warning("  [ADB] Could not find a working adb executable — all candidates failed")
    return "adb"   # last-ditch fallback; will produce clear errors at call time

_ADB_EXE = _find_adb()


# ---------------------------------------------------------------------------
# Cross-process device I/O lock
# ---------------------------------------------------------------------------
# 2026-08-27: The GUI folder-audit (FolderAudit backtrack verification) and
# the standalone scan-duplicates CLI have been observed live running against
# the same physical Walkman over the same USB link at once, each spawning
# its own adb subprocess per call. Interleaved transfers compete for USB
# bandwidth and have triggered real 'adb command timed out' failures (the
# 2026-08-24 run hit this mid-audit). Worse, the daemon-recovery path below
# calls `adb kill-server` on failure — if that fires while another process
# has a pull/push genuinely in flight, it aborts that transfer outright.
#
# An OS-level byte-range lock on a small file serializes every adb
# invocation across processes (regardless of which script started it) so
# only one is ever mid-command. Using msvcrt.locking rather than a
# PID-in-a-file scheme means a crashed holder releases automatically when
# Windows reclaims the file handle — no manual staleness/liveness check
# needed. Fails open (proceeds without the lock, logs a warning) rather
# than deadlocking a multi-hour run over a coordination guard.
import contextlib as _contextlib

try:
    import msvcrt as _msvcrt
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

_DEVICE_LOCK_PATH = Path(__file__).resolve().parent.parent / "state" / ".device_io.lock"
_DEVICE_LOCK_TIMEOUT_S = 90.0
_DEVICE_LOCK_POLL_S = 0.15


@_contextlib.contextmanager
def _device_io_lock(timeout: float = _DEVICE_LOCK_TIMEOUT_S):
    """Serialize one adb subprocess invocation at a time, across processes."""
    if not _MSVCRT_AVAILABLE:
        yield
        return

    _DEVICE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(_DEVICE_LOCK_PATH, "a+b")
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                f.seek(0)
                _msvcrt.locking(f.fileno(), _msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning(
                        f"  [ADB/Lock] Could not acquire device I/O lock within "
                        f"{timeout:.0f}s — proceeding without it (contention risk)"
                    )
                    break
                time.sleep(_DEVICE_LOCK_POLL_S)
        yield
    finally:
        if acquired:
            try:
                f.seek(0)
                _msvcrt.locking(f.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        f.close()


# ---------------------------------------------------------------------------
# Core adb runner
# ---------------------------------------------------------------------------

def _adb_cmd(args: list, timeout: int = ADB_TIMEOUT,
             input_data: str = None, check: bool = False) -> str:
    """
    Run an adb command and return stdout as a stripped string.

    check=False (default): return "" on any failure — safe for probes.
    check=True            : raise subprocess.CalledProcessError on non-zero exit.
    Always raises subprocess.TimeoutExpired on timeout (regardless of check).

    REV 14.9.1 — `result.stdout` can be None when the adb subprocess is
    killed by a signal or the device disconnects mid-call.  Accessing
    .strip() on None raised AttributeError, which in LOG-13 Run 2 surfaced
    as '[Index] find failed: \\'NoneType\\' object has no attribute \\'strip\\''
    and left the in-memory device index empty — which disabled the
    scrub, free-space check, and C2 fast paths silently.  Now we coerce
    None to empty string so probes stay safe.

    REV 14.9.6 (U1) — `text=True` makes subprocess decode stdout using the
    system's default encoding (cp1252 on Windows).  When `adb shell find`
    returns paths with bytes outside cp1252 (any Japanese / Korean / Arabic
    / Cyrillic / Greek artist name in the user's library), the decoder dies
    with `'charmap' codec can't decode byte 0x8d`.  LOG_16 caught this in
    `_readerthread` — the index refresh failed twice and the in-memory
    device index was disabled for the entire run, taking the Rev 14.9
    scrub and free-space fast paths down with it.  Fix: explicit
    encoding="utf-8" with errors="replace" so any malformed byte sequences
    become U+FFFD instead of crashing.  Filenames stay debuggable; the
    pipeline keeps running.
    2026-08-20 — collapses the `["shell", "sh", "-c", X]` calling pattern
    (used throughout this module) to `["shell", X]` before building argv.
    Confirmed live: passing "sh", "-c", and the command string as three
    SEPARATE elements of the Python list handed to subprocess.run() causes
    the remote command to silently run as if `sh -c` received no effective
    script at all (e.g. `find` defaulted to an unfiltered `find .` from the
    device shell's cwd; `test -e <real path>` reported false for a path
    that genuinely exists) — no exception, no error, just wrong/empty
    results. Collapsing "sh", "-c", and the command into ONE pre-joined
    string element (still one `adb shell <one-string>` call — adb's own
    non-interactive `shell` already implicitly runs a single opaque string
    through the device's shell, so the explicit `sh -c` wrapper was always
    redundant, not required) was tested live and works correctly. This
    likely explains a real fraction of this project's long-documented "ADB
    flakiness" history — every _mkdir_p/_rm_f/_mv/_exists/rename_*/
    scan_device_audio call built on this exact pattern was silently
    returning wrong results whenever it went through this code path.
    """
    if len(args) == 4 and args[0] == "shell" and args[1] == "sh" and args[2] == "-c":
        args = ["shell", args[3]]
    cmd = [_ADB_EXE]
    if ADB_SERIAL:
        cmd += ["-s", ADB_SERIAL]
    cmd += args
    try:
        with _device_io_lock():
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout, input=input_data, check=check,
            )
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        log.warning(f"adb command timed out: {cmd}")
        raise
    except subprocess.CalledProcessError as e:
        if check:
            raise
        log.debug(f"adb command failed (rc={e.returncode}): {' '.join(cmd)}")
        return ""
    except FileNotFoundError:
        log.error(f"adb not found at '{_ADB_EXE}' — install Android Platform Tools")
        if check:
            raise
        return ""


# ---------------------------------------------------------------------------
# REV 14.9 — ADB push output parsing + post-transfer verification
# ---------------------------------------------------------------------------

_PUSH_SUCCESS_RE = re.compile(
    r'(\d+)\s+file(?:s)?\s+pushed,?\s*(\d+)\s+skipped',
    re.IGNORECASE,
)
_PUSH_BYTES_RE = re.compile(
    r'\(([0-9]+)\s+bytes?\s+in\s+[0-9.]+s\)',
    re.IGNORECASE,
)


def _parse_adb_push_output(stdout: str, stderr: str) -> tuple[bool, str, int]:
    """
    Parse the output of `adb push`.  ADB emits "1 file pushed, 0 skipped"
    on success even when exit code is nonzero due to a benign warning.

    Returns (pushed_ok, warning_text, bytes_transferred).
    - pushed_ok: True iff output contains "N file(s) pushed" with N>=1.
    - warning_text: non-empty string if stderr contains a warning keyword
      ("No space left", "remote write failed", "couldn't stat", etc.);
      empty if no meaningful warning.
    - bytes_transferred: parsed from "(N bytes in T.Ts)" if present, else 0.

    On modern adb the push summary can appear on either stream depending on
    build; we check both.
    """
    combined = (stdout or "") + "\n" + (stderr or "")

    pushed_ok = False
    m = _PUSH_SUCCESS_RE.search(combined)
    if m:
        try:
            pushed_ok = int(m.group(1)) >= 1
        except ValueError:
            pushed_ok = False

    bytes_transferred = 0
    m2 = _PUSH_BYTES_RE.search(combined)
    if m2:
        try:
            bytes_transferred = int(m2.group(1))
        except ValueError:
            bytes_transferred = 0

    # Extract warning text: look for known warning keywords
    warning = ""
    lc = (stderr or "").lower()
    for keyword in (
        "no space left on device",
        "remote write failed",
        "couldn't stat",
        "permission denied",
        "read-only file system",
        "invalid argument",
    ):
        if keyword in lc:
            warning = (stderr or "").strip()[:300]
            break

    return pushed_ok, warning, bytes_transferred


def _verify_remote_size(remote_path: str, expected_size: int,
                        retries: int = 5, base_wait_s: float = 2.0) -> tuple[bool, str]:
    """
    Verify a file exists on the device at the expected size.
    Returns (ok, reason).  ok=False with a human-readable reason if missing,
    size mismatch, or stat failed.

    MK8 Fix (LOG_17_3): The Sony Walkman MTP stack creates a 4096-byte
    placeholder file immediately after ADB push begins, then overwrites it
    with actual data.  Checking size too quickly returns 4096 bytes instead
    of the real file size, causing a spurious verification failure and a COM
    fallback that also fails (remote size 0 in Shell.Application).

    Fix: retry up to `retries` times with exponential backoff (2s, 4s, 8s...)
    whenever we see a known placeholder size (≤4096 bytes) and the expected
    size is larger.  The wait is short because the Walkman typically finalises
    the file within 2-8 seconds of the push completing.
    """
    _PLACEHOLDER_THRESHOLD = 4096  # bytes — any size ≤ this is treated as placeholder

    import time as _time
    last_reason = "not attempted"

    for attempt in range(retries + 1):
        try:
            stat_out = _adb_cmd(
                ["shell", "stat", "-c", "%s", _shell_quote(remote_path)],
                timeout=30, check=False,
            )
            stat_out = (stat_out or "").strip()
            if not stat_out:
                last_reason = "stat returned empty output (file likely missing)"
                if attempt < retries:
                    _time.sleep(base_wait_s * (2 ** attempt))
                    continue
                return False, last_reason

            first_line = stat_out.splitlines()[0].strip()
            if not first_line.isdigit():
                last_reason = f"stat returned non-numeric output: {first_line!r}"
                if attempt < retries:
                    _time.sleep(base_wait_s * (2 ** attempt))
                    continue
                return False, last_reason

            actual_size = int(first_line)

            # Placeholder detection: if actual_size is suspiciously small and
            # we still have retries left, wait and try again.
            if (actual_size <= _PLACEHOLDER_THRESHOLD
                    and expected_size > _PLACEHOLDER_THRESHOLD
                    and attempt < retries):
                log.debug(
                    f"  [ADB/Verify] {remote_path!r}: placeholder size {actual_size} "
                    f"(expected {expected_size}) — retry {attempt + 1}/{retries} in "
                    f"{base_wait_s * (2 ** attempt):.0f}s"
                )
                _time.sleep(base_wait_s * (2 ** attempt))
                continue

            if actual_size != expected_size:
                return False, (
                    f"size mismatch: expected {expected_size} bytes, "
                    f"got {actual_size} bytes"
                )
            return True, ""
        except Exception as e:
            last_reason = f"stat errored: {type(e).__name__}: {e}"
            if attempt < retries:
                _time.sleep(base_wait_s)
                continue

    return False, last_reason


def _cfg():
    """Lazy config accessor so import cycles don't break this module."""
    try:
        import config as _c
        return _c
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Storage path resolution
# ---------------------------------------------------------------------------

def _get_storage_path(storage_name: str) -> str:
    """
    Map a human-readable storage name to a device filesystem path.
    Results are cached on the function object to avoid repeated adb ls calls.
    """
    cache: dict = getattr(_get_storage_path, "_cache", {})
    _get_storage_path._cache = cache

    if storage_name in cache:
        return cache[storage_name]

    out = _adb_cmd(["shell", "ls", "/storage"])
    roots = [r.strip() for r in out.splitlines() if r.strip()]

    # `adb shell ls /storage` returns bare directory names ("emulated", "4A21-0000"),
    # NOT full paths — so check for "emulated" (the emulated/0 parent), not the full path.
    internal = "/storage/emulated/0" if "emulated" in roots else "/sdcard"

    # SD card volume: any entry that is not a known system directory
    _SYSTEM_DIRS = {"emulated", "emulated/0", "self", "usr", "mnt"}
    sd = None
    for r in roots:
        if r and r not in _SYSTEM_DIRS:
            full = f"/storage/{r}"
            if full != internal:
                sd = full
                break

    mapping = {
        "Internal shared storage": internal,
        "SD card": sd,
    }

    # Distinguish "unknown name" from "valid name but storage absent"
    if storage_name not in mapping:
        raise RuntimeError(f"Unknown storage name '{storage_name}'. "
                           f"Expected 'Internal shared storage' or 'SD card'.")
    path = mapping[storage_name]
    if not path:
        raise RuntimeError(f"Storage '{storage_name}' is not available "
                           f"(no SD card detected in /storage).")
    cache[storage_name] = path
    return path


def reset_storage_cache():
    """Clear the storage path cache (e.g. after SD card change)."""
    _get_storage_path._cache = {}


def _invalidate_storage_cache():
    """Alias kept for compatibility with mtp_transfer_com interface."""
    reset_storage_cache()


def reconnect() -> bool:
    """
    Attempt to recover a degraded ADB connection.

    Runs `adb reconnect` (soft reconnect — does not kill the server) then
    waits up to 5 seconds for the device to reappear.  Clears the storage
    path cache so the next operation re-resolves storage roots cleanly.

    Returns True if the device is reachable after reconnect, False otherwise.
    """
    log.info("  [ADB] Reconnecting ADB session ...")
    _adb_cmd(["reconnect"], timeout=10, check=False)
    import time as _t
    for _ in range(5):
        _t.sleep(1)
        out = _adb_cmd(["devices"], timeout=6, check=False)
        if any(
            line.strip().endswith("device")
            for line in out.splitlines()
            if not line.startswith("List")
        ):
            reset_storage_cache()
            log.info("  [ADB] Reconnect successful — device reachable")
            return True
    log.warning("  [ADB] Reconnect failed — device not visible after 5s")
    return False


# ---------------------------------------------------------------------------
# ADB over WiFi (TCP/IP) — 2026-07-30
# ---------------------------------------------------------------------------
# Same adb protocol as everything above, just carried over TCP instead of a
# USB cable — every function in this file keeps working unchanged once
# connect_wifi() succeeds. Distinct from localsend_transfer.py, which is a
# different push-only protocol used as a first-choice transport; this is a
# full read/write fallback link for when USB ADB itself is the problem.

def enable_wifi_debugging(port: int = 5555) -> Optional[str]:
    """
    Arm ADB-over-WiFi. Requires the device to be reachable via USB right
    now (adb tcpip only works over an already-authorized session). Runs
    `adb tcpip <port>`, waits for adbd to restart in TCP mode, then reads
    the device's own WiFi IP via `ip addr show wlan0` (falls back to
    `ip route get 1` if wlan0 is renamed/absent on this device).

    Returns the device's IP as a string, or None on any failure (not
    connected, no WiFi interface found, tcpip command failed).
    """
    if not device_is_connected(""):
        log.warning("  [ADB] enable_wifi_debugging: no USB-connected device found")
        return None

    out = _adb_cmd(["tcpip", str(port)], timeout=10)
    if "restarting" not in out.lower() and out.strip() != "":
        log.debug(f"  [ADB] tcpip output: {out!r}")

    # 2026-08-21: was a fixed 2s sleep — confirmed live that the connection
    # can take ~10s to respond again after `adb tcpip` restarts adbd (traced:
    # empty output at 2/4/6/8s, real output at 10s). Poll with retries
    # instead of guessing a fixed delay.
    import time as _t
    ip_out = ""
    for _ in range(6):
        _t.sleep(2)
        ip_out = _adb_cmd(["shell", "ip", "addr", "show", "wlan0"], timeout=10)
        if ip_out.strip():
            break

    m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/', ip_out)
    if not m:
        # Some devices don't call the WiFi interface "wlan0" — fall back to
        # the source IP of the default route, which is WiFi on a device
        # with no other active network interface (this device has no
        # ethernet/cellular).
        route_out = _adb_cmd(["shell", "ip", "route", "get", "1"], timeout=10)
        m = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', route_out)
    if not m:
        log.warning("  [ADB] enable_wifi_debugging: could not determine device IP "
                     f"(wlan0 output: {ip_out[:120]!r})")
        return None

    ip = m.group(1)
    log.info(f"  [ADB] TCP/IP mode enabled on port {port} — device IP: {ip}")
    return ip


def connect_wifi(host: str, port: int = 5555, timeout: float = 5.0) -> bool:
    """
    Connect to a device already armed via enable_wifi_debugging() (or a
    prior session — TCP/IP mode persists until reboot/adbd restart).
    Verifies the connection actually reached "device" state in `adb
    devices` (not just that `adb connect` printed something hopeful — its
    own output is not a reliable success signal on every adb version).

    On success, pins ADB_SERIAL to this target so subsequent _adb_cmd()
    calls address the WiFi link specifically — necessary once USB is
    unplugged (nothing else to default to) and safe even if a stale/offline
    USB entry is still listed (an unpinned _adb_cmd would otherwise error
    with "more than one device").
    """
    global ADB_SERIAL
    target = f"{host}:{port}"
    _adb_cmd(["connect", target], timeout=int(timeout) + 5)

    import time as _t
    _t.sleep(1)
    out = _adb_cmd(["devices"], timeout=6)
    for line in out.splitlines():
        if line.startswith(target) and line.strip().endswith("device"):
            ADB_SERIAL = target
            reset_storage_cache()
            log.info(f"  [ADB] Connected over WiFi: {target}")
            return True

    log.warning(f"  [ADB] WiFi connect to {target} did not reach 'device' state "
                f"(adb devices output: {out[:200]!r})")
    return False


def disconnect_wifi(host: Optional[str] = None, port: int = 5555) -> bool:
    """
    Disconnect the WiFi ADB link (`adb disconnect [host:port]`, or all
    TCP/IP targets if host is omitted) and un-pin ADB_SERIAL so a plain USB
    device becomes addressable again. Closing the TCP/IP port matters on
    untrusted networks — adb's open port 5555 is a known scan target.
    """
    global ADB_SERIAL
    if host:
        _adb_cmd(["disconnect", f"{host}:{port}"], timeout=10)
    else:
        _adb_cmd(["disconnect"], timeout=10)
    if ADB_SERIAL and ":" in ADB_SERIAL:
        ADB_SERIAL = None
    reset_storage_cache()
    log.info("  [ADB] WiFi ADB disconnected")
    return True


def reconnect_wifi_if_configured() -> bool:
    """
    Second-stage fallback for a degraded USB ADB session: if the user has
    previously confirmed a WiFi ADB target (config.ADB_WIFI_ENABLED +
    ADB_WIFI_HOST, set by main.py's "Connect over WiFi" button on a real
    success), try that link before giving up and degrading to COM/MTP.
    Distinct from reconnect() above (which only re-pokes the SAME
    already-authorized USB session) — this switches to a different physical
    link, so callers should try reconnect() first and only fall through to
    this on failure. No-op (returns False immediately) if WiFi ADB was
    never configured, so this is safe to call unconditionally.
    """
    import config as _cfg
    if not getattr(_cfg, "ADB_WIFI_ENABLED", False):
        return False
    host = getattr(_cfg, "ADB_WIFI_HOST", None)
    if not host:
        return False
    port = getattr(_cfg, "ADB_WIFI_PORT", 5555)
    log.info(f"  [ADB] USB reconnect failed — trying WiFi ADB fallback ({host}:{port}) ...")
    return connect_wifi(host, port)


# ---------------------------------------------------------------------------
# ADB connection diagnostics — 2026-07-30
# ---------------------------------------------------------------------------
# Codifies the manual troubleshooting steps from a live ADB-connectivity
# session on the ZX507 (persistently `offline`, cycling through
# `authorizing` for only ~5s at a time even after ruling out driver
# binding, USB hub, stale keys, and a locked screen — see progress.md
# "Session: 2026-07-30" for the full live investigation). These turn that
# one-off diagnosis into reusable tools rather than living only in a
# terminal transcript, matching this project's established pattern (e.g.
# preflight.py's recovery functions, the reconnect helpers above).

def get_device_state(device_name: str = "") -> str:
    """
    Parse `adb devices -l` for this device's actual state, distinguishing
    the causes a standard ADB-over-USB troubleshooting flow separates into
    different fixes: "device" (authorized, healthy), "offline" (enumerated
    but the daemon-side session died before/during the handshake — cable/
    port/hub instability is the most common cause, confirmed live on this
    project 2026-07-30: continuous re-enumeration with a steadily climbing
    transport_id, unrelated to keys/driver/screen-lock), "unauthorized"
    (RSA prompt was declined, or a stale host key with no new prompt),
    "authorizing" (the prompt is up on the device screen right now —
    narrow window, tap Allow immediately), "no permissions" (Linux udev
    rule missing — not expected on this Windows-only project, kept for
    completeness), or "absent" (adb sees nothing at all — cable/port/
    driver problem, not something a software retry fixes).

    device_is_connected() above stays the simple boolean every existing
    caller already depends on; this is for diagnostic tooling only.
    """
    out = _adb_cmd(["devices", "-l"])
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            return parts[1]
    return "absent"


def reset_adb_pairing() -> Tuple[bool, str]:
    """
    PC-side half of the standard "force a fresh RSA prompt" fix: kill the
    adb server, back up (rename — never delete) the host's RSA key pair so
    a brand-new key is generated on next start, then restart the server.
    A stale/already-half-trusted key is a documented cause of a permanent
    unauthorized/offline state where no new prompt ever appears; deleting
    it forces the device to treat this host as never-seen-before and show
    a fresh "Allow USB debugging?" dialog.

    Returns (ok, detail). This only does the PC-side half — the device
    still needs its own on-device prompt accepted afterward, which no
    PC-side code can do; ok=True means the reset itself ran cleanly, not
    that authorization is now complete.
    """
    import datetime as _dt

    _adb_cmd(["kill-server"], timeout=10)

    android_dir = Path.home() / ".android"
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    moved = []
    for name in ("adbkey", "adbkey.pub"):
        src = android_dir / name
        if src.exists():
            dst = android_dir / f"{name}.bak-{stamp}"
            try:
                src.rename(dst)
                moved.append(name)
            except Exception as e:
                log.warning(f"  [ADB] Could not back up {name}: {e}")

    _adb_cmd(["start-server"], timeout=10)

    if moved:
        detail = (f"Backed up {', '.join(moved)} to *.bak-{stamp} — a fresh key pair will "
                  "be generated. Replug/reconnect and accept the new 'Allow USB debugging?' "
                  "prompt on the device (tick 'Always allow' if offered).")
    else:
        detail = "No existing adbkey found (already fresh) — adb server restarted."
    return True, detail


def diagnose_windows_driver(vendor_id: str = "054C") -> dict:
    """
    Windows-only diagnostic (this project is Windows-only throughout —
    Shell.Application COM, PowerShell CopyHere, etc.): queries Device
    Manager (Get-PnpDevice) for every USB entry matching Sony's vendor ID
    and reports whether an "ADB Interface" is bound and its Status. This
    distinguishes the single most common USB-ADB failure cause — Windows
    only loaded the MTP "portable device" driver, never the separate
    Android Composite ADB Interface — from an RSA/cable/hub problem, where
    the ADB Interface already shows Status=OK and the fault is downstream
    of the driver (confirmed exactly this way live on this project's own
    ZX507 2026-07-30: driver was already bound + OK, ruling out a Windows
    driver reinstall and pointing at the physical link instead).

    Best-effort: returns {"ok": False, "detail": ...} on any failure
    (PowerShell missing, nothing matching, JSON parse issue) rather than
    raising — this is a diagnostic aid, never load-bearing for the pipeline.
    """
    try:
        from wpd_transfer import _find_ps_exe
        ps_exe = _find_ps_exe()
        if not ps_exe:
            return {"ok": False, "detail": "No PowerShell executable found on PATH"}

        script = (
            f"Get-PnpDevice | Where-Object {{ $_.InstanceId -match 'VID_{vendor_id}' }} "
            "| Select-Object FriendlyName, Status | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return {"ok": False, "detail":
                     f"No USB devices found matching VID_{vendor_id} — check cable/port"}

        import json as _json
        data = _json.loads(raw)
        entries = data if isinstance(data, list) else [data]
        adb_entries = [e for e in entries if "ADB" in (e.get("FriendlyName") or "")]

        if not adb_entries:
            names = ", ".join(e.get("FriendlyName", "?") for e in entries)
            return {
                "ok": False, "driver_bound": False,
                "detail": ("No 'ADB Interface' entry found — Windows may only have loaded "
                          f"the MTP driver. Other entries present: {names}"),
            }

        statuses = [e.get("Status") for e in adb_entries]
        healthy = all(s == "OK" for s in statuses)
        return {
            "ok": healthy, "driver_bound": True,
            "detail": (f"ADB Interface found, Status={statuses[0]}"
                      + ("" if healthy else " (not OK — try reinstalling the driver)")),
        }
    except Exception as e:
        return {"ok": False, "detail": f"Diagnostic failed: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Low-level device helpers
# ---------------------------------------------------------------------------

def _shell_quote(s: str) -> str:
    """Single-quote a shell argument, safely handling embedded single quotes.
    Required because naive ['shell','mkdir','-p', path_with_spaces] causes
    Android sh to word-split on spaces — 'Music/The Weeknd' becomes three args.
    """
    return "'" + s.replace("'", "'\\''" ) + "'"


def _mkdir_p(path: str, device_index=None) -> None:
    """
    Create directory and all parents on device (shell-quoted for spaces).

    REV 14.9 (C2+B3) — when device_index is provided:
      * Skip the mkdir if the index already knows `path` is a directory.
      * If the index or a live check says `path` (or an ancestor) is a FILE
        (legacy pre-14.9 corruption), remove the blocker before mkdir.
    """
    if device_index is not None:
        # Fast path: index says it's already a directory
        if device_index.is_dir(path):
            return
        # If index says this path is a file, remove the blocker
        if device_index.is_file(path):
            log.warning(f"  [ADB] Removing blocking file before mkdir: {path}")
            _adb_cmd(["shell", "sh", "-c", "rm -f " + _shell_quote(path)], check=False)
            device_index.remove(path)

    _adb_cmd(["shell", "sh", "-c", "mkdir -p " + _shell_quote(path)], check=False)

    if device_index is not None:
        device_index.mark_dir(path)


def _rm_f(path: str) -> None:
    """Delete a file on device (no-op if absent, shell-quoted)."""
    _adb_cmd(["shell", "sh", "-c", "rm -f " + _shell_quote(path)], check=False)


def _mv(src_path: str, dest_path: str) -> bool:
    """
    Move/rename a file on-device (shell-quoted). Creates dest's parent dir
    first. Returns True if dest exists after the move (verifies rather than
    trusting mv's own exit code, matching _exists's device-warmup-tolerant
    pattern — a bare `mv && echo 1` can read back empty on a jittery link).
    """
    dest_dir = dest_path.rsplit("/", 1)[0] if "/" in dest_path else ""
    if dest_dir:
        _mkdir_p(dest_dir)
    _adb_cmd(
        ["shell", "sh", "-c", "mv " + _shell_quote(src_path) + " " + _shell_quote(dest_path)],
        check=False,
    )
    return _exists(dest_path)


def _exists(path: str) -> bool:
    """Return True if path exists on device (shell-quoted).

    REV 14.6 — robust against BusyBox/toybox quirks on the ZX500.
    The old implementation ran one `test -e` and compared stdout to "1".
    On several real-device runs the scan returned "not found" for every
    storage root even though transfers to those same paths succeeded
    seconds later — root cause was an empty adb stdout (device warming
    up / usb jitter) which the old code silently treated as False.

    The new implementation:
      1. Runs `test -e PATH` twice and accepts "1" on either attempt.
      2. Falls back to `ls -d PATH` and accepts any non-empty directory
         listing that does NOT contain "No such file" / "not found".
      3. On persistent False, logs the raw adb stdout+stderr once at
         DEBUG level so the next log file shows exactly what the device
         replied (empty string? permission error? usb re-enumeration?).
    """
    import time as _t

    def _try_test_e() -> tuple[bool, str]:
        raw = _adb_cmd(
            ["shell", "sh", "-c",
             "test -e " + _shell_quote(path) + " && echo 1 || echo 0"],
            check=False,
        )
        return raw.strip() == "1", raw

    # First attempt.
    ok, raw1 = _try_test_e()
    if ok:
        return True

    # If first attempt returned blank, give adb 250ms and retry once.
    if not raw1.strip():
        _t.sleep(0.25)
        ok, raw2 = _try_test_e()
        if ok:
            return True
    else:
        raw2 = raw1

    # Fallback: `ls -d PATH` succeeds with non-empty output when the
    # path exists, and prints "No such file or directory" otherwise.
    try:
        ls_out = _adb_cmd(
            ["shell", "sh", "-c", "ls -d " + _shell_quote(path) + " 2>&1"],
            check=False,
        )
        ls_lo = ls_out.strip().lower()
        if ls_lo and "no such" not in ls_lo and "not found" not in ls_lo \
                and "permission denied" not in ls_lo:
            return True
    except Exception:
        ls_out = ""

    # Persistent False — emit a DEBUG trace so the next log reveals what
    # the device actually returned (empty stdout, usb-timeout, etc.).
    log.debug(
        f"  [_exists] False for {path!r}  "
        f"test1={raw1.strip()!r}  test2={raw2.strip()!r}  ls={ls_out.strip()[:80]!r}"
    )
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def device_is_connected(device_name: str) -> bool:
    """Return True if at least one Android device is reachable via adb."""
    out = _adb_cmd(["devices"])
    lines = [l for l in out.splitlines() if l and not l.startswith("List")]
    return any("device" in l for l in lines)


def get_device_free_space(device_name: str, storage_name: str) -> int:
    """Return free bytes on storage, or -1 on error."""
    try:
        path = _get_storage_path(storage_name)
    except RuntimeError:
        return -1
    out = _adb_cmd(["shell", "df", "-B1", path])
    # df -B1 output:  Filesystem  Size  Used  Avail  Use%  Mounted on
    # The path appears at the start of the data line (may wrap).
    for line in out.splitlines():
        m = re.search(r'\s+(\d+)\s+\d+%', line)
        if m:
            return int(m.group(1))
        # Some Android df variants: path  total  used  free  pct  mount
        cols = line.split()
        if len(cols) >= 4 and cols[0] == path:
            try:
                return int(cols[3])
            except ValueError:
                pass
    return -1


def get_device_storage_pct_used(storage_name: str) -> float:
    """Return percent used (0-100) on storage, or -1.0 on error. Reads the
    device's own Use% column directly rather than computing free/total
    ourselves, so it can't drift out of sync with get_device_free_space."""
    try:
        path = _get_storage_path(storage_name)
    except RuntimeError:
        return -1.0
    out = _adb_cmd(["shell", "df", "-B1", path])
    for line in out.splitlines():
        m = re.search(r'\s+(\d+)%', line)
        if m:
            return float(m.group(1))
    return -1.0


# Last-known-good storage telemetry for pick_storage_balanced. `adb shell df`
# intermittently returns empty under heavy concurrent ADB load (4 download
# workers + transfers), which would otherwise collapse every routing decision
# to the WRITE_STORAGE default and silently stop balancing. Reuse the last
# good reading (per storage) for up to _BALANCED_TELEMETRY_TTL_S instead.
_BALANCED_TELEMETRY: dict = {}          # storage_name -> (free_bytes, pct_used, t)
_BALANCED_TELEMETRY_TTL_S = 300


def _balanced_reading(storage_name: str) -> "tuple[int, float]":
    """(free_bytes, pct_used) for a storage, falling back to the last good
    reading (<= _BALANCED_TELEMETRY_TTL_S old) when the live df query fails."""
    import time as _t
    free = get_device_free_space("", storage_name)
    pct  = get_device_storage_pct_used(storage_name)
    if free != -1 and pct != -1.0:
        _BALANCED_TELEMETRY[storage_name] = (free, pct, _t.time())
        return free, pct
    cached = _BALANCED_TELEMETRY.get(storage_name)
    if cached and (_t.time() - cached[2]) <= _BALANCED_TELEMETRY_TTL_S:
        log.debug(f"  [Router/Balanced] {storage_name}: live df failed, using cached "
                  f"({cached[0]} B free, {cached[1]:.0f}%%, {int(_t.time()-cached[2])}s old)")
        return cached[0], cached[1]
    return free, pct   # both still bad — let the caller handle -1


def pick_storage_balanced(file_size: int, filename: "str | None" = None) -> str:
    """
    Balance placement across Internal storage and SD card by percent-used
    (not raw free bytes), so both fill up proportionally — 2026-07-17 user
    direction, distinct from _pick_storage_for_file's primary/fallback
    policy (which stays unchanged for the existing ADB/MTP transfer path).
    Internal storage additionally reserves config.INTERNAL_STORAGE_MIN_FREE_MB
    so the OS is never starved. Raises RuntimeError if neither storage has
    room for file_size.
    """
    import config as _cfg
    internal_reserve = getattr(_cfg, "INTERNAL_STORAGE_MIN_FREE_MB", 1024) * 1024 * 1024
    storage_buffer    = getattr(_cfg, "STORAGE_BUFFER", 5 * 1024 * 1024 * 1024)

    free_internal, pct_internal = _balanced_reading("Internal shared storage")
    free_sd,       pct_sd       = _balanced_reading("SD card")

    internal_ok = (free_internal != -1 and free_internal - file_size >= internal_reserve)
    sd_ok       = (free_sd != -1 and free_sd - file_size >= storage_buffer)

    tag = filename or f"{file_size // 1024} KB file"
    if internal_ok and sd_ok:
        if pct_internal == -1.0 or pct_sd == -1.0:
            chosen = "Internal shared storage" if pct_internal != -1.0 else "SD card"
        else:
            chosen = "Internal shared storage" if pct_internal <= pct_sd else "SD card"
        log.info(f"  [Router/Balanced] {tag} -> {chosen} "
                 f"(internal={pct_internal:.0f}% used, sd={pct_sd:.0f}% used)")
        return chosen
    if internal_ok:
        log.info(f"  [Router/Balanced] {tag} -> Internal shared storage (SD card full/unreachable)")
        return "Internal shared storage"
    if sd_ok:
        log.info(f"  [Router/Balanced] {tag} -> SD card (Internal reserve would be violated)")
        return "SD card"
    raise RuntimeError(
        f"No storage has room for {tag} "
        f"(internal free={free_internal}, sd free={free_sd})"
    )


# REV 14.4 — free-space cache to avoid a `df` round-trip on every file
# in a bulk transfer.  Keyed on storage name; value is (bytes_free, t_fetched).
_FREE_SPACE_CACHE: dict = {}


def _cached_free_space(storage_name: str) -> int:
    """Cached wrapper around get_device_free_space, respecting
    config.STORAGE_FREE_SPACE_CACHE_SECONDS (0 disables caching)."""
    try:
        import config as _cfg
        ttl = int(getattr(_cfg, "STORAGE_FREE_SPACE_CACHE_SECONDS", 30))
    except Exception:
        ttl = 30

    import time as _t
    now = _t.time()
    if ttl > 0 and storage_name in _FREE_SPACE_CACHE:
        free, t = _FREE_SPACE_CACHE[storage_name]
        if now - t < ttl:
            return free

    free = get_device_free_space("", storage_name)
    _FREE_SPACE_CACHE[storage_name] = (free, now)
    return free


def invalidate_free_space_cache(storage_name: str | None = None) -> None:
    """Drop cached free-space entries.  Call after a large transfer batch or
    on a transfer failure so the next picker decision reflects reality."""
    if storage_name is None:
        _FREE_SPACE_CACHE.clear()
    else:
        _FREE_SPACE_CACHE.pop(storage_name, None)


# ──────────────────────────────────────────────────────────────────────────
# MK17 Patch 2 — degraded-mode flag for "both storages unknown"
# ──────────────────────────────────────────────────────────────────────────
# Pre-MK17 this case logged WARNING per-file forever, polluting the log
# with hundreds of identical warnings.  MK17 attempts ONE recovery
# (clear cache + reconnect ADB + retry), and on persistent failure flips
# this flag so the per-file warning emits only once.  Subsequent files
# under the same condition route silently to primary, since the operator
# has already been told.  The flag resets on a successful free-space
# read so transient-network cases recover automatically.
_BOTH_UNKNOWN_RECOVERY_ATTEMPTED = [False]
_BOTH_UNKNOWN_DEGRADED_LOGGED   = [False]


def _reset_both_unknown_state() -> None:
    """Called from run-start to reset session state.  Public so tests can
    invoke it cleanly between scenarios."""
    _BOTH_UNKNOWN_RECOVERY_ATTEMPTED[0] = False
    _BOTH_UNKNOWN_DEGRADED_LOGGED[0]   = False


def _attempt_free_space_recovery(primary_storage: str,
                                 fallback_storage: str) -> tuple[int, int]:
    """
    Recovery sequence for the both-unknown case.  Returns the (primary,
    fallback) free-space values after recovery.  Either or both may
    still be -1 if recovery couldn't complete.

    Steps:
      1. Drop the cache for both storages.
      2. Try `adb reconnect` (no-op if already connected; recovers from
         half-open USB sockets that show as connected but won't respond).
      3. Re-query free space.  We re-call `_cached_free_space` (which
         after invalidation re-fetches from the device).  This routes
         through any test mocks the same way the original Case-1/2
         lookups did, so testability is preserved.
    """
    try:
        invalidate_free_space_cache()
    except Exception:
        pass

    try:
        # `adb reconnect` is the cheapest recovery — it tells the daemon
        # to re-establish the device-side socket without dropping the
        # USB enumeration.  If the daemon itself is dead, this returns
        # an error and we fall through; a separate code path
        # (Rev 14.9.7 P3) handles dead-daemon recovery on push errors.
        _adb_cmd(["reconnect"], timeout=5, check=False)
    except Exception as e:
        log.debug(f"  [Router/Recovery] adb reconnect raised: {e}")

    # Brief pause for the device to re-enumerate before re-querying
    try:
        import time as _t
        _t.sleep(0.5)
    except Exception:
        pass

    new_primary  = _cached_free_space(primary_storage)
    new_fallback = _cached_free_space(fallback_storage)
    return new_primary, new_fallback


def _pick_storage_for_file(file_size: int,
                           filename: str | None = None) -> str:
    """
    Return a storage name that has enough free space for file_size bytes.

    Tries WRITE_STORAGE first, then SD_STORAGE as fallback.  When
    STORAGE_ROUTING_VERBOSE is True, logs the decision at INFO level
    for every call — answers "where did my files go?" without debug digging.

    Raises RuntimeError if neither storage has room.  Callers should catch
    this and either (a) propagate as StorageFullError for graceful shutdown,
    or (b) trigger a pre-sync dedup pass and retry.
    """
    try:
        import config as _cfg
        storage_buffer  = _cfg.STORAGE_BUFFER
        primary_storage = _cfg.WRITE_STORAGE
        fallback_storage = _cfg.SD_STORAGE
        verbose = bool(getattr(_cfg, "STORAGE_ROUTING_VERBOSE", False))
    except ImportError:
        storage_buffer = 500 * 1024 * 1024
        primary_storage = "Internal shared storage"
        fallback_storage = "SD card"
        verbose = False

    needed = file_size + storage_buffer
    tag = filename or f"{file_size // 1024} KB file"

    free_primary = _cached_free_space(primary_storage)
    free_fallback = _cached_free_space(fallback_storage)

    # REV 14.9.1 (F2) — when primary free-space is unknown (-1), don't
    # assume "permissive → primary wins".  In LOG-13 Run 2 the primary
    # (internal) was 100% full, cache returned -1 on some race-y reads,
    # and the router routed every file to the doomed volume.  New rule:
    # if primary is -1 (unknown) but fallback has a known-healthy value,
    # prefer fallback — unknown is riskier than measured.  Only stick with
    # primary-when-unknown if fallback is also unknown (both -1).
    primary_ok = (free_primary >= needed) if free_primary != -1 else False
    primary_unknown = (free_primary == -1)
    fallback_ok = (free_fallback >= needed) if free_fallback != -1 else False
    fallback_unknown = (free_fallback == -1)

    # Case 1: primary has measured room → use it
    if primary_ok:
        if verbose:
            free_str = f"{free_primary // (1024**3)} GB free"
            log.info(f"  [Router] {tag} → {primary_storage} ({free_str})")
        return primary_storage

    # Case 2: primary full/unknown and fallback has measured room → use fallback
    if fallback_ok:
        if verbose:
            primary_str = (
                "unknown" if primary_unknown
                else f"{free_primary // (1024**2)} MB free"
            )
            free_str = f"{free_fallback // (1024**3)} GB free"
            log.info(
                f"  [Router] {tag} → {fallback_storage} ({free_str}) "
                f"[primary {primary_storage} was {primary_str}]"
            )
        return fallback_storage

    # Case 3 (MK17 Patch 2): both unknown → attempt one recovery
    # (clear cache + adb reconnect + re-query).  If it works, fall back
    # through Case 1 / Case 2 above with fresh values.  If it still
    # comes back unknown, log WARNING ONCE per session and silently
    # route to primary thereafter.  Pre-MK17 this case logged a
    # WARNING per-file forever, polluting the log with hundreds of
    # identical lines.
    if primary_unknown and fallback_unknown:
        if not _BOTH_UNKNOWN_RECOVERY_ATTEMPTED[0]:
            _BOTH_UNKNOWN_RECOVERY_ATTEMPTED[0] = True
            log.warning(
                f"  [Router] Both storages unknown for {tag}.  "
                f"Attempting recovery (clearing cache, reconnecting ADB)..."
            )
            new_primary, new_fallback = _attempt_free_space_recovery(
                primary_storage, fallback_storage
            )
            # Re-evaluate with refreshed values
            if new_primary != -1 and new_primary >= needed:
                log.info(
                    f"  [Router/Recovery] Refresh succeeded: {primary_storage} "
                    f"now reports {new_primary // (1024**3)} GB free."
                )
                # Reset degraded flag — transient issue resolved
                _BOTH_UNKNOWN_DEGRADED_LOGGED[0] = False
                _BOTH_UNKNOWN_RECOVERY_ATTEMPTED[0] = False
                return primary_storage
            if new_fallback != -1 and new_fallback >= needed:
                log.info(
                    f"  [Router/Recovery] Refresh succeeded: {fallback_storage} "
                    f"now reports {new_fallback // (1024**3)} GB free."
                )
                _BOTH_UNKNOWN_DEGRADED_LOGGED[0] = False
                _BOTH_UNKNOWN_RECOVERY_ATTEMPTED[0] = False
                return fallback_storage
            # else: still unknown after recovery — fall through to
            # degraded-mode warning below.

        # Surface the degraded mode ONCE per session.
        if not _BOTH_UNKNOWN_DEGRADED_LOGGED[0]:
            _BOTH_UNKNOWN_DEGRADED_LOGGED[0] = True
            log.warning(
                f"  [Router] DEGRADED MODE: both storages still report "
                f"unknown free space after one recovery attempt.  Routing "
                f"to {primary_storage} for the rest of this session "
                f"without further free-space checks.  Subsequent transfers "
                f"may silently fill the device — monitor manually.  "
                f"This warning will not repeat."
            )

        # Track the unknown-free event for the run-summary (MK17 Patch 5).
        # We use a try/except guard to handle the unit-test case where the
        # downloader module isn't imported.  In production, the counter
        # bump is reliable because downloader is always already imported.
        try:
            _dl_mod = sys.modules.get("downloader")
            if _dl_mod is not None and hasattr(_dl_mod, "_XFER_COUNTERS_UNKNOWN_FREE"):
                _dl_mod._XFER_COUNTERS_UNKNOWN_FREE[0] += 1
        except Exception:
            pass

        return primary_storage

    # Case 4: both known but neither has room
    raise RuntimeError(
        f"No storage with enough free space for {tag} "
        f"(need {needed // (1024**2)} MB). "
        f"{primary_storage}: {free_primary // (1024**2)} MB free. "
        f"{fallback_storage}: {free_fallback // (1024**2)} MB free."
    )


def copy_file_to_device(
    src: Path,
    dest_parts: List[str],
    device_name: str,
    storage_name: str,
    overwrite: bool = False,
    device_index=None,  # REV 14.9 (C2) — optional DeviceIndex for in-memory checks
) -> str:
    """
    Push a local file to the device.

    REV 14.8 — contract aligned with mtp_transfer_com.copy_file_to_device:
    `dest_parts` is the DESTINATION FOLDER (no filename at end). Filename
    comes from src.name.

    REV 14.9 — adds (A1) ADB output parsing, (A3) post-transfer size
    verification, (B3) "Not a directory" self-heal recovery, (C2) optional
    device_index for cache-accelerated existence checks.
    """
    filename     = src.name                          # REV 14.8
    folder_parts = list(dest_parts)                  # REV 14.8
    storage_path = _get_storage_path(storage_name)
    device_folder = storage_path + "/" + "/".join(folder_parts) if folder_parts else storage_path
    remote_path   = device_folder + "/" + filename

    # REV 14.9 (C2) — prefer index-aware mkdir if available; falls back to
    # ADB probe when index absent or path not yet known.
    _mkdir_p(device_folder, device_index=device_index)

    # REV 14.9 (C2) — index-first existence check, with verified size
    # comparison when the index holds size metadata.
    if not overwrite:
        if device_index is not None and device_index.is_file(remote_path):
            try:
                local_size = Path(src).stat().st_size
                indexed_size = device_index.file_size(remote_path)
                if indexed_size is not None and indexed_size == local_size:
                    log.debug(f"  [ADB] Skip (index hit, size match): {filename}")
                    return storage_name
            except Exception:
                pass  # fall through to ADB probe below
        if _exists(remote_path):
            log.debug(f"  [ADB] Skip (exists): {filename} on {storage_name}")
            return storage_name

    # 2026-09-07 — SAFE OVERWRITE. Previously an overwrite did `_rm_f(remote)`
    # up-front, then pushed; a truncated/failed push during ADB instability
    # then left NOTHING (the KLINK favorited-FLAC data-loss incident). Now the
    # existing file is kept untouched: we push to a sidecar temp, let
    # _attempt_push_with_recovery fully verify it, and only then swap it in
    # with an on-device `mv`. A failed push just leaves a stale sidecar (and
    # the original still in place).
    swap_from = None
    push_target = remote_path
    if overwrite:
        if _exists(remote_path):
            push_target = remote_path + ".__ovwtmp"
            _rm_f(push_target)          # clear any stale sidecar from a prior fail
            swap_from = push_target
        elif device_index is not None:
            device_index.remove(remote_path)

    # Avoid shell quoting issues by using an ASCII-safe local name for push
    safe_name = re.sub(r'[^\w\-.]', '_', filename)
    own_tmp = False
    push_src = Path(src)

    if safe_name != filename:
        tmp_src = push_src.parent / safe_name
        shutil.copy2(push_src, tmp_src)
        push_src = tmp_src
        own_tmp = True

    try:
        # REV 14.9 (B3) — up to 2 attempts: if the first push fails with
        # "Not a directory", self-heal by removing the blocking path and
        # retry once.  Legacy corruption from pre-14.8 runs left files
        # sitting where album folders need to be (LOG-13, 177/177 failures).
        _attempt_push_with_recovery(
            push_src, push_target, filename, device_folder, src,
            device_index=device_index,
        )
    except Exception:
        if swap_from:
            _rm_f(swap_from)   # ditch the failed partial — original is intact
        raise
    finally:
        if own_tmp and push_src.exists():
            push_src.unlink()

    if swap_from:
        # verified replacement is on the device — swap it into place
        _rm_f(remote_path)
        if not _mv(swap_from, remote_path):
            _mv(swap_from, remote_path)   # one retry; mv is same-fs metadata op
        if device_index is not None:
            device_index.remove(remote_path)

    log.debug(f"  [ADB] Pushed {filename} → {storage_name}")
    # REV 14.9 (C2) — update index on successful transfer.
    if device_index is not None:
        try:
            device_index.mark_file(remote_path, size=Path(src).stat().st_size)
        except Exception:
            device_index.mark_file(remote_path)
    return storage_name


def _attempt_push_with_recovery(
    push_src: "Path", remote_path: str, filename: str,
    device_folder: str, original_src: "Path",
    device_index=None,
) -> None:
    """
    REV 14.9 — Run adb push with:
      * A1 output parsing (trust "N file(s) pushed" over exit code)
      * A3 post-transfer size verification via stat -c %s
      * B3 "Not a directory" self-heal: if ADB reports a file-blocking-folder
        condition on the remote path, remove the blocker and retry once.

    Raises RuntimeError on any definitive failure after recovery.
    """
    attempts = 0
    last_err = None

    while attempts < 2:
        attempts += 1

        cmd = [_ADB_EXE]
        if ADB_SERIAL:
            cmd += ["-s", ADB_SERIAL]
        cmd += ["push", str(push_src), remote_path]

        # MK17 Patch 12 — adaptive timeout.  Pre-MK17 used a fixed
        # ADB_TIMEOUT=120s for every push; a 4MB MP3 doesn't need that
        # long, and a 50MB FLAC over slow USB might.  Now derived from
        # file size + format (FLAC files get a 1.5× format multiplier).
        try:
            import adaptive_timeout as _at
            _src_path_obj = push_src if hasattr(push_src, "stat") else None
            _src_size = None
            try:
                if _src_path_obj is None:
                    import pathlib as _pl
                    _src_path_obj = _pl.Path(str(push_src))
                _src_size = _src_path_obj.stat().st_size
            except Exception:
                pass
            _ext = ""
            if "." in filename:
                _ext = "." + filename.rsplit(".", 1)[-1]
            _push_timeout = _at.adb_push_timeout(size_bytes=_src_size, ext=_ext)
        except Exception:
            _push_timeout = ADB_TIMEOUT  # fallback to legacy fixed value

        with _device_io_lock():
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",  # Rev 14.9.6 U1
                timeout=_push_timeout, check=False,
            )
        pushed_ok, push_warning, _pushed_bytes = _parse_adb_push_output(
            result.stdout or "", result.stderr or ""
        )

        if push_warning:
            log.warning(f"  [ADB] push warning for {filename!r}: {push_warning}")

        if not pushed_ok:
            stderr_text = (result.stderr or "").lower()

            # REV 14.9 (B3) — self-heal "Not a directory" by removing the
            # blocker.  This is the legacy corruption pattern from LOG-13.
            if attempts == 1 and "not a directory" in stderr_text:
                log.warning(
                    f"  [ADB/Recovery] 'Not a directory' on push of {filename!r}. "
                    f"Probing parent path to identify blocker..."
                )
                # Walk up the remote path to find the component that is a FILE
                blocker = _find_file_blocking_path(remote_path, device_folder)
                if blocker:
                    log.warning(
                        f"  [ADB/Recovery] Removing blocking file: {blocker}  "
                        f"(legacy pre-14.9 corruption)"
                    )
                    _rm_f(blocker)
                    if device_index is not None:
                        device_index.remove(blocker)
                    # Recreate the directory chain after removing blocker
                    _mkdir_p(device_folder, device_index=device_index)
                    continue  # retry the push
                else:
                    log.warning(
                        f"  [ADB/Recovery] Could not identify a file blocker "
                        f"under {device_folder!r}; falling through."
                    )

            # Rev 14.9.7 (P3) — self-heal "daemon not running" / "cannot
            # connect to daemon" with an explicit start-server + retry.
            # LOG_16_1 captured this pattern 5× over a 13-hour run: ADB
            # daemon dies, the push command implicitly tries to start a new
            # one but the loopback socket isn't ready before timeout.  The
            # transport falls back to COM in the outer layer, but COM is
            # slower and adds its own headaches.  An explicit `adb
            # start-server` after a brief wait usually recovers cleanly.
            if attempts == 1 and (
                "daemon not running" in stderr_text
                or "cannot connect to daemon" in stderr_text
                or "failed to get feature set" in stderr_text
            ):
                log.warning(
                    f"  [ADB/Recovery] daemon dead — restarting and retrying "
                    f"push of {filename!r}"
                )
                try:
                    # Force a clean restart: kill any zombie, wait, start fresh.
                    # Locked as one atomic unit (not two separate _device_io_lock
                    # calls) — kill-server aborts any OTHER process's in-flight
                    # adb command outright, so nothing else may be mid-command
                    # anywhere in this kill+restart window.
                    with _device_io_lock():
                        subprocess.run(
                            [_ADB_EXE, "kill-server"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=5, check=False,
                        )
                        time.sleep(1.0)  # let port free up
                        subprocess.run(
                            [_ADB_EXE, "start-server"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=10, check=False,
                        )
                        time.sleep(0.5)  # let server settle before push
                    continue  # retry the push (attempts goes to 2)
                except subprocess.TimeoutExpired:
                    log.warning(
                        "  [ADB/Recovery] adb start-server timed out; "
                        "falling through to COM fallback"
                    )

            # No recovery available — fail definitively
            stderr_tail = (result.stderr or "").strip()[-500:]
            stdout_tail = (result.stdout or "").strip()[-500:]
            last_err = RuntimeError(
                f"adb push failed (rc={result.returncode}) for "
                f"{push_src.name!r} -> {remote_path!r}: "
                f"stderr={stderr_tail!r} stdout={stdout_tail!r}"
            )
            raise last_err

        # REV 14.9 (A3) — verify
        if getattr(_cfg(), "VERIFY_TRANSFERS_ENABLED", True):
            local_size = Path(original_src).stat().st_size
            ok, reason = _verify_remote_size(remote_path, local_size)

            # 2026-09-06 — a truncated push during ADB instability used to be
            # fatal right here: remove the partial + raise. For an
            # overwrite=True caller (walkman_cover_fill / walkman_year_backfill
            # rewrite in place) the ORIGINAL was already deleted above, so a
            # transient truncation meant PERMANENT loss of the file — a
            # favorited FLAC in the incident that prompted this. Truncation is
            # almost always transient, so re-push a few times before the
            # destructive give-up.
            if not ok:
                for _vr in range(1, 4):
                    log.warning(
                        f"  [ADB] Verification failed for {filename!r}: {reason}. "
                        f"Re-pushing (verify-retry {_vr}/3)."
                    )
                    _rm_f(remote_path)
                    time.sleep(1.5)
                    try:
                        with _device_io_lock():
                            _vres = subprocess.run(
                                cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=_push_timeout, check=False,
                            )
                        _vok, _, _ = _parse_adb_push_output(
                            _vres.stdout or "", _vres.stderr or ""
                        )
                    except Exception as _ve:
                        log.warning(f"  [ADB] verify-retry push errored: {_ve}")
                        _vok = False
                    if _vok:
                        ok, reason = _verify_remote_size(remote_path, local_size)
                        if ok:
                            log.info(
                                f"  [ADB] {filename!r} verified after "
                                f"verify-retry {_vr}."
                            )
                            break

            if not ok:
                log.warning(
                    f"  [ADB] Verification failed for {filename!r}: "
                    f"{reason}.  Removing corrupt remote file (retries exhausted)."
                )
                _rm_f(remote_path)
                if device_index is not None:
                    device_index.remove(remote_path)
                # MK17 Patch 5: track verify failure for run-summary
                try:
                    _dl_mod = sys.modules.get("downloader")
                    if _dl_mod is not None and hasattr(_dl_mod, "_XFER_COUNTERS_VERIFY_FAIL"):
                        _dl_mod._XFER_COUNTERS_VERIFY_FAIL[0] += 1
                except Exception:
                    pass
                raise RuntimeError(
                    f"Post-transfer verification failed for {filename!r}: {reason}"
                )

        return  # success


def _find_file_blocking_path(remote_path: str, device_folder: str) -> str | None:
    """
    REV 14.9 (B3 helper) — Walk up from device_folder checking whether any
    path component is a FILE instead of a DIRECTORY.  Returns the first
    blocking path found, or None.
    """
    # Check the folder itself first (most common case: /Music/Album is a file)
    parts = device_folder.rstrip("/").split("/")
    for i in range(len(parts), 1, -1):  # longest path first
        candidate = "/".join(parts[:i])
        if not candidate:
            continue
        # Is this path a file?
        try:
            out = _adb_cmd(
                ["shell", "sh", "-c",
                 f"if [ -f {_shell_quote(candidate)} ]; then echo FILE; "
                 f"elif [ -d {_shell_quote(candidate)} ]; then echo DIR; "
                 f"else echo NONE; fi"],
                timeout=10, check=False,
            )
            out = (out or "").strip()
            if out == "FILE":
                return candidate
        except Exception:
            continue
    return None


def _verify_pulled_size(remote_path: str, dest_local: Path) -> bool:
    """
    Post-pull verification (the pull-direction counterpart to
    _verify_remote_size, which only checks the push direction). `adb pull`
    exiting 0 does not guarantee a byte-complete transfer — confirmed live
    (2026-08-29) on this project's own ad-hoc device->PC move: a flaky
    USB/MTP session can hand back a truncated local file while adb still
    reports success, and nothing downstream (dedup Pass 3's fallback full
    pull, or anything else calling read_file_from_device) was catching it.
    Returns True iff the local file's size matches the device's real size.
    """
    try:
        local_size = dest_local.stat().st_size
    except OSError:
        return False
    try:
        stat_out = _adb_cmd(
            ["shell", "stat", "-c", "%s", _shell_quote(remote_path)],
            timeout=30, check=False,
        )
        remote_size = int((stat_out or "").strip())
    except (ValueError, subprocess.SubprocessError):
        # Can't confirm the remote size — don't fail the pull over a stat
        # hiccup, but don't silently trust it either; caller sees this via
        # the return value being based on local_size > 0 only.
        return local_size > 0
    return local_size == remote_size


def read_file_from_device(
    src_parts: List[str],
    filename: str,
    dest_local: Path,
    device_name: str,
    storage_name: str,
) -> bool:
    """Pull a file from the device to a local path. Returns True on success
    AND on the pulled local file matching the device's real size — a clean
    adb exit code alone is not treated as sufficient (see
    _verify_pulled_size)."""
    storage_path  = _get_storage_path(storage_name)
    device_folder = storage_path + "/" + "/".join(src_parts) if src_parts else storage_path
    remote_path   = device_folder + "/" + filename

    dest_local.parent.mkdir(parents=True, exist_ok=True)

    try:
        _adb_cmd(["pull", remote_path, str(dest_local)], check=True)
        if _verify_pulled_size(remote_path, dest_local):
            return True
        log.warning(
            f"  [ADB] Pulled {filename} from {storage_name} but size "
            f"mismatch vs device — treating as a failed pull, not a "
            f"partial success."
        )
        dest_local.unlink(missing_ok=True)
    except subprocess.CalledProcessError:
        pass

    # Walkman MTP strips extensions — try stem only
    stem = Path(filename).stem
    remote_stem = device_folder + "/" + stem
    try:
        _adb_cmd(["pull", remote_stem, str(dest_local)], check=True)
        if _verify_pulled_size(remote_stem, dest_local):
            return True
        log.warning(
            f"  [ADB] Pulled {filename} (stem fallback) from {storage_name} "
            f"but size mismatch vs device — treating as a failed pull."
        )
        dest_local.unlink(missing_ok=True)
        return False
    except subprocess.CalledProcessError:
        log.warning(f"  [ADB] Failed to pull {filename} from {storage_name}")
        return False


def read_file_header_from_device(
    src_parts: List[str],
    filename: str,
    dest_local: Path,
    device_name: str,
    storage_name: str,
    max_bytes: int = 1_500_000,
) -> bool:
    """
    Pull only the first `max_bytes` of a device file — enough to cover
    ID3v2 / FLAC-STREAMINFO+VORBIS_COMMENT metadata blocks (all located
    at the START of the file by format spec) for tag-completeness scoring,
    without a full USB transfer of the audio stream. 2026-08-27: added
    because device_metadata_score_lookup() (walkman_dedup.py) was doing a
    complete `adb pull` per candidate just to read embedded tags — for
    20-30MB FLACs that made Pass 3 the dominant cost of a scan-duplicates
    run (observed live: ~15s/file, multi-hour runtime).

    Uses `adb exec-out` + `dd` rather than `adb pull` because pull always
    transfers the whole remote file — there's no partial-pull flag. Raw
    stdout (not `_adb_cmd`, which is text-mode and would corrupt binary
    audio bytes) is streamed straight to a local file handle.

    Callers MUST treat a truncated/unparseable result as expected (not an
    error) and fall back to read_file_from_device() for that file — this
    is a best-effort optimization, not a guarantee of a complete file.
    M4A/MP4 in particular can place its moov atom (and duration) at the
    file's END on non-"faststart" encodes, which this can never capture.
    """
    storage_path  = _get_storage_path(storage_name)
    device_folder = storage_path + "/" + "/".join(src_parts) if src_parts else storage_path
    remote_path   = device_folder + "/" + filename

    dest_local.parent.mkdir(parents=True, exist_ok=True)

    cmd = [_ADB_EXE]
    if ADB_SERIAL:
        cmd += ["-s", ADB_SERIAL]
    block_size = 65536
    count = max(1, (max_bytes + block_size - 1) // block_size)
    dd_cmd = f"dd if={_shell_quote(remote_path)} bs={block_size} count={count} 2>/dev/null"
    cmd += ["exec-out", dd_cmd]

    try:
        with _device_io_lock():
            with open(dest_local, "wb") as fh:
                subprocess.run(
                    cmd, stdout=fh, stderr=subprocess.DEVNULL,
                    timeout=ADB_TIMEOUT, check=False,
                )
        return dest_local.exists() and dest_local.stat().st_size > 0
    except subprocess.TimeoutExpired:
        log.warning(f"  [ADB] header pull timed out for {filename}")
        return False
    except OSError as e:
        log.debug(f"  [ADB] header pull failed for {filename}: {e}")
        return False


def write_text_file_to_device(
    content: str,
    dest_parts: List[str],
    filename: str,
    device_name: str,
    storage_name: str,
) -> None:
    """Write a UTF-8 text file (e.g. M3U8 playlist) to the device."""
    # BUG FIX 2026-09-04: copy_file_to_device's contract (REV 14.8) is
    # "dest_parts is the destination FOLDER; filename comes from src.name" --
    # this used to call it with `dest_parts + [filename]`, which _navigate()
    # then created as a FOLDER (create=True), with the actual pushed file
    # landing inside it under the tempfile's own random name. Confirmed live:
    # produced 12 stray "<name>.m3u8/tmpXXXXXXXX.m3u8" directories on-device
    # going back to 2026-04-30, each imported by the app as a garbage
    # "tmpXXXXXXXX"-named empty playlist (relative paths inside the file
    # resolve one directory level too deep to find any track). Fix: name the
    # local tempfile itself `filename` so `src.name` is correct and
    # `dest_parts` alone is the folder, per the documented contract.
    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / filename
    try:
        tmp_path.write_text(content, encoding="utf-8")
        copy_file_to_device(tmp_path, dest_parts,
                            device_name, storage_name, overwrite=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def rename_file_on_device(
    device_name: str,
    storage_name: str,
    folder_parts: List[str],
    old_filename: str,
    new_filename: str,
) -> bool:
    """Rename a file on device. Returns True on success."""
    storage_path  = _get_storage_path(storage_name)
    device_folder = storage_path + "/" + "/".join(folder_parts) if folder_parts else storage_path
    old_path = device_folder + "/" + old_filename
    new_path = device_folder + "/" + new_filename
    try:
        _adb_cmd(["shell", "sh", "-c",
                  "mv " + _shell_quote(old_path) + " " + _shell_quote(new_path)],
                 check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def rename_folder_on_device(
    device_name: str,
    storage_name: str,
    parent_parts: List[str],
    old_name: str,
    new_name: str,
) -> bool:
    """Rename a folder on device. Returns True on success."""
    storage_path = _get_storage_path(storage_name)
    parent_path  = storage_path + "/" + "/".join(parent_parts) if parent_parts else storage_path
    old_path = parent_path + "/" + old_name
    new_path = parent_path + "/" + new_name
    try:
        _adb_cmd(["shell", "sh", "-c",
                  "mv " + _shell_quote(old_path) + " " + _shell_quote(new_path)],
                 check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def delete_empty_device_folders(
    device_name: str,
    storage_name: str,
    parent_parts: List[str],
) -> int:
    """
    Recursively delete empty folders under parent_parts.
    Returns 0 (count not tracked; caller can ignore return value).
    """
    storage_path = _get_storage_path(storage_name)
    root_path    = storage_path + "/" + "/".join(parent_parts) if parent_parts else storage_path

    if not _exists(root_path):
        return 0

    # Use sh -c so the compound command is parsed by the device shell, not adb
    cmd = f'find "{root_path}" -type d -depth -empty -exec rmdir {{}} \\;'
    _adb_cmd(["shell", "sh", "-c", cmd], check=False)
    return 0


def scan_device_audio(
    device_name: str,
    storage_roots: List[Tuple[str, str]],
) -> List[dict]:
    """
    Enumerate all audio files across all storage roots.
    Returns list of dicts: {filename, ext, parts, storage}.
    """
    results = []
    audio_exts = [".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".opus", ".wma", ".dsf", ".dff"]

    for storage_name, subfolder in storage_roots:
        try:
            storage_path = _get_storage_path(storage_name)
        except RuntimeError as e:
            log.debug(f"  [ADB Scan] Skipping {storage_name}: {e}")
            continue

        # FIX 18: when subfolder is empty (root scan) avoid a trailing '/'
        # in the root path and a leading '' element in folder_parts.
        # storage_path + '/' + '' = storage_path + '/' which is fine for find
        # but the len(root) strip would produce rel starting with '/' → parts[0]=''.
        root = (storage_path + "/" + subfolder) if subfolder else storage_path
        root_prefix = root.rstrip("/") + "/"  # canonical prefix for line.startswith
        if not _exists(root):
            log.debug(f"  [ADB Scan] {storage_name}/{subfolder or '(root)'} not found, skipping")
            continue

        # 2026-08-20: was `["shell", "sh", "-c", cmd]` with the whole find
        # command (path + -type f + \( -name X -o -name Y \) grouping) built
        # as ONE string. Confirmed live, definitively, that this specific
        # sh -c wrapping pattern breaks for this command REGARDLESS of quote
        # style (tested raw double quotes, _shell_quote()'s single quotes,
        # and no quoting at all — all three produced the identical broken
        # result: `find` silently ran as if invoked with zero arguments,
        # defaulting to an unfiltered `find .` from the shell's cwd, listing
        # relative `./...` paths instead of the requested absolute root).
        # Calling `find` directly via separate argv elements (no `sh -c`
        # wrapper at all) was tested live and works correctly — real,
        # correct absolute paths, immediately. `find` accepts a path
        # positional + flags fine without any shell-level string
        # interpolation; the `\( -o \)` OR-grouping was the only reason a
        # shell was involved at all, so that's replaced with a plain
        # `-type f` scan + extension filtering done here in Python instead.
        # This made ADB-based device audio scanning (audit.py's
        # device-enumeration step, whenever ADB is the primary transport)
        # silently return 0 files even with thousands of real tracks
        # present — no exception raised, so the ADB->COM fallback in
        # mtp_transfer.py never triggered either.
        out = _adb_cmd(["shell", "find", root, "-type", "f"], timeout=ADB_TIMEOUT * 2)

        found = 0
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith(root_prefix):
                continue
            rel = line[len(root_prefix):]   # guaranteed no leading '/'
            parts = rel.split("/")
            filename = parts[-1]
            ext = Path(filename).suffix.lower()
            if ext not in audio_exts:
                continue
            # FIX 18: build folder_parts cleanly — never include a leading empty string.
            # subfolder may be '' (root), 'Music', or 'Downloads'.
            raw_folder_parts = ([subfolder] if subfolder else []) + parts[:-1]
            folder_parts = [p for p in raw_folder_parts if p]  # strip any empty segments
            results.append({
                "filename": filename,
                "ext": ext,
                "parts": folder_parts,
                "storage": storage_name,
            })
            found += 1

        log.info(f"  [ADB Scan] {storage_name}/{subfolder}: {found} audio file(s) found")

    return results


def log_health_check(label: str) -> bool:
    """Log connection status and free space. Returns True if device is reachable."""
    if not device_is_connected(""):
        log.warning(f"  [{label}] Device not reachable via adb.")
        return False
    try:
        free = get_device_free_space("", "Internal shared storage")
        if free >= 0:
            log.info(f"  [{label}] Internal free: {free // (1024 ** 2)} MB")
    except Exception as e:
        log.warning(f"  [{label}] Could not query free space: {e}")
    return True


def rename_mojibake_folders(device_name: str, storage_roots: list) -> None:
    """
    FIX B4 (ADB path): Scan Music folders and rename mojibake folder names via
    'adb shell mv'.  e.g. "AminÃ©" → "Aminé"
    """
    for storage_name, subfolder in storage_roots:
        try:
            base = _get_storage_path(storage_name)
            root = f"{base}/{subfolder}" if subfolder else base
            if not _exists(root):
                log.debug(f"  [Mojibake/ADB] {storage_name}/{subfolder} not found, skipping")
                continue
            _adb_rename_mojibake_in_folder(root)
        except Exception as e:
            log.warning(f"  [Mojibake/ADB] Error for {storage_name}/{subfolder}: {e}")


def _adb_rename_mojibake_in_folder(dir_path: str) -> None:
    """Recurse through dir_path on device and rename mojibake-named directories."""
    out = _adb_cmd(["shell", "sh", "-c", "ls -1 " + _shell_quote(dir_path)])
    for name in out.splitlines():
        name = name.strip()
        if not name:
            continue
        full = f"{dir_path}/{name}"
        is_dir = _adb_cmd(
            ["shell", "sh", "-c", f'test -d "{full}" && echo 1 || echo 0']
        ).strip() == "1"
        if not is_dir:
            continue
        try:
            fixed = name.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            fixed = None
        if fixed and fixed != name and len(fixed) >= 2 and any(c.isalpha() for c in fixed):
            new_full = f"{dir_path}/{fixed}"
            log.info(f"  [Mojibake/ADB] mv {name!r} → {fixed!r}")
            _adb_cmd(["shell", "sh", "-c",
                      "mv " + _shell_quote(full) + " " + _shell_quote(new_full)],
                     check=False)
            _adb_rename_mojibake_in_folder(new_full)
        else:
            _adb_rename_mojibake_in_folder(full)


def delete_file_on_device(
    device_name:  str,
    storage_name: str,
    folder_parts: List[str],
    filename:     str,
) -> bool:
    """
    FIX F2 (ADB path): Delete a file on the device via adb shell rm.
    Returns True on success or if file already absent, False on error.
    """
    try:
        storage_path  = _get_storage_path(storage_name)
        device_folder = storage_path + "/" + "/".join(folder_parts) if folder_parts else storage_path
        remote_path   = device_folder + "/" + filename
        if not _exists(remote_path):
            stem = Path(filename).stem
            remote_stem = device_folder + "/" + stem
            if not _exists(remote_stem):
                log.debug(f"  [ADB Delete] Not found (already absent): {filename}")
                return True
            remote_path = remote_stem
        _rm_f(remote_path)
        log.debug(f"  [ADB Delete] Removed: {remote_path}")
        return True
    except Exception as e:
        log.warning(f"  [ADB Delete] Failed for {filename!r}: {e}")
        return False


# ---------------------------------------------------------------------------
# Compatibility stubs (mirror the mtp_transfer_com interface)
# ---------------------------------------------------------------------------

def _get_storage_root(device_name: str, storage_name: str) -> str:
    """Return the root path string for a storage (no COM object needed in ADB)."""
    return _get_storage_path(storage_name)


def _navigate(folder, parts: List[str], create: bool = True):
    """
    In ADB mode 'folder' is a path string; just join parts and optionally mkdir.
    Returns the resulting path string.
    """
    if isinstance(folder, str):
        path = folder.rstrip("/") + "/" + "/".join(parts)
        if create:
            _mkdir_p(path)
        return path
    raise NotImplementedError("ADB transport does not use COM folder objects")
