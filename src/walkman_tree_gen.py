import subprocess, json, re, sys
from pathlib import Path

PROJECTION = "_id,_data,title,artist,album,album_artist,year,track,duration,mime_type"
OUT = Path(__file__).resolve().parent.parent / "state" / "walkman_snapshot.json"


def _adb(args, **kw):
    return subprocess.run(
        ["adb"] + args, capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kw
    )


def _parse_content_rows(raw):
    rows = []
    for line in raw.splitlines():
        m = re.match(r"Row: \d+ (.+)", line)
        if not m:
            continue
        entry = {}
        for pair in re.split(r", (?=[a-z_]+=)", m.group(1)):
            k, _, v = pair.partition("=")
            entry[k.strip()] = None if v.strip() == "NULL" else v.strip()
        rows.append(entry)
    return rows


def query_mediastore():
    r = _adb([
        "shell", "content", "query",
        "--uri", "content://media/external/audio/media",
        "--projection", PROJECTION,
    ])
    if r.returncode != 0 or "Exception" in r.stderr:
        # 2026-07-30: was sys.exit() -- SystemExit is a BaseException, not an
        # Exception, so spiral_orchestrator.py's _run_device_scan() `except
        # Exception` wrapper around this call never caught it. That let an
        # ADB-offline failure here kill the entire cli_runner.py process
        # (exit 1) even after a full audit/reorganize/full run had already
        # completed successfully -- confirmed live, see progress.md
        # "Session: 2026-07-30". Raising instead makes the existing wrapper
        # actually work as designed.
        raise RuntimeError(f"ADB MediaStore query failed:\n{r.stderr or r.stdout}")
    return r.stdout


def query_genres():
    """Return {audio_id: genre_name} by joining audio_genres + their members."""
    r = _adb([
        "shell", "content", "query",
        "--uri", "content://media/external/audio/genres",
        "--projection", "_id,name",
    ])
    if r.returncode != 0 or not r.stdout.strip():
        print("  (genre table unavailable, skipping)")
        return {}

    genres = {row["_id"]: row.get("name") or ""
              for row in _parse_content_rows(r.stdout)
              if row.get("_id")}

    audio_genre: dict[str, str] = {}
    for gid, gname in genres.items():
        r2 = _adb([
            "shell", "content", "query",
            "--uri", f"content://media/external/audio/genres/{gid}/members",
            "--projection", "audio_id",
        ])
        for row in _parse_content_rows(r2.stdout):
            aid = row.get("audio_id")
            if aid:
                audio_genre[aid] = gname
    return audio_genre


def parse_rows(raw, audio_genre: dict):
    rows = []
    for entry in _parse_content_rows(raw):
        data = entry.get("_data") or ""
        entry["ext"] = Path(data).suffix.lower()
        entry["storage"] = (
            "sd" if data.startswith("/storage/") and "/storage/emulated" not in data
            else "internal"
        )
        ms = int(entry.get("duration") or 0)
        entry["duration_minsec"] = f"{ms // 60000}:{(ms // 1000) % 60:02d}"
        entry["genre"] = audio_genre.get(entry.get("_id") or "", None)
        artist = (entry.get("artist") or "").strip()
        title  = (entry.get("title")  or "").strip()
        entry["cache_key"] = f"{artist}|{title}"  # matches .metadata_cache.json key format
        rows.append(entry)
    return sorted(rows, key=lambda r: (
        r.get("album") or "",
        r.get("track") or "",
        r.get("_data") or "",
    ))


def wait_for_mediastore(min_sd=1000, poll_secs=8, stable_rounds=2, max_wait=300):
    """Poll MediaStore until SD card tracks appear and count stabilises."""
    import time
    prev = 0
    stable = 0
    deadline = time.time() + max_wait
    print(f"Waiting for MediaStore to index SD card (target: {min_sd}+ SD tracks)…", flush=True)
    while time.time() < deadline:
        r = _adb(["shell", "content", "query",
                  "--uri", "content://media/external/audio/media",
                  "--projection", "_id,_data"])
        lines = r.stdout.splitlines()
        total = sum(1 for l in lines if l.startswith("Row:"))
        sd    = sum(1 for l in lines if "/storage/" in l and "/storage/emulated" not in l and l.startswith("Row:"))
        print(f"  {total} total / {sd} SD  ", end="\r", flush=True)
        if sd >= min_sd and sd == prev:
            stable += 1
            if stable >= stable_rounds:
                print(f"\nReady — {total} tracks ({sd} on SD)")
                return True
        else:
            stable = 0
        prev = sd
        time.sleep(poll_secs)
    print(f"\nTimeout after {max_wait}s — {prev} SD tracks indexed (expected {min_sd}+)")
    return False


def main():
    if "--wait" in sys.argv:
        if not wait_for_mediastore():
            sys.exit("MediaStore not ready — check USB connection and retry.")

    print("Querying Android MediaStore via ADB…")
    raw = query_mediastore()

    print("Fetching genre assignments…")
    audio_genre = query_genres()

    rows = parse_rows(raw, audio_genre)

    exts: dict[str, int] = {}
    storages: dict[str, int] = {}
    for r in rows:
        exts[r["ext"]] = exts.get(r["ext"], 0) + 1
        storages[r["storage"]] = storages.get(r["storage"], 0) + 1

    # Guard: refuse to overwrite snapshot with a partial scan.
    sd_count = storages.get("sd", 0)
    if not audio_genre:
        print(
            f"WARNING: genre table unavailable — MediaStore still initialising.\n"
            f"Found {len(rows)} tracks ({sd_count} on SD). Snapshot NOT overwritten.\n"
            f"Wait 60 s after connecting, then retry."
        )
        return
    if sd_count < 500:
        print(
            f"WARNING: only {sd_count} SD tracks found — SD card not fully scanned.\n"
            f"Snapshot NOT overwritten. Wait 60 s and retry."
        )
        return
    if OUT.exists():
        import json as _jchk
        prev_count = len(_jchk.loads(OUT.read_text(encoding="utf-8")))
        if len(rows) < prev_count * 0.7:
            print(
                f"WARNING: new scan ({len(rows)}) is 30%+ smaller than previous ({prev_count}).\n"
                f"Snapshot NOT overwritten. If device library genuinely shrank, delete\n"
                f"{OUT.name} manually then re-run."
            )
            return

    OUT.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"{len(rows)} tracks → {OUT.name}")
    for storage, n in sorted(storages.items()):
        print(f"  [{storage}] {n} tracks")
    for ext, n in sorted(exts.items()):
        print(f"  {ext or '(no ext)'}: {n}")


if __name__ == "__main__":
    main()
