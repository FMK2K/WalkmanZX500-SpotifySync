"""
audit_benchmark.py — MK39.1 E.1: audit-speed measurement harness
================================================================================

Council MK39.1 directive (Section 4, Priority P0):
    "E — MK8-era audit speed: The operator's ability to quickly re-audit
    their library after a fix is essential for recovery. This target
    directly enables the feedback loop we demanded in Phase II."

Adversarial-review constraint (locked): real measurement against the
operator's 5,000-track library is not possible in this sandbox. This
delivery ships the HARNESS — a measurement framework that can be run by
the operator on their real library to produce baseline + delta numbers
on subsequent optimization sub-sprints.

What the harness measures:
  - Per-file audit overhead (mocked I/O, isolating the in-process logic)
  - p50, p99, total wall time across N files
  - JSON output for trend tracking across runs

What the harness does NOT do this delivery:
  - Optimize the audit path. That's the next sub-sprint, after operator
    runs the harness on their real library and we have a baseline number.
  - Compare to MK8 directly (the MK8 code is archived; comparison requires
    operator to also keep a Git checkout of the MK8 release running).
"""

from __future__ import annotations

import dataclasses
import json
import statistics
import time
from pathlib import Path
from typing import Callable, Iterable, Optional


@dataclasses.dataclass
class BenchmarkSample:
    """Single timing sample."""
    label:        str
    duration_ms:  float
    success:      bool = True
    note:         str = ""


@dataclasses.dataclass
class BenchmarkReport:
    """Aggregated benchmark output."""
    label:           str
    sample_count:    int
    total_ms:        float
    p50_ms:          float
    p99_ms:          float
    mean_ms:         float
    success_count:   int
    failure_count:   int
    samples:         list[BenchmarkSample]

    def to_dict(self) -> dict:
        return {
            "label":         self.label,
            "sample_count":  self.sample_count,
            "total_ms":      round(self.total_ms, 3),
            "p50_ms":        round(self.p50_ms, 3),
            "p99_ms":        round(self.p99_ms, 3),
            "mean_ms":       round(self.mean_ms, 3),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }


def benchmark(
    label: str,
    *,
    work: Callable[[int], None],
    n: int = 100,
) -> BenchmarkReport:
    """Run `work(i)` n times. Each call's wall time is recorded.

    The work callable should take an integer index (the iteration number)
    and perform one audit unit. Exceptions are caught + counted as failures
    so the harness keeps running.
    """
    samples: list[BenchmarkSample] = []
    for i in range(n):
        t0 = time.perf_counter()
        ok = True
        note = ""
        try:
            work(i)
        except Exception as e:
            ok = False
            note = f"{type(e).__name__}: {e}"
        dt = (time.perf_counter() - t0) * 1000.0
        samples.append(BenchmarkSample(
            label=f"{label}#{i}", duration_ms=dt, success=ok, note=note,
        ))

    durations = [s.duration_ms for s in samples]
    durations_sorted = sorted(durations)
    return BenchmarkReport(
        label=label,
        sample_count=n,
        total_ms=sum(durations),
        p50_ms=statistics.median(durations) if durations else 0.0,
        p99_ms=durations_sorted[int(0.99 * (n - 1))] if n > 0 else 0.0,
        mean_ms=statistics.mean(durations) if durations else 0.0,
        success_count=sum(1 for s in samples if s.success),
        failure_count=sum(1 for s in samples if not s.success),
        samples=samples,
    )


def write_report(report: BenchmarkReport, path: str) -> None:
    """Persist a report to JSON for trend tracking."""
    payload = report.to_dict()
    payload["per_sample_ms"] = [s.duration_ms for s in report.samples]
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def synthetic_audit_call(i: int) -> None:
    """Mock per-file audit — fixed-cost work to anchor measurement.

    Operator runs a real audit harness; this is the BASELINE harness for
    a sandbox sanity check (the real one substitutes a real audit call).
    """
    # Tiny computation to ensure the harness measures something nonzero
    _ = sum(j * j for j in range(50))


def run_baseline_demo(n: int = 100) -> BenchmarkReport:
    """Self-test entry point for the harness. Real operator invokes
    benchmark() with their actual audit unit."""
    return benchmark("synthetic_audit_baseline", work=synthetic_audit_call, n=n)


__all__ = [
    "BenchmarkSample", "BenchmarkReport",
    "benchmark", "write_report",
    "synthetic_audit_call", "run_baseline_demo",
]


if __name__ == "__main__":
    # Operator can run: python3 audit_benchmark.py [N]
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    rpt = run_baseline_demo(n=n)
    print(json.dumps(rpt.to_dict(), indent=2))
