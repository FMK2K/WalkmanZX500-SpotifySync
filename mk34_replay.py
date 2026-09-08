#!/usr/bin/env python3
"""
mk34_replay.py — MK34 P5-T2: Deterministic Replay from Audit Log
==================================================================
CTO Phase 5 Verify: "all tracks resolved, no orphaned transfers, no zero-byte
files, device convergence achieved." For replay-based forensics.

Reads the append-only .mk34_audit.jsonl that the engine writes during normal
operation and reconstructs:
    - Which decisions fired in which order
    - Which conflicts were detected
    - Whether the engine's output for a given run-id was deterministic

Usage:
    python mk34_replay.py                          # show recent run summary
    python mk34_replay.py --run-id <RUN_ID>        # full trace for a run
    python mk34_replay.py --runs                   # list all runs in log
    python mk34_replay.py --decisions-for <RUN>    # decisions only
    python mk34_replay.py --drift-check            # rerun and compare
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


# ANSI colors (no external dep)
class _C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
    M = "\033[35m"; C = "\033[36m"; D = "\033[2m"; BLD = "\033[1m"; END = "\033[0m"


DEFAULT_AUDIT_PATH = Path(".mk34_audit.jsonl")


# ───────────────────────────────────────────────────────────────────────────

def load_events(path: Path) -> List[dict]:
    """Read and parse the JSONL audit log. Returns all event dicts."""
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"{_C.Y}⚠ Line {line_no}: parse error — {e}{_C.END}",
                      file=sys.stderr)
    return events


def group_by_run(events: List[dict]) -> Dict[str, List[dict]]:
    runs = defaultdict(list)
    for e in events:
        runs[e.get("runId", "UNKNOWN")].append(e)
    return runs


# ───────────────────────────────────────────────────────────────────────────

def cmd_runs(events: List[dict]) -> None:
    """List all run-ids in the audit log."""
    runs = group_by_run(events)
    if not runs:
        print(f"{_C.D}No audit events found.{_C.END}")
        return
    print(f"\n{_C.BLD}══ Runs in audit log ══{_C.END}\n")
    print(f"{'Run ID':<40} {'Events':>7} {'Started':<22}")
    print("─" * 75)
    for run_id, events in sorted(runs.items(),
                                  key=lambda kv: kv[1][0].get("timestamp", 0),
                                  reverse=True):
        started_ts = events[0].get("timestamp", 0)
        from datetime import datetime
        started = datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{run_id:<40} {len(events):>7} {started:<22}")
    print()


def cmd_run_detail(events: List[dict], run_id: str) -> None:
    """Show full trace of one run."""
    matches = [e for e in events if e.get("runId") == run_id]
    if not matches:
        print(f"{_C.R}No events found for run-id {run_id!r}.{_C.END}")
        # Suggest close matches
        runs = group_by_run(events)
        candidates = [r for r in runs if run_id in r]
        if candidates:
            print(f"\n{_C.D}Did you mean one of:{_C.END}")
            for c in candidates[:5]:
                print(f"  {c}")
        return

    print(f"\n{_C.BLD}══ Run trace: {run_id} ══{_C.END}\n")
    counts = defaultdict(int)
    for e in matches:
        counts[e.get("type", "UNKNOWN")] += 1

    print(f"{_C.BLD}Event summary:{_C.END}")
    for k in ["INGEST", "NORMALIZE", "CONFLICT_DETECTED", "DECISION_MADE", "WRITEBACK"]:
        if counts[k]:
            print(f"  {_C.C}{k:<20}{_C.END} {counts[k]:>5}")
    print()

    # Show writeback summary if available
    writebacks = [e for e in matches if e.get("type") == "WRITEBACK"]
    if writebacks:
        print(f"{_C.BLD}Final summary:{_C.END}")
        summary = writebacks[-1].get("payload", {}).get("summary", {})
        for k, v in summary.items():
            print(f"  {k:<20} {v}")
        print()


def cmd_decisions(events: List[dict], run_id: str) -> None:
    """Show all DECISION_MADE events for a run."""
    decisions = [
        e for e in events
        if e.get("runId") == run_id and e.get("type") == "DECISION_MADE"
    ]
    if not decisions:
        print(f"{_C.R}No decisions for run-id {run_id!r}.{_C.END}")
        return

    print(f"\n{_C.BLD}══ Decisions for run {run_id} ({len(decisions)} total) ══{_C.END}\n")
    for d in decisions:
        p = d.get("payload", {})
        print(
            f"  {_C.G}{p.get('fieldPath', '?'):<35}{_C.END} "
            f"{_C.C}{p.get('rule', '?'):<35}{_C.END} "
            f"→ {p.get('selectedValue')!r}"
        )
        if p.get("rejected_count"):
            print(f"      {_C.D}rejected: {p['rejected_count']}{_C.END}")
    print()


def cmd_drift_check(events: List[dict]) -> None:
    """
    For the most recent run, indicate whether replay would produce the same
    decisions. Drift detection is heuristic: same input + same rules + same
    priority order → same output.

    A real drift check would require persisting the input bundle alongside
    the audit log; that's MK35+ scope. This command prints what the audit
    log shows about each decision so the user can spot-check.
    """
    runs = group_by_run(events)
    if not runs:
        print(f"{_C.D}No runs in audit log.{_C.END}")
        return

    latest_run = max(runs, key=lambda r: runs[r][0].get("timestamp", 0))
    print(f"\n{_C.BLD}══ Drift check — latest run: {latest_run} ══{_C.END}\n")
    print(
        f"{_C.D}A real replay would re-execute the engine against the same input bundle\n"
        f"and compare output. The audit log on its own captures decisions; replay-driven\n"
        f"drift detection requires input-bundle persistence (MK35+).{_C.END}\n"
    )
    cmd_run_detail(events, latest_run)


# ───────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="MK34 P5-T2: Replay from .mk34_audit.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--audit-log", default=str(DEFAULT_AUDIT_PATH),
                    help=f"Path to audit JSONL (default: {DEFAULT_AUDIT_PATH})")
    ap.add_argument("--runs", action="store_true",
                    help="List all run-ids in the audit log")
    ap.add_argument("--run-id", metavar="RUN_ID",
                    help="Show full trace for one run-id")
    ap.add_argument("--decisions-for", metavar="RUN_ID",
                    help="Show all DECISION_MADE events for one run-id")
    ap.add_argument("--drift-check", action="store_true",
                    help="Check decisions of the most recent run")
    args = ap.parse_args()

    audit_path = Path(args.audit_log)
    events = load_events(audit_path)

    if not events:
        print(f"{_C.D}Audit log {audit_path} is empty or missing.{_C.END}\n"
              f"{_C.D}Run the reconciliation engine first to populate it.{_C.END}")
        return

    if args.runs:
        cmd_runs(events)
    elif args.run_id:
        cmd_run_detail(events, args.run_id)
    elif args.decisions_for:
        cmd_decisions(events, args.decisions_for)
    elif args.drift_check:
        cmd_drift_check(events)
    else:
        # Default: show runs list
        cmd_runs(events)


if __name__ == "__main__":
    main()
