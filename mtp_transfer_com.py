"""
mtp_transfer_com.py  —  MTP device I/O via Windows Shell.Application COM
=========================================================================
Original COM implementation, kept as fallback when ADB is unavailable.
mtp_transfer.py wraps this and tries ADB first.

Now includes rename_folder_on_device for the reorganiser.
"""

import os
import re
import sys
import time
import logging
import pathlib
import subprocess
import tempfile
import threading
import unicodedata

import pythoncom
import win32com.client

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 2026-07-18 — subprocess isolation for copy_file_to_device (see
# copy_file_to_device_isolated below). Same rationale + marker convention as
# wpd_transfer.py's scan-subprocess isolation: a thread-based watchdog cannot
# reliably guard a blocked COM call (it can hold the GIL and freeze the
# watchdog thread too) — only a real OS process, killed from outside, can.
# ---------------------------------------------------------------------------
_COPY_RESULT_MARKER = "__COPY_RESULT__:"
_COPY_ERROR_MARKER   = "__COPY_ERROR__:"
_COPY_HEARTBEAT      = "\x00HB\x00"

# 2026-07-19 — same rationale, for the existence-check primitive. Added after
# COMTransport.file_exists() was found to be a new, unguarded raw COM call
# (_navigate + folder.Items()) despite the class's own docstring claiming to
# only reuse hardened primitives — closes that gap the same way.
_EXISTS_RESULT_MARKER = "__EXISTS_RESULT__:"
_EXISTS_ERROR_MARKER  = "__EXISTS_ERROR__:"
_EXISTS_HEARTBEAT     = "\x00HB\x00"

# 2026-07-25 — same rationale, for folder-creation. Added after
# walkman_dedup._ensure_deleted_folder()'s COM fallback was found to be raw,
# unguarded _find_folder_by_name()/NewFolder() COM calls (zero subprocess
# isolation, zero timeout) — confirmed live to hang 90+s on this device.
_ENSURE_FOLDER_RESULT_MARKER = "__ENSURE_FOLDER_RESULT__:"
_ENSURE_FOLDER_ERROR_MARKER  = "__ENSURE_FOLDER_ERROR__:"
_ENSURE_FOLDER_HEARTBEAT     = "\x00HB\x00"

# 2026-07-22 — subprocess isolation for a standalone _verify_com_copy call
# (see verify_com_copy_isolated below). Needed because the deferred pending-
# file recheck (walkman_sync.py Stage 6) calls _verify_com_copy OUTSIDE
# copy_file_to_device_isolated's subprocess, so it has no dual-watchdog
# protection today unlike the copy-time verify call.
_VERIFY_RESULT_MARKER = "__VERIFY_RESULT__:"
_VERIFY_ERROR_MARKER  = "__VERIFY_ERROR__:"
_VERIFY_HEARTBEAT     = "\x00HB\x00"

# 2026-07-22 — prefix used by _verify_com_copy to mark a failure as
# "ambiguous" (WPD/shell cache staleness — file may actually be fine) rather
# than a confirmed corrupt/mismatched write. copy_file_to_device uses this to
# decide whether to delete the remote file or mark it device_status=pending
# for a later recheck instead. See _verify_com_copy's docstring.
_AMBIGUOUS_PREFIX = "AMBIGUOUS_CACHE_STALE: "

# 2026-08-19 — subprocess isolation for rename_file_on_device (see
# rename_file_on_device_isolated below). Found live: COMTransport.rename()'s
# same-folder path (via rename_file_on_device -> raw read_file_from_device +
# copy_file_to_device, both in-process, zero subprocess isolation, zero
# timeout) hung 40+ minutes on this device — the same "blocked COM call holds
# the GIL" class already fixed for the WPD scanner (Task 4) and the isolated
# copy path (Task 6/20), never applied here. Closes that gap the same way.
_RENAME_RESULT_MARKER = "__RENAME_RESULT__:"
_RENAME_ERROR_MARKER  = "__RENAME_ERROR__:"
_RENAME_HEARTBEAT     = "\x00HB\x00"

# 2026-08-19 — same rationale, for the standalone device-to-local read
# primitive used by COMTransport.rename()'s cross-folder branch.
_READ_RESULT_MARKER = "__READ_RESULT__:"
_READ_ERROR_MARKER  = "__READ_ERROR__:"
_READ_HEARTBEAT     = "\x00HB\x00"

_scan_progress = [0]  # reset by scan_device_audio before each full scan

_THIS_PC        = 17

# P9 CTO fix: Serial MTP operations for soft-delete
# CTO: "MTP: only one operation at a time is reliable"
# Sony Walkman NW-ZX500 + Android 9 FUSE — concurrent MTP writes cause silent failures.
# 2026-07-22: confirmed dead (never .acquire()'d anywhere) and, being a
# threading.Lock, process-local — it cannot coordinate concurrent OS
# processes/sync sessions, which is the actual contention risk. Left
# out-of-scope for the WPD-verify hardening pass (see task_plan.md); a real
# fix would need a cross-process lock, e.g. reusing sync_state.sync_intent's
# owner_id/lease_epoch mechanism.
import threading as _p9_threading
_MTP_DEDUP_LOCK = _p9_threading.Lock()


COPY_TIMEOUT_S  = 600
STORAGE_RETRY_S = 30


# REV 14.8 — Per-worker CopyHere circuit breaker
COPYHERE_CIRCUIT_ENABLED   = getattr(config, "COPYHERE_CIRCUIT_ENABLED", True)
COPYHERE_CIRCUIT_THRESHOLD = getattr(config, "COPYHERE_CIRCUIT_THRESHOLD", 3)

_copyhere_tls = threading.local()

def _copyhere_record_success() -> None:
    if not COPYHERE_CIRCUIT_ENABLED:
        return
    _copyhere_tls.consecutive_timeouts = 0

def _copyhere_record_timeout() -> int:
    if not COPYHERE_CIRCUIT_ENABLED:
        return 0
    n = getattr(_copyhere_tls, "consecutive_timeouts", 0) + 1
    _copyhere_tls.consecutive_timeouts = n
    return n

def _copyhere_is_disabled() -> bool:
    if not COPYHERE_CIRCUIT_ENABLED:
        return False
    return getattr(_copyhere_tls, "consecutive_timeouts", 0) >= COPYHERE_CIRCUIT_THRESHOLD

def _copyhere_current_count() -> int:
    return getattr(_copyhere_tls, "consecutive_timeouts", 0)


_AUDIO_EXTS = {".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a"}

# ---------------------------------------------------------------------------
# Sony Walkman MTP extension workaround
# ---------------------------------------------------------------------------
# The Windows Shell COM layer strips file extensions from item.Name on Sony
# Walkman NW-ZX500.  Use item.Type instead to identify audio files.

_TYPE_TO_EXT: dict[str, str] = {
    "mp3 file":        ".mp3",
    "flac file":       ".flac",
    "aac file":        ".aac",
    "m4a file":        ".m4a",
    "ogg file":        ".ogg",
    "opus file":       ".opus",
    "wav file":        ".wav",
    "wma file":        ".wma",
    "aiff file":       ".aiff",
    "ape file":        ".ape",
    "wv file":         ".wv",
    "mpeg audio file": ".mp3",
    "audio file":      ".mp3",
    "flac audio":      ".flac",
    "ogg vorbis":      ".ogg",
}


def _item_audio_ext(item) -> str | None:
    """Return the audio extension for a Shell FolderItem, or None."""
    try:
        item_type = (item.Type or "").lower().strip()
        if item_type in _TYPE_TO_EXT:
            return _TYPE_TO_EXT[item_type]
        for key, ext in _TYPE_TO_EXT.items():
            if key in item_type:
                return ext
    except Exception:
        pass
    try:
        suffix = pathlib.Path(item.Name).suffix.lower()
        if suffix in _AUDIO_EXTS:
            return suffix
    except Exception:
        pass
    return None


class StorageFullError(RuntimeError):
    """Raised when both Internal shared storage and SD card are full/absent."""
    pass


# ---------------------------------------------------------------------------
# COM helpers
# ---------------------------------------------------------------------------

def _coinit():
    """Initialise COM for the calling thread. Safe to call multiple times."""
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass


def _shell():
    return win32com.client.Dispatch("Shell.Application")


def _get_this_pc():
    """
    Returns the folder object for This PC that correctly enumerates MTP devices.

    CRITICAL: shell.Namespace(17).Items() does NOT return MTP devices.
    The correct traversal is shell.Namespace(17).self.GetFolder.Items().
    """
    return _shell().Namespace(_THIS_PC).Self.GetFolder


def _get_device_root(device_name: str):
    """
    Finds the device under This PC and returns its folder object.
    Tries exact match, partial match, then portable-device-type fallback.
    """
    this_pc = _get_this_pc()
    try:
        items = list(this_pc.Items())
    except Exception as e:
        raise RuntimeError(
            f"Could not enumerate This PC items: {e}. "
            "Ensure the Walkman is connected in File Transfer (MTP) mode."
        )

    log.debug(f"_get_device_root: {len(items)} item(s) under This PC:")
    portable_candidate = None

    for item in items:
        try:
            name  = item.Name or ""
            itype = item.Type or ""
        except Exception:
            continue

        log.debug(f"  Name={name!r}  Type={itype!r}  IsFolder={item.IsFolder}")

        name_lc = name.lower()
        dev_lc  = device_name.lower()

        if name_lc == dev_lc:
            log.debug(f"  -> Matched by exact name: {name!r}")
            return item.GetFolder

        if dev_lc in name_lc or name_lc in dev_lc:
            log.debug(f"  -> Matched by partial name: {name!r}")
            return item.GetFolder

        itype_lc = itype.lower()
        if "portable" in itype_lc or "phone" in itype_lc or "media" in itype_lc:
            portable_candidate = item

    if portable_candidate is not None:
        log.warning(
            f"Device '{device_name}' not found by name. "
            f"Using portable device fallback: {portable_candidate.Name!r}. "
            f"Update DEVICE_NAME in config.py to match exactly."
        )
        return portable_candidate.GetFolder

    visible = [getattr(i, 'Name', '?') for i in items]
    raise RuntimeError(
        f"Device '{device_name}' not found under This PC. "
        f"Items visible: {visible}. "
        f"Connect the Walkman via USB in File Transfer (MTP) mode."
    )


def _get_storage_root(device_name: str, storage_name: str):
    """
    Returns the named storage root folder object.
    Retries for STORAGE_RETRY_S seconds to handle MTP lazy enumeration.
    """
    deadline   = time.time() + STORAGE_RETRY_S
    last_names: list[str] = []

    while time.time() < deadline:
        try:
            device = _get_device_root(device_name)
            items  = list(device.Items())
            names  = []
            for item in items:
                if item.IsFolder:
                    names.append(item.Name)
                    if item.Name.lower() == storage_name.lower():
                        return item.GetFolder
            last_names = names
        except Exception as e:
            log.debug(f"_get_storage_root retry: {e}")
        time.sleep(0.5)

    raise RuntimeError(
        f"Storage '{storage_name}' not found on device '{device_name}' "
        f"after {STORAGE_RETRY_S}s. "
        f"Storage roots visible: {last_names}. "
        f"Expected 'Internal shared storage' and/or 'SD card'."
    )


def _navigate(folder, parts: list[str], create: bool = True,
              device_index=None, storage_name: str | None = None,
              progress_cb=None):
    """
    Navigate (and optionally create) a folder path inside a COM folder.

    REV 14.9 (C2) — When device_index is supplied:
      * Skip COM enumeration if the index already confirms the folder
        exists at the expected absolute path (one enumeration call per
        un-indexed level instead of per level).
      * Before NewFolder(), check the index for a file-vs-folder blocker
        at the target path; if present, delete the blocker via ADB before
        COM tries to create the directory (prevents the 233 folder-creation
        timeouts seen in LOG-13).

    Fall-back semantics: if device_index is None or doesn't know the path,
    behaviour is identical to pre-14.9.  The index is advisory.
    """
    # Compute the running device-side absolute path as we descend, so we
    # can compare against the index.  The root folder itself has a known
    # absolute path via config + storage_name.
    cur_abs_path = None
    if device_index is not None and storage_name:
        try:
            from adb_transfer import _get_storage_path
            import config as _cfg
            # The caller passes dest_parts *including* MUSIC_ROOT at [0],
            # so we descend from the storage root.  Absolute path prefix
            # is /storage/emulated/0/ (or SD equivalent).
            cur_abs_path = _get_storage_path(storage_name).rstrip("/")
        except Exception:
            cur_abs_path = None

    for part in parts:
        # Track the would-be absolute path of this level
        next_abs = None
        if cur_abs_path is not None:
            next_abs = cur_abs_path + "/" + part

        # REV 14.9 (C2) fast path — if index confirms this is already a
        # directory on the device, we still need the COM folder object
        # to continue navigation, so we enumerate once; but we skip the
        # polling loop that assumes we just created it.
        index_says_dir = (
            next_abs is not None
            and device_index is not None
            and device_index.is_dir(next_abs)
        )

        # REV 14.9 (C2) blocker — if index says a file occupies this name,
        # delete it before attempting NewFolder.  Pre-14.9 COM would call
        # NewFolder, the Walkman would silently fail, then poll 30s for the
        # folder to appear (it never does), then retry.  LOG-13 captured
        # 233 of these.
        if (
            create
            and next_abs is not None
            and device_index is not None
            and device_index.is_file(next_abs)
        ):
            log.warning(f"  [COM/Recovery] Removing file blocking folder create: {next_abs}")
            try:
                from adb_transfer import _adb_cmd, _shell_quote
                _adb_cmd(["shell", "sh", "-c", "rm -f " + _shell_quote(next_abs)],
                         timeout=30, check=False)
                device_index.remove(next_abs)
            except Exception as e:
                log.warning(f"  [COM/Recovery] Could not remove blocker via ADB: {e}")

        found = None
        if progress_cb is not None:
            progress_cb()  # about to make a potentially-blocking COM call
        try:
            items = list(folder.Items())
        except Exception as e:
            raise RuntimeError(f"Cannot enumerate folder looking for '{part}': {e}")

        for item in items:
            try:
                if item.Name.lower() == part.lower() and item.IsFolder:
                    found = item.GetFolder
                    break
            except Exception:
                continue

        if found:
            folder = found
            if next_abs is not None and device_index is not None:
                device_index.mark_dir(next_abs)
            cur_abs_path = next_abs
            continue

        if not create:
            avail = []
            for item in items:
                try: avail.append(item.Name)
                except Exception: pass
            log.debug(f"_navigate: '{part}' not found. Available: {avail}")
            raise RuntimeError(f"Folder '{part}' not found on device. Available: {avail}")

        # REV 14.9 (C2) — If the index said it's a directory but COM's
        # enumerator didn't return it, that's MTP caching lag.  Still call
        # NewFolder (idempotent on a pre-existing directory), but don't
        # worry as much about polling timeouts.
        # FIX: increased folder-appear wait from 5s to 15s.
        # NW-ZX507 MTP is slow to expose newly-created folders, especially
        # after rapid sequential transfers. 5s caused spurious failures on
        # large album folders; 15s + one retry eliminates them.
        folder.NewFolder(part)
        found = None
        for _attempt in range(2):  # try up to twice (immediate + 1 retry)
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    for item in list(folder.Items()):
                        if item.Name.lower() == part.lower() and item.IsFolder:
                            found = item.GetFolder
                            break
                except Exception:
                    pass
                if found:
                    break
                time.sleep(0.5)
            if found:
                break
            if _attempt == 0:
                log.debug(f"  [COM] Folder '{part}' not visible after 15s — retrying NewFolder")
                try:
                    folder.NewFolder(part)  # retry: folder may have silently failed
                except Exception:
                    pass  # ignore if it now exists

        if not found:
            # REV 14.9 (C2) — if the index says this is already a dir,
            # it's almost certainly an MTP enumeration lag and retrying
            # won't help; log + fail to let caller fall back.  But if the
            # index DOESN'T know, the old error stands.
            if index_says_dir:
                raise RuntimeError(
                    f"COM could not see folder '{part}' but index shows it "
                    f"as a directory (MTP enumeration lag).  Transport will fall back."
                )
            raise RuntimeError(f"Created folder '{part}' but it did not appear within 30s (2 attempts).")
        folder = found
        if next_abs is not None and device_index is not None:
            device_index.mark_dir(next_abs)
        cur_abs_path = next_abs

    return folder


def _wait_for_file(
    folder, filename: str,
    timeout: int = COPY_TIMEOUT_S,
    settling_delay_s: float = 1.5,
) -> bool:
    """
    Poll until a file appears in a COM folder, or timeout expires.
    Matches by full name first, then stem only (Walkman MTP strips extensions).
    """
    # P3 CTO fix: settling delay before first poll
    # Sony Walkman NW-ZX500 needs ~1.5s for MTP metadata to reflect actual size
    import time as _t_settle
    if settling_delay_s > 0:
        _t_settle.sleep(settling_delay_s)

    deadline = time.time() + timeout
    name_lc  = filename.lower()
    stem_lc  = pathlib.Path(filename).stem.lower()
    while time.time() < deadline:
        try:
            for item in list(folder.Items()):
                try:
                    iname = item.Name.lower()
                    if iname == name_lc or iname == stem_lc:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _wait_for_stable_size(
    folder, filename: str, expected_bytes: int,
    timeout: int = COPY_TIMEOUT_S,
    stable_polls: int = 2,
    poll_interval: float = 2.0,
    settling_delay_s: float = 1.5,
    progress_cb=None,
) -> tuple[bool, str]:
    """
    MK18 #2 + MK27 P3 — CopyHere stabilization + 3-condition size check.

    P3 CTO fix: Add `settling_delay_s` before first size poll.
    Evidence: Sony Walkman NW-ZX500 (Android 9 FUSE layer) needs 1-3s after
    CopyHere reports completion before MTP metadata reflects the actual file size.
    Without this delay, the first poll fires immediately and sees size=0.

    MK18: The previous _wait_for_file() returned True the instant the filename
    appeared in the MTP folder enumeration.  On Windows Shell, CopyHere is
    asynchronous: the file node appears in the COM namespace while the
    Shell is still flushing bytes.  This causes zero-byte and truncated
    stubs to be accepted as successful transfers.

    Three conditions must ALL be true before we declare success:
      1. File exists (name appears in folder.Items())
      2. Size is stable: two consecutive polls return the same non-zero value
      3. Size equals source bytes (within 0 bytes — no tolerance for audio)

    If expected_bytes <= 0 (source stat failed), conditions 1+2 only.

    Returns (ok: bool, reason: str).
    """
    # P3 CTO fix: settling delay before first poll
    # Sony Walkman NW-ZX500 needs ~1.5s for MTP metadata to reflect actual size
    import time as _t_settle
    if settling_delay_s > 0:
        _t_settle.sleep(settling_delay_s)

    deadline = time.time() + timeout
    name_lc  = filename.lower()
    stem_lc  = pathlib.Path(filename).stem.lower()
    last_size: int = -1
    stable_count: int = 0

    def _get_size() -> int:
        """Return the remote file size via COM, or -1 if not found."""
        try:
            for item in list(folder.Items()):
                try:
                    iname = item.Name.lower()
                    if iname == name_lc or iname == stem_lc:
                        return int(item.Size)
                except Exception:
                    continue
        except Exception:
            pass
        return -1

    while time.time() < deadline:
        if progress_cb is not None:
            progress_cb()
        size = _get_size()

        if size < 0:
            # Not appeared yet — reset stability counter
            stable_count = 0
            last_size    = -1
            time.sleep(poll_interval)
            continue

        # Condition 2: stability across consecutive polls
        if size == last_size and size > 0:
            stable_count += 1
        else:
            stable_count = 0
            last_size    = size

        if stable_count >= stable_polls:
            # Condition 3: size parity
            if expected_bytes > 0 and size != expected_bytes:
                return False, (
                    f"size mismatch: remote={size}B source={expected_bytes}B "
                    f"(delta={(size - expected_bytes):+d}B)"
                )
            return True, f"ok (size={size}B stable_polls={stable_polls})"

        time.sleep(poll_interval)

    # Timeout — report last known state
    if last_size < 0:
        return False, f"file never appeared in COM folder after {timeout}s"
    return False, (
        f"size never stabilised after {timeout}s "
        f"(last seen {last_size}B, expected {expected_bytes}B)"
    )


# ──────────────────────────────────────────────────────────────────────────
# MK17 Patch 4 — COM verification parity with ADB
# ──────────────────────────────────────────────────────────────────────────
# ADB's copy_file_to_device has had post-transfer size verification since
# Rev 14.9 (A3); COM did not.  Pre-MK17, COM said "copy happened" the
# moment the file appeared in the folder enumeration, even if the
# Windows Shell COM marshaller had only flushed a partial write.  This
# could produce a 0-byte placeholder or a truncated file on the device
# that still passed the existence check.  MK17 closes the parity gap by
# comparing item.Size to the local file size after _wait_for_file
# returns.  On size mismatch, the remote is deleted and a real failure
# is raised so the upper-level transport can retry or fall back.

def _find_remote_item(folder, filename: str):
    """Return the COM item matching *filename* in *folder*, or None."""
    name_lc = filename.lower()
    stem_lc = pathlib.Path(filename).stem.lower()
    try:
        for item in list(folder.Items()):
            try:
                iname = item.Name.lower()
                if iname == name_lc or iname == stem_lc:
                    return item
            except Exception:
                continue
    except Exception:
        pass
    return None


def _adb_cross_check(device_name: str, storage_name: str, dest_parts: list,
                      filename: str, expected_size: int) -> "bool | None":
    """
    Best-effort ADB-backed size confirmation, used only when _verify_com_copy's
    own COM-based check is ambiguous (folder/item unreachable, or size stuck
    at/near 0 after all retries). `adb shell stat` reads the device's real
    filesystem directly, bypassing the WPD driver's shell-namespace cache
    entirely — see _verify_com_copy's docstring for the diagnosis this
    resolves.

    Returns True/False on a real answer, or None if ADB itself isn't
    reachable/usable right now (device offline/unauthorized, adb_transfer
    unavailable, etc.) — callers MUST treat None as "no signal, fall back to
    COM's own ambiguous answer," never as a positive or negative result.
    """
    try:
        import adb_transfer as _adb
    except ImportError:
        return None
    try:
        if not _adb.device_is_connected(device_name):
            return None
        abs_path = _adb._get_storage_path(storage_name) + "/" + "/".join(list(dest_parts) + [filename])
    except Exception:
        return None
    try:
        ok, _reason = _adb._verify_remote_size(abs_path, expected_size)
        return ok
    except Exception:
        return None


def _verify_com_copy(device_name: str, storage_name: str, dest_parts: list,
                     filename: str, expected_size: int,
                     tolerance_bytes: int = 0,
                     retries: int = 5, base_wait_s: float = 3.0,
                     progress_cb=None) -> tuple[bool, str]:
    """
    Verify a COM-transferred file matches the expected size.

    Returns (ok, reason).  ok=True when the file exists and its size
    matches expected_size within tolerance.  ok=False with a human-
    readable reason on any mismatch.

    `tolerance_bytes` is non-zero only for FLAC ↔ MP3 conversions where
    the on-device size may differ slightly from the local source.  Default
    is exact match.

    MK8 Fix (LOG_17_3): Shell.Application exposes MTP files with size=0
    immediately after CopyHere returns, while the Walkman writes the data.
    This caused spurious "remote size is 0 (likely empty placeholder)"
    failures.  Fix: retry up to `retries` times when size is 0.

    2026-07-20 — added progress_cb: found live that this function had zero
    heartbeat instrumentation despite running inside
    copy_file_to_device_isolated's subprocess, unlike _navigate/
    _wait_for_stable_size/CopyHere (hardened 2026-07-18). Confirmed live: a
    real transfer completed correctly (file landed on-device with the right
    content) while THIS function's _find_remote_item call blocked past the
    quiet-timeout with no heartbeat to show it was even running, causing a
    false-failure report — a real transfer got reported as failed/hung
    purely because its verification step wasn't instrumented.

    2026-07-22 — takes (device_name, storage_name, dest_parts) instead of a
    pre-fetched folder object, and re-navigates fresh on EVERY retry
    instead of reusing one folder handle across all attempts. Confirmed
    live: a folder object obtained once (before the transfer, as
    copy_file_to_device's caller previously did) can report size=0 on the
    SAME remote item for 90+ seconds straight with zero change, while ADB
    — a completely independent transport — showed the file was already
    correct within seconds. Windows Shell's MTP folder enumeration is
    cached per-object and does not refresh just by re-calling .Items() on
    the same handle; only a fresh GetFolder navigation picks up current
    device state. This was very likely the real cause of most/all
    "remote size is 0 (likely empty placeholder)" failures logged
    throughout this project's history, not genuine empty-placeholder
    writes.

    2026-07-22 (later same day) — the above fix reduces but does not
    eliminate the problem: the staleness can live in the WPD driver stack
    itself (below anything a fresh COM navigation can see), so re-navigation
    alone cannot always resolve it within a bounded number of retries. Added
    an ADB cross-check (_adb_cross_check) as a last resort when COM's own
    retries are exhausted and the outcome is still ambiguous (folder/item
    unreachable, or size stuck at/near 0) — ADB's `stat` bypasses the WPD
    cache entirely by querying the device's real filesystem. This is NOT
    consulted for a confirmed nonzero size MISMATCH (below): that's a
    different, unambiguous signal (real truncation/corruption), and letting
    ADB override it would mask genuine corruption. When the cross-check
    can't resolve things either (ADB unreachable, or it also disagrees), the
    failure reason is prefixed with _AMBIGUOUS_PREFIX so the caller
    (copy_file_to_device) knows not to delete the file outright — see that
    function's docstring.
    """
    import time as _verify_time

    last_item = None
    last_actual = -1

    for attempt in range(retries + 1):
        if progress_cb is not None:
            progress_cb()
        try:
            fresh_root   = _get_storage_root(device_name, storage_name)
            fresh_folder = _navigate(fresh_root, dest_parts, create=False, progress_cb=progress_cb)
        except Exception as e:
            if attempt < retries:
                _verify_time.sleep(base_wait_s)
                continue
            cross = _adb_cross_check(device_name, storage_name, dest_parts, filename, expected_size)
            if cross:
                return True, f"verified via ADB cross-check (COM folder not navigable): size={expected_size}"
            return False, f"{_AMBIGUOUS_PREFIX}could not re-navigate to verify folder: {e}"
        item = _find_remote_item(fresh_folder, filename)
        if item is None:
            if attempt < retries:
                _verify_time.sleep(base_wait_s)
                continue
            cross = _adb_cross_check(device_name, storage_name, dest_parts, filename, expected_size)
            if cross:
                return True, f"verified via ADB cross-check (remote item not visible to COM): size={expected_size}"
            return False, f"{_AMBIGUOUS_PREFIX}remote item not visible after copy"

        last_item = item
        try:
            actual_size = int(item.Size or 0)
        except (TypeError, ValueError):
            try:
                actual_size = int(str(item.Size).strip())
            except Exception:
                if attempt < retries:
                    _verify_time.sleep(base_wait_s)
                    continue
                cross = _adb_cross_check(device_name, storage_name, dest_parts, filename, expected_size)
                if cross:
                    return True, f"verified via ADB cross-check (COM size unreadable): size={expected_size}"
                return False, f"{_AMBIGUOUS_PREFIX}could not read remote size (raw={item.Size!r})"

        last_actual = actual_size

        if actual_size <= 0:
            if attempt < retries:
                log.debug(
                    f"  [COM/Verify] {filename!r}: size={actual_size} (placeholder), "
                    f"retry {attempt + 1}/{retries} in {base_wait_s:.0f}s"
                )
                _verify_time.sleep(base_wait_s)
                continue
            cross = _adb_cross_check(device_name, storage_name, dest_parts, filename, expected_size)
            if cross:
                return True, f"verified via ADB cross-check (COM size stuck at {actual_size}): size={expected_size}"
            return False, f"{_AMBIGUOUS_PREFIX}remote size is {actual_size} (likely empty placeholder)"

        if expected_size <= 0:
            return True, f"present, size={actual_size} (no expected_size to compare)"

        delta = abs(actual_size - expected_size)
        if delta <= tolerance_bytes:
            return True, f"verified, size={actual_size}"

        # Size is non-zero but wrong — could still be a partial write.
        # Only retry if actual_size is less than expected (incomplete write).
        if actual_size < expected_size and attempt < retries:
            log.debug(
                f"  [COM/Verify] {filename!r}: partial write {actual_size}/{expected_size} bytes, "
                f"retry {attempt + 1}/{retries}"
            )
            _verify_time.sleep(base_wait_s)
            continue

        return False, (
            f"size mismatch: expected {expected_size}, got {actual_size} "
            f"(delta={delta} bytes, tolerance={tolerance_bytes})"
        )

    cross = _adb_cross_check(device_name, storage_name, dest_parts, filename, expected_size)
    if cross:
        return True, f"verified via ADB cross-check (COM size stuck at {last_actual}): size={expected_size}"
    return False, f"{_AMBIGUOUS_PREFIX}remote size is {last_actual} after {retries} retries"


def _delete_remote_after_failed_verify(
    folder,
    filename: str,
    device_name: str = "",
    storage_name: str = "",
    folder_parts: "list[str] | None" = None,
) -> bool:
    """Best-effort delete of a corrupted remote file after verification
    failure.  Returns True if the delete completed, False otherwise.

    Prefers wpd_transfer.delete_file_ps() (PowerShell subprocess, hwnd=0,
    no dialog) when device_name/storage_name/folder_parts are supplied.
    Falls back to InvokeVerb('delete') as last resort; dialog_watcher
    will auto-dismiss the resulting Confirm File Delete dialog.
    """
    # ── Primary: dialog-free PowerShell delete ────────────────────────
    if device_name and storage_name and folder_parts is not None:
        try:
            import wpd_transfer as _wpd
            ok = _wpd.delete_file_ps(
                device_name  = device_name,
                storage_name = storage_name,
                dest_parts   = folder_parts,
                filename     = filename,
            )
            if ok:
                return True
            log.debug(f"  [COM/Verify] PS delete returned False for {filename!r}")
        except Exception as e:
            log.debug(f"  [COM/Verify] PS delete failed ({e}), falling back to InvokeVerb")

    # ── Fallback: InvokeVerb (triggers Confirm File Delete dialog) ────
    item = _find_remote_item(folder, filename)
    if item is None:
        return True  # nothing to delete
    try:
        item.InvokeVerb("delete")
        return True
    except Exception as e:
        log.debug(f"  [COM/Verify] InvokeVerb('delete') failed: {e}")
        return False


def _ascii_safe(name: str) -> str:
    """Convert a filename stem to ASCII-safe for Shell.Application CopyHere."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "track"


# ---------------------------------------------------------------------------
# Storage fallback helper
# ---------------------------------------------------------------------------

def _pick_storage_for_file(file_size: int,
                           filename: str | None = None) -> str:
    """
    Returns the storage name to use for a file of the given size.

    Rev 14.4: `filename` is optional and used only to enrich log lines.
    Behaviour is otherwise unchanged.

    Routing logic:
      1. Query Internal free space.
         - If known and sufficient → use Internal.
         - If known and insufficient → try SD card.
         - If unknown (-1): query both storages and pick the one with more
           free space, preferring SD card when both are unknown.
      2. Query SD free space.
         - If known and sufficient → use SD card.
         - If unknown (-1) → still use SD card (safe: 209 GB available).
      3. Raise StorageFullError only when both free values are known AND
         both are insufficient.

    The old behaviour treated -1 as "infinite Internal free space", which
    silently routed every file to Internal until it was full (6.53 GB gone)
    while the SD card's 209 GB sat completely untouched.
    """
    _coinit()
    needed = file_size + config.STORAGE_BUFFER
    verbose = bool(getattr(config, "STORAGE_ROUTING_VERBOSE", False))
    tag = filename or f"{file_size // 1024} KB file"

    primary_free = get_device_free_space(config.DEVICE_NAME, config.WRITE_STORAGE)
    sd_free      = get_device_free_space(config.DEVICE_NAME, config.SD_STORAGE)

    # Both known — standard routing
    if primary_free != -1 and sd_free != -1:
        if primary_free > needed:
            if verbose:
                log.info(
                    f"  [Router] {tag} → {config.WRITE_STORAGE} "
                    f"({primary_free // (1024**3)} GB free)"
                )
            return config.WRITE_STORAGE
        if sd_free > needed:
            log.info(
                f"  [Router] {tag} → {config.SD_STORAGE} ({sd_free // (1024**3)} GB free) "
                f"[overflow; {config.WRITE_STORAGE} had only "
                f"{primary_free // (1024**2)} MB free]"
            )
            return config.SD_STORAGE
        raise StorageFullError(
            f"Both storages full for {tag}. "
            f"Internal: {primary_free//(1024**2)} MB. "
            f"SD: {sd_free//(1024**2)} MB. "
            f"Need: {needed//(1024**2)} MB."
        )

    # Primary known, SD unknown — trust primary if sufficient, else fall to SD
    if primary_free != -1 and sd_free == -1:
        if primary_free > needed:
            if verbose:
                log.info(
                    f"  [Router] {tag} → {config.WRITE_STORAGE} "
                    f"({primary_free // (1024**3)} GB free; SD unknown)"
                )
            return config.WRITE_STORAGE
        log.info(
            f"  [Router] {tag} → {config.SD_STORAGE} "
            f"[{config.WRITE_STORAGE} had {primary_free // (1024**2)} MB; SD unknown]"
        )
        return config.SD_STORAGE

    # Primary unknown, SD known — use SD when internal can't be confirmed
    if primary_free == -1 and sd_free != -1:
        if sd_free > needed:
            if verbose:
                log.info(
                    f"  [Router] {tag} → {config.SD_STORAGE} "
                    f"({sd_free // (1024**3)} GB free; Internal unknown)"
                )
            return config.SD_STORAGE
        log.warning(
            f"  [Router] {tag}: SD low ({sd_free // (1024**2)} MB) "
            f"and {config.WRITE_STORAGE} unknown — trying {config.WRITE_STORAGE}."
        )
        return config.WRITE_STORAGE

    # Both unknown (-1/-1) — prefer SD card (always larger on NW-ZX507)
    log.warning(
        f"  [Router] {tag}: free space unknown for both storages "
        f"— defaulting to {config.SD_STORAGE} (larger)."
    )
    return config.SD_STORAGE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def device_is_connected(device_name: str) -> bool:
    """Returns True if the named device is visible under This PC via MTP."""
    _coinit()
    try:
        for item in list(_get_this_pc().Items()):
            name_lc = (item.Name or "").lower()
            dev_lc  = device_name.lower()
            if name_lc == dev_lc or dev_lc in name_lc:
                return True
        return False
    except Exception:
        return False


def diagnose_mtp() -> str:
    """
    Diagnostic helper — returns a human-readable string showing what COM sees
    under This PC, to identify the correct device and storage names.
    """
    _coinit()
    lines = ["=== MTP Diagnostic (COM) ==="]
    try:
        this_pc = _get_this_pc()
        items   = list(this_pc.Items())
        lines.append(f"Items under This PC: {len(items)}")
        for i, item in enumerate(items):
            try:
                name  = getattr(item, 'Name', '?')
                itype = getattr(item, 'Type', '?')
                path  = getattr(item, 'Path', '?')
                lines.append(f"  [{i}] Name={name!r}  Type={itype!r}  Path={path!r}")
                if item.IsFolder:
                    try:
                        sub_items = list(item.GetFolder.Items())
                        for j, sub in enumerate(sub_items):
                            sname = getattr(sub, 'Name', '?')
                            stype = getattr(sub, 'Type', '?')
                            lines.append(f"       [{j}] {sname!r}  type={stype!r}")
                    except Exception as e2:
                        lines.append(f"       (could not enumerate subitems: {e2})")
            except Exception as e:
                lines.append(f"  [{i}] ERROR: {e}")
    except Exception as e:
        lines.append(f"ERROR enumerating This PC: {e}")
    return "\n".join(lines)


def list_storage_roots(device_name: str) -> list[str]:
    """Returns the names of all storage roots visible on the device."""
    _coinit()
    try:
        device = _get_device_root(device_name)
        return [item.Name for item in list(device.Items()) if item.IsFolder]
    except Exception:
        return []


def get_device_free_space(device_name: str, storage_name: str) -> int:
    """
    Returns free bytes on a storage root, or -1 if unavailable.

    Four-layer fallback chain:

    1. Shell.Application ExtendedProperty("System.FreeSpace") on the MTP
       storage root object — works on most devices but silently returns None
       on NW-ZX507 (Sony MTP stack doesn't expose FreeSpace on root).

    2. PowerShell WMI Win32_LogicalDisk: enumerates ALL removable drives
       (DriveType=2) with their VolumeName, FreeSpace, Size, and drive letter.
       Matches against storage_name:
         "Internal shared storage" → picks the LARGER removable disk (≈50 GB)
         "SD card"                 → picks the SMALLER removable disk (or 200+ GB)
       Falls back to matching by VolumeName substring if only one disk present.

    3. PowerShell Get-Volume (modern CIM path, Windows 10+): same principle
       but uses Get-Volume | Where-Object DriveType -eq 'Removable'.

    4. Returns -1 — caller keeps last cached value (safe: treated as
       "unknown" rather than "empty", preventing false storage-full errors).
    """
    _coinit()

    # ── Layer 1: Shell ExtendedProperty ──────────────────────────────────────
    try:
        storage = _get_storage_root(device_name, storage_name)
        val = storage.Self.ExtendedProperty("System.FreeSpace")
        if val is not None and int(val) > 0:
            return int(val)
    except Exception:
        pass

    # ── Layer 2: WMI Win32_LogicalDisk with volume matching ──────────────────
    # NW-ZX507 in MTP/File Transfer mode exposes two removable drives:
    #   Internal shared storage  ≈ 50 GB total  (DriveType=2, VolumeName varies)
    #   SD card                  ≈ user-size     (DriveType=2, VolumeName e.g. "4A21-0000")
    # We discriminate by Size: internal is always smaller than the SD card.
    try:
        import subprocess as _sp2
        ps2 = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-WmiObject Win32_LogicalDisk | Where{$_.DriveType -eq 2} |"
            "Select DeviceID,VolumeName,Size,FreeSpace |"
            "ForEach{$_.DeviceID+','+$_.VolumeName+','+$_.Size+','+$_.FreeSpace}"
        )
        r2 = _sp2.run(
            ["powershell", "-NoProfile", "-Command", ps2],
            capture_output=True, text=True, timeout=12,
        )
        if r2.returncode == 0:
            disks = []
            for line in r2.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 4:
                    try:
                        drv   = parts[0].strip()
                        vname = parts[1].strip().lower()
                        size  = int(parts[2].strip() or "0")
                        free  = int(parts[3].strip() or "0")
                        if size > 0:
                            disks.append((drv, vname, size, free))
                    except ValueError:
                        pass
            if disks:
                # Sort by size: smallest first (internal), largest last (SD)
                disks.sort(key=lambda d: d[2])
                if len(disks) == 1:
                    # Only one removable drive visible — return its free space
                    # regardless of which storage was requested
                    return disks[0][3]
                # Two or more: internal=smallest, SD=largest
                sn_lc = storage_name.lower()
                if "internal" in sn_lc:
                    return disks[0][3]   # smallest = internal
                else:
                    return disks[-1][3]  # largest = SD card
    except Exception:
        pass

    # ── Layer 3: Get-Volume (Windows 10+ CIM) ────────────────────────────────
    try:
        import subprocess as _sp3
        ps3 = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-Volume | Where{$_.DriveType -eq 'Removable'} |"
            "Select FileSystemLabel,Size,SizeRemaining |"
            "ForEach{$_.FileSystemLabel+','+$_.Size+','+$_.SizeRemaining}"
        )
        r3 = _sp3.run(
            ["powershell", "-NoProfile", "-Command", ps3],
            capture_output=True, text=True, timeout=12,
        )
        if r3.returncode == 0:
            vols = []
            for line in r3.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 3:
                    try:
                        label    = parts[0].strip().lower()
                        size     = int(parts[1].strip() or "0")
                        rem      = int(parts[2].strip() or "0")
                        if size > 0:
                            vols.append((label, size, rem))
                    except ValueError:
                        pass
            if vols:
                vols.sort(key=lambda v: v[1])
                sn_lc = storage_name.lower()
                if "internal" in sn_lc:
                    return vols[0][2]
                else:
                    return vols[-1][2]
    except Exception:
        pass

    return -1


def copy_file_to_device(
    src:          pathlib.Path,
    dest_parts:   list[str],
    device_name:  str,
    storage_name: str,
    overwrite:    bool = False,
    device_index=None,   # REV 14.9 (C2) — optional DeviceIndex
    progress_cb=None,    # 2026-07-18 — optional heartbeat, see copy_file_to_device_isolated
) -> str:
    """
    Copies a local file to the Walkman under storage_name / dest_parts /.
    Returns the storage_name actually used.
    overwrite=True replaces existing file — old file only deleted AFTER
    new copy is confirmed visible.
    """
    _coinit()
    if progress_cb is not None:
        progress_cb()
    storage_root  = _get_storage_root(device_name, storage_name)
    if progress_cb is not None:
        progress_cb()
    # REV 14.9 (C2) — pass device_index into _navigate so it can consult
    # the in-memory index before polling the slow MTP enumerator.
    target_folder = _navigate(
        storage_root, dest_parts, create=True,
        device_index=device_index,
        storage_name=storage_name,
        progress_cb=progress_cb,
    )

    # Sanitise source filename for Shell.Application
    safe_stem = _ascii_safe(src.stem)
    # Fail fast when CJK/non-ASCII stripping collapses ' - ' separators
    # (e.g. "Artist - シティ・コネクション - 01 タイトル" → "Artist - - 01").
    # The collapse is visible as a drop in separator count: the original has
    # N occurrences of ' - ', the safe stem has fewer.  Shell.Application
    # CopyHere would create a 0-byte placeholder under the degenerate name;
    # raising here lets ADB handle the file without that corrupt artifact.
    if safe_stem.count(" - ") < src.stem.count(" - "):
        raise RuntimeError(
            f"[COM] CJK stripping collapsed filename separators for "
            f"{src.name!r} (safe_stem={safe_stem!r}). Use ADB for this file."
        )
    safe_name = safe_stem + src.suffix.lower()
    safe_src  = src.parent / safe_name
    if safe_src != src:
        src.rename(safe_src)
        src = safe_src

    safe_stem_lc = safe_stem.lower()
    safe_name_lc = safe_name.lower()

    # Find any existing item that matches
    existing_item = None
    try:
        for item in list(target_folder.Items()):
            try:
                iname = item.Name.lower()
                if iname == safe_name_lc or iname == safe_stem_lc:
                    existing_item = item
                    break
            except Exception:
                continue
    except Exception:
        pass

    if existing_item is not None and not overwrite:
        log.debug(f"  [Skip] {safe_name} already on device.")
        return storage_name

    # Try PowerShell transfer first, fall back to CopyHere
    # 2026-07-22: this is a single blocking subprocess.run() call with no
    # heartbeat for its whole duration (confirmed live: a real transfer
    # succeeded here while the outer isolated-copy supervisor's quiet-timeout
    # fired and killed it, reporting a false failure — same bug class as the
    # 2026-07-20 _verify_com_copy fix, one call site earlier). Beat right
    # before entering it so the supervisor's quiet-timeout window starts
    # fresh at the point we're about to block, same convention as every
    # other potentially-blocking COM call in this function.
    if progress_cb is not None:
        progress_cb()
    ps_ok = False
    try:
        import wpd_transfer as _wpd
        ps_ok = _wpd._try_powershell(
            src_path     = src,
            dest_parts   = dest_parts,
            device_name  = device_name,
            storage_name = storage_name,
        )
    except ImportError:
        log.warning("  [Transfer] wpd_transfer import failed — using in-process CopyHere only (slower)")
    except Exception as _ps_err:
        log.warning(f"  [Transfer] PowerShell path errored ({type(_ps_err).__name__}: {_ps_err}) — falling back to in-process CopyHere")

    if not ps_ok:
        # REV 14.8 — Per-worker circuit breaker
        if _copyhere_is_disabled():
            raise RuntimeError(
                f"[CopyHereCircuit] disabled in this worker after "
                f"{_copyhere_current_count()} consecutive timeouts; "
                f"refusing to attempt '{safe_name}'. "
                f"P8: If this is 100% failure rate, likely STRUCTURAL (missing .deleted/ dir "
                f"or full storage) not transient — check device storage and .deleted/ folder."
            )

        if progress_cb is not None:
            progress_cb()
        storage_root  = _get_storage_root(device_name, storage_name)
        target_folder = _navigate(storage_root, dest_parts, create=True, progress_cb=progress_cb)
        src_ns   = _shell().Namespace(str(src.parent))
        src_item = src_ns.ParseName(src.name) if src_ns else None
        if src_item is None:
            raise RuntimeError(f"Shell could not resolve source: {src}")
        if progress_cb is not None:
            progress_cb()
        target_folder.CopyHere(src_item, 4 | 16 | 0x400 | 0x200)
        # MK18 #2: 3-condition stable-size check replaces bare _wait_for_file()
        try:
            expected_bytes = src.stat().st_size
        except OSError:
            expected_bytes = -1

        ok_stable, stable_reason = _wait_for_stable_size(
            target_folder, safe_name, expected_bytes, progress_cb=progress_cb
        )
        if not ok_stable:
            _n = _copyhere_record_timeout()
            if _n >= COPYHERE_CIRCUIT_THRESHOLD:
                log.warning(
                    f"  [CopyHereCircuit] worker hit {_n} consecutive "
                    f"CopyHere timeouts — disabled for the rest of this "
                    f"session in this worker thread.  Further attempts "
                    f"will fail fast instead of waiting {COPY_TIMEOUT_S}s."
                )
            # Delete the corrupt/truncated stub before raising
            try:
                _delete_remote_after_failed_verify(
                    target_folder, safe_name, device_name, storage_name, dest_parts
                )
            except Exception:
                pass
            raise RuntimeError(
                f"CopyHere failed 3-condition check for '{safe_name}': "
                f"{stable_reason} (worker consecutive timeouts: {_n})."
            )
        else:
            _copyhere_record_success()

    # MK17 Patch 4 — verify the COM/PowerShell transfer actually landed
    # the right file size.  ADB has had this since Rev 14.9 A3; COM did
    # not.  We do this verification regardless of whether PowerShell or
    # CopyHere did the actual work — both paths can produce truncated
    # writes if the Shell COM marshaller times out mid-transfer.
    if getattr(__import__("config"), "VERIFY_TRANSFERS_ENABLED", True):
        try:
            expected_size = src.stat().st_size
        except OSError:
            expected_size = -1

        ok, reason = _verify_com_copy(
            device_name, storage_name, dest_parts, safe_name, expected_size,
            progress_cb=progress_cb,
        )
        if not ok:
            # 2026-07-22 — an "ambiguous" failure (WPD/shell cache staleness,
            # ADB cross-check couldn't resolve it either) means the file may
            # actually be fine on-device — deleting it here is the false-
            # positive-deletion bug this fix targets. Mark it pending for a
            # later recheck (walkman_sync.py Stage 6) instead of deleting,
            # UNLESS sync_state itself isn't usable, in which case fall back
            # to today's delete+raise exactly (never silently keep a file we
            # can't track).
            if reason.startswith(_AMBIGUOUS_PREFIX):
                try:
                    import sync_state as _ss
                    _ss.transition_to(str(src.absolute()), "device_status", "pending")
                    log.warning(
                        f"  [Transfer] Verification AMBIGUOUS for {safe_name!r}: {reason}.  "
                        f"NOT deleting — marked device_status=pending for deferred re-check."
                    )
                    return storage_name
                except Exception as _ss_err:
                    log.warning(
                        f"  [Transfer] sync_state unavailable ({_ss_err}) — falling back to "
                        f"delete-and-retry for ambiguous verify (no provisional-pending path)."
                    )
            log.warning(
                f"  [Transfer] Verification FAILED for {safe_name!r}: {reason}.  "
                f"Removing corrupt remote file."
            )
            _delete_remote_after_failed_verify(
                target_folder, safe_name, device_name, storage_name, dest_parts
            )
            # Surface a real failure so the upper-level transport can retry
            # or fall back to the OTHER transport (ADB) on the next attempt.
            try:
                # Track for the run-summary so the operator sees the parity bug
                import sys as _sys
                _dl_mod = _sys.modules.get("downloader")
                if _dl_mod is not None and hasattr(_dl_mod, "_XFER_COUNTERS_VERIFY_FAIL"):
                    _dl_mod._XFER_COUNTERS_VERIFY_FAIL[0] += 1
            except Exception:
                pass
            raise RuntimeError(
                f"Post-COM-transfer verification failed for {safe_name!r}: {reason}"
            )
        else:
            log.debug(f"  [Transfer/Verify] {safe_name!r}: {reason}")

    # Only delete old file AFTER new copy is confirmed
    if existing_item is not None and overwrite:
        try:
            import wpd_transfer as _wpd
            _wpd.delete_file_ps(
                device_name  = device_name,
                storage_name = storage_name,
                dest_parts   = dest_parts,
                filename     = existing_item.Name,
            )
            log.debug(f"  [Transfer] Old version removed: {existing_item.Name}")
        except ImportError:
            try:
                existing_item.InvokeVerb("delete")
                log.debug(f"  [Transfer] Old version removed via InvokeVerb: {existing_item.Name}")
            except Exception as _iv_err:
                log.warning(f"  [Transfer] Could not remove old version: {_iv_err}")
        except Exception as _del_err:
            log.warning(f"  [Transfer] Could not remove old version: {_del_err}")

    # Fix T: trigger Android MediaStore rescan so the file is indexed without reboot
    _trigger_media_scan_file(dest_parts, safe_name, storage_name)

    return storage_name


def _trigger_media_scan_file(
    dest_parts: list[str],
    filename: str,
    storage_name: str,
) -> None:
    """
    Ask Android's MediaScannerService to index the newly transferred file.
    Without this, HighResMediaPlayerApp only discovers the file after a device
    reboot or a manual 'Scan for new content' action.

    Two broadcasts are sent via ADB (best-effort — failure is logged, not raised):
      1. MEDIA_SCANNER_SCAN_FILE for the specific file path
      2. MEDIA_MOUNTED for the storage root (broader rescan trigger)

    The device path is reconstructed from storage_name and dest_parts.
    Known storage prefixes: /storage/emulated/0 (internal), /storage/XXXX-XXXX (SD).
    """
    try:
        from adb_transfer import _adb_cmd, _ADB_EXE, _shell_quote
        if not _ADB_EXE:
            return

        # Map storage_name label to a likely on-device root
        sn_lc = storage_name.lower()
        if "internal" in sn_lc or "emulated" in sn_lc:
            storage_root = "/storage/emulated/0"
        else:
            storage_root = "/storage/sdcard1"  # SD card fallback; may vary

        rel = "/".join(dest_parts + [filename])
        device_path = f"{storage_root}/{rel}"
        file_uri = "file://" + device_path

        _adb_cmd(
            ["shell", "am", "broadcast",
             "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
             "--es", "path", _shell_quote(file_uri)],
            timeout=10, check=False,
        )
    except Exception as _e:
        log.debug(f"  [MediaScan] per-file broadcast failed: {_e}")


def trigger_media_mounted(storage_name: str = "internal") -> None:
    """
    Broadcast MEDIA_MOUNTED for an entire storage root.
    Call once at end-of-run after all transfers complete.
    """
    try:
        from adb_transfer import _adb_cmd, _ADB_EXE, _shell_quote
        if not _ADB_EXE:
            return
        sn_lc = storage_name.lower()
        if "internal" in sn_lc or "emulated" in sn_lc:
            storage_root = "/storage/emulated/0"
        else:
            storage_root = "/storage/sdcard1"
        _adb_cmd(
            ["shell", "am", "broadcast",
             "-a", "android.intent.action.MEDIA_MOUNTED",
             "--es", "path", _shell_quote(f"file://{storage_root}")],
            timeout=10, check=False,
        )
        log.info(f"  [MediaScan] MEDIA_MOUNTED broadcast sent for {storage_root}")
    except Exception as _e:
        log.debug(f"  [MediaScan] MEDIA_MOUNTED broadcast failed: {_e}")


def read_file_from_device(
    src_parts:    list[str],
    filename:     str,
    dest_local:   pathlib.Path,
    device_name:  str,
    storage_name: str,
) -> bool:
    """
    Copies a file FROM the device to a local path (for re-tagging / audit).
    Returns True on success, False if not found.
    """
    _coinit()
    try:
        storage_root = _get_storage_root(device_name, storage_name)
        folder       = _navigate(storage_root, src_parts, create=False)

        for item in list(folder.Items()):
            iname    = item.Name.lower()
            fname_lc = filename.lower()
            stem_lc  = pathlib.Path(filename).stem.lower()
            if iname == fname_lc or iname == stem_lc:
                dest_local.parent.mkdir(parents=True, exist_ok=True)
                dest_shell = _shell().Namespace(str(dest_local.parent))
                # MK18's 3-condition stable-size check (name + non-zero +
                # settled + matches source bytes) already fixed this exact
                # "truncated stub accepted as success" failure mode for the
                # push direction — this pull direction was still using the
                # bare existence-only _wait_for_file() it replaced there.
                # CopyHere is async on Windows Shell either way, so the same
                # risk applies going device -> PC.
                try:
                    expected_bytes = int(item.Size)
                except Exception:
                    expected_bytes = -1
                dest_shell.CopyHere(item, 4 | 16)
                ok, _reason = _wait_for_stable_size(
                    dest_shell, filename, expected_bytes, settling_delay_s=0.0
                )
                return ok
        return False
    except RuntimeError:
        return False


def write_text_file_to_device(
    content:      str,
    dest_parts:   list[str],
    filename:     str,
    device_name:  str,
    storage_name: str,
):
    """Writes a UTF-8 text file (e.g. M3U8 playlist) to the device."""
    _coinit()
    fd, tmp_path = tempfile.mkstemp(suffix=pathlib.Path(filename).suffix)
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)

        storage_root  = _get_storage_root(device_name, storage_name)
        target_folder = _navigate(storage_root, dest_parts, create=True)

        fname_lc = filename.lower()
        stem_lc  = pathlib.Path(filename).stem.lower()
        for item in list(target_folder.Items()):
            iname = item.Name.lower()
            if iname == fname_lc or iname == stem_lc:
                # FIX F2: use PS subprocess — InvokeVerb triggers Win11 dialog in-process
                try:
                    import wpd_transfer as _wpd
                    _wpd.delete_file_ps(device_name, storage_name, dest_parts, filename)
                except ImportError:
                    item.InvokeVerb("Delete")   # fallback, dialog_watcher handles it
                time.sleep(0.5)
                break

        target_folder.CopyHere(tmp_path, 4 | 16)
        _wait_for_file(target_folder, filename)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def rename_file_on_device(
    device_name:  str,
    storage_name: str,
    folder_parts: list[str],
    old_filename: str,
    new_filename: str,
) -> bool:
    """
    Renames a file on the device by downloading, renaming, re-uploading.
    Returns True on success.

    2026-08-19: now does its OWN explicit post-upload verification
    (_verify_com_copy) rather than trusting copy_file_to_device()'s return
    value at face value. copy_file_to_device() returns non-None (looks like
    "success") both on a clean confirmed write AND on an AMBIGUOUS/pending
    write (by design — its normal callers track the pending state via
    sync_state, keyed to the LOCAL source path, for a later deferred
    recheck). For rename, that local "source" is new_tmp — a throwaway temp
    file deleted in this function's own `finally` block — so the pending
    entry is orphaned the moment this function returns and the promised
    "later recheck" never happens. Found live: a rename reported True while
    the real device state was a 0-byte destination placeholder and the
    source never deleted. Fix: explicitly re-verify the real remote size
    before ever deleting the old file or reporting success; on anything
    short of a confirmed match, preserve the old file and report False —
    same fail-toward-preserving-data bias as the rest of this module.
    """
    _coinit()
    tmp_path = None
    new_tmp  = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=pathlib.Path(old_filename).suffix)
        os.close(fd)
        tmp_path = pathlib.Path(tmp)
        ok = read_file_from_device(folder_parts, old_filename, tmp_path, device_name, storage_name)
        if not ok:
            return False
        new_tmp = tmp_path.parent / new_filename
        tmp_path.rename(new_tmp)
        expected_size = new_tmp.stat().st_size
        copy_file_to_device(new_tmp, folder_parts, device_name, storage_name, overwrite=True)
        # retries=2 (not the default 5): copy_file_to_device already ran a
        # full internal retry cycle before returning "ambiguous" — a second
        # full 5-retry cycle back-to-back roughly doubled worst-case latency
        # live (confirmed: pushed a healthy, non-hung rename past the 300s
        # guard). A lighter follow-up check is enough independent signal
        # without stacking two nearly-identical wait cycles.
        verified, reason = _verify_com_copy(
            device_name, storage_name, folder_parts, new_filename, expected_size,
            retries=2, base_wait_s=3.0,
        )
        if not verified:
            log.warning(
                f"  [Rename] Re-upload of {new_filename!r} did not verify ({reason}) — "
                f"NOT deleting the original {old_filename!r}, reporting failure rather "
                f"than a rename that may not have actually completed."
            )
            return False
        # Delete old via PowerShell subprocess — avoids Win11 dialog (FIX F2)
        try:
            import wpd_transfer as _wpd
            _wpd.delete_file_ps(
                device_name  = device_name,
                storage_name = storage_name,
                dest_parts   = folder_parts,
                filename     = old_filename,
            )
        except Exception as e:
            log.warning(f"  [Rename] Could not delete old file: {e}")
        return True
    except Exception as e:
        log.warning(f"  [Rename] Failed: {e}")
        return False
    finally:
        for p in [tmp_path, new_tmp]:
            if p and pathlib.Path(p).exists():
                try: pathlib.Path(p).unlink()
                except Exception: pass


def rename_folder_on_device(
    device_name:  str,
    storage_name: str,
    parent_parts: list[str],
    old_name:     str,
    new_name:     str,
) -> bool:
    """
    Rename a folder on the device using Shell rename.
    Returns True on success.
    """
    _coinit()
    try:
        storage_root = _get_storage_root(device_name, storage_name)
        parent = _navigate(storage_root, parent_parts, create=False) if parent_parts else storage_root

        # Find the folder item
        folder_item = None
        for item in list(parent.Items()):
            try:
                if item.IsFolder and item.Name.lower() == old_name.lower():
                    folder_item = item
                    break
            except Exception:
                continue

        if folder_item is None:
            log.debug(f"  [RenameFolder] Could not find folder '{old_name}'")
            return False

        # Use InvokeVerbEx("rename", new_name) – works on MTP folders
        folder_item.InvokeVerbEx("rename", new_name)
        time.sleep(1.0)
        return True
    except Exception as e:
        log.warning(f"  [RenameFolder] Failed: {e}")
        return False


def delete_file_on_device(
    device_name:  str,
    storage_name: str,
    folder_parts: list[str],
    filename:     str,
) -> bool:
    """
    Deletes a file on the device. Returns True on success.

    FIX F2: routes through wpd_transfer.delete_file_ps() (PowerShell subprocess)
    instead of InvokeVerb("delete") in-process.  In-process InvokeVerb triggers
    Win11 "Confirm File Delete" dialogs because the Python process has a visible
    window handle.  A -NonInteractive -WindowStyle Hidden PS subprocess has
    hwnd=0, so Win11 cannot attach a dialog to it.
    """
    _coinit()
    try:
        import wpd_transfer as _wpd
        return _wpd.delete_file_ps(
            device_name  = device_name,
            storage_name = storage_name,
            dest_parts   = folder_parts,
            filename     = filename,
        )
    except ImportError:
        # wpd_transfer unavailable — fall back to InvokeVerb (dialog risk;
        # dialog_watcher should catch it)
        log.warning("  [Delete] wpd_transfer unavailable — InvokeVerb fallback (dialog risk)")
        try:
            storage_root = _get_storage_root(device_name, storage_name)
            folder = _navigate(storage_root, folder_parts, create=False)
            fname_lc = filename.lower()
            stem_lc  = pathlib.Path(filename).stem.lower()
            for item in list(folder.Items()):
                if item.Name.lower() in (fname_lc, stem_lc):
                    item.InvokeVerb("delete")
                    return True
            return False
        except Exception as e:
            log.warning(f"  [Delete] Failed: {e}")
            return False
    except Exception as e:
        log.warning(f"  [Delete] PS delete failed: {e}")
        return False


def _recurse_audio(folder, path_parts: list[str], storage_name: str, results: list):
    """
    Recursively enumerate audio files under a COM folder.
    Materialises folder.Items() immediately — lazy COM iterators
    hang on dirty MTP sessions.
    COM STA note: folder and all Items() must stay on the calling thread.
    """
    try:
        items = list(folder.Items())
    except Exception as e:
        log.warning(f"  [Scan] Could not enumerate folder {path_parts}: {e}")
        return
    # Log each artist-level folder so the scan shows progress in the log
    if len(path_parts) == 2:  # Music/<Artist>
        log.info(f"  [Scan] Artist: {path_parts[-1]}/ ({_scan_progress[0]} files so far)")

    for item in items:
        try:
            if item.IsFolder:
                _recurse_audio(item.GetFolder, path_parts + [item.Name], storage_name, results)
            else:
                ext = _item_audio_ext(item)
                if ext:
                    results.append({
                        "filename": item.Name,
                        "ext":      ext,
                        "parts":    path_parts,
                        "storage":  storage_name,
                    })
                    _scan_progress[0] += 1
                    if _scan_progress[0] % 500 == 0:
                        log.info(f"  [Scan] {_scan_progress[0]} files enumerated (current: {'/'.join(path_parts)}/)")
        except Exception as e:
            log.warning(f"  [Scan] Skipping item in {path_parts}: {e}")


_SCAN_STORAGE_TIMEOUT_S = 120  # per-storage scan hard timeout


def _scan_one_storage(device_name: str, storage_name: str, subfolder: str) -> list[dict]:
    """
    Scan a single storage root in the CALLING thread's COM apartment.
    Called from a daemon thread that initialises its own STA so COM objects
    stay on the thread that created them — no cross-apartment marshalling.
    """
    _coinit()
    results: list[dict] = []
    try:
        storage_root = _get_storage_root(device_name, storage_name)
    except RuntimeError as e:
        log.debug(f"scan_device_audio: skipping {storage_name}: {e}")
        return results

    try:
        top_items = list(storage_root.Items())
    except Exception as e:
        log.warning(f"  [Scan] {storage_name}: could not list top-level items: {e}")
        return results

    music_folder = None
    for item in top_items:
        try:
            if item.IsFolder and item.Name.lower() == subfolder.lower():
                music_folder = item.GetFolder
                break
        except Exception:
            continue

    if music_folder is None:
        if subfolder == "":
            music_folder = storage_root
        else:
            avail = []
            for item in top_items:
                try: avail.append(item.Name)
                except Exception: pass
            log.debug(
                f"  [Scan] {storage_name}: subfolder '{subfolder}' not found. "
                f"Top-level: {avail}"
            )
            return results

    _recurse_audio(music_folder, [subfolder] if subfolder else [], storage_name, results)
    return results


def scan_device_audio(
    device_name:   str,
    storage_roots: list[tuple[str, str]],
) -> list[dict]:
    """
    Enumerates audio files across ALL configured storage roots via COM/MTP.

    Each storage root is scanned in a dedicated daemon thread with its own
    COM STA so a hung MTP call (e.g. dirty SD card session) cannot block
    the pipeline indefinitely.  Times out per storage after
    _SCAN_STORAGE_TIMEOUT_S seconds with a warning.

    storage_roots is a list of (storage_name, subfolder) tuples, e.g.:
        [("Internal shared storage", "Music"),
         ("SD card", "Music")]

    Returns list of dicts: {filename, ext, parts, storage}
    """
    # WPD fast-path: batch enumeration (32 handles/round-trip), no Shell STA apartment.
    # If WPD is unavailable or returns None, fall through to Shell.Application below.
    # If WPD returns a list (even empty), trust it — device may be unresponsive and
    # Shell.Application would just cascade-timeout anyway.
    try:
        import wpd_transfer as _wpd
        _wpd_result = _wpd.scan_device_audio_wpd(device_name, storage_roots)
        if _wpd_result is not None:
            log.info(f"  [Scan] WPD batch scan: {len(_wpd_result)} audio files total")
            return _wpd_result
        log.info("  [Scan] WPD unavailable — falling back to Shell.Application")
    except Exception as _wpd_err:
        log.debug(f"  [Scan] WPD fast-path error: {_wpd_err} — falling back to Shell.Application")

    import threading as _t
    results: list[dict] = []
    _scan_progress[0] = 0

    for storage_name, subfolder in storage_roots:
        log.info(f"  [Scan] {storage_name}/{subfolder}: starting (timeout={_SCAN_STORAGE_TIMEOUT_S}s)...")
        thread_results: list[dict] = []
        done = _t.Event()

        def _worker(sn=storage_name, sf=subfolder, out=thread_results, ev=done):
            try:
                out.extend(_scan_one_storage(device_name, sn, sf))
            except Exception as e:
                log.warning(f"  [Scan] {sn}: worker error: {e}")
            finally:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
                ev.set()

        t = _t.Thread(target=_worker, daemon=True,
                      name=f"mtp-scan-{storage_name[:20]}")
        t.start()
        finished = done.wait(timeout=_SCAN_STORAGE_TIMEOUT_S)
        if not finished:
            log.warning(
                f"  [Scan] {storage_name}/{subfolder}: scan timed out after "
                f"{_SCAN_STORAGE_TIMEOUT_S}s — MTP session may be dirty. "
                f"Skipping this storage root. Unplug/replug the Walkman to reset."
            )
            continue

        found = len(thread_results)
        log.info(f"  [Scan] {storage_name}/{subfolder}: {found} audio file(s) found")
        results.extend(thread_results)

    return results


_DELETE_EMPTY_TIMEOUT_S = 60


def delete_empty_device_folders(
    device_name:  str,
    storage_name: str,
    parent_parts: list[str],
) -> int:
    """
    Recursively delete empty folders under parent_parts.
    Returns number of deleted folders. Times out after 60s to avoid
    hanging on a dirty MTP session (mirrors scan_device_audio behaviour).
    """
    import threading as _t
    result = [0]
    done = _t.Event()

    def _worker():
        try:
            result[0] = _delete_empty_folders_impl(device_name, storage_name, parent_parts)
        except Exception as e:
            log.warning(f"  [Cleanup] delete_empty_device_folders error: {e}")
        finally:
            done.set()

    t = _t.Thread(target=_worker, daemon=True, name="mtp-del-empty")
    t.start()
    if not done.wait(_DELETE_EMPTY_TIMEOUT_S):
        log.warning(
            f"  [Cleanup] delete_empty_device_folders timed out after {_DELETE_EMPTY_TIMEOUT_S}s "
            f"— skipping cleanup for {storage_name}. Unplug/replug Walkman to reset MTP."
        )
    return result[0]


def _delete_empty_folders_impl(
    device_name:  str,
    storage_name: str,
    parent_parts: list[str],
) -> int:
    _coinit()
    try:
        storage_root = _get_storage_root(device_name, storage_name)
        root = _navigate(storage_root, parent_parts, create=False) if parent_parts else storage_root
    except Exception:
        return 0

    deleted = 0

    def _recurse_empty(folder_item, path_parts):
        nonlocal deleted
        try:
            items = list(folder_item.Items())
        except Exception:
            return
        for item in items:
            if item.IsFolder:
                _recurse_empty(item.GetFolder, path_parts + [item.Name])
        try:
            remaining = list(folder_item.Items())
        except Exception:
            remaining = []
        if not remaining and path_parts:
            log.info(f"  [Cleanup] Deleting empty folder: {storage_name}/{'/'.join(path_parts)}")
            folder_item.InvokeVerb("delete")
            time.sleep(0.5)
            deleted += 1

    _recurse_empty(root, parent_parts)
    return deleted


def rename_mojibake_folders(device_name: str, storage_roots: list) -> None:
    """
    FIX B4 (COM path): Scan Music folders and rename any whose name is
    Latin-1/UTF-8 mojibake (e.g. "AminÃ©" → "Aminé").

    Uses rename_folder_on_device() (which calls InvokeVerbEx internally)
    and re-navigates into the renamed folder to continue recursion.
    Falls back gracefully — never leaves a folder partially renamed.
    """
    _coinit()
    for storage_name, subfolder in storage_roots:
        try:
            storage_root = _get_storage_root(device_name, storage_name)
            if subfolder:
                try:
                    music_folder = _navigate(storage_root, [subfolder], create=False)
                except RuntimeError:
                    log.debug(f"  [Mojibake] {storage_name}/{subfolder} not found, skipping")
                    continue
            else:
                music_folder = storage_root
            _rename_mojibake_in_folder(
                folder       = music_folder,
                device_name  = device_name,
                storage_name = storage_name,
                parent_parts = [subfolder] if subfolder else [],
            )
        except Exception as e:
            log.warning(f"  [Mojibake] Could not process {storage_name}/{subfolder}: {e}")


def _rename_mojibake_in_folder(folder, device_name: str, storage_name: str,
                                parent_parts: list[str]) -> None:
    """Recurse through folder and rename mojibake-named subfolders."""
    try:
        items = list(folder.Items())
    except Exception as e:
        log.debug(f"  [Mojibake] Cannot enumerate {parent_parts}: {e}")
        return

    for item in items:
        try:
            if not item.IsFolder:
                continue
            name = item.Name
        except Exception:
            continue

        # Attempt Latin-1 → UTF-8 mojibake decode
        try:
            fixed = name.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            fixed = None

        needs_rename = (
            fixed is not None
            and fixed != name
            and len(fixed) >= 2
            and any(c.isalpha() for c in fixed)
        )

        if needs_rename:
            log.info(
                f"  [Mojibake] {storage_name}/{'/'.join(parent_parts)}: "
                f"{name!r} → {fixed!r}"
            )
            ok = rename_folder_on_device(
                device_name  = device_name,
                storage_name = storage_name,
                parent_parts = parent_parts,
                old_name     = name,
                new_name     = fixed,
            )
            if ok:
                # Re-navigate using the new name and recurse
                try:
                    storage_root = _get_storage_root(device_name, storage_name)
                    new_folder   = _navigate(
                        storage_root, parent_parts + [fixed], create=False
                    )
                    _rename_mojibake_in_folder(
                        new_folder, device_name, storage_name,
                        parent_parts + [fixed],
                    )
                except Exception as re_e:
                    log.debug(f"  [Mojibake] Could not recurse into renamed {fixed!r}: {re_e}")
            else:
                log.warning(f"  [Mojibake] rename_folder_on_device returned False for {name!r}")
        else:
            # No rename needed — still recurse
            try:
                _rename_mojibake_in_folder(
                    item.GetFolder, device_name, storage_name,
                    parent_parts + [name],
                )
            except Exception as re2:
                log.debug(f"  [Mojibake] Recurse error in {name!r}: {re2}")


def reset_storage_cache():
    """Clear any cached storage paths (COM doesn't cache, but keep for API consistency)."""
    pass


def _invalidate_storage_cache():
    reset_storage_cache()


def log_health_check(label: str) -> bool:
    """Log connection status and free space. Returns True if device is reachable."""
    if not device_is_connected(config.DEVICE_NAME):
        log.warning(f"  [{label}] Device not reachable via COM.")
        return False
    try:
        free = get_device_free_space(config.DEVICE_NAME, config.WRITE_STORAGE)
        if free >= 0:
            log.info(f"  [{label}] Internal free: {free // (1024 ** 2)} MB")
    except Exception as e:
        log.warning(f"  [{label}] Could not query free space: {e}")
    return True


# ---------------------------------------------------------------------------
# Subprocess-isolated copy (2026-07-18) — see copy_file_to_device_isolated
# ---------------------------------------------------------------------------

def _copy_worker_main(src: str, dest_parts: list, device_name: str,
                       storage_name: str, overwrite: bool) -> None:
    """
    Runs in a CHILD PROCESS (see copy_file_to_device_isolated, which spawns
    `python mtp_transfer_com.py --copy-worker ...`). Mirrors
    wpd_transfer._wpd_scan_worker_main's structure exactly — same rationale:
    a thread-based watchdog cannot reliably guard a blocked COM call.

    Prints a heartbeat marker to stderr at each potentially-blocking COM call
    (same call sites copy_file_to_device's progress_cb now threads through:
    before the PS-transfer attempt, _get_storage_root, each level of
    _navigate, the CopyHere dispatch, and each poll of _wait_for_stable_size)
    so the parent can detect a stall; all
    other stderr lines are normal log output the parent relays into its own
    logger. On success prints the resulting storage_name to stdout behind
    _COPY_RESULT_MARKER; on failure, _COPY_ERROR_MARKER.
    """
    def _beat() -> None:
        print(_COPY_HEARTBEAT, file=sys.stderr, flush=True)

    try:
        import sync_state as _ss
        _ss.open_db()
    except Exception as e:
        print(f"[Worker] sync_state.open_db() failed: {e}", file=sys.stderr, flush=True)

    try:
        result = copy_file_to_device(
            pathlib.Path(src), list(dest_parts), device_name, storage_name,
            overwrite=overwrite, progress_cb=_beat,
        )
        print(f"{_COPY_RESULT_MARKER}{result}", flush=True)
    except Exception as e:
        print(f"{_COPY_ERROR_MARKER}{type(e).__name__}: {e}", flush=True)


def copy_file_to_device_isolated(
    src, dest_parts: list, device_name: str, storage_name: str,
    overwrite: bool = False,
    quiet_timeout_s: float = 180.0,
    max_duration_s: float = 600.0,
) -> "str | None":
    """
    Subprocess-isolated variant of copy_file_to_device — same contract
    (returns the storage_name used) but runs the ENTIRE copy in a CHILD
    PROCESS (`python mtp_transfer_com.py --copy-worker ...`), supervised here
    with the same two independent guards as wpd_transfer.scan_device_audio_wpd:

    quiet_timeout_s alone is not enough — confirmed live (2026-07-16/17, WPD
    scan path) that a degraded MTP session can either dead-hang (no heartbeat
    ever again) or live-lock (heartbeats keep coming, no real progress).
    max_duration_s is an independent wall-clock cap on the whole copy,
    regardless of heartbeat activity, to catch the second case.

    Returns None on any failure (subprocess wouldn't start, timed out and was
    killed, or the worker itself raised) so callers can fall back to whatever
    they'd otherwise do on a COM failure — this function never raises and
    never hangs past max_duration_s + a few seconds of drain/join overhead.
    """
    import json as _json_copy, threading as _thr_copy, time as _t_copy

    src = str(src)
    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--copy-worker",
             src, _json_copy.dumps(list(dest_parts)), device_name, storage_name,
             "1" if overwrite else "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.warning(f"  [COM/Copy] Could not start copy subprocess: {e}")
        return None

    started   = _t_copy.time()
    last_beat = [started]
    stdout_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            last_beat[0] = _t_copy.time()
            line = line.rstrip("\n")
            if line and line != _COPY_HEARTBEAT:
                log.info(line)

    def _drain_stdout() -> None:
        for line in proc.stdout:
            last_beat[0] = _t_copy.time()
            stdout_chunks.append(line)

    t_err = _thr_copy.Thread(target=_drain_stderr, daemon=True)
    t_out = _thr_copy.Thread(target=_drain_stdout, daemon=True)
    t_err.start()
    t_out.start()

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            now     = _t_copy.time()
            quiet   = now - last_beat[0]
            elapsed = now - started
            reason  = None
            if quiet > quiet_timeout_s:
                reason = f"No progress for {quiet:.0f}s (limit {quiet_timeout_s:.0f}s)"
            elif elapsed > max_duration_s:
                reason = (f"Copy still running after {elapsed:.0f}s (limit {max_duration_s:.0f}s) "
                          f"despite ongoing heartbeats — likely a live-lock, not a dead hang")
            if reason:
                log.warning(
                    f"  [COM/Copy] {reason} — device likely hung/looping on a COM call. Killing "
                    f"copy subprocess. (a real process kill reliably terminates a blocked COM "
                    f"call; an in-process watchdog thread previously could not — see Task 4)"
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                return None

    t_err.join(timeout=5)
    t_out.join(timeout=5)

    output = "".join(stdout_chunks)
    if _COPY_RESULT_MARKER in output:
        return output.split(_COPY_RESULT_MARKER, 1)[1].strip()
    if _COPY_ERROR_MARKER in output:
        log.warning(f"  [COM/Copy] Worker error: {output.split(_COPY_ERROR_MARKER, 1)[1].strip()}")
        return None
    log.warning(f"  [COM/Copy] Worker produced no result marker (exit={proc.returncode})")
    return None


# ---------------------------------------------------------------------------
# Subprocess-isolated existence check (2026-07-19)
# ---------------------------------------------------------------------------
# Closes a real gap found in code review: COMTransport.file_exists() (added
# 2026-07-19 alongside the opt-in COMTransport class) did its own raw
# _navigate()/folder.Items() call with no guard at all — the same call shape
# that live-locked repeatedly the same week — despite that class's docstring
# claiming to reuse only already-hardened primitives. This is that primitive.

def _file_exists_raw(device_name: str, storage_name: str,
                      folder_parts: list, filename: str,
                      progress_cb=None) -> bool:
    """Navigate to folder_parts and check whether filename (or its stem)
    appears in it. Same logic COMTransport.file_exists() used to do inline —
    factored out so both the isolated worker and (if ever needed) an
    in-process caller share one implementation."""
    if progress_cb is not None:
        progress_cb()
    storage_root = _get_storage_root(device_name, storage_name)
    if progress_cb is not None:
        progress_cb()
    folder = _navigate(storage_root, folder_parts, create=False, progress_cb=progress_cb)
    fname_lc = filename.lower()
    stem_lc = pathlib.Path(filename).stem.lower()
    if progress_cb is not None:
        progress_cb()
    for item in list(folder.Items()):
        if item.Name.lower() in (fname_lc, stem_lc):
            return True
    return False


def _exists_worker_main(device_name: str, storage_name: str,
                         folder_parts: list, filename: str) -> None:
    """Runs in a CHILD PROCESS (see file_exists_isolated). Mirrors
    _copy_worker_main's structure exactly."""
    def _beat() -> None:
        print(_EXISTS_HEARTBEAT, file=sys.stderr, flush=True)

    try:
        found = _file_exists_raw(device_name, storage_name, list(folder_parts),
                                  filename, progress_cb=_beat)
        print(f"{_EXISTS_RESULT_MARKER}{'1' if found else '0'}", flush=True)
    except Exception as e:
        print(f"{_EXISTS_ERROR_MARKER}{type(e).__name__}: {e}", flush=True)


def file_exists_isolated(
    device_name: str, storage_name: str, folder_parts: list, filename: str,
    quiet_timeout_s: float = 60.0, max_duration_s: float = 90.0,
) -> "bool | None":
    """
    Subprocess-isolated variant of a folder existence/enumeration check —
    same quiet-timeout + wall-clock dual-guard model as
    copy_file_to_device_isolated (see that function's docstring for the full
    rationale). Shorter default timeouts than the copy path since a single
    folder navigation + enumeration is a much smaller operation than a full
    file transfer.

    Returns True/False on a real answer, None if the subprocess couldn't be
    started, timed out, or the worker raised — callers MUST treat None as
    "unknown," not as False, especially anywhere a False positive (assuming
    absent when it might exist) could cause data loss.
    """
    import json as _json_exists, threading as _thr_exists, time as _t_exists

    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--exists-worker",
             device_name, storage_name, _json_exists.dumps(list(folder_parts)), filename],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.warning(f"  [COM/Exists] Could not start exists-check subprocess: {e}")
        return None

    started = _t_exists.time()
    last_beat = [started]
    stdout_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            last_beat[0] = _t_exists.time()
            line = line.rstrip("\n")
            if line and line != _EXISTS_HEARTBEAT:
                log.debug(line)

    def _drain_stdout() -> None:
        for line in proc.stdout:
            last_beat[0] = _t_exists.time()
            stdout_chunks.append(line)

    t_err = _thr_exists.Thread(target=_drain_stderr, daemon=True)
    t_out = _thr_exists.Thread(target=_drain_stdout, daemon=True)
    t_err.start()
    t_out.start()

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            now = _t_exists.time()
            quiet = now - last_beat[0]
            elapsed = now - started
            reason = None
            if quiet > quiet_timeout_s:
                reason = f"No progress for {quiet:.0f}s (limit {quiet_timeout_s:.0f}s)"
            elif elapsed > max_duration_s:
                reason = f"Exists-check still running after {elapsed:.0f}s (limit {max_duration_s:.0f}s)"
            if reason:
                log.warning(f"  [COM/Exists] {reason} — killing exists-check subprocess.")
                try:
                    proc.kill()
                except Exception:
                    pass
                return None

    t_err.join(timeout=5)
    t_out.join(timeout=5)

    output = "".join(stdout_chunks)
    if _EXISTS_RESULT_MARKER in output:
        return output.split(_EXISTS_RESULT_MARKER, 1)[1].strip() == "1"
    if _EXISTS_ERROR_MARKER in output:
        log.debug(f"  [COM/Exists] Worker error: {output.split(_EXISTS_ERROR_MARKER, 1)[1].strip()}")
        return None
    log.warning(f"  [COM/Exists] Worker produced no result marker (exit={proc.returncode})")
    return None


# ---------------------------------------------------------------------------
# Subprocess-isolated folder creation (2026-07-25)
# ---------------------------------------------------------------------------
# Replaces walkman_dedup._ensure_deleted_folder()'s COM fallback, which used
# to do its own raw _find_folder_by_name()/NewFolder() COM calls in-process
# with zero subprocess isolation and zero timeout — confirmed live to hang
# 90+ seconds on this device. Reuses _get_storage_root/_navigate(create=True)
# verbatim (the same nested-folder-creation primitive copy_file_to_device
# already relies on) rather than reimplementing folder creation.

def _ensure_folder_worker_main(device_name: str, storage_name: str,
                                folder_parts: list) -> None:
    """Runs in a CHILD PROCESS (see ensure_folder_isolated). Mirrors
    _exists_worker_main's structure exactly."""
    def _beat() -> None:
        print(_ENSURE_FOLDER_HEARTBEAT, file=sys.stderr, flush=True)

    try:
        storage_root = _get_storage_root(device_name, storage_name)
        _navigate(storage_root, list(folder_parts), create=True, progress_cb=_beat)
        print(f"{_ENSURE_FOLDER_RESULT_MARKER}1", flush=True)
    except Exception as e:
        print(f"{_ENSURE_FOLDER_ERROR_MARKER}{type(e).__name__}: {e}", flush=True)


def ensure_folder_isolated(
    device_name: str, storage_name: str, folder_parts: list,
    quiet_timeout_s: float = 60.0, max_duration_s: float = 90.0,
) -> "bool | None":
    """
    Subprocess-isolated folder creation (navigate to folder_parts, creating
    any missing level) — same quiet-timeout + wall-clock dual-guard model as
    file_exists_isolated / copy_file_to_device_isolated.

    Returns True on success, None if the subprocess couldn't be started,
    timed out, or the worker raised — callers should treat None the same as
    any other "could not confirm" outcome, not as a hard failure.
    """
    import json as _json_ensure, threading as _thr_ensure, time as _t_ensure

    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--ensure-folder-worker",
             device_name, storage_name, _json_ensure.dumps(list(folder_parts))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.warning(f"  [COM/EnsureFolder] Could not start subprocess: {e}")
        return None

    started   = _t_ensure.time()
    last_beat = [started]
    stdout_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            last_beat[0] = _t_ensure.time()
            line = line.rstrip("\n")
            if line and line != _ENSURE_FOLDER_HEARTBEAT:
                log.debug(line)

    def _drain_stdout() -> None:
        for line in proc.stdout:
            last_beat[0] = _t_ensure.time()
            stdout_chunks.append(line)

    t_err = _thr_ensure.Thread(target=_drain_stderr, daemon=True)
    t_out = _thr_ensure.Thread(target=_drain_stdout, daemon=True)
    t_err.start()
    t_out.start()

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            now     = _t_ensure.time()
            quiet   = now - last_beat[0]
            elapsed = now - started
            reason  = None
            if quiet > quiet_timeout_s:
                reason = f"No progress for {quiet:.0f}s (limit {quiet_timeout_s:.0f}s)"
            elif elapsed > max_duration_s:
                reason = f"Ensure-folder still running after {elapsed:.0f}s (limit {max_duration_s:.0f}s)"
            if reason:
                log.warning(f"  [COM/EnsureFolder] {reason} — killing subprocess.")
                try:
                    proc.kill()
                except Exception:
                    pass
                return None

    t_err.join(timeout=5)
    t_out.join(timeout=5)

    output = "".join(stdout_chunks)
    if _ENSURE_FOLDER_RESULT_MARKER in output:
        return True
    if _ENSURE_FOLDER_ERROR_MARKER in output:
        log.debug(f"  [COM/EnsureFolder] Worker error: {output.split(_ENSURE_FOLDER_ERROR_MARKER, 1)[1].strip()}")
        return None
    log.warning(f"  [COM/EnsureFolder] Worker produced no result marker (exit={proc.returncode})")
    return None


# ---------------------------------------------------------------------------
# Subprocess-isolated standalone verify (2026-07-22)
# ---------------------------------------------------------------------------
# The copy-time call to _verify_com_copy (inside copy_file_to_device) already
# runs inside copy_file_to_device_isolated's subprocess and inherits its
# dual-watchdog protection for free. A STANDALONE call to _verify_com_copy —
# e.g. the deferred pending-file recheck in walkman_sync.py's Stage 6 — does
# not go through that subprocess at all and would otherwise have zero
# isolation. This closes that gap the same way file_exists_isolated closed it
# for the existence-check primitive.

def _verify_worker_main(device_name: str, storage_name: str, dest_parts: list,
                         filename: str, expected_size: int) -> None:
    """Runs in a CHILD PROCESS (see verify_com_copy_isolated). Mirrors
    _exists_worker_main's structure exactly."""
    import json as _json_verify_worker

    def _beat() -> None:
        print(_VERIFY_HEARTBEAT, file=sys.stderr, flush=True)

    try:
        ok, reason = _verify_com_copy(
            device_name, storage_name, list(dest_parts), filename, expected_size,
            progress_cb=_beat,
        )
        print(f"{_VERIFY_RESULT_MARKER}{_json_verify_worker.dumps([ok, reason])}", flush=True)
    except Exception as e:
        print(f"{_VERIFY_ERROR_MARKER}{type(e).__name__}: {e}", flush=True)


def verify_com_copy_isolated(
    device_name: str, storage_name: str, dest_parts: list, filename: str,
    expected_size: int,
    quiet_timeout_s: float = 90.0, max_duration_s: float = 180.0,
) -> "tuple[bool, str] | None":
    """
    Subprocess-isolated variant of _verify_com_copy — same (ok, reason)
    contract, but runs the ENTIRE verify in a CHILD PROCESS
    (`python mtp_transfer_com.py --verify-worker ...`), supervised here with
    the same two independent guards as copy_file_to_device_isolated /
    file_exists_isolated. Shorter default timeouts than the copy path since a
    lone verify (folder navigation + one item lookup) is far smaller than a
    full file transfer.

    Returns None on any failure (subprocess wouldn't start, timed out and was
    killed, or the worker itself raised) — callers MUST treat None as
    "unknown, no signal," not as a False verification result.
    """
    import json as _json_verify, threading as _thr_verify, time as _t_verify

    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--verify-worker",
             device_name, storage_name, _json_verify.dumps(list(dest_parts)),
             filename, str(expected_size)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.warning(f"  [COM/Verify] Could not start verify subprocess: {e}")
        return None

    started   = _t_verify.time()
    last_beat = [started]
    stdout_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            last_beat[0] = _t_verify.time()
            line = line.rstrip("\n")
            if line and line != _VERIFY_HEARTBEAT:
                log.debug(line)

    def _drain_stdout() -> None:
        for line in proc.stdout:
            last_beat[0] = _t_verify.time()
            stdout_chunks.append(line)

    t_err = _thr_verify.Thread(target=_drain_stderr, daemon=True)
    t_out = _thr_verify.Thread(target=_drain_stdout, daemon=True)
    t_err.start()
    t_out.start()

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            now     = _t_verify.time()
            quiet   = now - last_beat[0]
            elapsed = now - started
            reason  = None
            if quiet > quiet_timeout_s:
                reason = f"No progress for {quiet:.0f}s (limit {quiet_timeout_s:.0f}s)"
            elif elapsed > max_duration_s:
                reason = f"Verify still running after {elapsed:.0f}s (limit {max_duration_s:.0f}s)"
            if reason:
                log.warning(f"  [COM/Verify] {reason} — killing verify subprocess.")
                try:
                    proc.kill()
                except Exception:
                    pass
                return None

    t_err.join(timeout=5)
    t_out.join(timeout=5)

    output = "".join(stdout_chunks)
    if _VERIFY_RESULT_MARKER in output:
        try:
            ok, reason = _json_verify.loads(output.split(_VERIFY_RESULT_MARKER, 1)[1].strip())
            return bool(ok), reason
        except Exception as e:
            log.warning(f"  [COM/Verify] Could not parse worker result: {e}")
            return None
    if _VERIFY_ERROR_MARKER in output:
        log.warning(f"  [COM/Verify] Worker error: {output.split(_VERIFY_ERROR_MARKER, 1)[1].strip()}")
        return None
    log.warning(f"  [COM/Verify] Worker produced no result marker (exit={proc.returncode})")
    return None


# ---------------------------------------------------------------------------
# Subprocess-isolated device-to-local read (2026-08-19)
# ---------------------------------------------------------------------------
# Same rationale as rename's isolation below — COMTransport.rename()'s
# cross-folder branch calls read_file_from_device() raw, in-process, with the
# same "blocked COM call" hang risk. Scoped narrowly to that one caller for
# now, not a project-wide sweep of every read_file_from_device() call site
# (most others go through the ADB-primary dispatcher and only reach this COM
# path when ADB is down — a broader audit is a separate, follow-up concern).

def _read_worker_main(src_parts: list, filename: str, dest_local: str,
                       device_name: str, storage_name: str) -> None:
    """Runs in a CHILD PROCESS (see read_file_from_device_isolated)."""
    import threading as _read_thr

    stop_beat = _read_thr.Event()

    def _beat_loop() -> None:
        while not stop_beat.wait(5):
            print(_READ_HEARTBEAT, file=sys.stderr, flush=True)

    beat_thread = _read_thr.Thread(target=_beat_loop, daemon=True)
    beat_thread.start()

    try:
        ok = read_file_from_device(
            list(src_parts), filename, pathlib.Path(dest_local), device_name, storage_name,
        )
        print(f"{_READ_RESULT_MARKER}{'1' if ok else '0'}", flush=True)
    except Exception as e:
        print(f"{_READ_ERROR_MARKER}{type(e).__name__}: {e}", flush=True)
    finally:
        stop_beat.set()


def read_file_from_device_isolated(
    src_parts: list, filename: str, dest_local, device_name: str, storage_name: str,
    quiet_timeout_s: float = 90.0, max_duration_s: float = 180.0,
) -> "bool | None":
    """
    Subprocess-isolated variant of read_file_from_device — same contract
    (returns True on success), same dual-guard model as
    rename_file_on_device_isolated. Returns None (not False) on subprocess
    failure/timeout — callers should treat None as "did not succeed."
    """
    import json as _json_read, threading as _thr_read, time as _t_read

    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--read-worker",
             _json_read.dumps(list(src_parts)), filename, str(dest_local),
             device_name, storage_name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.warning(f"  [COM/Read] Could not start read subprocess: {e}")
        return None

    started   = _t_read.time()
    last_beat = [started]
    stdout_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            last_beat[0] = _t_read.time()
            line = line.rstrip("\n")
            if line and line != _READ_HEARTBEAT:
                log.debug(line)

    def _drain_stdout() -> None:
        for line in proc.stdout:
            last_beat[0] = _t_read.time()
            stdout_chunks.append(line)

    t_err = _thr_read.Thread(target=_drain_stderr, daemon=True)
    t_out = _thr_read.Thread(target=_drain_stdout, daemon=True)
    t_err.start()
    t_out.start()

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            now     = _t_read.time()
            quiet   = now - last_beat[0]
            elapsed = now - started
            reason  = None
            if quiet > quiet_timeout_s:
                reason = f"No progress for {quiet:.0f}s (limit {quiet_timeout_s:.0f}s)"
            elif elapsed > max_duration_s:
                reason = f"Read still running after {elapsed:.0f}s (limit {max_duration_s:.0f}s)"
            if reason:
                log.warning(f"  [COM/Read] {reason} — killing read subprocess.")
                try:
                    proc.kill()
                except Exception:
                    pass
                return None

    t_err.join(timeout=5)
    t_out.join(timeout=5)

    output = "".join(stdout_chunks)
    if _READ_RESULT_MARKER in output:
        return output.split(_READ_RESULT_MARKER, 1)[1].strip() == "1"
    if _READ_ERROR_MARKER in output:
        log.warning(f"  [COM/Read] Worker error: {output.split(_READ_ERROR_MARKER, 1)[1].strip()}")
        return None
    log.warning(f"  [COM/Read] Worker produced no result marker (exit={proc.returncode})")
    return None


# ---------------------------------------------------------------------------
# Subprocess-isolated rename (2026-08-19)
# ---------------------------------------------------------------------------
# rename_file_on_device()'s own read_file_from_device()/copy_file_to_device()
# calls don't accept a progress_cb, so this worker emits heartbeats from an
# independent timer thread instead of threading progress_cb through every
# inner call — the thread only ever prints, never tries to interrupt the
# blocked COM call itself (that's the supervisor's job, via a real process
# kill — see copy_file_to_device_isolated's docstring for why a thread alone
# can't do that part).

def _rename_worker_main(device_name: str, storage_name: str, folder_parts: list,
                         old_filename: str, new_filename: str) -> None:
    """Runs in a CHILD PROCESS (see rename_file_on_device_isolated). Mirrors
    _copy_worker_main's sync_state.open_db() self-init (2026-08-09 fix) —
    rename_file_on_device()'s internal re-upload step goes through the same
    copy_file_to_device() ambiguous-verify path, which needs sync_state open
    to mark-pending instead of delete-and-raise on an ambiguous cache read."""
    import threading as _rename_thr

    try:
        import sync_state as _ss_rename
        _ss_rename.open_db()
    except Exception as e:
        print(f"[Worker] sync_state.open_db() failed: {e}", file=sys.stderr, flush=True)

    stop_beat = _rename_thr.Event()

    def _beat_loop() -> None:
        while not stop_beat.wait(5):
            print(_RENAME_HEARTBEAT, file=sys.stderr, flush=True)

    beat_thread = _rename_thr.Thread(target=_beat_loop, daemon=True)
    beat_thread.start()

    try:
        ok = rename_file_on_device(
            device_name, storage_name, list(folder_parts), old_filename, new_filename,
        )
        print(f"{_RENAME_RESULT_MARKER}{'1' if ok else '0'}", flush=True)
    except Exception as e:
        print(f"{_RENAME_ERROR_MARKER}{type(e).__name__}: {e}", flush=True)
    finally:
        stop_beat.set()


def rename_file_on_device_isolated(
    device_name: str, storage_name: str, folder_parts: list,
    old_filename: str, new_filename: str,
    quiet_timeout_s: float = 150.0, max_duration_s: float = 420.0,
) -> "bool | None":
    """
    Subprocess-isolated variant of rename_file_on_device — same contract
    (returns True on success), but runs the ENTIRE rename (download -> local
    rename -> re-upload -> delete original) in a CHILD PROCESS (`python
    mtp_transfer_com.py --rename-worker ...`), supervised here with the same
    quiet-timeout + wall-clock dual-guard model as copy_file_to_device_isolated
    / file_exists_isolated. Wider default than a single copy's timeout — a
    rename does a full pull AND a push+verify (two potentially-slow steps, not
    one); a live 2026-08-19 confirmation run saw a single push's own verify
    alone take ~80s on this device, so 180s total was too tight for a routine
    (non-hung) rename and risked spurious timeouts.

    Returns None on any failure (subprocess wouldn't start, timed out and was
    killed, or the worker itself raised) — callers should treat None the same
    as any other "rename did not succeed" outcome, never as a silent success.
    """
    import json as _json_rename, threading as _thr_rename, time as _t_rename

    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--rename-worker",
             device_name, storage_name, _json_rename.dumps(list(folder_parts)),
             old_filename, new_filename],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.warning(f"  [COM/Rename] Could not start rename subprocess: {e}")
        return None

    started   = _t_rename.time()
    last_beat = [started]
    stdout_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            last_beat[0] = _t_rename.time()
            line = line.rstrip("\n")
            if line and line != _RENAME_HEARTBEAT:
                log.debug(line)

    def _drain_stdout() -> None:
        for line in proc.stdout:
            last_beat[0] = _t_rename.time()
            stdout_chunks.append(line)

    t_err = _thr_rename.Thread(target=_drain_stderr, daemon=True)
    t_out = _thr_rename.Thread(target=_drain_stdout, daemon=True)
    t_err.start()
    t_out.start()

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            now     = _t_rename.time()
            quiet   = now - last_beat[0]
            elapsed = now - started
            reason  = None
            if quiet > quiet_timeout_s:
                reason = f"No progress for {quiet:.0f}s (limit {quiet_timeout_s:.0f}s)"
            elif elapsed > max_duration_s:
                reason = f"Rename still running after {elapsed:.0f}s (limit {max_duration_s:.0f}s)"
            if reason:
                log.warning(
                    f"  [COM/Rename] {reason} — device likely hung/looping on a COM call. "
                    f"Killing rename subprocess."
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                return None

    t_err.join(timeout=5)
    t_out.join(timeout=5)

    output = "".join(stdout_chunks)
    if _RENAME_RESULT_MARKER in output:
        return output.split(_RENAME_RESULT_MARKER, 1)[1].strip() == "1"
    if _RENAME_ERROR_MARKER in output:
        log.warning(f"  [COM/Rename] Worker error: {output.split(_RENAME_ERROR_MARKER, 1)[1].strip()}")
        return None
    log.warning(f"  [COM/Rename] Worker produced no result marker (exit={proc.returncode})")
    return None


if __name__ == "__main__":
    # Child-process entry point for copy_file_to_device_isolated()'s
    # subprocess isolation — see _copy_worker_main's docstring for why this
    # runs out-of-process instead of in a thread.
    if len(sys.argv) >= 6 and sys.argv[1] == "--copy-worker":
        import json as _json_main
        logging.basicConfig(
            level=logging.INFO, format="%(message)s",
            handlers=[logging.StreamHandler(sys.stderr)], force=True,
        )
        _copy_worker_main(
            sys.argv[2], _json_main.loads(sys.argv[3]), sys.argv[4], sys.argv[5],
            overwrite=(len(sys.argv) >= 7 and sys.argv[6] == "1"),
        )
    elif len(sys.argv) >= 6 and sys.argv[1] == "--exists-worker":
        import json as _json_main2
        logging.basicConfig(
            level=logging.INFO, format="%(message)s",
            handlers=[logging.StreamHandler(sys.stderr)], force=True,
        )
        _exists_worker_main(
            sys.argv[2], sys.argv[3], _json_main2.loads(sys.argv[4]), sys.argv[5],
        )
    elif len(sys.argv) >= 7 and sys.argv[1] == "--verify-worker":
        import json as _json_main3
        logging.basicConfig(
            level=logging.INFO, format="%(message)s",
            handlers=[logging.StreamHandler(sys.stderr)], force=True,
        )
        _verify_worker_main(
            sys.argv[2], sys.argv[3], _json_main3.loads(sys.argv[4]),
            sys.argv[5], int(sys.argv[6]),
        )
    elif len(sys.argv) >= 5 and sys.argv[1] == "--ensure-folder-worker":
        import json as _json_main4
        logging.basicConfig(
            level=logging.INFO, format="%(message)s",
            handlers=[logging.StreamHandler(sys.stderr)], force=True,
        )
        _ensure_folder_worker_main(
            sys.argv[2], sys.argv[3], _json_main4.loads(sys.argv[4]),
        )
    elif len(sys.argv) >= 7 and sys.argv[1] == "--rename-worker":
        import json as _json_main5
        logging.basicConfig(
            level=logging.INFO, format="%(message)s",
            handlers=[logging.StreamHandler(sys.stderr)], force=True,
        )
        _rename_worker_main(
            sys.argv[2], sys.argv[3], _json_main5.loads(sys.argv[4]),
            sys.argv[5], sys.argv[6],
        )
    elif len(sys.argv) >= 7 and sys.argv[1] == "--read-worker":
        import json as _json_main6
        logging.basicConfig(
            level=logging.INFO, format="%(message)s",
            handlers=[logging.StreamHandler(sys.stderr)], force=True,
        )
        _read_worker_main(
            _json_main6.loads(sys.argv[2]), sys.argv[3], sys.argv[4],
            sys.argv[5], sys.argv[6],
        )