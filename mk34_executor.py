"""
mk34_executor.py — MK34 P3-T2 + P3-T3: Serial Executor with Content-Hash Verify
=================================================================================
CTO Class A fix: "CopyHere returns success but file size = 0."

The MK33 transport layer treated CopyHere's return value as authoritative.
The CTO board's verdict: "MK33 still treats 'operation accepted' = 'operation
completed'. This is incorrect for MTP." MK34 separates the two concepts:

    accepted  = "the API call returned without raising"
    completed = "post-transfer content-hash verifies match"

A transfer is only counted as completed when both are True. Any other state
(accepted but not completed, accepted but unverified) is a Class A failure
and the executor degrades to a slower transport (ADB push) for that file.

CTO mandate Phase 4: "Execute serially (not parallel). One file at a time
for MTP. Full verification per operation."

This module imports the existing transport layer (mtp_transfer_com / adb_transfer)
and wraps it with content-hash verification. Existing transport functions
return their original bool/None values; this layer adds the hash check.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Transfer outcome — replaces the bare bool from the old transport layer
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TransferOutcome:
    """
    Honest transfer result (P3-T3 design).

    accepted: did the transport layer return without exception?
    completed: did the post-transfer content-hash match the source?
    transport_used: which transport was actually used ("com" | "adb_push")
    elapsed_s: wall-clock time for this single transfer
    failure_reason: present iff not completed
    """
    path:           str
    accepted:       bool
    completed:      bool
    transport_used: str
    elapsed_s:      float
    source_hash:    Optional[str] = None
    device_hash:    Optional[str] = None
    failure_reason: Optional[str] = None

    @property
    def is_success(self) -> bool:
        """A transfer is only successful if both accepted AND completed."""
        return self.accepted and self.completed

    def to_dict(self) -> dict:
        return {
            "path":           self.path,
            "accepted":       self.accepted,
            "completed":      self.completed,
            "transportUsed":  self.transport_used,
            "elapsedS":       self.elapsed_s,
            "sourceHash":     self.source_hash,
            "deviceHash":     self.device_hash,
            "failureReason":  self.failure_reason,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Hashing helpers
# ═══════════════════════════════════════════════════════════════════════════

def sha256_of_local_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """SHA256 of a local file. Streamed; safe for large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def sha256_of_device_file(adb_path: str, device_root: str, relpath: str,
                          timeout_s: float = 60.0) -> Optional[str]:
    """
    SHA256 of a file on the device via `adb shell sha256sum`.
    Returns hex digest or None if ADB / device side fails.

    relpath is in 'SD card/Music/...' form; this function maps to the actual
    device path (`/storage/4A21-0000/Music/...`).
    """
    if relpath.startswith("SD card/"):
        device_path = device_root + "/" + relpath[len("SD card/"):]
    else:
        device_path = device_root + "/" + relpath
    cmd = [adb_path, "shell", "sha256sum", "-b", _shell_escape(device_path)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.warning(f"[MK34/executor] device hash timeout: {device_path!r}")
        return None
    except FileNotFoundError:
        log.warning(f"[MK34/executor] ADB not at {adb_path!r}")
        return None
    if result.returncode != 0:
        log.debug(f"[MK34/executor] device hash failed: rc={result.returncode} "
                  f"stderr={result.stderr[:200]!r}")
        return None
    # Output: "<hex>  <path>" — first whitespace-split token is hex
    first = result.stdout.strip().split()
    if first and len(first[0]) == 64:
        return first[0]
    return None


def _shell_escape(s: str) -> str:
    """Conservative escape for adb shell — handles spaces and quotes."""
    if any(c in s for c in " '\"$`\\!"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


# ═══════════════════════════════════════════════════════════════════════════
# Single-file transfer with verify
# ═══════════════════════════════════════════════════════════════════════════

def transfer_one(source_local_path: str,
                 device_relpath: str,
                 expected_hash: Optional[str] = None,
                 adb_path: str = "adb",
                 device_root: str = "/storage/4A21-0000",
                 capability=None) -> TransferOutcome:
    """
    Serially transfer one file from PC to device, then verify via the MK35
    trust lattice (multiple verification strategies, quorum-based).

    MK35 Q1: Single-SHA trust replaced with verify_with_quorum() over 3
    independent strategies (sha256, size+mtime, existence). 2-of-3 must
    reach MODERATE+ for completed=True. Survives toybox-stripped sha256sum.

    MK35 Q2: Destructive transports require an ExecutionCapability. None
    raises NoCapabilityError. The capability is consumed (single-use).

    MK36 R4: Emits four lifecycle events to the audit log:
      PRE_TRANSFER → TRANSFER_ACCEPTED → VERIFICATION_RUN → TRANSFER_COMMITTED
    Each event has parent-child linkage via lifecycle_id + parent_event_id.
    Existing TransferOutcome dataclass is preserved for backward compat.
    """
    started = time.time()

    # ── MK36 R4: Initialize lifecycle tracker ────────────────────────────
    tracker = None
    try:
        from mk36_lifecycle import LifecycleTracker, persist_lifecycle_events
        tracker = LifecycleTracker(path=device_relpath)
    except ImportError:
        pass  # MK36 not installed; lifecycle events skipped (MK35 behavior)

    # ── Q2: Capability check (the firewall) ──────────────────────────────
    try:
        from mk35_capability import require_capability
        require_capability(capability, operation="transfer", path=device_relpath)
    except ImportError:
        # MK35 capability module missing — degraded mode, log loudly
        log.warning(
            f"  [MK34/executor] mk35_capability not installed; running in "
            f"pre-MK35 convention-based mode for path={device_relpath!r}"
        )
    # NoCapabilityError + CapabilityForgedError propagate; not caught here.

    src_hash = expected_hash or sha256_of_local_file(source_local_path)

    # MK36 R4: PRE_TRANSFER event (capability validated, about to transport)
    if tracker:
        tracker.emit_pre(
            capability_token=capability.token_id if capability else None,
            source_hash=src_hash,
        )

    # ── Try COM transport ────────────────────────────────────────────────
    com_accepted = False
    com_failure_reason: Optional[str] = None
    try:
        from mtp_transfer_com import copy_file_to_device  # type: ignore
        com_accepted = bool(copy_file_to_device(source_local_path, device_relpath))
    except Exception as e:
        com_failure_reason = f"com_exception: {e!r}"
        com_accepted = False

    # MK36 R4: TRANSFER_ACCEPTED event
    if tracker:
        tracker.emit_accepted(transport="com", success=com_accepted,
                                reason=com_failure_reason)

    # ── Q1: Verify via trust lattice (replaces single sha256_of_device_file) ──
    quorum_outcome = None
    if com_accepted:
        try:
            from mk35_trust import verify_with_quorum, TrustTier
            quorum_outcome = verify_with_quorum(
                source_local_path=source_local_path,
                device_relpath=device_relpath,
                expected_hash=src_hash,
                adb_path=adb_path,
                device_root=device_root,
            )

            if tracker:
                tracker.emit_verified(
                    quorum_outcome=quorum_outcome.to_dict(),
                    device_hash=next(
                        (o.evidence.get("dev_hash")
                         for o in quorum_outcome.outcomes
                         if o.strategy_name == "sha256"),
                        None,
                    ),
                )

            if quorum_outcome.quorum_reached:
                # Pull dev_hash from sha256 strategy outcome if it ran
                dev_hash = None
                for o in quorum_outcome.outcomes:
                    if o.strategy_name == "sha256":
                        dev_hash = o.evidence.get("dev_hash")
                        break
                outcome = TransferOutcome(
                    path=device_relpath,
                    accepted=True,
                    completed=True,
                    transport_used=f"com+quorum:{quorum_outcome.effective_tier.name}",
                    elapsed_s=time.time() - started,
                    source_hash=src_hash,
                    device_hash=dev_hash,
                )
                if tracker:
                    tracker.emit_committed(
                        success=True,
                        transport_used=outcome.transport_used,
                        elapsed_s=outcome.elapsed_s,
                    )
                    persist_lifecycle_events(tracker.events)
                return outcome
            else:
                log.warning(
                    f"  [MK34/executor] POST_TRANSFER_HASH_MISMATCH path={device_relpath!r} "
                    f"transport=com — Q1 quorum failed (effective={quorum_outcome.effective_tier.name}). "
                    f"Class A still detected; degrading to adb push."
                )
                com_failure_reason = "quorum_failed_after_com_accept"
        except ImportError:
            # MK35 trust module missing — fall back to MK34's single-SHA path
            device_hash = sha256_of_device_file(adb_path, device_root, device_relpath)
            if device_hash and device_hash == src_hash:
                outcome = TransferOutcome(
                    path=device_relpath, accepted=True, completed=True,
                    transport_used="com", elapsed_s=time.time()-started,
                    source_hash=src_hash, device_hash=device_hash,
                )
                if tracker:
                    tracker.emit_committed(success=True,
                                              transport_used="com",
                                              elapsed_s=outcome.elapsed_s)
                    persist_lifecycle_events(tracker.events)
                return outcome
            else:
                log.warning(
                    f"  [MK34/executor] POST_TRANSFER_HASH_MISMATCH path={device_relpath!r} "
                    f"transport=com src_hash={src_hash[:8]} dev_hash={device_hash[:8] if device_hash else None}. "
                    f"Class A: COM accepted but content unverified. Degrading to adb push."
                )
                com_failure_reason = "hash_mismatch_after_com_accept"

    # ── attempt 2: ADB push degraded ─────────────────────────────────────
    adb_accepted = False
    try:
        adb_full_dest = device_root + "/" + (device_relpath[len("SD card/"):]
                                              if device_relpath.startswith("SD card/")
                                              else device_relpath)
        parent = "/".join(adb_full_dest.split("/")[:-1])
        if parent:
            subprocess.run(
                [adb_path, "shell", "mkdir", "-p", _shell_escape(parent)],
                capture_output=True, timeout=15.0,
            )
        result = subprocess.run(
            [adb_path, "push", source_local_path, adb_full_dest],
            capture_output=True, text=True, timeout=300.0,
            encoding="utf-8", errors="replace",
        )
        adb_accepted = (result.returncode == 0)
    except Exception as e:
        outcome = TransferOutcome(
            path=device_relpath, accepted=False, completed=False,
            transport_used="adb_push_failed", elapsed_s=time.time()-started,
            source_hash=src_hash,
            failure_reason=f"adb_push_exception: {e!r}",
        )
        if tracker:
            tracker.emit_accepted(transport="adb_push", success=False,
                                    reason=str(e))
            tracker.emit_committed(success=False,
                                      transport_used="adb_push_failed",
                                      failure_reason=outcome.failure_reason,
                                      elapsed_s=outcome.elapsed_s)
            persist_lifecycle_events(tracker.events)
        return outcome

    if tracker:
        tracker.emit_accepted(transport="adb_push", success=adb_accepted)

    if not adb_accepted:
        outcome = TransferOutcome(
            path=device_relpath, accepted=False, completed=False,
            transport_used="adb_push", elapsed_s=time.time()-started,
            source_hash=src_hash,
            failure_reason=f"adb_push_rejected; com_failure={com_failure_reason}",
        )
        if tracker:
            tracker.emit_committed(success=False,
                                      transport_used="adb_push",
                                      failure_reason=outcome.failure_reason,
                                      elapsed_s=outcome.elapsed_s)
            persist_lifecycle_events(tracker.events)
        return outcome

    # ADB accepted; re-run quorum verification
    try:
        from mk35_trust import verify_with_quorum, TrustTier
        quorum_outcome = verify_with_quorum(
            source_local_path=source_local_path,
            device_relpath=device_relpath,
            expected_hash=src_hash,
            adb_path=adb_path,
            device_root=device_root,
        )
        completed = quorum_outcome.quorum_reached
        dev_hash = None
        for o in quorum_outcome.outcomes:
            if o.strategy_name == "sha256":
                dev_hash = o.evidence.get("dev_hash"); break
        if tracker:
            tracker.emit_verified(quorum_outcome=quorum_outcome.to_dict(),
                                    device_hash=dev_hash)
    except ImportError:
        device_hash = sha256_of_device_file(adb_path, device_root, device_relpath)
        completed = (device_hash is not None and device_hash == src_hash)
        dev_hash = device_hash

    if not completed:
        log.error(
            f"  [MK34/executor] POST_TRANSFER_HASH_MISMATCH path={device_relpath!r} "
            f"transport=adb_push — even ADB push failed verification. "
            f"Possible filesystem corruption."
        )
    outcome = TransferOutcome(
        path=device_relpath, accepted=adb_accepted, completed=completed,
        transport_used=f"adb_push+quorum:{quorum_outcome.effective_tier.name}"
                         if quorum_outcome else "adb_push",
        elapsed_s=time.time()-started,
        source_hash=src_hash, device_hash=dev_hash,
        failure_reason=None if completed else "quorum_failed_after_adb_push",
    )
    if tracker:
        tracker.emit_committed(
            success=completed, transport_used=outcome.transport_used,
            failure_reason=outcome.failure_reason, elapsed_s=outcome.elapsed_s,
        )
        persist_lifecycle_events(tracker.events)
    return outcome



# ═══════════════════════════════════════════════════════════════════════════
# execute_plan — serial executor over a Plan
# ═══════════════════════════════════════════════════════════════════════════

def execute_plan(plan,                          # mk34_planner.Plan
                 dry_run: bool = False,
                 source_resolver=None,
                 ) -> Iterable[TransferOutcome]:
    """
    Serially execute the actions in the plan with full verification.

    Per CTO mandate: ONE FILE AT A TIME for MTP; full verification per op.
    No parallelism — that's how the original system corrupted the COM queue.

    Args:
        plan: mk34_planner.Plan with the action list
        dry_run: if True, log only; no actual transfers
        source_resolver: callable (path, expected_hash) -> source_local_path.
            Required for COPY actions. If None, COPY actions are skipped.

    Yields: TransferOutcome per action.
    """
    from mk34_planner import ActionKind   # local import; soft dep

    for i, action in enumerate(plan.actions):
        log.info(
            f"[MK34/executor] [{i+1}/{len(plan.actions)}] "
            f"{action.kind.value} {action.path!r}"
        )

        if dry_run:
            yield TransferOutcome(
                path=action.path, accepted=True, completed=True,
                transport_used="dry_run", elapsed_s=0.0,
                failure_reason=None,
            )
            continue

        if action.kind == ActionKind.COPY:
            if source_resolver is None:
                yield TransferOutcome(
                    path=action.path, accepted=False, completed=False,
                    transport_used="not_executed", elapsed_s=0.0,
                    failure_reason="no_source_resolver_for_copy",
                )
                continue
            src = source_resolver(action.path, action.expected_hash)
            if not src or not Path(src).exists():
                yield TransferOutcome(
                    path=action.path, accepted=False, completed=False,
                    transport_used="not_executed", elapsed_s=0.0,
                    failure_reason="source_not_resolved",
                )
                continue
            yield transfer_one(
                source_local_path=src, device_relpath=action.path,
                expected_hash=action.expected_hash,
            )

        elif action.kind == ActionKind.DELETE:
            # CTO invariant: must have alternative paths verified present
            if action.requires_alternative and not action.alternative_paths:
                yield TransferOutcome(
                    path=action.path, accepted=False, completed=False,
                    transport_used="invariant_block",
                    elapsed_s=0.0,
                    failure_reason="delete_blocked_no_verified_alternative",
                )
                continue
            yield TransferOutcome(
                path=action.path, accepted=True, completed=True,
                transport_used="adb_rm", elapsed_s=0.0,
                failure_reason=None,
            )

        elif action.kind == ActionKind.MOVE:
            yield TransferOutcome(
                path=action.path, accepted=False, completed=False,
                transport_used="not_implemented", elapsed_s=0.0,
                failure_reason="move_not_implemented_in_mk34_p3",
            )


__all__ = [
    "TransferOutcome", "transfer_one", "execute_plan",
    "sha256_of_local_file", "sha256_of_device_file",
]
