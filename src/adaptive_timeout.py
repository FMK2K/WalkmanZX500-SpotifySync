"""
adaptive_timeout.py — MK17 Patch 12
=====================================
Centralised, size-and-type-aware timeout calculation.

Pre-MK17 P12, every transfer/operation used a fixed timeout: ADB push
was 120s regardless of file size; the reorganize-side delete was 60s
regardless of format; PowerShell upload was 120s for everything.

Apr 30 / May 2 user direction:  "shouldnt all our timeouts be custom
functions of filetype & size".  Yes — a 4MB MP3 doesn't need 60s, and
a 50MB FLAC might.

Design:
    timeout_for(operation, size_bytes=None, ext=None) -> float seconds

    For each operation, we have:
      - a base (the floor — for tiny files)
      - a per-MB rate (how much extra time per MB of payload)
      - a ceiling (the absolute cap, never exceed this)
      - a format multiplier (FLAC takes ~3× longer to transfer over
        USB than MP3 because of bitrate; M4A is roughly equal to MP3)

The result is `min(ceiling, base + per_mb * size_mb * format_mult)`,
clipped at `base` for unknown sizes.

Operations:
    "adb_push"   — adb push to device
    "com_copy"   — Shell.Application CopyHere into MTP namespace
    "com_delete" — Shell rename + MoveHere to temp + os.remove
    "ps_upload"  — PowerShell -NonInteractive Shell.Application copy
    "verify"     — post-transfer size verification round-trip

All functions read from config so values can be tuned without code
changes.  Defaults are calibrated to the Sony NW-ZX500 + USB 2.0
typical-case observed in LOG_17.
"""
from __future__ import annotations

# Fallback defaults if config is unavailable
_DEFAULT_TABLES: dict[str, dict] = {
    "adb_push":   {"base": 30,  "per_mb": 1.5, "ceiling": 600},
    "com_copy":   {"base": 60,  "per_mb": 4.0, "ceiling": 900},
    "com_delete": {"base": 15,  "per_mb": 0.5, "ceiling": 90},
    "ps_upload":  {"base": 60,  "per_mb": 4.0, "ceiling": 900},
    "verify":     {"base": 5,   "per_mb": 0.2, "ceiling": 60},
}

# Format multipliers — FLAC files are ~3-4× the size of MP3 at typical
# bitrates, but the per-MB transfer time is similar.  The multiplier
# captures format-specific overhead (lossless tag overhead, codec
# verification, etc).
_FORMAT_MULT: dict[str, float] = {
    ".flac":  1.5,   # lossless: longer cover-art parse, larger files
    ".wav":   1.4,
    ".mp3":   1.0,   # baseline
    ".m4a":   1.1,   # MP4 container parse overhead
    ".aac":   1.0,
    ".ogg":   1.1,
    ".opus":  1.0,
    ".wma":   1.2,
}


def _table(operation: str) -> dict:
    """Pull the timeout table from config if possible, else default."""
    try:
        import config
        cfg_tables = getattr(config, "ADAPTIVE_TIMEOUT_TABLES", None)
        if isinstance(cfg_tables, dict) and operation in cfg_tables:
            return cfg_tables[operation]
    except Exception:
        pass
    return _DEFAULT_TABLES.get(operation, _DEFAULT_TABLES["adb_push"])


def _format_mult(ext: str | None) -> float:
    if not ext:
        return 1.0
    return _FORMAT_MULT.get(ext.lower(), 1.0)


def timeout_for(operation: str,
                size_bytes: int | None = None,
                ext: str | None = None,
                multiplier: float = 1.0) -> float:
    """
    Compute an adaptive timeout in seconds.

    operation: one of "adb_push", "com_copy", "com_delete", "ps_upload",
               "verify" (anything else falls back to adb_push table).
    size_bytes: file size; if None, returns the base timeout.
    ext:        file extension (".flac", ".mp3", ...) for the format
                multiplier.  Case-insensitive.
    multiplier: caller-supplied scalar (e.g. 2.0 for retry attempts
                that should be more patient).

    Returns a float (seconds).
    """
    table = _table(operation)
    base   = float(table.get("base",   30))
    per_mb = float(table.get("per_mb", 1.5))
    ceil_  = float(table.get("ceiling", 600))

    if size_bytes is None:
        return min(ceil_, base * multiplier)

    size_mb = max(0.0, size_bytes) / (1024.0 * 1024.0)
    fmult   = _format_mult(ext)
    raw     = base + per_mb * size_mb * fmult
    return min(ceil_, raw * multiplier)


# Convenience wrappers — use these in the actual call sites for clarity
def adb_push_timeout(size_bytes: int | None = None,
                     ext: str | None = None) -> float:
    return timeout_for("adb_push", size_bytes, ext)


def com_copy_timeout(size_bytes: int | None = None,
                     ext: str | None = None) -> float:
    return timeout_for("com_copy", size_bytes, ext)


def com_delete_timeout(size_bytes: int | None = None,
                       ext: str | None = None) -> float:
    return timeout_for("com_delete", size_bytes, ext)


def ps_upload_timeout(size_bytes: int | None = None,
                      ext: str | None = None) -> float:
    return timeout_for("ps_upload", size_bytes, ext)


def verify_timeout(size_bytes: int | None = None,
                   ext: str | None = None) -> float:
    return timeout_for("verify", size_bytes, ext)
