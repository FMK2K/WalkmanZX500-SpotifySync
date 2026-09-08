# -*- coding: utf-8 -*-
"""
PATCH 1 -- genre_client.py
MK15/16 Fix: Stop ENRICHMENT_FAIL_OPEN log spam (once-per-session warning)
"""
import re, sys

TARGET = sys.argv[1]

with open(TARGET, 'r', encoding='utf-8', errors='replace') as f:
    src = f.read()

changed = False

# ── Change 1: add _FAIL_OPEN_WARNED flag after _ENRICH_ALL_FAIL_STREAK ──
STREAK_LINE = "_ENRICH_ALL_FAIL_STREAK"
WARNED_LINE = "_FAIL_OPEN_WARNED"

if WARNED_LINE in src:
    print("  [SKIP] _FAIL_OPEN_WARNED flag already present")
else:
    # Find the streak declaration line and append the new flag after it
    src = re.sub(
        r'(_ENRICH_ALL_FAIL_STREAK\s*:?\s*list\s*=\s*\[0\][^\n]*)',
        r'\1\n_FAIL_OPEN_WARNED:       list = [False]  # MK15: emit fail-open warning once per session',
        src, count=1
    )
    if WARNED_LINE in src:
        print("  [OK] Added _FAIL_OPEN_WARNED flag")
        changed = True
    else:
        print("  [WARN] Could not find _ENRICH_ALL_FAIL_STREAK declaration")

# ── Change 2: gate the warning to fire only once ──
# Find the pattern: if _ENRICH_ALL_FAIL_STREAK[0] == _fo_thresh: log.warning(...)
# Replace the `if streak == thresh:` check with the warned-flag guard
if "not _FAIL_OPEN_WARNED[0]" in src:
    print("  [SKIP] Warning guard already in place")
elif "_ENRICH_ALL_FAIL_STREAK[0] == _fo_thresh" in src:
    # Replace the equality check with the session flag check
    src = re.sub(
        r'if _ENRICH_ALL_FAIL_STREAK\[0\] == _fo_thresh:',
        'if not _FAIL_OPEN_WARNED[0]:  # MK15: emit exactly once per session',
        src, count=1
    )
    # Insert the flag-set line after the opening of the if block
    src = re.sub(
        r'(if not _FAIL_OPEN_WARNED\[0\]:  # MK15: emit exactly once per session\n)',
        r'\1                        _FAIL_OPEN_WARNED[0] = True\n',
        src, count=1
    )
    if "_FAIL_OPEN_WARNED[0] = True" in src:
        print("  [OK] Warning block now gated to once per session")
        changed = True
    else:
        print("  [WARN] Could not insert flag-set line")
else:
    print("  [WARN] Could not find fail-open warning block -- check genre_client.py manually")

with open(TARGET, 'w', encoding='utf-8') as f:
    f.write(src)

print("[OK] Patched: {}".format(TARGET) if changed else "[WARN] No changes made to: {}".format(TARGET))
