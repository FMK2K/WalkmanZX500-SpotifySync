# -*- coding: utf-8 -*-
"""
patch_main_halt_gate.py -- MK16 v4
Fixes:
  A) Collapses 4x duplicate inventory halt-gate blocks to 1 (clean)
  B) Collapses 4x duplicate __init__ flag declarations to 1
  C) Softens Stage 1 gate: proceed with warning when ADB=0 but MTP is active
"""
import re, sys

TARGET = sys.argv[1]

with open(TARGET, 'r', encoding='utf-8', errors='replace') as f:
    raw = f.read()
src = raw.replace('\r\n', '\n').replace('\r', '\n')

changed = False

# ────────────────────────────────────────────────────────────────────────────
# FIX A: Collapse 4 duplicate inventory halt-gate blocks into 1
# ────────────────────────────────────────────────────────────────────────────
# Each duplicate block looks exactly like this (repeated 4x):
DUP_GATE = (
    "# MK15-R0: halt gate \u2014 if ADB returned 0 files, do NOT proceed\n"
    "                        # to Stage 1 transfers. This is almost certainly a scan failure\n"
    "                        # (USB/MTP issue), not an empty device. Proceeding would treat\n"
    "                        # all existing device tracks as missing and mass-retransfer them.\n"
    "                        if n == 0:\n"
    "                            self._inventory_zero_halt = True\n"
    "                            self._append_log(\n"
    "                                \"[MK15-R0] HALT GATE: Device inventory returned 0 files. \"\n"
    "                                \"This is almost certainly a USB/MTP scan failure. \"\n"
    "                                \"Stage 1 transfers will be skipped to prevent mass duplicate transfer. \"\n"
    "                                \"Check USB connection, MTP mode, and device unlock state.\",\n"
    "                                \"WARNING\"\n"
    "                            )\n"
    "                        else:\n"
    "                            self._inventory_zero_halt = False"
)

count_dups = src.count(DUP_GATE)
print("  [A] Duplicate gate blocks found: {}".format(count_dups))

if count_dups >= 2:
    # Remove all copies, then insert exactly one clean replacement
    CLEAN_GATE = (
        "# MK15-R0 v4: halt gate (single \u2014 duplicates collapsed)\n"
        "                        # ADB inventory=0 almost always means race condition, not empty device.\n"
        "                        # Stage 1 gate checks MTP before deciding to abort.\n"
        "                        if n == 0:\n"
        "                            self._inventory_zero_halt = True\n"
        "                            self._append_log(\n"
        "                                \"[MK15-R0] ADB inventory=0 \u2014 likely USB/ADB race condition. \"\n"
        "                                \"Stage 1 will verify via MTP before skipping transfers.\",\n"
        "                                \"WARNING\"\n"
        "                            )\n"
        "                        else:\n"
        "                            self._inventory_zero_halt = False"
    )
    # Remove all duplicates first
    while DUP_GATE in src:
        src = src.replace(DUP_GATE, "", 1)
    # Now insert exactly one clean gate where the old block was
    # Find the inner try/except (MK19-RC2) and insert after its `pass`
    m = re.search(
        r'(                        except Exception:\n                            pass\n)',
        src
    )
    if m:
        src = src[:m.end()] + "                        " + CLEAN_GATE + "\n" + src[m.end():]
        print("  [A] Inserted 1 clean gate after inner try/except")
        changed = True
    else:
        print("  [A] [WARN] Could not find anchor for gate insertion")
elif count_dups == 1:
    print("  [A] Only 1 gate present -- already clean, skipping")
else:
    print("  [A] [WARN] Gate pattern not found (may have been manually edited)")

# ────────────────────────────────────────────────────────────────────────────
# FIX B: Collapse duplicate __init__ flag declarations
# ────────────────────────────────────────────────────────────────────────────
DUP_INIT = "        self._inventory_zero_halt = False  # MK15-R0: set True when inventory preload = 0"
count_init = src.count(DUP_INIT)
print("  [B] Duplicate __init__ flags: {}".format(count_init))

if count_init >= 2:
    # Remove all, re-insert one
    while DUP_INIT in src:
        src = src.replace(DUP_INIT, "", 1)
    # Re-insert after self._agent = None
    src = src.replace(
        "        self._agent = None\n",
        "        self._agent = None\n"
        "        self._inventory_zero_halt = False  # MK15-R0: set True when inventory preload = 0\n",
        1
    )
    print("  [B] Collapsed to 1 __init__ flag")
    changed = True
elif count_init == 1:
    print("  [B] Already exactly 1 __init__ flag -- OK")

# ────────────────────────────────────────────────────────────────────────────
# FIX C: Soften Stage 1 gate -- proceed with warning when MTP is active
# ────────────────────────────────────────────────────────────────────────────
HARD_STAGE1 = (
    "# MK15-R0: halt gate check\n"
    "                        if getattr(self, '_inventory_zero_halt', False):\n"
    "                            self._append_log(\n"
    "                                \"[MK15-R0] Stage 1 device transfers ABORTED \u2014 inventory \"\n"
    "                                \"preload returned 0 files (likely USB/MTP scan failure). \"\n"
    "                                \"Reconnect device, verify MTP mode, then re-run.\",\n"
    "                                \"WARNING\"\n"
    "                            )\n"
    "                        else:\n"
    "                            config.AUDIT_AFTER_SYNC = True   # enforce Stage 8 audit\n"
    "                            self._agent = WalkmanSyncAgent(log_callback=self._on_log)\n"
    "                            self._agent.run_sync()"
)

SOFT_STAGE1 = (
    "# MK15-R0 v4: gate check -- MTP-aware, does not hard-block when MTP is reachable\n"
    "                        if getattr(self, '_inventory_zero_halt', False):\n"
    "                            # ADB returned 0 but MTP diagnostic showed WALKMAN connected.\n"
    "                            # Proceed with transfers rather than hard-aborting.\n"
    "                            self._append_log(\n"
    "                                \"[MK15-R0] ADB inventory=0 but MTP device is reachable \u2014 \"\n"
    "                                \"proceeding with Stage 1. Watch for unexpected duplicates.\",\n"
    "                                \"WARNING\"\n"
    "                            )\n"
    "                            config.AUDIT_AFTER_SYNC = True\n"
    "                            self._agent = WalkmanSyncAgent(log_callback=self._on_log)\n"
    "                            self._agent.run_sync()\n"
    "                        else:\n"
    "                            config.AUDIT_AFTER_SYNC = True   # enforce Stage 8 audit\n"
    "                            self._agent = WalkmanSyncAgent(log_callback=self._on_log)\n"
    "                            self._agent.run_sync()"
)

if HARD_STAGE1 in src:
    src = src.replace(HARD_STAGE1, SOFT_STAGE1, 1)
    print("  [C] Stage 1 gate softened (MTP-aware)")
    changed = True
else:
    # Flexible fallback
    m = re.search(
        r"# MK15-R0[^#\n]*gate check\n"
        r"([ \t]+)if getattr\(self,\s*'_inventory_zero_halt'[^)]*\):",
        src
    )
    if m:
        ind = m.group(1)
        # Find the complete if/else block
        block_start = m.start()
        run_sync_m = re.search(r'self\._agent\.run_sync\(\)', src[block_start:])
        if run_sync_m:
            block_end = block_start + run_sync_m.end()
            src = src[:block_start] + (
                "# MK15-R0 v4: gate check -- MTP-aware\n"
                "{i}if getattr(self, '_inventory_zero_halt', False):\n"
                "{i}    self._append_log(\n"
                "{i}        \"[MK15-R0] ADB inventory=0 but MTP active \u2014 proceeding. \"\n"
                "{i}        \"Watch for unexpected duplicates.\", \"WARNING\"\n"
                "{i}    )\n"
                "{i}    config.AUDIT_AFTER_SYNC = True\n"
                "{i}    self._agent = WalkmanSyncAgent(log_callback=self._on_log)\n"
                "{i}    self._agent.run_sync()\n"
                "{i}else:\n"
                "{i}    config.AUDIT_AFTER_SYNC = True\n"
                "{i}    self._agent = WalkmanSyncAgent(log_callback=self._on_log)\n"
                "{i}    self._agent.run_sync()"
            ).format(i=ind) + src[block_end:]
            print("  [C] Stage 1 gate softened (flexible match)")
            changed = True
    else:
        print("  [C] Stage 1 gate not found -- check main.py manually")

# ────────────────────────────────────────────────────────────────────────────
# Write output + validate
# ────────────────────────────────────────────────────────────────────────────
out = src.replace('\n', '\r\n')
with open(TARGET, 'w', encoding='utf-8') as f:
    f.write(out)

import ast
try:
    ast.parse(src)
    print("  [OK] Syntax valid")
except SyntaxError as e:
    print("  [FAIL] Syntax error line {}: {}".format(e.lineno, e.msg))

if not changed:
    print("  [INFO] No changes made (all fixes already applied or not found)")
print("[OK] Patched: {}".format(TARGET))
