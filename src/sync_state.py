"""
sync_state.py  —  SQLite-backed per-file sync state manifest (Rev 14)
======================================================================
Replaces the flat `.audit_state.json` metakey-skip mechanism with a
per-field, per-file status store that drives failure-first prioritisation.

Why this module exists
----------------------
The Rev 13 audit uses `.audit_state.json` as a skip list: if a file's
metakey hasn't changed in <AUDIT_STALE_DAYS>, skip it entirely.  That's
fine for throughput but it has two structural problems:

  1. Files that failed last run get the same low priority as files that
     succeeded.  A transient "scan timeout" failure gets the same 30-day
     grace period as a clean success, so broken files stay broken for a
     month.

  2. Per-field status is lost.  If a file had a correct title but a wrong
     year last run, and the audit fixed the year, we don't know whether
     the title is verified-correct or just untouched.  Next run, we
     re-verify the whole file.

SQLite gives us:
  - Per-field status {pending, verified, failed, skipped}.
  - Priority score per file, re-computed from (failure_count,
    last_failure_age, field_fail_mask).
  - `select_priority_queue(n)` to pull the N highest-priority files for
    the next audit pass.
  - ACID writes, so partial-completion is safe.

Backwards compatibility
-----------------------
If the old `.audit_state.json` exists and the new DB does not, we migrate
on first open — every key in the old JSON becomes a row in `files` with
`verified_at = <its timestamp>` and all field statuses = "verified".
The JSON is then renamed `.audit_state.json.migrated` rather than
deleted, so rollback is possible.

Thread safety
-------------
`sqlite3` connections are per-thread by default.  This module uses a
`threading.local` pool: each thread opens its own connection on first
use, closed at thread exit.  All writes use `BEGIN IMMEDIATE` to
serialise in the presence of multiple audit workers (though
AUDIT_WORKERS=1 is the shipped default, the design supports future
parallelism).

Public API
----------
    open_db(path=None) -> None                          Initialise.
    close_db() -> None                                  Tear down.
    mark_verified(relpath, fields=None) -> None
    mark_failed(relpath, field, reason) -> None
    mark_skipped(relpath, reason) -> None
    get_state(relpath) -> dict | None
    select_priority_queue(limit) -> list[str]
    should_audit(relpath, stale_days) -> bool
    migrate_from_json(json_path) -> int
"""
from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import threading
import time

import config

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS files (
    relpath            TEXT PRIMARY KEY,
    first_seen_at      INTEGER NOT NULL,
    last_touched_at    INTEGER NOT NULL,
    verified_at        INTEGER,              -- last time every field passed
    failure_count      INTEGER DEFAULT 0,
    last_failure_at    INTEGER,
    last_failure_field TEXT,
    last_failure_msg   TEXT,
    metakey            TEXT,                 -- for legacy compatibility
    status             TEXT DEFAULT 'pending',
    -- MK29 G4-T1: Tri-state sync tracking (CTO: "sync state machine")
    -- local_status:  clean | dirty | unknown
    -- device_status: synced | pending | missing | unknown
    -- MK30 F4-T2: "unknown" split into uninitialized | error | migrating (CTO: "unknown does too much work")
    -- uninitialized = fresh row, never processed | error = last op failed | migrating = schema in progress
    local_status       TEXT DEFAULT 'uninitialized',
    device_status      TEXT DEFAULT 'uninitialized',
    last_sync_attempt  INTEGER
);

CREATE TABLE IF NOT EXISTS field_status (
    relpath   TEXT NOT NULL,
    field     TEXT NOT NULL,
    status    TEXT NOT NULL,                 -- 'verified' | 'failed' | 'pending'
    updated_at INTEGER NOT NULL,
    detail    TEXT,
    PRIMARY KEY (relpath, field),
    FOREIGN KEY (relpath) REFERENCES files(relpath) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- MK31 M4-T1: Intent log for crash-safe operations.
-- CTO: "no transactional coupling — crash leaves DB/device diverged."
-- Pattern: write intent BEFORE device op; mark complete AFTER.
-- Startup scan: orphaned intents (no completed_at) → reset device_status=pending.
CREATE TABLE IF NOT EXISTS sync_intent (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    relpath       TEXT NOT NULL,
    intended_op   TEXT NOT NULL,
    intended_status TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    completed_at  INTEGER,
    heartbeat_at  INTEGER,
    -- MK33 O4-T1: Monotonic ownership prevents split-brain orphan recovery.
    -- CTO: "process A could recover orphan while process B resumes after stall."
    -- owner_id: UUID4 stamped at process start; unique per process lifetime.
    -- lease_epoch: monotonic counter per process; helps detect stale leases.
    -- Orphan recovery only resets intents NOT owned by current process AND past lease.
    owner_id      TEXT,                -- UUID4 of the process that began this intent
    lease_epoch   INTEGER              -- per-process monotonic int (incremented per begin)
);
CREATE INDEX IF NOT EXISTS idx_sync_intent_orphaned
    ON sync_intent (completed_at) WHERE completed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_files_status         ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_failure_count  ON files(failure_count);
CREATE INDEX IF NOT EXISTS idx_files_last_failure   ON files(last_failure_at);
CREATE INDEX IF NOT EXISTS idx_field_status_status  ON field_status(status);
"""

# Priority weights used by select_priority_queue().  Each file's score is
# the sum of the applicable weights; higher = audited sooner.
_W_FAILED_LAST_RUN        = 1000   # any field failed on most recent attempt
_W_MULTI_FAILURE          = 500    # failure_count > 1
_W_NEVER_VERIFIED         = 300    # verified_at IS NULL
_W_FIELD_FAILED_PER       = 100    # per failed field
_W_AGE_DAYS_BONUS_PER_DAY = 1      # stale files drift up slowly


# ---------------------------------------------------------------------------
# Per-thread connection pool
# ---------------------------------------------------------------------------

_tls = threading.local()
_db_path: pathlib.Path | None = None
_init_lock = threading.Lock()
_initialised = False


def _conn() -> sqlite3.Connection:
    """Return this thread's sqlite connection, creating it on first use."""
    c: sqlite3.Connection | None = getattr(_tls, "conn", None)
    if c is None:
        if _db_path is None:
            raise RuntimeError("sync_state.open_db() not called yet")
        c = sqlite3.connect(
            str(_db_path),
            timeout=30.0,                 # wait up to 30s for lock
            isolation_level=None,         # we manage transactions manually
            check_same_thread=True,       # per-thread pool
        )
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _tls.conn = c
    return c


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def open_db(path: pathlib.Path | str | None = None) -> None:
    """
    Initialise the state DB.  Idempotent — safe to call multiple times.
    If the old JSON state file exists and the DB is new, migrate.
    """
    global _db_path, _initialised
    with _init_lock:
        if _initialised:
            return
        _db_path = pathlib.Path(
            path or getattr(config, "SYNC_STATE_DB",
                            str(config.BASE_DIR / ".sync_state.db"))
        )
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _db_path.exists()

        c = _conn()
        c.executescript(_SCHEMA)
        # MK29 G4-T1: Migrate existing DBs — ADD COLUMN is idempotent
        # F4-T2: new rows get 'uninitialized', old 'unknown' rows migrated below
        for _col, _default in [
            ("local_status",      "'uninitialized'"),
            ("device_status",     "'uninitialized'"),
            ("last_sync_attempt", "NULL"),
        ]:
            try:
                c.execute(f"ALTER TABLE files ADD COLUMN {_col} TEXT DEFAULT {_default}")
            except Exception:
                pass
        # MK32 N4-T1: ALTER existing sync_intent table to add heartbeat_at
        try:
            c.execute("ALTER TABLE sync_intent ADD COLUMN heartbeat_at INTEGER")
        except Exception:
            pass  # column already exists
        # MK33 O4-T1: owner_id + lease_epoch for split-brain prevention
        try:
            c.execute("ALTER TABLE sync_intent ADD COLUMN owner_id TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sync_intent ADD COLUMN lease_epoch INTEGER")
        except Exception:
            pass

        for _col, _default in []:  # neutralized — handled above
            try:
                c.execute(f"ALTER TABLE files ADD COLUMN {_col} TEXT DEFAULT {_default}")
            except Exception:
                pass  # column already exists
        # F4-T2: migrate 'unknown' → 'uninitialized'; M5-T1: 'error' → 'retryable_error'
        try:
            c.execute("UPDATE files SET local_status='uninitialized' WHERE local_status='unknown'")
            c.execute("UPDATE files SET device_status='uninitialized' WHERE device_status='unknown'")
            # M5-T1: migrate old generic "error" → "retryable_error" (more operationally precise)
            c.execute("UPDATE files SET local_status='retryable_error' WHERE local_status='error'")
            c.execute("UPDATE files SET device_status='retryable_error' WHERE device_status='error'")
        except Exception:
            pass

        # M4-T2 (A1): Startup crash reconciliation — find orphaned intent rows.
        # CTO: "crash between transition_to and device push leaves DB diverged."
        try:
            import time as _t_recon
            _now = int(_t_recon.time())
            # MK32 N4-T1: Heartbeat-aware orphan detection.
            # CTO: "long-running operations and crashed operations must be distinguishable."
            # An intent is orphaned if EITHER:
            #   (a) no heartbeat AND started >30s ago (legacy / non-heartbeated ops)
            #   (b) heartbeat present BUT older than 5min (process died between beats)
            _no_hb_cutoff   = _now - 30     # 30s for non-heartbeated ops
            _hb_cutoff      = _now - 300    # 5min for heartbeated ops
            # MK33 O4-T1: Only recover intents NOT owned by current process.
            # CTO: "prevents process A from clearing process B's in-flight intents."
            orphans = c.execute(
                "SELECT DISTINCT relpath FROM sync_intent "
                "WHERE completed_at IS NULL "
                "AND ( "
                "  (heartbeat_at IS NULL AND started_at < ?) "
                "  OR (heartbeat_at IS NOT NULL AND heartbeat_at < ?) "
                ") "
                "AND (owner_id IS NULL OR owner_id != ?)",
                (_no_hb_cutoff, _hb_cutoff, _PROCESS_OWNER_ID)
            ).fetchall()
            if orphans:
                import logging as _lr
                _lr.getLogger(__name__).warning(
                    f"[SyncState/M4-T2] {len(orphans)} orphaned intent rows → "
                    f"resetting device_status=pending for crash recovery."
                )
                for (rp,) in orphans:
                    c.execute(
                        "UPDATE files SET device_status='pending' WHERE relpath = ?", (rp,)
                    )
        except Exception:
            pass  # best-effort — sync_intent table may not exist on old DB
        c.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )

        _initialised = True

        if is_new:
            legacy = pathlib.Path(getattr(config, "AUDIT_STATE_FILE",
                                          config.BASE_DIR / ".audit_state.json"))
            if legacy.exists():
                try:
                    migrated = migrate_from_json(legacy)
                    log.info(f"  [sync_state] Migrated {migrated} entries from {legacy.name}")
                except Exception as e:
                    log.warning(f"  [sync_state] Migration failed: {e}")


def close_db() -> None:
    """Close the per-thread connection (if any).  Does not reset schema."""
    c: sqlite3.Connection | None = getattr(_tls, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _tls.conn = None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

# MK30 F4-T1 (A1): Valid transition DAG — CTO: "state machine needs invariant enforcement."
# CTO: "future devs will write illegal transitions — degenerates into flags again."
# local_status DAG:   uninitialized → clean | dirty
#                     dirty         → clean (re-fixed)
#                     clean         → dirty (detected regression)
#                     error         → uninitialized (retry after error)
# device_status DAG:  uninitialized → pending | missing
#                     pending       → synced | error | missing
#                     synced        → pending (re-sync needed)
#                     error         → pending (retry)
#                     missing       → pending (re-appeared)
# MK31 M5-T1 (A1): Split "error" into retryable_error | manual_intervention.
# CTO: "terminal states should mean manual intervention, not generic failure."
# retryable_error: MTP timeout, size mismatch, transient network → auto-retry via pending
# manual_intervention: corrupt tag, invalid path, permission denied → no auto-transition
_LOCAL_VALID_TRANSITIONS  = {
    "uninitialized":       {"clean", "dirty", "retryable_error"},
    "clean":               {"dirty", "retryable_error"},
    "dirty":               {"clean", "retryable_error", "manual_intervention"},
    "retryable_error":     {"uninitialized", "clean"},    # auto-retry path
    "manual_intervention": set(),                          # no auto-transition — requires explicit clearance
    "migrating":           {"uninitialized"},
}
_DEVICE_VALID_TRANSITIONS = {
    "uninitialized":       {"pending", "missing", "retryable_error"},
    "pending":             {"synced", "retryable_error", "missing", "manual_intervention"},
    "synced":              {"pending", "missing"},
    "retryable_error":     {"pending", "uninitialized"},   # auto-retry on next audit pass
    "manual_intervention": {"pending"},                     # explicit clearance required first
    "missing":             {"pending"},
    "migrating":           {"uninitialized"},
}


class SyncStateTransitionError(ValueError):
    """Raised when an illegal sync state transition is attempted."""


def _get_current_state(relpath: str, field: str) -> str:
    """Return current value of local_status or device_status for a relpath."""
    c = _conn()
    row = c.execute(
        f"SELECT {field} FROM files WHERE relpath = ?", (relpath,)
    ).fetchone()
    return row[0] if row else "uninitialized"


def transition_to(relpath: str, field: str, new_state: str) -> None:
    """
    MK30 F4-T1: Guard all local_status and device_status writes.
    Raises SyncStateTransitionError on invalid transition.
    Valid DAGs defined in _LOCAL_VALID_TRANSITIONS and _DEVICE_VALID_TRANSITIONS.
    """
    import logging as _log_f4
    _log = _log_f4.getLogger(__name__)

    if field == "local_status":
        valid_map = _LOCAL_VALID_TRANSITIONS
    elif field == "device_status":
        valid_map = _DEVICE_VALID_TRANSITIONS
    else:
        raise ValueError(f"transition_to: unknown field {field!r}")

    current = _get_current_state(relpath, field)
    allowed  = valid_map.get(current, set())

    if new_state not in allowed:
        raise SyncStateTransitionError(
            f"Illegal {field} transition for {relpath!r}: "
            f"{current!r} → {new_state!r}. "
            f"Allowed from {current!r}: {sorted(allowed)}"
        )
    _log.debug(
        f"  [SyncState/F4-T1] {field}: {current!r} → {new_state!r} "
        f"for {relpath!r}"
    )
    _upsert_file(relpath, **{field: new_state})


def mark_local_fixed(relpath: str) -> None:
    """MK30 F4-T3: Mark local file as fixed — routes through transition_to() guard."""
    import time as _t_g4
    try:
        transition_to(relpath, "local_status",  "clean")
        transition_to(relpath, "device_status", "pending")
        _upsert_file(relpath, last_sync_attempt=int(_t_g4.time()))
    except SyncStateTransitionError as _e:
        import logging as _log_mark
        _log_mark.getLogger(__name__).warning(
            f"  [SyncState/F4-T3] mark_local_fixed transition blocked: {_e}"
        )


def mark_device_synced(relpath: str) -> None:
    """MK30 F4-T3: Mark device synced — routes through transition_to() guard."""
    import time as _t_g4b
    try:
        transition_to(relpath, "device_status", "synced")
        _upsert_file(relpath, last_sync_attempt=int(_t_g4b.time()))
    except SyncStateTransitionError as _e:
        import logging as _log_dev
        _log_dev.getLogger(__name__).warning(
            f"  [SyncState/F4-T3] mark_device_synced transition blocked: {_e}"
        )


# MK33 O4-T1: Process-scoped owner_id + monotonic lease counter.
# CTO: "split-brain recovery remains possible — process A could recover process B's intent."
# UUID4 ensures uniqueness even with concurrent processes; lease_epoch is per-process counter.
import uuid as _uuid_o4
import threading as _th_o4
_PROCESS_OWNER_ID    = str(_uuid_o4.uuid4())   # generated once per process import
_PROCESS_LEASE_EPOCH = 0
_PROCESS_LEASE_LOCK  = _th_o4.Lock()


def get_process_owner_id() -> str:
    """Return this process's owner_id (UUID4 generated at import)."""
    return _PROCESS_OWNER_ID


def _next_lease_epoch() -> int:
    """Monotonically increasing lease counter for this process."""
    global _PROCESS_LEASE_EPOCH
    with _PROCESS_LEASE_LOCK:
        _PROCESS_LEASE_EPOCH += 1
        return _PROCESS_LEASE_EPOCH


def begin_intent(relpath: str, intended_op: str, intended_status: str) -> int:
    """
    MK31 M4-T1 + MK33 O4-T1: Write intent BEFORE device operation with process owner stamp.
    Pattern: begin_intent → device_op → complete_intent
    On crash: startup reconciliation detects NULL completed_at; only resets intents NOT
    owned by the CURRENT process (prevents split-brain).
    """
    import time as _t_intent
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(
            "INSERT INTO sync_intent "
            "(relpath, intended_op, intended_status, started_at, owner_id, lease_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (relpath, intended_op, intended_status, int(_t_intent.time()),
             _PROCESS_OWNER_ID, _next_lease_epoch())
        )
        intent_id = c.lastrowid
        c.execute("COMMIT")
        return intent_id
    except Exception:
        c.execute("ROLLBACK")
        return -1


def complete_intent(intent_id: int) -> None:
    """Mark a sync intent as completed (device op succeeded)."""
    if intent_id < 0:
        return
    import time as _t_ci
    c = _conn()
    c.execute(
        "UPDATE sync_intent SET completed_at = ? WHERE id = ?",
        (int(_t_ci.time()), intent_id)
    )


def heartbeat_intent(intent_id: int) -> None:
    """
    MK32 N4-T1: Update heartbeat for a long-running intent.
    Call every 30s during slow operations (large MTP transfers, batch ops).
    Distinguishes "still alive but slow" from "crashed".
    Orphan detection treats heartbeated ops with 5-min cutoff (vs 30s for non-heartbeated).
    """
    if intent_id < 0:
        return
    import time as _t_hb
    c = _conn()
    c.execute(
        "UPDATE sync_intent SET heartbeat_at = ? WHERE id = ?",
        (int(_t_hb.time()), intent_id)
    )


def get_pending_device_relpaths(limit: int = 500) -> list:
    """MK29 G4-T3: Return relpaths where device_status=pending, ordered by age."""
    c = _conn()
    rows = c.execute(
        "SELECT relpath FROM files WHERE device_status='pending' "
        "ORDER BY last_sync_attempt ASC NULLS LAST LIMIT ?",
        (limit,)
    ).fetchall()
    return [r[0] for r in rows]


# MK32 N5-T1 (A1): Manual intervention queue inspection + explicit clearance.
# CTO: "absorbing states without inspection tooling become permanent forgotten limbo."

def list_manual_intervention(limit: int = 500) -> list:
    """
    Return list of dicts for files in manual_intervention state (either local or device).
    Operator must explicitly review and call clear_manual_intervention() to unstick.
    """
    c = _conn()
    rows = c.execute(
        "SELECT relpath, local_status, device_status, "
        "       last_failure_field, last_failure_msg, last_failure_at, last_touched_at "
        "FROM files "
        "WHERE local_status='manual_intervention' OR device_status='manual_intervention' "
        "ORDER BY last_failure_at DESC NULLS LAST "
        "LIMIT ?",
        (limit,)
    ).fetchall()
    return [
        {
            "relpath":            r[0],
            "local_status":       r[1],
            "device_status":      r[2],
            "last_failure_field": r[3],
            "last_failure_msg":   r[4],
            "last_failure_at":    r[5],
            "last_touched_at":    r[6],
        }
        for r in rows
    ]


def clear_manual_intervention(relpath: str, reason: str = "operator_cleared") -> bool:
    """
    Explicitly clear a manual_intervention state, transitioning back to pending.
    Logs the clearance with reason for audit trail.
    Returns True if cleared, False if relpath not in manual_intervention state.
    """
    import logging as _log_n5
    _log = _log_n5.getLogger(__name__)
    c = _conn()
    row = c.execute(
        "SELECT local_status, device_status FROM files WHERE relpath = ?", (relpath,)
    ).fetchone()
    if not row:
        return False
    cleared = False
    c.execute("BEGIN IMMEDIATE")
    try:
        if row[0] == "manual_intervention":
            # local: manual_intervention has empty transition set; we override here intentionally
            c.execute(
                "UPDATE files SET local_status='uninitialized' WHERE relpath = ?", (relpath,)
            )
            cleared = True
        if row[1] == "manual_intervention":
            c.execute(
                "UPDATE files SET device_status='pending' WHERE relpath = ?", (relpath,)
            )
            cleared = True
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        return False
    if cleared:
        _log.warning(
            f"  [SyncState/N5-T1] MANUAL_INTERVENTION_CLEARED relpath={relpath!r} "
            f"reason={reason!r}"
        )
    return cleared


def list_orphaned_intents(limit: int = 500) -> list:
    """
    MK32 N5-T1: Return orphaned sync_intent rows (no completed_at, stale heartbeat or no heartbeat).
    Used by walkman_inspect.py to show what crashed/stalled.
    """
    import time as _t_loi
    c = _conn()
    _now = int(_t_loi.time())
    _no_hb_cutoff = _now - 30
    _hb_cutoff    = _now - 300
    rows = c.execute(
        "SELECT id, relpath, intended_op, intended_status, started_at, heartbeat_at "
        "FROM sync_intent "
        "WHERE completed_at IS NULL "
        "AND ( (heartbeat_at IS NULL AND started_at < ?) "
        "      OR (heartbeat_at IS NOT NULL AND heartbeat_at < ?) ) "
        "ORDER BY started_at DESC LIMIT ?",
        (_no_hb_cutoff, _hb_cutoff, limit)
    ).fetchall()
    return [
        {
            "id":              r[0],
            "relpath":         r[1],
            "intended_op":     r[2],
            "intended_status": r[3],
            "started_at":      r[4],
            "heartbeat_at":    r[5],
            "age_s":           _now - r[4],
        }
        for r in rows
    ]


def get_status_summary() -> dict:
    """MK32 N5-T1: Return status counts for dashboard view."""
    c = _conn()
    summary = {"local": {}, "device": {}}
    for row in c.execute("SELECT local_status, COUNT(*) FROM files GROUP BY local_status"):
        summary["local"][row[0]] = row[1]
    for row in c.execute("SELECT device_status, COUNT(*) FROM files GROUP BY device_status"):
        summary["device"][row[0]] = row[1]
    return summary


def _upsert_file(relpath: str, **cols) -> None:
    now = int(time.time())
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(
            """INSERT OR IGNORE INTO files (relpath, first_seen_at, last_touched_at)
               VALUES (?, ?, ?)""",
            (relpath, now, now),
        )
        if cols:
            set_clause = ", ".join(f"{k} = ?" for k in cols) + ", last_touched_at = ?"
            vals = list(cols.values()) + [now, relpath]
            c.execute(f"UPDATE files SET {set_clause} WHERE relpath = ?", vals)
        else:
            c.execute("UPDATE files SET last_touched_at = ? WHERE relpath = ?", (now, relpath))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def mark_verified(relpath: str, fields: list[str] | None = None) -> None:
    """
    Mark *relpath* as fully verified (all fields passed), or individual
    *fields* as verified while leaving overall status alone.
    """
    now = int(time.time())
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(
            """INSERT OR IGNORE INTO files (relpath, first_seen_at, last_touched_at)
               VALUES (?, ?, ?)""",
            (relpath, now, now),
        )
        if fields is None:
            # Whole-file verification
            c.execute(
                """UPDATE files
                     SET verified_at = ?, status = 'verified',
                         last_touched_at = ?
                   WHERE relpath = ?""",
                (now, now, relpath),
            )
            # All known fields for this file → verified
            c.execute(
                "UPDATE field_status SET status = 'verified', updated_at = ? WHERE relpath = ?",
                (now, relpath),
            )
        else:
            # Per-field
            for f in fields:
                c.execute(
                    """INSERT INTO field_status (relpath, field, status, updated_at)
                       VALUES (?, ?, 'verified', ?)
                       ON CONFLICT(relpath, field) DO UPDATE SET
                           status = 'verified', updated_at = excluded.updated_at""",
                    (relpath, f, now),
                )
            c.execute("UPDATE files SET last_touched_at = ? WHERE relpath = ?", (now, relpath))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def mark_failed(relpath: str, field: str, reason: str) -> None:
    """Record a field-level failure; bumps failure_count on the file row."""
    now = int(time.time())
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(
            """INSERT OR IGNORE INTO files (relpath, first_seen_at, last_touched_at)
               VALUES (?, ?, ?)""",
            (relpath, now, now),
        )
        c.execute(
            """UPDATE files
                 SET failure_count     = failure_count + 1,
                     last_failure_at   = ?,
                     last_failure_field = ?,
                     last_failure_msg  = ?,
                     status            = 'failed',
                     last_touched_at   = ?
               WHERE relpath = ?""",
            (now, field, (reason or "")[:500], now, relpath),
        )
        c.execute(
            """INSERT INTO field_status (relpath, field, status, updated_at, detail)
               VALUES (?, ?, 'failed', ?, ?)
               ON CONFLICT(relpath, field) DO UPDATE SET
                   status = 'failed', updated_at = excluded.updated_at, detail = excluded.detail""",
            (relpath, field, now, (reason or "")[:500]),
        )
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def mark_skipped(relpath: str, reason: str) -> None:
    """Intentional skip (e.g. file locked) — does NOT bump failure_count."""
    _upsert_file(relpath, status="skipped", last_failure_msg=(reason or "")[:500])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_state(relpath: str) -> dict | None:
    """Return the row dict for *relpath* or None if absent."""
    c = _conn()
    row = c.execute("SELECT * FROM files WHERE relpath = ?", (relpath,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    fs = c.execute(
        "SELECT field, status, detail FROM field_status WHERE relpath = ?",
        (relpath,),
    ).fetchall()
    d["fields"] = {r["field"]: {"status": r["status"], "detail": r["detail"]} for r in fs}
    return d


def should_audit(relpath: str, stale_days: int | None = None) -> bool:
    """
    Return True if *relpath* should be audited on this run.

    Logic:
      - Never-seen file → True
      - File with failed status → True (failure-first)
      - verified_at older than stale_days → True
      - Otherwise → False
    """
    stale = stale_days if stale_days is not None else getattr(config, "AUDIT_STALE_DAYS", 30)
    if stale == 0:
        return True  # forced full re-audit

    st = get_state(relpath)
    if st is None:
        return True
    if st.get("status") == "failed":
        return True
    v_at = st.get("verified_at")
    if v_at is None:
        return True
    age_days = (time.time() - float(v_at)) / 86400.0
    return age_days >= stale


def _score_row(row: sqlite3.Row, now: int) -> int:
    score = 0
    if row["status"] == "failed":
        score += _W_FAILED_LAST_RUN
    if (row["failure_count"] or 0) > 1:
        score += _W_MULTI_FAILURE
    if row["verified_at"] is None:
        score += _W_NEVER_VERIFIED
    # Age bonus: measured from last_touched_at
    age_days = max(0, (now - int(row["last_touched_at"] or now)) // 86400)
    score += _W_AGE_DAYS_BONUS_PER_DAY * min(age_days, 365)
    return score


def select_priority_queue(limit: int) -> list[str]:
    """
    Return the top *limit* relpaths in audit priority order.

    Used by the audit pass to decide what to look at first when batch
    size < library size.
    """
    c = _conn()
    rows = c.execute(
        """SELECT f.*,
                  COALESCE(COUNT(CASE WHEN fs.status = 'failed' THEN 1 END), 0) AS failed_fields
             FROM files f
             LEFT JOIN field_status fs ON fs.relpath = f.relpath
            GROUP BY f.relpath"""
    ).fetchall()

    now = int(time.time())
    scored = []
    for r in rows:
        s = _score_row(r, now)
        s += _W_FIELD_FAILED_PER * int(r["failed_fields"] or 0)
        if s > 0:
            scored.append((s, r["relpath"]))
    scored.sort(reverse=True)   # high score first
    return [rp for _s, rp in scored[:limit]]


# ---------------------------------------------------------------------------
# Migration from legacy .audit_state.json
# ---------------------------------------------------------------------------

def migrate_from_json(json_path: pathlib.Path) -> int:
    """
    One-shot migration.  Old format (observed from Rev 12):
        { "<relpath>": {"metakey": "...", "checked_at": <epoch>} }

    Returns the number of rows migrated.  Renames the JSON to
    `.migrated` on success so a second run of open_db() won't redo it.
    """
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    if not isinstance(raw, dict):
        return 0

    n = 0
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        for relpath, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            mk = entry.get("metakey", "")
            ts = int(entry.get("checked_at") or entry.get("ts") or 0)
            if not ts:
                continue
            c.execute(
                """INSERT OR IGNORE INTO files
                       (relpath, first_seen_at, last_touched_at,
                        verified_at, metakey, status)
                   VALUES (?, ?, ?, ?, ?, 'verified')""",
                (relpath, ts, ts, ts, mk),
            )
            n += 1
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise

    try:
        json_path.rename(json_path.with_suffix(json_path.suffix + ".migrated"))
    except Exception:
        pass

    return n
