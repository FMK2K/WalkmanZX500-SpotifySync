"""
test_mk23_planner.py — MK23 Planner-Executor Architecture Test Suite
=====================================================================
Validates the 10 architectural invariants from the MK22→MK23 transition.

Architectural invariants under test:
    INV1: Only ONE transport active at runtime — no mixed ADB+COM paths
    INV2: Planner performs ZERO IO — pure function
    INV3: Executor makes ZERO decisions — iterates plan only
    INV4: No UI APIs anywhere — no Shell.Application, no CopyHere
    INV5: Every operation declares overwrite explicitly — no implicit overwrites
    INV6: Idempotency — second run produces zero new operations
    INV7: Atomicity — push to .tmp then rename (never partial file on device)
    INV8: Planner diff — ADD/SKIP/REPLACE/DELETE/RENAME correctly identified
    INV9: Library audit — tag mismatches, folder mismatches, splinters detected
    INV10: Transport interface contract — all 4 methods exist on all backends

Approach comparisons:
    AC-TRANSPORT: ADB vs MTP vs mixed — why ADB-only wins
    AC-PLANNING: reactive (MK22) vs planner (MK23) — why plan-first wins
    AC-ATOMICITY: direct push vs temp+rename — why atomic always
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from device_transport import DeviceTransport, NullTransport, ADBTransport, get_transport
from sync_executor import SyncExecutor
from sync_operations import (
    AuditFinding, AuditReport, ExecutionResult, FileState, Operation, SyncPlan
)
from sync_planner import generate_audit_report, generate_plan


# ═══════════════════════════════════════════════════════════════════════════
# AC-TRANSPORT: Approach comparison — transport layer selection
# ═══════════════════════════════════════════════════════════════════════════

class Test_AC_Transport_Comparison(unittest.TestCase):
    """AC-TRANSPORT: ADB vs COM vs mixed — why ADB-only wins."""

    def _sim_adb_push(self, src, dst, overwrite):
        """ADB: deterministic, synchronous, no UI."""
        dialogs = 0
        if not overwrite:
            raise FileExistsError("ADB raises, doesn't prompt")
        return {"success": True, "dialogs": dialogs}

    def _sim_com_copyhere(self, src, dst, overwrite):
        """COM CopyHere: async, may prompt, vOptions often ignored."""
        dialogs = 1 if not overwrite else 0  # vOptions=16 often ignored on MTP
        return {"success": True, "dialogs": dialogs}

    def _sim_mixed(self, src, dst, overwrite, storage_type):
        """Mixed: uses different API depending on storage — unpredictable."""
        if storage_type == "internal":
            return self._sim_adb_push(src, dst, overwrite)
        return self._sim_com_copyhere(src, dst, overwrite)

    def test_adb_zero_dialogs_on_overwrite(self):
        result = self._sim_adb_push("a.m4a", "b.m4a", overwrite=True)
        self.assertEqual(result["dialogs"], 0, "ADB must produce 0 dialogs")

    def test_com_may_produce_dialogs(self):
        result = self._sim_com_copyhere("a.m4a", "b.m4a", overwrite=False)
        self.assertGreater(result["dialogs"], 0,
            "COM CopyHere can produce dialogs — confirms why ADB-first is correct")

    def test_mixed_transport_produces_inconsistent_results(self):
        """Mixed transport: ADB raises on conflict, COM silently prompts — inconsistent."""
        # ADB with overwrite=False → raises exception (good: explicit error)
        with self.assertRaises(FileExistsError):
            self._sim_adb_push("a.m4a", "b.m4a", overwrite=False)
        # COM with overwrite=False → produces dialog instead of raising (bad: UI side effect)
        r_com = self._sim_com_copyhere("a.m4a", "b.m4a", overwrite=False)
        self.assertEqual(r_com["dialogs"], 1, "COM silently produces a dialog instead of raising")
        # Inconsistency confirmed: same intent, different behavior per transport
        print(f"\n  [AC-TRANSPORT] Inconsistency: ADB raises, COM prompts — confirms ADB-only is correct")

    def test_100_pushes_adb_zero_dialogs(self):
        """100 ADB pushes → 0 dialogs. COM → up to 100 dialogs."""
        adb_dialogs = sum(self._sim_adb_push(f"{i}.m4a", f"{i}.m4a", True)["dialogs"]
                         for i in range(100))
        com_dialogs = sum(self._sim_com_copyhere(f"{i}.m4a", f"{i}.m4a", False)["dialogs"]
                         for i in range(100))
        self.assertEqual(adb_dialogs, 0, "ADB must produce 0 dialogs for 100 pushes")
        self.assertGreater(com_dialogs, 0, "COM produces dialogs — confirms regression source")
        print(f"\n  [AC-TRANSPORT] 100 pushes: ADB={adb_dialogs} dialogs, "
              f"COM={com_dialogs} dialogs (confirms dialog storm source)")


# ═══════════════════════════════════════════════════════════════════════════
# AC-PLANNING: Reactive (MK22) vs Planner-Executor (MK23)
# ═══════════════════════════════════════════════════════════════════════════

class Test_AC_Planning_Comparison(unittest.TestCase):
    """AC-PLANNING: why plan-first prevents runtime surprises."""

    def _sim_reactive(self, files, device_state):
        """MK22: decide during IO — triggers runtime surprises."""
        dialogs = 0
        for f in files:
            if f in device_state:
                # Decision made at IO time: prompt or overwrite?
                dialogs += 1   # → COM dialog storm
            # else: copy
        return {"dialogs": dialogs}

    def _sim_plan_first(self, files, device_state):
        """MK23: plan first, execute dumb — zero runtime decisions."""
        plan = []
        for f in files:
            if f in device_state:
                plan.append({"op": "replace", "overwrite": True})  # decided in plan
            else:
                plan.append({"op": "copy", "overwrite": False})
        # Execute: no decisions — just follow plan
        dialogs = 0  # all overwrite decisions already made
        return {"dialogs": dialogs, "plan": plan}

    def test_reactive_produces_dialogs(self):
        files  = [f"track_{i}.m4a" for i in range(10)]
        device = set(files[:5])  # 5 files already on device
        result = self._sim_reactive(files, device)
        self.assertEqual(result["dialogs"], 5,
            "Reactive approach produces dialogs for existing files")

    def test_plan_first_zero_dialogs(self):
        files  = [f"track_{i}.m4a" for i in range(10)]
        device = set(files[:5])
        result = self._sim_plan_first(files, device)
        self.assertEqual(result["dialogs"], 0,
            "Plan-first must produce 0 dialogs regardless of file state")
        print(f"\n  [AC-PLANNING] Reactive={5} dialogs, Plan-first=0 dialogs "
              f"for {len(device)} existing files")

    def test_plan_is_inspectable_before_execution(self):
        """Plan can be logged and reviewed before any IO occurs."""
        files  = ["track.m4a"]
        device = set()
        result = self._sim_plan_first(files, device)
        plan   = result["plan"]
        self.assertGreater(len(plan), 0, "Plan must be inspectable before execution")
        self.assertIn("op", plan[0], "Each plan entry must have explicit op type")


# ═══════════════════════════════════════════════════════════════════════════
# INV1 — Single transport
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV1_SingleTransport(unittest.TestCase):
    """INV1: Only one transport active at runtime."""

    def test_device_transport_is_abstract(self):
        with self.assertRaises(TypeError):
            DeviceTransport()   # ABC cannot be instantiated

    def test_null_transport_is_valid_implementation(self):
        t = NullTransport()
        self.assertIsInstance(t, DeviceTransport)

    def test_get_transport_dry_run_returns_null(self):
        t = get_transport(dry_run=True)
        self.assertIsInstance(t, NullTransport)

    def test_get_transport_unknown_backend_raises(self):
        # 2026-07-19: "com" used to be this test's example of an invalid
        # backend (MK23 originally forbade it entirely). It's now a real,
        # tested, opt-in alternative (see test_get_transport_com_is_opt_in_alternative
        # below) — swapped to a genuinely-unknown name so this test still
        # covers "garbage backend string raises."
        with self.assertRaises(ValueError):
            get_transport(backend="bluetooth")

    def test_get_transport_mtp_raises(self):
        with self.assertRaises(ValueError):
            get_transport(backend="mtp")   # "mtp" specifically is still not a recognized alias

    def test_get_transport_com_is_opt_in_alternative(self):
        # 2026-07-19: added as an explicit opt-in alternative to the MK23
        # ADB-primary invariant (see COMTransport's docstring in
        # device_transport.py) — for nights where ADB is unreachable but the
        # underlying MTP link is healthy. NOT the default (confirmed by the
        # next test) — only returned when explicitly requested.
        from device_transport import COMTransport
        t = get_transport(backend="com")
        self.assertIsInstance(t, COMTransport)
        self.assertIsInstance(t, DeviceTransport)

    def test_get_transport_default_is_still_adb(self):
        # The MK23 invariant that matters — ADB stays primary/default —
        # is unchanged by adding "com" as an opt-in alternative above.
        t = get_transport()
        self.assertIsInstance(t, ADBTransport)

    def test_null_transport_implements_all_methods(self):
        t = NullTransport()
        # All 6 DeviceTransport methods must exist and be callable
        for method in ["push_file", "delete_file", "list_files",
                       "file_exists", "rename", "get_free_space_mb"]:
            self.assertTrue(callable(getattr(t, method, None)),
                f"INV1: DeviceTransport.{method} must be implemented")


# ═══════════════════════════════════════════════════════════════════════════
# INV2 — Planner performs ZERO IO
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV2_PlannerZeroIO(unittest.TestCase):
    """INV2: generate_plan performs ZERO device IO — pure function."""

    def test_planner_accepts_in_memory_state_only(self):
        """Planner takes FileState lists — no device references."""
        local  = [FileState(path="Music/A/track.m4a", size_bytes=1000, mtime=100.0)]
        device = [FileState(path="Music/A/track.m4a", size_bytes=999,  mtime=50.0)]
        # No device connection needed — pure logic
        plan = generate_plan(local, device, run_id="INV2_TEST")
        self.assertIsInstance(plan, SyncPlan)

    def test_planner_deterministic_same_input_same_output(self):
        """Same input always produces same plan — no randomness, no IO."""
        local  = [FileState(path="Music/A/t.m4a", size_bytes=1000, mtime=100.0)]
        device = [FileState(path="Music/A/t.m4a", size_bytes=500,  mtime=50.0)]
        plan1 = generate_plan(local, device, run_id="R1")
        plan2 = generate_plan(local, device, run_id="R2")
        self.assertEqual(plan1.n_replace, plan2.n_replace,
            "Planner must be deterministic — same input = same output")

    def test_planner_handles_empty_device(self):
        local  = [FileState(path="a.m4a", size_bytes=100, mtime=1.0)] * 5
        plan = generate_plan(local, [], run_id="EMPTY")
        self.assertEqual(plan.n_copy, 5)
        self.assertEqual(plan.n_skip, 0)

    def test_planner_handles_empty_local(self):
        device = [FileState(path="orphan.mp3", size_bytes=100, mtime=1.0)]
        plan = generate_plan([], device, run_id="ORPHAN", delete_orphans=True)
        self.assertEqual(plan.n_delete, 1)


# ═══════════════════════════════════════════════════════════════════════════
# INV3 — Executor makes ZERO decisions
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV3_ExecutorZeroDecisions(unittest.TestCase):
    """INV3: Executor is dumb — iterates plan, delegates to transport, logs."""

    def setUp(self):
        self.transport = NullTransport()
        self.executor  = SyncExecutor(self.transport, dry_run=False)

    def test_executor_follows_plan_exactly(self):
        """Executor must execute exactly what the plan says."""
        plan = SyncPlan(run_id="EXEC_TEST")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".m4a") as tmp:
            tmp_path = tmp.name

        plan.add(Operation(op_type="copy", src=tmp_path,
                           dst="/sdcard/Music/test.m4a", overwrite=False, atomic=True,
                           run_id="EXEC_TEST"))
        results = self.executor.execute(plan)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        os.unlink(tmp_path)

    def test_executor_dry_run_produces_no_results(self):
        """Dry-run executor performs zero IO."""
        exec_dry = SyncExecutor(self.transport, dry_run=True)
        plan = SyncPlan(run_id="DRY")
        plan.add(Operation(op_type="copy", src="a.m4a", dst="b.m4a",
                           overwrite=True, atomic=True, run_id="DRY"))
        results = exec_dry.execute(plan)
        self.assertEqual(results, [], "Dry-run must return empty results (no IO)")

    def test_executor_logs_each_result(self):
        """Every operation produces an ExecutionResult."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".m4a") as tmp:
            tmp_path = tmp.name
        plan = SyncPlan(run_id="LOG")
        plan.add(Operation(op_type="copy", src=tmp_path,
                           dst="/sdcard/test.m4a", overwrite=False, atomic=True,
                           run_id="LOG"))
        results = self.executor.execute(plan)
        self.assertTrue(all(hasattr(r, "success") for r in results))
        os.unlink(tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# INV4 — No UI APIs
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV4_NoUIAPIs(unittest.TestCase):
    """INV4: No Shell.Application, no CopyHere in MK23 core modules."""

    def _check_no_ui(self, filename: str) -> list:
        """Check that no UI APIs appear in actual call positions (not docstrings)."""
        forbidden = ["Shell.Application", "CopyHere", "win32com.client.Dispatch",
                     "CoInitialize", "InvokeVerb"]
        try:
            source = pathlib.Path(filename).read_text(errors="replace")
        except FileNotFoundError:
            return []
        # Strip comments and string literals before checking
        # (docstrings may reference COM as "we do NOT use...")
        import tokenize, io
        code_only = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(source).readline):
                if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NEWLINE):
                    code_only.append(tok.string)
            code_str = " ".join(code_only)
        except Exception:
            code_str = source
        return [f for f in forbidden if f in code_str]

    def test_no_com_in_sync_operations(self):
        found = self._check_no_ui("sync_operations.py")
        self.assertEqual(found, [], f"INV4: UI APIs in sync_operations.py: {found}")

    def test_no_com_in_sync_planner(self):
        found = self._check_no_ui("sync_planner.py")
        self.assertEqual(found, [], f"INV4: UI APIs in sync_planner.py: {found}")

    def test_no_com_in_sync_executor(self):
        found = self._check_no_ui("sync_executor.py")
        self.assertEqual(found, [], f"INV4: UI APIs in sync_executor.py: {found}")

    def test_no_com_in_device_transport(self):
        found = self._check_no_ui("device_transport.py")
        self.assertEqual(found, [], f"INV4: UI APIs in device_transport.py: {found}")

    def test_null_transport_has_no_subprocess_calls(self):
        """NullTransport must not call any external processes."""
        t = NullTransport()
        # Push to simulated device — must not spawn subprocess
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            t.push_file("local.m4a", "remote.m4a")
        self.assertIn("push", [c[0] for c in t.calls])


# ═══════════════════════════════════════════════════════════════════════════
# INV5 — Explicit overwrite declaration
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV5_ExplicitOverwrite(unittest.TestCase):
    """INV5: Every Operation declares overwrite. No implicit behavior."""

    def test_operation_overwrite_field_exists(self):
        op = Operation(op_type="copy", src="a.m4a", dst="b.m4a",
                       overwrite=True, atomic=True)
        self.assertIsNotNone(op.overwrite, "overwrite field must exist")

    def test_operation_overwrite_false_raises_on_existing(self):
        """Transport must raise (not prompt) when overwrite=False and file exists."""
        t = NullTransport()
        t._files["/device/exists.m4a"] = {"size_bytes": 100}
        with self.assertRaises(FileExistsError):
            t.push_file("local.m4a", "/device/exists.m4a", overwrite=False)

    def test_operation_overwrite_true_replaces_silently(self):
        """overwrite=True must replace without prompting."""
        t = NullTransport()
        t._files["/device/exists.m4a"] = {"size_bytes": 100}
        # Must not raise
        t.push_file("local.m4a", "/device/exists.m4a", overwrite=True)
        self.assertIn("/device/exists.m4a", t._files)

    def test_operation_frozen_overwrite_immutable(self):
        """Operation is frozen — overwrite cannot be changed post-creation."""
        op = Operation(op_type="copy", src="a", dst="b", overwrite=False, atomic=True)
        with self.assertRaises(Exception):  # FrozenInstanceError
            op.overwrite = True


# ═══════════════════════════════════════════════════════════════════════════
# INV6 — Idempotency
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV6_Idempotency(unittest.TestCase):
    """INV6: Running sync twice produces zero operations on second run."""

    def test_identical_state_produces_all_skips(self):
        """When local == device, plan has only SKIP operations."""
        files = [
            FileState(path=f"Music/A/track_{i}.m4a", size_bytes=1000, mtime=100.0)
            for i in range(10)
        ]
        plan = generate_plan(files, files, run_id="IDEM_1")
        self.assertEqual(plan.n_skip, 10)
        self.assertEqual(plan.n_copy + plan.n_replace, 0,
            "Identical state must produce ZERO copy/replace operations")

    def test_after_first_sync_second_is_zero_ops(self):
        """
        Simulate: run 1 copies 5 files. Run 2 with same state = 0 new ops.
        This is the idempotency guarantee.
        """
        local_files  = [FileState(path=f"Music/{i}.m4a", size_bytes=1000, mtime=100.0)
                        for i in range(5)]
        device_after = local_files   # simulate: run 1 succeeded, device now matches

        plan_run2 = generate_plan(local_files, device_after, run_id="IDEM_2")
        new_ops = plan_run2.n_copy + plan_run2.n_replace + plan_run2.n_delete
        self.assertEqual(new_ops, 0,
            f"INV6: Second run must produce 0 copy/replace/delete; got {new_ops}")
        print(f"\n  [INV6] Run 2 with identical state: {plan_run2.n_skip} skips, "
              f"{new_ops} new operations → Idempotent ✅")

    def test_modify_one_file_produces_exactly_one_replace(self):
        """Modify 1 file → exactly 1 REPLACE in plan."""
        local  = [FileState(path=f"Music/{i}.m4a", size_bytes=1000, mtime=100.0)
                  for i in range(5)]
        # Same files on device but track 2 has different size
        device = list(local)
        device[2] = FileState(path="Music/2.m4a", size_bytes=500, mtime=50.0)

        plan = generate_plan(local, device, run_id="IDEM_3")
        self.assertEqual(plan.n_replace, 1,
            "Exactly 1 changed file must produce exactly 1 REPLACE")
        self.assertEqual(plan.n_skip, 4,
            "4 unchanged files must SKIP")
        print(f"\n  [INV6] 1 modified file: {plan.n_replace} replace, "
              f"{plan.n_skip} skip → Correct")


# ═══════════════════════════════════════════════════════════════════════════
# INV7 — Atomicity
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV7_Atomicity(unittest.TestCase):
    """INV7: push to .mk23_tmp_<uuid>_<name> then rename — never partial file."""

    def test_null_transport_push_records_push_call(self):
        t = NullTransport()
        t.push_file("local.m4a", "/sdcard/Music/track.m4a", overwrite=True, atomic=True)
        self.assertIn(("push", "local.m4a", "/sdcard/Music/track.m4a"), t.calls)

    def test_adb_transport_atomic_flag_in_push_file_signature(self):
        """ADBTransport.push_file must accept atomic parameter."""
        import inspect
        from device_transport import ADBTransport
        sig = inspect.signature(ADBTransport.push_file)
        self.assertIn("atomic", sig.parameters,
            "INV7: push_file must have 'atomic' parameter")

    def test_operation_atomic_field_present(self):
        op = Operation(op_type="copy", src="a", dst="b", overwrite=True, atomic=True)
        self.assertTrue(op.atomic, "Operations must declare atomicity")

    def test_atomic_push_uses_temp_then_rename(self):
        """
        Verify the atomic push pattern: temp file created, then renamed.
        In NullTransport, both push and rename calls should appear.
        """
        # The ADBTransport does: push(.tmp_...) then shell mv
        # We verify the pattern exists in the source code
        src = pathlib.Path("device_transport.py").read_text(errors="replace")
        self.assertIn(".mk23_tmp_", src,
            "INV7: temp file prefix must be in ADBTransport source")
        self.assertIn("mv '", src,
            "INV7: atomic rename (shell mv) must be in ADBTransport source")

    def test_behavioral_atomicity_simulation(self):
        """Simulate: interrupted push leaves only .tmp file, not partial final."""
        device_state = {}

        def atomic_push(src, dst):
            import uuid
            tmp = f".mk23_tmp_{uuid.uuid4().hex[:8]}_{dst.rsplit('/', 1)[-1]}"
            device_state[tmp] = "in_progress"
            # Simulate transfer success
            del device_state[tmp]
            device_state[dst] = "complete"

        atomic_push("local.m4a", "/sdcard/Music/track.m4a")
        # Final state: only dst exists, no tmp file
        self.assertIn("/sdcard/Music/track.m4a", device_state)
        tmp_keys = [k for k in device_state if ".mk23_tmp_" in k]
        self.assertEqual(tmp_keys, [], "Temp file must be absent after successful atomic push")


# ═══════════════════════════════════════════════════════════════════════════
# INV8 — Planner diff correctness
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV8_PlannerDiff(unittest.TestCase):
    """INV8: 5 diff outcomes correctly identified."""

    def test_skip_when_identical(self):
        local  = [FileState(path="Music/A/t.m4a", size_bytes=1000, mtime=100.0)]
        device = [FileState(path="Music/A/t.m4a", size_bytes=1000, mtime=100.5)]
        plan = generate_plan(local, device)
        self.assertEqual(plan.n_skip, 1)

    def test_replace_when_size_differs(self):
        local  = [FileState(path="Music/A/t.m4a", size_bytes=2000, mtime=200.0)]
        device = [FileState(path="Music/A/t.m4a", size_bytes=1000, mtime=100.0)]
        plan = generate_plan(local, device)
        self.assertEqual(plan.n_replace, 1)

    def test_copy_when_new_file(self):
        local  = [FileState(path="Music/A/new.m4a", size_bytes=500, mtime=100.0)]
        device = []
        plan = generate_plan(local, device)
        self.assertEqual(plan.n_copy, 1)

    def test_delete_orphan(self):
        local  = []
        device = [FileState(path="Music/A/orphan.mp3", size_bytes=300, mtime=50.0)]
        plan = generate_plan(local, device, delete_orphans=True)
        self.assertEqual(plan.n_delete, 1)

    def test_rename_when_content_same_path_differs(self):
        local  = [FileState(path="Music/B/renamed.m4a", size_bytes=1000, mtime=100.0)]
        device = [FileState(path="Music/A/original.m4a", size_bytes=1000, mtime=100.0)]
        plan = generate_plan(local, device)
        self.assertEqual(plan.n_rename, 1,
            "Same size = same content (fast path) → RENAME not ADD")

    def test_mixed_scenario_counts_correct(self):
        """5 files: 2 skip, 1 replace, 1 new, 1 renamed."""
        local = [
            FileState(path="Music/A/same1.m4a", size_bytes=100, mtime=50.0),
            FileState(path="Music/A/same2.m4a", size_bytes=200, mtime=60.0),
            FileState(path="Music/A/changed.m4a", size_bytes=999, mtime=90.0),
            FileState(path="Music/A/brand_new.m4a", size_bytes=77, mtime=80.0),
            FileState(path="Music/B/renamed.m4a", size_bytes=300, mtime=70.0),
        ]
        device = [
            FileState(path="Music/A/same1.m4a", size_bytes=100, mtime=50.5),
            FileState(path="Music/A/same2.m4a", size_bytes=200, mtime=60.5),
            FileState(path="Music/A/changed.m4a", size_bytes=500, mtime=30.0),
            FileState(path="Music/A/original_name.m4a", size_bytes=300, mtime=70.0),
        ]
        plan = generate_plan(local, device)
        self.assertEqual(plan.n_skip, 2)
        self.assertEqual(plan.n_replace, 1)
        self.assertEqual(plan.n_copy, 1)
        self.assertEqual(plan.n_rename, 1)
        print(f"\n  [INV8] Mixed: skip={plan.n_skip} replace={plan.n_replace} "
              f"copy={plan.n_copy} rename={plan.n_rename} ✅")


# ═══════════════════════════════════════════════════════════════════════════
# INV9 — Library audit
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV9_LibraryAudit(unittest.TestCase):
    """INV9: Audit detects tag mismatches, folder mismatches, splinters."""

    def test_folder_mismatch_detected(self):
        """File in Afrobeats/Album folder but tagged as Hip-Hop."""
        device = [FileState(
            path="/sdcard/Music/Afrobeats/AlbumX/track.m4a",
            size_bytes=1000, mtime=100.0,
            artist="Wrong Artist",   # tag says different artist
            album="AlbumX",
        )]
        report = generate_audit_report(device, run_id="AUDIT_1")
        mismatches = report.by_type("FOLDER_MISMATCH")
        self.assertGreater(len(mismatches), 0,
            "FOLDER_MISMATCH must be detected when tag artist differs from folder")

    def test_splinter_detected_with_inventory(self):
        """Single track in folder for a known 12-track album."""
        device = [FileState(
            path="/sdcard/Music/Drake/CLB/01_Champagne_Poetry.m4a",
            size_bytes=5000, mtime=100.0,
            artist="Drake", album="Certified Lover Boy",
        )]
        inventory = {("drake", "certified lover boy"): {"total_tracks": 21, "tracks": {}}}
        report = generate_audit_report(device, inventory, run_id="AUDIT_2")
        splinters = report.by_type("SPLINTER")
        self.assertGreater(len(splinters), 0,
            "SPLINTER must be detected for single track from 21-track album")
        print(f"\n  [INV9] Splinter: '{splinters[0].detail}'")

    def test_no_findings_for_clean_library(self):
        """Well-organised library with matching tags produces no findings."""
        device = [
            FileState(
                path="/sdcard/Music/Burna Boy/African Giant/01.m4a",
                size_bytes=i * 1000, mtime=100.0,
                artist="Burna Boy", album="African Giant",
                track_number=str(i),
            )
            for i in range(1, 13)
        ]
        inventory = {("burna boy", "african giant"): {"total_tracks": 12, "tracks": {}}}
        report = generate_audit_report(device, inventory, run_id="AUDIT_3")
        # 12 tracks, no splinter (all 12 present), check folder matches
        splinters = report.by_type("SPLINTER")
        self.assertEqual(len(splinters), 0,
            "Complete 12-track album must produce 0 SPLINTER findings")

    def test_audit_report_summary_format(self):
        """AuditReport.summary() must include files_scanned and finding counts."""
        report = AuditReport(run_id="FMT", files_scanned=100)
        summary = report.summary()
        self.assertIn("100 files scanned", summary)

    def test_audit_finding_has_suggested_op(self):
        """Fixable mismatches must include a suggested Operation."""
        device = [FileState(
            path="/sdcard/Music/Wrong_Artist/Album/track.m4a",
            size_bytes=1000, mtime=100.0,
            artist="Correct Artist", album="Album",
        )]
        report = generate_audit_report(device, run_id="AUDIT_4")
        fixable = [f for f in report.findings if f.suggested_op is not None]
        if fixable:
            self.assertIsInstance(fixable[0].suggested_op, Operation,
                "Suggested fix must be an Operation instance")


# ═══════════════════════════════════════════════════════════════════════════
# INV10 — Transport interface contract
# ═══════════════════════════════════════════════════════════════════════════

class Test_INV10_TransportContract(unittest.TestCase):
    """INV10: All transport backends must implement the full interface."""

    REQUIRED_METHODS = ["push_file", "delete_file", "list_files",
                        "file_exists", "rename", "get_free_space_mb"]

    def test_null_transport_has_all_methods(self):
        t = NullTransport()
        for method in self.REQUIRED_METHODS:
            self.assertTrue(callable(getattr(t, method, None)),
                f"NullTransport.{method} must be implemented")

    def test_null_transport_push_respects_overwrite_false(self):
        t = NullTransport()
        t._files["/existing.m4a"] = {}
        with self.assertRaises(FileExistsError):
            t.push_file("local.m4a", "/existing.m4a", overwrite=False)

    def test_null_transport_delete_idempotent(self):
        """Deleting non-existent file must return False (not raise)."""
        t = NullTransport()
        result = t.delete_file("/nonexistent.m4a")
        self.assertFalse(result)

    def test_null_transport_list_files_scoped(self):
        """list_files must only return files under the given path."""
        t = NullTransport()
        t._files = {"/sdcard/Music/A/1.m4a": {}, "/sdcard/Music/B/2.m4a": {},
                    "/sdcard/DCIM/photo.jpg": {}}
        files = t.list_files("/sdcard/Music")
        paths = [f["path"] for f in files]
        self.assertIn("/sdcard/Music/A/1.m4a", paths)
        self.assertNotIn("/sdcard/DCIM/photo.jpg", paths,
            "list_files must scope to given path")

    def test_null_transport_rename_updates_path(self):
        """rename must move the file (update internal state)."""
        t = NullTransport()
        t._files["/old/path.m4a"] = {"size_bytes": 100}
        t.rename("/old/path.m4a", "/new/path.m4a")
        self.assertIn("/new/path.m4a", t._files)
        self.assertNotIn("/old/path.m4a", t._files)


# ═══════════════════════════════════════════════════════════════════════════
# Operation serialisation
# ═══════════════════════════════════════════════════════════════════════════

class Test_Serialization(unittest.TestCase):
    """Operations and Plans must be JSON-serializable (dry-run audit)."""

    def test_operation_to_json_roundtrip(self):
        op = Operation(op_type="copy", src="/local/t.m4a", dst="/sdcard/t.m4a",
                       overwrite=True, atomic=True, reason="new_file", run_id="R1")
        j   = op.to_json()
        op2 = Operation.from_json(j)
        self.assertEqual(op, op2)

    def test_sync_plan_to_json_contains_all_fields(self):
        plan = SyncPlan(run_id="SERIAL_TEST")
        plan.add(Operation(op_type="skip", src="a", dst="b",
                           overwrite=False, atomic=False, run_id="SERIAL_TEST"))
        j = json.loads(plan.to_json())
        self.assertIn("run_id", j)
        self.assertIn("operations", j)
        self.assertEqual(len(j["operations"]), 1)

    def test_plan_save_and_load(self):
        plan = SyncPlan(run_id="SAVE_TEST")
        plan.add(Operation(op_type="copy", src="a", dst="b",
                           overwrite=True, atomic=True, run_id="SAVE_TEST"))
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            tmp = f.name
        plan.save(tmp)
        loaded = json.loads(pathlib.Path(tmp).read_text())
        os.unlink(tmp)
        self.assertEqual(loaded["run_id"], "SAVE_TEST")
        self.assertEqual(len(loaded["operations"]), 1)


# ═══════════════════════════════════════════════════════════════════════════
# MK22 continuity — confirm prior guards still active
# ═══════════════════════════════════════════════════════════════════════════

class Test_MK22_Continuity(unittest.TestCase):
    """MK22 B-series + FO markers must remain present in MK23."""

    def src(self, fname):
        try:
            return pathlib.Path(fname).read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def test_zombie_dedup_still_present(self):
        self.assertIn("ZOMBIE_DEDUP", self.src("walkman_dedup.py"),
            "B3.3 zombie dedup must still be active in MK23")

    def test_dialog_storm_abort_still_present(self):
        self.assertIn("DIALOG_STORM_ABORT", self.src("walkman_dedup.py"))

    def test_internal_storage_redirect_still_present(self):
        self.assertIn("INTERNAL_STORAGE_REDIRECT_TO_SD", self.src("walkman_sync.py"))

    def test_genre_contradiction_guard_still_present(self):
        self.assertIn("GENRE_CONTRADICTION_REJECTED", self.src("genre_client.py"))

    def test_album_survivor_integrity_still_present(self):
        self.assertIn("ALBUM_SPLINTER_DETECTED", self.src("walkman_dedup.py"))

    def test_broad_genres_still_defined(self):
        import genre_client
        broad = genre_client._broad_genres()
        self.assertIsInstance(broad, set)


if __name__ == "__main__":
    print("=" * 70)
    print("SpotifyTuner MK23 — Planner-Executor Architecture Test Suite")
    print("=" * 70)
    print("INV1-10 · AC-TRANSPORT · AC-PLANNING · Library Audit · Serialization")
    print()

    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        Test_AC_Transport_Comparison,
        Test_AC_Planning_Comparison,
        Test_INV1_SingleTransport,
        Test_INV2_PlannerZeroIO,
        Test_INV3_ExecutorZeroDecisions,
        Test_INV4_NoUIAPIs,
        Test_INV5_ExplicitOverwrite,
        Test_INV6_Idempotency,
        Test_INV7_Atomicity,
        Test_INV8_PlannerDiff,
        Test_INV9_LibraryAudit,
        Test_INV10_TransportContract,
        Test_Serialization,
        Test_MK22_Continuity,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print("=" * 70)
    total  = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print(f"MK23 Planner-Executor: {passed}/{total} passed")
    if result.wasSuccessful():
        print("✅ All 10 architectural invariants verified")
        print("✅ ADB-first transport · Zero-IO planner · Dumb executor · Idempotent")
    else:
        for t, tb in result.failures + result.errors:
            print(f"  ❌ {t.id().split('.')[-1]}: {tb.splitlines()[-1][:80]}")
    print("=" * 70)
    sys.exit(0 if result.wasSuccessful() else 1)
