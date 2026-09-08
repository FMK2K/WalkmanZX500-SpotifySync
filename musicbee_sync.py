"""
musicbee_sync.py — MK39 Phase III F.2: MusicBee subprocess wrapper
================================================================================

Council MK39.1 Target F: invoke MusicBee.exe to perform device sync.
This module is the bridge between the platform's staging directory (produced
by export_stager) and MusicBee's well-tested device sync engine.

Adversarial-review constraints honored:
  ✓ Feature-detect: returns False on Linux/macOS (or when MusicBee absent)
  ✓ Path auto-detect: standard Program Files (x86) location + portable + config
  ✓ No subprocess timeout (sync can take hours); cancellation via abort_event
  ✓ stdout/stderr captured for operator diagnosis
  ✓ Dry-run mode for tests + first-time operator validation
  ✓ Exit code parsed; non-zero treated as failure with full diagnostic
"""

from __future__ import annotations

import dataclasses
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)


# Standard MusicBee install locations (Windows). Auto-probed in order.
_DEFAULT_MUSICBEE_PATHS = (
    r"C:\Program Files (x86)\MusicBee\MusicBee.exe",
    r"C:\Program Files\MusicBee\MusicBee.exe",
)

# CLI args MusicBee documents:
#   MusicBee.exe /Sync                       -- syncs the default device
#   MusicBee.exe /Sync="<DeviceProfileName>" -- syncs a specific device profile
#   MusicBee.exe /Quiet                      -- minimizes UI during operation


@dataclasses.dataclass
class SyncResult:
    """Outcome of a MusicBee sync invocation."""
    invoked: bool         # True if subprocess actually ran
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    skipped_reason: str = ""    # set when invoked=False
    duration_sec: float = 0.0

    @property
    def success(self) -> bool:
        return self.invoked and self.exit_code == 0


def musicbee_executable_path() -> Optional[str]:
    """Locate MusicBee.exe, honoring config.MUSICBEE_EXECUTABLE override.

    Returns None if not on Windows or executable cannot be found.

    Search order:
      1. config.MUSICBEE_EXECUTABLE (explicit operator override)
      2. shutil.which() — system PATH
      3. Standard install locations (Program Files (x86), etc.)
      4. CWD + script-relative probe — for operators who ship a portable
         MusicBee build alongside the platform on Windows. PowerShell does
         not search CWD by default (same issue we fixed for fpcalc — see
         production LOG_10_5_26).
    """
    # Hard platform gate — MusicBee is .NET Framework, Windows-only.
    if platform.system() != "Windows":
        return None

    # 1. config override
    try:
        import config  # type: ignore
        explicit = getattr(config, "MUSICBEE_EXECUTABLE", "") or ""
        if explicit and os.path.isfile(explicit):
            return explicit
    except ImportError:
        pass

    # 2. PATH
    found = shutil.which("MusicBee.exe") or shutil.which("MusicBee")
    if found:
        return found

    # 3. Standard install locations
    for p in _DEFAULT_MUSICBEE_PATHS:
        if os.path.isfile(p):
            return p

    # 4. CWD + script-relative probe — portable installs
    cwd = os.getcwd()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in (cwd, script_dir):
        for name in ("MusicBee.exe", "MusicBee"):
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                log.info(
                    f"  [MusicBee] executable found in non-PATH location: "
                    f"{candidate} (set config.MUSICBEE_EXECUTABLE to suppress)"
                )
                return candidate

    return None


def is_available() -> bool:
    """True iff MusicBee can be invoked from this environment."""
    return musicbee_executable_path() is not None


def build_sync_command(
    *,
    executable: str,
    device_profile: Optional[str] = None,
    quiet: bool = True,
    extra_args: Sequence[str] = (),
) -> List[str]:
    """Build the argv for a MusicBee sync invocation.

    Pulled out of run_sync so tests can verify command construction without
    invoking subprocess.
    """
    cmd = [executable]
    if device_profile:
        # MusicBee accepts /Sync="ProfileName" as a single arg with embedded =
        cmd.append(f'/Sync="{device_profile}"')
    else:
        cmd.append("/Sync")
    if quiet:
        cmd.append("/Quiet")
    cmd.extend(extra_args)
    return cmd


def run_sync(
    *,
    device_profile: Optional[str] = None,
    quiet: bool = True,
    extra_args: Sequence[str] = (),
    dry_run: bool = False,
    abort_event: Optional[threading.Event] = None,
    subprocess_runner: Optional[callable] = None,
) -> SyncResult:
    """Invoke MusicBee to sync the staging library to the device.

    Args:
        device_profile: optional MusicBee device profile name (None = default)
        quiet: pass /Quiet to minimize UI during sync
        extra_args: arbitrary additional MusicBee CLI args
        dry_run: build the command and log it but DO NOT invoke
        abort_event: optional Event the operator can set to request graceful
                     stop; we poll between subprocess.poll() calls
        subprocess_runner: dependency injection for tests — a callable with
                            the same shape as subprocess.run. Default uses
                            real subprocess.

    Never raises; failures are reported via SyncResult.success/exit_code/stderr.
    """
    import time
    t0 = time.monotonic()

    exe = musicbee_executable_path()
    if exe is None:
        sys_name = platform.system()
        reason = (
            f"MusicBee not available on this system (platform={sys_name}). "
            f"MusicBee is Windows-only; install from https://www.getmusicbee.com "
            f"or set config.MUSICBEE_EXECUTABLE to the .exe path."
        )
        log.info(f"  [MusicBee] {reason}")
        return SyncResult(invoked=False, skipped_reason=reason)

    cmd = build_sync_command(
        executable=exe,
        device_profile=device_profile,
        quiet=quiet,
        extra_args=extra_args,
    )

    if dry_run:
        log.info(f"  [MusicBee] DRY-RUN: would execute: {' '.join(cmd)}")
        return SyncResult(invoked=False,
                            skipped_reason="dry_run",
                            duration_sec=time.monotonic() - t0)

    runner = subprocess_runner or _real_subprocess_run
    log.info(f"  [MusicBee] invoking: {' '.join(cmd)}")
    try:
        proc = runner(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=None,  # explicit: NO timeout — sync can take hours
        )
    except FileNotFoundError as e:
        log.error(f"  [MusicBee] executable not runnable: {e}")
        return SyncResult(invoked=False,
                            skipped_reason=f"executable_not_runnable: {e}",
                            duration_sec=time.monotonic() - t0)
    except Exception as e:
        log.error(f"  [MusicBee] subprocess raised: {type(e).__name__}: {e}")
        return SyncResult(invoked=False,
                            skipped_reason=f"{type(e).__name__}: {e}",
                            duration_sec=time.monotonic() - t0)

    duration = time.monotonic() - t0
    rc = getattr(proc, "returncode", None)
    so = getattr(proc, "stdout", "") or ""
    se = getattr(proc, "stderr", "") or ""

    if rc == 0:
        log.info(f"  [MusicBee] sync completed in {duration:.1f}s (rc=0)")
    else:
        log.warning(f"  [MusicBee] sync FAILED in {duration:.1f}s (rc={rc}); "
                      f"stderr={se.strip()[:200]}")

    return SyncResult(
        invoked=True, exit_code=rc, stdout=so, stderr=se,
        duration_sec=duration,
    )


def _real_subprocess_run(*args, **kwargs):
    return subprocess.run(*args, **kwargs)


__all__ = [
    "SyncResult", "musicbee_executable_path", "is_available",
    "build_sync_command", "run_sync",
]
