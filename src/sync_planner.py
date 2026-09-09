"""
sync_planner.py — MK23 Pure-Logic Planning Engine
==================================================
Generates an immutable SyncPlan from local vs device FileState lists.
ZERO IO. ZERO device calls. ZERO side effects.

MK23 invariant: if this function touches the device, it is a bug.

Design (Terraform-style diff):
    SKIP:       path + content match — nothing to do
    ADD:        local file absent on device — schedule copy
    REPLACE:    path exists on both, content differs — schedule replace
    DELETE:     device file absent locally — schedule delete (if policy permits)
    RENAME:     content matches, paths differ — schedule rename (saves bandwidth)
    TAG_UPDATE: file content matches but tags differ — schedule metadata fix
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict

from sync_operations import (
    AuditFinding, AuditReport, DeviceSnapshot, FileState,
    IncompleteDeviceScanError, Operation, SyncPlan
)


class CacheMisuseError(RuntimeError):
    """
    P6: Raised if any cache module is used inside the Planner.
    Rule: Planner uses ONLY the DeviceSnapshot for existence/comparison decisions.
    Cache (genre, metadata, spotify) is valid ONLY for metadata enrichment downstream.
    """


def _assert_no_cache_use() -> None:
    """P6: Runtime guard — verify no cache module is imported in planner call stack."""
    import sys
    cache_modules = {"metadata_enricher", "genre_client", "spotify_client",
                     "streaming_history", "ai_bulk_metadata"}
    for mod in cache_modules:
        if mod in sys.modules:
            frame = sys._getframe(2)
            caller = frame.f_code.co_filename
            if "sync_planner" in caller:
                raise CacheMisuseError(
                    f"Cache module '{mod}' must not be used inside the Planner. "
                    f"Cache is for metadata enrichment only, not planning decisions."
                )

log = logging.getLogger(__name__)


def _norm_path(p: str) -> str:
    return p.replace("\\", "/").strip().lower()


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def _coerce_filestates(files: list, *, label: str) -> list:
    """
    MK39.1 L3.4 / Council P1 root-cause guard, generalized: coerce bare path
    strings into minimal FileState objects so `.path` access never raises
    AttributeError, regardless of which planner entry point or argument
    receives them. Originally only applied to device_snapshot.files inside
    generate_plan(); local_files and generate_audit_report's device_files
    had the same exposure with no guard.
    """
    coerced = 0
    sample: list = []
    out: list = []
    for f in files:
        if isinstance(f, str):
            coerced += 1
            if len(sample) < 3:
                sample.append(f)
            out.append(FileState(path=f, size_bytes=0, mtime=0.0))
        else:
            out.append(f)
    if coerced:
        log.warning(
            f"[Planner] DEFENSIVE_COERCION — {label} received {coerced} bare "
            f"path string(s); coerced to FileState with size=0. Caller should "
            f"pass FileState objects. Sample: {sample}."
        )
    return out


def generate_plan(
    local_files:     list,
    device_snapshot: "DeviceSnapshot | list",
    *,
    run_id:          str  = "",
    delete_orphans:  bool = False,
    dry_run:         bool = False,
    use_hash:        bool = False,
) -> SyncPlan:
    """
    Pure-logic diff between local library and device snapshot.

    MK24 P1: accepts DeviceSnapshot (not raw list).
    If snapshot.complete=False → raises IncompleteDeviceScanError.
    This prevents silent plan corruption from partial ADB scans.

    MK24 P4: rename detection uses content_hash when available (size-only
    match is insufficient — two tracks can have identical byte counts).

    Args:
        local_files:     FileState list of local source tracks.
        device_snapshot: DeviceSnapshot (verified) OR raw list (legacy compat).
        run_id:          Pipeline run ID for correlation.
        delete_orphans:  Schedule DELETE for device files with no local match.
        dry_run:         Tag the plan as dry-run.
        use_hash:        Use content hash for comparison (slower but exact).

    Returns:
        SyncPlan with compute_hash() for dry-run equivalence verification.
    """
    # P1: enforce snapshot contract
    if isinstance(device_snapshot, DeviceSnapshot):
        device_snapshot.assert_complete()
        device_files = device_snapshot.files
    else:
        device_files = device_snapshot   # raw list (backward compat)

    # MK39.1 L3.4 / Council P1 (Sync Planner Crash root-cause), generalized
    # via _coerce_filestates — see its docstring. Originally this guard only
    # covered device_snapshot.files; local_files had the identical exposure
    # (line below: `local.path`) with no guard, so it's covered too now.
    device_files = _coerce_filestates(device_files, label="device_snapshot.files")
    local_files = _coerce_filestates(local_files, label="local_files")

    plan = SyncPlan(run_id=run_id, dry_run=dry_run)

    device_by_normpath = {_norm_path(f.path): f for f in device_files}
    device_by_size: dict = {}
    for f in device_files:
        device_by_size.setdefault(f.size_bytes, []).append(f)

    device_paths_seen: set = set()

    for local in local_files:
        norm = _norm_path(local.path)

        if norm in device_by_normpath:
            device = device_by_normpath[norm]
            device_paths_seen.add(norm)

            if local.matches(device, use_hash=use_hash):
                plan.add(Operation(op_type="skip", src=local.path, dst=device.path,
                                   overwrite=False, atomic=False, reason="content_match",
                                   run_id=run_id))
                log.debug(f"[Planner] SKIP  {local.path}")
            else:
                plan.add(Operation(op_type="replace", src=local.path, dst=device.path,
                                   overwrite=True, atomic=True, reason="content_changed",
                                   run_id=run_id))
                log.info(f"[Planner] REPLACE {local.path}")

        elif local.size_bytes > 0 and local.size_bytes in device_by_size:
            candidates = device_by_size[local.size_bytes]
            # P4: rename requires content match, not just size.
            # Two songs can have identical byte counts — size-only rename
            # would replace the wrong file.
            def _is_content_match(local_f, candidate):
                if local_f.content_hash and candidate.content_hash:
                    return local_f.content_hash == candidate.content_hash
                # Fall back to size+mtime if no hash available
                return local_f.matches(candidate, use_hash=False)

            renamed_from = next(
                (c for c in candidates if _is_content_match(local, c)), None
            )
            if renamed_from:
                device_paths_seen.add(_norm_path(renamed_from.path))
                plan.add(Operation(op_type="rename", src=renamed_from.path, dst=local.path,
                                   overwrite=True, atomic=True,
                                   reason="path_changed_content_same", run_id=run_id))
                log.info(f"[Planner] RENAME {renamed_from.path} -> {local.path}")
            else:
                plan.add(Operation(op_type="copy", src=local.path, dst=local.path,
                                   overwrite=False, atomic=True, reason="new_file",
                                   run_id=run_id))
                log.info(f"[Planner] ADD    {local.path}")
        else:
            plan.add(Operation(op_type="copy", src=local.path, dst=local.path,
                               overwrite=False, atomic=True, reason="new_file", run_id=run_id))
            log.info(f"[Planner] ADD    {local.path}")

    if delete_orphans:
        for norm, device in device_by_normpath.items():
            if norm not in device_paths_seen:
                plan.add(Operation(op_type="delete", src=device.path, dst="",
                                   overwrite=False, atomic=False,
                                   reason="absent_locally", run_id=run_id))
                log.info(f"[Planner] DELETE {device.path}")

    log.info(f"[Planner] {plan.summary()}")
    plan.planned_dirs = sorted({
        op.dst.replace("\\", "/").rsplit("/", 1)[0]
        for op in plan.operations
        if op.op_type in ("copy", "replace") and op.dst and "/" in op.dst
    })
    return plan


def generate_audit_report(
    device_files:      list,
    spotify_inventory: dict | None = None,
    run_id:            str  = "",
) -> AuditReport:
    """
    Analyse device files for tag/folder/splinter issues.

    Detects:
        FOLDER_MISMATCH  — file in wrong Artist/Album directory
        WRONG_TRACK_NUM  — track number conflicts with Spotify
        SPLINTER         — 1 track in known multi-track album folder
    """
    device_files = _coerce_filestates(device_files, label="generate_audit_report.device_files")
    report = AuditReport(run_id=run_id, files_scanned=len(device_files))
    by_album_folder: dict = defaultdict(list)

    for f in device_files:
        parts    = f.path.replace("\\", "/").split("/")
        music_i  = next((i for i, p in enumerate(parts) if p.lower() == "music"), -1)
        if music_i >= 0 and len(parts) > music_i + 2:
            key = "/".join(parts[music_i:music_i + 3])
        else:
            key = "/".join(parts[:-1])
        by_album_folder[key].append(f)

    for folder_key, tracks in by_album_folder.items():
        artist_fp, album_fp = _folder_artist_album(folder_key)

        for t in tracks:
            if t.artist and artist_fp and _norm_name(t.artist) != _norm_name(artist_fp):
                report.add(AuditFinding(
                    path=t.path,
                    finding_type="FOLDER_MISMATCH",
                    detail=f"Tag artist={t.artist!r} but folder implies {artist_fp!r}",
                    severity="high",
                    suggested_op=Operation(
                        op_type="rename", src=t.path,
                        dst=_canonical_path(t), reason="folder_artist_mismatch",
                        run_id=run_id,
                    ) if t.artist and t.album else None,
                ))

            if spotify_inventory and t.artist and t.album:
                key = (_norm_name(t.artist), _norm_name(t.album))
                inv = spotify_inventory.get(key, {})
                if inv and t.track_number:
                    title_key = _norm_name(t.path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
                    expected  = inv.get("tracks", {}).get(title_key)
                    if expected and str(t.track_number) != str(expected):
                        report.add(AuditFinding(
                            path=t.path,
                            finding_type="WRONG_TRACK_NUM",
                            detail=f"Tag={t.track_number!r} vs Spotify={expected!r}",
                            severity="high",
                        ))

        if len(tracks) == 1 and spotify_inventory:
            t   = tracks[0]
            key = (_norm_name(t.artist or ""), _norm_name(t.album or ""))
            inv = spotify_inventory.get(key, {})
            if inv.get("total_tracks", 0) > 1:
                report.add(AuditFinding(
                    path=t.path,
                    finding_type="SPLINTER",
                    detail=f"1/{inv['total_tracks']} tracks present for '{t.artist}' — '{t.album}'",
                    severity="critical",
                ))

    log.info(f"[Audit] {report.summary()}")
    return report


def _folder_artist_album(folder_key: str):
    parts = [p for p in folder_key.rstrip("/").split("/")
             if p.lower() not in ("music", "sd card", "internal shared storage",
                                  "storage", "emulated", "0")]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _canonical_path(f) -> str:
    if not (f.artist and f.album):
        return f.path

    def safe(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return re.sub(r'[\\/:*?"<>|]', "_", s).strip()

    filename  = f.path.rsplit("/", 1)[-1]
    parts     = f.path.replace("\\", "/").split("/")
    music_i   = next((i for i, p in enumerate(parts) if p.lower() == "music"), -1)
    prefix    = "/".join(parts[:music_i + 1]) if music_i >= 0 else "/storage/emulated/0/Music"
    return f"{prefix}/{safe(f.artist)}/{safe(f.album)}/{filename}"

# MK15/16 alias — walkman_sync.py calls build_sync_plan(); generate_plan() is the implementation
build_sync_plan = generate_plan
