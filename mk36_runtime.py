"""
mk36_runtime.py — MK36 R3: Runtime Determinism Contract
=========================================================
CTO MK35 review:
  "True replay determinism requires runtime determinism contracts.
   Floating-point rounding, regex engine behavior, JSON canonicalization,
   Python version, locale. Right now replay is operationally deterministic,
   not mathematically deterministic."

This module captures a `RuntimeFingerprint` — a SHA256 hash over the
runtime context that affects engine output. The fingerprint is embedded
in every persisted input bundle. On replay, the current runtime
fingerprint is computed and compared to the stored one; mismatches are
recorded in the DriftReport as `runtime_drift` so an operator knows
whether observed differences are due to code changes or runtime changes.

What we capture:
  - Python version (sys.version_info as tuple)
  - Byte order (sys.byteorder)
  - Locale (locale.getlocale() with normalization for None)
  - Hash of mk34_*.py and mk35_*.py and mk36_*.py source files
  - Hash of mk35_default_policy.json
  - Canonical JSON form (sample) for canonicalization stability check

What we DON'T capture (yet):
  - Installed-package versions (too noisy, scope creep)
  - LD_PRELOAD / loaded shared libs (Linux-specific, brittle)
  - Filesystem encoding (assumed UTF-8 throughout)
"""

from __future__ import annotations

import hashlib
import json
import locale
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. CANONICAL JSON HELPER (R3-T3) — used by audit + bundles + fingerprint
# ═══════════════════════════════════════════════════════════════════════════

def canonical_json(obj) -> str:
    """
    Single canonical-form JSON serializer used by all replay-relevant
    persistence. Locale-independent, deterministic, ASCII-safe.

    Replaces drift between MK34 audit log writes (which used default
    json.dumps) and MK35 bundle writes (also default). MK36 routes
    everything through this.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )


# Sample object used to detect canonicalization drift across runtimes.
# Contains: nested dict, list, special floats (handled by default=str),
# unicode escape (forced to \uXXXX by ensure_ascii), boolean, null.
_CANONICAL_SAMPLE = {
    "z": [3, 1, 2],
    "a": {"x": True, "y": None, "name": "café"},
    "b": 1.5,
    "c": "hello",
}


# ═══════════════════════════════════════════════════════════════════════════
# 2. RUNTIME FINGERPRINT (R3-T1)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RuntimeFingerprint:
    """
    A deterministic capture of the runtime context that affects engine output.
    Frozen, JSON-serializable, hashable.
    """
    python_version:        Tuple[int, int, int]
    byte_order:            str        # "little" or "big"
    locale_str:            str        # canonical locale name
    module_hashes_sha256:  str        # SHA256 over all mk34/35/36 .py sources
    policy_file_sha256:    Optional[str]  # SHA256 of mk35_default_policy.json
    canonical_form_sha256: str        # SHA256 of canonical_json(_CANONICAL_SAMPLE)
    fingerprint_sha256:    str        # SHA256 of the above as canonical JSON

    def to_dict(self) -> dict:
        return {
            "pythonVersion":         list(self.python_version),
            "byteOrder":             self.byte_order,
            "localeStr":             self.locale_str,
            "moduleHashesSha256":    self.module_hashes_sha256,
            "policyFileSha256":      self.policy_file_sha256,
            "canonicalFormSha256":   self.canonical_form_sha256,
            "fingerprintSha256":     self.fingerprint_sha256,
        }

    def matches(self, other: "RuntimeFingerprint",
                strict: bool = False) -> bool:
        """
        Compare two fingerprints. `strict=True` requires bit-for-bit equality
        of fingerprint_sha256. `strict=False` accepts when only the locale
        differs (a common operational case).
        """
        if strict:
            return self.fingerprint_sha256 == other.fingerprint_sha256
        # Lenient: ignore locale_str
        return (self.python_version == other.python_version
                and self.byte_order == other.byte_order
                and self.module_hashes_sha256 == other.module_hashes_sha256
                and self.policy_file_sha256 == other.policy_file_sha256
                and self.canonical_form_sha256 == other.canonical_form_sha256)


def _hash_files(paths: Iterable[Path]) -> str:
    """Cumulative SHA256 over a sequence of file contents (sorted by path)."""
    sorted_paths = sorted(paths)
    h = hashlib.sha256()
    for p in sorted_paths:
        try:
            with p.open("rb") as fh:
                # Include the path name in the hash so file renames change it
                h.update(p.name.encode("utf-8"))
                h.update(b"\x00")
                while True:
                    chunk = fh.read(1 << 16)
                    if not chunk: break
                    h.update(chunk)
                h.update(b"\xff")
        except Exception as e:
            log.warning(f"  [MK36/R3] file hash failed for {p}: {e}")
    return h.hexdigest()


def _normalize_locale() -> str:
    """
    Get a normalized locale string. locale.getlocale() can return (None, None)
    on POSIX containers where LC_ALL isn't set; we normalize to "C" in that
    case so the fingerprint is stable across container-vs-host runs.
    """
    try:
        loc = locale.getlocale()
        if loc == (None, None) or loc is None:
            return "C"
        return f"{loc[0] or 'C'}.{loc[1] or 'UTF-8'}"
    except Exception:
        return "C"


def compute_runtime_fingerprint(
    project_dir: Path | str | None = None,
) -> RuntimeFingerprint:
    """
    Compute the current runtime fingerprint.

    Args:
        project_dir: directory containing the mk34/35/36 modules. Defaults
            to the directory containing this file.
    """
    pd = Path(project_dir) if project_dir else Path(__file__).parent

    # Module hashes — cover mk34/35/36 .py + mk34/35/36 helpers
    module_paths = []
    for prefix in ("mk34_", "mk35_", "mk36_"):
        module_paths.extend(pd.glob(f"{prefix}*.py"))
    module_hashes = _hash_files(module_paths)

    # Policy file hash (may not exist)
    policy_path = pd / "mk35_default_policy.json"
    policy_hash = None
    if policy_path.exists():
        with policy_path.open("rb") as fh:
            policy_hash = hashlib.sha256(fh.read()).hexdigest()

    # Canonical JSON form sample
    canonical_sample = canonical_json(_CANONICAL_SAMPLE)
    canonical_form_hash = hashlib.sha256(canonical_sample.encode()).hexdigest()

    # Build fingerprint and hash it
    body = {
        "pythonVersion":      list(sys.version_info[:3]),
        "byteOrder":          sys.byteorder,
        "localeStr":          _normalize_locale(),
        "moduleHashesSha256": module_hashes,
        "policyFileSha256":   policy_hash,
        "canonicalFormSha256": canonical_form_hash,
    }
    fp_hash = hashlib.sha256(canonical_json(body).encode()).hexdigest()

    return RuntimeFingerprint(
        python_version       = tuple(sys.version_info[:3]),
        byte_order           = sys.byteorder,
        locale_str           = _normalize_locale(),
        module_hashes_sha256 = module_hashes,
        policy_file_sha256   = policy_hash,
        canonical_form_sha256= canonical_form_hash,
        fingerprint_sha256   = fp_hash,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. DRIFT DESCRIPTION (R3-T2 helper)
# ═══════════════════════════════════════════════════════════════════════════

def describe_runtime_drift(
    stored: RuntimeFingerprint,
    current: RuntimeFingerprint,
) -> "list[str]":
    """
    Return a list of human-readable drift descriptions between two
    fingerprints. Empty list = no drift.
    """
    drifts: list = []
    if stored.python_version != current.python_version:
        drifts.append(
            f"python_version: stored={stored.python_version} current={current.python_version}"
        )
    if stored.byte_order != current.byte_order:
        drifts.append(
            f"byte_order: stored={stored.byte_order} current={current.byte_order}"
        )
    if stored.locale_str != current.locale_str:
        drifts.append(
            f"locale: stored={stored.locale_str} current={current.locale_str}"
        )
    if stored.module_hashes_sha256 != current.module_hashes_sha256:
        drifts.append(
            f"module_hashes: stored={stored.module_hashes_sha256[:16]}... "
            f"current={current.module_hashes_sha256[:16]}..."
        )
    if stored.policy_file_sha256 != current.policy_file_sha256:
        drifts.append(
            f"policy_file: stored={stored.policy_file_sha256 and stored.policy_file_sha256[:16]}... "
            f"current={current.policy_file_sha256 and current.policy_file_sha256[:16]}..."
        )
    if stored.canonical_form_sha256 != current.canonical_form_sha256:
        drifts.append(
            f"canonical_form: stored={stored.canonical_form_sha256[:16]}... "
            f"current={current.canonical_form_sha256[:16]}..."
        )
    return drifts


__all__ = [
    "canonical_json",
    "RuntimeFingerprint",
    "compute_runtime_fingerprint",
    "describe_runtime_drift",
]
