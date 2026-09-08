"""
device_index.py — Rev 14.9
===========================
In-memory, session-scoped snapshot of the Walkman's /Music tree.

Why: pre-14.9, every transfer burned multiple ADB round-trips asking the
device questions about existing state (`_exists` probes in adb_transfer,
`_wait_for_file` polling in mtp_transfer_com, folder-exists checks in
walkman_reorganize).  LOG-13 captured 177 transfers × ~3 device probes
each = 500+ wasted round-trips in a single run.  One `adb shell find` at
session start populates an in-memory index that answers the same
questions in O(1) from memory.

What this IS:
  * Ephemeral.  Rebuilt every session.  No disk persistence.  No cache file.
  * Size-aware for files (populated from find output), but not hash-aware.
  * Mutation-tracked: transports call mark_file / mark_dir / remove after
    writes so the index reflects reality as the session progresses.

What this is NOT:
  * Not a persistent cache (that's Rev 15 material).
  * Not content-addressable (no hashing, no diff sync).
  * Not a source of truth when disagreement is possible — when in doubt,
    callers can still fall back to live device probes.

Graceful degradation: if `refresh()` fails (no device, adb not installed),
the index silently returns empty.  Callers treating the index as advisory
(not authoritative) continue to work; a disabled index is equivalent to
the pre-14.9 behaviour.
"""
from __future__ import annotations

import logging
import posixpath
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional

log = logging.getLogger(__name__)


def _norm(path: str) -> str:
    if not path:
        return path
    # Convert Windows backslashes to forward, collapse redundant segments,
    # and strip trailing slash.  Device paths are POSIX.
    p = path.replace("\\", "/")
    p = posixpath.normpath(p)
    return p


@dataclass
class DeviceIndex:
    """In-memory snapshot of device filesystem state under a music root."""

    root: str = "/storage/emulated/0/Music"
    dirs: set = field(default_factory=set)
    files: dict = field(default_factory=dict)  # path -> size (int) or None if unknown
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _refreshed: bool = field(default=False, init=False)
    # MK17 Patch 3: explicit "index dead for the rest of this session" flag.
    # Pre-MK17 a failed refresh just returned False and the caller logged
    # noise per file; this single flag lets queries short-circuit cleanly
    # and emits exactly one "[Index] disabled for session" warning.
    _session_disabled: bool = field(default=False, init=False)

    # ── Population ──────────────────────────────────────────────────────

    def refresh(self, max_depth: int = 3) -> bool:
        """
        Rebuild the index from the device in a single adb shell find.

        Uses a Busybox/Android-shell-compatible find invocation that emits
        one line per entry with a type marker and (for files) the size.

        Returns True on success, False on any error (index is left empty
        or unchanged — the caller's code should treat False as "index
        unavailable, fall back to live probes").

        Rev 14.9.2 hardening: LOG-14 showed this silently failing with
        "find returned unexpected output", leading both the device index
        AND the downstream audit to give up.  We now:
          - retry once on empty/malformed output (transient adb hiccups)
          - log the first 500 chars of the actual output for diagnosis
          - treat "only the sentinel echoed, no find lines either side"
            as "device scanned but genuinely empty" (valid state), not
            as a parse failure.

        MK17 Patch 3 — self-heal sequence on failure:
          1. retry the primary `find` once (existing 14.9.2 behaviour)
          2. on persistent failure, run `adb start-server` to recover
             from a dead daemon, then retry once more
          3. if still failing, try a SHALLOWER fallback `find -maxdepth 1`
             scan — partial index is better than no index for the scrub
             and free-space fast paths
          4. on total failure, set self._session_disabled = True and emit
             ONE clear "[Index] disabled for session — falling back to
             live probes" warning instead of returning False quietly
        """
        try:
            # Import lazily to avoid import-time coupling
            from adb_transfer import _adb_cmd, _shell_quote
        except Exception as e:
            log.warning(f"[Index] adb_transfer unavailable: {e}")
            self._session_disabled = True
            return False

        q_root = _shell_quote(self.root)
        # NOTE: Built with concatenation rather than nested-quote f-strings so
        # the source parses cleanly on Python 3.10 / 3.11 (PEP 701 nested
        # quotes only landed in 3.12).  The shell-side quoting we want is:
        #   sh -c 'echo "$(stat -c %s "$1")|$1"' _ {} \;
        _stat_exec = '-exec sh -c \'echo "$(stat -c %s "$1")|$1"\' _ {} \\; 2>/dev/null'
        script = (
            f"find {q_root} -maxdepth {max_depth} -type d 2>/dev/null; "
            f"echo __WALKMAN_INDEX_SPLIT__; "
            f"find {q_root} -maxdepth {max_depth} -type f "
            + _stat_exec
        )

        out = None
        last_err: Exception | None = None
        # MK17 Patch 3: 3 attempts, with adb-server-restart between attempts 2 and 3
        for attempt in (1, 2, 3):
            try:
                out = _adb_cmd(["shell", "sh", "-c", script], timeout=120, check=False)
            except Exception as e:
                last_err = e
                log.warning(
                    f"[Index] find failed (attempt {attempt}/3): "
                    f"{type(e).__name__}: {e}"
                )
                out = None

            if out and "__WALKMAN_INDEX_SPLIT__" in out:
                break  # good output — proceed to parse

            if attempt == 1:
                import time as _time
                _time.sleep(2.0)
                log.info("[Index] Retrying device scan after 2s backoff ...")
            elif attempt == 2:
                # MK17 Patch 3: try restarting the adb server before the
                # final attempt.  This catches the daemon-died state
                # (LOG_16_1 evidence) before declaring the index dead.
                log.info(
                    "[Index] Attempting adb server restart before final retry..."
                )
                try:
                    _adb_cmd(["kill-server"], timeout=5, check=False)
                except Exception:
                    pass
                import time as _time
                _time.sleep(1.0)
                try:
                    _adb_cmd(["start-server"], timeout=10, check=False)
                except Exception:
                    pass
                _time.sleep(0.5)

        # MK17 Patch 3: fallback to a shallower scan (max-depth 1) when
        # the deep scan returns no output but ADB is otherwise alive.
        # A partial index is better than no index for the scrub and
        # router fast paths.
        if (not out) or ("__WALKMAN_INDEX_SPLIT__" not in out):
            log.info(
                "[Index] Deep scan failed; trying shallow fallback "
                "(max-depth 1) to populate top-level dirs only..."
            )
            shallow_script = (
                f"find {q_root} -maxdepth 1 -type d 2>/dev/null; "
                f"echo __WALKMAN_INDEX_SPLIT__; "
                f"echo"  # empty file block
            )
            try:
                shallow_out = _adb_cmd(
                    ["shell", "sh", "-c", shallow_script],
                    timeout=30, check=False,
                )
                if shallow_out and "__WALKMAN_INDEX_SPLIT__" in shallow_out:
                    out = shallow_out
                    log.info("[Index] Shallow fallback succeeded.")
            except Exception as e:
                log.debug(f"[Index] Shallow fallback raised: {e}")

        if not out:
            log.warning(
                f"[Index] find returned no output after 3 attempts; "
                f"index DISABLED for session — falling back to live probes. "
                f"Last error: {last_err!r}"
            )
            self._session_disabled = True
            return False

        if "__WALKMAN_INDEX_SPLIT__" not in out:
            preview = (out[:500] + "…") if len(out) > 500 else out
            log.warning(
                f"[Index] sentinel missing from find output; index "
                f"DISABLED for session — falling back to live probes. "
                f"First 500 chars: {preview!r}"
            )
            self._session_disabled = True
            return False

        try:
            dir_block, file_block = out.split("__WALKMAN_INDEX_SPLIT__", 1)
        except ValueError:
            return False

        new_dirs: set[str] = set()
        new_files: dict[str, Optional[int]] = {}

        # MK14 FIX-D: Filter Android system directories from the dir count.
        # ROOT CAUSE: `find /storage/emulated/0/Music -type d` includes every
        # Android app data dir (com.spotify.music, .thumbnails, .nomedia etc.)
        # resulting in 160,706 dirs for only 3,387 files (47:1 ratio in log).
        # These dirs are created by Android system services and Spotify cache —
        # they are NOT music folders and bloat the index with useless entries.
        # Also: 2,912 files deleted by dedup left orphan empty album dirs.
        _SYSTEM_DIR_PATTERNS = (
            "/Android/", "/.thumbnails", "/.nomedia",
            "/com.", "/.cache", "/.data", "/lost+found",
            "/.android_secure", "/.Trash",
        )
        for line in dir_block.splitlines():
            line = line.strip()
            if line:
                # Skip Android system dirs
                if any(pat in line for pat in _SYSTEM_DIR_PATTERNS):
                    continue
                new_dirs.add(_norm(line))

        for line in file_block.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            size_s, _, path = line.partition("|")
            path = _norm(path.strip())
            try:
                size = int(size_s.strip())
            except ValueError:
                size = None
            if path:
                new_files[path] = size

        with self._lock:
            self.dirs = new_dirs
            self.files = new_files
            self._refreshed = True

        log.info(
            f"[Index] Snapshot: {len(new_dirs)} dir(s), {len(new_files)} file(s) "
            f"under {self.root}"
        )
        return True

    # ── Queries ────────────────────────────────────────────────────────

    def is_refreshed(self) -> bool:
        return self._refreshed

    def is_session_disabled(self) -> bool:
        """MK17 Patch 3: True if a refresh failed permanently for this session.
        Callers can use this to skip optimistic fast-path queries and go
        straight to live ADB probes without polluting the log."""
        return self._session_disabled

    def is_dir(self, path: str) -> bool:
        p = _norm(path)
        with self._lock:
            return p in self.dirs

    def is_file(self, path: str) -> bool:
        p = _norm(path)
        with self._lock:
            return p in self.files

    def file_size(self, path: str) -> Optional[int]:
        p = _norm(path)
        with self._lock:
            return self.files.get(p)

    def exists(self, path: str) -> bool:
        p = _norm(path)
        with self._lock:
            return p in self.dirs or p in self.files

    def zero_byte_files(self) -> list[str]:
        """All indexed files with size == 0 (stub files from failed writes)."""
        with self._lock:
            return [p for p, sz in self.files.items() if sz == 0]

    def blocking_files_for_dirs(self, planned_dirs: Iterable[str]) -> list[str]:
        """
        Return paths from `planned_dirs` that currently exist as files
        (i.e., would block mkdir -p).
        """
        out: list[str] = []
        with self._lock:
            for d in planned_dirs:
                p = _norm(d)
                if p in self.files:
                    out.append(p)
        return out

    # ── Mutation ───────────────────────────────────────────────────────

    def mark_dir(self, path: str) -> None:
        p = _norm(path)
        with self._lock:
            self.dirs.add(p)
            self.files.pop(p, None)

    def mark_file(self, path: str, size: Optional[int] = None) -> None:
        p = _norm(path)
        with self._lock:
            self.files[p] = size
            self.dirs.discard(p)

    def remove(self, path: str) -> None:
        p = _norm(path)
        with self._lock:
            self.files.pop(p, None)
            self.dirs.discard(p)


# Module-level singleton convenience.  walkman_sync populates this at session
# start; modules that need read access can import it lazily.
_default_index: Optional[DeviceIndex] = None


def get_default_index() -> Optional[DeviceIndex]:
    return _default_index


def set_default_index(idx: Optional[DeviceIndex]) -> None:
    global _default_index
    _default_index = idx
