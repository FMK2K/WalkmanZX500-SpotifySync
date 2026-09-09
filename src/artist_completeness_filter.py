"""
artist_completeness_filter.py — cut artist_completeness_report.json down to
the actionable core-album gaps.

The raw report (from `cli_runner.py artist-completeness-report`) fetches
every Deezer release for each followed artist -- album, EP, single, and all
the noise a prolific catalog accumulates: instrumental editions, remix
singles, "+ / (Deluxe) / (Expanded)" re-releases, chopped-and-screwed
versions, soundtrack tie-ins. Flagging all of those as "incomplete" buries
the real signal (a genuine studio album the user follows the artist for,
sitting on the device only half-present). This filters to that signal.

Rules (all heuristic -- this is a review aid, it downloads nothing):
  - Drop releases whose title carries a noise marker (instrumental, remix,
    a cappella, chopped/screwed, live, session, "music from/inspired by",
    "(... version)" edits).
  - Drop `album_type` != "album" unless the EP is >=50% missing (a
    followed artist's mostly-absent EP is still worth surfacing; a
    mostly-present one isn't).
  - De-dupe deluxe/expanded pairs: if both "X" and "X + / (Deluxe) /
    (Expanded)" appear, keep only the smaller-titled base entry's gap
    (the "+" edition's extra tracks are usually the least essential).
  - Only surface a gap when missing_count >= 3 AND missing fraction >= 0.34
    -- OR the album is entirely absent (missing == expected), which is
    always surfaced regardless of size.

Usage: python artist_completeness_filter.py [--min-missing N]
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "state"   # <repo>/state (src/ reorg)
REPORT = ROOT / "artist_completeness_report.json"
OUT = ROOT / "artist_completeness_actionable.json"

_NOISE = re.compile(
    r"(instrumental|instrumentals|\bremix|remixes|a\s?cappella|acappella|chopped|"
    r"screwed|slowed|reverb|\blive\b|\bsession|unplugged|karaoke|\bdemos?\b|"
    r"music from|inspired by|\bscore\b|commentary|interview|soundtrack|"
    r"motion picture|original series|\bost\b|\bthis is it\b|\bimmortal\b|"
    # compilation / greatest-hits / reissue-anthology / anniversary markers
    r"best of|greatest hits|the collection|anthology|essential|"
    r"very best|the hits|\bmasters\b|millennium|songbook|20th century|"
    r"definitive|ultimate|\bclassics?\b|the complete|complete collection|"
    r"complete edition|\bduets\b|\bgold\b|platinum|the best|\bvol\.?\s?\d|volume \d|"
    r"unreleased|rarities|b-sides|bonus tracks|detroit mix|m\+m mixes|"
    r"universal masters|10 series|love songs|number 1'?s|#1'?s|"
    r"\d\d(th|st|nd|rd)\s+anniversary|super deluxe|\bhits\b|and friends|"
    r"light in the attic)",
    re.I,
)
_DELUXE_SUFFIX = re.compile(
    r"\s*(\+|-\s*deluxe.*|\(deluxe.*?\)|\(expanded.*?\)|deluxe edition|"
    r"expanded edition|\bdeluxe$|\(bonus.*?\)|\(complete edition\)|"
    r"\(.*anniversary.*?\)|\(glo'?d up.*?\)|\(.*version\)|\(alc edition\)|"
    r"the alc edition)\s*$", re.I)


def _base_title(t: str) -> str:
    return _DELUXE_SUFFIX.sub("", t).strip().lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-missing", type=int, default=3)
    ap.add_argument("--min-frac", type=float, default=0.34)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not REPORT.exists():
        print(f"No report at {REPORT}")
        return 1
    data = json.loads(REPORT.read_text(encoding="utf-8"))

    out_artists = []
    for a in data.get("artists", []):
        liked = a.get("liked") or 0
        saved = a.get("saved_albums") or 0
        # "get the whole catalog" only makes sense for an artist the user is
        # genuinely deep on. For everyone else, only surface albums they've
        # actually STARTED (>=1 track present) -- the real "partial album"
        # case (started Alfredo, have 9/10). Freddie Gibbs (5 liked, 2 saved)
        # clears this bar, so Alfredo 2 (entirely absent) still surfaces;
        # Marvin Gaye (2 liked, 1 saved) does not, so his 55 compilations +
        # deep 1960s catalog don't flood the report.
        deep = saved >= 2 or liked >= 15
        # index albums by base title so deluxe/standard pairs collapse
        by_base: dict = {}
        for alb in a.get("albums", []):
            title = alb["album"]
            if _NOISE.search(title):
                continue
            exp = alb.get("expected") or 0
            miss = alb.get("missing_count") or 0
            if exp <= 0:
                continue
            frac = miss / exp
            absent = miss >= exp
            present = exp - miss
            if alb.get("album_type") != "album":
                if frac < 0.5:
                    continue
            present_frac = present / exp
            # A genuine "started this album" case has a real chunk present
            # (Alfredo 9/10, Here Comes The Cowboy 8/13). 1-3 stray tracks on
            # a 15-25 track album is almost always just singles the user
            # liked that happen to also live on that album -- not evidence
            # they're collecting it. Treat <25% present as effectively absent.
            if not absent and present_frac < 0.25:
                absent = True
            if absent and not deep:
                continue
            if not absent and (miss < args.min_missing or frac < args.min_frac):
                continue
            base = _base_title(title)
            # keep the base-titled entry over a "+"/(Deluxe) sibling; if only
            # the deluxe exists, keep it
            prev = by_base.get(base)
            is_deluxe = bool(_DELUXE_SUFFIX.search(title))
            if prev is None or (prev["_deluxe"] and not is_deluxe):
                by_base[base] = {**alb, "frac": round(frac, 2), "absent": absent,
                                 "_deluxe": is_deluxe}
        gaps = sorted(by_base.values(), key=lambda x: -x["missing_count"])
        for g in gaps:
            g.pop("_deluxe", None)
        # Cap per artist -- a prolific artist with 40 mixtapes shouldn't
        # produce a 40-line block. Keep partial (started) albums always, plus
        # the top few entirely-absent ones by size.
        partial = [g for g in gaps if not g["absent"]]
        absent_g = [g for g in gaps if g["absent"]][:6]
        gaps = sorted(partial + absent_g, key=lambda x: -x["missing_count"])
        if not gaps:
            continue
        total_missing = sum(g["missing_count"] for g in gaps)
        out_artists.append({
            "artist": a["artist"], "liked": a.get("liked"),
            "saved_albums": a.get("saved_albums"),
            "total_missing": total_missing,
            "absent_albums": sum(1 for g in gaps if g["absent"]),
            "gaps": gaps,
        })

    out_artists.sort(key=lambda x: -x["total_missing"])
    OUT.write_text(json.dumps(out_artists, ensure_ascii=False, indent=1), encoding="utf-8")

    grand_missing = sum(x["total_missing"] for x in out_artists)
    print(f"{len(out_artists)} followed artist(s) with actionable core-album gaps  "
          f"({grand_missing} missing tracks total)\n")
    for x in out_artists[:40]:
        print(f"{x['artist']}  (liked={x['liked']}, saved_albums={x['saved_albums']}, "
              f"{x['total_missing']} missing, {x['absent_albums']} album(s) entirely absent)")
        for g in x["gaps"]:
            tag = "  *** ENTIRELY ABSENT ***" if g["absent"] else ""
            print(f"    {g['album']}  [{g['expected']-g['missing_count']}/{g['expected']}]"
                  f"  ({int(g['frac']*100)}% missing){tag}")
    if len(out_artists) > 40:
        print(f"\n... and {len(out_artists) - 40} more artists")
    print(f"\nFull data -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
