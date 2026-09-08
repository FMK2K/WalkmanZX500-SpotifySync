"""
session_guard.py  —  Session integrity & Windows Update awareness (Rev 14.3)
============================================================================
Addresses three failure modes that LOG-9 + the 2026-04-18 shutdown
analysis exposed:

  1. Windows Update auto-restart during a long overnight run.
     Event-log forensics on the 2026-04-18 incident showed three
     successive TrustedInstaller-initiated restarts in six minutes.
     Windows Update had queued updates during the sync, hit its forced
     install deadline, and rebooted — interrupting the run.  This had
     nothing to do with the agent itself; the agent was just the
     unattended workload running when Update decided to reboot.

  2. Interrupted runs leaving stale audit-folder-mode temp directories.
     `audit.py` creates `audit_folder_*` directories under the system
     temp dir (typically ~200 MB-2 GB each, full of pulled MP3/FLAC
     files).  When a run is interrupted mid-folder, these directories
     are orphaned.  Over time they consume significant disk space.

  3. Interrupted runs losing audit progress.
     Rev 14's `.sync_state.db` has ACID semantics, but the audit code
     was batching commits implicitly via the underlying SQLite
     auto-commit behaviour — under WAL mode a mid-run power loss can
     lose up to several minutes of work.  This module exposes a simple
     `mark_flush()` helper that forces a WAL checkpoint after each
     batch, so the cost of a mid-run restart is at most one batch's
     worth of work.

Public API
----------
    preflight_checks() -> dict
        Session-start diagnostics.  Returns a dict of {check_name:
        result} for the caller to log.  Warns if Windows Update has a
        pending restart, warns if stale temp dirs exceed
        STALE_TEMP_WARN_MB, cleans them if STALE_TEMP_AUTOCLEAN=True.

    check_windows_update_pending() -> tuple[bool, str]
        True + reason string if a reboot is pending.  Multi-source
        check: RebootRequired reg key, PendingFileRenameOperations,
        MsUsoClient pending state.  Best-effort — returns False on any
        error rather than blocking the run.

    clean_stale_audit_temps() -> tuple[int, int]
        Returns (num_dirs_removed, total_bytes_freed).  Only removes
        `audit_folder_*` / `walkman_audit_dl_*` directories older than
        STALE_TEMP_AGE_HOURS.

    mark_flush() -> None
        Forces a sync_state.db WAL checkpoint.  Call after each batch
        of audit commits.
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil
import tempfile
import time

import config

log = logging.getLogger(__name__)

# How old a temp dir must be before we consider it "stale" — a live run's
# audit_folder_* dirs are typically only a few minutes old at any moment,
# so 2 hours is very conservative.
STALE_TEMP_AGE_HOURS = getattr(config, "STALE_TEMP_AGE_HOURS", 2)

# Warn if stale temp dirs exceed this size.  Default 500 MB.
STALE_TEMP_WARN_MB = getattr(config, "STALE_TEMP_WARN_MB", 500)

# Automatically clean stale temps at session start.  Disabled by default
# because auto-delete of temp data feels aggressive without opt-in.
STALE_TEMP_AUTOCLEAN = getattr(config, "STALE_TEMP_AUTOCLEAN", False)


# ---------------------------------------------------------------------------
# Windows Update pending-restart detection
# ---------------------------------------------------------------------------

def check_windows_update_pending() -> tuple[bool, str]:
    """
    Multi-source check for "Windows Update wants to reboot."

    Three independent indicators, any one triggers True:
      1. HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\
         Auto Update\\RebootRequired key exists
      2. HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\
         PendingFileRenameOperations has entries
      3. Component-Based Servicing RebootPending key exists

    Returns (pending: bool, reason: str).  Never raises — registry
    access failures return (False, "check_failed: ...") so the caller
    can log but not block.
    """
    try:
        import winreg  # type: ignore
    except ImportError:
        return False, "winreg unavailable (non-Windows)"

    reasons: list[str] = []

    # 1. Windows Update's own RebootRequired flag
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
            0, winreg.KEY_READ,
        )
        winreg.CloseKey(key)
        reasons.append("WindowsUpdate RebootRequired key present")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.debug(f"  [session_guard] WU key probe failed: {e}")

    # 2. Pending file rename operations (common after CBS staging)
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
            0, winreg.KEY_READ,
        )
        try:
            val, _ = winreg.QueryValueEx(key, "PendingFileRenameOperations")
            if val and len(val) > 0:
                # The value is a multi-string; empty list still means no pending.
                # Count non-empty strings.
                nonempty = [s for s in val if s]
                if nonempty:
                    reasons.append(f"{len(nonempty)//2} pending file rename operation(s)")
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        log.debug(f"  [session_guard] PFRO probe failed: {e}")

    # 3. Component-Based Servicing pending reboot
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
            0, winreg.KEY_READ,
        )
        winreg.CloseKey(key)
        reasons.append("CBS RebootPending key present")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.debug(f"  [session_guard] CBS probe failed: {e}")

    if reasons:
        return True, "; ".join(reasons)
    return False, "no pending restart detected"


# ---------------------------------------------------------------------------
# Stale temp dir cleanup
# ---------------------------------------------------------------------------

_STALE_PREFIXES = ("audit_folder_", "walkman_audit_dl_", "wman_")


def _dir_size_bytes(path: pathlib.Path) -> int:
    """Return total size in bytes of *path* recursively.  0 on error.
    MK15-HF4: Hard timeout via threading so a junction-point loop or
    network path in the Windows temp dir cannot hang the pipeline.
    """
    import threading as _t
    result_box = [0]
    def _walk():
        total = 0
        try:
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        continue
        except Exception:
            pass
        result_box[0] = total
    t = _t.Thread(target=_walk, daemon=True)
    t.start()
    t.join(timeout=10)   # 10s max — prevents 13h freeze on junction loops
    return result_box[0]


def find_stale_audit_temps() -> list[tuple[pathlib.Path, int, float]]:
    """
    Locate abandoned audit temp directories.
    Returns list of (path, size_bytes, age_seconds) for each stale dir.
    """
    tempdir = pathlib.Path(tempfile.gettempdir())
    cutoff = time.time() - (STALE_TEMP_AGE_HOURS * 3600)

    found: list[tuple[pathlib.Path, int, float]] = []
    try:
        for entry in tempdir.iterdir():
            if not entry.is_dir():
                continue
            if not any(entry.name.startswith(p) for p in _STALE_PREFIXES):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue   # too recent — may be a live run
            size = _dir_size_bytes(entry)
            age_s = time.time() - mtime
            found.append((entry, size, age_s))
    except Exception as e:
        log.debug(f"  [session_guard] temp scan error: {e}")

    return found


def clean_stale_audit_temps() -> tuple[int, int]:
    """
    Remove all stale audit temp dirs.  Returns (num_removed, total_bytes).
    Errors on individual dirs are logged and skipped.
    """
    stale = find_stale_audit_temps()
    if not stale:
        return 0, 0

    removed = 0
    freed = 0
    for path, size, age_s in stale:
        try:
            shutil.rmtree(path, ignore_errors=False)
            removed += 1
            freed += size
            log.info(
                f"  [session_guard] Cleaned stale temp: {path.name} "
                f"({size/1024/1024:.1f} MB, age {age_s/3600:.1f}h)"
            )
        except Exception as e:
            log.warning(f"  [session_guard] Could not clean {path}: {e}")

    return removed, freed


# ---------------------------------------------------------------------------
# Sync-state checkpoint helper
# ---------------------------------------------------------------------------

def mark_flush() -> None:
    """
    Force a WAL checkpoint on the sync_state database.

    Call after each audit batch (typically AUDIT_BATCH_SIZE files) so an
    interrupted run loses at most one batch of work.  Safe to call even
    if sync_state isn't initialised — no-ops gracefully.
    """
    try:
        import sync_state as _ss
        # _conn() is internal but the whole module is internal — acceptable
        # for this helper.  We use PRAGMA wal_checkpoint(TRUNCATE) to force
        # all WAL pages into the main db file AND reset the WAL file.
        c = _ss._conn()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        log.debug(f"  [session_guard] mark_flush: {e}")


# ---------------------------------------------------------------------------
# Orchestrator — called once per session at start
# ---------------------------------------------------------------------------

def preflight_checks() -> dict:
    """
    Run the Rev 14.3 session-start integrity checks.  Logs everything
    at INFO or WARNING; returns a summary dict the caller can inspect.
    """
    result: dict = {}

    # ── 1. Windows Update pending-restart check ────────────────────────
    pending, reason = check_windows_update_pending()
    result["windows_update_pending"] = {"pending": pending, "reason": reason}
    if pending:
        log.warning(
            f"  [session_guard] ⚠ Windows Update has a pending restart: {reason}.  "
            "If you are starting a long overnight run, consider:  "
            "Settings → Windows Update → Pause updates (1 week), "
            "OR set Active Hours to cover your sync window."
        )
    else:
        log.info(f"  [session_guard] Windows Update check: {reason}")

    # ── 2. Stale temp dir scan ─────────────────────────────────────────
    stale = find_stale_audit_temps()
    total_mb = sum(s for _p, s, _a in stale) / 1024 / 1024
    result["stale_temps"] = {
        "count": len(stale),
        "total_mb": total_mb,
        "paths": [str(p) for p, _s, _a in stale],
    }

    if stale:
        msg = (
            f"  [session_guard] Found {len(stale)} stale audit temp dir(s) "
            f"totaling {total_mb:.1f} MB (older than {STALE_TEMP_AGE_HOURS}h)."
        )
        if total_mb >= STALE_TEMP_WARN_MB:
            log.warning(msg + f"  Exceeds warn threshold ({STALE_TEMP_WARN_MB} MB).")
        else:
            log.info(msg)

        if STALE_TEMP_AUTOCLEAN:
            removed, freed = clean_stale_audit_temps()
            log.info(
                f"  [session_guard] Auto-cleaned {removed} stale temp dir(s), "
                f"freed {freed/1024/1024:.1f} MB."
            )
            result["stale_temps_cleaned"] = {"count": removed, "freed_mb": freed/1024/1024}
        else:
            log.info(
                "  [session_guard] Auto-clean disabled (STALE_TEMP_AUTOCLEAN=False).  "
                "Run session_guard.clean_stale_audit_temps() manually, "
                "or enable auto-clean in config.py."
            )
    else:
        log.info("  [session_guard] No stale audit temp dirs found.")

    return result
