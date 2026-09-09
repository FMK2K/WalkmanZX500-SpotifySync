"""
snapshot_cross_check.py
Cross-references actual file tags (from walkman_tag_snapshot.json) against
all project caches.  Falls back to MediaStore snapshot when tag snapshot is
absent (run walkman_tag_scan.py first for the most accurate results).

Run:   python snapshot_cross_check.py
Out:   walkman_audit_report.json + console summary
"""

import json, re, subprocess, sys, unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT       = Path(__file__).parent
SNAPSHOT      = PROJECT / "walkman_snapshot.json"       # MediaStore (fast, used for paths/durations)
TAG_SNAPSHOT  = PROJECT / "walkman_tag_snapshot.json"   # Actual file tags (accurate, player-level)
META_CACHE    = PROJECT / ".metadata_cache.json"
SPOTIFY_CACHE = PROJECT / ".spotify_metadata_cache.json"
GENRE_CACHE   = PROJECT / ".genre_cache.json"
SCORES_CACHE  = PROJECT / ".streaming_scores_cache.json"
DEDUP_PLAN    = PROJECT / ".dedup_plan.json"
OUT           = PROJECT / "walkman_audit_report.json"

FORMAT_TIER = {
    ".dsf": 95, ".dff": 95,
    ".flac": 90, ".m4a": 82, ".aac": 80, ".ogg": 75,
    ".opus": 74, ".wav": 70, ".mp3": 50, ".wma": 20,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNICODE_ASCII_MAP = str.maketrans({
    "…": "...", "'": "'", "'": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "¿": "?", "¡": "!", "«": '"', "»": '"',
    "×": "x", "÷": "/", "•": "-", "æ": "ae", "Æ": "AE",
    "ø": "o", "Ø": "O",
})

def _norm(text: str) -> str:
    """Mirrors device_library._norm(): NFKD→ASCII→lower→strip articles→collapse ws."""
    text = str(text or "").translate(_UNICODE_ASCII_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    text = re.sub(r"^(the |a |an )", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def _norm_key(artist: str, title: str) -> str:
    return f"{_norm(artist)}|{_norm(title)}"


def _load(path: Path) -> dict | list:
    if not path.exists():
        print(f"  (missing: {path.name})")
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _adb(args: list[str]) -> str:
    r = subprocess.run(["adb"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else ""


def _parse_content_rows(raw: str) -> list[dict]:
    rows = []
    for line in raw.splitlines():
        m = re.match(r"Row: \d+ (.+)", line)
        if not m:
            continue
        entry: dict = {}
        for pair in re.split(r", (?=[a-z_]+=)", m.group(1)):
            k, _, v = pair.partition("=")
            entry[k.strip()] = None if v.strip() == "NULL" else v.strip()
        rows.append(entry)
    return rows


def _title_tokens_in_stem(title: str, stem: str) -> float:
    """Fraction of title tokens that appear in the filename stem."""
    tok = lambda s: set(re.split(r"[\s\-_.()\[\]&]+", s.lower())) - {"", "feat", "ft"}
    ta, tb = tok(title), tok(stem)
    return len(ta & tb) / len(ta) if ta else 1.0


def _tag_field(ts: dict, field: str, ms_fallback: dict, ms_field: str) -> str:
    """Read a tag field from the tag snapshot if available, else from MediaStore entry."""
    v = ts.get(field)
    if v is None:
        v = ms_fallback.get(ms_field)
    return (v or "").strip()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_duplicates(snapshot: list[dict], tag_snap: dict | None = None) -> list[dict]:
    """Same normalized artist+title on device more than once."""
    if tag_snap is None:
        tag_snap = {}
    groups: dict[str, list] = defaultdict(list)
    for t in snapshot:
        ts     = tag_snap.get(t["_data"], {})
        artist = ts.get("tag_artist") or t.get("artist") or ""
        title  = ts.get("tag_title")  or t.get("title")  or ""
        key = _norm_key(artist, title)
        groups[key].append(t)

    results = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        ranked = sorted(
            members,
            key=lambda t: (FORMAT_TIER.get(t["ext"], 0), int(t.get("duration") or 0)),
            reverse=True,
        )
        keep = ranked[0]
        results.append({
            "key":    key,
            "keep":   {"path": keep["_data"], "ext": keep["ext"],
                       "tier": FORMAT_TIER.get(keep["ext"], 0),
                       "duration": keep.get("duration_minsec")},
            "delete": [{"path": t["_data"], "ext": t["ext"],
                        "tier": FORMAT_TIER.get(t["ext"], 0),
                        "duration": t.get("duration_minsec")}
                       for t in ranked[1:]],
        })
    return results


def check_tag_drift(snapshot: list[dict], meta: dict, tag_snap: dict) -> list[dict]:
    """
    Actual file tags (what the HiRes player reads) vs what .metadata_cache.json expects.
    When tag_snap is available, uses real ID3/FLAC values rather than MediaStore cache.
    """
    # (tag_snapshot field, metadata_cache field)
    FIELDS = [
        ("tag_title",        "title"),
        ("tag_artist",       "artist"),
        ("tag_album",        "album"),
        ("tag_album_artist", "album_artist"),
        ("tag_year",         "year"),
        ("tag_genre",        "genre"),
        ("tag_track",        "track_number"),
    ]
    results = []
    for t in snapshot:
        ck    = (t.get("cache_key") or "").lower()
        entry = meta.get(ck)
        if not entry:
            continue
        ts = tag_snap.get(t["_data"], {})
        diffs: dict = {}
        for tag_f, cache_f in FIELDS:
            # Prefer actual file tag; fall back to MediaStore
            ms_map = {"tag_title": "title", "tag_artist": "artist", "tag_album": "album",
                      "tag_album_artist": "album_artist", "tag_year": "year",
                      "tag_genre": "genre", "tag_track": "track"}
            dev_val = (ts.get(tag_f) or t.get(ms_map.get(tag_f, ""), "") or "").strip()
            exp_val = (entry.get(cache_f) or "").strip()
            if dev_val and exp_val and _norm(dev_val) != _norm(exp_val):
                diffs[tag_f.replace("tag_", "")] = {"on_device": dev_val, "expected": exp_val}
        if diffs:
            results.append({"path": t["_data"], "title": t.get("title"),
                            "artist": t.get("artist"), "diffs": diffs})
    return results


def check_hires_player_issues(snapshot: list[dict], tag_snap: dict) -> dict:
    """
    Issues that affect how the ZX500 HiRes player displays or plays each track.
    Grouped by issue type for easy triage.
    """
    id3v4, no_header, no_art, png_cover, prog_jpeg, big_apic = [], [], [], [], [], []

    for t in snapshot:
        ts     = tag_snap.get(t["_data"], {})
        entry  = {"path": t["_data"], "title": t.get("title"), "artist": t.get("artist"),
                  "id3_version": ts.get("id3_version")}

        if str(ts.get("id3_version") or "").endswith(".4"):
            id3v4.append(entry)

        if any("no ID3 header" in i for i in ts.get("issues", [])):
            no_header.append(entry)

        if not ts.get("has_apic") and not ts.get("error"):
            no_art.append({**entry, "ext": t["ext"]})

        if ts.get("apic_mime") == "image/png":
            png_cover.append({**entry, "apic_size_kb": ts.get("apic_size_kb")})

        if ts.get("apic_progressive"):
            prog_jpeg.append({**entry, "apic_size_kb": ts.get("apic_size_kb")})

        if (ts.get("apic_size_kb") or 0) > 90 and ts.get("apic_mime") == "image/jpeg":
            big_apic.append({**entry, "apic_size_kb": ts.get("apic_size_kb")})

    return {
        "id3v4_blank_in_player": id3v4,
        "no_id3_header":         no_header,
        "no_embedded_art":       no_art,
        "png_cover_rejected":    png_cover,
        "progressive_jpeg":      prog_jpeg,
        "oversized_apic_gt90kb": big_apic,
    }


def check_mislabeled(snapshot: list[dict], meta: dict, spotify: dict,
                     tag_snap: dict) -> list[dict]:
    """Tracks where filename↔title overlap is low, or Spotify reference disagrees."""
    results = []
    for t in snapshot:
        issues = []
        stem  = Path(t["_data"]).stem
        ts    = tag_snap.get(t["_data"], {})

        # Use actual file tag if available, else MediaStore
        title      = (ts.get("tag_title")        or t.get("title")        or "").strip()
        dev_artist = (ts.get("tag_artist")        or t.get("artist")       or "").strip()
        dev_album  = (ts.get("tag_album")         or t.get("album")        or "").strip()

        if _title_tokens_in_stem(title, stem) < 0.25 and len(title.split()) >= 2:
            issues.append(f"filename '{stem}' shares <25% tokens with title tag '{title}'")

        ck      = (t.get("cache_key") or "").lower()
        mc      = meta.get(ck, {})
        spot_id = mc.get("spotify_id")
        if spot_id:
            sp = spotify.get(spot_id, {})
            if sp:
                if sp.get("title") and _norm(sp["title"]) != _norm(title):
                    issues.append(f"title='{title}' but Spotify expects '{sp['title']}'")
                if sp.get("artist") and _norm(sp["artist"]) != _norm(dev_artist):
                    issues.append(f"artist='{dev_artist}' but Spotify expects '{sp['artist']}'")
                if sp.get("album") and _norm(sp["album"]) != _norm(dev_album):
                    issues.append(f"album='{dev_album}' but Spotify expects '{sp['album']}'")

        ms = int(t.get("duration") or 0)
        if ms == 0:
            issues.append("duration=0 (corrupt or unreadable file)")
        elif ms < 60_000:
            issues.append(f"only {t.get('duration_minsec')} long (likely truncated or wrong file)")

        if issues:
            results.append({"path": t["_data"], "title": title, "artist": dev_artist,
                            "duration": t.get("duration_minsec"), "issues": issues})
    return results


def check_orphans(snapshot: list[dict], meta: dict) -> list[dict]:
    """Tracks on device with no entry in .metadata_cache.json."""
    return [
        {"path": t["_data"], "title": t.get("title"), "artist": t.get("artist"),
         "album": t.get("album"), "ext": t["ext"], "storage": t.get("storage")}
        for t in snapshot
        if (t.get("cache_key") or "").lower() not in meta
    ]


def check_dedup_pending(snapshot: list[dict], dedup_plan: dict) -> list[dict]:
    """Dedup plan candidates that are still present on the device."""
    snap_paths = {t["_data"] for t in snapshot}
    sd_prefix  = next(
        (re.match(r"(/storage/[^/]+)", t["_data"]).group(1)
         for t in snapshot if t.get("storage") == "sd"
         and re.match(r"(/storage/[^/]+)", t["_data"])),
        "",
    )

    def _resolve(hp: str) -> str:
        if hp.startswith("Internal shared storage/"):
            return "/storage/emulated/0/" + hp[len("Internal shared storage/"):]
        if hp.startswith("SD card/") and sd_prefix:
            return sd_prefix + "/" + hp[len("SD card/"):]
        return hp

    return [
        {"path": _resolve(c["human_path"]), "reason": c.get("reason"),
         "category": c.get("category"), "superior_path": c.get("superior_path"),
         "size_bytes": c.get("size")}
        for c in dedup_plan.get("candidates", [])
        if _resolve(c.get("human_path", "")) in snap_paths
    ]


def check_missing_art_by_album(snapshot: list[dict], tag_snap: dict) -> list[dict]:
    """
    Albums where every track lacks an embedded APIC frame.
    Uses actual file tag data — what the HiRes player sees directly.
    Falls back to ADB album table query when tag_snap is empty.
    """
    if tag_snap:
        # Group tracks by album; check APIC presence per track
        album_tracks: dict[str, list] = defaultdict(list)
        for t in snapshot:
            album_tracks[_norm(t.get("album") or "")].append(t)

        results = []
        for norm_album, tracks in album_tracks.items():
            no_art_tracks = [
                t for t in tracks
                if not tag_snap.get(t["_data"], {}).get("has_apic")
                and not tag_snap.get(t["_data"], {}).get("error")
            ]
            if not no_art_tracks:
                continue
            sample = tracks[0]
            results.append({
                "album":             sample.get("album"),
                "artist":            sample.get("artist"),
                "tracks_missing_art": len(no_art_tracks),
                "total_tracks":      len(tracks),
                "all_missing":       len(no_art_tracks) == len(tracks),
                "affected_paths":    [t["_data"] for t in no_art_tracks],
            })
        return sorted(results, key=lambda x: -x["tracks_missing_art"])

    # Fallback: Android album table (MediaStore — may be stale for HiRes player)
    print("  (no tag snapshot — falling back to Android album table, less accurate)")
    raw = _adb(["shell", "content", "query",
                "--uri", "content://media/external/audio/albums",
                "--projection", "_id,album,artist,album_art"])
    album_count: dict[str, int] = defaultdict(int)
    for t in snapshot:
        album_count[_norm(t.get("album") or "")] += 1

    return [
        {"album": row.get("album"), "artist": row.get("artist"),
         "track_count": album_count.get(_norm(row.get("album") or ""), 0),
         "all_missing": True}
        for row in _parse_content_rows(raw)
        if row.get("album_art") is None
    ]


def check_playlists(snapshot: list[dict]) -> list[dict]:
    """Enumerate playlists; flag orphaned entries (deleted files still referenced)."""
    snap_by_id = {t["_id"]: t for t in snapshot if t.get("_id")}
    raw = _adb(["shell", "content", "query",
                "--uri", "content://media/external/audio/playlists",
                "--projection", "_id,name"])
    results = []
    for row in _parse_content_rows(raw):
        pid, pname = row.get("_id"), row.get("name")
        if not pid:
            continue
        raw2 = _adb(["shell", "content", "query",
                     "--uri", f"content://media/external/audio/playlists/{pid}/members",
                     "--projection", "audio_id"])
        members, orphaned = [], []
        for mr in _parse_content_rows(raw2):
            aid = mr.get("audio_id")
            if not aid:
                continue
            t = snap_by_id.get(aid)
            if t:
                members.append({"audio_id": aid, "title": t.get("title"),
                                "artist": t.get("artist")})
            else:
                orphaned.append(aid)
        results.append({"playlist": pname, "track_count": len(members),
                        "tracks": members, "orphaned_ids": orphaned})
    return results


def check_genre_mismatches(snapshot: list[dict], genre_cache: dict,
                            tag_snap: dict) -> list[dict]:
    """
    Compare actual TCON frame value (what the player shows) vs genre_cache expected value.
    Uses file tags when available; MediaStore genre as fallback.
    """
    results = []
    for t in snapshot:
        ck = (t.get("cache_key") or "").lower()
        gc = genre_cache.get(ck)
        if not gc:
            continue
        ts        = tag_snap.get(t["_data"], {})
        dev_genre = (ts.get("tag_genre") or t.get("genre") or "").strip()
        exp_genre = (gc.get("genre") or "").strip()
        if exp_genre and _norm(dev_genre) != _norm(exp_genre):
            results.append({"path": t["_data"], "title": t.get("title"),
                            "artist": t.get("artist"),
                            "tcon_on_device": dev_genre, "expected_genre": exp_genre,
                            "source": "file_tag" if ts.get("tag_genre") else "mediastore"})
    return results


def check_track_number_issues(snapshot: list[dict], tag_snap: dict) -> list[dict]:
    results = []
    for t in snapshot:
        ts    = tag_snap.get(t["_data"], {})
        track = ts.get("tag_track") or t.get("track")
        issues = []
        if not track:
            issues.append("missing track number")
        else:
            # Strip "N/total" format → just take numerator
            n_str = str(track).split("/")[0].strip()
            try:
                n = int(n_str)
                if n == 0:
                    issues.append("track number is 0")
                elif n >= 1000:
                    disc, tno = n // 1000, n % 1000
                    issues.append(
                        f"compound track number {n} — likely disc {disc} track {tno} "
                        f"(ZX500 may display incorrectly)"
                    )
                elif n > 500:
                    issues.append(
                        f"unusually high track number {n} — verify this is not a compound encoding"
                    )
            except ValueError:
                issues.append(f"non-numeric track value '{track}'")
        if issues:
            results.append({"path": t["_data"], "title": t.get("title"),
                            "track": track, "issues": issues})
    return results


def genre_distribution(snapshot: list[dict], tag_snap: dict) -> dict[str, int]:
    """Genre counts by actual TCON frame value (or MediaStore fallback)."""
    dist: dict[str, int] = {}
    for t in snapshot:
        ts = tag_snap.get(t["_data"], {})
        g  = (ts.get("tag_genre") or t.get("genre") or "").strip() or "(unset)"
        dist[g] = dist.get(g, 0) + 1
    return dict(sorted(dist.items(), key=lambda x: -x[1]))


def enrich_streaming(snapshot: list[dict], meta: dict, scores: dict) -> dict:
    enriched = []
    for t in snapshot:
        ck      = (t.get("cache_key") or "").lower()
        spot_id = meta.get(ck, {}).get("spotify_id")
        sc      = scores.get(f"spotify:track:{spot_id}", {}) if spot_id else {}
        if not sc:
            continue
        enriched.append({
            "path":           t["_data"],
            "title":          t.get("title"),
            "artist":         t.get("artist"),
            "play_count":     sc.get("play_count", 0),
            "recent_12m":     sc.get("recent_12m", 0),
            "skip_rate":      sc.get("skip_rate", 0),
            "priority_score": sc.get("priority_score", 0),
        })
    by_plays = sorted(enriched, key=lambda x: x["play_count"], reverse=True)
    return {
        "top_played":     by_plays[:10],
        "never_played":   [e for e in enriched if e["play_count"] == 0],
        "high_skip_rate": sorted(
            [e for e in enriched if (e["skip_rate"] or 0) >= 0.7],
            key=lambda x: x["skip_rate"], reverse=True,
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not SNAPSHOT.exists():
        sys.exit("walkman_snapshot.json not found — run walkman_tree_gen.py first.")

    using_tag_snap = TAG_SNAPSHOT.exists()
    if not using_tag_snap:
        print("WARNING: walkman_tag_snapshot.json not found.")
        print("  Genre/art checks will use MediaStore data (less accurate — run walkman_tag_scan.py).\n")

    print("Loading caches…")
    snapshot    = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    tag_snap    = _load(TAG_SNAPSHOT) if using_tag_snap else {}
    meta        = _load(META_CACHE)
    spotify     = _load(SPOTIFY_CACHE)
    genre_cache = _load(GENRE_CACHE)
    scores      = _load(SCORES_CACHE)
    dedup_plan  = _load(DEDUP_PLAN)
    print(f"  snapshot: {len(snapshot)} tracks  |  tag scan: {'yes' if using_tag_snap else 'no (less accurate)'}")

    print("Checking duplicates…")
    duplicates = check_duplicates(snapshot, tag_snap)

    print("Checking tag drift (file tags vs metadata cache)…")
    tag_drifts = check_tag_drift(snapshot, meta, tag_snap)

    print("Checking HiRes player compatibility issues…")
    hires_issues = check_hires_player_issues(snapshot, tag_snap)

    print("Checking for mislabels / misdownloads…")
    mislabeled = check_mislabeled(snapshot, meta, spotify, tag_snap)

    print("Finding pipeline orphans…")
    orphans = check_orphans(snapshot, meta)

    print("Checking dedup candidates still on device…")
    dedup_pending = check_dedup_pending(snapshot, dedup_plan)

    print("Checking missing album art…")
    missing_art = check_missing_art_by_album(snapshot, tag_snap)

    print("Querying playlists via ADB…")
    playlists = check_playlists(snapshot)

    print("Checking genre mismatches (TCON vs genre cache)…")
    genre_mismatches = check_genre_mismatches(snapshot, genre_cache, tag_snap)

    print("Checking track number anomalies…")
    track_issues = check_track_number_issues(snapshot, tag_snap)

    print("Building genre distribution…")
    genre_dist = genre_distribution(snapshot, tag_snap)

    print("Enriching streaming stats…")
    streaming = enrich_streaming(snapshot, meta, scores)

    # ── Summaries ──
    delete_count  = sum(len(d["delete"]) for d in duplicates)
    orphaned_pl   = sum(len(p["orphaned_ids"]) for p in playlists)
    top_title     = f"\"{streaming['top_played'][0]['title']}\"" if streaming["top_played"] else "N/A"
    albums_all_missing = sum(1 for a in missing_art if a.get("all_missing"))

    hi = hires_issues
    report = {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "snapshot_total":      len(snapshot),
        "tag_scan_used":       using_tag_snap,
        "summary": {
            "duplicate_sets":              len(duplicates),
            "files_to_delete":             delete_count,
            "tag_drifts":                  len(tag_drifts),
            "mislabeled":                  len(mislabeled),
            "pipeline_orphans":            len(orphans),
            "dedup_pending":               len(dedup_pending),
            "albums_with_missing_art":     len(missing_art),
            "albums_all_art_missing":      albums_all_missing,
            "playlists":                   len(playlists),
            "orphaned_playlist_members":   orphaned_pl,
            "genre_mismatches":            len(genre_mismatches),
            "track_number_issues":         len(track_issues),
            "never_played":                len(streaming["never_played"]),
            "high_skip_rate":              len(streaming["high_skip_rate"]),
            # HiRes player issues
            "id3v4_blank_in_player":       len(hi["id3v4_blank_in_player"]),
            "no_id3_header":               len(hi["no_id3_header"]),
            "tracks_no_art":               len(hi["no_embedded_art"]),
            "png_cover_rejected":          len(hi["png_cover_rejected"]),
            "progressive_jpeg":            len(hi["progressive_jpeg"]),
            "oversized_apic_gt90kb":       len(hi["oversized_apic_gt90kb"]),
        },
        "hires_player_issues": hires_issues,
        "duplicates":          duplicates,
        "tag_drifts":          tag_drifts,
        "mislabeled":          mislabeled,
        "pipeline_orphans":    orphans,
        "dedup_pending":       dedup_pending,
        "missing_art_albums":  missing_art,
        "playlists":           playlists,
        "genre_mismatches":    genre_mismatches,
        "genre_distribution":  genre_dist,
        "track_number_issues": track_issues,
        "streaming":           streaming,
    }

    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    w = 28
    print()
    print(f"=== HiRes Player Issues (file-level) ===")
    print(f"{'[ID3v2.4 → BLANK in player]':<{w}} {len(hi['id3v4_blank_in_player'])}")
    print(f"{'[No ID3 header]':<{w}} {len(hi['no_id3_header'])}")
    print(f"{'[No embedded art]':<{w}} {len(hi['no_embedded_art'])} tracks ({albums_all_missing} albums fully missing)")
    print(f"{'[PNG cover rejected]':<{w}} {len(hi['png_cover_rejected'])}")
    print(f"{'[Progressive JPEG]':<{w}} {len(hi['progressive_jpeg'])}")
    print(f"{'[APIC >90 KB]':<{w}} {len(hi['oversized_apic_gt90kb'])}")
    print()
    print(f"=== Cache Comparison ===")
    print(f"{'[DUPLICATES]':<{w}} {len(duplicates)} sets  ({delete_count} files to delete)")
    print(f"{'[TAG DRIFT]':<{w}} {len(tag_drifts)} tracks")
    print(f"{'[GENRE MISMATCH]':<{w}} {len(genre_mismatches)} tracks (TCON vs genre cache)")
    print(f"{'[MISLABELED]':<{w}} {len(mislabeled)} suspicious")
    print(f"{'[ORPHANS]':<{w}} {len(orphans)} not in pipeline caches")
    print(f"{'[DEDUP PENDING]':<{w}} {len(dedup_pending)} candidates still on device")
    print(f"{'[PLAYLISTS]':<{w}} {len(playlists)},  {orphaned_pl} orphaned members")
    print(f"{'[TRACK# ISSUES]':<{w}} {len(track_issues)}")
    print(f"{'[STREAMING]':<{w}} top: {top_title}  |  {len(streaming['never_played'])} never played  |  {len(streaming['high_skip_rate'])} high-skip")
    print()
    print(f"Genre distribution (top 10 by TCON):")
    for genre, cnt in list(genre_dist.items())[:10]:
        print(f"  {cnt:>5}  {genre}")
    print(f"\nFull report  ->  {OUT.name}")


if __name__ == "__main__":
    main()
