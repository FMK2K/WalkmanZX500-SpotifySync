"""
test_ai.py  —  Standalone AI provider connectivity tester
==========================================================
Run this to diagnose why AI genre/metadata inference is failing.

Usage:
    python test_ai.py              # tests all configured providers
    python test_ai.py --groq       # tests Groq only
    python test_ai.py --gemini     # tests Gemini only

Output tells you exactly which key is missing or misconfigured.

NOTE: This is a CONNECTIVITY DIAGNOSTIC, not a regression test.  It
always exits 0 — its job is to print what's working and what isn't.
In sandboxed/offline environments (e.g. CI without API allowlist),
network errors are expected and reported as 'no network' rather than
shown as test failures.
"""

import sys
import os
import json
import socket
import urllib.request
import urllib.error
import pathlib

# Load .env from same directory
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=pathlib.Path(__file__).parent / ".env", override=False)
    print("✓ .env loaded")
except ImportError:
    print("! python-dotenv not installed — reading from environment only")

GROQ_KEY   = os.environ.get("GROQ_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")


def _can_reach(host: str, port: int = 443, timeout: float = 3.0) -> bool:
    """Return True if a TCP connection to host:port succeeds.  Used to
    distinguish 'API key is broken' (we got an HTTP response, just not
    the one we wanted) from 'no network access at all' (sandboxed CI,
    no internet).  In the latter case the diagnostic should say so
    plainly rather than flag every model as a failure."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Key presence check ──────────────────────────────────────────────────────

print("\n── API Keys ──────────────────────────────────────────────────────")
if GROQ_KEY:
    masked = GROQ_KEY[:8] + "..." + GROQ_KEY[-4:]
    print(f"  GROQ_API_KEY   : {masked}  ({len(GROQ_KEY)} chars)")
else:
    print("  GROQ_API_KEY   : ✗ NOT SET")

if GEMINI_KEY:
    masked = GEMINI_KEY[:8] + "..." + GEMINI_KEY[-4:]
    print(f"  GEMINI_API_KEY : {masked}  ({len(GEMINI_KEY)} chars)")
else:
    print("  GEMINI_API_KEY : ✗ NOT SET")

# ── Groq test ───────────────────────────────────────────────────────────────

def test_groq(key: str) -> dict:
    if not key:
        return {"ok": False, "error": "GROQ_API_KEY not set"}
    print("\n── Groq connectivity test ────────────────────────────────────────")
    if not _can_reach("api.groq.com"):
        print("  ⏭  api.groq.com unreachable — running in sandboxed/offline env.")
        print("     (This is expected on CI without API allowlist; not a test failure.)")
        return {"ok": False, "skipped": True, "error": "no network"}
    # 2026-08-22: all three prior models decommissioned by Groq; see
    # ai_helper.py's _GROQ_MODELS comment for the live model-list survey.
    models = {
        "groq/compound":      "primary",
        "groq/compound-mini": "fallback / last resort",
    }
    results = {}
    for model, role in models.items():
        try:
            payload = {
                "model": model,
                "max_tokens": 30,
                "temperature": 0.0,
                "messages": [
                    {"role": "user",
                     "content": 'What genre is Travis Scott? Reply ONLY with JSON: {"genre": "string"}'}
                ],
                "response_format": {"type": "json_object"},
            }
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type":    "application/json",
                    "Authorization":   f"Bearer {key}",
                    # Cloudflare 1010 blocks bare Python urllib — use realistic browser headers
                    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept":          "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Origin":          "https://console.groq.com",
                    "Referer":         "https://console.groq.com/",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                genre = parsed.get("genre", "?")
                print(f"  ✓ {model} ({role}): genre={genre!r}")
                results[model] = {"ok": True, "genre": genre}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")[:200]
            # Detect sandboxed-egress proxies that return 403 with "allowlist"
            # in the body — that's not an API failure, it's the test env
            # blocking outbound traffic.
            if e.code == 403 and "allowlist" in body.lower():
                print(f"  ⏭  {model} ({role}): sandboxed env (egress proxy blocks api.groq.com)")
                results[model] = {"ok": False, "skipped": True}
                continue
            print(f"  ✗ {model} ({role}): HTTP {e.code} — {body}")
            results[model] = {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            print(f"  ✗ {model} ({role}): {type(e).__name__}: {e}")
            results[model] = {"ok": False, "error": str(e)}
    return results

# ── Gemini test ─────────────────────────────────────────────────────────────

def test_gemini(key: str) -> dict:
    if not key:
        return {"ok": False, "error": "GEMINI_API_KEY not set"}
    print("\n── Gemini connectivity test ──────────────────────────────────────")
    if not _can_reach("generativelanguage.googleapis.com"):
        print("  ⏭  generativelanguage.googleapis.com unreachable — running in sandboxed/offline env.")
        print("     (This is expected on CI without API allowlist; not a test failure.)")
        return {"ok": False, "skipped": True, "error": "no network"}
    try:
        # 2026-08-22: gemini-2.0-flash decommissioned (404 → use
        # gemini-3.6-flash). The replacement thinks by default and consumes
        # maxOutputTokens on invisible reasoning before any visible answer —
        # 30 was fine for the old model but leaves no room here. See
        # ai_helper.py's _gemini_direct() for the full writeup.
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-3.6-flash:generateContent")
        payload = {
            "contents": [{"parts": [{"text":
                'What genre is Travis Scott? Reply ONLY with JSON: {"genre": "string"}'}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 256,
                "responseMimeType": "application/json",
            },
        }
        req = urllib.request.Request(
            f"{url}?key={key}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            genre = parsed.get("genre", "?")
            print(f"  ✓ gemini-3.6-flash: genre={genre!r}")
            return {"ok": True, "genre": genre}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        if e.code == 403 and "allowlist" in body.lower():
            print("  ⏭  Gemini: sandboxed env (egress proxy blocks generativelanguage.googleapis.com)")
            return {"ok": False, "skipped": True}
        print(f"  ✗ Gemini: HTTP {e.code} — {body}")
        if e.code == 429:
            print("    → Rate limited. This is normal for heavy usage.")
        elif e.code == 401:
            print("    → Invalid API key. Check GEMINI_API_KEY in .env")
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        print(f"  ✗ Gemini: {type(e).__name__}: {e}")
        return {"ok": False, "error": str(e)}

# ── Run tests ────────────────────────────────────────────────────────────────

only_groq   = "--groq"   in sys.argv
only_gemini = "--gemini" in sys.argv

if not only_gemini:
    test_groq(GROQ_KEY)

if not only_groq:
    test_gemini(GEMINI_KEY)

print("\n── Summary ───────────────────────────────────────────────────────")
if not GROQ_KEY and not GEMINI_KEY:
    print("  ✗ No API keys set. Add to .env:")
    print("    GROQ_API_KEY=gsk_...   (free at https://console.groq.com/keys)")
    print("    GEMINI_API_KEY=...     (free at https://aistudio.google.com/app/apikey)")
elif not GROQ_KEY:
    print("  ! GROQ_API_KEY missing — add it for rate-limit resilience")
    print("    Free key: https://console.groq.com/keys")
elif not GEMINI_KEY:
    print("  ! GEMINI_API_KEY missing — Groq will handle all AI inference")
else:
    print("  ✓ Both keys configured")
print()
