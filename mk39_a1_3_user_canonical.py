"""
mk39_a1_3_user_canonical.py — User-Canonical Source Recognition
================================================================================

Sprint:        MK39-S1 (Council reconsideration, 2026-05-09)
Closes:        Council "absence is mandatory absence" verdict was over-rigid.
               Operator-curated music has authoritative metadata signals
               (filename intent, folder organization, explicit override files)
               that are NOT inference and should NOT be conflated with
               AI / graph / cache propagation.

Background:
  The MK38.0 freeze + MK39 gate correctly block contamination-class writes
  from inference sources (ai_inference, claude_fallback, graph_propagation,
  artist_cache_inferred, etc.). However, the original Council policy of
  "leave unknowns blank, period" missed a key category: **the operator
  themselves**.

  When a user names a file `Rob Zombie - The Sinister Urge - 01 Scum Of The
  Earth.mp3`, that's not inference — that's STRUCTURED HUMAN INTENT. When
  the user organizes `/Music/<Artist>/<Album>/<track>.mp3`, the directory
  hierarchy IS metadata. When the user drops a `.user_metadata.json` next
  to their tracks, that's an explicit canonical statement.

  These signals deserve recognition as canonical (alongside spotify_canonical,
  musicbrainz_canonical, fingerprint_match, user_explicit) — but only when
  they meet strict, conservative criteria designed to prevent fabrication.

Council reconsideration verdict (locked, 2026-05-09):
  "The architecture supports — and mandates — that unknown fields remain
   unknown. The current audit crash is a minor code bug, not a policy
   failure." → adjusted: blank-when-unknown remains the default, BUT the
   operator's explicit signals (filename, folder, override file) ARE
   canonical and deserve recognition.

Design (adversarial CTO checklist, locked):

  1. ADDITIVE TO CANONICAL_SOURCES — registers three new sources alongside
     the existing four:
       - "user_canonical_override"  — explicit JSON override (STRONG)
       - "user_canonical_filename"  — structured filename pattern (MEDIUM)
       - "user_canonical_folder"    — folder hierarchy match (MEDIUM)
     The MK38.0 freeze already permits CANONICAL_SOURCES; extending this
     set lets these signals reach the gate without bypassing any check.

  2. CONSERVATIVE PARSING — only HIGH-CONFIDENCE patterns are recognized:
       - Filename: requires the " - " separator AND ≥2 parts AND no
         empty fields after stripping. Reject "track01.mp3" (no separator)
         and " - " (empty parts). Reject names that look like generic
         SoundCloud-style downloads (with watermark prefixes etc.).
       - Folder: requires the path to descend through Music/ AND have at
         least 2 levels (Artist/Album OR Artist/Track). Reject root-level
         files and pathologically deep paths.
       - Override: requires the JSON to validate against a strict schema
         AND to declare a `_provenance: "user_explicit"` field per track.

  3. CROSS-VALIDATION — when both filename and folder yield values for
     the same field:
       - Agree → STRONG (mark "user_canonical" full)
       - Disagree → recorded as conflict; field is NOT written; warning
         logged. Conservative: never auto-pick when human signals conflict.

  4. FILL-ONLY-BLANKS — the user-canonical writer NEVER overwrites a
     non-empty field unless an explicit override file says to. Existing
     metadata wins by default. This protects user's prior-set tags AND
     anything written by higher-trust canonical sources.

  5. MEDIATED — every write goes through the MK39-A1 mediator with an
     OperationDescriptor of op_class=TAG_WRITE and a capability whose
     semantic_scope explicitly authorizes the field. Direct mutagen calls
     would still be blocked by the MK39-Gate.

  6. REVERSIBLE — every user-canonical write is journaled by the mediator
     to .mk39_mutations.jsonl with the source attribution
     ("user_canonical_filename" etc.). Operators can audit and revert.

Public API:
  parse_filename_canonical(path)         → Dict[field, (value, confidence)]
  parse_folder_canonical(path)           → Dict[field, (value, confidence)]
  load_override_file(album_folder)       → Dict[filename, Dict[field, value]]
  resolve_user_canonical(path, existing) → Dict[field, (value, source, conf)]
                                            with conflict resolution + fill-only-blanks
  register_as_canonical_source()         → adds 3 new sources to MK38.0
                                            CANONICAL_SOURCES; idempotent

Format conventions recognized:
  Filename:
    "Artist - Album - NN Title.ext"     (3+ parts; canonical)
    "Artist - NN Title.ext"             (2 parts; treats as Artist-Title)
    "NN. Title.ext"                     (leading number; just title)
  Folder:
    /Music/Artist/Album/<track>.ext
    /Music/Artist - Album/<track>.ext   (combined folder)
  Override file (.user_metadata.json):
    {
      "version": 1,
      "tracks": {
        "AMApiano #1.mp3": {
          "artist": "FeisalK",
          "album": "Personal Mixes",
          "genre": "Amapiano",
          "year": 2024,
          "_provenance": "user_explicit"
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Source identifiers + confidence levels
# ═══════════════════════════════════════════════════════════════════════════

USER_CANONICAL_OVERRIDE = "user_canonical_override"
USER_CANONICAL_FILENAME = "user_canonical_filename"
USER_CANONICAL_FOLDER   = "user_canonical_folder"

USER_CANONICAL_SOURCES = frozenset({
    USER_CANONICAL_OVERRIDE,
    USER_CANONICAL_FILENAME,
    USER_CANONICAL_FOLDER,
})

# Confidence tiers (aligned with existing TrustTier int values where possible)
CONF_STRONG = "STRONG"      # explicit override OR filename+folder cross-validate
CONF_MEDIUM = "MEDIUM"      # filename alone OR folder alone
CONF_WEAK   = "WEAK"        # uncertain — should NOT be written

# Reserved override filename
OVERRIDE_FILENAME = ".user_metadata.json"


# ═══════════════════════════════════════════════════════════════════════════
# Watermark + reject patterns (to avoid promoting noise to canonical)
# ═══════════════════════════════════════════════════════════════════════════

# If a filename or folder contains these substrings, REFUSE to extract
# user-canonical metadata. These are signs the file came from a download
# tool that injected its own naming, NOT user intent.
_REJECT_FILENAME_SUBSTRINGS = (
    "soundcloudaud.com", "y2mate.com", "ytdl.", ".tmp", "_tmp",
    "walkman_dl_", ".download", "untitled",
)

# Generic / noise tokens that often appear as filename "artist" but aren't
_GENERIC_TOKENS = frozenset({
    "various", "various artists", "va", "compilation", "unknown",
    "unknown artist", "track", "audio", "music", "song", "untitled",
})


# ═══════════════════════════════════════════════════════════════════════════
# Filename parsing
# ═══════════════════════════════════════════════════════════════════════════

_LEADING_NUMBER_RE = re.compile(r"^\d{1,3}[\s.\-_]+")


def _strip_leading_number(s: str) -> str:
    """Remove leading track-number prefixes like '01 ', '01. ', '01-'."""
    return _LEADING_NUMBER_RE.sub("", s).strip() or s


def _is_rejected_name(s: str) -> bool:
    """Return True if the name contains watermark / tmp / generic markers
    that disqualify it from being a canonical user signal."""
    sl = s.lower()
    if any(w in sl for w in _REJECT_FILENAME_SUBSTRINGS):
        return True
    if sl.strip() in _GENERIC_TOKENS:
        return True
    return False


def parse_filename_canonical(path: Any) -> Dict[str, Tuple[str, str]]:
    """Parse a filename for user-canonical metadata signals.

    Returns a dict mapping {field_name: (value, confidence)}. Empty dict
    when no high-confidence pattern matches.

    Recognized patterns (require " - " separator, no empty parts,
    no watermark substrings):
      "Artist - Album - NN Title.ext"  → artist, album, title
      "Artist - NN Title.ext"          → artist, title
      "NN. Title.ext"                   → title (no artist info)
    """
    if path is None:
        return {}
    p = Path(str(path))
    stem = p.stem.strip()
    if not stem or _is_rejected_name(stem):
        return {}

    # Try " - " separated patterns first
    if " - " in stem:
        parts = [p_.strip() for p_ in stem.split(" - ")]
        # Reject if any part is empty (e.g., "Artist -  - Title")
        if any(not pt for pt in parts):
            return {}
        # Reject if any part is a generic token
        if any(_is_rejected_name(pt) for pt in parts):
            return {}

        if len(parts) >= 3:
            # Canonical: Artist - Album - NN Title
            artist = parts[0]
            album  = parts[1]
            title  = _strip_leading_number(parts[-1])
            if not (artist and album and title):
                return {}
            return {
                "artist":       (artist, CONF_MEDIUM),
                "album_artist": (artist, CONF_MEDIUM),
                "album":        (album,  CONF_MEDIUM),
                "title":        (title,  CONF_MEDIUM),
            }
        elif len(parts) == 2:
            # Two parts: ambiguous — could be Artist-Title or NN-Title
            # If first part is a leading number, treat as title only
            stripped_first = _strip_leading_number(parts[0])
            if stripped_first != parts[0] and not parts[0].replace(stripped_first, "").strip("- _."):
                # Was just a leading number; only title is reliable
                title = _strip_leading_number(parts[1])
                if title:
                    return {"title": (title, CONF_MEDIUM)}
                return {}
            # Otherwise: Artist - Title
            artist = parts[0]
            title  = _strip_leading_number(parts[1])
            if not (artist and title):
                return {}
            return {
                "artist": (artist, CONF_MEDIUM),
                "title":  (title,  CONF_MEDIUM),
            }
        else:
            return {}

    # No separator: maybe just "NN. Title.ext"
    stripped = _strip_leading_number(stem)
    if stripped != stem and stripped:
        return {"title": (stripped, CONF_MEDIUM)}

    # Bare name like "AMApiano #1" — no canonical signal
    return {}


# ═══════════════════════════════════════════════════════════════════════════
# Folder parsing
# ═══════════════════════════════════════════════════════════════════════════

def parse_folder_canonical(path: Any) -> Dict[str, Tuple[str, str]]:
    """Parse the folder hierarchy for user-canonical metadata signals.

    Recognizes:
      .../Music/<Artist>/<Album>/<track>.ext      (separate)
      .../Music/<Artist> - <Album>/<track>.ext    (combined)
      .../<Artist>/<Album>/<track>.ext            (Music optional)

    Returns dict mapping {field: (value, confidence)}. Confidence is
    MEDIUM for any single match; combined-folder pattern with " - "
    separator counts as MEDIUM for both artist and album.
    """
    if path is None:
        return {}
    p = Path(str(path))
    if not p.parts:
        return {}
    parents = list(p.parts[:-1])    # exclude filename

    # Find anchor: "Music" anywhere (case-insensitive)
    anchor_idx = None
    for i, part in enumerate(parents):
        if part.lower() == "music":
            anchor_idx = i
            break
    # If no Music anchor, use the last 2 parents as Artist/Album candidates
    # (only if both exist and look reasonable)
    if anchor_idx is None:
        if len(parents) < 2:
            return {}
        candidates = parents[-2:]
    else:
        candidates = parents[anchor_idx + 1:]

    if not candidates or any(_is_rejected_name(c) for c in candidates):
        return {}

    if len(candidates) == 1:
        # Just /Music/<X>/file — X could be artist OR album, ambiguous
        # Try combined "Artist - Album" pattern in the single name
        name = candidates[0]
        if " - " in name:
            sub = [s.strip() for s in name.split(" - ", 1)]
            if len(sub) == 2 and all(sub) and not any(_is_rejected_name(s) for s in sub):
                return {
                    "artist":       (sub[0], CONF_MEDIUM),
                    "album_artist": (sub[0], CONF_MEDIUM),
                    "album":        (sub[1], CONF_MEDIUM),
                }
        # Single non-separator name: don't guess artist vs album
        return {}

    # 2+ levels under Music: treat as Artist/Album/...
    artist_raw = candidates[0]
    album_raw  = candidates[1]
    if _is_rejected_name(artist_raw) or _is_rejected_name(album_raw):
        return {}
    out: Dict[str, Tuple[str, str]] = {
        "artist":       (artist_raw, CONF_MEDIUM),
        "album_artist": (artist_raw, CONF_MEDIUM),
        "album":        (album_raw,  CONF_MEDIUM),
    }
    # If the album folder is itself "Artist - Album", split it
    if " - " in album_raw:
        sub = [s.strip() for s in album_raw.split(" - ", 1)]
        if len(sub) == 2 and all(sub):
            # Cross-check: if artist folder agrees with the prefix, this is
            # the redundant "Artist/Artist - Album/track" pattern (the
            # operator's observed convention)
            if sub[0].lower() == artist_raw.lower():
                out["album"] = (sub[1], CONF_MEDIUM)
            # Otherwise leave the original album_raw — don't guess
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Override file
# ═══════════════════════════════════════════════════════════════════════════

def load_override_file(album_folder: Any) -> Dict[str, Dict[str, Any]]:
    """Load .user_metadata.json from `album_folder` if present.

    Returns dict mapping {filename: {field: value}}. Empty dict if no
    override file exists or it fails schema validation.

    Required schema:
      {
        "version": 1,
        "tracks": {
          "<filename>": {
            "<field>": <value>,
            "_provenance": "user_explicit"   (REQUIRED — otherwise track
                                              is rejected as unattributed)
          },
          ...
        }
      }
    """
    if album_folder is None:
        return {}
    p = Path(str(album_folder))
    override_path = p / OVERRIDE_FILENAME
    if not override_path.exists() or not override_path.is_file():
        return {}
    try:
        data = json.loads(override_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"  [MK39-UserCanonical] override file parse failed at "
                    f"{override_path!s}: {type(e).__name__}: {e}")
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        log.warning(f"  [MK39-UserCanonical] override file at {override_path!s} "
                    f"missing version=1 — rejecting")
        return {}
    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        return {}
    valid: Dict[str, Dict[str, Any]] = {}
    for fname, fields in tracks.items():
        if not isinstance(fields, dict):
            continue
        # Require explicit provenance attribution
        if fields.get("_provenance") != "user_explicit":
            log.warning(f"  [MK39-UserCanonical] override entry {fname!r} missing "
                        f"_provenance='user_explicit' — rejecting")
            continue
        # Strip the provenance marker; keep field values
        valid[str(fname)] = {k: v for k, v in fields.items() if not k.startswith("_")}
    return valid


# ═══════════════════════════════════════════════════════════════════════════
# Resolution: cross-validate, conflict-detect, fill-only-blanks
# ═══════════════════════════════════════════════════════════════════════════

def _is_path_tainted(file_path: Any) -> bool:
    """True if any part of the path contains a watermark / tmp / download
    substring. Files living in such paths haven't yet had user
    organizational intent applied — they're staging-area artifacts of
    the download pipeline, not curated music."""
    if file_path is None:
        return False
    p = Path(str(file_path))
    for part in p.parts:
        sl = str(part).lower()
        if any(w in sl for w in _REJECT_FILENAME_SUBSTRINGS):
            return True
    return False


def resolve_user_canonical(file_path: Any,
                             existing: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Tuple[Any, str, str]],
                                                                                    Dict[str, Tuple[Any, str, str]]]:
    """Combine all user-canonical signals for a file and resolve conflicts.

    Returns a 2-tuple ``(writable, quarantined)`` per Council 2026-05-09
    Conditional Approval (5 binding constraints):

      - **writable** dict — fields SAFE to write directly through the
        mediator with a `make_user_canonical_capability()` capability:
          * STRONG sources only: cross-validated (filename + folder agree)
            OR explicit `.user_metadata.json` override
          * Existing non-empty fields are NEVER in this dict (Council #4)
          * Conflicting filename↔folder values are NEVER in this dict

      - **quarantined** dict — single-signal MEDIUM-confidence fields
        that need operator review per Council Constraint #5:
          * Filename-only signal → quarantined (operator hasn't expressed
            organizational intent via folder structure)
          * Folder-only signal → quarantined (filename doesn't carry the
            specific field)
          * NEVER auto-written. Persist via `write_quarantine_file()`;
            operator promotes acceptable items to `.user_metadata.json`.

    Skip rules (apply to BOTH dicts):
      - All fields if path contains any watermark/tmp segment (Council #2)
      - Fields already non-empty in `existing` (Council #4 fill-only-blanks)
      - Conflicting filename↔folder values (no auto-resolution)

    Source labels:
      "user_canonical_override" — `.user_metadata.json` (STRONG, writable)
      "user_canonical"          — filename+folder agree (STRONG, writable)
      "user_canonical_filename" — filename only (MEDIUM, quarantined)
      "user_canonical_folder"   — folder only (MEDIUM, quarantined)
    """
    existing = existing or {}
    file_path = Path(str(file_path))

    if _is_path_tainted(file_path):
        log.debug(f"  [MK39-UserCanonical] path tainted (watermark/tmp segment), "
                  f"refusing all signal extraction: {file_path!s}")
        return {}, {}

    filename_signals = parse_filename_canonical(file_path)
    folder_signals   = parse_folder_canonical(file_path)
    override_data    = load_override_file(file_path.parent)
    override_for_this = override_data.get(file_path.name, {})

    writable: Dict[str, Tuple[Any, str, str]] = {}
    quarantined: Dict[str, Tuple[Any, str, str]] = {}
    all_fields = set(filename_signals.keys()) | set(folder_signals.keys()) | set(override_for_this.keys())

    for field in all_fields:
        # Council #4: existing non-empty wins (fill-only-blanks)
        existing_val = existing.get(field)
        if existing_val and str(existing_val).strip():
            continue

        # Override → STRONG, writable
        if field in override_for_this:
            ov = override_for_this[field]
            if ov is not None and str(ov).strip():
                writable[field] = (ov, USER_CANONICAL_OVERRIDE, CONF_STRONG)
            continue

        fn_val = filename_signals.get(field)
        fl_val = folder_signals.get(field)

        if fn_val and fl_val:
            # Council #1: cross-validation
            fn_v, fn_c = fn_val
            fl_v, fl_c = fl_val
            if fn_v.strip().lower() == fl_v.strip().lower():
                # AGREE → STRONG, writable
                writable[field] = (fn_v, "user_canonical", CONF_STRONG)
            else:
                # DISAGREE → CONFLICT, neither writable nor quarantined
                # (operator must explicitly resolve via override file)
                log.warning(
                    f"  [MK39-UserCanonical] CONFLICT field={field!r} "
                    f"filename={fn_v!r} folder={fl_v!r} for {file_path!s} — "
                    f"NOT written, NOT quarantined (operator override required)"
                )
            continue
        # Council #1: SINGLE SIGNAL → quarantine, not writable
        if fn_val:
            v, c = fn_val
            quarantined[field] = (v, USER_CANONICAL_FILENAME, c)
            continue
        if fl_val:
            v, c = fl_val
            quarantined[field] = (v, USER_CANONICAL_FOLDER, c)
    return writable, quarantined


# ═══════════════════════════════════════════════════════════════════════════
# Council Constraint #3: mediator capability builder
# ═══════════════════════════════════════════════════════════════════════════

def make_user_canonical_capability(source: str, fields: List[str]) -> Dict[str, Any]:
    """Build a capability dict suitable for `mediator.mediated_operation()`
    that authorizes writes attributed to a user-canonical source.

    Per Council Constraint #3, user-canonical writes MUST flow through
    the mediated write barrier with telemetry. This helper produces the
    capability the mediator and gate require so the call site is:

        from mk39_a1_mediator import mediated_operation, OperationDescriptor
        from mk39_a1_3_user_canonical import (
            resolve_user_canonical, make_user_canonical_capability
        )

        writable, _quarantined = resolve_user_canonical(path, existing=current)
        for field, (val, src, conf) in writable.items():
            cap = make_user_canonical_capability(src, [field])
            descr = OperationDescriptor(op_class="TAG_WRITE", target=str(path),
                                          attribution={"source": src, "confidence": conf})
            with mediated_operation(descr, cap):
                # Inside this scope the gate allows this exact field write
                set_tag(path, field, val)   # routes through mutagen+gate

    The capability authorizes only the listed fields; widening the scope
    after the fact requires a new capability.
    """
    if source not in (USER_CANONICAL_SOURCES | {"user_canonical"}):
        raise ValueError(f"source must be one of USER_CANONICAL_SOURCES or "
                          f"'user_canonical', got {source!r}")
    return {
        "source": source,
        "issued_by": "mk39_a1_3_user_canonical",
        "semantic_scope": {
            "allowed_op_classes": ["TAG_WRITE"],
            "allowed_fields": list(fields),
            "max_writes": len(fields),    # one write per authorized field
        },
        "telemetry_marker": "USER_CANONICAL_WRITE",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Council Constraint #5: quarantine workflow
# (operator must approve single-signal data before it becomes authoritative)
# ═══════════════════════════════════════════════════════════════════════════

QUARANTINE_FILENAME = ".user_canonical_pending.json"


def write_quarantine_file(album_folder: Any,
                            quarantined_per_track: Dict[str, Dict[str, Tuple[Any, str, str]]],
                            merge: bool = True) -> Optional[Path]:
    """Persist quarantined single-signal user-canonical metadata to
    `<album_folder>/.user_canonical_pending.json` for operator review.

    Per Council Constraint #5: single-signal extractions are MEDIUM
    confidence and need human approval before they become authoritative.
    This file accumulates all pending suggestions; operators review it,
    copy acceptable entries to `.user_metadata.json` (with `_provenance:
    user_explicit`), and the next sync sees them as STRONG/writable.

    Args:
      album_folder: directory to write the pending file into
      quarantined_per_track: mapping {filename: quarantined_dict} where
        each quarantined_dict is the second element of resolve_user_canonical
      merge: if True (default), merge with existing pending file rather
        than overwriting. Existing entries for the same file+field are
        preserved; new entries are appended.

    Returns the Path to the pending file, or None on failure.

    File format (JSON):
      {
        "version": 1,
        "generated_at": "ISO-timestamp",
        "council_constraint": "MK39-A1.3 #5 (operator review required)",
        "_instructions": "Review entries below. To accept an entry, copy
                          its field+value into .user_metadata.json with
                          `_provenance: user_explicit`. Quarantined items
                          are NOT written to audio files.",
        "pending": {
          "<filename>": {
            "<field>": {"value": ..., "source": "...", "confidence": "MEDIUM",
                         "first_seen": "ISO-timestamp"},
            ...
          },
          ...
        }
      }
    """
    if not quarantined_per_track:
        return None
    p = Path(str(album_folder))
    if not p.exists() or not p.is_dir():
        log.warning(f"  [MK39-UserCanonical] quarantine target not a directory: {p!s}")
        return None
    out = p / QUARANTINE_FILENAME
    from datetime import datetime
    now_iso = datetime.utcnow().isoformat() + "Z"

    # Merge with existing if requested
    existing_pending: Dict[str, Dict[str, Any]] = {}
    if merge and out.exists():
        try:
            existing_data = json.loads(out.read_text(encoding="utf-8"))
            existing_pending = existing_data.get("pending", {})
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"  [MK39-UserCanonical] existing quarantine file unreadable, "
                        f"starting fresh: {type(e).__name__}: {e}")

    # Merge: preserve `first_seen` for existing entries; add new ones
    merged: Dict[str, Dict[str, Any]] = dict(existing_pending)
    for fname, q_dict in quarantined_per_track.items():
        if not q_dict:
            continue
        merged.setdefault(fname, {})
        for field, (val, src, conf) in q_dict.items():
            if field in merged[fname]:
                # Preserve first_seen
                first_seen = merged[fname][field].get("first_seen", now_iso)
                # Update value+source if changed
                merged[fname][field] = {
                    "value": val, "source": src, "confidence": conf,
                    "first_seen": first_seen, "last_seen": now_iso,
                }
            else:
                merged[fname][field] = {
                    "value": val, "source": src, "confidence": conf,
                    "first_seen": now_iso, "last_seen": now_iso,
                }

    payload = {
        "version": 1,
        "generated_at": now_iso,
        "council_constraint": "MK39-A1.3 #5 (operator review required)",
        "_instructions": (
            "Review entries below. To accept an entry, copy its field+value "
            f"into .user_metadata.json with `_provenance: user_explicit`. "
            "Quarantined items are NOT written to audio files."
        ),
        "pending": merged,
    }
    try:
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        log.info(f"  [MK39-UserCanonical] QUARANTINE_WRITTEN file={out!s} "
                 f"tracks={len(merged)} (operator review per Council #5)")
        return out
    except OSError as e:
        log.error(f"  [MK39-UserCanonical] failed to write quarantine: "
                  f"{type(e).__name__}: {e}")
        return None


def load_quarantine_file(album_folder: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Load the quarantine file from `<album_folder>/.user_canonical_pending.json`.
    Returns the pending dict (filename → field → entry), or empty dict if
    no file exists or it fails to parse."""
    if album_folder is None:
        return {}
    p = Path(str(album_folder)) / QUARANTINE_FILENAME
    if not p.exists() or not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}
    pending = data.get("pending")
    return pending if isinstance(pending, dict) else {}


def quarantine_summary(quarantined_per_track: Dict[str, Dict[str, Tuple[Any, str, str]]]) -> Dict[str, int]:
    """Build a summary suitable for operator dashboards: count of quarantined
    items per source label."""
    summary: Dict[str, int] = {}
    for fname, q in quarantined_per_track.items():
        for field, (val, src, conf) in q.items():
            summary[src] = summary.get(src, 0) + 1
    summary["total_quarantined"] = sum(v for k, v in summary.items()
                                         if k != "total_quarantined")
    return summary


# ═══════════════════════════════════════════════════════════════════════════
# Registration with MK38.0 CANONICAL_SOURCES
# ═══════════════════════════════════════════════════════════════════════════

_REGISTRATION_LOCK = threading.RLock()
_REGISTERED = False


def register_as_canonical_source() -> bool:
    """Add USER_CANONICAL_SOURCES (override, filename, folder) to MK38.0's
    CANONICAL_SOURCES set so the freeze permits writes attributed to these
    sources. Idempotent: subsequent calls return False.

    Also registers "user_canonical" (combined / cross-validated) which is
    the source label used when filename + folder agree.
    """
    global _REGISTERED
    with _REGISTRATION_LOCK:
        if _REGISTERED:
            return False
    try:
        import mk38_0_semantic_write_barrier as mk38
        # Add all four labels (3 individual + 1 combined "user_canonical")
        new_sources = {
            USER_CANONICAL_OVERRIDE,
            USER_CANONICAL_FILENAME,
            USER_CANONICAL_FOLDER,
            "user_canonical",   # the combined cross-validated label
        }
        # CANONICAL_SOURCES is a Set[str] — extend in place
        mk38.CANONICAL_SOURCES.update(new_sources)
        with _REGISTRATION_LOCK:
            _REGISTERED = True
        log.info(
            f"  [MK39-UserCanonical] CANONICAL_SOURCES_EXTENDED — added "
            f"{sorted(new_sources)} to MK38.0 freeze whitelist; "
            f"total canonical sources now: {len(mk38.CANONICAL_SOURCES)}"
        )
        return True
    except ImportError:
        log.warning("  [MK39-UserCanonical] mk38_0_semantic_write_barrier not "
                    "available; user-canonical sources will not be registered")
        return False
    except Exception as e:
        log.error(f"  [MK39-UserCanonical] registration failed: "
                  f"{type(e).__name__}: {e}")
        return False


def is_registered() -> bool:
    with _REGISTRATION_LOCK:
        return _REGISTERED


__all__ = [
    "USER_CANONICAL_OVERRIDE", "USER_CANONICAL_FILENAME", "USER_CANONICAL_FOLDER",
    "USER_CANONICAL_SOURCES", "OVERRIDE_FILENAME", "QUARANTINE_FILENAME",
    "CONF_STRONG", "CONF_MEDIUM", "CONF_WEAK",
    "parse_filename_canonical", "parse_folder_canonical",
    "load_override_file", "resolve_user_canonical",
    "register_as_canonical_source", "is_registered",
    # Council Conditional-Approval constraints (2026-05-09)
    "make_user_canonical_capability",       # #3 mediated barrier
    "write_quarantine_file",                # #5 operator review
    "load_quarantine_file",                 # #5 operator review
    "quarantine_summary",                   # #5 operator review
]
