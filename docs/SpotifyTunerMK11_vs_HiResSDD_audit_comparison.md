# SpotifyTunerMK11 Audit Comparison Report

**Scope:** Compares (a) what `SpotifyTunerMK11` currently *returns* (the `____Output____.txt` run log and `walkman_audit_report.json`), against (b) what the codebase's own logic should *produce* if it ran correctly, against (c) what the `HighResMediaPlayerApp` Software Design Document says the device *expects to receive*.

**Sources reviewed:** `____Output____.txt` (25,985 lines), `walkman_audit_report.json` (14.2 MB), `walkman_tag_snapshot.json` (8.3 MB, 13,528 entries), `walkman_snapshot.json` (7.4 MB, 13,528 entries), and the full `SpotifyTunerMK11` Python codebase (`audit.py`, `walkman_reorganize.py`, `walkman_dedup.py`, `adb_transfer.py`, `mtp_transfer_com.py`, `id3_helpers.py`, `cover_art.py`, `device_inventory.py`, `config.py`, `snapshot_cross_check.py`, and supporting modules), cross-referenced against the previously delivered `readme_SDD.docx` / Java reconstruction of `HighResMediaPlayerApp`.

No files in `SpotifyTunerMK11` were modified during this review.

---

## 0. The headline finding: the audit is watching the wrong database

Every check in `snapshot_cross_check.py` (the script that produces `walkman_audit_report.json`) is built on two inputs:

- `walkman_snapshot.json` — populated by querying `content://media/external/audio/media`, `.../albums`, `.../genres`, `.../playlists` (confirmed in `walkman_tree_gen.py` and `snapshot_cross_check.py`).
- `walkman_tag_snapshot.json` — populated by `walkman_tag_scan.py` reading raw ID3/FLAC/MP4 tags directly off each file with `mutagen`.

`content://media/external/audio/*` is **Android's generic, OS-level MediaStore** — not Sony's proprietary provider. Per the SDD, `HighResMediaPlayerApp` reads from its own `PlayerMediaProvider` at authority `com.sony.walkman.highresmediaplayer.playback.mediadb.provider`, backed by a 7-table SQLite schema (`media.db` v7) with 35+ columns on the `tracks` table alone — `samplingrate`, `bitspersample`, `bitrate`, `channel`, `codec`, `format`, `is_hires`, `effective_artist_id`, `effective_album_id`, `crossfade_fade_in_start_time`, `crossfade_fade_out_end_time`, `media_id`, `is_favorites`, `start_time`.

The generic MediaStore schema SpotifyTunerMK11 actually queries carries none of those columns. This is confirmed structurally: both `walkman_snapshot.json` entries and the `_EASY_KEYS` / tag-scan schemas only ever contain `title, artist, album, album_artist, year, track, disc, genre, duration, mime_type, ext` — never sample rate, bit depth, codec, channel count, or `is_hires`.

**Consequence:** SpotifyTunerMK11 has no way to verify, report on, or even ask the question "is this file actually Hi-Res, and will the device correctly flag it as such?" — despite the device's entire product premise being Hi-Res playback. Every other gap below is best understood as a downstream symptom of auditing one layer removed from the actual app database the SDD describes.

| | Current output | Expected from code | SDD-desired |
|---|---|---|---|
| Sample rate / bit depth / codec visibility | Absent from all 3 JSON artifacts and the log | N/A — never queried, never logged | `TrackInfo.samplingRate`, `bitsPerSample`, `codec`, `format` populated per file; `is_hires` derived from them |
| Composer | Absent from `_EASY_KEYS`, tag snapshot, and MediaStore snapshot schemas | Never written, never read, never audited | `TrackInfo.composerName` / `composer_id`, `ComposerInfo` browse table |
| Favourites / crossfade / start_time | Not modeled anywhere in SpotifyTunerMK11 | N/A | `is_favorites`, `crossfade_fade_in_start_time`, `crossfade_fade_out_end_time`, `start_time` columns on `tracks` |

---

## 1. Transport layer: two independent, compounding bugs

### 1.1 ADB executable is never found

`adb_transfer.py:_find_adb()` probes five candidate paths for `adb.exe`:

```
"adb"                                                  (PATH)
<adb_transfer.py's own folder>/adb.exe                 (project root)
C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe
C:\Program Files\Android\android-sdk\platform-tools\adb.exe
~\AppData\Local\Android\Sdk\platform-tools\adb.exe
```

The zip bundles `adb.exe` at `SpotifyTunerMK11/tools/platform-tools/adb.exe` — **none of the five candidates point there.** All five fail, `_find_adb()` falls back to the bare string `"adb"`, and every subsequent ADB call raises `[WinError 2] The system cannot find the file specified`.

- **Current output:** 242 `[Transport/bulk] ADB failed ([WinError 2]...); falling back to COM` lines — i.e. ADB transfer never once succeeds in this run.
- **Expected from code:** Exactly this. The candidate list was simply never updated when `adb.exe` was relocated into `tools/platform-tools/`.
- **SDD-desired:** Not directly an SDD concern (ADB is a host-side transport detail, not part of the on-device app), but it forces every transfer through the COM/PowerShell fallback, which has its own defect (§1.2) and which is the path that ultimately determines what `MediaScannerService` sees on next scan.

### 1.2 The COM/PowerShell fallback uploads a file that doesn't exist

In `walkman_reorganize.py`, the upload sequence is:

```python
pulled = tmp_dir / src_filename          # the file that actually exists on disk
...
safe_path = tmp_dir / dest_filename      # ASCII-cleaned name — comment claims
                                          # "adb_transfer handles ASCII-safety"
...
mtp.copy_file_to_device(src=safe_path, ...)   # passes the NONEXISTENT path
```

The comment block (MK17 Patch 6) explains that a previous local rename step was *deliberately removed* on the theory that `adb_transfer.py`'s internal ASCII-safe copy (confirmed present at `adb_transfer.py:804-813`, which correctly copies *from* the real source *to* a sanitized temp name) makes the local rename redundant. That's true for the ADB push path — but `safe_path` is also handed to the **COM/PowerShell** path inside `mtp.copy_file_to_device`, which has no equivalent internal rename. Since §1.1 means ADB never runs, every single upload in this log goes through COM/PowerShell with a `src` argument pointing at a file that was never created.

- **Current output:** 260 `[ERROR] [Move] Upload failed` lines, almost all on filenames containing diacritics or typographic punctuation (Í, ñ, ó, curly apostrophes, em dashes) — e.g. `'Kali Uchis - ORQUÍDEAS - 08 Tu Corazón Es Mío.mp3'` → attempted move to `'...ORQUIDEAS - 08 Tu Corazon Es Mio.mp3'`, "Shell could not resolve source." Plus 103 `[PS] Failed (rc=1): SRC_ITEM_NOT_FOUND` lines and a final `ERROR LOOP DETECTED` that aborts the entire run.
- **Expected from code:** Identical to observed — this is a deterministic bug, not a flaky one. Any filename requiring ASCII sanitization will fail to upload whenever ADB is unavailable.
- **SDD-desired:** Irrelevant to the device once a file fails to transfer — `MediaScannerService` simply never sees it, so none of `TrackInfo`'s 35+ fields get populated for that track at all. The SDD's metadata pipeline assumes the file *arrives*; this bug means a measurable fraction of the library (every non-ASCII title) never reaches that pipeline in a given run.

### 1.3 Dedup soft-delete: a `NameError` repeated 3,598 times

`mtp_transfer_com.py:_wait_for_file()` (used by the dedup soft-delete path) contains:

```python
def _wait_for_file(folder, filename: str, timeout: int = COPY_TIMEOUT_S) -> bool:
    ...
    if settling_delay_s > 0:        # <-- settling_delay_s is not a parameter
        _t_settle.sleep(settling_delay_s)   # of THIS function
```

`settling_delay_s` is a parameter only of the sibling function `_wait_for_stable_size()` (default `1.5`), whose docstring and comment block were apparently copy-pasted into `_wait_for_file()` without the parameter itself. Every call to `_wait_for_file()` raises `NameError: name 'settling_delay_s' is not defined`.

- **Current output:** `[Dedup/soft] COM move failed: name 'settling_delay_s' is not defined` × 3,598 (and a matching `[Dedup]` line per failure — 3,598 + 3,598 in the warning tally).
- **Expected from code:** 100% failure rate for every soft-delete dedup move that takes this path — confirmed exactly.
- **SDD-desired:** N/A directly, but the practical effect is that the `dedup_pending: 3589` figure in `walkman_audit_report.json` (duplicates the dedup planner already identified but never actually removed) is essentially fully explained by this one-line bug, not by any difficulty identifying duplicates.

---

## 2. Track-number integrity: a long fallback chain that mostly doesn't fire, plus real tag corruption

`walkman_reorganize.py` has a five-stage fallback for missing track numbers: tag → sibling Spotify-folder index → filename regex → AI inference → **folder-position fallback** (sort siblings alphabetically, assign 1-based rank). The final stage is explicitly designed so "every track gets a unique, stable, plausible track number even when no other source has it."

- **Current output:** Only 19 `[PositionFallback]` successes in the whole run, vs. 192 `SKIP (track=00)` files dropped entirely (never transferred, never reach `MediaScannerService`). That's roughly a 9%-success / 91%-failure ratio for the safety net the code comments describe as a guaranteed catch-all.
- **Expected from code:** The fallback only succeeds when `self._folder_groups` already has a populated sibling-index entry for that exact `(storage_name, folder_parts)` key. For folders the sibling index never indexed (most of the 192 skips), `grp` resolves to `{}`, `_pos` stays `None`, and the file is dropped with no further fallback. The 9% success rate is consistent with that being the common case rather than the exception.
- **SDD-desired:** `TrackInfo.trackNumber` defaults to `-1` ("unknown") when the column is absent — the Java model tolerates a missing value gracefully. But a *skipped* file never reaches the device at all, which is strictly worse than an unknown-but-present track: the SDD's whole metadata pipeline (and the user's library) never gets the chance to see it.

**Separately — confirmed tag corruption, not just missing data:**

```json
{"path": ".../Childish Gambino - _Awaken, My Love!_ - 01 Baby Boy.mp3",
 "diffs": {"track": {"on_device": "1", "expected": "9"}}}
{"path": ".../Childish Gambino - _Awaken, My Love!_ - 01 Boogieman.mp3",
 "diffs": {"track": {"on_device": "1", "expected": "3"}}}
{"path": ".../Childish Gambino - _Awaken, My Love!_ - 01 California.mp3",
 "diffs": {"track": {"on_device": "1", "expected": "7"}}}
```

Three different songs from the same album, all carrying both a **filename** prefix of `01` *and* an embedded ID3 tag of `track=1`. And separately, `track_number_issues` includes a literal `"compound track number 1000"` case the script itself flags as "likely disc 1 track 0 (ZX500 may display incorrectly)" — i.e. a `disc*1000+track` encoding that was never decomposed back into separate `disc`/`track` fields before being saved.

- **SDD-desired:** `MainActivity`'s `CursorLoader` sorts album track lists with `ORDER BY disc ASC, track ASC`. Any album where every track tag reads `track=1` will display in an arbitrary (likely insertion/alphabetical) order in the actual player, not the artist's intended sequence — this is a real, demonstrable user-facing playback-order defect, not a cosmetic one.

---

## 3. The same source tracks are present multiple times under different naming conventions

Sampling `walkman_audit_report.json`'s `missing_art_albums` and `duplicates` sections surfaces something the summary counters don't make obvious on their own:

- **"The College Dropout"** (Kanye West) reports `total_tracks: 51` for an album that has 21 real songs — the same 21 tracks appear under at least three filename conventions side by side in the same folder (`NN Artist - Title.flac`, `Artist - Album - NN Title.flac`, and bare `Artist - Title.flac`).
- **"Flower Boy"** (Tyler, The Creator) reports `total_tracks: 56` for a ~14-track album — the same songs recur as both `.m4a` and `.mp3`, several with the double-feat artifact `walkman_filename_sanitizer.py` was specifically built to strip (`"Foreword (feat. Rex Orange County) (feat. Rex Orange County)"`) still present on-device.

This is consistent with `duplicate_sets: 3973` / `files_to_delete: 6329` in the report — a very large fraction of the library appears to be self-inflicted re-processing residue from earlier pipeline revisions (the codebase's own `Rev`/`MK` numbering goes past 39), not organic "same song downloaded from two services" duplication. Combined with the dedup soft-delete bug in §1.3, none of this residue is currently being cleared.

- **SDD-desired:** `MediaScannerService` will happily index all of these as distinct tracks (different `absolute_path`, so different `_id` rows) — the Java model has no mechanism of its own to detect that three files are "the same song." `AlbumInfo.getTrackCount()` will report an inflated count, and the album browse view will show duplicate entries rather than 21 (or 14) clean tracks. This is purely an upstream library-hygiene problem; the device app can't fix it after the fact.

---

## 4. Tag schema gaps relative to `TrackInfo`

`id3_helpers.py:_EASY_KEYS` is the single canonical map every tag-write in the codebase routes through:

```python
_EASY_KEYS = {
    "title": "title", "artist": "artist", "album": "album",
    "album_artist": "albumartist", "release_year": "date",
    "track_number": "tracknumber", "disc_number": "discnumber",
    "genre": "genre",
}
```

| TrackInfo field (SDD) | Written by SpotifyTunerMK11? | Notes |
|---|---|---|
| title, artist, albumArtistName, albumName, genreName | Yes | Core 5, consistently written |
| trackNumber, discNumber | Yes, but **bare integer only** | `downloader.py:_clean_num()` explicitly strips any `"N/Total"` suffix — comment says "the ZX500 does not parse it cleanly" |
| **totalTracks, totalDiscs** | **Never written** | Direct consequence of the stripping above — these `TrackInfo` fields will be `-1` for every file this pipeline touches |
| **composerName** | **Never read, written, or audited anywhere** in the codebase | Zero references outside an internal MK38/39 "tag write gate" allow-list that never gets populated for this field |
| samplingRate, bitsPerSample, bitrate, channel, codec, format, isHires | **Not modeled at all** | See §0 |
| isVbr | Not modeled | SDD documents this as a JNI-only (`SpMetaParser`) field never exposed via Android's MediaStore either, so this gap is structurally expected, not a bug |

---

## 5. A genuine, hard-won device quirk the SDD didn't know about — and one it should now incorporate

`id3_helpers.py`'s docstring documents an empirical finding: **the actual Sony Walkman music app on this device ignores ID3v2.4 frames outright** — tags appear blank in the on-device browser even when written correctly, unless the file is saved as ID3v2.3 with `update_to_v23()` called first (otherwise `TDRC`/`TIPL`/`TMCL`/`TSOP`/`TSOA` silently vanish on save). This is invisible from APK decompilation alone — it's the kind of thing only discoverable by writing real files and watching the real device. It's strong, independent corroboration of the SDD's `SpMetaParser`/JNI layer being a stricter, more idiosyncratic parser than Android's stock `MediaMetadataRetriever`.

The fix is intentionally centralized — but it isn't 100% deployed:

- **Current output (`walkman_audit_report.json`):** `id3v4_blank_in_player: 64` — 64 tracks on the actual device are still ID3v2.4 and will show blank metadata in the real player despite the fix existing in code. `no_id3_header: 2` — two files have no ID3 header at all (e.g. `AMApiano #1.mp3`, visible at the very top of the run log with every field empty except a genre value from a separate AI pass).
- **Expected from code:** The fix only applies to files that pass through `write_zx500_tags()` / `save_easy_mp3()` / `save_v23_id3()`. Files ingested before this module existed, or via any code path that doesn't call through it, are never retroactively migrated. 64 + 2 = 66 confirmed escapees is consistent with "fix exists, no backfill pass was ever run."
- **SDD recommendation:** Document the ID3v2.3-only constraint explicitly in the SDD's metadata-pipeline section as a known hardware/firmware limitation, not an assumption that any v2.3/v2.4-tagged file will display identically. Anyone building or validating against `TrackInfo`/`MediaScannerService` should treat "tag exists and parses" and "tag will render in the actual player" as two different claims.

---

## 6. Cover art: a well-designed fix, only half-deployed

`cover_art.py:normalize_bytes()` is a genuinely solid piece of engineering — flattens PNG alpha to white (the ZX500 firmware renders transparency as black), forces baseline (non-progressive) JPEG, caps the long edge, and iteratively drops JPEG quality until under a byte cap. It's correctly wired into `downloader.py` (new downloads) and `audit.py` (the tag-fix pass). It is **not** wired into any backfill across the existing on-device library.

- **Current output:** `tracks_no_art: 8068` (60% of the 13,528-track library has no embedded art at all — e.g. the entire 51-file "College Dropout" duplication mess), `albums_all_art_missing: 1375`, `oversized_apic_gt90kb: 2577`, `progressive_jpeg: 659`, `png_cover_rejected: 10`.
- **Expected from code:** Exactly what's observed — this is new-content-only protection with no retroactive sweep, applied against a library that was largely populated before the fix existed.
- **SDD-desired:** `CoverArtLoader` decodes whatever `SpMetaParser`/the native cover-art extraction returns via `content://…/coverart/{id}`. The SDD doesn't document any client-side downsampling before `BitmapFactory.decodeStream()` — if the embedded JPEG really is e.g. 199 KB at full resolution, that's exactly what gets decoded into memory for every list-row request, multiplied across 2,577 files. This is consistent with treating oversized/legacy embedded art as a real performance risk for `CoverArtLoader`'s `LruCache`, not just a cosmetic browser-lag complaint on the old firmware.

---

## 7. Data provenance and genre coverage

- **`pipeline_orphans: 10237`** out of `snapshot_total: 13528` — **75.7%** of everything currently on the device has no entry in `.metadata_cache.json`, the tool's own record of what it processed. Three quarters of the library arrived by some path the tool doesn't have provenance for (manual copies, pre-tool imports, or imports from a now-missing cache). This severely limits how much of `tag_drifts` (3,101) and `mislabeled` (341) — both of which require a cache entry to compare against — can be checked at all; the true mismatch rate against the *un-orphaned* quarter of the library is necessarily higher than the raw percentages suggest.
- **`genre_distribution`:** `2549 (unset)` + `1844 Unknown` = 4,393 tracks, **32.5%** of the library, with no usable genre. The SDD's `GenreInfo`/genre-browse view (`content://…/genres`) will show roughly a third of the library bucketed into one or two near-empty/junk buckets rather than meaningfully distributed.

---

## 8. Format-extension whitelist is inconsistent across the codebase, and disagrees with the SDD on DSD support

Four separate hand-maintained extension lists exist, and they disagree with each other:

| Module | Extensions recognized |
|---|---|
| `device_inventory.py` / `walkman_sd_migrate.py` | mp3, flac, m4a, aac, ogg, opus, wav, aiff, aif, ape, wv, wma, **dsf, dff** |
| `export_stager.py` | mp3, flac, m4a, aac, ogg, opus, wav, wma — **no aiff/ape/wv/dsf/dff** |
| `walkman_reorganize.py` (`TYPE_TO_EXT`) | mp3, flac, aac, m4a, ogg, opus, wav, wma, aiff — **no ape/wv/dsf/dff** |
| SDD `MediaFile.Format` (18 extensions incl. mp3, aac, m4a, mp4, flac, wav, aiff, aif, ape, wma, ogg, oga, **dsf, dff, mqa**, opus, tak, wv) | Full superset, DSD and MQA included as first-class Hi-Res formats |

`walkman_reorganize.py` is the module that actually moves, tags, and transfers files onto the device — and it's the one missing DSD (`.dsf`/`.dff`) entirely from its own extension recognition (`TYPE_TO_EXT`). Any `.dsf`/`.dff` file in the source library will not be recognized as audio by the very pipeline responsible for getting it onto a device whose flagship feature, per the SDD, is exactly that kind of Hi-Res format. `export_stager.py` has the same gap plus also drops AIFF/APE/WavPack.

---

## Summary table: current → expected → desired

| Area | Currently returns | Code-expected (root cause confirmed) | SDD wants |
|---|---|---|---|
| Provider queried | Generic Android MediaStore (`content://media/external/audio/*`) | Same — never wired to Sony's provider | Sony `PlayerMediaProvider` (35+ column `tracks` table) |
| Hi-Res fields | None present in any of the 3 JSON artifacts | Never queried/logged anywhere | `samplingRate`, `bitsPerSample`, `bitrate`, `channel`, `codec`, `format`, `is_hires` |
| ADB transfer | 242/242 attempts fail (`WinError 2`) | `_find_adb()` candidate list missing `tools/platform-tools/adb.exe` | N/A (host-side), but blocks files from ever reaching `MediaScannerService` |
| COM/PS fallback, non-ASCII names | 260 `[Move] Upload failed`; final run-aborting dialog loop | `safe_path` built from `dest_filename` instead of the real pulled file | N/A directly; files silently never appear in the library |
| Dedup soft-delete | 3,598 identical `NameError` failures | `settling_delay_s` undefined in `_wait_for_file()` | N/A; explains `dedup_pending: 3589` almost entirely |
| Track numbers | 192 files dropped (`SKIP track=00`); 19 successful folder-position rescues; confirmed `track=1` collisions across distinct songs in one album | Folder-position fallback only fires when sibling index already has an entry — true ~9% of the time it's needed | `disc ASC, track ASC` ordering in `MainActivity`; corrupted tags break intended play order |
| Duplicate inflation | `duplicate_sets: 3973`, one album reporting 51 tracks for a 21-track release | Same source files re-saved under 3 naming conventions across pipeline revisions, never deduped (blocked by the bug above) | `AlbumInfo.trackCount` will reflect the inflated, duplicated count |
| Composer / total_tracks / total_discs | Never written anywhere | Deliberately stripped (`_clean_num`) / never in `_EASY_KEYS` | First-class `TrackInfo` fields, left permanently `-1`/blank |
| ID3 version | 64 v2.4 + 2 no-header tracks confirmed still on device | Fix exists (`update_to_v23()`), no backfill ever run | Empirically confirmed real-device quirk now worth documenting in the SDD itself |
| Cover art | 8,068 tracks with no art; 2,577 oversized; 659 progressive JPEG | `normalize_bytes()` correct but wired only into download/audit paths, no backfill | `CoverArtLoader`/`BitmapFactory` decodes whatever's embedded, no documented client-side downsizing |
| Provenance / genre | 75.7% of library untracked by `.metadata_cache.json`; 32.5% genre unset/Unknown | Most of the library predates or bypassed the tracked pipeline | `GenreInfo` browse view will show a third of the library as one junk bucket |
| Extension support | 3 inconsistent whitelists; `walkman_reorganize.py` (the transfer path) excludes DSD entirely | Hand-maintained, never reconciled against each other | `MediaFile.Format` treats DSD/MQA as first-class Hi-Res formats |

---

## Suggested priority order for fixes

1. **Add `tools/platform-tools/adb.exe` as a candidate path in `_find_adb()`.** One-line fix, removes the dependency on the broken COM fallback for the vast majority of transfers.
2. **Fix `safe_path` in `walkman_reorganize.py` to reference the actual pulled file** (`pulled`), not `dest_filename`. Together with #1, this should eliminate nearly all 260 `[Move] Upload failed` errors and the run-aborting dialog loop.
3. **Add the missing `settling_delay_s` parameter to `_wait_for_file()`.** Unblocks the entire dedup soft-delete path (3,598 affected calls).
4. **Reconcile the four audio-extension whitelists into one shared constant**, and add `.dsf`/`.dff` to `walkman_reorganize.py` and `export_stager.py` so DSD files are actually handled by the transfer path.
5. **Run a one-time backfill** through `cover_art.normalize_bytes()` and `id3_helpers.write_zx500_tags()`/`update_to_v23()` across the existing on-device library, not just new content going forward.
6. **Point `snapshot_cross_check.py` (or a new variant) at Sony's `PlayerMediaProvider`**, if the device exposes it to `content query` the same way MediaStore does, so the audit can finally verify `is_hires`, codec, and sample-rate/bit-depth — the one category of check this tool cannot currently perform at all.
7. **Investigate the track-number corruption** (`track=1` across distinct songs; the `1000`-compound-value case) before relying on tag data for play-order — this looks like a logic bug in a save path, not bad source data.
