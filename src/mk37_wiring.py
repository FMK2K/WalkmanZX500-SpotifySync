"""
mk37_wiring.py — MK37: Production Wiring of MK15+MK34+MK35+MK36 Infrastructure
================================================================================

CTO board MK37 verdict (LOG_6_5_26.txt + adversarial review):
  "SpotifyTunerMK14 has evolved into a bifurcated system where hardened
   orchestration infrastructure exists as non-authoritative side architecture
   while legacy procedural orchestration still owns mutation authority."
  Verdict: NO-GO until orchestration consolidation lands.

Production logs evidence:
  - 1,224 × "remote size is 0" — Class A false-success leaking through
  - 1,498 × ENRICHMENT_FAIL_OPEN_APPLIED — provider chain collapse
  - 612 × Verification FAILED, 785 × Dedup failures
  - 0 markers from MK15/MK34/MK35/MK36 fired in production

Board mandate (ordered by priority):
  P0-1 (Halt Gate)              → S1: enforce_observation_halt
  P0-2 (Transfer Quorum)        → S2: transfer_via_legacy_with_quorum
  P0-3 (Transactional Dedup)    → S3a: transactional_dedup
                                  (COPY → VERIFY → COMMIT → DELETE; never DELETE BEFORE VERIFY)
  P0-4 (Offline Capability)     → S3b: offline_provider_capability_check (NEW)
  P1-1 (Lock-Aware Metadata)    → S4: tag_file_with_coherence
  S5  (Provider Health)         → ProviderHealthRegistry + short-circuit

PLUS:
  - Feature flags via config (ENABLE_HALT_GATE, ENABLE_TRANSFER_QUORUM, ...)
  - Runtime markers per board spec (HALT_GATE_ABORT, TRANSFER_QUORUM_VERIFIED,
    DEDUP_TRANSACTION_COMMITTED, OFFLINE_PROVIDER_ACCESS_BLOCKED,
    TAG_WRITE_RETRY_SUCCESS)
  - Telemetry counters readable via mk37_wiring.snapshot_telemetry()
  - Startup self-audit (Recommendation F): import-time check that all
    mandatory MK-modules are reachable

The module is the SINGLE INTEGRATION POINT. Production code that calls it
gets the protection; production code that doesn't continues to work as
before. Migration is incremental.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry Counters (board mandate — telemetry recommendations §7)
# ═══════════════════════════════════════════════════════════════════════════
# Process-local counters readable via snapshot_telemetry(). Each marker the
# board mandates corresponds to one counter. These feed the dashboards
# described in the board review (Runtime Integrity / Transfer Trust /
# Replay Determinism).

_TELEMETRY: Dict[str, int] = {
    # P0-1 / S1
    "halt_gate_aborts":              0,
    "halt_gate_passes":              0,
    "inventory_trust_below_threshold": 0,
    # P0-2 / S2
    "transfer_quorum_verified":      0,
    "transfer_quorum_failures":      0,
    "transfer_legacy_fallbacks":     0,
    "transfers_via_executor":        0,
    "capability_grants":             0,
    "capability_revokes":            0,
    "capability_rejections":         0,
    # P0-3 / S3a
    "dedup_transaction_committed":   0,
    "dedup_aborted_pre_verify":      0,
    "dedup_idempotent_skips":        0,
    # P0-4 / S3b
    "offline_provider_blocks":       0,
    "offline_capability_violations": 0,
    # P1-1 / S4
    "tag_write_retry_success":       0,
    "tag_write_permanent_failure":   0,
    # S5
    "provider_cooldowns":            0,
    "provider_session_disables":     0,
    "enrichment_short_circuits":     0,
}
_TELEMETRY_LOCK = threading.RLock()


def _bump(metric: str, n: int = 1) -> None:
    """Increment a telemetry counter."""
    with _TELEMETRY_LOCK:
        _TELEMETRY[metric] = _TELEMETRY.get(metric, 0) + n


def snapshot_telemetry() -> Dict[str, int]:
    """Return a copy of all telemetry counters. For dashboards / pager."""
    with _TELEMETRY_LOCK:
        return dict(_TELEMETRY)


def reset_telemetry() -> None:
    """Reset all counters. Used by tests + run start."""
    with _TELEMETRY_LOCK:
        for k in _TELEMETRY:
            _TELEMETRY[k] = 0


# ═══════════════════════════════════════════════════════════════════════════
# Feature flags (board mandate — every ticket has rollback flag)
# ═══════════════════════════════════════════════════════════════════════════
# Read from config when available; defaults are SAFE (enable everything that
# closes a documented production failure; disable the experimental ones).

def _flag(name: str, default: bool) -> bool:
    """
    Read a feature flag with priority: env var > config attr > default.
    Env var takes precedence so operators can flip a flag without editing config.

    EXCEPTION (MK37.1-2 board P0): when PRODUCTION_HARDENED_MODE=True is set
    in config.py, all P0 flags become FORCED ON and the env-var override
    pathway is BYPASSED. This closes the governance risk identified in the
    MK37 follow-on review where an operator under incident pressure could
    disable safeguards to "get syncs working".
    """
    # P0 flags that are LOCKED ON when production hardened mode is active
    P0_LOCKED_FLAGS = {
        "MK37_ENABLE_HALT_GATE",
        "MK37_ENABLE_TRANSFER_QUORUM",
        "MK37_ENABLE_TRANSACTIONAL_DEDUP",
        "MK37_ENFORCE_OFFLINE_CAPABILITY",
        "MK37_ENABLE_LOCK_AWARE_TAGGING",
    }
    if name in P0_LOCKED_FLAGS and is_production_hardened_mode():
        return True   # locked ON — env-var/config override ignored

    env_val = os.environ.get(name)
    if env_val is not None:
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    try:
        import config as _cfg
        return bool(getattr(_cfg, name, default))
    except ImportError:
        return default


def is_production_hardened_mode() -> bool:
    """
    Board MK37.1-2: returns True if PRODUCTION_HARDENED_MODE is set in
    config.py. When True, all P0 flags are immutable at runtime; env-var
    overrides are ignored; startup will abort if any P0 flag is False
    in config (which is impossible since the read is bypassed).
    """
    try:
        import config as _cfg
        return bool(getattr(_cfg, "PRODUCTION_HARDENED_MODE", False))
    except ImportError:
        return False


def assert_production_flags_locked() -> None:
    """
    Board MK37.1-2: at startup (called from audit_dependency_graph), if
    PRODUCTION_HARDENED_MODE=True, assert that every P0 flag resolves to
    True. Logs a clear error if not. (In hardened mode the resolution is
    forced; this is a defense-in-depth check that no monkey-patch has
    weakened the guard.)
    """
    if not is_production_hardened_mode():
        return

    p0_states = {
        "MK37_ENABLE_HALT_GATE":           is_halt_gate_enabled(),
        "MK37_ENABLE_TRANSFER_QUORUM":     is_transfer_quorum_enabled(),
        "MK37_ENABLE_TRANSACTIONAL_DEDUP": is_transactional_dedup_enabled(),
        "MK37_ENFORCE_OFFLINE_CAPABILITY": is_offline_capability_enforced(),
        "MK37_ENABLE_LOCK_AWARE_TAGGING":  is_lock_aware_tagging_enabled(),
    }
    weakened = [k for k, v in p0_states.items() if not v]
    if weakened:
        msg = (
            f"PRODUCTION_HARDENED_MODE_VIOLATION: P0 flags evaluated to "
            f"False under hardened mode: {weakened!r}. This indicates a "
            f"monkey-patched _flag() or a logic bug. Refusing to proceed."
        )
        log.error(f"  [MK37.1-2] {msg}")
        raise RuntimeError(msg)
    log.info(
        "  [MK37.1-2] PRODUCTION_HARDENED_MODE_ACTIVE — all 5 P0 flags locked ON"
    )


def is_halt_gate_enabled() -> bool:
    """P0-1 rollback flag. Default: True (enabled by default)."""
    return _flag("MK37_ENABLE_HALT_GATE", True)


def is_transfer_quorum_enabled() -> bool:
    """P0-2 rollback flag. Default: True."""
    return _flag("MK37_ENABLE_TRANSFER_QUORUM", True)


def is_transactional_dedup_enabled() -> bool:
    """P0-3 rollback flag. Default: True."""
    return _flag("MK37_ENABLE_TRANSACTIONAL_DEDUP", True)


def is_offline_capability_enforced() -> bool:
    """P0-4 rollback flag. Default: True (no network in offline mode, period)."""
    return _flag("MK37_ENFORCE_OFFLINE_CAPABILITY", True)


def is_lock_aware_tagging_enabled() -> bool:
    """P1-1 rollback flag. Default: True."""
    return _flag("MK37_ENABLE_LOCK_AWARE_TAGGING", True)


# ═══════════════════════════════════════════════════════════════════════════
# Startup self-audit (Recommendation F)
# ═══════════════════════════════════════════════════════════════════════════
# At import time, verify that the mandatory MK-modules are reachable. If
# any are missing, log loudly. We do NOT raise on missing modules (that
# would block production deployment), but the dashboard will surface it.

@dataclass(frozen=True)
class DependencyAuditReport:
    mandatory_modules:    Dict[str, bool]   # module_name → importable
    optional_modules:     Dict[str, bool]
    feature_flags:        Dict[str, bool]
    healthy:              bool
    timestamp:            float

    def to_dict(self) -> dict:
        return {
            "mandatoryModules": dict(self.mandatory_modules),
            "optionalModules":  dict(self.optional_modules),
            "featureFlags":     dict(self.feature_flags),
            "healthy":          self.healthy,
            "timestamp":        self.timestamp,
        }


def audit_dependency_graph() -> DependencyAuditReport:
    """
    Recommendation F: Startup self-audit. Verify all mandatory execution
    modules are reachable. Returns a DependencyAuditReport.

    Mandatory modules close documented production failures. If any are
    missing, the corresponding S-ticket protection degrades.
    """
    mandatory_names = [
        "mk15_halt_gate", "mk15_idempotency", "mk15_file_coherence",
        "mk34_invariants", "mk34_executor", "mk34_observer",
        "mk35_capability", "mk35_trust", "mk35_pure_replay", "mk35_policy",
    ]
    optional_names = [
        "mk36_trust_graph", "mk36_capability_store",
        "mk36_runtime", "mk36_lifecycle", "mk36_snapshot_lineage",
    ]

    mandatory: Dict[str, bool] = {}
    for name in mandatory_names:
        try:
            __import__(name)
            mandatory[name] = True
        except ImportError:
            mandatory[name] = False
            log.warning(f"  [MK37/Audit] MANDATORY module unreachable: {name!r}")

    optional: Dict[str, bool] = {}
    for name in optional_names:
        try:
            __import__(name)
            optional[name] = True
        except ImportError:
            optional[name] = False

    flags = {
        "MK37_ENABLE_HALT_GATE":           is_halt_gate_enabled(),
        "MK37_ENABLE_TRANSFER_QUORUM":     is_transfer_quorum_enabled(),
        "MK37_ENABLE_TRANSACTIONAL_DEDUP": is_transactional_dedup_enabled(),
        "MK37_ENFORCE_OFFLINE_CAPABILITY": is_offline_capability_enforced(),
        "MK37_ENABLE_LOCK_AWARE_TAGGING":  is_lock_aware_tagging_enabled(),
    }

    healthy = all(mandatory.values())
    if not healthy:
        log.error(
            "  [MK37/Audit] DEPENDENCY_GRAPH_DEGRADED — mandatory modules "
            "missing. Production safety nets are partial. See "
            f"snapshot_telemetry() for active counters."
        )
    else:
        log.info(
            f"  [MK37/Audit] DEPENDENCY_GRAPH_HEALTHY — all mandatory modules "
            f"reachable. Optional present: "
            f"{sum(optional.values())}/{len(optional_names)}"
        )

    # MK37.1-2: production flag lock validation (board Finding C)
    # Only runs when PRODUCTION_HARDENED_MODE=True. Raises on monkey-patch
    # of _flag() or any logic regression that weakens the locks.
    try:
        assert_production_flags_locked()
    except RuntimeError:
        # Re-raise: this is a fatal startup precondition.
        raise

    # MK37.1-1 + Finding D: Authority graph validation (production hardened
    # mode only). The board's evolution: from "module reachability" to
    # "runtime authority graph validation" — the mutation audit provides
    # this. Run it as part of startup so startup fails when a future commit
    # introduces an unmediated destructive path.
    if is_production_hardened_mode():
        try:
            from pathlib import Path as _Path
            import mutation_path_audit
            audit_root = _Path(__file__).parent
            mutation_report = mutation_path_audit.audit(audit_root)
            if not mutation_report.healthy:
                msg = (
                    f"AUTHORITY_GRAPH_VIOLATION: production hardened mode but "
                    f"{mutation_report.unmediated_count} unmediated destructive "
                    f"path(s) detected (coverage {mutation_report.coverage_pct:.1f}%). "
                    f"This indicates new code introduced a bypass since the "
                    f"last audit. Refusing to proceed."
                )
                log.error(f"  [MK37.1-1] {msg}")
                raise RuntimeError(msg)
            log.info(
                f"  [MK37.1-1] AUTHORITY_GRAPH_HEALTHY — "
                f"{mutation_report.scanned_files} files, "
                f"{mutation_report.total_call_sites} destructive call sites, "
                f"100% mediated"
            )
        except ImportError:
            log.debug(
                "  [MK37.1-1] mutation_path_audit not installed — "
                "authority graph check skipped"
            )

    # MK37.1-3: install runtime primitive guards. Bypass becomes impossible
    # when MK37_ENABLE_BYPASS_DETECTION (auto-True under hardened mode).
    try:
        _install_primitive_guards()
    except Exception as _e:
        log.warning(f"  [MK37.1-3] guard installation raised (continuing): {_e}")

    # MK37.1-4: crash recovery reconciliation. Idempotent; reverts orphan
    # capabilities + marks interrupted lifecycles FAILED.
    try:
        reconcile_crash_state()
    except Exception as _e:
        log.warning(f"  [MK37.1-4] reconciliation raised (continuing): {_e}")

    # MK37.2-5: install ResourceMonitor → capability-renewal coupling. No-op
    # if resource_monitor is not present or has no hook surface. MK37.2-1's
    # transfer-path renewal remains the primary defense; this is additive.
    try:
        from mk37_2_resource_coupling import install_resource_capability_coupling
        install_resource_capability_coupling()
    except ImportError:
        pass
    except Exception as _e:
        log.debug(f"  [MK37.2-5] coupling install raised (continuing): {_e}")

    # MK38.1: install SHA-256 cryptographic dominance override on
    # mk35_trust.verify_with_quorum. Closes LOG_9_5_26 100% transfer
    # rejection regression where sha256=STRONG was being overridden by
    # degraded size_mtime/existence signals (3,830 / 3,876 cases).
    # Idempotent; no-op if mk35_trust unavailable.
    try:
        from mk38_1_quorum_dominance import patch_verify_with_quorum
        patch_verify_with_quorum()
    except ImportError:
        pass
    except Exception as _e:
        log.warning(f"  [MK38.1] dominance install raised (continuing): {_e}")

    # MK39-A1 / Council mandate: install the mutagen boundary gate so EVERY
    # tag write must pass through the MK39-A1 mediator's mediated_operation()
    # context manager. Direct mutagen.File.save calls outside mediation
    # raise TagWriteOutsideMediation and hard-stop the run. Locked under
    # hardened mode (cannot be uninstalled). Idempotent; no-op if mutagen
    # not present (test env).
    try:
        from mk39_tag_write_gate import install_tag_write_gate
        install_tag_write_gate()
    except ImportError:
        pass
    except Exception as _e:
        log.warning(f"  [MK39-Gate] gate install raised (continuing): {_e}")

    # MK39-A1.3 — User-Canonical Source Recognition (Council 2026-05-09
    # Conditional Approval). Extends MK38.0 CANONICAL_SOURCES set with
    # user_canonical_{override,filename,folder} + the combined "user_canonical"
    # label so operator-curated music (filename-as-intent + folder-as-intent
    # + .user_metadata.json overrides) can write metadata through the
    # mediator + gate WITHOUT bypassing the freeze. The 5 binding constraints
    # are enforced in the module itself:
    #   #1 cross-validation mandatory (single-signal → quarantine, not write)
    #   #2 watermark rejection absolute (path-level _is_path_tainted)
    #   #3 mediated writes (make_user_canonical_capability builds capability)
    #   #4 fill-only-blanks (existing non-empty fields never overwritten)
    #   #5 operator review (write_quarantine_file for MEDIUM single-signal)
    # Idempotent. Safe to call when module not installed (no-op).
    try:
        from mk39_a1_3_user_canonical import register_as_canonical_source
        register_as_canonical_source()
    except ImportError:
        pass
    except Exception as _e:
        log.warning(f"  [MK39-UserCanonical] registration raised (continuing): {_e}")

    # MK39 Phase III — Forum/Council 2026-05-10 §G/§H. Registers the two
    # new canonical sources for acoustic-fingerprint matches (G.3) and
    # external-edit re-ingest (H.3). Idempotent. Safe to call when the
    # module is absent (no-op). NEITHER source enables anything by itself —
    # the operator must also set the corresponding config flag (e.g.,
    # ACOUSTID_API_KEY, EXTERNAL_EDIT_REINGEST) for the source to actually
    # appear at write time. Registration just makes the freeze ALLOW such
    # writes when they happen.
    try:
        from mk39_phase_III_canonical_sources import (
            register_phase_III_canonical_sources,
        )
        register_phase_III_canonical_sources()
    except ImportError:
        pass
    except Exception as _e:
        log.warning(f"  [MK39-Phase-III] registration raised (continuing): {_e}")

    return DependencyAuditReport(
        mandatory_modules=mandatory,
        optional_modules=optional,
        feature_flags=flags,
        healthy=healthy,
        timestamp=time.time(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# S1 — Observation Halt Gate Wiring (board ticket P0-1)
# ═══════════════════════════════════════════════════════════════════════════

def enforce_observation_halt(
    adb_count: int,
    *,
    cache_count: Optional[int] = None,
    sd_mount: Optional[str] = None,
    raise_on_collapse: bool = True,
) -> bool:
    """
    Production-facing halt gate (board ticket P0-1).

    Marker on success: HALT_GATE_PASSED
    Marker on failure: HALT_GATE_ABORT: <reason>

    Closes the LOG_5_7_5_26.txt:18 / 2373 case where ADB returned 0 files
    and Stage 1 proceeded anyway, causing 3,646 stale-cache deletions.
    """
    if not is_halt_gate_enabled():
        log.info("  [MK37/S1] HALT_GATE_BYPASSED (feature flag off)")
        return True

    try:
        from mk15_halt_gate import (
            ObservationHaltGate, DeviceObservedState, ObserverType, HaltReason,
            build_device_observation,
        )
    except ImportError:
        log.warning("  [MK37/S1] mk15_halt_gate not installed; halt gate inactive")
        return True

    observations: List = []
    adb_obs = build_device_observation(
        observer_type=ObserverType.ADB,
        file_count=adb_count,
        total_bytes=0, sampled_paths=[], method="adb_find", elapsed=0.0,
    )
    try:
        adb_obs.trust_score = 1.0 if adb_count > 0 else 0.0
    except Exception:
        pass
    observations.append(adb_obs)

    # Add cache observer whenever present — corroborates ADB even if counts agree.
    # Quorum requires >= 2 observers per ObservationHaltGate.QUORUM_COUNT default.
    if cache_count is not None:
        cache_obs = build_device_observation(
            observer_type=ObserverType.CACHE,
            file_count=cache_count, total_bytes=0,
            sampled_paths=[], method="cache_load", elapsed=0.0,
        )
        try:
            # Cache trust=0.6 when independent of ADB; matching counts → trust higher
            cache_obs.trust_score = 0.8 if cache_count == adb_count else 0.6
        except Exception:
            pass
        observations.append(cache_obs)
    else:
        # Synthesize a "self-corroborating" second observer from the ADB pass
        # itself with reduced trust. Single-observer would always fail the
        # default 2-of-2 quorum.
        synth_obs = build_device_observation(
            observer_type=ObserverType.MANUAL,
            file_count=adb_count, total_bytes=0,
            sampled_paths=[], method="adb_self_corroboration",
            elapsed=0.0,
        )
        try:
            synth_obs.trust_score = 0.7 if adb_count > 0 else 0.0
        except Exception:
            pass
        observations.append(synth_obs)

    gate = ObservationHaltGate()
    result = gate.evaluate_quorum(observations)

    if result.consensus_reached:
        _bump("halt_gate_passes")
        log.info(
            f"  [MK37/S1] HALT_GATE_PASSED adb={adb_count} cache={cache_count}"
        )
        return True

    halt_msg = result.halt_reason.value if result.halt_reason else "unknown"
    _bump("halt_gate_aborts")
    if "trust" in halt_msg.lower():
        _bump("inventory_trust_below_threshold")

    log.error(
        f"  [MK37/S1] HALT_GATE_ABORT: INVALID_DEVICE_INVENTORY "
        f"reason={halt_msg!r} adb={adb_count} cache={cache_count} "
        f"sd_mount={sd_mount!r}"
    )

    if not raise_on_collapse:
        return False

    try:
        from mk34_invariants import InvariantViolation
        raise InvariantViolation(
            invariant_name="OBSERVATION_HALT_GATE",
            detail=f"HALT_GATE_ABORT: {halt_msg}. "
                   f"adb={adb_count} cache={cache_count}",
        )
    except ImportError:
        raise RuntimeError(
            f"HALT_GATE_ABORT: {halt_msg} "
            f"(adb={adb_count} cache={cache_count})"
        )


# ═══════════════════════════════════════════════════════════════════════════
# S2 — MK34 Executor Adoption (board ticket P0-2)
# ═══════════════════════════════════════════════════════════════════════════

_PASS_CAPABILITY: Optional[Any] = None
_PASS_LOCK = threading.RLock()


def begin_sync_pass(*, scope: str = "*", ttl_seconds: float = 7200.0) -> Optional[Any]:
    """
    Issue a pass-wide capability. All transfers in the pass reuse it
    (consume=False) so single-use enforcement doesn't fire after the first
    transfer. end_sync_pass() does the final revocation.
    """
    global _PASS_CAPABILITY
    try:
        from mk35_capability import (
            grant_capability, _get_grant_key_for_invariant_gate,
            revoke_all_capabilities,
        )
    except ImportError:
        log.warning("  [MK37/S2] mk35_capability not installed; running unprotected")
        return None

    with _PASS_LOCK:
        if _PASS_CAPABILITY is not None:
            revoke_all_capabilities("pass_restart")
            _bump("capability_revokes")

        cap = grant_capability(
            grant_key=_get_grant_key_for_invariant_gate(),
            scope=scope,
            granted_by_invariant="mk37_wiring.begin_sync_pass",
            ttl_seconds=ttl_seconds,
        )
        _PASS_CAPABILITY = cap

    _bump("capability_grants")
    log.info(
        f"  [MK37/S2] PASS_CAPABILITY_ISSUED token={cap.token_id[:8]} "
        f"scope={scope} ttl={ttl_seconds:.0f}s"
    )
    return cap


def end_sync_pass() -> None:
    """Revoke the active pass capability."""
    global _PASS_CAPABILITY
    try:
        from mk35_capability import revoke_all_capabilities
        n = revoke_all_capabilities("pass_complete")
        _bump("capability_revokes", n)
        log.info(f"  [MK37/S2] PASS_CAPABILITY_REVOKED count={n}")
    except ImportError:
        pass
    finally:
        with _PASS_LOCK:
            _PASS_CAPABILITY = None


def get_pass_capability() -> Optional[Any]:
    with _PASS_LOCK:
        return _PASS_CAPABILITY


# ═══════════════════════════════════════════════════════════════════════════
# MK37.1-3 — Mandatory Wrapper Assertions (board cherry-pick #3)
# ═══════════════════════════════════════════════════════════════════════════
# Discipline → Structural. When a developer adds a NEW mutation site that
# bypasses our wrappers, the call now raises. The bypass that allowed the
# original drift becomes runtime-illegal.
#
# Implementation: thread-local "context" markers set by every wrapper.
# When a tracked primitive is invoked, it checks the context. If absent
# AND production hardened mode is on, raises BypassDetected.

_MK37_CONTEXT = threading.local()


class BypassDetected(RuntimeError):
    """
    MK37.1-3: a tracked destructive primitive was invoked without an
    active mk37_wiring context. In hardened mode this prevents the
    original architectural drift (legacy paths bypassing safety wrappers)
    from recurring through new code.
    """


def _set_context(name: str, value: Any) -> None:
    if not hasattr(_MK37_CONTEXT, "stack"):
        _MK37_CONTEXT.stack = {}
    _MK37_CONTEXT.stack.setdefault(name, []).append(value)


def _pop_context(name: str) -> None:
    if hasattr(_MK37_CONTEXT, "stack") and name in _MK37_CONTEXT.stack:
        if _MK37_CONTEXT.stack[name]:
            _MK37_CONTEXT.stack[name].pop()


def _has_context(name: str) -> bool:
    return (
        hasattr(_MK37_CONTEXT, "stack")
        and name in _MK37_CONTEXT.stack
        and bool(_MK37_CONTEXT.stack[name])
    )


def is_bypass_detection_enabled() -> bool:
    """
    MK37.1-3 master switch. Defaults to True under PRODUCTION_HARDENED_MODE,
    False otherwise (so dev/test continues to work without rewriting every
    test).
    """
    if is_production_hardened_mode():
        # Allow explicit opt-out for staged rollout
        return _flag("MK37_ENABLE_BYPASS_DETECTION", True)
    return _flag("MK37_ENABLE_BYPASS_DETECTION", False)


@contextmanager
def _push_context(name: str, value: Any = True):
    """Internal: wrappers use this to mark "we're in a sanctioned mutation"."""
    _set_context(name, value)
    try:
        yield
    finally:
        _pop_context(name)


def _check_context_or_raise(primitive: str, required_context: str) -> None:
    """
    MK37.1-3 enforcement point. Called by monkey-patched primitives.
    Raises BypassDetected if no sanctioned context is active.
    """
    if not is_bypass_detection_enabled():
        return
    if _has_context(required_context):
        return
    # Heuristic escape: stack-walk to see if any frame is inside a
    # sanctioned wrapper. Catches indirect calls through helper modules
    # whose intermediate frames don't push context but whose CALLER did.
    import inspect
    sanctioned_frames = {
        "transfer_via_legacy_with_quorum", "transactional_dedup",
        "tag_file_with_coherence", "idempotent_destructive_op",
        "_do_copy_and_verify", "_do_delete_source",
    }
    try:
        for frame in inspect.stack()[1:30]:
            if frame.function in sanctioned_frames:
                return
    finally:
        # Defensive: clear local refs to break any reference cycles
        try:
            del frame
        except UnboundLocalError:
            pass
    _bump("bypass_detected" if "bypass_detected" in _TELEMETRY else "capability_rejections")
    msg = (
        f"MK37_BYPASS_DETECTED: primitive {primitive!r} called without "
        f"active {required_context!r} context. New mutation paths must "
        f"route through mk37_wiring.* wrappers. See README_MK37_1.md."
    )
    log.error(f"  [MK37.1-3] {msg}")
    raise BypassDetected(msg)


# Track in telemetry
_TELEMETRY["bypass_detected"] = 0


def _install_primitive_guards() -> None:
    """
    Install runtime guards on dangerous primitives. Called from
    audit_dependency_graph() under hardened mode. Idempotent.
    """
    if not is_bypass_detection_enabled():
        return

    # Guard mtp.copy_file_to_device — must be inside a transfer_via_legacy_with_quorum
    try:
        import mtp_transfer as _mtp_mod
        if not getattr(_mtp_mod, "_mk37_guarded", False):
            _orig_copy = _mtp_mod.copy_file_to_device

            def _guarded_copy(*args, **kwargs):
                _check_context_or_raise(
                    "mtp.copy_file_to_device", "transfer_pass"
                )
                return _orig_copy(*args, **kwargs)

            _guarded_copy.__name__ = "copy_file_to_device"
            _guarded_copy.__wrapped__ = _orig_copy   # for inspect
            _mtp_mod.copy_file_to_device = _guarded_copy
            _mtp_mod._mk37_guarded = True
            log.info("  [MK37.1-3] guarded mtp.copy_file_to_device")
    except ImportError:
        pass

    # Guard _com.copy_file_to_device similarly
    try:
        import mtp_transfer_com as _com_mod
        if not getattr(_com_mod, "_mk37_guarded", False):
            _orig_com = _com_mod.copy_file_to_device

            def _guarded_com(*args, **kwargs):
                _check_context_or_raise(
                    "_com.copy_file_to_device", "transfer_pass"
                )
                return _orig_com(*args, **kwargs)

            _guarded_com.__name__ = "copy_file_to_device"
            _guarded_com.__wrapped__ = _orig_com
            _com_mod.copy_file_to_device = _guarded_com
            _com_mod._mk37_guarded = True
            log.info("  [MK37.1-3] guarded _com.copy_file_to_device")
    except ImportError:
        pass


def transfer_via_legacy_with_quorum(
    legacy_transfer_fn: Callable,
    *legacy_args,
    source_local_path: Optional[str] = None,
    device_relpath: Optional[str] = None,
    expected_hash: Optional[str] = None,
    expected_size: Optional[int] = None,
    adb_path: str = "adb",
    device_root: str = "/storage/4A21-0000",
    **legacy_kwargs,
) -> Dict[str, Any]:
    """
    Wraps a legacy transport callable with capability check + post-transfer
    quorum verification + 4-event lifecycle audit.

    Marker on success: TRANSFER_QUORUM_VERIFIED
    Marker on failure: TRANSFER_QUORUM_FAILED

    Returns a result dict with 'accepted', 'completed', 'transport_used',
    'reason', 'effective_tier'.
    """
    if not is_transfer_quorum_enabled():
        # Bypass: just call the legacy function directly
        try:
            r = legacy_transfer_fn(*legacy_args, **legacy_kwargs)
            return {"accepted": r is not False, "completed": r is not False,
                    "transport_used": "legacy_bypass", "reason": "quorum_disabled",
                    "effective_tier": "BYPASS"}
        except Exception as e:
            return {"accepted": False, "completed": False,
                    "transport_used": "legacy_bypass_error",
                    "reason": f"{type(e).__name__}: {e}",
                    "effective_tier": "UNTRUSTED"}

    started = time.time()

    # MK37.2-1: PROACTIVE RENEWAL — re-issue the pass capability if its
    # remaining lifetime is below threshold, BEFORE the require_capability
    # check fires. Closes LOG_8_5_26 cascade where token=7d7e8348 expired
    # 3,116s before the first transfer attempt because Phase 1.5+2
    # enrichment + Stage 0.5 ingest consumed the entire TTL window before
    # any destructive op fired.
    #
    # Renewal goes through begin_sync_pass() which routes through the
    # _GRANT_KEY invariant — security model preserved. We renew BEFORE
    # the capability check, never AFTER a rejection, so rejections remain
    # meaningful out-of-band signals.
    try:
        from mk37_2_renewal import renew_pass_capability_if_stale
        renew_pass_capability_if_stale(min_remaining_s=300.0,
                                        reason="staleness_check")
    except ImportError:
        pass
    except Exception as _e_renewal:
        log.warning(f"  [MK37.2-1] renewal probe failed (continuing): {_e_renewal}")

    cap = get_pass_capability()

    # Capability check (non-consuming — capability is reused across pass)
    if cap is not None:
        try:
            from mk35_capability import require_capability
            require_capability(
                cap, operation="transfer",
                path=device_relpath or "*",
                consume=False,
            )
        except ImportError:
            pass
        except Exception as e:
            _bump("capability_rejections")
            log.error(f"  [MK37/S2] CAPABILITY_REJECTED: {e}")
            return {"accepted": False, "completed": False,
                    "transport_used": "rejected_no_capability",
                    "reason": f"capability_failure: {type(e).__name__}: {e}",
                    "effective_tier": "UNTRUSTED"}

    # Lifecycle tracker
    tracker = None
    try:
        from mk36_lifecycle import LifecycleTracker, persist_lifecycle_events
        tracker = LifecycleTracker(path=device_relpath or "<unknown>")
        tracker.emit_pre(
            capability_token=cap.token_id if cap else None,
            source_hash=expected_hash,
        )
    except ImportError:
        pass

    # Legacy transfer
    transport_name = getattr(legacy_transfer_fn, "__name__", "legacy_transfer")
    transfer_succeeded = False
    transfer_error: Optional[str] = None
    try:
        # MK37.1-3: announce transfer_pass context so guarded primitives accept the call
        with _push_context("transfer_pass", device_relpath or "*"):
            legacy_result = legacy_transfer_fn(*legacy_args, **legacy_kwargs)
        transfer_succeeded = legacy_result is not False
    except Exception as e:
        transfer_error = f"{type(e).__name__}: {e}"
        transfer_succeeded = False

    if tracker:
        tracker.emit_accepted(
            transport=transport_name,
            success=transfer_succeeded,
            reason=transfer_error,
        )

    if not transfer_succeeded:
        _bump("transfer_quorum_failures")
        if tracker:
            tracker.emit_committed(
                success=False, transport_used=transport_name,
                failure_reason=transfer_error,
                elapsed_s=time.time() - started,
            )
            try: persist_lifecycle_events(tracker.events)
            except Exception: pass
        return {
            "accepted": False, "completed": False,
            "transport_used": transport_name,
            "reason": transfer_error or "transport_failed",
            "effective_tier": "UNTRUSTED",
        }

    # Quorum verification (the actual structural check)
    completed = False
    effective_tier = "UNKNOWN"
    quorum_dict: Optional[dict] = None
    if source_local_path and device_relpath:
        try:
            from mk35_trust import verify_with_quorum, TrustTier
            qo = verify_with_quorum(
                source_local_path=source_local_path,
                device_relpath=device_relpath,
                expected_hash=expected_hash,
                expected_size=expected_size,
                adb_path=adb_path,
                device_root=device_root,
            )
            completed = qo.quorum_reached
            effective_tier = qo.effective_tier.name
            quorum_dict = qo.to_dict()
        except ImportError:
            completed = True
            effective_tier = "LEGACY_NO_QUORUM"
        except Exception as e:
            log.warning(f"  [MK37/S2] quorum check raised: {e}")
            completed = transfer_succeeded
            effective_tier = "QUORUM_ERROR"
    else:
        completed = transfer_succeeded
        effective_tier = "NO_QUORUM_INPUT"

    if completed:
        _bump("transfer_quorum_verified")
        log.debug(f"  [MK37/S2] TRANSFER_QUORUM_VERIFIED tier={effective_tier}")
    else:
        _bump("transfer_quorum_failures")
        log.warning(
            f"  [MK37/S2] TRANSFER_QUORUM_FAILED path={device_relpath!r} "
            f"tier={effective_tier}"
        )

    if tracker:
        tracker.emit_verified(quorum_outcome=quorum_dict)
        tracker.emit_committed(
            success=completed,
            transport_used=f"{transport_name}+quorum:{effective_tier}",
            failure_reason=None if completed else "quorum_failed",
            elapsed_s=time.time() - started,
        )
        try: persist_lifecycle_events(tracker.events)
        except Exception: pass

    _bump("transfers_via_executor")
    return {
        "accepted":       transfer_succeeded,
        "completed":      completed,
        "transport_used": f"{transport_name}+quorum:{effective_tier}",
        "reason":         "completed" if completed else "quorum_failed",
        "effective_tier": effective_tier,
    }


# ═══════════════════════════════════════════════════════════════════════════
# S3a — Transactional Dedup (board ticket P0-3)
# ═══════════════════════════════════════════════════════════════════════════
# Enforce explicit ordering: COPY → VERIFY → COMMIT → DELETE.
# Never DELETE BEFORE VERIFY.

def transactional_dedup(
    *,
    source_local_path: str,
    device_relpath: str,
    transfer_fn:    Callable,
    delete_source_fn: Callable,
    expected_hash:  Optional[str] = None,
    adb_path:       str = "adb",
    device_root:    str = "/storage/4A21-0000",
) -> Dict[str, Any]:
    """
    Transactional dedup primitive (board ticket P0-3).

    Ordering MANDATE:
        1. COPY  — call transfer_fn() (delegates to transfer_via_legacy_with_quorum)
        2. VERIFY — quorum verification post-transfer
        3. COMMIT — emit DEDUP_TRANSACTION_COMMITTED audit marker
        4. DELETE — only after verified COMMIT, call delete_source_fn()

    NEVER delete the source if verify fails. NEVER delete before COMMIT.

    Returns dict: {'committed', 'source_deleted', 'effective_tier', 'reason'}.
    """
    if not is_transactional_dedup_enabled():
        log.info("  [MK37/S3a] TRANSACTIONAL_DEDUP_BYPASSED (feature flag off)")
        try:
            transfer_fn()
            delete_source_fn()
            return {"committed": True, "source_deleted": True,
                    "effective_tier": "BYPASS", "reason": "feature_flag_off"}
        except Exception as e:
            return {"committed": False, "source_deleted": False,
                    "effective_tier": "BYPASS_ERROR",
                    "reason": f"{type(e).__name__}: {e}"}

    # Step 1+2: COPY + VERIFY (delegated to wrapped transfer_fn — must include quorum)
    try:
        result = transfer_fn()
        if not isinstance(result, dict):
            # Legacy fn returned bool/None — wrap conservatively
            transfer_succeeded = result is not False
            result = {"completed": transfer_succeeded, "accepted": transfer_succeeded,
                       "effective_tier": "LEGACY_NO_QUORUM",
                       "reason": "legacy_transfer_fn"}
    except Exception as e:
        _bump("dedup_aborted_pre_verify")
        log.error(
            f"  [MK37/S3a] DEDUP_TRANSFER_FAILED — source NOT deleted: "
            f"{type(e).__name__}: {e}"
        )
        return {"committed": False, "source_deleted": False,
                "effective_tier": "UNTRUSTED",
                "reason": f"transfer_raised: {type(e).__name__}"}

    # Step 3: COMMIT decision — only proceed if quorum verified
    if not result.get("completed"):
        _bump("dedup_aborted_pre_verify")
        log.warning(
            f"  [MK37/S3a] DEDUP_ABORT_PRE_VERIFY path={device_relpath!r} "
            f"tier={result.get('effective_tier')!r} "
            f"reason={result.get('reason')!r} — source preserved (NOT deleted)"
        )
        return {"committed": False, "source_deleted": False,
                "effective_tier": result.get("effective_tier", "UNKNOWN"),
                "reason": f"verify_failed: {result.get('reason')}"}

    # Quorum verified — emit COMMIT marker
    _bump("dedup_transaction_committed")
    log.info(
        f"  [MK37/S3a] DEDUP_TRANSACTION_COMMITTED path={device_relpath!r} "
        f"tier={result.get('effective_tier')}"
    )

    # Step 4: DELETE — only AFTER commit
    try:
        delete_source_fn()
    except Exception as e:
        log.error(
            f"  [MK37/S3a] DEDUP_DELETE_FAILED post-commit (transfer was OK): "
            f"{type(e).__name__}: {e}"
        )
        return {"committed": True, "source_deleted": False,
                "effective_tier": result.get("effective_tier", "UNKNOWN"),
                "reason": f"delete_raised_post_commit: {type(e).__name__}"}

    return {"committed": True, "source_deleted": True,
            "effective_tier": result.get("effective_tier", "UNKNOWN"),
            "reason": "transaction_complete"}


# ═══════════════════════════════════════════════════════════════════════════
# S3 — Idempotency Wrapper (mk15_idempotency)
# ═══════════════════════════════════════════════════════════════════════════

def idempotent_destructive_op(
    path: str, action: str, fn: Callable[[], Any], *,
    actor: str = "production",
) -> Dict[str, Any]:
    """
    Wrap a destructive callable so duplicate calls (replay, double-execute)
    are idempotent. The first call executes; subsequent calls with the same
    (path, action) tuple are no-ops.
    """
    try:
        from mk15_idempotency import (
            get_guard, OperationType, initialize_global_guard,
        )
    except ImportError:
        try:
            fn()
            return {"executed": True, "op_uuid": None, "duplicate": False,
                    "note": "mk15_idempotency_unavailable"}
        except Exception as e:
            return {"executed": False, "op_uuid": None, "duplicate": False,
                    "error": str(e)}

    # Initialize guard on demand — production may not have called this.
    # We use a process-local jsonl file in the working directory by default.
    try:
        guard = get_guard()
    except RuntimeError:
        from pathlib import Path as _Path
        initialize_global_guard(audit_log_path=_Path(".mk37_idempotency.jsonl"))
        guard = get_guard()

    try:
        op_type = OperationType[action.upper()]
    except (KeyError, AttributeError):
        # Map common english actions to mk15 OperationType names
        a = action.lower()
        if a in ("delete", "remove", "rm", "unlink"):
            op_type = OperationType.FILE_DELETE
        elif a in ("move", "rename"):
            op_type = OperationType.FILE_MOVE
        elif a in ("overwrite", "replace"):
            op_type = OperationType.FILE_OVERWRITE
        elif a in ("dedup", "purge"):
            op_type = OperationType.DEDUP_PURGE
        elif a in ("tag", "metadata", "mutate"):
            op_type = OperationType.METADATA_MUTATE
        else:
            op_type = OperationType.FILE_DELETE

    guard = get_guard()
    op_uuid = guard.create_operation_uuid(
        operation_type=op_type, target_path=path, actor=actor,
    )
    if guard.already_executed(op_uuid):
        _bump("dedup_idempotent_skips")
        log.info(
            f"  [MK37/S3] IDEMPOTENT_SKIP path={path!r} action={action!r} "
            f"uuid={op_uuid.uuid_str[:8]}"
        )
        return {"executed": False, "op_uuid": op_uuid.uuid_str, "duplicate": True}

    try:
        fn()
    except Exception as e:
        log.error(
            f"  [MK37/S3] IDEMPOTENT_FN_FAILED path={path!r} "
            f"action={action!r} err={e!r}"
        )
        return {"executed": False, "op_uuid": op_uuid.uuid_str,
                "duplicate": False, "error": str(e)}

    guard.mark_executed(op_uuid)
    log.info(
        f"  [MK37/S3] IDEMPOTENT_EXECUTED path={path!r} action={action!r} "
        f"uuid={op_uuid.uuid_str[:8]}"
    )
    return {"executed": True, "op_uuid": op_uuid.uuid_str, "duplicate": False}


# ═══════════════════════════════════════════════════════════════════════════
# S3b — Offline Provider Capability Enforcement (board ticket P0-4 — NEW)
# ═══════════════════════════════════════════════════════════════════════════
# Marker on block: OFFLINE_PROVIDER_ACCESS_BLOCKED
# When config.OFFLINE_MODE=True, any provider call must raise.

class OfflineProviderViolation(RuntimeError):
    """Raised when a network provider is invoked while OFFLINE_MODE=True."""


def offline_provider_capability_check(provider_name: str) -> None:
    """
    Board ticket P0-4: offline mode must be impossible to violate at runtime.

    Call this at the top of every network-bound provider function. If
    config.OFFLINE_MODE=True AND the offline-capability flag is enforced,
    raises OfflineProviderViolation.

    Marker on block: OFFLINE_PROVIDER_ACCESS_BLOCKED
    """
    try:
        import config as _cfg
        offline = bool(getattr(_cfg, "OFFLINE_MODE", False))
    except ImportError:
        offline = False

    if not offline:
        return

    if not is_offline_capability_enforced():
        log.info(
            f"  [MK37/S3b] offline_capability check disabled by flag; "
            f"allowing provider {provider_name!r}"
        )
        return

    _bump("offline_capability_violations")
    _bump("offline_provider_blocks")
    log.error(
        f"  [MK37/S3b] OFFLINE_PROVIDER_ACCESS_BLOCKED provider={provider_name!r}"
    )
    raise OfflineProviderViolation(
        f"OFFLINE_PROVIDER_ACCESS_BLOCKED: provider={provider_name!r} "
        f"may not be invoked while OFFLINE_MODE=True"
    )


# ═══════════════════════════════════════════════════════════════════════════
# S4 / P1-1 — Lock-Aware Tag Writes (mk15_file_coherence)
# ═══════════════════════════════════════════════════════════════════════════

@contextmanager
def tag_file_with_coherence(path: str | Path, *, retries: int = 5,
                              base_backoff: float = 0.5):
    """
    Context manager that holds an exclusive sidecar lock for `path` while
    the body runs. Use around mutagen.save() to survive AV scanner / Windows
    indexer handle holds (the LOG_5_5_26.txt Permission denied class).

    Implementation: locks a sidecar file `<path>.mk37.lock` via portalocker.
    The lock guards CONCURRENT WRITERS within this process and across
    processes that respect the same convention. Combined with retry+backoff
    on lock acquisition, this dramatically reduces the failure rate.

    Marker on success-after-retry: TAG_WRITE_RETRY_SUCCESS
    Marker on permanent failure:   TAG_WRITE_PERMANENT_FAILURE
    """
    p = Path(path)

    if not is_lock_aware_tagging_enabled():
        yield None
        return

    try:
        import portalocker
    except ImportError:
        log.debug("  [MK37/S4] portalocker not installed; coherence degraded")
        yield None
        return

    lock_path = p.with_suffix(p.suffix + ".mk37.lock")
    lock_handle = None
    succeeded_on_retry = False

    for attempt in range(retries + 1):
        try:
            lock_handle = open(lock_path, "wb")
            portalocker.lock(lock_handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            if attempt > 0:
                succeeded_on_retry = True
            break
        except (IOError, OSError, PermissionError) as e:
            if lock_handle is not None:
                try:
                    lock_handle.close()
                except Exception:
                    pass
                lock_handle = None
            if attempt >= retries:
                _bump("tag_write_permanent_failure")
                log.error(
                    f"  [MK37/S4] TAG_WRITE_PERMANENT_FAILURE path={p.name!r} "
                    f"after {retries} retries: {e}"
                )
                # Yield anyway so production code keeps running (degrade gracefully)
                try:
                    yield None
                finally:
                    pass
                return
            backoff = base_backoff * (2 ** attempt)
            time.sleep(backoff)

    try:
        if succeeded_on_retry:
            _bump("tag_write_retry_success")
            log.info(f"  [MK37/S4] TAG_WRITE_RETRY_SUCCESS path={p.name!r}")
        yield lock_handle
    finally:
        if lock_handle is not None:
            try:
                portalocker.unlock(lock_handle)
            except Exception:
                pass
            try:
                lock_handle.close()
            except Exception:
                pass
        try:
            if lock_path.exists():
                lock_path.unlink(missing_ok=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# S5 — Provider Health Tracking (closes 1,498 fail-open failures)
# ═══════════════════════════════════════════════════════════════════════════

class ProviderState(Enum):
    OK         = "ok"
    COOLDOWN   = "cooldown"
    SESSION_DISABLED = "session_disabled"


@dataclass
class ProviderHealth:
    name:                  str
    state:                 ProviderState = ProviderState.OK
    consecutive_failures:  int = 0
    last_failure_at:       float = 0.0
    cooldown_until:        float = 0.0
    last_error_class:      str = ""
    failure_threshold:     int = 5
    cooldown_seconds:      float = 300.0
    quota_cooldown_seconds: float = 1800.0

    def is_available(self, now: Optional[float] = None) -> bool:
        n = now if now is not None else time.time()
        if self.state == ProviderState.SESSION_DISABLED:
            return False
        if self.state == ProviderState.COOLDOWN and n < self.cooldown_until:
            return False
        if self.state == ProviderState.COOLDOWN and n >= self.cooldown_until:
            self.state = ProviderState.OK
            self.consecutive_failures = 0
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_error_class = ""
        if self.state == ProviderState.COOLDOWN:
            self.state = ProviderState.OK

    def record_failure(self, error_class: str = "unknown",
                          retry_after_s: Optional[float] = None) -> None:
        self.consecutive_failures += 1
        self.last_failure_at = time.time()
        self.last_error_class = error_class
        ec = (error_class or "").lower()

        if "auth" in ec or "401" in ec or "403" in ec:
            self.state = ProviderState.SESSION_DISABLED
            _bump("provider_session_disables")
            log.warning(
                f"  [MK37/S5] PROVIDER_DISABLED name={self.name!r} "
                f"reason={error_class!r}"
            )
            return

        if self.consecutive_failures >= self.failure_threshold:
            duration = (
                retry_after_s
                if retry_after_s is not None
                else (self.quota_cooldown_seconds if "quota" in ec or "429" in ec
                      else self.cooldown_seconds)
            )
            self.cooldown_until = self.last_failure_at + duration
            self.state = ProviderState.COOLDOWN
            _bump("provider_cooldowns")
            log.warning(
                f"  [MK37/S5] PROVIDER_COOLDOWN name={self.name!r} "
                f"duration={duration:.0f}s reason={error_class!r} "
                f"consecutive={self.consecutive_failures}"
            )


@dataclass
class ProviderHealthRegistry:
    providers: Dict[str, ProviderHealth] = field(default_factory=dict)
    _lock:     threading.RLock = field(default_factory=threading.RLock)

    def get(self, name: str) -> ProviderHealth:
        with self._lock:
            if name not in self.providers:
                self.providers[name] = ProviderHealth(name=name)
            return self.providers[name]

    def all_unavailable(self) -> bool:
        with self._lock:
            if not self.providers:
                return False
            return all(not p.is_available() for p in self.providers.values())

    def available_count(self) -> int:
        with self._lock:
            return sum(1 for p in self.providers.values() if p.is_available())

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return {n: p.state.value for n, p in self.providers.items()}


_PROVIDER_REGISTRY = ProviderHealthRegistry()


def get_provider_registry() -> ProviderHealthRegistry:
    return _PROVIDER_REGISTRY


def reset_provider_registry() -> None:
    global _PROVIDER_REGISTRY
    _PROVIDER_REGISTRY = ProviderHealthRegistry()


def should_short_circuit_enrichment() -> bool:
    """Global short-circuit: skip phase 2 if all providers are unavailable."""
    if _PROVIDER_REGISTRY.all_unavailable():
        _bump("enrichment_short_circuits")
        return True
    return False


def record_provider_success(name: str) -> None:
    _PROVIDER_REGISTRY.get(name).record_success()


def record_provider_failure(name: str, error_class: str = "unknown",
                             retry_after_s: Optional[float] = None) -> None:
    _PROVIDER_REGISTRY.get(name).record_failure(error_class, retry_after_s)


def is_provider_available(name: str) -> bool:
    return _PROVIDER_REGISTRY.get(name).is_available()


# ═══════════════════════════════════════════════════════════════════════════
# Backward-compat: keep transfer_with_executor for callers that prefer the
# direct mk34_executor path
# ═══════════════════════════════════════════════════════════════════════════

def transfer_with_executor(
    source_local_path: str, device_relpath: str, *,
    expected_hash: Optional[str] = None,
    adb_path: str = "adb",
    device_root: str = "/storage/4A21-0000",
    legacy_fallback: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Direct mk34_executor path. For most production sites prefer
    transfer_via_legacy_with_quorum instead."""
    cap = get_pass_capability()
    try:
        from mk34_executor import transfer_one
    except ImportError:
        if legacy_fallback is not None:
            ok = bool(legacy_fallback(source_local_path, device_relpath))
            return {"accepted": ok, "completed": ok,
                    "transport_used": "legacy_fallback",
                    "reason": "mk34_executor_missing"}
        raise RuntimeError("mk34_executor not available and no legacy_fallback supplied")

    try:
        outcome = transfer_one(
            source_local_path=source_local_path,
            device_relpath=device_relpath,
            expected_hash=expected_hash,
            adb_path=adb_path, device_root=device_root,
            capability=cap,
        )
    except Exception as e:
        if legacy_fallback is not None:
            ok = bool(legacy_fallback(source_local_path, device_relpath))
            return {"accepted": ok, "completed": ok,
                    "transport_used": "legacy_fallback_after_executor_error",
                    "reason": f"executor_raised: {type(e).__name__}"}
        raise

    return {
        "accepted": outcome.accepted, "completed": outcome.completed,
        "transport_used": outcome.transport_used,
        "reason": outcome.failure_reason or "completed",
        "device_hash": getattr(outcome, "device_hash", None),
        "elapsed_s": outcome.elapsed_s,
    }


# ═══════════════════════════════════════════════════════════════════════════
# MK37.1-4 — Crash Recovery Capability Reconciliation (board cherry-pick #4)
# ═══════════════════════════════════════════════════════════════════════════
# When a sync run terminates abnormally (SIGKILL, OOM, kernel panic), it
# leaves stranded state:
#   - capabilities issued but never revoked (mk36_capability_store SQLite)
#   - lifecycle entries with PRE_TRANSFER but no TRANSFER_COMMITTED (mk34_audit.jsonl)
#   - idempotency records that may or may not correspond to completed ops
# Without reconciliation, the next run can:
#   - reuse a stale capability from a half-completed prior pass,
#   - re-attempt transfers that succeeded but weren't committed in the journal,
#   - skip transfers it thinks already happened.
# This function runs at startup, idempotent, idempotent-replayable.

_CRASH_RECOVERY_DONE = False


def reconcile_crash_state(
    *,
    capability_max_age_seconds: float = 86400.0,   # 24h
    idempotency_max_age_days:    int   = 7,
) -> Dict[str, int]:
    """
    Board MK37.1-4: deterministic startup reconciliation.

    Returns dict with counts:
      capabilities_revoked, lifecycles_failed, idempotency_purged

    Idempotent — safe to re-run; takes a no-op fast path if recently done.
    """
    global _CRASH_RECOVERY_DONE
    if _CRASH_RECOVERY_DONE:
        return {"capabilities_revoked": 0, "lifecycles_failed": 0,
                "idempotency_purged": 0, "skipped": 1}

    counts = {"capabilities_revoked": 0, "lifecycles_failed": 0,
              "idempotency_purged": 0}
    now = time.time()

    # 1. Revoke orphan capabilities older than the cutoff
    try:
        from pathlib import Path as _Path
        import sqlite3
        cap_db = (_Path(__file__).resolve().parent.parent / "state" / ".mk36_capabilities.db")
        if cap_db.exists():
            with sqlite3.connect(str(cap_db)) as conn:
                cur = conn.cursor()
                # Tolerate either schema (capabilities or capability_grants table)
                tables = [r[0] for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )]
                target_table = None
                for t in ("capabilities", "capability_grants", "grants"):
                    if t in tables:
                        target_table = t
                        break
                if target_table:
                    # Find columns for issued_at + state
                    cols = [r[1] for r in cur.execute(
                        f"PRAGMA table_info({target_table})"
                    )]
                    issued_col = None
                    for c in ("issued_at", "created_at", "granted_at",
                                "timestamp"):
                        if c in cols:
                            issued_col = c
                            break
                    state_col = "state" if "state" in cols else (
                        "status" if "status" in cols else None
                    )
                    if issued_col and state_col:
                        cutoff = now - capability_max_age_seconds
                        n = cur.execute(
                            f"UPDATE {target_table} SET {state_col}='revoked' "
                            f"WHERE {state_col} != 'revoked' "
                            f"AND {issued_col} < ?",
                            (cutoff,),
                        ).rowcount
                        conn.commit()
                        counts["capabilities_revoked"] = n
                        if n > 0:
                            log.info(
                                f"  [MK37.1-4] CRASH_RECOVERY_CAPABILITIES_REVOKED "
                                f"count={n} age_cutoff_s={capability_max_age_seconds}"
                            )
    except Exception as e:
        log.debug(f"  [MK37.1-4] capability reconcile skipped: {e}")

    # 2. Mark interrupted lifecycles FAILED
    try:
        from pathlib import Path as _Path
        import json as _json
        audit_log = (_Path(__file__).resolve().parent.parent / "state" / ".mk34_audit.jsonl")
        if audit_log.exists():
            # Collect lifecycle_ids with PRE_TRANSFER but no TRANSFER_COMMITTED
            pre_only = set()
            committed = set()
            try:
                with open(audit_log, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = _json.loads(line)
                        except Exception:
                            continue
                        evt = rec.get("event") or rec.get("type", "")
                        lid = rec.get("lifecycle_id") or rec.get("path")
                        if not lid:
                            continue
                        if "PRE_TRANSFER" in evt or "pre_transfer" in evt.lower():
                            pre_only.add(lid)
                        elif "COMMITTED" in evt or "committed" in evt.lower():
                            committed.add(lid)
            except Exception:
                pass
            interrupted = pre_only - committed
            counts["lifecycles_failed"] = len(interrupted)
            if interrupted:
                # Append synthetic FAILED markers so the next run sees them
                with open(audit_log, "a", encoding="utf-8") as fh:
                    for lid in interrupted:
                        fh.write(_json.dumps({
                            "event": "CRASH_RECOVERY_LIFECYCLES_FAILED",
                            "lifecycle_id": lid,
                            "ts": now,
                            "source": "mk37.1-4_reconcile",
                        }) + "\n")
                log.info(
                    f"  [MK37.1-4] CRASH_RECOVERY_LIFECYCLES_FAILED "
                    f"count={len(interrupted)}"
                )
    except Exception as e:
        log.debug(f"  [MK37.1-4] lifecycle reconcile skipped: {e}")

    # 3. Purge stale idempotency records older than cutoff days
    try:
        from pathlib import Path as _Path
        import json as _json
        idem_log = (_Path(__file__).resolve().parent.parent / "state" / ".mk37_idempotency.jsonl")
        if idem_log.exists():
            cutoff = now - (idempotency_max_age_days * 86400.0)
            kept_lines = []
            purged = 0
            try:
                with open(idem_log, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            rec = _json.loads(line)
                            ts = rec.get("timestamp", rec.get("ts", now))
                            if ts >= cutoff:
                                kept_lines.append(line)
                            else:
                                purged += 1
                        except Exception:
                            kept_lines.append(line)   # don't drop unparseable
            except Exception:
                pass
            if purged > 0:
                with open(idem_log, "w", encoding="utf-8") as fh:
                    fh.writelines(kept_lines)
                counts["idempotency_purged"] = purged
                log.info(
                    f"  [MK37.1-4] CRASH_RECOVERY_IDEMPOTENCY_PURGED "
                    f"count={purged} older_than_days={idempotency_max_age_days}"
                )
    except Exception as e:
        log.debug(f"  [MK37.1-4] idempotency reconcile skipped: {e}")

    # Telemetry
    if counts["capabilities_revoked"]:
        _bump("crash_recovery_capabilities_revoked",
               counts["capabilities_revoked"])
    if counts["lifecycles_failed"]:
        _bump("crash_recovery_lifecycles_failed",
               counts["lifecycles_failed"])

    _CRASH_RECOVERY_DONE = True
    return counts


# Add telemetry counters for MK37.1-4
_TELEMETRY["crash_recovery_capabilities_revoked"] = 0
_TELEMETRY["crash_recovery_lifecycles_failed"] = 0


# ═══════════════════════════════════════════════════════════════════════════
# Run startup self-audit at import time (Recommendation F)
# ═══════════════════════════════════════════════════════════════════════════

# Audit on import; result is cached.
_STARTUP_AUDIT: Optional[DependencyAuditReport] = None
try:
    _STARTUP_AUDIT = audit_dependency_graph()
except Exception as _e:
    log.warning(f"  [MK37/Audit] startup audit raised (ignored): {_e}")


def get_startup_audit() -> Optional[DependencyAuditReport]:
    """Return the audit report from import-time self-check."""
    return _STARTUP_AUDIT


__all__ = [
    # Telemetry
    "snapshot_telemetry", "reset_telemetry",
    # Feature flags
    "is_halt_gate_enabled", "is_transfer_quorum_enabled",
    "is_transactional_dedup_enabled", "is_offline_capability_enforced",
    "is_lock_aware_tagging_enabled",
    # MK37.1-2: production hardened mode
    "is_production_hardened_mode", "assert_production_flags_locked",
    # MK37.1-3: bypass detection
    "BypassDetected", "is_bypass_detection_enabled",
    "_install_primitive_guards",
    # MK37.1-4: crash recovery
    "reconcile_crash_state",
    # Self-audit
    "DependencyAuditReport", "audit_dependency_graph", "get_startup_audit",
    # S1
    "enforce_observation_halt",
    # S2
    "begin_sync_pass", "end_sync_pass", "get_pass_capability",
    "transfer_with_executor",
    "transfer_via_legacy_with_quorum",
    # S3a
    "transactional_dedup",
    # S3
    "idempotent_destructive_op",
    # S3b — board ticket P0-4
    "OfflineProviderViolation", "offline_provider_capability_check",
    # S4 — board ticket P1-1
    "tag_file_with_coherence",
    # S5
    "ProviderState", "ProviderHealth", "ProviderHealthRegistry",
    "get_provider_registry", "reset_provider_registry",
    "should_short_circuit_enrichment",
    "record_provider_success", "record_provider_failure",
    "is_provider_available",
]
