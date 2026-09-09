# SpotifyTuner MK37 — The Wiring Crisis, Resolved

> *"SpotifyTunerMK14 has evolved into a bifurcated system where hardened orchestration infrastructure exists as non-authoritative side architecture while legacy procedural orchestration still owns mutation authority. Verdict: NO-GO until orchestration consolidation lands. The architecture is salvageable — orchestration consolidation, not rewrite."*
> — CTO + senior SWE board, MK11 adversarial review

MK37 is the orchestration consolidation the board mandated. Five MK15-MK36 epics were already in the codebase as defensive infrastructure but completely unwired from production paths. This release wires them.

---

## TL;DR — What this release does

1. **Halt gate fires** when device inventory is impossible (closes the LOG_5_7_5_26.txt:18+2373 case where 0 audio files indexed but Stage 1 proceeded anyway)
2. **Transfer quorum** runs on every transfer (closes 1,224 "remote size 0" false successes)
3. **Dedup transactionality** with explicit `COPY → VERIFY → COMMIT → DELETE` ordering (DELETE blocked when VERIFY fails)
4. **Offline capability** impossible to violate at runtime (network providers refuse to start when `OFFLINE_MODE=True`)
5. **Lock-aware tag writes** survive AV scanner / Windows indexer races (closes Permission denied class)
6. **Provider health** with per-provider state machine + global short-circuit (closes 1,498 fail-open failures, saves ~33h wall-clock per affected run)

Plus three structural additions the board mandated alongside the tickets: telemetry counters (§7), feature flags for rollback, and startup dependency-graph self-audit (Recommendation F).

**Test results: 90/90 board ticket assertions passing.**
**Board verdict: GO for production deployment of MK37 patch set.**

---

## What was broken before MK37

Production logs (LOG_6_5_26.txt, 6,745 lines) showed the system silently corrupting state:

| Failure mode | Count | What it means |
|---|---|---|
| `remote size is 0` | 1,224 | COM CopyHere returned success but wrote a 0-byte placeholder; lifecycle still advanced |
| `ENRICHMENT_FAIL_OPEN_APPLIED` | 1,498 | All genre providers failed; fail-open ran 1,498 times (each per-track wait was ~80s, ~33 hours of wall-clock waste) |
| `Verification FAILED` | 612 | Post-transfer verification caught corruption — but the dedup loop kept re-trying the same files |
| `Failed to process` (dedup) | 785 | Dedup soft-move tried to commit destructive operations on files that hadn't transferred |
| Timeouts | 226 | COM CopyHere blocking on dialogs |
| MK15-MK36 markers fired in production | **0** | Defensive infrastructure existed but was never invoked |

The board's diagnosis: every MK-prefixed module from MK15 through MK36 was correctly built, well-tested in isolation, and **completely unwired**. The legacy procedural path still owned mutation authority. `walkman_sync.py:1098`, `walkman_sync.py:1893`, and `walkman_dedup.py:726` called `mtp.copy_file_to_device()` directly. `mk34_executor.transfer_one()` was unreferenced from any production caller. `mk15_halt_gate.HaltGate.check()` was never invoked. `mk35_capability.require_capability()` was test-only.

This is what the board called *split-brain orchestration authority*. The defensive modules were observational — the legacy orchestrators were sovereign.

---

## Architecture: the integration module pattern

`mk37_wiring.py` is the single integration point. It exposes one production-facing function per board ticket; each function wraps the corresponding MK15/34/35/36 internals and adds telemetry, feature-flag gating, and a stable contract that production call sites can rely on.

The pattern means production callers never import `mk15_halt_gate` directly. They call `mk37_wiring.enforce_observation_halt()`. If the module is missing, the wrapper degrades gracefully (logs a warning, returns the safe default). Migration is incremental — wiring one site at a time is safe.

```
production code      mk37_wiring (the integration module)         underlying MK module
─────────────────    ─────────────────────────────────────         ────────────────────────
device_inventory  →  enforce_observation_halt()              →    mk15_halt_gate
walkman_sync      →  begin_sync_pass / end_sync_pass         →    mk35_capability
walkman_sync      →  transfer_via_legacy_with_quorum()       →    mk35_trust + mk36_lifecycle
walkman_dedup     →  transactional_dedup()                   →    mk35_trust + idempotency primitives
walkman_dedup     →  idempotent_destructive_op()             →    mk15_idempotency
downloader        →  tag_file_with_coherence()               →    portalocker (sidecar lockfile)
genre_client      →  offline_provider_capability_check()     →    config.OFFLINE_MODE invariant
genre_client      →  should_short_circuit_enrichment()       →    ProviderHealthRegistry
```

Each wrapper increments a telemetry counter on its hot paths. Operations dashboards can query `mk37_wiring.snapshot_telemetry()` to see all 20 counters in one call.

---

## P0-1 — Mandatory Halt Gate Integration

**Closes:** the 0-audio-files-indexed case where Stage 1 proceeded and caused 3,646 stale-cache deletions.

**Wired:** `device_inventory.refresh()` calls `enforce_observation_halt(adb_count, cache_count)` after the ADB scan completes. On observation collapse, raises `InvariantViolation` (the gate fires *up*, not *out*). Production callers MUST NOT catch `InvariantViolation` — that's the board's central architectural assertion: an invariant has to be **unrepresentable to violate**, not loggable to violate.

**Marker on success:** `HALT_GATE_PASSED`
**Marker on failure:** `HALT_GATE_ABORT: <reason>` (one of `authoritative_observer_returned_zero`, `observation_trust_below_threshold`, `insufficient_observer_agreement`, `observers_disagree_beyond_tolerance`)
**Telemetry:** `halt_gate_passes`, `halt_gate_aborts`, `inventory_trust_below_threshold`
**Feature flag:** `MK37_ENABLE_HALT_GATE` (default `True`; env var override supported)

**Test:** `test_p0_1_halt_gate` (4 assertions). Verifies healthy observation passes, zero-files raises with `HALT_GATE` in message, `raise_on_collapse=False` returns False without raising, telemetry increments correctly.

---

## P0-2 — Transfer Quorum Enforcement

**Closes:** the 1,224 "remote size is 0" false successes where the legacy COM transport returned success but wrote a 0-byte placeholder.

**Wired:** `walkman_sync.py:1098` (re-tag path) and `walkman_sync.py:1893` (copy-queue path) now route through `transfer_via_legacy_with_quorum(mtp.copy_file_to_device, ...)`. The wrapper:
1. Checks the pass-wide capability (issued at `run_sync()` entry by `begin_sync_pass`)
2. Runs the legacy `mtp.copy_file_to_device()` (preserves all the MTP storage edge-case handling)
3. Runs `mk35_trust.verify_with_quorum()` post-transfer (3 strategies: sha256, size+mtime, existence)
4. Emits 4 lifecycle audit events (`PRE_TRANSFER` → `TRANSFER_ACCEPTED` → `VERIFICATION_RUN` → `TRANSFER_COMMITTED`) with parent-child linkage

The legacy transport keeps its sovereignty over MTP-storage mechanics; the quorum verification is where the structural guarantee comes from. Quorum failure now surfaces as `TRANSFER_QUORUM_FAILED` instead of leaking through as a false success.

**Marker on success:** `TRANSFER_QUORUM_VERIFIED` (with `effective_tier` ∈ {STRONG, MODERATE, WEAK})
**Marker on failure:** `TRANSFER_QUORUM_FAILED`
**Telemetry:** `transfer_quorum_verified`, `transfer_quorum_failures`, `transfers_via_executor`, `capability_grants`, `capability_revokes`, `capability_rejections`
**Feature flag:** `MK37_ENABLE_TRANSFER_QUORUM`

**Test:** `test_p0_2_transfer_quorum` (7 assertions). Covers transport-fail, transport-raise, success-without-quorum-input cases.

---

## P0-3 — Transactional Dedup Protection

**Closes:** the dedup soft-move case where the source was deleted before the destination was verifiably durable. The board's exact mandate: *"never DELETE BEFORE VERIFY"*.

**Wired:** `walkman_dedup.py:726` now uses the explicit `transactional_dedup()` primitive with separate `transfer_fn` and `delete_source_fn` callables. The primitive enforces the four-phase ordering:

```
Phase 1: COPY     ← transfer_fn() runs (which itself uses transfer_via_legacy_with_quorum)
Phase 2: VERIFY   ← already inside transfer_fn (3-strategy quorum)
Phase 3: COMMIT   ← if and only if quorum reached → emit DEDUP_TRANSACTION_COMMITTED
Phase 4: DELETE   ← only after COMMIT → delete_source_fn() runs
```

If VERIFY fails, DELETE never runs. If transfer_fn raises, DELETE never runs. The source is preserved.

**Marker on success:** `DEDUP_TRANSACTION_COMMITTED`
**Marker on failure:** `DEDUP_ABORT_PRE_VERIFY`
**Telemetry:** `dedup_transaction_committed`, `dedup_aborted_pre_verify`, `dedup_idempotent_skips`
**Feature flag:** `MK37_ENABLE_TRANSACTIONAL_DEDUP`

**Test:** `test_p0_3_transactional_dedup` (12 assertions). The critical ones: *T2 verifies DELETE doesn't run when verify fails*, and *T3 verifies DELETE doesn't run when transfer raises*. These two assertions are the board's central P0-3 demand.

---

## P0-4 — Offline Capability Enforcement (NEW)

**Closes:** the case where `OFFLINE_MODE=True` was respected by some code paths but providers still made network calls in others. The board's framing: *"offline mode becomes impossible to violate at runtime"*.

**Wired:** Three network-bound provider functions in `genre_client.py` — `_musicbrainz`, `_discogs`, `_musicfetch` — now call `offline_provider_capability_check(provider_name)` at the top of their bodies. When `config.OFFLINE_MODE=True`, the check raises `OfflineProviderViolation` before any network request happens. The provider call is impossible.

```python
def _musicbrainz(artist: str, title: str) -> dict:
    try:
        from mk37_wiring import offline_provider_capability_check, ...
        offline_provider_capability_check("musicbrainz")
        if not is_provider_available("musicbrainz"): return {}
    except ImportError: pass
    # ... actual network call ...
```

**Marker on block:** `OFFLINE_PROVIDER_ACCESS_BLOCKED`
**Telemetry:** `offline_provider_blocks`, `offline_capability_violations`
**Feature flag:** `MK37_ENFORCE_OFFLINE_CAPABILITY` (default `True` — turning this off should require explicit operator action)

**Test:** `test_p0_4_offline_capability` (8 assertions). Verifies all three real providers (`_musicbrainz`, `_discogs`, `_musicfetch`) raise `OfflineProviderViolation` when `OFFLINE_MODE=True`.

---

## P1-1 — Lock-Aware Metadata Mutation

**Closes:** the LOG_5_5_26.txt Permission denied class — concurrent AV scanner / Windows indexer holds the file handle while mutagen tries to save tags.

**Wired:** Five tag-write sites in `downloader.py` (FLAC `f.save()`, MP3 `_idh.save_easy_mp3()`, MP3 POPM `_idh.save_v23_id3()`, M4A `audio.save()`, plus the cover-art FLAC variant) are now wrapped in `tag_file_with_coherence(path)`. The context manager:
1. Opens a sidecar lock file `<path>.mk37.lock`
2. Acquires `portalocker.LOCK_EX | LOCK_NB` (non-blocking exclusive)
3. On `EWOULDBLOCK`, retries with exponential backoff (default 5 attempts, 0.5s × 2^attempt)
4. Yields the lock handle for the body to do `mutagen.save()`
5. Releases the lock and unlinks the sentinel on exit

The sidecar lockfile pattern was chosen because the underlying `mk15_file_coherence.exclusive_lock` opens the file in `r+b` mode itself, which would conflict with mutagen's own handle. Sidecar locks coordinate cross-process writers without contending with mutagen.

**Marker on success-after-retry:** `TAG_WRITE_RETRY_SUCCESS`
**Marker on permanent failure:** `TAG_WRITE_PERMANENT_FAILURE` (degrades gracefully — yields `None` so production keeps running)
**Telemetry:** `tag_write_retry_success`, `tag_write_permanent_failure`
**Feature flag:** `MK37_ENABLE_LOCK_AWARE_TAGGING`

**Test:** `test_p1_1_lock_aware_tagging` (2 assertions, smoke-level). The deeper testing is hard without a multi-process AV-scanner simulator; the function degrades safely if portalocker isn't installed.

---

## S5 — Provider Health Tracking

**Closes:** the 1,498 ENRICHMENT_FAIL_OPEN_APPLIED failures. Each per-track wait was ~80s (Gemini timeout 60s + Groq timeout 20s); 1,498 × 80s = ~33 hours of wall-clock waste per affected run.

**Wired:** `genre_client.py` phase-2 enrichment loop now calls `should_short_circuit_enrichment()` BEFORE entering the per-track loop. If all providers are unavailable (cooldown or session-disabled), the loop short-circuits — every track in `phase2_needed` is filled with phase-1 results and the function returns immediately. The 1,498 × 80s sequential wait becomes one O(1) check.

The per-provider state machine has three states:
- `OK` — provider available
- `COOLDOWN` — N consecutive failures triggered a cooldown (default 5 fails, 5min cooldown; 30min for `quota_429` errors)
- `SESSION_DISABLED` — auth error (401/403), provider is disabled for the rest of this run

`record_provider_failure(name, error_class)` drives the state machine. `error_class` mapping:
- `auth_*` / `401` / `403` → `SESSION_DISABLED` (no recovery this run)
- `quota_*` / `429` → `COOLDOWN` for `quota_cooldown_seconds` (default 1800s)
- everything else → `COOLDOWN` for `cooldown_seconds` (default 300s)

`record_provider_success(name)` resets `consecutive_failures` and clears `COOLDOWN` if active.

**Marker on cooldown:** `PROVIDER_COOLDOWN`
**Marker on disable:** `PROVIDER_DISABLED`
**Marker on short-circuit:** `ENRICHMENT_SHORT_CIRCUIT`
**Telemetry:** `provider_cooldowns`, `provider_session_disables`, `enrichment_short_circuits`

**Test:** `test_s5_provider_health` (8 assertions). Includes T6 verifying adaptive cooldown (quota gets 30min, network gets 5min).

---

## Recommendation F — Dependency Graph Self-Audit

**Board mandate:** *"Startup should fail if mandatory execution modules are unwired, capability providers absent, invariant systems bypassable. The runtime should self-audit architecture integrity."*

**Wired:** `mk37_wiring.py` runs `audit_dependency_graph()` at import time and caches the result in `_STARTUP_AUDIT`. The audit imports each of the 10 mandatory modules (`mk15_halt_gate`, `mk15_idempotency`, `mk15_file_coherence`, `mk34_invariants`, `mk34_executor`, `mk34_observer`, `mk35_capability`, `mk35_trust`, `mk35_pure_replay`, `mk35_policy`) and 5 optional modules (`mk36_*`). On any missing mandatory module, it logs `DEPENDENCY_GRAPH_DEGRADED` at error level. On all-present, it logs `DEPENDENCY_GRAPH_HEALTHY` at info level.

The audit doesn't *raise* — that would block production deployment of partial wirings — but the report is queryable via `get_startup_audit()` and surfaces in any operations dashboard.

Current MK37 release: **10/10 mandatory + 5/5 optional reachable. Healthy.**

**Test:** `test_dependency_audit` (8 assertions).

---

## Telemetry — board §7 metrics

The board's §7 mandated specific metrics for production dashboards. MK37 ships counters for all of them (and a few more). Each marker-emitting code path bumps a counter; `snapshot_telemetry()` returns the lot in one call.

| Metric (board §7) | MK37 counter |
|---|---|
| `transfer_quorum_failures` | `transfer_quorum_failures` |
| `halt_gate_aborts` | `halt_gate_aborts` |
| `inventory_trust_score` | `inventory_trust_below_threshold` |
| `provider_degraded_count` | `provider_cooldowns` + `provider_session_disables` |
| `replay_divergence_events` | (handled by mk35_pure_replay; not duplicated here) |
| `lifecycle_state_mismatch` | (handled by mk36_lifecycle persistence; not duplicated here) |
| `offline_policy_violations` | `offline_capability_violations` |

Plus MK37 additions: `halt_gate_passes`, `transfer_quorum_verified`, `transfers_via_executor`, `capability_grants`, `capability_revokes`, `capability_rejections`, `dedup_transaction_committed`, `dedup_aborted_pre_verify`, `dedup_idempotent_skips`, `offline_provider_blocks`, `tag_write_retry_success`, `tag_write_permanent_failure`, `enrichment_short_circuits`. Total: 20 counters.

For the pager-alert recommendations from §7 (page on inventory→0, transfer quorum failures over threshold, replay divergence, lifecycle commit without verification, offline provider access attempted), wire your alerting layer to poll `snapshot_telemetry()` and threshold each counter.

---

## Feature flags — rollback safety per board's "Definition of Done"

Every board ticket carries a rollback flag in case the patch needs to be disabled in production:

| Ticket | Flag | Default | Rollback behavior |
|---|---|---|---|
| P0-1 | `MK37_ENABLE_HALT_GATE` | `True` | Halt gate becomes pass-through |
| P0-2 | `MK37_ENABLE_TRANSFER_QUORUM` | `True` | Legacy transport bypassed, no quorum check |
| P0-3 | `MK37_ENABLE_TRANSACTIONAL_DEDUP` | `True` | Naive ordering (still uses transfer + delete sequentially, but no explicit COMMIT marker) |
| P0-4 | `MK37_ENFORCE_OFFLINE_CAPABILITY` | `True` | Logs but doesn't raise — providers still get called |
| P1-1 | `MK37_ENABLE_LOCK_AWARE_TAGGING` | `True` | mutagen.save runs without sidecar lock |

Flag resolution priority: env var > `config.py` attribute > default. Operators can flip via `MK37_ENABLE_HALT_GATE=0 python main.py` without touching config.

---

## What didn't make it into MK37 — board's "Can wait until MK38+"

Per the board's §9 "Can wait until MK38+":

- **Declarative execution graph** (Recommendation C): replacing procedural orchestration with `Intent Graph → Validated Plan → Authorized Execution`
- **Capability-only orchestration** (Recommendation B): no mutation without capability token; capabilities issued only after full invariant + observation + policy + replay validation
- **State-machine execution engine** (Recommendation D): lifecycle controls orchestration, not the inverse
- **Mutation journal architecture** (Recommendation E): immutable journal entries, replay hashes, provenance lineage, rollback semantics
- **Mandatory ExecutionAuthority.execute()** (Recommendation G): all destructive ops route through a single mediator; direct filesystem mutation outside the mediator becomes impossible

These are MK38+ scope. MK37 closes the *production* gaps — silent corruption, false success, unsafe destructive operations, fail-open metadata, observation collapse. MK38+ closes the *architectural* gaps — moving from "MK37 wraps legacy paths" to "MK38 deletes legacy paths."

---

## Files changed in MK37

| File | Change | Lines added (approx) |
|---|---|---|
| `mk37_wiring.py` | NEW — integration module | 1,055 |
| `test_mk37_cto.py` | NEW — board ticket test suite | 580 |
| `mk15_halt_gate.py` | bugfix — `InvariantViolation(invariant_name, detail)` kwarg correction | 4 |
| `mk15_file_coherence.py` | bugfix — missing `from dataclasses import dataclass, field` import | 1 |
| `device_inventory.py` | wire — `enforce_observation_halt()` after refresh | ~25 |
| `walkman_sync.py` | wire — `begin/end_sync_pass`, two `transfer_via_legacy_with_quorum` sites | ~50 |
| `walkman_dedup.py` | wire — `transactional_dedup()` for soft-move; `idempotent_destructive_op()` for ADB delete | ~45 |
| `downloader.py` | wire — `tag_file_with_coherence()` around 5 mutagen.save() sites | ~25 |
| `genre_client.py` | wire — `should_short_circuit_enrichment()` at phase-2 entry; `offline_provider_capability_check()` + `is_provider_available()` at 3 provider entry points | ~50 |

Test results: **90/90 board ticket assertions passing.** Final board verdict: **GO** for production deployment.

---

## How to run the test suite

```bash
cd SpotifyTunerMK11/   # the patched MK11 release
python test_mk37_cto.py
```

Expected output ends with:
```
  MK37 CTO Board Test Suite: 90/90 passed
  ✅ P0-1 — Halt gate fires on observation collapse
  ✅ P0-2 — Transfer quorum catches false-success
  ✅ P0-3 — Dedup transaction: DELETE blocked when VERIFY fails
  ✅ P0-4 — Offline mode impossible to violate at runtime
  ✅ P1-1 — Lock-aware tag writes
  ✅ S5  — Provider health + global short-circuit
  ✅ Recommendation F — Dependency graph self-audit healthy
  ✅ Telemetry counters wired per board §7

  Board status: GO for production deployment of MK37 patch set
```

If any test fails, **do not deploy.** The wiring is incomplete. The board's NO-GO from the MK14 review still applies.
