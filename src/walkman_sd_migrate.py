"""
walkman_sd_migrate.py  —  Internal→SD Card Migration Utility  (MK9)
====================================================================
Addresses the critical space issue from LOG_17_4: the Walkman's internal
storage has dropped below 0.5 GB, Android's OS stability floor. The pipeline
could not free space because:

  1. Deletions weren't executing (DEDUP_MODE defaulted to "report")
  2. No atomic move-to-SD pipeline existed — files were just re-copied
     without verifying the copy or deleting the source

This module provides a hardened pipeline that:

  ① Audits internal storage for files that can safely move to SD
  ② Atomically copies each file to SD using ADB with size-verify
  ③ Triggers Android MediaScanner on the new SD path
  ④ Deletes the internal copy only after the SD copy is confirmed
  ⑤ Updates Sony's music player database via Settings→Update Database
     (automated via adb shell input tap if UI coords are known)
  ⑥ Reports a before/after space summary

Public API
----------
    migrate_internal_to_sd(
        dry_run=True,
        max_gb=None,          # cap total migration size, None = unlimited
        target_gb_free=2.0,   # stop once internal has >= this much free space
        cancelled_flag=None,
    ) -> MigrationReport

    check_internal_space() -> dict   # quick headroom check, no side effects

    trigger_media_scanner(paths: list[str]) -> bool  # ADB broadcast only

Called from preflight.py when internal space < config.MIN_FREE_SPACE_GB.
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config refs
# ---------------------------------------------------------------------------

def _cfg(attr, default=None):
    try:
        import config as _c
        return getattr(_c, attr, default)
    except Exception:
        return default

# ADB binary — bundled adb.exe takes priority, then PATH
_ADB_EXE = str(Path(__file__).parent / "adb.exe") if (
    Path(__file__).parent / "adb.exe"
).exists() else "adb"

# Internal storage mount (Android universal) and SD detection pattern
_INTERNAL_PREFIX = "/storage/emulated/0"
_AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wav", ".aiff", ".aif", ".ape", ".wv", ".wma", ".dsf", ".dff",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MigratedFile:
    internal_path: str
    sd_path: str
    size_bytes: int
    verified: bool
    deleted_source: bool
    error: str = ""


@dataclass
class MigrationReport:
    dry_run: bool
    internal_free_before: int  # bytes, -1 if unknown
    internal_free_after: int
    sd_free_before: int
    sd_free_after: int
    files_scanned: int
    files_migrated: int
    bytes_moved: int
    files_failed: int
    migrated: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        moved_mb = self.bytes_moved / (1024 ** 2)
        int_before_gb = self.internal_free_before / (1024 ** 3) if self.internal_free_before >= 0 else -1
        int_after_gb  = self.internal_free_after  / (1024 ** 3) if self.internal_free_after  >= 0 else -1
        return (
            f"Migration {'[DRY RUN] ' if self.dry_run else ''}complete: "
            f"{self.files_migrated} files moved ({moved_mb:.1f} MB). "
            f"Internal: {int_before_gb:.2f} GB → {int_after_gb:.2f} GB free. "
            f"Errors: {self.files_failed}."
        )


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def _adb(*args, timeout: int = 30, check: bool = True) -> str:
    """Run an adb command; return stdout as str."""
    cmd = [_ADB_EXE] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",   # MK11: cp1252 crashes on non-Latin filenames
            timeout=timeout, check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"adb {' '.join(args[:3])} failed (rc={result.returncode}): "
                f"{result.stderr[:200]}"
            )
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb {' '.join(args[:3])} timed out after {timeout}s")


def _adb_available() -> bool:
    """Return True if adb is on PATH and a device is connected."""
    try:
        out = _adb("devices", check=False)
        lines = [l for l in out.splitlines() if l.strip() and "daemon" not in l and "attached" not in l]
        return len(lines) > 1  # header + at least 1 device line
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Space probing
# ---------------------------------------------------------------------------

_DF_RE = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+%\s+(\S+)", re.MULTILINE)


def _parse_df(output: str) -> dict[str, tuple[int, int]]:
    """Return {mount_point: (total_bytes, avail_bytes)} for all df rows."""
    result = {}
    for m in _DF_RE.finditer(output or ""):
        total_kb = int(m.group(2))
        avail_kb = int(m.group(4))
        mount    = m.group(5)
        result[mount] = (total_kb * 1024, avail_kb * 1024)
    return result


def _get_free_bytes(mount_hint: str) -> int:
    """Query df -k for free bytes at a mount, or -1 on failure."""
    try:
        out = _adb("shell", "df", "-k", timeout=20, check=False)
        mounts = _parse_df(out)
        # Exact match first
        if mount_hint in mounts:
            return mounts[mount_hint][1]
        # Prefix match
        for mp, (_, avail) in mounts.items():
            if mp.startswith(mount_hint):
                return avail
        return -1
    except Exception as e:
        log.debug(f"[SDMigrate] df probe failed: {e}")
        return -1


def _find_sd_mount() -> str | None:
    """Return the /storage/XXXX-XXXX mount for the SD card, or None."""
    try:
        out = _adb("shell", "df", "-k", timeout=20, check=False)
        mounts = _parse_df(out)
        candidates = [
            mp for mp in mounts
            if mp.startswith("/storage/")
            and not mp.startswith("/storage/emulated")
            and not mp.startswith("/storage/self")
        ]
        if not candidates:
            return None
        # Pick largest partition (SD card is typically the biggest non-internal)
        candidates.sort(key=lambda mp: mounts[mp][0], reverse=True)
        return candidates[0]
    except Exception:
        return None


def check_internal_space() -> dict:
    """
    Quick internal storage headroom check.  No side effects.
    Returns {'free_bytes': int, 'free_gb': float, 'ok': bool, 'adb': bool}.
    """
    if not _adb_available():
        return {"free_bytes": -1, "free_gb": -1.0, "ok": None, "adb": False,
                "message": "ADB not connected — enable USB Debugging"}
    free = _get_free_bytes(_INTERNAL_PREFIX)
    min_floor = int(_cfg("MIN_FREE_SPACE_GB", 0.5) * 1024 ** 3)
    ok = free >= min_floor if free >= 0 else None
    return {
        "free_bytes": free,
        "free_gb":    round(free / (1024 ** 3), 3) if free >= 0 else -1.0,
        "ok":         ok,
        "adb":        True,
        "message":    (
            f"{free / (1024**3):.2f} GB free on internal" if free >= 0
            else "df probe returned no data"
        ),
    }


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------

def _list_internal_audio(music_subdir: str = "Music") -> list[dict]:
    """
    Return list of dicts {path, size_bytes} for audio files on internal storage.
    Only scans the Music subtree to avoid touching system files.
    """
    scan_root = posixpath.join(_INTERNAL_PREFIX, music_subdir)
    try:
        # Use find with -printf to get path and size atomically
        # Android's busybox find supports -printf on most ROMs; fallback to ls -la
        out = _adb(
            "shell", f"find {scan_root} -type f",
            timeout=60, check=False,
        )
        paths = [l.strip() for l in out.splitlines() if l.strip()]
        result = []
        for p in paths:
            ext = posixpath.splitext(p)[1].lower()
            if ext not in _AUDIO_EXTENSIONS:
                continue
            # Get file size via stat
            try:
                sz_out = _adb("shell", f"stat -c %s '{p}'", timeout=5, check=False)
                size = int(sz_out.strip()) if sz_out.strip().isdigit() else 0
            except Exception:
                size = 0
            result.append({"path": p, "size_bytes": size})
        return result
    except Exception as e:
        log.error(f"[SDMigrate] Failed to enumerate internal audio: {e}")
        return []


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

def _sd_dest_path(internal_path: str, sd_mount: str) -> str:
    """
    Map an internal path to its equivalent on the SD card.

    /storage/emulated/0/Music/Artist/Album/01.mp3
    → /storage/XXXX-XXXX/Music/Artist/Album/01.mp3
    """
    rel = posixpath.relpath(internal_path, _INTERNAL_PREFIX)
    return posixpath.join(sd_mount, rel)


def _copy_to_sd(src: str, dst: str) -> bool:
    """
    Copy src→dst on device using cp, creating parent dirs.
    Returns True if successful and sizes match.
    """
    # Create destination directory
    dst_dir = posixpath.dirname(dst)
    _adb("shell", f"mkdir -p '{dst_dir}'", timeout=10, check=False)

    # Copy
    _adb("shell", f"cp '{src}' '{dst}'", timeout=120, check=False)

    # Verify by size comparison
    try:
        src_sz = _adb("shell", f"stat -c %s '{src}'", timeout=5, check=False).strip()
        dst_sz = _adb("shell", f"stat -c %s '{dst}'", timeout=5, check=False).strip()
        if src_sz and dst_sz and src_sz == dst_sz and src_sz != "0":
            return True
        log.warning(f"[SDMigrate] Size mismatch: src={src_sz} dst={dst_sz} for {posixpath.basename(src)}")
        return False
    except Exception as e:
        log.warning(f"[SDMigrate] Verify failed for {posixpath.basename(src)}: {e}")
        return False


def trigger_media_scanner(paths: list[str]) -> bool:
    """
    Trigger the Sony Walkman Music app to rescan its private library DB.

    The Walkman app uses its own SQLite database (PlayerMediaProvider /
    media.db) — it does NOT read Android's MediaStore. The effective trigger
    is MEDIA_MOUNTED sent to the SD card root, which SystemActionReceiver
    forwards to MediaScannerService.startScan(). As a fallback the session
    ends with walkman_app_refresh.refresh() (ui_tap mode).

    The old per-file MEDIA_SCANNER_SCAN_FILE broadcasts have been removed —
    they only updated Android's MediaStore and had zero effect on the Walkman.

    Returns True if the MEDIA_MOUNTED broadcast was sent (or refresh ran).
    """
    # Send MEDIA_MOUNTED for the SD root — received by SystemActionReceiver
    # which starts MediaScannerService. No per-file loop needed.
    sd = _find_sd_mount()
    if sd:
        try:
            _adb(
                "shell", "am", "broadcast",
                "-a", "android.intent.action.MEDIA_MOUNTED",
                "-d", f"file://{sd}",
                timeout=15, check=False,
            )
            log.info(f"[SDMigrate] Walkman rescan triggered via MEDIA_MOUNTED for {sd}")
            return True
        except Exception as e:
            log.debug(f"[SDMigrate] MEDIA_MOUNTED broadcast failed: {e}")

    # Fallback: UI-tap refresh
    try:
        import walkman_app_refresh as _war
        return _war.refresh()
    except Exception as e:
        log.debug(f"[SDMigrate] walkman_app_refresh fallback failed: {e}")
    return False


def _prompt_sony_db_rebuild():
    """
    Log a prominent reminder to rebuild Sony's internal music DB.
    We cannot automate this without root; UI-automation is firmware-fragile.
    """
    log.warning(
        "[SDMigrate] IMPORTANT — Sony music player DB refresh required:\n"
        "  On your Walkman go to:  Settings → Library → Update Database\n"
        "  (or Settings → Device Settings → Rebuild Database for a full rebuild).\n"
        "  The stock Music app will NOT see the newly migrated files until you do this.\n"
        "  Third-party apps (VLC, Poweramp) are refreshed via MediaStore automatically."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def migrate_internal_to_sd(
    dry_run: bool = True,
    max_gb: float | None = None,
    target_gb_free: float = 2.0,
    music_subdir: str = "Music",
    cancelled_flag: list | None = None,
) -> MigrationReport:
    """
    Move audited audio files from internal storage to SD card.

    Parameters
    ----------
    dry_run : bool
        If True (default), log what would happen without moving anything.
    max_gb : float | None
        Maximum total data to migrate in GB. None = unlimited.
    target_gb_free : float
        Stop migrating once internal has this many GB free.
    music_subdir : str
        Subdirectory of /storage/emulated/0/ to scan (default: "Music").
    cancelled_flag : list | None
        [False] mutable flag; set to True externally to interrupt the run.

    Returns
    -------
    MigrationReport
    """
    log.info(f"[SDMigrate] Starting {'DRY RUN ' if dry_run else ''}migration "
             f"(target: {target_gb_free:.1f} GB free on internal)")

    report = MigrationReport(
        dry_run=dry_run,
        internal_free_before=-1,
        internal_free_after=-1,
        sd_free_before=-1,
        sd_free_after=-1,
        files_scanned=0,
        files_migrated=0,
        bytes_moved=0,
        files_failed=0,
    )

    if not _adb_available():
        msg = "ADB not connected — enable USB Debugging on the Walkman and retry"
        log.error(f"[SDMigrate] {msg}")
        report.errors.append(msg)
        return report

    # Probe initial free space
    report.internal_free_before = _get_free_bytes(_INTERNAL_PREFIX)
    sd_mount = _find_sd_mount()

    if not sd_mount:
        msg = "SD card not found in df output — insert the card and retry"
        log.error(f"[SDMigrate] {msg}")
        report.errors.append(msg)
        return report

    report.sd_free_before = _get_free_bytes(sd_mount)
    log.info(
        f"[SDMigrate] Internal free: {report.internal_free_before / (1024**3):.2f} GB | "
        f"SD free: {report.sd_free_before / (1024**3):.2f} GB (at {sd_mount})"
    )

    # Enumerate audio files on internal
    log.info(f"[SDMigrate] Scanning {_INTERNAL_PREFIX}/{music_subdir} ...")
    files = _list_internal_audio(music_subdir)
    report.files_scanned = len(files)
    log.info(f"[SDMigrate] Found {len(files)} audio files on internal storage")

    # Sort by size descending so we free the most space fastest
    files.sort(key=lambda f: f["size_bytes"], reverse=True)

    target_free_bytes    = int(target_gb_free * 1024 ** 3)
    max_bytes            = int(max_gb * 1024 ** 3) if max_gb else float("inf")
    bytes_migrated_total = 0
    scanner_paths: list[str] = []

    for i, f in enumerate(files):
        if cancelled_flag and cancelled_flag[0]:
            log.info("[SDMigrate] Cancelled by caller")
            break

        # Check if we've already freed enough space
        current_free = _get_free_bytes(_INTERNAL_PREFIX)
        if current_free >= target_free_bytes:
            log.info(
                f"[SDMigrate] Target reached: {current_free / (1024**3):.2f} GB free "
                f">= {target_gb_free:.1f} GB goal. Stopping."
            )
            break

        if bytes_migrated_total >= max_bytes:
            log.info(f"[SDMigrate] max_gb cap reached ({max_gb:.1f} GB). Stopping.")
            break

        src_path = f["path"]
        dst_path = _sd_dest_path(src_path, sd_mount)
        size     = f["size_bytes"]
        fname    = posixpath.basename(src_path)

        if dry_run:
            log.info(f"  [DRY RUN] Would move: {fname} ({size / (1024**2):.1f} MB)")
            report.migrated.append(asdict(MigratedFile(
                internal_path=src_path, sd_path=dst_path, size_bytes=size,
                verified=False, deleted_source=False, error="DRY RUN",
            )))
            report.files_migrated += 1
            report.bytes_moved    += size
            bytes_migrated_total  += size
            continue

        # Check SD has room
        sd_free = _get_free_bytes(sd_mount)
        if sd_free >= 0 and sd_free < size + 100 * 1024 * 1024:  # need file + 100MB headroom
            log.warning(
                f"[SDMigrate] SD card low on space ({sd_free / (1024**3):.2f} GB). "
                "Stopping migration."
            )
            report.errors.append(f"SD card full after {report.files_migrated} files")
            break

        # ── Atomic copy → verify → delete ────────────────────────────────────
        log.info(
            f"[SDMigrate] [{i+1}/{len(files)}] Copying {fname} "
            f"({size / (1024**2):.1f} MB) → SD"
        )
        try:
            copied = _copy_to_sd(src_path, dst_path)
            if not copied:
                raise RuntimeError("Copy verification failed (size mismatch)")

            # Delete source only after verified copy
            _adb("shell", f"rm '{src_path}'", timeout=15, check=False)

            # Check source is gone
            ls_out = _adb("shell", f"ls '{src_path}' 2>/dev/null", timeout=5, check=False)
            deleted = len(ls_out.strip()) == 0 or "No such file" in ls_out

            scanner_paths.append(dst_path)
            record = MigratedFile(
                internal_path=src_path, sd_path=dst_path,
                size_bytes=size, verified=True,
                deleted_source=deleted,
            )
            report.migrated.append(asdict(record))
            report.files_migrated    += 1
            report.bytes_moved       += size
            bytes_migrated_total     += size
            log.info(
                f"  ✓ {fname}: moved {size/(1024**2):.1f} MB to SD "
                f"{'(source deleted)' if deleted else '(WARNING: source delete failed)'}"
            )

        except Exception as e:
            log.error(f"  ✗ {fname}: migration failed — {e}")
            report.files_failed += 1
            report.errors.append(f"{fname}: {e}")
            # Clean up any partial SD copy
            try:
                _adb("shell", f"rm -f '{dst_path}'", timeout=5, check=False)
            except Exception:
                pass

        # Brief pause between files to let the MTP stack breathe
        time.sleep(0.1)

    # Final space probe
    report.internal_free_after = _get_free_bytes(_INTERNAL_PREFIX)
    report.sd_free_after       = _get_free_bytes(sd_mount)

    # Trigger MediaScanner for all successfully migrated files
    if scanner_paths and not dry_run:
        log.info(f"[SDMigrate] Triggering MediaScanner for {len(scanner_paths)} files ...")
        trigger_media_scanner(scanner_paths)
        _prompt_sony_db_rebuild()

    log.info(f"[SDMigrate] {report.summary()}")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Migrate internal Walkman storage to SD card via ADB"
    )
    parser.add_argument("--execute",   action="store_true",
                        help="Actually move files (default: dry run)")
    parser.add_argument("--max-gb",    type=float, default=None,
                        help="Cap total migration in GB")
    parser.add_argument("--target-gb", type=float, default=2.0,
                        help="Stop when internal has this many GB free (default: 2.0)")
    parser.add_argument("--check",     action="store_true",
                        help="Just check internal space and exit")
    parser.add_argument("--scan-only", action="store_true",
                        help="Trigger MediaScanner for Music/ on SD card and exit")
    args = parser.parse_args()

    if args.check:
        info = check_internal_space()
        print(f"Internal storage: {info['message']}")
        print(f"ADB connected: {info['adb']}")
        raise SystemExit(0 if info.get("ok") else 1)

    if args.scan_only:
        sd = _find_sd_mount()
        if sd:
            paths = [posixpath.join(sd, "Music")]
            trigger_media_scanner(paths)
            _prompt_sony_db_rebuild()
        else:
            print("SD card not found")
        raise SystemExit(0)

    report = migrate_internal_to_sd(
        dry_run=not args.execute,
        max_gb=args.max_gb,
        target_gb_free=args.target_gb,
    )
    print(report.summary())
    if report.errors:
        print("Errors:")
        for e in report.errors:
            print(f"  • {e}")
    raise SystemExit(0 if report.files_failed == 0 else 1)
