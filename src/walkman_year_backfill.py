"""
walkman_year_backfill.py — fill the release-year tag on device files that
have none.

~30 % of the library has no `date` / `TDRC` / year tag (see
walkman_incompleteness_scan.py). The Sony player groups and sorts by year,
so the "Release year" browse view and album ordering are wrong for those.

Groups year-less files by (album_artist, album), one Deezer `/album` lookup
per album for its `release_date`, writes the 4-digit year into each file in
place (same path -> the Sony DB's is_favorites / playlist_tracks rows are
untouched, same as walkman_cover_fill.py). Resumable.

Usage:
    python walkman_year_backfill.py --dry-run
    python walkman_year_backfill.py
    python walkman_year_backfill.py --limit 500
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import adb_transfer as adb
import walkman_dedup as wd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent / "state"   # <repo>/state (src/ reorg)
STATE = ROOT / "year_backfill_state.json"
_DEEZER = "https://api.deezer.com"
_HDR = 300_000
_YEAR = re.compile(r"(19|20)\d{2}")


def _split(fn: str):
    bits = [b.strip() for b in os.path.splitext(fn)[0].split(" - ")]
    return (bits[0], bits[1]) if len(bits) >= 3 else ("", "")


def _file_year(local: Path) -> str:
    """Return the file's 4-digit year, or '' if absent/unparseable."""
    try:
        from mutagen import File as MF
        a = MF(local)
        if a is None or not a.tags:
            return ""
        for k in ("date", "TDRC", "year", "originaldate", "\xa9day", "TYER"):
            v = a.tags.get(k)
            if v:
                m = _YEAR.search(str(v[0] if isinstance(v, list) else v))
                if m:
                    return m.group(0)
    except Exception:
        pass
    return ""


def _deezer_year(artist: str, album: str) -> str:
    import requests
    try:
        q = f'artist:"{artist}" album:"{album}"'
        d = requests.get(f"{_DEEZER}/search/album", params={"q": q}, timeout=12).json()
        cand = [a for a in (d.get("data") or []) if wd._norm(a.get("title", "")) == wd._norm(album)]
        cand = cand or (d.get("data") or [])[:1]
        if not cand:
            return ""
        aid = cand[0].get("id")
        alb = requests.get(f"{_DEEZER}/album/{aid}", timeout=12).json()
        m = _YEAR.search(alb.get("release_date", "") or cand[0].get("release_date", ""))
        return m.group(0) if m else ""
    except Exception as e:
        print(f"    ! deezer year lookup failed ({artist} - {album}): {e}")
        return ""


def _write_year(path: Path, year: str, is_mp3: bool) -> bool:
    try:
        if is_mp3:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TDRC
            from id3_helpers import save_v23_id3
            try:
                id3 = ID3(path)
            except Exception:
                MP3(path).add_tags(); id3 = ID3(path)
            id3.delall("TDRC"); id3.delall("TYER"); id3.delall("TDAT")
            id3.add(TDRC(encoding=3, text=[year]))
            save_v23_id3(id3)
        else:
            from mutagen.flac import FLAC
            a = FLAC(path)
            a["date"] = year
            a["year"] = year
            a.save()
        return True
    except Exception as e:
        print(f"    ! year write failed ({path.name}): {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    st = {"done": [], "have_year": []}
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    done = set(tuple(x) for x in st.get("done", []))
    have = set(tuple(x) for x in st.get("have_year", []))

    def _save():
        STATE.write_text(json.dumps({"done": [list(x) for x in done],
                                     "have_year": [list(x) for x in have]}), encoding="utf-8")

    files, _ = wd._bulk_device_scan(wd._default_storages())
    audio = [f for f in files if f["filename"].lower().endswith((".mp3", ".flac"))]
    print(f"scanned {len(audio)} file(s); {len(done)} written, {len(have)} already had a year")

    yearless, checked = [], 0
    for f in audio:
        key = (f["storage"], "/".join(f["parts"]), f["filename"])
        if key in done or key in have:
            continue
        checked += 1
        tmp = Path(tempfile.mkdtemp())
        hp = tmp / f["filename"]
        try:
            ok = adb.read_file_header_from_device(
                src_parts=f["parts"], filename=f["filename"], dest_local=hp,
                device_name=config.DEVICE_NAME, storage_name=f["storage"], max_bytes=_HDR)
            y = _file_year(hp) if (ok and hp.exists()) else ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if y:
            have.add(key)
        else:
            yearless.append(f)
        if args.limit and len(yearless) >= args.limit:
            break
        if checked % 200 == 0:
            print(f"  checked {checked} ... {len(yearless)} year-less"); _save()
    _save()

    from collections import defaultdict
    groups = defaultdict(list)
    for f in yearless:
        a, al = _split(f["filename"])
        groups[(a, al) if a and al else ("?", f["parts"][-1] if f["parts"] else "?")].append(f)
    print(f"\nyear-less files: {len(yearless)} across {len(groups)} album(s)")

    if args.dry_run:
        for (a, al), fs in list(groups.items())[:40]:
            print(f"  {a} - {al}  ({len(fs)})")
        if len(groups) > 40:
            print(f"  ... and {len(groups) - 40} more")
        print("\nDRY-RUN — pass no --dry-run to look up + write")
        return 0

    n_write = n_noyear = n_fail = 0
    ycache: dict = {}
    for (a, al), fs in groups.items():
        ck = (wd._norm(a), wd._norm(al))
        if ck not in ycache:
            ycache[ck] = _deezer_year(a, al) if a != "?" else ""
        year = ycache[ck]
        if not (year and _YEAR.fullmatch(year)):
            n_noyear += len(fs)
            continue
        for f in fs:
            ext = os.path.splitext(f["filename"])[1].lower()
            tmp = Path(tempfile.mkdtemp())
            lp = tmp / f["filename"]
            _push_failed = False
            try:
                ok = adb.read_file_from_device(
                    src_parts=f["parts"], filename=f["filename"], dest_local=lp,
                    device_name=config.DEVICE_NAME, storage_name=f["storage"])
                if not (ok and lp.exists()) or not _write_year(lp, year, ext == ".mp3"):
                    n_fail += 1
                    continue
                if adb.copy_file_to_device(src=lp, dest_parts=f["parts"], device_name=config.DEVICE_NAME,
                                           storage_name=f["storage"], overwrite=True):
                    n_write += 1
                    done.add((f["storage"], "/".join(f["parts"]), f["filename"]))
                else:
                    n_fail += 1
                    _push_failed = True
            except Exception as e:
                print(f"    ! {f['filename']}: {e}"); n_fail += 1
                _push_failed = True
            finally:
                # 2026-09-06 — see walkman_cover_fill.py: a failed overwrite
                # push can leave the device file gone. `lp` holds a VALID
                # (year-tagged) copy — stash it rather than lose it.
                if _push_failed and lp.exists():
                    try:
                        rec = ROOT / ".year_backfill_recovery"
                        rec.mkdir(exist_ok=True)
                        dst = rec / f["filename"]
                        shutil.copy2(lp, dst)
                        print(f"    !! PUSH FAILED — stashed valid copy to {dst}")
                    except Exception as _re:
                        print(f"    !! recovery stash failed: {_re}")
                shutil.rmtree(tmp, ignore_errors=True)
            if n_write % 25 == 0 and n_write:
                _save()
        _save()

    print(f"\nyear written={n_write}  no-year-found={n_noyear}  failed={n_fail}")
    print("Trigger a device 'Update database' rescan to refresh the Release-year view.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
