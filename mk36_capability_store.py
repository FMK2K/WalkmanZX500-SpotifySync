"""
mk36_capability_store.py — MK36 R2: Durable Capability Authority Store
========================================================================
CTO MK35 review:
  "Right now the capability model appears memory-local, runtime-local.
   Restart semantics unclear, multi-process semantics unclear, distributed
   worker semantics unclear. Eventually replay workers, async executors,
   parallel reconciliation will appear. At that point capability authority
   must become durable and transactional. Otherwise you create split-brain
   execution authority."

This module backs the MK35 capability registry with SQLite (WAL mode + file
lock) so capabilities survive process restart and grant/consume operations
are atomic across concurrent processes.

Architecture:
  L1 (process-local dict, MK35) — fast lookup; cleared on restart
  L2 (SQLite file, MK36)        — durable; multi-process safe; survives restart

Reads check L1 first, fall through to L2 on miss, populate L1 on hit.
Writes go to L2 inside an immediate transaction; L1 is updated post-commit.

Crash semantics:
  - Mid-grant crash: WAL guarantees atomicity — either grant committed or not
  - Mid-consume crash: same — either consume committed or not
  - Stale L1 entries: detected on next L2 lookup (used flag mismatch)

The MK35 in-memory path is preserved as a fallback when SQLite isn't
available (e.g. read-only filesystem, missing module). All MK35 tests
continue to pass against the L1-only path.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterator, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Schema (R2-T1)
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_STORE_PATH = Path(".mk36_capabilities.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capabilities (
    token_id              TEXT PRIMARY KEY,
    scope                 TEXT NOT NULL,
    granted_at            REAL NOT NULL,
    expires_at            REAL NOT NULL,
    granted_by_invariant  TEXT NOT NULL,
    internal_secret_hash  TEXT NOT NULL,
    used                  INTEGER NOT NULL DEFAULT 0,
    consumed_at           REAL,
    consumed_for          TEXT
);

CREATE INDEX IF NOT EXISTS idx_caps_expires ON capabilities(expires_at);
CREATE INDEX IF NOT EXISTS idx_caps_used    ON capabilities(used);

CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    action     TEXT NOT NULL,
    token_id   TEXT NOT NULL,
    detail     TEXT
);
"""


# ═══════════════════════════════════════════════════════════════════════════
# DurableCapabilityStore (R2-T1 + R2-T2)
# ═══════════════════════════════════════════════════════════════════════════

class DurableCapabilityStore:
    """
    SQLite-backed, multi-process-safe capability store.

    Each instance opens its own connection; SQLite handles cross-process
    concurrency via WAL mode + file-level locking. The store is single-file,
    portable, and works on any filesystem that supports advisory locks.
    """

    def __init__(self, db_path: Path | str = DEFAULT_STORE_PATH):
        self.db_path = Path(db_path)
        self._init_lock = RLock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """
        Open a connection per call (SQLite connections are not thread-safe by
        default). WAL mode + synchronous=NORMAL is the right balance for
        capability grants — durable enough that we don't lose tokens to a
        normal crash, fast enough to not bottleneck the grant path.
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,   # we manage transactions explicitly
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._init_lock:
            with self._connect() as conn:
                # executescript() implicitly commits and ends any active txn,
                # so we don't need (and can't issue) an additional COMMIT.
                conn.executescript(_SCHEMA)

    # ── Grant (R2-T2 atomic insert) ──────────────────────────────────────

    def grant(self, *,
              token_id: str, scope: str, granted_at: float, expires_at: float,
              granted_by_invariant: str, internal_secret_hash: str,
             ) -> bool:
        """
        Atomically insert a new capability row. Returns True on success.

        Since token_ids are uuid4, collisions are effectively impossible;
        we use INSERT (not REPLACE) so a duplicate would surface as an
        IntegrityError — useful as a sanity check that the issuer isn't
        reusing ids.
        """
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO capabilities "
                    "(token_id, scope, granted_at, expires_at, "
                    " granted_by_invariant, internal_secret_hash, used) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (token_id, scope, granted_at, expires_at,
                     granted_by_invariant, internal_secret_hash),
                )
                conn.execute(
                    "INSERT INTO audit_log (ts, action, token_id, detail) "
                    "VALUES (?, 'GRANT', ?, ?)",
                    (time.time(), token_id, f"scope={scope!r}"),
                )
                conn.execute("COMMIT")
                return True
            except sqlite3.IntegrityError as e:
                conn.execute("ROLLBACK")
                log.error(f"[MK36/R2] grant collision token_id={token_id[:8]}: {e}")
                return False

    # ── Lookup (R2-T2 + R2-T3) ───────────────────────────────────────────

    def lookup(self, token_id: str) -> Optional[dict]:
        """Return the capability row as a dict, or None if not present."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token_id, scope, granted_at, expires_at, "
                "       granted_by_invariant, internal_secret_hash, used, "
                "       consumed_at, consumed_for "
                "FROM capabilities WHERE token_id=?",
                (token_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "token_id":             row[0],
            "scope":                row[1],
            "granted_at":           row[2],
            "expires_at":           row[3],
            "granted_by_invariant": row[4],
            "internal_secret_hash": row[5],
            "used":                 bool(row[6]),
            "consumed_at":          row[7],
            "consumed_for":         row[8],
        }

    # ── Consume (R2-T2 atomic single-use) ────────────────────────────────

    def consume(self, token_id: str, operation: str, path: str) -> bool:
        """
        Atomically mark a capability as used. Returns True if this call
        successfully consumed the capability (i.e. it was unused before).
        Returns False if the capability was already consumed by another
        caller (or process), is missing, or is expired.

        Implementation note: the WHERE clause checks both used=0 and
        expires_at > now, ensuring atomicity against expiry races.
        """
        now = time.time()
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE capabilities SET used=1, consumed_at=?, consumed_for=? "
                    "WHERE token_id=? AND used=0 AND expires_at > ?",
                    (now, f"{operation}:{path}", token_id, now),
                )
                if cursor.rowcount == 1:
                    conn.execute(
                        "INSERT INTO audit_log (ts, action, token_id, detail) "
                        "VALUES (?, 'CONSUME', ?, ?)",
                        (now, token_id, f"op={operation!r} path={path!r}"),
                    )
                    conn.execute("COMMIT")
                    return True
                else:
                    conn.execute("ROLLBACK")
                    return False
            except sqlite3.Error as e:
                try: conn.execute("ROLLBACK")
                except Exception: pass
                log.error(f"[MK36/R2] consume failed token_id={token_id[:8]}: {e}")
                return False

    # ── GC (R2-T3 expired cleanup) ───────────────────────────────────────

    def gc_expired(self, before: Optional[float] = None) -> int:
        """
        Delete expired or consumed-and-old rows. Returns count deleted.
        Should be called periodically (typically once per run, at the end).
        """
        cutoff = before if before is not None else time.time()
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "DELETE FROM capabilities "
                    "WHERE expires_at < ? OR (used=1 AND consumed_at < ?)",
                    (cutoff, cutoff - 3600),  # keep consumed for 1h for audit
                )
                count = cursor.rowcount
                conn.execute(
                    "INSERT INTO audit_log (ts, action, token_id, detail) "
                    "VALUES (?, 'GC', '*', ?)",
                    (time.time(), f"removed={count}"),
                )
                conn.execute("COMMIT")
                return count
            except sqlite3.Error as e:
                try: conn.execute("ROLLBACK")
                except Exception: pass
                log.error(f"[MK36/R2] gc failed: {e}")
                return 0

    # ── Revoke all (operator emergency) ──────────────────────────────────

    def revoke_all(self, reason: str = "explicit_revoke") -> int:
        """Mark all unused capabilities as used. Returns count revoked."""
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE capabilities SET used=1, consumed_at=?, consumed_for=? "
                    "WHERE used=0",
                    (time.time(), f"REVOKED:{reason}"),
                )
                count = cursor.rowcount
                conn.execute(
                    "INSERT INTO audit_log (ts, action, token_id, detail) "
                    "VALUES (?, 'REVOKE_ALL', '*', ?)",
                    (time.time(), f"reason={reason!r} count={count}"),
                )
                conn.execute("COMMIT")
                log.warning(
                    f"  [MK36/R2] CAPABILITIES_REVOKED_DURABLE count={count} "
                    f"reason={reason!r} (durable store)"
                )
                return count
            except sqlite3.Error as e:
                try: conn.execute("ROLLBACK")
                except Exception: pass
                return 0

    # ── Diagnostics ──────────────────────────────────────────────────────

    def list_active(self) -> "list[dict]":
        """Return all currently-valid (unused, unexpired) capability rows."""
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT token_id, scope, granted_at, expires_at, "
                "       granted_by_invariant "
                "FROM capabilities WHERE used=0 AND expires_at > ?",
                (now,),
            ).fetchall()
        return [
            {"token_id": r[0], "scope": r[1], "granted_at": r[2],
             "expires_at": r[3], "granted_by_invariant": r[4]}
            for r in rows
        ]


# ═══════════════════════════════════════════════════════════════════════════
# Singleton accessor (used by mk35_capability when MK36 store is enabled)
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT_STORE: Optional[DurableCapabilityStore] = None
_STORE_LOCK = RLock()


def get_default_store(db_path: Path | str = DEFAULT_STORE_PATH) -> DurableCapabilityStore:
    """Return the process-wide default store, creating it on first call."""
    global _DEFAULT_STORE
    with _STORE_LOCK:
        if _DEFAULT_STORE is None or _DEFAULT_STORE.db_path != Path(db_path):
            _DEFAULT_STORE = DurableCapabilityStore(db_path)
        return _DEFAULT_STORE


def reset_default_store() -> None:
    """Clear the singleton (used by tests)."""
    global _DEFAULT_STORE
    with _STORE_LOCK:
        _DEFAULT_STORE = None


__all__ = [
    "DurableCapabilityStore", "DEFAULT_STORE_PATH",
    "get_default_store", "reset_default_store",
]
