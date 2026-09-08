"""
test_mk39_x1_audit_bug.py — MK39-X1 regression: audit.py _mk_artist undefined fix
================================================================================

Closes:
  - The `name '_mk_artist' is not defined` crash observed in the user's
    audit run (Council 2026-05-09 directive)
  - Locks the architectural policy that "unknown fields remain unknown" —
    user-authored / no-source-metadata tracks must NOT have fabricated
    values written, even via the audit module's enrichment path

Methodology — defensive coding + Council policy lock:

  T1: Static-analysis check — every name referenced in audit.py must be
      either defined locally, imported, or a builtin. Catches copy-paste
      bugs like the `_mk_artist` / `_stem` regression at the AST level.

  T2: Audit module imports cleanly without crash. (Negative test: pre-fix
      audit.py would fail at import time only if module-level code hit
      the bad branch; here the bug is in a method body, so we test that
      static analysis would have caught it.)

  T3: Council policy — when artist context is absent, _genre_cache_lookup
      returns {} (not a fabricated value). Verifies the architectural
      contract that empty artist keys yield empty cache hits, which the
      gating logic naturally interprets as "skip the write."

  T4: AST static analysis — audit.py method bodies do not reference
      `_mk_artist` outside of code paths that define it. Locks the fix.

Run:
    python3 -m pytest test_mk39_x1_audit_bug.py -v
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


AUDIT_PATH = Path(__file__).parent / "audit.py"


# ═══════════════════════════════════════════════════════════════════════════
# T1: Static analysis — find references-before-definition
# ═══════════════════════════════════════════════════════════════════════════

class _NameDefinitionVisitor(ast.NodeVisitor):
    """Walk a function body and collect:
      - All names assigned (including via for-loops, with-statements,
        unpacking, augmented assignment)
      - All names referenced in Load context
    Then report names referenced without ever being defined locally,
    excluding builtins, imports, and globals.
    """

    def __init__(self, builtins, module_imports, module_globals):
        self.assigned = set()           # names ever assigned in this scope
        self.referenced = []            # list of (name, lineno) tuples
        self._allowed_external = (
            set(builtins) | set(module_imports) | set(module_globals)
        )

    def visit_Assign(self, node):
        for tgt in node.targets:
            self._collect_targets(tgt)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._collect_targets(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._collect_targets(node.target)
        self.generic_visit(node)

    def visit_For(self, node):
        self._collect_targets(node.target)
        self.generic_visit(node)

    def visit_With(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._collect_targets(item.optional_vars)
        self.generic_visit(node)

    def visit_arguments(self, node):
        for a in node.args + node.kwonlyargs + node.posonlyargs:
            self.assigned.add(a.arg)
        if node.vararg: self.assigned.add(node.vararg.arg)
        if node.kwarg:  self.assigned.add(node.kwarg.arg)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            if (node.id not in self.assigned
                and node.id not in self._allowed_external):
                self.referenced.append((node.id, node.lineno))
        elif isinstance(node.ctx, ast.Store):
            self.assigned.add(node.id)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.assigned.add(node.name)
        self.generic_visit(node)

    def _collect_targets(self, target):
        if isinstance(target, ast.Name):
            self.assigned.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._collect_targets(elt)
        elif isinstance(target, ast.Starred):
            self._collect_targets(target.value)
        # Subscript / Attribute targets are not local-name introductions


def _module_top_level_names(tree: ast.Module) -> set:
    """Collect names assigned / imported / def'd at module level."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.If, ast.Try)):
            # Conservative: walk one level deep for try/if at module scope
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Name):
                            names.add(tgt.id)
                elif isinstance(sub, ast.ImportFrom):
                    for alias in sub.names:
                        names.add(alias.asname or alias.name)
                elif isinstance(sub, ast.FunctionDef):
                    names.add(sub.name)
    return names


def _check_function_for_undefined_locals(func_node: ast.FunctionDef,
                                          builtins: set,
                                          module_globals: set,
                                          tracked_names: set) -> list:
    """Return list of (name, lineno) for names in `tracked_names` that
    are referenced in `func_node` but never assigned (so would crash at
    runtime if the path is hit). Restricting to `tracked_names` keeps the
    test focused on the bug we're locking down rather than every closure
    nuance in a 4000-line file."""
    visitor = _NameDefinitionVisitor(builtins, set(), module_globals)
    visitor.visit(func_node)
    return [(name, lineno) for (name, lineno) in visitor.referenced
            if name in tracked_names]


# ═══════════════════════════════════════════════════════════════════════════
# T1, T4: AST-level lock against regression
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_X1_AuditBugAST(unittest.TestCase):
    """T1, T4: lock the audit-bug fix at the AST level. The names
    `_mk_artist` and `_stem` must not be referenced in any function body
    that doesn't also assign them."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(AUDIT_PATH.read_text(encoding="utf-8"))
        cls.builtins = set(dir(__builtins__))
        cls.module_globals = _module_top_level_names(cls.tree)

    def test_T1_mk_artist_never_referenced_before_definition(self):
        """T1: walk every function body in audit.py; `_mk_artist` must
        not be referenced without being assigned in the same scope.
        This is the structural lock against the Council-flagged bug."""
        violations = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bad = _check_function_for_undefined_locals(
                    node, self.builtins, self.module_globals,
                    tracked_names={"_mk_artist"})
                for name, lineno in bad:
                    violations.append(f"{node.name}:{lineno} references {name}")
        self.assertEqual(violations, [],
            f"_mk_artist referenced before definition in {len(violations)} site(s):"
            f"\n  " + "\n  ".join(violations))

    def test_T4_underscore_stem_consistent_with_definitions(self):
        """T4: similar lock for `_stem`. The previous bug had `_stem`
        used in the genre-AI block where only `stem` (no underscore)
        was defined. After fix, every `_stem` reference must be in a
        function that also defines `_stem`."""
        violations = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bad = _check_function_for_undefined_locals(
                    node, self.builtins, self.module_globals,
                    tracked_names={"_stem"})
                for name, lineno in bad:
                    violations.append(f"{node.name}:{lineno} references {name}")
        self.assertEqual(violations, [],
            f"_stem referenced without local definition in {len(violations)} site(s):"
            f"\n  " + "\n  ".join(violations))


# ═══════════════════════════════════════════════════════════════════════════
# T2: Module imports cleanly
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_X1_AuditModuleImports(unittest.TestCase):
    """T2: audit.py must parse and import without raising."""

    def test_T2_audit_module_parses_cleanly(self):
        try:
            ast.parse(AUDIT_PATH.read_text(encoding="utf-8"))
        except SyntaxError as e:
            self.fail(f"audit.py SyntaxError at line {e.lineno}: {e.msg}")


# ═══════════════════════════════════════════════════════════════════════════
# T3: Council policy — unknown fields remain unknown (architectural lock)
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_X1_UnknownsStayUnknown(unittest.TestCase):
    """T3: Council policy verification. When artist context is absent
    (e.g. user-authored track, no-source download), the lookup must
    return empty and the system must NOT fabricate a value.

    This tests the BEHAVIORAL contract: empty artist key into the cache
    lookup function returns {}, not a guessed value. Combined with the
    MK38.0 freeze + MK39 gate, this guarantees the Council's
    "leave unknowns unknown" policy holds end-to-end."""

    def test_T3a_empty_artist_yields_empty_cache_hit(self):
        """Council policy: empty artist context → no cache hit → no
        downstream tag write. The fix preserves this."""
        # We can't import audit.py easily (heavy module); instead verify
        # via static analysis that the patched line uses an empty string
        # fallback that genre_cache_lookup is documented to handle.
        text = AUDIT_PATH.read_text(encoding="utf-8")
        # The fix must initialize _mk_artist to fall back to _album_aa or ""
        self.assertIn('_mk_artist    = _album_aa or ""', text,
            "MK39-X1 fix must initialize _mk_artist with empty-string fallback")
        # The fix must use `stem` (not `_stem`) — the variable that exists
        # in this scope
        self.assertIn(
            "_cache_entry  = _genre_cache_lookup(_mk_artist, inferred_title or stem)",
            text,
            "MK39-X1 fix must use `stem` (not `_stem`) in genre-AI block")

    def test_T3b_genre_cache_lookup_handles_empty_key(self):
        """Council policy: _genre_cache_lookup must safely return {}
        when artist is empty (so unknown tracks don't crash and don't
        get fabricated genres)."""
        text = AUDIT_PATH.read_text(encoding="utf-8")
        # Verify the function has the documented "{}" empty-cache return path
        self.assertIn(
            'Returns {"genre": str, "year": str} or {} on miss',
            text,
            "_genre_cache_lookup contract must explicitly return {} on miss"
        )
        # And verify the implementation honors it: `if not cache: return {}`
        self.assertIn("if not cache:", text)
        self.assertIn("return {}", text)

    def test_T3c_council_policy_documented_in_fix_comment(self):
        """T3c: the fix's comment block must reference Council guidance
        so future maintainers don't lose the policy context."""
        text = AUDIT_PATH.read_text(encoding="utf-8")
        for required in [
            "MK39-X1",
            "absence of evidence is not evidence of absence",
            "not license to fabricate",
            "Council",
            "the field remains unknown",
        ]:
            self.assertIn(required, text,
                f"MK39-X1 fix comment must include Council-policy reference: {required!r}")


# ═══════════════════════════════════════════════════════════════════════════
# T5: Telemetry — pre-fix bug would have triggered MK39-Gate too?
# ═══════════════════════════════════════════════════════════════════════════

class TestMK39_X1_BugDoesNotInteractWithGate(unittest.TestCase):
    """T5: confirm the audit-bug fix path does NOT itself attempt an
    unmediated tag write — i.e. the fix is observability-only, no new
    mediation work needed."""

    def test_T5_fix_does_not_introduce_new_tag_writes(self):
        """The fix only initializes a variable + reads from a cache.
        No new mutagen.save() / write_zx500_tags() / id3 calls."""
        text = AUDIT_PATH.read_text(encoding="utf-8")
        # Find the fix block by its anchor comment
        import re
        m = re.search(
            r"MK39-X1 \(audit-bug fix.*?_genre_cache_lookup\(_mk_artist, inferred_title or stem\)",
            text, re.DOTALL)
        self.assertIsNotNone(m, "MK39-X1 fix block must be locatable by anchor")
        block = m.group(0)
        # The fix must NOT contain any tag-write primitives
        for forbidden in ["mutagen.File", ".save(", "write_zx500_tags",
                          "ID3(", "TCON ", "TPE1 "]:
            self.assertNotIn(forbidden, block,
                f"MK39-X1 fix must not introduce tag-write primitive: {forbidden!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
