"""
storage_thresholds.py — MK39.1 L6.3 / Board GOV-F1+F2
================================================================================

Board Review Revision 2 — Governance §F (Internal Storage Capacity):

  GOV-F1 — Thresholds configurable above hard floors:
             Low free space warning:  1 GB  (floor: 500 MB) → AMBER
             Reorganize blocked:    500 MB  (floor: 200 MB) → RED
             All writes blocked:    100 MB  (floor: 100 MB) → RED + halt

  GOV-F2 — Abort threshold escalation:
             WRITE_TARGET free < 1 GB        → AMBER  (continue with WARNING)
             WRITE_TARGET free < 500 MB      → RED    (block reorganize)
             WRITE_TARGET free < 100 MB      → RED    (block all writes, halt)
             INTERNAL free < 500 MB          → AMBER  (banner WARNING)
             INTERNAL free < 100 MB          → RED    (block reorganize)

  GOV-F3 — Volume segregation: silent_move must not write to internal
           storage. (Enforced separately; this module is the threshold
           policy layer.)

LOG_11_5_26 evidence:
  Internal storage at 499 MB free / 50 GB (99% full). The platform
  emitted VERDICT GREEN. The Council mandated this become an AMBER
  trigger. Under L6.3 it would have been AMBER (internal < 500 MB)
  AND RED if internal had dropped below 100 MB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Board GOV-F1 — Hardcoded floors. These cannot be overridden below.
# ─────────────────────────────────────────────────────────────────────────

FLOOR_AMBER_GB    = 0.5     # write-target AMBER floor
FLOOR_RED_GB      = 0.2     # write-target RED floor
FLOOR_HALT_GB     = 0.1     # write-target HALT floor (board's hard minimum)
FLOOR_INTERNAL_AMBER_GB = 0.5
FLOOR_INTERNAL_RED_GB   = 0.1


@dataclass
class StorageThresholds:
    """Active thresholds for a run. Configurable above the floors."""
    write_target_amber_gb:    float = 1.0   # AMBER if write-target free < 1 GB
    write_target_red_gb:      float = 0.5   # RED if free < 500 MB
    write_target_halt_gb:     float = 0.1   # RED+halt if free < 100 MB
    internal_amber_gb:        float = 0.5   # AMBER if internal free < 500 MB
    internal_red_gb:          float = 0.1   # RED if internal free < 100 MB

    def validate_against_floors(self) -> list:
        """Return list of violations of GOV-F1 floors. Operator config
        cannot lower a threshold below its floor."""
        violations = []
        if self.write_target_amber_gb < FLOOR_AMBER_GB:
            violations.append(
                f"write_target_amber_gb={self.write_target_amber_gb} below "
                f"floor {FLOOR_AMBER_GB} (Board GOV-F1)"
            )
        if self.write_target_red_gb < FLOOR_RED_GB:
            violations.append(
                f"write_target_red_gb={self.write_target_red_gb} below "
                f"floor {FLOOR_RED_GB} (Board GOV-F1)"
            )
        if self.write_target_halt_gb < FLOOR_HALT_GB:
            violations.append(
                f"write_target_halt_gb={self.write_target_halt_gb} below "
                f"floor {FLOOR_HALT_GB} (Board GOV-F1)"
            )
        if self.internal_amber_gb < FLOOR_INTERNAL_AMBER_GB:
            violations.append(
                f"internal_amber_gb={self.internal_amber_gb} below floor "
                f"{FLOOR_INTERNAL_AMBER_GB} (Board GOV-F1)"
            )
        if self.internal_red_gb < FLOOR_INTERNAL_RED_GB:
            violations.append(
                f"internal_red_gb={self.internal_red_gb} below floor "
                f"{FLOOR_INTERNAL_RED_GB} (Board GOV-F1)"
            )
        return violations


class StorageVerdict(Enum):
    """Storage component of the safety verdict (Board GOV-F2)."""
    GREEN   = "green"
    AMBER   = "amber"
    RED     = "red"
    HALT    = "halt"
    UNKNOWN = "unknown"  # probe failed; verdict undetermined


@dataclass
class StorageAssessment:
    """Result of a storage threshold evaluation."""
    write_target_verdict: StorageVerdict
    internal_verdict:     StorageVerdict
    overall_verdict:      StorageVerdict   # max(write_target, internal)
    halt_pipeline:        bool             # True only when HALT condition met
    reasons:              list             # human-readable per-condition reasons

    def as_dict(self) -> dict:
        return {
            "write_target_verdict": self.write_target_verdict.value,
            "internal_verdict": self.internal_verdict.value,
            "overall_verdict": self.overall_verdict.value,
            "halt_pipeline": self.halt_pipeline,
            "reasons": self.reasons,
        }


# Caller injects a probe — signature: (storage_name: str) -> bytes_free | None
StorageProbe = Callable[[str], Optional[int]]


def _gb(bytes_free: Optional[int]) -> Optional[float]:
    if bytes_free is None:
        return None
    return bytes_free / (1024 ** 3)


def assess_storage(
    *,
    write_target_storage: str,
    internal_storage: str,
    probe: StorageProbe,
    thresholds: Optional[StorageThresholds] = None,
) -> StorageAssessment:
    """Evaluate storage thresholds against current free-space readings.

    `probe(storage_name)` should return free-bytes for that storage or
    None if unprobeable. The board's policy treats unprobeable storage
    as UNKNOWN — does NOT halt the pipeline (operator continues at own
    risk; banner reflects the uncertainty).
    """
    t = thresholds or StorageThresholds()

    write_free_gb    = _gb(probe(write_target_storage))
    internal_free_gb = _gb(probe(internal_storage)) if internal_storage != write_target_storage else write_free_gb

    reasons: list = []

    # ── Write-target evaluation ────────────────────────────────────────
    if write_free_gb is None:
        write_v = StorageVerdict.UNKNOWN
        reasons.append(f"write-target free-space probe returned None")
    elif write_free_gb < t.write_target_halt_gb:
        write_v = StorageVerdict.HALT
        reasons.append(
            f"write-target ({write_target_storage!r}) free "
            f"{write_free_gb:.2f} GB < halt threshold {t.write_target_halt_gb} GB "
            f"— all writes blocked (Board GOV-F2)"
        )
    elif write_free_gb < t.write_target_red_gb:
        write_v = StorageVerdict.RED
        reasons.append(
            f"write-target free {write_free_gb:.2f} GB < RED threshold "
            f"{t.write_target_red_gb} GB — reorganize blocked"
        )
    elif write_free_gb < t.write_target_amber_gb:
        write_v = StorageVerdict.AMBER
        reasons.append(
            f"write-target free {write_free_gb:.2f} GB < AMBER threshold "
            f"{t.write_target_amber_gb} GB — continue with WARNING"
        )
    else:
        write_v = StorageVerdict.GREEN

    # ── Internal storage evaluation ────────────────────────────────────
    if internal_free_gb is None:
        internal_v = StorageVerdict.UNKNOWN
        reasons.append(f"internal storage free-space probe returned None")
    elif internal_free_gb < t.internal_red_gb:
        internal_v = StorageVerdict.RED
        reasons.append(
            f"internal storage ({internal_storage!r}) free "
            f"{internal_free_gb:.2f} GB < RED threshold {t.internal_red_gb} GB "
            f"— reorganize blocked even if write-target is SD"
        )
    elif internal_free_gb < t.internal_amber_gb:
        internal_v = StorageVerdict.AMBER
        reasons.append(
            f"internal storage free {internal_free_gb:.2f} GB < AMBER "
            f"threshold {t.internal_amber_gb} GB — operator notice"
        )
    else:
        internal_v = StorageVerdict.GREEN

    # ── Overall verdict = strictest of the two ─────────────────────────
    order = [StorageVerdict.GREEN, StorageVerdict.UNKNOWN, StorageVerdict.AMBER,
             StorageVerdict.RED, StorageVerdict.HALT]
    overall = max([write_v, internal_v], key=order.index)

    return StorageAssessment(
        write_target_verdict=write_v,
        internal_verdict=internal_v,
        overall_verdict=overall,
        halt_pipeline=(write_v == StorageVerdict.HALT),
        reasons=reasons,
    )


def emit_storage_banner_line(assessment: StorageAssessment) -> str:
    """Format the storage portion of the safety profile banner.

    Returns a string suitable for inclusion in the L4 PROFILE log line.
    """
    return (
        f"storage_write={assessment.write_target_verdict.value} "
        f"storage_internal={assessment.internal_verdict.value}"
    )
