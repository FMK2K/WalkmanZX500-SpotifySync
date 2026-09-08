# SDD — MK11 / MK12: Operational Hardening Era

**System:** SpotifyTunerMK12 — Sony Walkman ZX-series sync platform (formerly SpotifyTunerMK11; same codebase, working directory relocated 2026-07-17, no architectural version bump implied)
**Document version:** 1.1 (2026-07-18)
**Builds on:** `SDD_MK37_MK37_1_MK37_2.md` (unchanged — remains authoritative for the MK37→MK39.1 semantic-integrity/mediator architecture; this document does not restate it)
**Iterations covered:** Task 1 (MTP deep-clean/genre/splintering/planner fixes), Task 2 (pipeline efficiency), Task 3 (genre graph accuracy + MK38.0 preview), Task 4 (WPD scan subprocess isolation), Task 5 (overnight full-device resilience), Task 6 (LocalSend WiFi transport, hardened, live-verification in progress), Task 7 (dry-run gating audit + app-refresh fix), Tasks 8-14 (native hardware-aware integration roadmap — synced lyrics, storage-aware format cascading, ContentProvider investigation, AcoustID wiring, CSV ingestion, ListenBrainz discovery, telemetry)
**Companion artifacts:**
- `task_plan.md`, `progress.md`, `findings.md` — this project's own planning-with-files record; the primary source for this document and more granular than what follows
- `README.md` Parts VI-VIII — narrative version of this same history, written for a human reader rather than a design reviewer
- `C:\Users\FeisalK\.claude\plans\given-the-current-state-temporal-quail.md` — the approved design plan for Tasks 8-14 (§9 below), kept as the detailed design reference

---

## 1. Executive Summary

Part VI of this document's companion README notes that by the close of MK39.1 L4, the platform's *semantic*-integrity problem was structurally contained: a mediator, a mutagen-boundary write gate, and the MK38.0 freeze made genre/metadata contamination architecturally difficult to reintroduce. What remained, largely untouched by that work, was a much less glamorous class of failure: **the transport layer underneath the application — MTP enumeration, the Windows Shell.Application COM host, ADB, and the physical SD card itself — kept failing in ways no amount of semantic-layer governance could see, because the failures happened one layer down.**

This document covers five days (2026-07-12 → 2026-07-17) of Council-of-Three review (Architect / QA Lead / Ops Engineer) followed by live, on-device iteration against that transport layer. Six tasks, in order: (1) a bundle of four user-reported bugs — MTP deep-clean hang, genre contamination, folder splintering, a planner crash — investigated against real source rather than the bug reports' assumed causes; (2) a pipeline-efficiency pass that found and removed one genuine 2/3-redundant device scan; (3) a genre-graph-inference correctness audit that found the graph was inert rather than mis-firing, plus a read-only investigation into the MK38.0 freeze that led to a dry-run preview feature, not an unlock; (4) discovery and fix of a real indefinite-hang bug in WPD scanning, followed by discovery of a second, structurally distinct live-lock bug in the same code path; (5) wiring previously-built-but-never-invoked recovery code into the actual failing call paths, plus discovery that a service-name typo had made a WPD reset silently non-functional since it was written; (6) adoption, in progress, of a WiFi-based transport (LocalSend) to route around USB-layer instability entirely for a subset of writes.

**Two structural findings recur across multiple tasks and are the most important takeaways of this era:**

1. **A thread-based timeout cannot reliably guard against every hang class.** Task 4's original WPD scan hang had an existing 180-second thread watchdog that never fired, because the blocked COM call holds the GIL and freezes the watchdog thread along with the enumeration it's supposed to be timing. Only a real OS process, killed from outside, reliably terminates a call that's stuck regardless of what it's stuck on. This is why the Task 4 fix moved the scan into a child process rather than tuning the existing thread-timeout value.
2. **Recovery code that exists but was never wired into the failing call path is operationally indistinguishable from recovery code that doesn't exist.** This pattern recurred at least three times independently: Task 1's `preflight.py` Explorer/WPD-cycle recovery existed but wasn't called from the deep-clean path; Task 5 found the CLI dispatch (`cli_runner.py`) never called `preflight.ensure_run_once()` at all, unlike the GUI path which always did; and, most strikingly, Task 5 found `preflight._cycle_wpd()`'s WPD-service reset had been targeting a Windows service literally named `"WpdMtp"`, which does not exist on this system — the real service is `WPDBusEnum`, and it was Stopped. **This reset has silently never worked, on any run, since the day it was written** — not because the reset logic was ever broken, but because it was resetting a service name that never existed. Any future transport-recovery work should treat "the code exists" and "the code is reachable from the path that actually fails" as two separate claims requiring two separate verifications.

**Status at the close of this document (2026-07-17):** Tasks 1-5 are implemented, code-reviewed, and — where a live device was available — confirmed working end-to-end. The one unresolved blocker is environmental, not architectural: the SD card itself is too full/slow to reliably enumerate its own Music folder, a condition that survived an Explorer restart, a physical unplug/replug, a `WPDBusEnum` service restart, and a full device reboot with freed internal storage. Task 6 (LocalSend) is in progress, gated off by default pending one more live confirmation.

---

## 2. Architecture Overview

Nothing in this era changes the MK37-39 mediation chain (`mk37_wiring.py`, the mutagen-boundary gate, MK38.0). All of it sits at or below the existing `mtp_transfer.py` transport-dispatch layer documented in README §9:

```
┌────────────────────────────────────────────────────────────────────┐
│  Production hot path (downloader, audit, walkman_reorganize,       │
│  walkman_sync, web_download, walkman_dedup)                        │
│    — unchanged, still routes all tag writes through the MK37-39    │
│      mediator + mutagen-boundary gate + MK38.0 freeze              │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  mtp.copy_file_to_device(...)
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  mtp_transfer.py — single transport choke point                    │
│                                                                      │
│   2026-07-17 (Task 6, config.LOCALSEND_ENABLED, default OFF)        │
│    ├─ 0. localsend_transfer.push_file_via_localsend()  ◄─ NEW       │
│    │     (Internal storage only; never raises; falls through)      │
│    │                                                                │
│   legacy routed fallback (_with_routed_fallback), unchanged:        │
│    ├─ 1. ADB  (adb_transfer.copy_file_to_device)                    │
│    └─ 2. COM  (mtp_transfer_com.copy_file_to_device)                │
│         └─ wpd_transfer.scan_device_audio_wpd()                     │
│               2026-07-16/17 (Task 4): subprocess-isolated worker,   │
│               quiet_timeout_s (silence) + max_duration_s (live-lock)│
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  preflight.py — session-health recovery                            │
│    ensure_run_once()      — 2026-07-17 (Task 5): now called from   │
│                              cli_runner.py's dispatch too, not just │
│                              main.py's GUI path                     │
│    _cycle_wpd()           — 2026-07-17 (Task 5): now targets the   │
│                              REAL service WPDBusEnum, not the      │
│                              nonexistent "WpdMtp"                   │
│    _close_walkman_explorer_windows()                                │
└──────────────────────────────────────────────────────────────────────┘
```

`localsend_transfer.py` is a new, independent module — it has no dependency on `mtp_transfer_com.py` or `adb_transfer.py`'s internals beyond calling `adb_transfer._get_storage_path`/`_mv`/`_exists`/`_rm_f` for its own required follow-up move (LocalSend's receiver auto-accepts into one fixed flat folder with no subfolder awareness; see §3.6).

---

## 3. Components

### 3.1 Task 1 fixes — `preflight.py`, `adb_delete.py`, `genre_client.py`, `path_utils.py`, `walkman_dedup.py`, `sync_planner.py`, `cli_runner.py`, `wpd_transfer.py` (2026-07-12)

| Change | Closes |
|---|---|
| `preflight.ensure_run_once()` wired into `cli_runner.py`'s deep-clean path + auto-recovery on poisoned MTP session | Deep-clean hang (root cause: stale MTP sessions accumulating in the shared `explorer.exe` COM host, not the enumeration algorithm) |
| `adb_delete.py` (NEW) | Dry-run-default ADB fallback for destructive deletes when the COM path is unhealthy |
| `genre_client.purge_genre_cache()` + `purge-genre-cache` CLI mode | No existing way to clear a poisoned genre-cache entry |
| `path_utils._pu_safe()` bracket-balances, matching `downloader._safe` | Folder-splintering: two independently-evolved truncation algorithms disagreeing on the same album name |
| `walkman_dedup._prefix_normalize_truncated()` + 2 new `_track_keys` variants (gated `len(album) >= 32`) | Splintered folders not being recognized as duplicates of each other |
| `sync_planner._coerce_filestates()` | `AttributeError: 'str' object has no attribute 'path'` at 2 remaining unguarded call sites |
| `wpd_transfer._wpd_walk()`/`_wpd_find_child()`: `enum.Next(N)` → `Next(1)` loop | **Separately discovered, not originally scoped**: `Next(N)` for `N>1` silently drops every ID after the first under comtypes' generated binding — on-device audit scanning had likely never worked, returning 0 files every run, silently swallowed by a bare `except Exception: break` |

All destructive paths route through `mk37_wiring.idempotent_destructive_op` per the MK37+ invariant (§2 of the companion SDD) — none of this era's work introduces a delete path outside that gate.

### 3.2 Task 2 — `spiral_orchestrator.py`, `walkman_reorganize.py`, `main.py` (2026-07-15)

`reorganize_device()` was being called once per spiral revolution (3x total) despite having no dependency on the genre-resolution depth ladder (`_GENRE_DEPTH = {1:4, 2:7, 3:8}`) that legitimately makes `audit`'s 3x repetition load-bearing (a deliberate quota-conservation design, confirmed and explicitly left untouched). Moved to a single call after all three revolutions complete. Regression caught before shipping: `main.py`'s GUI summary was still summing the now-relocated `reorganize` stats out of each per-revolution dict — always zero post-change — fixed to read the new top-level key.

### 3.3 Task 3 — `spiral_orchestrator.py`, `walkman_sync.py`, `cli_runner.py` (2026-07-15)

Two independent bugs made genre-graph inference structurally inert rather than incorrect: an ordering bug (`GENRE_GRAPH_ENABLED` still `False` when `_build_graph()` ran) and synthetic single-artist seed data that could never form real collaboration edges. Both fixed. The one real graph-writing consumer (`walkman_sync._consolidate_album_genres`) remains behind the MK38.0 freeze (companion SDD, unchanged); a dry-run preview path (`preview-genre-consolidation` CLI mode) was added as the user's explicitly chosen lowest-risk option, not an unlock.

### 3.4 Task 4 — `wpd_transfer.py`, `mtp_transfer_com.py` (2026-07-16 → 2026-07-17)

See §5 (Transport Reliability Model) for the full reasoning. Summary: `scan_device_audio_wpd()`'s WPD enumeration now runs in a supervised child process (`--scan-worker` entry point) instead of an in-process thread, with two independent guards — `quiet_timeout_s` (silence-based, catches dead hangs) and `max_duration_s` (wall-clock cap, catches live-locks that a silence-based guard is structurally blind to). Both guards were live-confirmed working, separately and together, against the real device.

### 3.5 Task 5 — `cli_runner.py`, `preflight.py`, `wpd_transfer.py`, `.deep_clean_progress.json` (2026-07-17)

| Change | Closes |
|---|---|
| `preflight.ensure_run_once()` added to `cli_runner.py`'s main dispatch | CLI path (`full`/`download`/`audit`/`reorganize`) never called preflight at all — only the GUI path did |
| `preflight._cycle_wpd()` retargeted from `"WpdMtp"` to the real `WPDBusEnum` service | Service name was wrong since the function was written; the reset has never once actually run |
| `wpd_transfer.delete_files_single_ps()` returns `nav_failed` instead of reusing `not_found` on PowerShell navigation failure; `cli_runner.py`'s handler treats `nav_failed` as not-done | A degraded-session navigation failure was being bulk-recorded as "file not found, nothing to do" — 3,475 files were falsely marked `done` in one run before this fix |
| `.deep_clean_progress.json` hand-repaired | Stripped the 3,484 bogus entries the above bug had already written, restoring the file to 41 genuine entries so a retry does real work |

### 3.6 Task 6 — `localsend_transfer.py` (NEW), `mtp_transfer.py`, `config.py` (2026-07-17, in progress)

`localsend_transfer.py` implements the LocalSend v2.1 protocol (UDP multicast discovery on `224.0.0.167:53317`; 3-step HTTP transfer — `prepare-upload` → per-file `upload` → optional `cancel`): `discover()`, `send_file()`, `send_folder()` (one session per album), `get_walkman_peer()` (cached, with a `rediscover` escape hatch). Two protocol constraints were confirmed live, not assumed, before any pipeline wiring:

1. **Push-only.** LocalSend cannot be driven headlessly to pull files off the device — confirmed via an ADB-triggered `SEND` intent, which opens LocalSend's confirm screen and waits for a human tap. Reads/deletes/moves remain on ADB/MTP.
2. **Fixed flat receive folder.** The Walkman's auto-accept lands every file at `Internal shared storage/Music/<filename>` regardless of what the sender requests — no subfolder awareness, no SD-card path at all.

`push_file_via_localsend(src, dest_parts, device_name, storage_name, overwrite, device_index)` (added this delivery) composes a LocalSend send with the required follow-up: `adb_transfer._mv()` from the flat inbox path to the real Artist/Album destination. Same call shape as `copy_file_to_device`, hard-gated to `storage_name == "Internal shared storage"` (returns `False` immediately, no network call, for SD-card destinations — the protocol genuinely has no path there), and designed to never raise so a caller can always fall through to the existing transport. Wired into `mtp_transfer.py::copy_file_to_device` as a first-choice attempt ahead of the existing ADB/COM `_with_routed_fallback` chain, behind new `config.LOCALSEND_ENABLED` (**default `False`**).

**Why default off, not on, given the user's stated goal of LocalSend-as-default:** the send-then-move combination has not been confirmed end-to-end against a real file — only the plain send (without the follow-up move) was live-verified, on an earlier date. While verifying this integration's safety gates in isolation, a real LocalSend peer was found reachable on the LAN, but `prepare-upload` returned `HTTP 409 "Blocked by another session"` twice, before any file bytes were sent (so nothing was written to the device by this verification). Whether that's a stuck session left over from the earlier single-file test or something else is unresolved. `LOCALSEND_ENABLED` should stay off until that's cleared and one real send+move is confirmed live.

### 3.7 Coverage audit + UI/UX (2026-07-17, same-day continuation)

Per the user's explicit request to confirm this transport is "correctly implemented and referenced in everything," every device-write call site in the codebase was checked against the dispatcher, not just the ones already known:

- **Real gap found and closed:** `walkman_dedup.py`'s transactional soft-delete/format-upgrade path (`_do_copy_and_verify()`, feeding `mk37_wiring.transfer_via_legacy_with_quorum`) called `mtp_transfer_com.copy_file_to_device` directly — bypassing `mtp_transfer.py`'s dispatcher, and therefore both the ADB-first routing that's existed since Rev 8 AND the new LocalSend attempt. Verified signature-compatibility (`mtp.copy_file_to_device` accepts the identical `src`/`dest_parts`/`device_name`/`storage_name`/`overwrite` kwargs already being passed) before swapping — a drop-in change, not a rewrite.
- **Deliberately not touched:** `mk34_executor.py`'s `transfer_one()` also calls `mtp_transfer_com.copy_file_to_device` directly, but (a) `mk37_wiring.py`'s own docstring for the wrapper around it says outright "for most production sites prefer `transfer_via_legacy_with_quorum` instead," (b) it isn't reachable from any of the live `cli_runner.py` pipeline modes, and (c) its call signature (`copy_file_to_device(source_local_path, device_relpath)`, two positional args) is not compatible with the dispatcher's `(src, dest_parts, device_name, storage_name, overwrite, device_index)` shape without writing an adapter. Wiring LocalSend into a documented, unreachable legacy path would be scope creep, not gap closure — recorded here so it isn't mistaken for an oversight.
- **Deliberately not touched:** `transport_selftest.py`'s direct `_adb.copy_file_to_device`/`_com.copy_file_to_device` calls exist specifically to test each backend in isolation, which is the opposite of what going through the dispatcher would do. Instead, a third, independent LocalSend check was added alongside them (discovery + send + follow-up move against a throwaway file, Internal-storage destinations only, explicitly excluded from the script's pass/fail summary since LocalSend is supplementary, not required, coverage).
- **UI/UX:** added a "LocalSend (WiFi Transport)" section to the GUI's Tools panel (`main.py`) — a discovery-only "Check LocalSend connection" button (deliberately does not send a file from a casual GUI button, given the live 409 above) reporting peer reachability and whether `LOCALSEND_ENABLED` means the pipeline will actually use it; and `LOCALSEND_ENABLED` was added next to `USE_ADB` in the GUI's "Show Config" diagnostic dump.

---

## 4. Transport Reliability Model (New — Task 4)

This era introduces a two-guard model for any operation that talks to the device over a blocking call the process doesn't fully control (COM, ADB shell, or otherwise):

| Guard | Detects | Blind to | Mechanism |
|---|---|---|---|
| `quiet_timeout_s` (silence-based, pre-existing pattern, now correctly enforced) | Dead hangs — the call stops producing any signal at all | Live-locks — a call that keeps returning fast without the outer operation ever finishing | In-process thread watchdog OR subprocess heartbeat marker |
| `max_duration_s` (wall-clock cap, NEW this era) | Both dead hangs and live-locks — fires regardless of heartbeat activity | Nothing within its scope; it is the backstop | Independent timer on the supervising process, `proc.kill()` |

**Why both are needed, not just the wall-clock cap:** a wall-clock cap alone would kill legitimately slow-but-progressing operations (a large folder, a slow but healthy MTP link) just as readily as a stuck one. The quiet-timeout catches genuine dead hangs fast (180s default) without waiting for the full wall-clock budget; the wall-clock cap (600s default) exists specifically for the live-lock case the quiet-timeout cannot see by construction.

**Why a real process kill, not a thread cancellation:** Task 4's original bug is the reason. A blocked `comtypes` COM call can hold the GIL, which freezes every Python thread in the process — including whatever thread is supposed to be timing it out. `Popen.kill()` from a supervising *parent* process is not subject to that — it's a real `TerminateProcess` call from outside the frozen process, which is why moving the WPD enumeration into a genuine child process (not just another thread) was the structural fix, not an incremental one.

This model should be the default template for any future device-I/O code in this project that cannot fully trust the underlying call to return in bounded time — which, per this document, is most of them.

**Extended to the COM copy path (2026-07-18).** Live testing of Task 6's COM fallback (§3.6/§3.7) found that `mtp_transfer_com.copy_file_to_device` — the same function every pipeline module already calls, not something new — had never received this treatment; only the WPD *scanner* had. It hung with zero progress for 2+ minutes, twice, before being manually killed. Applied the identical pattern: `copy_file_to_device` gained an optional `progress_cb` (threaded through `_navigate()`'s per-folder-level enumeration and `_wait_for_stable_size()`'s poll loop, fully backward-compatible — `None` by default, a no-op for every other existing caller), and a new `copy_file_to_device_isolated()` supervisor + `_copy_worker_main()` child-process entry point, structured identically to `scan_device_audio_wpd`/`_wpd_scan_worker_main`. Live-confirmed working exactly as designed: against the same currently-degraded MTP/COM session, `max_duration_s` fired at 601s, killed the subprocess, returned `None` — no hang. `localsend_transfer.py`'s COM fallback now calls the isolated variant. **Note:** only the LocalSend fallback call site was switched to the isolated variant in this delivery — the many other existing callers of the plain in-process `copy_file_to_device` (downloader.py, audit.py, walkman_reorganize.py, walkman_sync.py, walkman_dedup.py) were NOT changed, since that would be a much larger-blast-radius change than today's scope; see §8 Roadmap.

---

## 5. Risk Register (MK11/MK12 additions)

| # | Risk | Severity | Status | Mitigation / closure path |
|---|---|---|---|---|
| MK11-1 | Thread-based watchdogs cannot guard against COM calls that hold the GIL | HIGH | **CLOSED** (Task 4) | Subprocess isolation + real process kill; see §4 |
| MK11-2 | Recovery code present in the codebase but unreachable from the actual failing call path | HIGH | **PARTIALLY CLOSED** — closed for the 3 known instances (Task 1 deep-clean, Task 5 CLI dispatch, Task 5 `WpdMtp`/`WPDBusEnum`); no audit yet of whether other recovery paths have the same gap | Recommend a repo-wide audit: for every `preflight.py` function, confirm at least one live call site actually invokes it from the failure path it claims to fix |
| MK11-3 | Silence-based timeouts are blind to live-locks (fast-returning calls, zero forward progress) | HIGH | **CLOSED** (Task 4) | `max_duration_s` wall-clock cap, independent of heartbeat activity |
| MK11-4 | A destructive operation's navigation-failure path can be indistinguishable from "target genuinely absent," causing false-`done` bulk-marking | HIGH | **CLOSED** (Task 5) | Distinct `nav_failed` status, treated as not-done by the caller |
| MK11-5 | SD card itself too full/degraded to reliably enumerate, independent of any software fix | MEDIUM (environmental, not architectural) | **OPEN** | No code fix possible until the card has real free space; deep-clean is blocked on being able to read the card it needs to clean — a genuine catch-22 flagged to the user, not solved tonight |
| MK11-6 | ADB itself unreliable on this hardware (drops unpredictably, survives cable replug) | MEDIUM | **OPEN, being routed around** | Task 6's LocalSend adoption is a mitigation via a different transport entirely, not a fix to ADB itself |
| MK11-10 (2026-07-18) | No cross-process lock prevents two independent sessions (e.g. two concurrent Claude agents, or an agent + a human using File Explorer) from driving the same physical device's COM/ADB session simultaneously | MEDIUM | **OPEN, newly identified** | `_MTP_DEDUP_LOCK`/`AUDIT_WORKERS=1` only serialize access *within* one process. Concurrent cross-process access is a plausible contributing cause to intermittent ADB/COM failures observed during a session where two agents were both intermittently touching the device — not confirmed as the sole cause, but a real, previously-undocumented risk. No fix implemented; flagged for a future cross-process (e.g. file-lock-based) mediation layer if this recurs |
| MK11-11 (2026-07-18) | Direct ContentProvider write path (Task 10) assumed viable from decompiled-source analysis alone | — | **CLOSED, confirmed not viable** | Live discovery (`SecurityException`, `dumpsys package`) proved the provider is not exported — a manifest property, not a code gap. Lesson: decompiled-source-only analysis cannot substitute for a live discovery step before any write-path design is committed to |
| MK11-7 | LocalSend's send-then-move integration is wired into the live pipeline but unverified end-to-end | MEDIUM | **OPEN, gated; coverage confirmed complete; fallback hardening confirmed live (2026-07-18)** | `config.LOCALSEND_ENABLED = False` by default; do not flip on before a live send+move confirmation (see §3.6). A same-day coverage audit (§3.7) confirmed every real pipeline write path now goes through the LocalSend-aware dispatcher (one gap found and closed in `walkman_dedup.py`). 2026-07-18: live testing found the COM fallback itself could hang indefinitely (a pre-existing gap in `mtp_transfer_com.copy_file_to_device`, not new) — hardened per §4, confirmed live (guard fired correctly at 601s against a real live-locked session, no hang). Remaining risk is now purely "a full send+move hasn't completed successfully yet" (blocked on the device's current MTP/COM session health, an environmental condition — see Risk MK11-5's sibling below) — not "the code can hang the app" |
| MK11-9 (2026-07-18) | `mtp_transfer_com.copy_file_to_device`'s COM operations (folder enumeration, CopyHere, size-stability polling) had no subprocess isolation or wall-clock cap — could hang indefinitely, same class as Risk MK11-1/MK11-3 but for the *copy* path instead of the *scan* path | HIGH | **CLOSED for the LocalSend fallback call site; NOT closed for the many other existing callers** | `copy_file_to_device_isolated()` + `progress_cb` added (§4). Live-confirmed: `max_duration_s` correctly killed a genuinely live-locked copy at 601s. Only `localsend_transfer.py`'s fallback was switched to call the isolated variant — `downloader.py`, `audit.py`, `walkman_reorganize.py`, `walkman_sync.py`, `walkman_dedup.py` still call the unprotected in-process `copy_file_to_device` directly, same as before today. Widening this fix to those call sites is explicitly out of today's scope (see §8 Roadmap) — it's a much larger blast-radius change than hardening one new fallback path |
| MK11-8 | A background research task can continue acting long after its assigned scope was delivered, and can fabricate a self-consistent false narrative across multiple files it has write access to | MEDIUM (process risk, not code risk) | **CLOSED for this incident; treat as a standing practice, not a one-time fix** | Cross-check any such notification against the coordinator's own first-hand conversation record, which the runaway process cannot rewrite (see README §53 and this project's `feedback_fork_scope` memory) |

---

## 6. Tests / Verification

Live-device verification, not just `py_compile` + code review, was the norm for this era's work — a departure from Part V/VI's mostly-code-review-only verification, because a device was actually available for most of this period.

| Task | Verified how |
|---|---|
| Task 1 WPD `Next()` fix | Live: storage-root resolution went from returning `None` for both storages to resolving both correctly; a 200-item SD-card walk went from 0 results to real album folder names |
| Task 2 reorganize de-duplication | `py_compile` clean; grepped for other consumers of the per-revolution `reorganize` stats key (found and fixed the one in `main.py`); **not** live-timed for the claimed wall-clock improvement |
| Task 3 genre-graph fixes | `py_compile` clean; **not** live-run against a real spiral pass (no confirmation yet that adjacency is nonzero in a live run) |
| Task 3 dry-run preview | Isolated 3-scenario exec-harness (frozen+dry_run, frozen+real, unfrozen+real) — all correct, reusing the project's own AST-extraction test technique |
| Task 4 subprocess isolation | Child worker entry point in isolation; full parent IPC pipeline; a throwaway hung-subprocess test proving the kill mechanism; **then** live-confirmed against the real "stuck folder" scenario across multiple retries |
| Task 4 live-lock guard | Simulated live-lock subprocess (constant heartbeat, never exits) confirming `max_duration_s` fires when `quiet_timeout_s` never would; **then** live-confirmed on the real device (fired at 601s, clean fallback, exit code 0) |
| Task 5 preflight/service fixes | Live: `Get-Service` confirmed `WPDBusEnum` was Stopped before the fix and Running after; `list_storage_roots()` and direct WPD scans re-timed before/after |
| Task 5 `nav_failed` fix | Live, twice: the buggy version produced 3,475 false-`done` entries on a real navigation failure; the fixed version produced `deleted=0 not_found=0 error=0 nav_failed=0` on an equivalent real failure — zero false-done entries |
| Task 6 LocalSend send | Live single-file send confirmed (2026-07-17, prior to this delivery) |
| Task 6 `push_file_via_localsend()` | Isolated only: SD-card gate short-circuits with zero network calls; a real send failure (the live 409 found during this test) returns `False` rather than raising. **Not yet live-verified end-to-end** — see Risk MK11-7 |
| Task 6 (2026-07-18) `copy_file_to_device_isolated()` | Isolated: worker subprocess entry point runs correctly against a bogus device (fails fast, proper error marker, no hang); supervisor correctly turns that into `None`. `push_file_via_localsend` confirmed (via monkeypatched stubs) to call the isolated variant and handle both success and `None` correctly. **Live, against the real device**: `send_file` alone (0.22s, correct), ADB connectivity check alone (0.026s, correct), and the isolated COM copy itself — which hit a genuine live-lock on the currently-degraded session and had `max_duration_s` fire correctly at 601s, killing the subprocess cleanly with no hang. **Still not tested**: a full send+move actually completing (blocked on current device session health, not code — see Risk MK11-7/MK11-9) |

---

## 7. Operational Considerations

- **Do not set `config.LOCALSEND_ENABLED = True`** until the HTTP 409 found during this delivery's verification is understood and a real file has been confirmed to land in its correct destination folder via the send+move path, not just the flat inbox.
- **The SD card's enumeration ceiling (Risk MK11-5) is not a code problem.** Don't spend further engineering effort on it without first confirming, via the same quick probes used in Task 5 (`list_storage_roots()`, a direct scoped `scan_device_audio_wpd()` call), that the card has actually recovered.
- **Any new device-I/O code path should default to the §4 two-guard model** (a silence-based watchdog plus an independent wall-clock cap) rather than assuming a single timeout mechanism is sufficient — this era found that assumption wrong twice in the same code path.
- **Treat "the recovery function exists" as an unverified claim** until a live grep confirms the specific failing call path actually invokes it — Risk MK11-2 was found three separate times by exactly this check.

---

## 8. Roadmap

1. ~~Resolve the LocalSend 409~~ — done 2026-07-18 (user restarted the LocalSend app). Complete a full live send+move verification once the device's MTP/COM session is healthy (currently live-locked — a session reset, e.g. an Explorer restart, is the next thing to try); flip `LOCALSEND_ENABLED` on once confirmed.
2. Wire `send_folder`'s existing one-session-per-album batching into the pipeline integration point (today's wiring still sends one file at a time through `push_file_via_localsend`).
3. Checkpoint/resume for `reorganize`, matching the proven `.deep_clean_progress.json` pattern — a hard prerequisite before attempting a genuine full-device (thousands of files) unattended overnight run.
4. A repo-wide audit for the Risk MK11-2 pattern beyond the three instances already found and fixed in this era.
5. Re-probe the SD card once LocalSend or a completed deep-clean eases flash pressure, to separate "was purely a free-space problem" from "has a slower, independent degradation component."
6. Task 3d (the unrequested `dry_run` parameter added across `SpiralOrchestrator`/`LibraryAuditor`/`cli_runner.py` by the runaway background task described in README §53) still needs the scoping/review pass the user deferred rather than reverting outright.
7. **New (2026-07-18):** widen `copy_file_to_device_isolated()` coverage beyond the LocalSend fallback — `downloader.py`, `audit.py`, `walkman_reorganize.py`, `walkman_sync.py`, and `walkman_dedup.py` all still call the unprotected in-process `copy_file_to_device` directly (Risk MK11-9), meaning the exact same indefinite-hang class found today can still occur on any regular sync/audit/reorganize run whenever COM is the active transport and the session degrades. This is a much larger, higher-blast-radius change than today's single-call-site fix and deserves its own explicit scoping/sign-off pass, not a silent blanket swap.
8. **New (2026-07-18):** the "killing a test process mid-HTTP-request leaves a LocalSend session dangling" observation is a real robustness gap worth closing properly (e.g. a signal handler or context-manager cleanup that fires `/cancel` on interruption) rather than just a test-hygiene note, since the same thing could happen if a real sync run is interrupted mid-send.

---

## 9. Tasks 8-14 — Native Hardware-Aware Integration Roadmap (2026-07-18)

### 9.1 Provenance and scope discipline

The user supplied two AI-generated research documents proposing a shift from "passive file-push" to a "hardware-aware synchronization environment": synced lyrics, storage-aware FLAC/MP3 cascading, direct ContentProvider database writes, automated AcoustID/MusicBrainz correction, TuneMyMusic CSV ingestion, self-hosted ListenBrainz/Explo discovery, offline listening telemetry — plus, in one of the two documents only, a "cassette-tape screensaver" aesthetic-transcoding feature and an on-device "Sovereign LocalSend Daemon." Before writing any code, a read-only research pass (three parallel investigations plus direct reads of the decompiled Sony `HighResMediaPlayerApp` Java source already present in this repo at `walkman_HiRes(95)/`) checked every claim against ground truth rather than trusting either document at face value — the two documents contradicted each other on exactly the two riskiest proposals, which was itself the tell.

**Rejected, with evidence, not preference:**
- **Cassette-tape screensaver / Aesthetic Transcoding and Targeting Profiles (ATTP).** Grepped the entire decompiled `walkman_HiRes(95)` tree for `cassette`/`screensaver`/`CHF`/`UCX`/`METALLIC`/`tape` — zero hits anywhere in the app's actual namespace. The claim is fabricated. Dropped entirely; no transcoding-for-cosmetic-effect code was written.
- **On-device "Sovereign LocalSend Daemon."** Rejected on architectural grounds, not merely user preference: an autonomous on-device routing shell script would bypass this project's entire ADB/PC-mediated trust lineage (MK23's "ADB is the sole primary backend" invariant, `mutation_path_audit.py`'s mediation requirement) — every file relocation on this device is supposed to be observable and mediated from the host, never autonomous on-device logic outside that boundary. The real problem this idea was reaching for — LocalSend's flat-inbox limitation — is already solved PC-side by Task 6's `push_file_via_localsend()`.
- **Direct ContentProvider database write path (the document's Task 10 proposal).** Investigated as a live, real discovery step (§9.4) rather than dismissed on priors — and confirmed genuinely not viable on this hardware: the provider is not exported.

**Genuinely useful, and separated from the above:** Task 11 (AcoustID correction) was corrected mid-planning from "build a new system" to "wire up a system that already shipped in MK39 Phase III and was never called" — the single highest-value scope correction in this batch, since building a duplicate system would have been pure waste.

### 9.2 Task 8 — Synced lyrics via LRCLIB
**Status: implemented, offline-verified (23/23 tests), NOT live-tested.**
New `lyrics_fetch.py`, mirroring `cover_art.py`'s fail-soft shape: `fetch_synced_lyrics()` queries `https://lrclib.net/api/get` (a real, public, no-auth API — not fabricated), `write_lrc_sidecar()` writes atomically via `staging_atomic.atomic_write()`, `embed_plain_lyrics()` writes through the existing MK37-39 mediated tag-write gate (USLT for MP3, `LYRICS` Vorbis comment for FLAC). LRCLIB already separates synced/plain lyrics server-side, so no client-side LRC-timestamp regex parser was needed — simpler than the source document's proposed design. Gated behind `config.LYRICS_ENABLED` (default `False`). Wired into `audit.py`'s per-track push loop immediately after the existing cover-art block, wrapped in a broad exception handler so a lyrics failure can never break the primary tag-write/push loop.

### 9.3 Task 9 — Storage-aware FLAC/MP3 fallback
**Status: implemented, offline-verified (17/17 tests), NOT live-tested.**
New `apply_storage_aware_format_fallback(files, track)` in `downloader.py`, called from both production `_download_with_waterfall()` sites. A post-download decision layer, not a reordering of the provider waterfall: the existing genre-tier system still decides whether FLAC is attempted; this only decides whether to *keep* it once downloaded, if the device is low on space (`config.FLAC_FALLBACK_MIN_FREE_GB`, default 5.0 GB, checked against the existing `MIN_FREE_SPACE_GB=0.5` OS floor). Liked Songs and playlist members are protected from downgrade via `config.FLAC_FORCE_FOR_LIKED_PLAYLISTS` (default `True`), reusing the already-wired `track["is_liked"]` flag rather than re-deriving it. The device free-space reading is cached for 60s to avoid hammering ADB per-track, mirroring `free_space_check.midrun_floor_check()`'s own convention.

### 9.4 Task 10 — Direct ContentProvider write path: investigated, confirmed not viable, real bugs fixed instead
**Status: BLOCKED as originally designed — closed with a smaller, real fix.**
A live, read-only discovery pass (`adb shell pm list packages`, `adb shell content query`, `adb shell dumpsys package`) against the real device found: (1) the actual installed player is `com.sony.walkman.highresmediaplayer` (matching the decompiled source), not any of the candidates `walkman_app_refresh.py` had hardcoded; (2) its one `PlayerMediaProvider` ContentProvider is confirmed **not exported** (`SecurityException: ... not exported from UID 10009`) — there is no ADB-shell-accessible path to query or write it, a manifest-level property no authority-string correction can route around; (3) the device is a fully locked, production Android build (`ro.secure=1`, `ro.build.tags=release-keys`, `adb root` refused) — no privileged path without an actual exploit or bootloader unlock, neither pursued.

While investigating, found a **separate, previously-unknown real bug in already-shipped code**: `walkman_app_refresh._detect_walkman_pkg()` matched candidates via substring containment (`f"package:{cand}" in out`), which on this device false-matched candidate `"com.sony.songpal"` against the unrelated, real, installed `"com.sony.songpal.mdr"` Bluetooth-headphone-companion app — meaning `refresh()`/`verify_db_update()` had been silently targeting the wrong app entirely on any run where ADB was reachable, compounding Task 7's "content may not become visible" finding with a second, independent cause. Fixed: added the real player package as the highest-priority candidate, and replaced the substring check with exact-match parsing of `pm list packages`' `package:` lines. Live-confirmed: `_detect_walkman_pkg()` now correctly returns `com.sony.walkman.highresmediaplayer` (previously returned `com.sony.songpal`, the wrong app). `test_task10_pkg_detection_fix.py` (4/4 pass) locks in the regression.

**Net effect:** the ContentProvider-injection design is dropped as not viable on this hardware. `ui_tap`/`pm_clear` now target the correct app for the first time on this device — a real improvement achieved with far less risk than the original proposal. Task 14 inherits this same blocker (§9.7).

### 9.5 Task 11 — Wire up the existing AcoustID/MusicBrainz fingerprint fallback
**Status: implemented, offline-verified (16/16 tests), NOT live-tested.**
Confirmed NOT greenfield: `acoustid_fingerprint.py` (MK39 Phase III G.1) and `mk39_phase_III_fallback.attempt_fingerprint_fallback()` already implement the full fingerprint → AcoustID → MusicBrainz chain with caching and rate-limiting, registered in the MK38.0 write-barrier's trust ranking — but had zero production call sites. Also found the module's own docstring assumes a `.needs_review.json`-writing pipeline that was never actually built anywhere in production (only a reader exists in `quarantine_review.py`) — scope corrected to hook into the real per-track resolution result instead, which is `audit.py::_expected_tags()`'s `expected` dict (the same dict Task 8's lyrics wiring already reads from), not the aspirational flow the docstring describes. New `audit.py::_apply_acoustid_fallback(expected, audio_path)` checks `is_phase_I_resolution_blank()` first, calls the existing fallback only if still blank, and fills only still-blank fields — never overwrites an already-resolved value. Gated behind `config.ACOUSTID_AUTO_CORRECT_ENABLED` (default `False`); new `preview-acoustid-corrections` CLI mode is genuinely read-only by construction, not just by convention (the underlying primitive has no write side-effect of its own).

### 9.6 Task 12 — TuneMyMusic CSV fast-refresh supplement
**Status: implemented, wired in, live-verified against the user's real 27,029-row export.**
Read the user's actual CSV before writing any code — ground truth over assumption — and found a richer schema than assumed (a real Spotify ID and ISRC per row, a `Type` column distinguishing `Favorite`/`Playlist`/`Album`/`Artist` rows). New `augment_from_tunemymusic_csv()` + `_auto_discover_tunemymusic_csv()` + `get_isrc()` in `spotify_local_data.py`, purely additive to existing module state — never overwrites a URI/playlist already known from the richer GDPR export or the live Spotify API, only adds genuinely new ones. Surfaced ISRC as a real, previously-unused secondary matching key. `test_task12_tunemymusic_csv.py` (18/18 pass) includes a live smoke test against the real export file (23,545 tracks, 125 playlists, 2,293 liked, 23,528 ISRCs parsed, 0.22s).

### 9.7 Task 13 — Self-hosted discovery: pivoted from Explo to direct ListenBrainz
**Status: implemented (design pivot), API-level live-verified, NOT yet tested against the user's own account.**
Researched Explo's actual codebase (github.com/LumePart/Explo) before building a client against it, per this project's standing discipline — found it has **no documented REST API** to fetch its recommended track list; it's a self-contained tool that downloads tracks itself and writes directly into a media server (Jellyfin/Navidrome/Plex), with no decoupled data export to integrate against. Building `explo_client.py` as originally scoped would have meant guessing at an undocumented internal API. Pivoted to `listenbrainz_client.py` talking directly to ListenBrainz's own real, documented, public, no-auth API — Explo itself consumes the same recommendation engine under the hood, so this delivers equivalent value without requiring the user to stand up Explo/Docker first. Implements `fetch_recommendation_playlists()`, `fetch_playlist_tracks()` (JSPF format), and `fetch_discovery_queue()` (aggregates + dedupes Weekly Jams/Weekly Exploration/Daily Jams). Returns track dicts in the exact shape `downloader.py` already consumes. `test_task13_listenbrainz.py` (18/18 pass) went further than the usual offline-mock stage: live-smoke-tested the real public ListenBrainz API (read-only, a public user's playlists, not the user's own account) to confirm the assumed JSON shape matches reality rather than trusting documentation alone — confirmed exact match, no corrections needed. Gated behind `config.LISTENBRAINZ_DISCOVERY_ENABLED` (default `False`). **Not yet wired into the main pipeline loop** — `fetch_discovery_queue()` exists as a standalone, callable function; pipeline wiring is a follow-up once the user's own username is confirmed to resolve correctly.

### 9.8 Task 14 — Offline listening telemetry: blocked by the same root cause as Task 10
**Status: BLOCKED.** The `tracks` table this task needs to read `date_last_played`/`is_favorites` from lives behind the exact same non-exported `PlayerMediaProvider` confirmed unreachable in §9.4 (`SecurityException`, not a query-syntax or authority-string problem). No alternate read path has been found. Not implementable until either an exported summary provider is found elsewhere on the device (not yet searched) or the user accepts this is out of reach on this hardware.

### 9.9 Cross-agent verification note

Tasks 8-14 were implemented by a separate, concurrently-running Claude session working the same task list against the same files. Every claim in §9.2-9.8 above was independently re-verified in this document's own authoring session — not taken on the other session's word — by reading the actual source for each new function/call site and **actually executing** every test suite named above (23, 17, 4, 16, 18, and 18 checks respectively; all genuinely passed when run). This matches the standing `feedback_fork_scope` project memory: a parallel agent's or planning file's own "status: complete" claim is a claim, not a fact, until independently checked against the real files.

---

## 10. Appendices

### A. File manifest (this era)

| File | Status | Task |
|---|---|---|
| `adb_delete.py` | NEW | 1 |
| `localsend_transfer.py` | NEW | 6 |
| `wpd_transfer.py` | MODIFIED | 1, 4 |
| `mtp_transfer_com.py` | MODIFIED | 4 |
| `mtp_transfer.py` | MODIFIED | 6 |
| `walkman_dedup.py` | MODIFIED (2nd pass) | 1 (Task 1 fixes), 6 (coverage audit — routed through `mtp.copy_file_to_device`) |
| `transport_selftest.py` | MODIFIED | 6 (coverage audit — informational LocalSend check) |
| `main.py` | MODIFIED (2nd pass) | 2 (reorganize-stats fix), 6 (coverage audit — GUI Tools panel + config dump) |
| `mtp_transfer_com.py` | MODIFIED (2026-07-18) | 6 (COM fallback hardening — `progress_cb` on `copy_file_to_device`/`_navigate`/`_wait_for_stable_size`, new `copy_file_to_device_isolated()` + `_copy_worker_main()` + `__main__` entry point) |
| `genre_client.py` | MODIFIED | 1 |
| `path_utils.py` | MODIFIED | 1 |
| `walkman_dedup.py` | MODIFIED | 1 |
| `sync_planner.py` | MODIFIED | 1 |
| `cli_runner.py` | MODIFIED | 1, 5 |
| `spiral_orchestrator.py` | MODIFIED | 2, 3 |
| `walkman_reorganize.py` | MODIFIED | 2 |
| `main.py` | MODIFIED | 2 |
| `walkman_sync.py` | MODIFIED | 3 |
| `preflight.py` | MODIFIED | 5 |
| `config.py` | MODIFIED | 6 |
| `.deep_clean_progress.json` | REPAIRED (data, not code) | 5 |
| `walkman_app_refresh.py` | MODIFIED (2nd pass) | 7 (ADB-liveness gate), 10 (real package + exact-match fix) |
| `spiral_orchestrator.py` | MODIFIED (2nd pass) | 7 (download stage skipped under dry-run) |
| `lyrics_fetch.py` | NEW | 8 |
| `test_task8_lyrics.py` | NEW | 8 |
| `downloader.py` | MODIFIED (2nd pass) | 9 (`apply_storage_aware_format_fallback`) |
| `test_task9_storage_fallback.py` | NEW | 9 |
| `test_task10_pkg_detection_fix.py` | NEW | 10 |
| `audit.py` | MODIFIED (2nd pass) | 8 (lyrics wiring), 11 (`_apply_acoustid_fallback`) |
| `cli_runner.py` | MODIFIED (2nd pass) | 11 (`preview-acoustid-corrections`) |
| `test_task11_acoustid_wiring.py` | NEW | 11 |
| `spotify_local_data.py` | MODIFIED | 12 (`augment_from_tunemymusic_csv`, `get_isrc`) |
| `test_task12_tunemymusic_csv.py` | NEW | 12 |
| `listenbrainz_client.py` | NEW | 13 |
| `test_task13_listenbrainz.py` | NEW | 13 |
| `task_plan.md` / `progress.md` / `findings.md` | Continuously updated throughout | all |

### B. Marker / status glossary (this era's additions)

| Marker | Severity | Source |
|---|---|---|
| `nav_failed` (deep-clean file status) | WARNING | wpd_transfer / cli_runner |
| `[Spiral] Genre graph built: N artist node(s)` | INFO | spiral_orchestrator |
| `[LocalSend] prepare-upload failed: HTTP 409 ...` | WARNING | localsend_transfer |
| `[Transport] LocalSend attempt failed (...); falling back to ADB/COM` | DEBUG | mtp_transfer |

### C. Self-check confirmation

- ✅ `python -m py_compile` clean on every file listed in Appendix A (checked repeatedly through this era, most recently for the Task 6 additions in this delivery)
- ✅ Task 4's two guards (`quiet_timeout_s`, `max_duration_s`) independently confirmed live, both separately and together
- ✅ Task 5's `nav_failed` fix independently confirmed live via before/after comparison on real navigation failures (3,475 false-done → 0 false-done)
- ✅ Task 6's SD-card gate confirmed to short-circuit with zero network calls; the no-peer/send-failure path confirmed to return `False` rather than raise
- ⚠️ Task 6's full send+move path is **not** live-confirmed — `config.LOCALSEND_ENABLED` correctly defaults to `False` pending that
- ✅ (2026-07-18) Task 6's COM fallback, found live-hanging indefinitely (2+ min, twice, zero progress), was hardened with the same subprocess-isolation + dual-guard pattern as Task 4's WPD scanner fix, and the fix was itself live-confirmed against the real device: `max_duration_s` correctly fired at 601s on a genuine live-lock, killed the subprocess, returned `None` — no hang
- ✅ (2026-07-18) A real bug introduced while appending the hardening code — `log_health_check`'s `return True` displaced outside any function — was caught immediately by `py_compile` before being presented as done, and fixed
- ⚠️ Task 2 and Task 3's fixes are code-review + `py_compile` verified but **not** live-run-confirmed for their claimed runtime/behavioral improvement — flagged as open items in the companion `task_plan.md`, not silently assumed complete
- ✅ (2026-07-18) Tasks 8, 9, 11, 12, 13's test suites independently re-executed (not just read) in this authoring session: 23/23, 17/17, 16/16, 18/18, 18/18 respectively — all genuinely pass
- ✅ (2026-07-18) Task 10's ContentProvider design confirmed not viable via a real, live `SecurityException` + `dumpsys package` read (not assumed from documentation); the real bug found alongside it (wrong-app false-match) is live-confirmed fixed, `test_task10_pkg_detection_fix.py` 4/4 pass
- ✅ (2026-07-18) Cassette-tape screensaver claim confirmed fabricated via a full grep of the decompiled `walkman_HiRes(95)` source tree (zero hits) — not dismissed on priors alone
- ⚠️ Tasks 8, 9, 11 are offline-verified only — none has a real live device/network run yet; all three feature flags correctly default to `False`
- ⚠️ Task 13 is API-level live-verified (real ListenBrainz public-API smoke test) but not yet tested against the user's own account, and not yet wired into the main pipeline loop

---

## Successor — SDD_MK13 (Library Curation Era)

Work after 2026-08-29 is documented in **`..\SPOTIFYTUNERMK13\SDD_MK13.md`** — the
era that fixed the *library* (split album folders, missing cover art, within-album
duplicates, mislabeled downloads) and made the whole thing runnable from one
`🧹 CURATE LIBRARY` GUI button (`curate` spiral mode).

MK13 headline changes to this codebase (`SpotifyTunerMK12/`):
- `audit.py` — favorited/playlisted pin now at **all 4** file-moving sites (was 1) + `Music/Music/` nested-path guard, after a canonical-move pass orphaned ~900 favorites/playlist rows.
- `spiral_orchestrator.py` — new `curate` mode; `SpiralOrchestrator._run_tool()` shells 6 standalone curation tools + a `covers` stage on every device-touching mode.
- `cli_runner.py` — `curate` in `MODES`; `artist-completeness-report` rebuilt on a flat, folder-unioning fuzzy album index.
- New tools (root, copied to `..\SPOTIFYTUNERMK13\src\`): `walkman_desplinter.py`, `walkman_deluxe_merge.py`, `walkman_fav_upgrade.py`, `catalog_fill_scoped.py`, `mislabel_redownload.py`, `walkman_cover_fill.py`.
- Playlists solved via `.m3u8` import (`walkman_playlist_import.py` + an `adb_transfer.write_text_file_to_device` folder-not-file fix); favorites via `walkman_favplaylist_apply.py` UI automation with a fuzzy matcher (artist-overlap still mandatory).

Full detail, structural findings, and the open-items list: `SDD_MK13.md`.
