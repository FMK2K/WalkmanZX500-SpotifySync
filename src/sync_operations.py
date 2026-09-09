"""
sync_operations.py — MK23 Planner-Executor Operation Contracts
===============================================================
Immutable, serializable Operation and FileState value objects.

MK23 architectural invariant: Every file operation is declared as an
explicit Operation before any IO occurs. No implicit overwrites. No
decisions during execution. The Planner produces the plan; the Executor
is dumb.

Design principles:
    - frozen=True: plan is immutable after generation
    - All fields explicit: overwrite, atomic, reason all declared
    - JSON-serializable: dry-run audit writes the full plan to disk
    - No IO in this module — pure data structures
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


# ─── Operation types ─────────────────────────────────────────────────────────

OpType = Literal["copy", "replace", "delete", "rename", "skip", "tag_update"]


@dataclass(frozen=True)
class Operation:
    """
    A single, immutable unit of work for the Executor.

    Fields:
        op_type:   What to do. One of copy/replace/delete/rename/skip/tag_update.
        src:       Source path (local for push, device for delete/rename).
        dst:       Destination path on device (empty for delete).
        overwrite: Whether to overwrite an existing destination file.
        atomic:    If True, push to .tmp_<uuid>_<name> then rename atomically.
        reason:    Human-readable reason (for audit log). E.g. "inferior_format".
        run_id:    Links back to the pipeline run for correlation.
    """
    op_type:   OpType
    src:       str
    dst:       str
    overwrite: bool  = True
    atomic:    bool  = True
    reason:    str   = ""
    run_id:    str   = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "Operation":
        return cls(**json.loads(s))


@dataclass
class FileState:
    """
    Snapshot of a file's state for comparison by the Planner.
    Either local (Path-based) or remote (ADB path string).
    """
    path:         str
    size_bytes:   int
    mtime:        float          # Unix timestamp; 0 if unknown
    content_hash: str  = ""     # MD5/SHA256 if computed; empty = not computed yet
    storage:      str  = ""     # "SD card" | "Internal shared storage" | "local"
    artist:       str  = ""     # from ID3/Vorbis tag (if read)
    album:        str  = ""
    track_number: str  = ""
    genre:        str  = ""

    def matches(self, other: "FileState", use_hash: bool = False) -> bool:
        """
        Returns True if this file and other are considered identical.
        Fast path: mtime + size match (avoids hash computation).
        Slow path: content_hash match (used when mtime is unreliable).
        """
        if use_hash and self.content_hash and other.content_hash:
            return self.content_hash == other.content_hash
        # Fast path: size must match; mtime within 2s tolerance (FAT32 quirk)
        size_match  = self.size_bytes == other.size_bytes
        mtime_match = abs(self.mtime - other.mtime) < 2.0 if (self.mtime and other.mtime) else True
        return size_match and mtime_match


@dataclass
class SyncPlan:
    """
    The immutable output of the Planner. Consumed by the Executor.
    Serializable to JSON for dry-run auditing.
    """
    run_id:       str
    created_at:   float = field(default_factory=time.time)
    operations:   list  = field(default_factory=list)
    dry_run:      bool  = False

    planned_dirs: list  = field(default_factory=list)

    # Metrics
    n_copy:       int   = 0
    n_replace:    int   = 0
    n_delete:     int   = 0
    n_rename:     int   = 0
    n_skip:       int   = 0
    n_tag_update: int   = 0
    n_total:      int   = 0
    estimated_bytes: int = 0

    def add(self, op: Operation) -> None:
        self.operations.append(op)
        counter = f"n_{op.op_type}"
        if hasattr(self, counter):
            setattr(self, counter, getattr(self, counter) + 1)
        self.n_total += 1

    def summary(self) -> str:
        return (
            f"SyncPlan run_id={self.run_id}: "
            f"copy={self.n_copy} replace={self.n_replace} delete={self.n_delete} "
            f"rename={self.n_rename} tag_update={self.n_tag_update} "
            f"skip={self.n_skip} total={self.n_total}"
        )

    def compute_hash(self) -> str:
        """
        P5: Canonical SHA-256 of the plan for dry-run equivalence.
        Serialization is deterministic (sorted keys, stable order).
        Executor verifies this hash before executing to detect plan drift.
        """
        import hashlib
        ops_json = json.dumps(
            [{"op_type": o.op_type, "src": o.src, "dst": o.dst,
              "overwrite": o.overwrite, "atomic": o.atomic, "reason": o.reason}
             for o in self.operations],
            sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(ops_json.encode()).hexdigest()[:16]

    def to_json(self, indent: int = 2) -> str:
        d = {
            "run_id":     self.run_id,
            "created_at": self.created_at,
            "dry_run":    self.dry_run,
            "plan_hash":  self.compute_hash(),
            "summary":    self.summary(),
            "operations": [asdict(op) for op in self.operations],
        }
        return json.dumps(d, indent=indent, ensure_ascii=False)

    def save(self, path: str | Path) -> None:
        """Write plan JSON to disk before execution (dry-run audit)."""
        Path(path).write_text(self.to_json(), encoding="utf-8")


@dataclass
class ExecutionResult:
    """Result of executing one Operation."""
    operation:  Operation
    success:    bool
    duration_s: float
    error_msg:  str  = ""
    remote_size: int = 0

    def to_log_entry(self) -> dict:
        return {
            "timestamp":  time.time(),
            "op_type":    self.operation.op_type,
            "src":        self.operation.src,
            "dst":        self.operation.dst,
            "success":    self.success,
            "duration_s": round(self.duration_s, 3),
            "error":      self.error_msg or None,
        }




# ─── P1: Device Snapshot (MK24 hardening) ────────────────────────────────────

class IncompleteDeviceScanError(RuntimeError):
    """
    Raised when the Planner is asked to plan against an incomplete device snapshot.
    MK24 P1: Planner must never run on partial data — a stale or incomplete ADB
    scan produces a corrupted plan where files are missed or wrongly added.
    """


@dataclass
class DeviceSnapshot:
    """
    Verified scan of the device state. Planner input contract.

    MK24 P1: Planner accepts DeviceSnapshot, NOT raw file lists.
    If complete=False → Planner raises IncompleteDeviceScanError.

    Fields:
        files:     Verified FileState list from a single ADB scan.
        scan_id:   UUID for this scan (correlates with logs).
        timestamp: When the scan completed.
        complete:  True only if scan completed without errors or timeouts.
        source:    "adb_scan" | "null_transport" | "cached" (last = not allowed for planning)
        error_msg: Set when complete=False.
    """
    files:     list = field(default_factory=list)
    scan_id:   str  = field(default_factory=lambda: __import__("uuid").uuid4().hex[:8])
    timestamp: float = field(default_factory=time.time)
    complete:  bool  = True
    source:    str   = "adb_scan"
    error_msg: str   = ""

    def assert_complete(self) -> None:
        """Raise IncompleteDeviceScanError if scan was not complete."""
        if not self.complete:
            raise IncompleteDeviceScanError(
                f"DeviceSnapshot {self.scan_id} is incomplete: {self.error_msg}. "
                f"Re-scan the device before planning."
            )

# ─── Library audit result types ──────────────────────────────────────────────

@dataclass
class AuditFinding:
    """
    A single finding from the library audit pass.
    MK23: tracks misplaced files, wrong tags, splintered albums.
    """
    FindingType = Literal[
        "TAG_MISMATCH",     # ID3 tag doesn't match expected from Spotify
        "FOLDER_MISMATCH",  # file in wrong Artist/Album folder
        "SPLINTER",         # single track in multi-track album folder
        "DUPLICATE",        # same content in multiple locations
        "MISSING_TAG",      # required tag field absent
        "WRONG_TRACK_NUM",  # track number conflicts with Spotify
    ]

    path:         str
    finding_type: str
    detail:       str
    severity:     Literal["critical", "high", "medium", "low"] = "medium"
    suggested_op: Operation | None = None   # the fix, if automatable


@dataclass
class AuditReport:
    """Output of run_library_audit()."""
    run_id:        str
    created_at:    float = field(default_factory=time.time)
    findings:      list  = field(default_factory=list)
    files_scanned: int   = 0

    def add(self, finding: AuditFinding) -> None:
        self.findings.append(finding)

    def by_type(self, finding_type: str) -> list:
        return [f for f in self.findings if f.finding_type == finding_type]

    def summary(self) -> str:
        from collections import Counter
        counts = Counter(f.finding_type for f in self.findings)
        return (
            f"AuditReport: {self.files_scanned} files scanned, "
            f"{len(self.findings)} findings: " +
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
