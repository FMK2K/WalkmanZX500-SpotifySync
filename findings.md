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

---

# 2026-09-09 (cont.52) — README / SDD feature-gap audit (for the doc-update task)

Goal: document where README.md describes buttons/features that are not wired, are
broken, or behave differently than written. Verified against the live `src/` tree.

## G1 — README §6 "Buttons" table is 5 buttons; the GUI has ~93
- README §6 lists only: START SYNC, DOWNLOAD ONLY, AUDIT ONLY, REORGANISE, CANCEL.
- `main.py` actually builds: `▶ START SYNC` (`_start_sync`), `⚡ FULL PIPELINE`
  (`_start_full_pipeline`, l.700), `🧹 CURATE LIBRARY` (`_start_curate`, l.1564),
  `🧰 DOWNLOAD ONLY`, `🔍 AUDIT ONLY`, `🔀 REORGANISE`, `■ CANCEL`, **plus the
  entire Tools panel** — ~80 `_tool_btn(...)` entries (l.774-1398): genre-cache
  ops, log forensics, device/ADB diagnostics, LocalSend check, dedup + every
  curation stage as a dry-run/EXECUTE pair, promotion plan build/execute/rollback,
  genre-graph, completeness, snapshot viewers, batch-rebuild, config editors.
- Memory (cont.51) confirms "93 buttons" headless-verified. README documents ~5%.

## G2 — README §7 "All 9 Stages" describes the LEGACY waterfall, not what runs
- `_sync_thread` (main.py:3747): `if config.SPIRAL_ENABLED (default True): _spiral_run("full"); return`.
  The legacy `WalkmanSyncAgent.run_sync()` (the Stage 0-8 waterfall README §7
  documents) is only reached when `SPIRAL_ENABLED=False`.
- `▶ START SYNC` → `_spiral_run("full")` → `SpiralOrchestrator` with
  `_STAGE_MATRIX["full"]` = {dedup_hard, dedup_soft, completeness, download,
  transfer, tag, audit, reorganize, catalog_scoped, duration_verify, mislabel_fix,
  desplinter, deluxe_merge, fav_upgrade, covers, year_backfill, storage_rebalance,
  album_normalize, favsync} run over 3 genre-depth "revolutions". ~19 stages,
  not 9, different names, different order.
- The "9 Stages" narrative is still literally accurate ONLY for `⚡ FULL PIPELINE`
  (`_full_pipeline_thread` → `WalkmanSyncAgent.run_sync()`) and the
  `SPIRAL_ENABLED=False` fallback.
- `walkman_sync.py`'s `run_sync()` calls from `_audit_thread`/`_reorganize_thread`/
  `_sync_thread` are dead code under the default `SPIRAL_ENABLED=True`.

## G3 — Playlists & Favorites: partially closed since the 2026-08-22 gap, still not "automatic"
- HISTORY: 2026-08-22/23 (Task 31 Interrupt 3 / Task 33) found playlist generation
  + album-completeness lived ONLY in `WalkmanSyncAgent.run_sync()`, unreachable
  except via `⚡ FULL PIPELINE`. Memory MEMORY.md still says "NOT fixed, real
  feature gap" — that line is now STALE.
- 2026-09-02 (post-wipe recovery) DID wire both into the spiral:
  - `completeness` stage (spiral_orchestrator.py:706-757, Rev 1 only) calls
    `album_completeness.run_completeness_pass()`; `rank_followed_artists()`
    (album_completeness.py:189) now ranks by liked-track engagement — so
    "prioritized album-completeness" now EXISTS (was the Task 33 Part B ask).
  - `favsync` stage (spiral_orchestrator.py:388-402): runs
    `cli_runner build-favorites-playlists-plan` then `walkman_playlist_import.py`
    (writes M3U8s).
- REMAINING GAPS (real, current):
  1. `favsync` writes M3U8s but CANNOT load them — it logs a WARNING telling the
     user to MANUALLY trigger a device "Update database" rescan (UI tap) and to
     MANUALLY re-star deleted favorites via
     `WALKMAN_FAVPLAYLIST_PLAN=... python walkman_favplaylist_apply.py favorites`.
     So playlists/favorites are semi-automatic at best.
  2. Smart / streaming-history playlists (legacy "Stage 7b" `smart_playlists()` off
     `streaming_history.py`) were NEVER ported to the spiral — only the
     plan-based one-playlist-per-Spotify-playlist M3U8s. `_build_scores` /
     `priority_score` exist but nothing wires them into spiral playlist output.
  3. README §7 Stage 7 still says "the tool writes .m3u8 ... The Walkman's music
     player reads these" as if it's automatic and complete.

## G4 — LocalSend (README §9 "Method 0") is BROKEN, not merely "optional"
- README §9 + §56 present LocalSend as a working optional WiFi transport
  ("Internal Storage Only").
- 2026-08-22 (task_plan.md:3021-3033): root-caused — the Walkman's LocalSend app
  now requires **mutual TLS (client certificate)**. `http://<ip>:53317/api/localsend/v2/info`
  returns raw TLS Alert bytes; HTTPS gives `TLSV13_ALERT_CERTIFICATE_REQUIRED`.
  `localsend_transfer.py` only speaks plain unauthenticated HTTP.
- Status: NOT fixed (needs client-cert generation + LocalSend pairing/registration
  flow). Every LocalSend attempt fails and falls back to ADB/COM. `LOCALSEND_ENABLED`
  defaults `False`. The "Check LocalSend connection" GUI button just reproduces the
  failure.

## G5 — `🔀 REORGANISE` button can orphan favorites/playlists
- `_STAGE_MATRIX["reorganize"]` runs the `reorganize` stage →
  `walkman_reorganize.reorganize_device()`. Per SDD_MK13 §8, `walkman_reorganize.py`
  has NO favorited/playlisted pin. `curate` deliberately avoids the `reorganize`
  stage (uses desplinter + deluxe_merge instead) but the `reorganize` mode /
  REORGANISE button still calls it. A reorganize run moves favorited files →
  their `is_favorites` rows are content-rematched on rescan (survive) but M3U8
  playlist lines break until a `favsync` (which `reorganize` mode DOES include).
  Favorites for any file the reorg happens to delete are lost.

## G6 — curate pipeline + `album_normalize --exhaustive` wired but never run E2E
- Tasks 37/38 (2026-09-08): full `curate` chain + `--exhaustive` album-order fix
  wired into `_STAGE_MATRIX` and given standalone GUI buttons, all compile-clean,
  but "NOT yet run end-to-end from the GUI (device was asleep/ADB down at session
  end)". The 3 cleanup passes that DID run used `--fix-tags`, not `--exhaustive`,
  so scrambled-TRCK-tag clean-looking folders (the "IGOR still out of order"
  class) are still unfixed library-wide.
- 2026-09-09: device IS now connected (NW-ZX500Series on ADB) — the live
  click-through from README §93 "Deferred" is now unblocked.

## G7 — smaller known deltas
- Stats bar reads 0/0/0 for the whole spiral run — `_spiral_run` calls `orch.run()`
  as one blocking call; `_update_audit_stats()` fires once at the end. Known UX
  gap, not a bug (task_plan.md:3065). README §6 "Stats Bar ... shows live counts"
  overstates it for spiral runs.
- Genre graph: 100% of `.genre_cache.json` has `source="phase1"`, which
  `_build_graph()` doesn't trust → `graph=off` every revolution → starvation
  fallback (the genre-bleed mechanism). Task 33 Part C (thread real provider
  identity) is deferred. Partially mitigated by later guards.
- `_consolidate_duplicate_folders` groups folders by last-path-component only →
  still merges distinct same-titled albums by different artists (SDD_MK13 §4.2 /
  §8). Why `curate` uses `walkman_desplinter.py` instead of a full `audit`.
- `pick_storage_balanced()` still never called (findings 2026-09-02) — but
  functionally superseded by `walkman_storage_rebalance.py` + the
  `storage_rebalance` stage, so this is now cosmetic dead code, not a gap.

## Doc-build mechanics
- No pandoc. `SDD_MK13.docx` docProps/core.xml creator = `python-docx`,
  default template (Mac Word Normal.dotm). anaconda base has `python-docx` 1.2.0
  + `markdown`. → write a small md→docx converter (headings / tables / code
  fences / bold / lists), commit it as `docs/md_to_docx.py` for repeatability.
  DONE 2026-09-09.

---

# 2026-09-09 (cont.52) — Comprehensive master SDD (Task 40) — source map

Building docs/SDD_SpotifyTuner_Complete.md. Astral sample structure (from
C:\Users\FeisalK\Downloads\Astral-SDD-Research-Page.docx, dumped to
scratchpad/astral_sdd_dump.txt): Cover ("Software Design Document"/title/team/
distribution clause) -> REVISION HISTORY table (Revision|Date|Affected Pages|
Explanation) -> TABLE OF CONTENTS -> 1 Scope (Document Purpose and Scope) ->
2 Overview (system philosophy / operational workflow / user journeys / frontend
separation of concerns / theoretical background + definition of terms) ->
3 Software Architecture (high-level flow / Development Environment / SBOM tables
[Component|Version|Usage|Developer|Integration Status] / File Structure tree /
Class structure) -> 4 Data Design (Object structure / Database Model / IPC /
Memory Schema / JSON Payload Structure) -> 5 Software Functionality (core
capabilities / use cases / function execution) -> 6 Conclusion (decision +
rationale / success metrics / key risks + mitigations / integration challenges /
proposed strategy). ~490 lines, 2 tables, H1/H2/H3.

## Version-history spine (READ from README this session)
- NUMBERING IS TANGLED. Two+ schemes:
  * Rev 1..Rev 14, then Rev 14.1..14.9.8. Rev 14.9.8 == "MK17".
  * "MK8 Patch Notes" (LOG_17/17_2, 7 bugs) sits between Rev history and Part II.
  * Part II "MK26->MK33" = the SDD_MK15..MK22 line renumbered (adversarial CTO).
  * Part III MK34, IV MK35, V MK36, VI MK37-39.1, VII MK11/MK12 (yes, "11/12"
    is the Operational Hardening era, out of numeric order), IX MK13, X-XII.
- Era I Rev1-14 (hardware reality): Rev1 CSV tag writer; Rev2 ID3v2.4-silently-
  ignored -> save(v2_version=3), two-pass fuzzy match, TPE2 AlbumArtist mandatory,
  FAT32 sanitize; Rev3 Spotify API (genre 403 latent); Rev4 spotipy OAuth + Liked
  + POPM/RATING workaround; Rev5 Shell.Application CopyHere (async discovery +
  single-thread serialize) + Tk GUI; Rev6 WALKMAN_STORAGE "Memory Card" bug (100%
  of "folder not found"), genre_client waterfall (Last.fm->MB->Deezer->Discogs),
  FEATURE_ARTISTS_IN_TITLE; Rev7 Stage 0 auto-download SpotiFLAC+TIDAL, album-wise,
  ffmpeg dep; Rev8 adb_transfer.py + dispatcher pattern (mtp_transfer.py router);
  Rev9 device_library.py structured (norm_artist,norm_album)->{titles} index
  (killed "Intro" false positives); Rev10 audit.py + walkman_reorganize.py;
  Rev11 ai_helper.py (Gemini->DeepSeek, 5s timeout), GENRE_WORKERS 50, F15
  sleep-prevent; Rev12 Tools sidebar, log viewer + error analyser, per-artist
  genre purge; Rev13 USE_ADB=True default, am broadcast MEDIA_SCANNER per file,
  dialog_watcher.py daemon (3 backends), AUDIT_FOLDER_MODE.
- Rev14 (diagnostic): HypB id3_helpers.py — save(v2_version=3) silently drops
  v2.4-only frames (TDRC/TIPL/TMCL/TSOP/TSOA) unless update_to_v23() first; 22
  save sites, 0 called it, bug since Rev8; 1,417 downgrades in LOG-9. HypA
  walkman_app_refresh.py — Sony Music app has private SQLite DB, ignores
  MediaStore; ui_tap (safe) / pm_clear (destructive) / off. HypC cover_art.py —
  ZX500 only decodes baseline JPEG <=500px <=90KB reliably. + scan retry ladder,
  preflight.py (kill adb.exe / close Explorer / cycle WPD / disable AutoPlay),
  workload routing bulk/audit, sync_state.py SQLite per-file/field priority DB,
  resource_monitor.py (GDI/USER/handle watchdog 3-tier warn/throttle/abort).
- Rev14.1 LOG-9 hotfix (reorg thread missing hooks; silent-success hooks;
  _ps_exe NameError; implicit workload tag). 14.2 dialog_watcher.start() NEVER
  CALLED since Rev13 + Win11 Fluent hyperlink dialogs + Cancel-safety list.
  14.3 session_guard.py — Win Update TrustedInstaller 3x reboot mis-blamed as
  agent shutdown; pending-restart warn + stale temp sweep + WAL checkpoint per
  batch. 14.4 walkman_dedup.py rewrite (zero-length, inferior-metadata, hard vs
  soft .deleted/, report/active/off, canonical (artist,album,title) key). 14.5
  resource_monitor 3-tier (WinError 1450 = GDI not disk). 14.6 per-provider
  adaptive timeouts (was flat 180s x9), provider circuit breaker (15 fails),
  streamrip 3x->1x, UTF-8 stdout, robust _exists(), Gemini breaker (20x429),
  post-scan ADB 3x retry; MC 4.33x speedup. 14.7 walkman_promote.py (dedup
  BEFORE reorganize, ZX500 format priority ladder, 4-gate execute safety +
  rollback journal). 14.8 ADB contract bug (dest_parts folder-vs-file),
  per-worker CopyHere breaker, deferred_transfers.py (>100MB), transport_selftest.
  14.9 self-heal: _parse_adb_push_output (N pushed => success despite exit 1),
  _verify_remote_size, device_scrub.py (remove file-blocking-folders),
  sync_planner.py, device_index.py (find -maxdepth 3 once), free_space_check.py.
  14.9.1 F1 GUI clobber of WRITE_STORAGE / F2 router -1 fallthrough / NoneType
  .strip() killed index; MC: refresh TIDAL token = 40x any code fix. 14.9.2
  7 patches (empty playlists key-vs-content; African Giant genre splinter ->
  _consolidate_album_genres; m4a->mp3 _tag_file return path; audit "0 files"
  no-op; metakey invalidation). 14.9.3 hardcoded track_number="1" in local
  fallback. 14.9.4 title cleaning + NFC + master artist string. 14.9.5
  OFFLINE_MODE (spotify_local_data primary) + 2-tier genre consolidation +
  narrowed _BROAD_GENRES. 14.9.6 U1 utf-8 subprocess / A1 artist genre cache /
  A2 Groq fallback when Gemini breaker trips / R2 smart title-case. 14.9.7 P1
  tag-write retry (600 Errno13 Defender locks/13h) / O1 audit stray Spotify call
  in OFFLINE_MODE / P2 _safe paren-balance / P3 ADB daemon self-heal. 14.9.8
  (MK17) Part A P1-P5 operator hardening + Part B M1-M7 (downloads_ingest.py,
  sparse-artist genre guard GENRE_ARTIST_MIN_TRACKS, cross-format dedup, album
  completeness "WE ALSO WANNA MAKE SURE WE HAVE COMPLETE ALBUMS ALWAYS").
- Part II MK26-33: adversarial CTO, 62 patches. MK26 8 log bugs (DIALOG_STORM,
  RunAbortPolicy). MK27 10 structural (_stage1_ok gate, _ensure_deleted_folder,
  settling_delay_s=1.5 Sony FUSE, _MTP_DEDUP_LOCK). MK28 3 epics (inference/cache
  separation, metakey redesign, device sync contract). MK29 epistemic separation
  (source-type discrimination, truth resolver, lazy invalidation, sync state
  machine). MK30 evaluability field, legacy_unknown, sampling validation. MK31
  authority field, contested_external, intent log, retryable-vs-manual. MK32
  AUTHORITY_POLICY frozen dict, jitter, heartbeat, operator CLI. MK33
  POLICY_VERSION+decision_trace, provider rate budget (token buckets), novel
  severity, owner_id+lease, ResolvedView. Archive: walkman_sdd_MK19.docx.
- Part III MK34 Reconciliation Engine: 3 failure classes (A false success
  CopyHere size 0 / B state divergence ADB 27901 vs 0 / C silent destructive
  Stage0 deletes 3646 then Stage1 fails continue). Schema v34.0: SourceRef ->
  RawRecord -> NormalizedTrack + FieldProvenance / FieldConflict / Reconciliation
  Decision / AuditEvent; 6-method engine ingest->normalize->detectConflicts->
  resolve->audit->export. mk34_reconciliation/observer/planner/executor/
  invariants/replay.py. .mk34_audit.jsonl. Archive walkman_sdd_MK20.docx.
- Part IV MK35: trust lattice + quorum verification, capability-locked execution
  (5-min TTL tokens, _CAPABILITY_REGISTRY RLock), pure replay vs persisted
  bundle, declarative policy DSL (PolicyDocument.from_json, STRATEGY_HANDLERS).
  Archive walkman_sdd_MK21.docx.
- Part V MK36: trust provenance graph (correlated-failure domains — 3 strategies
  through same ADB daemon != independent), SQLite durable capability store
  (multi-process), runtime fingerprint in every bundle, 4-event transfer
  lifecycle (PRE_TRANSFER/TRANSFER_COMMITTED...), content-addressable snapshot
  lineage. Archive walkman_sdd_MK22.docx. spotifytuner_graph.json.
- Part VI MK37-39.1: SDD_MK37_MK37_1_MK37_2.md is authoritative. MK37 consolidated
  MK15/34/35/36 infra into mk37_wiring.py single integration point. MK37.1 4
  governance risks. MK37.2 capability-TTL endurance defect (9h45m run). Genre
  contamination incident ("everything became Afrobeats", LOG_17) -> MK38.0
  emergency freeze of ALL inference-sourced semantic writes at call-site
  (mk38_0_semantic_write_barrier.py, is_frozen() defaults True, 3-way-AND unlock).
  MK38.1 SHA-256 quorum fix. MK39 mediator + mutagen-boundary monkey-patch gate
  (hard-fails unmediated tag writes) + user-canonical source path (filenames =
  human intent, recognized without reopening inference vector) + Phase I/II/III
  containment. MK39.1 L4: 320/320 regression tests.
- Part VII MK11/MK12 (2026-07-12->17): Council of Three. Task1 MTP deep-clean
  hang (Explorer/WPD COM host poisoning, not .Items() algo; preflight.ensure_run_
  once never called from deep-clean; adb_delete.py). Task2 full-sync ran
  reorganize 3x/run -> once. Task3 genre graph _build_graph never built (empty
  Rev1 cohort skip) + MK38.0 stays frozen (preview-genre-consolidation CLI
  instead). Task4 wpd_transfer.py WPD scan indefinite hang -> subprocess
  isolation + OS kill + quiet_timeout_s + max_duration_s dual guard. Task4b
  WPDBusEnum (was "WpdMtp" — reset never worked ever). Task5 overnight
  resilience. Task6 localsend_transfer.py (WiFi, internal-only, send-then-adb-mv;
  LOCALSEND_ENABLED=False). Tasks8-14: LRCLIB lyrics, storage-aware FLAC/MP3
  fallback, ContentProvider write CONFIRMED DEAD (non-exported, SecurityException),
  AcoustID wiring, TuneMyMusic CSV, ListenBrainz discovery, offline telemetry
  (blocked same wall as Task10). SDD_MK12.md.
- Part VII cont Tasks 15-29: Task15 opt-in COMTransport + COM free-space; Task16
  ADB-over-WiFi (192.168.68.63:5555); Task17 adb diag tooling; Task18 pnputil
  driver-removal ADB fix (later NOT reliable) + SystemExit-vs-Exception crash;
  Task19 SD 0-files silent-skip; Task20 sync_state bug in isolated COM worker +
  VPN breaks LocalSend discovery; Task22 ADB flakiness ROOT CAUSE = physical USB
  I/O error, fixed by cable+port swap (ADB_TRACE=all); Task23 rename_file_on_
  device hang; Task25 _adb_cmd `sh -c` calling pattern silently broke device
  enumeration project-wide; Task26 INCIDENT audit --dry-run made 103 real writes
  (dry_run not respected at depth); Task27 fix + 2nd incident (Backtrack Reorg
  1,150 moves); Task28 7.76h clean re-verify (33,033 [DRY-RUN] lines, 0 writes).
- Part IX MK13 + Tasks 30-40: Task30 real-run crash (shared tmp_dir rmtree) +
  87 MF DOOM canonical-move dupes (folder-only compare, not filename). Task31
  tiered plan. Task33 port playlists+completeness (legacy-only) into Spiral.
  Task34 device cleanup then BLOCKED by peer rooting session; INTERNAL STORAGE
  WIPED by peer's fastboot oem unlock x2 (userdata wiped, bootloader stayed
  locked); genre corruption 1,797 SD files by 8h audit (starvation fallback ->
  library-dominant Afrobeats/Amapiano; Adele "19" bled). Post-wipe recovery
  (SD-only): genre bleed gate, album-completeness a/b/c into Spiral, playlists
  M3U8. MK13 5 bodies of work: (1) Playlists via .m3u8 import (write_text_file_
  to_device folder-not-file bug since 2026-04-30) + Favorites via
  walkman_favplaylist_apply.py UI-tap (star is a TOGGLE; wrong-artist bug: Steve
  Lacy "Only If" -> Janet "If"). (2) Audit incident: _consolidate_duplicate_
  folders no pin, 900 files orphaned + Music/Music nested tree; 4-site pin fix.
  (3) 8 tools: desplinter/deluxe_merge/fav_upgrade/cover_fill/year_backfill/
  storage_rebalance/incompleteness_scan/catalog_fill_scoped. (4) fill tools.
  (5) curate spiral mode + CURATE LIBRARY button. Findings: state keyed to path
  needs pin at EVERY move site; favorites survive DB rebuild (content-matched),
  playlists don't; exact-normalized title match wrong 15-30% one-directionally.
- Part X curation hardening: safe-overwrite adb (.__ovwtmp sidecar+verify+swap);
  walkman_album_normalize.py rebuilt (canonical-Deezer-anchored, --fix-tags
  rewrites TRCK which player orders by, --exhaustive drops filename pre-filter);
  duration_verify stage. Part XI src/ reorg (flat 488-item -> git repo, run.py
  launcher, config.BASE_DIR=STATE_DIR). Part XII feature-gap register (this
  session — SDD_MK13 §10 / README Part XII).

## Device naming
Docs variously say NW-ZX500 / NW-ZX507 / ZX500Series. adb getprop ro.product.model
= "NW-ZX500Series" (2026-09-09). Android 9, toybox (NO unzip), locked bootloader,
non-rooted, com.sony.walkman.highresmediaplayer with non-exported PlayerMediaProvider.

---

## 2026-10-03 — Task 41 planning: Spotify link-paste sync, architecture survey

Read-only survey for the "paste a Spotify link -> sync just that scope" feature plan
(full writeup in task_plan.md Task 41). Key facts confirmed against live src/:

- `downloader.run_downloads(track_map, cancelled, liked_album_ids=set(), sp_client=None)`
  is the established universal entry point for "download+tag+transfer a specific track
  list" -- `catalog_fill_download.py` and `catalog_fill_scoped.py` both call it with
  `track_map = {"__key__": tracks}` / `cancelled = [False]`. This is the reuse target,
  not a new pipeline.
- Album-wise vs per-track download is decided by `ALBUM_DOWNLOAD_THRESHOLD_MISSING`
  (default 3, `downloader.py:557`) compared against how many of an album's tracks are
  present in the track_map passed in (`downloader.py:3598-3611`). This means: pass 1
  track -> stays per-track; pass a whole album's tracks -> triggers the existing
  `_download_full_album` whole-album TIDAL/monochrome path (`downloader.py:3089`) for
  free, no new branching needed.
- `_tag_file` (`downloader.py:1352`) writes title/artist/album/album_artist/track#/
  disc#/year immediately at download time. `_embed_cover_for_track`
  (`downloader.py:94`) embeds cover art immediately too, sourced from
  `track["album_image"]`, which `spotify_client._parse_track` (`spotify_client.py:245`)
  already populates from Spotify's own CDN image URL. Only `track["genre"]` is never
  set by any existing scoped-download caller -- genre resolution lives exclusively
  inside `audit.LibraryAuditor.run()`'s huge per-file loop (`audit.py` ~1400-2600,
  calling `genre_client`'s private `_lastfm`/`_deezer`/`_musicbrainz`/`_discogs`/AI
  waterfall functions directly) -- there is no clean, already-extracted
  `resolve_genre(artist, title)` function to call standalone today.
- `spotify_client.get_playlist_tracks(sp, playlist_id)` (`spotify_client.py:199`)
  already works for ANY accessible playlist regardless of ownership (uses
  `sp.playlist_items`, no extra scope) -- reusable as-is for a pasted public-playlist
  link. No `get_track`/`get_album_tracks` helpers exist yet; those are the only new
  Spotify-fetch code the feature needs (thin `sp.track()`/`sp.album()`+
  `sp.album_tracks()` wrappers feeding the existing `_parse_track()` dict shape).
- The M3U8-writing path for a playlist link can reuse `walkman_playlist_import.
  build_m3u8()` + `adb_transfer.write_text_file_to_device()` verbatim, plus the
  (artist,title)-normalized device-matching helper already written inline in
  `cli_runner.py`'s `build-favorites-playlists-plan` mode (`cli_runner.py:661-677`,
  function `_resolve()`) against `walkman_dedup._bulk_device_scan()`. Should be lifted
  into a small shared helper rather than duplicated.
- `cli_runner.py`'s generic `run-tool <script.py> [args]` delegator +
  `_RUN_TOOL_OK` whitelist (`cli_runner.py:109-126`) is the existing mechanism for
  adding a new standalone script to the GUI without touching the MODES tuple -- exactly
  the wiring point for a new `walkman_link_sync.py`.
- GUI already has `tk.Entry`/`StringVar` free-text-input precedent (ADB shell popup,
  `main.py:2599-2605`; two search bars) and the `_tool_btn` helper (`main.py:774`) for
  wiring a new button into the Tools panel with the same threading.Thread + CLI-viewer
  streaming every other tool button uses.
- This feature needs a LIVE Spotify OAuth client regardless of `config.OFFLINE_MODE`
  (no offline-export data exists for a link the user hasn't already synced).

No code written this session -- planning only, per user request.

---

## 2026-10-04 -- Task 41 sandbox experiment: no-developer-API fetch methods, head-to-head

User asked to prototype, in an isolated sandbox only (nothing in this repo touched --
all files live under the session scratchpad dir
`link_sync_sandbox/{spotify_link.py,methods.py,compare.py}`), assuming the Spotify
Developer API is NOT available, and to test multiple fetch routes against each other:
web scraping vs. an AI inquiry. Ran all methods against 4 real public links (a track,
an album, a small playlist, a >100-track playlist) from this sandbox's network.

### Method A -- oEmbed (`open.spotify.com/oembed?url=...`)
Official, unauthenticated, zero risk. Returns only `{title, thumbnail_url}`. Confirmed
working for track/album/playlist alike. Not sufficient alone -- no artist field
separated from title, no album, no tracklist, no duration.

### Method B -- `/embed/{track|album|playlist}/{id}` page's server-rendered
`__NEXT_DATA__` JSON (no token, no login, no cookies needed)
This is the real find: unlike the normal `open.spotify.com/track/...` page (pure
client-shell, nothing server-rendered -- confirmed empty `<meta>`/`<title>`), the
`/embed/` variant server-renders a full JSON blob with structured data:
- **track**: title, artists[] (name+uri), releaseDate, duration, explicit flag,
  3 cover-art sizes. CONFIRMED GAP: no album name/id anywhere on a bare track's
  embed entity (`relatedEntityUri` points at the artist, not the album).
- **album**: name, subtitle (=album artist string), releaseDate (CONFIRMED often
  `null` even for a real, old, well-known album -- Scorpion returned
  `"releaseDate": null`, a real asymmetry vs. the official Web API which always has
  this), full `trackList[]` (confirmed exactly matches real length, e.g. 25/25 for
  Scorpion) but each item only has `{title, subtitle(=artist string), uri, duration}`
  -- no explicit track_number/disc_number field (order = list order; multi-disc
  albums have no disc marker at all) and no per-track album-art/ISRC.
- **playlist**: name, full `trackList[]` up to a CONFIRMED HARD CAP of exactly 100
  items with NO pagination support -- tried `?offset=100`, `?offset=100&limit=100`,
  `?page=2`, all three returned the identical first-100 (same first/last track),
  and no `nextOffset`/`totalCount`/`hasNext` field exists anywhere in the payload to
  even detect truncation programmatically. "Peaceful Piano" (real-world playlist,
  >100 tracks) silently truncates at 100 via this method. Also CONFIRMED: playlist
  track items carry NO album field at all (only the artist subtitle) -- a naive
  reuse of that subtitle as "album" is actively wrong (e.g. Taylor Swift's "Patient
  Zero" got mislabeled "Taylor Swift" as its album in the first prototype pass).
- Fragility/ToS note: this is unofficial scraping of a page not meant for
  programmatic structured-data extraction; the `__NEXT_DATA__` shape can change
  without notice (unlike a versioned public API contract) and depending on jurisdiction
  this may run against Spotify's ToS even though technically unauthenticated.

### Method C -- AI inquiry (reusing the real project's own `ai_helper.call_ai()`
7-provider waterfall, read-only import, no repo changes)
Confirmed NON-FUNCTIONAL to mostly-useless in this sandbox:
- Groq (first 5 of 7 providers in the waterfall) returned `HTTP 403: Access denied.
  Please check your network settings` on EVERY call, including the connectivity
  self-test -- this sandbox's network blocks groq.com outright (separately, memory
  already notes Groq+gemini-2.0-flash models were decommissioned by their providers,
  compounding the problem).
- First full run (tracklist-listing prompt): ALL 7 providers in the waterfall
  returned nothing -- `call_ai()` returned `None`.
- Second run (single-fact prompt, "what album is this track on"): got a response,
  but it was `{"album": "�"}` -- a single mojibake/garbage character, unusable.
- Even disregarding this sandbox's specific network restrictions, the structural
  problem stands: an LLM has no live view of a playlist's actual current contents
  and can only "recall" training-data knowledge of very famous, static releases --
  not suitable as a primary metadata source for an exact tracklist. Not recommended
  as a data source at all; at most a disambiguation aid, and even that is unproven here.

### Method D (discovered during testing, not in the original 2-method framing) --
Deezer's free, keyless public search/album API (`api.deezer.com`)
Already a trusted data source elsewhere in THIS codebase (`genre_client._deezer()`,
`walkman_album_normalize.py`'s "canonical Deezer tracklist" fetch) -- reused here to
backfill Method B's one real gap (album name for a bare track link, and for every
playlist-track item). Confirmed working: freeform `q=artist title` search (the
`artist:"X" track:"Y"` advanced-syntax form returned 0 results against this API in
testing -- freeform works, advanced syntax didn't), then `/album/{id}` gives
`record_type` ("single" vs "album") + `nb_tracks` -- a clean, reusable signal to
reject singles/remixes in favor of the real studio album, confirmed concretely
(Deezer's top hit for "Shape of You" was the single, `record_type="single"`,
`nb_tracks=1` -- the real album is "÷ (Divide)"). CAVEAT found live in this session's
own prototype: a naive title-regex-only filter (reusing just the
`catalog_fill_scoped._BAD_ALBUM`-style keyword list) is NOT enough to catch a
same-named single -- the filter must actually check `record_type`/`nb_tracks`, not
just the title text. The real implementation must use the structured field, not
copy the regex-only heuristic uncritically.

### Net comparison
No single method is sufficient alone. B is the strongest primary source (real
structured data, no auth) but has three concrete gaps: no album on bare tracks, no
album on playlist items, and a hard 100-track playlist cap with no detection signal
beyond "trackList length is suspiciously exactly 100." D closes the album gap (at the
cost of one extra HTTP round-trip per track needing it) using a source this codebase
already trusts. A is a reasonable byte-cheap pre-check but insufficient alone. C is
not viable as a metadata source here, both empirically (network-blocked + garbage
output) and structurally (no live knowledge of actual current playlist contents).
The live repo (confirmed in the 2026-10-03 Task 41 investigation) already has real
Spotify Developer credentials configured -- this entire B+D sandbox pipeline is a
fallback/contingency path, not a replacement for the spotipy route, and should be
tried second, not first, in the real implementation.

Sandbox files (NOT part of the real src/ tree, nothing committed, nothing in the
live repo modified): `<scratchpad>/link_sync_sandbox/spotify_link.py`,
`methods.py`, `compare.py`.

---

## 2026-10-04 -- Task 41 build: Spotify Web API returning 403 for this app
(live, external, not a code bug)

While dry-run testing the newly-built `walkman_link_sync.py` against 3 real
links (a track, an album, a playlist), EVERY spotipy call
(`sp.track`, `sp.album`, `sp.playlist`) returned the same error:

    403: Active premium subscription required for the owner of the app.
    When the subscription status changes, it can take a few hours before
    requests are allowed again.

This is Spotify's own API responding this way for this app's registered
credentials -- not an exception in our code, not a parsing bug. It means the
registered developer app's OWNER account currently needs an active Premium
subscription for these specific endpoints (`GET /v1/tracks/{id}`,
`GET /v1/albums/{id}`, `GET /v1/playlists/{id}`) to work at all, independent
of whatever scope/auth the end user granted.

Net effect: `walkman_link_sync.py`'s automatic primary-then-fallback design
(try spotipy, catch ANY exception, fall back to `spotify_scrape_fallback.
resolve_link()`) absorbed this cleanly -- every dry-run still completed
correctly via the fallback. But this means the fallback built in Task 43,
originally framed as a contingency for "if we don't have the developer API,"
is currently the ONLY working path for this feature, live, today. Not
re-tested: whether the REST of this app's existing spotipy usage (the bulk
`fetch_and_build()` / `current_user_playlists` / `current_user_saved_tracks`
path used by the main sync pipeline) hits the same 403 -- those are different
endpoints and this session did not check them. If this turns out to be
affecting the main pipeline too, that is a separate, higher-priority
investigation outside this feature's scope -- flagged here for visibility,
not chased further this session.

## 2026-10-04 -- Task 41 pre-build review: two real bugs caught before they shipped

User asked explicitly to confirm playlist labeling and address any
unaddressed issues from the planning docs before building Task 41. Two real
(not hypothetical) bugs were found and fixed in this pass:

1. **Blank-album folder-name bug.** `downloader.py` builds new album folder
   names as a bare `f"{artist} - {album}"` with no guard for `album == ""`.
   `spotify_scrape_fallback.resolve_link()` can legitimately return
   `album == ""` whenever its Deezer backfill finds no confident match --
   confirmed common in real testing (72-80% of tracks on two of three real
   playlists tested). Left unfixed, this would have produced broken
   `"Artist - "` device folders for a large fraction of fallback-resolved
   playlist tracks. Fixed in `walkman_link_sync.sync_link()`: default
   `album` to the track's own title when blank, before building
   `track_map`. spotipy's primary path never produces a blank album, so this
   guard is a no-op whenever the primary path succeeds.
2. **Device-matcher regression caught by its own test.** While porting
   `cli_runner.py`'s build-favorites-playlists-plan matching logic into
   `walkman_link_sync._resolve_to_device()` (kept as a local copy rather than
   an import, since the original is a private inline closure inside a much
   bigger function), the leading-track-number strip
   (`re.sub(r"^\d{1,3}\s+", "", bits[-1])`) was dropped. The new unit test
   `test_resolve_to_device_matches_by_normalized_artist_title` failed
   immediately against a realistically-named file
   (`"Artist - Song - 01 Song.mp3"`), catching the regression before any real
   run could silently fail to match tracks already on the device. Fixed.

Both are logged in task_plan.md's Task 41 section with full detail.

---

## 2026-10-12 -- sandbox review: does "the audit line" actually clean up
dupes/cross-format, mislabels/missing labels, incomplete albums/playlists,
out-of-order tracks? (3 parallel read-only reviews, sandbox copy, no edits)

User asked to copy the current codebase into a sandbox and review whether
the audit pipeline handles 4 things. Reviewed via 3 parallel read-only
passes against a full sandbox copy (`<scratchpad>/audit_review_sandbox/`,
src+tests+docs+planning files, no state/logs/data/archive). Headline,
true across all three: **the "audit" spiral mode (the GUI's
"🔍 AUDIT ONLY" button) does almost none of the four things asked about.**
Its `_STAGE_MATRIX` entry is `{"tag", "audit", "covers", "year_backfill"}`
-- genre/year/cover-art backfill only. Everything else below lives in
`"curate"` (🧹 CURATE LIBRARY) or `"full"` only.

### 1. Duplicates, including same song as different file types -- YES,
handled, but not in "audit" mode
`walkman_dedup.py`'s identity key (`_track_keys()`, ~line 566-599) is built
from normalized (artist, album, title) text ONLY -- file extension is
never part of the key, so an MP3 rip and a FLAC rip of the same song
correctly collapse into one duplicate set rather than being treated as
unrelated files. Keep-priority: `FORMAT_RANK = {".flac":5, ".wav":4,
".m4a":3, ".aac":3, ".mp3":2, ".ogg":1}` (walkman_dedup.py:2447), ties
broken by longer duration. `walkman_album_normalize.py` runs an
independent, overlapping (not conflicting) cross-format pass per album
against a canonical Deezer tracklist, using the identical priority order
in its own words ("protected > FLAC > MP3 > M4A, then larger").
Delete mode is NOT uniform by design: `walkman_dedup.prune_before_sync()`
is hard-delete (pre-sync, frees space), `prune_during_audit()` is
soft-delete (`.deleted/`, reviewable); `walkman_album_normalize.py` is
hard-delete only.
**Real inconsistency found**: `prune_before_sync()`'s hard-delete
explicitly skips a metadata-completeness comparison with a comment
saying "audit will catch those later" -- but per the `_STAGE_MATRIX`
below, plain `"audit"` mode never runs any dedup stage at all, so that
comment is stale/misleading; only `curate`/`full`'s `dedup_soft` actually
would catch it.
**Minor tech-debt**: two independent `FORMAT_RANK`-equivalent tables
(`walkman_dedup.py` and `walkman_album_normalize.py`) with identical
values today but no shared constant -- a future edit to one could
silently diverge from the other.

### 2. Mislabeled tracks + missing labels -- split behavior, confirmed
**Missing labels (blank genre/year) ARE automatic** in any mode that
includes the `tag`/`audit` stage, including plain `"audit"` -- this is
the main audit loop's core job.
**Mislabeling (wrong song under right-looking tags) is a COMPLETELY
SEPARATE pass**, not part of the main tag loop at all. `audit.py`'s
per-file loop (`_expected_tags()`/`_build_diff()`) derives expected tags
from filename/folder/metadata lookups and never checks the actual audio
against them. AcoustID fingerprint verification exists
(`_apply_acoustid_fallback`) but is opt-in (`config.
ACOUSTID_AUTO_CORRECT_ENABLED`, default `False`) and only fills BLANK
identity fields -- it never re-verifies an already-populated (possibly
wrong) tag. Real mislabel detection is `duration_verify.py` (duration-
ratio vs reference) -> `duration_verify_categorize.py` (buckets:
OVERSIZED / HIGH-CONFIDENCE WRONG TRACK / ALTERNATE VERSION / POSSIBLE
TRIM/SEGUE / REVIEW -- only the first two are "actionable" by default)
-> `mislabel_redownload.py` (actually deletes the bad file and re-feeds
it through `downloader.run_downloads()` for a real replacement, not just
a flag -- favorite/playlist-preserving). This full chain IS wired
end-to-end automatically, with zero manual steps between stages -- but
ONLY inside `curate`/`full`. `"audit"` mode doesn't run any part of it.

### 3. Incomplete albums & playlists -- automatic, but split across two
systems, neither in "audit"
- `album_completeness.run_completeness_pass()`: triggers per-album at
  >=3 liked tracks (`ALBUM_COMPLETE_MIN_LIKED`) or any saved album by
  that artist, capped at 200 new downloads/run, calls
  `downloader.run_downloads()` directly (a real fill, not a report).
  `_STAGE_MATRIX`: only `"download"` and `"full"`.
- `catalog_scoped` (followed-artist deep-catalog fill,
  `catalog_fill_scoped.py`): separate trigger (followed, not just
  liked/saved), capped at `--max-queue 1500`. `_STAGE_MATRIX`: only
  `"curate"` and `"full"`.
- Playlists: `favsync` stage regenerates M3U8s -- `_STAGE_MATRIX`: only
  `"curate"`/`"full"`, and still needs the same manual device "Update
  database" rescan afterward (unchanged, matches prior memory).
- Legacy listening-history "smart playlists" confirmed still dead/
  unported -- zero references in `spiral_orchestrator.py` (re-confirmed
  current, not stale from old memory). `streaming_history.py` itself
  isn't fully dead, though -- `audit.py`/`downloader.py` still use it for
  download-priority *scoring*, just not playlist generation.

### 4. Out-of-order albums/playlists -- fixed, but only in curate/full,
and correctly uses the thorough mode
`walkman_album_normalize.py` is invoked with `--exhaustive`
(spiral_orchestrator.py ~368), which per its own code comment is the mode
that catches "scrambled TRCK tags" even on albums with CLEAN filenames --
the harder case `--fix-tags` alone would miss (confirmed this matters:
this is literally the "IGOR still out of order" class of bug from an
earlier session). Gated on `"album_normalize" in self._stages`, which
`_STAGE_MATRIX` only sets for `"curate"`/`"full"`. Not in `"audit"`.

### The _STAGE_MATRIX, as it stands right now (all three reviews agree):
```python
"full":       {dedup_hard, dedup_soft, completeness, download, transfer, tag,
               audit, reorganize, catalog_scoped, duration_verify, mislabel_fix,
               desplinter, deluxe_merge, fav_upgrade, covers, year_backfill,
               storage_rebalance, album_normalize, favsync}
"download":   {completeness, download, transfer, tag, covers, year_backfill}
"audit":      {tag, audit, covers, year_backfill}
"reorganize": {dedup_soft, reorganize, audit, covers, year_backfill, storage_rebalance}
"curate":     {catalog_scoped, duration_verify, mislabel_fix, desplinter,
               deluxe_merge, dedup_soft, fav_upgrade, covers, year_backfill,
               storage_rebalance, album_normalize, favsync}
```

### Net conclusion
Everything the user asked about DOES exist and IS automatic (none of it
is "just a report" -- every stage calls real download/delete/retag
functions) -- but **"audit" alone does only 1 of the 4** (missing-label
backfill). The other 3 (dedup/cross-format, mislabel-fix, completeness,
track-order) all require `"curate"` (🧹 CURATE LIBRARY) or `"full"`. If
the user's mental model was "running audit cleans all of this up," that's
the one correction worth acting on -- either always reach for CURATE
LIBRARY for a real cleanup pass, or (a separate, scoped decision, not
done here) extend `"audit"`'s own `_STAGE_MATRIX` entry if a lighter
mode that still does more than tag/genre is wanted.

No code changed this session -- read-only review, sandbox only, as asked.

---

## 2026-10-13 -- sandbox: 2 real dry-runs on the live device, standard "audit"
vs. "curate" -- proving the review's findings empirically, not just by
code-reading

Both run from the sandbox's `src/cli_runner.py` (not the real repo) against
the real, connected Walkman. Run sequentially, back-to-back, in one
background job -- NOT in parallel, deliberately: this codebase has repeated
documented history of concurrent-device-I/O corruption (`AUDIT_WORKERS=1`
"MTP is serial", `_MTP_DEDUP_LOCK`), and both runs would contend for the
same single USB/MTP link regardless, so parallel execution had no speed
upside and only downside. Safety of `curate --dry-run` against the real
device was verified BEFORE running it, not assumed: traced every `_run_tool`
call site curate's stage set touches and confirmed each underlying script
(`walkman_desplinter.py`, `walkman_deluxe_merge.py`, `walkman_fav_upgrade.py`,
`walkman_storage_rebalance.py`, `walkman_album_normalize.py`) defaults to
safe/non-destructive behavior with no flags (`execute = "--execute" in
sys.argv`, default False, or `argparse` `store_true` default False) --
matches the documented fix for the prior real "audit --dry-run made 100+
real writes" incident, now independently re-verified for curate's own stage
set specifically.

Both runs were killed partway by the harness's background-process low-
memory reaper (system-wide memory pressure, unrelated to either command;
confirmed not a bug in either run) -- `audit --dry-run` completed in full
before this happened on its own attempt; `curate --dry-run` got through
most of its stages with real, concrete findings before being killed during
the post-rebalance second covers pass (never reached album_normalize or
favsync this run).

### Dry Run 1 -- `audit --dry-run` (the GUI's "🔍 AUDIT ONLY" button), COMPLETE
Elapsed 1312s (~22 min). Cohorts empty (nothing new to tag/genre-resolve).
Found: 400 art-less files across 171 albums; 938 year-less files across 473
albums. **That's it.** Zero mention of duplicates, mislabeled tracks,
incomplete albums, playlists, or track order -- confirms the code-review
finding empirically, on the live device, not just by reading
`_STAGE_MATRIX`.

### Dry Run 2 -- `curate --dry-run`, PARTIAL (killed mid-run, not finished)
Real findings surfaced that "audit" never would have:
- **Desplinter: 381 split album groups, 716 planned moves, 133 cross-
  storage skipped.** This alone is a huge, concrete "incomplete/splintered
  library" signal invisible to audit mode.
- **Deluxe/base merge: 7 folder groups to merge** (e.g. Don Toliver "Love
  Sick" base+Deluxe+stray-single all merging into one canonical folder,
  16-track canonical tracklist confirmed via Deezer), one of which
  (Naza "Gros bébé") surfaced a real cross-edition duplicate on "Loin de
  moi" -- notably the SAME track that this session's earlier real playlist
  sync separately caught via the duration-mismatch guard as a suspected
  mislabel. Two independent mechanisms converging on the same real problem
  track is a good cross-check.
- **Metadata-completeness dedup pass (Pass 3): planned=0** -- nothing
  extra found beyond what the scans above already surfaced, for this
  library's current state.
- **Device-wide dedup scan: 16 inferior-format duplicate candidates**,
  plus several `TRACK_NUMBER_CONFLICT` deferrals (same normalized title,
  conflicting track numbers across copies -- e.g. three different "time"-
  titled files with track numbers 03/08/05/16) correctly deferred to
  report-only rather than auto-resolved, per the existing B2.1 safety guard.
- **Artist-completeness report: "0 followed artist(s) with any engagement
  signal (of 255 total followed)."** Caveat, not a clean bill of health --
  this means the engagement-signal input data (streaming history) isn't
  populated in a way the report could use here, so the resulting "0
  actionable gaps" likely UNDER-reports real completeness gaps rather than
  confirming none exist.
- **duration_verify / mislabel_redownload: 0 pending.** Same caveat, more
  pointed: the orchestrator deliberately SKIPS the real (hours-long) sweep
  on a dry run ("duration_verify_report.json" doesn't even exist) and just
  re-categorizes whatever report already existed -- which was none. "0"
  here means "never actually checked this run," not "confirmed clean."
- **Real bug caught live: `walkman_fav_upgrade.py` failed (exit 1).** The
  preceding step ("Dedup scan (for fav-upgrade)") writes `.dedup_log.json`,
  but `walkman_fav_upgrade.py` looks for a DIFFERENT filename,
  `.dedup_plan.json`, which is never produced by that step -- a real
  filename-contract mismatch between two adjacent pipeline stages, not a
  theoretical gap. Non-fatal (orchestrator logs it as a WARNING and
  continues), but the fav/playlist-protected dedup upgrade pass silently
  never ran as a result. Not fixed this session -- flagged for the user.
- **Not reached before the kill**: `album_normalize --exhaustive` (the
  out-of-order-track fix) and `favsync` (playlist regen) -- so this
  particular run has no result yet for 2 of the 4 original concern areas.
  A finished run would have them; this one just didn't get there.

### Net
Dry Run 1 vs Dry Run 2, on the SAME real library, same session: audit
found 2 categories of gap (art, year); curate found those same categories'
stages weren't even run PLUS 381 splintered albums, 7 deluxe/base merges
(with a real cross-referenced duplicate), 16 format-dupes, several
track-number conflicts, and one real pipeline wiring bug -- while still not
finishing. This is the empirical version of the earlier code-review's
conclusion, not just a restatement of it.

---

## 2026-10-13 cont. -- fixed fav_upgrade for real + extended audit mode for
real, both LIVE-VERIFIED on the device (3 attempts; device connectivity
was the blocker, not the code)

User: fix the fav_upgrade bug, make audit mode genuinely address all 4
concern areas, flag anything else affecting library quality, then finish
the dry-run verification.

### Fix 1 -- `.dedup_plan.json` written to the wrong directory
Root cause was deeper than a filename mismatch: `_write_dedup_plan_json()`
(walkman_dedup.py) wrote to `Path("./.dedup_plan.json")` -- relative to
the SUBPROCESS'S CWD (`<repo>/src/`, per `_run_tool`'s `cwd=config.
SRC_DIR`), never `<repo>/state/` where `walkman_fav_upgrade.py`'s `ROOT`
actually looks. The file was always being written, silently (wrapped in a
swallow-all try/except logged only at debug level) -- just to the wrong
place. Fixed to use `config.BASE_DIR`, matching the sibling `journal_path`
two lines above in the same `__init__`. **LIVE-CONFIRMED working** in the
very first verification attempt: the `DEDUP_PLAN.exists()` check that used
to fail no longer did.

### Fix 2 -- a SECOND, different fav_upgrade bug, found only because Fix 1
got far enough to expose it
`walkman_fav_upgrade.py` also reads `favorites_playlists_plan.json`
(`PLAN`) completely unconditionally, no `.exists()` guard -- and that file
is only (re)built by the `favsync` block, which runs LATER in the same
stage sequence. A real, pre-existing ordering gap (same bug already
existed in `curate` mode's stage order; never surfaced/noticed before this
session's verification). Fixed two ways: (1) `spiral_orchestrator.py` --
added a second prerequisite call ("Rebuild favorites/playlists plan (for
fav-upgrade)") right before fav_upgrade runs, same "build what I need
right before I need it" pattern already used one line above for the
dedup-scan prerequisite. Deliberately did NOT move the LATER favsync block
itself -- it correctly runs last so its M3U8s reflect post-reorganize
paths; fav_upgrade only needs to know WHICH files are favorited, not their
final paths, so an earlier, pre-reorganize plan is fine for its narrower
purpose. (2) `walkman_fav_upgrade.py` -- added a `PLAN.exists()` guard
mirroring its own existing `DEDUP_PLAN.exists()` pattern, so a missing
prerequisite (from any cause) fails gracefully instead of a raw traceback.
**LIVE-CONFIRMED working** on the third verification attempt: "Upgrade
favorited/playlisted dupes: exit 0".

### Fix 3 -- audit mode extended to cover all 4 concern areas
`_STAGE_MATRIX["audit"]` extended from `{tag, audit, covers,
year_backfill}` to that union curate's full stage set minus
`storage_rebalance` (a capacity concern, not a quality one). Verified no
other `self.mode ==` gating exists elsewhere that would block this
(grepped, zero hits -- `_STAGE_MATRIX` membership is the only gate). REAL,
DELIBERATE CONSEQUENCE: a real (non-dry) "AUDIT ONLY" run is no longer a
quick ~20min pass -- it now takes as long as curate (hours), since it
inherits the real duration_verify sweep + the exhaustive album-order
check. "download" mode remains the fast genre/cover/year-only option.

### Verification took 3 attempts -- all 3 device-connectivity issues, zero
code issues, each one different and each handled gracefully by EXISTING
pipeline safety logic, not by anything added this round:
1. Canary (Rev 0) correctly ABORTED a run before touching the library when
   a real file pulled 0 bytes despite `adb devices` reporting connected --
   exactly what the canary exists to catch.
2. A later attempt hit `subprocess.TimeoutExpired` on the artist-
   completeness bulk `ls -lR` scan (120s timeout) -- logged, non-fatal,
   pipeline continued using whatever partial data existed (meaning that
   specific stage's "0 actionable gaps" result from that run should not
   be trusted as a clean bill of health).
3. The same run later had the device drop again mid-pipeline (confirmed:
   "ADB found but device NOT connected" mid-run) during the post-rebalance
   cleanup block -- desplinter correctly hard-aborted on a 0-file scan
   (`walkman_desplinter.py`'s own deliberate safety guard, not a bug);
   deluxe_merge and album_normalize degraded to harmless no-ops; the FINAL
   favsync plan-rebuild resolved 0/2270 favorites and 0/3214 playlist
   tracks as a result -- harmless since this is dry-run (nothing was
   pushed to the device), but that specific number from that run is not
   meaningful.
None of these three are code bugs -- they're the same documented USB/MTP
instability class this project has hit before (prior root cause: a
physical cable/port issue), now hit 4 times in one session. The 3rd
verification attempt, after the user reseated the connection, completed
clean enough to confirm both real fixes plus rich, genuine findings from
the healthy portions of the run.

### Real findings from the healthy portions of the final run (this is
what "AUDIT ONLY" surfaces now, that it never did before this session):
- **Dedup scan: 26 duplicate/corrupt candidates** (16 inferior-format +
  10 inferior-metadata -- the metadata-completeness pass, Pass 3, actually
  ran this time with a healthy connection and caught 10 identical
  "Instrumental" duplicate tracks within Pharrell Williams' "In My Mind
  (Deluxe Edition)" CD2, all collapsing onto one keeper).
- **Deluxe/base merge: 7 folder groups**, including the SAME real
  cross-edition duplicate on Naza "Loin de moi" caught again (now the
  third independent mechanism this session to flag this exact track, after
  the playlist-sync duration guard and the first partial curate run).
- **duration_verify / mislabel_fix: 0 pending** -- same caveat as before,
  the real sweep is deliberately skipped on dry-run, so this means "not
  checked this run," not "confirmed clean."
- **Artist-completeness: inconclusive this run** due to the ADB timeout
  above -- needs a clean re-run to trust the number.
- fav_upgrade, covers, year_backfill, first desplinter/deluxe_merge pass,
  and album_normalize's first pass all completed cleanly against the real,
  full 11,842-file library.

All 3 new/updated code fixes have their own regression tests (5 total
across `test_walkman_dedup_plan_json_path.py`,
`test_spiral_stage_matrix_audit.py`, `test_walkman_fav_upgrade_plan_guard.py`)
and were git-stash-compared against the real test suite with strictly
fewer failures than without. Nothing committed.

---

## 2026-10-04 -- sandbox: fixing the 100-track playlist cap + a retry-until-
converged loop (NOT yet in the real repo)

User: the French Afrobeats playlist has 246 real tracks but only 42 ended
up loaded on the Walkman -- "update our playlist route to consider
extremely large playlists," prototyped in the sandbox first (per the
established pattern this session). Also suggested: compare downloaded vs
needed and retry up to a bounded limit.

### Root-cause split (two DIFFERENT problems, confirmed separately)
1. **The 100-track embed-page cap** (already known) means tracks #101-246
   were never even seen by the fallback resolver -- not a download
   failure, an invisibility problem.
2. **Of the 100 tracks the fallback DID see, only 42 ended up matched on
   device** -- and the real run's own numbers rule out "downloads failed"
   as the explanation: `downloader.run_downloads()` reported
   `downloaded=139, failed=0`. Every attempted download succeeded. The
   gap is in MATCHING, not fetching -- most likely `_resolve_to_device()`'s
   folder-naming assumptions not matching how a given album-wise download
   actually laid files out (confirmed one concrete case this session: a
   title containing "/" produced a real nested `R2/7_7 _ 24_24/` folder
   structure, not the usual flat `Artist - Album/` pattern the matcher
   expects) -- the same class of blind spot Task 44 already exists to fix
   for `has_track()`, just showing up in `_resolve_to_device()` too.

### Fix 1 (sandbox-verified, real data) -- Deezer playlist mirror for the
100-track cap
Spotify's own API (`sp.playlist()` AND `sp.playlist_items()`, tested
directly) both hit the identical 403 the rest of this session has been
working around -- confirmed there is no cleaner primary-path pagination
available right now, dead end. Instead: search Deezer's keyless
`search/playlist` API for a playlist with the SAME name; several
decoys exist (an "L'Afrobeats Français 2026" playlist with 155 tracks, a
"4 Kampe..." playlist with 507 -- similarly-themed, NOT the same
playlist) -- so the fix VERIFIES before trusting: fetch the top
candidate's first 15 tracks and require real (artist,title) overlap
against our own Spotify-scraped tracks (>=60%) before using it. For the
real case: Deezer playlist 15657876443, exact name match, nb_tracks=246,
verified overlap=0.80 (one track had a differing primary-artist credit
between platforms, expected, not a red flag on its own). Deezer's
`playlist/{id}/tracks` endpoint paginates properly past 100 (confirmed by
fetching index=150 directly) and already includes album/cover/duration/
ISRC per track -- no extra per-track Deezer backfill call needed, unlike
the bare-track case. Prototype: `<scratchpad>/link_sync_sandbox/
large_playlist.py` -- real run recovered all 246 tracks.

### Fix 2 (sandbox-verified, control-flow only) -- sync-until-converged
retry loop
User's idea, evaluated honestly against the real run's numbers above:
a naive "retry failed downloads" loop would have retried NOTHING in the
French Afrobeats run, since 0 downloads were reported failed -- the gap
there is a matching problem, not a transient-failure problem, so a retry
loop is not a substitute for fixing the matcher. It still earns its place
as a defensive layer for the failure modes it DOES catch (a provider
timeout, a momentary ADB drop, the project's own documented history of
exactly these transient classes of failure). Prototyped the control flow
only (`<scratchpad>/link_sync_sandbox/retry_loop.py`) with 3 cases: everything
succeeds immediately (1 iteration), a track needs one retry to succeed
(2 iterations), and a track that's genuinely never available (stops at
the stall after iteration 2 instead of burning the full 5-iteration
budget retrying something that will never appear). All 3 assertions pass.

### Net recommendation
Both are worth building for real, but they solve different halves of the
problem and neither alone fully closes "42/246 loaded": the Deezer-mirror
fix gets ALL 246 tracks resolved and queued; the retry loop adds
resilience against transient failures during that queue's download; but
the actual 42/100 MATCH-rate gap found this session needs either Task
44's duration-aware has_track() or a more robust `_resolve_to_device()`
folder-matching heuristic (nested-path-aware) to really close -- flagging
this honestly rather than claiming the two sandbox fixes alone would have
produced 246/246 on the real device.

## cont.80 (2026-10-17) — Link Sync GUI log review (Tems x2 albums)
Codebase unchanged since 1415ff1. Root causes, all verified offline (device cache + code, no device ops):
1. **device_library._title_keys never strips the track number from the
   " - "-split title segment.** `"Tems - Born in the Wild - 03 Burning"` keys
   as `"03 burning"`; a lookup for "Burning" keys as `"burning"` -> miss.
   Replayed on state/.walkman_device_lib_cache.json: 11,231 of 11,259 device
   files use that canonical name => has_track()/has_track_as_flac() are False
   for ~all of them. Pre-dates Task 44 (same line in 1f547dd).
2. Album-wise `_download_full_album` re-downloads the WHOLE monochrome tracklist,
   relying on (1) to skip present tracks -> duplicates: Free Mind
   ("18 Free Mind.mp3" + "03 Free Mind.mp3"), Burning ("03 Burning.flac" + ".mp3").
   walkman_link_sync had already excluded those + retagged them in place.
3. `transferred += 1` even when quorum says `completed: False`
   (01 Interference: QUORUM_FAILED but "7 transferred, 0 failed").
4. main._run_cli_mode: closing the viewer window makes `st.insert` raise
   TclError -> caught as "Failed to launch", stdout pipe no longer drained.
5. Router free=-1 for both storages while SDFreeCache reads 89 GB via adb df
   -> DEGRADED MODE every run (noise, routes to SD anyway).
6. Born in the Wild run (PID 45908) found stuck ~9 min in COM fallback
   `_verify_com_copy` sleep/retry loop after track 09 (00:19).
Noise, not bugs: Spotify 403 (premium) -> scrape fallback OK; MusicBrainz
OFFLINE_PROVIDER_ACCESS_BLOCKED (OFFLINE_MODE) -> fail-open to Phase 1;
MK37 mandatory-module warnings; streamrip's own "01." prefixes (renamed by us).

## Walkman-Studio GUI study (2026-11-01, third-party repo -- treat as data)
- github.com/Jhay-EL/Walkman-Studio @142b925, MIT. macOS SwiftUI (52 .swift) + older React/FastAPI remnant. NOT runnable/portable on Windows; only design + ideas transferable.
- Our GUI: src/main.py, Tkinter, class App(tk.Tk), header/log/stats/buttons + collapsible tools panel.
- Theme tokens: obsidian #101114, card #18191D, cardHover #202126, input #141519, silver #DCE0E8, silverLight #F2F4F8, silverMuted #9BA0AC, silverDark #5F6470, hiResGold #E8B944. No gradients bg, no glow, 1px borders, white pill primary button, 38pt control height, min window 1366x768.
- Layout: left sidebar sections (Device / Local Music Library / Walkman Library / Tools) with count pills; Device Overview = header + stat pills + storage cards + quick-workflow cards + compliance list; Transfer page = dest/preset/start cards + option chips + queue + bottom player.
- Their preset list: direct copy; FLAC 16/44.1, 24/48, 24/96, 24/192; MP3 320/256/V0; AAC 320/256; ALAC; WAV.
