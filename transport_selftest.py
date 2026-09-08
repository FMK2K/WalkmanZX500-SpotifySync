"""
transport_selftest.py — Rev 14.8
=================================
Quick diagnostic of MTP transport paths.  Run before a long sync to confirm
which transport(s) actually work on this system + device.  Prints a
green/yellow/red summary.

Invocation:
    python transport_selftest.py

What it does:
    1. Generates a 1 KB dummy binary file in a temp directory.
    2. Tries to push it to /Music/.walkman_transport_test/ via each available
       path (ADB, then COM).
    3. Deletes the dummy from the device after each successful path.
    4. Prints a summary and exits with 0 if at least one path works, 2 if none.

Driven by LOG-12 / 2026-04-19: a 12-hour sync ran with a broken ADB push
contract and an unavailable PowerShell subprocess — both dead.  Only the slow
in-process CopyHere path worked.  None of this was visible until the run was
already deep in cascade failure.  Run this script once after connecting the
Walkman and you'd know immediately.

Exits 0 if at least one transport works; exits 2 if all failed.
"""
from __future__ import annotations

import sys
import time
import pathlib
import tempfile
import traceback

import config


# ANSI colors — works on Windows Terminal and modern PowerShell.  No-op on
# plain cmd.exe if ansicon isn't installed, which is fine (readable anyway).
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
DIM    = "\033[2m"
BOLD   = "\033[1m"


def _make_test_file() -> pathlib.Path:
    """Create a 1 KB dummy binary file in a tempdir."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="walkman_selftest_"))
    p = tmp / "walkman_transport_test.bin"
    p.write_bytes(b"\x00" * 1024)
    return p


def _try_path(label: str, fn) -> tuple[bool, float, str]:
    """Run a transport function, return (ok, elapsed_seconds, detail)."""
    t0 = time.time()
    try:
        fn()
        return True, time.time() - t0, "OK"
    except Exception as e:
        return False, time.time() - t0, f"{type(e).__name__}: {e}"


def main() -> int:
    print(f"{BOLD}=== Walkman Transport Self-Test (Rev 14.8) ==={RESET}")
    print()

    test_file = _make_test_file()
    dest_folder_parts = [config.MUSIC_ROOT, ".walkman_transport_test"]
    dest_folder_display = "/".join(dest_folder_parts)
    storage = getattr(config, "WRITE_STORAGE", "Internal shared storage")
    device  = getattr(config, "DEVICE_NAME", "WALKMAN")
    print(f"{DIM}Test file : {test_file} (1 KB){RESET}")
    print(f"{DIM}Destination: {storage}/{dest_folder_display}/{RESET}")
    print(f"{DIM}Device     : {device}{RESET}")
    print()

    results: list[tuple[str, bool, float, str]] = []

    # ── ADB path ────────────────────────────────────────────────────────────
    print(f"{BOLD}[1/2] ADB push...{RESET}", flush=True)
    try:
        import adb_transfer as _adb

        def _adb_try():
            _adb.copy_file_to_device(
                src=test_file,
                dest_parts=dest_folder_parts,
                device_name=device,
                storage_name=storage,
                overwrite=True,
            )
        ok, secs, detail = _try_path("adb", _adb_try)
        results.append(("ADB push", ok, secs, detail))
        color = GREEN if ok else RED
        print(f"  {color}{'✓' if ok else '✗'} ADB push: {detail} ({secs:.2f}s){RESET}")
    except ImportError:
        results.append(("ADB push", False, 0.0, "adb_transfer module missing"))
        print(f"  {RED}✗ adb_transfer module missing — cannot test ADB path{RESET}")

    # ── COM path (tries PS subprocess first, then in-process CopyHere) ──────
    print(f"{BOLD}[2/3] COM path (PowerShell subprocess → in-process CopyHere)...{RESET}", flush=True)
    try:
        import mtp_transfer_com as _com

        def _com_try():
            _com.copy_file_to_device(
                src=test_file,
                dest_parts=dest_folder_parts,
                device_name=device,
                storage_name=storage,
                overwrite=True,
            )
        ok, secs, detail = _try_path("com", _com_try)
        results.append(("COM CopyHere", ok, secs, detail))
        color = GREEN if ok else RED
        print(f"  {color}{'✓' if ok else '✗'} COM CopyHere: {detail} ({secs:.2f}s){RESET}")
    except ImportError:
        results.append(("COM CopyHere", False, 0.0, "mtp_transfer_com module missing"))
        print(f"  {RED}✗ mtp_transfer_com module missing — cannot test COM path{RESET}")

    # ── LocalSend path (WiFi, Internal storage only — added 2026-07-17) ────
    # Informational only: never counted toward the required/failed summary
    # below, since ADB+COM already provide full coverage and LocalSend is a
    # supplementary WiFi-only route for Internal-storage writes (config.
    # LOCALSEND_ENABLED gates whether the live pipeline actually uses it).
    print(f"{BOLD}[3/3] LocalSend (WiFi push, Internal storage only)...{RESET}", flush=True)
    localsend_result = None
    if storage != "Internal shared storage":
        print(f"  {DIM}— SKIPPED: destination storage is {storage!r}; LocalSend's receiver "
              f"has no path to anything but Internal storage.{RESET}")
    else:
        try:
            import localsend_transfer as _ls

            peer = _ls.get_walkman_peer()
            if peer is None:
                print(f"  {YELLOW}⚠ No LocalSend peer discovered on the LAN "
                      f"(deviceModel='SonyAudio') — is LocalSend running on the Walkman?{RESET}")
            else:
                def _ls_try():
                    ok = _ls.push_file_via_localsend(
                        src=test_file,
                        dest_parts=dest_folder_parts,
                        device_name=device,
                        storage_name=storage,
                        overwrite=True,
                    )
                    if not ok:
                        raise RuntimeError("push_file_via_localsend returned False")
                ok, secs, detail = _try_path("localsend", _ls_try)
                localsend_result = ("LocalSend push+mv", ok, secs, detail)
                color = GREEN if ok else YELLOW
                print(f"  {color}{'✓' if ok else '⚠'} LocalSend push+mv: {detail} ({secs:.2f}s){RESET}")
        except ImportError:
            print(f"  {RED}✗ localsend_transfer module missing — cannot test LocalSend path{RESET}")
        except Exception as e:
            print(f"  {YELLOW}⚠ LocalSend probe errored: {type(e).__name__}: {e}{RESET}")

    # ── PowerShell availability probe ───────────────────────────────────────
    # Separately surfaced because it's the most common silent failure mode.
    print()
    print(f"{BOLD}[Auxiliary] PowerShell availability:{RESET}", flush=True)
    try:
        import wpd_transfer as _wpd
        _ps = _wpd._find_ps_exe()
        if _ps:
            print(f"  {GREEN}✓ Found PowerShell executable: {_ps}{RESET}")
            print(f"  {DIM}  (the fast PS-subprocess CopyHere path is available){RESET}")
        else:
            print(f"  {YELLOW}⚠ No 'pwsh' or 'powershell' on PATH{RESET}")
            print(f"  {DIM}  Install PowerShell 7 from https://aka.ms/powershell{RESET}")
            print(f"  {DIM}  Without it you are forced onto the slower in-process CopyHere.{RESET}")
    except ImportError:
        print(f"  {RED}✗ wpd_transfer module missing{RESET}")
    except Exception as e:
        print(f"  {YELLOW}⚠ PowerShell probe errored: {type(e).__name__}: {e}{RESET}")

    # ── Cleanup ─────────────────────────────────────────────────────────────
    try:
        test_file.unlink()
        test_file.parent.rmdir()
    except Exception:
        pass
    # Best-effort device cleanup — try ADB rm, ignore failures
    try:
        import adb_transfer as _adb2
        _adb2._adb_cmd([
            "shell", "rm", "-rf",
            _adb2._get_storage_path(storage) + "/" + "/".join(dest_folder_parts),
        ])
    except Exception:
        pass

    # ── Summary ─────────────────────────────────────────────────────────────
    # LocalSend is deliberately excluded from n_ok/n_total: ADB+COM already
    # provide full coverage, LocalSend only ever covers a subset (Internal
    # storage), so its absence should never read as "sync will fail."
    print()
    print(f"{BOLD}── Summary ──{RESET}")
    n_ok = sum(1 for _, ok, _, _ in results if ok)
    n_total = len(results)
    for label, ok, secs, detail in results:
        color = GREEN if ok else RED
        mark = "✓" if ok else "✗"
        print(f"  {color}{mark} {label:15s}{RESET}  {secs:6.2f}s  {detail[:120]}")
    if localsend_result:
        label, ok, secs, detail = localsend_result
        color = GREEN if ok else YELLOW
        mark = "✓" if ok else "⚠"
        print(f"  {color}{mark} {label:15s}{RESET}  {secs:6.2f}s  {detail[:120]}  {DIM}(informational, not required){RESET}")

    print()
    if n_ok == n_total:
        print(f"{GREEN}All transport paths OK.  Your sync should run at full speed.{RESET}")
        return 0
    elif n_ok == 0:
        print(f"{RED}No transport paths work.  A sync will fail entirely.  Check:{RESET}")
        print(f"  - Walkman is connected, unlocked, and shown as 'WALKMAN' in File Explorer")
        print(f"  - USB cable is data-capable (not charge-only)")
        print(f"  - Device has enough free space")
        print(f"  - For ADB: USB debugging is enabled on the Walkman (if applicable)")
        return 2
    else:
        print(f"{YELLOW}{n_ok}/{n_total} transport paths work.  Sync will run but may be slower than optimal.{RESET}")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[Interrupted]")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(3)
