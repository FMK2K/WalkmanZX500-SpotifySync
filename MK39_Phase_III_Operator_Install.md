# MK39 Phase III — Operator Install Guide

**Status:** Phase III F/G/H implemented and tested. All 9 subtickets shipped.
**Combined regression:** 234/234 (199 prior + 35 new).
**Default behavior:** Phase III is OPT-IN. Set the relevant config flag below to enable.

---

## What Phase III adds

| Target | What it does | Default |
|---|---|---|
| **F** MusicBee Integration | Replaces ADB/COM device transfer with MusicBee's mature sync engine | OFF (legacy ADB/COM still active) |
| **G** Beets+AcoustID fingerprint | When Phase I leaves identity blank, fingerprints the audio and looks up MusicBrainz via AcoustID | OFF (no API key by default) |
| **H** Picard-friendly staging | Atomic-write tag normalization + ID3v2.3 + APIC dedup; re-ingests external Picard edits | OFF |

---

## Installing the binaries

### Linux (sandbox, dev, headless server)

```bash
# G — Chromaprint (fingerprint generation)
sudo apt install libchromaprint-tools         # provides fpcalc

# G — Python AcoustID client
pip install pyacoustid                         # already a dep

# F — MusicBee NOT AVAILABLE on Linux
#     (MusicBee is .NET Framework, Windows-only.
#      Use the legacy ADB/COM path on Linux dev machines.)

# H — Mutagen (already a project dep)
pip install mutagen
```

### macOS

```bash
brew install chromaprint                       # provides fpcalc
pip install pyacoustid mutagen
# F — MusicBee not available on macOS either
```

### Windows (production operator setup)

```powershell
# F — Install MusicBee
# Download from https://www.getmusicbee.com (free, no account required)
# Install with default options. The platform auto-detects the .exe at:
#   C:\Program Files (x86)\MusicBee\MusicBee.exe
# Or set config.MUSICBEE_EXECUTABLE if installed elsewhere.

# G — Install Chromaprint (fpcalc)
# Download fpcalc.exe from https://acoustid.org/chromaprint
# Place in C:\Tools\fpcalc.exe (or any PATH location)
# Or set config.FPCALC_PATH = r"C:\Tools\fpcalc.exe"

# Python deps (if not already installed)
pip install pyacoustid mutagen
```

---

## Getting an AcoustID API key (free)

1. Register at https://acoustid.org/login (Google/GitHub/MusicBrainz login OK)
2. Go to https://acoustid.org/new-application
3. Create an application (any name, e.g. "WalkmanSync personal")
4. Copy the **API key**, NOT the application id
5. Edit `.env` or `config.py`:
   ```python
   ACOUSTID_API_KEY = "your-key-here"
   ```

Free tier limit: ~3 req/sec. The platform throttles automatically.

---

## Enabling each Phase III feature

### F — Route device sync through MusicBee

Edit `config.py`:
```python
SYNC_VIA_MUSICBEE = True
MUSICBEE_DEVICE_PROFILE = "ZX500"   # name of your MusicBee device profile, or "" for default
STAGING_ROOT = r"C:\WalkmanSync_Staging"   # MusicBee will sync from here
```

What happens at runtime:
- Stages 1-5 (download, enrich, tag) run as today
- Stage 6 routes through `export_stager.stage_tree(...)` → builds clean staging dir
- Stage 7 invokes MusicBee.exe /Sync via `musicbee_sync.run_sync(...)`
- Walkman receives files from MusicBee, not from our ADB/COM code

### G — Enable fingerprint fallback when Phase I returns blank

```python
ACOUSTID_API_KEY = "your-key"
# Optional tuning:
ACOUSTID_SCORE_THRESHOLD = 0.85    # raise for stricter matches
ACOUSTID_CACHE_PATH = ".acoustid_cache.json"
```

What happens at runtime:
- Phase I containment runs as today
- For files where Phase I leaves blank artist OR blank title AND audio is on disk:
  - `mk39_phase_III_fallback.attempt_fingerprint_fallback(...)` runs fpcalc
  - Result fed to AcoustID → MusicBrainz match
  - Match scored ≥ threshold → identity written with provenance `acoustic_fingerprint`
  - Cached by file SHA-256 — no re-fingerprinting on later runs
- Files that still don't resolve → written to `.needs_review.json` (TARGET-I, shipped Phase II)

### H — Picard-friendly staging + external-edit re-ingest

```python
PICARD_COMPAT_NORMALIZE = True   # post-stage pass: ID3v2.3 + APIC dedup
EXTERNAL_EDIT_REINGEST = True    # detect operator's Picard edits on next run
```

What happens at runtime:
- After staging dir is built (whether routed to MusicBee or not), `picard_compat.normalize_directory(...)` runs over it
- Operator can then open the staging folder in Picard, make corrections
- On next platform run, files with mtime > last cache-write are detected as externally edited; their on-disk tags become canonical with provenance `external_edit`

---

## Verification: smoke test the install

```bash
# G — verify Chromaprint
fpcalc --help          # should print usage
fpcalc /path/to/any/song.mp3   # should print DURATION + FINGERPRINT

# G — verify the integration
python3 -c "
import acoustid_fingerprint as af
fp = af.fingerprint_file('/path/to/any/song.mp3')
print(fp)
"

# F — Windows only — verify MusicBee path
python3 -c "
import musicbee_sync as mb
print('MusicBee available:', mb.is_available())
print('Path:', mb.musicbee_executable_path())
"

# H — verify Picard compat normalization
python3 -c "
from picard_compat import ensure_picard_compatible
print(ensure_picard_compatible('/path/to/any/song.mp3'))
"
```

---

## What still requires the next sprint

This delivery ships F/G/H as **integrations** with feature-detect graceful
degradation. The Council mandate to "archive ADB/COM as fallback module"
(F.3 deeper refactor) is sequenced as a NEXT sub-sprint — when an operator
has validated end-to-end MusicBee sync on at least one real Walkman, we can
flip the legacy ADB/COM path to a fallback-only mode. Until that operator
validation, ADB/COM remains the production-default path; F is opt-in.

Same for H.3 deeper integration: full external-edit cache invalidation + sticky
`external_authority` bit in the audit cache (rather than just at the
write-barrier permission level) is queued for the next sub-sprint when the
Picard workflow gets real operator usage feedback.

---

*— MK39 Phase III install guide. Locked 2026-05-10 with combined regression at 234/234.*
