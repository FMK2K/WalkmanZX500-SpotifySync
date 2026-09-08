"""
MK16 v4 Patch Applicator
Usage: python APPLY_PATCHES.py [path_to_SpotifyTunerMK11_folder]
Run from the same folder as the patch scripts, or pass folder path as argument.
"""
import sys, shutil, subprocess, os
from pathlib import Path

folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
patches_dir = Path(__file__).parent

patches = [
    ("genre_client.py",      "patch_genre_client.py"),
    ("sync_planner.py",      "patch_sync_planner.py"),
    ("main.py",              "patch_main_halt_gate.py"),
    ("downloader.py",        "patch_downloader_tagwrite.py"),
    ("device_inventory.py",  "patch_device_inventory.py"),
]

print("Applying MK16 v4 patches to: {}".format(folder.resolve()))
print()

env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

for target_name, patch_name in patches:
    target = folder / target_name
    patch_script = patches_dir / patch_name
    if not target.exists():
        print("  [SKIP] {} not found".format(target_name))
        continue
    if not patch_script.exists():
        print("  [SKIP] patch {} not found next to APPLY_PATCHES.py".format(patch_name))
        continue
    backup = target.with_suffix(target.suffix + ".v3_bak")
    shutil.copy2(target, backup)
    result = subprocess.run(
        [sys.executable, str(patch_script), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    if result.returncode == 0:
        print("  [OK] {}".format(target_name))
        for line in result.stdout.strip().splitlines():
            if line.strip():
                print("       {}".format(line))
    else:
        print("  [FAILED] {}".format(target_name))
        for line in (result.stdout + result.stderr)[:500].splitlines():
            print("       {}".format(line))
        shutil.copy2(backup, target)
        print("       Restored from backup.")

print()
print("Copying new modules...")
for m in ["mk15_halt_gate.py", "mk15_idempotency.py", "mk15_file_coherence.py"]:
    src = patches_dir / m
    dst = folder / m
    if not src.exists():
        print("  [SKIP] {} not found".format(m))
        continue
    try:
        shutil.copy2(src, dst)
        print("  [OK] Copied {}".format(m))
    except PermissionError:
        print("  [WARN] {} locked (close app first, or copy manually)".format(m))
    except Exception as e:
        print("  [WARN] {} -- {}".format(m, e))

print()
print("Done. Backups saved as *.v3_bak")
