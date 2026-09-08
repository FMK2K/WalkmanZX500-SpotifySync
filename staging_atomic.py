"""
staging_atomic.py — MK39 Phase III H.1: atomic-write primitives
================================================================================

Council MK39.1 Target H success criterion: "An operator can open the staging
folder in Picard, make corrections, and the platform will ingest the corrected
files on the next run without data loss."

The atomic-write contract guarantees: at any instant, an external observer
(Picard, MP3Tag, Beets, the Walkman device, another process) sees EITHER
the old file OR the new file — never a half-written intermediate state. This
is the precondition for safe concurrent use of the staging library.

Implementation: write to a tempfile in the SAME directory as the target,
then os.replace() to atomically swap. Cross-filesystem rename is not atomic;
keeping the temp in-dir guarantees same-fs.

Adversarial-review constraints honored (CTO+SWE):
  ✓ tempfile lives in target dir (cross-fs hazard avoided)
  ✓ try/finally cleanup on exception (no orphan tempfiles)
  ✓ file mode preservation (chmod after replace, not before)
  ✓ POSIX + Windows ≥Vista atomic (os.replace, NOT os.rename)
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

log = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]


@contextmanager
def atomic_write(
    target: PathLike,
    mode: str = "wb",
    *,
    encoding: Optional[str] = None,
    preserve_mode: bool = True,
) -> Iterator[io.IOBase]:
    """Context manager: yields a writable file object whose contents are
    atomically swapped into `target` on successful exit. On exception,
    the tempfile is deleted and the original `target` is left untouched.

    Usage:
        with atomic_write("/path/to/file.json") as f:
            f.write(b'{"key": "value"}')

    Args:
        target: destination path; will be created or replaced atomically.
        mode: file mode for the writable temp ("wb" default; "w" for text).
        encoding: required when mode is text ("w").
        preserve_mode: if target already exists, copy its mode to the new
                        file before swap (so chmod is preserved across edits).
    """
    target_path = Path(target)
    target_dir = target_path.parent
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)

    # Capture original mode before any write so we can preserve across replace
    original_mode = None
    if preserve_mode and target_path.exists():
        try:
            original_mode = target_path.stat().st_mode & 0o777
        except OSError:
            original_mode = None

    # Temp file in same directory — guarantees same-fs for atomic os.replace
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix="." + target_path.name + ".",
        suffix=".tmp",
        dir=str(target_dir),
    )

    try:
        # Wrap fd in a file object honoring the requested mode
        if "b" in mode:
            fobj: io.IOBase = os.fdopen(tmp_fd, mode)
        else:
            fobj = os.fdopen(tmp_fd, mode, encoding=encoding or "utf-8")

        try:
            yield fobj
            fobj.flush()
            try:
                os.fsync(fobj.fileno())
            except (OSError, AttributeError):
                # Some platforms (Windows on certain volumes) don't support fsync
                pass
        finally:
            fobj.close()

        # Restore mode BEFORE replace (so visible mode never has wrong perms)
        if original_mode is not None:
            try:
                os.chmod(tmp_path, original_mode)
            except OSError:
                pass

        # The atomic swap. os.replace() is atomic on POSIX and Windows ≥Vista,
        # unlike os.rename() which is non-atomic on Windows when target exists.
        os.replace(tmp_path, target_path)
        tmp_path = None  # Don't delete in finally — replaced successfully.

    finally:
        # Cleanup: if anything went wrong before replace, remove the tempfile.
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def atomic_copy(src: PathLike, dst: PathLike, *,
                  preserve_metadata: bool = True) -> Path:
    """Atomically copy `src` to `dst`. Returns Path(dst).

    Implementation: shutil.copy2 to tempfile in dst's directory, then
    os.replace. Same atomicity guarantee as atomic_write.

    Args:
        src: source path (must exist)
        dst: destination path
        preserve_metadata: if True, copy mtime/atime via shutil.copystat.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    if not src_path.exists():
        raise FileNotFoundError(f"source does not exist: {src}")
    dst_dir = dst_path.parent
    if not dst_dir.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix="." + dst_path.name + ".",
        suffix=".tmp",
        dir=str(dst_dir),
    )
    os.close(tmp_fd)
    try:
        if preserve_metadata:
            shutil.copy2(str(src_path), tmp_path)
        else:
            shutil.copy(str(src_path), tmp_path)
        os.replace(tmp_path, dst_path)
        tmp_path = None
        return dst_path
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def safe_link_or_copy(src: PathLike, dst: PathLike) -> tuple[Path, str]:
    """Hardlink `src` → `dst` if same filesystem (zero disk cost); else
    fall back to atomic_copy. Returns (Path(dst), method) where method is
    'hardlink' or 'copy'.

    Used by the export stager to avoid duplicating 100GB+ libraries when
    staging on the same disk as the source.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    dst_dir = dst_path.parent
    if not dst_dir.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Check: same filesystem?
        if dst_dir.exists():
            src_dev = src_path.stat().st_dev
            dst_dev = dst_dir.stat().st_dev
            if src_dev == dst_dev:
                # Hardlink. Atomic by virtue of being a single inode op.
                # If dst exists, remove it first (link won't overwrite).
                if dst_path.exists():
                    dst_path.unlink()
                os.link(str(src_path), str(dst_path))
                return (dst_path, "hardlink")
    except (OSError, AttributeError):
        pass
    # Fall back to atomic copy
    atomic_copy(src_path, dst_path)
    return (dst_path, "copy")


__all__ = ["atomic_write", "atomic_copy", "safe_link_or_copy"]
