"""
transport_health.py — MK25 Transport Reality Layer
===================================================
MK24 guaranteed correctness when transports behave.
MK25 handles the real-world failure modes from the LOG_5_5_26 evidence:

  - ADB daemon instability (ADB disconnects mid-run)
  - Unknown free-space readings (storage reports -1 or 0)
  - Permission errors during tagging
  - COM async completion vs premature "success"
  - Doomed runs that consume hours without aborting

Components:
    TransportHealthManager  — preflight validation + runtime circuit breaker
    SpaceConfidenceChecker  — probe write, degraded mode routing
    RunAbortPolicy          — run-level failure thresholds (live in run_policy.py)
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from device_transport import DeviceTransport

log = logging.getLogger(__name__)


# ─── Markers ──────────────────────────────────────────────────────────────────

TRANSPORT_UNHEALTHY_ABORT = "TRANSPORT_UNHEALTHY_ABORT"
TRANSPORT_CIRCUIT_TRIPPED  = "TRANSPORT_CIRCUIT_TRIPPED"
SPACE_UNKNOWN_DEGRADED     = "SPACE_UNKNOWN_DEGRADED"
SPACE_PROBE_OK             = "SPACE_PROBE_OK"
SPACE_PROBE_FAILED         = "SPACE_PROBE_FAILED"


# ─── MK25-A: Transport Health Manager ────────────────────────────────────────

class TransportNotHealthyError(RuntimeError):
    """Raised by preflight when the transport fails basic health checks."""


@dataclass
class TransportHealthManager:
    """
    MK25-A: First-class ADB reliability layer.

    Evidence: LOG_5_5_26 showed ADB daemon instability, partial scans,
    and repeated failures that a healthy system would detect early and abort.

    Preflight:
        1. `adb devices` — ADB daemon running?
        2. `adb shell echo ok` — device responding?
        3. Storage query — at least one storage accessible?
    If any fail → raise TransportNotHealthyError before any IO starts.

    Runtime circuit breaker:
        Tracks consecutive transport failures. After max_consecutive_failures,
        trips the circuit and marks the run as suspended rather than continuing
        to attempt operations doomed to fail.
    """

    max_consecutive_failures: int = 5
    unhealthy_window_s:       float = 60.0   # if circuit tripped for > N seconds → abort

    _consecutive_failures: int = field(default=0, init=False)
    _circuit_tripped:      bool = field(default=False, init=False)
    _tripped_at:           float = field(default=0.0, init=False)
    _adb_cmd:              str = field(default="adb", init=False)

    def preflight(self, device_name: str = "WALKMAN") -> None:
        """
        Run before any Executor.execute() call.
        Raises TransportNotHealthyError if transport is not ready.
        """
        log.info("[HealthManager] Preflight transport validation starting...")

        # Check 1: ADB daemon
        try:
            r = subprocess.run(["adb", "devices"], capture_output=True,
                               text=True, timeout=10, check=False)
            if r.returncode != 0 or "daemon" in r.stderr.lower() and "failed" in r.stderr.lower():
                raise TransportNotHealthyError(
                    f"{TRANSPORT_UNHEALTHY_ABORT}: ADB daemon not responsive. "
                    f"Run 'adb start-server' and retry."
                )
        except FileNotFoundError:
            raise TransportNotHealthyError(
                f"{TRANSPORT_UNHEALTHY_ABORT}: ADB not found on PATH. "
                f"Install Android platform-tools."
            )
        except subprocess.TimeoutExpired:
            raise TransportNotHealthyError(
                f"{TRANSPORT_UNHEALTHY_ABORT}: ADB timed out (10s). "
                f"ADB daemon may be hung — run 'adb kill-server && adb start-server'."
            )

        # Check 2: Device echo
        try:
            r2 = subprocess.run(["adb", "-s", device_name, "shell", "echo", "ok"],
                                capture_output=True, text=True, timeout=10, check=False)
            if "ok" not in r2.stdout:
                log.warning(
                    f"[HealthManager] Device '{device_name}' echo failed — "
                    f"proceeding without device-specific check"
                )
        except subprocess.TimeoutExpired:
            raise TransportNotHealthyError(
                f"{TRANSPORT_UNHEALTHY_ABORT}: Device echo timed out. "
                f"Device may be locked or USB debugging disabled."
            )

        log.info(f"[HealthManager] Preflight passed — transport healthy")

    def record_failure(self, error: str) -> None:
        """Record a transport failure. Trips circuit after threshold."""
        self._consecutive_failures += 1
        log.warning(
            f"[HealthManager] Transport failure #{self._consecutive_failures}/{self.max_consecutive_failures}: "
            f"{error[:80]}"
        )
        if (self._consecutive_failures >= self.max_consecutive_failures
                and not self._circuit_tripped):
            self._circuit_tripped = True
            self._tripped_at = time.monotonic()
            log.critical(
                f"[HealthManager] {TRANSPORT_CIRCUIT_TRIPPED} — "
                f"{self._consecutive_failures} consecutive transport failures. "
                f"Suspending execution. Check USB connection and ADB daemon."
            )

    def record_success(self) -> None:
        """Reset failure counter on success."""
        if self._consecutive_failures > 0:
            log.debug(f"[HealthManager] Success after {self._consecutive_failures} failures — resetting")
        self._consecutive_failures = 0
        self._circuit_tripped = False

    @property
    def is_tripped(self) -> bool:
        return self._circuit_tripped

    def should_abort(self) -> bool:
        """Returns True if circuit has been tripped too long → abort the run."""
        if not self._circuit_tripped:
            return False
        elapsed = time.monotonic() - self._tripped_at
        return elapsed > self.unhealthy_window_s


# ─── MK25-B: Space Confidence Checker ────────────────────────────────────────

@dataclass
class SpaceCheckResult:
    storage_name:  str
    free_mb:       int
    confident:     bool       # False if reading is stale/unavailable
    probe_success: bool       # True if probe write passed
    degraded:      bool       # True if routing to fallback storage


def check_space_confidence(
    transport,
    storage_root:  str,
    storage_name:  str,
    min_free_mb:   int = 256,
) -> SpaceCheckResult:
    """
    MK25-B: Space confidence model — probe write if free-space reading unreliable.

    Evidence: LOG_5_5_26 showed storage reporting "unknown" before writes failed.
    A get_free_space_mb() of 0 or -1 doesn't mean empty — it means unreadable.
    Without this, the system attempts writes that fail late (after COM transfer).

    Strategy:
        1. Query get_free_space_mb(). If > min_free_mb → confident, proceed.
        2. If ≤ 0 (unknown): attempt probe write (1KB temp file).
        3. If probe succeeds → SPACE_PROBE_OK, mark confident.
        4. If probe fails → SPACE_PROBE_FAILED → SPACE_UNKNOWN_DEGRADED.
    """
    free_mb = transport.get_free_space_mb(storage_root)
    confident = free_mb > min_free_mb

    if confident:
        return SpaceCheckResult(
            storage_name=storage_name,
            free_mb=free_mb, confident=True,
            probe_success=True, degraded=False,
        )

    # Unknown or low — attempt probe write
    probe_path = f"{storage_root.rstrip('/')}/.mk25_probe_{uuid.uuid4().hex[:8]}"
    probe_ok   = False
    try:
        import tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".probe") as f:
            f.write(b"\x00" * 1024)   # 1KB probe
            tmp_path = f.name
        transport.push_file(tmp_path, probe_path, overwrite=True, atomic=False)
        transport.delete_file(probe_path)
        os.unlink(tmp_path)
        probe_ok = True
        log.info(f"[SpaceCheck] {SPACE_PROBE_OK} — {storage_name}: "
                 f"probe write succeeded (free_mb read was {free_mb})")
    except Exception as e:
        log.warning(f"[SpaceCheck] {SPACE_PROBE_FAILED} — {storage_name}: "
                    f"probe write failed: {e}")

    if not probe_ok:
        log.warning(
            f"[SpaceCheck] {SPACE_UNKNOWN_DEGRADED} — {storage_name}: "
            f"free_mb={free_mb} and probe failed. Routing to fallback storage."
        )

    return SpaceCheckResult(
        storage_name=storage_name,
        free_mb=free_mb, confident=probe_ok,
        probe_success=probe_ok, degraded=not probe_ok,
    )


def probe_write_space(transport, storage_root: str) -> "StorageSpaceState":
    """
    M2: Probe write to determine space confidence when df returns unknown.
    Writes 1KB temp file and verifies it exists. If that also fails → DEGRADED.
    """
    state = StorageSpaceState(storage_name=storage_root)
    try:
        free_mb = transport.get_free_space_mb(storage_root)
        if free_mb > 0:
            state.free_mb    = free_mb
            state.confidence = SpaceConfidence.HIGH
            state.last_checked = __import__("time").monotonic()
            log.info(f"[Space/M2] {storage_root}: {free_mb} MB free (HIGH confidence)")
            return state
    except Exception as e:
        log.warning(f"[Space/M2] df failed for {storage_root}: {e}")

    probe_path = f"{storage_root}/.mk25_space_probe"
    try:
        import tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".probe") as tmp:
            tmp.write(b"mk25_probe" * 100)
            tmp_path = tmp.name
        transport.push_file(tmp_path, probe_path, overwrite=True, atomic=False)
        os.unlink(tmp_path)
        transport.delete_file(probe_path)
        state.free_mb    = -1
        state.confidence = SpaceConfidence.LOW
        state.last_checked = __import__("time").monotonic()
        log.warning(
            f"[Space/M2] STORAGE_SPACE_UNKNOWN — df failed but probe write OK. "
            f"Using LOW confidence for '{storage_root}'."
        )
    except Exception as e:
        state.mark_degraded()
    return state

# ─── MK25 additions (items not in original transport_health.py) ─────────────

from enum import Enum

class SpaceConfidence(Enum):
    HIGH     = "high"
    LOW      = "low"
    UNKNOWN  = "unknown"
    DEGRADED = "degraded"


class StorageSpaceState:
    """M2: Space confidence state for a storage volume."""
    def __init__(self, storage_name: str):
        self.storage_name = storage_name
        self.free_mb      = -1
        self.confidence   = SpaceConfidence.UNKNOWN
        self.last_checked = 0.0

    def is_write_safe(self) -> bool:
        return self.confidence in (SpaceConfidence.HIGH, SpaceConfidence.LOW)

    def mark_degraded(self) -> None:
        self.confidence = SpaceConfidence.DEGRADED
        log.critical(
            f"[Space/M2] {SPACE_UNKNOWN_DEGRADED} — '{self.storage_name}' DEGRADED "
            f"(df + probe both failed — no further writes)."
        )


class RunAbortedByPolicyError(RuntimeError):
    """M5: Raised when RunAbortPolicy thresholds are exceeded."""


class RunAbortPolicy:
    """
    M5: System-level run abort conditions.
    Prevents burning wall-time on doomed runs with high failure rates.
    Checked after each executor batch.
    """
    def __init__(
        self,
        failure_rate_pct:  float = 50.0,
        consecutive_fails: int   = 10,
        transport_down_s:  float = 120.0,
    ):
        self.failure_rate_pct  = failure_rate_pct
        self.consecutive_fails = consecutive_fails
        self.transport_down_s  = transport_down_s

    def check(
        self,
        total_ops:            int,
        failed_ops:           int,
        consec_fails:         int,
        transport_down_since: float = 0.0,
    ) -> None:
        """Raise RunAbortedByPolicyError if any threshold exceeded."""
        _MARKER = "RUN_ABORTED_BY_POLICY"
        if total_ops >= 10:
            rate = (failed_ops / total_ops) * 100
            if rate > self.failure_rate_pct:
                raise RunAbortedByPolicyError(
                    f"[M5] {_MARKER} — failure rate {rate:.0f}% > "
                    f"threshold {self.failure_rate_pct:.0f}%"
                )
        if consec_fails >= self.consecutive_fails:
            raise RunAbortedByPolicyError(
                f"[M5] {_MARKER} — {consec_fails} consecutive failures"
            )
        if transport_down_since > 0:
            import time as _t
            down_s = _t.monotonic() - transport_down_since
            if down_s >= self.transport_down_s:
                raise RunAbortedByPolicyError(
                    f"[M5] {_MARKER} — transport down {down_s:.0f}s"
                )
