# Master Fix Plan — Run 3 (`____Output_1____.txt`) + New Dedup Findings

This is a planning document only — nothing in the codebase has been touched. It (1) validates this run's log against the priority-1/2 fixes from the original audit, (2) reviews the newly-pasted dedup root-cause analysis and Fix A–D plan against source and the log, refining one root-cause explanation that was close but not quite right, and (3) folds everything into one prioritized implementation plan.

---

## 1. What this new run confirms: Priority 1 and 2 from the original audit are still live, and Priority 2's scope is bigger than first written

The run ends with a real summary block this time (not truncated):

```
Total scanned    : 25880
Already correct  : 0
Moved            : 0
Skipped (tags)   : 254
Errors           : 716
```

**Zero files moved.** 716 errors, all the same two-stage pattern from the original report: `[Transport/bulk] ADB failed ([WinError 2]...)` (707 occurrences) → fall back to COM/PowerShell → `[PS] Failed (rc=1): SRC_ITEM_NOT_FOUND` / `Shell could not resolve source` (552 occurrences). This confirms the `_find_adb()` candidate-path fix and the `safe_path`/`dest_filename` fix from the original report have not yet been applied — they remain the single highest-leverage thing to fix, more so than anything new in this run.

**Correction to the original report's framing of the `safe_path` bug.** The original writeup characterized this as biting "almost all on filenames containing diacritics or typographic punctuation." This run shows that's too narrow — plain-ASCII titles fail identically:

```
[Move] Upload failed for "Don Toliver - Life of a DON - 17 Swangin' On Westheimer.mp3": Shell could not resolve source...
[Move] Upload failed for "Viktor Vaughn - Vaudeville Villain - 01 Lickupon.mp3": Shell could not resolve source...
```

No accented characters, no curly punctuation — these just fail the same way. The actual trigger isn't "the filename needed ASCII sanitization," it's **"`dest_filename` differs from `src_filename` at all.`** `walkman_reorganize.py` builds `safe_path = tmp_dir / dest_filename` — the *canonical, reorganized* name — while the file that was actually pulled to disk sits at `tmp_dir / src_filename`. Since reorganize's entire purpose is renaming files from whatever they were called into the canonical `Artist - Album - NN Title.ext` convention, `dest_filename != src_filename` is the **normal case**, not an edge case. That's why this run shows 0 successful moves: essentially every file that needs reorganizing hits this. Worth re-stating in the team's tracker so the fix isn't scoped as a Unicode-handling patch — it needs to handle the general case.

No code changes recommended beyond what the original report already specified for this bug; the fix (`safe_path` should reference `pulled`, the file that actually exists, not `dest_filename`) already covers the general case correctly. This section is purely a severity/framing correction.

---

## 2. Review of the new dedup root-cause analysis

### Root cause 1 (duplicate dedup candidates) — confirmed, but the *mechanism* needs a precise correction

The submitted analysis guessed: *"self.storages contains overlapping entries (e.g. 'Internal shared storage' and 'Internal storage' both resolving to /storage/emulated/0)."*

That's not what's happening, and `config.py` is explicit about why — it has a comment block specifically warning against this exact mistake:

```python
# These are NOT the same as "Internal Storage" or "Memory Card" — those
# older names were wrong and caused 100% of transfer failures in early runs.

STORAGE_ROOTS: list[tuple[str, str]] = [
    ("Internal shared storage", ""),           # root of internal storage
    ("Internal shared storage", "Music"),
    ("Internal shared storage", "Downloads"),
    ("SD card",                 ""),           # root of SD card
    ("SD card",                 "Music"),
    ("SD card",                 "Downloads"),
]
```

There are genuinely only two storage *names* here — the "6 storage root(s)" in the log is 2 storages × 3 subfolder entries each, by design, so files sitting directly at the storage root, in `Music/`, or in `Downloads/` all get picked up. That part is intentional and correct.

**The actual bug** is in how the ADB-fallback scan turns each `(storage, subdir)` pair into a `find` command:

```python
scan_root = posixpath.join(mount, subdir) if subdir else mount
...
out = subprocess.run([_adb_exe, "shell", f"find '{scan_root}' -type f"], ...)
```

For `subdir=""`, `scan_root` is the bare storage mount (e.g. `/storage/emulated/0`), and `find` from there is **recursive** — it walks into `Music/` and `Downloads/` too. Then the separate `subdir="Music"` and `subdir="Downloads"` entries run `find` again, directly on those same subfolders. So for "Internal shared storage" alone, any file actually sitting in `Music/` or `Downloads/` gets enumerated **twice**: once by the root-level recursive `find`, once by the dedicated subfolder `find`. Same for SD card. Two physical storages × this double-counting on the Music/Downloads portion of each = the systematic duplication observed.

This distinction matters for two reasons:

1. It tells you precisely *which* files double — anything under `Music/` or `Downloads/` on either storage, not an arbitrary subset.
2. It means the fix can be applied at either of two levels: post-hoc deduplication of the merged file list (Fix A/B, as proposed — simpler, more robust, doesn't need to reason about find-command overlap), or root-cause prevention by making the root-level (`subdir=""`) `find` non-recursive or explicitly excluding `Music/`/`Downloads/` from it when those are separately listed. **Recommend doing Fix A/B as written** — it's correct regardless of which scan-root combination produced the duplicate, and it's far less fragile than trying to keep three independent `find` commands mutually exclusive by construction.

**Quantified against this run's actual data**, to scope the real impact precisely: the doubling is isolated to the **pass-1 (zero-length/small-file) candidates only**.

```
"small_file" WOULD DELETE lines:        64 total → 32 unique   (exactly 2× — fully explained)
all other categories (pass-2, format_upgrade/track_renumber/truncation_dupe/other): 3,532 total → 3,532 unique  (zero duplication)
```

So in this run, the bug currently affects 32 real corrupt/zero-length files (listed as 64), not the 3,532 pass-2 candidates. That's good news for current blast radius, but it also means pass-2's apparent immunity is likely incidental (it probably groups candidates by a normalized title/artist key before iterating the raw file list, which happens to absorb the duplication) rather than a deliberate safeguard — Fix B (dedup `plan.candidates` before execute) is exactly the right defense-in-depth for the day pass-2's logic changes and loses that incidental protection.

### Root cause 2 (DEL_-prefixed files re-entering scan) — confirmed

`DEL_1776470470_BROCKHAMPTON - SATURATION II - 01 GUMMY.mp3.mp3` is visible in this run's small_file WOULD DELETE list (and, per the duplication bug above, listed twice). The double `.mp3.mp3` extension suggests the soft-delete rename logic itself has a separate, smaller issue worth a one-line look — appending `.mp3` to a filename that already ends in `.mp3` rather than inserting the `DEL_<timestamp>_` prefix before the existing name. Worth confirming whether this is cosmetic (doesn't affect the exclusion fix) or symptomatic of the rename logic doing something slightly wrong beyond just needing a prefix filter.

### Root cause 3 (LIVE_DEVICE_REQUIRED downgrade) — confirmed as intentional, and this run shows exactly the trigger condition

```
[DeviceInventory] ADB command timed out: devices
[DeviceInventory] ADB not available (timed out) — Enable USB Debugging on the Walkman and retry.
[Dedup/Gate] LIVE_DEVICE_REQUIRED — device inventory is stale or ADB was not connected at preflight. Downgrading hard-delete to report-only...
```

This confirms the analysis: the gate did exactly what it's designed to do, in response to `adb devices` timing out at startup — not a code bug. The same underlying ADB-availability problem (§1) is the root condition; fixing the `_find_adb()` candidate list should also reduce how often this gate fires unnecessarily, since a working `adb.exe` resolution would make the startup `adb devices` probe far more likely to succeed before timing out.

### Root cause 4 (truncated log, unknown failure) — now answered by this run

This run's log is complete and ends in a clean summary block rather than truncating mid-output. So root cause 4 isn't "there might be a hidden crash" — there isn't one in this run; the pipeline runs to completion and reports its own failure honestly (`Moved: 0, Errors: 716`). The earlier truncation in the first uploaded log was almost certainly just a copy/paste or terminal-buffer length limit, not evidence of an unhandled exception. Nothing to fix here.

---

## 3. Status check on Fixes A–D as described

The pasted transcript reports Fixes A, B, C were already present in the user's live file, and Fix D was just added. I can't verify exact line numbers against my own extracted copy of `walkman_dedup.py` (it's a slightly earlier snapshot — `flagged_idents`, the `.deleted` exclusion, and the ADB-fallback scan loop all exist in my copy but at different line numbers and without the Fix A/B/D code blocks shown in the transcript, confirming the live file has moved on since the zip was uploaded). I have no reason to doubt the transcript's account, and the logic in all four fixes matches the confirmed root causes:

- **Fix A** (dedup `adb_files` by `(storage, parts, filename)` after the scan loop) — correctly targets the root cause described in §2 above, regardless of whether the mechanism is "two overlapping labels" (the original guess) or "nested recursive scan roots" (the corrected mechanism) — the fix operates on the final file identity either way.
- **Fix B** (dedup `plan.candidates` before execute) — correct defense-in-depth; given pass-2's apparent immunity is likely incidental rather than designed (§2), this is genuinely useful, not redundant.
- **Fix C / Fix D** (skip `DEL_`-prefixed filenames in the ADB scan and the COM scan respectively) — both correct; a prefix-based filename check is the right guard since the existing exclusion only looked at path *parts* (i.e. a `.deleted/` folder component), which doesn't catch a `DEL_`-prefixed file sitting in its original folder rather than moved into a trash folder.

**One thing worth double-checking once you're back in the live file:** confirm Fix D was added to *every* place files get filtered in the COM scan path, not just the one block after the `.deleted` folder exclusion the transcript pointed at. If `walkman_dedup.py`'s COM scan has more than one filtering point (e.g. a separate filter inside the small-file pass vs. the format-dominance pass), a single insertion point could miss a second one. Worth a quick second grep for `.deleted` or `startswith` filename checks across the whole file before considering this closed.

---

## 4. Consolidated implementation plan

Ordered by what actually blocks progress right now, based on this run's real numbers (0 moved / 716 errors) rather than abstract severity.

### Tier 1 — blocking every transfer (do these first; nothing else matters until these land)

1. **Fix `_find_adb()`'s candidate list** (`adb_transfer.py`) to include `tools/platform-tools/adb.exe`. One-line addition. Confirmed still unfixed — 707 ADB failures in this run alone.
2. **Fix `safe_path` in `walkman_reorganize.py`** to reference the actual pulled file (`pulled`) rather than `dest_filename`. Confirmed still unfixed, and confirmed (this section, §1) to be the *general* case for any renamed file, not a Unicode edge case — this single fix should turn most of the 552 "Shell could not resolve source" errors into successful moves.

These two together should take "Moved: 0 / Errors: 716" to something close to "Moved: ~25,000 / Errors: near 0" on the next run, assuming no other blocking issue is hiding behind them. Recommend running the pipeline once with *only* these two fixes applied before touching anything else, specifically so the next log cleanly shows whatever failure mode is left, without it being masked by these two dominant ones.

### Tier 2 — dedup correctness (apply once Tier 1 is confirmed fixed, so the dedup pass is operating on a library that's actually finished transferring)

3. **Fix A** — dedup `adb_files` by `(storage, parts, filename)` after the ADB-fallback scan loop. Per the transcript, already in the live file.
4. **Fix B** — dedup `plan.candidates` before the execute loop. Per the transcript, already in the live file.
5. **Fix C** — skip `DEL_`-prefixed filenames in the ADB scan loop. Per the transcript, already in the live file.
6. **Fix D** — skip `DEL_`-prefixed filenames in the COM scan filter block. Per the transcript, just added. **Action item:** grep the full file for any other filename-filtering point in the COM path that might need the same one-liner — don't assume there's only one.

### Tier 3 — smaller, non-blocking items surfaced in this review

7. Investigate the double-extension issue in the soft-delete rename (`...GUMMY.mp3.mp3`) — likely a prefix-insertion bug rather than a true `.mp3` duplication, low urgency but cheap to check while already in this code.
8. No action needed on `LIVE_DEVICE_REQUIRED` (§2, root cause 3) or the "truncated log" concern (§2, root cause 4) — both confirmed as working-as-intended / non-issues respectively. Fixing Tier 1's `_find_adb()` should incidentally reduce how often the `LIVE_DEVICE_REQUIRED` gate fires, as a side benefit rather than a direct fix.

### Carried over from the original audit, still open, not touched by this run's findings

These were already documented and remain unaddressed; this run doesn't change their priority relative to each other, only confirms #1 and #2 above are still the dominant blockers ahead of all of them:

- `mtp_transfer_com.py:_wait_for_file()` missing `settling_delay_s` parameter (fires at both the dedup soft-delete path and `pull_file_from_device()`).
- `walkman_tag_scan.py`'s bare `"adb"` call in `adb_pull_head()` (same bug class as the original audit's Fix A, third occurrence).
- Four-then-five inconsistent audio-extension whitelists; DSD (`.dsf`/`.dff`) missing from `walkman_reorganize.py`'s extension recognition and from `FORMAT_TIER`'s dedup ranking.
- Composer field never populated upstream; `totaltracks`/`totaldiscs` not representable via EasyID3 for MP3 as originally proposed (works for FLAC's freeform Vorbis comments only).
- `check_duplicates()` reading MediaStore artist/title instead of the more accurate tag snapshot.
- No backfill pass for cover-art normalization or ID3v2.3 downgrade across the existing library — only new content going forward.
