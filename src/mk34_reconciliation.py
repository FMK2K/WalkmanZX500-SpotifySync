"""
mk34_reconciliation.py — MK34 Reconciliation Engine (CTO Schema v34.0)
========================================================================
The CTO board's diagnosis of MK33: "80% engineering maturity, 20% architectural
mismatch — that 20% determines 100% of failures." The mismatch was treating a
non-deterministic external system (MTP/COM/ADB/Spotify) as a deterministic
pipeline target.

MK34 implements the reconciliation engine described in the v34.0 schema:

    Raw heterogeneous music metadata
        → deterministic, explainable, auditable canonical dataset

Core invariants (CTO motto):
    "No field exists without provenance.
     No merge exists without explanation.
     No write exists without justification."

This module implements P1 of MK34: the typed core. Other modules implement
the rest of the architecture:
    mk34_observer.py    — ADB-first single observe() (Class B fix)
    mk34_planner.py     — pure-function diff planner
    mk34_executor.py    — serial executor + content-hash verify (Class A fix)
    mk34_invariants.py  — hard-stop assertions (Class C fix)
    mk34_replay.py      — deterministic replay from audit log (P5-T2)

Bridges from MK33: ResolvedView (genre_client.ResolvedView) feeds into
NormalizedTrack via from_resolved_view(). The MK33 evidence projection is
the input to the reconciliation engine; the engine's output replaces the
single chosen value with a fully traced decision.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Tries to read the policy version from MK33 config; falls back gracefully
try:
    from config import POLICY_VERSION as _POLICY_VERSION  # type: ignore
except Exception:
    _POLICY_VERSION = "mk34.unstamped"


# ═══════════════════════════════════════════════════════════════════════════
# 1. SOURCE LAYER TYPES (schema §1)
# ═══════════════════════════════════════════════════════════════════════════

class SourceType(str, Enum):
    """Source type enum — string-valued for direct JSON serialization."""
    SPOTIFY_METADATA_CACHE = "spotify_metadata_cache"
    GENRE_CACHE            = "genre_cache"
    METADATA_CACHE         = "metadata_cache"
    USER_OVERRIDE          = "user_override"
    EXTERNAL_INGEST        = "external_ingest"
    DEVICE_ADB             = "device_adb"           # MK34 addition: device-side observed
    MK33_RESOLVED_VIEW     = "mk33_resolved_view"   # MK34 addition: MK33 bridge


@dataclass(frozen=True)
class SourceRef:
    """Schema §1: SourceRef. Immutable, hashable, JSON-serializable."""
    source_type:  SourceType
    source_id:    str
    timestamp:    float                # epoch seconds
    version:      str = "mk34.0"

    def to_dict(self) -> dict:
        return {"sourceType": self.source_type.value, "sourceId": self.source_id,
                "timestamp": self.timestamp, "version": self.version}


# ═══════════════════════════════════════════════════════════════════════════
# 2. RAW INPUT MODEL (schema §2)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RawRecord:
    """Schema §2: RawRecord — a snapshot from one source for one canonical key."""
    key:     str                       # e.g. "artist|title"
    payload: dict                      # arbitrary, source-specific shape
    source:  SourceRef


# ═══════════════════════════════════════════════════════════════════════════
# 3. NORMALIZED TRACK + 4. FIELD PROVENANCE (schema §3+§4)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FieldProvenance:
    """
    Schema §4: every field's value carries the sources that produced it,
    an aggregated confidence, and an updated-at timestamp.
    """
    value:         Any
    sources:       Tuple[SourceRef, ...]   # tuple → hashable; immutable
    confidence:    Optional[float]         # None for evaluability=verifiable
    last_updated:  float

    def to_dict(self) -> dict:
        return {"value": self.value,
                "sources": [s.to_dict() for s in self.sources],
                "confidence": self.confidence,
                "lastUpdated": self.last_updated}


@dataclass(frozen=True)
class AnomalySignal:
    """Schema §7: typed anomaly types — bounded set."""
    type:        str    # "genre_mismatch" | "artist_collision" | etc.
    severity:    float  # 0.0–1.0
    description: str

    def to_dict(self) -> dict: return asdict(self)


@dataclass(frozen=True)
class IntegrityFlags:
    """Schema §7: per-track integrity + audit flags."""
    has_missing_critical_fields: bool = False
    has_conflicts:               bool = False
    is_fully_reconciled:         bool = True
    requires_human_review:       bool = False
    anomaly_signals:             Tuple[AnomalySignal, ...] = ()

    def to_dict(self) -> dict:
        return {"hasMissingCriticalFields": self.has_missing_critical_fields,
                "hasConflicts": self.has_conflicts,
                "isFullyReconciled": self.is_fully_reconciled,
                "requiresHumanReview": self.requires_human_review,
                "anomalySignals": [a.to_dict() for a in self.anomaly_signals]}


@dataclass(frozen=True)
class NormalizedTrack:
    """
    Schema §3: the single truth target structure before resolution decisions.
    Provenance is field-keyed so 'why does this track have this genre' is
    answerable from the track alone.
    """
    track_key:    str
    identity:     dict                    # {title, artist, album, albumArtist}
    classification: dict                  # {genre, year}
    platform:     dict                    # {spotifyId, isLiked, albumImage}
    provenance:   Dict[str, FieldProvenance]  # path -> provenance
    integrity:    IntegrityFlags = field(default_factory=IntegrityFlags)

    def to_dict(self) -> dict:
        return {"trackKey": self.track_key,
                "identity": self.identity,
                "classification": self.classification,
                "platform": self.platform,
                "provenance": {k: v.to_dict() for k, v in self.provenance.items()},
                "integrity": self.integrity.to_dict()}

    @classmethod
    def from_resolved_view(cls, track_key: str, resolved_view: dict,
                           identity: dict | None = None) -> "NormalizedTrack":
        """
        MK34 P1-T3 bridge: hydrate a NormalizedTrack from MK33 ResolvedView.
        Used during the migration window when both engines coexist.
        """
        all_evidence = resolved_view.get("all_evidence", [])
        chosen       = resolved_view.get("chosen", "")
        ts           = float(resolved_view.get("ts", time.time()))

        sources = tuple(
            SourceRef(
                source_type = SourceType.GENRE_CACHE,
                source_id   = ev.get("source", "unknown"),
                timestamp   = ts,
                version     = resolved_view.get("policy_version", "mk33.unknown"),
            )
            for ev in all_evidence
        )
        prov = {
            "classification.genre": FieldProvenance(
                value=chosen, sources=sources,
                confidence=None, last_updated=ts,
            )
        }
        return cls(
            track_key=track_key,
            identity=identity or {},
            classification={"genre": chosen},
            platform={},
            provenance=prov,
            integrity=IntegrityFlags(
                has_conflicts=(resolved_view.get("severity") in ("categorical", "novel")),
                is_fully_reconciled=(resolved_view.get("severity") in ("trivial", "taxonomic")),
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. CONFLICT GRAPH (schema §5)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ConflictValue:
    """Schema §5: one competing value in a field-level conflict."""
    value:      Any
    source:     SourceRef
    confidence: Optional[float]

    def to_dict(self) -> dict:
        return {"value": self.value, "source": self.source.to_dict(),
                "confidence": self.confidence}


@dataclass(frozen=True)
class FieldConflict:
    """Schema §5: a contradiction at a specific field path."""
    field_path:       str             # "classification.genre"
    competing_values: Tuple[ConflictValue, ...]
    severity:         str             # "low" | "medium" | "high" | "blocking"

    def to_dict(self) -> dict:
        return {"fieldPath": self.field_path,
                "competingValues": [c.to_dict() for c in self.competing_values],
                "severity": self.severity}


# ═══════════════════════════════════════════════════════════════════════════
# 6. RECONCILIATION DECISION (schema §6)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ReconciliationDecision:
    """
    Schema §6: every resolved field must explain itself.
    'rule' is the canonical rule id; 'tie_breakers_used' lists secondary
    rules that mattered.
    """
    field_path:       str
    selected_value:   Any
    rule:             str            # e.g. "spotify_priority_v3"
    explanation:      str
    tie_breakers_used: Tuple[str, ...]
    used_sources:     Tuple[SourceRef, ...]
    rejected_values:  Tuple[ConflictValue, ...]

    def to_dict(self) -> dict:
        return {"fieldPath": self.field_path,
                "selectedValue": self.selected_value,
                "reasoning": {"rule": self.rule, "explanation": self.explanation,
                              "tieBreakersUsed": list(self.tie_breakers_used)},
                "usedSources": [s.to_dict() for s in self.used_sources],
                "rejectedValues": [r.to_dict() for r in self.rejected_values]}


# ═══════════════════════════════════════════════════════════════════════════
# 10. AUDIT EVENT STREAM (schema §10)
# ═══════════════════════════════════════════════════════════════════════════

class AuditEventType(str, Enum):
    INGEST            = "INGEST"
    NORMALIZE         = "NORMALIZE"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    DECISION_MADE     = "DECISION_MADE"
    WRITEBACK         = "WRITEBACK"


@dataclass(frozen=True)
class AuditEvent:
    """Schema §10: append-only event log row."""
    event_id:  str       # uuid4
    run_id:    str       # process-level run id
    type:      AuditEventType
    payload:   dict
    timestamp: float

    def to_dict(self) -> dict:
        return {"eventId": self.event_id, "runId": self.run_id,
                "type": self.type.value,
                "payload": self.payload, "timestamp": self.timestamp}


# ═══════════════════════════════════════════════════════════════════════════
# 11. RECONCILIATION RULE ENGINE (schema §11)
# ═══════════════════════════════════════════════════════════════════════════

class RuleStrategy(str, Enum):
    HIGHEST_CONFIDENCE = "HIGHEST_CONFIDENCE"
    SOURCE_PRIORITY    = "SOURCE_PRIORITY"
    MOST_RECENT        = "MOST_RECENT"
    WEIGHTED_VOTE      = "WEIGHTED_VOTE"


@dataclass(frozen=True)
class ReconciliationRule:
    """Schema §11: a rule that resolves conflicts at matching field paths."""
    id:              str
    applies_to:      str           # regex matched against field_path
    strategy:        RuleStrategy
    weight_function: Optional[Callable[[SourceRef], float]] = None


# ═══════════════════════════════════════════════════════════════════════════
# 8 + 9 + 12. ENGINE INPUT, OUTPUT, AND CONTRACT (schema §8, §9, §12)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MK34InputBundle:
    """Schema §8: input to the engine. Only mutable in this top level."""
    records:           List[RawRecord]
    source_priority:   Tuple[SourceType, ...] = (
        SourceType.USER_OVERRIDE,
        SourceType.SPOTIFY_METADATA_CACHE,
        SourceType.METADATA_CACHE,
        SourceType.GENRE_CACHE,
        SourceType.MK33_RESOLVED_VIEW,
        SourceType.EXTERNAL_INGEST,
        SourceType.DEVICE_ADB,
    )
    allow_inference:   bool = True
    strict_mode:       bool = False
    run_id:            str = ""
    version:           str = "MK34.0"

    def __post_init__(self):
        if not self.run_id:
            self.run_id = f"mk34-{int(time.time())}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class MK34ReconciledOutput:
    """Schema §9: final state."""
    tracks:        Tuple[NormalizedTrack, ...]
    decisions:     Tuple[ReconciliationDecision, ...]
    conflicts:     Tuple[FieldConflict, ...]
    audit_trail:   Tuple[AuditEvent, ...]
    summary:       dict
    run_id:        str

    def to_dict(self) -> dict:
        return {"tracks":    [t.to_dict() for t in self.tracks],
                "decisions": [d.to_dict() for d in self.decisions],
                "conflicts": [c.to_dict() for c in self.conflicts],
                "auditTrail":[a.to_dict() for a in self.audit_trail],
                "summary":   self.summary,
                "runId":     self.run_id}


# ─── Default rule registry ──────────────────────────────────────────────────

# Default rules — order matters: _select_rule returns the FIRST matching rule,
# so specific paths must come before the catchall.
DEFAULT_RULES: Tuple[ReconciliationRule, ...] = (
    # Genre: highest confidence wins (defers to MK33 evaluability/authority via
    # the FieldProvenance.confidence field; ties broken by recency).
    ReconciliationRule(
        id="genre_highest_confidence_v2",
        applies_to=r"^classification\.genre$",
        strategy=RuleStrategy.HIGHEST_CONFIDENCE,
    ),
    # Year: most recent observation. Spotify can correct old metadata; we prefer
    # the most recent authoritative observation.
    ReconciliationRule(
        id="year_most_recent_observation_v1",
        applies_to=r"^classification\.year$",
        strategy=RuleStrategy.MOST_RECENT,
    ),
    # Identity + Platform: source priority. Spotify's title/artist/spotifyId is
    # canonical; user_override (priority 0) always wins when present.
    ReconciliationRule(
        id="spotify_priority_v3",
        applies_to=r"^(identity|platform)\..*",
        strategy=RuleStrategy.SOURCE_PRIORITY,
    ),
    # Catchall: anything else uses source priority. user_override still wins.
    ReconciliationRule(
        id="default_source_priority_v1",
        applies_to=r".*",
        strategy=RuleStrategy.SOURCE_PRIORITY,
    ),
)


# ═══════════════════════════════════════════════════════════════════════════
# THE ENGINE (schema §12: MK34ReconciliationEngine)
# ═══════════════════════════════════════════════════════════════════════════

class ReconciliationEngine:
    """
    Schema §12: the full system contract.

    Lifecycle:
        1. ingest()         — accept raw heterogeneous records
        2. normalize()      — produce NormalizedTrack with provenance
        3. detectConflicts()— compute field-level conflict graph
        4. resolve()        — apply rules, produce ReconciliationDecisions
        5. audit()          — emit AuditEvent stream
        6. export()         — package as MK34ReconciledOutput

    The engine is deterministic: same input + same rules + same priority
    order = same output. This is what enables replay (mk34_replay.py).
    """

    def __init__(self,
                 input_bundle: MK34InputBundle,
                 rules: Tuple[ReconciliationRule, ...] = DEFAULT_RULES,
                 audit_log_path: Path | None = None,
                 policy=None):
        self.input      = input_bundle
        self.rules      = rules
        self.policy     = policy            # MK35 Q4: optional declarative policy
        self._audit:    List[AuditEvent] = []
        self._audit_lock = Lock()
        self._audit_log_path = audit_log_path or Path(".mk34_audit.jsonl")
        self._raw:       List[RawRecord] = []
        self._tracks:    List[NormalizedTrack] = []
        self._conflicts: List[FieldConflict] = []
        self._decisions: List[ReconciliationDecision] = []

        # MK35 Q3-T1: persist the input bundle for pure replay
        try:
            from mk35_pure_replay import persist_input_bundle
            persist_input_bundle(input_bundle)
        except ImportError:
            pass  # MK35 not installed; engine still works (degraded replay)

    # ── audit helpers ────────────────────────────────────────────────────

    def _emit(self, event_type: AuditEventType, payload: dict) -> None:
        """Append-only audit emission. Persists to JSONL immediately."""
        ev = AuditEvent(
            event_id  = uuid.uuid4().hex,
            run_id    = self.input.run_id,
            type      = event_type,
            payload   = payload,
            timestamp = time.time(),
        )
        with self._audit_lock:
            self._audit.append(ev)
            try:
                with self._audit_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev.to_dict()) + "\n")
            except Exception as e:
                log.warning(f"[MK34/audit] persist failed (non-fatal): {e}")

    # ── §12.1: ingest ────────────────────────────────────────────────────

    def ingest(self) -> List[RawRecord]:
        """
        Accept raw records, validate basic shape, emit INGEST events.
        Returns the validated list (input may have been filtered).
        """
        validated: List[RawRecord] = []
        for r in self.input.records:
            if not isinstance(r, RawRecord):
                log.warning(f"[MK34/ingest] non-RawRecord skipped: {type(r).__name__}")
                continue
            if not r.key or not isinstance(r.payload, dict):
                log.warning(f"[MK34/ingest] malformed record skipped: key={r.key!r}")
                continue
            validated.append(r)
        self._raw = validated
        self._emit(AuditEventType.INGEST, {
            "records_in":  len(self.input.records),
            "records_out": len(validated),
            "rejected":    len(self.input.records) - len(validated),
        })
        return validated

    # ── §12.2: normalize ─────────────────────────────────────────────────

    def normalize(self, records: List[RawRecord] | None = None) -> List[NormalizedTrack]:
        """
        Group raw records by trackKey; for each group, build a NormalizedTrack
        with FieldProvenance for every observed field.
        """
        if records is None:
            records = self._raw

        by_key: Dict[str, List[RawRecord]] = {}
        for r in records:
            by_key.setdefault(r.key, []).append(r)

        tracks: List[NormalizedTrack] = []
        for track_key, group in by_key.items():
            identity, classification, platform = {}, {}, {}
            provenance: Dict[str, FieldProvenance] = {}

            for r in group:
                self._fold_into_provenance(r, identity, classification,
                                           platform, provenance)

            integrity = self._compute_integrity(provenance)
            tracks.append(NormalizedTrack(
                track_key=track_key, identity=identity,
                classification=classification, platform=platform,
                provenance=provenance, integrity=integrity,
            ))

        self._tracks = tracks
        self._emit(AuditEventType.NORMALIZE, {
            "tracks_normalized": len(tracks),
            "input_records":     len(records),
        })
        return tracks

    def _fold_into_provenance(self, r: RawRecord, identity: dict,
                              classification: dict, platform: dict,
                              provenance: Dict[str, FieldProvenance]) -> None:
        """Map a payload's known keys into the canonical structure + provenance."""
        # Identity
        for k_in, k_out in (("title", "title"), ("artist", "artist"),
                             ("album", "album"), ("album_artist", "albumArtist"),
                             ("albumArtist", "albumArtist")):
            if k_in in r.payload and r.payload[k_in] not in (None, ""):
                identity[k_out] = r.payload[k_in]
                self._set_prov(provenance, f"identity.{k_out}",
                               r.payload[k_in], r.source, r.payload.get("_confidence"))
        # Classification
        for k_in, k_out in (("genre", "genre"), ("year", "year")):
            if k_in in r.payload and r.payload[k_in] not in (None, ""):
                classification[k_out] = r.payload[k_in]
                self._set_prov(provenance, f"classification.{k_out}",
                               r.payload[k_in], r.source, r.payload.get("_confidence"))
        # Platform
        for k_in, k_out in (("spotify_id", "spotifyId"), ("spotifyId", "spotifyId"),
                             ("is_liked", "isLiked"), ("isLiked", "isLiked"),
                             ("album_image", "albumImage"), ("albumImage", "albumImage")):
            if k_in in r.payload and r.payload[k_in] is not None:
                platform[k_out] = r.payload[k_in]
                self._set_prov(provenance, f"platform.{k_out}",
                               r.payload[k_in], r.source, None)

    @staticmethod
    def _set_prov(provenance: Dict[str, FieldProvenance], path: str,
                  value: Any, source: SourceRef, confidence: Optional[float]) -> None:
        """Add or merge a field's provenance entry."""
        existing = provenance.get(path)
        if existing is None:
            provenance[path] = FieldProvenance(
                value=value, sources=(source,),
                confidence=confidence, last_updated=source.timestamp,
            )
        else:
            # If same value, append source; if different, leave to detectConflicts
            if existing.value == value:
                provenance[path] = FieldProvenance(
                    value=value,
                    sources=existing.sources + (source,),
                    confidence=max(filter(lambda c: c is not None,
                                          (existing.confidence, confidence)), default=None),
                    last_updated=max(existing.last_updated, source.timestamp),
                )

    @staticmethod
    def _compute_integrity(provenance: Dict[str, FieldProvenance]) -> IntegrityFlags:
        """Heuristics over the field map: critical fields + obvious anomalies."""
        anomalies: List[AnomalySignal] = []
        critical_missing = not any(p.startswith("identity.title") for p in provenance) \
                            or not any(p.startswith("identity.artist") for p in provenance)
        # year sanity
        year = provenance.get("classification.year")
        if year is not None and year.value is not None:
            try:
                y = int(year.value)
                if not (1900 <= y <= 2030):
                    anomalies.append(AnomalySignal(
                        type="year_out_of_range", severity=0.7,
                        description=f"year={y}",
                    ))
            except (TypeError, ValueError):
                anomalies.append(AnomalySignal(
                    type="year_out_of_range", severity=0.5,
                    description=f"non-numeric year: {year.value!r}",
                ))
        return IntegrityFlags(
            has_missing_critical_fields=critical_missing,
            is_fully_reconciled=not critical_missing and not anomalies,
            anomaly_signals=tuple(anomalies),
        )

    # ── §12.3: detectConflicts ───────────────────────────────────────────

    def detect_conflicts(self,
                         tracks: List[NormalizedTrack] | None = None
                        ) -> List[FieldConflict]:
        """
        For each track and each field path, scan the original raw records to
        find disagreements. A conflict is emitted only when ≥2 sources disagree.
        """
        if tracks is None:
            tracks = self._tracks
        conflicts: List[FieldConflict] = []

        # Re-bucket raw by trackKey for cross-record comparison
        raw_by_key: Dict[str, List[RawRecord]] = {}
        for r in self._raw:
            raw_by_key.setdefault(r.key, []).append(r)

        for track in tracks:
            # For each field path that *appeared* across this track's raw records,
            # collect the values + sources, and emit a conflict if ≥2 distinct.
            field_to_values: Dict[str, List[Tuple[Any, SourceRef, Optional[float]]]] = {}
            for r in raw_by_key.get(track.track_key, []):
                for path, value in self._payload_to_paths(r.payload).items():
                    field_to_values.setdefault(path, []).append(
                        (value, r.source, r.payload.get("_confidence"))
                    )
            for path, values in field_to_values.items():
                distinct = {v[0] for v in values if v[0] not in (None, "")}
                if len(distinct) >= 2:
                    cvs = tuple(
                        ConflictValue(value=v, source=s, confidence=c)
                        for (v, s, c) in values
                    )
                    severity = self._classify_severity(path, distinct)
                    conflicts.append(FieldConflict(
                        field_path=path, competing_values=cvs, severity=severity,
                    ))

        self._conflicts = conflicts
        self._emit(AuditEventType.CONFLICT_DETECTED, {
            "conflicts_found": len(conflicts),
            "tracks_scanned":  len(tracks),
        })
        return conflicts

    @staticmethod
    def _payload_to_paths(payload: dict) -> Dict[str, Any]:
        """Flatten a payload to canonical field paths."""
        out = {}
        for k, v in payload.items():
            if k.startswith("_"):     # private fields like _confidence
                continue
            if k in ("title", "artist", "album"):
                out[f"identity.{k}"] = v
            elif k in ("album_artist", "albumArtist"):
                out["identity.albumArtist"] = v
            elif k in ("genre", "year"):
                out[f"classification.{k}"] = v
            elif k in ("spotify_id", "spotifyId"):
                out["platform.spotifyId"] = v
            elif k in ("is_liked", "isLiked"):
                out["platform.isLiked"] = v
            elif k in ("album_image", "albumImage"):
                out["platform.albumImage"] = v
        return out

    @staticmethod
    def _classify_severity(path: str, distinct_values: set) -> str:
        """Severity for a conflict given the field path and distinct value set."""
        if path == "identity.title" or path == "identity.artist":
            return "blocking"  # different titles for same trackKey → can't reconcile
        if path == "platform.spotifyId":
            return "blocking"  # two spotify ids for same key → data corruption
        if path == "classification.genre":
            # Defer to MK32 N3 family-based severity if available
            try:
                from genre_client import classify_conflict_severity  # type: ignore
                vals = sorted(str(v) for v in distinct_values)
                sev = classify_conflict_severity(vals[0], vals[1])
                return {"trivial": "low", "taxonomic": "low",
                        "novel": "medium", "categorical": "high"}.get(sev, "medium")
            except Exception:
                return "medium"
        return "medium"

    # ── §12.4: resolve ───────────────────────────────────────────────────

    def resolve(self,
                tracks: List[NormalizedTrack] | None = None,
                conflicts: List[FieldConflict] | None = None
               ) -> Tuple[List[NormalizedTrack], List[ReconciliationDecision]]:
        """
        Apply rules to each conflict; produce a ReconciliationDecision per
        resolved field. Tracks are not mutated here (they're frozen);
        decisions feed the export step.
        """
        if tracks is None:    tracks    = self._tracks
        if conflicts is None: conflicts = self._conflicts

        decisions: List[ReconciliationDecision] = []
        for conflict in conflicts:
            rule = self._select_rule(conflict.field_path)
            decision = self._apply_rule(rule, conflict)
            decisions.append(decision)
            self._emit(AuditEventType.DECISION_MADE, {
                "fieldPath": conflict.field_path,
                "rule": decision.rule,
                "selectedValue": decision.selected_value,
                "rejected_count": len(decision.rejected_values),
            })

        self._decisions = decisions
        return tracks, decisions

    def _select_rule(self, field_path: str) -> ReconciliationRule:
        """First matching rule wins. Caller ensures rules are ordered by precedence."""
        for r in self.rules:
            if re.match(r.applies_to, field_path):
                return r
        # Default: highest confidence
        return ReconciliationRule(id="default_v1", applies_to=r".*",
                                   strategy=RuleStrategy.HIGHEST_CONFIDENCE)

    def _apply_rule(self, rule: ReconciliationRule,
                    conflict: FieldConflict) -> ReconciliationDecision:
        """
        Apply a rule to a conflict; return the decision with explanation.

        MK35 Q4: when self.policy is provided, the if/elif strategy chain is
        replaced by the pure policy interpreter (mk35_policy.evaluate_policy).
        Otherwise, the original MK34 dispatch is preserved for backward
        compatibility.
        """
        # MK35 Q4-T2: declarative policy dispatch path
        if self.policy is not None:
            try:
                from mk35_policy import evaluate_policy
                return evaluate_policy(self.policy, conflict, self.input.source_priority)
            except Exception as e:
                log.warning(
                    f"  [MK34] policy evaluator failed ({e!r}); falling back to "
                    f"MK34 if/elif dispatch."
                )
                # fall through to legacy path

        # Legacy MK34 dispatch (preserved for backward compat / fallback)
        cvs = list(conflict.competing_values)
        if rule.strategy == RuleStrategy.SOURCE_PRIORITY:
            priority = list(self.input.source_priority)
            cvs.sort(key=lambda c: priority.index(c.source.source_type)
                     if c.source.source_type in priority else 999)
            chosen = cvs[0]
            explanation = (
                f"source_priority: {chosen.source.source_type.value} "
                f"is at priority index {priority.index(chosen.source.source_type) if chosen.source.source_type in priority else 999}"
            )
            tie_breakers = ()
        elif rule.strategy == RuleStrategy.HIGHEST_CONFIDENCE:
            cvs.sort(key=lambda c: -(c.confidence if c.confidence is not None else 0.0))
            chosen = cvs[0]
            explanation = f"highest_confidence: {chosen.confidence}"
            tie_breakers = ("most_recent",) if chosen.confidence is None else ()
            if chosen.confidence is None:   # tie-break by recency
                cvs.sort(key=lambda c: -c.source.timestamp)
                chosen = cvs[0]
        elif rule.strategy == RuleStrategy.MOST_RECENT:
            cvs.sort(key=lambda c: -c.source.timestamp)
            chosen = cvs[0]
            explanation = f"most_recent: ts={chosen.source.timestamp:.0f}"
            tie_breakers = ()
        elif rule.strategy == RuleStrategy.WEIGHTED_VOTE:
            wf = rule.weight_function or (lambda s: 1.0)
            tally: Dict[Any, float] = {}
            for c in cvs:
                tally[c.value] = tally.get(c.value, 0.0) + wf(c.source)
            best_value = max(tally, key=tally.get)
            chosen = next(c for c in cvs if c.value == best_value)
            explanation = f"weighted_vote: tally={tally}"
            tie_breakers = ()
        else:
            chosen = cvs[0]
            explanation = f"fallback (unknown strategy: {rule.strategy})"
            tie_breakers = ()

        rejected = tuple(c for c in conflict.competing_values if c is not chosen)
        return ReconciliationDecision(
            field_path=conflict.field_path,
            selected_value=chosen.value,
            rule=rule.id,
            explanation=explanation,
            tie_breakers_used=tie_breakers,
            used_sources=(chosen.source,),
            rejected_values=rejected,
        )

    # ── §12.5: audit ─────────────────────────────────────────────────────

    def audit(self) -> List[AuditEvent]:
        """Return the in-memory audit event stream."""
        with self._audit_lock:
            return list(self._audit)

    # ── §12.6: export ────────────────────────────────────────────────────

    def export(self) -> MK34ReconciledOutput:
        """Package the engine's outputs as MK34ReconciledOutput."""
        resolved_fully = sum(1 for t in self._tracks if t.integrity.is_fully_reconciled)
        with_conflicts = sum(1 for t in self._tracks if t.integrity.has_conflicts)
        partial        = len(self._tracks) - resolved_fully - with_conflicts

        summary = {
            "totalTracks": len(self._tracks),
            "resolvedFully": resolved_fully,
            "partial": partial,
            "failed": with_conflicts,
            "policyVersion": _POLICY_VERSION,
        }
        # Emit WRITEBACK BEFORE assembling output so audit_trail includes it
        self._emit(AuditEventType.WRITEBACK, {"summary": summary})

        return MK34ReconciledOutput(
            tracks=tuple(self._tracks),
            decisions=tuple(self._decisions),
            conflicts=tuple(self._conflicts),
            audit_trail=tuple(self._audit),
            summary=summary,
            run_id=self.input.run_id,
        )

    # ── one-shot lifecycle ───────────────────────────────────────────────

    def run(self) -> MK34ReconciledOutput:
        """Execute the full lifecycle: ingest → normalize → conflicts → resolve → export."""
        self.ingest()
        self.normalize()
        self.detect_conflicts()
        self.resolve()
        return self.export()


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def make_record(key: str, payload: dict, source_type: SourceType,
                source_id: str = "", confidence: float | None = None) -> RawRecord:
    """Convenience builder used by callers feeding the engine."""
    return RawRecord(
        key=key,
        payload={**payload, **({"_confidence": confidence} if confidence is not None else {})},
        source=SourceRef(
            source_type=source_type,
            source_id=source_id or key,
            timestamp=time.time(),
        ),
    )


__all__ = [
    "SourceType", "SourceRef", "RawRecord",
    "FieldProvenance", "AnomalySignal", "IntegrityFlags", "NormalizedTrack",
    "ConflictValue", "FieldConflict",
    "ReconciliationDecision",
    "AuditEventType", "AuditEvent",
    "RuleStrategy", "ReconciliationRule", "DEFAULT_RULES",
    "MK34InputBundle", "MK34ReconciledOutput",
    "ReconciliationEngine",
    "make_record",
]
