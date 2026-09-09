"""
walkman_cover_fill.py — embed album cover art into device files that have none.

The Sony HiRes player only shows EMBEDDED artwork — it has no folder.jpg /
cover.jpg fallback (confirmed in the decompiled PlayerMediaProvider: cover
art is "the embedded artwork extracted from the audio file"). A big slice of
the library (older downloads, some providers) has no APIC / FLAC picture
block, so those tracks render with the grey placeholder note.

This groups art-less files by (album_artist, album), fetches one cover from
Deezer per album, and embeds it into each file in place (same path → the
Sony DB's is_favorites / playlist_tracks rows are untouched). Cover bytes go
through cover_art.normalize_bytes for the player's baseline-JPEG / ≤500px
constraints, same as the audit's own cover path.

Usage:
    python walkman_cover_fill.py --dry-run
    python walkman_cover_fill.py                 # embed
    python walkman_cover_fill.py --limit 200     # first 200 art-less files
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
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
STATE = ROOT / "cover_fill_state.json"
_DEEZER = "https://api.deezer.com"
_HDR_BYTES = 300_000   # enough to see an ID3 tag / FLAC picture block if present


def _has_art(local: Path, ext: str) -> bool:
    try:
        from mutagen import File as _MF
        a = _MF(local)
        if a is None:
            return False
        if getattr(a, "pictures", None):
            return True
        if a.tags:
            return any(str(k).startswith("APIC") or k == "covr" for k in a.tags.keys())
    except Exception:
        pass
    return False


def _split(fn: str):
    stem = os.path.splitext(fn)[0]
    bits = [b.strip() for b in stem.split(" - ")]
    if len(bits) >= 3:
        return bits[0], bits[1]
    return "", ""


def _deezer_cover(artist: str, album: str) -> "str | None":
    import requests
    try:
        q = f'artist:"{artist}" album:"{album}"'
        d = requests.get(f"{_DEEZER}/search/album", params={"q": q}, timeout=12).json()
        for a in (d.get("data") or []):
            if wd._norm(a.get("title", "")) == wd._norm(album):
                return a.get("cover_big") or a.get("cover_xl")
        if d.get("data"):
            return d["data"][0].get("cover_big") or d["data"][0].get("cover_xl")
    except Exception as e:
        print(f"    ! deezer cover lookup failed ({artist} - {album}): {e}")
    return None


def _fetch(url: str) -> "bytes | None":
    import requests
    try:
        r = requests.get(url, timeout=15)
        return r.content if r.ok and len(r.content) > 2000 else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    state = {"done": [], "have_art": []}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    done = set(tuple(x) for x in state.get("done", []))
    have_art = set(tuple(x) for x in state.get("have_art", []))

    def _save():
        STATE.write_text(json.dumps({
            "done": [list(x) for x in done],
            "have_art": [list(x) for x in have_art],
        }), encoding="utf-8")

    files, _ = wd._bulk_device_scan(wd._default_storages())
    audio = [f for f in files if os.path.splitext(f["filename"])[1].lower() in (".mp3", ".flac")]
    print(f"scanned {len(audio)} mp3/flac file(s); {len(done)} embedded, "
          f"{len(have_art)} already had art (skipped)")

    # find art-less files via a header pull
    artless = []
    checked = 0
    for f in audio:
        key = (f["storage"], "/".join(f["parts"]), f["filename"])
        if key in done or key in have_art:
            continue
        checked += 1
        tmp = Path(tempfile.mkdtemp())
        hp = tmp / f["filename"]
        try:
            ok = adb.read_file_header_from_device(
                src_parts=f["parts"], filename=f["filename"], dest_local=hp,
                device_name=config.DEVICE_NAME, storage_name=f["storage"],
                max_bytes=_HDR_BYTES,
            )
            has = ok and hp.exists() and _has_art(hp, "")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        if has:
            have_art.add(key)
        else:
            artless.append(f)
        if args.limit and len(artless) >= args.limit:
            break
        if checked % 200 == 0:
            print(f"  checked {checked} ... {len(artless)} art-less so far")
            _save()
    _save()

    print(f"\nart-less files: {len(artless)}")
    # group by album
    from collections import defaultdict
    groups = defaultdict(list)
    for f in artless:
        a, al = _split(f["filename"])
        if not a or not al:
            groups[("?", f["parts"][-1] if f["parts"] else "?")].append(f)
        else:
            groups[(a, al)].append(f)
    print(f"across {len(groups)} album(s)")

    if args.dry_run:
        for (a, al), fs in list(groups.items())[:40]:
            print(f"  {a} — {al}  ({len(fs)} file(s))")
        if len(groups) > 40:
            print(f"  ... and {len(groups) - 40} more albums")
        print("\nDRY-RUN — pass no --dry-run to fetch + embed")
        return 0

    import cover_art as _ca
    n_embed = n_nocover = n_fail = 0
    cover_cache: dict = {}
    for (a, al), fs in groups.items():
        ck = (wd._norm(a), wd._norm(al))
        if ck not in cover_cache:
            url = _deezer_cover(a, al) if a != "?" else None
            raw = _fetch(url) if url else None
            cover_cache[ck] = raw
        raw = cover_cache[ck]
        if not raw:
            n_nocover += len(fs)
            continue
        try:
            img, _mime = _ca.normalize_bytes(raw)
        except Exception:
            img = raw
        for f in fs:
            ext = os.path.splitext(f["filename"])[1].lower()
            tmp = Path(tempfile.mkdtemp())
            lp = tmp / f["filename"]
            _push_failed = False
            try:
                ok = adb.read_file_from_device(
                    src_parts=f["parts"], filename=f["filename"], dest_local=lp,
                    device_name=config.DEVICE_NAME, storage_name=f["storage"])
                if not (ok and lp.exists()):
                    n_fail += 1
                    continue
                emb = _embed(lp, img, ext == ".mp3")
                if not emb:
                    n_fail += 1
                    continue
                pushed = adb.copy_file_to_device(
                    src=lp, dest_parts=f["parts"], device_name=config.DEVICE_NAME,
                    storage_name=f["storage"], overwrite=True)
                if pushed:
                    n_embed += 1
                    done.add((f["storage"], "/".join(f["parts"]), f["filename"]))
                else:
                    n_fail += 1
                    _push_failed = True
            except Exception as e:
                print(f"    ! {f['filename']}: {e}")
                n_fail += 1
                _push_failed = True
            finally:
                import shutil
                # 2026-09-06 — if the overwrite push failed, the device file
                # may now be GONE (copy_file_to_device rm's the original before
                # pushing, and removes a corrupt partial on verify-fail). The
                # embedded local copy in `lp` is a VALID file — never destroy
                # it silently. Stash it so it can be re-pushed by hand.
                if _push_failed and lp.exists():
                    try:
                        rec = ROOT / ".cover_fill_recovery"
                        rec.mkdir(exist_ok=True)
                        dst = rec / f["filename"]
                        shutil.copy2(lp, dst)
                        print(f"    !! PUSH FAILED — stashed valid copy to {dst}")
                        print(f"       re-push:  adb push \"{dst}\" "
                              f"\"{adb._get_storage_path(f['storage'])}/"
                              f"{'/'.join(f['parts'])}/{f['filename']}\"")
                    except Exception as _re:
                        print(f"    !! recovery stash failed: {_re}")
                shutil.rmtree(tmp, ignore_errors=True)
            if n_embed % 25 == 0 and n_embed:
                _save()
        _save()

    print(f"\nembedded={n_embed}  no-cover-found={n_nocover}  failed={n_fail}")
    print("Trigger a device 'Update database' rescan to refresh the art.")
    return 0


def _embed(path: Path, img_bytes: bytes, is_mp3: bool) -> bool:
    try:
        import cover_art as _ca
        from id3_helpers import save_v23_id3
        from mutagen.id3 import ID3
        from mutagen.flac import FLAC
        if is_mp3:
            try:
                id3 = ID3(path)
            except Exception:
                from mutagen.mp3 import MP3
                MP3(path).add_tags()
                id3 = ID3(path)
            id3.delall("APIC")
            id3.add(_ca.build_apic(img_bytes))
            save_v23_id3(id3)
        else:
            audio = FLAC(path)
            audio.clear_pictures()
            audio.add_picture(_ca.build_flac_picture(img_bytes))
            audio.save()
        return True
    except Exception as e:
        print(f"    ! embed failed ({path.name}): {e}")
        return False


if __name__ == "__main__":
    raise SystemExit(main())
