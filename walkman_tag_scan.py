"""
walkman_tag_scan.py

Pulls the first 512 KB of every audio file from the Walkman via ADB
and reads the actual embedded tags with mutagen — reflecting exactly
what the ZX500 HiRes player sees (not Android MediaStore, which the
player ignores entirely).

ZX500 HiRes player facts (from readme_SDD.pdf):
  - Reads tags directly via its own SQLite DB (PlayerMediaProvider); MediaStore unused
  - MP3s MUST be ID3v2.3 — ID3v2.4 frames appear BLANK in the player browser
  - APIC must be baseline JPEG ≤500px ≤90KB; PNG is rejected by firmware
  - Genre from TCON; year from TYER (v2.3) or TDRC (v2.4, ignored by player)

Prerequisites:
  - Walkman connected via USB with USB Debugging on
  - walkman_snapshot.json present  (run walkman_tree_gen.py first)
  - pip install mutagen

Run:   python walkman_tag_scan.py
Out:   walkman_tag_snapshot.json  (keyed by device path)
Then:  python snapshot_cross_check.py
"""
import json, struct, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from adb_transfer import _ADB_EXE

try:
    import mutagen.id3
    import mutagen.flac
    import mutagen.mp4
except ImportError:
    sys.exit("mutagen not installed — run: pip install mutagen")

PROJECT  = Path(__file__).parent
SNAPSHOT = PROJECT / "walkman_snapshot.json"
OUT      = PROJECT / "walkman_tag_snapshot.json"

PULL_KB     = 512   # covers all text frames + ≤90 KB APIC per cover_art.py limits
MAX_WORKERS = 3     # parallel ADB exec-out streams (USB bandwidth is shared)


# ---------------------------------------------------------------------------
# ADB
# ---------------------------------------------------------------------------

def adb_pull_head(device_path: str, dest: Path) -> bool:
    """Write first PULL_KB KB from device_path to dest via ADB exec-out."""
    blocks = (PULL_KB * 1024) // 512
    safe   = device_path.replace("'", r"'\''")   # POSIX shell escape
    r = subprocess.run(
        [_ADB_EXE, "exec-out", f"dd if='{safe}' bs=512 count={blocks} 2>/dev/null"],
        capture_output=True, timeout=60,
    )
    if r.returncode == 0 and r.stdout:
        dest.write_bytes(r.stdout)
        return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_progressive_jpeg(data: bytes) -> bool:
    """True if data begins with a JPEG that contains a SOF2 (progressive) marker."""
    i = 2  # skip SOI 0xFF 0xD8
    while i + 3 < len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xC2:   # SOF2 = progressive DCT
            return True
        if marker == 0xC0:   # SOF0 = baseline DCT
            return False
        if marker in (0xD8, 0xD9):
            break
        seg_len = struct.unpack(">H", data[i + 2: i + 4])[0] if i + 4 <= len(data) else 0
        i += 2 + seg_len
    return False


def _apic_issues(mime: str | None, data: bytes, size_kb: float) -> list[str]:
    issues = []
    if mime == "image/png":
        issues.append(
            f"cover is PNG ({size_kb:.0f} KB) — ZX500 firmware rejects PNG; must be baseline JPEG"
        )
    elif mime == "image/jpeg":
        if _is_progressive_jpeg(data[:2048]):
            issues.append("cover is progressive JPEG — ZX500 requires baseline JPEG; art may not render")
        if size_kb > 90:
            issues.append(f"cover is {size_kb:.0f} KB — ZX500 safe limit is 90 KB (may cause slowdown)")
    return issues


# ---------------------------------------------------------------------------
# Format-specific parsers
# ---------------------------------------------------------------------------

def _parse_mp3(path: str) -> dict:
    r: dict = {
        "id3_version": None, "has_apic": False,
        "apic_mime": None, "apic_size_kb": None, "apic_progressive": None,
        "issues": [],
    }
    try:
        tags = mutagen.id3.ID3(path)
        _, minor, _ = tags.version
        r["id3_version"] = f"2.{minor}"

        if minor == 4:
            r["issues"].append(
                "ID3v2.4 — ZX500 HiRes player ignores v2.4 frames entirely; "
                "title, artist, genre, and year will appear BLANK in the player"
            )
        elif minor not in (2, 3):
            r["issues"].append(f"unusual ID3 version 2.{minor}")

        def _txt(fid: str) -> str | None:
            f = tags.get(fid)
            return str(f.text[0]).strip() if f and getattr(f, "text", None) else None

        r["tag_title"]        = _txt("TIT2")
        r["tag_artist"]       = _txt("TPE1")
        r["tag_album"]        = _txt("TALB")
        r["tag_album_artist"] = _txt("TPE2")
        r["tag_year"]         = _txt("TDRC") or _txt("TYER")
        r["tag_genre"]        = _txt("TCON")   # raw TCON value, may include legacy codes like "(18)"
        r["tag_track"]        = _txt("TRCK")
        r["tag_disc"]         = _txt("TPOS")

        apic_keys = [k for k in tags if k.startswith("APIC")]
        r["has_apic"] = bool(apic_keys)
        if apic_keys:
            apic            = tags[apic_keys[0]]
            data            = getattr(apic, "data", b"") or b""
            r["apic_mime"]     = getattr(apic, "mime", None)
            r["apic_size_kb"]  = round(len(data) / 1024, 1)
            r["apic_progressive"] = _is_progressive_jpeg(data[:2048]) if r["apic_mime"] == "image/jpeg" else None
            r["issues"].extend(_apic_issues(r["apic_mime"], data, r["apic_size_kb"]))

    except mutagen.id3.ID3NoHeaderError:
        r["issues"].append("no ID3 header found — file has no tags at all")
    except Exception as e:
        r["issues"].append(f"ID3 parse error: {e}")
    return r


def _parse_flac(path: str) -> dict:
    r: dict = {
        "id3_version": "FLAC/VorbisComment", "has_apic": False,
        "apic_mime": None, "apic_size_kb": None, "apic_progressive": None,
        "issues": [],
    }
    try:
        audio = mutagen.flac.FLAC(path)

        def _vc(key: str) -> str | None:
            t = audio.tags
            if not t:
                return None
            v = t.get(key) or t.get(key.upper()) or t.get(key.lower()) or []
            return v[0].strip() if v else None

        r["tag_title"]        = _vc("title")
        r["tag_artist"]       = _vc("artist")
        r["tag_album"]        = _vc("album")
        r["tag_album_artist"] = _vc("albumartist")
        r["tag_year"]         = _vc("date")
        r["tag_genre"]        = _vc("genre")
        r["tag_track"]        = _vc("tracknumber")
        r["tag_disc"]         = _vc("discnumber")

        pics = audio.pictures
        r["has_apic"] = bool(pics)
        if pics:
            pic              = pics[0]
            data             = pic.data or b""
            r["apic_mime"]    = pic.mime
            r["apic_size_kb"] = round(len(data) / 1024, 1)
            r["apic_progressive"] = _is_progressive_jpeg(data[:2048]) if pic.mime == "image/jpeg" else None
            r["issues"].extend(_apic_issues(pic.mime, data, r["apic_size_kb"]))

    except Exception as e:
        r["issues"].append(f"FLAC parse error: {e}")
    return r


def _parse_m4a(path: str) -> dict:
    r: dict = {
        "id3_version": "MP4/iTunes", "has_apic": False,
        "apic_mime": None, "apic_size_kb": None, "apic_progressive": None,
        "issues": [],
    }
    try:
        audio = mutagen.mp4.MP4(path)
        t = audio.tags or {}

        def _atom(key: str) -> str | None:
            v = t.get(key)
            return str(v[0]).strip() if v else None

        r["tag_title"]        = _atom("\xa9nam")
        r["tag_artist"]       = _atom("\xa9ART")
        r["tag_album"]        = _atom("\xa9alb")
        r["tag_album_artist"] = _atom("aART")
        r["tag_year"]         = _atom("\xa9day")
        r["tag_genre"]        = _atom("\xa9gen")

        trck = t.get("trkn")
        r["tag_track"] = str(trck[0][0]) if trck else None
        r["tag_disc"]  = str(trck[0][1]) if trck and len(trck[0]) > 1 and trck[0][1] else None

        covr = t.get("covr")
        r["has_apic"] = bool(covr)
        if covr:
            img              = covr[0]
            data             = bytes(img)
            r["apic_size_kb"] = round(len(data) / 1024, 1)
            if img.imageformat == mutagen.mp4.MP4Cover.FORMAT_JPEG:
                r["apic_mime"]        = "image/jpeg"
                r["apic_progressive"] = _is_progressive_jpeg(data[:2048])
            else:
                r["apic_mime"] = "image/png"
            r["issues"].extend(_apic_issues(r["apic_mime"], data, r["apic_size_kb"]))

    except Exception as e:
        r["issues"].append(
            f"M4A parse error (moov atom may be at EOF for non-faststart files): {e}"
        )
    return r


_PARSERS = {
    ".mp3":  _parse_mp3,
    ".flac": _parse_flac,
    ".m4a":  _parse_m4a,
    ".mp4":  _parse_m4a,
    ".aac":  _parse_m4a,
}


def parse_tags(tmp: Path, ext: str) -> dict:
    fn = _PARSERS.get(ext.lower())
    if not fn:
        return {"issues": [f"unsupported format: {ext}"], "has_apic": False,
                "id3_version": None, "tag_genre": None}
    return fn(str(tmp))


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def scan_track(track: dict, tmp_dir: Path) -> tuple[str, dict]:
    path     = track["_data"]
    ext      = track["ext"]
    tmp_file = tmp_dir / f"_tc_{track['_id']}{ext}"
    try:
        if not adb_pull_head(path, tmp_file):
            return path, {
                "error": True, "has_apic": False, "id3_version": None, "tag_genre": None,
                "issues": ["ADB read failed — device disconnected or path changed"],
            }
        result = parse_tags(tmp_file, ext)
        result["storage"]  = track.get("storage")
        result["ext"]      = ext
        result["duration"] = track.get("duration_minsec")
        return path, result
    finally:
        tmp_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not SNAPSHOT.exists():
        sys.exit("walkman_snapshot.json not found — run walkman_tree_gen.py first.")

    snapshot: list[dict] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    total = len(snapshot)
    print(f"Tag-scanning {total} tracks  ({PULL_KB} KB per file, {MAX_WORKERS} parallel ADB streams)")
    print("Pulling from device — typically 3-8 min over USB.\n")

    tag_snap: dict[str, dict] = {}
    done = errors = adb_failures = 0

    with tempfile.TemporaryDirectory(prefix="wlkmn_tag_") as tmp_str:
        tmp_dir = Path(tmp_str)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(scan_track, t, tmp_dir): t for t in snapshot}
            for fut in as_completed(futures):
                path, result = fut.result()
                tag_snap[path] = result
                done += 1
                if result.get("issues") or result.get("error"):
                    errors += 1
                if result.get("error"):
                    adb_failures += 1
                if done % 25 == 0 or done == total:
                    print(f"  {done}/{total}  issues so far: {errors}", end="\r")

    print(f"\n\nDone. {total} tracks scanned, {errors} with issues.\n")

    # 2026-07-28: confirmed live — when ADB drops mid-run (or is already down
    # before this starts), every adb_pull_head() call fails identically and
    # scan_track()'s per-file error path fires for 100% of tracks, but nothing
    # here previously distinguished that from a genuine "no embedded art / no
    # genre" finding. This silently overwrote a real, working tag snapshot
    # with 1420/1420 blank error placeholders and still exited 0. Same
    # loud-warning-instead-of-silent-write pattern as _pass_zero_length's
    # UNRELIABLE check: refuse to replace the snapshot when most of the run
    # never actually read the device.
    if total and adb_failures / total > 0.5:
        sys.exit(
            f"ABORTED — {adb_failures}/{total} track(s) failed with 'ADB read failed' "
            f"(device likely went offline during this run). Not overwriting "
            f"{OUT.name} with unreliable data. Reconnect ADB and try again."
        )

    no_apic   = sum(1 for v in tag_snap.values() if not v.get("has_apic"))
    png       = sum(1 for v in tag_snap.values() if v.get("apic_mime") == "image/png")
    prog      = sum(1 for v in tag_snap.values() if v.get("apic_progressive"))
    big       = sum(1 for v in tag_snap.values() if (v.get("apic_size_kb") or 0) > 90)
    v4        = sum(1 for v in tag_snap.values() if str(v.get("id3_version") or "").endswith(".4"))
    no_header = sum(1 for v in tag_snap.values() if any("no ID3 header" in i for i in v.get("issues", [])))

    genre_dist: dict[str, int] = {}
    for v in tag_snap.values():
        g = (v.get("tag_genre") or "").strip() or "(unset)"
        genre_dist[g] = genre_dist.get(g, 0) + 1

    w = 34
    print(f"{'No embedded art (has_apic=False):':<{w}} {no_apic}")
    print(f"{'No ID3 header at all:':<{w}} {no_header}")
    print(f"{'PNG covers (rejected by ZX500):':<{w}} {png}")
    print(f"{'Progressive JPEG (may not render):':<{w}} {prog}")
    print(f"{'Oversized APIC >90 KB:':<{w}} {big}")
    print(f"{'ID3v2.4 files (BLANK in player):':<{w}} {v4}")

    print(f"\nGenre distribution by TCON frame (top 20):")
    for genre, cnt in sorted(genre_dist.items(), key=lambda x: -x[1])[:20]:
        print(f"  {cnt:>5}  {genre}")

    OUT.write_text(json.dumps(tag_snap, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nTag snapshot  ->  {OUT.name}")
    print("Run snapshot_cross_check.py to cross-reference against caches.")


if __name__ == "__main__":
    main()
