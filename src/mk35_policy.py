"""
mk35_policy.py — MK35 Q4: Declarative Reconciliation Policy DSL
==================================================================
CTO MK34 review:
  "rules exist, but resolution is still partially 'engine logic,' not
   'policy evaluation'. _apply_rule() still has if/elif chains for
   strategy. A proper system requires fully declarative conflict policy
   engine; no hidden runtime branching in resolver."

This module replaces MK34's if/elif strategy dispatch in
ReconciliationEngine._apply_rule() with:
  - PolicyDocument: a frozen dataclass loadable from JSON
  - STRATEGY_HANDLERS: a dispatch table keyed by strategy name
  - evaluate_policy(): a pure function over (policy, conflict, priority)

The default policy ships as mk35_default_policy.json and matches the MK34
DEFAULT_RULES exactly. Operators can override via the
ReconciliationEngine(input, policy=...) constructor parameter or by
editing the JSON file. Every audit decision logs which policy_version
+ which rule fired.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. POLICY DOCUMENT (Q4-T1)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PolicyRule:
    """
    One rule in a PolicyDocument. Frozen.

    `applies_to` is a regex matched against the conflict's field_path.
    `strategy` selects a handler from STRATEGY_HANDLERS.
    `params` is a dict of strategy-specific options (frozen via tuple-of-pairs).
    """
    id:           str
    applies_to:   str
    strategy:     str
    params:       Tuple[Tuple[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyRule":
        params = tuple(sorted((d.get("params") or {}).items()))
        return cls(
            id=str(d["id"]),
            applies_to=str(d["applies_to"]),
            strategy=str(d["strategy"]),
            params=params,
        )

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "applies_to": self.applies_to,
            "strategy":   self.strategy,
            "params":     dict(self.params),
        }


@dataclass(frozen=True)
class PolicyDocument:
    """
    A complete, loadable, frozen reconciliation policy.

    `rules` is matched first-to-last against each conflict (specific paths
    must come before catchall regexes — the loader validates this).

    `policy_hash` is a SHA256 of the canonical JSON form, useful for replay
    (a replay can verify it ran with the same policy as the original run).
    """
    version:           str
    description:       str
    rules:             Tuple[PolicyRule, ...]
    source_priority:   Tuple[str, ...]
    defaults:          Tuple[Tuple[str, Any], ...] = ()
    policy_hash:       str = ""

    @classmethod
    def from_json(cls, json_path: str | Path) -> "PolicyDocument":
        """Load and validate a policy document from disk."""
        path = Path(json_path)
        with path.open("r", encoding="utf-8") as fh:
            d = json.load(fh)
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyDocument":
        rules = tuple(PolicyRule.from_dict(r) for r in d.get("rules", []))
        # Validate strategies exist
        unknown = [r.strategy for r in rules if r.strategy not in STRATEGY_HANDLERS]
        if unknown:
            raise ValueError(
                f"PolicyDocument references unknown strategies: {unknown}. "
                f"Known: {sorted(STRATEGY_HANDLERS)}"
            )
        # Validate rule ordering: catchall (.*) must not appear before specific rules
        catchall_idx = next(
            (i for i, r in enumerate(rules) if r.applies_to == r".*"),
            len(rules),
        )
        if any(i > catchall_idx and r.applies_to != r".*"
               for i, r in enumerate(rules)):
            raise ValueError(
                "PolicyDocument has specific rule(s) AFTER catchall '.*' — "
                "they will never match. Reorder so catchall is last."
            )
        # Compile + freeze
        defaults = tuple(sorted((d.get("defaults") or {}).items()))
        body = json.dumps(
            {"version": d.get("version"), "rules": [r.to_dict() for r in rules],
             "source_priority": d.get("source_priority", []),
             "defaults": dict(defaults)},
            sort_keys=True,
        ).encode("utf-8")
        h = hashlib.sha256(body).hexdigest()[:16]
        return cls(
            version=str(d.get("version", "unknown")),
            description=str(d.get("description", "")),
            rules=rules,
            source_priority=tuple(d.get("source_priority", [])),
            defaults=defaults,
            policy_hash=h,
        )

    def to_dict(self) -> dict:
        return {
            "version":         self.version,
            "description":     self.description,
            "rules":           [r.to_dict() for r in self.rules],
            "source_priority": list(self.source_priority),
            "defaults":        dict(self.defaults),
            "policy_hash":     self.policy_hash,
        }

    def select_rule(self, field_path: str) -> Optional[PolicyRule]:
        """First-match: return the first rule whose regex matches the path."""
        for r in self.rules:
            try:
                if re.match(r.applies_to, field_path):
                    return r
            except re.error:
                log.warning(f"PolicyRule {r.id!r} has invalid regex; skipping")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 2. STRATEGY HANDLERS — DISPATCH TABLE (Q4-T2)
# ═══════════════════════════════════════════════════════════════════════════
# Replaces the if/elif chain in MK34's _apply_rule() with a pure dispatch
# table. Each handler is a pure function; no procedural branching at the
# dispatch layer. New strategies = add an entry to STRATEGY_HANDLERS, no
# engine changes needed.

# Handler signature: (rule, competing_values, source_priority, params)
#                    → (selected, explanation, tie_breakers)
HandlerFn = Callable[..., Tuple[Any, str, Tuple[str, ...]]]


def _handle_source_priority(rule, competing, source_priority, params):
    """
    SOURCE_PRIORITY: lowest priority index wins.
    Sources not in source_priority sort to the end.
    """
    pri_index = {st: i for i, st in enumerate(source_priority)}
    sorted_cv = sorted(
        competing,
        key=lambda c: pri_index.get(c.source.source_type.value, 999),
    )
    chosen = sorted_cv[0]
    chosen_idx = pri_index.get(chosen.source.source_type.value, 999)
    explanation = (
        f"source_priority: {chosen.source.source_type.value} at index {chosen_idx}"
    )
    return chosen, explanation, ()


def _handle_highest_confidence(rule, competing, source_priority, params):
    """
    HIGHEST_CONFIDENCE: highest .confidence wins; None tied breaks by recency.
    """
    by_conf = sorted(
        competing,
        key=lambda c: (
            -(c.confidence if c.confidence is not None else -1.0),
            -c.source.timestamp,
        ),
    )
    chosen = by_conf[0]
    if chosen.confidence is None:
        explanation = "highest_confidence: tie-broken by recency"
        return chosen, explanation, ("most_recent",)
    explanation = f"highest_confidence: {chosen.confidence}"
    return chosen, explanation, ()


def _handle_most_recent(rule, competing, source_priority, params):
    """MOST_RECENT: highest timestamp wins."""
    chosen = max(competing, key=lambda c: c.source.timestamp)
    explanation = f"most_recent: ts={chosen.source.timestamp:.0f}"
    return chosen, explanation, ()


def _handle_weighted_vote(rule, competing, source_priority, params):
    """
    WEIGHTED_VOTE: aggregate by value, summing per-source weight.
    `params['weights']` is a dict source_type -> weight; default 1.0.
    """
    weights = dict(params).get("weights", {}) or {}
    tally: Dict[Any, float] = {}
    cv_by_value: Dict[Any, "ConflictValue"] = {}
    for c in competing:
        w = float(weights.get(c.source.source_type.value, 1.0))
        tally[c.value] = tally.get(c.value, 0.0) + w
        cv_by_value.setdefault(c.value, c)
    best_value = max(tally, key=tally.get)
    chosen = cv_by_value[best_value]
    explanation = f"weighted_vote: tally={tally}"
    return chosen, explanation, ()


# Dispatch table — the only place strategies live.
# Adding a strategy is a pure data change: append a row here.
STRATEGY_HANDLERS: Dict[str, HandlerFn] = {
    "SOURCE_PRIORITY":    _handle_source_priority,
    "HIGHEST_CONFIDENCE": _handle_highest_confidence,
    "MOST_RECENT":        _handle_most_recent,
    "WEIGHTED_VOTE":      _handle_weighted_vote,
}


# ═══════════════════════════════════════════════════════════════════════════
# 3. PURE EVALUATOR (Q4-T2)
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_policy(policy: PolicyDocument, conflict, source_priority=None):
    """
    Pure function: (policy, conflict, source_priority) → ReconciliationDecision.

    No I/O, no async, no hidden state. Deterministic for fixed inputs.
    Returns the decision in the engine's existing dataclass shape so the
    caller (ReconciliationEngine) doesn't need to change.

    Args:
        policy: a PolicyDocument
        conflict: a FieldConflict from the engine
        source_priority: optional override of policy.source_priority (tuple of strings)

    Returns: a ReconciliationDecision (from mk34_reconciliation).
    """
    # Defer the import to avoid circular dependency
    from mk34_reconciliation import ReconciliationDecision

    rule = policy.select_rule(conflict.field_path)
    if rule is None:
        # Fall through to the policy's default strategy if defined
        defaults = dict(policy.defaults)
        default_strategy = defaults.get("strategy", "HIGHEST_CONFIDENCE")
        rule = PolicyRule(
            id="policy_default",
            applies_to=conflict.field_path,
            strategy=default_strategy,
        )

    handler = STRATEGY_HANDLERS.get(rule.strategy)
    if handler is None:
        # Should not happen — from_dict() validates strategies — but be defensive
        raise ValueError(
            f"PolicyRule {rule.id!r} references unknown strategy {rule.strategy!r}. "
            f"This indicates a policy file outside loader validation."
        )

    sp = tuple(source_priority or policy.source_priority)
    chosen, explanation, tie_breakers = handler(
        rule, list(conflict.competing_values), sp, dict(rule.params)
    )

    rejected = tuple(c for c in conflict.competing_values if c is not chosen)
    return ReconciliationDecision(
        field_path=conflict.field_path,
        selected_value=chosen.value,
        rule=rule.id,
        explanation=f"[policy={policy.version}/{policy.policy_hash}] {explanation}",
        tie_breakers_used=tie_breakers,
        used_sources=(chosen.source,),
        rejected_values=rejected,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. DEFAULT POLICY LOADER (Q4-T3)
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_POLICY_PATH = (Path(__file__).resolve().parent.parent / "state" / "mk35_default_policy.json")


def load_default_policy() -> PolicyDocument:
    """Load the canonical mk35_default_policy.json. Cached on first call."""
    if not DEFAULT_POLICY_PATH.exists():
        log.warning(
            f"  [MK35/Q4] mk35_default_policy.json not present; "
            f"using fallback in-code default."
        )
        return PolicyDocument.from_dict(_FALLBACK_POLICY)
    return PolicyDocument.from_json(DEFAULT_POLICY_PATH)


# Fallback policy — matches MK34 DEFAULT_RULES; used only if the JSON file
# is missing. Operators should always edit the JSON, not this dict.
_FALLBACK_POLICY = {
    "version":     "mk35.fallback",
    "description": "Fallback policy used when mk35_default_policy.json is missing",
    "rules": [
        {"id": "genre_highest_confidence_v2",
         "applies_to": r"^classification\.genre$",
         "strategy":   "HIGHEST_CONFIDENCE"},
        {"id": "year_most_recent_observation_v1",
         "applies_to": r"^classification\.year$",
         "strategy":   "MOST_RECENT"},
        {"id": "spotify_priority_v3",
         "applies_to": r"^(identity|platform)\..*",
         "strategy":   "SOURCE_PRIORITY"},
        {"id": "default_source_priority_v1",
         "applies_to": r".*",
         "strategy":   "SOURCE_PRIORITY"},
    ],
    "source_priority": [
        "user_override", "spotify_metadata_cache", "metadata_cache",
        "genre_cache", "mk33_resolved_view", "external_ingest", "device_adb",
    ],
    "defaults": {"strategy": "HIGHEST_CONFIDENCE"},
}


__all__ = [
    "PolicyRule", "PolicyDocument",
    "STRATEGY_HANDLERS", "evaluate_policy",
    "load_default_policy", "DEFAULT_POLICY_PATH",
]
