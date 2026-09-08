"""
test_task8_lyrics.py — isolated verification for lyrics_fetch.py (Task 8)

Stage 2 of this project's verification ladder (py_compile -> offline mock
validation -> single-track live deploy). No network, no device required.
Generates two tiny real audio files (FLAC + MP3) via the vendored ffmpeg
binary so embed_plain_lyrics() exercises real mutagen tag I/O, not mocks.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

import config

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


def _make_silent(path: pathlib.Path):
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )


tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task8_lyrics_test_"))
flac_path = tmpdir / "test.flac"
mp3_path = tmpdir / "test.mp3"
_make_silent(flac_path)
_make_silent(mp3_path)

import lyrics_fetch as lf

# ─── 1. LYRICS_ENABLED=False short-circuits before any network call ────────
print("\n[1] LYRICS_ENABLED=False short-circuit")
config.LYRICS_ENABLED = False
with mock.patch("requests.get") as m_get:
    result = lf.fetch_synced_lyrics("Some Title", "Some Artist")
    _check("returns None when disabled", result is None)
    _check("requests.get never called", not m_get.called)

config.LYRICS_ENABLED = True

# ─── 2. Miss (LRCLIB returns no lyrics) ─────────────────────────────────────
print("\n[2] LRCLIB miss")
class _RespMiss:
    status_code = 200
    def json(self):
        return {"syncedLyrics": None, "plainLyrics": None}

with mock.patch("requests.get", return_value=_RespMiss()):
    result = lf.fetch_synced_lyrics("Nonexistent Track", "Nobody")
    _check("returns None on miss", result is None)

# ─── 3. Hit — synced + plain lyrics ─────────────────────────────────────────
print("\n[3] LRCLIB hit — synced + plain")
_SYNCED = "[00:01.00]Hello line one\n[00:02.50]Hello line two"
_PLAIN = "Hello line one\nHello line two"

class _RespHit:
    status_code = 200
    def json(self):
        return {"syncedLyrics": _SYNCED, "plainLyrics": _PLAIN}

with mock.patch("requests.get", return_value=_RespHit()) as m_get:
    result = lf.fetch_synced_lyrics("Title", "Artist", "Album", duration_s=180)
    _check("hit returns a LyricsResult", result is not None)
    _check("synced matches", result.synced == _SYNCED)
    _check("plain matches", result.plain == _PLAIN)
    _check("has_any True", result.has_any)
    called_kwargs = m_get.call_args.kwargs
    _check("duration param passed through", called_kwargs["params"].get("duration") == 180)

# ─── 4. .lrc sidecar write is atomic and correct (FLAC path) ────────────────
print("\n[4] .lrc sidecar write")
ok = lf.write_lrc_sidecar(flac_path, result)
lrc_path = flac_path.with_suffix(".lrc")
_check("write_lrc_sidecar returned True", ok)
_check(".lrc file exists", lrc_path.exists())
_check(".lrc content matches synced block", lrc_path.read_text(encoding="utf-8") == _SYNCED)
_check("no leftover .tmp files in dir", not list(tmpdir.glob("*.tmp")))

# ─── 5. Plain-text tag embed — FLAC ─────────────────────────────────────────
print("\n[5] Plain-text tag embed (FLAC)")
ok = lf.embed_plain_lyrics(flac_path, result, is_mp3=False)
_check("embed_plain_lyrics returned True (FLAC)", ok)
from mutagen.flac import FLAC
audio = FLAC(flac_path)
_check("LYRICS tag present and correct", audio.get("LYRICS", [""])[0] == _PLAIN)

# ─── 6. Plain-text tag embed — MP3 (USLT) ───────────────────────────────────
print("\n[6] Plain-text tag embed (MP3 USLT)")
ok = lf.embed_plain_lyrics(mp3_path, result, is_mp3=True)
_check("embed_plain_lyrics returned True (MP3)", ok)
from mutagen.id3 import ID3
id3 = ID3(mp3_path)
uslt_frames = id3.getall("USLT")
_check("exactly one USLT frame (delall before add)", len(uslt_frames) == 1)
_check("USLT text matches plain lyrics", uslt_frames[0].text == _PLAIN)
_check("USLT lang is eng", uslt_frames[0].lang == "eng")

# ─── 7. has_lyrics() detects the sidecar + tags just written ────────────────
print("\n[7] has_lyrics() avoids re-fetch")
_check("has_lyrics True for FLAC (sidecar+tag present)", lf.has_lyrics(flac_path, is_mp3=False))
_check("has_lyrics True for MP3 (USLT present)", lf.has_lyrics(mp3_path, is_mp3=True))

# ─── 8. Miss must never touch the file (no .lrc created, no tag written) ───
print("\n[8] Miss path never touches files")
fresh_flac = tmpdir / "fresh.flac"
shutil.copy2(flac_path, fresh_flac)
# strip lyrics tag first to get a clean baseline
_clean = FLAC(fresh_flac)
if "LYRICS" in _clean:
    del _clean["LYRICS"]
    _clean.save()
_check("no .lrc for fresh file before miss-path call", not fresh_flac.with_suffix(".lrc").exists())
ok_lrc = lf.write_lrc_sidecar(fresh_flac, None)
_check("write_lrc_sidecar(None) returns False", ok_lrc is False)
_check("still no .lrc after None input", not fresh_flac.with_suffix(".lrc").exists())

config.LYRICS_ENABLED = False
shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*60}\nTask 8 (lyrics_fetch.py): {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
