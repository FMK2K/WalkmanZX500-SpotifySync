"""
mk34_planner.py — MK34 P3-T1: Pure-Function Diff Planner
==========================================================
CTO Board mandate (Phase 3):
    "Plan minimal diff. Generate COPY actions, DELETE actions (only after
     verification), MOVE actions (logical only, not physical assumption)."

This module is intentionally pure: takes (DesiredState, ObservedState),
returns an ordered list of Actions. No I/O. No async. No state. No mutation.
Fully testable and replay-friendly.

Action ordering invariants (enforced by the planner):
  1. COPY before DELETE (never delete a track without verifying its replacement)
  2. MOVE before COPY (move first, then verify, then fill remaining gaps)
  3. DELETE only of tracks where alternative_paths is non-empty
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# DesiredState — what the system thinks the device should look like
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class DesiredState:
    """
    The set of paths that should be on the device, with metadata to support
    decision-making. Consumed by the planner; never mutated.
    """
    expected_paths:     FrozenSet[str]      # paths that should be present
    forbidden_paths:    FrozenSet[str]      # paths that should be removed
    expected_hashes:    "dict[str, str]"    # path -> sha256 of source file
    cross_format_alts:  "dict[str, list[str]]"  # path -> alt-format paths

    def __post_init__(self):
        # Lightweight invariant: forbidden ∩ expected = ∅
        overlap = self.expected_paths & self.forbidden_paths
        if overlap:
            raise ValueError(
                f"DesiredState invariant violated: {len(overlap)} paths are both "
                f"expected and forbidden — pick one. Sample: {list(overlap)[:3]}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Actions — the planner's output type
# ═══════════════════════════════════════════════════════════════════════════

class ActionKind(str, Enum):
    COPY   = "COPY"     # source on PC, dest on device, source file exists
    DELETE = "DELETE"   # path on device — but only if alternative verified present
    MOVE   = "MOVE"     # logical: rename/relocate on device


@dataclass(frozen=True)
class PlannedAction:
    """
    One step in the plan. Frozen — the planner produces; the executor consumes.
    """
    kind:                  ActionKind
    path:                  str                 # relpath on device
    source_path:           Optional[str]       # for COPY: path on PC
    expected_hash:         Optional[str]       # for COPY: SHA256 of source
    move_from:             Optional[str]       # for MOVE: original device path
    requires_alternative:  bool                # CTO: DELETE needs verified alt
    alternative_paths:     Tuple[str, ...]     # paths that MUST exist before this
    reason:                str                 # human-readable explanation

    def to_dict(self) -> dict:
        return {
            "kind":                self.kind.value,
            "path":                self.path,
            "sourcePath":          self.source_path,
            "expectedHash":        self.expected_hash,
            "moveFrom":            self.move_from,
            "requiresAlternative": self.requires_alternative,
            "alternativePaths":    list(self.alternative_paths),
            "reason":              self.reason,
        }


@dataclass(frozen=True)
class Plan:
    """The planner's output. Frozen, ordered, deterministic."""
    actions:        Tuple[PlannedAction, ...]
    plan_hash:      str                # sha256 of the action sequence
    summary:        dict               # {copies: N, deletes: M, moves: K}

    def to_dict(self) -> dict:
        return {"actions":  [a.to_dict() for a in self.actions],
                "planHash": self.plan_hash,
                "summary":  self.summary}


# ═══════════════════════════════════════════════════════════════════════════
# plan_diff — the pure function
# ═══════════════════════════════════════════════════════════════════════════

def plan_diff(desired: DesiredState, observed: "DeviceObservedState",  # noqa: F821
              preserve_alternatives: bool = True) -> Plan:
    """
    Compute the ordered minimal-diff action list.

    Args:
        desired: what the system wants the device to look like.
        observed: the canonical observation from mk34_observer.observe().
        preserve_alternatives: if True (default), DELETE only emitted for
            tracks that have at least one alternative path verified present.
            CTO mandate: "no_track_is_deleted_unless_verified_present_elsewhere."

    Returns: Plan with actions ordered as: MOVE → COPY → DELETE.
    """
    import hashlib, json

    actions: List[PlannedAction] = []

    # 1. MOVE: pre-existing tracks at wrong path that should be relocated.
    #    For now, we don't compute MOVEs (would require additional metadata
    #    about path equivalence). Reserved for future use.
    moves: List[PlannedAction] = []

    # 2. COPY: tracks in desired.expected_paths but not in observed
    copies: List[PlannedAction] = []
    for path in sorted(desired.expected_paths):
        if path in observed.tracks_present_paths:
            continue   # already present
        copies.append(PlannedAction(
            kind=ActionKind.COPY,
            path=path,
            source_path=desired.expected_hashes.get(path) and path,  # caller provides source mapping
            expected_hash=desired.expected_hashes.get(path),
            move_from=None,
            requires_alternative=False,
            alternative_paths=(),
            reason=f"missing_on_device",
        ))

    # 3. DELETE: tracks in desired.forbidden_paths that ARE present, but only
    #    if their alternative paths are verified present (CTO mandate).
    deletes: List[PlannedAction] = []
    skipped_deletes_no_alternative: List[str] = []
    for path in sorted(desired.forbidden_paths):
        if path not in observed.tracks_present_paths:
            continue   # already absent — nothing to do
        alts = tuple(desired.cross_format_alts.get(path, []))
        verified_alts = tuple(a for a in alts if a in observed.tracks_present_paths)
        if preserve_alternatives and not verified_alts:
            skipped_deletes_no_alternative.append(path)
            continue
        deletes.append(PlannedAction(
            kind=ActionKind.DELETE,
            path=path,
            source_path=None,
            expected_hash=None,
            move_from=None,
            requires_alternative=preserve_alternatives,
            alternative_paths=verified_alts,
            reason=f"forbidden; alternatives_verified={len(verified_alts)}",
        ))

    if skipped_deletes_no_alternative:
        log.warning(
            f"[MK34/planner] DELETE_DEFERRED_NO_ALTERNATIVE: skipped "
            f"{len(skipped_deletes_no_alternative)} deletes — no verified alternatives. "
            f"CTO invariant: no_track_deleted_unless_verified_present_elsewhere. "
            f"Sample: {skipped_deletes_no_alternative[:3]}"
        )

    # Order: MOVE → COPY → DELETE (CTO: copy before delete)
    ordered = tuple(moves + copies + deletes)

    # Deterministic plan hash for replay
    plan_repr = json.dumps([a.to_dict() for a in ordered], sort_keys=True)
    plan_hash = hashlib.sha256(plan_repr.encode()).hexdigest()[:16]

    summary = {
        "moves":              len(moves),
        "copies":              len(copies),
        "deletes":             len(deletes),
        "deletes_deferred":    len(skipped_deletes_no_alternative),
        "total":               len(ordered),
        "plan_hash":           plan_hash,
    }
    log.info(
        f"[MK34/planner] PLAN_COMPUTED: "
        f"copies={len(copies)} deletes={len(deletes)} "
        f"deletes_deferred={len(skipped_deletes_no_alternative)} "
        f"plan_hash={plan_hash}"
    )

    return Plan(actions=ordered, plan_hash=plan_hash, summary=summary)


__all__ = ["DesiredState", "ActionKind", "PlannedAction", "Plan", "plan_diff"]
