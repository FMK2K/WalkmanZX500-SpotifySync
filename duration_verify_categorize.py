"""
duration_verify_categorize.py — sort duration_verify_report.json into
likely-cause buckets for human review.

A raw duration mismatch alone doesn't say WHY -- confirmed live this
session that the same signal covers several very different real causes:
  - genuine wrong-song swap (Brent Faiyaz "Rolling Stone" was actually
    The Weeknd's track of the same title -- a real download-time mismatch)
  - boundary-trim/segue artifact (Kendrick Lamar "These Walls" on To Pimp A
    Butterfly reads 248.7s vs a 301s reference, but BOTH on-device copies
    are byte-identical and confirmed via full pull -- consistent with an
    imprecise rip cut on an album known for tracks segueing together, not
    a different song)
  - legitimate alternate version (Marvin Gaye "Mercy Mercy Me (The Ecology)
    (Mono Single Version)" on a 50th-anniversary deluxe reissue)
  - Deezer reference-data quirks (wrong edition matched, or Deezer's own
    duration metadata off for a lesser-known release)

Heuristics used (none are certain -- this triages for review, it does not
delete or auto-fix anything):
  - ALBUM-LEVEL: 2+ suspects in the same (artist,album) -- more likely the
    wrong Deezer edition got matched for the whole album than N independent
    per-track swaps.
  - ALTERNATE VERSION: matched title contains a version-marker keyword
    (live, remix, acoustic, demo, instrumental, radio edit, single version,
    extended, mono, clean, explicit, edit, alternate, bonus, remaster,
    deluxe, interlude, skit).
  - HIGH-CONFIDENCE WRONG TRACK: local/reference ratio < 0.5 or > 2.0 --
    too large a gap to explain as a trim; closest signature to the
    confirmed Rolling Stone case (local was 68s off a 162s reference, a
    huge relative gap: ~1.42x).
  - POSSIBLE TRIM/SEGUE: local is moderately shorter (ratio 0.5-0.92) and
    none of the above apply.
  - REVIEW: everything else (local moderately longer than reference,
    ratio 1.08-2.0, no other signal).

Usage: python duration_verify_categorize.py
Reads duration_verify_report.json, writes duration_verify_categorized.json
and prints a summary.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
REPORT_PATH = ROOT / "duration_verify_report.json"
OUT_PATH = ROOT / "duration_verify_categorized.json"
PENDING_PATH = ROOT / "mislabel_redownload_pending.json"

# categories worth re-downloading. ALBUM-LEVEL is a reference-match artifact
# (wrong Deezer edition), ALTERNATE VERSION is usually a correct-but-different
# edit, REVIEW is noise — none of those get queued by default.
_ACTIONABLE = {
    "OVERSIZED FILE (likely a full mix/compilation, not a single track)",
    "HIGH-CONFIDENCE WRONG TRACK (large magnitude mismatch)",
}
_ACTIONABLE_TRIM = _ACTIONABLE | {"POSSIBLE TRIM/SEGUE (moderate shortfall)"}

_VERSION_KEYWORDS = (
    "live", "remix", "acoustic", "demo", "instrumental", "radio edit",
    "single version", "extended", "mono", "clean", "explicit", " edit",
    "alternate", "bonus", "remaster", "deluxe", "interlude", "skit",
    "unplugged", "session", "reprise",
)


def categorize(entry: dict, album_suspect_count: int) -> str:
    ratio = entry["local_duration_s"] / entry["reference_duration_s"] if entry["reference_duration_s"] else 1.0
    # 2026-09-04: ratio > 5 was ground-truth verified against the REAL remote
    # file size (not just the header-derived duration) for two independent
    # cases -- FM Skyline "Veranda" (local=2618s, real file 104.8MB) and
    # Boldy James "S.N.O.R.T." (local=2370s, real file 94.9MB) -- both
    # genuinely oversized files (a full mix/compilation downloaded as a
    # single mislabeled track), not a reference-mismatch or calculation
    # artifact. This is the single most reliable signal here, so it takes
    # priority over album-clustering: an album with several independently
    # oversized tracks should read as several OVERSIZED-FILE hits, not get
    # relabeled as one wrong-edition-matched album.
    # Absolute ceiling as a second trigger for the same category: a long
    # reference baseline (e.g. Thriller-era tracks with 12" extended mixes
    # already running 4-5 min) can dilute the ratio for a genuinely
    # oversized file below 5x even though the raw duration is still
    # implausible for a single track (Michael Jackson "Billie Jean" showed
    # 821.9s / 13:41 at ratio 2.81 -- caught here, missed by ratio alone).
    if ratio > 5.0 or entry["local_duration_s"] > 600:
        return "OVERSIZED FILE (likely a full mix/compilation, not a single track)"
    if album_suspect_count >= 2:
        return "ALBUM-LEVEL (likely wrong Deezer edition matched)"
    title_lc = entry["title"].lower()
    if any(kw in title_lc for kw in _VERSION_KEYWORDS):
        return "ALTERNATE VERSION (title suggests a different edit)"
    if ratio < 0.5 or ratio > 2.0:
        return "HIGH-CONFIDENCE WRONG TRACK (large magnitude mismatch)"
    if 0.5 <= ratio < 0.92:
        return "POSSIBLE TRIM/SEGUE (moderate shortfall)"
    return "REVIEW (moderate mismatch, no other signal)"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-pending", action="store_true",
                    help="also emit mislabel_redownload_pending.json (OVERSIZED + "
                         "HIGH-CONFIDENCE WRONG TRACK) for mislabel_redownload.py")
    ap.add_argument("--include-trim", action="store_true",
                    help="with --write-pending, also queue POSSIBLE TRIM/SEGUE")
    args = ap.parse_args()
    if not REPORT_PATH.exists():
        print(f"No report found at {REPORT_PATH}")
        # not fatal for a pipeline run — nothing flagged means nothing to do
        if args.write_pending:
            PENDING_PATH.write_text("[]", encoding="utf-8")
        return 0
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if not report:
        print("Report is empty -- nothing to categorize.")
        return 0

    album_counts: dict = defaultdict(int)
    for e in report:
        album_counts[(e["artist"], e["album"])] += 1

    buckets: dict = defaultdict(list)
    for e in report:
        cat = categorize(e, album_counts[(e["artist"], e["album"])])
        e["category"] = cat
        e["ratio"] = round(e["local_duration_s"] / e["reference_duration_s"], 2) if e["reference_duration_s"] else None
        buckets[cat].append(e)

    OUT_PATH.write_text(json.dumps(dict(buckets), ensure_ascii=False, indent=1), encoding="utf-8")

    if args.write_pending:
        want = _ACTIONABLE_TRIM if args.include_trim else _ACTIONABLE
        pend, seen = [], set()
        for cat in want:
            for e in buckets.get(cat, []):
                k = (e["artist"].strip().lower(), e["title"].strip().lower(),
                     e["album"].strip().lower())
                if k in seen:
                    continue
                seen.add(k)
                pend.append({"artist": e["artist"], "title": e["title"],
                             "album": e["album"], "reason": cat})
        PENDING_PATH.write_text(json.dumps(pend, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {PENDING_PATH.name}: {len(pend)} track(s) queued for re-download "
              f"({'incl trim/segue' if args.include_trim else 'oversized + wrong-track only'})")

    order = [
        "OVERSIZED FILE (likely a full mix/compilation, not a single track)",
        "HIGH-CONFIDENCE WRONG TRACK (large magnitude mismatch)",
        "ALBUM-LEVEL (likely wrong Deezer edition matched)",
        "ALTERNATE VERSION (title suggests a different edit)",
        "POSSIBLE TRIM/SEGUE (moderate shortfall)",
        "REVIEW (moderate mismatch, no other signal)",
    ]
    print(f"Total suspects: {len(report)}\n")
    for cat in order:
        items = buckets.get(cat, [])
        print(f"=== {cat}: {len(items)} ===")
        for e in sorted(items, key=lambda x: -x["diff_s"])[:15]:
            print(f"  {e['artist']} -- {e['album']} -- {e['title']}: "
                  f"local={e['local_duration_s']}s ref={e['reference_duration_s']}s "
                  f"ratio={e['ratio']}")
        if len(items) > 15:
            print(f"  ... and {len(items) - 15} more")
        print()
    print(f"Full categorized data -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
