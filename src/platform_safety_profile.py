"""
platform_safety_profile.py — MK39.1 L4.4 / Council L3 §4.3 aggregated banner
================================================================================

Council L3 Patch Review 2026-05-10 §4.3:

  > "As noted, a single aggregated safety-profile summary at startup
  > ('Platform Safety Profile: Dedup Strict ON; AI Validation STRICT ON;
  > Gate Severity hard_fail; Canonical Sources ACTIVE') would be a high-
  > value operator experience improvement for the Simplicity Sprint. The
  > banner added in L3.5 is a good start."

The L3.5 banner is dedup-only. This module emits a single one-line summary
covering the WHOLE platform safety posture, so the operator can skim-detect
misconfiguration in the first dozen lines of any log. Idempotent — fires
ONCE per process via a module-level guard.

Banner format
-------------
  [Safety/L4] PROFILE dedup=strict/0.60/active ai=strict harden=off
              scrub=on verify=on musicbee=off acoustid=off picard=off
              → VERDICT GREEN (Council-endorsed unattended baseline)

The verdict GREEN/AMBER/RED is computed from the Council-endorsed baseline:

  GREEN: DEDUP_STRICT_MODE=True, DEDUP_MODE='active',
         STRICT_AI_GENRE_VALIDATION=True, SCRUB_ENABLED=True,
         VERIFY_TRANSFERS_ENABLED=True
  AMBER: any of the above are off but no hard-block conditions
  RED:   DEDUP_MODE='off' OR config import fails entirely

Usage
-----
  import platform_safety_profile
  platform_safety_profile.emit_banner()    # call once at session start

The session bootstrap in main.py and walkman_sync.py should call this near
the top of run() / sync() — but it's safe to call multiple times because
the module suppresses re-emission with _EMITTED guard.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


log = logging.getLogger(__name__)

# Module-level idempotency guard — ensures one banner per process.
_EMITTED = False


def _safe_get(cfg, name, default):
    """getattr with non-raising fallback."""
    try:
        return getattr(cfg, name, default)
    except Exception:
        return default


def _format_bool(v: bool, on_label: str = "on", off_label: str = "off") -> str:
    return on_label if v else off_label


def build_profile() -> dict:
    """Read all relevant flags from config; return profile dict.

    Pure function — no logging. Used by emit_banner() AND by tests that want
    to assert the profile contents without grepping the log output.
    """
    try:
        import config as cfg
    except ImportError:
        return {
            "import_failed": True,
            "verdict": "RED",
            "verdict_reason": "config module failed to import",
        }

    # Dedup safety (L2/L3)
    dedup_mode    = _safe_get(cfg, "DEDUP_MODE", "report")
    dedup_strict  = bool(_safe_get(cfg, "DEDUP_STRICT_MODE", False))
    dedup_thresh  = float(_safe_get(cfg, "DEDUP_TITLE_ONLY_MIN_OVERLAP", 0.5))

    # Env override on strict mode (matches walkman_dedup logic)
    _env_override = os.environ.get("DEDUP_STRICT_MODE", "").strip().lower()
    if _env_override in ("1", "true", "yes", "on"):
        dedup_strict = True
        dedup_strict_origin = "env_on"
    elif _env_override in ("0", "false", "no", "off"):
        dedup_strict = False
        dedup_strict_origin = "env_off"
    else:
        dedup_strict_origin = "config"

    # Semantic (MK39.1 C2)
    ai_strict = bool(_safe_get(cfg, "STRICT_AI_GENRE_VALIDATION", False))

    # Hardened mode
    hardened = bool(_safe_get(cfg, "PRODUCTION_HARDENED_MODE", False))

    # Self-healing sync flags (Rev 14.9)
    scrub_on   = bool(_safe_get(cfg, "SCRUB_ENABLED", False))
    verify_on  = bool(_safe_get(cfg, "VERIFY_TRANSFERS_ENABLED", False))

    # Phase III (F/G/H) opt-ins
    musicbee_on = bool(_safe_get(cfg, "SYNC_VIA_MUSICBEE", False))
    acoustid_on = bool(_safe_get(cfg, "ACOUSTID_API_KEY", ""))
    picard_on   = bool(_safe_get(cfg, "PICARD_COMPAT_NORMALIZE", False))

    # Verdict aggregation
    verdict = "GREEN"
    verdict_reasons = []
    if dedup_mode == "off":
        verdict = "RED"
        verdict_reasons.append("DEDUP_MODE=off")
    elif dedup_mode == "report":
        verdict = "AMBER"
        verdict_reasons.append("DEDUP_MODE=report (no execution)")
    if not dedup_strict:
        # Strict OFF is acceptable for manual cleanup; AMBER not RED.
        if verdict == "GREEN":
            verdict = "AMBER"
        verdict_reasons.append("DEDUP_STRICT_MODE=False (manual cleanup posture)")
    if not ai_strict:
        if verdict == "GREEN":
            verdict = "AMBER"
        verdict_reasons.append("STRICT_AI_GENRE_VALIDATION=False")
    if not scrub_on:
        if verdict == "GREEN":
            verdict = "AMBER"
        verdict_reasons.append("SCRUB_ENABLED=False")
    if not verify_on:
        if verdict == "GREEN":
            verdict = "AMBER"
        verdict_reasons.append("VERIFY_TRANSFERS_ENABLED=False")

    return {
        "dedup_mode": dedup_mode,
        "dedup_strict": dedup_strict,
        "dedup_strict_origin": dedup_strict_origin,
        "dedup_threshold": dedup_thresh,
        "ai_strict": ai_strict,
        "hardened": hardened,
        "scrub_on": scrub_on,
        "verify_on": verify_on,
        "musicbee_on": musicbee_on,
        "acoustid_on": acoustid_on,
        "picard_on": picard_on,
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
    }


def format_banner_line(profile: dict) -> str:
    """Format the profile dict as the one-line banner. Pure function."""
    if profile.get("import_failed"):
        return ("[Safety/L4] PROFILE CONFIG_IMPORT_FAILED — verdict=RED. "
                "Cannot proceed safely without config.py.")

    dedup_str = (
        f"dedup={'strict' if profile['dedup_strict'] else 'permissive'}"
        f"/{profile['dedup_threshold']:.2f}/{profile['dedup_mode']}"
    )
    if profile["dedup_strict_origin"].startswith("env"):
        dedup_str += f"[{profile['dedup_strict_origin']}]"

    return (
        f"[Safety/L4] PROFILE "
        f"{dedup_str} "
        f"ai={'strict' if profile['ai_strict'] else 'permissive'} "
        f"harden={_format_bool(profile['hardened'])} "
        f"scrub={_format_bool(profile['scrub_on'])} "
        f"verify={_format_bool(profile['verify_on'])} "
        f"musicbee={_format_bool(profile['musicbee_on'])} "
        f"acoustid={_format_bool(profile['acoustid_on'])} "
        f"picard={_format_bool(profile['picard_on'])} "
        f"→ VERDICT {profile['verdict']}"
        + (f" (Council-endorsed unattended baseline)" if profile["verdict"] == "GREEN" else
           f" — reasons: {', '.join(profile['verdict_reasons'])}" if profile["verdict_reasons"] else "")
    )


def emit_banner(force: bool = False) -> Optional[dict]:
    """Emit the aggregated platform safety banner. Idempotent: only fires
    once per process unless force=True (for tests).

    Returns the profile dict for inspection. None on no-op (already emitted).
    """
    global _EMITTED
    if _EMITTED and not force:
        return None
    profile = build_profile()
    line = format_banner_line(profile)
    if profile.get("verdict") == "RED":
        log.error(line)
    elif profile.get("verdict") == "AMBER":
        log.warning(line)
    else:
        log.info(line)
    _EMITTED = True
    return profile


def reset_for_testing() -> None:
    """Test-only helper: reset the once-per-process guard."""
    global _EMITTED
    _EMITTED = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    profile = emit_banner(force=True)
    if profile is not None:
        print(f"\nProfile dict: {profile}")
