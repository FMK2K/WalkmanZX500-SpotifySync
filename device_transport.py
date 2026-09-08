"""
device_transport.py — MK23 Unified Device Transport Interface
==============================================================
Abstract DeviceTransport + ADBTransport concrete implementation.

MK23 invariant: ALL device file operations go through this interface.
No direct adb subprocess calls. No Shell.Application. No CopyHere.

Why ADB-first:
    - Deterministic: synchronous exit codes, no async copy queues
    - No UI: zero confirmation dialogs
    - Fast: up to 30 MB/s vs MTP's ~1.2 MB/s
    - Scriptable: CLI, predictable flags, timestamp preservation (-a)
    - Atomic: temp→rename on same partition = O(1) inode update

COM/MTP is retained ONLY for SD card operations where ADB push may
require additional routing, routed through the same DeviceTransport
interface — never called directly.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


# ─── Abstract base ─────────────────────────────────────────────────────────────

class DeviceTransport(ABC):
    """
    Single transport interface for all device file operations.

    MK23 architectural invariant: no file operation bypasses this class.
    The Executor calls only these methods. The Planner calls only list_files.
    """

    @abstractmethod
    def push_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = True,
        atomic: bool = True,
    ) -> bool:
        """
        Upload local_path to remote_path on device.

        If atomic=True: push to .tmp_<uuid>_<name> first, then rename.
        This ensures remote_path is either absent or complete — never partial.

        Raises:
            FileExistsError: if overwrite=False and remote_path exists
            IOError: if transfer fails
        """

    @abstractmethod
    def delete_file(self, remote_path: str) -> bool:
        """Delete remote_path. Returns True if deleted, False if already gone."""

    @abstractmethod
    def list_files(self, remote_path: str) -> list[dict]:
        """
        List files under remote_path.
        Returns list of dicts: {path, size_bytes, mtime, is_dir}.
        """

    @abstractmethod
    def file_exists(self, remote_path: str) -> bool:
        """Returns True if remote_path exists on device."""

    @abstractmethod
    def rename(self, remote_src: str, remote_dst: str) -> bool:
        """Rename/move remote_src to remote_dst (atomic on same partition)."""

    @abstractmethod
    def get_free_space_mb(self, storage_root: str) -> int:
        """Return available free space in MB for the storage containing storage_root."""

    @abstractmethod
    def create_snapshot(self, remote_path: str, timeout_s: int = 120) -> "DeviceSnapshot":
        """
        P1: Create a verified DeviceSnapshot by recursively scanning remote_path.
        If scan is interrupted (timeout, permission error, ADB disconnect) → complete=False.
        Planner will raise IncompleteDeviceScanError on an incomplete snapshot.
        """

    def cleanup_orphaned_temps(self, storage_root: str) -> int:
        """
        P10: Find and delete any .mk23_tmp_* files left by interrupted pushes.
        Run at startup before planning. Returns count of files deleted.
        """
        return 0   # default: no-op (implemented by concrete transports)


# ─── ADB Implementation ────────────────────────────────────────────────────────

class ADBTransport(DeviceTransport):
    """
    ADB-backed transport. Wraps the existing adb_transfer.py functions
    behind the DeviceTransport interface.

    Design choices:
    - All pushes default to atomic=True (temp→rename)
    - Timestamps preserved via `adb push -a` where available
    - No dialog suppression needed — ADB has no UI
    - delete uses `adb shell rm -f` — instant, no confirmation
    """

    def __init__(self, device_name: str = "WALKMAN", storage_name: str = "SD card"):
        self.device_name  = device_name
        self.storage_name = storage_name
        self._adb_cmd     = self._find_adb()

    def _find_adb(self) -> str:
        import shutil
        adb = shutil.which("adb")
        if not adb:
            raise RuntimeError("ADB not found on PATH. Install Android platform-tools.")
        return adb

    def _run(self, *args, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self._adb_cmd] + list(args)
        log.debug(f"[ADB] {' '.join(cmd)}")
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)

    def _shell(self, cmd: str, timeout: int = 30) -> str:
        r = self._run("shell", cmd, timeout=timeout, check=False)
        return r.stdout.strip()

    # ── Core interface ────────────────────────────────────────────────────────

    def push_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = True,
        atomic: bool = True,
    ) -> bool:
        """
        MK23 atomic push: local → .tmp_<uuid>_<name> → rename to final.

        Atomic guarantee: remote_path is either absent or complete.
        A power failure during push leaves only the .tmp file, which
        is cleaned up on the next preflight scan.
        """
        if not overwrite and self.file_exists(remote_path):
            raise FileExistsError(
                f"[ADBTransport] overwrite=False and remote file exists: {remote_path}"
            )

        if atomic:
            # Push to temp, then rename atomically
            remote_dir  = "/".join(remote_path.rsplit("/", 1)[:-1])
            filename    = remote_path.rsplit("/", 1)[-1]
            tmp_name    = f".mk23_tmp_{uuid.uuid4().hex[:8]}_{filename}"
            tmp_path    = f"{remote_dir}/{tmp_name}" if remote_dir else tmp_name

            try:
                t0 = time.monotonic()
                self._run("push", local_path, tmp_path)
                elapsed = time.monotonic() - t0

                # Verify size before rename
                local_size  = os.path.getsize(local_path)
                remote_size = self._get_remote_size(tmp_path)
                if remote_size != local_size:
                    self._shell(f"rm -f '{tmp_path}'")
                    raise IOError(
                        f"[ADBTransport] Size mismatch after push: "
                        f"local={local_size} remote={remote_size} path={remote_path}"
                    )

                # Atomic rename
                self._shell(f"mv '{tmp_path}' '{remote_path}'")
                log.info(
                    f"[ADB/push] ✓ {filename} ({local_size/1024/1024:.1f} MB "
                    f"in {elapsed:.1f}s)"
                )
                return True

            except Exception as e:
                # Cleanup temp on failure
                self._shell(f"rm -f '{tmp_path}'")
                raise

        else:
            # Non-atomic push (legacy fallback, not recommended)
            self._run("push", local_path, remote_path)
            return True

    def delete_file(self, remote_path: str) -> bool:
        """
        ADB rm -f: silent delete, no UI prompt, no confirmation dialog.
        Returns True if deleted, False if already absent.
        """
        if not self.file_exists(remote_path):
            log.debug(f"[ADB/delete] Already absent: {remote_path}")
            return False
        self._shell(f"rm -f '{remote_path}'")
        log.info(f"[ADB/delete] ✓ {remote_path}")
        return True

    def list_files(self, remote_path: str) -> list[dict]:
        """
        List files via `adb shell find`. Returns structured dicts.
        Faster than MTP enumerate for large directories.
        """
        # Use find with -printf for size + mtime
        output = self._shell(
            f"find '{remote_path}' -type f "
            f"-printf '%p\\t%s\\t%T@\\n' 2>/dev/null",
            timeout=120
        )
        results = []
        for line in output.splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 2:
                results.append({
                    "path":       parts[0],
                    "size_bytes": int(parts[1]) if parts[1].isdigit() else 0,
                    "mtime":      float(parts[2]) if len(parts) > 2 else 0.0,
                    "is_dir":     False,
                })
        return results

    def file_exists(self, remote_path: str) -> bool:
        out = self._shell(f"test -e '{remote_path}' && echo 1 || echo 0")
        return out.strip() == "1"

    def rename(self, remote_src: str, remote_dst: str) -> bool:
        """
        Atomic rename on device — O(1) if same partition.
        MK23 uses this for file moves within SD card (e.g. reorganize).
        """
        # Ensure destination directory exists
        dst_dir = "/".join(remote_dst.rsplit("/", 1)[:-1])
        if dst_dir:
            self._shell(f"mkdir -p '{dst_dir}'")
        self._shell(f"mv '{remote_src}' '{remote_dst}'")
        log.info(f"[ADB/rename] {remote_src} → {remote_dst}")
        return True

    def get_free_space_mb(self, storage_root: str) -> int:
        out = self._shell(f"df '{storage_root}'")
        import re
        m = re.search(r"(\d+)\s+\d+%", out)
        if m:
            return int(m.group(1))
        m2 = re.search(r"\d+\s+\d+\s+(\d+)", out)
        return int(m2.group(1)) // 1024 if m2 else 0

    def create_snapshot(self, remote_path: str, timeout_s: int = 120):
        """
        P1: Scan device and return a DeviceSnapshot with complete=True/False.
        On any ADB failure → complete=False rather than raising, so Planner
        can give a clear IncompleteDeviceScanError instead of a cryptic exception.
        """
        try:
            from sync_operations import DeviceSnapshot, FileState
        except ImportError:
            from sync_operations import DeviceSnapshot, FileState

        scan_id  = __import__("uuid").uuid4().hex[:8]
        files    = []
        complete = False
        error    = ""
        try:
            raw = self.list_files(remote_path)
            for f in raw:
                files.append(FileState(
                    path=f["path"], size_bytes=f["size_bytes"], mtime=f.get("mtime", 0.0),
                    storage=self.storage_name,
                ))
            complete = True
        except Exception as e:
            error = str(e)
            log.warning(f"[Snapshot] Incomplete scan for {remote_path}: {error}")

        snap = DeviceSnapshot(
            files=files, scan_id=scan_id, complete=complete,
            source="adb_scan", error_msg=error,
        )
        log.info(
            f"[Snapshot] scan_id={scan_id} complete={complete} "
            f"files={len(files)} path={remote_path}"
        )
        return snap

    def cleanup_orphaned_temps(self, storage_root: str) -> int:
        """
        P10: Remove .mk23_tmp_* files left by interrupted pushes.
        Call at startup before any planning or execution.
        """
        out = self._shell(
            f"find '{storage_root}' -name '.mk23_tmp_*' -type f 2>/dev/null"
        )
        paths = [p.strip() for p in out.splitlines() if p.strip()]
        deleted = 0
        for p in paths:
            self._shell(f"rm -f '{p}'")
            log.info(f"[Cleanup] Removed orphaned temp: {p}")
            deleted += 1
        if deleted:
            log.info(f"[Cleanup] P10: {deleted} orphaned .mk23_tmp_* files removed from {storage_root}")
        return deleted

    def _get_remote_size(self, remote_path: str) -> int:
        out = self._shell(f"stat -c %s '{remote_path}' 2>/dev/null || wc -c < '{remote_path}'")
        try:
            return int(out.strip().split()[0])
        except (ValueError, IndexError):
            return -1


# ─── COM/MTP Implementation ─────────────────────────────────────────────────────

class COMTransport(DeviceTransport):
    """
    COM/MTP-backed transport (Shell.Application via mtp_transfer_com.py).

    2026-07-19: added as an explicit OPT-IN alternative to the MK23 ADB-primary
    invariant above — NOT a replacement for it. get_transport()'s default
    remains "adb" unchanged. This exists for nights where ADB itself is
    unreachable/flaky but the underlying USB/MTP link is healthy (observed
    live 2026-07-18/19: `adb devices` cycling offline/absent repeatedly while
    Shell.Application enumeration — via main.py's diagnostic — stayed solid
    the whole time). The original MK23 reasoning above (deterministic exit
    codes, no dialogs, ~25x faster, atomic temp-rename) is still valid and is
    exactly why ADB stays the default; this class exists for the case where
    ADB simply isn't available right now.

    IMPORTANT path-semantics difference from ADBTransport: ADBTransport's
    remote_path is an ABSOLUTE Android filesystem path (e.g.
    "/storage/4A21-0000/Music/Artist/Album/track.flac"), because ADB shell
    commands operate on the device's real POSIX filesystem. MTP/COM has no
    concept of that filesystem at all — Shell.Application navigates a
    storage-root -> named-folder object hierarchy by NAME, never by absolute
    path (confirmed against mtp_transfer_com.py's own dest_parts/folder_parts
    convention, used throughout that module). So COMTransport's remote_path
    is instead a path RELATIVE TO self.storage_name, e.g.
    "Music/Artist/Album/track.flac". Callers selecting backend="com" must
    pass storage-relative paths — these are NOT interchangeable 1:1 with
    ADBTransport's absolute paths without translation.

    Reuses mtp_transfer_com.py's already-hardened primitives (subprocess
    isolation + dual timeout guards for copy/scan, built in Tasks 4-6 after
    real live-lock/dead-hang incidents) rather than making any new raw COM
    calls of its own.
    """

    def __init__(self, device_name: str = "WALKMAN", storage_name: str = "SD card"):
        self.device_name  = device_name
        self.storage_name = storage_name

    @staticmethod
    def _split(remote_path: str) -> tuple[list[str], str]:
        """Split a storage-relative path into (folder_parts, filename)."""
        parts = [p for p in remote_path.strip("/").split("/") if p]
        if not parts:
            raise ValueError("[COMTransport] empty remote_path")
        return parts[:-1], parts[-1]

    def push_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = True,
        atomic: bool = True,
    ) -> bool:
        """
        COM has its own verified-copy guarantee (_wait_for_stable_size /
        _verify_com_copy inside copy_file_to_device_isolated) rather than
        ADBTransport's temp-then-rename convention — `atomic` is accepted
        for interface compatibility but COM's own verification is used
        regardless of its value; there is no separate atomic mode to select.
        """
        import mtp_transfer_com as _com

        if not overwrite and self.file_exists(remote_path):
            raise FileExistsError(
                f"[COMTransport] overwrite=False and remote file exists: {remote_path}"
            )

        folder_parts, filename = self._split(remote_path)
        src = Path(local_path)
        # copy_file_to_device_isolated names the destination after src's own
        # filename — stage a renamed temp copy if remote_path wants a
        # different name than the local file already has.
        cleanup_dir = None
        if src.name != filename:
            import shutil as _shutil
            import tempfile as _tempfile
            cleanup_dir = Path(_tempfile.mkdtemp(prefix="com_transport_"))
            staged = cleanup_dir / filename
            _shutil.copy2(src, staged)
            src = staged

        try:
            result = _com.copy_file_to_device_isolated(
                src, folder_parts, self.device_name, self.storage_name,
                overwrite=overwrite,
            )
        finally:
            if cleanup_dir is not None:
                import shutil as _shutil2
                _shutil2.rmtree(cleanup_dir, ignore_errors=True)

        if result is not None:
            log.info(f"[COM/push] ✓ {filename} -> {remote_path}")
        return result is not None

    def delete_file(self, remote_path: str) -> bool:
        """
        2026-07-19: no longer pre-checks file_exists() — that was a redundant,
        unguarded raw COM call (see file_exists()'s docstring) on top of a
        call that already handles "already absent" safely and quickly on its
        own: delete_file_on_device (-> wpd_transfer.delete_file_ps) returns
        True when the target simply isn't there, per its own docstring, and
        does so inside a real 30s-bounded subprocess timeout. Removing the
        pre-check closes the hang risk entirely for this method rather than
        just bounding it.
        """
        import mtp_transfer_com as _com
        folder_parts, filename = self._split(remote_path)
        ok = _com.delete_file_on_device(
            self.device_name, self.storage_name, folder_parts, filename,
        )
        if ok:
            log.info(f"[COM/delete] ✓ {remote_path}")
        return ok

    def list_files(self, remote_path: str) -> list[dict]:
        """
        remote_path here is the storage-relative SUBFOLDER to scan (e.g.
        "Music"), not a single file — matches scan_device_audio's contract.
        Uses the hardened scan (WPD fast-path + per-storage timeout +
        Shell.Application fallback), never a raw unbounded COM enumeration.
        """
        import mtp_transfer_com as _com
        subfolder = remote_path.strip("/")
        raw = _com.scan_device_audio(self.device_name, [(self.storage_name, subfolder)])
        results = []
        for f in raw:
            path = "/".join(list(f.get("parts", [])) + [f["filename"]])
            results.append({"path": path, "size_bytes": 0, "mtime": 0.0, "is_dir": False})
        return results

    def file_exists(self, remote_path: str) -> bool:
        """
        2026-07-19: now goes through mtp_transfer_com.file_exists_isolated()
        (subprocess-isolated, dual-guard) instead of a raw in-process
        _navigate()/folder.Items() call — the previous implementation was a
        new, unguarded COM call despite this class's docstring claiming to
        reuse only already-hardened primitives.

        On an unknown result (subprocess couldn't start, timed out, or the
        folder genuinely doesn't exist so navigation raised) this defaults to
        True, not False: the only current caller is push_file()'s
        overwrite=False guard, where a false "doesn't exist" is the unsafe
        direction (it would let a should-have-been-blocked write proceed and
        potentially clobber something). Fail toward blocking, not toward
        silently overwriting.
        """
        import mtp_transfer_com as _com
        folder_parts, filename = self._split(remote_path)
        result = _com.file_exists_isolated(
            self.device_name, self.storage_name, folder_parts, filename,
        )
        if result is None:
            log.warning(
                f"[COM/exists] Could not determine existence for {remote_path!r} "
                f"(guard fired or subprocess failed) — assuming it may exist "
                f"(fail toward blocking a write, not toward silently overwriting)"
            )
            return True
        return result

    def rename(self, remote_src: str, remote_dst: str) -> bool:
        """
        Same-folder rename uses rename_file_on_device_isolated (download ->
        rename -> reupload, per that function's own implementation).
        Cross-folder moves (different folder_parts) have no COM primitive at
        all, so this falls back to pull -> push to the new location ->
        delete original, reusing read_file_from_device_isolated / push_file /
        delete_file rather than inventing new COM calls.

        2026-08-19: both the same-folder and cross-folder pull step now go
        through subprocess-isolated primitives (rename_file_on_device_isolated
        / read_file_from_device_isolated) — the raw, in-process versions were
        found live to hang 40+ minutes (blocked COM call, no timeout guard at
        all), the same "blocked COM call holds the GIL" class already fixed
        elsewhere (WPD scanner, isolated copy) but never applied to rename.
        """
        import mtp_transfer_com as _com
        src_parts, src_name = self._split(remote_src)
        dst_parts, dst_name = self._split(remote_dst)

        if src_parts == dst_parts:
            ok = bool(_com.rename_file_on_device_isolated(
                self.device_name, self.storage_name, src_parts, src_name, dst_name,
            ))
            if ok:
                log.info(f"[COM/rename] {remote_src} → {remote_dst}")
            return ok

        import tempfile as _tempfile
        tmp_dir = Path(_tempfile.mkdtemp(prefix="com_transport_move_"))
        tmp_local = tmp_dir / dst_name
        try:
            pulled = _com.read_file_from_device_isolated(
                src_parts, src_name, tmp_local, self.device_name, self.storage_name,
            )
            if not pulled:
                log.warning(f"[COM/rename] Could not pull {remote_src} for cross-folder move")
                return False
            if not self.push_file(str(tmp_local), remote_dst, overwrite=True):
                log.warning(f"[COM/rename] Could not push to {remote_dst} for cross-folder move")
                return False
            self.delete_file(remote_src)
            log.info(f"[COM/rename] (cross-folder) {remote_src} → {remote_dst}")
            return True
        finally:
            import shutil as _shutil3
            _shutil3.rmtree(tmp_dir, ignore_errors=True)

    def get_free_space_mb(self, storage_root: str) -> int:
        import mtp_transfer_com as _com
        free_bytes = _com.get_device_free_space(self.device_name, self.storage_name)
        if free_bytes is None or free_bytes < 0:
            return 0
        return free_bytes // (1024 * 1024)

    def create_snapshot(self, remote_path: str, timeout_s: int = 120):
        """
        P1-equivalent for COM: scan and return a DeviceSnapshot with
        complete=True/False — mirrors ADBTransport.create_snapshot's
        fail-soft contract (any scan failure -> complete=False rather than
        raising, so Planner gets a clear IncompleteDeviceScanError instead
        of a bare exception).
        """
        from sync_operations import DeviceSnapshot, FileState

        scan_id = uuid.uuid4().hex[:8]
        files: list = []
        complete = False
        error = ""
        try:
            raw = self.list_files(remote_path)
            for f in raw:
                files.append(FileState(
                    path=f["path"], size_bytes=f["size_bytes"], mtime=f.get("mtime", 0.0),
                    storage=self.storage_name,
                ))
            complete = True
        except Exception as e:
            error = str(e)
            log.warning(f"[COM/Snapshot] Incomplete scan for {remote_path}: {error}")

        snap = DeviceSnapshot(
            files=files, scan_id=scan_id, complete=complete,
            source="com_scan", error_msg=error,
        )
        log.info(
            f"[COM/Snapshot] scan_id={scan_id} complete={complete} "
            f"files={len(files)} path={remote_path}"
        )
        return snap

    def cleanup_orphaned_temps(self, storage_root: str) -> int:
        # COM's copy path uses its own verified-copy guarantee (see push_file
        # docstring), not ADBTransport's ".mk23_tmp_*" temp-then-rename
        # convention — there is nothing of that shape for COM to clean up.
        return 0


# ─── Null transport (dry-run / testing) ────────────────────────────────────────

class NullTransport(DeviceTransport):
    """
    Dry-run transport: logs all operations without performing IO.
    Used for plan preview and testing.
    """

    def __init__(self):
        self._files: dict = {}     # simulated device state
        self.calls:  list = []     # audit trail

    def push_file(self, local_path, remote_path, *, overwrite=True, atomic=True) -> bool:
        if not overwrite and remote_path in self._files:
            raise FileExistsError(f"NullTransport: overwrite=False, {remote_path} exists")
        self._files[remote_path] = {"size_bytes": os.path.getsize(local_path) if os.path.exists(local_path) else 0}
        self.calls.append(("push", local_path, remote_path))
        log.info(f"[DRY-RUN] PUSH {local_path} → {remote_path}")
        return True

    def delete_file(self, remote_path) -> bool:
        existed = remote_path in self._files
        self._files.pop(remote_path, None)
        self.calls.append(("delete", remote_path))
        log.info(f"[DRY-RUN] DELETE {remote_path}")
        return existed

    def list_files(self, remote_path) -> list:
        return [{"path": k, "size_bytes": v.get("size_bytes", 0), "mtime": 0.0, "is_dir": False}
                for k, v in self._files.items() if k.startswith(remote_path)]

    def file_exists(self, remote_path) -> bool:
        return remote_path in self._files

    def rename(self, remote_src, remote_dst) -> bool:
        if remote_src in self._files:
            self._files[remote_dst] = self._files.pop(remote_src)
        self.calls.append(("rename", remote_src, remote_dst))
        log.info(f"[DRY-RUN] RENAME {remote_src} → {remote_dst}")
        return True

    def get_free_space_mb(self, storage_root) -> int:
        return 32768

    def create_snapshot(self, remote_path: str, timeout_s: int = 120):
        from sync_operations import DeviceSnapshot, FileState
        files = [
            FileState(path=k, size_bytes=v.get("size_bytes", 0), mtime=0.0,
                      storage="null")
            for k, v in self._files.items()
            if k.startswith(remote_path)
        ]
        return DeviceSnapshot(files=files, source="null_transport", complete=True)

    def cleanup_orphaned_temps(self, storage_root: str) -> int:
        orphans = [k for k in self._files if ".mk23_tmp_" in k]
        for k in orphans:
            del self._files[k]
        return len(orphans)


# ─── Factory ───────────────────────────────────────────────────────────────────

def get_transport(
    backend: str = "adb",
    device_name: str = "WALKMAN",
    storage_name: str = "SD card",
    dry_run: bool = False,
) -> DeviceTransport:
    """
    MK23 transport factory. Returns exactly ONE transport backend.
    No fallback logic — if the selected transport is unavailable,
    raise explicitly rather than silently trying another method.

    Default remains "adb" (MK23's original invariant, still true today:
    deterministic exit codes, no dialogs, faster, atomic temp-rename).
    2026-07-19: "com" added as an explicit OPT-IN alternative (see
    COMTransport's docstring) for when ADB itself is unreachable but the
    underlying MTP link is healthy — callers must request it explicitly;
    nothing defaults to it.
    """
    if dry_run:
        log.info("[Transport] DRY-RUN mode — NullTransport active")
        return NullTransport()
    if backend == "adb":
        log.info("[Transport] ADBTransport active (primary, dialog-free)")
        return ADBTransport(device_name=device_name, storage_name=storage_name)
    if backend == "com":
        log.info("[Transport] COMTransport active (explicit opt-in, not MK23 default)")
        return COMTransport(device_name=device_name, storage_name=storage_name)
    raise ValueError(
        f"[Transport] Unknown backend '{backend}'. "
        f"Supported: 'adb' (MK23 default), 'com' (opt-in alternative), 'dry_run'."
    )
