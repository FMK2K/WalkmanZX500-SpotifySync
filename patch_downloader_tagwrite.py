# -*- coding: utf-8 -*-
"""
PATCH 4 -- downloader.py
MK15-N1: 500ms pre-sleep for local file tag write (AV race condition fix)
"""
import re, sys

TARGET = sys.argv[1]

with open(TARGET, 'r', encoding='utf-8', errors='replace') as f:
    src = f.read()

if "MK15-N1" in src:
    print("  [SKIP] MK15-N1 sleep already present")
    print("[OK] Patched: {}".format(TARGET))
    sys.exit(0)

changed = False

# Strategy: find the local-file copy block. Look for several anchors in order
# of preference. The block copies a local file to tmp_copy then sets files=[tmp_copy].

# Anchor 1: shutil.copy2 + chmod + files = [tmp_copy]  (exact MK14 pattern)
anchor1 = re.search(
    r'(shutil\.copy2\(local_src_file,\s*tmp_copy\).*?'
    r'tmp_copy\.chmod.*?'
    r')(files\s*=\s*\[tmp_copy\])',
    src, re.DOTALL
)

# Anchor 2: simpler - just before `files = [tmp_copy]` when it follows a chmod
anchor2 = re.search(
    r'(tmp_copy\.chmod\([^\n]+\)\s*\n(?:\s+except[^\n]+\n\s+pass\s*\n)?)'
    r'(\s*)(files\s*=\s*\[tmp_copy\])',
    src
)

# Anchor 3: most flexible - find `files = [tmp_copy]` with any preceding local context
anchor3_match = re.search(
    r'(winning_provider\s*=\s*["\']local["\'][^\n]*\n)',
    src
)

SLEEP_CODE = (
    "                # MK15-N1: 500ms wait after local-file copy so Windows AV/indexer\n"
    "                # finishes scanning before mutagen writes tags.\n"
    "                time.sleep(0.5)\n"
)

if anchor2:
    # Insert the sleep before `files = [tmp_copy]`
    before = anchor2.group(1)
    indent = anchor2.group(2)
    files_line = anchor2.group(3)
    new_block = before + SLEEP_CODE + indent + files_line
    src = src.replace(anchor2.group(0), new_block, 1)
    if "time.sleep(0.5)" in src:
        print("  [OK] Added 500ms pre-tag sleep (anchor 2)")
        changed = True
elif anchor1:
    full = anchor1.group(0)
    new_full = full.replace(
        anchor1.group(2),
        SLEEP_CODE + "                " + anchor1.group(2).strip()
    )
    src = src.replace(full, new_full, 1)
    if "time.sleep(0.5)" in src:
        print("  [OK] Added 500ms pre-tag sleep (anchor 1)")
        changed = True
else:
    # Fallback: find any `files = [tmp_copy]` and insert before it
    m = re.search(r'(\n)(\s*)(files\s*=\s*\[tmp_copy\])', src)
    if m:
        src = src.replace(
            m.group(0),
            "\n" + SLEEP_CODE + m.group(2) + m.group(3),
            1
        )
        if "time.sleep(0.5)" in src:
            print("  [OK] Added 500ms pre-tag sleep (fallback)")
            changed = True
    if not changed:
        print("  [WARN] Could not find insertion point -- patch not applied")
        print("         Manual fix: add time.sleep(0.5) after the local file chmod,")
        print("         just before the tag-write block in downloader.py")

with open(TARGET, 'w', encoding='utf-8') as f:
    f.write(src)

print("[OK] Patched: {}".format(TARGET) if changed else "[WARN] No sleep added to: {}".format(TARGET))
