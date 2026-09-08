"""
walkman_diagnose.py
───────────────────
Diagnostic script — does NOT move any files.

Walks the Walkman storage roots and prints everything it finds so we can
understand exactly what Shell COM sees.  Run this first so we know the
real item types/names before touching the reorganiser.

Usage:
    python walkman_diagnose.py
    python walkman_diagnose.py --depth 4   # how many folder levels to show
"""

import argparse
import sys
import time
from pathlib import Path

try:
    import win32com.client
    import pythoncom
except ImportError:
    sys.exit("ERROR: pip install pywin32")

# ── Shell helpers ─────────────────────────────────────────────────────────────

_SH = None

def sh():
    global _SH
    if _SH is None:
        pythoncom.CoInitialize()
        _SH = win32com.client.Dispatch("Shell.Application")
    return _SH

def this_pc():
    return sh().NameSpace(0x11)

# ── Probe a single Shell item and print everything we can read ────────────────

def probe_item(item, indent=""):
    """Print every attribute we can safely read from a Shell FolderItem."""
    attrs = [
        "Name", "Path", "Type", "Size", "IsFolder",
        "IsLink", "IsReadOnly", "IsBrowsable",
    ]
    for attr in attrs:
        try:
            val = getattr(item, attr)
            print(f"{indent}  .{attr} = {val!r}")
        except Exception as e:
            print(f"{indent}  .{attr} = ERROR({e})")

    # Extended properties via ExtendedProperty
    for prop in ["Type", "Size", "Date modified", "Attributes"]:
        try:
            val = item.ExtendedProperty(prop)
            print(f"{indent}  ExtProp[{prop!r}] = {val!r}")
        except Exception:
            pass


def walk(folder_item, label, max_depth, depth=0, audio_found=None):
    if audio_found is None:
        audio_found = []

    indent = "  " * depth

    try:
        folder = folder_item.GetFolder
    except Exception as e:
        print(f"{indent}[GetFolder FAILED: {e}]")
        return audio_found

    # Try iterating Items()
    try:
        items = list(folder.Items())
    except Exception as e:
        print(f"{indent}[Items() FAILED: {e}]")
        return audio_found

    print(f"{indent}[{label}]  ({len(items)} items)")

    audio_exts = {
        ".mp3", ".flac", ".aac", ".m4a", ".ogg", ".opus",
        ".wav", ".wma", ".aiff", ".ape", ".wv",
    }

    # item.Type → extension mapping (Walkman MTP strips extensions from item.Name)
    type_to_ext = {
        "mp3 file": ".mp3", "flac file": ".flac", "aac file": ".aac",
        "m4a file": ".m4a", "ogg file": ".ogg",  "opus file": ".opus",
        "wav file": ".wav", "wma file": ".wma",  "aiff file": ".aiff",
        "ape file": ".ape", "wv file":  ".wv",
        "mpeg audio file": ".mp3", "audio file": ".mp3", "flac audio": ".flac",
    }

    def _get_audio_ext(item_name, item_type_str):
        """Try item.Type first, then filename suffix."""
        t = (item_type_str or "").lower().strip()
        if t in type_to_ext:
            return type_to_ext[t]
        for key, ext in type_to_ext.items():
            if key in t:
                return ext
        suffix = Path(item_name).suffix.lower()
        return suffix if suffix in audio_exts else None

    for item in items:
        try:
            name = item.Name
        except Exception as e:
            print(f"{indent}  <item.Name FAILED: {e}>")
            continue

        try:
            is_folder = item.IsFolder
        except Exception:
            is_folder = None

        try:
            item_type = item.Type
        except Exception:
            item_type = "?"

        suffix   = Path(name).suffix.lower()
        ext      = _get_audio_ext(name, item_type)
        is_audio = ext is not None

        marker = "[DIR] " if is_folder else ("[AUDIO] " if is_audio else "[file] ")
        print(f"{indent}  {marker}{name!r}  type={item_type!r}")

        if is_audio:
            audio_found.append(f"{label}/{name}  ({ext})")

        if is_folder and depth < max_depth:
            walk(item, name, max_depth, depth + 1, audio_found)

    return audio_found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=5,
                    help="Max folder depth to walk (default 5)")
    args = ap.parse_args()

    print("=" * 70)
    print("Walkman MTP Diagnostic")
    print("=" * 70)

    # ── Find WALKMAN ──────────────────────────────────────────────────────────
    pc = this_pc()
    walkman = None
    print("\n[This PC] items:")
    for item in pc.Items():
        try:
            name = item.Name
            path = item.Path
            is_f = item.IsFolder
        except Exception as e:
            print(f"  <item error: {e}>")
            continue
        print(f"  Name={name!r}  IsFolder={is_f}  Path={path!r}")
        if "WALKMAN" in name.upper():
            walkman = item

    if walkman is None:
        print("\nERROR: WALKMAN not found. Connect the device and retry.")
        return

    print(f"\n[WALKMAN] found: {walkman.Name!r}")
    print("Probing WALKMAN item attributes:")
    probe_item(walkman, indent="")

    # ── List storage roots ────────────────────────────────────────────────────
    print("\n[WALKMAN storage roots]")
    try:
        storage_roots = list(walkman.GetFolder.Items())
    except Exception as e:
        print(f"  GetFolder.Items() on WALKMAN failed: {e}")
        return

    for storage in storage_roots:
        try:
            sname = storage.Name
        except Exception as e:
            print(f"  <storage.Name failed: {e}>")
            continue
        print(f"\n  Storage: {sname!r}")
        probe_item(storage, indent="  ")

    # ── Walk each storage root ────────────────────────────────────────────────
    all_audio = []
    for storage in storage_roots:
        try:
            sname = storage.Name
        except Exception:
            continue
        print(f"\n{'=' * 70}")
        print(f"Walking: {sname!r}  (max depth={args.depth})")
        print("=" * 70)
        found = walk(storage, sname, max_depth=args.depth)
        all_audio.extend(found)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"AUDIO FILES FOUND: {len(all_audio)}")
    for p in all_audio[:50]:
        print(f"  {p}")
    if len(all_audio) > 50:
        print(f"  … and {len(all_audio) - 50} more")
    print("=" * 70)

    if len(all_audio) == 0:
        print("""
NOTHING FOUND — possible causes:
  1. Items() returns 0 for shell-virtual MTP items on this device/driver.
     Try: open Windows Explorer, navigate to the Walkman Music folder,
     then run this script again (Explorer sometimes 'wakes' the MTP cache).
  2. The Walkman may need to be set to 'MTP' (not 'MSC') in its USB settings.
  3. Windows MTP shell cache is stale — eject, reconnect, retry.
""")


if __name__ == "__main__":
    main()
