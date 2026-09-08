"""
run_policy.py — MK25 Run-Level Abort Policy
============================================
Prevents doomed runs from consuming overnight time.

Evidence: LOG_5_5_26 ran for 11+ hours in a state where recovery was
impossible (99% internal storage, 743 dialogs, infinite retry loop).
A run-level policy would have detected this within the first batch.

RunAbortPolicy is checked by the Executor after each batch. If any
threshold is exceeded → journal suspended, emit RUN_ABORTED_POLICY,
stop all IO immediately.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

RUN_ABORTED_POLICY = "RUN_ABORTED_POLICY"


class RunAbortError(RuntimeError):
    """Raised when RunAbortPolicy triggers. Caught by Executor to suspend run."""


@dataclass
class RunAbortPolicy:
    """
    MK25-E: Run-level failure thresholds.

    Checked after every batch. Raises RunAbortError if any limit breached.

    Knobs:
        max_failure_pct:         abort if > X% of completed ops failed
        max_consecutive_failures: abort if N ops fail in a row
        transport_unhealthy_s:   abort if transport circuit tripped for > N seconds
        min_ops_before_pct_check: don't apply pct rule until at least N ops run
                                   (prevents abort on first op of a 1000-op run)
    """
    max_failure_pct:             float = 20.0   # abort if >20% fail
    max_consecutive_failures:    int   = 10     # abort if 10 in a row
    transport_unhealthy_s:       float = 120.0  # abort if transport dead >2min
    min_ops_before_pct_check:    int   = 20     # wait for meaningful sample

    _total:        int   = field(default=0,   init=False)
    _failed:       int   = field(default=0,   init=False)
    _consecutive:  int   = field(default=0,   init=False)
    _started_at:   float = field(default_factory=time.monotonic, init=False)

    def record_op(self, success: bool) -> None:
        self._total  += 1
        if success:
            self._consecutive = 0
        else:
            self._failed      += 1
            self._consecutive += 1

    def check(self, health_manager=None) -> None:
        """
        Check all abort conditions. Raises RunAbortError if any triggered.
        Call after each batch from the Executor.
        """
        # Transport circuit breaker timeout
        if health_manager and health_manager.should_abort():
            raise RunAbortError(
                f"{RUN_ABORTED_POLICY}: Transport circuit breaker tripped for "
                f">{health_manager.unhealthy_window_s:.0f}s. "
                f"USB connection or ADB daemon is unresponsive. "
                f"Run is suspended — reconnect and resume from journal."
            )

        # Consecutive failure threshold
        if self._consecutive >= self.max_consecutive_failures:
            raise RunAbortError(
                f"{RUN_ABORTED_POLICY}: {self._consecutive} consecutive operation failures. "
                f"Transport or device may be in an unrecoverable state. "
                f"total={self._total} failed={self._failed}"
            )

        # Percentage failure threshold (only after enough ops for meaningful signal)
        if self._total >= self.min_ops_before_pct_check and self._total > 0:
            pct = (self._failed / self._total) * 100
            if pct > self.max_failure_pct:
                raise RunAbortError(
                    f"{RUN_ABORTED_POLICY}: {pct:.1f}% failure rate exceeds "
                    f"threshold of {self.max_failure_pct}%. "
                    f"failed={self._failed}/{self._total}. "
                    f"Check free space, ADB stability, and device health."
                )

    @property
    def stats(self) -> dict:
        return {
            "total":       self._total,
            "failed":      self._failed,
            "consecutive": self._consecutive,
            "failure_pct": round((self._failed / self._total * 100) if self._total else 0, 1),
            "elapsed_s":   round(time.monotonic() - self._started_at, 1),
        }
