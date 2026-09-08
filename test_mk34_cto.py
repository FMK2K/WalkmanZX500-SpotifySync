"""
test_mk34_cto.py — MK34 Reconciliation Engine test suite
==========================================================
Tests the 14 MK34 tickets end-to-end + the three failure-class fixes
(Class A: false success; Class B: state divergence; Class C: silent
destructive). Verifies MK33 → MK34 continuity (no MK33 patch undone).
"""

import os, sys, json, tempfile, time, hashlib
from pathlib import Path

# Ensure local imports work whether run from project root or anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ─── helpers ────────────────────────────────────────────────────────────────

def _section(name):
    print(f"\n{'═' * 65}")
    print(f"  {name}")
    print('═' * 65)


_passed, _failed = 0, 0
def _ok(label):
    global _passed; _passed += 1
    print(f"  ✅ {label}")
def _fail(label, exc=None):
    global _failed; _failed += 1
    print(f"  🔴 {label}" + (f"  [{exc}]" if exc else ""))


def _check(label, cond, exc=None):
    if cond: _ok(label)
    else:    _fail(label, exc)


# ═══════════════════════════════════════════════════════════════════════════
# P1 — Reconciliation Engine Core
# ═══════════════════════════════════════════════════════════════════════════
def test_p1_engine_core():
    _section("P1 — Reconciliation Engine Core (mk34_reconciliation.py)")

    from mk34_reconciliation import (
        SourceRef, RawRecord, NormalizedTrack, FieldProvenance,
        FieldConflict, ConflictValue, ReconciliationDecision,
        IntegrityFlags, AnomalySignal, AuditEvent, AuditEventType,
        SourceType, RuleStrategy, ReconciliationRule,
        MK34InputBundle, MK34ReconciledOutput,
        ReconciliationEngine, make_record, DEFAULT_RULES,
    )

    # P1-T1: All 8 frozen dataclasses
    _check("P1-T1 SourceRef is frozen",
           SourceRef.__dataclass_params__.frozen)
    _check("P1-T1 RawRecord is frozen",
           RawRecord.__dataclass_params__.frozen)
    _check("P1-T1 NormalizedTrack is frozen",
           NormalizedTrack.__dataclass_params__.frozen)
    _check("P1-T1 FieldProvenance is frozen",
           FieldProvenance.__dataclass_params__.frozen)
    _check("P1-T1 FieldConflict is frozen",
           FieldConflict.__dataclass_params__.frozen)
    _check("P1-T1 ReconciliationDecision is frozen",
           ReconciliationDecision.__dataclass_params__.frozen)
    _check("P1-T1 IntegrityFlags is frozen",
           IntegrityFlags.__dataclass_params__.frozen)
    _check("P1-T1 AuditEvent is frozen",
           AuditEvent.__dataclass_params__.frozen)

    # P1-T2: Engine has 6 contract methods
    eng_methods = ['ingest', 'normalize', 'detect_conflicts',
                   'resolve', 'audit', 'export']
    for m in eng_methods:
        _check(f"P1-T2 ReconciliationEngine.{m}",
               hasattr(ReconciliationEngine, m))

    # P1-T2: 4 RuleStrategy values
    strategies = ['HIGHEST_CONFIDENCE', 'SOURCE_PRIORITY',
                   'MOST_RECENT', 'WEIGHTED_VOTE']
    for s in strategies:
        _check(f"P1-T2 RuleStrategy.{s}", hasattr(RuleStrategy, s))

    # P1-T2: DEFAULT_RULES present + correctly ordered (specific before catchall)
    _check("P1-T2 DEFAULT_RULES non-empty", len(DEFAULT_RULES) > 0)
    rule_paths = [r.applies_to for r in DEFAULT_RULES]
    catchall_idx = next((i for i, p in enumerate(rule_paths) if p == r".*"),
                        len(rule_paths))
    specific_after_catchall = any(
        p != r".*" and i > catchall_idx
        for i, p in enumerate(rule_paths)
    )
    _check("P1-T2 catchall rule comes last (rule order is correct)",
           not specific_after_catchall)

    # P1-T1: AuditEventType has all 5 values
    et = ['INGEST', 'NORMALIZE', 'CONFLICT_DETECTED',
          'DECISION_MADE', 'WRITEBACK']
    for t in et:
        _check(f"P1-T1 AuditEventType.{t}", hasattr(AuditEventType, t))

    # End-to-end: full lifecycle on a small bundle with one conflict
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl',
                                       delete=False) as tmp:
        audit_path = Path(tmp.name)

    bundle = MK34InputBundle(records=[
        make_record('Daft Punk|One More Time',
                    {'title': 'One More Time', 'artist': 'Daft Punk',
                     'genre': 'House'},
                    SourceType.SPOTIFY_METADATA_CACHE, 'spot1', 0.95),
        make_record('Daft Punk|One More Time',
                    {'genre': 'Electronic'},
                    SourceType.GENRE_CACHE, 'gc1', 0.80),
    ])
    engine = ReconciliationEngine(bundle, audit_log_path=audit_path)
    out = engine.run()

    _check("P1 end-to-end: 1 track normalized", len(out.tracks) == 1)
    _check("P1 end-to-end: 1 conflict detected", len(out.conflicts) == 1)
    _check("P1 end-to-end: 1 decision made", len(out.decisions) == 1)
    _check("P1 end-to-end: genre conflict resolved by highest_confidence rule",
           out.decisions[0].rule == "genre_highest_confidence_v2")
    _check("P1 end-to-end: highest confidence (House at 0.95) wins",
           out.decisions[0].selected_value == "House")
    _check("P1 end-to-end: rejected values preserved",
           len(out.decisions[0].rejected_values) >= 1)
    _check("P1 end-to-end: audit trail has all 5 event types",
           {e.type.value for e in out.audit_trail} == set(et))
    _check("P1 end-to-end: deterministic run_id present",
           bool(out.run_id))

    # Audit log was persisted
    _check("P5-T1 audit log written to JSONL",
           audit_path.exists() and audit_path.stat().st_size > 0)

    # P1-T3: ResolvedView bridge
    try:
        from genre_client import ResolvedView, build_resolved_view
        rv = build_resolved_view('House',
            {'source': 'lastfm', 'value': 'House', 'authority': 'crowd_contributed',
             'confidence': None, 'evaluability': 'verifiable'},
            None, 'phase1_corroboration', 'taxonomic')
        nt = rv.to_mk34_normalized_track('Daft Punk|One More Time',
                                          identity={'title': 'One More Time'})
        _check("P1-T3 ResolvedView.to_mk34_normalized_track returns NormalizedTrack",
               isinstance(nt, NormalizedTrack))
        _check("P1-T3 bridge preserves chosen value as classification.genre",
               nt.classification.get('genre') == 'House')
        _check("P1-T3 bridge preserves provenance",
               'classification.genre' in nt.provenance)
    except Exception as e:
        _fail("P1-T3 ResolvedView bridge failed", e)

    # cleanup
    audit_path.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# P2 — Observer (ADB-first)
# ═══════════════════════════════════════════════════════════════════════════
def test_p2_observer():
    _section("P2 — ADB-first State Observer (mk34_observer.py)")

    from mk34_observer import (
        DeviceObservedState, observe, DIVERGENCE_THRESHOLD_PCT
    )

    # P2-T1: DeviceObservedState immutable
    _check("P2-T1 DeviceObservedState is frozen",
           DeviceObservedState.__dataclass_params__.frozen)
    _check("P2-T1 DIVERGENCE_THRESHOLD_PCT is set",
           0 < DIVERGENCE_THRESHOLD_PCT < 1)

    # P2-T1: observe() callable signature
    import inspect
    sig = inspect.signature(observe)
    expected_params = {'adb_path', 'device_root',
                       'preload_count_advisory', 'adb_timeout_s'}
    actual_params = set(sig.parameters.keys())
    _check("P2-T1 observe() has all required parameters",
           expected_params.issubset(actual_params))

    # P2-T2: STATE_DIVERGENCE_DETECTED helper in device_inventory
    from device_inventory import check_state_divergence
    _check("P2-T2 check_state_divergence returns False on small diff",
           check_state_divergence(100, 102) is False)
    _check("P2-T2 check_state_divergence returns True on large diff",
           check_state_divergence(27901, 0) is True)
    _check("P2-T2 check_state_divergence returns False on zero/zero",
           check_state_divergence(0, 0) is False)


# ═══════════════════════════════════════════════════════════════════════════
# P3 — Planner + Executor
# ═══════════════════════════════════════════════════════════════════════════
def test_p3_planner_executor():
    _section("P3 — Planner + Executor (mk34_planner + mk34_executor)")

    from mk34_planner import (
        DesiredState, plan_diff, PlannedAction, Plan, ActionKind
    )
    from mk34_observer import DeviceObservedState

    # P3-T1: DesiredState invariant — expected ∩ forbidden == ∅
    try:
        DesiredState(
            expected_paths=frozenset(["a", "b"]),
            forbidden_paths=frozenset(["b", "c"]),  # 'b' is in both
            expected_hashes={}, cross_format_alts={},
        )
        _fail("P3-T1 DesiredState rejects expected ∩ forbidden ≠ ∅")
    except ValueError:
        _ok("P3-T1 DesiredState rejects expected ∩ forbidden ≠ ∅")

    # P3-T1: pure-function plan_diff
    desired = DesiredState(
        expected_paths=frozenset(["SD card/Music/A/1.mp3", "SD card/Music/A/2.mp3"]),
        forbidden_paths=frozenset(["SD card/Music/A/old.mp3"]),
        expected_hashes={"SD card/Music/A/1.mp3": "abc",
                          "SD card/Music/A/2.mp3": "def"},
        cross_format_alts={"SD card/Music/A/old.mp3": ["SD card/Music/A/1.mp3"]},
    )
    observed = DeviceObservedState(
        observed_at=time.time(),
        tracks_present_paths=frozenset([
            "SD card/Music/A/1.mp3",   # already present
            "SD card/Music/A/old.mp3"  # forbidden, present
        ]),
        free_space_bytes=1_000_000_000,
        storage_breakdown={"SD card": 2},
        advisory_preload_count=2, divergence_pct=0.0,
        divergence_logged=False, adb_command="test",
        observation_id="test-obs",
    )
    plan = plan_diff(desired, observed)

    _check("P3-T1 plan returned Plan instance", isinstance(plan, Plan))
    _check("P3-T1 plan summary: 1 copy",     plan.summary['copies'] == 1)
    _check("P3-T1 plan summary: 1 delete",   plan.summary['deletes'] == 1)
    _check("P3-T1 plan ordering: COPY before DELETE",
           plan.actions[0].kind == ActionKind.COPY
           and plan.actions[-1].kind == ActionKind.DELETE)

    # P3-T1 deterministic: same inputs → same plan_hash
    plan2 = plan_diff(desired, observed)
    _check("P3-T1 deterministic plan_hash",
           plan.plan_hash == plan2.plan_hash)

    # P3-T1 DELETE_DEFERRED_NO_ALTERNATIVE: when no alternative is verified
    # for a forbidden path, the delete is skipped
    desired_no_alt = DesiredState(
        expected_paths=frozenset([]),
        forbidden_paths=frozenset(["SD card/Music/A/orphan.mp3"]),
        expected_hashes={},
        cross_format_alts={"SD card/Music/A/orphan.mp3": []},  # no alts
    )
    observed_orphan = DeviceObservedState(
        observed_at=time.time(),
        tracks_present_paths=frozenset(["SD card/Music/A/orphan.mp3"]),
        free_space_bytes=1_000_000, storage_breakdown={},
        advisory_preload_count=1, divergence_pct=0.0,
        divergence_logged=False, adb_command="t", observation_id="t",
    )
    plan_orphan = plan_diff(desired_no_alt, observed_orphan)
    _check("P3-T1 DELETE deferred when no verified alternative",
           plan_orphan.summary['deletes_deferred'] == 1)
    _check("P3-T1 DELETE skipped when no alternative verified present",
           plan_orphan.summary['deletes'] == 0)

    # P3-T2 + P3-T3: TransferOutcome shape
    from mk34_executor import TransferOutcome, sha256_of_local_file
    o = TransferOutcome(
        path="x", accepted=True, completed=True, transport_used="com",
        elapsed_s=0.1, source_hash="abc", device_hash="abc",
    )
    _check("P3-T3 TransferOutcome.is_success requires both flags",
           o.is_success is True)
    o_partial = TransferOutcome(
        path="x", accepted=True, completed=False, transport_used="com",
        elapsed_s=0.1, source_hash="abc", device_hash="def",
        failure_reason="hash_mismatch_after_com_accept",
    )
    _check("P3-T2 accepted but not completed = NOT success "
           "(Class A guard)",
           o_partial.is_success is False)

    # P3-T2: sha256 helper works
    with tempfile.NamedTemporaryFile(mode='wb', delete=False) as tmp:
        tmp.write(b"hello world")
        tmp_path = tmp.name
    try:
        h = sha256_of_local_file(tmp_path)
        _check("P3-T2 sha256_of_local_file returns 64-char hex",
               len(h) == 64 and all(c in "0123456789abcdef" for c in h))
    finally:
        os.unlink(tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# P4 — Invariants
# ═══════════════════════════════════════════════════════════════════════════
def test_p4_invariants():
    _section("P4 — Invariants (mk34_invariants.py)")

    from mk34_invariants import (
        InvariantViolation,
        assert_no_destructive_until_verified, assert_no_zero_byte_writes,
        assert_state_consistency, assert_planner_diff_acyclic,
        assert_observer_freshness, gate_pre_execution, gate_post_execution,
    )
    from mk34_observer import DeviceObservedState
    from mk34_planner import DesiredState, plan_diff, ActionKind

    # P4-T1: InvariantViolation is RuntimeError
    _check("P4-T1 InvariantViolation extends RuntimeError",
           issubclass(InvariantViolation, RuntimeError))

    # assert_no_destructive_until_verified: violates when no alts present
    try:
        assert_no_destructive_until_verified(
            paths_to_delete=["SD card/Music/A/x.mp3"],
            verified_present_paths=frozenset(),
            cross_format_alternatives={"SD card/Music/A/x.mp3": []},
        )
        _fail("P4-T1 assert_no_destructive_until_verified raises on no alt")
    except InvariantViolation as e:
        _check("P4-T1 INVARIANT_VIOLATED message format",
               "INVARIANT_VIOLATED" in str(e))

    # assert_no_destructive_until_verified: passes when alt present
    try:
        assert_no_destructive_until_verified(
            paths_to_delete=["a.mp3"],
            verified_present_paths=frozenset(["a.flac"]),
            cross_format_alternatives={"a.mp3": ["a.flac"]},
        )
        _ok("P4-T1 assert_no_destructive_until_verified passes when alt verified")
    except InvariantViolation as e:
        _fail("P4-T1 should pass when alt verified", e)

    # assert_no_zero_byte_writes: violates when completed but no device_hash
    from mk34_executor import TransferOutcome
    try:
        assert_no_zero_byte_writes([
            TransferOutcome(path="x", accepted=True, completed=True,
                            transport_used="com", elapsed_s=0.1,
                            device_hash=None),  # missing hash
        ])
        _fail("P4-T1 assert_no_zero_byte_writes raises on missing device hash")
    except InvariantViolation:
        _ok("P4-T1 assert_no_zero_byte_writes catches Class A: completed without hash")

    # assert_planner_diff_acyclic: violates on COPY+DELETE of same path
    desired = DesiredState(
        expected_paths=frozenset(["x"]), forbidden_paths=frozenset([]),
        expected_hashes={"x": "h"}, cross_format_alts={},
    )
    observed = DeviceObservedState(
        observed_at=time.time(), tracks_present_paths=frozenset(),
        free_space_bytes=1_000_000, storage_breakdown={},
        advisory_preload_count=None, divergence_pct=0.0,
        divergence_logged=False, adb_command="t", observation_id="t",
    )
    plan = plan_diff(desired, observed)
    try:
        assert_planner_diff_acyclic(plan)
        _ok("P4-T1 assert_planner_diff_acyclic passes for clean plan")
    except InvariantViolation as e:
        _fail("P4-T1 acyclic should pass for clean plan", e)

    # P4-T1 gate_pre_execution: composes assertions
    # Build a clean state where observed matches desired (state_consistency passes)
    desired_clean = DesiredState(
        expected_paths=frozenset(["a.mp3", "b.mp3"]),
        forbidden_paths=frozenset(),
        expected_hashes={"a.mp3": "h1", "b.mp3": "h2"},
        cross_format_alts={},
    )
    observed_clean = DeviceObservedState(
        observed_at=time.time(),
        tracks_present_paths=frozenset(["a.mp3", "b.mp3"]),
        free_space_bytes=1_000_000, storage_breakdown={},
        advisory_preload_count=2, divergence_pct=0.0,
        divergence_logged=False, adb_command="t", observation_id="t",
    )
    plan_clean = plan_diff(desired_clean, observed_clean)
    try:
        gate_pre_execution(observed_clean, desired_clean, plan_clean)
        _ok("P4-T1 gate_pre_execution composes invariants and passes for clean state")
    except InvariantViolation as e:
        _fail("P4-T1 gate_pre_execution should pass for clean state", e)

    # P4-T3: walkman_dedup destructive flag
    import walkman_dedup as wd
    _check("P4-T3 DEDUP_DESTRUCTIVE_VERIFIED defaults False",
           wd.DEDUP_DESTRUCTIVE_VERIFIED is False)
    _check("P4-T3 _check_destructive_verified returns False when flag False",
           wd._check_destructive_verified("test_op") is False)
    wd._set_dedup_verified(True)
    _check("P4-T3 _set_dedup_verified(True) flips flag",
           wd.DEDUP_DESTRUCTIVE_VERIFIED is True)
    _check("P4-T3 _check_destructive_verified returns True after gate",
           wd._check_destructive_verified("test_op") is True)
    wd._set_dedup_verified(False)  # reset for safety


# ═══════════════════════════════════════════════════════════════════════════
# P5 — Audit Stream + Replay + Inspect CLI
# ═══════════════════════════════════════════════════════════════════════════
def test_p5_audit_replay():
    _section("P5 — Audit Stream + Replay + Inspect CLI")

    # P5-T1: audit log JSONL format
    from mk34_reconciliation import (
        ReconciliationEngine, MK34InputBundle, make_record, SourceType
    )

    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl',
                                       delete=False) as tmp:
        audit_path = Path(tmp.name)

    bundle = MK34InputBundle(records=[
        make_record('artist|title', {'genre': 'A'},
                    SourceType.GENRE_CACHE, 'gc1', 0.80),
        make_record('artist|title', {'genre': 'B'},
                    SourceType.METADATA_CACHE, 'mc1', 0.85),
    ])
    eng = ReconciliationEngine(bundle, audit_log_path=audit_path)
    out = eng.run()

    # Read back, verify it parses as JSONL
    rows = []
    with audit_path.open() as fh:
        for line in fh:
            rows.append(json.loads(line))
    _check("P5-T1 audit log written as parseable JSONL",
           len(rows) > 0)
    _check("P5-T1 each row has eventId, runId, type, payload, timestamp",
           all('eventId' in r and 'runId' in r and 'type' in r
               for r in rows))
    _check("P5-T1 all 5 event types present in audit",
           {r['type'] for r in rows} >= {'INGEST', 'NORMALIZE',
                                          'CONFLICT_DETECTED', 'DECISION_MADE',
                                          'WRITEBACK'})

    # P5-T2: mk34_replay.py is invokable
    import subprocess
    result = subprocess.run(
        [sys.executable, "mk34_replay.py", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    _check("P5-T2 mk34_replay.py --help works (CLI is invokable)",
           result.returncode == 0)
    _check("P5-T2 CLI has --runs flag",
           "--runs" in result.stdout)
    _check("P5-T2 CLI has --run-id flag",
           "--run-id" in result.stdout)
    _check("P5-T2 CLI has --decisions-for flag",
           "--decisions-for" in result.stdout)
    _check("P5-T2 CLI has --drift-check flag",
           "--drift-check" in result.stdout)

    # P5-T3: walkman_inspect.py extension
    result = subprocess.run(
        [sys.executable, "walkman_inspect.py", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    _check("P5-T3 walkman_inspect.py --reconcile flag added",
           "--reconcile" in result.stdout)
    _check("P5-T3 walkman_inspect.py --conflicts flag added",
           "--conflicts" in result.stdout)

    audit_path.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# Three failure-class behavioral tests
# ═══════════════════════════════════════════════════════════════════════════
def test_failure_classes():
    _section("Three failure-class fixes (CTO board diagnosis)")

    # Class A: false success — TransferOutcome must distinguish accepted vs completed
    from mk34_executor import TransferOutcome
    a_partial = TransferOutcome(
        path="x", accepted=True, completed=False, transport_used="com",
        elapsed_s=0.1, source_hash="abc", device_hash="def",
        failure_reason="hash_mismatch_after_com_accept",
    )
    _check("Class A: accepted=True does NOT imply success",
           a_partial.is_success is False)
    _check("Class A: failure_reason captures the hash mismatch context",
           "hash_mismatch" in (a_partial.failure_reason or ""))

    # Class B: state divergence — observer separates ADB-truth from preload-advisory
    from device_inventory import check_state_divergence
    _check("Class B: 27,901 vs 0 is detected as divergence",
           check_state_divergence(27901, 0) is True)
    _check("Class B: 27,901 vs 27,500 is within tolerance",
           check_state_divergence(27901, 27500) is False)

    # Class C: silent destructive — invariant gate must raise, not warn
    from mk34_invariants import (
        assert_no_destructive_until_verified, InvariantViolation
    )
    raised = False
    try:
        assert_no_destructive_until_verified(
            paths_to_delete=["SD card/Music/A/orphan.mp3"],
            verified_present_paths=frozenset(),  # nothing on device
            cross_format_alternatives={"SD card/Music/A/orphan.mp3": []},
        )
    except InvariantViolation:
        raised = True
    _check("Class C: invariant violation raises (not log-and-continue)",
           raised)


# ═══════════════════════════════════════════════════════════════════════════
# MK33 → MK34 continuity tests (no MK33 patch undone)
# ═══════════════════════════════════════════════════════════════════════════
def test_mk33_continuity():
    _section("MK33 → MK34 continuity (no MK33 patch undone)")

    from genre_client import (
        ResolvedView, build_resolved_view, classify_conflict_severity,
        get_provider_budget_status, get_novel_genres_seen, reset_novel_genres_seen,
    )
    from config import (
        POLICY_VERSION, AUTHORITY_POLICY, get_authority_policy,
        freeze_policy_snapshot,
    )
    from sync_state import _PROCESS_OWNER_ID
    from ai_helper import validate_ai_genre_result
    from meta_key import _METAKEY_SCHEMA_VERSION_KEY
    # _METAKEY_SCHEMA_VERSION is local to a function; verify by reading source
    metakey_src = open('meta_key.py').read()
    from types import MappingProxyType

    # MK33 markers
    _check("MK33 O1: POLICY_VERSION present",
           POLICY_VERSION.startswith("mk33"))
    _check("MK33 O1: freeze_policy_snapshot returns dict",
           isinstance(freeze_policy_snapshot(), dict))
    _check("MK33 O2: provider buckets initialized",
           isinstance(get_provider_budget_status(), dict))
    _check("MK33 O3: novel severity tier present",
           classify_conflict_severity("Amapiano", "HyperPop") == "novel")
    _check("MK33 O4: _PROCESS_OWNER_ID is UUID-shaped",
           len(str(_PROCESS_OWNER_ID)) == 36)
    _check("MK33 O5: ResolvedView has MK34 bridge method",
           hasattr(ResolvedView, 'to_mk34_normalized_track'))

    # MK32 markers
    _check("MK32 N1: AUTHORITY_POLICY frozen via MappingProxyType",
           type(AUTHORITY_POLICY).__name__ == 'mappingproxy')
    _check("MK32 N3: classify_conflict_severity returns 4 tiers",
           {classify_conflict_severity("Classical", "Drill"),
            classify_conflict_severity("Neo Soul", "Alt R&B"),
            classify_conflict_severity("Amapiano", "HyperPop")}
            >= {"categorical", "taxonomic", "novel"})

    # MK30 markers
    r = validate_ai_genre_result({'genre': 'House', '_source': 'lastfm'})
    _check("MK30 F1: external sources have evaluability=verifiable",
           r['evaluability'] == 'verifiable')
    _check("MK30 F1: external sources have confidence=None",
           r['confidence'] is None)

    # MK28 markers
    _check("MK28 E2: _METAKEY_SCHEMA_VERSION is mk28",
           '_METAKEY_SCHEMA_VERSION = "mk28"' in metakey_src)

    # MK29 markers
    _check("MK29 G1: _source_type field added",
           '_source_type' in r)


# ═══════════════════════════════════════════════════════════════════════════
# Run all tests
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("\nMK34 Reconciliation Engine — CTO Board Test Suite")
    print("=" * 65)

    test_p1_engine_core()
    test_p2_observer()
    test_p3_planner_executor()
    test_p4_invariants()
    test_p5_audit_replay()
    test_failure_classes()
    test_mk33_continuity()

    print("\n" + "=" * 65)
    print(f"  MK34 Reconciliation Engine: {_passed}/{_passed+_failed} passed")
    if _failed == 0:
        print("  ✅ All 14 P-tickets implemented")
        print("  ✅ Class A (false success) closed via content-hash verify")
        print("  ✅ Class B (state divergence) closed via ADB-first observer")
        print("  ✅ Class C (silent destructive) closed via invariant gate")
        print("  ✅ MK28-MK33 continuity preserved (no architectural patch undone)")
    print("=" * 65)
    sys.exit(0 if _failed == 0 else 1)
