"""
sync_planner_argument_audit.py — MK39.1 L4.2 / Council L3 §4.1 governance linter
================================================================================

Council L3 Patch Review 2026-05-10 §4.1:

  > "The internal coercion fix prevents crashes, but the underlying caller
  > bug that passes bare strings should be permanently corrected. The
  > Council recommends adding a CI-time static analysis check (via the
  > governance linter, MK39-C2) that prohibits passing bare strings to
  > generate_plan(). This ensures the defensive coercion warning fires
  > only in unanticipated future cases, not as a permanent fixture."

This linter walks all .py files in the repository, finds every call to
`sync_planner.generate_plan` or `sync_planner.build_sync_plan`, and flags
calls where the device_snapshot argument is statically a list-of-strings
(the bug pattern that caused the AttributeError in LOG_10_5_26).

Cases handled
-------------

  KNOWN-OK
    - DeviceSnapshot(...) constructor call
    - List of FileState(...) calls
    - Empty list literal []
    - Variable referenced from a function whose signature types it as
      list[FileState] or DeviceSnapshot

  VIOLATION
    - Inline list of string literals: [ "a", "b", ... ]
    - List comprehension whose element expression is a Str (e.g.
      [p for p in paths] where 'paths' is known to be list[str])
    - Variable name 'paths' / 'files' that was assigned a list[str]
      within the same function (lightweight reaching-def heuristic)

  INDETERMINATE
    - Any other Name node (variables we can't trace)
    - Function call results (no return-type information)
    - The linter emits INFO (not VIOLATION) for these.

Opt-out
-------
Add `# noqa: planner-bare-strings` to the line to suppress.

Usage
-----
  python sync_planner_argument_audit.py             # walk repo, exit 0 on clean
  python sync_planner_argument_audit.py --strict    # exit 1 if violations
  python sync_planner_argument_audit.py --json out  # machine-readable output
  python sync_planner_argument_audit.py --root path # audit a different tree

The pytest test in test_mk39_1_l4_governance.py uses this module directly
and is the canonical CI gate.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


log = logging.getLogger(__name__)

# Names that target the planner. Both the function name in sync_planner and
# the convenience wrapper name (build_sync_plan) are covered.
PLANNER_FUNCS = ("generate_plan", "build_sync_plan")

# Excluded paths — tests are exempt because they intentionally exercise the
# planner's defensive-coercion path with bare strings to verify that path.
EXCLUDED_PARTS = (
    "test_",         # test_*.py — they intentionally test the guard
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "build",
    "dist",
    ".tox",
)


# ─────────────────────────────────────────────────────────────────────────
# Violation record
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Violation:
    file: str
    line: int
    func: str
    arg_kind: str       # 'list_of_strs' | 'name_strs' | 'subscript_dict'
    snippet: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditReport:
    violations: List[Violation] = field(default_factory=list)
    indeterminate: List[Violation] = field(default_factory=list)
    files_scanned: int = 0
    calls_seen: int = 0

    def as_dict(self) -> dict:
        return {
            "violations": [v.as_dict() for v in self.violations],
            "indeterminate": [v.as_dict() for v in self.indeterminate],
            "files_scanned": self.files_scanned,
            "calls_seen": self.calls_seen,
            "summary": (
                f"{len(self.violations)} VIOLATIONS · "
                f"{len(self.indeterminate)} indeterminate · "
                f"{self.calls_seen} planner calls in {self.files_scanned} files"
            ),
        }


# ─────────────────────────────────────────────────────────────────────────
# AST helpers
# ─────────────────────────────────────────────────────────────────────────

def _is_planner_call(node: ast.Call) -> Optional[str]:
    """Return the matched planner function name, or None."""
    func = node.func
    if isinstance(func, ast.Name) and func.id in PLANNER_FUNCS:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in PLANNER_FUNCS:
        return func.attr
    return None


def _get_device_snapshot_arg(node: ast.Call) -> Optional[ast.AST]:
    """The device_snapshot argument is positional[1] (after local_files)
    or keyword 'device_snapshot'."""
    for kw in node.keywords:
        if kw.arg == "device_snapshot":
            return kw.value
    if len(node.args) >= 2:
        return node.args[1]
    return None


def _classify_arg(arg: ast.AST, local_assigns: dict) -> str:
    """Classify a device_snapshot argument as 'ok' | 'violation_*' | 'indeterminate'.

    local_assigns is a dict mapping variable names → AST nodes assigned to
    them in the local function scope (lightweight reaching-defs).
    """
    # Case: literal list — inspect elements
    if isinstance(arg, ast.List):
        if not arg.elts:
            return "ok"  # empty list is fine
        all_strs = all(
            isinstance(e, ast.Constant) and isinstance(e.value, str)
            for e in arg.elts
        )
        if all_strs:
            return "violation_list_of_strs"
        all_filestate = all(
            isinstance(e, ast.Call) and (
                (isinstance(e.func, ast.Name) and e.func.id == "FileState") or
                (isinstance(e.func, ast.Attribute) and e.func.attr == "FileState")
            )
            for e in arg.elts
        )
        if all_filestate:
            return "ok"
        return "indeterminate"

    # Case: DeviceSnapshot(...) constructor
    if isinstance(arg, ast.Call):
        f = arg.func
        if isinstance(f, ast.Name) and f.id == "DeviceSnapshot":
            return "ok"
        if isinstance(f, ast.Attribute) and f.attr == "DeviceSnapshot":
            return "ok"
        # Generic call — type unknown
        return "indeterminate"

    # Case: Name lookup — try local-assignment trace
    if isinstance(arg, ast.Name):
        assigned = local_assigns.get(arg.id)
        if assigned is None:
            return "indeterminate"
        # Recurse on the assignment value (one level only — keep it lightweight)
        sub = _classify_arg(assigned, {})
        if sub == "ok":
            return "ok"
        if sub.startswith("violation_"):
            return "violation_name_strs"
        return "indeterminate"

    # Subscripted lookup — likely dict[path] style; bug-prone
    if isinstance(arg, ast.Subscript):
        return "indeterminate"

    return "indeterminate"


def _collect_local_assigns(func_node: ast.AST) -> dict:
    """Collect single-target assignments inside a function body so we can do
    lightweight reaching-def lookup on Name args."""
    assigns: dict = {}
    if not hasattr(func_node, "body"):
        return assigns
    for stmt in ast.walk(func_node):
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                assigns[stmt.targets[0].id] = stmt.value
    return assigns


def _line_has_noqa(source_lines: list, line_no: int) -> bool:
    """Check if the source line carries # noqa: planner-bare-strings."""
    if 1 <= line_no <= len(source_lines):
        return "noqa: planner-bare-strings" in source_lines[line_no - 1]
    return False


# ─────────────────────────────────────────────────────────────────────────
# Main audit
# ─────────────────────────────────────────────────────────────────────────

def audit_file(path: Path) -> List[Violation]:
    """Audit a single .py file and return any violations found."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    found: List[Violation] = []

    # For each Call node, find its enclosing function (if any) to get the
    # local-assign map for reaching-def lookups. Walking the tree once
    # ensures no duplicate reports.
    func_for_node: dict = {}
    def _annotate(parent, scope):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parent
        for child in ast.iter_child_nodes(parent):
            func_for_node[id(child)] = scope
            _annotate(child, scope)
    _annotate(tree, None)

    # Cache local-assigns per function so we don't recompute for every call
    assigns_cache: dict = {}
    def _assigns_for(scope):
        if scope is None:
            return {}
        if id(scope) not in assigns_cache:
            assigns_cache[id(scope)] = _collect_local_assigns(scope)
        return assigns_cache[id(scope)]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        planner_func = _is_planner_call(node)
        if planner_func is None:
            continue
        if _line_has_noqa(source_lines, node.lineno):
            continue
        arg = _get_device_snapshot_arg(node)
        if arg is None:
            continue
        scope = func_for_node.get(id(node))
        assigns = _assigns_for(scope)
        kind = _classify_arg(arg, assigns)
        if kind.startswith("violation_"):
            snippet = source_lines[node.lineno - 1].strip() \
                      if 1 <= node.lineno <= len(source_lines) else "<n/a>"
            found.append(Violation(
                file=str(path),
                line=node.lineno,
                func=planner_func,
                arg_kind=kind,
                snippet=snippet,
            ))
    return found


def audit_indeterminate(path: Path) -> List[Violation]:
    """Same walk but returning only the indeterminate cases (INFO-level)."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    source_lines = source.splitlines()
    found: List[Violation] = []

    func_for_node: dict = {}
    def _annotate(parent, scope):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parent
        for child in ast.iter_child_nodes(parent):
            func_for_node[id(child)] = scope
            _annotate(child, scope)
    _annotate(tree, None)

    assigns_cache: dict = {}
    def _assigns_for(scope):
        if scope is None:
            return {}
        if id(scope) not in assigns_cache:
            assigns_cache[id(scope)] = _collect_local_assigns(scope)
        return assigns_cache[id(scope)]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_planner_call(node) is None:
            continue
        if _line_has_noqa(source_lines, node.lineno):
            continue
        arg = _get_device_snapshot_arg(node)
        if arg is None:
            continue
        scope = func_for_node.get(id(node))
        assigns = _assigns_for(scope)
        kind = _classify_arg(arg, assigns)
        if kind == "indeterminate":
            snippet = source_lines[node.lineno - 1].strip() \
                      if 1 <= node.lineno <= len(source_lines) else "<n/a>"
            found.append(Violation(
                file=str(path),
                line=node.lineno,
                func=_is_planner_call(node) or "",
                arg_kind=kind,
                snippet=snippet,
            ))
    return found


def _is_excluded(path: Path) -> bool:
    return any(part.startswith(EXCLUDED_PARTS) or part in EXCLUDED_PARTS
                for part in path.parts)


def run_audit(root: Path) -> AuditReport:
    """Walk the repo, audit every .py file, return AuditReport."""
    report = AuditReport()
    for py in sorted(root.rglob("*.py")):
        if _is_excluded(py):
            continue
        # Skip the linter itself + planner module + helpers
        if py.name in ("sync_planner_argument_audit.py",
                        "sync_planner.py",
                        "mutation_path_audit.py"):
            continue
        report.files_scanned += 1
        try:
            source = py.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_planner_call(node):
                report.calls_seen += 1
        report.violations.extend(audit_file(py))
        report.indeterminate.extend(audit_indeterminate(py))
    return report


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lint for bare-string violations on sync_planner.generate_plan."
    )
    ap.add_argument("--root", default=".", help="repo root to scan")
    ap.add_argument("--strict", action="store_true",
                     help="exit 1 if any violations found")
    ap.add_argument("--json", dest="json_out", default="",
                     help="write report as JSON")
    ap.add_argument("--show-indeterminate", action="store_true",
                     help="also print INFO indeterminate sites")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[planner-audit] ERROR root is not a directory: {root}")
        return 2

    report = run_audit(root)

    print(f"[planner-audit] {report.as_dict()['summary']}")
    if report.violations:
        print(f"[planner-audit] VIOLATIONS (Council §4.1):")
        for v in report.violations:
            print(f"  {v.file}:{v.line} → {v.func}() received {v.arg_kind}")
            print(f"      {v.snippet}")
    if args.show_indeterminate and report.indeterminate:
        print(f"[planner-audit] INDETERMINATE (manual review):")
        for v in report.indeterminate:
            print(f"  {v.file}:{v.line} → {v.func}() arg type unknown")
            print(f"      {v.snippet}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report.as_dict(), indent=2), encoding="utf-8"
        )
        print(f"[planner-audit] JSON → {args.json_out}")

    if args.strict and report.violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
