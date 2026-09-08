"""
free_space_check.py — Rev 14.9.2
================================
Pre-flight device free-space check.  Uses `adb shell df -k` to read the
mounted storage under the configured WRITE_STORAGE target, compares
against the estimated Stage 0 download size × config.FREE_SPACE_SAFETY_MARGIN,
and emits a CRITICAL log line if insufficient.

LOG-13 showed "No space left on device" errors firing mid-run, after
hours of work.  This check surfaces the problem at startup when the user
can still act on it.

Rev 14.9.2 fixes:
  1. LOG-14 emitted "1 MB available on device, but ~44284 MB required"
     while the run proceeded to successfully transfer dozens of tracks
     to SD card.  Root cause: the check hard-coded mount_hint=
     "/storage/emulated", which is the INTERNAL partition (typically
     small on a ZX500), while the user's WRITE_STORAGE was "SD card".
     We now resolve the mount from config.WRITE_STORAGE so the reading
     reflects the storage actually being written to.
  2. A cold-cache read can return a tiny bogus value (e.g. 1 MB).  We
     now treat readings below SANITY_MIN_BYTES as "probe returned
     garbage — unknown" rather than "insufficient", retry once, and
     log the reading verbatim for diagnosis.

Does NOT block: the user may intend to delete content as the sync runs.
The agent warns prominently and continues.
"""
from __future__ import annotations

import logging
import re
import time

try:
    import config
except Exception:
    config = None  # type: ignore

log = logging.getLogger(__name__)


# Readings below this are almost certainly garbage on a device that has
# any music library at all.  Triggers retry + "unknown" classification.
SANITY_MIN_BYTES = 10 * 1024 * 1024  # 10 MB


_DF_RE = re.compile(
    r'^\S+\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+%\s+(\S+)',
    re.MULTILINE,
)


def _parse_df_k(output: str, mount_hint: str) -> tuple[int, int] | None:
    """
    Parse `df -k` output, returning (total_kb, available_kb) for the line
    whose mount column contains `mount_hint`, or None if no match.
    """
    for m in _DF_RE.finditer(output or ""):
        total_kb = int(m.group(1))
        avail_kb = int(m.group(3))
        mount    = m.group(4)
        if mount_hint in mount or mount == mount_hint:
            return total_kb, avail_kb
    return None


def _resolve_mount_hint() -> str:
    """
    Pick the df mount path that corresponds to config.WRITE_STORAGE.

    Sony ZX500/ZX507 expose:
      - Internal storage as /storage/emulated/0
      - SD card as /storage/XXXX-XXXX/ (hex volume UUID, varies per card)

    Since we can't know the UUID up-front, for "SD card" we match any
    /storage/ path that ISN'T /storage/emulated and isn't /storage/self.
    The caller probes each candidate and picks the first with a
    plausible reading.
    """
    write_storage = ""
    if config is not None:
        write_storage = str(getattr(config, "WRITE_STORAGE", "") or "").strip().lower()

    if "sd" in write_storage or "card" in write_storage or "external" in write_storage:
        return "__SD_CARD__"  # sentinel, handled in get_device_free_bytes
    return "/storage/emulated"


def _find_sd_mount(df_output: str) -> tuple[int, int] | None:
    """
    Scan df output for an SD-card-shaped mount path.  Sony mounts the
    external card at /storage/XXXX-XXXX where XXXX-XXXX is the volume UUID.
    Returns (total_kb, avail_kb) for the largest plausible match, or None.
    """
    best: tuple[int, int] | None = None
    best_size = -1
    for m in _DF_RE.finditer(df_output or ""):
        total_kb = int(m.group(1))
        avail_kb = int(m.group(3))
        mount    = m.group(4)
        # Skip internal emulated storage and bind mounts
        if not mount.startswith("/storage/"):
            continue
        if mount.startswith("/storage/emulated") or mount.startswith("/storage/self"):
            continue
        # Prefer the largest /storage/* partition — SD card is typically
        # much bigger than any other mounted external volume.
        if total_kb > best_size:
            best = (total_kb, avail_kb)
            best_size = total_kb
    return best


def get_device_free_bytes(mount_hint: str | None = None) -> int | None:
    """
    Ask the device for free bytes for the configured WRITE_STORAGE.
    Returns None if the query fails or returns an implausibly small
    value (caller should treat None as "unknown, proceed").

    Retries once on None/implausible result before giving up.
    """
    try:
        from adb_transfer import _adb_cmd
    except Exception as e:
        log.warning(f"[FreeSpace] adb_transfer unavailable: {e}")
        return None

    hint = mount_hint if mount_hint is not None else _resolve_mount_hint()

    for attempt in (1, 2):
        try:
            out = _adb_cmd(["shell", "df", "-k"], timeout=30, check=False)
        except Exception as e:
            log.warning(f"[FreeSpace] df failed (attempt {attempt}/2): {e}")
            out = None

        if out:
            if hint == "__SD_CARD__":
                parsed = _find_sd_mount(out)
                what = "SD card (largest /storage/* non-emulated mount)"
            else:
                parsed = _parse_df_k(out, hint)
                what = f"mount matching {hint!r}"

            if parsed is None:
                log.info(
                    f"[FreeSpace] Attempt {attempt}/2: could not find {what} "
                    f"in df output"
                )
            else:
                _total_kb, avail_kb = parsed
                avail_bytes = avail_kb * 1024
                if avail_bytes >= SANITY_MIN_BYTES:
                    return avail_bytes
                # Implausibly small — most likely a cold cache or a stale
                # bind mount read.  Retry.
                log.info(
                    f"[FreeSpace] Attempt {attempt}/2: implausible reading "
                    f"({avail_kb} KB available for {what}); "
                    f"treating as stale cache"
                )

        if attempt == 1:
            time.sleep(1.5)  # let the device settle before retry

    # 2026-07-19: ADB df failed/unavailable after both retries — fall back to
    # the already-built, already-hardened COM/MTP free-space query
    # (mtp_transfer_com.get_device_free_space, exposed via mtp_transfer's
    # ADB->COM proxy) before giving up entirely. Directly addresses nights
    # where ADB is flaky/offline but MTP/COM enumeration is still healthy —
    # this function previously had no path off ADB at all.
    try:
        import mtp_transfer as _mtp
        device_name = getattr(config, "DEVICE_NAME", "") if config else ""
        storage_name = getattr(config, "WRITE_STORAGE", "") if config else ""
        if device_name and storage_name:
            com_bytes = _mtp.get_device_free_space(device_name, storage_name)
            if com_bytes is not None and com_bytes > 0 and com_bytes >= SANITY_MIN_BYTES:
                log.info(
                    f"[FreeSpace] ADB df unavailable — used COM/MTP fallback: "
                    f"{com_bytes / (1024 ** 3):.2f} GB free"
                )
                return com_bytes
    except Exception as e:
        log.debug(f"[FreeSpace] COM/MTP fallback also failed: {e}")

    return None


def check_free_space(
    estimated_bytes: int,
    mount_hint: str | None = None,
) -> dict:
    """
    Compare available device space against an estimated download size.
    Returns a dict with 'ok', 'below_floor', 'available_bytes',
    'internal_available_bytes', 'required_bytes', 'safety_margin',
    'min_free_bytes', and 'message'.

    Rev 14.9.4 D1 — also probes internal storage even when the user is
    syncing to SD.  Internal must always have headroom for the
    Android system (MediaStore index growth, ART optimization, app
    crash dumps).  If internal is below the floor, log CRITICAL but
    do NOT halt unless WRITE_STORAGE is internal — the user might
    have a separate plan to free internal that doesn't involve us.
    """
    margin = 1.3
    min_free_gb = 0.5
    if config is not None:
        margin = float(getattr(config, "FREE_SPACE_SAFETY_MARGIN", 1.3))
        min_free_gb = float(getattr(config, "MIN_FREE_SPACE_GB", 0.5))
    required = int(estimated_bytes * margin)
    min_free_bytes = int(min_free_gb * 1024 * 1024 * 1024)

    available = get_device_free_bytes(mount_hint)

    # Rev 14.9.4 D1 — independent probe of internal storage, regardless
    # of where WRITE_STORAGE points.  Pass the explicit /storage/emulated
    # mount hint to bypass the "follow WRITE_STORAGE" logic in
    # _resolve_mount_hint.  Result is informational unless internal IS
    # WRITE_STORAGE (in which case the regular `available` query already
    # covers it).
    internal_available = get_device_free_bytes("/storage/emulated")
    write_is_internal = (
        config is not None
        and "internal" in str(getattr(config, "WRITE_STORAGE", "")).lower()
    )
    if (
        internal_available is not None
        and not write_is_internal
        and internal_available < min_free_bytes
    ):
        log.critical(
            f"[FreeSpace] INTERNAL STORAGE near-full — only "
            f"{internal_available / (1024**3):.2f} GB free on the device's "
            f"emulated/0 partition (your sync target is the SD card, but "
            f"Android needs internal headroom for MediaStore indexing).  "
            f"This is advisory — sync continues — but device may become "
            f"unstable.  Consider freeing internal storage on the Walkman."
        )

    if available is None:
        msg = (
            f"Could not determine device free space after retry; skipping check. "
            f"Estimated need: {estimated_bytes / (1024*1024):.0f} MB. "
            f"If sync fails mid-run with 'No space left on device', "
            f"free up space manually."
        )
        log.warning(f"[FreeSpace] {msg}")
        return {
            "ok":              True,  # unknown = proceed
            "below_floor":     False,
            "available_bytes": None,
            "internal_available_bytes": internal_available,
            "required_bytes":  required,
            "safety_margin":   margin,
            "min_free_bytes":  min_free_bytes,
            "message":         msg,
        }

    av_mb = available / (1024 * 1024)
    req_mb = required / (1024 * 1024)
    est_mb = estimated_bytes / (1024 * 1024)
    av_gb = available / (1024 * 1024 * 1024)

    # Rev 14.9.3 F5 — absolute floor check.  If the device is already
    # below MIN_FREE_SPACE_GB, the Android system itself is at risk
    # (MediaStore index, app crash dumps).  This is a HALT condition.
    below_floor = available < min_free_bytes
    if below_floor:
        msg = (
            f"DEVICE NEAR-FULL — only {av_gb:.2f} GB free, below the "
            f"{min_free_gb:.1f} GB safety floor.  The Android system needs "
            f"this headroom for MediaStore indexing and app stability.  "
            f"Sync HALTED.  Delete content from the device until at least "
            f"{min_free_gb:.1f} GB is free, then retry."
        )
        log.critical(f"[FreeSpace] {msg}")
        return {
            "ok":              False,
            "below_floor":     True,
            "available_bytes": available,
            "internal_available_bytes": internal_available,
            "required_bytes":  required,
            "safety_margin":   margin,
            "min_free_bytes":  min_free_bytes,
            "message":         msg,
        }

    if available >= required:
        msg = (
            f"OK — {av_mb:.0f} MB available on device, "
            f"{est_mb:.0f} MB estimated (+{margin:.0%} margin = {req_mb:.0f} MB), "
            f"floor {min_free_gb:.1f} GB satisfied"
        )
        log.info(f"[FreeSpace] {msg}")
        ok = True
    else:
        msg = (
            f"INSUFFICIENT SPACE — {av_mb:.0f} MB available on device, "
            f"but ~{req_mb:.0f} MB required ({est_mb:.0f} MB × {margin:.0%} safety margin). "
            f"Sync will proceed but transfers may fail mid-run, and the run "
            f"will HALT if free space drops below the {min_free_gb:.1f} GB "
            f"system safety floor.  Free up space on the Walkman, or ensure "
            f"your SD card is configured as WRITE_STORAGE with sufficient "
            f"headroom."
        )
        log.critical(f"[FreeSpace] {msg}")
        ok = False

    return {
        "ok":              ok,
        "below_floor":     False,
        "available_bytes": available,
        "internal_available_bytes": internal_available,
        "required_bytes":  required,
        "safety_margin":   margin,
        "min_free_bytes":  min_free_bytes,
        "message":         msg,
    }


# ─── Rev 14.9.4 D2 — mid-run advisory monitor ──────────────────────────
# Lightweight wrapper around get_device_free_bytes that emits a CRITICAL
# log line if the device's WRITE_STORAGE has dropped below the floor.
# Designed to be called every N transfers from the download loop.
# Returns True if free-space is OK (or unknown), False if below the floor.
# It does NOT halt the run — that decision is left to the caller.
# Pre-14.9.4 had no mid-run check at all; if the device filled up between
# the preflight and track 451, we'd just keep pushing until adb errored.
def midrun_floor_check(min_free_gb: float | None = None) -> dict:
    """
    Return {'ok': bool, 'available_bytes': int|None, 'message': str}.
    Emits CRITICAL log line if below floor.  Cheap (~50ms) — safe to call
    every 10 transfers.
    """
    floor_gb = min_free_gb
    if floor_gb is None and config is not None:
        floor_gb = float(getattr(config, "MIN_FREE_SPACE_GB", 0.5))
    if floor_gb is None:
        floor_gb = 0.5

    available = get_device_free_bytes()  # uses WRITE_STORAGE-resolved hint
    if available is None:
        return {"ok": True, "available_bytes": None,
                "message": "free-space unknown (cache cold)"}

    floor_bytes      = int(floor_gb * 1024 ** 3)
    hard_floor_bytes = int(float(getattr(config, "MIN_FREE_SPACE_GB_HARD", 1.0)) * 1024**3) if config else int(1024**3)
    av_gb = available / (1024 ** 3)
    # MK18 #11: hard-stop check — halt all mutating operations immediately
    if available < hard_floor_bytes:
        hard_gb = hard_floor_bytes / (1024 ** 3)
        msg = (
            f"HARD FLOOR BREACHED — {av_gb:.2f} GB free is below the "
            f"{hard_gb:.1f} GB hard stop.  All transfers HALTED to prevent "
            f"device instability and MediaStore corruption."
        )
        log.critical(f"[FreeSpace] {msg}")
        return {"ok": False, "hard_stop": True, "available_gb": av_gb, "message": msg}
    if available < floor_bytes:
        msg = (
            f"MID-RUN: device dropped to {av_gb:.2f} GB free, below "
            f"{floor_gb:.1f} GB floor.  Subsequent transfers may "
            f"destabilise the device.  Consider cancelling this run."
        )
        log.critical(f"[FreeSpace] {msg}")
        return {"ok": False, "available_bytes": available, "message": msg}

    # All-clear — debug log only
    log.debug(f"[FreeSpace] mid-run check OK: {av_gb:.2f} GB free")
    return {"ok": True, "available_bytes": available,
            "message": f"{av_gb:.2f} GB free"}
