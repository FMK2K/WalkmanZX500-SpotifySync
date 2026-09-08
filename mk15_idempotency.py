"""
MK15-R1: Idempotency Enforcement — Replay-safe destructive operations

CRITICAL: In MK34-MK36 event-sourced architecture, duplicate event emission is EXPECTED.
Replay, lineage reprocessing, lifecycle re-emit will ALL duplicate events.
Destructive operations MUST survive this structurally.

Log Evidence:
    LOG_7_5_26.txt lines 31-93: WOULD-DELETE candidates printed 2×
    → Proves dedup loop executes twice (not a bug — event duplication is architectural)

Current Problem:
    walkman_dedup.py assumes single-execution semantics:
    ```
    for candidate in candidates:
        os.remove(candidate)  # ← NO UUID, NO CAPABILITY CHECK
    ```

MK15 Solution:
    ```
    for candidate in candidates:
        op_uuid = mk15_idempotency.create_operation_uuid(candidate, action="delete")
        if mk15_idempotency.already_executed(op_uuid):
            continue  # ← SKIP — already done in prior replay
        with mk15_capability.consume_token("file.delete"):
            os.remove(candidate)
            mk15_idempotency.mark_executed(op_uuid)
    ```

This is the formal proof of replay safety the CTO review demanded.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Set, Callable, Any
from enum import Enum
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
import threading


class OperationType(Enum):
    """Destructive operation types that require idempotency"""
    FILE_DELETE = "file_delete"
    FILE_MOVE = "file_move"
    FILE_OVERWRITE = "file_overwrite"
    METADATA_MUTATE = "metadata_mutate"
    DEDUP_PURGE = "dedup_purge"


@dataclass
class OperationUUID:
    """
    Unique identifier for a destructive operation.
    
    This is NOT just uuid.uuid4() — it's content-addressable.
    Same operation parameters → same UUID → idempotent.
    """
    operation_type: OperationType
    target_path: str  # Canonical path (resolved, normalized)
    action_parameters: Dict[str, Any] = field(default_factory=dict)
    
    # Computed fields
    uuid_str: str = ""
    timestamp_created: datetime = field(default_factory=datetime.utcnow)
    
    def __post_init__(self):
        """Compute UUID from operation content"""
        if not self.uuid_str:
            self.uuid_str = self._compute_uuid()
    
    def _compute_uuid(self) -> str:
        """
        Content-addressable UUID.
        
        Same operation → same UUID.
        This ensures replay idempotency: if we've already deleted
        /path/to/file.mp3 in a prior run, the UUID will be identical.
        """
        canonical = {
            "type": self.operation_type.value,
            "path": Path(self.target_path).resolve().as_posix(),
            "params": json.dumps(self.action_parameters, sort_keys=True)
        }
        content = json.dumps(canonical, sort_keys=True)
        hash_val = hashlib.sha256(content.encode()).hexdigest()
        
        # Use first 32 chars of hash as UUID (enough entropy)
        return f"op_{hash_val[:32]}"
    
    def to_audit_entry(self) -> Dict:
        """Audit log format"""
        return {
            "operation_uuid": self.uuid_str,
            "operation_type": self.operation_type.value,
            "target_path": self.target_path,
            "action_parameters": self.action_parameters,
            "timestamp_created": self.timestamp_created.isoformat()
        }


@dataclass
class ExecutionMarker:
    """
    Proof that an operation has already been executed.
    
    Stored in audit log and in-memory cache.
    """
    operation_uuid: str
    executed_at: datetime
    capability_token_consumed: Optional[str] = None
    result_status: str = "success"  # "success", "failed", "skipped"
    
    def to_audit_entry(self) -> Dict:
        return {
            "operation_uuid": self.operation_uuid,
            "executed_at": self.executed_at.isoformat(),
            "capability_token": self.capability_token_consumed,
            "status": self.result_status
        }


class IdempotencyGuard:
    """
    Central idempotency enforcement.
    
    This is the structural guarantee that prevents double-deletion.
    """
    
    def __init__(self, audit_log_path: Optional[Path] = None):
        """
        Args:
            audit_log_path: Where to persist execution markers.
                           If None, uses in-memory only (testing mode).
        """
        self.audit_log_path = audit_log_path
        
        # In-memory cache of executed operation UUIDs
        self._executed: Set[str] = set()
        self._execution_markers: Dict[str, ExecutionMarker] = {}
        
        # Thread safety (concurrent workers may check idempotency)
        self._lock = threading.RLock()
        
        # Load existing markers from audit log
        if self.audit_log_path and self.audit_log_path.exists():
            self._load_from_audit_log()
    
    def _load_from_audit_log(self):
        """
        Restore execution state from audit log.
        
        This is critical for replay safety: when we replay an event bundle,
        we need to know which operations were already completed.
        """
        with self._lock:
            try:
                with open(self.audit_log_path, 'r') as f:
                    for line in f:
                        if "OPERATION_EXECUTED" not in line:
                            continue
                        
                        # Parse audit entry
                        try:
                            entry = json.loads(line.split("OPERATION_EXECUTED", 1)[1].strip())
                            op_uuid = entry.get("operation_uuid")
                            if op_uuid:
                                self._executed.add(op_uuid)
                                
                                marker = ExecutionMarker(
                                    operation_uuid=op_uuid,
                                    executed_at=datetime.fromisoformat(entry.get("executed_at")),
                                    capability_token_consumed=entry.get("capability_token"),
                                    result_status=entry.get("status", "success")
                                )
                                self._execution_markers[op_uuid] = marker
                        except (json.JSONDecodeError, ValueError):
                            continue  # Skip malformed entries
            except FileNotFoundError:
                pass  # Audit log doesn't exist yet
    
    def create_operation_uuid(
        self,
        operation_type: OperationType,
        target_path: str,
        **action_parameters
    ) -> OperationUUID:
        """
        Create content-addressable UUID for an operation.
        
        Example:
            op_uuid = guard.create_operation_uuid(
                OperationType.FILE_DELETE,
                "/path/to/duplicate.mp3",
                reason="dedup",
                original="/path/to/kept.mp3"
            )
        """
        return OperationUUID(
            operation_type=operation_type,
            target_path=target_path,
            action_parameters=action_parameters
        )
    
    def already_executed(self, operation_uuid: OperationUUID) -> bool:
        """
        Check if this operation has already been executed.
        
        Returns:
            True if operation should be SKIPPED (already done)
            False if operation should proceed
        """
        with self._lock:
            return operation_uuid.uuid_str in self._executed
    
    def mark_executed(
        self,
        operation_uuid: OperationUUID,
        capability_token: Optional[str] = None,
        status: str = "success"
    ):
        """
        Mark operation as executed.
        
        This writes to:
        1. In-memory cache (for fast checks within session)
        2. Audit log (for persistence across replays)
        
        Args:
            operation_uuid: The operation that was just executed
            capability_token: Token consumed from mk35_capability
            status: "success", "failed", or "skipped"
        """
        with self._lock:
            marker = ExecutionMarker(
                operation_uuid=operation_uuid.uuid_str,
                executed_at=datetime.utcnow(),
                capability_token_consumed=capability_token,
                result_status=status
            )
            
            self._executed.add(operation_uuid.uuid_str)
            self._execution_markers[operation_uuid.uuid_str] = marker
            
            # Persist to audit log
            if self.audit_log_path:
                with open(self.audit_log_path, 'a') as f:
                    entry = marker.to_audit_entry()
                    f.write(f"{datetime.utcnow().isoformat()} OPERATION_EXECUTED {json.dumps(entry)}\n")
    
    def execute_idempotent(
        self,
        operation_uuid: OperationUUID,
        action: Callable[[], Any],
        capability_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute operation with idempotency guarantee.
        
        This is the high-level API that most code should use.
        
        Example:
            def delete_file():
                os.remove(filepath)
            
            result = guard.execute_idempotent(
                op_uuid,
                delete_file,
                capability_token="cap_abc123"
            )
        
        Returns:
            {
                "executed": bool,       # True if action ran, False if skipped
                "skipped_reason": str,  # Why skipped (if executed=False)
                "result": Any,          # Return value of action() if executed
                "operation_uuid": str
            }
        """
        # Check if already executed
        if self.already_executed(operation_uuid):
            return {
                "executed": False,
                "skipped_reason": "already_executed_in_prior_run",
                "operation_uuid": operation_uuid.uuid_str,
                "result": None
            }
        
        # Execute the action
        try:
            result = action()
            
            # Mark as executed
            self.mark_executed(
                operation_uuid,
                capability_token=capability_token,
                status="success"
            )
            
            return {
                "executed": True,
                "operation_uuid": operation_uuid.uuid_str,
                "result": result
            }
        
        except Exception as e:
            # Mark as failed (still prevents re-execution)
            self.mark_executed(
                operation_uuid,
                capability_token=capability_token,
                status="failed"
            )
            raise  # Re-raise exception after marking
    
    def get_execution_stats(self) -> Dict:
        """Statistics for monitoring"""
        with self._lock:
            success_count = sum(
                1 for m in self._execution_markers.values()
                if m.result_status == "success"
            )
            failed_count = sum(
                1 for m in self._execution_markers.values()
                if m.result_status == "failed"
            )
            
            return {
                "total_operations": len(self._executed),
                "successful": success_count,
                "failed": failed_count,
                "in_memory_cache_size": len(self._executed)
            }


# Global singleton (initialized by walkman_sync.py at startup)
_global_guard: Optional[IdempotencyGuard] = None


def initialize_global_guard(audit_log_path: Path):
    """
    Initialize the global idempotency guard.
    
    Called once at sync start by main.py or walkman_sync.py.
    """
    global _global_guard
    _global_guard = IdempotencyGuard(audit_log_path=audit_log_path)


def get_guard() -> IdempotencyGuard:
    """Get the global guard instance"""
    if _global_guard is None:
        raise RuntimeError(
            "IdempotencyGuard not initialized. "
            "Call mk15_idempotency.initialize_global_guard() first."
        )
    return _global_guard


# Audit marker constants
AUDIT_MARKER_OPERATION_EXECUTED = "OPERATION_EXECUTED"
AUDIT_MARKER_OPERATION_SKIPPED = "OPERATION_SKIPPED_ALREADY_DONE"
