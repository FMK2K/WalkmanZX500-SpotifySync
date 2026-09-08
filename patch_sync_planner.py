"""
PATCH 2 — sync_planner.py
MK15/16 Fix: Add build_sync_plan alias
----------------------------------------------
Root cause: walkman_sync.py calls sync_planner.build_sync_plan() but the function
is named generate_plan(). This causes an AttributeError that silently skips preflight.
"""

import sys

TARGET = sys.argv[1]  # path to sync_planner.py

with open(TARGET, 'r', encoding='utf-8') as f:
    src = f.read()

# Find the end of the generate_plan function definition to add alias after it
# The alias must be at module level, after the function
ANCHOR = "def generate_plan("
assert ANCHOR in src, f"Could not find generate_plan function"

# Add the alias at the end of the file (after all function definitions)
ALIAS_BLOCK = "\n\n# MK15/16 alias — walkman_sync.py calls build_sync_plan(); generate_plan() is the implementation\nbuild_sync_plan = generate_plan\n"
if "build_sync_plan = generate_plan" not in src:
    src = src.rstrip() + ALIAS_BLOCK

with open(TARGET, 'w', encoding='utf-8') as f:
    f.write(src)

print("[OK] Patched: {}".format(TARGET))
