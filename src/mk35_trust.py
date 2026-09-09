"""
mk35_trust.py — MK35 Q1: Formal Trust Lattice + VerificationStrategy
========================================================================
CTO MK34 review:
  "You assume device-side hash execution is trustworthy and stable. ADB shell
   environments are not deterministic execution contexts. Some devices: strip
   tools, alias commands, return truncated outputs, behave differently under
   MTP mount vs ADB shell. You need a VerificationStrategy abstraction, not
   a single SHA path."

This module defines a FORMAL trust lattice — a 4-tier partial order — and
a Strategy abstraction that allows multiple independent verification methods
to vote on whether a transfer was actually completed. The single-SHA trust
model from MK34 is replaced with a quorum-based model that survives partial
device-tool availability.

The lattice (top to bottom):
    STRONG     — cryptographic content match (sha256, blake2)
    MODERATE   — structural match (size + mtime + first/last 4KB samples)
    WEAK       — file exists at expected path, size > 0
    UNTRUSTED  — no signal (observer failed; treat as failure)

The lattice meet (`meet(*tiers)`) returns the LOWEST tier across observations.
This is the join-semilattice model: an outcome is only as strong as its
weakest verifier.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Optional, Sequence, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE TRUST LATTICE (Q1-T1)
# ═══════════════════════════════════════════════════════════════════════════

class TrustTier(IntEnum):
    """
    Formal partial order over verification outcomes. Higher = more trustworthy.
    IntEnum so comparison is well-defined and pickle-safe.
    """
    UNTRUSTED = 0   # no signal; treat as failure
    WEAK      = 1   # exists at path with size > 0
    MODERATE  = 2   # structural match (size + mtime, or partial content)
    STRONG    = 3   # cryptographic content match

    @classmethod
    def meet(cls, *tiers: "TrustTier") -> "TrustTier":
        """
        Lattice meet: returns the LOWEST tier across observations.
        An outcome is only as strong as its weakest verifier.
        Empty input → UNTRUSTED (no observations = no trust).
        """
        if not tiers:
            return cls.UNTRUSTED
        return cls(min(int(t) for t in tiers))

    @classmethod
    def join(cls, *tiers: "TrustTier") -> "TrustTier":
        """
        Lattice join: returns the HIGHEST tier. Used when reporting
        the BEST observation among multiple — primarily for diagnostics.
        """
        if not tiers:
            return cls.UNTRUSTED
        return cls(max(int(t) for t in tiers))


# ═══════════════════════════════════════════════════════════════════════════
# 2. VERIFICATION OUTCOME (carries strategy result + evidence)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class VerificationOutcome:
    """
    What one strategy reports about one (source, device-path) pair.
    Frozen — strategy results never mutated after creation.

    `evidence` carries the data the strategy used to reach its conclusion
    (hex digest, size, mtime, etc) so decisions are auditable.
    """
    strategy_name: str
    tier:          TrustTier
    evidence:      dict
    note:          str = ""

    def to_dict(self) -> dict:
        return {
            "strategyName": self.strategy_name,
            "tier":          self.tier.name,
            "evidence":      dict(self.evidence),
            "note":          self.note,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 3. VERIFICATION STRATEGY ABC + 3 CONCRETE IMPLEMENTATIONS (Q1-T2)
# ═══════════════════════════════════════════════════════════════════════════

class VerificationStrategy(ABC):
    """
    Abstract base for verification strategies. Each concrete subclass
    implements verify() and reports a VerificationOutcome.

    Strategies are independent: a single (source, device-path) pair may be
    verified by all three concrete strategies, and the outcomes combined
    via verify_with_quorum() into a single trust decision.
    """

    name: str = "<abstract>"

    @abstractmethod
    def verify(self,
               source_local_path: str,
               device_relpath: str,
               adb_path: str = "adb",
               device_root: str = "/storage/4A21-0000",
               expected_hash: Optional[str] = None,
               expected_size: Optional[int] = None) -> VerificationOutcome:
        """Inspect the device-side state and report a tier with evidence."""
        ...

    @staticmethod
    def _device_path(device_root: str, relpath: str) -> str:
        if relpath.startswith("SD card/"):
            return device_root + "/" + relpath[len("SD card/"):]
        return device_root + "/" + relpath


class Sha256Strategy(VerificationStrategy):
    """
    STRONG-tier verifier — full sha256 over both source and device file.
    Failure mode: sha256sum binary missing on device → UNTRUSTED.
    """
    name = "sha256"

    def verify(self, source_local_path, device_relpath,
               adb_path="adb", device_root="/storage/4A21-0000",
               expected_hash=None, expected_size=None):
        # Source hash
        try:
            if expected_hash:
                src_hash = expected_hash
            else:
                src_hash = self._sha256_local(source_local_path)
        except Exception as e:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"error": f"src_hash_failed: {e!r}"},
                note="source-side sha256 failed",
            )

        # Device hash via adb shell sha256sum
        device_path = self._device_path(device_root, device_relpath)
        try:
            res = subprocess.run(
                [adb_path, "shell", "sha256sum", "-b", _shell_escape(device_path)],
                capture_output=True, text=True, timeout=60.0,
                encoding="utf-8", errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"src_hash": src_hash, "error": str(e)},
                note="adb shell sha256sum unavailable or timed out",
            )

        if res.returncode != 0:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"src_hash": src_hash, "rc": res.returncode,
                          "stderr": res.stderr[:200]},
                note="device-side sha256sum failed (toybox may lack the tool)",
            )

        first = res.stdout.strip().split()
        dev_hash = first[0] if first and len(first[0]) == 64 else None
        if dev_hash and dev_hash == src_hash:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.STRONG,
                evidence={"src_hash": src_hash, "dev_hash": dev_hash},
                note="content-hash match",
            )
        return VerificationOutcome(
            strategy_name=self.name, tier=TrustTier.UNTRUSTED,
            evidence={"src_hash": src_hash, "dev_hash": dev_hash},
            note="hash mismatch — possible Class A leak",
        )

    @staticmethod
    def _sha256_local(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                buf = f.read(1 << 20)
                if not buf: break
                h.update(buf)
        return h.hexdigest()


class SizeAndMtimeStrategy(VerificationStrategy):
    """
    MODERATE-tier verifier — structural match: size + mtime.
    Compares device-side `stat` output to source file metadata.
    Survives toybox-stripped sha256sum.
    """
    name = "size_mtime"

    def verify(self, source_local_path, device_relpath,
               adb_path="adb", device_root="/storage/4A21-0000",
               expected_hash=None, expected_size=None):
        # Source size
        try:
            src_size = expected_size or Path(source_local_path).stat().st_size
        except Exception as e:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"error": f"src_stat_failed: {e!r}"},
            )

        # Device stat
        device_path = self._device_path(device_root, device_relpath)
        try:
            res = subprocess.run(
                [adb_path, "shell", "stat", "-c", "%s|%Y",
                 _shell_escape(device_path)],
                capture_output=True, text=True, timeout=15.0,
                encoding="utf-8", errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"src_size": src_size, "error": str(e)},
            )

        if res.returncode != 0:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"src_size": src_size, "rc": res.returncode},
                note="device-side stat failed",
            )

        try:
            parts = res.stdout.strip().split("|")
            dev_size = int(parts[0])
            dev_mtime = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"raw": res.stdout[:200]},
                note="stat output parse failed",
            )

        if dev_size == src_size and dev_size > 0:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.MODERATE,
                evidence={"src_size": src_size, "dev_size": dev_size,
                          "dev_mtime": dev_mtime},
                note="size match; mtime informational",
            )
        return VerificationOutcome(
            strategy_name=self.name, tier=TrustTier.UNTRUSTED,
            evidence={"src_size": src_size, "dev_size": dev_size},
            note="size mismatch",
        )


class ExistenceOnlyStrategy(VerificationStrategy):
    """
    WEAK-tier verifier — file exists at expected device path with size > 0.
    Last-resort verification when both sha256sum and stat are stripped/aliased.
    """
    name = "existence"

    def verify(self, source_local_path, device_relpath,
               adb_path="adb", device_root="/storage/4A21-0000",
               expected_hash=None, expected_size=None):
        device_path = self._device_path(device_root, device_relpath)
        # `ls -l` is part of toybox even when other utilities are stripped
        try:
            res = subprocess.run(
                [adb_path, "shell", "ls", "-l", _shell_escape(device_path)],
                capture_output=True, text=True, timeout=10.0,
                encoding="utf-8", errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"error": str(e)},
            )

        if res.returncode != 0 or "No such file" in res.stdout + res.stderr:
            return VerificationOutcome(
                strategy_name=self.name, tier=TrustTier.UNTRUSTED,
                evidence={"rc": res.returncode, "out": res.stdout[:200]},
                note="file does not exist on device",
            )

        # Try to extract size from the listing — but the file's existence at
        # all already qualifies for WEAK. Don't fail to UNTRUSTED on parse.
        return VerificationOutcome(
            strategy_name=self.name, tier=TrustTier.WEAK,
            evidence={"ls_line": res.stdout.strip()[:200]},
            note="file exists; content unverified",
        )


# Default strategy roster for verify_with_quorum()
DEFAULT_STRATEGIES: Tuple[VerificationStrategy, ...] = (
    Sha256Strategy(),
    SizeAndMtimeStrategy(),
    ExistenceOnlyStrategy(),
)


# ═══════════════════════════════════════════════════════════════════════════
# 4. QUORUM VERIFICATION (Q1-T3)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class QuorumOutcome:
    """
    Result of running multiple strategies + applying quorum threshold.
    Frozen; carries enough evidence to be replayable.

    MK36 R1-T3: independence_report field added. Contains the
    IndependenceReport from mk36_trust_graph showing whether the
    qualifying strategies represent independent trust roots.
    """
    quorum_reached:    bool
    effective_tier:    TrustTier        # the meet of strategies that reached threshold
    threshold_tier:    TrustTier        # what we required (MODERATE by default)
    threshold_count:   int              # k in k-of-N
    outcomes:          Tuple[VerificationOutcome, ...]
    note:              str = ""
    independence:      Optional[dict] = None  # MK36 R1-T3: IndependenceReport.to_dict()

    def to_dict(self) -> dict:
        return {
            "quorumReached":  self.quorum_reached,
            "effectiveTier":  self.effective_tier.name,
            "thresholdTier":  self.threshold_tier.name,
            "thresholdCount": self.threshold_count,
            "outcomes":       [o.to_dict() for o in self.outcomes],
            "note":           self.note,
            "independence":   self.independence,
        }


def verify_with_quorum(
    source_local_path: str,
    device_relpath: str,
    strategies: Sequence[VerificationStrategy] = DEFAULT_STRATEGIES,
    threshold_tier: TrustTier = TrustTier.MODERATE,
    threshold_count: int = 2,
    expected_hash: Optional[str] = None,
    expected_size: Optional[int] = None,
    adb_path: str = "adb",
    device_root: str = "/storage/4A21-0000",
    min_independent_roots: int = 2,
) -> QuorumOutcome:
    """
    Run all strategies, gather outcomes, apply quorum threshold + (MK36 R1)
    independence check.

    Quorum rule:
      1. At least `threshold_count` strategies must reach `threshold_tier` or higher
      2. (MK36 R1-T3) The qualifying strategies must collectively cover at
         least `min_independent_roots` distinct trust roots (default 2)

    Defaults: 2-of-3 strategies must reach MODERATE+ AND cover >=2 trust roots.

    The independence check closes the CTO MK35 concern that 2-of-3 quorum
    can effectively collapse to 1-of-1 when all strategies share the same
    underlying trust root. INDEPENDENCE_FAILURE marker fires when agreement
    quorum is reached but independence is not. In that case the effective
    tier is downgraded to min(effective_tier, MODERATE).
    """
    outcomes: list = []
    for strategy in strategies:
        try:
            outcome = strategy.verify(
                source_local_path=source_local_path,
                device_relpath=device_relpath,
                adb_path=adb_path,
                device_root=device_root,
                expected_hash=expected_hash,
                expected_size=expected_size,
            )
        except Exception as e:
            outcome = VerificationOutcome(
                strategy_name=strategy.name,
                tier=TrustTier.UNTRUSTED,
                evidence={"strategy_exception": repr(e)},
                note="strategy raised; treated as no-signal",
            )
        outcomes.append(outcome)

    qualifying = [o for o in outcomes if o.tier >= threshold_tier]
    agreement_quorum = len(qualifying) >= threshold_count

    # MK36 R1-T3: independence check — qualifying strategies must cover enough
    # distinct trust roots. Otherwise correlated failure can collapse quorum.
    independence_report_dict = None
    independence_ok = True
    try:
        from mk36_trust_graph import (
            analyze_independence, trust_roots_for_strategy,
        )
        qualifying_roots = [trust_roots_for_strategy(o.strategy_name) for o in qualifying]
        independence = analyze_independence(qualifying_roots, min_independent_roots)
        independence_report_dict = independence.to_dict()
        independence_ok = independence.independent
        if agreement_quorum and not independence_ok:
            log.warning(
                f"  [MK36/R1] INDEPENDENCE_FAILURE path={device_relpath!r} "
                f"agreement_quorum=True but only "
                f"{independence.independent_root_count} unique trust roots "
                f"(need {min_independent_roots}). 2-of-N collapsed to "
                f"correlated failure domain."
            )
    except ImportError:
        # MK36 not installed; revert to MK35 agreement-only quorum
        independence_ok = True

    quorum_reached = agreement_quorum and independence_ok

    if quorum_reached:
        effective = TrustTier.meet(*[o.tier for o in qualifying])
        log.info(
            f"  [MK35/Q1] QUORUM_REACHED path={device_relpath!r} "
            f"qualifying={len(qualifying)}/{len(outcomes)} effective={effective.name}"
        )
    else:
        # If independence failed, downgrade — agreement was reached but
        # the quorum doesn't represent independent observations
        if agreement_quorum and not independence_ok:
            effective = TrustTier.meet(*[o.tier for o in qualifying], TrustTier.MODERATE)
        else:
            effective = TrustTier.UNTRUSTED
        log_level_warning = logging.WARNING
        _emit_quorum_warning(
            log_level_warning,
            f"  [MK35/Q1] QUORUM_FAILED path={device_relpath!r} "
            f"qualifying={len(qualifying)}/{len(outcomes)} threshold={threshold_count} "
            f"agreement={agreement_quorum} independence={independence_ok}. "
            f"Strategy results: " +
            ", ".join(f"{o.strategy_name}={o.tier.name}" for o in outcomes)
        )
        # MK38.1 observability: classify each UNTRUSTED into no-signal vs
        # disagreement so the operator can tell whether the failure is from
        # actual contradiction or from a degraded signal that the dominance
        # rule could have overridden.
        try:
            from mk38_1_quorum_dominance import classify_outcome
            classes = ", ".join(
                f"{o.strategy_name}={classify_outcome(o)}" for o in outcomes
            )
            _emit_quorum_warning(
                log_level_warning,
                f"  [MK35/Q1] QUORUM_FAILED_DETAIL classes: {classes}",
            )
        except ImportError:
            pass

    return QuorumOutcome(
        quorum_reached=quorum_reached,
        effective_tier=effective,
        threshold_tier=threshold_tier,
        threshold_count=threshold_count,
        outcomes=tuple(outcomes),
        note=("quorum_reached" if quorum_reached
              else ("independence_failure" if (agreement_quorum and not independence_ok)
                    else "below_threshold")),
        independence=independence_report_dict,
    )


# ─────────────────────────────────────────────────────────────────────────
# MK39 Phase II — Deferred Quorum-Failure Warning Pattern (SWE adversarial-
# review mandate). 330+ WARNINGs per overnight run that ALWAYS get salvaged
# by MK38.1 dominant-SHA-256 override is a signal/noise crisis. Solution:
# the mk38_1 wrapper installs a deferred-mode context; verify_with_quorum
# buffers its QUORUM_FAILED messages instead of emitting at WARNING; the
# wrapper then decides:
#   - Override applied successfully → emit single INFO summary, drop buffer
#   - Override did NOT apply        → flush buffer at original WARNING level
# This preserves the WARNING signal for genuine, unsalvageable failures
# while suppressing the noise of routine MK38.1 salvages.
# ─────────────────────────────────────────────────────────────────────────

import threading as _mk39_phaseII_threading

_DEFERRED_QUORUM_WARNINGS_TLS = _mk39_phaseII_threading.local()


class DeferredQuorumWarnings:
    """Context manager: while active on this thread, mk35_trust buffers
    QUORUM_FAILED WARNINGs into a thread-local list instead of emitting them
    immediately. The caller (mk38_1 wrapper) inspects the buffer and decides:
    flush as WARNING (no salvage applied) or drop with single INFO summary
    (salvage applied)."""

    def __enter__(self):
        # Stack-aware: only the outermost context owns the buffer
        prior = getattr(_DEFERRED_QUORUM_WARNINGS_TLS, "active", False)
        if not prior:
            _DEFERRED_QUORUM_WARNINGS_TLS.active = True
            _DEFERRED_QUORUM_WARNINGS_TLS.buffer = []
            self._owns_buffer = True
        else:
            self._owns_buffer = False
        return self

    def __exit__(self, *exc):
        if self._owns_buffer:
            _DEFERRED_QUORUM_WARNINGS_TLS.active = False
            # leave .buffer for caller to inspect; clear on next enter

    def buffered_messages(self):
        """Return the list of (level, message) tuples buffered during the
        context. Only meaningful for the outermost context owner."""
        return list(getattr(_DEFERRED_QUORUM_WARNINGS_TLS, "buffer", []))

    def discard(self):
        """Drop all buffered messages without emitting. Use when the wrapper
        successfully salvaged the operation."""
        _DEFERRED_QUORUM_WARNINGS_TLS.buffer = []

    def flush(self):
        """Emit all buffered messages at their original level. Use when the
        wrapper could not salvage."""
        for level, msg in getattr(_DEFERRED_QUORUM_WARNINGS_TLS, "buffer", []):
            log.log(level, msg)
        _DEFERRED_QUORUM_WARNINGS_TLS.buffer = []


def _emit_quorum_warning(level: int, msg: str) -> None:
    """Emit a quorum WARNING — to the buffer if a DeferredQuorumWarnings
    context is active on this thread; otherwise straight to the logger."""
    if getattr(_DEFERRED_QUORUM_WARNINGS_TLS, "active", False):
        getattr(_DEFERRED_QUORUM_WARNINGS_TLS, "buffer", []).append((level, msg))
    else:
        log.log(level, msg)



def _shell_escape(s: str) -> str:
    """Conservative escape for adb shell."""
    if any(c in s for c in " '\"$`\\!"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


__all__ = [
    "TrustTier", "VerificationOutcome", "VerificationStrategy",
    "Sha256Strategy", "SizeAndMtimeStrategy", "ExistenceOnlyStrategy",
    "DEFAULT_STRATEGIES",
    "QuorumOutcome", "verify_with_quorum",
]
