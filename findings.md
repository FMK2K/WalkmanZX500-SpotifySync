# Findings

## Research

### Issue 1 — MTP deep-clean hang (Ops Engineer investigation, complete)

**Root cause, confirmed against actual code:**
- `wpd_transfer.py:973` — `$root.GetFolder.Items() | ForEach-Object {...}` builds `$musicItems`
  in ONE blocking COM call. `.Items()` forces the MTP driver to run a full `GetObjectHandles` over
  ~1,754 album folders before the call returns *at all* — this is not a lazily-streamed pipeline;
  the block happens before iteration starts. **Candidate #1 (streaming per-item heartbeat) would
  not have worked** — the hang is inside the single `.Items()` call, not in how the result is
  consumed.
- The "pre-enumeration into a hashtable" approach the brief describes is already implemented
  (`$musicItems[$parts[0]]`, line 981); only sub-album-depth navigation still uses ParseName.
- **Correction to candidate #2**: `Popen.terminate()` and `.kill()` are identical on Windows (both
  call `TerminateProcess`) — there's no SIGTERM distinction to exploit. "Graceful termination" as
  literally proposed doesn't exist on this platform.
- The real mechanism behind "each kill makes the next run worse": Shell.Application MTP calls are
  served by the long-lived, shared `explorer.exe` COM host, not a private process per
  `powershell.exe` invocation. A killed PS client can't clean that shared host's cached MTP
  session. This matches this project's own prior memory finding (Explorer restart / WPD service
  cycle is the documented working fix) — and `preflight.py` already implements that recovery
  (`_close_walkman_explorer_windows()`, `_cycle_wpd()`) but was **never called from
  cli_runner.py's deep-clean path**. That's the actual gap.
- `cli_runner.py`'s deep-clean path called `_wpd2.delete_files_single_ps()` / `_fpath.unlink()`
  directly — zero routing through `mk37_wiring.py` (`mutation_path_audit.py` would flag these
  UNMEDIATED).
- `adb_transfer.py` already has a complete ADB delete implementation
  (`delete_file_on_device`/`_get_storage_path`/`_rm_f`/`_exists`), already proven in `audit.py`'s
  cross-format prune pass. Candidate #4 needed a thin driver, not new adb-shell code.

**Fixes applied:** `preflight.ensure_run_once()` wired into deep-clean start; auto Explorer/WPD
recovery on poisoned-session detection (`hung_gi == -1`) instead of just logging "reconnect
device"; best-effort `taskkill` before hard `kill()` in the watchdog (documented as marginal, not
a real fix); all deletes (MTP batch, direct-filesystem, new ADB path) routed through
`mk37_wiring.idempotent_destructive_op`; new `adb_delete.py` fallback sharing
`.deep_clean_progress.json` with the MTP path.

### Issue 2 — Genre tagging contamination (QA Lead investigation, complete)
- `_artist_override` signature mismatch: **ALREADY FIXED** in prior MK39 Phase I-E sprint.
  `genre_client.py:2390` defines `_artist_override(artist, title="")` (2 params, no `album` kwarg).
  Bad call site (`audit.py:1062-1065`) now goes through `mk39_phase_I_containment.safe_artist_override(artist, title="", **kwargs)`
  (`mk39_phase_I_containment.py:311`) which absorbs the extra `album=` kwarg before delegating. Other call
  sites (`audit.py:1546`, `audit.py:2531`) call the 2-arg form directly and were never broken.
  Test coverage: `test_mk39_phase_I_containment.py:231-281`, `test_mk39_phase_I_complete.py:202-212`.
- `_consolidate_album_genres` actually lives in **`walkman_sync.py:450`**, not `audit.py` (brief mis-attributed it).
  It does album-mode → artist-mode → graph-propagation fallback. It correctly **short-circuits under freeze**
  (`walkman_sync.py:497-512`, calls `mk38_0_semantic_write_barrier.is_frozen()`, returns early if frozen —
  confirmed live in `sync_log.txt`, 9+ "SKIPPED — semantic write freeze active" lines).
- `is_frozen()` (`mk38_0_semantic_write_barrier.py:142-173`) defaults `True`; only unlocks via a 3-way AND
  (override active AND hardened-mode unlocked AND `MK38_0_ALLOW_SEMANTIC_WRITES=1` env var). Fails closed. Correct.
- **Real remaining gap**: no genre-cache purge command exists anywhere (`purge-deleted` in `cli_runner.py:54`
  is for on-device `.deleted` staging, unrelated). Cache lives at `config.GENRE_CACHE_FILE`, managed via
  `_get_genre_cache()`/`_genre_cache_set()`/`flush_genre_cache()`/`invalidate_genre_cache_singleton()`
  (`genre_client.py:2470-2561`, `2539`). Contamination the user sees = pre-MK39-fix poisoned cache rows that
  the (correctly-working) freeze now prevents from ever being overwritten.
- **Fix plan**: add `purge-genre-cache` CLI mode to `cli_runner.py` MODES tuple (line 54), route the actual
  cache-file mutation through `mk37_wiring.py`/MK39-gate since it mutates persisted state.

### Issue 3 — Album folder splintering (Architect investigation, complete)
- Root cause: THREE divergent sanitization/truncation implementations coexist, violating path_utils.py's own
  docstring mandate (path_utils.py:4-8 says all modules must use `canonical_folder()`):
  1. `downloader.py:1086 _safe(name, max_len=60)` — paren-balancing truncation. Used at 12+ real placement
     sites (downloader.py:3135-3917) — the primary engine never migrated to the "canonical" function.
  2. `path_utils.py:23 _pu_safe(name, max_len=60)` — "canonical" but does raw byte-boundary truncation,
     **no** paren-balancing (path_utils.py:35-38).
  3. `walkman_promote.py:357 _safe(s)` — third signature, **no truncation at all**, used for orphan relocation.
- `walkman_dedup._track_keys` (walkman_dedup.py:518-587) already has partial normalization:
  `_strip_trailing_truncation()` (walkman_dedup.py:249-257, "MK39.1 L4.3 / Council L3 Patch"), but its regex
  `\s*\([^)]*$` only strips a trailing *unclosed paren* fragment — does nothing for `_pu_safe`'s plain
  mid-word byte-boundary cuts, which is the majority case. That's why L2/L3 patches only partially worked.
- Fix plan: (a) minimal-delta option — make `_pu_safe` paren-balance identically to `downloader._safe` so all
  3 truncate the same way; (b) generalize `_strip_trailing_truncation` in `_track_keys` to also strip a
  trailing partial-word fragment via prefix comparison, not just unclosed parens.
- mk37_wiring.py is the integration point; a folder-merge command should follow the existing COPY→VERIFY→
  COMMIT→DELETE transactional pattern already used for P0-3 dedup (not a direct move).

### Issue 4 — sync_planner.py AttributeError (Architect investigation, complete)
- Mostly **already fixed**. `generate_plan()`/`build_sync_plan` (sync_planner.py:66/303) has a coercion guard
  at lines 111-130 ("MK39.1 L3.4 / Council P1") converting bare path strings in `device_files`/
  `device_snapshot.files` to `FileState(path=f, size_bytes=0, mtime=0.0)` before `.path` access.
- **Gap A**: `local_files` param (line 141: `for local in local_files: norm = _norm_path(local.path)`) has
  NO coercion — currently dead code risk only, since the sole production call site
  (`walkman_sync.py:1404`) hardcodes `local_files=[]`.
  **Gap B**: `generate_audit_report()` (sync_planner.py:206) is a separate entry point, accesses `f.path`
  directly at line 223 with zero guard — but has no production callers (test-only, per grep).
- `FileState` defined `sync_operations.py:64` (fields: path, size_bytes, mtime, content_hash).
  `patch_sync_planner.py` is an unrelated historical one-shot patch (added the build_sync_plan alias) — not
  the source of this AttributeError.
- Fix plan: factor coercion into a shared `_coerce_filestates(files)` helper, call it for `local_files` too
  in `generate_plan`, and add it to `generate_audit_report`. If the crash is still occurring live in
  production, it is NOT via the known/guarded path — flag to user that we need an actual traceback to confirm
  this hardening addresses the real live failure vs. is precautionary.

## Fixes applied (all 4 issues)
- **Issue 1**: `preflight.ensure_run_once()` wired into deep-clean; auto Explorer/WPD recovery on
  poisoned-session detection; best-effort `taskkill` before hard kill (documented as marginal);
  all deletes routed through `mk37_wiring.idempotent_destructive_op`; new `adb_delete.py`.
- **Issue 2**: `genre_client.purge_genre_cache(artist=None)` + `purge-genre-cache` CLI mode
  (`cli_runner.py`), gated through `idempotent_destructive_op`, dry-run by default.
- **Issue 3**: `path_utils._pu_safe` now bracket-balances identically to `downloader._safe`
  (prevents *future* splintering); `walkman_dedup._prefix_normalize_truncated()` + two new key
  variants in `_track_keys` (retroactively unifies *existing* splinters for dedup matching); new
  `merge-splintered-folders` CLI mode (groups by normalized (artist, album-prefix), moves
  unique files into the canonical folder via `mk37_wiring.transactional_dedup`, deletes true
  duplicates via `idempotent_destructive_op`). Dry-run by default.
- **Issue 4**: factored `_coerce_filestates()` in `sync_planner.py`, applied to `device_files`
  (already covered), `local_files` (Gap A, now closed), and `generate_audit_report`'s
  `device_files` (Gap B, now closed).

## Trade-offs / residual gaps
- Issue 3: `walkman_promote.py:357 _safe(s)` (the third truncation variant, no truncation at all)
  was intentionally **not** touched — it's a narrower orphan-relocation path and changing it risks
  behavior outside this task's scope. It can still produce a folder name that doesn't match the
  other two variants' 60-char-truncated output; flagged for a follow-up, not fixed here.
- Issue 4: the guard closes two real code-path gaps, but neither had a confirmed live production
  traceback (Gap A's only caller hardcodes `local_files=[]`; Gap B has no production caller per
  grep). If the AttributeError is still reproducing in production, we need an actual traceback to
  confirm this hardening reaches the real failure site.
- Issue 1's ADB fallback and Issue 3's folder-merge both require the device in ADB/debug mode,
  which is a manual on-device toggle — neither can run as part of the "unattended overnight"
  automated pipeline as-is; both are one-time/manual cleanup tools.
## Lessons Learned
- This codebase already contains most of the primitives these fixes needed
  (`preflight.py` recovery steps, `adb_transfer.py`'s ADB delete, `mk37_wiring.transactional_dedup`,
  audit.py's Backtrack Reorg pull-push-delete pattern) — the actual bugs were mostly *wiring gaps*
  (existing fix never called from the failing path) rather than missing logic.

---

# TASK 2 — Pipeline Efficiency Pass (audit / reorganize / full-sync), ZX507 target

## Prior investigation (2026-07-15, WPD CPU-usage question — completed before this task started)
- Transport: `wpd_transfer.py` PowerShell + `Shell.Application` COM per file; real `IPortableDevice`
  WPD path exists (`wpd_transfer.py:448-583`) but is dead code — device fails on it.
- Throughput ceiling is the USB/MTP link (~1.2 MB/s, `device_transport.py:12`), not CPU — low CPU
  during WPD calls is correct/expected.
- Local CPU work already parallelized: `TAG_WORKERS=32` (`walkman_sync.py:288`, pool `:2051`),
  `GENRE_WORKERS=25`/`GENRE_WORKERS_AI=8` (`config.py:303,1106`), `DOWNLOAD_WORKERS`
  (`downloader.py:4297`), `TIDAL_META/DL_WORKERS` (`audit.py:4298,4377`).
- Device I/O deliberately serial: `AUDIT_WORKERS=1` (`config.py:210-211`, comment "MTP is serial —
  do not increase either of these above 1"); `_MTP_DEDUP_LOCK` (`mtp_transfer_com.py:33-34`,
  "concurrent MTP writes cause silent failures").
- `preflight._close_walkman_explorer_windows()` / `_cycle_wpd()` exist specifically to reset stale
  COM/WPD state from repeated device access — corroborates 2026-07-12 session's MTP corruption
  root-cause finding above. Any concurrency added to WPD/MTP calls would reopen that class of bug.
- `resource_monitor.py` is a GDI/USER-object/handle-count watchdog (throttles subprocess spawns near
  Windows handle exhaustion) — not CPU-related, not the source of the low-CPU perception.
- Loose end, not yet investigated: `walkman_reorganize.py:33` imports `ThreadPoolExecutor`, never
  instantiated — dead import or missed-parallelism opportunity in reorganize's local-only steps.

## Research (Phase 1, complete — 2026-07-15)

### Finding A — full-sync repeats reorganize + audit 3x per invocation (largest cost candidate)
- `spiral_orchestrator.py:35-37` — `_STAGE_MATRIX["full"]` includes `reorganize` and `audit`/`tag`
  unconditionally for all 3 "revolutions" (rev 1/low, rev 2/medium, rev 3/high — `:116-139`).
- `_run_revolution()` (`:390-415`) calls `walkman_reorganize.reorganize_device(execute=True, ...)`
  and `audit.LibraryAuditor().run()` once per revolution, each unfiltered to that revolution's
  cohort. `reorganize_device()` always does a full two-storage scan (up to 120s timeout x2) and,
  per file found, pulls it from the device via COM `CopyHere` just to check tags
  (`prepare_file()`, `walkman_reorganize.py:1257-1293`).
- Net: a full-sync run pays 3x full-library scan+pull for reorganize, 3x `LibraryAuditor.run()`,
  3x the post-audit `_run_device_scan()` (`:427-510`). Likely the largest real wall-clock cost on
  a ~1000s-of-tracks hi-res library (est. tens of minutes of redundant device round-trips).
- **Not a clear bug** — may be deliberate incremental-safety design (device left in a better state
  if interrupted mid-run between revolutions). Needs a user decision, not a unilateral fix.

### Finding B — misleading "Parallel pull" log line in walkman_reorganize.py (cosmetic, safe)
- `ThreadPoolExecutor` import at `walkman_reorganize.py:33` is genuinely dead — never instantiated.
- `PULL_WORKERS=4` (`:148`) and a `workers` param are threaded into `ReorgWorker.__init__`
  (`:1099-1107`, queue sized `workers*2`) but `reorganize_device()` only ever starts one producer
  thread + one consumer thread (`:1731-1732`) — no pool ever spawns.
- Log line `"Parallel pull: %d workers"` (`:1665`) is false — misleading during runs, not a perf bug.
- Not recommending adding real parallelism here: `prepare_file()` (the producer's per-file work)
  does a device `CopyHere` pull — threading it would be device-I/O concurrency, same corruption
  risk class as MTP, out of scope per hard constraints.

### False leads (confirmed NOT problems, no fix needed)
- Dedup (`walkman_dedup.py`) is tag/path-key based, not content-hashing — no expensive full-FLAC
  hashing exists. `content_hash` in `FileState` is passthrough, never computed from file bytes.
- Genre cache correctly consulted before AI calls (`genre_client.py:3951-3957`, `_should_refetch`)
  — re-runs skip already-cached tracks as intended.
- `walkman_sync.py` calls `reorganize_device`/`LibraryAuditor` in-process (`:2386-2416`), not via
  subprocess — no Python-startup or re-scan-from-disk tax from process boundaries.
- No force-rescan or cache-TTL config flags defaulting to "always rescan."

### Scope confirmation
Neither finding touches `AUDIT_WORKERS`, `mtp_transfer_com.py`, `wpd_transfer.py`,
`device_transport.py`, or `_MTP_DEDUP_LOCK`.

## Phase 1.5 — deeper dive on Finding A (2026-07-15, user asked to investigate before deciding)

- Revolutions (`spiral_orchestrator.py`) are a **genre-resolution risk ladder**, not a file-
  processing ladder: `_GENRE_DEPTH = {1:4, 2:7, 3:8}` unlocks progressively more genre sources
  (rev1: cache+3 free APIs; rev2: +graph inference from rev1's confident results; rev3: +AI,
  quota-gated). Only `download` is cohort-filtered per revolution (`_filter_track_map`, `:377`).
  `reorganize` (`:390-401`) and `audit` (`:403-415`) are called **unfiltered**, full two-storage
  scan, every revolution — the cohort variable exists in scope but isn't passed to either.
- **`audit`'s repetition is load-bearing — do not collapse it.** It's a deliberate quota-
  conservation design: cheap sources get first shot before AI burns quota on rev3. Its *scan* cost
  (re-pulling tags for already-resolved tracks) is technically separable from its genre-depth
  logic, but no scan-state caching exists between revolutions — reducing that would require
  `LibraryAuditor` to support reuse of prior scan state. **Higher risk, not recommended this pass.**
- **`reorganize`'s repetition looks like an oversight, not a safety design.** `reorganize_device()`
  takes no per-revolution parameter, has no dependency on genre-tag content or `_GENRE_DEPTH`/
  `AI_GENRE_ENABLED`, and no resumability/checkpoint code was found that depends on it re-running
  after each revolution specifically (`_verify_db()` runs post-revolution regardless). It simply
  never got the same cohort-filtering treatment `download` received.
- **Recommended minimal fix**: run `reorganize_device()` once, after all 3 revolutions' downloads
  have landed, instead of once per revolution. Removes 2 of 3 full two-storage scans + per-file
  device pulls. Does not touch `audit`'s genre-tier logic at all.
- **Verdict: not "leave it alone" wholesale, and not "fix everything" either.** Reorganize
  de-duplication is the safe, load-bearing-free win. Audit scan-cost reduction is explicitly
  out of scope for this pass — separate, riskier change.

---

# TASK 3b — MK38.0 unlock path investigation (read-only, 2026-07-15)

- **Incident reconstructed**: LOG_17 — a graph built from the 2271-track "Liked Songs" playlist
  created near-complete artist-to-artist edges (every artist co-occurs in it), drowning real
  feature-collab signal. 776/805 sparse-artist genre "rescues" collapsed to "Afrobeats"
  (`genre_graph.py` comments, corroborated `walkman_sync.py:570-586`;
  `test_mk38_0_semantic_freeze.py:48-55` confirms the write path was `ai_inference`). Smaller
  related incident: "Alfa Kat → Abstract Hip-Hop" modal-of-1 on a 1-track sparse artist
  (`walkman_sync.py:463-469`).
- **Safeguards that exist now but didn't at incident time** (all independent of MK38.0 itself):
  playlist purity weighting + min-purity skip (`genre_graph.py:222-354`), explicit Liked-Songs
  handling + oversized-playlist proportional sampling (`:238-300`), diversity guard tightened
  0.5→0.35 with pre-scan suppression (`walkman_sync.py:570-646`), specificity guard — graph
  inference can never override an already-specific genre, only broad placeholders (`:704-717`).
- **Is the freeze over-cautious now?** Partially, but not clearly wrong to keep — MK38.0's design
  intent (`mk38_0_semantic_write_barrier.py:149-151`) is a backstop independent of any one path's
  correctness ("any code path that forgets to check this gets blocked, not allowed"). It blocks
  ALL inference sources for ALL semantic fields project-wide, not just this path.
- **A supervised-unlock template already exists**: `activate_spiral_rev3()`
  (`mk38_0_semantic_write_barrier.py:369-401`) — scoped `with`-block promotion of `ai_inference` to
  canonical, blocked under hardened mode, logged at entry/exit. `_consolidate_album_genres` has no
  equivalent scoped path today, only the blanket `is_frozen()` short-circuit.
- **Options, ranked by risk**:
  1. **Lowest risk**: add a dry-run/preview — log the old→new genre list `_consolidate_album_genres`
     would produce (it already builds this at `:649`) without writing. No freeze change at all.
  2. **Medium risk**: one supervised run via the existing `with_freeze_override(reason)` +
     `MK38_0_ALLOW_SEMANTIC_WRITES=1` + hardened-mode-unlocked mechanism (`:404-413`, already built
     for "batch operations the operator has explicitly approved"). Inspect `snapshot_telemetry()`
     + a sample of real writes before deciding whether to leave it on.
  3. **Not recommended as a standing change**: permanently loosening MK38.0 (e.g. adding
     `graph_propagation`/`modal_consensus` to `CANONICAL_SOURCES`) removes the defense-in-depth
     backstop the module exists to provide.
- **No option is "definitely do this"** — freeze remains defensible; 1-2 are the safe ways to get
  informed before any decision. Nothing implemented from this investigation.

## Structural assessment vs. SDD_MK12.md (2026-07-30, read-only, no code changes)

User asked for a structural/architectural assessment of the codebase against what `SDD_MK12.md`
says the project is trying to achieve, explicitly noting the SDD is known to be outdated. Confirmed
that directly: **`SDD_MK12.md` (v1.1) and README.md Parts VII/VIII both stop at Task 14
(2026-07-18)** — 12 days and five further sessions of real work (Tasks 15-19 in `task_plan.md`:
ADB-over-WiFi transport, ADB connection diagnostics tooling, the `SystemExit`-vs-`Exception` crash
fix, the WPD silent-skip logging fix, cohort_classifier/tag_scan fixes) are entirely undocumented in
either living design doc. The SDD's own §2 Architecture Overview diagram doesn't show ADB-over-WiFi
at all, despite it now being a real, wired-in second-stage fallback in `cli_runner.py`'s reconnect
path (`adb_transfer.reconnect_wifi_if_configured()`).

**1. The core dispatcher design is intact and matches the SDD/README's stated architecture** —
`mtp_transfer.py` (539 lines) remains a genuinely thin single-choke-point router (per README §9:
"a change to exactly one function, not every caller"). This is the one place structure has NOT
drifted from intent.

**2. The SDD's own predicted risk (MK11-2/MK11-9) is confirmed still open, 12 days later.** SDD §8
Roadmap item 7 flagged: the quiet-timeout + max-duration dual-guard pattern (§4, built for Task 4's
WPD scanner) was only ever applied to one new call site (`localsend_transfer.py`'s COM fallback) —
`downloader.py`, `audit.py`, `walkman_reorganize.py`, `walkman_sync.py`, `walkman_dedup.py` were
explicitly named as still calling the unprotected in-process `copy_file_to_device` directly. Grepped
all five tonight (2026-07-30): zero references to `quiet_timeout_s`/`max_duration_s`/
`copy_file_to_device_isolated` in any of them — the gap is unchanged since the SDD flagged it. This
is the same "recovery code exists but isn't wired into the failing path" pattern (Risk MK11-2) that
the SDD itself says recurred three separate times in one era — now effectively a fourth/fifth
recurrence (tonight's SystemExit crash and WPD silent-skip bugs are structurally the same class:
correct-looking handling that the actual failure path doesn't reach).

**3. `config.py` has real, quantifiable sprawl.** 1,536 lines, 67 `# ---` section-divider comments
as the only organizing mechanism, 23 boolean `*_ENABLED` feature flags spanning transport
(`LOCALSEND_ENABLED`, `ADB_WIFI_ENABLED`), metadata (`LYRICS_ENABLED`, `GENRE_GRAPH_ENABLED`,
`ACOUSTID_AUTO_CORRECT_ENABLED`), discovery (`LISTENBRAINZ_DISCOVERY_ENABLED`), and pipeline
behavior (`VERIFY_TRANSFERS_ENABLED`, `SCRUB_ENABLED`) all flat in one namespace with no grouping
construct (no nested config object, no per-feature file). Every task across many sessions adds one
or two more flags here; nothing has reduced the flat structure as it's grown.

**4. `adb_transfer.py` has quietly absorbed a second responsibility.** Originally an ADB
transfer-primitives module (mirroring `mtp_transfer_com.py`'s COM-primitives role), it's now also
the home for a full WiFi-ADB *connection lifecycle* (`connect_wifi`, `disconnect_wifi`,
`reconnect_wifi_if_configured`, `get_device_state`, `reset_adb_pairing`,
`diagnose_windows_driver`) added tonight — 1,658 lines total. Not wrong to put it there (it's still
ADB-specific), but `device_is_connected` now exists independently in three places
(`mtp_transfer.py`, `mtp_transfer_com.py`, `adb_transfer.py`) with no shared interface — worth a
look if a fourth transport-state concept gets added, though not yet bad enough to call broken.

**Net assessment**: the *dispatcher* structure the SDD centers on is healthy and hasn't drifted.
What has drifted is documentation currency (12-day/5-session gap) and — more importantly — the
SDD's own named reliability-pattern-coverage gap (Risk MK11-9) remains genuinely unresolved, not
merely undocumented, which matters more than the doc staleness itself.
---

## 2026-09-02 — Post-wipe recovery project — findings

### Storage split (goal 1)
- `adb_transfer.pick_storage_balanced(file_size, filename)` ALREADY EXISTS (built 2026-07-17 for this
  exact ask — "balance across Internal + SD by percent-used so both fill proportionally"). Reserves
  `config.INTERNAL_STORAGE_MIN_FREE_MB`. Chooses the volume with lower % used.
- **It is NEVER CALLED.** Only referenced in a config.py comment (line ~1208). "Built but never
  wired up" pattern again.
- The live storage picker is `adb_transfer._pick_storage_for_file()` = primary(`WRITE_STORAGE`)
  then fallback(`SD_STORAGE`). Called from `downloader.py` (~line 3281, 3619) and referenced across
  `audit.py`. No config flag toggles balanced-vs-primary.
- Wiring cost (ponytail): add `config.STORAGE_BALANCED_PLACEMENT` flag; in downloader's ~2 pick
  sites, call `pick_storage_balanced` when the flag is set. DEFERRED to Phase D (needs internal
  storage stable = rooting settled).

### Album completeness (goal 4)
- `album_completeness.find_missing_tracks_for_albums(saved_album_ids, ...)` exists.
  `ALBUM_COMPLETENESS_ENABLED=True`, `ALBUM_COMPLETENESS_MAX_NEW_DOWNLOADS=200`.
- Per 2026-08-23 investigation: only called from the LEGACY `WalkmanSyncAgent.run_sync()` path,
  reachable ONLY via the "⚡ FULL PIPELINE" button (the one thread without the `SPIRAL_ENABLED`
  gate). Not in `SpiralOrchestrator._STAGE_MATRIX`. `saved_album_ids` is an unordered set, no
  prioritization, flat 200 cap.
- `main.py:3433/3437` — FULL PIPELINE builds `saved_album_ids` via
  `spotify_local_data.fetch_and_build_offline()` (offline) or `spotify_client.fetch_and_build()`
  (live), passes as `liked_album_ids=` to the agent.
- User's decision: complete Spotify-SAVED albums fully; playlist-only lost tracks = re-fetch the
  individual lost tracks only. So "⚡ FULL PIPELINE" already does the saved-album-completion part
  (subject to the 200 cap — may need raising for a 411-album backlog).

### Genre corruption root cause (goal 3) — CONFIRMED live
- `graph=off` every revolution: `_build_graph()` finds "no confident cache entries" because
  `_build_graph` only trusts cache `source in ("lastfm","tidal","discogs","musicbrainz","override")`
  and **100% of `.genre_cache.json` (4703 entries) has `source="phase1"`** (Task 33 Part C — the
  `_source` field is dead, never propagated through the Phase 1/1.5/2 waterfall).
- With no graph, the genre pipeline falls to the STARVATION FALLBACK, which seeds weak edges from
  the genre cache. The user's library is heavy on Amapiano (256 cache entries) / Afrobeats (192) /
  Afro House. Blank-genre tracks (Adele "19" was `genre=None` in the 2026-07-28 tag snapshot) get
  assigned the library-dominant genre -> Adele "19" = 10/12 "Afrobeats", 1 "Amapiano", 1 "Soul
  Blues". Exact LOG_17 "everything became Afrobeats" pattern.
- The bad values are ALSO written back into `.genre_cache.json` (`adele|daydreamer -> Soul Blues`,
  `adele|make you feel my love -> Amapiano`) so a re-run re-applies garbage.
- Blast radius: **1,797 SD audio files** rewritten 2026-09-02 (parsed from `ls -lR` mtimes;
  `scratchpad/today_rewritten.txt`). ~28% of the 6,411-file SD library. Not all genre-wrong (some
  are legit year/track#/art fixes; genuinely-African artists got correct genres) but a large
  fraction of the genre writes are corrupted.
- Fix options: (A) Task 33 Part C — thread real provider identity so the graph builds properly;
  (B) minimal — gate the starvation fallback so it cannot assign a genre that no confident
  same-artist / same-album evidence supports (i.e. never let "cache is mostly X" alone decide an
  unrelated artist's genre). (B) is smaller and directly kills the bleed.

### Device wipe cause — CONFIRMED via peer session
- Peer `spotifytunermk12-42` attempted `fastboot oem unlock` via uuu.exe TWICE. Both failed
  client-side timeout, bootloader stayed `green`/locked — **but userdata got wiped both times**
  (= the two reboots this session). Theory: Android `oem_unlock_allowed` toggle never persisted
  (`settings get global oem_unlock_allowed` returns null), so the bootloader wipes userdata as an
  unconditional anti-tamper step in its unlock handling without granting the unlock.
- Not a full factory reset — apps (LocalSend), WiFi config, `/data/data` survived; only
  `/storage/emulated/0` media was formatted.
- Rooting is PAUSED, no ETA, may be abandoned. Peer will ping with a real decision.
- => internal storage is UNSAFE to repopulate until that decision. All recovery work is SD-only.

### Album-completeness wiring (goal 4) — detailed, for the "into main sync" ask
- Legacy `walkman_sync.py` ~line 1901 "Stage 0.7": gated on `ALBUM_COMPLETENESS_ENABLED` +
  `saved_album_ids` + `sp` (online) + not cancelled. Calls
  `album_completeness.find_missing_tracks_for_albums(saved_album_ids, track_map, sp_client, dev_lib,
  max_new_downloads=cap)`, injects results as `track_map["__completeness_fill__"]`, runs a follow-up
  `dl.run_downloads(track_map={"__completeness_fill__": missing}, liked_album_ids=saved_album_ids, ...)`.
- Reachable ONLY via "⚡ FULL PIPELINE" (`_full_pipeline_thread` -> `WalkmanSyncAgent.run_sync`).
- `SpiralOrchestrator._run_revolution` (spiral_orchestrator.py ~465): `download` stage calls
  `dl.run_downloads(track_map=_filter_track_map(track_map, cohort), cancelled, liked_album_ids=
  self._liked_ids, sp_client=self._sp, log_callback=self._log)`. NO album_completeness call.
- `downloader.run_downloads(track_map, cancelled, liked_album_ids=None, sp_client=None,
  log_callback=None)` — uses `liked_album_ids` only for per-track `is_liked` derivation (line ~2911),
  NOT for completeness.
- track_map track dicts carry: `id, title, artist, artists, album, album_artist, album_id, is_liked`
  (album_completeness.py:70 docstring; spotify_client.py:292). Enough to build the a/b/c expanded set
  with no extra Spotify API calls.
- Plan: `build_completion_album_set()` + `run_completeness_pass()` shared helpers in
  `album_completeness.py`; new `"completeness"` stage in `_STAGE_MATRIX["full"]` + `["download"]`;
  legacy Stage 0.7 refactored to call the shared helper. New config
  `ALBUM_COMPLETE_MIN_LIKED=3`, `ALBUM_COMPLETE_ARTIST_HAS_SAVED=True`.

### Playlists (goal 5, added 2026-09-02) — wiring
- `_write_m3u8()` (~walkman_sync.py:1280) + smart-playlist block "Stage 7b" (~2347-2387) + a per-run
  incremental `m3u8_entries` list appended during the legacy transfer loop.
- Reachable ONLY via "⚡ FULL PIPELINE". No `"playlist"` in `SpiralOrchestrator._STAGE_MATRIX`.
- Smart playlists cross-ref only `self.tracks_transferred` (this-run only) — a real legacy limitation.
- `spotify_client.fetch_and_build` / `spotify_local_data` — need to confirm whether they pull the
  user's FOLLOWED (not just owned) playlists. `sp.current_user_playlists()` returns both; filter by
  `pl['owner']['id'] == me` for created vs followed.
- Plan: new end-of-run `"playlist"` stage that re-scans the device, fuzzy-matches each playlist's
  tracks to on-device files, writes/overwrites M3U8s, prunes M3U8s for deleted/unfollowed playlists,
  and feeds playlist tracks into the download queue.

### Genre corruption SCOPE (Phase 0.3 result) — much smaller than feared
- 1,797 SD files rewritten by the audit on 2026-09-02.
- 40-artist stratified sample: 8 OK, **0 clearly wrong**, 30 no-baseline-but-reasonable.
- 15 Western-artist targeted check: **only Adele bled** (9 Afrobeats + 1 Amapiano + 1 Soul Blues of
  11). "Alice In Chains -> Alte" (wrong). Everyone else OK/close.
- `.genre_cache.json`: 469 African-genre entries / 107 artists — 50-sample = 100% genuinely African
  artists correctly tagged. NOT poisoned.
- Cache contradiction scan (corruption signature = 2+ distinct specific genres per artist): only 6
  artists, only `adele` {Amapiano, Soul Blues} is real corruption. `drake`/`smiths`/`tayk` defensible.
  `smashing pumpkins`/`sza` have a stray "Alte".
- "Alte" = a real Nigerian-alt genre (Cruel Santino, Odunsi) — the pipeline mis-applies it to Western
  alt/grunge artists. Same bleed mechanism as Adele->Afrobeats: library-dominant African genre
  assigned when the lookup finds no strong signal.
- => targeted fix (~dozens of files), not a full 6,400-file re-audit.
