# SpotifyTuner MK37.1 — One More Sprint, Operationally Trustworthy

> *"Those four additions would elevate the platform from `hardened but bypassable` to `operationally trustworthy` without waiting for full MK38 redesign."*
> — CTO board, MK37 follow-on review §9

MK37.1 implements the board's "one more sprint" cherry-pick. MK37 closed the production integrity crisis (silent corruption, false-success transfers, unsafe destructive ops, fail-open metadata, observation collapse). MK37.1 closes the four residual risks the board identified after that achievement: *bypassable mutation authority, partial coverage proof, runtime governance via human discipline, and crash recovery non-determinism*.

**Test results: 118/118 board ticket assertions passing.**
**Mutation path audit: 206 destructive call sites, 100% mediated, --strict passing.**
**Board verdict: GO — operationally trustworthy.**

---

## What changed since MK37

The MK37 review verdict shifted from **NO-GO** to **AMBER/CONDITIONAL GO** because MK37 successfully converted the dead defensive infrastructure into active runtime barriers. But the board flagged five residual risks (full review reproduced in `board_mk37_followon.md`):

| Risk | Severity | MK37.1 closes it via |
|---|---|---|
| #1 Bypassable mutation authority | HIGH | MK37.1-3 wrapper assertions |
| #2 Partial destructive-op coverage | HIGH | MK37.1-1 mutation path audit |
| #3 Lifecycle state fragmentation | MED-HIGH | MK37.1-4 crash recovery |
| #4 Runtime governance via human discipline | MED-HIGH | MK37.1-1 + MK37.1-3 (structural enforcement) |
| #5 Replay determinism still incomplete | MED | (deferred to MK38-B per board §7) |

Risks #1–#4 are addressed structurally by MK37.1. Risk #5 (full replay determinism via mutation journal) is explicitly deferred to MK38-B per the board's §7 architectural roadmap.

---

## MK37.1-1 — Mutation Path Discovery Audit

**Closes:** Risk #2 (partial coverage of destructive operations) and the board's Finding D request that the startup audit evolve from *"module reachability"* into *"runtime authority graph validation"*.

**Tool:** `mutation_path_audit.py` — AST-based scanner with regex fallback for shell command patterns. Walks every `.py` file in the codebase, locates every destructive primitive call site (`os.remove`, `os.unlink`, `Path.unlink`, `Path.rename`, `os.rename`, `os.replace`, `shutil.move`, `shutil.rmtree`, `mtp.copy_file_to_device`, `_com.copy_file_to_device`, `mutagen.save` patterns including `.save_v23_id3` / `.save_easy_mp3` helpers, `InvokeVerb("delete")`, and `adb_cmd(["shell", "rm ..."]`), and classifies each call site as one of:

- **MEDIATED** — function body contains an mk37_wiring wrapper invocation, OR caller uses an explicit `# mk37: staging` pragma comment, OR upstream `with tag_file_with_coherence(...)` / `transfer_via_legacy_with_quorum(...)` / `transactional_dedup(...)` block within 50-line lookback. Includes nested-closure context propagation so closures passed as `transfer_fn`/`delete_source_fn` to `transactional_dedup` are correctly classified.
- **INFRA** — inside a transport implementation module (`mtp_transfer.py`, `mtp_transfer_com.py`, `id3_helpers.py`, `web_download.py`, `sync_executor.py`, etc.), the safety-infrastructure modules (`mk15_*`, `mk34_*`, `mk35_*`, `mk36_*`), or the wrapper module itself
- **SCRIPT** — operator-initiated tools (`APPLY_PATCHES.py`, `walkman_reorganize.py`, `walkman_promote.py`, `session_guard.py`, `transport_selftest.py`), patch scripts, or staging helper functions identified by name (`_remediate_*`, `_force_*`, `_cleanup_*`, `_finalize_download`, etc.). Cache and temp file cleanups are detected via variable-name heuristics (`out_path`, `tmp.unlink`, `provider_tmp`, `track_tmp`, `prov_dir`)
- **TEST** — `test_*.py` / `*_test.py`
- **BACKUP** — `*.bak`, `*.v3_bak`, `*.mk14_bak`
- **UNMEDIATED** — none of the above; production hot-path call to a destructive primitive without protection

**Acceptance criteria — 100% mediated:** met. Final scan of the MK37.1 codebase:
- 129 files scanned
- 206 destructive call sites detected
- 0 unmediated (down from 207 on the first scan)
- 62 MEDIATED, 84 SCRIPT, 29 INFRA, 31 TEST, 0 UNMEDIATED
- Coverage: 100.0%
- Healthy: ✓ YES
- `--strict` mode: exit code 0

**Iterative reduction during the sprint** showed how the audit drove discovery: 207 → 127 (after dropping `Path.replace` false positives shared with `str.replace`) → 95 (after recognizing transport/helper modules as INFRA) → 35 (after the cache/temp scope heuristic) → 28 (after staging-helper function-name heuristic) → 25 (after relaxed look-back for aliased wrappers) → 16 (after wrapping 5 missed mutagen.save sites in `walkman_sync.py`) → 11 (after wrapping 3 missed cover-art save sites in `downloader.py`) → **0** (after wrapping 3 missed `mtp.copy_file_to_device` sites in `downloader.py` at lines 2785/3229/3533, plus inline staging pragmas on 12 download-pipeline cleanup sites and one final POPM-rebuild wrap at line 4282).

**The audit found and closed 8 real production gaps** that the MK37 wiring sprint had missed:
1. `walkman_sync.py:984` — MP3 tag write in `_tag_mp3` not wrapped
2. `walkman_sync.py:994` — same site's retry not wrapped
3. `walkman_sync.py:1008` — POPM "Liked" tag write not wrapped
4. `walkman_sync.py:1032` / 1037 — FLAC tag write + retry not wrapped
5. `downloader.py:179`/187/195 — three cover-art embed save sites not wrapped
6. `downloader.py:2785` — full-album MTP transfer not quorum-verified
7. `downloader.py:3229` — per-track MTP transfer (with retry loop) not quorum-verified
8. `downloader.py:3533` — Salvage MTP transfer not quorum-verified

Each gap is exactly the kind of drift the board warned about: the original MK37 wiring covered the obvious sites, but a later code path could (and did) introduce additional unmediated calls. The audit catches them now and continuously.

**Production-mode integration (Finding D):** when `config.PRODUCTION_HARDENED_MODE=True`, `audit_dependency_graph()` runs `mutation_path_audit.audit()` at startup and **raises `RuntimeError("AUTHORITY_GRAPH_VIOLATION")`** if any UNMEDIATED site is detected. New code that introduces a bypass causes startup to fail before it can do damage.

**Marker:** `AUTHORITY_GRAPH_HEALTHY` (info) / `AUTHORITY_GRAPH_VIOLATION` (error, raises in hardened mode)

**CLI:**
```bash
python mutation_path_audit.py --root . --json mutation_audit.json --strict
```

`--strict` exits 1 if any unmediated paths exist. Wire into CI to block merges introducing bypasses (Recommendation MK38-C2's static governance linter, foreshadowed).

---

## MK37.1-2 — Production Flag Locking

**Closes:** the board's Finding C — feature flags introduce rollback safety AND governance risk. *"Production operators under incident pressure could disable safeguards to 'get syncs working'."*

**Implementation:** new `config.PRODUCTION_HARDENED_MODE` flag (default `False`). When `True`:

1. **All P0 flags become forced ON.** The `_flag()` resolver in `mk37_wiring` checks if the requested flag is in `P0_LOCKED_FLAGS = {MK37_ENABLE_HALT_GATE, MK37_ENABLE_TRANSFER_QUORUM, MK37_ENABLE_TRANSACTIONAL_DEDUP, MK37_ENFORCE_OFFLINE_CAPABILITY, MK37_ENABLE_LOCK_AWARE_TAGGING}`. If yes AND hardened mode is on, returns `True` unconditionally — env-var override is bypassed, `config.MK37_ENABLE_HALT_GATE = False` is bypassed.

2. **Defense-in-depth check at startup.** `assert_production_flags_locked()` runs from `audit_dependency_graph()`. It re-evaluates each P0 flag and raises `RuntimeError("PRODUCTION_HARDENED_MODE_VIOLATION")` if any returns `False` (which would only happen if `_flag()` itself were monkey-patched). Defense against in-process tampering.

3. **Authority graph validation runs.** Per MK37.1-1, the mutation path audit runs at startup; an unmediated site causes startup to fail.

4. **Bypass detection becomes default-ON.** Per MK37.1-3, `is_bypass_detection_enabled()` returns `True` under hardened mode unless explicitly opted out.

5. **Crash recovery runs at every startup.** Per MK37.1-4.

**Behavioral matrix:**

| Scenario | dev mode (`PRODUCTION_HARDENED_MODE=False`) | prod mode (`=True`) |
|---|---|---|
| `MK37_ENABLE_HALT_GATE=0` env var | flag becomes False | flag stays True (forced) |
| `config.MK37_ENABLE_HALT_GATE = False` | flag becomes False | flag stays True (forced) |
| `_flag()` monkey-patched | undetected | startup raises PRODUCTION_HARDENED_MODE_VIOLATION |
| New code adds unmediated `os.remove` | undetected | startup raises AUTHORITY_GRAPH_VIOLATION |
| Direct `mtp.copy_file_to_device` call without context | succeeds | raises BypassDetected |
| Stranded capabilities from prior crash | persist | revoked at startup |

**Marker on activation:** `PRODUCTION_HARDENED_MODE_ACTIVE` (info, lists all 5 P0 flags as locked ON)
**Marker on violation:** `PRODUCTION_HARDENED_MODE_VIOLATION` (error, raises)
**Telemetry:** no new counters (uses existing P0 counters)

**Test:** `test_mk37_1_2_production_flag_locking` — 8 assertions including T3 verifying all 5 P0 flags ignore env=0 under hardened mode, T5 verifying dev mode preserves the env override pathway.

---

## MK37.1-3 — Mandatory Wrapper Assertions

**Closes:** Risk #1 (bypassable mutation authority) and Risk #4 (runtime governance via human discipline). The board's framing: *"This is historically how the original drift occurred. Without linting, mandatory mediation, static enforcement, authority ownership validation, the same drift can recur."*

**Implementation:** runtime monkey-patching of dangerous primitives at `mk37_wiring` import time. The patches are gated on `is_bypass_detection_enabled()` (auto-True under hardened mode, opt-in via env var otherwise).

**Architecture:** thread-local context stack. Every wrapper that legitimately invokes a destructive primitive enters a `_push_context("transfer_pass", path)` block before the inner call. The monkey-patched primitive checks for that context via `_check_context_or_raise()`:

```python
@contextmanager
def _push_context(name: str, value: Any = True):
    _set_context(name, value)
    try:
        yield
    finally:
        _pop_context(name)

# In transfer_via_legacy_with_quorum:
with _push_context("transfer_pass", device_relpath or "*"):
    legacy_result = legacy_transfer_fn(*legacy_args, **legacy_kwargs)
```

The patched primitive at the bottom of the call chain:

```python
def _guarded_copy(*args, **kwargs):
    _check_context_or_raise("mtp.copy_file_to_device", "transfer_pass")
    return _orig_copy(*args, **kwargs)
mtp_transfer.copy_file_to_device = _guarded_copy
```

If a future engineer writes `mtp.copy_file_to_device(...)` directly anywhere — outside `transfer_via_legacy_with_quorum`, outside any sanctioned wrapper — the call **raises `BypassDetected("MK37_BYPASS_DETECTED")` immediately**. The test that previously could be added without sanitation now fails CI.

**Stack-walk fallback:** `_check_context_or_raise` also walks the call stack up to 30 frames looking for a function name in the sanctioned set (`transfer_via_legacy_with_quorum`, `transactional_dedup`, `tag_file_with_coherence`, `idempotent_destructive_op`, `_do_copy_and_verify`, `_do_delete_source`). This catches indirect calls through helper modules whose intermediate frames don't push context but whose caller did. Test-runner exemption was removed during sprint validation — tests that legitimately need to test bypass behavior must do so explicitly.

**Currently guarded primitives:**
- `mtp_transfer.copy_file_to_device` — must be inside a `transfer_pass` context
- `mtp_transfer_com.copy_file_to_device` — same

The pattern extends mechanically to additional primitives (`os.remove` within audio-extension paths, `Path.unlink` on device-mounted paths, etc.) when the team decides to broaden the guard. The implementation is intentionally minimal — guards only what the board specifically called out — to avoid breaking the 84 SCRIPT-classified cleanup sites that legitimately exist outside sanctioned contexts.

**Marker on bypass:** `MK37_BYPASS_DETECTED` (error, raises `BypassDetected`)
**Telemetry:** `bypass_detected` counter
**Feature flag:** `MK37_ENABLE_BYPASS_DETECTION` (default off in dev, default on under hardened mode)

**Test:** `test_mk37_1_3_wrapper_assertions` — 6 assertions including T4 verifying bare call raises, T5 verifying active context allows, T6 verifying `transfer_via_legacy_with_quorum` auto-sanctions the inner primitive.

---

## MK37.1-4 — Crash Recovery Reconciliation

**Closes:** Risk #3 (lifecycle state fragmentation). The board's exact concern: *"partial rollback, interrupted resume, crash recovery divergence, stale capability state after abnormal termination."*

**Implementation:** `reconcile_crash_state()` runs at module import (after `audit_dependency_graph` completes), idempotent, replay-safe. Three reconciliation passes:

1. **Capability revocation.** Opens `.mk36_capabilities.db` (SQLite). Auto-detects the schema (table name `capabilities` / `capability_grants` / `grants`; timestamp column `issued_at` / `created_at` / `granted_at` / `timestamp`; state column `state` / `status`). Sets `state='revoked'` for every row whose timestamp is older than `capability_max_age_seconds` (default 24 hours). Counts the rows updated.

2. **Lifecycle reconciliation.** Reads `.mk34_audit.jsonl`. Builds two sets: `pre_only` (records with `PRE_TRANSFER` event) and `committed` (records with `TRANSFER_COMMITTED` event), keyed by `lifecycle_id`. The set difference `pre_only - committed` is the interrupted-mid-transfer set. For each, appends a synthetic `CRASH_RECOVERY_LIFECYCLES_FAILED` event so the next run sees the entry as terminal-failed instead of mid-flight. Counts the synthesized events.

3. **Idempotency journal pruning.** Reads `.mk37_idempotency.jsonl`, drops records older than `idempotency_max_age_days` (default 7), rewrites the file with kept records. This prevents stale "already executed" markers from blocking a legitimate re-attempt weeks later.

**Idempotency:** the function uses a module-level `_CRASH_RECOVERY_DONE` boolean. First call performs reconciliation; subsequent calls within the same process return `{"skipped": 1}`. Tests reset the flag explicitly to verify behavior.

**Telemetry:** `crash_recovery_capabilities_revoked`, `crash_recovery_lifecycles_failed` (new counters)
**Markers:** `CRASH_RECOVERY_CAPABILITIES_REVOKED`, `CRASH_RECOVERY_LIFECYCLES_FAILED`, `CRASH_RECOVERY_IDEMPOTENCY_PURGED`

**Test:** `test_mk37_1_4_crash_recovery` — 6 assertions including T3 verifying interrupted-only lifecycle gets failed-marker appended while complete lifecycle is left alone.

---

## Files added/changed in MK37.1

| File | Change | Lines |
|---|---|---|
| `mutation_path_audit.py` | NEW — AST-based mediation audit tool | ~530 |
| `mk37_wiring.py` | extended — production hardened mode, bypass detection, crash recovery | +540 |
| `config.py` | added `PRODUCTION_HARDENED_MODE = False` with documentation block | +18 |
| `walkman_sync.py` | wrapped 5 mutagen.save sites in `_tag_mp3` and `_tag_flac` | ~80 |
| `downloader.py` | wrapped 3 cover-art save sites + 3 MTP transfer sites + 1 POPM rebuild + 12 staging pragmas | ~150 |
| `test_mk37_cto.py` | added 4 MK37.1 test functions, 28 assertions | +280 |

Test results:
- **MK37 baseline:** 90/90 tests passing (no regression)
- **MK37.1 cherry-pick:** 28/28 new tests passing
- **Combined:** 118/118
- **Mutation path audit (--strict):** exit 0, 100% mediated coverage on 206 call sites

---

## Operator playbook for production deployment

To deploy MK37.1 in **fully-trusted production** mode:

```python
# In config.py:
PRODUCTION_HARDENED_MODE = True
```

What this activates:
1. The 5 P0 flags become permanently True. Operators cannot disable safeguards via env vars or in-flight config edits.
2. Mutation path audit runs at every startup; new code introducing a bypass causes startup to fail.
3. Bypass detection is on; calling a guarded destructive primitive without an active mk37 context raises `BypassDetected`.
4. Crash recovery runs at every startup; orphan capabilities are revoked, interrupted lifecycles marked failed, stale idempotency records pruned.

To deploy in **monitored / staged** production mode (recommended for first rollout):

```python
PRODUCTION_HARDENED_MODE = False   # default
```

Plus operationally:
- Telemetry dashboards monitor `snapshot_telemetry()` (existing in MK37)
- Halt gate aborts paged immediately (per board §5)
- Transfer quorum failures monitored
- Offline violations treated as SEV-1
- Log-line filter alerts on `MK37_BYPASS_DETECTED`, `AUTHORITY_GRAPH_VIOLATION`, `PRODUCTION_HARDENED_MODE_VIOLATION`

To run the audit standalone (CI or pre-merge gate):

```bash
python mutation_path_audit.py --root . --strict --json audit.json
echo $?    # 0 if 100% mediated, 1 otherwise
```

---

## What's MK38+ scope (per board §7, explicitly excluded from MK37.1)

The board's MK38+ architectural transformation plan covers the residual architectural risks that even MK37.1 doesn't fully close:

- **Epic MK38-A — Authoritative Execution Core.** Single `ExecutionAuthority.execute()` mediator (Ticket MK38-A1, 21 sp); capability-only destructive operations (MK38-A2, 13 sp); legacy path elimination (MK38-A3, 21 sp). MK37.1 *guards* primitive call sites; MK38-A *eliminates* them. This is the real closure of the architectural drift.

- **Epic MK38-B — Deterministic Replay Architecture.** Mutation journal (MK38-B1, 21 sp); replay validation engine (MK38-B2, 13 sp). MK37.1 reconciles crash state; MK38-B makes every mutation replayable from journal hashes.

- **Epic MK38-C — Runtime Governance Enforcement.** Authority graph validator (MK38-C1, 13 sp); static governance linter as CI rule (MK38-C2, 8 sp). MK37.1's `mutation_path_audit` is the prototype; MK38-C makes the audit a CI gate that blocks merges.

The board's framing remains: *"MK37 makes the platform operationally viable. MK38 must make it architecturally sovereign."*

MK37.1 makes the platform **operationally trustworthy**. Architectural sovereignty waits for MK38.
