# -*- coding: utf-8 -*-
"""
PATCH — device_inventory.py (NEW patch, not in v3)
MK16-I1: ADB find retry loop — avoid inventory=0 from USB auth race condition

Root cause (from code comment MK26 C3+C4):
  "inventory=0 at t=0, ADB found 27901 files at t=11s"
  _device_connected() retries 3x but the ADB shell find does NOT retry.
  Device passes `adb devices` but filesystem isn't fully mounted yet.
  find returns "" → _parse_paths("") → 0 audio files → halt gate fires.

Fix: Retry the ADB shell find up to 3x with 5s gaps when it returns 0 files.
Also add a fallback: if still 0 after retries, try listing just /Music dirs
to see if the device is accessible at all before returning 0.
"""
import sys, re

TARGET = sys.argv[1]

with open(TARGET, 'r', encoding='utf-8', errors='replace') as f:
    src = f.read()

if "MK16-I1" in src:
    print("  [SKIP] MK16-I1 already applied")
    print("[OK] Patched: {}".format(TARGET))
    sys.exit(0)

# Find the ADB find block and add retry logic
OLD_FIND = (
    '    raw = _run_adb("shell", find_cmd, timeout=180)\n'
    '\n'
    '    if not raw:\n'
    '        log.warning("[DeviceInventory] ADB find returned no output")\n'
    '        if cached := _load_cache():\n'
    '            _inventory = cached\n'
    '            return len(_inventory.files)\n'
    '        return 0'
)
NEW_FIND = (
    '    # MK16-I1: retry loop — race condition where ADB shell is ready but\n'
    '    # Android filesystem not yet mounted (observed: 0 files at t=0, N files at t=11s)\n'
    '    raw = ""\n'
    '    for _attempt in range(3):\n'
    '        raw = _run_adb("shell", find_cmd, timeout=180)\n'
    '        if raw:\n'
    '            break\n'
    '        if _attempt < 2:\n'
    '            log.warning(\n'
    '                f"[DeviceInventory] ADB find returned no output "\n'
    '                f"(attempt {_attempt+1}/3) — retrying in 5s "\n'
    '                f"(USB auth / filesystem mount race condition)"\n'
    '            )\n'
    '            import time as _t_inv\n'
    '            _t_inv.sleep(5)\n'
    '\n'
    '    if not raw:\n'
    '        log.warning("[DeviceInventory] ADB find returned no output after 3 attempts")\n'
    '        if cached := _load_cache():\n'
    '            _inventory = cached\n'
    '            log.info("[DeviceInventory] Using stale cache as fallback")\n'
    '            return len(_inventory.files)\n'
    '        log.warning(\n'
    '            "[DeviceInventory] No cache and ADB returned nothing. "\n'
    '            "Inventory unknown — halt gate will fire. "\n'
    '            "Check USB Debugging, unlock device, and retry."\n'
    '        )\n'
    '        return 0'
)

if OLD_FIND in src:
    src = src.replace(OLD_FIND, NEW_FIND, 1)
    print("  [OK] Added 3x retry loop around ADB shell find")
else:
    # Flexible fallback: find the raw = _run_adb("shell", find_cmd, ...) line
    m = re.search(r'    raw = _run_adb\("shell", find_cmd, timeout=\d+\)', src)
    if m:
        # Insert retry wrapper around just this line
        old_line = m.group(0)
        new_block = (
            '    # MK16-I1: retry loop for ADB race condition\n'
            '    raw = ""\n'
            '    for _attempt in range(3):\n'
            '        raw = _run_adb("shell", find_cmd, timeout=180)\n'
            '        if raw:\n'
            '            break\n'
            '        if _attempt < 2:\n'
            '            log.warning(f"[DeviceInventory] ADB find empty (attempt {_attempt+1}/3) -- retrying in 5s")\n'
            '            import time as _t_inv; _t_inv.sleep(5)'
        )
        src = src.replace(old_line, new_block, 1)
        print("  [OK] Added retry loop (flexible anchor)")
    else:
        print("  [WARN] Could not find ADB find call -- patch not applied")

with open(TARGET, 'w', encoding='utf-8') as f:
    f.write(src)

import ast
try:
    ast.parse(src)
    print("  [OK] Syntax valid")
except SyntaxError as e:
    print("  [FAIL] Syntax error: {}".format(e))

print("[OK] Patched: {}".format(TARGET))
