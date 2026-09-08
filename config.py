"""
config.py  —  Central configuration for the Walkman Sync Agent
=============================================================
Edit this file to match your setup. Every other module imports from here.
No code logic lives here — only constants and settings.

Quick-start checklist
---------------------
1. Set DEVICE_NAME to the exact name shown under This PC in File Explorer
2. Confirm WRITE_STORAGE matches what Windows shows (run Detect in the GUI)
3. Add your API keys to a .env file in the same folder as this script:
       SPOTIFY_CLIENT_ID=...
       SPOTIFY_CLIENT_SECRET=...
       LASTFM_API_KEY=...          # free: last.fm/api/account/create
       DISCOGS_TOKEN=...           # free: discogs.com → Settings → Developers
       GEMINI_API_KEY=...          # free: aistudio.google.com/app/apikey
4. Run:  pip install spotipy mutagen requests python-dotenv pywin32 SpotiFLAC
         conda install -c conda-forge ffmpeg
"""

import os
import pathlib

# Load .env from this file's directory — works regardless of CWD
try:
    from dotenv import load_dotenv as _ld
    _ld(dotenv_path=pathlib.Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Device identity
# ---------------------------------------------------------------------------

# Exact name shown under This PC > WALKMAN in File Explorer (case-insensitive)
DEVICE_NAME = "WALKMAN"

# ---------------------------------------------------------------------------
# Storage roots  ← MOST COMMON SOURCE OF ERRORS — read carefully
# ---------------------------------------------------------------------------
#
# The NW-ZX500 exposes exactly these two storage names to Windows via MTP:
#
#   "Internal shared storage"   always present (~64 GB built-in)
#   "SD card"                   only appears when a microSD card is inserted
#
# These are NOT the same as "Internal Storage" or "Memory Card" — those
# older names were wrong and caused 100% of transfer failures in early runs.
# Use the Detect button in the GUI to confirm what your device reports.
#
# STORAGE_ROOTS: all (storage_name, subfolder) pairs to scan during sync and
# audit.  SD card entries are silently skipped if no card is inserted.
#
# To scan existing files that are stored directly under the storage root,
# we include the root itself (empty subfolder).  The Music subfolder is also
# kept for files already organised by the agent or by hand.

STORAGE_ROOTS: list[tuple[str, str]] = [
    ("Internal shared storage", ""),           # root of internal storage
    ("Internal shared storage", "Music"),      # organised music folder
    ("Internal shared storage", "Downloads"),  # downloads folder
    ("SD card",                 ""),           # root of SD card
    ("SD card",                 "Music"),      # organised music folder
    ("SD card",                 "Downloads"),  # downloads folder
]

# Primary write target for new files during sync.
# The storage fallback logic (see mtp_transfer.py) will automatically try
# SD card if internal storage is full, and abort with a clear error if both
# are full or unavailable.
WRITE_STORAGE   = "SD card"
WRITE_SUBFOLDER = "Music"

# 2026-09-03 (user direction) — when True, new downloads are routed across
# Internal + SD by percent-used (adb_transfer.pick_storage_balanced via
# mtp_transfer._pick_storage_for_file) so both fill proportionally, instead of
# the WRITE_STORAGE-primary / SD-fallback policy. ADB-only; both storages keep
# a hard floor (INTERNAL_STORAGE_MIN_FREE_MB on internal, STORAGE_BUFFER on SD).
# Reading is resilient to transient `adb shell df` failures under load via
# adb_transfer._balanced_reading's 5-min last-known-good cache.
# ON as of 2026-09-03: SD is at ~87%; the ~5,200-track Phase B recovery filled
# it, and the backlog (478 deferred completeness albums + 227 retries) plus
# ongoing GUI syncs need the internal headroom. NOTE: internal storage is
# still wiped by the peer's rooting fastboot-unlock attempts — this
# deliberately accepts partial exposure (user decision, AskUserQuestion
# 2026-09-03). Set back to False to return to SD-only.
STORAGE_BALANCED_PLACEMENT: bool = True

# SD card storage name and subfolder — used as fallback when internal is full
SD_STORAGE   = "SD card"
SD_SUBFOLDER = "Music"

# Root folder name used in M3U8 path construction
MUSIC_ROOT = "Music"

# ID3 version written to every MP3.
# ZX500 silently ignores ID3v2.4 — tags appear blank in the browser.
ID3_VERSION = 3

# ---------------------------------------------------------------------------
# Local music library
# ---------------------------------------------------------------------------

BASE_DIR = pathlib.Path(__file__).parent

LOCAL_FOLDERS = [
    pathlib.Path(os.path.expanduser("~/Music")),
    pathlib.Path(os.path.expanduser("~/Downloads")),
]

# ---------------------------------------------------------------------------
# Download stage (Stage 0)
# ---------------------------------------------------------------------------
#
# When DOWNLOAD_MISSING is True, the agent automatically downloads missing
# or corrupt tracks as lossless FLAC using SpotiFLAC before each sync.
# SpotiFLAC takes a Spotify URL and resolves it to TIDAL/Qobuz/Amazon —
# no separate account needed beyond what Monochrome already uses.
#
# DOWNLOAD_DIR is a temporary staging area only.
# Flow: SpotiFLAC downloads here -> retag -> MTP to Walkman -> delete from PC.
# Files never accumulate permanently. Does NOT need to be in LOCAL_FOLDERS.

# ---------------------------------------------------------------------------
# Offline Spotify account data export
# ---------------------------------------------------------------------------
# Path to the extracted Spotify Account Data folder (from your data export).
# Contains: YourLibrary.json, Playlist1.json, StreamingHistory_music_0.json
# Download: spotify.com/account/privacy → 'Download your data'
# Set to None to skip offline enrichment (pipeline still works without it).
SPOTIFY_LOCAL_DATA_PATH: str | None = str(BASE_DIR / "Spotify Account Data")

DOWNLOAD_MISSING = True
DOWNLOAD_DIR     = pathlib.Path(os.path.expanduser("~/Downloads"))

# ---------------------------------------------------------------------------
# Download strategy (new)
# ---------------------------------------------------------------------------
# When to download entire albums instead of individual tracks:
#   "auto"   – decide based on free space and album completeness
#   "track"  – always download only missing tracks (original behaviour)
#   "album"  – always download whole albums (use carefully)
DOWNLOAD_STRATEGY = "auto"

# If an album is liked (saved) in Spotify, always download the whole album
# (only when free space allows).
PRIORITIZE_LIKED_ALBUMS = True

# For non‑liked albums, download the whole album if this many tracks are missing.
ALBUM_DOWNLOAD_THRESHOLD_MISSING = 3

# Minimum free space (GB) on the SD card to allow full‑album downloads.
# When free space falls below this, revert to per‑track downloads.
ALBUM_DOWNLOAD_MIN_FREE_SPACE_GB = 50

# ---------------------------------------------------------------------------
# Sync behaviour
# ---------------------------------------------------------------------------

# Minimum free space reserved on each storage root (bytes).
# Prevents the device OS database from running out of space.
# Set to 5 GB to fill internal until only 5 GB remains before switching to SD.
STORAGE_BUFFER = 5 * 1024 * 1024 * 1024   # 5 GB

# Maximum folder/filename component length (FAT32 filesystem limit)
MAX_NAME_LEN = 60

# Playlist name for Liked Songs as it appears on the Walkman
LIKED_SONGS_NAME = "Liked Songs"

# When True, feat. artists are written into the track title as "(feat. X, Y)"
# rather than the ARTIST tag.  This prevents ZX500 "artist splintering" where
# the same album appears under multiple artist entries in the library browser
# because different tracks have slightly different ARTIST tags.
FEATURE_ARTISTS_IN_TITLE = True

# Per-field control over which tags are written during sync.
# Set any to False to leave that field untouched on existing files.
WRITE_TAGS = {
    "title":        True,
    "artist":       True,
    "album":        True,
    "album_artist": True,
    "release_year": True,
    "track_number": True,
    "disc_number":  True,
    "genre":        True,
}

# ---------------------------------------------------------------------------
# Post-sync metadata audit (Stage 8)
# ---------------------------------------------------------------------------

# Run the full-library device audit automatically after every sync.
# Set False to trigger manually via the Audit Only button.
AUDIT_AFTER_SYNC = False   # re-enable via GUI checkbox once initial sync is done

# 2026-09-03 — when False, the "DOWNLOAD ONLY" GUI button / cli_runner
# `download` mode does NOT run the full-library LibraryAuditor "tag" stage
# (which is a multi-hour pass on a cold audit-state). Newly downloaded tracks
# are still tagged inline with whatever their track dict carries; run
# START SYNC or AUDIT ONLY afterwards for genre/cover enrichment + ID3 v2.3.
# START SYNC (mode="full") always runs the full audit regardless of this flag.
AUDIT_IN_DOWNLOAD_MODE: bool = False

# Run a post-reorganize audit after every reorganise pass.
# This catches files that were moved to canonical paths but whose tags still
# have issues (mojibake, wrong disc, stale albumartist).  The audit patch is
# lightweight on a freshly-reorganised library because the reorganiser only
# touches files it moved — MetaKey fast-path skips unchanged files.
# MK10: added — was missing from the pipeline.
AUDIT_AFTER_REORG = True

# Files audited within this many days are skipped on subsequent runs.
# Set 0 to force a full re-audit every time (slow but thorough).
AUDIT_STALE_DAYS = 30   # was 7 — re-audit each file monthly

# Number of device files to pull per MTP batch.
# Number of device files to pull per MTP batch.  Lowered from 200 to 50 in
# Rev 14.5 after the 2026-04-18 handle-exhaustion run — smaller bursts give
# Windows idle periods to release kernel-pool pressure between batches.
# Raise back toward 200 once resource_monitor confirms a handle baseline
# that doesn't climb across the run.
AUDIT_BATCH_SIZE = 50   # MC sim: 265 runs needed to reach all 13,237 files at this cap
                        # Raise to 100-150 when GDI handles stay below ~6,000 between batches

# 2026-09-03 (user direction) — periodic local-temp sweep during the audit so
# a long run can't fill the PC drive with pulled/prefetched staging copies of
# files that are already back on the Walkman (this is what crashed the audit
# with [Errno 28] at ~19 GB free). Runs in LibraryAuditor.run() so it holds
# for the CLI audit, the GUI subprocess audit, AND the legacy in-process GUI
# audit. Counted SEPARATELY per mode — no combined trigger:
#   folder-wise audit  -> every AUDIT_TEMP_SWEEP_EVERY_FOLDERS albums
#   file-batch audit   -> every AUDIT_TEMP_SWEEP_EVERY_FILES tracks
# Plus an emergency sweep whenever free space drops below the low-water mark.
AUDIT_TEMP_SWEEP_EVERY_FOLDERS = 5
AUDIT_TEMP_SWEEP_EVERY_FILES   = 100
AUDIT_TEMP_SWEEP_MIN_AGE_S     = 90       # never touch temp entries younger than this
AUDIT_TEMP_LOW_FREE_GB         = 12.0     # force a sweep below this much free on the temp drive

# Same idea for anything that downloads songs (downloader.run_downloads —
# the START SYNC / DOWNLOAD ONLY completeness + download stages, batch
# rebuild, recovery runs). Counted separately, no combined trigger:
#   album-wise downloads -> every DOWNLOAD_TEMP_SWEEP_EVERY_ALBUMS albums
#   per-track downloads  -> every DOWNLOAD_TEMP_SWEEP_EVERY_TRACKS tracks
# Plus an emergency sweep below DOWNLOAD_TEMP_LOW_FREE_GB. Uses temp_sweep.py.
DOWNLOAD_TEMP_SWEEP_EVERY_ALBUMS = 5
DOWNLOAD_TEMP_SWEEP_EVERY_TRACKS = 100
DOWNLOAD_TEMP_LOW_FREE_GB        = 12.0

# Force ID3v2.3 rewrite + art conversion on every file in the batch, not just
# files that happen to need a tag write for another reason. Targets the 3,103-track
# HiRes compatibility backlog (64 ID3v2.4, 659 progressive JPEG, 2,370 oversized APIC,
# 10 PNG). Toggle via Tools → Edit audit settings or set True here for a dedicated pass.
HIRES_COMPAT_PASS = False

# MTP is serial — do not increase either of these above 1.
AUDIT_WORKERS = 1

# Per-field control over which tags the audit is allowed to fix.
AUDIT_FIELDS = {
    "title":        True,
    "artist":       True,
    "album":        True,
    "album_artist": True,
    "release_year": True,
    "track_number": True,
    "disc_number":  True,
    "genre":        True,
    "composer":     False,
}

# ---------------------------------------------------------------------------
# Folder‑wise audit mode (album‑level)
# ---------------------------------------------------------------------------
# When True, the audit processes entire folders at once, moving them to
# canonical locations and fixing tags in one go. When False, it audits
# files in batches as before.
AUDIT_FOLDER_MODE = False

# When True, LibraryAuditor.run() skips the local-files phase (LOCAL_FOLDERS,
# e.g. ~/Downloads) entirely and goes straight to the on-device audit. Set via
# `python cli_runner.py audit --device-only` — useful for testing/validating
# device connectivity and the device-scan path in isolation, without the
# local pre-pass writing real tag changes to files awaiting reorganization.
AUDIT_DEVICE_ONLY = True
# ---------------------------------------------------------------------------
# Genre / metadata API keys
# ---------------------------------------------------------------------------
#
# Genre and release year come from four external APIs, tried in waterfall order:
#   Phase 1 (parallel, fast):  Last.fm  →  Deezer
#   Phase 2 (sequential, slow): MusicBrainz  →  Discogs
#
# Spotify is NEVER used for genre or year — its genre data is artist-level
# only, blocked by Developer Mode quotas, and less accurate than these sources.
#
# Get your free keys:
#   Last.fm:  https://www.last.fm/api/account/create
#   Discogs:  discogs.com → Settings → Developers → Generate token
#   Gemini:   https://aistudio.google.com/app/apikey
#
# Set these in a .env file in the same folder as this script.

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
DISCOGS_TOKEN  = os.environ.get("DISCOGS_TOKEN",  "")

# Google Gemini API key – used by genre fallback and transfer timeout advisor
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Anthropic API key – kept for backward compatibility (not used if GEMINI exists)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# AI inference — Groq fallback providers
# ---------------------------------------------------------------------------
# Groq provides a free-tier OpenAI-compatible API with sub-second latency.
# Models used in the waterfall (after Gemini direct):
#   llama-3.3-70b-versatile  → best quality, JSON mode supported
#   mixtral-8x7b-32768       → reliable JSON fallback
#   llama-3.1-8b-instant     → fast last resort
#
# Free tier: ~30 req/min per model.  No credit card required.
# Get your key at: https://console.groq.com/keys
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY",     "")
NEW_GROQ_API_KEY = os.environ.get("NEW_GROQ_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ---------------------------------------------------------------------------
# MusicFetch — music links & metadata API  (optional, recommended)
# ---------------------------------------------------------------------------
# MusicFetch returns per-track metadata (ISRC, label, release date, streaming
# links) by resolving a Spotify/Apple Music/Deezer URL to a canonical record.
# Better release-date and label coverage than MusicBrainz for mainstream
# tracks, significantly faster than Discogs.
#
# Integration: genre_client.py Phase 1.5 — inserted between Deezer and
# MusicBrainz, only called for tracks Phase 1 couldn't resolve a year for.
#
# Get your key at: https://musicfetch.io  (set MUSICFETCH_API_KEY in .env)
MUSICFETCH_API_KEY = os.environ.get("MUSICFETCH_API_KEY", "")

# Phase 1 parallel workers.  50 was the practical ceiling before Rev 14.5;
# lowered to 25 after the 2026-04-18 handle-exhaustion screenshot showed
# Windows running out of GDI/USER objects under the 50-worker subprocess tree.
# 25 is still well inside Last.fm's 5 req/s rate limit (effective parallelism
# is I/O-bound on the remote, not on local process count).  Raise back to 50
# only after confirming resource_monitor baselines stay well below throttle.
GENRE_WORKERS = 25

# ---------------------------------------------------------------------------
# File paths (all relative to the script folder)
# ---------------------------------------------------------------------------

LOG_FILE          = BASE_DIR / "sync_log.txt"
REPORT_FILE       = BASE_DIR / "missing_tracks.txt"
AUDIT_REPORT_FILE = BASE_DIR / "audit_report.txt"
CACHE_FILE        = BASE_DIR / ".spotify_metadata_cache.json"
GENRE_CACHE_FILE  = BASE_DIR / ".genre_cache.json"
AUDIT_STATE_FILE  = BASE_DIR / ".audit_state.json"
TOKEN_CACHE       = BASE_DIR / ".spotify_auth_cache"

METADATA_CACHE = GENRE_CACHE_FILE   # legacy alias kept for compatibility

# ---------------------------------------------------------------------------
# ADB transport settings
# ---------------------------------------------------------------------------
#
# When USE_ADB is True (default), mtp_transfer.py tries ADB for all device
# I/O before falling back to the original Shell.Application COM path.
# ADB is faster, more reliable, and works without a message pump.
#
# Prerequisites:
#   1. Enable USB Debugging on the Walkman (Settings → Developer Options).
#   2. Install Android Platform Tools so "adb" is in PATH:
#        https://developer.android.com/tools/releases/platform-tools
#   3. Connect via USB, accept the "Allow USB Debugging?" dialog on device.
#   4. Confirm with: adb devices  (should list your device as "device")
#
# Set USE_ADB = False to force the original COM/MTP path at all times.

# MK14.9: Reverted to False.  The ZX500's ADB stack goes offline unpredictably
# (reports `offline` in `adb devices`) and blocks the entire pipeline.
# The COM/MTP path via Shell.Application is the stable primary transport.
# Set to True only if USB debugging is consistently authorized and `adb devices`
# returns `device` (not `offline` or `unauthorized`) across reboots.
USE_ADB = True

# If multiple Android devices are connected simultaneously, set the serial
# number of the Walkman here.  Run "adb devices" to find it.
# Leave as None to use whichever single device is connected.
ADB_DEVICE_SERIAL: str | None = None

# Per-command timeout in seconds for adb operations.
# Increase if large FLAC transfers time out on slow USB connections.
ADB_TIMEOUT = 120

# ---------------------------------------------------------------------------
# ADB over WiFi (TCP/IP) — alternative link for the SAME adb protocol,
# added 2026-07-30
# ---------------------------------------------------------------------------
# Different from LocalSend below: this is still real ADB (just over TCP
# instead of USB), so every existing pull/stat/mv/rm/shell function keeps
# working unchanged once connected — full bidirectional access, not a
# push-only inbox+mv workaround. Does NOT fix ADB's own flakiness (same
# adbd daemon either way); it only removes the cable as a variable and
# gives audit/reorganize/dedup a way to keep using real ADB (not just COM
# fallback) when USB ADB drops mid-session.
# Requires one USB-connected "Enable ADB over WiFi" step to arm (`adb
# tcpip`) — reverts to USB-only after the device reboots or adbd restarts,
# so it is not a permanent cable replacement. Leaving the TCP/IP port open
# is a minor exposure on untrusted networks; use "Disconnect ADB WiFi" when
# done if that matters on your network.
ADB_WIFI_ENABLED = True
ADB_WIFI_HOST: str | None  = '192.168.68.56'   # 2026-09-01: device DHCP IP moved .63 -> .56
ADB_WIFI_PORT = 5555

# ---------------------------------------------------------------------------
# LocalSend (WiFi push transport) — tried first for Internal-storage writes
# ---------------------------------------------------------------------------
# Added 2026-07-17 after a night of MTP hangs/live-locks (fixed separately)
# and discovering ADB itself is flaky (drops unpredictably even after a
# cable replug). LocalSend is WiFi/HTTP, no USB — but push-only (the device
# can't be told to send files back headlessly) and the Walkman auto-accepts
# into one fixed flat folder, so every send needs a follow-up ADB `mv`
# (mtp_transfer.copy_file_to_device -> localsend_transfer.push_file_via_localsend).
# Falls back to the existing ADB/COM routed transport automatically on any
# failure — safe to leave on, but keep OFF until the send+mv combination has
# been confirmed against a real file on the live device (single-file send
# alone was verified 2026-07-17; the follow-up mv has not).
LOCALSEND_ENABLED = False
# ---------------------------------------------------------------------------
# Synced lyrics (LRCLIB) — Task 8, 2026-07-18
# ---------------------------------------------------------------------------
# Populates the Walkman's native 3-tier lyrics display (embedded tag -> .lrc
# sidecar -> .txt sidecar; confirmed against the decompiled Lyrics.java in
# walkman_HiRes(95)/) by querying LRCLIB (https://lrclib.net), a free,
# unauthenticated synced-lyrics API. Opt-in — new external dependency, same
# posture as LOCALSEND_ENABLED before it. See lyrics_fetch.py.
LYRICS_ENABLED = False
LYRICS_REQUEST_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# Self-hosted discovery via ListenBrainz — Task 13, 2026-07-18
# ---------------------------------------------------------------------------
# Originally scoped as an Explo (Docker) client; pivoted after finding Explo
# has no documented API for fetching its recommended track list without also
# running its own download step. Talks directly to ListenBrainz's own public
# API instead (real, documented, no auth needed for public playlists) — see
# listenbrainz_client.py. Opt-in; requires LISTENBRAINZ_USERNAME to be set.
LISTENBRAINZ_DISCOVERY_ENABLED = True
LISTENBRAINZ_USERNAME = ""
LISTENBRAINZ_REQUEST_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# MediaScanner trigger  ← DISABLED: these broadcasts are dead for the Walkman
# ---------------------------------------------------------------------------
# The Walkman Music app uses its OWN private SQLite database (media.db via
# PlayerMediaProvider). It does NOT query Android's MediaStore at all.
# android.intent.action.MEDIA_SCANNER_SCAN_FILE only updates MediaStore —
# the Walkman app never sees it.
#
# End-of-session refresh is handled by walkman_app_refresh.py (ui_tap or
# MEDIA_MOUNTED broadcast). Per-file scan broadcasts are pure overhead.
TRIGGER_MEDIA_SCAN = False

# ---------------------------------------------------------------------------
# Batch TIDAL library rebuild
# ---------------------------------------------------------------------------
# run_batch_rebuild() in downloader.py uses these to decide which artists to
# re-download fresh from TIDAL and replace on the device.
#
# REBUILD_BATCH_ARTISTS — explicit artist names, case-insensitive.
#   e.g. ["OutKast", "Run The Jewels"]
#
# REBUILD_BATCH_LETTERS — first-letter prefixes; matches all artists whose
#   normalised name starts with that letter.
#   e.g. ["O", "R"] processes all O- and R-artists in one run.
#
# Either list can be empty — only the non-empty one is used; if both are
# populated, the union of matched artists is rebuilt.
#
# REBUILD_PC_MIN_FREE_GB — abort a batch album download if the PC's free space
#   on DOWNLOAD_DIR's drive would drop below this value (GiB).
#   Prevents the staging area from filling the disk mid-run.

REBUILD_BATCH_ARTISTS:  list = []   # e.g. ["OutKast", "Tame Impala"]
REBUILD_BATCH_LETTERS:  list = []   # e.g. ["O", "T"]
REBUILD_PC_MIN_FREE_GB: float = 10.0

# ---------------------------------------------------------------------------
# REV 14 — Walkman app DB refresh  (Hypothesis A fix)
# ---------------------------------------------------------------------------
# Post-sync action to force the Sony Music app to re-scan its own DB.
# MEDIA_SCANNER_SCAN_FILE only populates Android MediaStore — the
# Walkman Music app keeps its own separate index.
#
#   "off"       do nothing (files may not appear until user manually
#                refreshes in the app)
#   "ui_tap"    open the Music app and tap through menu → Update
#               database via `adb shell input tap`.  Non-destructive,
#               preserves Favorites/ratings/play-counts.  Default.
#   "pm_clear"  `adb shell pm clear <walkman-pkg>`.  Forces a full DB
#               rebuild on next app launch, but ALSO WIPES Favorites,
#               bookmarks, play counts, and EQ presets.  Only use if
#               you don't care about any of those.
WALKMAN_DB_REFRESH_MODE = "ui_tap"

# Explicit package override.  Leave blank to auto-detect via
# `pm list packages` at first use; set this if your firmware uses a
# non-standard package name.
SONY_WALKMAN_PKG = ""

# Pixel coordinates used by the UI-tap path.  The defaults are calibrated
# for the stock ZX500 landscape-orientation Music app menu.  If Sony
# changes the layout in a firmware update, override the points here.
# None → use defaults in walkman_app_refresh.py.
WALKMAN_UI_TAP_COORDS: dict | None = None

# ---------------------------------------------------------------------------
# REV 14 — Preflight (dialog prevention, not dismissal)
# ---------------------------------------------------------------------------
# Runs once at session start, before any MTP transfer.  Prevents dialogs
# from spawning rather than racing to dismiss them after the fact.

ENABLE_PREFLIGHT           = True
PREFLIGHT_KILL_ADB         = True    # taskkill /F /IM adb.exe
PREFLIGHT_CLOSE_EXPLORER   = True    # close Shell windows pointing at device
PREFLIGHT_CYCLE_WPD        = False   # cycle WPDBusEnum service — requires admin elevation this app
                                      # never has; preflight.py's own docstring calls this "opt-in
                                      # only", but it was left True here, so every deep-clean run
                                      # logged a scary-looking (harmless, caught) "Access is denied"
                                      # from `net stop WPDBusEnum`. Flipped to match the documented
                                      # intent — flip back to True only when running elevated.
PREFLIGHT_DISABLE_AUTOPLAY = True    # per-user HKCU registry write


# ---------------------------------------------------------------------------
# REV 14 — Workload-aware backend routing
# ---------------------------------------------------------------------------
# ADB is 10-30× faster for bulk pushes.  COM / Shell-CopyHere is slightly
# more reliable for the audit re-stamp stage because COM transfers
# piggyback on the Sony on-disconnect MediaStore scan that Sony's
# firmware runs automatically.  The default routes bulk via ADB and
# audit via COM; both still fall back to the other backend on failure.
USE_BACKEND_ROUTING = True
BULK_BACKEND        = "adb"     # "adb" | "com"
AUDIT_BACKEND       = "com"     # "adb" | "com"

# If the dialog watcher dismisses more than this many dialogs during a
# single MTP copy, the shell stack is considered to be thrashing and the
# router preference flips for the next call.  Set to a very high number
# to effectively disable burst-failover.
DIALOG_BURST_THRESHOLD = 5

# ---------------------------------------------------------------------------
# REV 14 — SQLite sync state
# ---------------------------------------------------------------------------
# Replaces the flat .audit_state.json skip list with a per-field status
# manifest.  Enables failure-first prioritisation on subsequent runs.
# Setting this to None reverts to the old JSON behaviour.
SYNC_STATE_DB: str | None = str(BASE_DIR / ".sync_state.db")

# ---------------------------------------------------------------------------
# REV 14 — Cover art normalisation (Hypothesis C fix)
# ---------------------------------------------------------------------------
# Re-encoded APIC/Picture art embedded into audio files.  Progressive
# JPEGs, PNGs, and oversized images are converted to baseline JPEG
# ≤COVER_ART_MAX_PIXELS on the long edge and ≤COVER_ART_MAX_BYTES on
# disk.  Pillow is optional — without it, the normaliser is a
# passthrough with a warning.
COVER_ART_MAX_PIXELS = 500
COVER_ART_MAX_BYTES  = 90 * 1024   # 90 KB

# ---------------------------------------------------------------------------
# REV 14.3 — Session integrity & Windows Update awareness
# ---------------------------------------------------------------------------
# Driven by the 2026-04-18 incident analysis: a long overnight run was
# interrupted by a Windows Update TrustedInstaller-initiated triple
# restart.  The agent was fine; Windows just rebooted underneath it.
# These settings let the agent warn about a pending restart before
# starting, clean up temp dirs that interrupted runs leave behind, and
# checkpoint sync state more aggressively so an interruption costs
# minimal progress.

# How old (hours) an audit_folder_* temp dir must be before it's
# considered "stale" and eligible for cleanup.  A live run's dirs are
# typically minutes old; 2 hours is very conservative.
STALE_TEMP_AGE_HOURS = 2

# Warn (but do not auto-clean) if stale temp dirs total more than this
# many MB.  A 200-track audit can leave ~1-2 GB behind if interrupted.
STALE_TEMP_WARN_MB = 500

# Automatically delete stale audit temp dirs at session start.
# Default False — auto-delete feels aggressive without opt-in.  Set
# True if you want unattended runs to self-clean.
STALE_TEMP_AUTOCLEAN = False

# ---------------------------------------------------------------------------
# REV 14.3 — Windows Update guidance (INFORMATIONAL)
# ---------------------------------------------------------------------------
# The agent cannot disable Windows Update — that requires admin policy
# changes the user must make themselves.  But session_guard.py detects
# pending restarts and warns before starting a long run.  For the full
# mitigation, see README §11 "Overnight run interrupted by Windows
# Update" — three options from Pause-for-1-week (non-admin) to Group
# Policy no-auto-restart-with-logged-on-users (Pro/Enterprise only).


# ---------------------------------------------------------------------------
# REV 14.4 — Deduplication & flawed-file pruning
# ---------------------------------------------------------------------------
# The dedup engine removes three classes of flawed / redundant files from
# the device: zero-length / corrupt artifacts, inferior-format duplicates
# (MP3 when a FLAC of the same track exists), and inferior-metadata
# duplicates (two same-format files, keep the better-tagged one).
#
# It runs at two points:
#   - prune_before_sync    — HARD delete, runs at start of every sync.
#                            Frees space before new transfers begin.
#   - prune_during_audit   — SOFT delete (moves to .deleted/ folder),
#                            runs before reorganize so a human can review.
#
# IMPORTANT: DEDUP_MODE defaults to "report" — the engine will log what it
# WOULD delete but changes nothing.  Set to "active" to actually delete.
# This is deliberately conservative; see README §14 FAQ for the full
# behaviour contract before flipping this on.

# "report" (default): dry-run — log what would be deleted, change nothing.
# "active":           actually execute the deletion plan (hard or soft per hook).
# "off":              skip dedup entirely.
#
# MK17 Phase B (M5): bumped default to "active" per Apr 30 user direction —
# "if any repeats are found our audit should get rid of them" plus the
# specific Gongo-Aso-by-9ice-3-times case.  Pre-sync prune uses HARD delete
# (frees space immediately); during-audit prune uses SOFT delete (moves
# to .deleted/ for human review).
DEDUP_MODE = "active"

# Master on/off switches for each entry point.  DEDUP_MODE="off" overrides.
DEDUP_PRE_SYNC = True
DEDUP_PRE_REORGANIZE = True

# Folder name used for soft deletes.  Created at <storage>/<MUSIC_ROOT>/<this>/.
# Original subpath is preserved underneath so review can see where each
# file came from.  Change this if you already have a folder by this name.
DEDUP_SOFT_DELETE_FOLDER = ".deleted"

# Files smaller than this are treated as corrupt (probably failed-download
# artifacts) regardless of their canonical-key matches.  10 KB is well below
# any real MP3/FLAC but well above any accidental 0-byte file.
DEDUP_SMALL_FILE_THRESHOLD_BYTES = 10 * 1024

# Canonical matching strategy for passes 2 and 3:
#   "artist_title_album" — match on (artist, album, title); richest key wins.
#                          Title-only fallbacks route through the AI
#                          same-track resolver.  This is the "catches more,
#                          risks more false positives" setting chosen in
#                          Rev 14.4 design.
#   "title_only"         — match on title alone.  Much more aggressive;
#                          not recommended outside CLI diagnostic runs.
DEDUP_MATCH_MODE = "artist_title_album"

# When a title-only match is the only evidence two files are the same song,
# route the decision through the AI helper (same waterfall as genre lookup).
# Conservative — rejects matches with low confidence.  Set False to keep
# title-only matches but skip the AI check (faster, riskier).
DEDUP_AMBIGUOUS_USE_AI = True

# How often the on-disk dedup journal (.dedup_log.json) retains run records.
# The file keeps the last N runs so a mistaken deletion has provenance.
DEDUP_JOURNAL_KEEP_RUNS = 50


# ---------------------------------------------------------------------------
# REV 14.4 — Storage routing verbosity
# ---------------------------------------------------------------------------
# When True, adb_transfer._pick_storage_for_file() logs every per-file
# routing decision at INFO level:
#   [Router] track.mp3 (7.2MB) → SD card (214 GB free)
# This answers the single most common "where did my files go?" question
# without needing to pull debug logs.  Cost is one INFO line per transfer —
# for a 2,000-track sync that's 2,000 extra lines, which is still tiny next
# to everything else being logged.
STORAGE_ROUTING_VERBOSE = True

# When True, free-space queries are cached for this many seconds per
# storage.  Avoids hitting the device with a `stat -f` for every single
# file in a bulk transfer batch.  Set to 0 to disable caching entirely.
STORAGE_FREE_SPACE_CACHE_SECONDS = 30


# ---------------------------------------------------------------------------
# REV 14.5 — Windows handle-exhaustion monitor
# ---------------------------------------------------------------------------
# Driven by the 2026-04-18 screenshot showing Tkinter's own Tk_GetPixmap
# failing with "Not enough memory resources" and the log filling with
# [WinError 1450] Insufficient system resources.  Both are Windows
# handle-exhaustion errors, NOT disk-space errors.
#
# Windows caps per-process GDI and USER objects at 10,000 each (the
# cap lives in HKLM\...\Windows NT\CurrentVersion\Windows).  Long-running
# code that leaks handles eventually hits those ceilings — Tkinter can't
# allocate drawing surfaces, subprocess spawns fail with 1450, and the
# agent's own GUI breaks before the sync has finished.
#
# resource_monitor.py polls GDI/USER/raw-handle counts every
# RESOURCE_POLL_INTERVAL_S seconds and flips a throttle Event when any
# resource crosses the throttle threshold.  Workers that call
# resource_monitor.wait_if_throttled() before spawning subprocesses
# will pause until the handle count drops back below the relief mark.
#
# Numbers are set with headroom: throttle fires at 8,000 of the 10,000
# cap; abort fires at 9,500 (stops all new work).  On non-Windows the
# module is a no-op.

RESOURCE_MONITOR_ENABLED = True
RESOURCE_POLL_INTERVAL_S = 30.0

# Throttle thresholds: new subprocess spawns pause above these values
RESOURCE_GDI_THROTTLE    = 8000
RESOURCE_USER_THROTTLE   = 8000
RESOURCE_HANDLE_THROTTLE = 12000

# Relief thresholds: hysteresis — throttle releases only when ALL
# resources drop below these values.  Keep below the throttle set by
# at least 500 to avoid oscillation.
RESOURCE_GDI_RELIEF    = 7000
RESOURCE_USER_RELIEF   = 7000
RESOURCE_HANDLE_RELIEF = 10000

# Warn-only thresholds: log a WARNING but change no behaviour
RESOURCE_GDI_WARN    = 6000
RESOURCE_USER_WARN   = 6000
RESOURCE_HANDLE_WARN = 8000

# Abort thresholds: hard-stop new work.  Current in-flight work finishes
# but no new downloads / transfers / audit batches get scheduled.  The
# log line explicitly tells the user to restart Windows to reclaim
# leaked handles before the next run.
RESOURCE_GDI_ABORT    = 9500
RESOURCE_USER_ABORT   = 9500
RESOURCE_HANDLE_ABORT = 15000

# How long a throttled worker is willing to block waiting for relief
# before giving up and proceeding anyway.  60s default keeps the run
# moving if the monitor's thresholds are mis-calibrated.
RESOURCE_WAIT_TIMEOUT_S = 60.0

# ---------------------------------------------------------------------------
# REV 14.6 — Adaptive per-provider timeouts + circuit breakers
# ---------------------------------------------------------------------------
# All of these are optional overrides — downloader.py and genre_client.py use
# getattr(config, ..., default) so leaving them unset keeps the tuned defaults.
# Uncomment to customise.
#
# Waterfall timeout knobs
# -----------------------
# PROVIDER_TIMEOUTS — per-provider seconds budget.  Defaults tuned from LOG-10
# analysis: search-only providers get tight budgets (they fail fast), YouTube-
# backed providers get longer budgets (they legitimately need time).
#
# PROVIDER_TIMEOUTS = {
#     "tidaldl":      30,  "tidaldl_url":  45,
#     "qqdl":         30,  "monochrome":   20,
#     "tiddl":        15,  "spotiflac":    60,
#     "streamrip":    30,  "ytdlp":       120,
#     "spotdl":       90,
# }
#
# Duration scaling: tracks > LONG_THRESHOLD_S get 1.5x budget, < SHORT_THRESHOLD_S
# get 0.6x budget.  Floor prevents the short-track path from producing unviable
# budgets on already-tight providers.
# DURATION_SCALE_LONG_THRESHOLD_S  = 360     # 6 min
# DURATION_SCALE_LONG_MULT         = 1.5
# DURATION_SCALE_SHORT_THRESHOLD_S = 60
# DURATION_SCALE_SHORT_MULT        = 0.6
# DURATION_SCALE_FLOOR_S           = 10
#
# Provider circuit breaker: after N consecutive failures with ZERO successes
# on a given provider this session, stop scheduling it.  Primarily catches
# tiddl-with-no-login and dead-API-host cases.
# PROVIDER_CIRCUIT_BREAKER_ENABLED = True
# PROVIDER_CIRCUIT_FAIL_THRESHOLD  = 15
#
# Gemini 429 circuit breaker: after N consecutive 429s from Gemini, disable
# it for the rest of the session (LOG-10 burned 26 minutes on 546 consecutive
# 429s without it).  A single 200 OK resets the counter, so if the quota
# window rolls over mid-run we automatically re-enable.
# GEMINI_CIRCUIT_BREAKER_ENABLED = True
# GEMINI_429_CIRCUIT_THRESHOLD   = 20
#
# Device health retry: post-Stage-0 re-enumeration loses the tail of long
# syncs if we give up after one 15 s window.  3 retries @ 15 s keeps the
# success rate near 100 % in the real-device Monte Carlo.
# DEVICE_HEALTH_RETRY_COUNT  = 3
# DEVICE_HEALTH_RETRY_WAIT_S = 15
#
# ──────────────────────────────────────────────────────────────────────────
# Rev 14.7 — Album Folder Promotion (walkman_promote.py)
# ──────────────────────────────────────────────────────────────────────────
# OFF BY DEFAULT.  The Tools-panel "Build Promotion Plan (dry-run)" button
# always works regardless of this flag — it only builds and prints a plan.
# Set PROMOTE_ENABLED = True only after reviewing the dry-run plan against
# a real backup of your library and confirming the planned discards are
# acceptable.  A destructive execute with this flag True will permanently
# remove every file listed under DISCARDS in the plan.
# PROMOTE_ENABLED = False
#
# Q2 threshold — fraction of tracks that must name the same primary artist
# for that artist to become the album's canonical artist.  Below this,
# "Various Artists" wins.
# PROMOTE_ARTIST_AGREEMENT_THRESHOLD = 0.80
#
# Q1 orphan policy — if an album folder at /Music/<album>/ cannot be resolved
# to a canonical artist (too few tracks, no artist agreement), leave it
# alone and log a WARNING rather than quarantining to /Music/.quarantine/.
# Setting this True enables quarantine mode instead (NOT RECOMMENDED — your
# files will appear to "vanish" from the main library until you investigate).
# PROMOTE_QUARANTINE_ORPHANS = False

# ──────────────────────────────────────────────────────────────────────────
# Rev 14.8 — Transport hardening
# ──────────────────────────────────────────────────────────────────────────
COPYHERE_CIRCUIT_ENABLED   = True
COPYHERE_CIRCUIT_THRESHOLD = 3
MAX_MTP_FILE_SIZE_MB = 100
DEFERRED_TRANSFERS_FILE = None

# ──────────────────────────────────────────────────────────────────────────
# Rev 14.9 — Self-healing sync (scrub, index, verify, planner, observability)
# ──────────────────────────────────────────────────────────────────────────
# Driven by LOG-13 (2026-04-20): Rev 14.8 deployed correctly but surfaced
# three new failure modes Rev 14.8 didn't address:
#   1. adb: error: stat failed ... Not a directory — files sitting where
#      album folders need to be (legacy corruption from pre-14.8 runs)
#   2. ADB returning exit 1 on warnings even when "N file(s) pushed" indicates
#      success; code treated these as failures and fell through to COM
#   3. Silent device-out-of-space conditions; No warning until mid-run failures
# Rev 14.9 adds pre-flight scrub, in-memory device index, ADB-output parsing,
# post-transfer size verification, and progress observability.

# Pre-flight device scrub.  Runs before Stage 0 downloads begin.
# Scans /Music (to SCRUB_MAX_DEPTH) for files sitting at paths where planned
# album folders need to be created, and for zero-byte stub files left over
# from prior failed CopyHere calls.  Deletes them with a WARNING log line
# per deletion.  This automatically fixes the legacy corruption state
# documented in LOG-13.  Set SCRUB_ENABLED=False to skip (not recommended).
SCRUB_ENABLED   = True
SCRUB_MAX_DEPTH = 3   # covers /Music/<Album>/<file> and /Music/<Album>/<CD>/<file>

# In-memory device index.  One adb shell find at session start; after that
# existence checks answer from memory.  Not persisted across runs.
# Cuts ~100-200 device round-trips per sync in practice.
DEVICE_INDEX_ENABLED = True

# Pre-flight free-space check.  Uses df -k on the device and compares against
# estimated Stage 0 download size × FREE_SPACE_SAFETY_MARGIN.  Emits a
# CRITICAL log line if insufficient; does NOT block (user may have SD card
# headroom we can't predict).
FREE_SPACE_CHECK_ENABLED = True
FREE_SPACE_SAFETY_MARGIN = 1.3  # require 30% headroom over estimated size

# Rev 14.9.3 F5 — Absolute device free-space floor.  The Android system
# itself needs some breathing room (logcat, dexopt, MediaStore index, app
# crash dumps) — when free space drops below this, weird symptoms appear:
# Walkman app freezes, library scan stalls, sometimes the device reboots.
# The Walkman ZX500 community recommends 0.5 GB as a safe minimum.
# When the pre-flight detects the device below this, the sync HALTS
# (not just warns) — there's no way to safely continue.
# When mid-sync free-space drops below this floor, the next transfer
# block pauses and emits a CRITICAL log line.
MIN_FREE_SPACE_GB = 0.5

# Post-transfer verification.  After each successful transfer, adb shell stat
# the remote path and compare sizes.  On mismatch or missing, delete the
# remote file and treat as real failure.  Cost: ~100-300ms per file.
# Catches the "ADB reported success but No space left on device" case.
VERIFY_TRANSFERS_ENABLED = True

# ──────────────────────────────────────────────────────────────────────────
# Rev 14.9.1 — Bug fixes on top of Rev 14.9
# ──────────────────────────────────────────────────────────────────────────
# Driven by LOG-13 Run 2 (Rev 14.9 deployed, found 3 new surface bugs):
#   (1) GUI Detect silently overwrote WRITE_STORAGE on every click (F1).
#   (2) Router fell through to primary when free-space cache returned -1
#       (F2); on a full primary, every transfer went to the doomed volume.
#   (3) _adb_cmd crashed on stdout=None from killed subprocess — the
#       device index refresh silently failed and disabled the scrub/C2
#       optimizations for the whole run.
# Plus download-side fixes:
#   (D1) _find_ffmpeg() more resilient, CRITICAL log on absence.
#   (D4) TIDAL auth pre-check at startup.

# Optional explicit ffmpeg override.  Set this if _find_ffmpeg() somehow
# can't locate your install (diagnose via DEBUG log: look for
# '[ffmpeg] NOT FOUND after checking N locations').
# FFMPEG_EXE = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
FFMPEG_EXE = None

# Whether to run the TIDAL session pre-check at startup (D4).  500ms cost,
# catches stale auth before wasting hours on failed downloads.
TIDAL_AUTH_PRECHECK_ENABLED = True

# MK17/MK18 FIX-F11: two-tier free space thresholds
# APPROACH WINNER: flag+break (A3) — simplest, zero exception propagation risk.
# See test_mk18_fault.py AC-F11 for approach comparison results.
MIN_FREE_SPACE_GB_WARN: float = 1.0   # emit WARNING, continue transfers
MIN_FREE_SPACE_GB_HARD: float = 0.3   # emit CRITICAL, halt transfer loop

# MK17/MK18 FIX-F15 + OWN-C: run correlation ID
# Enables per-track lifecycle reconstruction in log post-mortems.
import os as _cfg_os, time as _cfg_time
RUN_ID: str = (
    f"{int(_cfg_time.time()) % 0xFFFFF:05X}"   # timestamp component
    f"{_cfg_os.getpid() % 0xFFF:03X}"           # PID component
)

# ==========================================================================
# MK15: FLAC-First Architecture Config
# ==========================================================================
# CTO directive: "Priority FLAC for Jazz/Soul/Neo Soul/Bossa Nova.
# Keep Hip-Hop/Trap as M4A — no meaningful FLAC uplift for these genres
# and 5x storage cost is not justified."
#
# Storage math (from genre cache analysis):
#   Priority FLAC (1,069 tracks × ~40MB): ~42.8 GB
#   M4A standard (2,185 tracks × ~8MB):   ~17.5 GB
#   Total mixed: ~60 GB  ← fits in 113GB SD with 53GB headroom
#   Full FLAC (4,719 tracks × ~40MB): ~189 GB  ← FAILS (only 113GB available)
#
# The flag FLAC_FIRST_ENABLED globally enables the tiered download logic.
# Set False to revert to M4A-only (Tidal) behaviour.
FLAC_FIRST_ENABLED: bool = True

# 2026-09-03 — per-provider on/off switch consumed by
# downloader._download_with_waterfall (enabled.get(p, True)).
# User direction: monochrome first, then fall back.
#   - monochrome: kept ON and primary. Its /album/ resolver (tracklist +
#     TIDAL cover art) works; its /track/ audio currently 403s ("Upstream API
#     error") but fails FAST (immediate 403, not a timeout) so it costs
#     ~2s/track to try, and the circuit breaker disables it after 15 straight
#     misses anyway. Re-checks itself if the upstream comes back mid-run.
#   - ytdlp: the working fallback (lossy AAC/MP3, ZX507-playable).
#   - spotdl: secondary YouTube-backed fallback.
#   - the rest are OFF: they only add slow dead-ends right now (tidaldl =
#     TIDAL session 401, spotiflac 0.2.8 endpoints dead + 60s timeout,
#     streamrip = no Deezer ARL, qqdl/tiddl/tidaldl_url = dead proxies).
#     Re-enable individually as sources recover (e.g. streamrip once an ARL
#     is set in ~/AppData/Roaming/streamrip/config.toml).
DOWNLOAD_PROVIDERS: dict[str, bool] = {
    "monochrome":   True,
    "tidaldl":      False,
    "tidaldl_url":  False,
    "qqdl":         False,
    "tiddl":        False,
    "spotiflac":    False,
    "streamrip":    True,   # 2026-09-05: re-enabled, Deezer ARL set (free acct -> MP3 128).
                            #  YouTube yt-dlp is bot-blocked; this is the working non-YT source.
    "ytdlp":        True,
    "spotdl":       True,
}

# Genres that should preferentially target FLAC sources (Qobuz/streamrip).
# These are the "audiophile" genres where hi-res depth genuinely matters
# and where catalog availability on Qobuz is strong.
FLAC_PRIORITY_GENRES: set[str] = {
    "Jazz", "Classic Jazz", "Contemporary Jazz", "Jazz Rap", "Jazz-Hop",
    "Soul", "Classic Soul", "Neo Soul", "Contemporary Soul",
    "R&B", "Classic R&B", "Alt R&B",
    "Bossa Nova", "MPB", "Samba",
    "Funk", "Classic Funk",
    "Conscious Hip-Hop",  # intellectual/sample-heavy — benefits from source quality
    "Abstract Hip-Hop",
    "City Pop",           # Japanese city pop — well-catalogued on Qobuz
    "Ambient",
    "Classical", "Traditional Gospel",
}

# Genres routed to Tier 2 providers (QQDL/Monochrome) for niche catalogs.
# These are African/global genres where Qobuz catalog is spotty but
# QQDL/Monochrome have deeper licensing for "World Music".
FLAC_NICHE_GENRES: set[str] = {
    "Afrobeats", "Afropop", "Afroswing",
    "Amapiano", "Gqom", "Afro House",
    "Bongo Flava", "Genge",
    "Kizomba", "Soukous", "Highlife", "Kompa", "Zouk",
    "Funk Carioca", "Baile Funk",
}

# Genres that stay as M4A — no meaningful FLAC quality uplift,
# and 3-5x storage cost is not justified for these genres.
# The downloader will skip FLAC sources and go straight to Tidal/SpotiFLAC.
M4A_PREFERRED_GENRES: set[str] = {
    "Hip-Hop", "Trap", "Drill", "UK Drill", "Grime",
    "Cloud Rap", "Boom Bap", "West Coast Rap", "Detroit Rap",
    "Chopped & Screwed", "Jersey Club", "Phonk", "Vaporwave",
    "Hyperpop", "Emo Rap", "Lo-Fi Hip-Hop",
    "Marching Band", "Anime Soundtrack",
}

# ──────────────────────────────────────────────────────────────────────────
# Task 9 (2026-07-18) — Storage-aware FLAC/MP3 fallback
# ──────────────────────────────────────────────────────────────────────────
# Additive to the genre-tier system above: after a genre-tier FLAC download
# completes, downgrade it to MP3 (CBR 320kbps) if the device's effective
# free space (device free minus the MIN_FREE_SPACE_GB system floor above)
# is below this threshold. Liked Songs / saved-playlist tracks bypass the
# downgrade by default. See downloader.py::apply_storage_aware_format_fallback.
FLAC_FALLBACK_MIN_FREE_GB: float = 5.0
FLAC_FORCE_FOR_LIKED_PLAYLISTS: bool = True

# ──────────────────────────────────────────────────────────────────────────
# Rev 14.9.5 — Offline mode + genre consolidation tuning
# ──────────────────────────────────────────────────────────────────────────
# OFFLINE_MODE=True skips Spotify API entirely and builds the track_map
# from local Spotify Account Data export files only (YourLibrary.json,
# Playlist1.json, StreamingHistory_music_*.json).  Place the unzipped
# "Spotify Account Data" folder in the project directory (BASE_DIR), or
# set SPOTIFY_LOCAL_DATA_PATH below to its absolute path.
#
# Trade-offs:
#   - No API rate limits, no auth refreshes, no failed-fetch retries
#   - Genre/year/album-image data is NOT in the export — those still come
#     from the genre_client (Last.fm/MusicBrainz) and yt-dlp thumbnails
#   - Track numbers are NOT in the export — the F1 sequential allocator
#     fills these in per-album (1, 2, 3, ...) which won't match canonical
#     CD order but does prevent filename collisions
#   - is_liked is preserved from YourLibrary.json
#   - Saved albums are loaded from YourLibrary.json:albums[] so the
#     liked-album-prioritised download path still works
# 2026-09-02: was a corrupted `_MODE = False` (the "OFFLINE" prefix had been
# chopped off in a past edit) — so nothing ever set config.OFFLINE_MODE and
# every run fell through to the Spotify API (-> 403 without Premium). User
# direction: use the local export only.
OFFLINE_MODE = True

# ---------------------------------------------------------------------------
# MK37.1-2 — Production hardened mode (CTO board mandate)
# ---------------------------------------------------------------------------
# When True:
#   - All P0 feature flags become forced ON regardless of env var or this file
#     (MK37_ENABLE_HALT_GATE, MK37_ENABLE_TRANSFER_QUORUM,
#      MK37_ENABLE_TRANSACTIONAL_DEDUP, MK37_ENFORCE_OFFLINE_CAPABILITY,
#      MK37_ENABLE_LOCK_AWARE_TAGGING)
#   - Startup runs mutation_path_audit and aborts if any unmediated
#     destructive path is introduced by future commits
#   - MK37.1-3 wrapper assertions become fatal (bypass = startup fail)
#   - MK37.1-4 crash recovery reconciliation runs at every startup
#
# Recommended for: production deployments, fleet rollouts, ops-monitored runs.
# Not recommended for: development, debugging sessions where flag flips are useful.
PRODUCTION_HARDENED_MODE = False

# Optional explicit path to the Spotify Account Data folder.  If unset,
# the loader auto-discovers BASE_DIR/"Spotify Account Data" and several
# common alternatives (Downloads/, my_spotify_data__N_/, etc.).
# SPOTIFY_LOCAL_DATA_PATH = r"C:\Users\You\my_spotify_data\Spotify Account Data"
# 2026-09-02: this line used to be `= None`, silently CLOBBERING the correct
# value set at line ~116. Keep it pointed at the in-project export folder.
SPOTIFY_LOCAL_DATA_PATH = str(BASE_DIR / "Spotify Account Data")

# Task 12 (2026-07-18) — optional explicit path to a TuneMyMusic library
# export CSV (tunemymusic.com/transfer), used as a fast-refresh supplement
# to the GDPR export above (which can take days to arrive). Purely additive
# — only fills in URIs/playlists not already known. If unset, auto-discovers
# ~/Downloads/"My Spotify Library.csv" and BASE_DIR/"My Spotify Library.csv".
# See spotify_local_data.py::augment_from_tunemymusic_csv().
TUNEMYMUSIC_CSV_PATH: str | None = None

# Genre consolidation tuning (Rev 14.9.5 G-fixes).
# Two-tier consolidation: album-level first, artist-level fallback.
# When an album has too few specific genres for confident album-mode,
# fall back to the artist's most common genre across the user's library.
GENRE_CONSOLIDATION_ENABLED = True
GENRE_ARTIST_FALLBACK_ENABLED = True
# Minimum tracks per album before we trust the album-mode result.
# Below this, prefer the artist-level mode if available.
GENRE_ALBUM_MIN_VOTES = 2

# ─── MK17 Phase B (M2/M3) — sparse-artist guard + graph inference ─────────
# When the artist's library footprint is too small, the modal-of-1 result
# isn't trustworthy (LOG_16_1 case: Alfa Kat is Amapiano, but his single
# Last.fm-tagged track came back "Abstract Hip-Hop" — modal-of-1 picked
# the wrong genre for the entire album).  Below this threshold we route
# to the graph-inference path instead of trusting the modal.
GENRE_ARTIST_MIN_TRACKS = 3

# Graph-based genre propagation.  Builds a co-occurrence graph at
# consolidation time: HEAVY edges (weight=3) between artists who appear
# together in a track's `artists` list (features/collabs); LIGHT edges
# (weight=0.5) between artists who share a playlist.  For sparse-data
# artists, vote weighted-by-edge from N-hop neighbors that DO have
# confident genres.
#
# MK17 Patch 8 (post-LOG_17 fix): playlist edge weight reduced from
# 1 to 0.5 because LOG_17 showed 776/805 sparse rescues collapsing to
# "Afrobeats" — Liked Songs (2271 tracks) was generating 2.5M weight-1
# edges that swamped the targeted weight-3 collab signal.
GENRE_GRAPH_INFERENCE_ENABLED = True
# MK39 Phase I-A5 (Council Emergency Directive 2026-05-10): explicit master
# kill-switch for the genre graph subsystem. Council §A5 required: "Investigate
# why GenreGraph builds 0 nodes; if a quick fix is not possible, add a config
# flag GENRE_GRAPH_ENABLED = False and set it to False until the graph can be
# repaired and validated." Root cause traced to track_map being empty in
# offline-mode / local-only audit paths combined with GENRE_ARTIST_MIN_TRACKS=3.
# Default is False for this release; flip to True only after the graph
# construction logic is fixed and validated against a populated library.
# This flag is checked BEFORE GENRE_GRAPH_INFERENCE_ENABLED — it is the master
# switch; the latter governs only inference once a graph exists.
GENRE_GRAPH_ENABLED = False
GENRE_GRAPH_FEATURE_WEIGHT = 3
GENRE_GRAPH_PLAYLIST_WEIGHT = 0.5
GENRE_GRAPH_HOPS = 2  # how far to walk; 2 is usually enough on a real library

# MK17 Patch 8: skip playlists with >this-many tracks when constructing
# light edges.  "Liked Songs" or "All Saved" type playlists tell the
# graph nothing about genre similarity — they just connect every artist
# to every other artist, drowning out targeted signal.
GENRE_GRAPH_MAX_PLAYLIST_SIZE = 300

# 2026-09-04 (user direction) — "use all the playlists except Rizz": playlists
# named here are hard-excluded from the genre graph regardless of purity.
# Every other playlist now contributes (weighted by its genre purity, same as
# always) instead of being dropped outright for being below the old
# GENRE_GRAPH_MIN_PURITY cutoff — see genre_graph.py::build_graph.
GENRE_GRAPH_EXCLUDED_PLAYLISTS: set[str] = {"Rizz"}

# ---------------------------------------------------------------------------
# MK10 ISSUE-D — Custom genre override table
# ---------------------------------------------------------------------------
# Artist-level and title-pattern-level genre overrides. Highest-priority —
# applied BEFORE any API lookup. Use for user-defined categories that APIs
# will never return correctly ("gache", "graph", personal genre labels).
#
# Two sub-tables:
#   GENRE_ARTIST_OVERRIDES  — {artist_name_lowercase: genre}
#     Applied when the track's primary artist matches (case-insensitive).
#   GENRE_TITLE_OVERRIDES   — {substring_lowercase: genre}
#     Applied when the track title contains the substring (case-insensitive).
#     Use sparingly — title patterns can be too broad.
#
# "gache" and "graph" are examples of user-defined micro-genres. Add your
# own entries here without touching any other file.

GENRE_ARTIST_OVERRIDES: dict[str, str] = {
    # Format: "lowercase artist name": "Canonical Genre Tag"
    # Example entries — customize freely:
    # "alfa kat":         "Amapiano",    # overrides graph inference
    # "tiakola":          "French R&B",  # French trap-soul artist
}

GENRE_TITLE_OVERRIDES: dict[str, str] = {
    # Format: "lowercase title substring": "Canonical Genre Tag"
    # Applied when the track title CONTAINS this substring.
    # "freestyle":    "Hip-Hop",        # loose freestyles → Hip-Hop
}

# User-defined micro-genre definitions. These are descriptive labels that
# don't map to any standard API taxonomy. Document them here so they can
# be applied consistently across the library.
GENRE_CUSTOM_DEFINITIONS: dict[str, str] = {
    # Format: "MicroGenre": "Description (for documentation only)"
    # "Gache":  "Haitian-influenced afrobeats-adjacent dance music",
    # "Graph":  "Graphic-novel-inspired concept rap",
}


# MK8 Fix (LOG_17_3): playlist purity threshold.  A playlist must have at
# least this fraction of its tagged tracks sharing a single dominant genre
# before its co-occurrence edges are added to the graph.  Below this
# threshold the playlist is too mixed to add useful genre signal and is
# skipped entirely.  A playlist that is 90% Afrobeats gets purity=0.9;
# a mixed playlist (30% Afrobeats, 25% Hip-Hop, 20% R&B) gets purity≈0.30
# and is skipped.  Range: 0.0–1.0; default 0.55 (slightly higher than the
# previous 0.50 to further reduce mixed-playlist noise).
GENRE_GRAPH_MIN_PURITY = 0.55

# MK17 Patch 8: diversity guard.  If the SAME genre wins more than
# this fraction of sparse rescues in a single run, the graph is broken
# (likely a high-cardinality playlist sneaking through).  Log a
# warning and stop applying further rescues that propose that genre.
# MK8 Fix (LOG_17_3): tightened from 0.5 → 0.35 — a genre needs to win
# fewer than 35% of rescues to pass (was 50%).  Combined with the purity
# filter this dramatically reduces Afrobeats over-tagging.
GENRE_GRAPH_DIVERSITY_GUARD_RATIO = 0.35
GENRE_GRAPH_DIVERSITY_GUARD_MIN_SAMPLE = 10  # need this many before judging

# ─── MK17 Phase B (M1/M6) — Downloads-folder ingestion ────────────────────
# When True, scan host-side Downloads (M1) and device-side /Download (M6)
# at sync start, fuzzy-match each loose audio file against the user's
# Spotify library, and promote matches to the canonical Music tree.
# Unmatched files are left alone (per user direction — match-only).
INGEST_ENABLED = True
INGEST_HOST_DIRS: list = []   # populated below in a try-block (path lookup)
INGEST_DEVICE_DIRS: list = ["/storage/emulated/0/Download"]  # ZX500 internal
INGEST_FUZZY_THRESHOLD = 0.85  # SequenceMatcher ratio

try:
    import pathlib as _ph
    _home = _ph.Path.home()
    INGEST_HOST_DIRS = [_home / "Downloads"]
except Exception:
    INGEST_HOST_DIRS = []

# ─── MK17 Phase B (M5) — cross-format dedup ───────────────────────────────
# Format-priority order from BEST to WORST.  When the same track is
# present in multiple formats, keep the highest-priority one and delete
# the others.  This is the canonical user-stated preference (Apr 30
# message): FLAC > MP3 > AAC > M4A.  Note this DIFFERS from the older
# walkman_dedup default which had M4A above MP3.
# MK14 CORRECTION: codec hierarchy updated from FLAC>MP3>M4A → FLAC>M4A>AAC>MP3
# Root cause: original config had MP3 ranked above M4A/AAC which caused M4A tracks
# to be deleted when inferior MP3 variants existed. MK18-R4 syncs this to match
# walkman_dedup._FORMAT_PREFERENCE (the actual dedup engine ranking).
DEDUP_LOCAL_FORMAT_PRIORITY = (".flac", ".m4a", ".aac", ".ogg",
                                ".opus", ".mp3", ".wma", ".wav")
DEDUP_AUTO_REMOVE = True  # if False, only logs candidates; if True, deletes

# ─── MK17 Phase B (M7) — album completeness ───────────────────────────────
# When True, after the main download stage, walk every album in the
# library and check whether all its tracks (per Spotify metadata) are
# present locally and on the device.  Missing tracks are queued for a
# follow-up download pass, so partial albums don't stay partial.
ALBUM_COMPLETENESS_ENABLED = True
# Stop trying after this many missing-track attempts in one run, to
# bound runtime when many albums have unobtainable tracks (DMCA).
# 2026-09-02: raised 200 -> 1000 for the post-wipe recovery run (user
# direction). Lower it back afterward if overnight runtime gets excessive.
ALBUM_COMPLETENESS_MAX_NEW_DOWNLOADS = 1000

# 2026-09-02 — album_completeness.run_completeness_pass() a/b/c rule.
# An album is completed IN FULL if ANY of:
#   (a) it is a Spotify-saved album, OR
#   (b) the user has >= ALBUM_COMPLETE_MIN_LIKED liked tracks from it, OR
#   (c) [ALBUM_COMPLETE_ARTIST_HAS_SAVED] its artist has >= 1 saved album.
# Rule (c) is broad (a prolific artist with one saved album pulls in the
# whole discography) — set False to disable it, or keep and rely on the
# MAX_NEW_DOWNLOADS cap to spread the work across runs.
ALBUM_COMPLETE_MIN_LIKED = 3
ALBUM_COMPLETE_ARTIST_HAS_SAVED = True

# =============================================================
# MK19 INCIDENT RESPONSE — 2026-05-04 Runtime control knobs
# RC1: AI dedup empty-candidate guard
# RC2: Live device gate for hard delete
# RC3: Stage-level AI circuit breaker
# RC4: Resource monitor scan throttle
# RC5: SleepBlocker long-run severity
# =============================================================
DEDUP_AI_ENABLED:                  bool  = True
DEDUP_AI_MAX_CALLS_PER_RUN:        int   = 200
DEDUP_AI_MIN_FIELD_LENGTH:         int   = 2
ALLOW_STALE_DEDUP:                 bool  = True
MAX_CONSECUTIVE_PROVIDER_FAILURES: int   = 5
DEDUP_SCAN_THROTTLE_INTERVAL:      int   = 500
LONG_RUN_THRESHOLD_HOURS:          float = 2.0

# =============================================================
# MK20 G1-G4: Genre graph + AI rate-limit fixes
# G1: Oversized playlist sampling (vs full skip)
# G2: Gemini probe exponential backoff
# G3: Groq adaptive worker reduction
# G4: Enrichment fail-open threshold
# =============================================================
GENRE_GRAPH_OVERSIZED_SAMPLE_SIZE: int   = 150    # G1: artists sampled from oversized playlists
GENRE_GRAPH_STARVATION_FALLBACK:   bool  = False  # 2026-09-02: OFF — weak "cache is mostly X" seeding biased blank-genre tracks to library-dominant genres (LOG_17 / Adele->Afrobeats). If the graph is empty, do not fake it.
GENRE_GRAPH_STARVATION_MIN_NODES:  int   = 5      # G1: trigger fallback below this

GEMINI_PROBE_MAX_FAILURES:         int   = 3      # G2: session-ban after N probe failures
GEMINI_PROBE_BACKOFF_MULTIPLIER:   float = 3.0    # G2: 5min→15min→45min probe intervals

GROQ_MAX_CALLS_PER_RUN:            int   = 2000   # G3: per-run Groq call budget
GROQ_THROTTLE_DETECTION_WINDOW:    int   = 5      # G3: 429s in this window → reduce workers
GROQ_THROTTLE_MIN_WORKERS:         int   = 3      # G3: floor for adaptive worker reduction

ENRICHMENT_FAIL_OPEN_THRESHOLD:    int   = 10     # G4: consecutive all-fail → skip track

# =============================================================
# MK20 NG3: Separate Phase 1 vs AI phase worker counts
# Phase 1 (Last.fm/Deezer, no AI): fast, can use 25 workers
# Phase 1.5+2 (AI/Groq): 8 workers + jitter prevents 429 cascade
# =============================================================
GENRE_WORKERS_PHASE1:   int   = 25   # Last.fm + Deezer (no AI, no rate limit issue)
GENRE_WORKERS_AI:       int   = 8    # Groq AI phase (free-tier: ~10 req/min)
GENRE_AI_REQUEST_JITTER: float = 1.0  # ± seconds of random delay between AI calls

# MK20 NG2: Gemini probe exponential backoff
GEMINI_PROBE_BACKOFF_MULTIPLIER: float = 3.0  # 300s → 900s → 1800s → 3600s (cap)
GEMINI_PROBE_MAX_FAILURES:       int   = 3    # disable probing after 3 fails

# MK20 NG1: Genre graph starvation fallback config
GENRE_GRAPH_FALLBACK_PURITY:        float = 0.25   # purity threshold for starvation retry
GENRE_GRAPH_STARVATION_FALLBACK:    bool  = False  # 2026-09-02: OFF (see note above)
GENRE_GRAPH_STARVATION_MIN_NODES:   int   = 5      # trigger fallback if < N nodes built
GENRE_GRAPH_OVERSIZED_SAMPLE_SIZE:  int   = 150    # artists to sample from oversized playlists

# =============================================================
# MK22 B3.2: Internal storage preflight hard gate
# CTO: "Add pre-Stage-1 free-space hard gate for internal storage
# (99% full should block run)."
# Evidence: 573MB free (99%) caused all COM transfers to produce
# size=0 placeholders → 415 verify failures → 743 dialog clicks
# =============================================================
INTERNAL_STORAGE_MIN_FREE_MB:  int   = 6144   # hard block if internal < 6GB free (2026-07-17: user-
                                                # confirmed OS minimum; also used by
                                                # adb_transfer.pick_storage_balanced)
INTERNAL_STORAGE_WARN_MB:      int   = 2048   # warn if internal < 2GB free
DIALOG_RATE_THRESHOLD_PER_MIN: int   = 20     # abort mutating ops if > 20 dialogs/min
ZOMBIE_DEDUP_MAX_ATTEMPTS:     int   = 3      # mark as zombie after N consecutive fails

# =============================================================
# MK22 B2.1: Track number conflict resolution
# CTO: "Track-number source-of-truth hierarchy:
# (1) Spotify track number, (2) album completeness, (3) deterministic allocator.
# Never let AI overwrite numeric track sequencing."
# =============================================================
TRACK_NUMBER_CONFLICT_GUARD:   bool  = True   # block dedup when track numbers disagree
ALBUM_SURVIVOR_INTEGRITY:      bool  = True   # check album completeness after dedup pass

# =============================================================
# MK39.1 L2: Dedup safety — title-only fallback gate
# Council Verdict 2026-05-10 (Forensic Review of Dry-Run Dedup Log):
# Two cross-artist data-loss false positives observed in LOG_10_5_26:
#   - Oliver Nelson 'Stolen Moments' (1961) paired with Ahmad Jamal cover
#   - Pharrell Williams 'Best Friend' paired with Brandy 'Best Friend'
# Both pairs had ZERO artist+album overlap; the title-only matching path
# had no protection beyond a fallible AI guard (failing-open in production).
# Council mandated D1 (overlap gate), D3 (warnings), D4 (strict mode).
#
# MK39.1 L3: Council "Adjustments for Fully Unattended Operation" 2026-05-10
# Operator confirmed use case is FULLY UNATTENDED OVERNIGHT RUNS. Council
# endorsed safe-default profile for this operational model:
#   DEDUP_STRICT_MODE = True            (was False)
#   DEDUP_TITLE_ONLY_MIN_OVERLAP = 0.6  (was 0.5)
# With these defaults, the nightly run will never autonomously delete a
# unique file. Some genuine duplicates spanning truncated folder names
# will NOT be cleaned automatically; operator can periodically run a
# manual non-strict dry-run to reclaim those.
# =============================================================
DEDUP_TITLE_ONLY_MIN_OVERLAP:  float = 0.6    # L3 / Council unattended-profile: raised from 0.5 to 0.6 as defense-in-depth on title-only matches
DEDUP_STRICT_MODE:             bool  = True   # L3 / Council unattended-profile: ON by default. Only rich (artist+album+title) matches survive. Operator can set False + run a manual dry-run to clean truncation dupes when ready. Env override: DEDUP_STRICT_MODE=0 to disable.

# =============================================================
# MK22 B3.4: Delete backend policy
# CTO: "DELETE_BACKEND_POLICY: internal => ADB rm (no dialog),
# SD => COM only with non-interactive flags else deferred."
# =============================================================
DELETE_BACKEND_POLICY:         str   = "smart"  # "smart" | "adb_only" | "com_only"
# "smart": internal storage → ADB rm (no dialog); SD card → COM

# =============================================================
# MK22 B1.1: Genre contradiction guard
# =============================================================
GENRE_CONTRADICTION_GUARD:     bool  = True   # reject AI if contradicts Phase-1 signal
GENRE_CONTRADICTION_FAMILIES = {
    # Keys: Phase-1 detected genre family
    # Values: genre families that AI must NOT replace them with
    "drill": {"lo-fi hip-hop", "afroswing", "jazz", "soul", "afrobeats", "amapiano"},
    "jersey-club": {"lo-fi hip-hop", "afroswing", "jazz", "soul"},
    "funk-carioca": {"lo-fi hip-hop", "afroswing", "jazz", "amapiano"},
    "uk-drill": {"lo-fi hip-hop", "afroswing", "jazz", "soul"},
    "grime": {"lo-fi hip-hop", "afroswing", "jazz"},
}

# =============================================================
# MK22 B3.2 CORRECTION: Internal full → redirect to SD, not block
# User insight: "we're supposed to move to SD card when out of disk space"
# INTERNAL_STORAGE_MIN_FREE_MB now triggers redirect (not hard block).
# Hard block only if BOTH storages are below HARD_FLOOR_MB.
# =============================================================
INTERNAL_STORAGE_REDIRECT_THRESHOLD_MB: int   = 1024  # redirect soft-deletes to SD if internal < 1GB
INTERNAL_STORAGE_HARD_FLOOR_MB:         int   = 256   # hard block only if internal < 256MB AND SD also low

# =============================================================
# MK26 RUNTIME STATE FLAGS (C1 + C3+C4 fixes)
# Set by preflight/inventory at runtime — NOT user-configurable.
# Using config.py avoids circular imports and dynamic module
# attribute tricks that failed silently in production.
# =============================================================
# C1 (A2): internal storage redirect — set True by preflight when internal < threshold
INTERNAL_REDIRECT_ACTIVE:     bool = False
# C3+C4 (A3): device inventory live — set True after confirmed ADB scan with retry
DEVICE_INVENTORY_IS_LIVE:     bool = False
# C6 (A3): cumulative dialog session counter for slow-drip detection
DIALOG_SESSION_TOTAL:         int  = 0
DIALOG_SESSION_ABORT_THRESH:  int  = 50   # abort if >50 dialog clicks this session

# =============================================================
# MK27 P1: Pipeline stage continuation policy (CTO adversarial)
# When False (default): Stage 2+ only run if Stage 1 completed.
# Set True to restore legacy "continue on any error" behaviour.
#
# MK39.1 L6.7 / Board GOV-B3 lockdown 2026-05-11:
# This module-level constant is RETAINED at False for backward
# compatibility, but it MUST NOT be flipped to True via config.py.
# Per board mandate: this footgun is available only as CLI flag
# --allow-continue-on-stage-fail with required --override-reason.
# CLI propagation uses os.environ["ALLOW_CONTINUE_ON_STAGE_FAIL_OVERRIDE"]
# read by main.py. PRODUCTION_HARDENED_MODE silently rejects the flag.
# =============================================================
ALLOW_CONTINUE_ON_STAGE_FAIL: bool = False

# =============================================================
# MK27 P6: Safe delete ordering (CTO adversarial)
# When True: Stage 0 only hard-deletes zero-byte files (always safe).
# Inferior-format duplicates are deferred to after Stage 1 confirms transfers.
# Default False preserves legacy behaviour.
# =============================================================
SAFE_DELETE_MODE: bool = False

# =============================================================
# MK28 E3-T1: Local audit metakey stamping (CTO adversarial)
# When False (default): audit of LOCAL PC files does NOT stamp
# metakey. Only device-side MTP audit (is_device=True) stamps.
# CTO: "processed ≠ correct — local fix ≠ device fix."
# Set True to restore legacy behaviour.
# =============================================================
AUDIT_STAMP_LOCAL_METAKEY: bool = False


# =============================================================
# MK32 N1-T1: AUTHORITY_POLICY — central authority semantics (CTO adversarial)
# =============================================================
# CTO: "your authority rules are becoming implicit law — they need centralization."
# Single source of truth for what each authority tier is allowed to do.
# Adding a new authority tier = single file change. No scattered conditionals.
#
# Schema per authority:
#   bypass_requires_corroboration : if True, audit gate only bypassed when Phase1 also confirms
#   allows_auto_write              : whether tags can be auto-written without human confirmation
#   max_age_days                   : after this many days, entry should be re-validated
#   conflict_action                : "log_only" | "queue_review" | "block_write"
#
# Frozen at import via tuple-of-tuples → MappingProxyType to prevent runtime mutation.
import types as _types_n1
_AUTHORITY_POLICY_SOURCE = {
    "curated": {
        "bypass_requires_corroboration": False,
        "allows_auto_write":             True,
        "max_age_days":                  90,
        "conflict_action":               "log_only",
    },
    "community_curated": {
        "bypass_requires_corroboration": False,
        "allows_auto_write":             True,
        "max_age_days":                  60,
        "conflict_action":               "log_only",
    },
    "crowd_contributed": {
        "bypass_requires_corroboration": True,    # Last.fm crowd tags need Phase1 corroboration
        "allows_auto_write":             True,
        "max_age_days":                  30,
        "conflict_action":               "queue_review",
    },
    "llm_generated": {
        "bypass_requires_corroboration": True,
        "allows_auto_write":             False,    # LLM never bypasses confidence gate
        "max_age_days":                  14,
        "conflict_action":               "block_write",
    },
    "contested": {
        "bypass_requires_corroboration": True,
        "allows_auto_write":             False,    # contested entries never auto-write
        "max_age_days":                  7,
        "conflict_action":               "queue_review",
    },
    "manual_review_required": {
        "bypass_requires_corroboration": True,
        "allows_auto_write":             False,
        "max_age_days":                  365,      # don't expire manual review entries
        "conflict_action":               "block_write",
    },
}
# Freeze: each value becomes immutable; top-level dict becomes read-only proxy.
AUTHORITY_POLICY = _types_n1.MappingProxyType({
    k: _types_n1.MappingProxyType(v) for k, v in _AUTHORITY_POLICY_SOURCE.items()
})


def get_authority_policy(authority: str) -> dict:
    """Safe lookup with fallback to most restrictive policy (llm_generated)."""
    return AUTHORITY_POLICY.get(authority.lower().strip(), AUTHORITY_POLICY["llm_generated"])


# =============================================================
# MK33 O1-T1: Policy versioning for decision reproducibility (CTO adversarial)
# =============================================================
# CTO: "MK35 changes community_curated.allows_auto_write to False — historical
# audit decisions become non-reproducible. You need policy_version, policy_snapshot,
# decision_trace."
# POLICY_VERSION is bumped whenever AUTHORITY_POLICY semantics change.
# Format: "mkNN.YYYY-MM-DD" — sortable, human-readable, links to release.
POLICY_VERSION = "mk33.2026-05-06"


def freeze_policy_snapshot() -> dict:
    """
    Return a serializable snapshot of the current authority policy + version.
    Stamped to audit_state once per run; allows future audits to replay decisions
    under the historical policy that made them.
    Returns: {"version": str, "policy": dict (deep copy), "stamped_at": int}
    """
    import time as _t_pol
    return {
        "version":     POLICY_VERSION,
        "policy":      {k: dict(v) for k, v in AUTHORITY_POLICY.items()},  # unfreeze for JSON
        "stamped_at":  int(_t_pol.time()),
    }


# ═══════════════════════════════════════════════════════════════════════════
# MK39 Phase III config flags (Council Forum 2026-05-10 §F/§G/§H)
# All default to safe-no-op so legacy behavior is preserved when operator
# has not opted in.
# ═══════════════════════════════════════════════════════════════════════════

# F — MusicBee Integration
# When True, walkman_sync Stage 6 routes through export_stager + musicbee_sync
# instead of the legacy ADB/COM transfer code. Default OFF for the rollout.
SYNC_VIA_MUSICBEE: bool = False

# Path to MusicBee.exe; auto-detected if blank. Honored by musicbee_sync.
MUSICBEE_EXECUTABLE: str = r"C:\Program Files (x86)\MusicBee\MusicBee.exe"

# Optional MusicBee device profile name. Empty = MusicBee's default device.
MUSICBEE_DEVICE_PROFILE: str = ""

# Where the export_stager produces the staging directory. Operator-tunable.
STAGING_ROOT: str = ""  # blank → derived as <MUSIC_DIR>/.staging at runtime

# G — Beets+AcoustID Fingerprinting
# Free key: https://acoustid.org/new-application . Empty disables the
# fingerprint fallback (graceful degradation per Council §G).
ACOUSTID_API_KEY: str = "HvOhmBkyMe"

# Path to the Chromaprint fpcalc binary. Empty = look up via PATH.
FPCALC_PATH: str = ""

# AcoustID match score threshold (0.0-1.0). Below this, the match is rejected.
ACOUSTID_SCORE_THRESHOLD: float = 0.85

# Where the fingerprint cache lives. Defaults to working directory.
ACOUSTID_CACHE_PATH: str = ".acoustid_cache.json"

# Task 11 (2026-07-18): wires mk39_phase_III_fallback.attempt_fingerprint_fallback
# (built, tested, and already registered in the MK38.0 trust ranking — see
# mk39_phase_III_canonical_sources.py — but never called from any production
# per-track resolution loop) into audit.py's per-track tag-resolution step.
# Fires only when artist/title are still blank after the full existing
# waterfall. Default OFF — first time this primitive is actually exercised
# live; opt in after reviewing `cli_runner.py preview-acoustid-corrections`.
ACOUSTID_AUTO_CORRECT_ENABLED: bool = True
# H — Picard-friendly staging
# When True, ensure_picard_compatible runs as a post-staging pass, normalizing
# ID3 tags to v2.3 + deduplicating APIC frames. Default OFF until validated
# against the operator's library.
PICARD_COMPAT_NORMALIZE: bool = False

# When True, walkman_sync detects externally-edited files (mtime > cache_mtime)
# and treats their on-disk tags as canonical with provenance "external_edit".
# Default OFF; operator opts in after they've used Picard at least once.
EXTERNAL_EDIT_REINGEST: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# MK39.1 C2 — Unknown-as-Terminal-State Policy (Council 2026-05-10 §4 P0+)
# ═══════════════════════════════════════════════════════════════════════════
# "Plug the bottom of the funnel first." The structural cure for the
# default-write contamination pattern (Phase I band-aid'd "Abstract Hip-Hop"
# on every unknown track). Default ON per Council mandate.
# ═══════════════════════════════════════════════════════════════════════════

# When True, AI-inferred genres that are NOT in the canonical taxonomy and
# NOT in the alias map are REJECTED (returned as blank ""). The track then
# routes to .needs_review.json for operator review (Picard / MP3Tag /
# quarantine_review --accept).
#
# When False (legacy permissive mode), the historical accept-with-warning
# behavior is restored — useful only if an operator has already done a full
# library audit and wants to bypass quarantine churn.
STRICT_AI_GENRE_VALIDATION: bool = True


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG CONSOLIDATION INDEX (MK39.1 C1.1 — operator-facing reference)
# ═══════════════════════════════════════════════════════════════════════════
# As of MK39.1 the configuration surface is grouped into these sections.
# Search this file for the section header to find the relevant flags.
#
#   POLICY_VERSION + AUTHORITY_POLICY    — provenance authority matrix (top)
#   API keys + endpoints                  — Spotify, MusicBrainz, etc.
#   Provider waterfall + circuit breaker — download + AI providers
#   Genre pipeline                        — taxonomy, AI gating, reeval
#   MK35-MK37 trust + audit               — quorum, fingerprint, capabilities
#   MK38.0/MK38.1 semantic freeze         — write barrier + dominance
#   MK39 Phase I containment              — operator override file, hard blocks
#   MK39 Phase II                          — log noise, needs-review CLI
#   MK39 Phase III F (MusicBee)           — SYNC_VIA_MUSICBEE, etc.
#   MK39 Phase III G (AcoustID)           — ACOUSTID_API_KEY, etc.
#   MK39 Phase III H (Picard staging)     — PICARD_COMPAT_NORMALIZE, etc.
#   MK39.1 C2 (this section)              — STRICT_AI_GENRE_VALIDATION
#
# All Phase III flags default OFF (opt-in). The C2 strict flag defaults ON
# per Council mandate ("plug the bottom of the funnel FIRST").
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Spiral Pipeline (MK11 Spiral Redesign)
# ═══════esalb═════════════════════════════════════════════════════════
SPIRAL_ENABLED: bool = True

# 2026-09-03 — when True (default), the GUI's START SYNC / DOWNLOAD ONLY /
# AUDIT ONLY / REORGANISE buttons run the pipeline as a `python cli_runner.py
# <mode>` child process (main.py::_spiral_run_subprocess), streaming its stdout
# into the log pane, instead of running SpiralOrchestrator inside a GUI
# thread. A 6.5h in-thread run leaked GDI/USER handles into the six figures
# (COM/MTP + Tk churn) and ResourceMonitor aborted it; the same work as
# cli_runner ran clean. The child exits and frees everything. Set False to
# use the legacy in-process path.
GUI_SUBPROCESS_PIPELINE: bool = True

# PlayerMediaProvider DB verification after each revolution's writes.
SPIRAL_DB_VERIFY_TIMEOUT: int      = 90     # seconds to wait for scan completion
SPIRAL_DB_VERIFY_MIN_RATIO: float  = 0.90   # 90% of expected tracks must appear

# ADB content query URI for the Sony Walkman Music app PlayerMediaProvider.
WALKMAN_CONTENT_PROVIDER_URI: str = (
    "content://com.sony.nwz.walkmanplayer.provider/audio"
)

# Genre waterfall depth per revolution (number of API tiers active).
# Full waterfall order: override_dict → cache → Last.fm → TIDAL →
#                       Discogs → MusicBrainz → genre_graph → AI
GENRE_WATERFALL_DEPTH: int = 8       # overridden per-revolution by orchestrator
AI_GENRE_ENABLED: bool     = True    # overridden to False for Rev 1-2 by orchestrator

# Spiral gate thresholds — minimum success fractions before advancing.
SPIRAL_GATE_TRANSFER_OK_MIN: float = 0.80
SPIRAL_GATE_AUDIT_OK_MIN:    float = 0.90

# Runtime slot for the spiral genre graph (set by orchestrator before Rev 3).
# Not a user-editable flag — managed entirely by SpiralOrchestrator.
_SPIRAL_GENRE_GRAPH = None

# streamrip album download timeout (seconds).  Full albums on hi-res can
# take several minutes depending on connection speed.
STREAMRIP_ALBUM_TIMEOUT_S: int = 600

# 2026-09-02 (user direction) — monochrome is the PRIMARY music provider for
# both singles and whole albums.
#   PRIMARY_DOWNLOAD_PROVIDER: forced to the front of the per-track waterfall
#     regardless of genre-tier ordering. Set "" to disable the override.
#   MONOCHROME_ALBUM_FIRST: _download_full_album resolves the full tracklist
#     from the monochrome /album/ proxy (no tidalapi / TIDAL session needed),
#     then downloads each track through the normal waterfall. Falls back to
#     tidalapi -> streamrip only if the monochrome resolve returns nothing.
# 2026-09-05: monochrome needs Cloudflare Turnstile (infeasible from a script),
# yt-dlp is YouTube-bot-blocked. streamrip+Deezer (ARL set) is the working path,
# so it's the primary until one of the others recovers.
PRIMARY_DOWNLOAD_PROVIDER: str = "streamrip"
MONOCHROME_ALBUM_FIRST: bool = False
MONOCHROME_ALBUM_RESOLVE_TIMEOUT_S: int = 20

# 2026-09-03 — YouTube 403s anonymous yt-dlp audio pulls. Give the ytdlp
# provider a signed-in session so it works again as the lossy last-resort
# fill for the recovery run.
#   YTDLP_COOKIE_FILE: path to a Netscape-format cookies.txt (takes priority).
#   YTDLP_COOKIES_FROM_BROWSER: browser name yt-dlp reads cookies from
#     ("chrome", "edge", "firefox", "brave", ...). Used only if no cookie file.
# Leave both "" to keep the current anonymous behaviour.
# 2026-09-05: --cookies-from-browser chrome fails on this machine ("Could not
# copy Chrome cookie database" — DB locked while Chrome runs, plus Chrome's
# App-Bound cookie encryption). Use an exported cookies.txt instead: point
# YTDLP_COOKIE_FILE at a Netscape-format file from a "Get cookies.txt" browser
# extension (youtube.com). That works while Chrome stays open.
YTDLP_COOKIE_FILE: str = ""
YTDLP_COOKIES_FROM_BROWSER: str = ""


