"""
mc_rev1497.py — Monte Carlo evaluation of Rev 14.9.7 fixes against
the LOG_16_1 baseline (full 13h run, Rev 14.9.5 in production).

Calibration anchor: LOG_16_1
  - 13h 10min run (21:45:08 → 10:55:12)
  - 4665 tracks transferred (Stage 0 complete: 4665 downloaded, 62 failed)
  - 600 [Errno 13] Permission denied tag-write failures (over 13% rate)
  - 71 [WinError 32] file-in-use errors
  - 5 ADB daemon failures requiring COM fallback
  - 5 ADB push failures with truncated long-paren filenames
  - 1 Spotify 403 in audit at 10:33:06 (12+ hours into the run)
  - 207 POPM=200 candidates from streaming history (informational)
  - 58 ResourceMonitor warnings — handles 8013 → 8416 → 8377 (mild leak)

Scenarios:
  baseline                = Rev 14.9.6 as currently shipped
  + O1 (audit OFFLINE)    = audit no longer makes Spotify API call
  + P1 (tag-write retry)  = 3 retries with exponential backoff
  + P2 (paren balance)    = orphan parens stripped after truncation
  + P3 (ADB daemon retry) = kill-server + start-server self-heal
  ALL                     = all four shipped together
"""
import random
import statistics

random.seed(42)

# ── Calibration from LOG_16_1 ─────────────────────────────────────────
TOTAL_TRANSFERS = 4665
TAG_FAILURES_PER_RUN = 600                # Errno 13 events
WINERROR_32_PER_RUN = 71                  # file-in-use events (separate population)
ADB_DAEMON_FAILURES_PER_RUN = 5
LONG_FILENAME_FAILURES_PER_RUN = 5
SPOTIFY_AUDIT_API_CALLS_PER_RUN = 1       # the 403

# Probability that a tag-write failure recovers within the 1.05s
# retry window (50+200+800ms).  AV scans typically clear in 100-300ms.
P_TAG_RETRY_RECOVERS = 0.85

# Probability that an ADB daemon failure recovers after kill + start
P_DAEMON_RETRY_RECOVERS = 0.80

# Each tag failure that doesn't recover means a track lands on the
# device with stale or missing metadata — counts as a "mislabeled" track.
# Each long-filename failure means a track doesn't transfer at all (in
# Rev 14.9.6; in P2 the filename gets balanced and transfer proceeds).
# Each daemon failure used to fall back to COM (slower but not lossy);
# P3 means it stays on ADB which is faster.

# COM fallback adds ~3s per transfer vs ADB
COM_OVERHEAD_PER_TRANSFER_S = 3.0


def simulate_one_run(scenario: list[str]) -> dict:
    flags = {
        "o1_offline_audit":  "o1" in scenario,
        "p1_tag_retry":      "p1" in scenario,
        "p2_paren_balance":  "p2" in scenario,
        "p3_daemon_retry":   "p3" in scenario,
    }

    # ── Stray Spotify API call in audit ─────────────────────────────
    if flags["o1_offline_audit"]:
        spotify_api_errors = 0
    else:
        # The 403 fires once per run, late in the audit.  In a healthy
        # subscription it would succeed; the user's case is failing.
        spotify_api_errors = SPOTIFY_AUDIT_API_CALLS_PER_RUN

    # ── Tag-write failures ──────────────────────────────────────────
    # Each tag failure is independent.  P1 retries 3x with backoff.
    tag_failures_attempted = TAG_FAILURES_PER_RUN
    if flags["p1_tag_retry"]:
        # Each failure has P_TAG_RETRY_RECOVERS chance of recovering
        recovered = sum(1 for _ in range(tag_failures_attempted)
                        if random.random() < P_TAG_RETRY_RECOVERS)
        mislabeled_tracks = tag_failures_attempted - recovered
    else:
        # All 600 land mislabeled
        mislabeled_tracks = tag_failures_attempted

    # ── Long-filename ADB push failures ─────────────────────────────
    long_path_failures = LONG_FILENAME_FAILURES_PER_RUN
    if flags["p2_paren_balance"]:
        # P2 strips the orphan paren so the resulting filename is
        # shorter and unambiguous.  Empirically all 5 LOG_16_1 cases
        # had > 100 chars and the closing paren was the truncation
        # boundary — so balancing trims the path comfortably under
        # MAX_PATH.
        long_path_failures_remaining = 0
    else:
        long_path_failures_remaining = long_path_failures

    # ── ADB daemon failures ─────────────────────────────────────────
    daemon_failures = ADB_DAEMON_FAILURES_PER_RUN
    if flags["p3_daemon_retry"]:
        # P3 self-heals with kill+start.  Failures that don't recover
        # still fall back to COM (legacy behaviour preserved).
        recovered_daemon = sum(1 for _ in range(daemon_failures)
                               if random.random() < P_DAEMON_RETRY_RECOVERS)
        com_fallbacks = daemon_failures - recovered_daemon
    else:
        # All 5 fall back to COM
        com_fallbacks = daemon_failures

    com_overhead_s = com_fallbacks * COM_OVERHEAD_PER_TRANSFER_S

    # Add some noise per-trial
    mislabeled_tracks = max(0, int(random.gauss(mislabeled_tracks, 5)))

    # Composite score weights:
    #   - mislabeled_tracks: -1 each (they show up wrong on device)
    #   - long_path_failures: -3 each (track missing entirely)
    #   - spotify_api_errors: -2 each (1 noisy log line per run)
    #   - com_overhead: -0.1 per second (COM is slower but works)
    score = -(
        mislabeled_tracks * 1.0
        + long_path_failures_remaining * 3.0
        + spotify_api_errors * 2.0
        + com_overhead_s * 0.1
    )

    return {
        "spotify_api_errors":          spotify_api_errors,
        "mislabeled_tracks":           mislabeled_tracks,
        "long_path_failures":          long_path_failures_remaining,
        "com_fallbacks":               com_fallbacks,
        "com_overhead_s":              com_overhead_s,
        "composite_score":             score,
    }


def run_scenario(scenario: list[str], trials: int = 300) -> dict:
    results = [simulate_one_run(scenario) for _ in range(trials)]
    return {
        "spotify_403_pct":  100 * sum(1 for r in results if r["spotify_api_errors"]) / trials,
        "mislabeled_mean":  statistics.mean(r["mislabeled_tracks"] for r in results),
        "mislabeled_sd":    statistics.stdev(r["mislabeled_tracks"] for r in results),
        "long_path_mean":   statistics.mean(r["long_path_failures"] for r in results),
        "com_overhead_s":   statistics.mean(r["com_overhead_s"] for r in results),
        "score_mean":       statistics.mean(r["composite_score"] for r in results),
        "score_sd":         statistics.stdev(r["composite_score"] for r in results),
    }


SCENARIOS = [
    ("baseline (LOG_16_1: Rev 14.9.5/14.9.6 as-is)",  []),
    ("+ O1 (audit OFFLINE_MODE)",                      ["o1"]),
    ("+ P1 (tag-write retry)",                         ["p1"]),
    ("+ P2 (paren-balance _safe)",                     ["p2"]),
    ("+ P3 (ADB daemon retry)",                        ["p3"]),
    ("ALL (Rev 14.9.7 shipped)",                       ["o1", "p1", "p2", "p3"]),
]

print(f"{'Scenario':<50}  {'403':>5}  {'mislabel':>10}  {'lost':>5}  {'COM ov':>8}  {'score':>14}")
print("-" * 105)
for name, flags in SCENARIOS:
    r = run_scenario(flags)
    print(
        f"{name:<50}  "
        f"{r['spotify_403_pct']:>4.0f}%  "
        f"{r['mislabeled_mean']:>5.0f}±{r['mislabeled_sd']:>3.0f}  "
        f"{r['long_path_mean']:>4.1f}   "
        f"{r['com_overhead_s']:>5.1f}s   "
        f"{r['score_mean']:>7.0f}±{r['score_sd']:>4.0f}"
    )
