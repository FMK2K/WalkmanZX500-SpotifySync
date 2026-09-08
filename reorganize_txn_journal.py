"""
reorganize_txn_journal.py — MK39.1 L6.2 / Board GOV-A5
================================================================================

Board Review Revision 2 — Governance §A5 (Transaction Journal):

  "The reorganize transaction journal is an ARCHITECTURAL REQUIREMENT,
  not optional observability. Each silent_move call must append one
  record to <RUN_LOG_DIR>/reorganize_txn_<run_id>.jsonl before beginning
  and update it at each step."

Schema per board mandate — 14 fields plus timestamps:

    txn_id, run_id, src, dest, src_filename, dest_filename,
    pull_ok, pull_verify_ok, tag_mutate_ok, rename_ok,
    push_ok, push_verify_ok, delete_ok, delete_verify_ok,
    recovery_required, recovery_cache_path, failure_step,
    failure_reason, ts_start, ts_end

The journal is the primary input to:
  - L6.6 recovery_manifest workflow (walkman_inventory_repair --recovery)
  - Future MK40 provenance system (mutation chain reconstruction)

The journal file is append-only at the file level. Records are emitted
ONE PER silent_move() call, FINALIZED at the end of the call (or on
exception). Mutating an in-progress record before flush is allowed; the
final on-disk row is the final state.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


# Singleton journal — one file per process / run.
_lock = threading.Lock()
_journal_fp = None
_journal_path: Optional[Path] = None
_run_id: Optional[str] = None


def _utc_iso() -> str:
    """ISO-8601 UTC timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_journal(run_id: str, base_dir: Optional[Path] = None) -> Path:
    """Open the per-run journal file. Idempotent: returns the existing
    journal path if already initialized for this run_id.

    By default the journal lives at:
        <BASE_DIR>/recovery_journal/reorganize_txn_<run_id>.jsonl

    Returns the absolute path to the journal file.
    """
    global _journal_fp, _journal_path, _run_id

    with _lock:
        if _journal_fp is not None and _run_id == run_id:
            return _journal_path
        if _journal_fp is not None:
            _journal_fp.close()
            _journal_fp = None

        if base_dir is None:
            try:
                import config as _cfg
                base_dir = Path(getattr(_cfg, "BASE_DIR", Path.cwd()))
            except Exception:
                base_dir = Path.cwd()

        journal_dir = base_dir / "recovery_journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        _journal_path = journal_dir / f"reorganize_txn_{run_id}.jsonl"
        _journal_fp = _journal_path.open("a", encoding="utf-8", buffering=1)
        _run_id = run_id

        log.info(
            f"[ReorgTxn/L6.2] Journal opened: {_journal_path} "
            f"(MK39.1 L6.2 / Board GOV-A5)"
        )
        return _journal_path


def close_journal() -> None:
    """Close the journal file (flushes pending writes)."""
    global _journal_fp, _journal_path, _run_id
    with _lock:
        if _journal_fp is not None:
            _journal_fp.flush()
            _journal_fp.close()
            log.info(f"[ReorgTxn/L6.2] Journal closed: {_journal_path}")
        _journal_fp = None
        _journal_path = None
        _run_id = None


def new_txn(*, src: str, dest: str, src_filename: str,
              dest_filename: str) -> dict:
    """Create a new transaction record for one silent_move() call.

    Returns a mutable dict that the caller updates step-by-step. The
    record is NOT written to the journal until finalize_txn() is called.
    """
    return {
        "txn_id": str(uuid.uuid4()),
        "run_id": _run_id or "",
        "src": src,
        "dest": dest,
        "src_filename": src_filename,
        "dest_filename": dest_filename,
        "pull_ok":          False,
        "pull_verify_ok":   False,
        "tag_mutate_ok":    False,
        "rename_ok":        False,
        "push_ok":          False,
        "push_verify_ok":   False,
        "delete_ok":        False,
        "delete_verify_ok": False,
        "recovery_required": False,
        "recovery_cache_path": None,
        "failure_step":     None,
        "failure_reason":   None,
        "ts_start":         _utc_iso(),
        "ts_end":           None,
        # Board GOV-A2 — verification level used for push step
        "push_verify_level": None,
    }


def finalize_txn(txn: dict) -> None:
    """Write the txn record to the journal. Sets ts_end and computes
    recovery_required from the step flags.

    A txn requires recovery if: pull_ok=True (file was pulled) AND
    push_ok=False (push failed or never attempted). In that state the
    track may have been device-side deleted (in the pre-L5 buggy code
    path) OR may simply have failed to upload.
    """
    global _journal_fp

    if txn.get("ts_end") is None:
        txn["ts_end"] = _utc_iso()

    # Board GOV-J2 — recovery_required computation
    # (informational; the recovery manifest workflow does its own
    # cross-reference for full classification).
    if txn.get("pull_ok") and not txn.get("push_ok"):
        txn["recovery_required"] = True

    line = json.dumps(txn, ensure_ascii=False)

    with _lock:
        if _journal_fp is None:
            # Lazy init under the most permissive default if caller
            # forgot init_journal — keep the data safe, log loudly.
            log.warning(
                "[ReorgTxn/L6.2] finalize_txn called without init_journal; "
                "auto-initializing with run_id='unknown'."
            )
            init_journal("unknown")
        _journal_fp.write(line + "\n")
        _journal_fp.flush()


def mark_step(txn: dict, step: str, ok: bool, *,
                reason: Optional[str] = None) -> None:
    """Convenience: update one step flag + record failure_step on failure."""
    txn[step] = ok
    if not ok and txn.get("failure_step") is None:
        txn["failure_step"] = step
        if reason is not None:
            txn["failure_reason"] = reason


def get_journal_path() -> Optional[Path]:
    """Return the current journal path, or None if not initialized."""
    return _journal_path


def read_journal(path: Path) -> list:
    """Read a journal file (for L6.6 recovery manifest workflow).

    Returns a list of transaction dicts. Empty list if the file is
    missing or unreadable.
    """
    if not path.is_file():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning(
                    f"[ReorgTxn/L6.2] Skipped malformed journal line: "
                    f"{e}. Line: {line[:120]!r}"
                )
    return records
