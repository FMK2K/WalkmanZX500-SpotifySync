"""
test_mediastore_refresh_exception.py — verifies the 2026-07-30 fix in
walkman_tree_gen.py::query_mediastore(): an ADB failure now raises
RuntimeError instead of calling sys.exit().

Live-confirmed bug: SystemExit is a BaseException, not an Exception, so
spiral_orchestrator.py::_run_device_scan()'s `except Exception` wrapper
around this call never caught it -- an ADB-offline failure here killed the
entire cli_runner.py process (exit 1) even after a full audit run had
already completed successfully. No device required -- subprocess.run is
mocked.
"""
from unittest import mock

import walkman_tree_gen as wtg
import spiral_orchestrator as so

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


print("[1] query_mediastore() raises RuntimeError (not SystemExit) on ADB failure")
with mock.patch.object(wtg, "_adb", return_value=mock.Mock(
        returncode=1, stdout="", stderr="error: device offline")):
    try:
        wtg.query_mediastore()
        _check("raised an exception", False)
    except SystemExit:
        _check("did NOT raise SystemExit (the old, uncatchable bug)", False)
    except RuntimeError as e:
        _check("raised RuntimeError", True)
        _check("message includes the adb stderr", "device offline" in str(e))
    except Exception as e:
        _check(f"raised RuntimeError specifically (got {type(e).__name__} instead)", False)

print("\n[2] query_mediastore() returns stdout normally on success")
with mock.patch.object(wtg, "_adb", return_value=mock.Mock(
        returncode=0, stdout="Row: 0 _id=1, title=Foo", stderr="")):
    result = wtg.query_mediastore()
    _check("returns raw stdout", result == "Row: 0 _id=1, title=Foo")

print("\n[3] spiral_orchestrator._run_device_scan()'s except-Exception wrapper "
      "now actually catches the failure (the real bug this fix closes)")
orch = object.__new__(so.SpiralOrchestrator)
orch._log = lambda msg, level="INFO": None
with mock.patch("walkman_tree_gen.query_mediastore",
                 side_effect=RuntimeError("ADB MediaStore query failed:\ndevice offline")), \
     mock.patch("config.USE_ADB", True), \
     mock.patch("audit._normalize_genre_cache", return_value=0):
    try:
        result = orch._run_device_scan(rev=2)
        _check("did not propagate — caught by the except Exception wrapper", True)
        _check("returns a dict (not crashing the caller)", isinstance(result, dict))
    except Exception as e:
        _check(f"propagated uncaught instead of being handled ({type(e).__name__}: {e})", False)

print(f"\n{'=' * 60}")
print(f"MediaStore refresh exception fix: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
