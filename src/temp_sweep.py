"""
temp_sweep.py — periodic local-temp cleanup for long device operations.

2026-09-03 (user direction): a long audit or a big download run stages every
pulled / downloaded file under %TEMP% before pushing it to the Walkman. Those
local copies are dead weight the moment the device push succeeds, but the
per-operation rmtree can silently fail (locked handle, AV scan) and the run
can outlast the drive — an audit crashed with [Errno 28] at ~19 GB free.

This module gives every such run ONE shared sweep:
  - matches Python's tmpXXXXXXXX staging dirs + the project's known prefixes
  - never touches anything younger than `min_age_s` (the batch in flight)
  - best-effort: a file still in use is left alone
Callers invoke it on their own cadence (every N albums / N tracks) and also
whenever free space drops below a low-water mark.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

# Python's tempfile.mkdtemp/mkstemp default names: "tmp" + 8 chars [a-z0-9_].
_TMP_NAME_RE = re.compile(r"^tmp[a-z0-9_]{8}$")

# Project staging-dir prefixes (downloader album dirs, audit prefetch, stream
# captures, per-provider temp roots).
DEFAULT_PREFIXES = (
    "walkman_audit_dl_", "audit_folder_", "walkman_dl_", "walkman_tmp",
    "walkman_check", "album_", "prov_", "wm_dl_", "spotdl_", "streamrip_",
)


def pc_free_gb(path: "str | Path | None" = None) -> float:
    """Free GiB on the drive holding `path` (default: the temp dir)."""
    try:
        return shutil.disk_usage(str(path or tempfile.gettempdir())).free / (1024 ** 3)
    except Exception:
        return 0.0


def sweep_stale_staging(
    reason: str = "",
    *,
    log_callback: Optional[Callable[[str], None]] = None,
    min_age_s: float = 90.0,
    prefixes: tuple = DEFAULT_PREFIXES,
    extra_roots: tuple = (),
) -> dict:
    """
    Remove stale staging dirs/files from %TEMP% (and any `extra_roots`).

    Returns {"removed": int, "freed_gb": float, "free_before_gb", "free_after_gb"}.
    """
    roots = [Path(tempfile.gettempdir())] + [Path(r) for r in extra_roots]
    roots = list(dict.fromkeys(p for p in roots if p.is_dir()))
    now = time.time()
    primary = roots[0] if roots else Path(tempfile.gettempdir())
    before = pc_free_gb(primary)
    removed = 0
    freed = 0

    for root in roots:
        try:
            entries = list(root.iterdir())
        except Exception:
            continue
        for e in entries:
            name = e.name
            if not (_TMP_NAME_RE.match(name) or name.startswith(prefixes)):
                continue
            try:
                if now - e.stat().st_mtime < min_age_s:
                    continue
            except Exception:
                continue
            try:
                if e.is_dir():
                    sz = 0
                    for f in e.rglob("*"):
                        try:
                            if f.is_file():
                                sz += f.stat().st_size
                        except Exception:
                            pass
                    shutil.rmtree(e, ignore_errors=True)
                    if not e.exists():
                        removed += 1
                        freed += sz
                else:
                    sz = e.stat().st_size
                    e.unlink()
                    removed += 1
                    freed += sz
            except Exception:
                pass  # in use / permission — skip

    after = pc_free_gb(primary)
    result = {
        "removed": removed,
        "freed_gb": freed / (1024 ** 3),
        "free_before_gb": before,
        "free_after_gb": after,
    }
    if removed and log_callback:
        try:
            log_callback(
                f"  [TempSweep{(' ' + reason) if reason else ''}] removed "
                f"{removed} stale staging item(s), ~{result['freed_gb']:.1f} GB — "
                f"PC free {before:.1f} -> {after:.1f} GB"
            )
        except Exception:
            pass
    return result
