"""
mk37_2_resource_coupling.py — MK37.2-5: ResourceMonitor ↔ Capability coupling
================================================================================

Sprint:        MK37.2 — Operational Endurance
Closes:        LOG_8_5_26 secondary observation — `[ResourceMonitor] Resource
               pressure climbing: GDI=49-50/8000 USER=196/8000 Handles=8081-8089/12000`
               with `[ResourceMonitor] Forced GC freed N handle(s)` interventions
               firing 16 times across the run.
Risk closed:   New risk #7 (Handle-leak compounding capability rejection
               cascade). Out-of-scope for the deepseek brief but in-scope for
               operational endurance — left untreated, a long enrichment-then-
               failed-transfer run would eventually exhaust Windows handles
               (12,000 ceiling) and crash the COM subsystem entirely.

Mechanism:
  - Hook into ResourceMonitor's existing `_check_pressure()` callback (if
    present); on pressure-climbing events, trigger:
      1. The existing forced GC (already wired)
      2. NEW: capability staleness probe via mk37_2_renewal — if the pass
         capability is below 600s remaining when handles approach the
         ceiling, renew preemptively. The reasoning: handle pressure
         correlates with run-length, and run-length correlates with
         capability staleness. Coupling the two means renewal is
         opportunistic (driven by pressure events) rather than purely
         time-based.
  - This module is OPTIONAL: if ResourceMonitor doesn't exist or its hook
    surface is absent, the module no-ops cleanly. Production resilience
    is preserved by MK37.2-1 alone; this is the additive layer.

Public API:
    install_resource_capability_coupling() -> bool
    snapshot_coupling_telemetry() -> Dict[str, int]
"""

from __future__ import annotations

import logging
import threading
from typing import Dict

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Telemetry
# ═══════════════════════════════════════════════════════════════════════════

_COUPLING_TELEMETRY: Dict[str, int] = {
    "pressure_events_observed":         0,
    "renewal_triggered_by_pressure":    0,
    "coupling_install_failures":        0,
}
_COUPLING_LOCK = threading.RLock()
_INSTALLED = False


def _bump(metric: str, n: int = 1) -> None:
    with _COUPLING_LOCK:
        _COUPLING_TELEMETRY[metric] = _COUPLING_TELEMETRY.get(metric, 0) + n


def snapshot_coupling_telemetry() -> Dict[str, int]:
    with _COUPLING_LOCK:
        return dict(_COUPLING_TELEMETRY)


# ═══════════════════════════════════════════════════════════════════════════
# Pressure-driven renewal callback
# ═══════════════════════════════════════════════════════════════════════════

def _on_resource_pressure(snapshot: dict) -> None:
    """
    Callback invoked by ResourceMonitor when pressure climbs.

    `snapshot` is expected to be a dict with at least:
      - "gdi": int      (current GDI count)
      - "user": int     (current USER count)
      - "handles": int  (current handle count)
    """
    _bump("pressure_events_observed")

    # Threshold: trigger renewal if remaining capability lifetime is
    # below 600s (10 minutes). This is wider than the transfer-path
    # threshold (300s) because pressure events are predictive — they
    # signal that the run will continue for some time, and a renewal
    # now beats one mid-cascade.
    try:
        from mk37_2_renewal import renew_pass_capability_if_stale
        renewed = renew_pass_capability_if_stale(
            min_remaining_s=600.0,
            reason="resource_pressure",
            stage=None,    # pressure callback doesn't know the active stage
        )
        if renewed:
            _bump("renewal_triggered_by_pressure")
            log.info(
                f"  [MK37.2-5] CAPABILITY_RENEWED_BY_PRESSURE "
                f"gdi={snapshot.get('gdi','?')} "
                f"user={snapshot.get('user','?')} "
                f"handles={snapshot.get('handles','?')}"
            )
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"  [MK37.2-5] pressure-driven renewal skipped: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Installer — wire the callback into ResourceMonitor if available
# ═══════════════════════════════════════════════════════════════════════════

def install_resource_capability_coupling() -> bool:
    """
    Install the pressure → renewal callback. Returns True if installed,
    False if ResourceMonitor unavailable or already installed.

    Idempotent: subsequent calls return False without re-installing.
    """
    global _INSTALLED
    with _COUPLING_LOCK:
        if _INSTALLED:
            return False

    try:
        import resource_monitor as _rm
    except ImportError:
        log.debug("  [MK37.2-5] resource_monitor not available; coupling skipped")
        _bump("coupling_install_failures")
        return False

    # Prefer a dedicated hook surface if present; otherwise
    # patch ResourceMonitor's pressure-handling method to also call us.
    register_fn = getattr(_rm, "register_pressure_callback", None)
    if callable(register_fn):
        try:
            register_fn(_on_resource_pressure)
            with _COUPLING_LOCK:
                _INSTALLED = True
            log.info("  [MK37.2-5] PRESSURE_COUPLING_INSTALLED via register_pressure_callback")
            return True
        except Exception as e:
            log.warning(f"  [MK37.2-5] register_pressure_callback failed: {e}")
            _bump("coupling_install_failures")
            return False

    # Fallback: monkey-patch a known method if we can find one
    target_method = None
    for cand in ("_check_pressure", "check_pressure", "_on_climb"):
        if hasattr(_rm, cand):
            target_method = cand
            break

    if target_method is None:
        log.debug(
            "  [MK37.2-5] resource_monitor present but no hook surface "
            "(no register_pressure_callback or known pressure method); "
            "MK37.2-5 inactive — MK37.2-1 still covers renewal via transfer path."
        )
        _bump("coupling_install_failures")
        return False

    original = getattr(_rm, target_method)

    def _wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        # If the original returned a snapshot dict, forward it; else pass empty
        snap = result if isinstance(result, dict) else {}
        try:
            _on_resource_pressure(snap)
        except Exception as e:
            log.debug(f"  [MK37.2-5] pressure callback error: {e}")
        return result

    try:
        setattr(_rm, target_method, _wrapped)
        with _COUPLING_LOCK:
            _INSTALLED = True
        log.info(
            f"  [MK37.2-5] PRESSURE_COUPLING_INSTALLED via monkey-patch on "
            f"resource_monitor.{target_method}"
        )
        return True
    except Exception as e:
        log.warning(f"  [MK37.2-5] monkey-patch on {target_method} failed: {e}")
        _bump("coupling_install_failures")
        return False


__all__ = [
    "install_resource_capability_coupling",
    "snapshot_coupling_telemetry",
]
