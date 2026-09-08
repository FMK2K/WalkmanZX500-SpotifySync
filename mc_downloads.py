"""
MC simulation of download-side fixes for Rev 14.9 Run 2 (post-transport-fix).

Calibrated against LOG-13 (2026-04-21 Rev 14.9 run):
- 54 unique tracks attempted in waterfall in ~1h37m
- 37 tidaldl successes (= 69% success rate on its attempts)
- 0 successes from any other provider
- 8 providers (tidaldl_url, qqdl, monochrome, tiddl, spotiflac, streamrip, ytdlp, spotdl)
  circuit-broke at 15/15 failures in first ~30 min

Per-provider observed failure times (from the IGOR'S THEME waterfall at 01:52:44 -> 01:54:10):
  tidaldl (success path):     ~3s  mean
  tidaldl_url (dead):         ~45s (1 fail)
  qqdl:                       ~0.2s
  monochrome:                 ~0.05s
  tiddl:                      ~6s (fast fail)
  spotiflac:                  ~1s (returns "no audio")
  streamrip:                  ~30s
  ytdlp:                      ~3s (actually downloads webm, then fails ffmpeg check)
  spotdl:                     ~90s (uses Spotify API + YT, slow)

Session constraint: post-F1+F2+F4 sim (transport works, SD routing correct).
Pipeline bottleneck is now downloads, so Stage 0 throughput is what we model.
"""
from __future__ import annotations
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Observed base rates (calibrated to LOG-13 Run 2)
# ---------------------------------------------------------------------------

LIBRARY_SIZE = 2271         # Spotify tracks
SESSION_HOURS = 6.0         # user's typical session length
SESSION_SECONDS = SESSION_HOURS * 3600

# Provider base stats: (success_rate, mean_time_on_success_s, mean_time_on_fail_s)
# Derived from LOG-13 Run 2.  "None" = unavailable / refuses fast.
# Realistic fail times: many "failed" providers actually DO expensive work
# before failing (e.g. yt-dlp downloads the full webm, then reports "no
# ffmpeg"; spotdl resolves metadata then downloads, then fails conversion).
# Recalibrated so baseline sim matches LOG-13: 37 tidaldl successes in 1.62h.
BASE_PROVIDERS = {
    "tidaldl":      (0.685, 4.0, 40.0),     # 37 succ/54 tried; fail = 30s timeout burn
    "tidaldl_url":  (0.00,  0.0, 45.0),     # full 45s timeout every time
    "qqdl":         (0.00,  0.0, 0.3),      # refuses fast
    "monochrome":   (0.00,  0.0, 0.2),
    "tiddl":        (0.00,  0.0, 6.0),
    "spotiflac":    (0.00,  0.0, 1.0),      # returns "no audio" in ~1s
    "streamrip":    (0.00,  0.0, 30.0),     # full timeout
    "ytdlp":        (0.00,  0.0, 30.0),     # DOWNLOADS the webm (~20-120s), then fails
    "spotdl":       (0.00,  0.0, 90.0),     # full downloader timeout
}

# Order in which waterfall attempts providers (verbatim from the log)
WATERFALL_ORDER = [
    "tidaldl", "tidaldl_url", "qqdl", "monochrome", "tiddl",
    "spotiflac", "streamrip", "ytdlp", "spotdl",
]

CIRCUIT_BREAKER_THRESHOLD = 15       # current rule
WORKERS = 4                          # concurrent waterfalls
TRANSFER_TIME_SUCCESS = 3.5          # seconds per successful transfer on SD

# Parallelism efficiency: with 4 workers, wall-clock speedup is NOT 4x.
# Real log: 54 waterfalls / 5820s = 108s/waterfall wall-clock.
# If per-worker serial cost is ~55s (mean), and workers=4, a perfect
# speedup would give 14s wall-clock/waterfall -> 415 waterfalls in 5820s.
# Actual was 54. So effective parallelism factor is 54/415 = 0.13 — much
# worse than 4x because of shared pool contention, rate limiting on TIDAL,
# and the `mtp_lock` serialization downstream.
# This is the key calibration constant.
PARALLELISM_EFFICIENCY = 0.15  # 4 workers → effective ~0.6x — essentially serial (TIDAL rate limit + mtp_lock)

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class SessionStats:
    tracks_downloaded: int = 0
    tracks_transferred: int = 0
    stage0_time_s: float = 0.0
    waterfall_time_s: float = 0.0
    provider_calls: dict = field(default_factory=dict)
    provider_successes: dict = field(default_factory=dict)
    disabled_at: dict = field(default_factory=dict)  # provider -> time_s disabled


def simulate_one_session(
    providers: dict,
    waterfall_order: list,
    circuit_threshold: int = CIRCUIT_BREAKER_THRESHOLD,
    session_seconds: float = SESSION_SECONDS,
    library_size: int = LIBRARY_SIZE,
    # Fixes as feature flags
    d1_ffmpeg_fix: bool = False,       # yt-dlp becomes real provider
    d2_fast_breaker: bool = False,     # break at 5 fail w/ no global success in 3min
    d3_adaptive_order: bool = False,   # reorder by rolling success rate
    d4_auth_precheck: bool = False,    # skip tidaldl_url if auth stale
    d6_gemini_quota_skip: bool = False,  # minor: Gemini quota preskip
    d7_content_cache: bool = False,    # skip already-downloaded tracks
    user_refresh_tidal_auth: bool = False,  # user action: fresh login
    rng: random.Random = None,
) -> SessionStats:
    if rng is None:
        rng = random.Random()
    stats = SessionStats()

    # ── Apply fixes to the provider table ─────────────────────────────
    active_providers = {k: list(v) for k, v in providers.items()}

    if user_refresh_tidal_auth:
        # Fresh login: tidaldl goes to ~92% success, tidaldl_url wakes up
        active_providers["tidaldl"]     = [0.92, 2.5, 6.0]
        active_providers["tidaldl_url"] = [0.85, 3.0, 15.0]

    if d1_ffmpeg_fix:
        # yt-dlp now produces playable MP3s instead of dead webms.
        # YT coverage for mainstream music is ~60%; .webm -> .mp3 conversion adds ~4s.
        # Fail case: yt-dlp couldn't find the track at all, ~25s search burn.
        active_providers["ytdlp"] = [0.60, 35.0, 25.0]

    if d4_auth_precheck and not user_refresh_tidal_auth:
        # Detects stale TIDAL auth at startup; logs once, skips tidaldl_url
        # entirely (saves 45s per doomed attempt, doesn't fix rate)
        active_providers["tidaldl_url"] = [0.0, 0.0, 0.0]  # skipped instantly

    # ── Waterfall ordering ────────────────────────────────────────────
    base_order = list(waterfall_order)
    # D3 is adaptive runtime logic — implemented below

    # ── Circuit breaker state ─────────────────────────────────────────
    consecutive_fails = {p: 0 for p in active_providers}
    successes_count = {p: 0 for p in active_providers}
    disabled = set()
    last_any_success_at = 0.0  # for D2
    rolling_success = {p: [] for p in active_providers}  # for D3

    # ── D7: content cache ─────────────────────────────────────────────
    # Simulates a cache hit rate proportional to how many tracks already
    # existed locally from a prior partial run.  From LOG-13: ~55
    # successful downloads across 54 tracks (some album-wise) had completed
    # in Run 1.  For a subsequent run, about 2.4% of library would be cached.
    # This is small — content-cache is a 2nd/3rd-run feature more than first-run.
    cache_hit_rate = 0.024 if d7_content_cache else 0.0

    # ── Main loop: parallel waterfalls across WORKERS ─────────────────
    # We model it as sequential per-worker, using a global clock.
    # For simplicity: each track consumes 1/WORKERS of the global time cost.
    clock = 0.0
    tracks_attempted = 0

    while clock < session_seconds and tracks_attempted < library_size:
        tracks_attempted += 1

        # D7 cache hit
        if rng.random() < cache_hit_rate:
            stats.tracks_downloaded += 1
            clock += (TRANSFER_TIME_SUCCESS + 0.5) / WORKERS
            continue

        # D3: adaptive ordering — put highest rolling success rate first
        if d3_adaptive_order:
            def rank(p):
                hits = rolling_success[p][-30:]
                if not hits:
                    return 0.5  # unknown: middle priority
                return sum(hits) / len(hits)
            order = sorted(base_order, key=rank, reverse=True)
            # Keep disabled providers at the end (skipped anyway)
        else:
            order = base_order

        # Walk the waterfall for this track
        track_succeeded = False
        track_time = 0.0

        for provider in order:
            if provider in disabled:
                continue

            # D2: fast breaker check — if >5 consecutive fails & no global
            # success in 3min, provisionally disable
            if d2_fast_breaker:
                if consecutive_fails[provider] >= 5 and (clock - last_any_success_at) > 180:
                    disabled.add(provider)
                    stats.disabled_at[provider] = clock
                    continue

            # Classic circuit breaker
            if consecutive_fails[provider] >= circuit_threshold and successes_count[provider] == 0:
                disabled.add(provider)
                stats.disabled_at[provider] = clock
                continue

            rate, t_succ, t_fail = active_providers[provider]

            # If this provider is already at rate=0 but has a nonzero fail time,
            # it still burns that time (e.g., streamrip waits 30s before giving up)
            stats.provider_calls[provider] = stats.provider_calls.get(provider, 0) + 1

            if rng.random() < rate:
                # Success
                track_time += t_succ
                consecutive_fails[provider] = 0
                successes_count[provider] = successes_count.get(provider, 0) + 1
                rolling_success[provider].append(1)
                stats.provider_successes[provider] = stats.provider_successes.get(provider, 0) + 1
                last_any_success_at = clock + track_time / WORKERS
                track_succeeded = True
                break
            else:
                # Failure
                track_time += t_fail
                consecutive_fails[provider] += 1
                rolling_success[provider].append(0)

        # Charge the track's time to the clock (scaled by parallelism efficiency)
        clock += (track_time / WORKERS) / PARALLELISM_EFFICIENCY

        if track_succeeded:
            stats.tracks_downloaded += 1
            # transfer is separate from download; it happens later, but we
            # also charge a small cost here (assume post-F1/F2 transport works)
            clock += (TRANSFER_TIME_SUCCESS / WORKERS) / PARALLELISM_EFFICIENCY
            stats.tracks_transferred += 1

    stats.stage0_time_s = clock
    return stats


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibrate():
    """
    Does the baseline reproduce LOG-13 Run 2?
    Target: 54 tracks attempted, 37 tidaldl succs, ~1h37m elapsed.
    """
    print("=" * 72)
    print("Calibration: baseline vs LOG-13 Run 2")
    print("=" * 72)
    print(f"Target: 54 tracks attempted, 37 tidaldl successes, ~1h37m (5820s)")
    print()

    # Calibrate to 1.62h window
    trials = 200
    downloads = []
    tidaldl_succs = []
    times = []

    for seed in range(trials):
        s = simulate_one_session(
            providers=BASE_PROVIDERS,
            waterfall_order=WATERFALL_ORDER,
            session_seconds=1.62 * 3600,
            rng=random.Random(seed),
        )
        downloads.append(s.tracks_downloaded)
        tidaldl_succs.append(s.provider_successes.get("tidaldl", 0))
        times.append(s.stage0_time_s)

    print(f"  Sim downloads:       {statistics.mean(downloads):6.1f} ± {statistics.stdev(downloads):5.1f}")
    print(f"  Sim tidaldl succs:   {statistics.mean(tidaldl_succs):6.1f} ± {statistics.stdev(tidaldl_succs):5.1f}")
    print(f"  Sim elapsed time s:  {statistics.mean(times):6.0f}")
    print()


# ---------------------------------------------------------------------------
# Scenario comparison
# ---------------------------------------------------------------------------

SCENARIOS = [
    ("baseline (post-F1+F2+F4, downloads unchanged)",
        dict()),
    ("D1 only  (ffmpeg fix, yt-dlp revives)",
        dict(d1_ffmpeg_fix=True)),
    ("D2 only  (fast circuit breaker)",
        dict(d2_fast_breaker=True)),
    ("D3 only  (adaptive waterfall order)",
        dict(d3_adaptive_order=True)),
    ("D4 only  (auth pre-check, skip tidaldl_url)",
        dict(d4_auth_precheck=True)),
    ("D7 only  (content cache for re-runs)",
        dict(d7_content_cache=True)),
    ("USER only  (refresh TIDAL auth — one-time user action)",
        dict(user_refresh_tidal_auth=True)),

    ("D2 + D4 (fast break + skip url)",
        dict(d2_fast_breaker=True, d4_auth_precheck=True)),
    ("D1 + D2 (ffmpeg + fast breaker)",
        dict(d1_ffmpeg_fix=True, d2_fast_breaker=True)),
    ("D1 + D2 + D4",
        dict(d1_ffmpeg_fix=True, d2_fast_breaker=True, d4_auth_precheck=True)),
    ("D1 + D4 + USER (ffmpeg + skip stale url + fresh auth)",
        dict(d1_ffmpeg_fix=True, d4_auth_precheck=True, user_refresh_tidal_auth=True)),
    ("D4 + USER (just fresh auth + skip stale url)",
        dict(d4_auth_precheck=True, user_refresh_tidal_auth=True)),

    ("ALL CODE FIXES (D1+D2+D3+D4+D7, no user action)",
        dict(d1_ffmpeg_fix=True, d2_fast_breaker=True,
             d3_adaptive_order=True, d4_auth_precheck=True,
             d7_content_cache=True)),
    ("ALL + USER AUTH REFRESH",
        dict(d1_ffmpeg_fix=True, d2_fast_breaker=True,
             d3_adaptive_order=True, d4_auth_precheck=True,
             d7_content_cache=True, user_refresh_tidal_auth=True)),
]


def run_scenarios(trials: int = 300):
    print("=" * 88)
    print(f"Scenario comparison  (n={trials} trials each, {SESSION_HOURS}h session, {LIBRARY_SIZE} tracks)")
    print("=" * 88)
    print(f"{'scenario':<60}{'downloaded μ±σ':>20}{'hours':>8}")
    print("-" * 88)

    results = {}
    for name, flags in SCENARIOS:
        downloads = []
        times = []
        for seed in range(trials):
            s = simulate_one_session(
                providers=BASE_PROVIDERS,
                waterfall_order=WATERFALL_ORDER,
                rng=random.Random(seed),
                **flags,
            )
            downloads.append(s.tracks_downloaded)
            times.append(s.stage0_time_s / 3600)
        mu = statistics.mean(downloads)
        sd = statistics.stdev(downloads) if len(set(downloads)) > 1 else 0.0
        mu_t = statistics.mean(times)
        print(f"{name:<60}{mu:>9.0f} ± {sd:<6.0f}    {mu_t:>5.2f}h")
        results[name] = (mu, sd, mu_t)
    print()
    return results


if __name__ == "__main__":
    calibrate()
    run_scenarios()
