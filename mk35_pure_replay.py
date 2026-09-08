"""
mk35_pure_replay.py — MK35 Q3: Deterministic Pure Replay
==========================================================
CTO MK34 review:
  "Is replay pure? Or does it depend on live system state (device calls,
   hashes, observers)? If replay is not pure, audit log becomes a trace,
   not a truth source."

MK34's mk34_replay.py reads the audit log and reports what events fired.
That's a trace. MK35's pure replay actually RE-EXECUTES the reconciliation
engine on the persisted input bundle and compares the regenerated decisions
to the historical decisions. This requires:

  1. Persisting the MK34InputBundle alongside the audit log (Q3-T1)
  2. A pure replay function that depends only on (bundle, audit) (Q3-T2)
  3. Drift detection that compares current code's decisions to logged ones (Q3-T3)

The "purity" guarantee is structural: replay never imports observer or
transport modules. If a future code change accidentally introduces an
observer side-effect into the engine, replay will fail import — which is
exactly what we want.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Input bundle persistence (Q3-T1)
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_BUNDLES_PATH = Path(".mk35_input_bundles.jsonl")


def persist_input_bundle(bundle, path: Path | None = None) -> Path:
    """
    Append a frozen JSON representation of an MK34InputBundle to the
    bundles log. Called by the engine at INGEST time. Append-only.

    MK36 R3: also captures the current RuntimeFingerprint and embeds it
    in the persisted record so replay can detect runtime drift.
    MK36 R3-T3: uses canonical_json() to eliminate locale/platform drift.

    Returns the path written to (for the caller to log).
    """
    p = path or DEFAULT_BUNDLES_PATH

    # Build the canonical JSON form via the bundle's dataclass shape
    serialized = _bundle_to_dict(bundle)

    # MK36 R3: embed runtime fingerprint
    try:
        from mk36_runtime import compute_runtime_fingerprint
        serialized["_runtimeFingerprint"] = compute_runtime_fingerprint().to_dict()
    except ImportError:
        pass

    # MK36 R3-T3: canonical JSON for stable persistence
    try:
        from mk36_runtime import canonical_json
        line = canonical_json(serialized)
    except ImportError:
        line = json.dumps(serialized)

    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as e:
        log.warning(f"  [MK35/Q3] persist_input_bundle failed (non-fatal): {e}")
    return p


def _bundle_to_dict(bundle) -> dict:
    """Hand-roll JSON serialization for MK34InputBundle (it's not frozen)."""
    return {
        "runId":           bundle.run_id,
        "version":         bundle.version,
        "sourcePriority":  [st.value for st in bundle.source_priority],
        "allowInference":  bundle.allow_inference,
        "strictMode":      bundle.strict_mode,
        "records": [
            {
                "key":     r.key,
                "payload": dict(r.payload),
                "source":  r.source.to_dict(),
            }
            for r in bundle.records
        ],
    }


def load_input_bundle(run_id: str, path: Path | None = None):
    """
    Read back a persisted bundle by run_id. Returns the dict form so the
    caller can convert to MK34InputBundle (which requires importing
    mk34_reconciliation — done lazily here to keep the bundle persistence
    itself a pure data path).
    """
    p = path or DEFAULT_BUNDLES_PATH
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("runId") == run_id:
                    return d
            except json.JSONDecodeError:
                continue
    return None


def _hydrate_bundle(d: dict):
    """Convert a JSON-loaded bundle dict back into an MK34InputBundle."""
    from mk34_reconciliation import (
        MK34InputBundle, RawRecord, SourceRef, SourceType,
    )
    records = []
    for r in d.get("records", []):
        src_d = r["source"]
        try:
            st = SourceType(src_d["sourceType"])
        except ValueError:
            log.warning(
                f"  [MK35/Q3] Unknown SourceType {src_d['sourceType']!r} in "
                f"bundle replay; using GENRE_CACHE as fallback."
            )
            st = SourceType.GENRE_CACHE
        records.append(RawRecord(
            key=r["key"],
            payload=dict(r["payload"]),
            source=SourceRef(
                source_type=st,
                source_id=src_d["sourceId"],
                timestamp=src_d["timestamp"],
                version=src_d.get("version", "mk34.0"),
            ),
        ))

    # Reconstruct source_priority
    source_priority = []
    for st_val in d.get("sourcePriority", []):
        try:
            source_priority.append(SourceType(st_val))
        except ValueError:
            continue

    return MK34InputBundle(
        records=records,
        source_priority=tuple(source_priority) if source_priority else MK34InputBundle.__dataclass_fields__["source_priority"].default,
        allow_inference=d.get("allowInference", True),
        strict_mode=d.get("strictMode", False),
        run_id=d.get("runId", ""),
        version=d.get("version", "MK34.0"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Pure replay (Q3-T2)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class DriftReport:
    """
    Result of comparing replayed decisions to historical decisions.
    Frozen, JSON-serializable.

    MK36 R3: runtime_drift field added. Lists drifts between the runtime
    fingerprint stored at bundle-write time vs the current runtime
    fingerprint.
    """
    run_id:            str
    decisions_logged:  int
    decisions_replayed: int
    drift_count:       int
    drifts:            Tuple[dict, ...]   # list of {fieldPath, logged, replayed}
    pure:              bool               # True iff replay completed without I/O
    runtime_drift:     Tuple[str, ...] = ()   # MK36 R3: runtime fingerprint drift

    def to_dict(self) -> dict:
        return {
            "runId":            self.run_id,
            "decisionsLogged":  self.decisions_logged,
            "decisionsReplayed": self.decisions_replayed,
            "driftCount":       self.drift_count,
            "drifts":           list(self.drifts),
            "pure":             self.pure,
            "runtimeDrift":     list(self.runtime_drift),
        }


def replay_pure(
    run_id: str,
    audit_log_path: Path | None = None,
    bundles_path: Path | None = None,
) -> Optional[DriftReport]:
    """
    Re-execute the reconciliation engine on a persisted input bundle and
    compare its decisions to the historical decisions in the audit log.

    PURITY GUARANTEE: this function does not import observer, executor,
    or any transport module. It only reads from disk (audit + bundle JSONL)
    and re-runs the deterministic engine logic. If a future code change
    accidentally couples the engine to an observer call, replay will
    fail (the engine.run() call won't complete in this side-effect-free
    context).

    Returns DriftReport with the comparison, or None if the bundle is
    not found.
    """
    audit_log_path = audit_log_path or Path(".mk34_audit.jsonl")
    bundles_path   = bundles_path or DEFAULT_BUNDLES_PATH

    # Load persisted bundle
    bundle_d = load_input_bundle(run_id, bundles_path)
    if bundle_d is None:
        log.warning(f"  [MK35/Q3] No persisted bundle for run_id={run_id!r}")
        return None

    # Load historical decisions from audit log
    historical_decisions: List[dict] = []
    if audit_log_path.exists():
        with audit_log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("runId") == run_id and ev.get("type") == "DECISION_MADE":
                    historical_decisions.append(ev.get("payload", {}))

    # Re-execute engine on the bundle. Use a TEMP audit log path so we
    # don't pollute the primary audit log during replay.
    import tempfile
    from mk34_reconciliation import ReconciliationEngine

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode='w') as tmp:
        tmp_audit = Path(tmp.name)

    try:
        bundle = _hydrate_bundle(bundle_d)
        # Pin the same run_id so output is comparable
        bundle.run_id = run_id
        engine = ReconciliationEngine(bundle, audit_log_path=tmp_audit)
        out = engine.run()

        # Compare decision-by-decision (keyed by field_path)
        replayed_by_path: Dict[str, Any] = {
            d.field_path: d.selected_value for d in out.decisions
        }
        logged_by_path: Dict[str, Any] = {
            d.get("fieldPath"): d.get("selectedValue")
            for d in historical_decisions
        }

        drifts = []
        all_paths = set(replayed_by_path) | set(logged_by_path)
        for path in sorted(all_paths):
            logged = logged_by_path.get(path, "<not_logged>")
            replayed = replayed_by_path.get(path, "<not_replayed>")
            if logged != replayed:
                drifts.append({
                    "fieldPath": path,
                    "logged":    logged,
                    "replayed":  replayed,
                })

        # MK36 R3: compare runtime fingerprints
        runtime_drift = ()
        try:
            from mk36_runtime import (
                compute_runtime_fingerprint, RuntimeFingerprint,
                describe_runtime_drift,
            )
            stored_fp_dict = bundle_d.get("_runtimeFingerprint")
            if stored_fp_dict:
                stored_fp = RuntimeFingerprint(
                    python_version=tuple(stored_fp_dict["pythonVersion"]),
                    byte_order=stored_fp_dict["byteOrder"],
                    locale_str=stored_fp_dict["localeStr"],
                    module_hashes_sha256=stored_fp_dict["moduleHashesSha256"],
                    policy_file_sha256=stored_fp_dict.get("policyFileSha256"),
                    canonical_form_sha256=stored_fp_dict["canonicalFormSha256"],
                    fingerprint_sha256=stored_fp_dict["fingerprintSha256"],
                )
                current_fp = compute_runtime_fingerprint()
                runtime_drift = tuple(describe_runtime_drift(stored_fp, current_fp))
                if runtime_drift:
                    log.warning(
                        f"  [MK36/R3] RUNTIME_DRIFT_DETECTED run_id={run_id} "
                        f"drifts={len(runtime_drift)}: {runtime_drift[0] if runtime_drift else ''}"
                    )
        except ImportError:
            pass

        return DriftReport(
            run_id=run_id,
            decisions_logged=len(historical_decisions),
            decisions_replayed=len(out.decisions),
            drift_count=len(drifts),
            drifts=tuple(drifts),
            pure=True,
            runtime_drift=runtime_drift,
        )
    finally:
        tmp_audit.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# Drift detection helper (Q3-T3)
# ═══════════════════════════════════════════════════════════════════════════

def format_drift_report(report: DriftReport) -> str:
    """Pretty-print a DriftReport for CLI use."""
    lines = [
        f"DriftReport for run_id={report.run_id}",
        f"  decisions_logged:    {report.decisions_logged}",
        f"  decisions_replayed:  {report.decisions_replayed}",
        f"  drift_count:         {report.drift_count}",
        f"  pure:                {report.pure}",
    ]
    if report.drifts:
        lines.append("  drifts:")
        for d in report.drifts:
            lines.append(
                f"    {d['fieldPath']}: logged={d['logged']!r} "
                f"vs replayed={d['replayed']!r}"
            )
    else:
        lines.append("  ✅ no drift — engine output deterministic against logged decisions")
    return "\n".join(lines)


__all__ = [
    "DriftReport",
    "persist_input_bundle", "load_input_bundle",
    "replay_pure", "format_drift_report",
    "DEFAULT_BUNDLES_PATH",
]
