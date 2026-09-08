"""
mk34_observer.py — MK34 P2: ADB-first State Observer (Class B fix)
====================================================================
CTO Board diagnosis of MK33 Class B failure:
    "ADB sees 27,901 files; preload sees 0 — same run, same device.
     The system has multiple unsynchronized state sources."

This module provides the SINGLE canonical observe() entry point. ADB is
authoritative. Preload/COM/cache observations are advisory only and used
for divergence detection (logging the disagreement), never for resolution.

CRITICAL: This module performs ZERO destructive operations. Per CTO:
    "Phase 1 — Observe (truth gathering only): no deletes, no transfers."

The output is a frozen DeviceObservedState that all downstream stages
consume. Downstream stages (planner, executor) cannot bypass it.
"""

from __future__ import annotations

import logging
import time
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

log = logging.getLogger(__name__)

# Configurable threshold for when ADB-vs-preload disagreement is worth logging
DIVERGENCE_THRESHOLD_PCT = 0.05   # 5%


# ═══════════════════════════════════════════════════════════════════════════
# Observed State (immutable output of the observer)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class DeviceObservedState:
    """
    Frozen snapshot of what the device reports right now.
    Authoritative source: ADB filesystem listing.
    Advisory sources: preload/COM/cache hits — recorded for divergence detection
    but NEVER used to override the ADB list.

    Downstream code consuming this state MUST treat `tracks_present_paths` as
    the truth. If a track is in tracks_present_paths, it's on the device. If
    not, it's not — regardless of what preload says.
    """
    observed_at:           float
    tracks_present_paths:  FrozenSet[str]   # ADB-reported paths (the truth)
    free_space_bytes:      Optional[int]    # may be None if ADB shell df fails
    storage_breakdown:     Dict[str, int]   # storage_id -> file count
    advisory_preload_count: Optional[int]   # what preload claimed; advisory only
    divergence_pct:        float            # |adb - preload| / max(adb, preload)
    divergence_logged:     bool             # True if STATE_DIVERGENCE_DETECTED fired
    adb_command:           str              # the ADB command used (for replay)
    observation_id:        str              # uuid for cross-module reference

    def __len__(self) -> int:
        return len(self.tracks_present_paths)

    def has_track(self, relpath: str) -> bool:
        """Authoritative check: is this track on the device?"""
        return relpath in self.tracks_present_paths

    def to_dict(self) -> dict:
        return {
            "observedAt":             self.observed_at,
            "tracksPresent":          len(self.tracks_present_paths),
            "freeSpaceBytes":         self.free_space_bytes,
            "storageBreakdown":       dict(self.storage_breakdown),
            "advisoryPreloadCount":   self.advisory_preload_count,
            "divergencePct":          self.divergence_pct,
            "divergenceLogged":       self.divergence_logged,
            "adbCommand":             self.adb_command,
            "observationId":          self.observation_id,
        }


# ═══════════════════════════════════════════════════════════════════════════
# observe() — the single entry point
# ═══════════════════════════════════════════════════════════════════════════

def observe(adb_path: str = "adb",
            device_root: str = "/storage/4A21-0000",
            preload_count_advisory: Optional[int] = None,
            adb_timeout_s: float = 90.0) -> DeviceObservedState:
    """
    Run the canonical Phase 1 observation.

    Args:
        adb_path: path to the ADB binary.
        device_root: SD card root on the Walkman (CTO-confirmed: /storage/4A21-0000).
        preload_count_advisory: if the COM/preload layer has a count, pass it
            here for divergence detection. Optional. NEVER used for resolution.
        adb_timeout_s: ADB command timeout. Default 90s; large libraries on
            slow USB take time.

    Returns: DeviceObservedState (frozen). Caller cannot mutate.

    Raises: RuntimeError if ADB itself fails to enumerate. Per CTO Class C
    fix, callers MUST NOT proceed past observe() failure — there is no
    "advisory-only" fallback that would let the planner run without truth.
    """
    import uuid

    obs_id = uuid.uuid4().hex
    started = time.time()
    cmd = [adb_path, "shell", "find", device_root,
           "-type", "f", "-not", "-path", "*/.deleted/*"]
    log.info(f"[MK34/observe] {' '.join(cmd)} (obs_id={obs_id[:8]})")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=adb_timeout_s,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"[MK34/observe] ADB enumeration timed out after {adb_timeout_s}s. "
            f"Cannot proceed without canonical device state. CTO Class C: "
            f"no forward progress on observation failure."
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"[MK34/observe] ADB binary not found at {adb_path!r}. "
            f"Cannot establish single source of truth."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"[MK34/observe] ADB returned {result.returncode}: "
            f"stderr={result.stderr[:300]!r}. Cannot establish canonical state."
        )

    # Build the authoritative path set
    paths: set = set()
    storage_breakdown: Dict[str, int] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Convert /storage/4A21-0000/Music/X/Y.mp3 → SD card/Music/X/Y.mp3
        # to match the relpath convention used elsewhere
        relpath = line.replace(device_root, "SD card", 1).lstrip("/")
        paths.add(relpath)
        # Track first path component for storage breakdown
        head = relpath.split("/", 1)[0] if "/" in relpath else "root"
        storage_breakdown[head] = storage_breakdown.get(head, 0) + 1

    # Free-space probe — best-effort, optional
    free_space: Optional[int] = None
    try:
        df_result = subprocess.run(
            [adb_path, "shell", "df", "-B1", device_root],
            capture_output=True, text=True, timeout=10.0,
            encoding="utf-8", errors="replace",
        )
        if df_result.returncode == 0:
            for line in df_result.stdout.splitlines():
                if device_root in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        free_space = int(parts[3])
                        break
    except Exception as e:
        log.debug(f"[MK34/observe] df probe failed (non-fatal): {e}")

    # Divergence detection — advisory only, never overrides ADB count
    adb_count = len(paths)
    div_pct = 0.0
    div_logged = False
    if preload_count_advisory is not None and preload_count_advisory > 0:
        div_pct = abs(adb_count - preload_count_advisory) / max(adb_count, preload_count_advisory)
        if div_pct > DIVERGENCE_THRESHOLD_PCT:
            log.warning(
                f"[MK34/observe] STATE_DIVERGENCE_DETECTED: "
                f"adb={adb_count} vs preload={preload_count_advisory} "
                f"({div_pct*100:.1f}% disagreement). ADB is canonical."
            )
            div_logged = True
        else:
            log.debug(
                f"[MK34/observe] adb={adb_count} vs preload={preload_count_advisory} "
                f"({div_pct*100:.2f}% diff — within threshold)."
            )

    log.info(
        f"[MK34/observe] CANONICAL_DEVICE_STATE_ESTABLISHED: "
        f"{adb_count} files on device · "
        f"{len(storage_breakdown)} storage groups · "
        f"free_space={free_space} · elapsed={time.time()-started:.1f}s · obs_id={obs_id[:8]}"
    )

    return DeviceObservedState(
        observed_at            = time.time(),
        tracks_present_paths   = frozenset(paths),
        free_space_bytes       = free_space,
        storage_breakdown      = dict(storage_breakdown),
        advisory_preload_count = preload_count_advisory,
        divergence_pct         = div_pct,
        divergence_logged      = div_logged,
        adb_command            = " ".join(cmd),
        observation_id         = obs_id,
    )


__all__ = ["DeviceObservedState", "observe", "DIVERGENCE_THRESHOLD_PCT"]
