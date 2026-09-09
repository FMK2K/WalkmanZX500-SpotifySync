"""
stage_continuation_override.py — MK39.1 L6.7 / Board GOV-B3
================================================================================

Board Review Revision 2 — Governance §B3 (ALLOW_CONTINUE_ON_STAGE_FAIL Lockdown):

  > "ALLOW_CONTINUE_ON_STAGE_FAIL is removed from config.py as a simple
  > boolean. It is not a user-settable config value. Available as a CLI-
  > only flag (--allow-continue-on-stage-fail) with a required companion
  > argument: --override-reason "<text>". The reason is written to the
  > run log at CRITICAL level. Triggers a RED startup banner that persists
  > for the duration of the run regardless of other profile signals.
  > Blocked entirely under hardened mode."

This module is the single read-site for whether the override is active.
All pipeline gates that previously checked
`config.ALLOW_CONTINUE_ON_STAGE_FAIL` now call `is_active()` from here.

The CLI propagates the override via an environment variable so it crosses
process boundaries cleanly:

    ALLOW_CONTINUE_ON_STAGE_FAIL_OVERRIDE="<reason text>"

Setting that env var is exactly equivalent to passing the CLI flag, and
that's what the CLI parser in main.py does. There's no way to flip the
behavior from config.py.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


log = logging.getLogger(__name__)

_ENV_KEY = "ALLOW_CONTINUE_ON_STAGE_FAIL_OVERRIDE"


def is_active() -> bool:
    """True if the operator has explicitly enabled stage-continue override.

    Always False under PRODUCTION_HARDENED_MODE (Board GOV-B3).
    """
    if _is_hardened():
        # Hardened mode silently rejects the override per board.
        if os.environ.get(_ENV_KEY):
            log.warning(
                f"[StageOverride/L6.7] PRODUCTION_HARDENED_MODE=True — "
                f"silently rejecting --allow-continue-on-stage-fail override. "
                f"(Board GOV-B3)"
            )
        return False
    return bool(os.environ.get(_ENV_KEY, "").strip())


def get_override_reason() -> Optional[str]:
    """Operator-provided reason for invoking the override, or None."""
    val = os.environ.get(_ENV_KEY, "").strip()
    return val if val else None


def activate(reason: str) -> bool:
    """Activate the override (called by CLI parser). reason is required.

    Returns True if the activation took effect; False if hardened-mode
    rejected it.
    """
    if not reason or not reason.strip():
        log.error(
            "[StageOverride/L6.7] activate() requires non-empty reason "
            "(Board GOV-B3 mandate)."
        )
        return False
    if _is_hardened():
        log.warning(
            f"[StageOverride/L6.7] PRODUCTION_HARDENED_MODE=True — refusing "
            f"to activate --allow-continue-on-stage-fail. Reason attempted: "
            f"{reason!r}. (Board GOV-B3)"
        )
        return False
    os.environ[_ENV_KEY] = reason.strip()
    log.critical(
        f"[StageOverride/L6.7] STAGE_CONTINUE_OVERRIDE_ACTIVE — operator "
        f"explicitly enabled --allow-continue-on-stage-fail. "
        f"Reason: {reason!r}. (Board GOV-B3)"
    )
    return True


def deactivate() -> None:
    """Clear the override (for test isolation)."""
    os.environ.pop(_ENV_KEY, None)


def _is_hardened() -> bool:
    """Check PRODUCTION_HARDENED_MODE without crashing if config can't load."""
    try:
        import config as _cfg
        return bool(getattr(_cfg, "PRODUCTION_HARDENED_MODE", False))
    except Exception:
        return False


def banner_severity() -> str:
    """When the override is active, the safety banner must show RED.
    Returns 'RED' if active, '' otherwise. (Used by L4 banner module.)"""
    return "RED" if is_active() else ""
