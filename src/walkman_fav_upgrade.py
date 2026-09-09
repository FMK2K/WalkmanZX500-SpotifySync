"""
walkman_fav_upgrade.py — replace a favorited / playlisted file with its
better copy, then reconnect the favorite / playlist to it.

The dedup passes skip any candidate whose delete-target is favorited or
playlisted (deleting it orphans the Sony app-DB `is_favorites` row and
breaks the M3U8 relative path). That leaves duplicate pairs sitting in
favorited albums forever — an MP3 the user starred plus the FLAC that
arrived later.

This resolves each such pair:

  1. verify the SUPERIOR copy exists on the device (somewhere — the
     de-splinter may have it in Music/<Artist>/<Album>/ while the
     inferior sits in Music/<Album>/)
  2. delete the inferior copy — no file is moved, so the de-splinter's
     folder layout is left intact
  3. PLAYLIST entries: nothing else needed — regenerate the plan and the
     M3U8 re-resolves to the surviving (.flac) copy by (album, title)
  4. STAR favorites: the star was keyed to the deleted file, so queue the
     (artist, title) for a re-star pass (walkman_favplaylist_apply.py
     searches by title and stars whatever the device now shows — the
     surviving better copy)

Nothing here re-stars on its own. After --execute:
    python cli_runner.py build-favorites-playlists-plan     # re-resolve
    python walkman_playlist_import.py                       # refresh M3U8
    # then trigger a device "Update database" rescan
    WALKMAN_FAVPLAYLIST_PLAN=fav_upgrade_restar.json python walkman_favplaylist_apply.py favorites

Usage:
    python walkman_fav_upgrade.py                # dry-run
    python walkman_fav_upgrade.py --execute
"""
from __future__ import annotations

import json
import os
import sys
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
PLAN = ROOT / "favorites_playlists_plan.json"
DEDUP_PLAN = ROOT / ".dedup_plan.json"
RESTAR_OUT = ROOT / "fav_upgrade_restar.json"

_QUAL = {".flac": 3, ".wav": 3, ".aiff": 3, ".m4a": 2, ".mp3": 2, ".aac": 2, ".ogg": 1, ".opus": 1}


def _dev(pth: str) -> str:
    pth = pth.replace("\\", "/").strip()
    if pth.startswith("SD card/"):
        return adb._get_storage_path("SD card") + "/" + pth[len("SD card/"):]
    if pth.startswith("Internal shared storage/"):
        return adb._get_storage_path("Internal shared storage") + "/" + pth[len("Internal shared storage/"):]
    return ""


def _norm_key(p: str):
    return p.replace("\\", "/").strip()


def main() -> int:
    execute = "--execute" in sys.argv

    if not DEDUP_PLAN.exists():
        print("no .dedup_plan.json — run `python cli_runner.py scan-duplicates` first")
        return 1

    plan = json.loads(PLAN.read_text(encoding="utf-8"))

    def _hp(d):
        return _norm_key(f"{d['storage']}/" + "/".join(d["parts"] + [d["filename"]]))

    fav_by_path, pl_by_path = {}, {}
    for e in plan["favorites"]["resolved"]:
        if e.get("device"):
            fav_by_path[_hp(e["device"])] = e
    for name, pd in plan["playlists"].items():
        for e in pd["resolved"]:
            if e.get("device"):
                pl_by_path.setdefault(_hp(e["device"]), []).append((name, e))

    dp = json.loads(DEDUP_PLAN.read_text(encoding="utf-8"))
    cands = dp["candidates"] if isinstance(dp, dict) else dp

    pairs = []          # (inferior_hp, superior_hp, roles)
    for c in cands:
        inf = _norm_key(c["human_path"])
        sup = _norm_key(c["superior_path"])
        roles = []
        if inf in fav_by_path:
            roles.append("fav")
        if inf in pl_by_path:
            roles.append("playlist")
        if not roles:
            continue
        inf_ext = os.path.splitext(inf)[1].lower()
        sup_ext = os.path.splitext(sup)[1].lower()
        # Only act on a real quality win: superior is FLAC and inferior isn't,
        # OR the dedup engine flagged the inferior on metadata/duration grounds
        # (inferior_metadata — e.g. a duration-suspect copy). Skip lossy->lossy
        # "upgrades" (m4a over mp3) — no gain, only risk.
        real_win = (sup_ext == ".flac" and inf_ext != ".flac") or c.get("reason") == "inferior_metadata"
        if not real_win:
            continue
        pairs.append((inf, sup, roles, c.get("reason", "")))

    print(f"favorited/playlisted upgrade pairs: {len(pairs)}")
    restar = []
    n_del = n_skip = 0

    for inf, sup, roles, reason in pairs:
        inf_dev, sup_dev = _dev(inf), _dev(sup)
        tag = "+".join(roles)
        print(f"  [{tag}] delete {inf}")
        print(f"         keep   {sup}")
        if "fav" in roles:
            e = fav_by_path[inf]
            restar.append((e.get("artist", ""), e.get("title", "")))
        if not execute:
            continue
        if not inf_dev or not sup_dev or not adb._exists(sup_dev):
            print("         ! superior copy not on device — skipping (inferior kept)")
            n_skip += 1
            if "fav" in roles:
                restar.pop()
            continue
        if adb._exists(inf_dev):
            adb._rm_f(inf_dev)
            if not adb._exists(inf_dev):
                n_del += 1
            else:
                print("         ! delete failed")
                n_skip += 1

    if not execute:
        n_fav = sum(1 for p in pairs if "fav" in p[2])
        print(f"\nDRY-RUN — {len(pairs)} pairs, {n_fav} touch a star favorite (need a re-star pass)")
        uniq = {r for r in restar}
        RESTAR_OUT.write_text(json.dumps(
            {"favorites": {"resolved": [{"artist": a, "title": t} for a, t in sorted(uniq)]}, "playlists": {}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {len(uniq)} (artist,title) to {RESTAR_OUT.name} (preview) — pass --execute to apply")
        return 0

    uniq = {(a, t) for a, t in restar if a and t}
    RESTAR_OUT.write_text(json.dumps(
        {"favorites": {"resolved": [{"artist": a, "title": t} for a, t in sorted(uniq)]},
         "playlists": {}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\ndeleted_inferior={n_del} skipped={n_skip}")
    print(f"re-star queue: {len(uniq)} track(s) -> {RESTAR_OUT.name}")
    print("next: rebuild plan + M3U8 + rescan, then")
    print(f"      WALKMAN_FAVPLAYLIST_PLAN={RESTAR_OUT.name} python walkman_favplaylist_apply.py favorites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
