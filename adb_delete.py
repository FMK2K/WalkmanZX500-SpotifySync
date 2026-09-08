"""
adb_delete.py — Emergency dedup-delete fallback via ADB shell rm.

Context: the MTP/Shell.Application path (wpd_transfer.delete_files_single_ps)
depends on `$root.GetFolder.Items()` enumerating ~1,750+ album subfolders in
one blocking COM call. Under heavy flash wear-leveling GC pressure (SD card
98%+ full) that call — and the per-album ParseName fallback, which internally
re-enumerates the same folder — can hang indefinitely. This tool sidesteps
enumeration entirely: `adb shell rm <path>` addresses each file directly by
its known path, no folder listing required.

Requires the device in Charging + USB debugging (ADB) mode, not MTP mode.

Usage:
    python adb_delete.py                 # dry-run, shows the plan
    python adb_delete.py --execute        # actually delete
    python adb_delete.py --execute --limit 200   # cap this run's file count

Shares .deep_clean_progress.json with cli_runner.py's `deep-clean --from-log
--execute` (MTP path) — both write to the same "done" set, so runs of either
tool resume correctly regardless of which one made progress last.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import adb_transfer
import mk37_wiring as wiring
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

HERE = Path(__file__).parent
LOG_PATH = HERE / ".dedup_log.json"
PROG_PATH = HERE / ".deep_clean_progress.json"


def _load_candidates() -> list:
    entries = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    entry = min(
        (e for e in entries if 13000 <= e.get("scan_total", 0) <= 14500),
        key=lambda e: abs(e.get("scan_total", 0) - 13237),
        default=None,
    )
    if not entry:
        log.error("[ADBDelete] No 13K-track entry found in .dedup_log.json")
        return []
    return entry["candidates"]


def _load_done() -> set:
    if not PROG_PATH.exists():
        return set()
    try:
        data = json.loads(PROG_PATH.read_text(encoding="utf-8"))
        return set(data.get("done", data.get("deleted", [])))
    except Exception as e:
        log.warning(f"[ADBDelete] Could not load progress file: {e}")
        return set()


def _save_done(done: set) -> None:
    existing_hung = []
    if PROG_PATH.exists():
        try:
            existing_hung = json.loads(PROG_PATH.read_text(encoding="utf-8")).get("hung_parts", [])
        except Exception:
            pass
    PROG_PATH.write_text(
        json.dumps({"done": sorted(done), "hung_parts": existing_hung}, ensure_ascii=False),
        encoding="utf-8",
    )


def _delete_or_raise(c: dict) -> None:
    ok = adb_transfer.delete_file_on_device(
        device_name="WALKMAN", storage_name=c["storage"],
        folder_parts=c["parts"], filename=c["filename"],
    )
    if not ok:
        raise RuntimeError(f"adb rm failed for {c['filename']!r}")


def main() -> int:
    execute = "--execute" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    cands = _load_candidates()
    if not cands:
        return 1
    reasons = Counter(c["reason"] for c in cands)
    log.info(f"[ADBDelete] {len(cands)} candidates: {dict(reasons)}")

    if not adb_transfer._adb_cmd(["get-state"], timeout=5).strip() == "device":
        log.error(
            "[ADBDelete] No ADB device reachable. Switch the Walkman from "
            "MTP to Charging + USB debugging mode, then retry."
        )
        return 1

    done = _load_done()
    todo = [
        c for c in cands
        if f"{c['storage']}|{'|'.join(c['parts'])}|{c['filename']}" not in done
    ]
    if limit:
        todo = todo[:limit]
    log.info(f"[ADBDelete] {len(todo)} files remaining to delete (of {len(cands)} total)")

    if not execute:
        log.info("[ADBDelete] DRY-RUN — pass --execute to delete via adb shell rm")
        for c in todo[:20]:
            log.info(f"  [{c['reason']}] {c.get('human_path', c.get('filename', ''))}")
        if len(todo) > 20:
            log.info(f"  ... and {len(todo) - 20} more")
        return 0

    n_del = n_err = 0
    for i, c in enumerate(todo, 1):
        key = f"{c['storage']}|{'|'.join(c['parts'])}|{c['filename']}"
        res = wiring.idempotent_destructive_op(
            key, "delete", lambda c=c: _delete_or_raise(c), actor="adb_delete",
        )
        if res.get("executed") or res.get("duplicate"):
            done.add(key)
            n_del += 1
        else:
            n_err += 1
            log.warning(f"[ADBDelete] Failed: {c['filename']!r}: {res.get('error')}")
        if i % 100 == 0:
            log.info(f"[ADBDelete] {i}/{len(todo)} processed (deleted={n_del} err={n_err})")
            _save_done(done)

    _save_done(done)
    log.info(f"[ADBDelete] Done — deleted={n_del} error={n_err} (of {len(todo)} this run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
