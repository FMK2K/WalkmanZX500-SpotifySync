# Deep-Clean Data-Loss Exposure Audit — 2026-07-27

**Trigger**: found and fixed a bug where `_stat_device_file()`'s ADB `stat` call passed
device paths unquoted, causing `adb shell` to word-split any path containing a space.
This audit reviews `.dedup_log.json` for past "corrupt, no backup kept" deletions that
may have been misclassified by this bug before it was fixed.

## Method

Scanned all 33 records in `.dedup_log.json`. Filtered for candidates with
`reason` in `("zero_length", "small_file")` and `superior_path == "(corrupt — no
replacement needed)"` (i.e. no backup was kept) whose device path contains a space,
parenthesis, or other shell metacharacter that would trigger the word-splitting bug.

## Result

- 1956 total no-backup "corrupt" candidates across all 33 scans (most are dry-run
  previews from `scan-duplicates`, not actual deletions).
- **89 of those came from REAL delete runs** (`dry_run: false`) — 2 runs on
  2026-05-03 and 2026-05-04, both `delete_mode="hard"` (**permanent** device delete,
  not moved to trash).
- Collapsing near-duplicate entries between the two runs (same track re-flagged both
  days): **45 unique tracks were permanently deleted from the Walkman with no backup.**
- **The reported "size" for 44 of these 45 tracks is exactly 4096 bytes** — not a
  random small number, which is what genuine corruption/truncation would produce.
  4096 is the classic signature of `stat` resolving to a *directory* instead of the
  file (a very plausible outcome of word-splitting a multi-word path apart). Real
  music files reporting an identical, suspiciously round size across dozens of
  unrelated albums is strong circumstantial evidence these were **false positives**,
  not actually corrupt.
- One outlier: `J. Cole - The Off-Season - 07 i n t e r l u d e.mp3` reported 17 bytes,
  not 4096 — worth separate scrutiny, may be a genuinely different failure mode.
- Two further real deletions were `delete_mode="soft"` (2026-05-12, 2026-06-20) —
  these were moved to trash, not permanently deleted, so likely still recoverable
  directly on the device (look for `DEL_<timestamp>_<original filename>` on the SD
  card / internal storage).

## Affected tracks (permanent, hard-delete, no backup, size≈4096B)

- Akon — Trouble (Deluxe Edition): Ghetto, Pot Of Gold, Show Out, Lonely, Journey,
  Locked Up - Remix
- Alikiba — CINDERELLA: Cinderella
- BROCKHAMPTON — SATURATION II: JELLO, TEETH, SWAMP, SCENE, TOKYO, JESUS, CHICK,
  JUNKY, SWEET, GAMBA, SUNNY, SUMMER, GUMMY (DEL_-prefixed, likely already a
  soft-deleted dupe before this hard-delete pass)
- BROCKHAMPTON — SATURATION III: TEAM
- Big K.R.I.T — Return of 4Eva: Highs and Lows
- Dadju — Poison Ou Antidote: Bobo au cœur
- Donny Hathaway — Extension of a Man: Love, Love, Love
- EJ Jones — Gas Station Love: Gas Station Love
- J. Cole — The Off-Season: "i n t e r l u d e" (17B, see note above),
  "9 5 . s o u t h", "a p p l y i n g . p r e s s u r e"
- JACKBOYS — JACKBOYS: JACKBOYS
- Kairo Keyz — float 2.0: float 2.0
- Michael Jackson — Off The Wall: Don't Stop 'Til You Get Enough, Off the Wall
- Noah Wise — Sunshine: A Song for Grosmami
- Radiohead — KID A MNESIA: Everything In Its Right Place
- Rick Ross — City Of God: Magneto
- Sonder Son — Sonder Son: All I Want
- System Of A Down — Hypnotize: Lonely Day (appears twice, two different folder
  layouts — "System Of A Down - Hypnotize/" and "System Of A Down/Hypnotize/")
- Unknown Artist — Aquemini: Hold On, Be Strong
- Unknown Artist — Room 25: no name
- WurlD — My WorlD With U: PRESS
- architecture in tokyo — Summer Paradise: SURF ~ FOREVER (mp3 and m4a variants)
- denn — nervous: nervous

(Full raw entries, including exact paths/timestamps, are in `.dedup_log.json` records
timestamped `2026-05-03 14:56:56` and `2026-05-04 17:22:01`.)

## Recommended next step

This tool syncs *from* a local Spotify-download library *to* the Walkman — the
deletions above only touched the on-device copies via ADB, not (as far as I can tell
from this audit) the PC-side source library. **These tracks are very likely still
present in the local source library and can simply be re-synced**, rather than lost
permanently. Worth confirming the source files still exist locally before assuming
anything is unrecoverable, and then re-running a normal sync for these specific
albums/tracks once confirmed.
