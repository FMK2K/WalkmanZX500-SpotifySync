"""
mtp_transfer.py  —  MTP device I/O via ADB (primary) with COM fallback
=======================================================================
Tries ADB first for every operation; falls back to the original COM
implementation (mtp_transfer_com.py) if ADB is unavailable or fails.

Setup
-----
1. Rename your original mtp_transfer.py  →  mtp_transfer_com.py
2. Drop this file in its place as mtp_transfer.py
3. Drop adb_transfer.py in the same folder

If USE_ADB = False in config.py the COM path is used unconditionally.
If adb_transfer cannot be imported, COM is used automatically.
"""

import logging
import sys
import pathlib  # REV 14.8

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Respect config.USE_ADB flag (default True if config is absent / flag missing)
# ---------------------------------------------------------------------------
try:
    import config as _cfg
    _USE_ADB = getattr(_cfg, "USE_ADB", True)
    _ADB_SERIAL = getattr(_cfg, "ADB_DEVICE_SERIAL", None)
    _ADB_TIMEOUT = getattr(_cfg, "ADB_TIMEOUT", 120)
except ImportError:
    _USE_ADB = True
    _ADB_SERIAL = None
    _ADB_TIMEOUT = 120

# ---------------------------------------------------------------------------
# Try to import ADB transport
# ---------------------------------------------------------------------------
ADB_AVAILABLE = False
_NO_TRANSPORT = False   # MK13: set True when no transport is available
_adb = None
if _USE_ADB:
    try:
        import adb_transfer as _adb
        # Push config values into the module
        _adb.ADB_SERIAL  = _ADB_SERIAL
        _adb.ADB_TIMEOUT = _ADB_TIMEOUT
        # Only mark ADB available if the device is actually connected.
        # Previously ADB_AVAILABLE=True on import success alone, so every
        # transfer attempt hit a dead ADB path → logged a warning → fell
        # back to COM. That added latency for every single copy operation.
        # Now we check adb.devices() at startup; if disconnected, skip ADB
        # entirely so COM is used directly with zero retry overhead.
        try:
            if _adb.device_is_connected(""):
                ADB_AVAILABLE = True
                # INFO (was DEBUG): surfaces the selected transport in every
                # run's log.  Without this you can't tell from an INFO-level
                # log whether ADB is actually engaged — and that's exactly
                # the information needed when upload failures cascade.
                log.info("  [Transport] ADB transport active — device connected via USB Debugging")
            else:
                log.info(
                    "  [Transport] ADB found but device NOT connected "
                    "('adb devices' is empty) — using COM only. "
                    "Enable USB Debugging on device to activate ADB path."
                )
        except Exception as _e:
            log.info(f"  [Transport] ADB connectivity check failed ({_e}) — using COM only.")
    except ImportError:
        log.warning("  [Transport] adb_transfer not found — using COM fallback")
    except Exception as e:
        log.warning(f"  [Transport] adb_transfer import failed: {e} — using COM fallback")
else:
    log.debug("  [Transport] USE_ADB=False — using COM only")


def refresh_adb_availability() -> bool:
    """
    Re-check live ADB connectivity and update the module-level ADB_AVAILABLE
    flag used by every _with_fallback() call in this file.

    2026-07-26: ADB_AVAILABLE was only ever computed once, at import time —
    if ADB happened to be disconnected at that exact moment, every operation
    for the rest of the process used COM/WPD exclusively, even if ADB
    reconnected moments later. Confirmed live: a scan-duplicates run
    correctly logged "ADB found but device NOT connected" at startup, never
    reconsidered, and spent ~22 minutes exhausting the WPD live-lock guard
    (601s) plus Shell.Application timeouts on all 6 storage roots (120s
    each) before giving up — the ADB path was never retried even though nothing
    here specifically attempts a reconnect. Callers that want to attempt one
    first should call adb_transfer.reconnect() themselves, then this
    function, before starting a scan.
    """
    global ADB_AVAILABLE
    if not _USE_ADB or _adb is None:
        return ADB_AVAILABLE
    try:
        ADB_AVAILABLE = bool(_adb.device_is_connected(""))
        if ADB_AVAILABLE:
            log.info("  [Transport] ADB transport now active (reconnected)")
    except Exception as _e:
        log.debug(f"  [Transport] ADB re-check failed: {_e}")
    return ADB_AVAILABLE


# ---------------------------------------------------------------------------
# Import COM (original) transport — required as final fallback
# ---------------------------------------------------------------------------
COM_AVAILABLE = False
try:
    import mtp_transfer_com as _com
    COM_AVAILABLE = True
    log.debug("  [Transport] COM transport (mtp_transfer_com) loaded")
except ImportError:
    log.error(
        "  [Transport] mtp_transfer_com not found.\n"
        "  Please rename your original mtp_transfer.py → mtp_transfer_com.py"
    )
    if not ADB_AVAILABLE:
        # MK13/MK17: replaced sys.exit(1) with a deferred RuntimeError.
        # sys.exit() at module import time killed the entire process —
        # including all simulations and tests that import genre_graph
        # (which imports walkman_sync → mtp_transfer). Now we just mark
        # the module as transport-less; the error fires on the first
        # actual transfer call via _with_fallback().
        log.critical("  No transport available — transfers will fail at runtime.")
        _NO_TRANSPORT = True

# ---------------------------------------------------------------------------
# Fallback helper
# ---------------------------------------------------------------------------

def _with_fallback(adb_func, com_func, *args, **kwargs):
    """
    Call adb_func if ADB is available AND adb_func is not None;
    on any exception fall back to com_func.
    Raises RuntimeError if neither transport succeeds.

    FIX: guard adb_func against None — callers pass None when ADB is
    unavailable, previously this raised TypeError instead of falling through.
    """
    if ADB_AVAILABLE and adb_func is not None:
        try:
            return adb_func(*args, **kwargs)
        except Exception as e:
            log.warning(f"  [Transport] ADB failed ({e}); falling back to COM")
            # MK17 Patch 5: track COM fallback for run-summary
            try:
                _dl_mod = sys.modules.get("downloader")
                if _dl_mod is not None and hasattr(_dl_mod, "_XFER_COUNTERS_COM_FALLBACK"):
                    _dl_mod._XFER_COUNTERS_COM_FALLBACK[0] += 1
            except Exception:
                pass
    if COM_AVAILABLE and com_func is not None:
        return com_func(*args, **kwargs)
    raise RuntimeError("No transport available (ADB failed or unavailable, COM not installed)")


# ---------------------------------------------------------------------------
# REV 14: Workload-aware routing
# ---------------------------------------------------------------------------
# ADB is ~10–30× faster for bulk pushes but Sony's on-disconnect
# MediaStore scan piggybacks on COM-completed transfers.  For the audit
# re-stamp stage, where per-file visibility is more important than raw
# throughput, prefer COM.  Both paths still fall back to the other
# backend on failure — the routing only changes which one is tried first.
#
# `_workload_ctx` is a thread-local so nested functions inherit it via
# context managers (see set_workload() below).

import threading as _threading_rev14
_workload_ctx = _threading_rev14.local()


def set_workload(kind: str) -> None:
    """
    Declare the workload kind for subsequent MTP calls on THIS thread.

    Accepted values: "bulk", "audit", or "" to reset.  Called at the
    start of each stage by the caller — e.g. walkman_sync sets "bulk"
    before transferring, audit.py sets "audit" before its re-stamp loop.
    """
    _workload_ctx.kind = (kind or "").strip().lower()


def _route_for_workload() -> tuple[bool, bool]:
    """
    Return (try_adb_first, try_com_first) for the current workload.

    Exactly one of the returned flags is True when USE_BACKEND_ROUTING
    is on and a workload has been declared.  Otherwise defaults to
    (True, False) — ADB-first, matching legacy behaviour.
    """
    if not getattr(_cfg, "USE_BACKEND_ROUTING", True):
        return True, False
    kind = getattr(_workload_ctx, "kind", "") or ""
    if kind == "bulk":
        prefer = getattr(_cfg, "BULK_BACKEND", "adb")
    elif kind == "audit":
        prefer = getattr(_cfg, "AUDIT_BACKEND", "com")
    else:
        prefer = "adb"   # legacy default
    return (prefer == "adb"), (prefer == "com")


def _with_routed_fallback(adb_func, com_func, *args, **kwargs):
    """
    Like _with_fallback, but respects the current workload.  The
    non-preferred backend is still available as a fallback on failure.
    """
    adb_first, com_first = _route_for_workload()

    # COM-first path (audit workload by default)
    if com_first and COM_AVAILABLE and com_func is not None:
        try:
            return com_func(*args, **kwargs)
        except Exception as e:
            log.warning(f"  [Transport/audit] COM failed ({e}); trying ADB")
        if ADB_AVAILABLE and adb_func is not None:
            return adb_func(*args, **kwargs)
        raise RuntimeError("COM failed and ADB unavailable")

    # ADB-first path (bulk workload and legacy default)
    if adb_first and ADB_AVAILABLE and adb_func is not None:
        try:
            return adb_func(*args, **kwargs)
        except Exception as e:
            log.warning(f"  [Transport/bulk] ADB failed ({e}); falling back to COM")
            # MK17 Patch 5: track COM fallback for run-summary
            try:
                _dl_mod = sys.modules.get("downloader")
                if _dl_mod is not None and hasattr(_dl_mod, "_XFER_COUNTERS_COM_FALLBACK"):
                    _dl_mod._XFER_COUNTERS_COM_FALLBACK[0] += 1
            except Exception:
                pass
    if COM_AVAILABLE and com_func is not None:
        return com_func(*args, **kwargs)
    raise RuntimeError("No transport available (ADB failed or unavailable, COM not installed)")


# ---------------------------------------------------------------------------
# REV 14: Dialog-dismissal burst failover
# ---------------------------------------------------------------------------
# The dialog watcher now exposes a monotonic dismissals counter.  If a
# single MTP call causes >N dismissals, the shell stack is thrashing and
# we should failover to the other backend mid-session rather than
# continue triggering dialogs.  Burst threshold is configurable via
# config.DIALOG_BURST_THRESHOLD (default 5).

def _dismissals_snapshot() -> int:
    try:
        import dialog_watcher as _dw
        return _dw.get_dismissals_total()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Public API  —  identical signatures to the original mtp_transfer_com
# ---------------------------------------------------------------------------

def copy_file_to_device(src, dest_parts, device_name, storage_name, overwrite=False, device_index=None):
    # REV 14.8 — defer files above MAX_MTP_FILE_SIZE_MB
    try:
        import deferred_transfers as _defer
        try:
            _sz = pathlib.Path(str(src)).stat().st_size
        except Exception:
            _sz = 0
        if _sz and _defer.should_defer(_sz):
            dest_folder = storage_name + "/" + "/".join(dest_parts) if dest_parts else storage_name
            _defer.defer(src_path=src, size_bytes=_sz, dest_folder=dest_folder)
            return storage_name
    except ImportError:
        pass

    # 2026-07-17: try LocalSend (WiFi, no USB) first — added after a night of
    # MTP hangs/live-locks and ADB itself proving flaky. Only applies to
    # Internal storage (LocalSend's receiver has no SD card path) and only
    # ever attempted, never trusted blindly — any failure falls through to
    # the existing ADB/COM routed transport below unchanged.
    if getattr(_cfg, "LOCALSEND_ENABLED", False):
        try:
            import localsend_transfer as _ls
            if _ls.push_file_via_localsend(
                src, dest_parts, device_name, storage_name,
                overwrite=overwrite, device_index=device_index,
            ):
                return storage_name
        except Exception as e:
            log.debug(f"  [Transport] LocalSend attempt failed ({e}); falling back to ADB/COM")

    # REV 14: burst-failover guard wrapping the routed call.  If the
    # dialog watcher dismisses >DIALOG_BURST_THRESHOLD dialogs during a
    # single COM copy, the shell stack is thrashing — abort the retry
    # within this call and force ADB on the next call.
    before = _dismissals_snapshot()
    try:
        return _with_routed_fallback(
            _adb.copy_file_to_device if ADB_AVAILABLE else None,
            _com.copy_file_to_device if COM_AVAILABLE else None,
            src, dest_parts, device_name, storage_name, overwrite,
            device_index=device_index,  # REV 14.9 (C2)
        )
    finally:
        after = _dismissals_snapshot()
        burst = after - before
        if burst > int(getattr(_cfg, "DIALOG_BURST_THRESHOLD", 5)):
            log.warning(
                f"  [Transport] Dialog-burst detected: {burst} dismissals "
                f"during copy of {getattr(src, 'name', src)!r}. "
                "Next call will prefer the alternative backend."
            )
            # Flip the workload preference just for this thread — next
            # call routes the other way.  Reset happens on set_workload().
            kind = getattr(_workload_ctx, "kind", "") or ""
            _workload_ctx.kind = "audit" if kind == "bulk" else "bulk"


def read_file_from_device(src_parts, filename, dest_local, device_name, storage_name):
    return _with_fallback(
        _adb.read_file_from_device if ADB_AVAILABLE else None,
        _com.read_file_from_device if COM_AVAILABLE else None,
        src_parts, filename, dest_local, device_name, storage_name,
    )


def read_file_header_from_device(src_parts, filename, dest_local, device_name,
                                  storage_name, max_bytes=1_500_000):
    """
    Partial-pull facade for tag-only scoring (Pass 3 metadata completeness).
    ADB-only — no COM equivalent (Windows Shell COM copy has no byte-range
    API) — so when ADB is unavailable/fails this transparently falls back
    to a full pull via read_file_from_device() rather than raising.
    """
    if ADB_AVAILABLE and _adb is not None and hasattr(_adb, "read_file_header_from_device"):
        try:
            return _adb.read_file_header_from_device(
                src_parts, filename, dest_local, device_name, storage_name,
                max_bytes=max_bytes,
            )
        except Exception as e:
            log.warning(f"  [Transport] ADB header pull failed ({e}); falling back to full pull")
    return read_file_from_device(src_parts, filename, dest_local, device_name, storage_name)


def write_text_file_to_device(content, dest_parts, filename, device_name, storage_name):
    return _with_fallback(
        _adb.write_text_file_to_device if ADB_AVAILABLE else None,
        _com.write_text_file_to_device if COM_AVAILABLE else None,
        content, dest_parts, filename, device_name, storage_name,
    )


def rename_file_on_device(device_name, storage_name, folder_parts, old_filename, new_filename):
    # 2026-08-19: COM fallback branch uses the subprocess-isolated variant —
    # the raw mtp_transfer_com.rename_file_on_device() was found to hang 40+
    # minutes live (blocked COM call, no timeout guard at all). Given ADB has
    # a long history of flakiness in this project, this fallback branch has
    # likely been exercised for real in past sessions whenever ADB was down.
    return _with_fallback(
        _adb.rename_file_on_device if ADB_AVAILABLE else None,
        _com.rename_file_on_device_isolated if COM_AVAILABLE else None,
        device_name, storage_name, folder_parts, old_filename, new_filename,
    )


def rename_folder_on_device(device_name, storage_name, parent_parts, old_name, new_name):
    return _with_fallback(
        _adb.rename_folder_on_device if ADB_AVAILABLE else None,
        _com.rename_folder_on_device if COM_AVAILABLE else None,
        device_name, storage_name, parent_parts, old_name, new_name,
    )


def delete_empty_device_folders(device_name, storage_name, parent_parts):
    return _with_fallback(
        _adb.delete_empty_device_folders if ADB_AVAILABLE else None,
        _com.delete_empty_device_folders if COM_AVAILABLE else None,
        device_name, storage_name, parent_parts,
    )


def scan_device_audio(device_name, storage_roots):
    return _with_fallback(
        _adb.scan_device_audio if ADB_AVAILABLE else None,
        _com.scan_device_audio if COM_AVAILABLE else None,
        device_name, storage_roots,
    )


def get_device_free_space(device_name, storage_name):
    return _with_fallback(
        _adb.get_device_free_space if ADB_AVAILABLE else None,
        _com.get_device_free_space if COM_AVAILABLE else None,
        device_name, storage_name,
    )


def device_is_connected(device_name):
    return _with_fallback(
        _adb.device_is_connected if ADB_AVAILABLE else None,
        _com.device_is_connected if COM_AVAILABLE else None,
        device_name,
    )


def log_health_check(label):
    return _with_fallback(
        _adb.log_health_check if ADB_AVAILABLE else None,
        _com.log_health_check if COM_AVAILABLE else None,
        label,
    )


def _pick_storage_for_file(file_size, filename=None):
    # Rev 14.4: optional filename threaded through for verbose routing logs.
    # Both ADB and COM versions now accept (file_size, filename=None).
    #
    # 2026-09-03 (user direction): when STORAGE_BALANCED_PLACEMENT is on, route
    # new writes across Internal + SD by percent-used so both fill up
    # proportionally (adb_transfer.pick_storage_balanced). ADB-only — the COM
    # path has no balanced picker, so fall back to the primary/fallback policy
    # if ADB is unavailable or the balanced picker can't decide.
    try:
        import config as _cfg
        if getattr(_cfg, "STORAGE_BALANCED_PLACEMENT", False) and ADB_AVAILABLE:
            return _adb.pick_storage_balanced(file_size, filename)
    except Exception as e:
        log.warning(f"  [Router] balanced placement failed ({e}) — using primary/fallback policy")
    return _with_fallback(
        _adb._pick_storage_for_file if ADB_AVAILABLE else None,
        _com._pick_storage_for_file if COM_AVAILABLE else None,
        file_size, filename,
    )


def _get_storage_root(device_name, storage_name):
    return _with_fallback(
        _adb._get_storage_root if ADB_AVAILABLE else None,
        _com._get_storage_root if COM_AVAILABLE else None,
        device_name, storage_name,
    )


def _navigate(folder, parts, create=True):
    return _with_fallback(
        _adb._navigate if ADB_AVAILABLE else None,
        _com._navigate if COM_AVAILABLE else None,
        folder, parts, create,
    )


def reset_storage_cache():
    if ADB_AVAILABLE:
        _adb.reset_storage_cache()
    if COM_AVAILABLE:
        _com.reset_storage_cache()


def _invalidate_storage_cache():
    reset_storage_cache()


def reconnect() -> bool:
    """
    Attempt to recover a degraded MTP/ADB connection.

    For ADB (primary): runs `adb reconnect` and waits for the device.
    For COM (fallback): clears the storage cache so the next operation
    re-acquires the Shell.Application namespace fresh.

    Returns True if recovery was attempted (not a guarantee of success).
    Called by the FolderAudit pull loop when a file pull fails unexpectedly.
    """
    if ADB_AVAILABLE:
        return _adb.reconnect()
    # COM fallback: no persistent connection to reset, but clearing the
    # cache forces a fresh Shell.Application Namespace() call next time.
    if COM_AVAILABLE:
        _com.reset_storage_cache()
        log.info("  [MTP/COM] Storage cache cleared for reconnect")
    return True


# ---------------------------------------------------------------------------
# COM-only functions (pass through unchanged when COM is available)
# These are never called via ADB — they are Windows-shell-specific helpers.
# ---------------------------------------------------------------------------

def diagnose_mtp() -> str:
    if COM_AVAILABLE:
        return _com.diagnose_mtp()
    return "[diagnose_mtp] COM not available — ADB only mode"


def list_storage_roots(device_name: str) -> list:
    if COM_AVAILABLE:
        return _com.list_storage_roots(device_name)
    # ADB fallback: list /storage contents
    if ADB_AVAILABLE:
        out = _adb._adb_cmd(["shell", "ls", "/storage"])
        return [r for r in out.splitlines() if r.strip()]
    return []


def delete_file_on_device(device_name: str, storage_name: str,
                          folder_parts: list, filename: str) -> bool:
    """
    Delete a single file on the device via the active transport.

    FIX F2: exposes this operation through the proxy so callers (audit.py
    backtrack) can route through ADB shell rm or PS-subprocess delete
    instead of calling COM InvokeVerb("delete") in-process, which triggers
    Win11 confirmation dialogs.
    """
    return _with_fallback(
        _adb.delete_file_on_device if ADB_AVAILABLE else None,
        _com.delete_file_on_device if COM_AVAILABLE else None,
        device_name, storage_name, folder_parts, filename,
    )


def _item_audio_ext(item) -> str | None:
    """
    FIX 12: Expose _item_audio_ext through the proxy.

    audit.py calls mtp._item_audio_ext(item) to detect the audio extension
    of a Shell COM FolderItem during dedup sweeps and folder-coherence checks.
    This function only makes sense with COM Shell items (ADB mode works with
    plain path strings, not Shell objects).  Always delegate to the COM
    implementation.  If COM is unavailable, fall back to a suffix check on
    item.Name so callers degrade gracefully rather than crashing.
    """
    if COM_AVAILABLE:
        return _com._item_audio_ext(item)
    # ADB-only fallback: read Name attribute and check suffix
    try:
        from pathlib import Path as _Path
        _AUDIO = frozenset({
            ".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac",
            ".opus", ".wma", ".ape", ".wv", ".aiff",
        })
        name = getattr(item, "Name", "") or ""
        suffix = _Path(name).suffix.lower()
        return suffix if suffix in _AUDIO else None
    except Exception:
        return None


def rename_mojibake_folders(device_name: str, storage_roots: list) -> None:
    """
    Scan Music folders and rename any whose name is Latin-1/UTF-8 mojibake.
    e.g. "AminÃ©" → "Aminé"

    FIX B4: this function was missing from the proxy, causing AttributeError
    in Stage 8 audit (audit.py calls mtp.rename_mojibake_folders).
    """
    return _with_fallback(
        _adb.rename_mojibake_folders if ADB_AVAILABLE else None,
        _com.rename_mojibake_folders if COM_AVAILABLE else None,
        device_name, storage_roots,
    )


# StorageFullError re-exported so callers don't need to know which module it lives in
try:
    from mtp_transfer_com import StorageFullError   # noqa: F401
except ImportError:
    class StorageFullError(RuntimeError):           # noqa: F811
        """Both internal storage and SD card are full or absent."""
        pass


