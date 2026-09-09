# SDD — MK13: The Library Curation Era

**System:** SpotifyTunerMK12 codebase (`C:\Users\FeisalK\WalkmanZX500 SpotifySync\SpotifyTunerMK12`), Sony Walkman NW-ZX507 sync platform.
**Document version:** 1.0 (2026-09-06)  ·  authoritative `.docx` build: `SDD_MK13.docx`
**Builds on:** `SDD_MK12.md` (MK11/MK12 Operational Hardening) and, below it, `SDD_MK37_MK37_1_MK37_2.md` (the semantic-integrity mediator). Neither is restated.
**Iterations covered:** one continuous multi-day session, 2026-08-29 → 2026-09-06 — from "downloads mostly land" to "followed-artist catalogs are substantially complete, favorites and playlists are real, cover art shows, and it runs from one GUI button."
**Companion artifacts:** `task_plan.md` / `progress.md` (cont.27–46) / `findings.md` — the planning-with-files record, the primary source for this doc. `README.md` Part IX — the narrative version. `catalog_completeness.html` — the per-artist album audit (also an Artifact). Memory `project_walkman_hires_player_playlists_favorites.md` updates #4–#12 — the cross-session record of the Playlists/Favorites solution.

---

## 0. SDD lineage — the route to here

The full chain of design docs is in `docs/sdd_history/` (extracted from the archived project snapshots in `C:\Users\FeisalK\WalkmanZX500 SpotifySync\`). Read in order they are the route a future dev takes to understand how the codebase reached its current shape:

| Doc | Era | What it covers |
|---|---|---|
| `SDD_SpotifyTunerMK4_Rev14.docx` | MK3–MK4 | first Spotify→Walkman sync; transfer basics |
| `walkman_sdd_rev6_complete.docx` … `walkman_sdd_rev12.docx` | rev6–rev12 (MK5–MK7) | MTP transfer, genre APIs, folder layout |
| `walkman_sdd_MK6.0.docx` … `walkman_sdd_MK6.9.docx` | MK6 point releases (MK8) | patch-driven hardening of the MK6 core |
| `walkman_sdd_MK8.docx` | MK8 | consolidation |
| `walkman_sdd_MK15.docx`, `MK16`, `MK18`–`MK22` | MK15–MK22 (MK10–MK11) | the CTO maturity arc — README Part II (MK26–MK33) is this line renumbered |
| `SDD_MK37_MK37_1_MK37_2.md` | MK37–MK39.1 (MK11) | the semantic-integrity mediator: `mk37_wiring.py`, the mutagen write gate, the MK38.0 freeze |
| `SDD_MK12.md` | MK11 / MK12 | Operational Hardening: MTP/COM/ADB transport resilience, subprocess isolation, `preflight` wiring |
| **`SDD_MK13.docx`** (this doc) | MK13 (MK12) | **The Library Curation Era** |

Nothing in MK13 changes the MK37→MK12 stack. It adds standalone curation scripts beside the pipeline and a thin orchestrator seam (`SpiralOrchestrator._run_tool`).

---

## 1. Executive summary

MK12 closed with the transport layer contained but the *library* — the ~8,000 files on the card — in poor shape: ~2,600 albums split across two or three folders, ~40–50 % of files with no embedded cover art, thousands of within-album duplicates, hundreds of mislabeled downloads, and a native-player Playlists/Favorites tab no PC-side process could write to.

MK13 fixed the library. The through-line: **the Sony HiRes player keys every piece of state — `is_favorites`, `playlist_tracks`, embedded cover art — to a specific on-device file at a specific path, and there is no root and no exported ContentProvider.** So every fix is either a file the player will re-scan, or a UI tap.

Five bodies of work:

1. **Playlists & Favorites, without root.** Playlists via `.m3u8` import (the scanner *does* import a well-formed one; a months-old `write_text_file_to_device` bug had produced malformed ones). Favorites via a UI-tap loop with a fuzzy matcher hardened against a real wrong-artist bug.
2. **The audit incident and the four-site pin.** A canonical-path consolidation pass moved ~900 favorited/playlisted files, orphaning their DB rows and every M3U8 that referenced them — because only 1 of `audit.py`'s 4 move paths honored the favorited/playlisted pin. All 4 now do, plus a `Music/Music/` nested-path guard.
3. **Structural cleanup — standalone tools** (§5): de-splinter, deluxe-merge, fav-upgrade, cover-fill, year-backfill, storage-rebalance, and a read-only 5-axis incompleteness audit.
4. **The fill tools** — `catalog_fill_scoped.py` (absent *and* partial), `mislabel_redownload.py`, `walkman_cover_fill.py`.
5. **GUI integration** — a `curate` spiral mode + `🧹 CURATE LIBRARY` button running the whole chain; the `covers` stage additionally on every device-touching mode.

**Structural findings — the takeaways of this era:**

1. **State keyed to a file path needs the pin at EVERY move site.** `audit.py` had 4 move paths and the pin at 1. The incident was a *coverage* bug, not a logic bug — same class as MK12's "recovery code that exists but isn't wired into the failing path."
2. **Favorites survive a full DB rebuild; playlists do not.** The player re-matches `is_favorites` by content/tags, so a *moved* favorited file keeps its star — only a *deleted* one loses it. Playlists (`.m3u8`, relative paths) break on any move and are regenerated every run.
3. **At library scale, exact-normalized title matching is wrong ~15–30 %, one-directionally — it inflates "missing."** Retagged titles, dropped feat credits, edition suffixes, split folders. This would have had the fill tools re-download present tracks. The fix (§6) is a fuzzy layer over a *flat, folder-unioning* album index — routing through `DeviceLibrary._find_best_entry` **regressed** the numbers because it picks one folder and a split album has several.

**Status at close (2026-09-06):** Playlists and favorites recovered and verified in-app. Three dedup passes + de-splinter + deluxe-merge run live (~980 duplicates removed, 814 files consolidated, 59 pairs merged). A scoped 459-track catalog-fill of absent albums landed; the full absent-*and*-partial fill (~3,400 tracks) is built and dry-tested but not run. `curate` mode is dry-tested end-to-end (10 stages, 758 s, exit 0). Cover-fill is validated on one album and wired into every mode. Open items in §8.

---

## 2. Architecture

MK13 adds **no new layer**. It adds standalone curation scripts *beside* the pipeline, plus one orchestrator seam:

```
GUI button  ─▶  cli_runner.py <mode>  ─▶  SpiralOrchestrator.run()
                                             │  Rev 1–3 (existing: dedup/download/tag/audit)
                                             ▼  post-revolution:
        _run_tool("...", script)  ─▶  subprocess: python <script> [--execute | --dry-run]
             catalog_fill_scoped · mislabel_redownload · walkman_desplinter
             walkman_deluxe_merge · walkman_fav_upgrade · walkman_cover_fill
             + cli_runner {artist-completeness-report, scan-duplicates, build-favorites-playlists-plan}
             + walkman_playlist_import
                                             ▼
                        adb_transfer.py / mtp_transfer.py  (unchanged transport)
                                             ▼
                                      Sony NW-ZX507
```

**Why subprocess** (same reason as MK12's `GUI_SUBPROCESS_PIPELINE`): each tool does a full device scan + thousands of ADB round-trips; running them in the long-lived orchestrator compounds handle/COM leakage. `_run_tool` streams the child's stdout into the log pane, honors `self._dry_run` (append `--execute` on real runs, `--dry-run` where supported, else the script's own dry default) and `self._cancel`.

**The favorited/playlisted pin, as a cross-cutting concern.** Every tool that deletes or moves a file loads `walkman_dedup._load_favplaylist_protected_paths()` (mtime-cached; reads `favorites_playlists_plan.json`) and skips any path in it. Enforced in: `audit.py` (4 sites), `walkman_dedup.py` deep-clean, `cli_runner.py` deep-clean `--from-log`, and each of the 6 new tools. Cover-fill is exempt — it rewrites a file *in place* at the same path.

---

## 3. Playlists & Favorites — the no-root solution

### 3.1 The constraint

`com.sony.walkman.highresmediaplayer` keeps its own SQLite `media.db` behind a **non-exported** `PlayerMediaProvider`. `is_favorites` is a column on `tracks`; playlists are a `playlists` table + a `playlist_tracks` junction. No root (`su` absent), app not debuggable (`run-as` refused), direct DB read denied. The only PC-side levers: drop a file the scanner picks up, or simulate a UI tap.

### 3.2 Playlists — `.m3u8` import

`MediaScannerService` **does** import a well-formed `.m3u8` on a normal rescan. It never found one because `adb_transfer.write_text_file_to_device` had a contract mismatch with `copy_file_to_device` (REV 14.8: "`dest_parts` is a folder; filename from `src.name`") — it had been silently creating a *directory* `<playlist>.m3u8/` with a random-named tempfile inside, since at least 2026-04-30.

- **Fix:** write to a proper local temp file named `filename`, then `copy_file_to_device(tmp, dest_parts, …)`.
- **`walkman_playlist_import.py`:** reads `favorites_playlists_plan.json`, writes one `<sanitized name>.m3u8` per playlist to `Music/` on the SD card; each line is `<parts[1:]>/<filename>` (drop the leading `Music`), relative to the m3u8's location.
- **Recipe:** regenerate the plan → run the import → trigger one "Update database" rescan. Idempotent. All 33 import with exact track-count match.

### 3.3 Favorites — `walkman_favplaylist_apply.py` (UI automation)

`adb shell input tap` + `uiautomator dump`, no root. Per track: type the title (minus any `(feat. …)` suffix) into the Library-top search box → tap the "song titles" count-chip at (120, 340) → dump the rows → match against the plan's `(artist, title)` → tap the row → the star toggle is a **fixed tap at (648, 848)** on the Now-Playing screen (no accessibility tree — screenshot + fixed coords is the only option).

Bugs hit and fixed: `uiautomator dump` fails ~⅓ of calls and leaves a stale file (rm the remote path first, retry on "dumped to"); `subprocess.run(env={"MSYS_NO_PATHCONV": "1"})` replaces the whole environment and breaks `adb` (must be `{**os.environ, …}`); **the star is a toggle** — a naive re-run un-favorites (check-before-tap + per-item state checkpointing); **a real wrong-artist bug** — `find_best_row`'s tier-3 fallback matched bare-title *substring* with no artist check and had starred 4 different-artist tracks (Steve Lacy "Only If" → Janet Jackson "If", etc.). Now strict: exact-normalized-title **and** artist-overlap.

### 3.4 The fuzzy re-star (MK13)

The strict matcher's precision-over-recall cost is a ~30–40 % NO-MATCH rate. MK13 added a **tier-2 fuzzy** to `find_best_row` (`_norm_fuzzy` strips parentheticals + trailing " - <edition>" tails; `_title_close` accepts exact / prefix within 3 chars / `SequenceMatcher ≥ 0.90` with a min-length guard) **plus multi-variant search queries** (full / punctuation-stripped / first-3 / first-2 words). **Artist overlap stays mandatory** — that is the load-bearing guard against §3.3 and is not relaxed. Recovered ~30 of 49 residual misses; the last ~19 are device search-index dead spots for short single-word titles ("Gbona", "500", "Wolves").

---

## 4. The audit incident

### 4.1 What happened

`cli_runner.py audit` runs, post-batch, four folder-restructuring passes. The batch phase's canonical-move code (`_canonical_dest_parts` → push to `Music/<Artist>/<Album>/`, delete original) already had a pin: *if this file's path is in `_load_favplaylist_protected_paths()`, force the destination back to the current path.* **The other three passes did not.**

An audit run moved ~900 non-pinned splinter files to canonical paths and pinned 224. That is correct *for those 224*. But `_consolidate_duplicate_folders` — which groups folders by their **last path component only** (so two different albums that share a title merge) and had no pin — then began collapsing the `Artist/Album/` structure the batch phase built and merging good folders *into* a pre-existing `Music/Music/` nested-bug tree (~1,668 folders). The ADB daemon also died mid-run. The pass was killed after 51 merges; its source-folder delete threw before running (calling `.Items()` on an ADB path string), so those 51 were file *copies*, not moves — **no data lost**. Playlist M3U8s and the `is_favorites` rows for the 900 batch-phase moves *were* invalidated, and were recovered by the §7 recipe.

### 4.2 The fix

`audit.py` applies the pin at **all four** move sites: batch Reorg (pre-existing); `_cleanup_all_mixed_folders` (was deleting protected dupes); `_consolidate_duplicate_folders` (skip a source folder if any file in it is protected; **never** pick a `Music/Music/…` path as canonical; skip a group where every candidate is nested there); the backtrack Reorg.

`_consolidate_duplicate_folders`'s folder-name-only grouping (still merges distinct same-titled albums by different artists) is a known residual — it is why `curate` uses `walkman_desplinter.py` (filename-keyed) for de-splintering instead of a full `audit`.

---

## 5. The curation tools

Standalone (`python <tool>.py`), dry-run by default, `--execute` to act, favorited/playlisted-pin-aware, resumable where they delete/move.

| Tool | Job | Method | Validated |
|---|---|---|---|
| `walkman_desplinter.py` | Collapse `Music/<Album>` + `Music/<Artist>/<Album>` + `Music/Music/<Album>` → one `Music/<Artist>/<Album>/`. | Group by `(norm_artist, norm_album)` from the **filename**. `adb mv` (instant, same-fs). Collisions left for dedup. Cross-storage skipped. | Live: 664 + 150 moves across two runs; 881 favs pinned. |
| `walkman_deluxe_merge.py` | Merge `Album` + `Album (Deluxe/Extended/Expanded)` pairs; renumber against the most-complete Deezer edition. | `album_completeness` Deezer fetch; dedup within (FLAC > MP3 > M4A); PIN protected. Unmatched titles keep their number. | Live: 59 groups; JID/*The Forever Story* verified. |
| `walkman_fav_upgrade.py` | Clear duplicate pairs in favorited/playlisted albums (plain dedup skips them). | For a pair whose delete-target is protected and keep-target is a real upgrade (FLAC-over-non-FLAC / `inferior_metadata`): **delete the inferior, no file moves**, queue star favorites (`fav_upgrade_restar.json`). | Live: 105 deletes, 89 re-star queued. |
| `catalog_fill_scoped.py` | Download tracks missing from a followed artist's albums — **absent and partial**. | `artist_completeness_report.json` + a per-track fuzzy device re-check; PARTIAL fills first, ABSENT only for deep artists (saved ≥ 2 or liked ≥ 15, expected ≥ 6); `--max-queue` cap; `--absent-only`. | Live: absent-only pass — 459/461 downloaded, 28 albums. Full pass (~3,400) dry-tested. |
| `mislabel_redownload.py` | Replace a wrong-content file (`mislabel_redownload_pending.json`). | Locate by `norm(artist+title)`; size-check the OVERSIZED bucket; delete (the downloader skips present tracks); re-feed all pending; verify + report "deleted but didn't come back". | Live: 11 deleted, 264 re-fetched; 1 unrecoverable (`Duckwrth – SOPRANO`). |
| `walkman_cover_fill.py` | Embed album art into files that have none (the player shows **embedded** art only). | One Deezer `cover_big` per album → `cover_art.normalize_bytes` → embed **in place** → push back. Records art-present files so repeat runs are cheap. | Live: "Denzel Curry – ii" — 6 covers, verified rendering after rescan. |
| `walkman_year_backfill.py` | Fill the release-year tag on files that have none (~30 % of the library). | One Deezer `/album` lookup per album for `release_date` → write the 4-digit year into `TDRC` / `date` **in place**. Resumable. | Dry-run validated. |
| `walkman_storage_rebalance.py` | Move whole album folders SD → internal toward a capacity-proportional music split (~99 %/1 % → ~82 %/18 %). | On-device `adb shell cp` across the mount boundary + size-verify + `rm` source (no PC round-trip); rollback on verify fail. Orders by fewest playlist tracks first (favorites survive a move; M3U8 lines don't — favsync regenerates them). | Dry-run: 309 folders / ~28 GB planned. |
| `walkman_incompleteness_scan.py` | Read-only 5-axis audit → `incompleteness_report.json` + `outputs/reports/incompleteness.html`. | cover/tag sample, catalog gaps, playlist gaps, folder splinters, storage split. | Run 2026-09-06 (§8 open-items table). |

---

## 6. The completeness matcher

### 6.1 The problem

`cli_runner.py artist-completeness-report` fetches each followed artist's Deezer discography + tracklists, then marks a track "missing" if it isn't on the device. The original check was `(norm_album, norm_title) ∈ dev_track_set`. At ~8,000 files that missed a large minority of real matches — and **every miss inflates `missing_count`**, which would have `catalog_fill_scoped` re-download present tracks.

### 6.2 Rejected

Routing through `device_library.DeviceLibrary.has_track` (album-folder-scoped). This **regressed**: `_find_best_entry` picks *one* folder by match score, and a split album (`Music/Because the Internet/` with 14 files + `Music/Childish Gambino/Because the Internet/` with 11) has several — a 19/19 album read as 6/19. Reverted; `contains_title` is unchanged.

### 6.3 Shipped

A **flat** `norm_album → {fuzzy_norm_title}` index that unions *every* folder mapping to an album name, matched with `device_library._fuzzy_title_in` (new helper: strip parentheticals + trailing " - <edition>"; accept exact / prefix within 3 / `SequenceMatcher ≥ 0.90` with a ≥ 5-char guard; the prefix rule also requires the shorter string be ≥ 4 chars or all-alpha so "500"/"5000" doesn't pass). Result: *Because the Internet* 19/19 (fixed), *Flower Boy* 14/14, ~75 "complete" albums (was ~50). Still undercounts heavily-split, heavily-retagged big albums (*Donda*, *Starboy*, *ASTROWORLD*) — the folder-split problem; `walkman_desplinter.py` is the lever.

### 6.4 The report

`catalog_completeness.html` (also an Artifact): 44 followed artists (30 deep), 455 studio albums — **80 complete / 180 partial / 195 absent**, ~4,245 missing tracks. **Broad but shallow** — core albums by top-followed artists sit at 20–60 % present.

---

## 7. The recovery recipe (repeatable)

After any pass that moves or deletes device files:

1. `python cli_runner.py build-favorites-playlists-plan` — re-resolve every Liked / playlist track to its current path.
2. `python walkman_playlist_import.py` — rewrite all 33 `.m3u8` files.
3. Trigger one "Update database" rescan (Library-top refresh icon at (558, 118) → YES at (585, 742)). ~5–8 min.
4. For **deleted**-file favorites only (moved ones keep their star — §1 finding 2): `WALKMAN_FAVPLAYLIST_PLAN=<queue>.json python walkman_favplaylist_apply.py favorites`.

Post-all-work this session: favorites 2,007 / 2,270 resolvable, playlists 2,850 / 3,214 — both up from the mid-incident 1,966 / 2,667.

---

## 8. Open items

| Item | State |
|---|---|
| Full catalog-fill (absent + partial, ~3,400 tracks) | Built, dry-tested, **not run**. Expect ~2–3 h + dedup churn. |
| `Duckwrth – SOPRANO` | Wrong-content file deleted; no provider had a clean copy. Manual source needed. |
| ~19 favorites re-star misses | Device search index returns nothing for short single-word titles. Root SQL or manual taps. |
| ~128 lossy-vs-lossy dupes in favorited albums | `fav_upgrade` only takes FLAC/metadata wins. |
| `_consolidate_duplicate_folders` folder-name grouping | Still merges distinct same-titled albums. Fix: folder-key = `(norm_artist, norm_album)` from file tags. |
| Long-title canonical filenames | ~24 push-fails + ~42 write-errors per audit run — path-length limit on very long `feat.` titles. Non-destructive. Fix: truncate or skip. |
| `walkman_reorganize.py` has NO favplaylist pin | `curate` avoids it; `reorganize` mode still calls it. Add the pin, or drop it in favour of desplinter + deluxe-merge. |
| Completeness matcher on split/retagged big albums | Undercounts. Mitigate: run `walkman_desplinter.py` before `artist-completeness-report`. |
| Cover art — 47% of files have none (~3,516) | `walkman_cover_fill.py` + the `covers` stage. Built, wired; not yet run library-wide (~1 h first pass). |
| Year tag — 30% missing (~2,213) | **Built** — `walkman_year_backfill.py` + the `year_backfill` stage. Not yet run library-wide. |
| Storage — music 98.8% SD / 1.2% internal | **Built** — `walkman_storage_rebalance.py` + the `storage_rebalance` stage. Dry-run: 309 folders / ~28 GB toward an 82/18 split. Not yet run. |

---

## 9. File-by-file change index (MK13 session)

**New:** `walkman_desplinter.py`, `walkman_deluxe_merge.py`, `walkman_fav_upgrade.py`, `catalog_fill_scoped.py`, `mislabel_redownload.py`, `walkman_cover_fill.py`, `walkman_year_backfill.py`, `walkman_storage_rebalance.py`, `walkman_incompleteness_scan.py`.

**Modified:**
- `audit.py` — favorited/playlisted pin at all 4 move sites; `Music/Music/` guard in `_consolidate_duplicate_folders`.
- `spiral_orchestrator.py` — `_STAGE_MATRIX`: new `curate` mode + tool-stages appended to `full`; `covers` + `year_backfill` on every device mode; `storage_rebalance` on `reorganize` / `curate` / `full` (before `favsync`). New `_run_tool()` subprocess helper. Post-revolution stage calls.
- `cli_runner.py` — `curate` in `MODES` + usage; `artist-completeness-report` rebuilt on a flat, folder-unioning fuzzy album index.
- `device_library.py` — new `_fuzzy_norm_title` / `_fuzzy_title_in` helpers (`contains_title` itself unchanged).
- `walkman_favplaylist_apply.py` — tier-2 fuzzy in `find_best_row` (artist-overlap still mandatory); multi-variant search queries; `WALKMAN_FAVPLAYLIST_PLAN` env override + separate state file.
- `walkman_dedup.py` — `_strip_feat` also strips `(with …)`; favplaylist-protection helpers + mtime cache.
- `adb_transfer.py` — `write_text_file_to_device` folder-not-file fix.
- `walkman_playlist_import.py` — `.m3u8` writer from the plan (built at the MK12/MK13 boundary).
- `main.py` — `🧹 CURATE LIBRARY` button, `_start_curate` / `_curate_thread`, button-state wiring, `FG_GREEN`.
- `config.py` — `PRIMARY_DOWNLOAD_PROVIDER = "streamrip"`, `DOWNLOAD_PROVIDERS["streamrip"] = True`.
- `~/AppData/Roaming/streamrip/config.toml` — Deezer ARL (credential; MP3-128 due to a streamrip 2.1.0 free-account bug).
