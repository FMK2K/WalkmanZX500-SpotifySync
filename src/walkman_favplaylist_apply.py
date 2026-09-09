"""
UI-automation runner that executes favorites_playlists_plan.json against the
Sony HiRes player on the Walkman via adb input taps + uiautomator dumps.

No batch/DB write path exists (provider not exported, no root — see
memory project_walkman_hires_player_playlists_favorites.md), so this drives
the real UI one track at a time: search -> tap top matching result -> tap
the Now-Playing star (favorites) or open its context menu and pick a
playlist (playlists). Resumable via a checkpoint file so it survives
interruption; every action is logged.

Usage:
    python walkman_favplaylist_apply.py favorites --limit 25
    python walkman_favplaylist_apply.py favorites            # full run
    python walkman_favplaylist_apply.py status
"""
import json, os, re, subprocess, sys, time, unicodedata
from pathlib import Path

_ADB_ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}

ROOT = Path(__file__).resolve().parent.parent / "state"   # <repo>/state (src/ reorg)
# WALKMAN_FAVPLAYLIST_PLAN lets a caller (e.g. walkman_fav_upgrade.py) point
# this at a narrower re-star worklist instead of the full plan.
import os as _os
_plan_override = _os.environ.get("WALKMAN_FAVPLAYLIST_PLAN", "").strip()
PLAN_PATH = Path(_plan_override) if _plan_override else (ROOT / "favorites_playlists_plan.json")
STATE_PATH = ROOT / ("favplaylist_apply_state.upgrade.json" if _plan_override
                     else "favplaylist_apply_state.json")
LOG_PATH = ROOT / "favplaylist_apply.log"

SEARCH_BOX = (360, 225)
LIBRARY_TOP_NAV = (558, 117)
STAR_COORD = (648, 848)
SONG_TITLES_TAB = (120, 340)   # "Search results by song titles" count chip

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def adb(*args, timeout=15):
    return subprocess.run(["adb"] + list(args), capture_output=True, timeout=timeout)

def tap(x, y):
    adb("shell", "input", "tap", str(x), str(y))

def keyevent(code):
    adb("shell", "input", "keyevent", str(code))

def back():
    keyevent(4)

def clear_and_type(text):
    tap(*SEARCH_BOX)
    time.sleep(0.4)
    keyevent(123)  # MOVE_END
    for _ in range(0, 80, 8):
        adb("shell", "input", "keyevent", *(["67"] * 8))  # KEYCODE_DEL x8/call
    time.sleep(0.2)
    escaped = text.replace(" ", "%s").replace("&", r"\&").replace("(", r"\(").replace(")", r"\)").replace("'", "")
    adb("shell", "input", "text", escaped)

def ui_dump(retries=5):
    for attempt in range(retries):
        adb("shell", "rm", "-f", "/sdcard/claude_ui.xml")
        r = subprocess.run(
            ["adb", "shell", "uiautomator", "dump", "/sdcard/claude_ui.xml"],
            capture_output=True, timeout=15, env=_ADB_ENV,
        )
        out = (r.stdout or b"").decode(errors="replace") + (r.stderr or b"").decode(errors="replace")
        if "dumped to" in out:
            subprocess.run(
                ["adb", "pull", "/sdcard/claude_ui.xml", str(ROOT / "_ui_tmp.xml")],
                capture_output=True, timeout=15, env=_ADB_ENV,
            )
            p = ROOT / "_ui_tmp.xml"
            if p.exists():
                return p.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.8)
    return ""

# Rows come as sibling nodes: track_left_top_text (title) then track_left_bottom_text
# (artist), each carrying its own bounds; the clickable row container isn't reliably
# tagged, so we pair top+bottom by document order and click the title's row center.
_TOP_RE = re.compile(
    r'text="([^"]*)" resource-id="[^"]*track_left_top_text"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
)
_BOTTOM_RE = re.compile(
    r'text="([^"]*)" resource-id="[^"]*track_left_bottom_text"'
)

def _norm(s):
    s = (s or "").lower().replace("&", " and ")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s)

_FUZZ_FEAT = re.compile(r"[\(\[\{]\s*(feat|ft|featuring|with|prod|remix)\b[^\)\]\}]*[\)\]\}]", re.I)
_FUZZ_PARENS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# trailing " - Remaster(ed) / 2019 Remaster / Mono / Live / Radio Edit / Single Version ..."
_FUZZ_TAIL = re.compile(
    r"\s*[-–]\s*(?:\d{4}\s*)?(?:remaster(?:ed)?|mono|stereo|live|radio\s*edit|"
    r"single\s*version|album\s*version|extended|edit|version|instrumental|"
    r"bonus\s*track|deluxe|explicit|clean)\s*$", re.I)


def _norm_fuzzy(s):
    """Aggressive title normalisation for the fuzzy tier: drop every
    parenthetical (feat / remix / version / edit) and any trailing
    ' - <edition marker>', then _norm."""
    s = (s or "").lower()
    s = _FUZZ_FEAT.sub("", s)
    s = _FUZZ_PARENS.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _FUZZ_TAIL.sub("", s).strip()
    return _norm(s)


def _title_close(a: str, b: str) -> bool:
    """True if two already-_norm_fuzzy'd titles are ~the same song.
    Accepts: exact; one a prefix of the other with <=3 trailing chars
    (a truncated / pluralised tag); SequenceMatcher ratio >= 0.90 when both
    are at least 5 chars. Conservative on purpose -- a wrong favorite is
    worse than a missed one, and artist-overlap is already required by the
    caller, so this only has to separate near-typos from real different
    songs ("stronger" vs "stranger" must NOT pass)."""
    if not a or not b or len(a) < 3 or len(b) < 3:
        return a == b and bool(a)
    if a == b:
        return True
    lo, hi = sorted((a, b), key=len)
    if hi.startswith(lo) and len(hi) - len(lo) <= 3 and (len(lo) >= 4 or lo.isalpha()):
        return True
    if len(lo) < 5:
        return False
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.90

def _artist_overlaps(target_a: str, row_a: str) -> bool:
    """
    Loose but non-trivial artist agreement check. Both args are already
    _norm()'d (lowercased, punctuation-stripped, concatenated -- no spaces).
    Containment ("davidhalliday" in "davidhallidayfeatxyz") covers most real
    cases (features, "feat." joins, ampersand-joined credits); a stray exact
    'artist1;artist2' row concatenation still contains the full target name
    as a substring, so this doesn't need to split on separators.
    """
    if not target_a or not row_a:
        return False
    return target_a in row_a or row_a in target_a

def find_best_row(xml_text, artist, title, fuzzy=False):
    """
    2026-09-04: this used to fall back to bare title-substring matching with
    NO artist check at all when no artist-agreeing exact-title row was
    found -- confirmed live via favplaylist_apply.log audit that this
    wrongly favorited at least 4 different-artist tracks sharing a short
    title/substring (Steve Lacy "Only If" -> Janet Jackson "If", TeaMarrr
    "Kinda Love" -> Keyshia Cole "Love", Flying Lotus "Black Gold" ->
    Brockhampton "GOLD", Ne-Yo "Because Of You" -> Bill Withers "All
    Because of You"). A wrong favorite is worse than a missed one, so this
    is now strict: exact-normalized-title AND artist-overlap is the only
    accepted match. No title-substring fallback at all -- an artist-only
    title match, or nothing, is preferable to guessing.
    """
    tops = [(m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
            for m in _TOP_RE.finditer(xml_text)]
    bottoms = [m.group(1) for m in _BOTTOM_RE.finditer(xml_text)]
    if not tops:
        return None
    target_t, target_a = _norm(title), _norm(artist)
    rows = []
    for i, (text, x1, y1, x2, y2) in enumerate(tops):
        bot = bottoms[i] if i < len(bottoms) else ""
        rows.append((text, bot, (x1 + x2) // 2, (y1 + y2) // 2))
    # Tier 1 — exact normalised title + artist overlap (unchanged).
    for text, bot, cx, cy in rows:
        if _norm(text) == target_t and _artist_overlaps(target_a, _norm(bot)):
            return (cx, cy, text, bot)

    # Tier 2 — FUZZY title, but artist overlap is still MANDATORY (this is
    # what keeps the 2026-09-04 wrong-artist bug from coming back). Only
    # fires when a row's artist agrees with the target; picks the closest
    # title among those. 2026-09-06: added for the ~34 re-star misses where
    # the on-device title tag drifted from the Spotify title (typos like
    # "Somthing's Missing", punctuation, dropped feat credits, edition tags).
    tgt_f = _norm_fuzzy(title)
    if not fuzzy or not tgt_f:
        return None
    best = None
    for text, bot, cx, cy in rows:
        if not _artist_overlaps(target_a, _norm(bot)):
            continue
        row_f = _norm_fuzzy(text)
        if _title_close(tgt_f, row_f):
            import difflib
            r = difflib.SequenceMatcher(None, tgt_f, row_f).ratio()
            if best is None or r > best[0]:
                best = (r, cx, cy, text, bot)
    if best:
        return (best[1], best[2], best[3], best[4])
    return None

def sample_star_filled():
    """Crop the star icon area and check if it's filled (favorited) vs outline."""
    adb("shell", "screencap", "-p", "/sdcard/claude_star.png")
    subprocess.run(
        ["adb", "pull", "/sdcard/claude_star.png", str(ROOT / "_star_tmp.png")],
        capture_output=True, timeout=15, env=_ADB_ENV,
    )
    try:
        from PIL import Image
        img = Image.open(ROOT / "_star_tmp.png").convert("L")
        x, y = STAR_COORD
        crop = img.crop((x - 10, y - 10, x + 10, y + 10))
        px = list(crop.getdata())
        bright = sum(1 for v in px if v > 150) / len(px)
        # Calibrated live against a confirmed-filled star (2026-09-04): a filled
        # star's own points/corners keep this well under 50% -- 0.28 measured on
        # a known-good "Added. Favorites" case. An empty outline star measures
        # near-zero (just a thin stroke). 0.15 cleanly separates the two.
        return bright > 0.15
    except Exception as e:
        log(f"  [warn] star sample failed: {e}")
        return None

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"favorites_done": [], "favorites_failed": []}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

def go_home():
    back()
    time.sleep(0.6)
    tap(*LIBRARY_TOP_NAV)
    time.sleep(0.8)

_FEAT_RE = re.compile(r"\s*[\(\[](feat\.?|ft\.?|with)\s[^\)\]]*[\)\]]\s*$", re.I)

def process_favorite(entry):
    artist, title = entry.get("artist", ""), entry.get("title", "")
    # title alone is usually the more distinctive/reliable search term; the search
    # covers song-title text, not artist, so leading the query with artist name
    # commonly returns 0 hits (tested live -- "Kendrick Lamar Money Trees" = 0,
    # "Money Trees" = 4). Search on title only, disambiguate by artist afterwards.
    # The on-device search index also doesn't match inside a "(feat. X)" suffix
    # (tested live -- 0 hits with it, hits without) so strip it from the query;
    # find_best_row still matches against the full original title text.
    base = _FEAT_RE.sub("", title).strip()
    # Query variants, tried in order until one returns matchable rows. The
    # device search index is prefix/word based and choppy on punctuation, so
    # a couple of shorter/cleaner variants rescue titles the full string
    # misses (typos, edition suffixes, "/" splits, long feat titles).
    _clean = re.sub(r"[^\w\s]", " ", base)
    _clean = re.sub(r"\s+", " ", _clean).strip()
    variants = []
    for q in (base, _clean, " ".join(_clean.split()[:3]), " ".join(_clean.split()[:2])):
        q = (q or "").strip()[:60]
        if q and q not in variants:
            variants.append(q)
    ok = False
    try:
        hit = None
        used_q = variants[0]
        for qi, query in enumerate(variants):
            tap(*LIBRARY_TOP_NAV)
            time.sleep(0.6)
            clear_and_type(query)
            time.sleep(1.2)
            tap(*SONG_TITLES_TAB)
            time.sleep(0.9)
            xml = ui_dump()
            xml2 = ui_dump()  # settle check: a mid-transition dump can miss/misreport
            if xml2 and xml2 != xml:
                xml = xml2
            hit = find_best_row(xml, artist, title, fuzzy=True)
            used_q = query
            if hit:
                break
        if not hit:
            log(f"  NO MATCH search='{used_q}' artist='{artist}' (tried {len(variants)} variant(s))")
            return False
        cx, cy, matched_text, matched_artist = hit
        tap(cx, cy)
        time.sleep(1.4)
        # Check the star's state BEFORE touching it: it's a toggle, so tapping an
        # already-filled star un-favorites it. A track already favorited (from a
        # prior run, a resumed/duplicate pass, or manually) must be left alone.
        already = sample_star_filled()
        if already:
            ok = True
            log(f"  SKIP(already-fav) '{artist} - {title}' -> matched '{matched_text}' / '{matched_artist}'")
        else:
            tap(*STAR_COORD)
            time.sleep(0.6)
            filled = sample_star_filled()
            ok = filled is not False  # treat unknown (None) as provisional success
            log(f"  {'OK' if ok else 'FAIL'} '{artist} - {title}' -> matched '{matched_text}' / '{matched_artist}' filled={filled}")
    except Exception as e:
        log(f"  EXCEPTION '{artist} - {title}': {e}")
        ok = False
    finally:
        go_home()  # always reset to Library top, success or not, so state never drifts
    return ok

def run_favorites(limit=None):
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    resolved = plan["favorites"]["resolved"]
    state = load_state()
    done_keys = set(tuple(x) for x in state["favorites_done"])
    todo = [e for e in resolved if (e["artist"], e["title"]) not in done_keys]
    if limit:
        todo = todo[:limit]
    log(f"=== favorites run: {len(todo)} to process ({len(done_keys)} already done) ===")
    for i, entry in enumerate(todo, 1):
        key = [entry["artist"], entry["title"]]
        try:
            ok = process_favorite(entry)
        except Exception as e:
            log(f"  EXCEPTION on '{entry.get('title')}': {e}")
            ok = False
        if ok:
            state["favorites_done"].append(key)
        else:
            state["favorites_failed"].append(key)
        save_state(state)  # every item -- a toggle is destructive on replay, so
                            # resume must never reprocess an item already handled
        if i % 5 == 0:
            log(f"  ... checkpoint {i}/{len(todo)}")
    save_state(state)
    log(f"=== favorites run complete: {len(state['favorites_done'])} done, {len(state['favorites_failed'])} failed total ===")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    limit = None
    for a in sys.argv[2:]:
        if a.startswith("--limit"):
            limit = int(a.split("=")[1]) if "=" in a else int(sys.argv[sys.argv.index(a) + 1])
    if mode == "favorites":
        run_favorites(limit=limit)
    elif mode == "status":
        st = load_state()
        print(f"done={len(st['favorites_done'])} failed={len(st['favorites_failed'])}")
    else:
        print("usage: walkman_favplaylist_apply.py [favorites|status] [--limit N]")
