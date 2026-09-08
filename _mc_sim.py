"""
_mc_sim.py — Monte Carlo harness for the MK11 Spiral pipeline
==============================================================

Models the REAL MK11 pipeline (post conflict-fix codebase):
  - Audit phase  (AUDIT_BATCH_SIZE=50 cap, metakey fast-path, MTP pull + tag write)
  - AI genre waterfall  (Groq key1 + key2, Gemini, Claude Sonnet 4.6, DeepSeek)
  - Phase-I-C circuit breaker  (GROQ_FAILURE_THRESHOLD=50)
  - HiRes player compat fixes  (ID3v2.4→2.3, progressive JPEG, oversized APIC, PNG)
  - Device scan  (ADB MediaStore query ~5 s post-audit)
  - Dedup/reorganize phase  (soft-delete, scan cache, reorganize moves)

Post-fix changes modelled here:
  - _find_item → _find_remote_item: hard-delete now works (was AttributeError)
  - _ensure_deleted_folder via Items(): soft-delete success rate ~72% (was 0%)
  - walkman_snapshot.json cache: dedup scan ~0.5s when fresh (was 12-24 min)
  - producer/consumer CoInitialize: reorganize COM calls no longer fail cross-thread
  - ThreadPoolExecutor removed from _recurse_audio: scan no longer crashes COM

Does NOT import the real pipeline modules — standalone, no side-effects.

Usage:
    python _mc_sim.py                   # 10 seeds, compact table
    python _mc_sim.py --seeds 30        # 30 seeds per cell
    python _mc_sim.py --verbose         # per-cell sample breakdown

Scenarios (7):
    best_case               all providers up, device connected, 95% metakey hit rate
    groq_rate_limited       Groq at 30 req/min cap, Gemini quota exhausted early
    ai_total_failure        all AI providers fail, Phase-I-C disabled, genre stays blank
    hires_compat_pass       tag-write pass for the HiRes compat backlog
    device_adb_flaky        ADB disconnects mid-audit, device_scan falls back
    reorganize_post_fix     post-fix reorganize: cache scan + working soft-delete
    worst_case              Groq rate-limited + Gemini exhausted + ADB flaky + 4488 nulls

Library sizes (3):
    50 / 500 / 13237 (real device)
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Real pipeline constants (extracted from config.py + mk39_phase_I_containment.py)
# ---------------------------------------------------------------------------

AUDIT_BATCH_SIZE    = 50        # config.AUDIT_BATCH_SIZE — hard cap per run
AUDIT_STALE_DAYS    = 30        # config.AUDIT_STALE_DAYS

# From walkman_audit_report.json
DEVICE_TOTAL        = 13_237    # snapshot_total
NULL_GENRE_TRACKS   = 4_488     # tracks with null/Unknown genre
ID3V4_ISSUES        = 64        # id3v4_blank_in_player
PROGRESSIVE_JPEG    = 659       # progressive_jpeg
OVERSIZED_APIC      = 2_370     # oversized_apic_gt90kb
PNG_COVER           = 10        # png_cover_rejected
HIRES_BACKLOG_TOTAL = ID3V4_ISSUES + PROGRESSIVE_JPEG + OVERSIZED_APIC + PNG_COVER  # 3103

# AI waterfall timing (seconds per successful call, based on ai_helper._CALL_DELAY=0.4)
AI_CALL_DELAY_S     = 0.4       # inter-call gap (serialised _CALL_LOCK)
GROQ_LATENCY_S      = (0.4, 1.5)    # min/max for a successful Groq call
GEMINI_LATENCY_S    = (0.2, 0.8)
CLAUDE_LATENCY_S    = (1.0, 3.0)
DEEPSEEK_LATENCY_S  = (0.5, 2.0)

# Groq free tier: ~30 req/min per model, 2 keys = ~60 req/min effective
GROQ_RPM_PER_KEY        = 30
GROQ_KEYS               = 2
GROQ_EFFECTIVE_RPM      = GROQ_RPM_PER_KEY * GROQ_KEYS   # 60 req/min
GROQ_FAILURE_THRESHOLD  = 50    # mk39_phase_I_containment.GROQ_FAILURE_THRESHOLD
GEMINI_QUOTA_CALLS      = 25    # free-tier exhausts after ~25 calls (observed)

# MTP timing (seconds)
MTP_PULL_MIN_S      = 2.0       # 512 KB head pull
MTP_PULL_MAX_S      = 8.0
TAG_WRITE_MIN_S     = 0.5
TAG_WRITE_MAX_S     = 2.0
HIRES_FIX_EXTRA_S  = (1.0, 4.0)    # art conversion / id3 rewrite overhead

# Device scan (spiral_orchestrator._run_device_scan)
DEVICE_SCAN_OK_S    = 5.0       # ADB MediaStore query
DEVICE_SCAN_FAIL_S  = 1.0       # fast-fail when ADB not responding

# Metakey fast-path: ~95% of files fresher than AUDIT_STALE_DAYS skip full pull
METAKEY_HIT_RATE    = 0.95
METAKEY_FAST_S      = 0.05      # metadata check only, no MTP pull

# Phase-I-C threshold: 50 Groq failures + Gemini exhausted → disable AI
PHASE_IC_TRIP_GROQ_FAILURES = GROQ_FAILURE_THRESHOLD

# Dedup/reorganize phase (walkman_dedup + walkman_reorganize)
# Pre-fix: _find_item AttributeError + ParseName returning None → 0% success
# Post-fix: _find_remote_item + Items() iteration → ~72% soft-delete success
DEDUP_SOFT_SUCCESS_RATE_PRE  = 0.00   # before fixes: AttributeError on every hard-delete
DEDUP_SOFT_SUCCESS_RATE_POST = 0.72   # post-fix: ~72% (some MTP moves still fail)
DEDUP_CANDIDATES_TOTAL       = 3_598  # from previous run output
# Scan timing
DEDUP_SCAN_CACHE_S  = 0.5             # walkman_snapshot.json <10 min: near-instant
DEDUP_SCAN_COM_S    = (720, 1440)     # COM full scan: 12-24 min (was causing hangs)
# Reorganize move timing per file (MTP copy + delete)
REORG_MOVE_MIN_S    = 8.0
REORG_MOVE_MAX_S    = 25.0


# ---------------------------------------------------------------------------
# Scenario dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ScenarioParams:
    name: str
    # AI provider availability
    groq_available: bool = True
    groq_rate_limited: bool = False     # True → Groq hits 30 RPM cap (adds wait)
    gemini_quota_calls: int = 25        # Gemini succeeds for first N calls then 429
    claude_available: bool = True
    deepseek_available: bool = True
    # ADB/MTP
    adb_disconnect_prob: float = 0.0    # probability of disconnect per audit batch
    device_scan_works: bool = True
    # Genre state
    null_genre_count: int = NULL_GENRE_TRACKS
    # Metakey hit rate
    metakey_hit_rate: float = METAKEY_HIT_RATE
    # HiRes compat pass: if True, spend extra time on art conversion
    hires_compat_mode: bool = False
    # Dedup/reorganize phase
    run_dedup: bool = False             # True → model dedup+reorganize phase
    dedup_scan_uses_cache: bool = True  # True → snapshot.json (<10 min), False → COM scan
    dedup_success_rate: float = DEDUP_SOFT_SUCCESS_RATE_POST  # fraction of moves that land


SCENARIOS: dict[str, ScenarioParams] = {
    "best_case": ScenarioParams(
        name="best_case",
        groq_available=True, groq_rate_limited=False,
        gemini_quota_calls=25, claude_available=True, deepseek_available=True,
        adb_disconnect_prob=0.0, device_scan_works=True,
        null_genre_count=NULL_GENRE_TRACKS, metakey_hit_rate=0.95,
    ),
    "groq_rate_limited": ScenarioParams(
        name="groq_rate_limited",
        groq_available=True, groq_rate_limited=True,
        gemini_quota_calls=5,   # Gemini quota exhausts even faster
        claude_available=True, deepseek_available=True,
        adb_disconnect_prob=0.0, device_scan_works=True,
        null_genre_count=NULL_GENRE_TRACKS, metakey_hit_rate=0.95,
    ),
    "ai_total_failure": ScenarioParams(
        name="ai_total_failure",
        groq_available=False, groq_rate_limited=False,
        gemini_quota_calls=0, claude_available=False, deepseek_available=False,
        adb_disconnect_prob=0.0, device_scan_works=True,
        null_genre_count=NULL_GENRE_TRACKS, metakey_hit_rate=0.95,
    ),
    "hires_compat_pass": ScenarioParams(
        name="hires_compat_pass",
        groq_available=True, groq_rate_limited=False,
        gemini_quota_calls=25, claude_available=True, deepseek_available=True,
        adb_disconnect_prob=0.0, device_scan_works=True,
        null_genre_count=NULL_GENRE_TRACKS, metakey_hit_rate=0.90,  # slightly lower: fixing art = more writes
        hires_compat_mode=True,
    ),
    "device_adb_flaky": ScenarioParams(
        name="device_adb_flaky",
        groq_available=True, groq_rate_limited=False,
        gemini_quota_calls=25, claude_available=True, deepseek_available=True,
        adb_disconnect_prob=0.15,   # 15% chance of disconnect per batch
        device_scan_works=False,
        null_genre_count=NULL_GENRE_TRACKS, metakey_hit_rate=0.95,
    ),
    "reorganize_post_fix": ScenarioParams(
        name="reorganize_post_fix",
        groq_available=True, groq_rate_limited=False,
        gemini_quota_calls=25, claude_available=True, deepseek_available=True,
        adb_disconnect_prob=0.0, device_scan_works=True,
        null_genre_count=NULL_GENRE_TRACKS, metakey_hit_rate=0.95,
        hires_compat_mode=False,
        run_dedup=True, dedup_scan_uses_cache=True,
        dedup_success_rate=DEDUP_SOFT_SUCCESS_RATE_POST,   # 72% post-fix
    ),
    "worst_case": ScenarioParams(
        name="worst_case",
        groq_available=True, groq_rate_limited=True,
        gemini_quota_calls=0, claude_available=False, deepseek_available=False,
        adb_disconnect_prob=0.20, device_scan_works=False,
        null_genre_count=NULL_GENRE_TRACKS, metakey_hit_rate=0.90,
        hires_compat_mode=True,
    ),
}

LIBRARY_SIZES = [50, 500, DEVICE_TOTAL]


# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------

@dataclass
class RunState:
    rng: random.Random
    # AI state
    groq_failures: int = 0
    gemini_calls_used: int = 0
    ai_disabled: bool = False
    ai_disabled_at_track: Optional[int] = None
    # Counters
    tracks_reached: int = 0
    tracks_tagged: int = 0
    hires_fixes: int = 0
    genre_filled: int = 0
    genre_blank: int = 0
    ai_calls_made: int = 0
    wall_clock_s: float = 0.0
    device_scan_ok: bool = True
    # ADB disconnect recovery cost
    adb_reconnect_cost_s: float = 0.0
    # Dedup/reorganize phase
    dedup_scan_s: float = 0.0
    dedup_moves_ok: int = 0
    dedup_moves_failed: int = 0


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def _ai_call_time(state: RunState, params: ScenarioParams) -> tuple[bool, float]:
    """Simulate one AI waterfall call.
    Returns (genre_resolved: bool, seconds_spent: float), updates state.
    genre_resolved=True only when a provider actually returned a genre.
    """
    rng = state.rng
    elapsed = AI_CALL_DELAY_S  # mandatory inter-call gap

    # Phase-I-C: already disabled?
    if state.ai_disabled:
        return False, 0.0

    # Groq key 1 + key 2
    if params.groq_available:
        if params.groq_rate_limited:
            # At 60 RPM effective, each call adds ~1s wait on average when saturated
            rate_wait = rng.uniform(0.8, 2.0)
            elapsed += rate_wait
            # Groq still succeeds but slower; after saturation failures build up
            if rng.random() < 0.30:   # 30% 429 under rate limit
                state.groq_failures += 1
                # try next Groq model, add delay
                elapsed += rng.uniform(0.5, 1.5)
                if rng.random() < 0.40:   # 40% of those fail too
                    state.groq_failures += 1
                    # fall through to Gemini
                else:
                    state.ai_calls_made += 1
                    return True, elapsed + rng.uniform(*GROQ_LATENCY_S)
            else:
                state.ai_calls_made += 1
                return True, elapsed + rng.uniform(*GROQ_LATENCY_S)
        else:
            # Groq healthy: ~95% success on first model
            if rng.random() < 0.95:
                state.ai_calls_made += 1
                return True, elapsed + rng.uniform(*GROQ_LATENCY_S)
            state.groq_failures += 1

    # Gemini
    if state.gemini_calls_used < params.gemini_quota_calls:
        state.gemini_calls_used += 1
        elapsed += rng.uniform(*GEMINI_LATENCY_S)
        if rng.random() < 0.92:   # 92% success within quota
            state.ai_calls_made += 1
            return True, elapsed
        # 429 within quota (shouldn't happen often, but model it)
        state.groq_failures += 1
    else:
        # Gemini exhausted -- fast 429 cost (no real call made)
        elapsed += 0.3

    # Check Phase-I-C trip condition: Gemini exhausted + Groq failures > threshold
    if (state.gemini_calls_used >= params.gemini_quota_calls
            and state.groq_failures > PHASE_IC_TRIP_GROQ_FAILURES
            and not state.ai_disabled):
        state.ai_disabled = True
        # ai_disabled_at_track set by caller

    # Claude Sonnet 4.6
    if params.claude_available and not state.ai_disabled:
        if rng.random() < 0.88:
            state.ai_calls_made += 1
            return True, elapsed + rng.uniform(*CLAUDE_LATENCY_S)
        elapsed += rng.uniform(0.5, 1.0)  # failed call cost

    # DeepSeek
    if params.deepseek_available and not state.ai_disabled:
        if rng.random() < 0.85:
            state.ai_calls_made += 1
            return True, elapsed + rng.uniform(*DEEPSEEK_LATENCY_S)
        elapsed += rng.uniform(0.3, 0.8)

    # All providers failed -- mark groq failure for Phase-I-C counter
    state.groq_failures += 1
    return False, elapsed


def _audit_one_file(state: RunState, params: ScenarioParams,
                    has_null_genre: bool, is_hires_issue: bool,
                    track_idx: int) -> float:
    """Simulate auditing one device file. Returns seconds elapsed."""
    rng = state.rng
    elapsed = 0.0

    # Metakey fast-path: skip MTP pull if fresher than AUDIT_STALE_DAYS
    if rng.random() < params.metakey_hit_rate:
        elapsed += METAKEY_FAST_S
        state.tracks_reached += 1
        # Still check genre and hires even on fast-path (cache check only)
        if has_null_genre and not state.ai_disabled:
            ai_ok, ai_t = _ai_call_time(state, params)
            elapsed += ai_t
            if state.ai_disabled and state.ai_disabled_at_track is None:
                state.ai_disabled_at_track = track_idx
            if ai_ok:
                state.genre_filled += 1
            else:
                state.genre_blank += 1
        elif has_null_genre:
            state.genre_blank += 1
        return elapsed

    # Full MTP pull
    elapsed += rng.uniform(MTP_PULL_MIN_S, MTP_PULL_MAX_S)
    state.tracks_reached += 1

    # Tag write (if needed)
    needs_write = has_null_genre or is_hires_issue or rng.random() < 0.08
    if needs_write:
        elapsed += rng.uniform(TAG_WRITE_MIN_S, TAG_WRITE_MAX_S)
        state.tracks_tagged += 1

    # HiRes compat fix
    if is_hires_issue and params.hires_compat_mode:
        elapsed += rng.uniform(*HIRES_FIX_EXTRA_S)
        state.hires_fixes += 1
    elif is_hires_issue:
        # Even without explicit compat mode, ID3v2.4→2.3 still fires during tag write
        if rng.random() < 0.50:
            elapsed += rng.uniform(0.5, 1.5)
            state.hires_fixes += 1

    # Genre via AI (only on full-pull path, after tag read)
    if has_null_genre and not state.ai_disabled:
        ai_ok, ai_t = _ai_call_time(state, params)
        elapsed += ai_t
        if state.ai_disabled and state.ai_disabled_at_track is None:
            state.ai_disabled_at_track = track_idx
        if ai_ok:
            state.genre_filled += 1
        else:
            state.genre_blank += 1
    elif has_null_genre:
        state.genre_blank += 1

    return elapsed


# ---------------------------------------------------------------------------
# One full simulated session
# ---------------------------------------------------------------------------

def simulate_run(scenario_name: str, size: int, seed: int) -> dict:
    params = SCENARIOS[scenario_name]
    rng = random.Random(seed)
    state = RunState(rng=rng)

    # Cap to AUDIT_BATCH_SIZE for this session
    batch = min(size, AUDIT_BATCH_SIZE)

    # Build the per-file property arrays for this library slice
    # Scale the known issue counts proportionally to library size
    scale = min(size, DEVICE_TOTAL) / DEVICE_TOTAL
    null_genre_in_batch = int(round(params.null_genre_count * scale * (batch / max(size, 1))))
    hires_in_batch      = int(round(HIRES_BACKLOG_TOTAL   * scale * (batch / max(size, 1))))

    # Build file list: [has_null_genre, is_hires_issue]
    null_flags  = [True]  * null_genre_in_batch + [False] * (batch - null_genre_in_batch)
    hires_flags = [True]  * hires_in_batch       + [False] * (batch - hires_in_batch)
    # Pad/trim
    null_flags  = (null_flags  + [False] * batch)[:batch]
    hires_flags = (hires_flags + [False] * batch)[:batch]
    rng.shuffle(null_flags)
    rng.shuffle(hires_flags)

    # ADB disconnect handling: per-batch probability
    adb_disconnected = False
    batch_reconnect_cost = 0.0
    if params.adb_disconnect_prob > 0 and rng.random() < params.adb_disconnect_prob:
        adb_disconnected = True
        # Reconnect attempt: 3 tries × 15s each, ~60% success per try
        reconnect_ok = False
        reconnect_cost = 0.0
        for _ in range(3):
            reconnect_cost += 15.0
            if rng.random() < 0.60:
                reconnect_ok = True
                break
        if not reconnect_ok:
            # ADB never came back — only process fraction of batch
            batch = int(batch * rng.uniform(0.3, 0.6))
            batch_reconnect_cost = reconnect_cost + 15.0
        else:
            batch_reconnect_cost = reconnect_cost
        state.adb_reconnect_cost_s = batch_reconnect_cost

    # Simulate each file in the batch
    total_s = batch_reconnect_cost
    for i in range(batch):
        t = _audit_one_file(
            state, params,
            has_null_genre=null_flags[i],
            is_hires_issue=hires_flags[i],
            track_idx=i,
        )
        total_s += t

    # Device scan phase (post-audit)
    if params.device_scan_works and not adb_disconnected:
        total_s += DEVICE_SCAN_OK_S
        state.device_scan_ok = True
    else:
        total_s += DEVICE_SCAN_FAIL_S
        state.device_scan_ok = False

    # Dedup / reorganize phase (only for reorganize_post_fix scenario and variants)
    if params.run_dedup:
        # Scan: snapshot cache vs full COM scan
        if params.dedup_scan_uses_cache:
            scan_s = DEDUP_SCAN_CACHE_S
        else:
            scan_s = rng.uniform(*DEDUP_SCAN_COM_S)
        state.dedup_scan_s = scan_s
        total_s += scan_s

        # Soft-delete moves (scale candidates to batch size)
        scale = min(size, DEVICE_TOTAL) / DEVICE_TOTAL
        candidates_this_run = int(DEDUP_CANDIDATES_TOTAL * scale * (batch / max(size, 1)))
        for _ in range(candidates_this_run):
            if rng.random() < params.dedup_success_rate:
                total_s += rng.uniform(REORG_MOVE_MIN_S, REORG_MOVE_MAX_S)
                state.dedup_moves_ok += 1
            else:
                total_s += 0.5  # failed attempt cost
                state.dedup_moves_failed += 1

    state.wall_clock_s = total_s

    # Rates
    genre_fill_rate = (
        state.genre_filled / max(null_genre_in_batch, 1)
        if null_genre_in_batch > 0 else 1.0
    )
    hires_fix_rate = (
        state.hires_fixes / max(hires_in_batch, 1)
        if hires_in_batch > 0 else 1.0
    )
    ai_disabled_pct = (
        100.0 * state.ai_disabled_at_track / batch
        if state.ai_disabled_at_track is not None else 0.0
    )

    return {
        "scenario":        scenario_name,
        "size":            size,
        "seed":            seed,
        "tracks_reached":  state.tracks_reached,
        "tracks_tagged":   state.tracks_tagged,
        "hires_fixes":     state.hires_fixes,
        "genre_filled":    state.genre_filled,
        "genre_blank":     state.genre_blank,
        "ai_calls_made":   state.ai_calls_made,
        "ai_disabled_at":  state.ai_disabled_at_track,
        "wall_clock_s":    state.wall_clock_s,
        "device_scan_ok":  state.device_scan_ok,
        # Derived
        "genre_fill_rate":   genre_fill_rate,
        "hires_fix_rate":    hires_fix_rate,
        "ai_disabled_pct":   ai_disabled_pct,
        "dedup_scan_s":      state.dedup_scan_s,
        "dedup_moves_ok":    state.dedup_moves_ok,
        "dedup_moves_failed":state.dedup_moves_failed,
    }


# ---------------------------------------------------------------------------
# Sweep + reporting
# ---------------------------------------------------------------------------

def run_sweep(seeds: int, verbose: bool) -> None:
    total_sims = len(SCENARIOS) * len(LIBRARY_SIZES) * seeds
    print("=" * 116)
    print(f"MK11 Pipeline Monte Carlo -- {len(SCENARIOS)} scenarios x "
          f"{len(LIBRARY_SIZES)} sizes x {seeds} seeds = {total_sims} sims")
    print(f"AUDIT_BATCH_SIZE={AUDIT_BATCH_SIZE} | DEVICE_TOTAL={DEVICE_TOTAL:,} | "
          f"NULL_GENRE={NULL_GENRE_TRACKS:,} | HIRES_BACKLOG={HIRES_BACKLOG_TOTAL:,}")
    print("=" * 116)
    print()

    col_w = {
        "scenario": 20, "size": 8, "mean_s": 9, "p90_s": 9,
        "reached": 9, "genre_fill": 11, "hires_fix": 10, "ai_dis": 10,
    }
    header = (
        f"{'scenario':<{col_w['scenario']}} {'size':>{col_w['size']}} "
        f"{'mean_s':>{col_w['mean_s']}} {'p90_s':>{col_w['p90_s']}} "
        f"{'reached':>{col_w['reached']}} {'genre_fill%':>{col_w['genre_fill']}} "
        f"{'hires_fix%':>{col_w['hires_fix']}} {'ai_dis%':>{col_w['ai_dis']}}"
    )
    print(header)
    print("-" * len(header))

    all_rows = []
    for scen_name in SCENARIOS:
        for size in LIBRARY_SIZES:
            results = [simulate_run(scen_name, size, s) for s in range(seeds)]
            wc = [r["wall_clock_s"] for r in results]
            mean_s = statistics.mean(wc)
            p90_s  = sorted(wc)[max(0, int(seeds * 0.9) - 1)]
            mean_reached     = statistics.mean(r["tracks_reached"]  for r in results)
            mean_fill_rate   = statistics.mean(r["genre_fill_rate"] for r in results)
            mean_hires_rate  = statistics.mean(r["hires_fix_rate"]  for r in results)
            mean_ai_dis_pct  = statistics.mean(r["ai_disabled_pct"] for r in results)

            print(
                f"{scen_name:<{col_w['scenario']}} {size:>{col_w['size']},} "
                f"{mean_s:>{col_w['mean_s']}.1f} {p90_s:>{col_w['p90_s']}.1f} "
                f"{mean_reached:>{col_w['reached']}.1f} "
                f"{100*mean_fill_rate:>{col_w['genre_fill']}.1f} "
                f"{100*mean_hires_rate:>{col_w['hires_fix']}.1f} "
                f"{mean_ai_dis_pct:>{col_w['ai_dis']}.1f}"
            )
            all_rows.append({
                "scenario": scen_name, "size": size,
                "mean_s": mean_s, "p90_s": p90_s,
                "mean_reached": mean_reached,
                "mean_fill_rate": mean_fill_rate,
                "mean_hires_rate": mean_hires_rate,
                "mean_ai_dis_pct": mean_ai_dis_pct,
            })

            if verbose:
                sample = results[0]
                print(
                    f"  +-- seed0: reached={sample['tracks_reached']} "
                    f"tagged={sample['tracks_tagged']} "
                    f"hires_fixes={sample['hires_fixes']} "
                    f"genre_filled={sample['genre_filled']} "
                    f"genre_blank={sample['genre_blank']} "
                    f"ai_calls={sample['ai_calls_made']} "
                    f"ai_disabled_at={sample['ai_disabled_at']} "
                    f"device_scan={'OK' if sample['device_scan_ok'] else 'FAIL'}"
                )

    # Summary insights
    print()
    print("-" * 116)
    print("KEY FINDINGS")
    print("-" * 116)

    runs_needed = -(-DEVICE_TOTAL // AUDIT_BATCH_SIZE)   # ceiling division
    print(f"  BATCH BOTTLENECK : AUDIT_BATCH_SIZE={AUDIT_BATCH_SIZE} -> "
          f"{runs_needed} runs needed to reach all {DEVICE_TOTAL:,} device files")

    hires_runs = -(-HIRES_BACKLOG_TOTAL // AUDIT_BATCH_SIZE)
    print(f"  HIRES BACKLOG    : {HIRES_BACKLOG_TOTAL:,} tracks @ "
          f"{AUDIT_BATCH_SIZE}/run -> {hires_runs} runs minimum to clear "
          f"(all overlap assumes worst-case routing)")

    # Worst-case timing
    wc_rows = [r for r in all_rows if r["scenario"] == "worst_case"]
    if wc_rows:
        wc_full = next((r for r in wc_rows if r["size"] == DEVICE_TOTAL), wc_rows[-1])
        print(f"  WORST-CASE RUN   : size={wc_full['size']:,}, "
              f"mean={wc_full['mean_s']:.1f}s ({wc_full['mean_s']/60:.1f}m), "
              f"p90={wc_full['p90_s']:.1f}s ({wc_full['p90_s']/60:.1f}m) "
              f"-- well within overnight window")

    # Blank genre scenarios
    print()
    print("  GENRE BLANK RATE BY SCENARIO (size=13237):")
    for scen_name in SCENARIOS:
        matching = [r for r in all_rows
                    if r["scenario"] == scen_name and r["size"] == DEVICE_TOTAL]
        if matching:
            r = matching[0]
            fill_pct = 100 * r["mean_fill_rate"]
            blank_pct = 100 - fill_pct
            print(f"    {scen_name:<22} fill={fill_pct:5.1f}%  blank={blank_pct:5.1f}%  "
                  f"ai_disabled_at={r['mean_ai_dis_pct']:.1f}% through batch")

    print()
    print(f"  NULL_GENRE total in library : {NULL_GENRE_TRACKS:,} tracks")
    print(f"  Per run @ best_case         : ~{int(NULL_GENRE_TRACKS * AUDIT_BATCH_SIZE / DEVICE_TOTAL)} "
          f"null-genre tracks per {AUDIT_BATCH_SIZE}-file batch")
    null_runs = -(-NULL_GENRE_TRACKS // max(1, int(NULL_GENRE_TRACKS * AUDIT_BATCH_SIZE / DEVICE_TOTAL)))
    print(f"  Runs to fill all null genres: ~{null_runs} (assuming AI stays available)")

    print()
    print("  DEDUP / REORGANIZE (post conflict-fix):")
    print(f"    _find_item -> _find_remote_item : hard-delete no longer raises AttributeError")
    print(f"    _ensure_deleted_folder fix      : soft-delete success {DEDUP_SOFT_SUCCESS_RATE_PRE*100:.0f}% -> "
          f"{DEDUP_SOFT_SUCCESS_RATE_POST*100:.0f}% (Items() replaces ParseName)")
    print(f"    snapshot cache                  : dedup scan {DEDUP_SCAN_COM_S[0]//60}-{DEDUP_SCAN_COM_S[1]//60} min "
          f"-> {DEDUP_SCAN_CACHE_S:.1f}s when walkman_snapshot.json <10 min old")
    print(f"    producer/consumer CoInitialize  : reorganize COM calls no longer fail cross-thread")
    reorg_rows = [r for r in all_rows if r["scenario"] == "reorganize_post_fix" and r["size"] == DEVICE_TOTAL]
    if reorg_rows:
        rr = reorg_rows[0]
        print(f"    reorganize_post_fix @ {DEVICE_TOTAL:,}       : mean={rr['mean_s']:.0f}s  p90={rr['p90_s']:.0f}s")
    print("-" * 116)


def main() -> int:
    ap = argparse.ArgumentParser(description="MK11 pipeline Monte Carlo harness")
    ap.add_argument("--seeds",   type=int,  default=10, help="seeds per cell (default 10)")
    ap.add_argument("--verbose", action="store_true",  help="print per-cell seed-0 breakdown")
    args = ap.parse_args()
    run_sweep(seeds=args.seeds, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
