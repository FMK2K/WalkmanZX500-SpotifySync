"""
mc_rev1496.py — Monte Carlo evaluation of Rev 14.9.6 fixes against the
LOG_16 baseline.

Calibration anchor: LOG_16
  - Rev 14.9.5 deployed (offline mode + tier-2 genre)
  - 2270 liked tracks, 5484 total entries
  - 1040 tracks needed Phase 1 enrichment
  - 1007 fell through to Phase 1.5+2 (took 51 minutes)
  - 611 Phase 3 Google scrapes total
  - 425 unique artists scraped → 186 redundant scrapes (30% of work)
  - 20 Gemini 429s tripped the breaker — Phase 4 went dark for the rest
  - UnicodeDecodeError disabled the device index (Rev 14.9 scrub OFF)
  - 614+ tracks transferred at avg 7.8s per completed transfer

Scenarios:
  baseline               = Rev 14.9.5 as observed in LOG_16
  + U1 (unicode fix)     = device index works, scrub fires, _exists fast path
  + A1 (artist cache)    = 30% of Phase 3 scrapes deduped at artist level
  + A2 (Groq fallback)   = Phase 4 keeps running after Gemini quota dies
  + R2 (title casing)    = no time impact, but +X correctly-formatted tags
  ALL                    = all four shipped together (cumulative)
"""
import random
import statistics

random.seed(42)

# ── Reality model from LOG_16 ─────────────────────────────────────────
SESSION_HOURS = 6
TOTAL_TRACKS = 2271
PHASE_15_2_TRACKS = 1007            # tracks needing Phase 1.5+2
GOOGLE_SCRAPE_TRACKS = 611          # tracks that hit Phase 3
UNIQUE_ARTISTS_SCRAPED = 425        # unique artists in Phase 3
SCRAPE_DURATION_S = 5.0             # ~2s sleep + 3s HTTP
PHASE_4_AI_RATE = 0.85              # fraction of Gemini calls that succeed when token healthy
GEMINI_QUOTA_LIMIT = 60             # rough free-tier RPM limit before 429s
GROQ_AI_RATE = 0.80                 # Groq llama-3 free tier success rate
TRANSFER_AVG_S = 7.8

# Index/scrub overhead in baseline (when index is BROKEN like LOG_16)
INDEX_BROKEN_PER_TRANSFER_OVERHEAD_S = 0.15   # extra _exists probe time
INDEX_BROKEN_TRANSFERS_LOST_TO_BLOCKERS = 5   # transfers requiring slow recovery


def simulate_one_run(scenario: list[str]) -> dict:
    """Return time-and-quality metrics for one simulated run."""
    flags = {
        "u1_unicode_fix":   "u1" in scenario,
        "a1_artist_cache":  "a1" in scenario,
        "a2_groq_fallback": "a2" in scenario,
        "r2_title_case":    "r2" in scenario,
    }

    # ── Phase 3 Google scrape time ──────────────────────────────────
    if flags["a1_artist_cache"]:
        scrapes = UNIQUE_ARTISTS_SCRAPED  # one per artist, not per track
    else:
        scrapes = GOOGLE_SCRAPE_TRACKS    # per-track (with duplication)
    phase3_seconds = scrapes * SCRAPE_DURATION_S

    # ── Phase 4 AI fallback (after Phase 3) ─────────────────────────
    # Tracks still needing genre after Phase 3 fall through to Phase 4.
    p4_attempts = max(0, PHASE_15_2_TRACKS - GOOGLE_SCRAPE_TRACKS // 2)
    # In baseline (Gemini-only), the breaker trips after ~20 calls
    # and the rest get nothing.
    if flags["a2_groq_fallback"]:
        # First 20 try Gemini (most fail with 429s mid-run, calibrate
        # Gemini success during quota-good window at ~40% before breaker).
        gemini_window = 20
        gemini_successes = int(gemini_window * 0.40)
        # Remaining route through Groq waterfall
        groq_attempts = p4_attempts - gemini_window
        groq_successes = int(groq_attempts * GROQ_AI_RATE)
        p4_genre_recovered = gemini_successes + groq_successes
    else:
        # Baseline: Gemini-only, first 20 calls then breaker
        gemini_window = 20
        gemini_successes = int(gemini_window * 0.40)
        p4_genre_recovered = gemini_successes

    # Add some noise per-trial
    p4_genre_recovered = max(0, int(random.gauss(p4_genre_recovered, 8)))

    # ── Transfer overhead (index broken vs working) ──────────────────
    if flags["u1_unicode_fix"]:
        # Index works; scrub runs; _exists hits fast path
        transfer_overhead_s = 0
        transfers_lost = 0
    else:
        # Index broken every run because of LOG_16's bug
        transfers = 614  # observed in LOG_16
        transfer_overhead_s = transfers * INDEX_BROKEN_PER_TRANSFER_OVERHEAD_S
        transfers_lost = INDEX_BROKEN_TRANSFERS_LOST_TO_BLOCKERS

    # ── Tag quality score ───────────────────────────────────────────
    # R2 affects how many tracks land with correctly-cased titles.
    # In LOG_16, ~30% of yt-dlp tracks have all-caps or all-lower titles
    # due to YouTube source noise.  R2 fixes those.
    yt_tracks_with_bad_case = int(368 * 0.30)  # 368 yt-dlp transfers
    tags_correctly_cased = (yt_tracks_with_bad_case
                            if flags["r2_title_case"] else 0)

    # ── Total time saved vs baseline ─────────────────────────────────
    baseline_phase3 = GOOGLE_SCRAPE_TRACKS * SCRAPE_DURATION_S
    baseline_overhead = 614 * INDEX_BROKEN_PER_TRANSFER_OVERHEAD_S
    saved_seconds = (baseline_phase3 - phase3_seconds) + (baseline_overhead - transfer_overhead_s)

    # Composite score: (genres recovered) + (tags correctly cased) + (saved seconds × 0.05)
    score = p4_genre_recovered + tags_correctly_cased + (saved_seconds * 0.05)

    return {
        "phase3_seconds":  phase3_seconds,
        "p4_genre_recovered": p4_genre_recovered,
        "transfer_overhead_s": transfer_overhead_s,
        "transfers_lost":  transfers_lost,
        "tags_correctly_cased": tags_correctly_cased,
        "saved_seconds":   saved_seconds,
        "composite_score": score,
    }


def run_scenario(scenario: list[str], trials: int = 300) -> dict:
    results = [simulate_one_run(scenario) for _ in range(trials)]
    return {
        "phase3_min":      statistics.mean(r["phase3_seconds"] / 60 for r in results),
        "p4_genre_mean":   statistics.mean(r["p4_genre_recovered"] for r in results),
        "p4_genre_sd":     statistics.stdev(r["p4_genre_recovered"] for r in results),
        "transfer_oh":     statistics.mean(r["transfer_overhead_s"] for r in results),
        "tags_cased":      statistics.mean(r["tags_correctly_cased"] for r in results),
        "saved_min":       statistics.mean(r["saved_seconds"] / 60 for r in results),
        "score_mean":      statistics.mean(r["composite_score"] for r in results),
        "score_sd":        statistics.stdev(r["composite_score"] for r in results),
    }


SCENARIOS = [
    ("baseline (LOG_16: Rev 14.9.5 as-is)", []),
    ("+ U1 (unicode subprocess fix)",        ["u1"]),
    ("+ A1 (artist genre cache)",            ["a1"]),
    ("+ A2 (Groq fallback for Phase 4)",     ["a2"]),
    ("+ R2 (smart title-case)",              ["r2"]),
    ("U1 + A1",                              ["u1", "a1"]),
    ("U1 + A1 + A2",                         ["u1", "a1", "a2"]),
    ("ALL (Rev 14.9.6 shipped)",             ["u1", "a1", "a2", "r2"]),
]

print(f"{'Scenario':<42}  {'P3 min':>7}  {'P4 add':>7}  {'tx oh':>6}  {'cased':>6}  {'saved':>7}  {'score':>14}")
print("-" * 105)
for name, flags in SCENARIOS:
    r = run_scenario(flags)
    print(
        f"{name:<42}  "
        f"{r['phase3_min']:>5.1f}m   "
        f"{r['p4_genre_mean']:>4.0f}±{r['p4_genre_sd']:>2.0f}  "
        f"{r['transfer_oh']:>5.1f}s  "
        f"{r['tags_cased']:>5.0f}   "
        f"{r['saved_min']:>5.1f}m   "
        f"{r['score_mean']:>6.0f}±{r['score_sd']:>4.0f}"
    )
