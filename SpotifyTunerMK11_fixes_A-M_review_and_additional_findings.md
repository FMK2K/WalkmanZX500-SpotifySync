# Addendum: Review of Proposed Fixes A–M + Additional Findings

Each proposed fix was checked directly against source and, where computable, against the actual `walkman_audit_report.json` / `walkman_tag_snapshot.json` / `walkman_snapshot.json` data — not taken on description alone. Three items needed a correction; three new issues turned up that the original list didn't cover.

---

## Confirmed exactly as described

**A.** `snapshot_cross_check.py:56` — `_adb()` is exactly `subprocess.run(["adb"] + args, ...)`, no candidate probing. Confirmed.

**F.** `parse_track_number()` (`walkman_reorganize.py:176-184`) handles `"01-06"` (regex `^\d+-(\d+)$`) and `"N/Total"` (`.split("/")[0]`), but has no branch for `int(s) > 1000` compound encoding. Confirmed, fix as described is correct.

**H.** `_norm()` (line 36-41) strips every non-alphanumeric character before comparison — `unicodedata.normalize("NFKD", ...).encode("ascii","ignore")` → `re.sub(r"[^a-z0-9 ]", "", ...)`. No token preservation of any kind. Confirmed.

**J.** Zero references to `cover_art` or `normalize_bytes` anywhere in `walkman_reorganize.py` — confirmed by direct grep, not just absence of a call site I happened to miss.

**K.** `check_duplicates(snapshot: list[dict])` — confirmed, single parameter, reads `t.get("artist")` / `t.get("title")` directly from the MediaStore-sourced `snapshot` list. `tag_snap` is never passed in.

**M.** `downloads_ingest.py:44` — `_AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav")`. Confirmed as the fifth list, 8 extensions, same gaps (no aiff/ape/wv/dsf/dff) as `export_stager.py` and `walkman_reorganize.py`.

---

## Confirmed, with a sharper number than the original estimate

**G.** `FORMAT_TIER` (line 26-29) is exactly `{".flac": 90, ".m4a": 82, ".aac": 80, ".ogg": 75, ".opus": 74, ".wav": 70, ".mp3": 50, ".wma": 20}` — no `.dsf`/`.dff` entry, so they'd fall to tier 0 via `.get(ext, 0)`.

I checked whether this is a dormant risk or an active one: **it's active.** Of the 3,973 duplicate sets in the current report, 2,551 (64%) span more than one file extension — meaning `FORMAT_TIER` is genuinely the tiebreaker deciding which copy survives in roughly two-thirds of all duplicate resolutions today, not a theoretical concern that only matters once DSD shows up. The library doesn't currently contain any `.dsf`/`.dff` files (extension breakdown below), so the specific DSD-loses-to-MP3 scenario hasn't fired yet — but the mechanism that would cause it is already doing exactly this kind of cross-format arbitration at scale, on every run.

**L.** Computed the actual overlap rather than estimating it:

| | id3v4_blank_in_player (64) ∩ |
|---|---|
| oversized_apic_gt90kb (2,577) | **0** |
| progressive_jpeg (659) | **0** |
| png_cover_rejected (10) | **0** |
| no_embedded_art (8,068) | **64 (100%)** |

Every one of the 64 ID3v2.4 stragglers also has zero embedded art — not bad art, none at all. That's a clean answer to the question: a single repair pass on those 64 files needs to (1) call `update_to_v23()` and (2) embed art from scratch, not normalize existing art. They're two different remediation actions but land on the identical 64-file set, so they can run atomically as you suggested.

Worth surfacing alongside this: the four art-issue categories overlap with each other enough that summing them is misleading. The true count of tracks with *any* cover-art problem (missing, oversized, progressive, or PNG) is **10,694 of 13,528 — 79% of the library** — noticeably worse than any single figure in the summary table suggests in isolation.

---

## Needed a correction

**E.** This one doesn't work as described. I installed mutagen and tested it directly:

```python
>>> EasyID3()["totaltracks"] = ["12"]
EasyID3KeyError: "'totaltracks' is not a valid key"
```

`EasyID3.valid_keys` has no `totaltracks` or `totaldiscs` entry — only `tracknumber` and `discnumber` exist, and `TRCK`/`TPOS` are each a single text field (`MultiSpec`, one string) with no separate "total" sub-field at the frame level. The `"N/Total"` string the codebase deliberately strips *is* the standard's only mechanism for expressing a total in ID3v2.3 — there's no alternate key to redirect it to.

The asymmetry matters: this constraint is **ID3/MP3-specific**. FLAC's Vorbis comments are freeform key-value pairs, so `audio["tracktotal"] = str(total)` (or `disctotal`) is perfectly valid for the 1,250 FLAC files in the library — mutagen's `FLAC` object doesn't reject arbitrary keys the way `EasyID3` does. So a real version of this fix would be: write `tracktotal`/`disctotal` as freeform Vorbis comments for FLAC, and for MP3 either (a) accept that total-tracks can only travel via the same `"N/Total"` string in `tracknumber` that's currently being stripped — which puts you back against the "ZX500 doesn't parse it cleanly" finding that motivated the stripping in the first place — or (b) write it as a non-standard `TXXX:TOTALTRACKS` user frame, which `MediaMetadataRetriever` (and therefore the SDD's `MediaScannerService`) has no defined mapping for and would simply never read into `TrackInfo.totalTracks`. There isn't a clean fix here for the 8,885 MP3s without first re-testing whether the native player actually mishandles the slash format, or living with `totalTracks` staying `-1` for MP3.

**D.** The `_EASY_KEYS` change is correct and TCOM is genuinely ID3v2.3-native (confirmed — `TCOM` is a standard frame, not a v2.4 addition, so it survives `update_to_v23()` fine). But the fix is necessary, not sufficient: I grepped `spotify_client.py`, `downloader.py`, `walkman_reorganize.py`, `ai_bulk_metadata.py`, and `context_builder.py` for "composer" and found **zero** references anywhere upstream. Nothing in this codebase currently extracts composer data from Spotify (whose standard track-catalog API doesn't reliably expose a composer field outside classical-specific endpoints anyway) or from any other source into the canonical `tags` dict that `write_zx500_tags()` consumes. Adding the `_EASY_KEYS` entry means: any file whose composer tag already existed in the original source file before this pipeline touched it will survive untouched (the write loop only sets keys present in `tags`, never clears unlisted ones) — but no new composer data will ever get *populated* for files that arrive without it, since there's no source feeding the field. The one-line fix is real and worth doing, but by itself it's closer to "stop accidentally being safe by omission" than "start populating a new field."

---

## Three further findings, not in the original A–M list

**N. `walkman_tag_scan.py` has the exact same bare-`"adb"` bug as fix A — in the script that produces the ground-truth tag snapshot itself.**

`adb_pull_head()` (line ~44) calls `subprocess.run(["adb", "exec-out", ...])` directly — not `adb_transfer._ADB_EXE`, and not even `snapshot_cross_check.py`'s own `_adb()` helper. This is a third independent occurrence of the unresolved-ADB-path bug, and it's the most consequential one: if it fires, `walkman_tag_scan.py` can't pull any file heads at all, and `walkman_tag_snapshot.json` — the file every other check in this pipeline treats as ground truth over the staler MediaStore data — would come up empty or stale. (It clearly *did* succeed for the run that produced the current snapshot — `adb` must have been resolvable in whatever environment that scan ran in — but nothing in the code guarantees that going forward, for the same reason fix A exists.)

**O. `walkman_tag_scan.py`'s `_PARSERS` dict only covers 5 of the 18 SDD-relevant extensions — a structural blind spot that happens to be dormant today, not eliminated.**

```python
_PARSERS = {".mp3": _parse_mp3, ".flac": _parse_flac, ".m4a": _parse_m4a,
            ".mp4": _parse_m4a, ".aac": _parse_m4a}
```

Anything else — `.wav`, `.aiff`/`.aif`, `.ape`, `.wv`, `.wma`, `.ogg`, `.opus`, and critically `.dsf`/`.dff` — falls through to `{"issues": ["unsupported format: .ext"], "has_apic": False, "id3_version": None, "tag_genre": None}`. No title, artist, album, track, or disc gets captured at all for those formats.

I checked how much this actually bites against the current library:

```
Extension distribution (13,528 tracks):
  8885  .mp3
  3392  .m4a
  1250  .flac
     1  .webm
```

Right now: not at all — the library happens to be pure MP3/M4A/FLAC, all five of which fall inside (or are aliased to) the supported parsers, so only the single `.webm` file gets an "unsupported format" record. But this is exactly the same shape of risk as fix G: the moment a `.dsf`/`.dff`/`.wav`/`.aiff` file is added — which, again, is the literal point of a Hi-Res device — every check downstream of `walkman_tag_snapshot.json` (`check_hires_player_issues`, `check_genre_mismatches`, `check_missing_art_by_album`, all of it) goes blind for that file with no error surfaced anywhere except a generic "unsupported format" string nobody currently inspects in aggregate. Fix C (exposing `mutagen`'s `.info.sample_rate` etc.) should be implemented inside parser functions for *all* of the SDD's 18 extensions, not just added to the existing 5.

As a related, smaller note: the one `.webm` file currently on-device (`Ary Kumar - TEMPT FATE - 01 FUNK OUT MY BRAIN.webm`, presumably a YouTube pull that never got transcoded) is invisible to all five extension whitelists across the codebase, to `walkman_tag_scan.py`'s parser dict, *and* to the SDD's own `MediaFile.Format` enum — a three-way blind spot for a single stray file that's worth a transcode-on-ingest check in `downloads_ingest.py` rather than a new format branch.

**P. `walkman_tag_scan.py` only pulls the first 512 KB of each file before parsing — uncomfortably close to confirmed real APIC sizes.**

```python
PULL_KB = 512   # covers all text frames + ≤90 KB APIC per cover_art.py limits
```

The comment's assumption is the *intended* 90 KB cap from `cover_art.normalize_bytes()` — but §6 of the original report (and the `oversized_apic_gt90kb` list itself) already shows real embedded art on this device at 178.7 KB and 199.0 KB, more than double that assumption. For files with both an oversized cover and a non-trivial run of text frames (comments, lyrics, multiple `TXXX` entries), a 512 KB partial pull risks truncating mid-frame, which `mutagen` would most likely surface as a generic parse exception rather than a clean signal. There's no truncation detection or retry-with-larger-pull logic visible — worth adding a check that flags (and retries) when the declared APIC frame length plus header offset exceeds what was actually pulled, rather than risking a silent or miscategorized parse failure.

---

## Updated priority list

Folding the corrections and new items into the original ranking:

| Fix | Status | Note |
|---|---|---|
| C | Confirmed, scope expanded | Should cover all 18 SDD extensions inside `_PARSERS`, not just the current 5 |
| G | Confirmed, actively exercised | Already deciding 64% of today's duplicate sets, not dormant |
| A | Confirmed | — |
| **N** | **New** | Same bug as A, third occurrence, in the script that produces ground truth |
| I | Confirmed (carried over from original audit) | — |
| F | Confirmed | — |
| H | Confirmed | — |
| **O** | **New** | `_PARSERS`'s 5-extension coverage vs. SDD's 18 — dormant today, will silently blind the scanner the moment a non-MP3/FLAC/M4A Hi-Res file is added |
| D | Confirmed, needs a second half | `_EASY_KEYS` entry alone is a no-op without an upstream composer source |
| E | **Needs rework** | `totaltracks`/`totaldiscs` aren't valid EasyID3 keys — works for FLAC (Vorbis comments), not for the 8,885 MP3s as literally described |
| J | Confirmed | — |
| **P** | **New, lower urgency** | 512 KB pull window vs. confirmed 199 KB+ real APIC sizes; add truncation detection |
| M | Confirmed | — |
| L | Answered | 64/64 overlap with no-art, 0/64 with oversized/progressive/PNG; 79% library-wide art-issue rate once unioned |
| K | Confirmed | — |
