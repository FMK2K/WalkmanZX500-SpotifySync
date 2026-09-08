"""
deferred_transfers.py — MK18 Hardened (replaces Rev 14.8)
==========================================================
MK18 #3 fix: JSONL schema + dedup + lifecycle.
"""
from __future__ import annotations
import json, logging, pathlib, threading
from datetime import datetime, timezone, timedelta
from collections import Counter

try:
    import config
except Exception:
    config = None  # type: ignore

log   = logging.getLogger(__name__)
_lock = threading.Lock()
DEFERRED_RETENTION_DAYS: int = 30

def _db_path() -> pathlib.Path:
    if config is not None:
        p = getattr(config, "DEFERRED_TRANSFERS_FILE", None)
        if p:
            return pathlib.Path(str(p)).with_suffix(".jsonl")
        sync_log = getattr(config, "SYNC_LOG_FILE", None)
        if sync_log:
            return pathlib.Path(sync_log).parent / "deferred_transfers.jsonl"
    return pathlib.Path("deferred_transfers.jsonl")

def max_mtp_size_mb() -> int:
    return int(getattr(config, "MAX_MTP_FILE_SIZE_MB", 100)) if config else 100

def should_defer(file_size_bytes: int) -> bool:
    try:
        max_mb = max_mtp_size_mb()
        return max_mb > 0 and (file_size_bytes / (1024 * 1024)) > max_mb
    except Exception:
        return False

def _canonical_key(src_path: str, dest_folder: str) -> str:
    import os
    return f"{os.path.basename(src_path)}|{dest_folder}"

def _load() -> dict[str, dict]:
    db = _db_path()
    entries: dict[str, dict] = {}
    if not db.exists():
        return entries
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=DEFERRED_RETENTION_DAYS)
    try:
        for line in db.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                key = e.get("track_key", "")
                if not key:
                    continue
                if e.get("resolved") and e.get("resolved_ts"):
                    try:
                        rdt = datetime.fromisoformat(e["resolved_ts"])
                        if rdt.tzinfo is None:
                            rdt = rdt.replace(tzinfo=timezone.utc)
                        if rdt < cutoff:
                            continue
                    except ValueError:
                        pass
                entries[key] = e
            except json.JSONDecodeError:
                continue
    except Exception as ex:
        log.debug(f"[Deferred] load error: {ex}")
    return entries

def _save(entries: dict[str, dict]) -> None:
    db = _db_path()
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries.values()) + "\n",
            encoding="utf-8",
        )
    except Exception as ex:
        log.warning(f"[Deferred] save error: {ex}")

def defer(src_path, size_bytes: int, dest_folder: str,
          reason: str = "exceeds MAX_MTP_FILE_SIZE_MB") -> None:
    """MK18 #3: idempotent JSONL deferral with dedup + lifecycle tracking."""
    src    = str(src_path)
    key    = _canonical_key(src, dest_folder)
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    size_mb = size_bytes / (1024 * 1024)
    with _lock:
        entries = _load()
        if key in entries:
            entries[key].update(last_seen=now_ts, reason=reason, resolved=False,
                                attempts=entries[key].get("attempts", 1) + 1)
            entries[key].pop("resolved_ts", None)
        else:
            entries[key] = dict(track_key=key, src=src, dest_folder=dest_folder,
                                size_mb=round(size_mb, 2), reason=reason,
                                first_seen=now_ts, last_seen=now_ts,
                                attempts=1, resolved=False, resolved_ts=None)
        _save(entries)
    log.warning(f"  [Deferred] {pathlib.Path(src).name} ({size_mb:.1f} MB) — {reason}")

def mark_resolved(src_path, dest_folder: str) -> bool:
    key = _canonical_key(str(src_path), dest_folder)
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    with _lock:
        entries = _load()
        if key in entries:
            entries[key].update(resolved=True, resolved_ts=now_ts)
            _save(entries)
            return True
    return False

def count_deferred_this_session() -> int:
    try:
        with _lock:
            return sum(1 for e in _load().values() if not e.get("resolved"))
    except Exception:
        return 0

def get_deferred_summary() -> dict:
    try:
        with _lock:
            entries = _load()
        unresolved = [e for e in entries.values() if not e.get("resolved")]
        resolved   = [e for e in entries.values() if e.get("resolved")]
        oldest     = min(unresolved, key=lambda e: e.get("first_seen", ""), default=None)
        return {
            "total": len(entries), "unresolved": len(unresolved),
            "resolved": len(resolved),
            "oldest_first_seen": oldest.get("first_seen") if oldest else None,
            "oldest_track":      pathlib.Path(oldest.get("src","")).name if oldest else None,
            "top_reasons":       Counter(e.get("reason","?") for e in unresolved).most_common(5),
        }
    except Exception as ex:
        return {"error": str(ex)}
