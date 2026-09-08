#!/usr/bin/env python3
"""
walkman_inspect.py — MK32 N5-T2 Operator CLI for stuck-state inspection
=========================================================================
CTO: "absorbing states without inspection tooling become permanent forgotten limbo."

Provides operator visibility into:
  - Tracks stuck in manual_intervention (require explicit clearance)
  - Orphaned sync_intent rows (crashed mid-operation)
  - Overall status dashboard (counts per state)
  - Explicit clearance with audit trail

Usage:
    python walkman_inspect.py --dashboard
    python walkman_inspect.py --stuck
    python walkman_inspect.py --orphans
    python walkman_inspect.py --clear "Music/artist/track.mp3" --reason "tag_fixed_manually"

Run without arguments to see all categories at once.
"""

from __future__ import annotations
import argparse
import datetime
import sys
from pathlib import Path

# Local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_state


# ── ANSI colors (no external dependency) ───────────────────────────────
class _C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
    M = "\033[35m"; C = "\033[36m"; W = "\033[37m"; D = "\033[2m"
    BLD = "\033[1m"; END = "\033[0m"


def _ts(epoch: int | None) -> str:
    if not epoch:
        return f"{_C.D}—never—{_C.END}"
    age = int(datetime.datetime.now().timestamp()) - epoch
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    if age < 86400:
        return f"{age // 3600}h ago"
    return f"{age // 86400}d ago"


# ── Commands ───────────────────────────────────────────────────────────

def cmd_dashboard():
    """Show summary of all status counts."""
    sync_state.open_db()
    summary = sync_state.get_status_summary()

    print(f"\n{_C.BLD}══ SpotifyTuner Sync State Dashboard ══{_C.END}\n")

    print(f"{_C.BLD}Local file status:{_C.END}")
    for status, count in sorted(summary.get("local", {}).items()):
        color = (_C.G if status == "clean"
                 else _C.R if "error" in status or "manual" in status
                 else _C.Y)
        print(f"  {color}{status:<25}{_C.END} {count:>6}")

    print(f"\n{_C.BLD}Device file status:{_C.END}")
    for status, count in sorted(summary.get("device", {}).items()):
        color = (_C.G if status == "synced"
                 else _C.R if "error" in status or "manual" in status
                 else _C.Y if status == "pending"
                 else _C.D)
        print(f"  {color}{status:<25}{_C.END} {count:>6}")

    # Show stuck/orphan counts
    stuck = sync_state.list_manual_intervention(limit=9999)
    orphans = sync_state.list_orphaned_intents(limit=9999)
    print()
    if stuck:
        print(f"  {_C.R}⚠ {len(stuck)} tracks in manual_intervention{_C.END} "
              f"— run with --stuck to inspect")
    if orphans:
        print(f"  {_C.R}⚠ {len(orphans)} orphaned intent rows{_C.END} "
              f"— run with --orphans to inspect")
    if not stuck and not orphans:
        print(f"  {_C.G}✓ No stuck or orphaned items.{_C.END}")
    print()


def cmd_stuck():
    """List all tracks in manual_intervention state."""
    sync_state.open_db()
    rows = sync_state.list_manual_intervention()

    if not rows:
        print(f"\n{_C.G}✓ No tracks in manual_intervention state.{_C.END}\n")
        return

    print(f"\n{_C.BLD}══ Manual Intervention Queue ({len(rows)} tracks) ══{_C.END}\n")
    print(f"{_C.D}These tracks require explicit operator review. They have absorbing-state "
          f"local_status or device_status and will not auto-retry.{_C.END}\n")

    for row in rows:
        print(f"  {_C.R}●{_C.END} {row['relpath']}")
        print(f"      local={row['local_status']!r:<25} device={row['device_status']!r}")
        if row['last_failure_field'] or row['last_failure_msg']:
            print(f"      failure_field={row['last_failure_field']!r}")
            print(f"      failure_msg={row['last_failure_msg']!r}")
        print(f"      last_failure_at={_ts(row['last_failure_at'])}  "
              f"last_touched={_ts(row['last_touched_at'])}")
        print()

    print(f"{_C.BLD}To clear:{_C.END} python walkman_inspect.py --clear "
          f"\"<relpath>\" --reason \"<your reason>\"\n")


def cmd_orphans():
    """List orphaned sync_intent rows (crashed or stalled operations)."""
    sync_state.open_db()
    rows = sync_state.list_orphaned_intents()

    if not rows:
        print(f"\n{_C.G}✓ No orphaned intent rows.{_C.END}\n")
        return

    print(f"\n{_C.BLD}══ Orphaned Sync Intents ({len(rows)} rows) ══{_C.END}\n")
    print(f"{_C.D}These intent rows have no completion timestamp and either no heartbeat "
          f"(>30s old) or stale heartbeat (>5min). They will be reset to pending on next "
          f"open_db() startup.{_C.END}\n")

    for row in rows:
        beat = "no heartbeat" if not row['heartbeat_at'] else f"heartbeat {_ts(row['heartbeat_at'])}"
        print(f"  {_C.Y}●{_C.END} intent_id={row['id']}  relpath={row['relpath']}")
        print(f"      op={row['intended_op']!r} → status={row['intended_status']!r}")
        print(f"      started {_ts(row['started_at'])} ({row['age_s']}s ago)  {beat}")
        print()


def cmd_clear(relpath: str, reason: str):
    """Explicitly clear a manual_intervention state."""
    sync_state.open_db()
    cleared = sync_state.clear_manual_intervention(relpath, reason=reason)
    if cleared:
        print(f"{_C.G}✓ Cleared{_C.END} {relpath}")
        print(f"  reason: {reason}")
        print(f"  local_status → uninitialized, device_status → pending")
        print(f"  Will be re-checked on next audit run.")
    else:
        print(f"{_C.R}✗ Not cleared:{_C.END} {relpath}")
        print(f"  Either not found, or not in manual_intervention state.")


def cmd_reconcile():
    """MK34 P5-T3: Show recent reconciliation engine runs from audit log."""
    audit_path = Path(".mk34_audit.jsonl")
    if not audit_path.exists():
        print(f"\n{_C.D}MK34 audit log not present (.mk34_audit.jsonl not found).{_C.END}")
        print(f"{_C.D}Run the reconciliation engine to populate it.{_C.END}\n")
        return

    import json
    from collections import defaultdict
    runs = defaultdict(list)
    try:
        with audit_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        ev = json.loads(line)
                        runs[ev.get("runId", "?")].append(ev)
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        print(f"{_C.R}Failed to read audit log: {e}{_C.END}")
        return

    if not runs:
        print(f"\n{_C.D}No MK34 reconciliation runs in audit log.{_C.END}\n")
        return

    print(f"\n{_C.BLD}══ MK34 Reconciliation Runs ({len(runs)}) ══{_C.END}\n")
    for run_id in sorted(runs, key=lambda r: runs[r][0].get("timestamp", 0), reverse=True)[:5]:
        events = runs[run_id]
        from collections import Counter
        counts = Counter(e.get("type", "?") for e in events)
        decisions = sum(1 for e in events if e.get("type") == "DECISION_MADE")
        conflicts = sum(1 for e in events if e.get("type") == "CONFLICT_DETECTED")

        # Find the WRITEBACK event for the summary
        wb = next((e for e in events if e.get("type") == "WRITEBACK"), None)
        summary = wb.get("payload", {}).get("summary", {}) if wb else {}

        print(f"  {_C.C}{run_id}{_C.END}")
        print(f"    decisions={decisions}  conflicts_detected={conflicts}  "
              f"events={len(events)}")
        if summary:
            for k, v in summary.items():
                print(f"    {k:<20} {v}")
        print()
    print(f"{_C.D}Use `python mk34_replay.py --run-id RUN_ID` for full trace.{_C.END}\n")


def cmd_conflicts():
    """MK34 P5-T3: Show field conflicts from the latest run."""
    audit_path = Path(".mk34_audit.jsonl")
    if not audit_path.exists():
        print(f"\n{_C.D}MK34 audit log not present.{_C.END}\n")
        return

    import json
    events = []
    try:
        with audit_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try: events.append(json.loads(line))
                    except json.JSONDecodeError: continue
    except Exception as e:
        print(f"{_C.R}Failed: {e}{_C.END}"); return

    decisions = [e for e in events if e.get("type") == "DECISION_MADE"]
    if not decisions:
        print(f"\n{_C.G}✓ No conflicts in audit log.{_C.END}\n")
        return

    # Get latest run's decisions
    latest_run = decisions[-1].get("runId", "?")
    latest = [d for d in decisions if d.get("runId") == latest_run]

    print(f"\n{_C.BLD}══ Field Conflicts (latest run: {latest_run}) "
          f"({len(latest)} resolved) ══{_C.END}\n")
    for d in latest:
        p = d.get("payload", {})
        rejected_count = p.get("rejected_count", 0)
        print(f"  {_C.Y}●{_C.END} {p.get('fieldPath', '?')}")
        print(f"      rule={p.get('rule', '?')}")
        print(f"      selected={p.get('selectedValue')!r}  "
              f"rejected={rejected_count} alternatives")
        print()


def main():
    p = argparse.ArgumentParser(
        description="MK32 N5-T2 + MK34 P5-T3: Operator inspection CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dashboard", action="store_true",
                   help="Show summary of all status counts (default if no args)")
    p.add_argument("--stuck", action="store_true",
                   help="List tracks in manual_intervention state")
    p.add_argument("--orphans", action="store_true",
                   help="List orphaned sync_intent rows")
    p.add_argument("--reconcile", action="store_true",
                   help="MK34: Show recent reconciliation engine runs")
    p.add_argument("--conflicts", action="store_true",
                   help="MK34: Show field conflicts from latest run")
    p.add_argument("--clear", metavar="RELPATH",
                   help="Clear manual_intervention for a specific relpath")
    p.add_argument("--reason", default="operator_cleared",
                   help="Reason for --clear (logged for audit trail)")
    args = p.parse_args()

    if args.clear:
        cmd_clear(args.clear, args.reason)
        return

    # If no flags, show everything
    if not (args.dashboard or args.stuck or args.orphans
            or args.reconcile or args.conflicts):
        cmd_dashboard()
        cmd_stuck()
        cmd_orphans()
        cmd_reconcile()
        return

    if args.dashboard: cmd_dashboard()
    if args.stuck:     cmd_stuck()
    if args.orphans:   cmd_orphans()
    if args.reconcile: cmd_reconcile()
    if args.conflicts: cmd_conflicts()


if __name__ == "__main__":
    main()
