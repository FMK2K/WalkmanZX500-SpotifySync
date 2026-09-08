"""
test_acoustid_cache_fix.py — AcoustID negative-cache-on-error fix (2026-07-24)
================================================================================
Found live: config.ACOUSTID_API_KEY was invalid, so every real lookup failed
with HTTP 400 "invalid API key". lookup_fingerprint() silently swallowed this
(log.debug, return None) and resolve_identity() then permanently cached the
result as {"status": "no_match"} — indistinguishable from a genuine "this
track isn't in MusicBrainz" result. This meant a bad key (or any transient
network failure) silently and permanently masked every file it ever touched,
even after the underlying problem was fixed.

Fix: lookup_fingerprint() now lets HTTP/transport errors propagate;
resolve_identity() catches them separately from a completed-but-empty lookup
and does NOT write them to the cache, so the file is retried on the next run
instead of staying falsely negative-cached forever.

Offline only — injects a fake http_get, no real network/fpcalc/API key needed.
"""
import dataclasses
import json
import pathlib
import sys
import tempfile
from unittest import mock

import acoustid_fingerprint as af

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


_tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="acoustid_cache_test_"))
_audio = _tmpdir / "track.flac"
_audio.write_bytes(b"not real audio, just needs to exist for is_file()")

_fake_fp = af.FingerprintResult(duration_sec=200.0, fingerprint="AQADfake")


# ─── 1. HTTP error during lookup -> not cached, resolve_identity returns None,
#        never raises ───────────────────────────────────────────────────────
print("\n[1] Lookup HTTP error (e.g. invalid API key) is not cached")

_cache1 = _tmpdir / "cache1.json"


def _boom_http_get(url, *, timeout):
    raise RuntimeError("HTTP Error 400: Bad Request")


with mock.patch("acoustid_fingerprint.fingerprint_file", return_value=_fake_fp):
    result = af.resolve_identity(
        _audio, api_key="bad-key", cache_path=_cache1, http_get=_boom_http_get,
    )
_check("resolve_identity never raises", True)  # if we got here, it didn't raise
_check("returns None on lookup error", result is None)
_check("cache file was NOT created (nothing to cache)", not _cache1.exists())


# ─── 2. Genuine no-match (successful call, no candidate above threshold) IS
#        cached as no_match ─────────────────────────────────────────────────
print("\n[2] Genuine no-match (successful API call, no confident candidate) IS cached")

_cache2 = _tmpdir / "cache2.json"


def _empty_results_http_get(url, *, timeout):
    return {"status": "ok", "results": []}


with mock.patch("acoustid_fingerprint.fingerprint_file", return_value=_fake_fp):
    result2 = af.resolve_identity(
        _audio, api_key="fine-key", cache_path=_cache2, http_get=_empty_results_http_get,
    )
_check("returns None for genuine no-match", result2 is None)
_check("cache file WAS created for a completed lookup", _cache2.exists())
if _cache2.exists():
    cache_data = json.loads(_cache2.read_text(encoding="utf-8"))
    _check("cached entry has status=no_match", any(v.get("status") == "no_match" for v in cache_data.values()))


# ─── 3. After an error (not cached), a SUBSEQUENT successful call for the
#        SAME file still gets a real chance to resolve — proves the fix
#        actually restores retry-ability rather than just suppressing the bug
# ────────────────────────────────────────────────────────────────────────────
print("\n[3] File is retried (not stuck) after a prior lookup error")

_cache3 = _tmpdir / "cache3.json"

with mock.patch("acoustid_fingerprint.fingerprint_file", return_value=_fake_fp):
    # First call: errors (e.g. bad key) — must not poison the cache.
    r1 = af.resolve_identity(_audio, api_key="bad-key", cache_path=_cache3, http_get=_boom_http_get)
    _check("first call (error) returns None", r1 is None)

    # Second call: key fixed, API now returns a confident match.
    def _match_http_get(url, *, timeout):
        return {
            "status": "ok",
            "results": [{
                "score": 0.95,
                "recordings": [{
                    "id": "abc-123",
                    "title": "Down",
                    "duration": 200,
                    "artists": [{"name": "Run The Jewels"}],
                    "releasegroups": [{"title": "Run The Jewels 3", "first_release_date": "2016-12-24"}],
                }],
            }],
        }
    r2 = af.resolve_identity(_audio, api_key="good-key", cache_path=_cache3, http_get=_match_http_get)
_check("second call (after fix) resolves successfully — file was NOT stuck", r2 is not None)
_check("resolved artist correct", r2 is not None and r2.artist == "Run The Jewels")


import shutil
shutil.rmtree(_tmpdir, ignore_errors=True)

print(f"\n{'='*60}\nAcoustID negative-cache-on-error fix: {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
