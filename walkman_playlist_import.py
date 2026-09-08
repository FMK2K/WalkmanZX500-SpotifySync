"""
Bulk playlist population via M3U8 file import -- the fast path, discovered
2026-09-04 after UI-automating favorites made clear per-track taps don't
scale for playlists (context-menu + pick-existing-playlist-by-name, ~30s/
track, 2829 playlist-track-adds = tens of hours).

Root cause / history: `adb_transfer.write_text_file_to_device` had a
contract mismatch bug (never updated for REV 14.8's "dest_parts is a
folder, filename comes from src.name" contract on copy_file_to_device) that
had been silently creating "<playlist name>.m3u8/" DIRECTORIES containing a
randomly-named tempfile instead of a real file at that path, since at least
2026-04-30. The app's own MediaScannerService DOES import .m3u8 files on a
normal rescan (contradicting an earlier, wrong assumption recorded in
memory) -- it just never found a well-formed one, because the file was
always one directory level too deep to resolve its own relative paths.
Fixed in adb_transfer.py; this script is the fast, correct path forward.

Verified live 2026-09-04: pushed a 5-track playlist, triggered "Update
database" via UI tap, rescan (~2-3 min) picked it up as a real playlist
with all 5 tracks in order, correct metadata. The 16 pre-existing garbage
"tmpXXXXXXXX"-named empty playlists from the old bug were manually deleted
from the device beforehand.

Usage:
    python walkman_playlist_import.py                 # writes all playlists' m3u8 files
    python walkman_playlist_import.py --dry-run        # just prints what would be written
Then trigger one "Update database" rescan (UI tap or GUI) to import them.
"""
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import adb_transfer as adb
import config

ROOT = Path(__file__).parent
PLAN_PATH = ROOT / "favorites_playlists_plan.json"


def _sanitize(name: str) -> str:
    # Keep it simple/predictable -- these become both the on-device
    # filename and (via import) the playlist's display name.
    bad = '<>:"/\\|?*'
    return "".join(c for c in name if c not in bad).strip()


def build_m3u8(resolved: list) -> str:
    lines = ["#EXTM3U"]
    for e in resolved:
        d = e.get("device")
        if not d:
            continue
        parts = d["parts"][1:] if d["parts"] and d["parts"][0].lower() == config.MUSIC_ROOT.lower() else d["parts"]
        lines.append("/".join(parts + [d["filename"]]))
    return "\n".join(lines) + "\n"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--storage", default="SD card")
    args = ap.parse_args()

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    playlists = plan["playlists"]

    written, skipped = [], []
    for name, data in playlists.items():
        resolved = data.get("resolved", [])
        if not resolved:
            skipped.append((name, "0 resolved tracks"))
            continue
        content = build_m3u8(resolved)
        fname = _sanitize(name) + ".m3u8"
        if args.dry_run:
            print(f"[dry-run] {fname}  ({len(resolved)} tracks)")
            written.append(name)
            continue
        try:
            adb.write_text_file_to_device(
                content=content,
                dest_parts=[config.MUSIC_ROOT],
                filename=fname,
                device_name=config.DEVICE_NAME,
                storage_name=args.storage,
            )
            print(f"OK  {fname}  ({len(resolved)} tracks)")
            written.append(name)
        except Exception as e:
            print(f"FAIL {fname}: {e}")
            skipped.append((name, str(e)))

    print(f"\n=== {len(written)} written, {len(skipped)} skipped ===")
    for name, reason in skipped:
        print(f"  skip: {name} -- {reason}")
    if not args.dry_run and written:
        print("\nNow trigger an 'Update database' rescan on the device "
              "(Library top -> refresh icon -> YES) to import these.")


if __name__ == "__main__":
    main()
