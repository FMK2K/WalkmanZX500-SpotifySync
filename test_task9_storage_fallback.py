"""
test_task9_storage_fallback.py — isolated verification for Task 9
(storage-aware FLAC/MP3 fallback in downloader.py)

Stage 2 of this project's verification ladder (py_compile -> offline mock
validation -> single-track live deploy). No network, no device required.
Generates a tiny real FLAC via the vendored ffmpeg binary so the ffmpeg
downgrade path is exercised for real, not mocked. Mocks free_space_check
(device free-space query) and spotify_local_data (liked/playlist lookup).
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

import config
import downloader as dl

FFMPEG = str(pathlib.Path(__file__).parent / "tools" / "ffmpeg-8.1.1-essentials_build" / "bin" / "ffmpeg.exe")

passed = 0
failed = 0


def _check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


def _make_silent_flac(path: pathlib.Path):
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )


def _reset_caches():
    dl._STORAGE_FALLBACK_STATE["free_gb"] = None
    dl._STORAGE_FALLBACK_STATE["checked_at"] = 0.0
    dl._PLAYLIST_PROTECTED_URI_CACHE = None


tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task9_storage_test_"))

config.FLAC_FIRST_ENABLED = True
config.FLAC_FALLBACK_MIN_FREE_GB = 5.0
config.MIN_FREE_SPACE_GB = 0.5
config.FLAC_FORCE_FOR_LIKED_PLAYLISTS = True

# ─── 1. Plenty of space — no downgrade ──────────────────────────────────────
print("\n[1] Plenty of space — FLAC kept unchanged")
_reset_caches()
f1 = tmpdir / "plenty.flac"
_make_silent_flac(f1)
with mock.patch("free_space_check.get_device_free_bytes", return_value=int(50 * 1024**3)):
    result = dl.apply_storage_aware_format_fallback([f1], {"id": "trackA", "is_liked": False})
_check("file list unchanged", result == [f1])
_check("original FLAC still exists", f1.exists())
_check("suffix still .flac", result[0].suffix.lower() == ".flac")

# ─── 2. Low space, non-liked/non-playlist track — downgrades to MP3 ────────
print("\n[2] Low space + unprotected track — downgrades to MP3 320kbps")
_reset_caches()
f2 = tmpdir / "lowspace.flac"
_make_silent_flac(f2)
with mock.patch("free_space_check.get_device_free_bytes", return_value=int(2 * 1024**3)), \
     mock.patch("spotify_local_data.get_liked_uris", return_value=set()), \
     mock.patch("spotify_local_data.get_all_playlists", return_value={}):
    result = dl.apply_storage_aware_format_fallback([f2], {"id": "trackB", "is_liked": False})
_check("returned exactly one file", len(result) == 1)
_check("downgraded to .mp3", result[0].suffix.lower() == ".mp3")
_check("original .flac deleted", not f2.exists())
_check("mp3 file actually exists and is non-trivial", result[0].exists() and result[0].stat().st_size > 4096)

# ─── 3. Low space, Liked Song — stays FLAC ──────────────────────────────────
print("\n[3] Low space + Liked Song — override keeps FLAC")
_reset_caches()
f3 = tmpdir / "liked.flac"
_make_silent_flac(f3)
with mock.patch("free_space_check.get_device_free_bytes", return_value=int(2 * 1024**3)):
    result = dl.apply_storage_aware_format_fallback([f3], {"id": "trackC", "is_liked": True})
_check("file list unchanged (liked override)", result == [f3])
_check("FLAC still exists", f3.exists())

# ─── 4. Low space, playlist member (not liked) — stays FLAC ─────────────────
print("\n[4] Low space + playlist member — override keeps FLAC")
_reset_caches()
f4 = tmpdir / "playlisted.flac"
_make_silent_flac(f4)
with mock.patch("free_space_check.get_device_free_bytes", return_value=int(2 * 1024**3)), \
     mock.patch("spotify_local_data.get_liked_uris", return_value=set()), \
     mock.patch("spotify_local_data.get_all_playlists",
                 return_value={"My Playlist": ["spotify:track:trackD"]}):
    result = dl.apply_storage_aware_format_fallback([f4], {"id": "trackD", "is_liked": False})
_check("file list unchanged (playlist override)", result == [f4])
_check("FLAC still exists", f4.exists())

# ─── 5. FLAC_FIRST_ENABLED=False — no-op regardless of space ───────────────
print("\n[5] FLAC_FIRST_ENABLED=False — no-op")
_reset_caches()
f5 = tmpdir / "disabled.flac"
_make_silent_flac(f5)
config.FLAC_FIRST_ENABLED = False
with mock.patch("free_space_check.get_device_free_bytes", return_value=int(0 * 1024**3)):
    result = dl.apply_storage_aware_format_fallback([f5], {"id": "trackE", "is_liked": False})
_check("file list unchanged when FLAC_FIRST_ENABLED=False", result == [f5])
config.FLAC_FIRST_ENABLED = True

# ─── 6. Already-compressed input — unaffected ───────────────────────────────
print("\n[6] Already-compressed (.mp3) input is untouched")
_reset_caches()
f6 = tmpdir / "already.mp3"
f6.write_bytes(b"\x00" * 100)  # not a real mp3, but suffix check short-circuits before any decode
with mock.patch("free_space_check.get_device_free_bytes", return_value=int(0 * 1024**3)) as m_fs:
    result = dl.apply_storage_aware_format_fallback([f6], {"id": "trackF", "is_liked": False})
_check("file list unchanged for non-lossless input", result == [f6])
_check("free-space check not even consulted for non-FLAC input", not m_fs.called)

# ─── 7. Unknown free space (None) — don't guess, keep FLAC ─────────────────
print("\n[7] Unknown free space — keep FLAC (fail safe)")
_reset_caches()
f7 = tmpdir / "unknown.flac"
_make_silent_flac(f7)
with mock.patch("free_space_check.get_device_free_bytes", return_value=None):
    result = dl.apply_storage_aware_format_fallback([f7], {"id": "trackG", "is_liked": False})
_check("file list unchanged when free space is unknown", result == [f7])
_check("FLAC still exists", f7.exists())

# ─── 8. Caching — second call within the recheck window skips a re-query ───
print("\n[8] Free-space reading is cached (avoids hammering ADB)")
_reset_caches()
f8a = tmpdir / "cache_a.flac"
f8b = tmpdir / "cache_b.flac"
_make_silent_flac(f8a)
_make_silent_flac(f8b)
with mock.patch("free_space_check.get_device_free_bytes", return_value=int(50 * 1024**3)) as m_fs:
    dl.apply_storage_aware_format_fallback([f8a], {"id": "trackH1", "is_liked": False})
    dl.apply_storage_aware_format_fallback([f8b], {"id": "trackH2", "is_liked": False})
_check("second call reused the cached reading (called once, not twice)", m_fs.call_count == 1)

_reset_caches()
config.LYRICS_ENABLED = False if hasattr(config, "LYRICS_ENABLED") else None
shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*60}\nTask 9 (storage-aware FLAC/MP3 fallback): {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
