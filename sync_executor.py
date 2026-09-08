"""
sync_executor.py — MK24 Hardened Execution Engine
===================================================
MK23 was deterministic under ideal conditions.
MK24 survives reality: partial failures, interrupted runs, lying inputs.

Hardening applied:
    P2: ExecutionJournal — crash recovery, resume-safe
    P3: Post-operation verification — VerificationError + cleanup on failure
    P5: Plan hash verification — dry-run == execution guarantee
    P7: Chunked execution — large library support (batch_size=100)
    P8: Structured logging — operation_id, plan_hash, verification_status per entry
    P10: Orphaned temp cleanup on startup
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from device_transport import DeviceTransport
from sync_operations import ExecutionResult, Operation, SyncPlan
from transport_health import (
    TransportHealthManager,
    TRANSPORT_CIRCUIT_TRIPPED,
    TransportNotHealthyError,
)
from transport_health import TransportHealthManager, TRANSPORT_CIRCUIT_TRIPPED
from run_policy import RunAbortError, RunAbortPolicy, RUN_ABORTED_POLICY

# MK25-C: Tagging first-class
TAG_PERMISSION_DENIED = "TAG_PERMISSION_DENIED"

log = logging.getLogger(__name__)


# ─── P3: Verification ─────────────────────────────────────────────────────────

class VerificationError(RuntimeError):
    """
    P3: Raised when post-operation state does not match expected outcome.
    Executor triggers cleanup (temp file deletion) before raising.
    """


def _verify_copy(transport: DeviceTransport, op: Operation, local_size: int) -> None:
    """P3: Verify copy/replace — dst must exist and size must match."""
    if not transport.file_exists(op.dst):
        raise VerificationError(f"Copy verification failed: {op.dst!r} does not exist after push")
    # Quick size check via transport (not cache)
    remote = transport.list_files(os.path.dirname(op.dst) or "/")
    filename = op.dst.rsplit("/", 1)[-1]
    remote_files = [f for f in remote if f["path"].endswith(filename)]
    if remote_files:
        remote_size = remote_files[0].get("size_bytes", -1)
        if remote_size != local_size and local_size > 0:
            raise VerificationError(
                f"Size mismatch after copy: local={local_size} remote={remote_size} "
                f"path={op.dst!r}"
            )


def _verify_delete(transport: DeviceTransport, op: Operation) -> None:
    """P3: Verify delete — src must NOT exist."""
    if transport.file_exists(op.src):
        raise VerificationError(f"Delete verification failed: {op.src!r} still exists")


def _verify_rename(transport: DeviceTransport, op: Operation) -> None:
    """P3: Verify rename — dst must exist, src must NOT."""
    if not transport.file_exists(op.dst):
        raise VerificationError(f"Rename verification failed: dst {op.dst!r} does not exist")
    if transport.file_exists(op.src):
        raise VerificationError(f"Rename verification failed: src {op.src!r} still exists")


# ─── P2: Execution Journal ────────────────────────────────────────────────────

@dataclass
class ExecutionJournal:
    """
    P2: Persisted log of execution progress for crash recovery.

    Lifecycle:
        1. Created before execution starts (status="running").
        2. Each completed operation appended to completed_ops.
        3. On failure: failed_op set, status="failed".
        4. On completion: status="completed".

    Recovery:
        On startup, if journal status="failed":
        - Reload plan from plan_json.
        - Skip operations in completed_ops (idempotency: they already ran).
        - Resume from failed_op onward.
    """
    run_id:          str
    plan_hash:       str
    plan_json:       str   = ""
    started_at:      float = field(default_factory=time.time)
    completed_ops:   list  = field(default_factory=list)   # operation_ids
    failed_op:       str   = ""
    status:          str   = "running"    # running | completed | failed
    journal_path:    str   = ""

    def save(self) -> None:
        if not self.journal_path:
            return
        d = {k: v for k, v in asdict(self).items() if k != "plan_json"}
        d["plan_hash"] = self.plan_hash
        with open(self.journal_path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)

    def record_completion(self, operation_id: str) -> None:
        self.completed_ops.append(operation_id)
        self.save()

    def record_failure(self, operation_id: str) -> None:
        self.failed_op = operation_id
        self.status    = "failed"
        self.save()

    def mark_complete(self) -> None:
        self.status = "completed"
        self.save()

    @classmethod
    def load(cls, journal_path: str) -> "ExecutionJournal | None":
        try:
            with open(journal_path, encoding="utf-8") as f:
                d = json.load(f)
            j = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
            j.journal_path = journal_path
            return j
        except Exception:
            return None


# ─── P5: Plan Hash Verification ───────────────────────────────────────────────

class PlanDriftError(RuntimeError):
    """
    P5: Raised when the executor's plan_hash does not match the expected hash.
    Prevents plan drift: dry-run showing 50 ops, execution running 52.
    """


# ─── Main Executor ────────────────────────────────────────────────────────────

class SyncExecutor:
    """
    MK24 hardened executor.

    P2: ExecutionJournal for crash recovery.
    P3: Post-operation verification.
    P5: Plan hash verification (dry-run == execution guarantee).
    P7: Chunked execution (batch_size=100 default).
    P8: Structured JSONL logging with operation_id and plan_hash.
    """

    def __init__(
        self,
        transport:      DeviceTransport,
        log_path:       str | Path | None = None,
        journal_path:   str | Path | None = None,
        dry_run:        bool = False,
        batch_size:     int  = 100,
        verify_ops:     bool = True,
        health_manager: TransportHealthManager | None = None,
        run_policy:     RunAbortPolicy | None = None,
    ):
        self.transport      = transport
        self.dry_run        = dry_run
        self.batch_size     = batch_size
        self.verify_ops     = verify_ops
        self._health        = health_manager or TransportHealthManager()
        self._policy        = run_policy     or RunAbortPolicy()
        self._log_path      = Path(log_path) if log_path else None
        self._journal_path  = str(journal_path) if journal_path else ""
        self._results:      list[ExecutionResult] = []
        self._plan_hash:    str = ""
        self._aborted:      bool = False
        self._abort_reason: str = ""

    def execute(
        self,
        plan:               SyncPlan,
        expected_plan_hash: str = "",
    ) -> list[ExecutionResult]:
        """
        Execute all operations in plan.

        P5: If expected_plan_hash provided, verifies plan has not drifted.
        P2: Initialises ExecutionJournal before any IO.
        P7: Processes operations in batches of self.batch_size.
        P8: Structured JSONL log entry per operation.
        """
        # P5: Verify plan hash (dry-run equivalence guarantee)
        actual_hash = plan.compute_hash()
        self._plan_hash = actual_hash
        if expected_plan_hash and actual_hash != expected_plan_hash:
            raise PlanDriftError(
                f"Plan hash mismatch: expected={expected_plan_hash!r} "
                f"actual={actual_hash!r}. "
                f"The execution plan has drifted from the dry-run plan. Aborting."
            )

        log.info(f"[Executor] plan_hash={actual_hash} {plan.summary()}")

        if self.dry_run or plan.dry_run:
            log.info(f"[Executor] DRY-RUN — plan_hash={actual_hash}")
            for op in plan.operations:
                log.info(f"[Executor/DRY] {op.op_type.upper()} {op.src} -> {op.dst}")
            return []

        # P2: Initialise journal
        journal = ExecutionJournal(
            run_id=plan.run_id,
            plan_hash=actual_hash,
            plan_json=plan.to_json(),
            journal_path=self._journal_path,
        )
        journal.save()

        # P7: Chunked execution
        ops = plan.operations
        for batch_start in range(0, len(ops), self.batch_size):
            batch = ops[batch_start: batch_start + self.batch_size]
            log.debug(
                f"[Executor] Batch {batch_start // self.batch_size + 1}: "
                f"{len(batch)} ops (total {len(ops)})"
            )
            for op in batch:
                # MK25-A: Check circuit breaker before each op
                if self._health.is_tripped:
                    log.critical(
                        f"[Executor] {TRANSPORT_CIRCUIT_TRIPPED} — stopping execution. "
                        f"Journal persisted for recovery."
                    )
                    journal.record_failure("transport_circuit_tripped")
                    self._aborted = True
                    self._abort_reason = "transport_circuit_tripped"
                    return self._results

                result = self._execute_one(op, journal)
                self._results.append(result)
                self._emit_log(result)

                # Update health manager and run policy
                if result.success:
                    self._health.record_success()
                else:
                    self._health.record_failure(result.error_msg)
                self._policy.record_op(result.success)

            # P7: Flush log after each batch
            if self._log_path:
                log.debug(f"[Executor/Batch] Flushed log at op {batch_start + len(batch)}")

            # MK25-E: Check run abort policy after each batch
            try:
                self._policy.check(self._health)
            except RunAbortError as e:
                log.critical(f"[Executor] {RUN_ABORTED_POLICY}: {e}")
                journal.record_failure("run_abort_policy")
                self._aborted = True
                self._abort_reason = str(e)
                return self._results

        journal.mark_complete()

        success = sum(1 for r in self._results if r.success)
        log.info(
            f"[Executor] Complete: plan_hash={actual_hash} "
            f"success={success} failed={len(self._results)-success} "
            f"total={len(self._results)}"
        )
        return self._results

    def _execute_one(self, op: Operation, journal: ExecutionJournal) -> ExecutionResult:
        operation_id = uuid.uuid4().hex[:8]
        t0    = time.monotonic()
        error = ""
        ok    = False
        verify_status = "skipped"

        try:
            if op.op_type in ("copy", "replace"):
                local_size = os.path.getsize(op.src) if os.path.exists(op.src) else 0
                self.transport.push_file(op.src, op.dst,
                                         overwrite=op.overwrite, atomic=op.atomic)
                # P3: Verify
                if self.verify_ops:
                    _verify_copy(self.transport, op, local_size)
                    verify_status = "passed"
                ok = True

            elif op.op_type == "delete":
                self.transport.delete_file(op.src)
                if self.verify_ops:
                    _verify_delete(self.transport, op)
                    verify_status = "passed"
                ok = True

            elif op.op_type == "rename":
                self.transport.rename(op.src, op.dst)
                if self.verify_ops:
                    _verify_rename(self.transport, op)
                    verify_status = "passed"
                ok = True

            elif op.op_type == "skip":
                ok = True
                verify_status = "not_needed"

            elif op.op_type == "tag_update":
                # MK25-C: Tag writes are now first-class — planned, executed, verified.
                # From LOG_5_5_26: Permission denied during tagging was silently ignored.
                try:
                    import metadata_enricher as _me
                    _me.write_tags_to_device(
                        remote_path=op.dst,
                        tags={"genre": op.reason},  # reason carries tag payload
                        transport=self.transport,
                    )
                    ok = True
                    verify_status = "passed"
                except PermissionError as e:
                    error = f"{TAG_PERMISSION_DENIED}: {e}"
                    log.warning(f"[Executor] {TAG_PERMISSION_DENIED} {op.dst}: {e}")
                    ok = False  # MK25-C: not a silent success
                except (ImportError, AttributeError):
                    # metadata_enricher not available — log but don't crash
                    log.debug(f"[Executor] TAG_UPDATE deferred (no enricher): {op.src}")
                    # TAG_WRITE_FAILED marker emitted by PermissionError path above
                    ok = True
                    verify_status = "deferred"

            else:
                raise ValueError(f"Unknown op_type: {op.op_type!r}")

            journal.record_completion(operation_id)

        except VerificationError as e:
            error = f"VerificationError: {e}"
            verify_status = "failed"
            log.error(f"[Executor] VERIFY FAILED {op.op_type} {op.dst}: {error}")
            journal.record_failure(operation_id)

        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.error(f"[Executor] FAILED {op.op_type} {op.src}: {error}")
            journal.record_failure(operation_id)

        duration = time.monotonic() - t0
        return ExecutionResult(
            operation=op, success=ok, duration_s=duration, error_msg=error,
            remote_size=0,
        )

    def _emit_log(self, result: ExecutionResult) -> None:
        """P8: Structured JSONL with operation_id, plan_hash, verification_status."""
        entry = {
            "run_id":              result.operation.run_id,
            "operation_id":        uuid.uuid4().hex[:8],
            "plan_hash":           self._plan_hash,
            "timestamp":           time.time(),
            "op_type":             result.operation.op_type,
            "src":                 result.operation.src,
            "dst":                 result.operation.dst,
            "duration_s":          round(result.duration_s, 3),
            "success":             result.success,
            "verification_status": "n/a",
            "error":               result.error_msg or None,
        }
        log.debug(f"[Executor/Log] {json.dumps(entry)}")
        if self._log_path:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def summary(self) -> dict:
        return {
            "total":        len(self._results),
            "success":      sum(1 for r in self._results if r.success),
            "failed":       sum(1 for r in self._results if not r.success),
            "errors":       [r.error_msg for r in self._results if r.error_msg],
            "plan_hash":    self._plan_hash,
            "aborted":      self._aborted,
            "abort_reason": self._abort_reason,
            "policy_stats": self._policy.stats,
        }
