"""
MK15-R0: Observation Halt Gate — Formal distributed consensus semantics

CRITICAL: This module implements the structural halt mechanism that Log 2 proved was missing.
When device inventory returns 0 files, the system must HALT before Stage 1, not log and continue.

Architecture:
    - ObservationQuorum: Multi-observer consensus protocol
    - TrustThreshold: Minimum trust_score required for Stage 1 entry
    - HaltGate: Enforced boundary at mk34_invariants.gate_stage_entry()

Log Evidence:
    LOG_5_7_5_26.txt line 18:   "Device inventory: 0 audio files indexed"
    LOG_5_7_5_26.txt line 2373: "Live scan returned 0 — almost certainly device-scan failure"
                                 → STAGE 1 PROCEEDED ANYWAY

This is existential — observation collapse without halt = data corruption.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from enum import Enum
import hashlib
import json
from datetime import datetime
import uuid


class ObserverType(Enum):
    """Observer identity for quorum tracking"""
    ADB = "adb"           # Primary authoritative observer
    COM = "com"           # MTP/COM alternative observer  
    CACHE = "cache"       # Snapshot lineage from previous run
    MANUAL = "manual"     # User-provided inventory


class HaltReason(Enum):
    """Why observation gate triggered halt"""
    TRUST_INSUFFICIENT = "observation_trust_below_threshold"
    QUORUM_FAILURE = "insufficient_observer_agreement"
    ZERO_OBSERVATION = "authoritative_observer_returned_zero"
    DIVERGENCE = "observers_disagree_beyond_tolerance"


@dataclass
class DeviceObservedState:
    """
    What an observer claims about device state.
    
    This extends mk34_observer return type to include trust metadata.
    Every observer must return this structure, not just a file list.
    """
    observer: ObserverType
    file_count: int
    total_bytes: int
    sampled_paths: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    # Trust metadata (NEW in MK15)
    trust_score: float = 1.0  # 0.0 = completely untrusted, 1.0 = fully trusted
    confidence: float = 1.0   # How confident is THIS observation (vs trust in observer)
    
    # Provenance
    observation_method: str = ""  # "adb_find", "com_listdir", "cache_restore"
    elapsed_seconds: float = 0.0
    
    # Snapshot hash for determinism check
    snapshot_hash: str = ""
    
    def compute_snapshot_hash(self) -> str:
        """
        Content-addressable hash of observed state.
        Used to detect observer divergence and lineage corruption.
        """
        # Sort paths deterministically
        paths_sorted = sorted(self.sampled_paths)
        content = {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "paths": paths_sorted[:1000]  # Cap at 1000 for hash stability
        }
        canonical = json.dumps(content, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass
class ObservationQuorumResult:
    """Result of quorum consensus evaluation"""
    consensus_reached: bool
    primary_observation: Optional[DeviceObservedState]
    quorum_observations: List[DeviceObservedState]
    halt_reason: Optional[HaltReason]
    
    # Diagnostics
    file_count_range: Tuple[int, int] = (0, 0)
    delta_percentage: float = 0.0
    trust_scores: Dict[ObserverType, float] = field(default_factory=dict)
    
    def to_audit_marker(self) -> Dict:
        """Audit log entry for gate decision"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "consensus_reached": self.consensus_reached,
            "halt_reason": self.halt_reason.value if self.halt_reason else None,
            "observations": [
                {
                    "observer": obs.observer.value,
                    "file_count": obs.file_count,
                    "trust_score": obs.trust_score,
                    "snapshot_hash": obs.snapshot_hash
                }
                for obs in self.quorum_observations
            ],
            "file_count_range": self.file_count_range,
            "delta_percentage": self.delta_percentage
        }


class ObservationHaltGate:
    """
    Distributed consensus gate for observation layer.
    
    This is the structural enforcement the CTO review demanded.
    NOT just logging — ACTUAL HALT via raised InvariantViolation.
    """
    
    # Configuration (tunable via mk35_policy)
    MINIMUM_TRUST_THRESHOLD = 0.5      # Trust score < 0.5 = HALT
    QUORUM_COUNT = 2                    # Require at least 2 observers
    QUORUM_DELTA_TOLERANCE = 0.10       # Observers must agree within 10%
    ZERO_OBSERVATION_IS_FATAL = True    # Zero files from primary = HALT
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        
        # Override defaults from policy if provided
        if config:
            self.MINIMUM_TRUST_THRESHOLD = config.get("min_trust", 0.5)
            self.QUORUM_COUNT = config.get("quorum_count", 2)
            self.QUORUM_DELTA_TOLERANCE = config.get("delta_tolerance", 0.10)
    
    def evaluate_quorum(
        self,
        observations: List[DeviceObservedState]
    ) -> ObservationQuorumResult:
        """
        Core consensus algorithm.
        
        Returns:
            ObservationQuorumResult with consensus_reached=True if safe to proceed,
            or consensus_reached=False with HaltReason if gate should fire.
        """
        if not observations:
            return ObservationQuorumResult(
                consensus_reached=False,
                primary_observation=None,
                quorum_observations=[],
                halt_reason=HaltReason.QUORUM_FAILURE
            )
        
        # Identify primary observer (highest trust)
        primary = max(observations, key=lambda o: o.trust_score)
        
        # CRITICAL CHECK 1: Zero observation from primary is FATAL
        if self.ZERO_OBSERVATION_IS_FATAL and primary.file_count == 0:
            return ObservationQuorumResult(
                consensus_reached=False,
                primary_observation=primary,
                quorum_observations=observations,
                halt_reason=HaltReason.ZERO_OBSERVATION,
                file_count_range=(0, 0),
                trust_scores={obs.observer: obs.trust_score for obs in observations}
            )
        
        # CRITICAL CHECK 2: Trust threshold
        if primary.trust_score < self.MINIMUM_TRUST_THRESHOLD:
            return ObservationQuorumResult(
                consensus_reached=False,
                primary_observation=primary,
                quorum_observations=observations,
                halt_reason=HaltReason.TRUST_INSUFFICIENT,
                trust_scores={obs.observer: obs.trust_score for obs in observations}
            )
        
        # CRITICAL CHECK 3: Quorum count
        if len(observations) < self.QUORUM_COUNT:
            return ObservationQuorumResult(
                consensus_reached=False,
                primary_observation=primary,
                quorum_observations=observations,
                halt_reason=HaltReason.QUORUM_FAILURE,
                trust_scores={obs.observer: obs.trust_score for obs in observations}
            )
        
        # CRITICAL CHECK 4: Observer agreement (delta tolerance)
        file_counts = [obs.file_count for obs in observations]
        min_count = min(file_counts)
        max_count = max(file_counts)
        
        if min_count == 0:
            delta_pct = 1.0  # 100% divergence if any observer sees zero
        else:
            delta_pct = (max_count - min_count) / min_count
        
        if delta_pct > self.QUORUM_DELTA_TOLERANCE:
            return ObservationQuorumResult(
                consensus_reached=False,
                primary_observation=primary,
                quorum_observations=observations,
                halt_reason=HaltReason.DIVERGENCE,
                file_count_range=(min_count, max_count),
                delta_percentage=delta_pct,
                trust_scores={obs.observer: obs.trust_score for obs in observations}
            )
        
        # ALL CHECKS PASSED — consensus reached
        return ObservationQuorumResult(
            consensus_reached=True,
            primary_observation=primary,
            quorum_observations=observations,
            halt_reason=None,
            file_count_range=(min_count, max_count),
            delta_percentage=delta_pct,
            trust_scores={obs.observer: obs.trust_score for obs in observations}
        )
    
    def gate_stage_entry(
        self,
        stage: int,
        observations: List[DeviceObservedState]
    ) -> ObservationQuorumResult:
        """
        Formal halt gate at stage boundary.
        
        This is called by mk34_invariants.gate_stage_entry(stage=1, observation_result).
        
        Raises:
            InvariantViolation: If consensus fails (imports from mk34_invariants)
        
        Returns:
            ObservationQuorumResult if consensus succeeds
        """
        result = self.evaluate_quorum(observations)
        
        if not result.consensus_reached:
            # Import here to avoid circular dependency
            from mk34_invariants import InvariantViolation
            
            # mk34_invariants.InvariantViolation signature:
            #   (invariant_name: str, detail: str, sample: list = None)
            raise InvariantViolation(
                invariant_name=f"OBSERVATION_HALT_GATE_STAGE_{stage}",
                detail=f"{result.halt_reason.value}: {result.to_audit_marker()}",
            )
        
        return result


# Factory for integration with existing code
def create_halt_gate(policy_config: Optional[Dict] = None) -> ObservationHaltGate:
    """Create gate with policy-driven configuration"""
    return ObservationHaltGate(config=policy_config)


# Convenience function for mk34_observer integration
def build_device_observation(
    observer_type: ObserverType,
    file_count: int,
    total_bytes: int,
    sampled_paths: List[str],
    method: str,
    elapsed: float,
    trust_score: float = 1.0
) -> DeviceObservedState:
    """
    Factory for DeviceObservedState that auto-computes snapshot hash.
    
    This should be called by mk34_observer.observe_device() instead of
    returning a raw dict.
    """
    obs = DeviceObservedState(
        observer=observer_type,
        file_count=file_count,
        total_bytes=total_bytes,
        sampled_paths=sampled_paths,
        observation_method=method,
        elapsed_seconds=elapsed,
        trust_score=trust_score
    )
    obs.snapshot_hash = obs.compute_snapshot_hash()
    return obs


# Audit marker constants (for grep-ability)
AUDIT_MARKER_HALT_FIRED = "OBSERVATION_HALT_GATE_FIRED"
AUDIT_MARKER_CONSENSUS_OK = "OBSERVATION_CONSENSUS_REACHED"
AUDIT_MARKER_QUORUM_OVERRIDE = "OBSERVATION_QUORUM_OVERRIDE"  # If operator forces past halt
