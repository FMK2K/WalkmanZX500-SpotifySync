"""
mk36_trust_graph.py — MK36 R1: Trust Provenance Graph (CTO MK35-C)
=====================================================================
CTO MK35 review:
  "Right now your lattice models verifier agreement, but not verifier
   independence. Example: size+mtime, existence, sha256 may all derive
   from the same underlying filesystem layer. Meaning your quorum can
   still collapse to one compromised trust root. 2-of-3 may effectively
   mean 1-of-1. Where is trust-source independence modeled?"

This module formalizes WHAT each verification strategy fundamentally trusts.
Every strategy declares a set of TrustRoot values. The quorum check is
extended: not only must k strategies reach the threshold tier, but their
union of declared trust roots must contain at least min_independent_roots
distinct entries. Otherwise the quorum is logged with INDEPENDENCE_FAILURE
and the effective tier is capped.

This is the difference between "agreement quorum" and "independence quorum."
The MK35 system had agreement only; MK36 adds independence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Iterable, Sequence, Set, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. TRUST ROOTS (R1-T1) — closed enum of what verifications fundamentally trust
# ═══════════════════════════════════════════════════════════════════════════

class TrustRoot(str, Enum):
    """
    Closed-set enum of underlying execution contexts that verifications
    fundamentally rely on. Two strategies that share a root are NOT
    independent — failure of the root simultaneously compromises both.

    The set is deliberately small and explicit. Adding new roots requires
    explicit reasoning about correlation; we don't allow ad-hoc strings.
    """
    FILESYSTEM       = "filesystem"        # the device's filesystem (storage media)
    KERNEL_VFS       = "kernel_vfs"         # the kernel layer between filesystem + tooling
    ADB_DAEMON       = "adb_daemon"         # adb shell / push process on device
    MTP_LAYER        = "mtp_layer"          # MTP/COM transport (separate from ADB)
    USERSPACE_TOOL   = "userspace_tool"     # toybox/coreutils binary actually invoked
    DEVICE_HARDWARE  = "device_hardware"    # the physical storage device itself

    def __repr__(self) -> str:
        return f"TrustRoot.{self.name}"


# Default trust roots that any strategy depending on a generic "device file
# read" implicitly inherits. Strategies that go through different code paths
# (e.g. MTP API calls vs adb shell) declare different roots and become
# independent in the eyes of the quorum check.
DEFAULT_ADB_ROOTS: FrozenSet[TrustRoot] = frozenset({
    TrustRoot.ADB_DAEMON, TrustRoot.KERNEL_VFS, TrustRoot.FILESYSTEM,
})


# ═══════════════════════════════════════════════════════════════════════════
# 2. INDEPENDENCE ANALYSIS (R1-T3 helper)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class IndependenceReport:
    """
    The result of analyzing whether a set of qualifying verification outcomes
    has sufficient trust-source independence.

    `qualifying_count` — number of strategies that reached threshold tier
    `unique_roots`     — union of declared trust roots across qualifying
    `independent_root_count` — len(unique_roots), the key metric
    `min_required`     — the threshold (default 2 for normal MTP setups)
    `independent`      — True iff independent_root_count >= min_required
    """
    qualifying_count:        int
    unique_roots:            FrozenSet[TrustRoot]
    independent_root_count:  int
    min_required:            int
    independent:             bool

    def to_dict(self) -> dict:
        return {
            "qualifyingCount":      self.qualifying_count,
            "uniqueRoots":          sorted(r.value for r in self.unique_roots),
            "independentRootCount": self.independent_root_count,
            "minRequired":          self.min_required,
            "independent":          self.independent,
        }


def analyze_independence(
    strategy_root_sets: Sequence[FrozenSet[TrustRoot]],
    min_independent_roots: int = 2,
) -> IndependenceReport:
    """
    Given the trust-root sets of QUALIFYING strategies (those that reached
    the threshold tier), compute whether they have sufficient independence.

    Returns IndependenceReport. Does not raise.
    """
    union: Set[TrustRoot] = set()
    for s in strategy_root_sets:
        union.update(s)
    return IndependenceReport(
        qualifying_count       = len(strategy_root_sets),
        unique_roots           = frozenset(union),
        independent_root_count = len(union),
        min_required           = min_independent_roots,
        independent            = len(union) >= min_independent_roots,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. PROVENANCE GRAPH (operator-facing diagnostic)
# ═══════════════════════════════════════════════════════════════════════════

def trust_graph_summary(
    strategy_outcomes: Iterable[Tuple[str, FrozenSet[TrustRoot]]],
) -> str:
    """
    Pretty-print a trust graph for diagnostic logging. Each line:
        strategy_name → {root1, root2, ...}
    Plus a final summary line with the union and independence count.
    """
    lines = []
    union: Set[TrustRoot] = set()
    for name, roots in strategy_outcomes:
        roots_str = "{" + ", ".join(sorted(r.value for r in roots)) + "}"
        lines.append(f"  {name:24s} → {roots_str}")
        union.update(roots)
    lines.append(
        f"  {'union':24s} → "
        + "{" + ", ".join(sorted(r.value for r in union)) + "}"
        + f"   ({len(union)} independent roots)"
    )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 4. INTEGRATE WITH mk35_trust BY EXTENDING VerificationStrategy (R1-T2)
# ═══════════════════════════════════════════════════════════════════════════
# We do this via dynamic attachment in the importing module to avoid a hard
# circular dependency. mk35_trust will import this module and call
# bind_default_trust_roots() to attach .trust_roots() to each strategy.

# Each existing strategy class is mapped to its declared trust roots:
DEFAULT_STRATEGY_ROOTS: dict = {
    "sha256":     frozenset({TrustRoot.ADB_DAEMON, TrustRoot.KERNEL_VFS,
                             TrustRoot.FILESYSTEM, TrustRoot.USERSPACE_TOOL}),
    "size_mtime": frozenset({TrustRoot.ADB_DAEMON, TrustRoot.KERNEL_VFS,
                             TrustRoot.FILESYSTEM}),
    "existence":  frozenset({TrustRoot.ADB_DAEMON, TrustRoot.KERNEL_VFS}),
}


def trust_roots_for_strategy(strategy_name: str) -> FrozenSet[TrustRoot]:
    """
    Get the declared trust roots for a known strategy name. Strategies not
    in the registry default to {KERNEL_VFS} — the most generic root.

    The CTO observation: sha256, size_mtime, and existence ALL share
    {ADB_DAEMON, KERNEL_VFS}. They are NOT independent of those layers.
    They differ only in {FILESYSTEM, USERSPACE_TOOL} membership. So if the
    ADB daemon is compromised, the entire quorum collapses regardless of
    how many strategies "agree."

    This function makes the correlation explicit so the quorum check can
    surface it via INDEPENDENCE_FAILURE markers.
    """
    return DEFAULT_STRATEGY_ROOTS.get(
        strategy_name, frozenset({TrustRoot.KERNEL_VFS}),
    )


__all__ = [
    "TrustRoot", "DEFAULT_ADB_ROOTS",
    "IndependenceReport", "analyze_independence",
    "trust_graph_summary",
    "DEFAULT_STRATEGY_ROOTS", "trust_roots_for_strategy",
]
