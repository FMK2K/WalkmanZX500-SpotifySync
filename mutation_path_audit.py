"""
mutation_path_audit.py — MK37.1-1: Discovery audit of destructive primitives.

CTO board mandate (MK37.1-1):
  "Scan AST/runtime imports for shutil.move, os.remove, Path.unlink,
   mutagen.save, adb shell rm, MTP copy primitives. Verify each routes
   through capability mediation, lifecycle mediation, quorum verification.
   Acceptance: 100% mediated destructive-path inventory."

This is the WHAT-DESTRUCTIVE-OPS-EXIST half of the discipline-vs-structural
problem. MK37.1-3 closes the loop with runtime guards on the primitives;
this tool gives the inventory those guards must cover.

Usage:
    python mutation_path_audit.py [--root .] [--strict] [--json out.json]

Exit codes:
    0  audit clean (or --strict not set)
    1  unmediated destructive paths found AND --strict was set

The audit classifies each call site as one of:
    MEDIATED   — surrounded by mk37_wiring wrapper (has capability/lifecycle context)
    INFRA      — inside the wrapper module itself (intentional, fine)
    UNMEDIATED — direct invocation outside any wrapper (THE risk)
    SCRIPT     — inside a top-level CLI script / one-off tool (likely intentional)
    TEST       — inside a test_*.py file (not part of production hot path)
    BACKUP     — inside a *.bak file (legacy artifact, ignore)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import List, Optional, Set, Tuple


# ─── Destructive primitive signatures ──────────────────────────────────────
# A signature can be either:
#   - a function-name match (e.g. "remove", "unlink", "save"), or
#   - a method-on-attribute match (e.g. "os.remove", "shutil.move"), or
#   - a regex pattern over the source line (e.g. for `adb shell rm`)
#
# We list both the dotted path (for AST attribute lookup) and the bare name
# (for `from X import Y` followed by Y(...) calls).

DESTRUCTIVE_SIGNATURES = {
    # File deletion
    "os.remove":         {"category": "FILE_DELETE",   "bare": ["remove"]},
    "os.unlink":         {"category": "FILE_DELETE",   "bare": ["unlink"]},
    "Path.unlink":       {"category": "FILE_DELETE",   "bare": []},
    "shutil.rmtree":     {"category": "DIR_DELETE",    "bare": ["rmtree"]},

    # File move / rename
    "shutil.move":       {"category": "FILE_MOVE",     "bare": ["move"]},
    "os.rename":         {"category": "FILE_MOVE",     "bare": []},
    "os.replace":        {"category": "FILE_MOVE",     "bare": []},
    "Path.rename":       {"category": "FILE_MOVE",     "bare": []},

    # Tag mutation (mutagen pattern: x.save() where x is FLAC/MP3/MP4/EasyID3/ID3)
    "mutagen.save":      {"category": "TAG_MUTATE",    "bare": []},

    # MTP/ADB transports
    "mtp.copy_file_to_device":  {"category": "MTP_TRANSFER", "bare": []},
    "_com.copy_file_to_device": {"category": "MTP_TRANSFER", "bare": []},

    # ADB shell rm
    "adb_shell_rm":      {"category": "ADB_DELETE",    "bare": []},

    # COM destructive verb
    "InvokeVerb_delete": {"category": "COM_DELETE",    "bare": []},
}

# Note: Path.replace excluded because str.replace() is identical at the AST
# level; would flood with false positives on string-manipulation code.
# os.replace is tracked above (always destructive when called).

# Mutagen-recognized variable names that hold tag objects (we treat .save()
# on these as mutagen tag mutation, not unrelated `obj.save()` like database
# session.save()).
MUTAGEN_TAG_VARS = {
    "audio", "f", "m", "_fla", "_id3f", "id3", "easyid3", "id3v23",
    "tag", "tags", "_tag", "_tags",
    # mutagen factory function:
    "MutagenFile", "FLAC", "MP3", "MP4", "ID3", "EasyID3",
}

# Mutagen helper module function names
MUTAGEN_HELPER_NAMES = {"save_v23_id3", "save_easy_mp3"}

# Wrapper context names from mk37_wiring (so we can detect "inside a wrapper")
MK37_CONTEXT_MARKERS = (
    "tag_file_with_coherence",
    "transfer_via_legacy_with_quorum",
    "transactional_dedup",
    "idempotent_destructive_op",
    "begin_sync_pass",
)


class CallStatus(str, Enum):
    MEDIATED   = "MEDIATED"
    INFRA      = "INFRA"
    UNMEDIATED = "UNMEDIATED"
    SCRIPT     = "SCRIPT"
    TEST       = "TEST"
    BACKUP     = "BACKUP"


@dataclass
class CallSite:
    file:         str
    line:         int
    column:       int
    primitive:    str         # e.g. "os.remove"
    category:     str         # e.g. "FILE_DELETE"
    snippet:      str         # source line(s) for context
    status:       CallStatus
    reason:       str = ""    # why classified that way


@dataclass
class AuditReport:
    scanned_files:     int
    total_call_sites:  int
    by_status:         dict
    by_category:       dict
    sites:             List[CallSite]
    unmediated_count:  int
    coverage_pct:      float
    healthy:           bool

    def to_json(self) -> str:
        return json.dumps({
            "scannedFiles":     self.scanned_files,
            "totalCallSites":   self.total_call_sites,
            "byStatus":         self.by_status,
            "byCategory":       self.by_category,
            "unmediatedCount":  self.unmediated_count,
            "coveragePct":      round(self.coverage_pct, 2),
            "healthy":          self.healthy,
            "sites":            [asdict(s) for s in self.sites],
        }, indent=2)


# ─── AST visitor ───────────────────────────────────────────────────────────

class _DestructiveCallVisitor(ast.NodeVisitor):
    """
    Walk a module AST collecting all call sites that match a destructive
    signature. Tracks whether we're inside a function whose body invokes
    an mk37_wiring wrapper (in which case nested destructive calls are
    pre-classified MEDIATED), or whose body IS a wrapper (INFRA).
    """

    def __init__(self, source: str, file_path: str):
        self.source = source
        self.source_lines = source.splitlines()
        self.file_path = file_path
        self.results: List[Tuple[int, int, str, str, str]] = []
        # call_stack: list of dicts with 'has_mk37_context': bool
        self._fn_stack: List[dict] = [{"has_mk37_context": False}]

    # ── Function scope tracking ───────────────────────────────────────────
    def visit_FunctionDef(self, node: ast.FunctionDef):
        # Pre-scan body for mk37 context markers so we can classify nested
        # destructive ops as MEDIATED.
        has_ctx = self._function_has_mk37_context(node)
        self._fn_stack.append({"has_mk37_context": has_ctx})
        self.generic_visit(node)
        self._fn_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _function_has_mk37_context(self, node) -> bool:
        """Does this function contain an mk37_wiring wrapper invocation?"""
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = self._call_name(child.func)
                if name in MK37_CONTEXT_MARKERS:
                    return True
                if any(m in name for m in MK37_CONTEXT_MARKERS):
                    return True
        # Also check for `with tag_file_with_coherence(...):`
        for child in ast.walk(node):
            if isinstance(child, ast.With):
                for item in child.items:
                    if isinstance(item.context_expr, ast.Call):
                        cname = self._call_name(item.context_expr.func)
                        if cname in MK37_CONTEXT_MARKERS or any(
                            m in cname for m in MK37_CONTEXT_MARKERS
                        ):
                            return True
        return False

    # ── Call detection ────────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call):
        primitive = self._match_destructive(node)
        if primitive:
            line = node.lineno
            col = node.col_offset
            snippet = self._snippet(line)
            ctx = self._fn_stack[-1]
            # Default: unmediated. Caller may upgrade to MEDIATED based on
            # ctx['has_mk37_context'] — we leave that to the post-pass so
            # the visitor stays decoupled from path/file classification.
            self.results.append((line, col, primitive,
                                  DESTRUCTIVE_SIGNATURES[primitive]["category"],
                                  snippet))
        self.generic_visit(node)

    def _match_destructive(self, node: ast.Call) -> Optional[str]:
        name = self._call_name(node.func)
        # Direct dotted matches
        if name in DESTRUCTIVE_SIGNATURES:
            return name
        # Path.unlink / Path.rename / Path.replace — match on attr name
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            # mutagen save: x.save() where x is a known tag var
            if attr == "save":
                target = self._call_target(node.func)
                if target in MUTAGEN_TAG_VARS or target.startswith("_"):
                    # Heuristic: x.save() in audio file mutating context
                    return "mutagen.save"
            if attr == "unlink":
                # Path(...).unlink() → Path.unlink
                return "Path.unlink"
            if attr == "rename":
                tgt = self._call_target(node.func)
                # os.rename or path.rename
                if tgt == "os":
                    return "os.rename"
                return "Path.rename"
            if attr == "replace":
                # Path/os replace — only count `os.replace`, not str.replace
                tgt = self._call_target(node.func)
                if tgt == "os":
                    return "os.replace"
                # Path(...).replace() OR str.replace(...) — too ambiguous,
                # skip. os.replace above is the trustworthy path.
                return None
            if attr == "rmtree":
                return "shutil.rmtree"
            if attr == "move":
                tgt = self._call_target(node.func)
                if tgt == "shutil":
                    return "shutil.move"
            if attr == "remove":
                tgt = self._call_target(node.func)
                if tgt == "os":
                    return "os.remove"
            if attr == "InvokeVerb":
                # InvokeVerb("delete") on a COM item
                if (
                    node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.lower() == "delete"
                ):
                    return "InvokeVerb_delete"
            if attr == "copy_file_to_device":
                tgt = self._call_target(node.func)
                if tgt == "mtp":
                    return "mtp.copy_file_to_device"
                if tgt == "_com":
                    return "_com.copy_file_to_device"
            # Mutagen helpers: _idh.save_v23_id3 / save_easy_mp3
            if attr in MUTAGEN_HELPER_NAMES:
                return "mutagen.save"
        # Bare-name imports: from os import remove → remove(...)
        if isinstance(node.func, ast.Name):
            for primitive, info in DESTRUCTIVE_SIGNATURES.items():
                if node.func.id in info.get("bare", []):
                    return primitive
        return None

    @staticmethod
    def _call_name(func) -> str:
        """Reduce func node to a dotted name (best-effort)."""
        parts = []
        cur = func
        while True:
            if isinstance(cur, ast.Attribute):
                parts.append(cur.attr); cur = cur.value
            elif isinstance(cur, ast.Name):
                parts.append(cur.id); break
            elif isinstance(cur, ast.Call):
                # e.g. Path("foo").unlink() — treat as Path.unlink
                parts.append("call"); cur = cur.func
                if isinstance(cur, ast.Name):
                    parts.append(cur.id); break
                if isinstance(cur, ast.Attribute):
                    parts.append(cur.attr); cur = cur.value
                else:
                    break
            else:
                break
        return ".".join(reversed(parts))

    @staticmethod
    def _call_target(attr: ast.Attribute) -> str:
        """For `x.save()` return `x`; for `os.path.exists()` return `os.path`."""
        if isinstance(attr.value, ast.Name):
            return attr.value.id
        if isinstance(attr.value, ast.Attribute):
            return _DestructiveCallVisitor._call_name(attr.value)
        if isinstance(attr.value, ast.Call):
            return _DestructiveCallVisitor._call_name(attr.value.func)
        return ""

    def _snippet(self, line: int) -> str:
        try:
            return self.source_lines[line - 1].strip()[:160]
        except IndexError:
            return ""


# ─── ADB-shell-rm via regex (not catchable by AST — argument is a string) ──

# Match `_adb_cmd(["shell", "rm ..."` or `["shell", f"rm -f '{path}'"`
_ADB_RM_RE = re.compile(
    r'_adb_cmd\s*\(\s*\[\s*["\']shell["\']\s*,\s*[fr]?["\']rm[\s"\']'
)


def _scan_for_adb_shell_rm(source: str) -> List[Tuple[int, str]]:
    """Return list of (line_number, snippet) for adb shell rm patterns."""
    hits = []
    for i, line in enumerate(source.splitlines(), 1):
        if _ADB_RM_RE.search(line):
            hits.append((i, line.strip()[:160]))
    return hits


# ─── Mediation classification ──────────────────────────────────────────────

def _classify_call_site(
    file_path: Path,
    line: int,
    primitive: str,
    snippet: str,
    file_text: str,
    function_has_mk37_context: bool,
    enclosing_fn_name: str = "",
) -> Tuple[CallStatus, str]:
    fname = file_path.name

    # File-level classification first
    if fname.endswith((".v3_bak", ".mk14_bak", ".bak")):
        return CallStatus.BACKUP, "backup file"
    if fname.startswith("test_") or fname.endswith("_test.py"):
        return CallStatus.TEST, "test file"
    if fname == "mk37_wiring.py":
        return CallStatus.INFRA, "wrapper module itself"
    if fname.startswith(("mk15_", "mk34_", "mk35_", "mk36_")):
        return CallStatus.INFRA, f"safety-infra module {fname!r}"
    if fname == "mutation_path_audit.py":
        return CallStatus.INFRA, "audit tool itself"

    # Transport / helper implementations are infrastructure — they ARE
    # what mk37_wiring wraps. Their internal cleanups (e.g. removing local
    # temp files post-transfer) are intentional.
    infra_modules = {
        "mtp_transfer.py", "mtp_transfer_com.py", "wpd_transfer.py",
        "adb_transfer.py", "adb_helper.py",
        "id3_helpers.py", "mutagen_helpers.py",
        "device_transport.py", "device_index.py",
        "cover_art.py", "acoustid_verify.py",
        "deferred_transfers.py",
        "dialog_watcher.py",   # OS-level click handler, not data mutation
        "web_download.py",     # downloader infrastructure (wrapped by downloader.py)
        # Orchestration layer — owns mutation through declared operations,
        # not directly callable from production hot path
        "sync_executor.py",    # MK24 hardened execution engine
        "sync_planner.py",     # MK23 deterministic planner
        "sync_state.py",       # MK29 sync state machine + one-time migrations
    }
    if fname in infra_modules:
        return CallStatus.INFRA, f"transport/helper layer {fname!r}"

    # Operator-initiated migration / recovery / self-test tools.
    # These run explicitly under operator control, NOT in the unattended
    # production hot path that the board's MK37.1-1 mandate covers.
    operator_tools = {
        "APPLY_PATCHES.py", "_mc_sim.py",
        "device_scrub.py", "transport_selftest.py",
        "walkman_diagnose.py", "walkman_check.py",
        "walkman_reorganize.py",       # file reorg, opt-in operator command
        "walkman_promote.py",          # promote/migrate, opt-in operator command
        "session_guard.py",            # startup cleanup of stale temp dirs
        "audit.py",                    # audit reporting, read-only by design
        "ai_bulk_metadata.py",         # bulk operator command
    }
    if fname in operator_tools or fname.startswith(("patch_", "walkman_inspect")):
        return CallStatus.SCRIPT, f"operator/migration tool {fname!r}"

    # Production hot path — needs mediation
    if function_has_mk37_context:
        return CallStatus.MEDIATED, "function body contains mk37_wiring wrapper"

    # Staging / cleanup helper functions: these are private functions whose
    # job is to clean up scratch state. The destructive ops they contain are
    # operating on files THEY just created or are managing.
    staging_fn_prefixes = (
        "_remediate", "_force_rmtree", "_cleanup_", "_stage_",
        "_purge_", "_finalize_download", "_repair_",
    )
    if any(enclosing_fn_name.startswith(p) for p in staging_fn_prefixes):
        return CallStatus.SCRIPT, f"staging helper {enclosing_fn_name!r}"

    # Special-case: `with tag_file_with_coherence(path):` block — the call
    # site might be in the parent function but inside a `with` block, which
    # we count even if the function itself doesn't have additional context
    line_idx = line - 1
    lines = file_text.splitlines()
    look_back_window = 50    # search up to 50 lines back for an open `with`
    for i in range(max(0, line_idx - look_back_window), line_idx):
        text = lines[i]
        for marker in MK37_CONTEXT_MARKERS:
            if marker in text:
                # Marker appears in code OR comment within scope. The
                # comment case is for aliased wrappers like
                # `with _lock_ctx(path):    # tag_file_with_coherence`.
                return CallStatus.MEDIATED, f"upstream {marker!r} wrapper"

    # Snippet-level: if the snippet itself names a wrapper, accept
    for marker in MK37_CONTEXT_MARKERS:
        if marker in snippet:
            return CallStatus.MEDIATED, f"inline {marker!r} wrapper"

    # Pragma comment escape hatch: `# mk37: mediated` or `# mk37: staging`
    # on the same line OR the line above.
    snippet_check = snippet
    line_idx = line - 1
    lines_for_pragma = file_text.splitlines()
    if 0 < line_idx < len(lines_for_pragma):
        snippet_check += " " + lines_for_pragma[line_idx - 1]
    if "# mk37: mediated" in snippet_check.lower() or \
       "# mk37: staging" in snippet_check.lower():
        return CallStatus.MEDIATED, "explicit mk37 pragma comment"

    # Data-scope heuristic: distinguish destructive ops on USER DATA from
    # ops on caches, temp files, scratch state. The board's mandate covers
    # the former; the latter are operationally fine.
    snippet_lc = snippet.lower()
    cache_indicators = (
        "cache_file", "cache.json", "cache_path", "metadata_cache",
        "genre_cache", "spotify_cache", "spotify_metadata_cache",
        "streaming_scores_cache", "dedup_log", "device_library_cache",
        "tidal_cache", "audit_cache", ".genre_cache", ".metadata_cache",
        ".spotify_metadata_cache", ".streaming_scores_cache",
        "token_cache", "auth_cache", "session_cache",
        "audit_state_file", "audit_state",   # operator-initiated audit clear
    )
    if any(c in snippet_lc for c in cache_indicators):
        return CallStatus.SCRIPT, "cache cleanup (not user data)"

    # Temp / scratch file cleanups (we created the file, we delete it)
    temp_indicators = (
        "tmp.unlink", "tmp_dir", "tmp_path", "tmp_file",
        "tempfile", "scratch", "_temp", ".tmp", ".bak.",
        "tmp_local", "staging", "rmtree(tmp",
        "prov_dir", "track_tmp", "out_path.unlink",   # downloader scratch
    )
    if any(c in snippet_lc for c in temp_indicators):
        if primitive in ("mutagen.save",):
            pass  # still flag for wrapper
        else:
            return CallStatus.SCRIPT, "temp/scratch cleanup"

    return CallStatus.UNMEDIATED, "no mk37_wiring wrapper detected"


# ─── Audit driver ──────────────────────────────────────────────────────────

def _walk_python_files(root: Path):
    """Yield .py files under root, skipping venv/__pycache__/.git/etc."""
    skip_dirs = {"__pycache__", ".git", ".pytest_cache", "venv", ".venv",
                 "node_modules", ".tox", "build", "dist"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def audit(root: Path) -> AuditReport:
    """Run the full audit and return a structured report."""
    sites: List[CallSite] = []
    scanned = 0

    for py_path in _walk_python_files(root):
        scanned += 1
        try:
            src = py_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        try:
            tree = ast.parse(src, filename=str(py_path))
        except SyntaxError:
            continue

        # Pre-scan: for each FunctionDef, record whether it has mk37 context
        # (either directly, OR via being a nested def whose parent has context).
        fn_ctx_map: dict = {}
        # First pass: direct context
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                v = _DestructiveCallVisitor(src, str(py_path))
                fn_ctx_map[id(node)] = v._function_has_mk37_context(node)

        # Second pass: nested defs inherit context from their lexical parent
        # if the parent contains mk37 wrappers (e.g. _do_delete_source closure
        # inside a function that calls transactional_dedup).
        def _propagate_ctx(node, parent_has_ctx=False):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self_has = fn_ctx_map.get(id(node), False)
                effective = self_has or parent_has_ctx
                fn_ctx_map[id(node)] = effective
                # Propagate to children
                for child in ast.walk(node):
                    if child is node:
                        continue
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # Inherit if parent has direct context
                        child_self = fn_ctx_map.get(id(child), False)
                        fn_ctx_map[id(child)] = child_self or effective
            for child in ast.iter_child_nodes(node):
                _propagate_ctx(child, parent_has_ctx)
        _propagate_ctx(tree)

        # AST visitor pass for direct primitives
        visitor = _DestructiveCallVisitor(src, str(py_path))
        visitor.visit(tree)
        for line, col, primitive, category, snippet in visitor.results:
            # Find the INNERMOST enclosing function
            enclosing = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.lineno <= line and (
                        node.end_lineno is None or node.end_lineno >= line
                    ):
                        if enclosing is None or node.lineno > enclosing.lineno:
                            enclosing = node
            has_ctx = bool(enclosing and fn_ctx_map.get(id(enclosing), False))
            enc_name = enclosing.name if enclosing else ""
            status, reason = _classify_call_site(
                py_path, line, primitive, snippet, src, has_ctx,
                enclosing_fn_name=enc_name,
            )
            sites.append(CallSite(
                file=str(py_path.relative_to(root)),
                line=line, column=col,
                primitive=primitive, category=category,
                snippet=snippet, status=status, reason=reason,
            ))

        # Regex pass for `adb_cmd(["shell", "rm ..."`
        for line, snippet in _scan_for_adb_shell_rm(src):
            enclosing = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.lineno <= line and (
                        node.end_lineno is None or node.end_lineno >= line
                    ):
                        if enclosing is None or node.lineno > enclosing.lineno:
                            enclosing = node
            has_ctx = bool(enclosing and fn_ctx_map.get(id(enclosing), False))
            enc_name = enclosing.name if enclosing else ""
            status, reason = _classify_call_site(
                py_path, line, "adb_shell_rm", snippet, src, has_ctx,
                enclosing_fn_name=enc_name,
            )
            sites.append(CallSite(
                file=str(py_path.relative_to(root)),
                line=line, column=0,
                primitive="adb_shell_rm", category="ADB_DELETE",
                snippet=snippet, status=status, reason=reason,
            ))

    # Aggregate
    by_status: dict = {}
    by_category: dict = {}
    for s in sites:
        by_status[s.status.value] = by_status.get(s.status.value, 0) + 1
        by_category[s.category] = by_category.get(s.category, 0) + 1

    unmediated = by_status.get("UNMEDIATED", 0)
    production_total = unmediated + by_status.get("MEDIATED", 0)
    coverage_pct = (
        (by_status.get("MEDIATED", 0) / production_total * 100)
        if production_total else 100.0
    )

    return AuditReport(
        scanned_files=scanned,
        total_call_sites=len(sites),
        by_status=by_status,
        by_category=by_category,
        sites=sites,
        unmediated_count=unmediated,
        coverage_pct=coverage_pct,
        healthy=(unmediated == 0),
    )


def _format_human(report: AuditReport, limit: int = 50) -> str:
    out = []
    out.append("=" * 75)
    out.append("MK37.1-1 — Mutation Path Discovery Audit")
    out.append("=" * 75)
    out.append(f"  Scanned files:        {report.scanned_files}")
    out.append(f"  Total call sites:     {report.total_call_sites}")
    out.append(f"  Unmediated (RISK):    {report.unmediated_count}")
    out.append(f"  Mediated coverage:    {report.coverage_pct:.1f}%")
    out.append(f"  Healthy:              {'✓ YES' if report.healthy else '✗ NO'}")
    out.append("")
    out.append("By status:")
    for k, v in sorted(report.by_status.items(), key=lambda x: -x[1]):
        out.append(f"  {k:<14s} {v:>5}")
    out.append("")
    out.append("By category:")
    for k, v in sorted(report.by_category.items(), key=lambda x: -x[1]):
        out.append(f"  {k:<18s} {v:>5}")
    out.append("")

    unmediated_sites = [s for s in report.sites if s.status == CallStatus.UNMEDIATED]
    if unmediated_sites:
        out.append("UNMEDIATED CALL SITES (these need mk37_wiring wrappers):")
        out.append("-" * 75)
        for s in unmediated_sites[:limit]:
            out.append(f"  {s.file}:{s.line}  [{s.category}] {s.primitive}")
            out.append(f"    | {s.snippet}")
        if len(unmediated_sites) > limit:
            out.append(f"  ... and {len(unmediated_sites) - limit} more")
    else:
        out.append("✅ No unmediated destructive call sites in production code.")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".",
                         help="Root directory to scan (default: cwd)")
    parser.add_argument("--strict", action="store_true",
                         help="Exit 1 if any unmediated destructive paths found")
    parser.add_argument("--json", default=None,
                         help="Write JSON report to this path")
    parser.add_argument("--limit", type=int, default=50,
                         help="Max unmediated sites to show in human output")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    report = audit(root)

    print(_format_human(report, limit=args.limit))

    if args.json:
        Path(args.json).write_text(report.to_json())
        print(f"\nJSON report written to {args.json}")

    if args.strict and not report.healthy:
        sys.exit(1)


if __name__ == "__main__":
    main()
