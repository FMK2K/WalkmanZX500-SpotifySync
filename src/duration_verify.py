"""
duration_verify.py — mislabeled-file sweep via duration cross-check.

2026-09-04: built after confirming live that Brent Faiyaz's on-device
"ROLLING STONE" was actually The Weeknd's track of the same title (duration
matched The Weeknd's real file to within 0.06s; the real Brent Faiyaz track
is 68s shorter). Download-time title-only matching can silently grab the
wrong artist's song when titles collide. This is the systemic check: for
every on-device (artist, album) group, fetch the real tracklist + per-track
duration from Deezer's public API (no auth needed — see
album_completeness.py's module note on why Deezer, not Spotify), and flag
any local file whose duration differs from its reference by more than a
threshold.

Local duration is read from a HEADER-ONLY pull (adb_transfer's existing
read_file_header_from_device, default 1.5MB), not a full download — this
pipeline's ffmpeg-produced files (Xing header for MP3, STREAMINFO block for
FLAC, both near the file start) yield the exact duration from just the
header. Confirmed live: a 512KB pull reported the identical duration
(229.896s) to a full-file pull, both via mutagen. A truncation-safety check
still guards against the rare file where the header alone isn't enough (see
_local_duration).

Resumable/checkpointed like walkman_favplaylist_apply.py — this sweeps
~3800 (artist,album) groups / ~12,500 files and will run for hours.

Usage:
    python duration_verify.py sweep [--limit N]
    python duration_verify.py status
"""
import argparse, json, re, sys, time, unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import album_completeness as ac
import walkman_dedup as wd
import adb_transfer as adb

ROOT = Path(__file__).resolve().parent.parent / "state"   # <repo>/state (src/ reorg)
STATE_PATH = ROOT / "duration_verify_state.json"
REPORT_PATH = ROOT / "duration_verify_report.json"
LOG_PATH = ROOT / "duration_verify.log"

# Flag a mismatch only past this margin, whichever is larger -- generous
# enough to tolerate legitimate variance (radio edits, remaster timing,
# different encoders' silence-trimming) while still catching real
# wrong-track swaps, which tend to be tens of seconds off or more.
MISMATCH_ABS_S = 20
MISMATCH_REL = 0.12

_HEADER_TMP = ROOT / "_dv_header_tmp"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"groups_done": [], "artist_album_cache": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def load_report() -> list:
    if REPORT_PATH.exists():
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    return []


def save_report(report: list) -> None:
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def _local_duration(storage: str, parts: list, filename: str, real_size: int) -> float | None:
    """
    Pull just the header and read duration via mutagen. Guards against the
    truncation trap: for a CBR file with no Xing/VBRI header, mutagen would
    compute duration from the LOCAL (truncated) file's size rather than the
    real remote size, silently under-reporting. Detect that case (parsed
    duration implausibly short vs what bitrate+real_size implies) and
    recompute from the real size instead; trust mutagen's own figure
    otherwise (correct for FLAC's STREAMINFO and for a Xing/VBRI header,
    both self-contained regardless of truncation).
    """
    _HEADER_TMP.parent.mkdir(parents=True, exist_ok=True)
    ok = adb.read_file_header_from_device(
        src_parts=parts, filename=filename, dest_local=_HEADER_TMP,
        device_name=config.DEVICE_NAME, storage_name=storage,
        max_bytes=1_500_000,
    )
    if not ok or not _HEADER_TMP.exists():
        return None
    try:
        import mutagen
        m = mutagen.File(_HEADER_TMP)
        if not m or not m.info:
            return None
        dur = float(m.info.length or 0)
        bitrate = getattr(m.info, "bitrate", 0) or 0
        local_size = _HEADER_TMP.stat().st_size
        # Suspicious-truncation guard: if the header pull already captured
        # the WHOLE file (local_size >= real_size), mutagen's figure is
        # already correct regardless of method. Otherwise, if the reported
        # duration roughly matches what the TRUNCATED size alone would give
        # at this bitrate (rather than the full remote size), mutagen used
        # filesize-based estimation on the truncated file -- recompute from
        # the real size instead.
        if local_size < real_size and bitrate > 0:
            dur_from_local_size = local_size * 8 / bitrate
            if abs(dur - dur_from_local_size) < 2.0:
                dur = real_size * 8 / bitrate
        return dur if dur > 0 else None
    except Exception as e:
        log(f"  [warn] mutagen parse failed for {filename}: {e}")
        return None
    finally:
        try:
            _HEADER_TMP.unlink(missing_ok=True)
        except Exception:
            pass


def _artist_album_for_file(f: dict) -> tuple:
    """
    2026-09-05: wd._parse_folder_artist_album() alone left 7473/12974 files
    (57.6%!) unclassified -- confirmed live -- whenever a file sits in a bare
    "Music/<Album>/" folder with no artist-level parent AND no properly-
    parented sibling folder to fall back to (a splintered-duplicate layout;
    "Hirosamii - Skyf Choef - 01 Skyf Choef.flac" under bare "Music/Skyf
    Choef/" was the case that surfaced this — a real mislabeled file the
    folder-only grouping never even looked at). Filenames in this pipeline
    consistently encode "Artist - Album - NN Title.ext" even when the folder
    structure doesn't, so fall back to parsing the filename itself via
    wd._parse_filename() when the folder-based extraction comes up empty.
    """
    artist, album = wd._parse_folder_artist_album(f["parts"])
    if artist and album:
        return artist, album
    parts, _ = wd._parse_filename(f["filename"])
    if len(parts) >= 3:
        return parts[0].strip(), parts[1].strip()
    return artist, album


def sweep(limit: int | None = None, fresh: bool = False):
    log("=== duration sweep: scanning device library ===")
    if fresh:
        # a pipeline run has just added / replaced / renamed files — the
        # per-group "done" flags are stale. Clear them (keep the tracklist
        # cache; that's keyed by artist/album and still valid) and re-verify
        # everything. Still checkpoints during the run so an ADB drop resumes.
        st = load_state()
        st["groups_done"] = []
        save_state(st)
        save_report([])
        log("  --fresh: cleared per-group done-state, full re-sweep")
    files, sizes = wd._bulk_device_scan(wd._default_storages())
    log(f"  {len(files)} audio file(s) on device")

    groups: dict = {}
    for f in files:
        artist, album = _artist_album_for_file(f)
        if not artist or not album:
            continue
        key = f"{artist.strip().lower()}|{album.strip().lower()}"
        groups.setdefault(key, {"artist": artist, "album": album, "files": []})
        size = sizes.get((f["storage"], tuple(f["parts"]), f["filename"]), 0)
        groups[key]["files"].append({**f, "size": size})

    state = load_state()
    done = set(state["groups_done"])
    artist_album_cache = state.get("artist_album_cache", {})
    report = load_report()

    todo_keys = [k for k in groups if k not in done]
    if limit:
        todo_keys = todo_keys[:limit]
    log(f"  {len(groups)} (artist,album) group(s) total, {len(todo_keys)} to process "
        f"({len(done)} already done)")

    for i, key in enumerate(todo_keys, 1):
        g = groups[key]
        artist, album = g["artist"], g["album"]
        try:
            artist_key = ac._norm(artist)
            if artist_key not in artist_album_cache:
                albums = ac.fetch_artist_albums(artist, include_types=("album", "ep", "single"))
                artist_album_cache[artist_key] = albums
            albums = artist_album_cache[artist_key]

            target_album_n = ac._norm(album)
            match = next((a for a in albums if ac._norm(a["name"]) == target_album_n), None)
            if not match:
                # loose fallback: containment either direction
                match = next((a for a in albums
                             if target_album_n in ac._norm(a["name"])
                             or ac._norm(a["name"]) in target_album_n), None)
            if not match:
                done.add(key)
                continue

            ref_tracks = ac.fetch_album_tracklist(match["id"])
            if not ref_tracks:
                done.add(key)
                continue
            ref_by_title = {ac._norm(t["title"]): t for t in ref_tracks if t.get("title")}

            for f in g["files"]:
                stem = Path(f["filename"]).stem
                bits = [b.strip() for b in stem.split(" - ")]
                title = re.sub(r"^\d{1,3}\s+", "", bits[-1]).strip() if bits else stem
                title = re.sub(r"^\d{1,3}[\s\-\.]+", "", title)
                ref = ref_by_title.get(ac._norm(title))
                if not ref or not ref.get("duration_s"):
                    continue
                ref_s = ref["duration_s"]
                local_s = _local_duration(f["storage"], f["parts"], f["filename"], f["size"])
                if local_s is None:
                    continue
                diff = abs(local_s - ref_s)
                threshold = max(MISMATCH_ABS_S, ref_s * MISMATCH_REL)
                if diff > threshold:
                    entry = {
                        "artist": artist, "album": album, "title": ref["title"],
                        "storage": f["storage"], "parts": f["parts"], "filename": f["filename"],
                        "local_duration_s": round(local_s, 1), "reference_duration_s": ref_s,
                        "diff_s": round(diff, 1),
                    }
                    report.append(entry)
                    log(f"  SUSPECT {artist} -- {album} -- {ref['title']}: "
                        f"local={local_s:.1f}s ref={ref_s}s diff={diff:.1f}s")
        except Exception as e:
            log(f"  EXCEPTION on {artist!r}/{album!r}: {e}")
        finally:
            done.add(key)

        if i % 20 == 0:
            state["groups_done"] = list(done)
            state["artist_album_cache"] = artist_album_cache
            save_state(state)
            save_report(report)
            log(f"  ... checkpoint {i}/{len(todo_keys)} ({len(report)} suspect(s) so far)")

    state["groups_done"] = list(done)
    state["artist_album_cache"] = artist_album_cache
    save_state(state)
    save_report(report)
    log(f"=== duration sweep run complete: {len(done)}/{len(groups)} group(s) done, "
        f"{len(report)} total suspect file(s) -> {REPORT_PATH} ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sweep", "status"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fresh", action="store_true",
                    help="clear per-group done-state and re-verify everything "
                         "(use after a run that added/renamed files)")
    args = ap.parse_args()
    if args.cmd == "sweep":
        sweep(limit=args.limit, fresh=args.fresh)
    else:
        st = load_state()
        rep = load_report()
        print(f"groups_done={len(st['groups_done'])} suspects={len(rep)}")
