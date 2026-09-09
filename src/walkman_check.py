#!/usr/bin/env python3
"""
walkman_check.py
────────────────
Reads walkman_tree.txt (produced by walkman_scan.ps1) and reports the
health of the library, categorising every audio file as:

  ✓ canonical   — Music/Artist/Album/Artist - Album - NN Title.ext
  ~ fixable     — has track number but not full canonical name
  ✗ bare        — bare title, no track number
  ✗ junk_folder — inside a SpotiDownloader / SoundCloud watermark folder
  ✗ wrong_depth — wrong nesting level under Music/

Then (with --deep-check) runs a half-assed AI audit per album:
  • Verifies expected track count against the official release
  • Flags missing track numbers (gaps in the sequence)
  • Flags duplicate track numbers or titles
  • Flags titles that look wrong for the album
  • Detects titles that belong to a completely different album

Usage:
    python walkman_check.py                          # structure check only
    python walkman_check.py --deep-check             # + AI album audit
    python walkman_check.py --deep-check --workers 8 # more AI threads
    python walkman_check.py --show-junk              # list all junk folder names
    python walkman_check.py --show-bare              # list all bare-title files
    python walkman_check.py --show-fixable           # list fixable files
    python walkman_check.py --export-issues issues.csv
    python walkman_check.py --clear-ai-cache         # re-query AI next run
"""

import csv
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_TREE = Path.home() / "Desktop" / "walkman_tree.txt"

# AI audit cache — avoids re-querying the same album on repeated runs
AI_CACHE_FILE = Path.home() / "Desktop" / "walkman_ai_cache.json"

# Gemini / DeepSeek keys — loaded from config.py if available, else env
def _load_api_keys():
    keys = {"gemini": os.environ.get("GEMINI_API_KEY", ""),
            "deepseek": os.environ.get("DEEPSEEK_API_KEY", "")}
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import config as _cfg
        keys["gemini"]   = getattr(_cfg, "GEMINI_API_KEY",   keys["gemini"])
        keys["deepseek"] = getattr(_cfg, "DEEPSEEK_API_KEY", keys["deepseek"])
    except Exception:
        pass
    return keys

API_KEYS = _load_api_keys()

# Rate-limiter shared across all AI worker threads
_AI_LOCK  = threading.Lock()
_AI_DELAY = 1.2   # seconds between calls

AUDIO_TYPE_FRAGMENTS = {
    "flac", "mp3", "m4a", "ogg", "wav", "aac", "wma", "ape", "opus",
    "audio", "mpeg",
}

JUNK_PREFIXES = {
    "spotidownloader", "spotifydownloader", "soundcloud.app",
    "ytmp3", "y2mate", "tubidy", "mp3juice", "downloadify",
    "snapsave",
}

SKIP_FOLDERS_TOP = {
    "android", "pictures", "alarms", "notifications", "ringtones",
    "dcim", "movies", "podcasts", "system volume information",
    ".android_secure",
}

# Canonical filename: Artist - Album - NN Title
RE_CANONICAL = re.compile(
    r"^(.+?)\s+-\s+(.+?)\s+-\s+(\d{2,3}(?:-\d{2,3})?)\s+(.+)$"
)
# Has track number but not full canonical
RE_TRACK_ONLY = re.compile(r"^(\d{2,3})\s+(.+)$")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_audio_type(type_str: str) -> bool:
    tl = type_str.lower()
    return any(f in tl for f in AUDIO_TYPE_FRAGMENTS)


def is_junk_folder(folder_name: str) -> bool:
    fl = folder_name.lower().strip()
    return any(fl.startswith(j) for j in JUNK_PREFIXES)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.strip().lower())


def parse_tree(tree_path: Path) -> list[dict]:
    """Parse the tab-separated CSV produced by walkman_scan.ps1.
    Also handles the older Format-Table fixed-width format."""
    files = []
    with open(tree_path, encoding="utf-8-sig") as f:
        content = f.read()

    # Detect format: CSV has quoted fields; Format-Table has runs of spaces
    if '"Path"' in content or ',"Type"' in content or '\t"Path"' in content:
        # Tab-separated CSV from new script
        reader = csv.DictReader(content.splitlines(), delimiter="\t")
        for row in reader:
            path  = row.get("Path", "").strip().strip('"')
            ftype = row.get("Type", "").strip().strip('"')
            size  = row.get("Size_MB", "").strip().strip('"')
            if path:
                files.append({"path": path, "type": ftype, "size": size})
    else:
        # Legacy Format-Table: fixed-width, split on 2+ spaces
        for line in content.splitlines():
            if not line.strip() or line.strip().startswith("Path") or line.strip().startswith("----"):
                continue
            parts = re.split(r"\s{2,}", line.strip(), maxsplit=2)
            if len(parts) >= 2:
                files.append({"path": parts[0].strip(), "type": parts[1].strip(),
                               "size": parts[2].strip() if len(parts) > 2 else ""})

    return files


# ─────────────────────────────────────────────────────────────────────────────
# AI album audit
# ─────────────────────────────────────────────────────────────────────────────

def _call_ai(prompt: str, timeout: float = 12.0) -> dict | None:
    """Send prompt to Gemini (primary) or DeepSeek (fallback). Returns parsed JSON or None."""
    with _AI_LOCK:
        time.sleep(_AI_DELAY)

    # 1. Gemini
    gk = API_KEYS.get("gemini", "")
    if gk:
        try:
            # 2026-08-22: gemini-2.0-flash decommissioned (404 → use
            # gemini-3.6-flash). maxOutputTokens=1024 is already generous
            # enough to absorb the replacement's default thinking overhead
            # (~150 tokens observed) — no other change needed here.
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 1024,
                    "responseMimeType": "application/json",
                },
            }).encode()
            req = urllib.request.Request(
                f"{url}?key={gk}", data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data  = json.loads(resp.read())
                text  = data["candidates"][0]["content"]["parts"][0]["text"]
                text  = re.sub(r"^```json\s*", "", text.strip())
                text  = re.sub(r"\s*```$",     "", text)
                return json.loads(text)
        except Exception as e:
            pass   # fall through to DeepSeek

    # 2. DeepSeek
    dk = API_KEYS.get("deepseek", "")
    if dk:
        try:
            url = "https://api.deepseek.com/v1/chat/completions"
            payload = json.dumps({
                "model": "deepseek-chat",
                "max_tokens": 1024,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {dk}"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data    = json.loads(resp.read())
                content = data["choices"][0]["message"]["content"]
                content = re.sub(r"^```json\s*", "", content.strip())
                content = re.sub(r"\s*```$",     "", content)
                return json.loads(content)
        except Exception:
            pass

    return None


def _strip_feat(title: str) -> str:
    return re.sub(r"\s*[\(\[].*?(?:feat\.|ft\.|featuring|with)\s[^\)\]]*[\)\]]", "",
                  title, flags=re.I).strip()


def _norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def audit_album_with_ai(
    artist: str,
    album: str,
    tracks_on_device: list[dict],   # [{"track": "01", "title": "Song", "filename": "..."}]
    cache: dict,
) -> dict:
    """
    Ask AI to verify the album against what's on the device.
    Returns a dict describing any issues found.
    Uses cache to avoid re-querying the same album.

    cache key: f"{norm_title(artist)}|{norm_title(album)}"
    """
    cache_key = f"{_norm_title(artist)}|{_norm_title(album)}"

    # Use cached AI response if available
    if cache_key in cache:
        ai_resp = cache[cache_key]
    else:
        track_list_str = "\n".join(
            f"  {t['track'] or '??'}. {t['title']}"
            for t in sorted(tracks_on_device, key=lambda x: x.get("track", "99"))
        )
        prompt = f"""You are a music expert with complete knowledge of album track listings.

Album to verify: "{album}" by {artist}

Tracks found on this device ({len(tracks_on_device)} files):
{track_list_str}

Please check this against the official standard/deluxe album release and return a JSON object with:
{{
  "official_track_count": <integer, standard edition>,
  "official_tracks": [
    {{"track": "01", "title": "Official Title Here"}},
    ...
  ],
  "issues": [
    {{
      "type": "missing" | "wrong_title" | "wrong_track_number" | "extra" | "duplicate" | "wrong_album",
      "track": "NN",
      "device_title": "what's on device",
      "expected_title": "what should be there (or null)",
      "note": "brief explanation"
    }}
  ],
  "verdict": "ok" | "minor_issues" | "major_issues",
  "confidence": "high" | "medium" | "low"
}}

Rules:
- Be specific. If track 01 is "Space" on Aquemini but device has "Aquemini", that's a wrong_track_number issue.
- Bonus tracks, skits, interludes beyond official count: flag as "extra" only if count difference is large (>3).
- Remastered titles that are slightly different (e.g. "(2012 Remaster)" suffix) are fine — don't flag those.
- If you don't recognise this album with confidence, set confidence to "low" and issues to [].
- If the album looks correct, set issues to [] and verdict to "ok".
"""
        ai_resp = _call_ai(prompt, timeout=15.0)
        if ai_resp:
            cache[cache_key] = ai_resp
        else:
            return {"error": "AI unavailable", "verdict": "unknown"}

    if not ai_resp:
        return {"error": "No AI response", "verdict": "unknown"}

    # Now cross-check device tracks against AI's response locally
    issues = list(ai_resp.get("issues", []))
    official_count = ai_resp.get("official_track_count", 0)
    device_count   = len(tracks_on_device)
    confidence     = ai_resp.get("confidence", "low")

    # Local checks that AI might miss — duplicate detection
    seen_tracks  = defaultdict(list)
    seen_titles  = defaultdict(list)
    for t in tracks_on_device:
        if t["track"] and t["track"] != "00":
            seen_tracks[t["track"]].append(t["title"])
        seen_titles[_norm_title(_strip_feat(t["title"]))].append(t["track"])

    for track_num, titles in seen_tracks.items():
        if len(titles) > 1:
            issues.append({
                "type": "duplicate",
                "track": track_num,
                "device_title": " / ".join(titles),
                "expected_title": None,
                "note": f"Track {track_num} appears {len(titles)} times",
            })

    for title_norm, track_nums in seen_titles.items():
        if len(track_nums) > 1:
            issues.append({
                "type": "duplicate",
                "track": " + ".join(sorted(set(track_nums))),
                "device_title": title_norm,
                "expected_title": None,
                "note": f"Same title on tracks {', '.join(sorted(set(track_nums)))}",
            })

    # Severity
    n_major  = sum(1 for i in issues if i.get("type") in ("missing", "wrong_album"))
    n_minor  = sum(1 for i in issues if i.get("type") in ("wrong_title", "wrong_track_number",
                                                            "extra", "duplicate"))
    if n_major:
        verdict = "major_issues"
    elif n_minor:
        verdict = "minor_issues"
    else:
        verdict = ai_resp.get("verdict", "ok")

    return {
        "artist":         artist,
        "album":          album,
        "official_count": official_count,
        "device_count":   device_count,
        "issues":         issues,
        "verdict":        verdict,
        "confidence":     confidence,
    }


def build_album_map(r: dict) -> dict[str, dict]:
    """
    From the analyse() result, build a map of cleanly-structured albums
    that have canonical filenames, suitable for AI auditing.

    Returns: {album_key: {"artist": str, "album": str, "tracks": [...]}}
    """
    RE_CAN = re.compile(r"^(.+?)\s+-\s+(.+?)\s+-\s+(\d{2,3}(?:-\d{2,3})?)\s+(.+)$")
    album_map = {}

    for entry in r["canonical"]:
        fn      = entry["_filename"]
        folders = entry["_folders"]   # [artist_folder, album_folder]
        storage = entry["_storage"]
        m = RE_CAN.match(fn)
        if not m:
            continue
        artist_tag = m.group(1).strip()
        album_tag  = m.group(2).strip()
        track_num  = m.group(3).strip()
        title      = m.group(4).strip()

        # Prefer the folder-derived artist/album as the lookup key
        if len(folders) >= 2:
            artist_folder = folders[-2]
            album_folder  = folders[-1]
        else:
            artist_folder = artist_tag
            album_folder  = album_tag

        key = f"{storage}/{artist_folder}/{album_folder}"
        if key not in album_map:
            album_map[key] = {
                "artist":  artist_tag,
                "album":   album_tag,
                "storage": storage,
                "tracks":  [],
            }
        album_map[key]["tracks"].append({
            "track":    track_num,
            "title":    title,
            "filename": fn,
        })

    return album_map


def run_deep_check(r: dict, workers: int = 4,
                   cache_path: Path = AI_CACHE_FILE) -> list[dict]:
    """
    Run the AI album audit on all canonical albums.
    Returns list of audit results sorted by severity.
    """
    if not (API_KEYS.get("gemini") or API_KEYS.get("deepseek")):
        print(c(RED, "  ERROR: No AI API key found. Set GEMINI_API_KEY or DEEPSEEK_API_KEY"))
        print("  In .env or as environment variables, or in config.py")
        return []

    # Load cache
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    album_map = build_album_map(r)
    total_albums = len(album_map)

    if not total_albums:
        print("  No canonical albums found for AI audit.")
        return []

    cached_count = sum(
        1 for info in album_map.values()
        if f"{_norm_title(info['artist'])}|{_norm_title(info['album'])}" in cache
    )
    fresh_count = total_albums - cached_count

    print()
    print(c(BOLD, "══════════════════════════════════════════════════════"))
    print(c(BOLD, "  AI Album Audit (half-assed but honest)"))
    print(c(BOLD, "══════════════════════════════════════════════════════"))
    print(f"  Albums to check : {total_albums}")
    print(f"  From AI cache   : {cached_count}")
    print(f"  Fresh AI calls  : {fresh_count}  (≈{fresh_count * 2}s with {workers} workers)")
    if fresh_count > 50:
        print(c(YELLOW, f"  Warning: {fresh_count} fresh calls — this may take several minutes"))
    print()

    results      = []
    cache_lock   = threading.Lock()
    done         = [0]
    done_lock    = threading.Lock()

    def _audit_one(key, info):
        result = audit_album_with_ai(
            artist           = info["artist"],
            album            = info["album"],
            tracks_on_device = info["tracks"],
            cache            = cache,
        )
        result["_key"] = key
        with cache_lock:
            ck = f"{_norm_title(info['artist'])}|{_norm_title(info['album'])}"
            # Persist fresh AI response
            if ck in cache:
                try:
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
                except Exception:
                    pass
        with done_lock:
            done[0] += 1
            n = done[0]
        if n % 10 == 0 or n == total_albums:
            print(f"  Audited {n}/{total_albums} albums ...", end="\r", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_audit_one, k, v): k for k, v in album_map.items()}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"_key": futures[fut], "error": str(e), "verdict": "unknown"})

    print()  # clear the \r progress line

    # Save updated cache
    try:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    # Sort: major first, then minor, then ok
    order = {"major_issues": 0, "minor_issues": 1, "ok": 2, "unknown": 3}
    results.sort(key=lambda x: order.get(x.get("verdict", "unknown"), 9))
    return results


def print_deep_check_report(results: list[dict], show_ok: bool = False):
    if not results:
        return

    n_major   = sum(1 for r in results if r.get("verdict") == "major_issues")
    n_minor   = sum(1 for r in results if r.get("verdict") == "minor_issues")
    n_ok      = sum(1 for r in results if r.get("verdict") == "ok")
    n_unknown = sum(1 for r in results if r.get("verdict") in ("unknown", None))

    print(f"  {c(RED,    '✗ Albums with major issues'):<50} {n_major}")
    print(f"  {c(YELLOW, '~ Albums with minor issues'):<50} {n_minor}")
    print(f"  {c(GREEN,  '✓ Albums that check out'):<50} {n_ok}")
    if n_unknown:
        print(f"  {c(CYAN,   '? Unknown / AI unavailable'):<50} {n_unknown}")
    print()

    ISSUE_ICON = {
        "missing":            c(RED,    "MISSING "),
        "wrong_title":        c(YELLOW, "TITLE?  "),
        "wrong_track_number": c(YELLOW, "TRACK#? "),
        "extra":              c(CYAN,   "EXTRA   "),
        "duplicate":          c(RED,    "DUPL.   "),
        "wrong_album":        c(RED,    "WRONG♫  "),
    }

    for res in results:
        verdict    = res.get("verdict", "unknown")
        issues     = res.get("issues", [])
        confidence = res.get("confidence", "low")
        artist     = res.get("artist", "?")
        album      = res.get("album",  "?")
        n_device   = res.get("device_count",   "?")
        n_official = res.get("official_count", "?")
        key        = res.get("_key", "")

        if verdict == "ok" and not show_ok:
            continue

        if verdict == "ok":
            print(f"  {c(GREEN, '✓')} {artist} — {album}  [{n_device}/{n_official} tracks]")
            continue

        # Header
        if verdict == "major_issues":
            icon = c(RED, "✗")
        elif verdict == "minor_issues":
            icon = c(YELLOW, "~")
        else:
            icon = c(CYAN, "?")

        conf_str = f" [{c(CYAN, 'low confidence')}]" if confidence == "low" else ""
        count_str = f"  [{n_device} on device"
        if n_official:
            delta = ""
            if isinstance(n_device, int) and isinstance(n_official, int):
                diff = n_device - n_official
                if diff > 0:  delta = c(YELLOW, f" +{diff} extra")
                elif diff < 0: delta = c(RED,   f" {diff} missing")
                else:          delta = c(GREEN,  " count OK")
            count_str += f" / {n_official} official{delta}"
        count_str += "]"

        print(f"  {icon} {c(BOLD, artist + ' — ' + album)}{conf_str}{count_str}")

        for issue in issues[:8]:   # cap per album so report doesn't scroll forever
            itype    = issue.get("type", "?")
            track    = issue.get("track", "??")
            d_title  = issue.get("device_title",  "")
            e_title  = issue.get("expected_title", "")
            note     = issue.get("note", "")
            icon_str = ISSUE_ICON.get(itype, c(YELLOW, "?       "))

            if itype == "missing":
                print(f"     {icon_str} Track {track}: {e_title or '?'} — NOT ON DEVICE")
            elif itype == "duplicate":
                print(f"     {icon_str} Track {track}: {d_title}  ← {note}")
            elif itype == "wrong_title":
                print(f"     {icon_str} Track {track}: found '{d_title}' — expected '{e_title}'")
            elif itype == "wrong_track_number":
                print(f"     {icon_str} '{d_title}' — {note}")
            elif itype == "wrong_album":
                print(f"     {icon_str} Track {track}: '{d_title}' — {note}")
            elif itype == "extra":
                print(f"     {icon_str} Track {track}: '{d_title}' — not on official release")
            else:
                print(f"     {icon_str} Track {track}: {note or d_title}")

        if len(issues) > 8:
            print(f"     ... and {len(issues)-8} more issues")

        if res.get("error"):
            print(f"     {c(CYAN, '!')} {res['error']}")
        print()


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse(files: list[dict]) -> dict:
    canonical     = []
    fixable       = []   # has track number, not full canonical
    bare          = []   # no track number
    junk_folder   = []
    wrong_depth   = []   # not under Music/, or wrong nesting
    root_files    = []   # audio files sitting at storage root (not in Music/)
    junk_names    = set()
    albums_ok     = defaultdict(list)
    albums_issues = defaultdict(list)

    for entry in files:
        path  = entry["path"].replace("\\\\", "\\")
        ftype = entry["type"]

        if not is_audio_type(ftype):
            continue

        segs    = path.split("\\")
        # segs: [WALKMAN, Storage, ...folders..., filename]
        storage  = segs[1] if len(segs) > 1 else ""
        rel_segs = segs[2:]  # after storage

        filename = rel_segs[-1] if rel_segs else ""
        folders  = rel_segs[:-1]

        # Must be under Music/
        if not folders or folders[0].lower() != "music":
            root_files.append(entry | {"_storage": storage, "_folders": folders, "_filename": filename})
            continue

        music_subfolders = folders[1:]  # after Music/

        # Skip system paths
        if any(s.lower() in SKIP_FOLDERS_TOP for s in music_subfolders):
            continue

        # Junk folder check
        in_junk = any(is_junk_folder(sf) for sf in music_subfolders)
        if in_junk:
            junk_names.update(sf for sf in music_subfolders if is_junk_folder(sf))
            junk_folder.append(entry | {"_storage": storage, "_folders": music_subfolders, "_filename": filename})
            continue

        # Depth check — canonical is exactly Music/Artist/Album (depth 2)
        depth = len(music_subfolders)
        if depth == 0:
            wrong_depth.append(entry | {"_storage": storage, "_folders": music_subfolders,
                                        "_filename": filename, "_issue": "no Artist/Album folders"})
            continue
        elif depth == 1:
            wrong_depth.append(entry | {"_storage": storage, "_folders": music_subfolders,
                                        "_filename": filename, "_issue": f"missing Artist folder (only '{music_subfolders[0]}')"})
        elif depth > 2:
            # FIX 19: detect double-Music nesting explicitly.
            # Music/Music/Artist/Album is the most common wrong-depth case;
            # it is caused by the walk_music_folder path bug (now fixed in
            # walkman_reorganize.py) or by an earlier reorganise run that
            # prepended 'Music/' to files already inside Music/.
            if music_subfolders[0].lower() == "music":
                _issue = (
                    f"double-Music nesting (Music/Music/...) — run Reorganise to fix"
                )
            else:
                _issue = f"too deep ({depth} levels: {'/'.join(music_subfolders)})"
            wrong_depth.append(entry | {"_storage": storage, "_folders": music_subfolders,
                                        "_filename": filename, "_issue": _issue})

        # Filename pattern check
        album_key = f"{storage}/{'/'.join(music_subfolders[:2])}"
        if RE_CANONICAL.match(filename):
            canonical.append(entry | {"_storage": storage, "_folders": music_subfolders, "_filename": filename})
            albums_ok[album_key].append(filename)
        elif RE_TRACK_ONLY.match(filename):
            fixable.append(entry | {"_storage": storage, "_folders": music_subfolders, "_filename": filename})
            albums_issues[album_key].append(("fixable", filename))
        else:
            bare.append(entry | {"_storage": storage, "_folders": music_subfolders, "_filename": filename})
            albums_issues[album_key].append(("bare", filename))

    return {
        "canonical":      canonical,
        "fixable":        fixable,
        "bare":           bare,
        "junk_folder":    junk_folder,
        "wrong_depth":    wrong_depth,
        "root_files":     root_files,
        "junk_names":     sorted(junk_names),
        "albums_ok":      albums_ok,
        "albums_issues":  albums_issues,
        "total_audio":    len(canonical) + len(fixable) + len(bare) + len(junk_folder) + len(wrong_depth),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def pct(n, total):
    return f"{100*n//total if total else 0}%"

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"

def c(colour, text):
    return f"{colour}{text}{RESET}"


def print_report(r: dict, show_junk=False, show_bare=False, show_fixable=False,
                 export_issues=None):
    total = r["total_audio"]
    n_ok    = len(r["canonical"])
    n_fix   = len(r["fixable"])
    n_bare  = len(r["bare"])
    n_junk  = len(r["junk_folder"])
    n_depth = len(r["wrong_depth"])
    n_root  = len(r["root_files"])

    print()
    print(c(BOLD, "══════════════════════════════════════════════════════"))
    print(c(BOLD, "  Walkman Library Health Report"))
    print(c(BOLD, "══════════════════════════════════════════════════════"))
    print(f"  Total audio files scanned : {total}")
    print()
    print(f"  {c(GREEN,  '✓ Canonical (fully organised)'):<50} {n_ok:>5}  ({pct(n_ok, total)})")
    print(f"  {c(YELLOW, '~ Fixable (has track #, not canonical)'):<50} {n_fix:>5}  ({pct(n_fix, total)})")
    print(f"  {c(RED,    '✗ Bare title (no track number)'):<50} {n_bare:>5}  ({pct(n_bare, total)})")
    print(f"  {c(RED,    '✗ Junk folder watermark'):<50} {n_junk:>5}  ({pct(n_junk, total)})")
    print(f"  {c(RED,    '✗ Wrong folder depth'):<50} {n_depth:>5}  ({pct(n_depth, total)})")
    print(f"  {c(YELLOW, '⚠ At storage root (not in Music/)'):<50} {n_root:>5}  ({pct(n_root, total)})")
    print()

    # Wrong-depth breakdown
    if r["wrong_depth"]:
        depth_issues = defaultdict(list)
        for e in r["wrong_depth"]:
            depth_issues[e["_issue"]].append(e)
        print(c(BOLD, "  Wrong-depth breakdown:"))
        for issue, items in sorted(depth_issues.items(), key=lambda x: -len(x[1])):
            # Show unique folder names
            folders = sorted({"/".join(e["_folders"]) for e in items})[:5]
            extra   = f"  + {len(folders)-5} more" if len(folders) > 5 else ""
            print(f"    {issue}: {len(items)} file(s)")
            for fld in folders:
                print(f"      Music/{fld}")
            if extra:
                print(f"      {extra}")
        print()

    # Junk folder names
    if r["junk_names"]:
        print(c(BOLD, f"  Junk folder names ({len(r['junk_names'])} unique):"))
        shown = r["junk_names"][:20]
        for n in shown:
            print(f"    {n}")
        if len(r["junk_names"]) > 20:
            print(f"    ... and {len(r['junk_names'])-20} more (use --show-junk)")
        print()

    # Albums with issues
    if r["albums_issues"]:
        print(c(BOLD, f"  Albums with file-naming issues ({len(r['albums_issues'])} folders):"))
        for album_key, issues in sorted(r["albums_issues"].items(),
                                        key=lambda x: -len(x[1]))[:15]:
            bare_c = sum(1 for t, _ in issues if t == "bare")
            fix_c  = sum(1 for t, _ in issues if t == "fixable")
            flags  = []
            if bare_c: flags.append(c(RED, f"{bare_c} bare"))
            if fix_c:  flags.append(c(YELLOW, f"{fix_c} fixable"))
            print(f"    {album_key}  [{', '.join(flags)}]")
        if len(r["albums_issues"]) > 15:
            print(f"    ... and {len(r['albums_issues'])-15} more")
        print()

    # Score
    if total:
        score = int(100 * n_ok / total)
        bar_full = "█" * (score // 5)
        bar_empty = "░" * (20 - score // 5)
        colour = GREEN if score >= 80 else YELLOW if score >= 50 else RED
        print(c(BOLD, "  Organisation score:"))
        print(f"  {c(colour, bar_full)}{bar_empty}  {c(colour, str(score)+'%')}  canonical")
        print()

    # What to do next
    print(c(BOLD, "  Recommended actions:"))
    if n_junk:
        print(f"  1. Run Reorganise — {n_junk} junk-folder files need renaming/moving")
    if n_depth:
        print(f"  2. Run Reorganise — {n_depth} files are at wrong folder depth")
    if n_bare:
        print(f"  3. Run Audit — {n_bare} bare-title files need track numbers filled in")
    if n_fix:
        print(f"  4. Run Audit — {n_fix} files have track# but not canonical filename")
    if n_ok == total:
        print(c(GREEN, "  Library is fully organised!"))
    print()

    # Optional detailed listings
    if show_junk:
        print(c(BOLD, "─── All junk-folder files ───"))
        for e in r["junk_folder"]:
            print(f"  {e['_storage']}/Music/{'/'.join(e['_folders'])}/{e['_filename']}")

    if show_bare:
        print(c(BOLD, "─── All bare-title files (no track number) ───"))
        for e in r["bare"]:
            print(f"  {e['_storage']}/Music/{'/'.join(e['_folders'])}/{e['_filename']}")

    if show_fixable:
        print(c(BOLD, "─── All fixable files (has track#, not canonical) ───"))
        for e in r["fixable"]:
            print(f"  {e['_storage']}/Music/{'/'.join(e['_folders'])}/{e['_filename']}")

    # Export issues CSV
    if export_issues:
        rows = []
        for cat, entries in [
            ("junk_folder", r["junk_folder"]),
            ("wrong_depth", r["wrong_depth"]),
            ("bare_title",  r["bare"]),
            ("fixable",     r["fixable"]),
        ]:
            for e in entries:
                rows.append({
                    "category": cat,
                    "storage":  e.get("_storage", ""),
                    "folder":   "/".join(e.get("_folders", [])),
                    "filename": e.get("_filename", ""),
                    "type":     e.get("type", ""),
                    "issue":    e.get("_issue", ""),
                })
        with open(export_issues, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["category","storage","folder","filename","type","issue"])
            w.writeheader()
            w.writerows(rows)
        print(f"Issues exported to: {export_issues}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Analyse walkman_tree.txt library health")
    ap.add_argument("tree_file", nargs="?", default=str(DEFAULT_TREE),
                    help="Path to walkman_tree.txt (default: Desktop)")
    ap.add_argument("--adb-scan", action="store_true",
                    help=(
                        "Scan the Walkman via ADB before analysing — skips the need to "
                        "run walkman_scan.ps1 or walkman_scan_adb.py separately.  "
                        "Requires USB Debugging enabled on the device and adb in PATH."
                    ))
    ap.add_argument("--adb-serial", default=None, metavar="SERIAL",
                    help="ADB device serial number (if multiple devices connected)")
    ap.add_argument("--adb-music-only", action="store_true",
                    help="With --adb-scan: only scan the Music subfolder (faster)")
    ap.add_argument("--adb-no-sizes", action="store_true",
                    help="With --adb-scan: skip file size fetching (faster)"
                    )
    ap.add_argument("--show-junk",     action="store_true", help="List all junk-folder files")
    ap.add_argument("--show-bare",     action="store_true", help="List all bare-title files")
    ap.add_argument("--show-fixable",  action="store_true", help="List all fixable files")
    ap.add_argument("--export-issues", metavar="FILE",      help="Export all issues to a CSV")
    ap.add_argument("--deep-check",    action="store_true",
                    help="Run AI album audit (track count, title verification, duplicate detection)")
    ap.add_argument("--show-ok",       action="store_true",
                    help="With --deep-check: also show albums that pass")
    ap.add_argument("--workers",       type=int, default=4,
                    help="AI worker threads for --deep-check (default 4)")
    ap.add_argument("--clear-ai-cache", action="store_true",
                    help="Delete the AI response cache before running")
    ap.add_argument("--export-audit",  metavar="FILE",
                    help="With --deep-check: export AI audit results to CSV")
    args = ap.parse_args()

    # Clear AI cache if requested
    if args.clear_ai_cache and AI_CACHE_FILE.exists():
        AI_CACHE_FILE.unlink()
        print(f"AI cache cleared: {AI_CACHE_FILE}")

    tree = Path(args.tree_file)

    # ── ADB scan ─────────────────────────────────────────────────────────────
    # When --adb-scan is given, run the ADB scanner in-process to produce
    # the tree file, then continue with analysis.  This is equivalent to:
    #   python walkman_scan_adb.py --output <tree_file>
    #   python walkman_check.py <tree_file>
    # but in a single command.
    if args.adb_scan:
        try:
            import walkman_scan_adb as _adb_scan
            if args.adb_serial:
                _adb_scan._ADB_SERIAL = args.adb_serial
            print("=" * 65)
            print("ADB scan requested — scanning device before analysis...")
            print("=" * 65)
            if not _adb_scan.check_device():
                print(
                    "\nERROR: No Android device detected via ADB.\n"
                    "  - Enable USB Debugging on the Walkman.\n"
                    "  - Accept the 'Allow USB debugging?' dialog on device.\n"
                    "  - Confirm with: adb devices\n"
                    "  - Or run walkman_scan.ps1 instead and re-run without --adb-scan."
                )
                sys.exit(1)
            storage_paths = _adb_scan.get_storage_paths()
            all_rows = []
            for storage_name, device_path in storage_paths.items():
                rows = _adb_scan.scan_storage(
                    storage_name, device_path,
                    music_only=args.adb_music_only,
                    audio_only=False,
                    fetch_sizes=not args.adb_no_sizes,
                )
                all_rows.extend(rows)
            import csv as _csv
            tree.parent.mkdir(parents=True, exist_ok=True)
            with open(tree, "w", newline="", encoding="utf-8-sig") as _f:
                _w = _csv.DictWriter(_f, fieldnames=["Path", "Type", "Size_MB"],
                                     delimiter="\t", extrasaction="ignore")
                _w.writeheader()
                _w.writerows(all_rows)
            print(f"Scan complete: {len(all_rows)} entries written to {tree}")
        except ImportError:
            print(
                "ERROR: walkman_scan_adb.py not found in the same folder.\n"
                "  Place walkman_scan_adb.py alongside walkman_check.py and retry."
            )
            sys.exit(1)
        except Exception as _scan_err:
            print(f"ADB scan failed: {_scan_err}")
            sys.exit(1)

    if not tree.exists():
        print(f"ERROR: Tree file not found: {tree}")
        if not args.adb_scan:
            print("Run walkman_scan.ps1 or walkman_scan_adb.py first to generate it,")
            print("or use --adb-scan to scan and analyse in one step.")
        sys.exit(1)

    print(f"Reading: {tree}  ({tree.stat().st_size // 1024} KB)")
    files = parse_tree(tree)
    print(f"Parsed {len(files)} entries.")

    result = analyse(files)
    print_report(
        result,
        show_junk    = args.show_junk,
        show_bare    = args.show_bare,
        show_fixable = args.show_fixable,
        export_issues= args.export_issues,
    )

    # Deep AI audit
    if args.deep_check:
        ai_results = run_deep_check(result, workers=args.workers)
        print_deep_check_report(ai_results, show_ok=args.show_ok)

        if args.export_audit and ai_results:
            rows = []
            for res in ai_results:
                base = {
                    "artist":         res.get("artist", ""),
                    "album":          res.get("album", ""),
                    "verdict":        res.get("verdict", ""),
                    "confidence":     res.get("confidence", ""),
                    "device_count":   res.get("device_count", ""),
                    "official_count": res.get("official_count", ""),
                }
                issues = res.get("issues", [])
                if not issues:
                    rows.append(base | {"issue_type": "", "track": "",
                                        "device_title": "", "expected_title": "", "note": ""})
                for iss in issues:
                    rows.append(base | {
                        "issue_type":    iss.get("type", ""),
                        "track":         iss.get("track", ""),
                        "device_title":  iss.get("device_title", ""),
                        "expected_title":iss.get("expected_title", ""),
                        "note":          iss.get("note", ""),
                    })
            fields = ["artist","album","verdict","confidence","device_count","official_count",
                      "issue_type","track","device_title","expected_title","note"]
            with open(args.export_audit, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
            print(f"Audit results exported to: {args.export_audit}")


if __name__ == "__main__":
    main()
