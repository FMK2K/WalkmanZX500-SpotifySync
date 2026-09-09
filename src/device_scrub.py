"""
device_scrub.py — Rev 14.9
===========================
Pre-flight device scrub.  Runs once before Stage 0 downloads begin.

Cleans up legacy corruption from pre-14.9 runs:

  (1) File-vs-folder blockers.  The ADB contract bug fixed in Rev 14.8
      left files sitting at paths where album folders need to be, because
      pre-14.8 code pushed directories to directories.  Those files block
      mkdir -p and produce the "Not a directory" error pattern that
      destroyed LOG-13 (177/177 ADB failures).

  (2) Zero-byte stub files.  Failed CopyHere calls on the Walkman's MTP
      filesystem sometimes leave behind a zero-byte file with the target
      name.  These look real to the device library scanner but are useless
      to playback, and they trigger exists() hits in the sync that skip
      re-downloading the actual track.

Scrub deletes both classes automatically, per user request, with a
WARNING log line per deletion.  No quarantine; no dry-run default.  A
summary line is emitted at the end.

The scrub uses the device_index (populated by refresh() earlier in the
same preflight sequence) to answer blocker questions in O(1) instead of
issuing one adb probe per planned album folder.
"""
from __future__ import annotations

import logging
import posixpath
from typing import Iterable

log = logging.getLogger(__name__)


def scrub_device(
    index,                              # DeviceIndex from device_index.py
    planned_dirs: Iterable[str],        # from sync_planner.SyncPlan.planned_dirs
    delete_zero_byte_stubs: bool = True,
) -> dict:
    """
    Execute the scrub.  Returns a summary dict:
      {
        "blocker_files_removed": int,
        "zero_byte_removed":     int,
        "blockers":              [str, ...],   # paths that were removed
        "zero_byte_files":       [str, ...],
      }

    The index is mutated in-place (removed paths are also removed from
    the index so subsequent queries stay consistent).
    """
    try:
        from adb_transfer import _adb_cmd, _shell_quote
    except Exception as e:
        log.warning(f"[Scrub] adb_transfer unavailable: {e}")
        return {
            "blocker_files_removed": 0,
            "zero_byte_removed":     0,
            "blockers":              [],
            "zero_byte_files":       [],
        }

    if not index.is_refreshed():
        log.warning("[Scrub] Device index is not populated; skipping scrub")
        return {
            "blocker_files_removed": 0,
            "zero_byte_removed":     0,
            "blockers":              [],
            "zero_byte_files":       [],
        }

    # ── (1) Find file blockers: planned dir paths that exist as files ──
    blockers = index.blocking_files_for_dirs(planned_dirs)

    # ── (2) Find zero-byte stub files ─────────────────────────────────
    zero_byte_files: list[str] = []
    if delete_zero_byte_stubs:
        zero_byte_files = index.zero_byte_files()

    if not blockers and not zero_byte_files:
        log.info("[Scrub] Device is clean — no blockers or zero-byte stubs found")
        return {
            "blocker_files_removed": 0,
            "zero_byte_removed":     0,
            "blockers":              [],
            "zero_byte_files":       [],
        }

    log.warning(
        f"[Scrub] Starting cleanup: {len(blockers)} file-blocker(s), "
        f"{len(zero_byte_files)} zero-byte stub(s)"
    )

    # ── Delete blockers individually so each deletion is logged ───────
    removed_blockers: list[str] = []
    for path in blockers:
        try:
            # Include file size in the log for forensic clarity
            sz = index.file_size(path)
            sz_str = f" ({sz} bytes)" if sz is not None else ""
            log.warning(
                f"[Scrub] Removing file blocking planned folder: "
                f"{path}{sz_str}"
            )
            _adb_cmd(
                ["shell", "rm", "-f", path],
                timeout=30, check=False,
            )
            index.remove(path)
            removed_blockers.append(path)
        except Exception as e:
            log.warning(f"[Scrub] Failed to remove blocker {path!r}: {e}")

    removed_stubs: list[str] = []
    for path in zero_byte_files:
        try:
            log.warning(f"[Scrub] Removing zero-byte stub: {path}")
            _adb_cmd(
                ["shell", "rm", "-f", path],
                timeout=30, check=False,
            )
            index.remove(path)
            removed_stubs.append(path)
        except Exception as e:
            log.warning(f"[Scrub] Failed to remove stub {path!r}: {e}")

    log.warning(
        f"[Scrub] Cleanup complete: "
        f"{len(removed_blockers)} blocker(s) removed, "
        f"{len(removed_stubs)} zero-byte stub(s) removed"
    )

    return {
        "blocker_files_removed": len(removed_blockers),
        "zero_byte_removed":     len(removed_stubs),
        "blockers":              removed_blockers,
        "zero_byte_files":       removed_stubs,
    }
