"""
preflight_check.py  —  Pre-run health-check for the Walkman Sync Agent
======================================================================
A standalone script to run before kicking off a long sync/audit session.
Produces a traffic-light summary (green/yellow/red) so you know whether
the environment is healthy enough for an unattended overnight run.

Usage
-----
    python preflight_check.py

Exit codes:
    0 = all green  — safe to start a long run
    1 = yellow     — non-fatal warnings, run is likely fine
    2 = red        — issues that should be addressed before starting

Checks run:
  1. Python version + key dependency imports (mutagen, pillow, pywinauto, pywin32)
  2. ADB connectivity (device reachable)
  3. Walkman Music app package detected
  4. Windows Update pending-restart check
  5. Stale audit temp dirs
  6. Battery health (if on laptop)
  7. Disk free space
  8. Config sanity (WALKMAN_DB_REFRESH_MODE, USE_ADB, etc.)

Does NOT start the sync agent, does NOT modify any files, does NOT
require elevated privileges.
"""
from __future__ import annotations

import sys
import os
import shutil
import importlib
import subprocess

# ANSI color codes (Windows 10+ terminals support them by default)
try:
    # Enable Windows ANSI support
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
except Exception:
    pass

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# Aggregated state
_checks: list[tuple[str, str, str]] = []   # (status, label, detail)
_WORST = {"green": 0, "yellow": 1, "red": 2}


def _emit(status: str, label: str, detail: str = "") -> None:
    """Record a check result and print it immediately."""
    _checks.append((status, label, detail))
    icon = {"green": "✓", "yellow": "⚠", "red": "✗"}.get(status, "?")
    color = {"green": GREEN, "yellow": YELLOW, "red": RED}.get(status, "")
    print(f"  {color}{icon}{RESET}  {label}")
    if detail:
        for line in detail.split("\n"):
            print(f"     {DIM}{line}{RESET}")


def _header(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")


# ---------------------------------------------------------------------------
# Check 1 — Python version + dependencies
# ---------------------------------------------------------------------------

def check_python() -> None:
    _header("1. Python environment")

    v = sys.version_info
    if v >= (3, 10):
        _emit("green", f"Python {v.major}.{v.minor}.{v.micro}")
    elif v >= (3, 8):
        _emit("yellow", f"Python {v.major}.{v.minor}.{v.micro}",
              "3.10+ recommended; 3.8/3.9 may work but some syntax features are used")
    else:
        _emit("red", f"Python {v.major}.{v.minor}.{v.micro}",
              "Too old — install Python 3.10+ from python.org")

    REQUIRED = [
        ("mutagen",       "red",    "ID3/FLAC tag reading/writing"),
        ("requests",      "red",    "HTTP client for genre APIs"),
        ("spotipy",       "red",    "Spotify API client"),
        ("dotenv",        "red",    "reads .env for API keys", "python-dotenv"),
    ]
    RECOMMENDED = [
        ("win32com",      "yellow", "COM/MTP fallback transport", "pywin32"),
        ("pywinauto",     "yellow", "Win11 Fluent dialog dismissal (effectively required on Win11)", "pywinauto"),
        ("PIL",           "yellow", "cover-art baseline-JPEG normalisation (Hypothesis C fix)", "pillow"),
    ]

    for spec in REQUIRED + RECOMMENDED:
        module = spec[0]
        severity = spec[1]
        purpose = spec[2]
        pip_name = spec[3] if len(spec) > 3 else module
        try:
            importlib.import_module(module)
            _emit("green", f"{pip_name:20s} imported",
                  f"used for: {purpose}")
        except ImportError:
            _emit(severity, f"{pip_name:20s} NOT installed",
                  f"purpose: {purpose}\nfix: pip install {pip_name}")


# ---------------------------------------------------------------------------
# Check 2 — ADB connectivity
# ---------------------------------------------------------------------------

def check_adb() -> None:
    _header("2. ADB connectivity")

    adb = shutil.which("adb")
    if not adb:
        # Also try the bundled adb.exe
        here = os.path.dirname(os.path.abspath(__file__))
        bundled = os.path.join(here, "adb.exe")
        if os.path.exists(bundled):
            adb = bundled
    if not adb:
        _emit("red", "adb not found on PATH",
              "install Android Platform Tools: developer.android.com/tools/releases/platform-tools")
        return

    _emit("green", f"adb found: {adb}")

    try:
        r = subprocess.run([adb, "devices"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",  # Rev 14.9.6 U1
                           timeout=10)
    except subprocess.TimeoutExpired:
        _emit("red", "adb devices timed out after 10s",
              "adb server may be hung — try: adb kill-server")
        return
    except Exception as e:
        _emit("red", f"adb devices failed: {e}")
        return

    lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    # First line is header "List of devices attached"
    device_lines = [l for l in lines[1:] if l]

    if not device_lines:
        _emit("yellow", "No device connected via ADB",
              "transfers will use COM fallback (slower, more dialog risk)\n"
              "to enable ADB: connect Walkman via USB, enable Developer Options + USB Debugging")
        return

    for dl in device_lines:
        parts = dl.split()
        if len(parts) >= 2:
            serial, state = parts[0], parts[1]
            if state == "device":
                _emit("green", f"device {serial}: {state}")
            elif state == "unauthorized":
                _emit("red", f"device {serial}: {state}",
                      "accept the 'Allow USB Debugging?' dialog on the Walkman screen")
            elif state == "offline":
                _emit("yellow", f"device {serial}: {state}",
                      "try unplugging and reconnecting the USB cable")
            else:
                _emit("yellow", f"device {serial}: {state}")


# ---------------------------------------------------------------------------
# Check 3 — Walkman Music app detection
# ---------------------------------------------------------------------------

def check_walkman_app() -> None:
    _header("3. Walkman Music app")

    adb = shutil.which("adb")
    if not adb:
        here = os.path.dirname(os.path.abspath(__file__))
        bundled = os.path.join(here, "adb.exe")
        if os.path.exists(bundled):
            adb = bundled
    if not adb:
        _emit("yellow", "cannot check — adb not available",
              "Walkman app refresh will be skipped at end of session")
        return

    try:
        r = subprocess.run([adb, "shell", "pm", "list", "packages"],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        _emit("yellow", f"pm list packages failed: {e}",
              "Walkman app refresh will be skipped")
        return

    out = r.stdout or ""
    candidates = [
        "com.sony.nwz.walkmanplayer",
        "com.sony.nwz.a3.walkmanplayer",
        "com.sony.songpal.mdr.vim",
        "com.sony.songpal",
    ]
    found = [c for c in candidates if f"package:{c}" in out]

    if found:
        _emit("green", f"Walkman app detected: {found[0]}",
              "end-of-session DB refresh will trigger on this app")
    else:
        _emit("yellow", "No known Walkman app package found",
              "the end-of-session refresh cannot run\n"
              "to find the real package: adb shell pm list packages | findstr -i walk\n"
              "then pin SONY_WALKMAN_PKG in config.py")


# ---------------------------------------------------------------------------
# Check 4 — Windows Update pending restart
# ---------------------------------------------------------------------------

def check_windows_update() -> None:
    _header("4. Windows Update pending-restart")

    if os.name != "nt":
        _emit("green", "not Windows — check not applicable")
        return

    try:
        import winreg  # type: ignore
    except ImportError:
        _emit("yellow", "winreg unavailable")
        return

    pending_reasons: list[str] = []

    for (hive, path, label) in [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
         "WindowsUpdate RebootRequired"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
         "CBS RebootPending"),
    ]:
        try:
            k = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
            winreg.CloseKey(k)
            pending_reasons.append(label)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    try:
        k = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
            0, winreg.KEY_READ,
        )
        try:
            val, _ = winreg.QueryValueEx(k, "PendingFileRenameOperations")
            nonempty = [s for s in (val or []) if s]
            if nonempty:
                pending_reasons.append(f"PendingFileRenameOperations ({len(nonempty)//2} op(s))")
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(k)
    except Exception:
        pass

    if pending_reasons:
        _emit("red", "Windows Update has a pending restart",
              "\n".join(["reasons: " + "; ".join(pending_reasons),
                         "the OS may reboot during your run — mitigations:",
                         "  Settings → Windows Update → Pause updates (1 week)",
                         "  OR Settings → Windows Update → Advanced → Active hours"]))
    else:
        _emit("green", "no pending restart detected")


# ---------------------------------------------------------------------------
# Check 5 — Stale audit temp dirs
# ---------------------------------------------------------------------------

def check_stale_temps() -> None:
    _header("5. Stale audit temp directories")

    import tempfile
    import pathlib
    import time

    tempdir = pathlib.Path(tempfile.gettempdir())
    cutoff = time.time() - (2 * 3600)
    prefixes = ("audit_folder_", "walkman_audit_dl_", "wman_")

    stale: list[tuple[pathlib.Path, int, float]] = []
    try:
        for entry in tempdir.iterdir():
            if not entry.is_dir():
                continue
            if not any(entry.name.startswith(p) for p in prefixes):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            size = 0
            try:
                for root, _d, files in os.walk(entry):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
            except Exception:
                pass
            stale.append((entry, size, time.time() - mtime))
    except Exception as e:
        _emit("yellow", f"could not scan tempdir: {e}")
        return

    if not stale:
        _emit("green", "no stale audit temp dirs")
        return

    total_mb = sum(s for _p, s, _a in stale) / 1024 / 1024
    status = "red" if total_mb >= 500 else "yellow"
    _emit(status, f"found {len(stale)} stale temp dir(s), {total_mb:.1f} MB total",
          "\n".join([
              f"oldest: {max(s[2] for s in stale)/3600:.1f}h ago",
              "to clean: run Walkman agent with STALE_TEMP_AUTOCLEAN=True",
              "or manually delete:",
              *[f"  {p}" for p, _s, _a in stale[:3]],
              f"  ... ({len(stale)-3} more)" if len(stale) > 3 else "",
          ]).strip())


# ---------------------------------------------------------------------------
# Check 6 — Battery health
# ---------------------------------------------------------------------------

def check_battery() -> None:
    _header("6. Battery health (laptop only)")

    if os.name != "nt":
        _emit("green", "not Windows — check skipped")
        return

    # Use WMI via subprocess to powershell — avoids WMI python package dependency
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance -ClassName Win32_Battery | "
             "Select-Object -Property BatteryStatus, EstimatedChargeRemaining, DesignVoltage, FullChargeCapacity | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        _emit("yellow", f"could not query battery: {e}")
        return

    out = (r.stdout or "").strip()
    if not out or out == "null":
        _emit("green", "no battery detected — desktop or battery-less system")
        return

    import json
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        _emit("yellow", "could not parse battery info")
        return

    # PowerShell returns dict for single battery, list for multiple
    if isinstance(data, list):
        data = data[0] if data else {}

    status = data.get("BatteryStatus")
    charge = data.get("EstimatedChargeRemaining")

    # BatteryStatus codes: 1=Discharging, 2=AC, 3=Fully Charged, 4=Low, 5=Critical, 6=Charging
    status_map = {1: "on battery (discharging)", 2: "on AC", 3: "fully charged on AC",
                  4: "low battery", 5: "critical battery", 6: "charging"}
    status_str = status_map.get(status, f"code {status}")

    if status in (1, 4, 5):
        _emit("red" if status in (4, 5) else "yellow",
              f"battery {status_str} ({charge}%)",
              "for long runs: plug into AC power\n"
              "sustained load on battery can trip thermal/power protection")
    else:
        _emit("green", f"power: {status_str} ({charge}%)")

    # Extra note: recommend running powercfg /batteryreport periodically
    _emit("green", "tip",
          "for battery health: powercfg /batteryreport /output battery.html\n"
          "replace if FULL CHARGE CAPACITY < 60% of DESIGN CAPACITY")


# ---------------------------------------------------------------------------
# Check 7 — Disk free space
# ---------------------------------------------------------------------------

def check_disk_space() -> None:
    _header("7. Disk free space")

    import tempfile
    tempdir = tempfile.gettempdir()
    try:
        total, used, free = shutil.disk_usage(tempdir)
        free_gb = free / (1024 ** 3)
        total_gb = total / (1024 ** 3)
    except Exception as e:
        _emit("yellow", f"could not check: {e}")
        return

    if free_gb >= 20:
        _emit("green", f"{free_gb:.1f} GB free on temp drive",
              f"({free_gb/total_gb*100:.0f}% of {total_gb:.0f} GB)")
    elif free_gb >= 5:
        _emit("yellow", f"{free_gb:.1f} GB free on temp drive",
              "audit temp dirs can reach several GB; 20 GB+ recommended for large libraries")
    else:
        _emit("red", f"only {free_gb:.1f} GB free on temp drive",
              "risk of transfer failure mid-run; free up space before starting")


# ---------------------------------------------------------------------------
# Check 8 — Config sanity
# ---------------------------------------------------------------------------

def check_config() -> None:
    _header("8. Walkman agent config sanity")

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import config  # type: ignore
    except ImportError:
        _emit("yellow", "config.py not found in this directory",
              "run this script from the Walkman agent folder")
        return

    # Key flags to report
    flags = [
        ("USE_ADB",                     True,     "ADB-first transport"),
        ("ENABLE_PREFLIGHT",            True,     "preflight cleanup"),
        ("WALKMAN_DB_REFRESH_MODE",    "ui_tap",  "end-of-session Music app refresh"),
        ("TRIGGER_MEDIA_SCAN",          False,    "per-file MediaStore broadcast (disabled: Walkman ignores MediaStore)"),
    ]
    for name, expected, purpose in flags:
        val = getattr(config, name, None)
        if val == expected:
            _emit("green", f"{name} = {val!r}",
                  f"({purpose})")
        elif val is None:
            _emit("yellow", f"{name} is not set",
                  f"should be {expected!r} for normal operation ({purpose})")
        else:
            severity = "yellow" if name == "WALKMAN_DB_REFRESH_MODE" else "green"
            _emit(severity, f"{name} = {val!r}",
                  f"expected {expected!r} — {purpose}")

    # Required API keys
    import os as _os
    keys = [
        ("SPOTIFY_CLIENT_ID",     "red",    "Spotify API access"),
        ("SPOTIFY_CLIENT_SECRET", "red",    "Spotify API access"),
        ("LASTFM_API_KEY",        "yellow", "genre enrichment"),
        ("GEMINI_API_KEY",        "yellow", "AI metadata inference"),
    ]
    for name, severity, purpose in keys:
        val = _os.environ.get(name, "") or getattr(config, name, "")
        if val:
            _emit("green", f"{name} is set",
                  f"({purpose})")
        else:
            _emit(severity, f"{name} is NOT set",
                  f"purpose: {purpose}\nadd to .env in this directory")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"{BOLD}Walkman Sync Agent — Pre-Run Health Check{RESET}")
    print(f"{DIM}Verifies environment before a long sync/audit session.{RESET}")

    check_python()
    check_adb()
    check_walkman_app()
    check_windows_update()
    check_stale_temps()
    check_battery()
    check_disk_space()
    check_config()

    # Summary
    counts = {"green": 0, "yellow": 0, "red": 0}
    for status, _label, _detail in _checks:
        counts[status] = counts.get(status, 0) + 1

    print(f"\n{BOLD}Summary{RESET}")
    print(f"  {GREEN}✓ {counts['green']} green{RESET}    "
          f"{YELLOW}⚠ {counts['yellow']} yellow{RESET}    "
          f"{RED}✗ {counts['red']} red{RESET}")

    if counts["red"] > 0:
        print(f"\n{RED}{BOLD}RED: issues should be addressed before starting a long run.{RESET}")
        return 2
    elif counts["yellow"] > 0:
        print(f"\n{YELLOW}{BOLD}YELLOW: warnings present. Run is likely fine but review above.{RESET}")
        return 1
    else:
        print(f"\n{GREEN}{BOLD}ALL GREEN: environment healthy, safe to start.{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
