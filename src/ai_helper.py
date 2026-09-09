"""
ai_helper.py  —  Centralised AI metadata inference
====================================================
Provider waterfall
------------------
1. Groq key 1: llama-3.3-70b-versatile  (free tier, primary)
2. Groq key 1: llama-3.1-70b-versatile  (free tier, fallback)
3. Groq key 1: llama-3.1-8b-instant     (free tier, last resort)
4. Groq key 2: llama-3.3-70b / llama-3.1-8b  (NEW_GROQ_API_KEY, independent backoff)
5. Gemini direct              (your own key — most accurate)
6. Claude Sonnet 4.6          (ANTHROPIC_API_KEY)
7. DeepSeek: deepseek-chat    (DEEPSEEK_API_KEY)
8. GPT-4o                     (OPENAI_API_KEY)

Each provider has its OWN independent backoff timer so a Gemini 429
does not stall the Groq providers and vice-versa.  Groq's free tier
has a generous RPM limit (~30 req/min per model) and sub-second latency,
making it far more reliable as a fallback than any routing proxy layer.

GROQ_API_KEY is free at: https://console.groq.com/keys

Also exports:
  infer_track_number()       — regex pre-pass -> AI fallback
  infer_artist_album_title() — unchanged Gemini path + Groq fallback
  call_ai()                  — single-prompt -> parsed JSON dict
  call_ai_batch()            — list of prompts -> list of results
  infer_genre()              — genre/year fallback for genre_client Phase 4
"""

from __future__ import annotations

import json
import os
import re
import time
import threading
import urllib.request
import urllib.error
import logging

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-provider backoff state
# ---------------------------------------------------------------------------
# MK8 Fix (LOG_17_2): raised from 30 → 3600 so auth-error 60s backoffs
# are not silently capped to 30s, and 429 backoffs can breathe properly.
_MAX_BACKOFF_S = 3600.0

_PROVIDERS: dict[str, dict] = {
    "gemini_direct":    {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    "groq_llama70b":    {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    "groq_mixtral":     {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    "groq_llama8b":     {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    # Secondary Groq key (NEW_GROQ_API_KEY) — independent backoff so rate-limit
    # on key 1 doesn't block key 2. Doubles effective free-tier RPM budget.
    "groq2_llama70b":   {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    "groq2_llama8b":    {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    # MK11 ISSUE-E: GPT-4o and Claude Sonnet — superior music knowledge per dev research.
    # Dev: "GPT-4o leads in comprehensive music understanding; Claude Sonnet strong in
    # music theory analysis." Both added as Phase 5 fallback after Groq exhaustion.
    "gpt4o":            {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    "claude_sonnet":    {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
    # MK39 Phase I-A1 (Council 2026-05-10 emergency directive): wire DeepSeek into
    # the AI waterfall. The .env contained DEEPSEEK_API_KEY but the codebase did not
    # use it — Council §2.1 called this "a massive missed opportunity." Inserted
    # between Groq exhaustion and the existing GPT-4o/Claude-Sonnet phases so it
    # provides a free-tier-quality fallback BEFORE escalating to paid endpoints.
    "deepseek":         {"until": 0.0, "lock": threading.Lock(), "auth_fails": 0},
}

# MK8 Fix (LOG_17_2): session-level circuit breaker for invalid API keys.
# When Groq returns HTTP 403 with error code 1010 (invalid/revoked key) the
# 60-second backoff loops forever, generating 186 identical WARNING lines in
# a single session.  After _AUTH_FAIL_THRESHOLD consecutive 401/403 responses
# we session-disable the provider — no further calls, one clear log message.
# Restart the application after fixing the key to re-enable.
_AUTH_FAIL_THRESHOLD = 3
_SESSION_DISABLED: set[str] = set()
_SESSION_DISABLED_LOCK = threading.Lock()


def _session_disable(name: str) -> None:
    """Permanently disable provider for this process session — key is broken."""
    with _SESSION_DISABLED_LOCK:
        if name not in _SESSION_DISABLED:
            _SESSION_DISABLED.add(name)
    log.warning(
        f"  [AI/{name}] SESSION DISABLED after {_AUTH_FAIL_THRESHOLD} consecutive "
        f"auth failures (401/403).  Fix your API key in .env and restart the app."
    )


def _record_auth_fail(name: str) -> None:
    """Count consecutive auth failures; session-disable after threshold."""
    with _PROVIDERS[name]["lock"]:
        _PROVIDERS[name]["auth_fails"] = _PROVIDERS[name].get("auth_fails", 0) + 1
        fails = _PROVIDERS[name]["auth_fails"]
    if fails >= _AUTH_FAIL_THRESHOLD:
        _session_disable(name)


# One outbound call at a time; 0.4s gap — Groq responds in <1s so we can
# afford a slightly shorter inter-call delay while staying well under RPM limits.
_CALL_LOCK  = threading.Lock()
_CALL_DELAY = 0.4


# ---------------------------------------------------------------------------
# Startup key check — warn immediately if fallback providers are unconfigured
# ---------------------------------------------------------------------------
def _check_keys() -> None:
    """
    Warn at import time if no AI keys are present.
    Claude (Anthropic) is the primary provider; Gemini/Groq are fallbacks.
    """
    anthropic = getattr(config, "ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    gemini    = getattr(config, "GEMINI_API_KEY",    "") or os.environ.get("GEMINI_API_KEY",    "")
    groq      = getattr(config, "GROQ_API_KEY",      "") or os.environ.get("GROQ_API_KEY",      "")

    if not anthropic and not gemini and not groq:
        log.error(
            "[AI] CRITICAL: No AI keys set (ANTHROPIC_API_KEY / GEMINI_API_KEY / GROQ_API_KEY). "
            "All AI inference will fail."
        )
        return

    if not groq:
        log.warning("[AI] GROQ_API_KEY not set — Groq (primary) unavailable; falling back to Gemini.")

    if groq:
        def _run_test():
            result = test_groq()
            if result["ok"]:
                log.info(
                    f"  [AI/Groq] Connected (fallback) — model={result['model']}"
                )
            else:
                log.warning(
                    f"  [AI/Groq] Connectivity test FAILED: {result['error']}"
                )
        import threading as _t
        _t.Thread(target=_run_test, daemon=True, name="groq-liveness-test").start()

# MK11 ISSUE-B: _check_keys() moved to AFTER test_groq() definition.
# The original module-level call was a forward reference — test_groq() was
# defined 157 lines later, causing NameError in the groq-liveness-test thread.
# Invocation is now at the bottom of the file, after all functions are defined.


def _is_backed_off(name: str) -> bool:
    # MK8: also block calls to session-disabled providers (invalid key)
    with _SESSION_DISABLED_LOCK:
        if name in _SESSION_DISABLED:
            return True
    return time.time() < _PROVIDERS[name]["until"]


def _set_backoff(name: str, seconds: float) -> None:
    secs = min(seconds, _MAX_BACKOFF_S)
    with _PROVIDERS[name]["lock"]:
        new_until = time.time() + secs
        if new_until > _PROVIDERS[name]["until"]:
            _PROVIDERS[name]["until"] = new_until
    log.debug(f"  [AI/{name}] Backoff set to {secs:.0f}s")


def _remaining_backoff(name: str) -> float:
    return max(0.0, _PROVIDERS[name]["until"] - time.time())


# ---------------------------------------------------------------------------
# Low-level provider calls
# ---------------------------------------------------------------------------

def _gemini_direct(prompt: str, max_tokens: int, temperature: float) -> dict | None:
    """Gemini via your own API key. Identical to original call_ai Gemini block."""
    gemini_key = getattr(config, "GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
    if not gemini_key:
        return None
    if _is_backed_off("gemini_direct"):
        log.debug(
            f"  [AI/Gemini] Still in backoff "
            f"({_remaining_backoff('gemini_direct'):.0f}s remaining)"
        )
        return None
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.6-flash:generateContent"
        )
        # 2026-08-22: gemini-2.0-flash was decommissioned (404, "use
        # models/gemini-3.6-flash"). The replacement thinks by default and
        # consumes maxOutputTokens for invisible reasoning before any visible
        # answer — confirmed live that small caller budgets (this project
        # passes as little as 48-128 for short classifications) get entirely
        # consumed by thinking, returning empty content (finishReason=
        # MAX_TOKENS). Also confirmed live that thinkingConfig.thinkingBudget
        # is NOT a reliable hard cap here (requesting budget=30-50 still
        # measured 111-122 actual thinking tokens) — the only combination
        # that reliably completed in testing was a generous maxOutputTokens
        # floor with no thinkingConfig override at all. Floor Gemini's
        # request specifically rather than raising every caller's max_tokens
        # (which also governs the non-thinking Groq/Claude/GPT-4o providers).
        gemini_max_tokens = max(max_tokens, 256)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": gemini_max_tokens,
                "responseMimeType": "application/json",
            },
        }
        req = urllib.request.Request(
            f"{url}?key={gemini_key}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # MK14 FIX-C: Exponential backoff instead of flat 10s.
            # Log showed 80×10s = 800s wasted. Exp: 10→20→40→60s cap.
            # Import the Gemini streak counter from genre_client for exp scaling.
            try:
                import genre_client as _gc
                _streak = getattr(_gc, "_GEMINI_429_STREAK", 0)
                _gc._gemini_circuit_record(429)
            except Exception:
                _streak = 0
            _g_backoff = min(10.0 * (2 ** min(_streak, 3)), 60.0)
            log.warning(
                f"  [AI/Gemini] 429 — backing off {_g_backoff:.0f}s "
                f"(streak={_streak}), trying next provider"
            )
            _set_backoff("gemini_direct", _g_backoff)
        else:
            log.debug(f"  [AI/Gemini] HTTP {e.code}")
    except Exception as e:
        log.debug(f"  [AI/Gemini] call failed: {e}")
    return None


_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# Groq free-tier model names.
# 2026-08-22: llama-3.3-70b-versatile, gemma2-9b-it, and llama-3.1-8b-instant
# were ALL decommissioned (confirmed live: none appear in /v1/models anymore).
# Queried the current model list live and tested each general-purpose
# candidate against this project's exact json_object-mode call shape —
# openai/gpt-oss-* and qwen/qwen3.6-27b all fail with json_validate_failed
# under strict JSON mode; groq/compound and groq/compound-mini are the only
# two that reliably return valid JSON. compound is slower/agentic (~7.7s,
# tool-use capable) so it's the quality tier; compound-mini is fast (~1.1s)
# so it covers both the "reliable fallback" and "fast last resort" tiers —
# there's no third distinct tier available right now.
_GROQ_MODELS = {
    "groq_llama70b":  "groq/compound",
    "groq_mixtral":   "groq/compound-mini",
    "groq_llama8b":   "groq/compound-mini",
    "groq2_llama70b": "groq/compound",        # secondary key slot
    "groq2_llama8b":  "groq/compound-mini",
}


def _groq_call(
    provider_key: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> dict | None:
    """Generic Groq chat completion with per-provider backoff.

    Groq uses the OpenAI-compatible chat completions API.
    JSON mode is requested via response_format; Groq honours this reliably.
    Free-tier limits: ~30 req/min per model, 6000 tokens/min per model.
    """
    if provider_key.startswith("groq2_"):
        groq_key = getattr(config, "NEW_GROQ_API_KEY", os.environ.get("NEW_GROQ_API_KEY", ""))
    else:
        groq_key = getattr(config, "GROQ_API_KEY", os.environ.get("GROQ_API_KEY", ""))
    if not groq_key:
        # Only warn once — for the primary key slot; groq2_ absence is expected
        if provider_key == "groq_llama70b":
            log.warning("  [AI/Groq] GROQ_API_KEY is empty — Groq disabled. "
                        "Add GROQ_API_KEY=... to your .env file (free at console.groq.com/keys).")
        return None
    if _is_backed_off(provider_key):
        log.debug(
            f"  [AI/{provider_key}] Still in backoff "
            f"({_remaining_backoff(provider_key):.0f}s remaining)"
        )
        return None
    model = _GROQ_MODELS[provider_key]
    try:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # MK13 FIX-A: Use `prompt` directly — NOT context_block.
            # context_block is for Claude Sonnet (system/user split) and GPT-4o
            # only. Groq calls must use the bare prompt which already contains
            # "JSON" in the classification instructions (satisfying Groq's
            # json_object mode requirement of the word "json" in the message).
            # Groq json_object mode requires the word "json" in the message.
            # If the caller's prompt doesn't contain it, append a minimal suffix.
            "messages": [{"role": "user", "content": prompt if "json" in prompt.lower()
                          else prompt + "\n\nRespond in JSON format."}],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            _GROQ_ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {groq_key}",
                # MK9 Fix ISSUE-1: Cloudflare 1010 blocks raw Python-urllib/* UA.
                # Any non-empty browser-like UA bypasses the rule. This was
                # documented on the Groq Community forum (Dec 2025) as the
                # canonical fix. Using our own product UA per RFC 7231 §5.5.3.
                "User-Agent": "WalkmanSync/9.0 (Windows NT 10.0; Win64; x64) Python/3.12",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            content = re.sub(r"^```json\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            return json.loads(content)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="ignore")[:200]
        except Exception:
            pass
        if e.code == 429:
            log.warning(f"  [AI/{provider_key}] 429 rate-limit — backing off 20s")
            _set_backoff(provider_key, 20.0)
            # MK20 G3: track Groq 429s for adaptive worker reduction
            if "groq" in provider_key.lower():
                try:
                    import genre_client as _gc_ref
                    _gc_ref._groq_record_429_and_get_workers()
                except Exception:
                    pass
        elif e.code in (401, 403):
            log.warning(f"  [AI/{provider_key}] HTTP {e.code} auth error — backing off 60s. {body}")
            _set_backoff(provider_key, 60.0)
            _record_auth_fail(provider_key)   # MK8: circuit breaker — disables after N fails
        else:
            log.warning(f"  [AI/{provider_key}] HTTP {e.code}: {body[:120]}")
            _set_backoff(provider_key, 30.0)
    except Exception as e:
        # Log at WARNING so connection errors (timeout, DNS, SSL) are visible
        # in the normal log — these indicate a configuration or network problem.
        log.warning(f"  [AI/{provider_key}] call failed: {type(e).__name__}: {e}")
    return None


def test_groq() -> dict:
    """
    One-shot liveness test for the Groq API.
    Call this at startup or via CLI to confirm the key works.

    Returns:
        {"ok": True, "model": str, "response": str}  on success
        {"ok": False, "error": str}                   on failure
    """
    groq_key = getattr(config, "GROQ_API_KEY", os.environ.get("GROQ_API_KEY", ""))
    if not groq_key:
        return {"ok": False, "error": "GROQ_API_KEY not set in .env"}

    model = _GROQ_MODELS["groq_llama8b"]   # smallest/fastest for the test
    try:
        payload = {
            "model": model,
            "max_tokens": 20,
            "temperature": 0.0,
            # MK13 FIX-B: Groq json_object mode requires the word "json" in messages.
            # Previous prompt 'Reply with {"status":"ok"}' had no literal "json".
            "messages": [{"role": "user", "content": 'Reply with JSON only: {"status":"ok"}'}],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            _GROQ_ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {groq_key}",
                # MK9 Fix ISSUE-1: Cloudflare 1010 blocks raw Python-urllib/* UA.
                # Any non-empty browser-like UA bypasses the rule. This was
                # documented on the Groq Community forum (Dec 2025) as the
                # canonical fix. Using our own product UA per RFC 7231 §5.5.3.
                "User-Agent": "WalkmanSync/9.0 (Windows NT 10.0; Win64; x64) Python/3.12",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"].strip()
            return {"ok": True, "model": model, "response": content}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="ignore")[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# MK11 ISSUE-B: Invoke startup key check HERE — after test_groq() is defined.
# Previously called at module scope before test_groq(), causing NameError.
_check_keys()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# MK11 ISSUE-E: GPT-4o provider
# ---------------------------------------------------------------------------
_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"


def _gpt4o_call(prompt: str, max_tokens: int, temperature: float, context_block: str = "") -> dict | None:
    """
    OpenAI GPT-4o — best-in-class music knowledge per dev research (late 2025).
    Requires OPENAI_API_KEY in .env.  Pay-per-token; only called as Phase 5
    fallback after all free-tier providers are exhausted/rate-limited.
    """
    api_key = getattr(config, "OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    if not api_key:
        return None
    if _is_backed_off("gpt4o"):
        return None
    try:
        payload = {
            "model": "gpt-4o-mini",   # cheaper than full gpt-4o; still excellent for genre
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            _OPENAI_ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent":    "WalkmanSync/11.0 (Windows NT 10.0; Win64; x64) Python/3.12",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data    = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            content = re.sub(r"^```json\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            return json.loads(content)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="ignore")[:200]
        except Exception:
            pass
        if e.code == 429:
            log.warning(f"  [AI/gpt4o] 429 rate-limit — backing off 30s")
            _set_backoff("gpt4o", 30.0)
        elif e.code in (401, 403):
            log.warning(f"  [AI/gpt4o] HTTP {e.code} auth error: {body}")
            _set_backoff("gpt4o", 60.0)
            _record_auth_fail("gpt4o")
        else:
            log.debug(f"  [AI/gpt4o] HTTP {e.code}: {body}")
    except Exception as e:
        log.warning(f"  [AI/gpt4o] call failed: {type(e).__name__}: {e}")
    return None


# ---------------------------------------------------------------------------
# MK11 ISSUE-E: Claude Sonnet provider (Anthropic API)
# ---------------------------------------------------------------------------
_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"


def _claude_sonnet_call(
    prompt: str,
    max_tokens: int,
    temperature: float,
    context_block: str = "",
) -> dict | None:
    """
    Anthropic Claude Sonnet — strong music theory knowledge per dev research.
    Requires ANTHROPIC_API_KEY in .env.  Pay-per-token; Phase 5 fallback only.

    MK12: Accepts an optional context_block — a structured evidence summary
    built by context_builder.build_context() that gives Claude all available
    library data (genre cache, streaming scores, collaborator genres, genre
    graph inference, device file format) BEFORE asking for classification.

    Architecture:
      system message: genre expert role + anti-default rules
      user  message:  context_block (evidence) + classification request

    The system/user split allows Claude to reason over the evidence separately
    from the classification instructions, producing more consistent results.
    """
    api_key = getattr(config, "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
    if not api_key:
        return None
    if _is_backed_off("claude_sonnet"):
        return None

    _SYSTEM = (
        "You are a world-class music librarian and genre classification expert. "
        "You have encyclopedic knowledge of global music genres including African "
        "subgenres (Afrobeats, Amapiano, Bongo Flava, Genge, Highlife, Soukous, "
        "Kizomba, Kompa), diaspora music, internet microgenres (Jersey Club, Phonk, "
        "Vaporwave, Lo-Fi Hip-Hop, Chopped and Screwed), and niche electronic scenes. "
        "\n\nCRITICAL RULES:\n"
        "1. NEVER default to Hip-Hop for uncertain tracks. If unsure, return empty string.\n"
        "2. HBCU Marching Bands (Jackson State, Grambling, Bethune) → always Marching Band.\n"
        "3. Arabic/North African artists → Arabic Music, Rai, Gnawa, or World Music.\n"
        "4. Use ALL provided library evidence — collaborator genres, graph inference, "
        "device format — before deciding.\n"
        "5. Return ONLY valid JSON: {\"genre\": \"string\"} — empty string if not confident."
    )

    # Build the user message: evidence block + classification request
    if context_block:
        user_content = context_block
    else:
        user_content = prompt   # bare prompt when no context available

    try:
        payload = {
            "model":      "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "system":     _SYSTEM,
            "messages":   [{"role": "user", "content": user_content}],
        }
        req = urllib.request.Request(
            _ANTHROPIC_ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "User-Agent":        "WalkmanSync/12.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data    = json.loads(resp.read())
            content = data["content"][0]["text"]
            # Extract JSON from text (Claude doesn't have strict json_mode)
            m = re.search(r"\{[^}]+\}", content)
            if m:
                return json.loads(m.group())
            return json.loads(content)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="ignore")[:200]
        except Exception:
            pass
        if e.code == 429:
            log.warning(f"  [AI/claude_sonnet] 429 rate-limit — backing off 30s")
            _set_backoff("claude_sonnet", 30.0)
        elif e.code in (401, 403):
            log.warning(f"  [AI/claude_sonnet] HTTP {e.code} auth error: {body}")
            _set_backoff("claude_sonnet", 60.0)
            _record_auth_fail("claude_sonnet")
        else:
            log.debug(f"  [AI/claude_sonnet] HTTP {e.code}: {body}")
    except Exception as e:
        log.warning(f"  [AI/claude_sonnet] call failed: {type(e).__name__}: {e}")
    return None


# ==========================================================================
# MK18 #14 — AI output schema validation + semantic constraints
# ==========================================================================
# ROOT CAUSE: AI genre inference results used directly in metadata write
# paths without validation.  Malformed/overly-broad genres, junk year
# values (e.g. "2024" for a 1962 recording), and hallucinated extra fields
# can corrupt the genre cache and persist across many sessions.
#
# Fix: validate_ai_genre_result() applies:
#   1. Year range [1900–2030]: reject values outside this window
#   2. Genre not empty string, not longer than 60 chars (hallucination signal)
#   3. Genre not in KNOWN_BAD list (known hallucination tokens)
#   4. Log rejected outputs for forensic tuning
# ==========================================================================

_YEAR_MIN: int = 1900
_YEAR_MAX: int = 2030

_KNOWN_BAD_GENRES: frozenset[str] = frozenset({
    "unknown", "genre", "music", "audio", "song", "track", "various",
    "n/a", "none", "null", "undefined", "not specified", "not available",
    "mixed", "other", "miscellaneous",
})


def validate_ai_genre_result(result: dict | None) -> dict | None:
    """
    MK18 #14: Validate and sanitise an AI genre inference result.
    Returns the result dict if valid (possibly with year stripped),
    or None if the result should be rejected entirely.

    Rejection criteria:
    - result is None or not a dict
    - genre is empty, too long (>60 chars), or in the known-bad list
    - year is present but outside [1900–2030]

    Rejected results are logged at DEBUG level for forensic tuning.
    """
    if not isinstance(result, dict):
        log.debug("[AI/Validate] Rejected: result is not a dict")
        return None

    genre = str(result.get("genre", "")).strip()
    year  = result.get("year", None)

    # Genre checks
    if not genre:
        log.debug("[AI/Validate] Rejected: empty genre")
        return None
    if len(genre) > 60:
        log.debug(f"[AI/Validate] Rejected: genre too long ({len(genre)} chars): {genre!r}")
        return None
    if genre.lower() in _KNOWN_BAD_GENRES:
        log.debug(f"[AI/Validate] Rejected: genre in known-bad list: {genre!r}")
        return None

    # Year checks (only if year field is present)
    validated: dict = {"genre": genre}
    if year is not None:
        try:
            y = int(str(year).strip()[:4])
            if _YEAR_MIN <= y <= _YEAR_MAX:
                validated["year"] = str(y)
            else:
                log.debug(f"[AI/Validate] Year {y} out of range [{_YEAR_MIN}–{_YEAR_MAX}], stripped")
        except (ValueError, TypeError):
            log.debug(f"[AI/Validate] Non-numeric year stripped: {year!r}")

    # MK28 E1-T1 (A3): Inject confidence score via source heuristic.
    # CTO: "no confidence scoring means bad inference becomes canonical truth."
    # Source reliability order (from MC sim evidence):
    #   Phase1/Deezer/LastFm = 0.90 (human-curated APIs)
    #   Gemini              = 0.85 (large model, more accurate)
    #   Groq/llama-3.1-8b   = 0.70 (smaller model, more genre errors)
    # Caller passes source via result.get("_source"); defaults to 0.70 (safe minimum).
    _source     = str(result.get("_source", "groq")).lower()
    # MK29 G1-T1 (A3): Confidence type discrimination — CTO:
    # "external metadata and LLM outputs are not on the same confidence scale."
    # type="external_metadata" → audit gate NEVER blocks (human-curated APIs)
    # type="llm_inference"     → audit gate applies (confidence<0.75 = skip)
    # type="heuristic"         → audit gate applies (for undefined/fallback sources)
    _source_type_map = {
        # External metadata APIs — always trusted, never LLM inference
        "lastfm": "external_metadata",   "last.fm": "external_metadata",
        "deezer": "external_metadata",   "phase1":  "external_metadata",
        "musicbrainz": "external_metadata", "discogs": "external_metadata",
        # LLM inference — subject to confidence gate
        "gemini": "llm_inference",
        "groq":   "llm_inference",       "llama":  "llm_inference",
    }
    _confidence_map = {
        # External metadata: 0.98 (not 0.90) — structurally different from LLM
        "lastfm":   0.98, "last.fm":  0.98, "deezer":   0.98,
        "musicbrainz": 0.95, "discogs": 0.95, "phase1": 0.98,
        # LLM: heuristic ranking (NOT statistical calibration)
        "gemini":   0.85,
        "groq":     0.70, "llama":    0.70,
    }
    _confidence  = 0.70  # safe default (Groq-equivalent)
    _source_type = "heuristic"  # safe default
    for _src_key in _confidence_map:
        if _src_key in _source:
            _confidence  = _confidence_map[_src_key]
            _source_type = _source_type_map.get(_src_key, "heuristic")
            break
    # MK30 F1-T1 (A3): Evaluability field — CTO: "determinism ≠ correctness probability."
    # external_metadata APIs are DETERMINISTIC (they return a value or don't) but not
    # necessarily CORRECT (LastFm/Deezer can be wrong). Mixing these into a scalar
    # confidence value conflates two orthogonal properties.
    # evaluability="verifiable"     → can be re-queried to check (external APIs)
    # evaluability="non_deterministic" → LLM outputs differ per call, can't verify
    # evaluability="assumed"        → heuristic/fallback, no verification path
    _evaluability_map = {
        "external_metadata": "verifiable",
        "llm_inference":     "non_deterministic",
        "heuristic":         "assumed",
    }
    _evaluability = _evaluability_map.get(_source_type, "assumed")

    # MK31 M1-T1 (A3): Authority field — CTO: "verifiable ≠ trustworthy."
    # Last.fm is crowd-contributed: verifiable but semantically noisy (crowd tags).
    # Deezer/MusicBrainz: curated or community-curated, higher semantic authority.
    # authority + evaluability TOGETHER determine bypass eligibility.
    _authority_map = {
        "lastfm": "crowd_contributed",  "last.fm": "crowd_contributed",
        "deezer": "curated",            "phase1":  "curated",
        "musicbrainz": "community_curated", "discogs": "community_curated",
        "gemini": "llm_generated",      "groq":    "llm_generated",
        "llama":  "llm_generated",
    }
    _authority = "llm_generated"
    for _ak in _authority_map:
        if _ak in _source:
            _authority = _authority_map[_ak]
            break

    if _source_type == "external_metadata":
        _confidence = None  # deterministic lookup, not a probability

    validated["confidence"]   = _confidence
    validated["evaluability"] = _evaluability
    validated["authority"]    = _authority
    validated["_source"]      = _source
    validated["_source_type"] = _source_type

    return validated


# ---------------------------------------------------------------------------
# MK39 Phase I-A1 (Council 2026-05-10 §2.1) — DeepSeek provider
# Council: "The .env file contains ANTHROPIC_API_KEY and DEEPSEEK_API_KEY,
# but the current codebase does not use them ... a massive missed opportunity."
# Anthropic was already partially wired via _claude_sonnet_call (Phase 6);
# this adds the missing DeepSeek path. Inserted in the waterfall between
# Groq exhaustion and the existing GPT-4o/Claude-Sonnet phases.
# ---------------------------------------------------------------------------

_DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"


def _deepseek_call(prompt: str, max_tokens: int, temperature: float,
                    context_block: str = "") -> dict | None:
    """DeepSeek via DEEPSEEK_API_KEY in .env. Pay-per-token but cheap;
    used when Gemini is circuit-broken and all Groq models have failed.
    Follows the same backoff/auth-failure pattern as the other providers
    so a 401/403 can session-disable it."""
    api_key = getattr(config, "DEEPSEEK_API_KEY",
                       os.environ.get("DEEPSEEK_API_KEY", ""))
    if not api_key:
        return None
    if _is_backed_off("deepseek"):
        return None

    _SYSTEM = (
        "You are a music genre classification expert. Return ONLY valid JSON "
        "of shape {\"genre\": \"<canonical-tag>\"} or {\"genre\": \"\"} if "
        "you are not confident. Never default to Hip-Hop for uncertain tracks; "
        "blank is preferred to a wrong guess."
    )
    user_content = context_block if context_block else prompt
    try:
        payload = {
            "model":       "deepseek-chat",
            "messages":    [
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": user_content},
            ],
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            _DEEPSEEK_ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent":    "WalkmanSync/12.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            # Reset auth-failure counter on success
            with _PROVIDERS["deepseek"]["lock"]:
                _PROVIDERS["deepseek"]["auth_fails"] = 0
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                # Try to salvage JSON if model wrapped it in prose
                m = re.search(r"\{[^}]+\}", content)
                if m:
                    try: return json.loads(m.group(0))
                    except json.JSONDecodeError: pass
                return None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _set_backoff("deepseek", 60.0)
            log.warning(f"  [AI/deepseek] 429 rate-limited; backoff 60s")
        elif e.code in (401, 403):
            _record_auth_fail("deepseek")
            log.warning(f"  [AI/deepseek] auth failure (HTTP {e.code})")
            _set_backoff("deepseek", 60.0)
        else:
            log.debug(f"  [AI/deepseek] HTTP {e.code}: {e.reason}")
            _set_backoff("deepseek", 30.0)
        return None
    except Exception as e:
        log.debug(f"  [AI/deepseek] call failed: {type(e).__name__}: {e}")
        _set_backoff("deepseek", 30.0)
        return None


def call_ai(prompt: str, max_tokens: int = 256, temperature: float = 0.2, context_block: str = "") -> dict | None:
    """
    Send a prompt through the provider waterfall, return parsed JSON dict.

    Waterfall:
      1. Groq: llama-3.3-70b-versatile       (free, <1s latency)
      2. Groq: mixtral-8x7b-32768            (free, reliable JSON mode)
      3. Groq: llama-3.1-8b-instant          (free, last resort)
      4. Gemini direct                       (your own key)
      5. Claude Sonnet 4.6                   (paid, anti-default system prompt)
      6. DeepSeek: deepseek-chat             (cheap pay-per-token)
      7. GPT-4o (with context block)         (paid, music-knowledge-rich)
    """
    # MK39 Phase I-C (Council 2026-05-10 §3-C): if Gemini is circuit-broken
    # AND Groq has failed > GROQ_FAILURE_THRESHOLD times this session, the
    # waterfall returns nothing useful and contributes nothing but latency.
    # Short-circuit immediately so the genre pipeline can fall through to
    # the "leave blank" path (Council's preferred terminal state).
    try:
        import mk39_phase_I_containment as _p1
        if _p1.ai_genre_disabled():
            return None
    except Exception:
        pass

    # Phase 1 — Groq (free, <1s latency)
    with _CALL_LOCK:
        time.sleep(_CALL_DELAY)

        result = _groq_call("groq_llama70b", prompt, max_tokens, temperature)
        if result is not None:
            return result

        result = _groq_call("groq_mixtral", prompt, max_tokens, temperature)
        if result is not None:
            return result

        result = _groq_call("groq_llama8b", prompt, max_tokens, temperature)
        if result is not None:
            return result
        log.debug("  [AI/groq_llama8b] returned None")

    # Phase 1b — secondary Groq key (NEW_GROQ_API_KEY); independent backoff
    with _CALL_LOCK:
        time.sleep(_CALL_DELAY)
        result = _groq_call("groq2_llama70b", prompt, max_tokens, temperature)
        if result is not None:
            return result
        result = _groq_call("groq2_llama8b", prompt, max_tokens, temperature)
        if result is not None:
            return result

    # Phase 2 — Gemini direct (skip if genre_client circuit-breaker already fired)
    try:
        import genre_client as _gc_cb
        _gemini_cb_dead = getattr(_gc_cb, "_GEMINI_DISABLED", False)
    except Exception:
        _gemini_cb_dead = False
    if not _gemini_cb_dead:
        with _CALL_LOCK:
            time.sleep(_CALL_DELAY)
            result = _gemini_direct(prompt, max_tokens, temperature)
            if result is not None:
                return result

    # Phase 3 — Claude Sonnet 4.6
    with _CALL_LOCK:
        time.sleep(_CALL_DELAY)
    result = _claude_sonnet_call(prompt, max_tokens, temperature,
                                 context_block=context_block)
    if result is not None:
        return result

    with _CALL_LOCK:
        time.sleep(_CALL_DELAY)
    result = _deepseek_call(prompt, max_tokens, temperature,
                              context_block=context_block)
    if result is not None:
        return result

    with _CALL_LOCK:
        time.sleep(_CALL_DELAY)
    result = _gpt4o_call(prompt, max_tokens, temperature,
                         context_block=context_block)
    if result is not None:
        return result

    log.warning(f"  [AI] All providers failed for prompt: {prompt[:60]!r}")
    # MK39 Phase I-C: feed the counter so AI_GENRE_DISABLED can trip when
    # combined with Gemini circuit-break. Repeated calls here = providers
    # exhausted; the next call_ai will short-circuit at the top.
    try:
        import mk39_phase_I_containment as _p1
        _p1.mark_groq_failure()
    except Exception:
        pass
    return None


def call_ai_batch(
    prompts: list[str],
    max_tokens: int = 256,
    temperature: float = 0.2,
) -> list[dict | None]:
    """
    Process a list of prompts sequentially through the waterfall.
    Returns results in the same order as prompts.

    Prefer this over looping call_ai() — consistent logging and lock behaviour.
    """
    results = []
    for i, prompt in enumerate(prompts):
        result = call_ai(prompt, max_tokens=max_tokens, temperature=temperature)
        results.append(result)
        if result is None:
            log.debug(f"  [AI/Batch] {i+1}/{len(prompts)} — all providers failed")
    return results


def _call_with_timeout(fn, *args, timeout: float = 5.0, default=None, **kwargs):
    """
    Run fn(*args, **kwargs) in a daemon thread; return default if it exceeds timeout.
    Prevents AI from blocking MTP transfers.
    """
    result = [default]

    def _target():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception:
            pass

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.debug(f"  [AI] Timeout ({timeout}s) — skipping inference for this file")
    return result[0]


# ---------------------------------------------------------------------------
# Regex pre-pass for track number extraction
# ---------------------------------------------------------------------------
# Applied BEFORE any AI call — well-named files never need an API call.
#
# Patterns handled (in priority order):
#   "Artist - Album - 01-06 Title"   disc=1, track=6
#   "Artist - Album - 06 Title"      disc=1, track=6
#   "CD2-06 Title"                   disc=2, track=6
#   "Disc 2 - 06 Title"              disc=2, track=6
#   "06 Title" / "06. Title"         track=6

_RE_CD_PREFIX  = re.compile(r"(?:CD|DISC|DISK)\s*(\d+)[-\s]+(\d{1,3})\b", re.I)
_RE_DISC_TRACK = re.compile(r"^(\d{1,2})-(\d{1,2})\s")
# Track-only: digits followed by a separator then a NON-DIGIT character.
# The negative lookahead (?!\d) prevents matching "5 TO 10" as track=5.
_RE_TRACK_ONLY = re.compile(r"^(\d{1,3})[\. ](?!\d)")
_RE_DISC_WORD  = re.compile(r"(?:CD|DISC|DISK)\s*(\d+)", re.I)

# Maximum plausible track number. 50 covers box sets / mixtapes with many
# tracks while excluding song-title numbers like "96 F**kries" (track 96
# would mean an album with 95+ tracks — effectively impossible).
_TRACK_MAX = 50


def _regex_track_number(filename_stem: str) -> tuple[str, str]:
    """
    Extract (track_number, disc_number) purely from the filename stem.
    Returns ("", "1") if nothing can be extracted.

    False-positive guard: a leading number is only treated as a track number
    when it falls in [1, _TRACK_MAX] AND is followed immediately by a
    separator then a non-digit character, so titles like "5 TO 10",
    "96 F**kries", "1985 - Intro" are not mis-parsed as track numbers.
    """
    stem = filename_stem.strip()
    parts = [p.strip() for p in stem.split(" - ")]
    last = parts[-1] if parts else stem

    # "CD2-06" or "Disc2-06" anywhere in the last segment
    m = _RE_CD_PREFIX.search(last)
    if m:
        return f"{int(m.group(2)):02d}", str(int(m.group(1)))

    # "01-06 Title" at the START of the last segment
    m = _RE_DISC_TRACK.match(last)
    if m:
        return f"{int(m.group(2)):02d}", str(int(m.group(1)))

    # Plain "06 Title" / "06. Title" at start of last segment
    m = _RE_TRACK_ONLY.match(last)
    if m:
        track = int(m.group(1))
        if 1 <= track <= _TRACK_MAX:
            dm = _RE_DISC_WORD.search(stem)
            disc = str(int(dm.group(1))) if dm else "1"
            return f"{track:02d}", disc

    return "", "1"


# ---------------------------------------------------------------------------
# Public inference helpers
# ---------------------------------------------------------------------------

def infer_track_number(
    filename: str,
    folder_parts: list[str] = None,
    artist_hint: str = "",
    album_hint: str = "",
) -> tuple[str, str]:
    """
    Infer (track_number, disc_number).
    Step 1: regex pre-pass — handles the majority of well-named files instantly.
    Step 2: structural gate — if the filename has no digit that could plausibly
            be a track number, skip AI entirely and return ("", "1").
            This eliminates wasted calls for bare song-title filenames like
            "Fire Squad.mp3", "Love Yourz.mp3", "AMApiano #1.mp3", etc.
    Step 3: AI fallback — only for files that regex couldn't parse but that
            do contain a digit pattern consistent with a track number.
    """
    # Step 1: regex pre-pass — free, instant, no quota
    regex_track, regex_disc = _regex_track_number(filename)
    if regex_track:
        log.debug(
            f"  [AI] Regex inferred track={regex_track}, disc={regex_disc} "
            f"for {filename!r}"
        )
        return regex_track, regex_disc

    # Step 2: structural gate — does this filename even look like it has a track number?
    # Patterns that pass: "07 Title", "Artist - Album - 07 Title", "CD2-07 Title"
    # Patterns that fail: "Love Yourz", "Fire Squad", "AMApiano #1", "Abantu"
    # "#1" is explicitly excluded — playlist/title ordinal, not a track number.
    _stem = filename.strip()
    _has_track_structure = bool(
        re.search(r"(?:^|\s[-–]\s)\d{1,3}[\s.\-]", _stem)    # leading "07 " or "- 07 "
        or re.search(r"(?:CD|DISC|DISK)\s*\d", _stem, re.I)   # disc marker
    )
    if not _has_track_structure:
        log.debug(f"  [AI] No track-number pattern in filename — skipping AI for {filename!r}")
        return "", "1"

    # Step 3: AI fallback — only reached when filename looks numbered but regex couldn't parse it
    folder_str = "/".join(folder_parts) if folder_parts else ""
    prompt = (
        "You are a music metadata expert. Extract the track number and disc number "
        "from this audio file.\n\n"
        f"Filename: {filename}\n"
        f"Folder path: {folder_str}\n"
        f"Artist hint: {artist_hint}\n"
        f"Album hint: {album_hint}\n\n"
        "Rules:\n"
        "- Track number is usually a 1-3 digit number at the beginning of the filename,\n"
        "  possibly after a disc prefix like '01-06' (disc=1, track=6).\n"
        "- If the filename contains 'CD2', 'Disc 2', 'CD 2', disc number is 2.\n"
        "- If no disc info, assume disc=1.\n"
        '- Return ONLY a JSON object: {"track": "NN", "disc": "D"}\n'
        "  NN is zero-padded to 2 digits (or \"\" if none). D is a digit string.\n"
        "- Do not guess if uncertain — return empty string for track.\n"
    )
    result = _call_with_timeout(call_ai, prompt, timeout=5.0, default=None)
    if result and isinstance(result, dict):
        track = str(result.get("track", "")).strip()
        disc  = str(result.get("disc", "1")).strip()
        track = f"{int(track):02d}" if track.isdigit() else ""
        disc  = str(int(disc)) if disc.isdigit() else "1"
        return track, disc

    return "", "1"


def infer_artist_album_title(
    filename: str,
    folder_parts: list[str] = None,
    existing_tags: dict = None,
) -> dict:
    """
    Use AI to infer missing artist, album, or title when tags are blank.
    Returns dict with keys: artist, album, title (only those inferred).
    """
    folder_str = "/".join(folder_parts) if folder_parts else ""
    existing = existing_tags or {}
    prompt = (
        "You are a music metadata expert. Infer missing or corrected metadata "
        "from the filename and folder.\n\n"
        f"Filename: {filename}\n"
        f"Folder: {folder_str}\n"
        "Existing tags (may be blank or wrong):\n"
        f"  Artist: {existing.get('artist', '')}\n"
        f"  Album:  {existing.get('album', '')}\n"
        f"  Title:  {existing.get('title', '')}\n\n"
        "Rules:\n"
        "- Standard filename format: 'Artist - Album - NN Title'\n"
        "- If the folder contains keywords like PRESENTS, VOL, CLUB CHOPS, MIXTAPE,\n"
        "  COLLECTION, it is a DJ compilation. Folder name gives album and album_artist.\n"
        "- Club edit / remix filenames are often REVERSED: 'SONG - ARTIST CLUB EDIT'\n"
        "  where the song name comes FIRST and the remixing artist comes SECOND.\n"
        "  Example: '500$ OUNCES - WESTSIDE GUNN CLUB EDIT'\n"
        "    -> title='500$ Ounces (Club Edit)', artist='Westside Gunn'\n"
        "- If existing artist tag contains a website watermark, REPLACE it.\n"
        "- Return ONLY a JSON object with fields you can infer with HIGH confidence.\n"
        '  Example: {"artist": "Tyler, the Creator", "title": "What A Day (Club Edit)"}\n'
        "- Omit any field you are unsure about. Never hallucinate data.\n"
    )
    result = _call_with_timeout(call_ai, prompt, timeout=5.0, default=None)
    if result and isinstance(result, dict):
        out = {}
        for key in ("artist", "album", "title"):
            val = (result.get(key) or "").strip()
            if val and val.lower() not in (
                "unknown", "unknown artist", "unknown album", "unknown title"
            ):
                out[key] = val
        return out
    return {}


def infer_genre(artist: str, title: str, album: str = "", spotify_id: str = "") -> dict:
    """
    Ask AI for genre and year when all database lookups fail (genre_client Phase 4).
    Returns {"genre": str, "year": str} — either may be empty.

    MK12: Uses context_builder to assemble library evidence before the AI call.
    The context block is passed to Claude Sonnet as the user message so it has
    collaborator genres, graph inference, device format, etc.
    """
    # Build library context block (fast — all in-memory after first load)
    _context_block = ""
    try:
        import context_builder as _cb
        _ctx = _cb.build_context(artist=artist, title=title, album=album,
                                  spotify_id=spotify_id)
        # For infer_genre we want genre + year, so extend the prompt block
        _context_block = _ctx.as_prompt_block().replace(
            'Return only: {"genre": "string"}',
            'Return ONLY: {"genre": "Genre Name", "year": "YYYY"} — empty strings if unsure'
        )
    except Exception as _cbe:
        log.debug(f"[infer_genre] ContextBuilder skipped: {_cbe}")

    prompt = (
        "You are a music metadata expert. Provide the genre and release year "
        "for this track.\n\n"
        f"Artist: {artist}\n"
        f"Title:  {title}\n"
        f"Album:  {album}\n\n"
        "Rules:\n"
        "- Use the most specific meaningful subgenre "
        "(e.g. \'Amapiano\' not \'Electronic\', \'Trap\' not \'Hip Hop\').\n"
        "- Year must be a 4-digit release year. Leave blank if unknown.\n"
        '- Return ONLY: {"genre": "Genre Name", "year": "YYYY"}\n'
        "- If not confident, return empty strings rather than guessing."
    )

    result = call_ai(prompt, max_tokens=128, temperature=0.2,
                     context_block=_context_block)
    if result and isinstance(result, dict):
        return {
            "genre": str(result.get("genre", "")).strip(),
            "year":  str(result.get("year",  "")).strip(),
        }
    return {}


