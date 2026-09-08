"""
main.py  —  Walkman Sync Agent GUI  (Rev 14.9.11)
===============================================
Tkinter desktop application.  All sync work runs on daemon background
threads so the UI stays responsive.

Primary Buttons
---------------
  ▶ START SYNC      Full pipeline: download → fix local → scan → tag →
                    transfer → M3U8 → audit
  ⬇ DOWNLOAD ONLY  Stage 0 only: fetch Spotify playlists, identify missing
                    tracks, download full albums via SpotiFLAC
                    
  🔍 AUDIT ONLY     Fix local files + verify/fix device files (no Spotify,
                    no transfers)
  🔀 REORGANISE     Move all files into canonical Music/Artist/Album/ structure
  ■ CANCEL          Stops gracefully after the current track/batch

Tools Panel (collapsible, right side)
--------------------------------------
  Cache Management:
    - Clear genre cache          Delete .genre_cache.json (forces API re-query)
    - Clear Spotify cache        Delete .spotify_metadata_cache.json
    - Clear Spotify auth token   Delete .spotify_auth_cache (forces re-login)
    - Clear TIDAL album cache    Delete .tidal_album_cache.json
    - Nuke audit state           Delete .audit_state.json + in-memory cache
    - Clear artist genre overrides  Selectively remove cache entiries for artists
                                    whose genre was wrong (e.g. mf doom → Alternative Rock)

  Log Tools:
    - View sync log              Open sync_log.txt in scrollable viewer
    - View missing tracks        Open missing_tracks.txt
    - View audit report          Open audit_report.txt
    - Analyse log (errors)       Parse sync_log.txt and display error/warning summary
    - Export log                 Copy sync_log.txt to Desktop

  Device Tools:
    - ADB: trigger media scan    Send MediaScanner broadcast via ADB
    - ADB: list device files     List all audio files on device
    - ADB: check free space      Query internal + SD card free space
    - ADB: shell                 Open an adb shell prompt in a popup terminal
    - Open missing_tracks.txt    Quick view in popup

  LocalSend (WiFi transport, added Rev MK12 / 2026-07-17):
    - Check LocalSend connection  Discover the Walkman over WiFi (no USB),
                                   send+move a real throwaway test file, and
                                   auto-enable config.LOCALSEND_ENABLED (saved
                                   to config.py) only if that real test succeeds

  Batch Rebuild:
    - Set artists / letters      Configure REBUILD_BATCH_ARTISTS or REBUILD_BATCH_LETTERS
    - Run batch rebuild          Re-download and replace specific artists on device

New in Rev 12:
  - Full Tools panel (collapsible sidebar)
  - Log viewer with search / filter
  - Error/warning analyser
  - ADB commands surfaced to GUI
  - Cache management buttons with confirmations
  - Artist-specific genre cache purge
  - Batch 2rebuild UI
"""

import json
import os
import re
import shutil
import subprocess as _subprocess
import threading
import pathlib
import datetime
import sys as _sys_early

# Rev 14.6: reconfigure stdout/stderr to UTF-8 BEFORE any logging-capable imports.
# The 2026-04-19 log showed an UnicodeEncodeError on ai_helper.py:95 because the
# Groq connectivity-test thread fires at module-import time, which can be BEFORE
# setup_logging() has wrapped stdout.  This early reconfigure makes every later
# module's logging safe on cp1252 Windows consoles regardless of handler order.
try:
    _sys_early.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys_early.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # Older Python or redirected stream — setup_logging() has its own fallback.
    pass

import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk, simpledialog, filedialog

from downloader import run_batch_rebuild
import config


# ---------------------------------------------------------------------------
# Sleep blocker (unchanged from Rev 11)
# ---------------------------------------------------------------------------

def _prevent_sleep(label: str = "WalkmanSync") -> "_SleepBlocker":
    return _SleepBlocker(label)


class _SleepBlocker:
    """
    Prevents Windows from sleeping during long sync runs.
    Uses SetThreadExecutionState (kernel32) — no subprocess, no admin rights.
    """
    # ponytail: kernel32 SetThreadExecutionState — the correct Windows API for this
    _ES_CONTINUOUS      = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self, label: str = "WalkmanSync"):
        self.label = label
        self._active = False

    def start(self):
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(
                self._ES_CONTINUOUS | self._ES_SYSTEM_REQUIRED
            )
            self._active = True
        except Exception as e:
            print(f"[SleepBlocker] Could not start: {e}")
            try:
                import config as _cfg_rc5
                _threshold_h = float(getattr(_cfg_rc5, "LONG_RUN_THRESHOLD_HOURS", 2.0))
                print(
                    f"\n[Preflight] SLEEPBLOCKER_MISSING_LONGRUN_RED\n"
                    f"  ⚠  SleepBlocker could not start.\n"
                    f"  ⚠  For runs expected to exceed {_threshold_h:.0f}h this is a RED risk.\n"
                    f"  ⚠  Windows may sleep mid-transfer, corrupting sync state.\n"
                    f"  ⚠  Fix options:\n"
                    f"       1. Set Active Hours in Windows Update to cover your run\n"
                    f"       2. Run with --short-run flag if < {_threshold_h:.0f}h expected\n"
                )
            except Exception:
                print("[Preflight] SLEEPBLOCKER_MISSING_LONGRUN_RED — long-run safety degraded")

    def stop(self):
        if self._active:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(self._ES_CONTINUOUS)
            except Exception:
                pass
            self._active = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


# ---------------------------------------------------------------------------
# Import project modules
# ---------------------------------------------------------------------------

try:
    import config
    import mtp_transfer as mtp
    from walkman_sync import WalkmanSyncAgent, run_audit, run_reorganize, setup_logging
    # MK26 H2: Reset Gemini circuit breaker state at session startup
    try:
        import genre_client as _gc_h2
        _gc_h2.reset_session_state()
    except Exception:
        pass
except Exception as _import_err:
    import traceback, sys
    _err = traceback.format_exc()
    try:
        import tkinter as _tk
        _r = _tk.Tk(); _r.withdraw()
        _tk.messagebox.showerror("Import error — check startup_error.txt", _err)
        _r.destroy()
    except Exception:
        pass
    try:
        pathlib.Path("startup_error.txt").write_text(_err, encoding="utf-8")
    except Exception:
        pass
    print(_err, file=sys.stderr)
    import sys; sys.exit(1)


# ---------------------------------------------------------------------------
# Colour palette (dark theme)
# ---------------------------------------------------------------------------
BG_MAIN   = "#1a1a2e"
BG_HEADER = "#16213e"
BG_DARK   = "#0f0f1a"
BG_PANEL  = "#12122a"
BG_BTN    = "#2a2a4a"

FG_WHITE  = "#eaeaea"
FG_GREY   = "#888888"
FG_TEAL   = "#4ecca3"
FG_AMBER  = "#f0a500"
FG_RED    = "#e94560"
FG_BLUE   = "#4e8eca"
FG_PURPLE = "#a78bfa"
FG_GREEN  = "#5fd35f"
FG_ORANGE = "#ff8c42"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_adb(*args, timeout: int = 30) -> tuple[int, str, str]:
    """Run an adb command, return (returncode, stdout, stderr)."""
    if not getattr(config, "USE_ADB", True):
        return -1, "", (
            "ADB is disabled (config.USE_ADB=False).\n\n"
            "The ZX500 uses MTP/COM as its primary transport.\n"
            "To re-enable ADB: set USE_ADB=True in config.py and\n"
            "ensure USB Debugging is authorized on the device."
        )
    try:
        import adb_transfer as _adb
        exe = _adb._ADB_EXE
    except Exception:
        exe = "adb"
    try:
        r = _subprocess.run(
            [exe] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except _subprocess.TimeoutExpired:
        return -1, "", "ADB command timed out"
    except FileNotFoundError:
        return -1, "", "adb not found — install Android Platform Tools"
    except Exception as e:
        return -1, "", str(e)


def _open_file_viewer(parent, title: str, path: pathlib.Path):
    """Open a scrollable read-only text viewer window for any text file."""
    if not path.exists():
        messagebox.showinfo(title, f"File not found:\n{path}", parent=parent)
        return
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("900x640")
    win.configure(bg=BG_DARK)

    # Toolbar
    bar = tk.Frame(win, bg=BG_HEADER, pady=4)
    bar.pack(fill="x")

    search_var = tk.StringVar()
    tk.Label(bar, text="Search:", bg=BG_HEADER, fg=FG_GREY,
             font=("Segoe UI", 9)).pack(side="left", padx=(8, 4))
    search_entry = tk.Entry(bar, textvariable=search_var, bg=BG_BTN, fg=FG_WHITE,
                            font=("Consolas", 9), width=30, insertbackground=FG_WHITE)
    search_entry.pack(side="left", padx=4)

    txt = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                    font=("Consolas", 9), relief="flat",
                                    state="disabled")
    txt.pack(expand=True, fill="both", padx=6, pady=6)

    # Tag colours for common log tokens
    txt.tag_config("ERROR",   foreground=FG_RED)
    txt.tag_config("WARNING", foreground=FG_AMBER)
    txt.tag_config("INFO",    foreground=FG_WHITE)
    txt.tag_config("SUCCESS", foreground=FG_TEAL)
    txt.tag_config("MATCH",   background="#333355")

    content = path.read_text(encoding="utf-8", errors="replace")
    txt.config(state="normal")
    for line in content.splitlines(keepends=True):
        if "[ERROR]"   in line: tag = "ERROR"
        elif "[WARNING]" in line: tag = "WARNING"
        elif "CHANGED"   in line or "COMPLETE" in line: tag = "SUCCESS"
        else:                   tag = "INFO"
        txt.insert(tk.END, line, tag)
    txt.config(state="disabled")
    txt.see(tk.END)

    def _do_search(*_):
        txt.tag_remove("MATCH", "1.0", tk.END)
        needle = search_var.get().strip()
        if not needle:
            return
        start = "1.0"
        while True:
            pos = txt.search(needle, start, stopindex=tk.END, nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(needle)}c"
            txt.tag_add("MATCH", pos, end)
            start = end
        if txt.tag_ranges("MATCH"):
            txt.see(txt.tag_ranges("MATCH")[0])

    search_entry.bind("<Return>", _do_search)
    tk.Button(bar, text="Find", command=_do_search,
              bg=BG_BTN, fg=FG_GREY, font=("Segoe UI", 9),
              relief="flat", padx=8, cursor="hand2").pack(side="left", padx=4)

    # Export button
    def _export():
        dest = filedialog.asksaveasfilename(
            parent=win, title="Save as",
            defaultextension=".txt",
            initialfile=path.name,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if dest:
            shutil.copy2(path, dest)
            messagebox.showinfo("Exported", f"Saved to:\n{dest}", parent=win)

    tk.Button(bar, text="💾 Export", command=_export,
              bg=BG_BTN, fg=FG_GREY, font=("Segoe UI", 9),
              relief="flat", padx=8, cursor="hand2").pack(side="right", padx=8)

    tk.Label(bar, text=f"{path.name}  ({path.stat().st_size // 1024} KB)",
             bg=BG_HEADER, fg=FG_GREY, font=("Segoe UI", 8)).pack(side="right", padx=8)


def _open_json_viewer(parent, title: str, path: pathlib.Path, *,
                       latest_of_list: bool = False, max_chars: int = 500_000):
    """
    Pretty-printed, read-only JSON viewer — mirrors _open_file_viewer's
    search/export/theming conventions. 2026-07-26: added so the .json outputs
    our own tools produce (scan-duplicates' .dedup_log.json, deep-clean's
    .deep_clean_progress.json, the device snapshot, etc.) can be inspected
    directly in the GUI instead of needing an external editor.

    latest_of_list=True shows only the most recent entry when the file's top
    level is a list — for journal-style files like .dedup_log.json that
    accumulate up to 50 historical entries, showing the whole history by
    default would be both slow to render and rarely what's wanted.
    """
    if not path.exists():
        messagebox.showinfo(title, f"File not found:\n{path}", parent=parent)
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        messagebox.showerror(title, f"Could not parse JSON:\n{path}\n\n{e}", parent=parent)
        return

    note = ""
    if latest_of_list and isinstance(data, list) and data:
        note = f"(showing most recent of {len(data)} entries in this file — use Export for the full history)\n\n"
        data = data[-1]

    pretty = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
    truncated = len(pretty) > max_chars
    if truncated:
        pretty = pretty[:max_chars]

    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("900x640")
    win.configure(bg=BG_DARK)

    bar = tk.Frame(win, bg=BG_HEADER, pady=4)
    bar.pack(fill="x")

    search_var = tk.StringVar()
    tk.Label(bar, text="Search:", bg=BG_HEADER, fg=FG_GREY,
             font=("Segoe UI", 9)).pack(side="left", padx=(8, 4))
    search_entry = tk.Entry(bar, textvariable=search_var, bg=BG_BTN, fg=FG_WHITE,
                            font=("Consolas", 9), width=30, insertbackground=FG_WHITE)
    search_entry.pack(side="left", padx=4)

    txt = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                    font=("Consolas", 9), relief="flat",
                                    state="disabled")
    txt.pack(expand=True, fill="both", padx=6, pady=6)
    txt.tag_config("NOTE",  foreground=FG_AMBER)
    txt.tag_config("MATCH", background="#333355")

    txt.config(state="normal")
    if note:
        txt.insert(tk.END, note, "NOTE")
    txt.insert(tk.END, pretty)
    if truncated:
        txt.insert(tk.END, f"\n\n... truncated at {max_chars:,} characters — use Export for the full file ...", "NOTE")
    txt.config(state="disabled")

    def _do_search(*_):
        txt.tag_remove("MATCH", "1.0", tk.END)
        needle = search_var.get().strip()
        if not needle:
            return
        start = "1.0"
        while True:
            pos = txt.search(needle, start, stopindex=tk.END, nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(needle)}c"
            txt.tag_add("MATCH", pos, end)
            start = end
        if txt.tag_ranges("MATCH"):
            txt.see(txt.tag_ranges("MATCH")[0])

    search_entry.bind("<Return>", _do_search)
    tk.Button(bar, text="Find", command=_do_search,
              bg=BG_BTN, fg=FG_GREY, font=("Segoe UI", 9),
              relief="flat", padx=8, cursor="hand2").pack(side="left", padx=4)

    def _export():
        dest = filedialog.asksaveasfilename(
            parent=win, title="Save as",
            defaultextension=".json",
            initialfile=path.name,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if dest:
            shutil.copy2(path, dest)
            messagebox.showinfo("Exported", f"Saved to:\n{dest}", parent=win)

    tk.Button(bar, text="💾 Export", command=_export,
              bg=BG_BTN, fg=FG_GREY, font=("Segoe UI", 9),
              relief="flat", padx=8, cursor="hand2").pack(side="right", padx=8)

    tk.Label(bar, text=f"{path.name}  ({path.stat().st_size // 1024} KB)",
             bg=BG_HEADER, fg=FG_GREY, font=("Segoe UI", 8)).pack(side="right", padx=8)


def _analyse_log(parent, path: pathlib.Path):
    """Parse sync_log.txt and display an error/warning summary."""
    if not path.exists():
        messagebox.showinfo("Log Analysis", f"Log file not found:\n{path}", parent=parent)
        return

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    errors   = [l for l in lines if "[ERROR]"   in l]
    warnings = [l for l in lines if "[WARNING]" in l]
    timeouts = [l for l in lines if "timed out" in l.lower() or "timeout" in l.lower()]
    missing  = [l for l in lines if "[Missing]" in l or "missing" in l.lower() and "[INFO]" in l]
    changed  = [l for l in lines if "CHANGED"   in l]
    ai_fails = [l for l in lines if "All providers failed" in l or "429" in l]

    win = tk.Toplevel(parent)
    win.title("Log Analysis")
    win.geometry("800x580")
    win.configure(bg=BG_DARK)

    nb = ttk.Notebook(win)
    nb.pack(expand=True, fill="both", padx=6, pady=6)

    def _make_tab(title: str, items: list, colour: str):
        frame = tk.Frame(nb, bg=BG_DARK)
        nb.add(frame, text=f"{title} ({len(items)})")
        tk.Label(frame, text=f"{len(items)} entries",
                 bg=BG_DARK, fg=colour, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=4)
        st = scrolledtext.ScrolledText(frame, bg=BG_DARK, fg=colour,
                                        font=("Consolas", 8), relief="flat", state="normal")
        st.pack(expand=True, fill="both", padx=6, pady=(0, 6))
        for line in items:
            st.insert(tk.END, line + "\n")
        st.config(state="disabled")
        return frame

    _make_tab("Errors",    errors,   FG_RED)
    _make_tab("Warnings",  warnings, FG_AMBER)
    _make_tab("AI Fails",  ai_fails, FG_ORANGE)
    _make_tab("Timeouts",  timeouts, FG_RED)
    _make_tab("Changed",   changed,  FG_TEAL)

    # Summary tab
    frame = tk.Frame(nb, bg=BG_DARK)
    nb.add(frame, text="Summary")
    summary_lines = [
        f"Log file:     {path}",
        f"Total lines:  {len(lines)}",
        "",
        f"Errors:       {len(errors)}",
        f"Warnings:     {len(warnings)}",
        f"AI failures:  {len(ai_fails)}",
        f"Timeouts:     {len(timeouts)}",
        f"Files changed:{len(changed)}",
        "",
    ]
    # Most common error patterns
    err_patterns: dict = {}
    for line in errors + warnings:
        m = re.search(r"\[(.*?)\]", line)
        if m:
            key = m.group(1)
            err_patterns[key] = err_patterns.get(key, 0) + 1
    if err_patterns:
        summary_lines.append("Top error tags:")
        for tag, count in sorted(err_patterns.items(), key=lambda x: -x[1])[:10]:
            summary_lines.append(f"  [{tag}]  ×{count}")

    st = scrolledtext.ScrolledText(frame, bg=BG_DARK, fg=FG_WHITE,
                                    font=("Consolas", 9), relief="flat", state="normal")
    st.pack(expand=True, fill="both", padx=6, pady=6)
    st.insert(tk.END, "\n".join(summary_lines))
    st.config(state="disabled")


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Walkman Sync Agent  (Rev 14.9.11)")
        self.minsize(900, 560)
        self.configure(bg=BG_MAIN)
        self.resizable(True, True)

        self._agent:       WalkmanSyncAgent | None = None
        self._cancel_flag: list[bool]              = [False]
        self._spiral_proc = None   # Popen handle when GUI_SUBPROCESS_PIPELINE runs the pipeline
        self._tools_visible = tk.BooleanVar(value=False)

        # REV 14.2 CRITICAL FIX: start dialog_watcher on app init.
        # The watchdog module has existed since Rev 13 with a complete
        # start()/stop() API, but NOTHING in the codebase ever called
        # start().  That's why a user screenshot on 2026-04-18 showed a
        # "Copy File — There is already a file with the same name"
        # dialog sitting unclicked, blocking overnight runs for 30+
        # minutes.  Without start(), the 250ms polling thread never
        # spins up and dialogs accumulate indefinitely.
        #
        # The watchdog is a daemon thread — it exits automatically with
        # the process.  Safe to start unconditionally at GUI launch.
        try:
            import dialog_watcher as _dw
            _dw.start()
            _dw.set_cancel_flag(self._cancel_flag)
        except Exception as _dwe:
            print(f"[main] dialog_watcher start failed (non-fatal): {_dwe}")

        # REV 14.5: Windows handle-exhaustion monitor.  Polls GDI/USER/
        # raw-handle counts and flips a throttle event when resources
        # climb toward Windows' per-process caps.  Driven by the
        # 2026-04-18 screenshot where Tkinter itself failed to allocate
        # a drawing surface mid-sync.  No-op on non-Windows.
        try:
            import resource_monitor as _rm
            if getattr(__import__("config"), "RESOURCE_MONITOR_ENABLED", True):
                _rm.start()
        except Exception as _rme:
            print(f"[main] resource_monitor start failed (non-fatal): {_rme}")

        self._build_ui()

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        # ── Outer container (main + tools panel side-by-side) ───────────────
        self._outer = tk.Frame(self, bg=BG_MAIN)
        self._outer.pack(expand=True, fill="both")

        # ── Left: main working area ──────────────────────────────────────────
        self._main_col = tk.Frame(self._outer, bg=BG_MAIN)
        self._main_col.pack(side="left", expand=True, fill="both")

        self._build_header(self._main_col)
        self._build_log(self._main_col)
        self._build_stats(self._main_col)
        self._build_buttons(self._main_col)

        # ── Right: collapsible tools panel ───────────────────────────────────
        self._tools_panel = tk.Frame(self._outer, bg=BG_PANEL, width=280)
        # Not packed yet — toggled by the Tools button

        self._build_tools_panel(self._tools_panel)

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_header(self, parent):
        header = tk.Frame(parent, bg=BG_HEADER, pady=6)
        header.pack(fill="x")

        # Device status label + detect
        self._roots_var = tk.StringVar(value="connect device → click Detect")
        tk.Label(
            header, textvariable=self._roots_var,
            bg=BG_HEADER, fg=FG_GREY, font=("Segoe UI", 9),
        ).pack(side="left", padx=(12, 4))

        self._btn_detect = tk.Button(
            header, text="🔎 Detect",
            command=self._detect_storage,
            bg=BG_BTN, fg=FG_GREY, font=("Segoe UI", 9),
            relief="flat", padx=8, pady=3, cursor="hand2",
        )
        self._btn_detect.pack(side="left")

        # Checkboxes
        def _chk(text, var, colour=FG_GREY, padx=12):
            tk.Checkbutton(
                header, text=text, variable=var,
                bg=BG_HEADER, fg=colour,
                selectcolor=BG_MAIN, activebackground=BG_HEADER,
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(padx, 0))

        self._audit_var = tk.BooleanVar(value=config.AUDIT_AFTER_SYNC)
        _chk("Audit after sync", self._audit_var)

        self._dl_var = tk.BooleanVar(value=config.DOWNLOAD_MISSING)
        _chk("Download missing", self._dl_var)

        self._force_audit_var = tk.BooleanVar(value=False)
        _chk("Force full re-audit", self._force_audit_var, FG_AMBER)

        self._reorg_after_audit_var = tk.BooleanVar(value=False)
        _chk("Reorganise after audit", self._reorg_after_audit_var, FG_AMBER)
        # MK10: Post-reorganize audit checkbox
        self._post_reorg_audit_var = tk.BooleanVar(
            value=getattr(config, "AUDIT_AFTER_REORG", True)
        )
        _chk("Audit after reorganise", self._post_reorg_audit_var, FG_AMBER)

        self._folder_mode_var = tk.BooleanVar(value=config.AUDIT_FOLDER_MODE)
        _chk("Folder-wise audit", self._folder_mode_var, FG_AMBER)

        self._nuke_audit_var = tk.BooleanVar(value=False)
        _chk("Nuke audit state", self._nuke_audit_var, FG_RED)

        # Tools toggle — right side
        tk.Button(
            header, text="🔧 Tools ▶",
            command=self._toggle_tools,
            bg=BG_BTN, fg=FG_ORANGE,
            font=("Segoe UI", 9, "bold"),
            relief="flat", padx=10, pady=3, cursor="hand2",
        ).pack(side="right", padx=12)

    # ── Log ─────────────────────────────────────────────────────────────────

    def _build_log(self, parent):
        self._log_area = scrolledtext.ScrolledText(
            parent, state="disabled",
            bg=BG_DARK, fg=FG_WHITE,
            font=("Consolas", 9),
            relief="flat", borderwidth=0,
        )
        self._log_area.pack(expand=True, fill="both", padx=10, pady=(6, 0))

        for tag, colour in [
            ("INFO",     FG_WHITE),
            ("WARNING",  FG_AMBER),
            ("ERROR",    FG_RED),
            ("SUCCESS",  FG_TEAL),
            ("AUDIT",    FG_PURPLE),
            ("DOWNLOAD", FG_BLUE),
        ]:
            self._log_area.tag_config(tag, foreground=colour)

    # ── Stats ────────────────────────────────────────────────────────────────

    def _build_stats(self, parent):
        stats_outer = tk.Frame(parent, bg=BG_HEADER, pady=4)
        stats_outer.pack(fill="x", padx=10, pady=(4, 0))

        def _section(label, colour, keys):
            tk.Label(stats_outer, text=label,
                     bg=BG_HEADER, fg=colour,
                     font=("Segoe UI", 8, "bold")).pack(side="left", padx=(8, 6))
            d = {}
            for k in keys:
                tk.Label(stats_outer, text=f"{k.capitalize()}:",
                         bg=BG_HEADER, fg=FG_GREY,
                         font=("Segoe UI", 9)).pack(side="left", padx=(4, 2))
                var = tk.StringVar(value="0")
                d[k] = var
                tk.Label(stats_outer, textvariable=var,
                         bg=BG_HEADER, fg=colour,
                         font=("Segoe UI", 9, "bold"), width=5).pack(side="left")
            return d

        self._sync_stat_vars  = _section("SYNC →",  FG_TEAL,   ["downloaded", "copied", "updated", "skipped", "missing"])
        tk.Label(stats_outer, text="  |  ", bg=BG_HEADER, fg=FG_GREY).pack(side="left")
        self._audit_stat_vars = _section("AUDIT →", FG_PURPLE, ["audited", "changed", "skipped", "errors"])

    # ── Buttons ──────────────────────────────────────────────────────────────

    def _build_buttons(self, parent):
        btn_frame = tk.Frame(parent, bg=BG_MAIN, pady=10)
        btn_frame.pack(fill="x")

        def _btn(text, cmd, bg, fg=BG_DARK, font_size=11):
            b = tk.Button(btn_frame, text=text, command=cmd,
                          bg=bg, fg=fg,
                          font=("Segoe UI", font_size, "bold"),
                          relief="flat", padx=18, pady=6, cursor="hand2")
            b.pack(side="left", padx=6)
            return b

        self._btn_sync     = _btn("▶  START SYNC",      self._start_sync,         FG_TEAL)
        # MK10: Full Pipeline button — runs the complete five-stage sequence:
        # dedup cleanup → sync → post-sync audit → reorganize → post-reorg audit
        self._btn_full     = _btn("⚡  FULL PIPELINE",    self._start_full_pipeline, FG_TEAL)
        self._btn_download = _btn("⬇  DOWNLOAD ONLY",   self._start_download_only, FG_BLUE)
        self._btn_audit    = _btn("🔍  AUDIT ONLY",      self._start_audit_only,   FG_PURPLE)
        self._btn_reorg    = _btn("🔀  REORGANISE",      self._start_reorganize,   FG_AMBER)
        # CURATE — catalog-fill (absent albums) → mislabel redownload → de-splinter
        # → deluxe/base folder merge → dedup → fav-upgrade → rebuild favorites/
        # playlists plan + M3U8. Honors the Dry-run checkbox below.
        self._btn_curate   = _btn("🧹  CURATE LIBRARY",  self._start_curate,       FG_GREEN)

        self._reorg_dryrun_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            btn_frame, text="Dry-run",
            variable=self._reorg_dryrun_var,
            bg=BG_MAIN, fg=FG_GREY,
            selectcolor=BG_MAIN, activebackground=BG_MAIN,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(4, 0))

        self._btn_cancel = tk.Button(
            btn_frame, text="■  CANCEL",
            command=self._cancel,
            bg=FG_RED, fg="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat", padx=18, pady=6, cursor="hand2",
            state="disabled",
        )
        self._btn_cancel.pack(side="left", padx=6)

        tk.Button(
            btn_frame, text="Clear log",
            command=self._clear_log,
            bg=BG_BTN, fg=FG_GREY,
            font=("Segoe UI", 9),
            relief="flat", padx=10, pady=6, cursor="hand2",
        ).pack(side="right", padx=12)

    # ── Tools panel ──────────────────────────────────────────────────────────

    def _build_tools_panel(self, parent):
        """Build the right-side collapsible tools panel."""
        parent.pack_propagate(False)

        tk.Label(parent, text="🔧  TOOLS",
                 bg=BG_PANEL, fg=FG_ORANGE,
                 font=("Segoe UI", 11, "bold")).pack(pady=(10, 4))

        tk.Frame(parent, bg=FG_ORANGE, height=1).pack(fill="x", padx=12, pady=2)

        # Scrollable area
        canvas = tk.Canvas(parent, bg=BG_PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", expand=True, fill="both")

        inner = tk.Frame(canvas, bg=BG_PANEL)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_frame_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_configure(e):
            canvas.itemconfig(inner_id, width=e.width)

        inner.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        # ── Section builder helpers ──────────────────────────────────────────
        def _section_label(text, colour=FG_ORANGE):
            tk.Label(inner, text=text,
                     bg=BG_PANEL, fg=colour,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10, pady=(10, 2))
            tk.Frame(inner, bg=colour, height=1).pack(fill="x", padx=10, pady=(0, 4))

        def _tool_btn(text, cmd, colour=FG_GREY, desc=None):
            row = tk.Frame(inner, bg=BG_PANEL)
            row.pack(fill="x", padx=8, pady=2)
            b = tk.Button(row, text=text, command=cmd,
                          bg=BG_BTN, fg=colour,
                          font=("Segoe UI", 9),
                          relief="flat", padx=8, pady=4,
                          cursor="hand2", anchor="w", width=28)
            b.pack(fill="x")
            if desc:
                tk.Label(row, text=desc, bg=BG_PANEL, fg="#555577",
                         font=("Segoe UI", 7), wraplength=240, justify="left").pack(anchor="w", padx=4)
            return b

        # ── 1. CACHE MANAGEMENT ─────────────────────────────────────────────
        _section_label("💾  Cache Management")

        _tool_btn("Clear genre cache",
                  self._clear_genre_cache,
                  FG_AMBER,
                  desc="Deletes .genre_cache.json — forces fresh API lookups for all tracks on next run")

        _tool_btn("Clear genre cache for artist...",
                  self._clear_genre_cache_for_artist,
                  FG_AMBER,
                  desc="Type an artist name to remove only their tracks from the genre cache")

        _tool_btn("Rebuild empty genre entries",
                  self._rebuild_genre_cache,
                  FG_TEAL,
                  desc="Fills every cache entry that has a blank genre via the Groq genre waterfall — runs now, no full sync needed")

        _tool_btn("Clear Spotify metadata cache",
                  self._clear_spotify_cache,
                  FG_GREY,
                  desc="Deletes .spotify_metadata_cache.json — re-fetches all playlist data on next run")

        _tool_btn("Clear Spotify auth token",
                  self._clear_spotify_token,
                  FG_GREY,
                  desc="Deletes .spotify_auth_cache — forces browser re-login on next run")

        _tool_btn("Clear TIDAL album cache",
                  self._clear_tidal_cache,
                  FG_GREY,
                  desc="Deletes .tidal_album_cache.json — re-resolves TIDAL album IDs on next download")

        _tool_btn("Nuke audit state",
                  self._nuke_audit_now,
                  FG_RED,
                  desc="Deletes .audit_state.json and clears in-memory metakeys — next audit re-checks every device file")

        _tool_btn("Fix wrong genre overrides",
                  self._fix_genre_overrides,
                  FG_AMBER,
                  desc="Removes known-wrong cache entries (e.g. MF DOOM → Alternative Rock, Cochise → Rap/Hip Hop)")

        # ── 2. LOG TOOLS ────────────────────────────────────────────────────
        _section_label("📋  Log Tools")

        _tool_btn("View sync log",
                  lambda: _open_file_viewer(self, "sync_log.txt", config.LOG_FILE),
                  FG_TEAL,
                  desc="Open sync_log.txt with search and colour coding")

        _tool_btn("Analyse log (errors/warnings)",
                  lambda: _analyse_log(self, config.LOG_FILE),
                  FG_TEAL,
                  desc="Parse sync_log.txt and show categorised error/warning/AI-failure summary")

        _tool_btn("View missing tracks",
                  lambda: _open_file_viewer(self, "missing_tracks.txt", config.REPORT_FILE),
                  FG_GREY,
                  desc="Tracks that couldn't be found or downloaded")

        _tool_btn("View audit report",
                  lambda: _open_file_viewer(self, "audit_report.txt", config.AUDIT_REPORT_FILE),
                  FG_PURPLE,
                  desc="Per-file audit results — what was changed and why")

        _tool_btn("Export sync log to Desktop",
                  self._export_log,
                  FG_GREY,
                  desc="Copy sync_log.txt to your Desktop with a timestamp")

        # ── 3. ADB / DEVICE TOOLS ───────────────────────────────────────────
        _section_label("📱  Device (ADB)")

        _tool_btn("Trigger MediaStore rescan",
                  self._adb_trigger_scan,
                  FG_TEAL,
                  desc="Sends ACTION_MEDIA_SCANNER_SCAN_FILE broadcast — makes Walkman Music app show new tags immediately")

        _tool_btn("Check free space",
                  self._adb_free_space,
                  FG_GREY,
                  desc="Query internal storage + SD card free space via ADB")

        _tool_btn("List device audio files",
                  self._adb_list_files,
                  FG_GREY,
                  desc="Show all .mp3 / .flac files found on device via ADB find command")

        _tool_btn("ADB devices (check connection)",
                  self._adb_check_devices,
                  FG_GREY,
                  desc="Run 'adb devices -l' and show the connection state (device/offline/"
                       "unauthorized/authorizing/absent) with guided next steps for each")

        _tool_btn("ADB driver diagnostic (Windows)",
                  self._adb_driver_diagnostic,
                  FG_GREY,
                  desc="Checks Device Manager for whether the 'Android Composite ADB Interface' "
                       "is actually bound (vs. only the MTP driver) — the single most common "
                       "cause of ADB seeing nothing despite MTP working fine")

        _tool_btn("Reset ADB pairing (regenerate host key)",
                  self._adb_reset_pairing,
                  FG_ORANGE,
                  desc="Backs up this PC's ADB RSA key pair and restarts the adb server, "
                       "forcing a brand-new 'Allow USB debugging?' prompt on the device — use "
                       "when the device is stuck offline/unauthorized with no new prompt ever "
                       "appearing")

        _tool_btn("ADB: query MediaStore DB",
                  self._adb_query_mediastore,
                  FG_GREY,
                  desc="Query Android MediaStore for title/artist/genre fields — compare against what was tagged")

        _tool_btn("ADB shell (interactive)",
                  self._adb_shell_popup,
                  FG_ORANGE,
                  desc="Open an interactive ADB shell command runner in a popup window")

        # ── 3z. WPD/MTP SESSION MAINTENANCE (added 2026-08-21) ───────────────
        # A full explorer.exe restart (not just closing Walkman-specific
        # windows, and not the admin-gated WPDBusEnum service cycle in
        # preflight.py) has been this project's most reliable fix for a
        # stale/degraded WPD-COM session throughout its history — used
        # repeatedly, manually, via a terminal, across many sessions. No GUI
        # button existed for it until now.
        _section_label("🔄  WPD/MTP Session")

        _tool_btn("Restart Windows Explorer (WPD/MTP reset)",
                  self._restart_windows_explorer,
                  FG_TEAL,
                  desc="Kills and restarts explorer.exe to clear a stale WPD/Shell.Application "
                       "COM session — the standard fix when device scans/transfers start timing "
                       "out or hanging despite ADB looking fine. Briefly closes File Explorer "
                       "windows and flashes the taskbar/desktop; does not affect ADB.")

        # ── 3a. ADB OVER WIFI (TCP/IP link, added 2026-07-30) ────────────────
        # Different from LocalSend below: this is still real ADB, just over
        # TCP instead of USB — full read/write access (pull/stat/mv/rm/shell
        # all keep working), not a push-only inbox. Useful specifically when
        # USB ADB itself is the flaky link, since it's the same adbd daemon
        # either way — doesn't fix ADB's own reliability, just the cable.
        _section_label("📡  ADB over WiFi")

        _tool_btn("Enable ADB over WiFi (needs USB now)",
                  self._adb_wifi_enable,
                  FG_TEAL,
                  desc="Arms TCP/IP mode over the current USB connection and reports "
                       "the device's WiFi IP — required once per device reboot/USB "
                       "replug before 'Connect over WiFi' will work")

        _tool_btn("Connect over WiFi",
                  self._adb_wifi_connect,
                  FG_TEAL,
                  desc="Connects to the device over WiFi ADB. On success, saves the "
                       "IP:port and enables ADB_WIFI_ENABLED in config.py so future "
                       "runs auto-fall-back to WiFi if USB ADB drops offline")

        _tool_btn("Disconnect ADB WiFi",
                  self._adb_wifi_disconnect,
                  FG_GREY,
                  desc="Closes the WiFi ADB link — use when done if you don't want "
                       "the TCP/IP port left open on this network")

        # ── 3b. LOCALSEND (WiFi transport, added 2026-07-17) ────────────────
        _section_label("📶  LocalSend (WiFi Transport)")

        _tool_btn("Check LocalSend connection",
                  self._localsend_check,
                  FG_TEAL,
                  desc="Discover the Walkman over WiFi (no USB), send+move a real "
                       "throwaway test file, and auto-enable LOCALSEND_ENABLED "
                       "(saved to config.py) only if that test succeeds")

        # ── 4. DEVICE MAINTENANCE ───────────────────────────────────────────
        _section_label("🗑  Device Maintenance")

        _tool_btn("Purge .deleted folder",
                  self._purge_soft_deleted,
                  FG_RED,
                  desc="Delete Music/.deleted/ from all storages via ParseName (O(1) lookup — works even when SD card is too full/slow for File Explorer)")

        # ── 4b. DUPLICATE CLEANUP (added 2026-07-24) ────────────────────────
        # scan-duplicates: read-only, always safe — identifies candidates and
        # writes .dedup_log.json. deep-clean: consumes that journal; the ADB
        # direct-path delete (2026-07-23) lets this actually finish instead
        # of hanging on the old whole-folder MTP enumeration.
        _section_label("🧹  Duplicate Cleanup")

        _tool_btn("Scan for duplicates",
                  self._scan_duplicates_gui,
                  FG_TEAL,
                  desc="Read-only device scan — identifies duplicate/corrupt files and writes .dedup_log.json. Never deletes anything.")

        _tool_btn("Deep-clean (dry-run)",
                  self._deepclean_dryrun_gui,
                  FG_AMBER,
                  desc="Preview what deep-clean would delete, based on the last scan's .dedup_log.json")

        _tool_btn("Deep-clean (EXECUTE)",
                  self._deepclean_execute_gui,
                  FG_RED,
                  desc="Actually deletes the duplicate/corrupt files from the last scan via the fast ADB direct-path delete")

        # ── 4c2. CURATION PIPELINE — INDIVIDUAL STAGES (added 2026-09-08) ────
        # Every stage the 🧹 CURATE LIBRARY pipeline runs, exposed as a
        # standalone button (dry-run + EXECUTE where the tool supports both) so
        # a single fix can be run without the full ~15-20h pipeline. All stream
        # into the CLI-mode viewer + main log. Routed through
        # cli_runner.py's `run-tool <script>` delegator (whitelisted).
        _section_label("🧰  Curation Pipeline — Individual Stages")

        _tool_btn("Album order + dupes (dry-run)",
                  lambda: self._run_tool_script(["walkman_album_normalize.py", "--exhaustive"],
                                                "Album Order + Dupes (dry-run)"),
                  FG_AMBER,
                  desc="--exhaustive preview: EVERY album folder vs its canonical Deezer tracklist — scrambled track-number TAGs (what the Sony player orders by), mp3/flac dupes, wrong numbering. Header-pulls files (probe-gated), no writes.")
        _tool_btn("Album order + dupes (EXECUTE, hard delete)",
                  lambda: self._run_tool_script(["walkman_album_normalize.py", "--execute", "--exhaustive"],
                                                "Album Order + Dupes (EXECUTE)",
                                                confirm="--exhaustive --execute: rewrites wrong track-number TAGs (pull/modify/push), HARD-deletes format duplicates (FLAC>MP3, favourited copy always wins), renumbers filenames vs Deezer. Favourited files move/rename but are never deleted. Hours, resumable. Continue?"),
                  FG_RED)

        _tool_btn("Duration cross-check sweep",
                  lambda: self._run_tool_script(["duration_verify.py", "sweep", "--fresh"],
                                                "Duration Cross-Check Sweep",
                                                confirm="Pulls a ~300KB header of every device file, compares duration to Deezer to flag wrong-track swaps + full-mix-as-single-track. Read-only on device; writes duration_verify_report.json. Hours, resumable. Continue?"),
                  FG_TEAL)
        _tool_btn("Duration suspects -> re-download queue",
                  lambda: self._run_tool_script(["duration_verify_categorize.py", "--write-pending"],
                                                "Categorize Duration Suspects"),
                  FG_TEAL,
                  desc="Buckets the last duration sweep + writes mislabel_redownload_pending.json (OVERSIZED + HIGH-CONFIDENCE WRONG TRACK). Read-only.")

        _tool_btn("Catalog-fill (dry-run)",
                  lambda: self._run_tool_script(["catalog_fill_scoped.py", "--dry-run"],
                                                "Catalog-Fill (dry-run)"),
                  FG_AMBER,
                  desc="Preview which missing tracks (partial + high-intent absent albums for saved/deep artists) would be downloaded. Needs a recent 'Check album completeness' run.")
        _tool_btn("Catalog-fill (EXECUTE, downloads)",
                  lambda: self._run_tool_script(["catalog_fill_scoped.py"],
                                                "Catalog-Fill (EXECUTE)",
                                                confirm="Downloads every missing track from the dry-run list via Deezer and transfers it to the device. Can be hundreds of tracks / hours. Continue?"),
                  FG_RED)

        _tool_btn("Mislabel re-download (dry-run)",
                  lambda: self._run_tool_script(["mislabel_redownload.py", "--dry-run"],
                                                "Mislabel Re-download (dry-run)"),
                  FG_AMBER,
                  desc="Preview: replace the files flagged in mislabel_redownload_pending.json (from the duration sweep) with fresh copies. Deletes the bad file then re-fetches.")
        _tool_btn("Mislabel re-download (EXECUTE)",
                  lambda: self._run_tool_script(["mislabel_redownload.py"],
                                                "Mislabel Re-download (EXECUTE)",
                                                confirm="Deletes each flagged mislabeled/oversized file and re-downloads the correct track. Favourited files queue a re-star (mislabel_restar.json). Continue?"),
                  FG_RED)

        _tool_btn("De-splinter folders (dry-run)",
                  lambda: self._run_tool_script(["walkman_desplinter.py"],
                                                "De-splinter Folders (dry-run)"),
                  FG_AMBER,
                  desc="Preview: fold split album folders (Music/<Album>/ + Music/<Artist>/<Album>/ + Music/Music/<Album>/) into one canonical folder. Favourited files move too; collisions left for dedup.")
        _tool_btn("De-splinter folders (EXECUTE)",
                  lambda: self._run_tool_script(["walkman_desplinter.py", "--execute"],
                                                "De-splinter Folders (EXECUTE)",
                                                confirm="Moves files into the canonical Music/<Artist>/<Album>/ folder (adb mv, instant, same filesystem). Non-destructive — collisions are left in place for the dedup pass. Continue?"),
                  FG_RED)

        _tool_btn("Deluxe/base merge (dry-run)",
                  lambda: self._run_tool_script(["walkman_deluxe_merge.py"],
                                                "Deluxe/Base Merge (dry-run)"),
                  FG_AMBER,
                  desc="Preview: merge 'Album' + 'Album (Deluxe/Extended/...)' folder pairs into one and renumber against the fullest edition's Deezer tracklist.")
        _tool_btn("Deluxe/base merge (EXECUTE)",
                  lambda: self._run_tool_script(["walkman_deluxe_merge.py", "--execute"],
                                                "Deluxe/Base Merge (EXECUTE)",
                                                confirm="Merges deluxe/base folder pairs, renumbers, and deletes the losing duplicate copies (FLAC>MP3). Favourited files move/rename but are never deleted. Continue?"),
                  FG_RED)

        _tool_btn("Storage rebalance (dry-run)",
                  lambda: self._run_tool_script(["walkman_storage_rebalance.py"],
                                                "Storage Rebalance (dry-run)"),
                  FG_AMBER,
                  desc="Preview: move whole album folders SD -> internal toward a capacity-proportional split (~82/18). On-device cp+verify+rm, rollback-safe.")
        _tool_btn("Storage rebalance (EXECUTE)",
                  lambda: self._run_tool_script(["walkman_storage_rebalance.py", "--execute"],
                                                "Storage Rebalance (EXECUTE)",
                                                confirm="Moves ~album folders from the SD card to internal storage (on-device copy + size-verify + remove source; a move that can't verify is rolled back). Breaks M3U8 lines for moved playlisted tracks until favsync regenerates them. Continue?"),
                  FG_RED)

        _tool_btn("Cover-fill (dry-run)",
                  lambda: self._run_tool_script(["walkman_cover_fill.py", "--dry-run"],
                                                "Cover-Fill (dry-run)"),
                  FG_AMBER,
                  desc="Preview: which art-less device files would get an embedded cover (one Deezer image per album). The Sony player shows EMBEDDED art only.")
        _tool_btn("Cover-fill (EXECUTE)",
                  lambda: self._run_tool_script(["walkman_cover_fill.py"],
                                                "Cover-Fill (EXECUTE)",
                                                confirm="Pulls each art-less file, embeds a Deezer cover, pushes it back (same path — favourites/playlists untouched). Resumable. Continue?"),
                  FG_RED)

        _tool_btn("Year-tag backfill (dry-run)",
                  lambda: self._run_tool_script(["walkman_year_backfill.py", "--dry-run"],
                                                "Year Backfill (dry-run)"),
                  FG_AMBER,
                  desc="Preview: which year-less device files would get a release year written (one Deezer /album lookup per album).")
        _tool_btn("Year-tag backfill (EXECUTE)",
                  lambda: self._run_tool_script(["walkman_year_backfill.py"],
                                                "Year Backfill (EXECUTE)",
                                                confirm="Writes the 4-digit release year into TDRC/date on year-less files in place (path unchanged — favourites/playlists safe). Resumable. Continue?"),
                  FG_RED)

        _tool_btn("Favorited-copy upgrade (dry-run)",
                  lambda: self._run_tool_script(["walkman_fav_upgrade.py"],
                                                "Favorited-Copy Upgrade (dry-run)"),
                  FG_AMBER,
                  desc="Preview: where a favourited MP3 has a superior FLAC sibling in a dedup pair — delete the inferior, queue a re-star. Needs a recent 'Scan for duplicates'.")
        _tool_btn("Favorited-copy upgrade (EXECUTE)",
                  lambda: self._run_tool_script(["walkman_fav_upgrade.py", "--execute"],
                                                "Favorited-Copy Upgrade (EXECUTE)",
                                                confirm="Deletes the inferior copy in each favourited/playlisted dedup pair and queues the FLAC for a re-star (fav_upgrade_restar.json). Continue?"),
                  FG_RED)

        _tool_btn("Incompleteness scan (read-only)",
                  lambda: self._run_tool_script(["walkman_incompleteness_scan.py", "--tag-sample", "400"],
                                                "Incompleteness Scan"),
                  FG_TEAL,
                  desc="5-axis read-only audit: missing covers/tags, incomplete albums & catalogs, incomplete playlists, un-organized/splintered folders, storage skew. Writes incompleteness_report.json.")

        _tool_btn("Re-star favourites (restar_merged.json)",
                  self._restar_favorites_gui,
                  FG_AMBER,
                  desc="UI automation: star each (artist,title) in restar_merged.json (deleted-file favourites from this session's cleanup). Toggle-safe — pre-checks each. Do a device 'Update database' rescan first.")

        # ── 4c. PREVIEW & REVIEW TOOLS (added 2026-07-24) ────────────────────
        # Read-only preview commands for features gated behind their own
        # config flags — review what WOULD change before turning them on.
        _section_label("🔎  Preview & Review Tools")

        _tool_btn("Preview genre consolidation",
                  self._preview_genre_consolidation_gui,
                  FG_TEAL,
                  desc="Shows proposed album-genre changes without writing anything or touching the MK38.0 freeze")

        _tool_btn("Preview AcoustID corrections",
                  self._preview_acoustid_gui,
                  FG_TEAL,
                  desc="Shows what fingerprint-based metadata corrections would resolve for blank-tag local files")

        _tool_btn("Preview ListenBrainz discovery",
                  self._preview_listenbrainz_gui,
                  FG_TEAL,
                  desc="Prompts for a username, shows recommendation-playlist tracks not yet seen — nothing downloaded or queued")

        _tool_btn("Merge splintered folders (dry-run)",
                  self._merge_splintered_dryrun_gui,
                  FG_AMBER,
                  desc="Preview merging near-duplicate album folders (e.g. truncated-name variants) on the device")

        _tool_btn("Merge splintered folders (EXECUTE)",
                  self._merge_splintered_execute_gui,
                  FG_RED,
                  desc="Actually merges the folders found above and deletes the losing copies")

        # ── 4e. GENRE GRAPH (added 2026-09-04) ───────────────────────────────
        # The artist co-occurrence graph was previously only ever built
        # in-memory inside a spiral run and discarded when it ended. These
        # give it a standalone build + a visual (networkx/matplotlib PNG,
        # nodes=artists colored by modal genre, sized by track count).
        _section_label("🕸  Genre Graph")

        _tool_btn("Rebuild genre graph",
                  self._rebuild_genre_graph_gui,
                  FG_TEAL,
                  desc="Builds the artist co-occurrence graph from the current genre cache "
                       "and saves it to genre_graph.json. Read-only except for that one file; "
                       "no device/library changes.")

        _tool_btn("Visualize genre graph",
                  self._visualize_genre_graph_gui,
                  FG_TEAL,
                  desc="Renders the saved graph to genre_graph.png (top ~250 most-connected "
                       "artists, colored by genre) and opens it. Run 'Rebuild genre graph' "
                       "first if it's missing or stale.")

        # ── 4f. ALBUM COMPLETENESS (added 2026-09-04) ────────────────────────
        _section_label("💿  Album Completeness")

        _tool_btn("Check album completeness",
                  self._completeness_report_gui,
                  FG_TEAL,
                  desc="For every album qualifying under the a/b/c rules (saved, 3+ liked "
                       "tracks, or artist has a saved album), checks the real device library "
                       "against the full expected tracklist and reports what's missing. "
                       "Read-only — writes completeness_report.json, downloads nothing.")

        # ── 4g. FAVORITES / PLAYLISTS PLAN (added 2026-09-04) ────────────────
        # The native app has no batch-write path for Favorites/Playlists right
        # now (no root; its own ContentProvider isn't exported) and its UI is
        # one-track-at-a-time even once that's unblocked. This resolves every
        # Spotify Liked / playlist track to its real on-device file ONCE and
        # saves the worklist, so whichever automation path eventually works
        # just consumes this instead of re-matching titles live.
        _section_label("⭐  Favorites / Playlists Plan")

        _tool_btn("Build favorites/playlists plan",
                  self._favplaylist_plan_gui,
                  FG_TEAL,
                  desc="Matches every Liked Song and playlist track against the real device "
                       "library (fast bulk scan) and saves the resolved/missing worklist to "
                       "favorites_playlists_plan.json. Read-only — no device writes.")

        _tool_btn("View favorites/playlists plan (favorites_playlists_plan.json)",
                  lambda: _open_json_viewer(self, "Favorites/Playlists Plan", pathlib.Path("favorites_playlists_plan.json")),
                  FG_TEAL,
                  desc="Result of the last 'Build favorites/playlists plan' run")

        # ── 4d. JSON OUTPUT VIEWERS (added 2026-07-26) ───────────────────────
        # Pretty-printed, read-only views of the .json files our own tools
        # write, so they can be inspected without an external editor.
        _section_label("📄  JSON Output Viewers")

        _tool_btn("View last duplicate scan (.dedup_log.json)",
                  lambda: _open_json_viewer(self, "Last Duplicate Scan", pathlib.Path(".dedup_log.json"), latest_of_list=True),
                  FG_TEAL,
                  desc="Most recent scan-duplicates result — candidates found, by reason")

        _tool_btn("View structured dedup plan (.dedup_plan.json)",
                  lambda: _open_json_viewer(self, "Dedup Plan", pathlib.Path(".dedup_plan.json")),
                  FG_TEAL,
                  desc="Categorized/grouped view of the same candidates, written alongside the journal")

        _tool_btn("View device snapshot (walkman_snapshot.json)",
                  lambda: _open_json_viewer(self, "Device Snapshot", pathlib.Path("walkman_snapshot.json")),
                  FG_TEAL,
                  desc="Full on-device file listing from the last COM/MTP scan (can be large)")

        _tool_btn("View album completeness report (completeness_report.json)",
                  lambda: _open_json_viewer(self, "Album Completeness Report", pathlib.Path("completeness_report.json")),
                  FG_TEAL,
                  desc="Result of the last 'Check album completeness' run — incomplete albums and their missing tracks")

        _tool_btn("View deep-clean progress (.deep_clean_progress.json)",
                  lambda: _open_json_viewer(self, "Deep-Clean Progress", pathlib.Path(".deep_clean_progress.json")),
                  FG_TEAL,
                  desc="Which files deep-clean has already processed — done list + deferred/hung albums")

        _tool_btn("View tag snapshot (walkman_tag_snapshot.json)",
                  lambda: _open_json_viewer(self, "Tag Snapshot", pathlib.Path("walkman_tag_snapshot.json")),
                  FG_TEAL,
                  desc="Real embedded tags read from the device — what the ZX500 player actually sees, keyed by device path")

        # ── 4e. SNAPSHOT REFRESH (added 2026-07-28) ──────────────────────────
        # walkman_snapshot.json (MediaStore file listing) auto-refreshes every
        # audit/reorganize/full run; walkman_tag_snapshot.json (real embedded
        # tags, used by cohort_classifier for skip/low/medium/high tiering)
        # does NOT — it only updates when this is run explicitly. A stale tag
        # snapshot degrades silently: cohort_classifier just falls back to
        # treating every file as "high" risk instead of erroring, which is
        # exactly what happened here (confirmed live: an 1420-file audit with
        # a tag snapshot from before the library moved to the SD card reported
        # cohorts high=1420, everything else 0). Every reader of the tag
        # snapshot re-reads it fresh from disk on each call — none of them
        # cache it in memory — so replacing the file here is all that's needed
        # for the very next audit/reorganize/full run to use the new data.
        _section_label("🔄  Snapshot Refresh")

        _tool_btn("Refresh tag snapshot from device",
                  self._refresh_tag_snapshot_gui,
                  FG_AMBER,
                  desc="Replaces walkman_tag_snapshot.json with a fresh read of every track's real "
                       "embedded tags — run this after big library changes (e.g. moving storage, "
                       "reorganizing) so audit/reorganize tiering isn't working off stale data")

        # ── 5. GENRE CACHE INSPECTOR ────────────────────────────────────────
        _section_label("🎵  Genre Cache Inspector")

        _tool_btn("Search genre cache",
                  self._inspect_genre_cache,
                  FG_TEAL,
                  desc="Search .genre_cache.json by artist name — see what genre is cached and delete individual entries")

        _tool_btn("Show cache statistics",
                  self._genre_cache_stats,
                  FG_GREY,
                  desc="Count cached entries, show top genres, find empty/Unknown genre entries")

        # ── 5. BATCH REBUILD ────────────────────────────────────────────────
        _section_label("🔄  Batch Rebuild (TIDAL)")

        _tool_btn("Configure batch rebuild artists",
                  self._configure_batch_rebuild,
                  FG_AMBER,
                  desc="Set REBUILD_BATCH_ARTISTS or REBUILD_BATCH_LETTERS for targeted re-download")

        _tool_btn("Run batch rebuild",
                  self._run_batch_rebuild_gui,
                  FG_RED,
                  desc="Re-download and replace specific artists on device from TIDAL — use carefully")

        # ── 6. MISC ─────────────────────────────────────────────────────────
        _section_label("⚙️  Misc")

        _tool_btn("Open project folder",
                  lambda: _subprocess.Popen(["explorer", str(config.BASE_DIR)]),
                  FG_GREY,
                  desc="Open the project directory in Windows Explorer")

        _tool_btn("Test AI providers",
                  self._test_ai_providers,
                  FG_GREY,
                  desc="Fire a test call to Gemini and Groq to confirm API keys work")

        _tool_btn("Show config values",
                  self._show_config,
                  FG_GREY,
                  desc="Display current config.py settings in a popup — useful for diagnosing wrong storage names etc.")

        # ── 7. SETTINGS ─────────────────────────────────────────────────────
        _section_label("🔑  Settings", FG_BLUE)

        _tool_btn("Edit API keys...",
                  self._edit_api_keys,
                  FG_BLUE,
                  desc="Update GROQ / Gemini / Anthropic / DeepSeek keys in .env without restarting")

        _tool_btn("Edit audit settings...",
                  self._edit_audit_settings,
                  FG_BLUE,
                  desc="Change AUDIT_STALE_DAYS and AUDIT_BATCH_SIZE for this session")

        # ── 7b. FEATURE TOGGLES (added 2026-07-24) ───────────────────────────
        # Checkboxes persist straight to config.py (same approach as
        # _edit_api_keys() persisting to .env) and update the in-memory
        # config module immediately, so the change applies to this session
        # too without a restart.
        _section_label("🎛️  Feature Toggles", FG_BLUE)

        def _config_toggle(text, flag_name, desc=None):
            row = tk.Frame(inner, bg=BG_PANEL)
            row.pack(fill="x", padx=8, pady=2)
            var = tk.BooleanVar(value=bool(getattr(config, flag_name, False)))
            cb = tk.Checkbutton(
                row, text=text, variable=var,
                command=lambda: self._set_config_flag(flag_name, var.get()),
                bg=BG_PANEL, fg=FG_WHITE, selectcolor=BG_DARK,
                activebackground=BG_PANEL, activeforeground=FG_WHITE,
                font=("Segoe UI", 9), anchor="w",
            )
            cb.pack(fill="x")
            if desc:
                tk.Label(row, text=desc, bg=BG_PANEL, fg="#555577",
                         font=("Segoe UI", 7), wraplength=240, justify="left").pack(anchor="w", padx=4)
            return cb

        _config_toggle("OFFLINE_MODE", "OFFLINE_MODE",
                        desc="ON: build the track list from your local Spotify export, no API calls "
                             "(works without Premium). OFF: use the live Spotify API — playlists/liked "
                             "songs/saved albums all need a Premium account on Spotify's current API "
                             "policy, and will fail with HTTP 403 without one.")

        _config_toggle("LOCALSEND_ENABLED", "LOCALSEND_ENABLED",
                        desc="Try WiFi transfer (LocalSend) before falling back to ADB/COM")

        _config_toggle("DEDUP_PRE_REORGANIZE", "DEDUP_PRE_REORGANIZE",
                        desc="Run a soft dedup pass automatically before each reorganize")

        _config_toggle("VERIFY_TRANSFERS_ENABLED", "VERIFY_TRANSFERS_ENABLED",
                        desc="Verify COM/PS transfers actually landed the right file size before accepting them")

        _config_toggle("AUDIT_DEVICE_ONLY", "AUDIT_DEVICE_ONLY",
                        desc="Skip the local-files phase during audit — test the device path in isolation")

        _config_toggle("LISTENBRAINZ_DISCOVERY_ENABLED", "LISTENBRAINZ_DISCOVERY_ENABLED",
                        desc="Let the main download pipeline actually queue ListenBrainz recommendations (preview first!)")

        _config_toggle("ACOUSTID_AUTO_CORRECT_ENABLED", "ACOUSTID_AUTO_CORRECT_ENABLED",
                        desc="Let audit auto-apply AcoustID fingerprint corrections for blank-tag files (preview first!)")

        # ── 8. Rev 14.7 — ALBUM PROMOTION (DRY-RUN) ─────────────────────────
        # Always-safe: builds a plan and writes it to disk. Does NOT touch the
        # device library unless config.PROMOTE_ENABLED is set to True in
        # config.py AND the plan has been reviewed by the operator.
        _section_label("🎼  Album Promotion")

        _tool_btn("Build promotion plan (dry-run)",
                  self._promote_dryrun,
                  FG_AMBER,
                  desc="Scans the library for duplicate copies of the same track and orphan album folders. "
                       "Writes walkman_promote_plan.txt. Never modifies files.")

        _tool_btn("Execute promotion plan...",
                  self._promote_execute,
                  FG_RED,
                  desc="DESTRUCTIVE. Runs dry-run, then after confirmation deletes losers and relocates "
                       "orphan folders. Requires PROMOTE_ENABLED=True in config.py.")

        _tool_btn("Rollback last promotion...",
                  self._promote_rollback,
                  FG_GREY,
                  desc="Best-effort undo using walkman_promote_journal.json. Can restore relocations "
                       "but cannot undo deletes — back up before executing.")

        _tool_btn("Execute promotion plan...",
                  self._promote_execute,
                  FG_RED,
                  desc="DESTRUCTIVE. Requires config.PROMOTE_ENABLED=True. Reads the last dry-run plan, "
                       "asks you to type PROMOTE to confirm, writes a rollback journal, then deletes "
                       "duplicate losers and relocates orphan album folders on the device.")

        _tool_btn("Rollback last promotion",
                  self._promote_rollback,
                  FG_GREY,
                  desc="Reverses relocations from the last execute (deletes cannot be undone). "
                       "Reads walkman_promote_rollback.json — the journal written before the execute.")

    # -----------------------------------------------------------------------
    # Tools panel toggle
    # -----------------------------------------------------------------------

    def _toggle_tools(self):
        if self._tools_visible.get():
            self._tools_panel.pack_forget()
            self._tools_visible.set(False)
        else:
            self._tools_panel.pack(side="right", fill="y", before=self._main_col)
            self._tools_visible.set(True)

    # -----------------------------------------------------------------------
    # Device detection
    # -----------------------------------------------------------------------

    def _detect_storage(self):
        self._append_log("[Detect] Scanning MTP devices...", "INFO")
        def _run():
            diag = mtp.diagnose_mtp()
            self._append_log(diag, "INFO")
            roots = mtp.list_storage_roots(config.DEVICE_NAME)
            if not roots:
                self.after(0, lambda: self._roots_var.set(
                    "Device not found — check log for details"
                ))
                return
            self.after(0, lambda: self._roots_var.set("Roots: " + ", ".join(roots)))

            # REV 14.9.1 (F1) — DO NOT auto-overwrite config.WRITE_STORAGE.
            # Pre-14.9.1, this routine blindly set WRITE_STORAGE to whichever
            # root contained 'internal' — clobbering the user's config.py
            # choice on every Detect click.  LOG-13 Run 2 captured the
            # consequence: WRITE_STORAGE was overwritten to 'Internal shared
            # storage' while the user's internal volume was 100% full (5 MB
            # free of 52 GB).  Every transfer then went to the doomed volume
            # and the SD card (219 GB free, configured as SD_STORAGE) was
            # ignored.
            #
            # New behaviour: we only DISCOVER the actual root names (for
            # variants like "Internal storage" vs "Internal shared storage")
            # and STORE them in config.STORAGE_ROOTS so the rest of the
            # pipeline can enumerate them.  We never change WRITE_STORAGE.
            # If the user's configured WRITE_STORAGE doesn't match any root
            # on this device, we log a loud warning so they can fix config.
            discovered_internal = None
            discovered_sd       = None
            for r in roots:
                r_low = r.lower()
                if "internal" in r_low:
                    discovered_internal = r
                elif "sd" in r_low or "card" in r_low:
                    discovered_sd = r
                # Make sure every discovered root is in STORAGE_ROOTS so
                # the free-space check, sweeper, and index know about them.
                existing = {s.lower() for s, _ in config.STORAGE_ROOTS}
                if r.lower() not in existing:
                    config.STORAGE_ROOTS += [(r, "Music"), (r, "Downloads")]

            # Validate that the configured WRITE_STORAGE actually exists on
            # this device.
            if config.WRITE_STORAGE not in roots:
                self._append_log(
                    f"[Detect] WARNING: config.WRITE_STORAGE={config.WRITE_STORAGE!r} "
                    f"is not one of the device roots {roots}.  Transfers will "
                    f"fail until you edit config.py to use one of these names. "
                    f"Discovered internal: {discovered_internal!r}, "
                    f"discovered SD: {discovered_sd!r}.",
                    "WARNING",
                )
            else:
                # Also warn if WRITE_STORAGE is internal and the device is
                # known to be nearly full (free-space pre-flight happened in
                # walkman_sync._rev149_preflight and logged CRITICAL).
                # Not re-run here to keep Detect fast.
                pass

            self._append_log(
                f"[Detect] Roots: {roots}  |  WRITE={config.WRITE_STORAGE}  "
                f"(config.py wins; GUI does not override)", "INFO"
            )
        t = threading.Thread(target=_run, daemon=True)
        t.start()

        def _timeout_guard():
            t.join(timeout=20)
            if t.is_alive():
                self._append_log(
                    "[Detect] No response after 20s — MTP session may be dirty. "
                    "Unplug the Walkman USB, wait 5s, replug, then click Detect again.",
                    "WARNING",
                )
        threading.Thread(target=_timeout_guard, daemon=True).start()

    # -----------------------------------------------------------------------
    # Primary button actions
    # -----------------------------------------------------------------------

    def _start_full_pipeline(self):
        """
        MK10: Full five-stage pipeline in one click:
          1. Pre-sync dedup (hard delete — frees space)
          2. Full sync (download → tag → transfer)
          3. Post-sync audit (Stage 8 — tag corrections on device)
          4. Pre-reorg dedup (soft delete — moves to .deleted/)
          5. Reorganize (canonical folder/filename structure)
          6. Post-reorganize audit (verify tags on moved files)
          7. Walkman DB refresh
        """
        # Force audit + reorganize on for the full pipeline run
        config.AUDIT_AFTER_SYNC = True
        if self._force_audit_var.get():
            import audit as _a_mod
            _a_mod._AUDIT_STATE = {}
        if self._nuke_audit_var.get():
            self._delete_audit_state()
        self._reset_stats()
        self._set_buttons_running()
        threading.Thread(target=self._full_pipeline_thread, daemon=True).start()

    def _start_sync(self):
        config.AUDIT_AFTER_SYNC = self._audit_var.get()
        config.DOWNLOAD_MISSING = self._dl_var.get()
        if self._force_audit_var.get():
            config.AUDIT_STALE_DAYS = 0
            try:
                import meta_key as _mk; _mk.invalidate_all()
            except ImportError:
                pass
        if self._nuke_audit_var.get():
            self._delete_audit_state()
        self._reset_stats()
        self._set_buttons_running()
        threading.Thread(target=self._sync_thread, daemon=True).start()

    def _start_download_only(self):
        config.DOWNLOAD_MISSING = True
        self._reset_stats()
        self._set_buttons_running()
        threading.Thread(target=self._download_thread, daemon=True).start()

    def _start_audit_only(self):
        if self._force_audit_var.get():
            config.AUDIT_STALE_DAYS = 0
            try:
                import meta_key as _mk; _mk.invalidate_all()
            except ImportError:
                pass
        config.AUDIT_FOLDER_MODE = self._folder_mode_var.get()
        if self._nuke_audit_var.get():
            self._delete_audit_state()
        self._reset_stats()
        self._set_buttons_running()
        threading.Thread(target=self._audit_thread, daemon=True).start()

    def _start_reorganize(self):
        self._reset_stats()
        self._set_buttons_running()
        threading.Thread(target=self._reorganize_thread, daemon=True).start()

    def _start_curate(self):
        self._reset_stats()
        self._set_buttons_running()
        threading.Thread(target=self._curate_thread, daemon=True).start()

    def _curate_thread(self):
        with _SleepBlocker("WalkmanSync-curate"):
            try:
                self._cancel_flag = [False]
                self._spiral_run("curate", dry_run=self._reorg_dryrun_var.get())
            except Exception as e:
                self._append_log(f"[Curate] error: {e}", "ERROR")
            finally:
                self.after(0, self._done)

    def _cancel(self):
        self._cancel_flag[0] = True
        if self._agent:
            self._agent.cancel()
        # Kill the pipeline subprocess tree if one is running (GUI_SUBPROCESS_PIPELINE).
        try:
            self._kill_spiral_proc()
        except Exception:
            pass
        self._btn_cancel.config(state="disabled", text="Cancelling ...")

    # -----------------------------------------------------------------------
    # Cache management tools
    # -----------------------------------------------------------------------

    def _rebuild_genre_cache(self):
        # 2026-09-05: wraps genre_backfill.backfill_empty_genres() (existing
        # module / CLI, no GUI entry before this). Fills cache rows whose
        # genre is "" without needing a full audit/sync pass.
        try:
            import genre_backfill as _gb
            n_empty = _gb.get_empty_count()
        except Exception as e:
            messagebox.showerror("Error", f"Could not load genre backfill: {e}")
            return
        if n_empty == 0:
            messagebox.showinfo("Rebuild empty genre entries",
                                "No empty genre entries in the cache — nothing to rebuild.")
            return
        if not messagebox.askyesno(
            "Rebuild empty genre entries",
            f"{n_empty} cache entr{'y' if n_empty == 1 else 'ies'} have a blank genre.\n\n"
            f"Fill them now via the Groq genre waterfall? This runs in the background "
            f"and may take a few minutes."
        ):
            return

        def _work():
            try:
                res = _gb.backfill_empty_genres(dry_run=False)
                self._append_log(f"[Tools] Genre backfill: {res.summary()}", "INFO")
                self.after(0, lambda: messagebox.showinfo("Done", res.summary()))
            except Exception as ex:
                self._append_log(f"[Tools] Genre backfill failed: {ex}", "ERROR")
                self.after(0, lambda: messagebox.showerror("Error", str(ex)))

        self._append_log(f"[Tools] Rebuilding {n_empty} empty genre entries...", "INFO")
        threading.Thread(target=_work, daemon=True).start()

    def _clear_genre_cache(self):
        if not messagebox.askyesno(
            "Clear genre cache",
            "Delete .genre_cache.json?\n\nAll cached genre/year lookups will be discarded. "
            "Next sync will re-query all APIs — this will be slow for large libraries.\n\n"
            "Only do this if many tracks have wrong genres."
        ):
            return
        try:
            if config.GENRE_CACHE_FILE.exists():
                config.GENRE_CACHE_FILE.unlink()
            self._append_log("[Tools] Genre cache cleared — will re-query all APIs on next run", "WARNING")
            messagebox.showinfo("Done", "Genre cache deleted successfully.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _clear_genre_cache_for_artist(self):
        artist = simpledialog.askstring(
            "Clear genre cache for artist",
            "Enter artist name (partial match OK):\ne.g.  MF DOOM  or  cochise  or  viktor",
            parent=self,
        )
        if not artist:
            return

        import unicodedata

        def _norm(s):
            s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
            s = re.sub(r"[^a-z0-9 ]", "", s.lower())
            s = re.sub(r"^(the |a |an )", "", s.strip())
            return re.sub(r"\s+", " ", s).strip()

        needle = _norm(artist)

        try:
            if not config.GENRE_CACHE_FILE.exists():
                messagebox.showinfo("Genre cache", "Cache file does not exist.")
                return

            cache = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
            removed = [k for k in list(cache.keys()) if needle in k.split("|")[0]]
            for k in removed:
                del cache[k]
            config.GENRE_CACHE_FILE.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            msg = (f"Removed {len(removed)} cache entries matching '{artist}'.\n\n"
                   "First 10 removed:\n" + "\n".join(removed[:10]))
            self._append_log(f"[Tools] Genre cache: removed {len(removed)} entries for '{artist}'", "WARNING")
            messagebox.showinfo("Done", msg)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _clear_spotify_cache(self):
        if not messagebox.askyesno(
            "Clear Spotify metadata cache",
            "Delete .spotify_metadata_cache.json?\n\n"
            "Spotify playlist and track data will be re-fetched on next run."
        ):
            return
        try:
            if config.CACHE_FILE.exists():
                config.CACHE_FILE.unlink()
            self._append_log("[Tools] Spotify metadata cache cleared", "WARNING")
            messagebox.showinfo("Done", "Spotify metadata cache deleted.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _clear_spotify_token(self):
        if not messagebox.askyesno(
            "Clear Spotify auth token",
            "Delete .spotify_auth_cache?\n\n"
            "You will need to log in via browser on the next sync."
        ):
            return
        try:
            if config.TOKEN_CACHE.exists():
                config.TOKEN_CACHE.unlink()
            self._append_log("[Tools] Spotify auth token cleared — will re-login on next run", "WARNING")
            messagebox.showinfo("Done", "Spotify auth token deleted. Browser login required on next run.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _clear_tidal_cache(self):
        tidal_cache = config.BASE_DIR / ".tidal_album_cache.json"
        if not messagebox.askyesno(
            "Clear TIDAL album cache",
            "Delete .tidal_album_cache.json?\n\n"
            "TIDAL album IDs will be re-resolved on next download run."
        ):
            return
        try:
            if tidal_cache.exists():
                tidal_cache.unlink()
            self._append_log("[Tools] TIDAL album cache cleared", "WARNING")
            messagebox.showinfo("Done", "TIDAL album cache deleted.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _nuke_audit_now(self):
        if not messagebox.askyesno(
            "Nuke audit state",
            "Delete .audit_state.json and clear in-memory metakeys?\n\n"
            "The next audit will re-check EVERY file on the device. "
            "This can take a long time for large libraries."
        ):
            return
        self._delete_audit_state()
        messagebox.showinfo("Done", "Audit state nuked. Next audit will re-check all device files.")

    def _delete_audit_state(self):
        try:
            import meta_key as _mk
            _mk.invalidate_all()
            self._append_log("[Tools] Audit state nuked — in-memory + file cache cleared", "WARNING")
        except Exception as e:
            self._append_log(f"Could not clear audit state: {e}", "ERROR")

    def _fix_genre_overrides(self):
        """Remove known-wrong cache entries for artists the override dict now fixes."""
        # Artists whose Last.fm/Deezer results are wrong and are now overridden
        KNOWN_BAD = [
            # (normalised_artist_prefix, wrong_genre, correct_genre)
            ("mf doom",        "Alternative Rock",  "Abstract Hip-Hop"),
            ("viktor vaughn",  "Alternative Rock",  "Abstract Hip-Hop"),
            ("dangerdoom",     "Alternative Rock",  "Abstract Hip-Hop"),
            ("madvillain",     "Alternative Rock",  "Abstract Hip-Hop"),
            ("cochise",        "Rap/Hip Hop",       "Cloud Rap"),
            ("outkast",        "",                  "Southern Hip-Hop"),
            ("vaudeville",     "Alternative Rock",  "Abstract Hip-Hop"),  # album in cache key
            # MK26 additions — from LOG_6_5_26 audit (Groq-only fallback produced these)
            ("weeknd",         "Afrobeats",         "Alt R&B"),
            ("luxury sa",      "Alternative Rock",  "Afro House"),
            ("babalwa m",      "Afrobeats",         "Afro House"),
            ("issa sisdoh",    "Afrobeats",         "Afro House"),
            ("dj kayd boizen", "Abstract Hip-Hop",  "Afro House"),
            ("peekay mzee",    "Abstract Hip-Hop",  "Afro House"),
            ("volka",          "Abstract Hip-Hop",  "Afro House"),
            ("beekay monalay", "Abstract Hip-Hop",  "Afro House"),
            ("shizuka kudo",   "Abstract Hip-Hop",  "City Pop"),
            ("kei ishiguro",   "Abstract Hip-Hop",  "City Pop"),
            ("kaoru akimoto",  "Abstract Hip-Hop",  "City Pop"),
        ]

        import unicodedata

        def _norm(s):
            s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
            s = re.sub(r"[^a-z0-9 ]", "", s.lower())
            s = re.sub(r"^(the |a |an )", "", s.strip())
            return re.sub(r"\s+", " ", s).strip()

        if not config.GENRE_CACHE_FILE.exists():
            messagebox.showinfo("Genre cache", "Cache file does not exist — nothing to fix.")
            return

        try:
            cache = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
            removed = []
            for key in list(cache.keys()):
                artist_norm = key.split("|")[0]
                for prefix, wrong_genre, _ in KNOWN_BAD:
                    if prefix in artist_norm:
                        entry = cache[key]
                        cached_genre = entry.get("genre", "") if isinstance(entry, dict) else ""
                        # Remove if genre is wrong OR empty (let override fill it)
                        if not wrong_genre or cached_genre == wrong_genre or not cached_genre:
                            del cache[key]
                            removed.append(f"{key}  (was: '{cached_genre}')")
                            break

            config.GENRE_CACHE_FILE.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # MK26 Fix: Also invalidate metakeys for corrected artists so the
            # next audit re-runs instead of skipping (perpetual-wrong-tag lock).
            mk_invalidated = 0
            try:
                import meta_key as _mk_fix
                for artist_prefix, _, _ in KNOWN_BAD:
                    _mk_fix.invalidate_by_artist(artist_prefix)
                    mk_invalidated += 1
                self._append_log(
                    f"[Tools] Invalidated metakeys for {mk_invalidated} artists "
                    f"— next audit will re-check and apply correct genres.",
                    "INFO"
                )
            except Exception as _mk_e:
                pass  # non-fatal

            summary = "\n".join([
                f"Removed {len(removed)} entries with wrong/empty genres.",
                "",
                "Artists corrected:",
            ] + [f"  {artist}" for artist, _, _ in KNOWN_BAD] + [
                "",
                "First 15 removed cache keys:"
            ] + removed[:15])

            self._append_log(
                f"[Tools] Fixed genre overrides: removed {len(removed)} bad cache entries", "WARNING"
            )
            messagebox.showinfo("Genre overrides fixed", summary)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # -----------------------------------------------------------------------
    # Log tools
    # -----------------------------------------------------------------------

    def _export_log(self):
        if not config.LOG_FILE.exists():
            messagebox.showinfo("Export", "sync_log.txt not found.")
            return
        desktop = pathlib.Path.home() / "Desktop"
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = desktop / f"walkman_sync_log_{timestamp}.txt"
        try:
            shutil.copy2(config.LOG_FILE, dest)
            self._append_log(f"[Tools] Log exported to {dest}", "INFO")
            messagebox.showinfo("Exported", f"Log saved to:\n{dest}")
        except Exception as e:
            messagebox.showerror("Export error", str(e))

    # -----------------------------------------------------------------------
    # ADB / Device tools
    # -----------------------------------------------------------------------

    def _purge_soft_deleted(self):
        """Delete Music/.deleted/ from all storages via targeted ParseName lookup."""
        if not messagebox.askyesno(
            "Purge .deleted folder",
            "This will permanently delete Music/.deleted/ from all Walkman storages.\n\n"
            "Soft-deleted files inside will be gone forever.\n\nProceed?",
        ):
            return

        def _run():
            self._append_log("[Purge] Deleting .deleted folder from all storages...", "INFO")
            try:
                import config as _cfg
                import wpd_transfer as _wpd
                device_name      = getattr(_cfg, "DEVICE_NAME", "WALKMAN")
                soft_delete_name = getattr(_cfg, "DEDUP_SOFT_DELETE_FOLDER", ".deleted")
                results = _wpd.purge_soft_deleted(device_name, soft_delete_name)
                lines = [f"  {k}: {v}" for k, v in results.items()]
                msg = "Purge complete:\n" + "\n".join(lines)
                self._append_log(f"[Purge] {msg}", "INFO")
                self.after(0, lambda: messagebox.showinfo("Purge .deleted", msg))
            except Exception as e:
                self._append_log(f"[Purge] Error: {e}", "ERROR")
                self.after(0, lambda: messagebox.showerror("Purge .deleted", str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_trigger_scan(self):
        """Send MediaScanner broadcast to make Walkman Music app pick up new tags."""
        def _run():
            self._append_log("[ADB] Sending MediaScanner broadcast...", "INFO")
            # Scan the whole Music folder recursively
            rc, out, err = _run_adb(
                "shell", "am", "broadcast",
                "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d", "file:///storage/emulated/0/Music",
                timeout=30,
            )
            if rc == 0:
                self._append_log(f"[ADB] MediaScanner triggered: {out}", "SUCCESS")
                self.after(0, lambda: messagebox.showinfo(
                    "MediaScanner triggered",
                    "Broadcast sent successfully.\n\n"
                    "The Walkman Music app will refresh its library.\n"
                    "If tracks still show wrong info, tap 'Update database' in the Music app settings.",
                ))
            else:
                self._append_log(f"[ADB] MediaScanner failed: {err}", "ERROR")
                self.after(0, lambda: messagebox.showerror("ADB error", err or "Unknown error"))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_free_space(self):
        def _run():
            self._append_log("[ADB] Querying device free space...", "INFO")
            rc, out, err = _run_adb("shell", "df", "-h", "/storage/emulated/0", timeout=15)
            rc2, out2, err2 = _run_adb("shell", "df", "-h", "/storage/sdcard1", timeout=15)
            result = []
            if rc == 0:
                result.append("Internal storage:\n" + out)
            if rc2 == 0:
                result.append("\nSD card:\n" + out2)
            if not result:
                result.append(err or "Could not query free space — is ADB connected?")
            msg = "\n".join(result)
            self._append_log(f"[ADB] Free space:\n{msg}", "INFO")
            self.after(0, lambda: messagebox.showinfo("Device free space", msg))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_list_files(self):
        def _run():
            self._append_log("[ADB] Listing device audio files (this may take a moment)...", "INFO")
            rc, out, err = _run_adb(
                "shell", "find", "/storage/emulated/0/Music",
                "-type", "f", r"\(", "-name", "*.mp3", "-o", "-name", "*.flac", r"\)",
                timeout=60,
            )
            if rc != 0 or not out:
                rc, out, err = _run_adb(
                    "shell", "find", "/sdcard/Music",
                    "-type", "f",
                    timeout=60,
                )
            if out:
                lines = out.splitlines()
                self._append_log(f"[ADB] Found {len(lines)} audio files on device", "INFO")
                # Show in viewer
                win = tk.Toplevel(self)
                win.title(f"Device audio files ({len(lines)})")
                win.geometry("700x500")
                win.configure(bg=BG_DARK)
                st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                                font=("Consolas", 8), relief="flat")
                st.pack(expand=True, fill="both", padx=6, pady=6)
                st.insert(tk.END, f"Found {len(lines)} audio files:\n\n")
                st.insert(tk.END, "\n".join(lines))
                st.config(state="disabled")
            else:
                self._append_log(f"[ADB] No files found or ADB error: {err}", "WARNING")
                self.after(0, lambda: messagebox.showwarning("ADB", err or "No audio files found"))

        threading.Thread(target=_run, daemon=True).start()

    def _restart_windows_explorer(self):
        """
        2026-08-21: kills and restarts explorer.exe to clear a stale
        WPD/Shell.Application COM session. This is a genuinely different
        reset than preflight.py's _close_walkman_explorer_windows() (which
        only closes windows navigated to the Walkman, not the whole shell
        process) or _cycle_wpd() (restarts the WPDBusEnum service, needs
        admin, off by default) — a full explorer.exe restart has been this
        project's most reliably effective fix for a degraded WPD session
        throughout its history, previously only ever run manually from a
        terminal. Confirms the session is actually healthy afterward via a
        cheap list_storage_roots() probe rather than just assuming success.
        """
        def _run():
            import time as _time
            self._append_log("[WPD] Restarting Windows Explorer...", "INFO")
            try:
                _subprocess.run(
                    ["taskkill", "/F", "/IM", "explorer.exe"],
                    capture_output=True, timeout=10,
                )
            except Exception as e:
                self._append_log(f"[WPD] taskkill failed (non-fatal, continuing): {e}", "WARNING")
            _time.sleep(2)
            try:
                _subprocess.Popen(["explorer.exe"])
            except Exception as e:
                self._append_log(f"[WPD] Could not relaunch explorer.exe: {e}", "ERROR")
                self.after(0, lambda: messagebox.showerror(
                    "WPD/MTP Reset", f"Could not relaunch explorer.exe: {e}\n\n"
                    "You may need to start it manually (Ctrl+Shift+Esc -> File -> "
                    "Run new task -> explorer.exe)."))
                return
            _time.sleep(2)

            try:
                import mtp_transfer_com as _com
                t0 = _time.time()
                roots = _com.list_storage_roots(getattr(config, "DEVICE_NAME", "WALKMAN"))
                elapsed = _time.time() - t0
                if roots:
                    msg = f"Explorer restarted. WPD session healthy: {roots} ({elapsed:.2f}s)"
                    self._append_log(f"[WPD] {msg}", "SUCCESS")
                    self.after(0, lambda: messagebox.showinfo("WPD/MTP Reset", msg))
                else:
                    msg = (f"Explorer restarted, but no storage roots found ({elapsed:.2f}s) — "
                           "device may not be connected, or needs a moment to settle.")
                    self._append_log(f"[WPD] {msg}", "WARNING")
                    self.after(0, lambda: messagebox.showwarning("WPD/MTP Reset", msg))
            except Exception as e:
                self._append_log(f"[WPD] Explorer restarted, but health check failed: {e}", "WARNING")
                self.after(0, lambda: messagebox.showwarning(
                    "WPD/MTP Reset", f"Explorer restarted. Health check failed: {e}"))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_check_devices(self):
        """
        2026-07-30: upgraded from a bare 'adb devices' dump to also report
        the parsed connection state and guided next steps per state,
        codifying a live troubleshooting session on this project's own
        ZX507 (see progress.md "Session: 2026-07-30") where distinguishing
        offline/unauthorized/authorizing/absent mattered a lot for picking
        the right fix.
        """
        _GUIDANCE = {
            "device": "Authorized and healthy.",
            "offline": ("Device enumerated but the ADB session died before/during the "
                       "handshake — the most common cause is an unstable USB link (a hub, "
                       "or a marginal cable/port): unplug from any hub and use a rear/root "
                       "port directly. If it keeps recurring even on a direct connection, "
                       "try 'Reset ADB pairing' below to force a fresh authorization prompt."),
            "unauthorized": ("The device declined this computer's key, or the prompt was "
                             "dismissed/timed out. Unlock the Walkman's screen and watch for "
                             "'Allow USB debugging?' — if no new prompt ever appears, use "
                             "'Reset ADB pairing' below to force one."),
            "authorizing": ("The 'Allow USB debugging?' prompt is up on the device screen "
                            "RIGHT NOW — go tap Allow immediately, this window can be as "
                            "short as a few seconds."),
            "no permissions": "Linux permissions issue (unexpected on this Windows-only setup).",
            "absent": ("adb sees nothing at all. Run 'ADB driver diagnostic' below to check "
                      "whether Windows even bound the ADB interface (vs. only MTP) — if that "
                      "looks fine, it's a cable/port/hardware issue, not something ADB itself "
                      "can retry past."),
        }

        def _run():
            import adb_transfer as _adb
            state = _adb.get_device_state()
            rc, out, err = _run_adb("devices", "-l", timeout=10)
            msg = out if out else err
            guidance = _GUIDANCE.get(state, f"Unrecognized state {state!r} — see raw output.")
            self._append_log(f"[ADB] Devices (state={state}):\n{msg}\n\n{guidance}", "INFO")
            self.after(0, lambda: messagebox.showinfo(
                "ADB devices", f"{msg or 'No output'}\n\nState: {state}\n\n{guidance}"))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_driver_diagnostic(self):
        def _run():
            self._append_log("[ADB] Checking Windows driver binding (Device Manager)...", "INFO")
            import adb_transfer as _adb
            result = _adb.diagnose_windows_driver()
            detail = result.get("detail", "(no detail)")
            level = "SUCCESS" if result.get("ok") else "WARNING"
            self._append_log(f"[ADB] Driver diagnostic: {detail}", level)
            box = messagebox.showinfo if result.get("ok") else messagebox.showwarning
            self.after(0, lambda: box("ADB Driver Diagnostic", detail))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_reset_pairing(self):
        if not messagebox.askyesno(
            "Reset ADB Pairing",
            "This backs up this PC's ADB RSA key pair (renamed, not deleted) and restarts "
            "the adb server. A brand-new 'Allow USB debugging?' prompt will appear on the "
            "device next time it connects — you'll need to accept it there.\n\nContinue?",
        ):
            return

        def _run():
            self._append_log("[ADB] Resetting ADB pairing (backing up host key)...", "INFO")
            import adb_transfer as _adb
            ok, detail = _adb.reset_adb_pairing()
            self._append_log(f"[ADB] {detail}", "SUCCESS" if ok else "WARNING")
            self.after(0, lambda: messagebox.showinfo("Reset ADB Pairing", detail))

        threading.Thread(target=_run, daemon=True).start()

    # -----------------------------------------------------------------------
    # 2026-07-24 — generic cli_runner.py launcher + config-flag persistence
    # -----------------------------------------------------------------------

    def _run_cli_mode(self, mode_args: list, title: str, confirm_msg: str = None):
        """
        Launch cli_runner.py as a subprocess for one of its CLI-only modes
        (deep-clean, scan-duplicates, the preview-* commands,
        merge-splintered-folders) and stream its output live into both a
        dedicated viewer window and the main app's own log panel, so GUI
        users get the same visibility CLI users already had.
        """
        if confirm_msg and not messagebox.askyesno(title, confirm_msg):
            return

        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("820x560")
        win.configure(bg=BG_DARK)
        st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                        font=("Consolas", 8), relief="flat")
        st.pack(expand=True, fill="both", padx=6, pady=6)
        cmd_str = "python cli_runner.py " + " ".join(mode_args)
        st.insert(tk.END, f"$ {cmd_str}\n\n")

        def _run():
            self._append_log(f"[Tools] Running: {cmd_str}", "INFO")
            try:
                proc = _subprocess.Popen(
                    [_sys_early.executable, "cli_runner.py"] + mode_args,
                    stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    cwd=str(config.BASE_DIR),
                    # Give the child its own (hidden) console. Without this,
                    # a GUI-spawned child inherits no console at all, and
                    # pywinauto/comtypes UIA calls inside DialogWatcher can
                    # hard-crash the process with an access violation
                    # (0xC0000005) — reproduced: identical command run
                    # directly from a terminal (which has a real console)
                    # completes fine; run from this GUI dispatcher it died
                    # before any output past the MK38.1 init line.
                    creationflags=_subprocess.CREATE_NO_WINDOW,
                )
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    st.insert(tk.END, line + "\n")
                    st.see(tk.END)
                    self._append_log(f"[{title}] {line}", "INFO")
                proc.wait()
                done_msg = f"[{title}] Finished (exit code {proc.returncode})"
                self._append_log(done_msg, "SUCCESS" if proc.returncode == 0 else "ERROR")
                st.insert(tk.END, f"\n{done_msg}\n")
                st.see(tk.END)
                st.config(state="disabled")
            except Exception as e:
                self._append_log(f"[{title}] Failed to launch: {e}", "ERROR")
                st.insert(tk.END, f"\nFailed to launch: {e}\n")

        threading.Thread(target=_run, daemon=True).start()

    def _set_config_flag(self, flag_name: str, new_value: bool) -> bool:
        """
        Persist a boolean config.py flag to disk and update the in-memory
        config module so the change takes effect immediately for this
        session too. Mirrors _edit_api_keys()'s precedent of writing
        straight to the source file — config.py has no separate
        overrides-loading layer to hook into instead.
        """
        cfg_path = config.BASE_DIR / "config.py"
        try:
            text = cfg_path.read_text(encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Config", f"Could not read config.py: {e}")
            return False

        pattern = re.compile(
            rf"^{re.escape(flag_name)}(\s*:\s*bool)?\s*=\s*(True|False)\s*$",
            re.MULTILINE,
        )
        new_literal = "True" if new_value else "False"
        new_text, n = pattern.subn(
            lambda m: f"{flag_name}{m.group(1) or ''} = {new_literal}", text, count=1
        )
        if n == 0:
            messagebox.showerror("Config", f"Could not find '{flag_name}' in config.py")
            return False

        try:
            cfg_path.write_text(new_text, encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Config", f"Could not write config.py: {e}")
            return False

        setattr(config, flag_name, new_value)
        self._append_log(f"[Config] {flag_name} = {new_value} (saved to config.py)", "INFO")
        return True

    def _set_config_str(self, flag_name: str, new_value) -> bool:
        """
        Same as _set_config_flag but for a string|None value (e.g.
        ADB_WIFI_HOST) — the type-annotated literal on the right of `=` is
        replaced with `None` or a quoted string instead of True/False.
        """
        cfg_path = config.BASE_DIR / "config.py"
        try:
            text = cfg_path.read_text(encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Config", f"Could not read config.py: {e}")
            return False

        pattern = re.compile(
            rf"^{re.escape(flag_name)}(\s*:\s*[^=\n]+)?\s*=\s*.*$",
            re.MULTILINE,
        )
        literal = "None" if new_value is None else repr(str(new_value))
        new_text, n = pattern.subn(
            lambda m: f"{flag_name}{m.group(1) or ''} = {literal}", text, count=1
        )
        if n == 0:
            messagebox.showerror("Config", f"Could not find '{flag_name}' in config.py")
            return False

        try:
            cfg_path.write_text(new_text, encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Config", f"Could not write config.py: {e}")
            return False

        setattr(config, flag_name, new_value)
        self._append_log(f"[Config] {flag_name} = {literal} (saved to config.py)", "INFO")
        return True

    # -----------------------------------------------------------------------
    # 2026-07-24 — GUI buttons for cli_runner.py's scan/deep-clean/preview
    # modes, all routed through _run_cli_mode() above.
    # -----------------------------------------------------------------------

    def _scan_duplicates_gui(self):
        self._run_cli_mode(
            ["scan-duplicates"], "Scan for Duplicates",
        )

    def _deepclean_dryrun_gui(self):
        self._run_cli_mode(
            ["deep-clean", "--from-log"], "Deep-Clean (dry-run)",
        )

    def _deepclean_execute_gui(self):
        self._run_cli_mode(
            ["deep-clean", "--from-log", "--execute"], "Deep-Clean (EXECUTE)",
            confirm_msg="This permanently deletes duplicate/corrupt files identified by the last "
                         "'Scan for duplicates' run. Files are matched by exact stored path — nothing "
                         "outside .dedup_log.json's candidate list is touched. Continue?",
        )

    def _run_tool_script(self, tool_args: list, title: str, confirm: str = None):
        """Run one standalone curation script via cli_runner.py's `run-tool`
        delegator, streaming into the CLI-mode viewer + main log. `tool_args`
        is [script.py, *flags]."""
        self._run_cli_mode(["run-tool", *tool_args], title, confirm_msg=confirm)

    def _restar_favorites_gui(self):
        if not pathlib.Path("restar_merged.json").exists():
            messagebox.showwarning("Re-star favourites",
                                   "restar_merged.json not found — nothing queued.")
            return
        if not messagebox.askyesno(
                "Re-star favourites",
                "Star every (artist, title) in restar_merged.json via UI automation "
                "(one at a time, toggle-safe — each is pre-checked). Make sure you've "
                "run a device 'Update database' rescan first. Continue?"):
            return
        win = tk.Toplevel(self); win.title("Re-star favourites")
        win.geometry("820x560"); win.configure(bg=BG_DARK)
        st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                       font=("Consolas", 8), relief="flat")
        st.pack(expand=True, fill="both", padx=6, pady=6)

        def _run():
            env = {**os.environ, "WALKMAN_FAVPLAYLIST_PLAN": "restar_merged.json",
                   "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            self._append_log("[Tools] Running: walkman_favplaylist_apply.py favorites "
                             "(WALKMAN_FAVPLAYLIST_PLAN=restar_merged.json)", "INFO")
            try:
                proc = _subprocess.Popen(
                    [_sys_early.executable, "-u", "walkman_favplaylist_apply.py", "favorites"],
                    stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", cwd=str(config.BASE_DIR),
                    env=env, creationflags=_subprocess.CREATE_NO_WINDOW)
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    st.insert(tk.END, line + "\n"); st.see(tk.END)
                    self._append_log(f"[Re-star] {line}", "INFO")
                proc.wait()
                msg = f"[Re-star] Finished (exit {proc.returncode})"
                self._append_log(msg, "SUCCESS" if proc.returncode == 0 else "ERROR")
                st.insert(tk.END, f"\n{msg}\n"); st.config(state="disabled")
            except Exception as e:
                self._append_log(f"[Re-star] Failed: {e}", "ERROR")
                st.insert(tk.END, f"\nFailed: {e}\n")

        threading.Thread(target=_run, daemon=True).start()

    def _preview_genre_consolidation_gui(self):
        self._run_cli_mode(
            ["preview-genre-consolidation"], "Preview Genre Consolidation",
        )

    def _preview_acoustid_gui(self):
        self._run_cli_mode(
            ["preview-acoustid-corrections"], "Preview AcoustID Corrections",
        )

    def _preview_listenbrainz_gui(self):
        username = simpledialog.askstring(
            "Preview ListenBrainz Discovery",
            "ListenBrainz username:",
            initialvalue=getattr(config, "LISTENBRAINZ_USERNAME", ""),
            parent=self,
        )
        if not username:
            return
        self._run_cli_mode(
            ["preview-listenbrainz-discovery", "--username", username],
            "Preview ListenBrainz Discovery",
        )

    def _refresh_tag_snapshot_gui(self):
        self._run_cli_mode(
            ["refresh-tag-snapshot"], "Refresh Tag Snapshot",
            confirm_msg="Re-scans every track's real embedded tags from the device (pulls a small "
                        "head chunk of each file over ADB) and replaces walkman_tag_snapshot.json "
                        "outright. Read-only on the device — nothing is deleted or modified. Can "
                        "take several minutes for a large library. Continue?",
        )

    def _merge_splintered_dryrun_gui(self):
        self._run_cli_mode(
            ["merge-splintered-folders"], "Merge Splintered Folders (dry-run)",
        )

    def _merge_splintered_execute_gui(self):
        self._run_cli_mode(
            ["merge-splintered-folders", "--execute"], "Merge Splintered Folders (EXECUTE)",
            confirm_msg="This merges near-duplicate album folders on the device (e.g. truncated-name "
                         "variants of the same album) and deletes the losing copies. Continue?",
        )

    def _rebuild_genre_graph_gui(self):
        self._run_cli_mode(
            ["rebuild-genre-graph"], "Rebuild Genre Graph",
        )

    def _visualize_genre_graph_gui(self):
        self._run_cli_mode(
            ["visualize-genre-graph"], "Visualize Genre Graph",
        )

    def _completeness_report_gui(self):
        self._run_cli_mode(
            ["completeness-report"], "Check Album Completeness",
        )

    def _favplaylist_plan_gui(self):
        self._run_cli_mode(
            ["build-favorites-playlists-plan"], "Build Favorites/Playlists Plan",
        )

    def _adb_wifi_enable(self):
        """
        Arms ADB-over-WiFi on the currently USB-connected device (adb tcpip)
        and reports the resulting IP. Does not connect or touch config —
        that's the separate "Connect over WiFi" button, so the operator can
        confirm the reported IP before anything is persisted.
        """
        def _run():
            self._append_log("[ADB-WiFi] Enabling TCP/IP mode (requires USB now)...", "INFO")
            import adb_transfer as _adb
            ip = _adb.enable_wifi_debugging(port=getattr(config, "ADB_WIFI_PORT", 5555))
            if ip is None:
                msg = ("Could not enable ADB over WiFi. Requires the device connected "
                       "via USB right now with USB debugging authorized.")
                self._append_log(f"[ADB-WiFi] {msg}", "WARNING")
                self.after(0, lambda: messagebox.showwarning("ADB over WiFi", msg))
                return
            port = getattr(config, "ADB_WIFI_PORT", 5555)
            msg = (f"TCP/IP mode enabled. Device IP: {ip}:{port}\n\n"
                   "You can now unplug USB and click 'Connect over WiFi'.")
            self._append_log(f"[ADB-WiFi] {msg}", "SUCCESS")
            self.after(0, lambda: messagebox.showinfo("ADB over WiFi", msg))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_wifi_connect(self):
        """
        Connects to the device over WiFi ADB (must already be armed via
        'Enable ADB over WiFi', this session or a prior one — TCP/IP mode
        persists until reboot/adbd restart). On a genuine success, persists
        the IP:port and enables ADB_WIFI_ENABLED in config.py so
        cli_runner.py's reconnect logic can auto-fall-back to it later.
        """
        ip = simpledialog.askstring(
            "Connect over WiFi",
            "Device IP address (from 'Enable ADB over WiFi', or Settings -> "
            "About -> Status on the device):",
            parent=self,
            initialvalue=getattr(config, "ADB_WIFI_HOST", "") or "",
        )
        if not ip:
            return
        ip = ip.strip()

        def _run():
            port = getattr(config, "ADB_WIFI_PORT", 5555)
            self._append_log(f"[ADB-WiFi] Connecting to {ip}:{port}...", "INFO")
            import adb_transfer as _adb
            ok = _adb.connect_wifi(ip, port)
            if not ok:
                msg = (f"Could not connect to {ip}:{port}. Make sure 'Enable ADB over "
                       "WiFi' was run first (on this session or a prior one since the "
                       "last reboot/USB replug) and the IP is correct.")
                self._append_log(f"[ADB-WiFi] {msg}", "WARNING")
                self.after(0, lambda: messagebox.showwarning("ADB over WiFi", msg))
                return

            self._set_config_str("ADB_WIFI_HOST", ip)
            if not bool(getattr(config, "ADB_WIFI_ENABLED", False)):
                self._set_config_flag("ADB_WIFI_ENABLED", True)
            msg = (f"Connected to {ip}:{port} over WiFi ADB.\n\n"
                   "ADB_WIFI_ENABLED is now True and this IP is saved to config.py — "
                   "future runs will try this link automatically if USB ADB drops.")
            self._append_log(f"[ADB-WiFi] {msg}", "SUCCESS")
            self.after(0, lambda: messagebox.showinfo("ADB over WiFi", msg))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_wifi_disconnect(self):
        """
        Closes the WiFi ADB link. Does NOT change ADB_WIFI_ENABLED/HOST in
        config.py — this just disconnects the current session's link; the
        saved target is left in place so 'Connect over WiFi' (or the
        automatic reconnect fallback) can re-establish it later without
        re-entering the IP.
        """
        def _run():
            self._append_log("[ADB-WiFi] Disconnecting...", "INFO")
            import adb_transfer as _adb
            _adb.disconnect_wifi(
                getattr(config, "ADB_WIFI_HOST", None),
                getattr(config, "ADB_WIFI_PORT", 5555),
            )
            self._append_log("[ADB-WiFi] Disconnected.", "INFO")
            self.after(0, lambda: messagebox.showinfo("ADB over WiFi", "Disconnected."))

        threading.Thread(target=_run, daemon=True).start()

    def _localsend_check(self):
        """
        Discovers the Walkman's LocalSend receiver, then sends+moves a real
        throwaway 1KB test file through the full push_file_via_localsend()
        path (same probe transport_selftest.py uses) — discovery alone
        doesn't prove a transfer will actually work. Only on a genuine
        send+move success does this auto-enable config.LOCALSEND_ENABLED
        (persisted to config.py via _set_config_flag()); on any failure the
        flag is left untouched. 2026-07-30: previously discovery-only, per a
        2026-07-17 live "Blocked by another session" (HTTP 409) concern —
        that state was independently confirmed cleared, and per-user request
        this button now performs the real test instead.
        """
        def _run():
            self._append_log("[LocalSend] Discovering peers (5s)...", "INFO")
            try:
                import localsend_transfer as _ls
            except ImportError:
                self._append_log("[LocalSend] localsend_transfer module missing", "ERROR")
                self.after(0, lambda: messagebox.showerror(
                    "LocalSend", "localsend_transfer.py not found"))
                return

            peer = _ls.get_walkman_peer(rediscover=True)
            if peer is None:
                msg = ("No LocalSend peer found (deviceModel='SonyAudio'). "
                       "Is LocalSend running on the Walkman, and is it on the same WiFi network?")
                self._append_log(f"[LocalSend] {msg}", "WARNING")
                self.after(0, lambda: messagebox.showwarning("LocalSend", msg))
                return

            ip, port, protocol = peer
            self._append_log(
                f"[LocalSend] Peer found: {ip}:{port} ({protocol}) — sending a real "
                "throwaway test file...", "INFO")

            import tempfile
            tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="walkman_localsend_check_"))
            test_file = tmp_dir / "walkman_localsend_check.bin"
            test_file.write_bytes(b"\x00" * 1024)
            dest_parts = [".walkman_localsend_check"]
            device = getattr(config, "DEVICE_NAME", "WALKMAN")
            storage = "Internal shared storage"

            try:
                sent_ok = _ls.push_file_via_localsend(
                    src=test_file, dest_parts=dest_parts, device_name=device,
                    storage_name=storage, overwrite=True,
                )
            except Exception as e:
                sent_ok = False
                self._append_log(
                    f"[LocalSend] Test send raised {type(e).__name__}: {e}", "ERROR")

            # Best-effort cleanup (local + on-device) — never let this affect the result.
            try:
                test_file.unlink()
                tmp_dir.rmdir()
            except Exception:
                pass
            try:
                import adb_transfer as _adb
                _adb._adb_cmd([
                    "shell", "rm", "-rf",
                    _adb._get_storage_path(storage) + "/" + "/".join(dest_parts),
                ])
            except Exception:
                pass

            if sent_ok:
                was_enabled = bool(getattr(config, "LOCALSEND_ENABLED", False))
                if not was_enabled:
                    self._set_config_flag("LOCALSEND_ENABLED", True)
                status = (
                    "Real send+move test SUCCEEDED — LOCALSEND_ENABLED is now "
                    "True (saved to config.py)" if not was_enabled else
                    "Real send+move test SUCCEEDED — LOCALSEND_ENABLED was already True")
                self._append_log(f"[LocalSend] {status}", "SUCCESS")
                self.after(0, lambda: messagebox.showinfo(
                    "LocalSend", f"Peer found: {ip}:{port} ({protocol})\n\n{status}"))
            else:
                status = (
                    "Peer found, but the real send+move test FAILED — "
                    "LOCALSEND_ENABLED left unchanged "
                    f"({getattr(config, 'LOCALSEND_ENABLED', False)})")
                self._append_log(f"[LocalSend] {status}", "WARNING")
                self.after(0, lambda: messagebox.showwarning(
                    "LocalSend", f"Peer found: {ip}:{port} ({protocol})\n\n{status}"))

        threading.Thread(target=_run, daemon=True).start()

    def _adb_query_mediastore(self):
        artist = simpledialog.askstring(
            "Query MediaStore",
            "Enter track title or artist to search for in MediaStore DB:\n"
            "(leave blank to show first 50 entries)",
            parent=self,
        )
        def _run():
            self._append_log("[ADB] Querying MediaStore database...", "INFO")
            if artist:
                rc, out, err = _run_adb(
                    "shell", "content", "query",
                    "--uri", "content://media/external/audio/media",
                    "--projection", "title,artist,album_artist,album,year,genre",
                    "--where", f"title LIKE '%{artist}%' OR artist LIKE '%{artist}%'",
                    timeout=30,
                )
            else:
                rc, out, err = _run_adb(
                    "shell", "content", "query",
                    "--uri", "content://media/external/audio/media",
                    "--projection", "title,artist,album_artist,year,genre",
                    timeout=30,
                )

            result = out if out else (err or "No results")
            # Truncate if enormous
            lines = result.splitlines()
            if len(lines) > 100:
                result = "\n".join(lines[:100]) + f"\n\n... ({len(lines)} total rows — showing first 100)"

            win = tk.Toplevel(self)
            win.title("MediaStore query result")
            win.geometry("800x500")
            win.configure(bg=BG_DARK)
            st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                            font=("Consolas", 8), relief="flat")
            st.pack(expand=True, fill="both", padx=6, pady=6)
            st.insert(tk.END, result)
            st.config(state="disabled")

        threading.Thread(target=_run, daemon=True).start()

    def _adb_shell_popup(self):
        """Interactive ADB shell command runner popup."""
        win = tk.Toplevel(self)
        win.title("ADB Shell")
        win.geometry("720x480")
        win.configure(bg=BG_DARK)

        tk.Label(win, text="ADB Shell — enter commands, press Run or Enter",
                 bg=BG_DARK, fg=FG_ORANGE, font=("Segoe UI", 9, "bold")).pack(pady=(8, 4))

        output = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                            font=("Consolas", 9), relief="flat",
                                            state="disabled", height=20)
        output.pack(expand=True, fill="both", padx=8, pady=4)

        cmd_frame = tk.Frame(win, bg=BG_DARK)
        cmd_frame.pack(fill="x", padx=8, pady=(0, 8))

        cmd_var = tk.StringVar()
        tk.Label(cmd_frame, text="adb shell >", bg=BG_DARK, fg=FG_TEAL,
                 font=("Consolas", 10, "bold")).pack(side="left")
        entry = tk.Entry(cmd_frame, textvariable=cmd_var, bg=BG_BTN, fg=FG_WHITE,
                         font=("Consolas", 10), width=55, insertbackground=FG_WHITE)
        entry.pack(side="left", padx=(6, 4), expand=True, fill="x")
        entry.focus()

        def _run_cmd(*_):
            cmd_text = cmd_var.get().strip()
            if not cmd_text:
                return
            cmd_var.set("")
            output.config(state="normal")
            output.insert(tk.END, f"$ adb shell {cmd_text}\n", )

            def _exec():
                rc, out, err = _run_adb("shell", *cmd_text.split(), timeout=30)
                result = out or err or "(no output)"
                output.config(state="normal")
                output.insert(tk.END, result + "\n\n")
                output.config(state="disabled")
                output.see(tk.END)

            output.config(state="disabled")
            threading.Thread(target=_exec, daemon=True).start()

        entry.bind("<Return>", _run_cmd)
        tk.Button(cmd_frame, text="Run", command=_run_cmd,
                  bg=FG_TEAL, fg=BG_DARK, font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10, cursor="hand2").pack(side="left")

        # Quick command shortcuts
        shortcuts = tk.Frame(win, bg=BG_DARK)
        shortcuts.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(shortcuts, text="Quick:", bg=BG_DARK, fg=FG_GREY,
                 font=("Segoe UI", 8)).pack(side="left")
        for label, cmd in [
            ("devices",  "devices"),
            ("free space", "df -h /storage/emulated/0"),
            ("ls Music",  "ls /storage/emulated/0/Music"),
            ("MediaScan", "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///storage/emulated/0/Music"),
        ]:
            def _make_cb(c=cmd):
                def _cb():
                    cmd_var.set(c)
                    entry.focus()
                return _cb
            tk.Button(shortcuts, text=label, command=_make_cb(),
                      bg=BG_BTN, fg=FG_GREY, font=("Segoe UI", 8),
                      relief="flat", padx=6, pady=2, cursor="hand2").pack(side="left", padx=3)

    # -----------------------------------------------------------------------
    # Genre cache inspector
    # -----------------------------------------------------------------------

    def _inspect_genre_cache(self):
        artist = simpledialog.askstring(
            "Search genre cache",
            "Enter artist name to search in .genre_cache.json:",
            parent=self,
        )
        if not artist:
            return

        import unicodedata

        def _norm(s):
            s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
            s = re.sub(r"[^a-z0-9 ]", "", s.lower())
            s = re.sub(r"^(the |a |an )", "", s.strip())
            return re.sub(r"\s+", " ", s).strip()

        needle = _norm(artist)

        if not config.GENRE_CACHE_FILE.exists():
            messagebox.showinfo("Genre cache", "Cache file does not exist.")
            return

        cache = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
        matches = {k: v for k, v in cache.items() if needle in k.split("|")[0]}

        if not matches:
            messagebox.showinfo("Genre cache", f"No entries found for '{artist}'.")
            return

        win = tk.Toplevel(self)
        win.title(f"Genre cache: '{artist}' ({len(matches)} entries)")
        win.geometry("700x480")
        win.configure(bg=BG_DARK)

        tk.Label(win, text=f"{len(matches)} cached entries for '{artist}'",
                 bg=BG_DARK, fg=FG_TEAL, font=("Segoe UI", 9, "bold")).pack(pady=(8, 4))

        st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                        font=("Consolas", 9), relief="flat")
        st.pack(expand=True, fill="both", padx=8, pady=4)

        for k, v in sorted(matches.items()):
            if isinstance(v, dict):
                genre = v.get("genre", "")
                year  = v.get("year", "")
            else:
                genre, year = str(v), ""
            parts = k.split("|")
            title_part = parts[1] if len(parts) > 1 else ""
            colour_tag = "WARN" if not genre or genre == "Unknown" else "OK"
            line = f"  {title_part:<50}  genre={genre!r:20}  year={year!r}\n"
            st.insert(tk.END, line)

        st.config(state="disabled")

        # Delete button
        def _delete_all():
            if not messagebox.askyesno("Delete", f"Remove all {len(matches)} entries for '{artist}'?",
                                        parent=win):
                return
            fresh = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
            for k in matches:
                fresh.pop(k, None)
            config.GENRE_CACHE_FILE.write_text(
                json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._append_log(f"[Tools] Deleted {len(matches)} cache entries for '{artist}'", "WARNING")
            messagebox.showinfo("Done", f"Removed {len(matches)} entries.", parent=win)
            win.destroy()

        tk.Button(win, text=f"Delete all {len(matches)} entries for '{artist}'",
                  command=_delete_all,
                  bg=FG_RED, fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(pady=8)

    def _genre_cache_stats(self):
        if not config.GENRE_CACHE_FILE.exists():
            messagebox.showinfo("Genre cache", "Cache file does not exist.")
            return

        cache = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
        genre_counts: dict[str, int] = {}
        empty = 0
        unknown = 0

        for k, v in cache.items():
            genre = v.get("genre", "") if isinstance(v, dict) else str(v)
            if not genre:
                empty += 1
            elif genre.lower() in ("unknown", "unknown genre"):
                unknown += 1
            else:
                genre_counts[genre] = genre_counts.get(genre, 0) + 1

        top_genres = sorted(genre_counts.items(), key=lambda x: -x[1])[:25]
        lines = [
            f"Cache file:      {config.GENRE_CACHE_FILE}",
            f"Total entries:   {len(cache)}",
            f"Empty genre:     {empty}",
            f"'Unknown' genre: {unknown}",
            "",
            "Top 25 genres:",
        ] + [f"  {count:5d}  {genre}" for genre, count in top_genres]

        win = tk.Toplevel(self)
        win.title("Genre cache statistics")
        win.geometry("500x440")
        win.configure(bg=BG_DARK)
        st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                        font=("Consolas", 9), relief="flat")
        st.pack(expand=True, fill="both", padx=8, pady=8)
        st.insert(tk.END, "\n".join(lines))
        st.config(state="disabled")

    # -----------------------------------------------------------------------
    # Batch rebuild tools
    # -----------------------------------------------------------------------

    def _configure_batch_rebuild(self):
        win = tk.Toplevel(self)
        win.title("Configure batch rebuild")
        win.geometry("520x340")
        win.configure(bg=BG_DARK)

        tk.Label(win, text="Batch Rebuild Configuration",
                 bg=BG_DARK, fg=FG_ORANGE, font=("Segoe UI", 11, "bold")).pack(pady=(12, 6))

        tk.Label(win, text="Artist names (comma-separated, case-insensitive):",
                 bg=BG_DARK, fg=FG_GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=14)
        artists_var = tk.StringVar(value=", ".join(config.REBUILD_BATCH_ARTISTS))
        artists_entry = tk.Entry(win, textvariable=artists_var, bg=BG_BTN, fg=FG_WHITE,
                                  font=("Segoe UI", 10), width=60, insertbackground=FG_WHITE)
        artists_entry.pack(fill="x", padx=14, pady=4)

        tk.Label(win, text='First-letter prefixes (comma-separated, e.g. "O, T, M"):',
                 bg=BG_DARK, fg=FG_GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(8, 0))
        letters_var = tk.StringVar(value=", ".join(config.REBUILD_BATCH_LETTERS))
        letters_entry = tk.Entry(win, textvariable=letters_var, bg=BG_BTN, fg=FG_WHITE,
                                  font=("Segoe UI", 10), width=60, insertbackground=FG_WHITE)
        letters_entry.pack(fill="x", padx=14, pady=4)

        tk.Label(win, text="Min PC free space (GB) — abort if disk drops below this:",
                 bg=BG_DARK, fg=FG_GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(8, 0))
        free_var = tk.StringVar(value=str(config.REBUILD_PC_MIN_FREE_GB))
        tk.Entry(win, textvariable=free_var, bg=BG_BTN, fg=FG_WHITE,
                 font=("Segoe UI", 10), width=10, insertbackground=FG_WHITE).pack(anchor="w", padx=14, pady=4)

        def _save():
            raw_artists = [a.strip() for a in artists_var.get().split(",") if a.strip()]
            raw_letters = [l.strip().upper() for l in letters_var.get().split(",") if l.strip()]
            try:
                min_free = float(free_var.get())
            except ValueError:
                min_free = 10.0
            config.REBUILD_BATCH_ARTISTS  = raw_artists
            config.REBUILD_BATCH_LETTERS  = raw_letters
            config.REBUILD_PC_MIN_FREE_GB = min_free
            self._append_log(
                f"[Tools] Batch rebuild configured — artists={raw_artists}, letters={raw_letters}",
                "INFO"
            )
            messagebox.showinfo("Saved", f"Artists: {raw_artists}\nLetters: {raw_letters}", parent=win)
            win.destroy()

        tk.Button(win, text="Save", command=_save,
                  bg=FG_TEAL, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=16, pady=6, cursor="hand2").pack(pady=14)

    def _run_batch_rebuild_gui(self):
        if not config.REBUILD_BATCH_ARTISTS and not config.REBUILD_BATCH_LETTERS:
            messagebox.showwarning(
                "Batch rebuild",
                "No artists or letters configured.\n"
                "Use 'Configure batch rebuild artists' first."
            )
            return
        if not messagebox.askyesno(
            "Run batch rebuild",
            f"Re-download and replace:\n"
            f"  Artists: {config.REBUILD_BATCH_ARTISTS or '(none)'}\n"
            f"  Letters: {config.REBUILD_BATCH_LETTERS or '(none)'}\n\n"
            "This will download fresh FLAC files from TIDAL and transfer them to the Walkman. "
            "Continue?"
        ):
            return
        self._reset_stats()
        self._set_buttons_running()
        def _thread():
            with _SleepBlocker("WalkmanSync-rebuild"):
                try:
                    setup_logging(self._on_log)
                    run_batch_rebuild(log_callback=self._on_log)
                    self.after(0, lambda: messagebox.showinfo("Batch rebuild complete", "Done."))
                except Exception as exc:
                    self.after(0, lambda: messagebox.showerror("Batch rebuild error", str(exc)))
                    self._append_log(f"ERROR: {exc}", "ERROR")
                finally:
                    self.after(0, self._done)
        threading.Thread(target=_thread, daemon=True).start()

    # -----------------------------------------------------------------------
    # Misc tools
    # -----------------------------------------------------------------------

    def _test_ai_providers(self):
        def _run():
            self._append_log("[Tools] Testing AI providers...", "INFO")
            results = []

            # Gemini
            try:
                import ai_helper as _ai
                result = _ai.call_ai('Reply with {"status":"ok","provider":"gemini"}', max_tokens=30)
                if result:
                    results.append(f"✓ Gemini: {result}")
                    self._append_log(f"[Tools] Gemini OK: {result}", "SUCCESS")
                else:
                    results.append("✗ Gemini: no response (key missing or rate-limited)")
                    self._append_log("[Tools] Gemini: no response", "WARNING")
                    # MK15-HF2: pre-trip the Gemini circuit breaker if the
                    # tools test already shows Gemini is down. This prevents
                    # the 20-silent-429s pattern that sets streak=20 before
                    # Phase 1.5+2 even starts, wasting ~20 min of backoffs.
                    try:
                        import genre_client as _gc
                        for _ in range(getattr(_gc, "_GEMINI_429_THRESHOLD", 5)):
                            _gc._gemini_circuit_record(429)
                        self._append_log(
                            "[Tools] Gemini pre-tripped — will use Groq for this session",
                            "INFO",
                        )
                    except Exception:
                        pass
            except Exception as e:
                results.append(f"✗ Gemini: {e}")
                self._append_log(f"[Tools] Gemini error: {e}", "ERROR")

            # Groq
            try:
                import ai_helper as _ai
                result = _ai.test_groq()
                if result.get("ok"):
                    results.append(f"✓ Groq ({result.get('model','?')}): {result.get('response','')}")
                    self._append_log(f"[Tools] Groq OK: {result}", "SUCCESS")
                else:
                    results.append(f"✗ Groq: {result.get('error','failed')}")
                    self._append_log(f"[Tools] Groq failed: {result.get('error')}", "WARNING")
            except Exception as e:
                results.append(f"✗ Groq: {e}")
                self._append_log(f"[Tools] Groq error: {e}", "ERROR")

            self.after(0, lambda: messagebox.showinfo("AI provider test", "\n".join(results)))

        threading.Thread(target=_run, daemon=True).start()

    def _show_config(self):
        lines = [
            "Current config.py values",
            "=" * 40,
            f"DEVICE_NAME:        {config.DEVICE_NAME}",
            f"WRITE_STORAGE:      {config.WRITE_STORAGE}",
            f"WRITE_SUBFOLDER:    {config.WRITE_SUBFOLDER}",
            f"SD_STORAGE:         {config.SD_STORAGE}",
            f"STORAGE_ROOTS:      {config.STORAGE_ROOTS}",
            "",
            f"DOWNLOAD_MISSING:   {config.DOWNLOAD_MISSING}",
            f"DOWNLOAD_STRATEGY:  {config.DOWNLOAD_STRATEGY}",
            f"DOWNLOAD_DIR:       {config.DOWNLOAD_DIR}",
            "",
            f"AUDIT_AFTER_SYNC:   {config.AUDIT_AFTER_SYNC}",
            f"AUDIT_STALE_DAYS:   {config.AUDIT_STALE_DAYS}",
            f"AUDIT_BATCH_SIZE:   {config.AUDIT_BATCH_SIZE}",
            f"AUDIT_FOLDER_MODE:  {config.AUDIT_FOLDER_MODE}",
            "",
            f"GENRE_WORKERS:      {config.GENRE_WORKERS}",
            f"USE_ADB:            {config.USE_ADB}",
            f"ADB_TIMEOUT:        {config.ADB_TIMEOUT}",
            f"LOCALSEND_ENABLED:  {getattr(config, 'LOCALSEND_ENABLED', False)}",
            f"ID3_VERSION:        {config.ID3_VERSION}",
            f"STORAGE_BUFFER:     {config.STORAGE_BUFFER // (1024**3)} GB",
            "",
            f"LOG_FILE:           {config.LOG_FILE}",
            f"GENRE_CACHE_FILE:   {config.GENRE_CACHE_FILE}",
            f"AUDIT_STATE_FILE:   {config.AUDIT_STATE_FILE}",
            "",
            "API keys present:",
            f"  LASTFM_API_KEY:   {'✓' if config.LASTFM_API_KEY else '✗ MISSING'}",
            f"  DISCOGS_TOKEN:    {'✓' if config.DISCOGS_TOKEN else '✗ MISSING'}",
            f"  GEMINI_API_KEY:   {'✓' if config.GEMINI_API_KEY else '✗ MISSING'}",
            f"  GROQ_API_KEY:     {'✓' if getattr(config,'GROQ_API_KEY','') else '✗ MISSING'}",
        ]
        win = tk.Toplevel(self)
        win.title("Config values")
        win.geometry("520x480")
        win.configure(bg=BG_DARK)
        st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                        font=("Consolas", 9), relief="flat")
        st.pack(expand=True, fill="both", padx=8, pady=8)
        for line in lines:
            if "✗" in line:
                st.insert(tk.END, line + "\n", "ERR")
            elif "✓" in line:
                st.insert(tk.END, line + "\n", "OK")
            else:
                st.insert(tk.END, line + "\n")
        st.tag_config("ERR", foreground=FG_RED)
        st.tag_config("OK",  foreground=FG_TEAL)
        st.config(state="disabled")

    def _edit_api_keys(self):
        """Popup to update API keys in .env and reload them into config memory."""
        dlg = tk.Toplevel(self)
        dlg.title("Edit API Keys")
        dlg.configure(bg=BG_MAIN)
        dlg.resizable(False, False)

        _fields = [
            ("GROQ_API_KEY",      "Groq API Key"),
            ("GEMINI_API_KEY",    "Gemini API Key"),
            ("ANTHROPIC_API_KEY", "Anthropic API Key"),
            ("DEEPSEEK_API_KEY",  "DeepSeek API Key"),
        ]
        _vars: dict[str, tk.StringVar] = {}
        for env_key, label in _fields:
            current = getattr(config, env_key, "") or os.environ.get(env_key, "")
            tk.Label(dlg, text=label + ":", bg=BG_MAIN, fg=FG_GREY,
                     font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(8, 0))
            v = tk.StringVar(value=current)
            tk.Entry(dlg, textvariable=v, bg=BG_DARK, fg=FG_WHITE,
                     font=("Consolas", 8), width=62).pack(padx=12, pady=(0, 2), fill="x")
            _vars[env_key] = v

        def _save():
            env_path = config.BASE_DIR / ".env"
            lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
            for env_key, _ in _fields:
                new_val = _vars[env_key].get().strip()
                if not new_val:
                    continue
                updated = False
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith(env_key + "=") or stripped.startswith(env_key + " ="):
                        lines[i] = f"{env_key}={new_val}"
                        updated = True
                        break
                if not updated:
                    lines.append(f"{env_key}={new_val}")
                setattr(config, env_key, new_val)
                os.environ[env_key] = new_val
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._append_log("[Settings] API keys saved to .env and applied to this session", "SUCCESS")
            dlg.destroy()

        tk.Button(dlg, text="Save", command=_save,
                  bg=FG_TEAL, fg=BG_DARK,
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=12, pady=4).pack(pady=10)

    def _edit_audit_settings(self):
        """Popup to change audit knobs for the current session."""
        dlg = tk.Toplevel(self)
        dlg.title("Audit Settings")
        dlg.configure(bg=BG_MAIN)
        dlg.resizable(False, False)

        tk.Label(dlg, text="Re-audit interval (AUDIT_STALE_DAYS):",
                 bg=BG_MAIN, fg=FG_GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 0))
        stale_var = tk.StringVar(value=str(config.AUDIT_STALE_DAYS))
        tk.Entry(dlg, textvariable=stale_var, bg=BG_DARK, fg=FG_WHITE,
                 font=("Consolas", 9), width=10).pack(anchor="w", padx=12)
        tk.Label(dlg, text="Days before a device file is re-checked. 0 = force every file.",
                 bg=BG_MAIN, fg="#555577", font=("Segoe UI", 7)).pack(anchor="w", padx=12)

        tk.Label(dlg, text="Audit batch size (AUDIT_BATCH_SIZE):",
                 bg=BG_MAIN, fg=FG_GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 0))
        batch_var = tk.StringVar(value=str(config.AUDIT_BATCH_SIZE))
        tk.Entry(dlg, textvariable=batch_var, bg=BG_DARK, fg=FG_WHITE,
                 font=("Consolas", 9), width=10).pack(anchor="w", padx=12)

        tk.Label(dlg, text="HiRes compat pass (HIRES_COMPAT_PASS):",
                 bg=BG_MAIN, fg=FG_GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 0))
        hires_var = tk.BooleanVar(value=config.HIRES_COMPAT_PASS)
        tk.Checkbutton(dlg, text="Force art normalisation + ID3v2.3 on every audited file",
                       variable=hires_var, bg=BG_MAIN, fg=FG_BLUE,
                       selectcolor=BG_DARK, font=("Segoe UI", 8),
                       activebackground=BG_MAIN).pack(anchor="w", padx=12)
        tk.Label(dlg, text="  Targets 3,103 HiRes issues (progressive JPEG, oversized APIC, PNG, ID3v2.4).\n  63 runs to clear backlog at AUDIT_BATCH_SIZE=50.",
                 bg=BG_MAIN, fg="#555577", font=("Segoe UI", 7)).pack(anchor="w", padx=12)

        tk.Label(dlg, text="Pre-reorganise dedup pass (DEDUP_PRE_REORGANIZE):",
                 bg=BG_MAIN, fg=FG_GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 0))
        dedup_var = tk.BooleanVar(value=getattr(config, "DEDUP_PRE_REORGANIZE", True))
        tk.Checkbutton(dlg, text="Run soft-delete dedup before reorganise (disable to skip ~20hr dedup)",
                       variable=dedup_var, bg=BG_MAIN, fg="#cc6600",
                       selectcolor=BG_DARK, font=("Segoe UI", 8),
                       activebackground=BG_MAIN).pack(anchor="w", padx=12)
        tk.Label(dlg, text="  3,598 candidates x ~20s COM soft-delete each = ~20 hrs. Disable to skip.\n  Run dedup separately once the reorganise backlog is clear.",
                 bg=BG_MAIN, fg="#555577", font=("Segoe UI", 7)).pack(anchor="w", padx=12)

        def _apply():
            try:
                config.AUDIT_STALE_DAYS = int(stale_var.get())
            except ValueError:
                pass
            try:
                config.AUDIT_BATCH_SIZE = int(batch_var.get())
            except ValueError:
                pass
            config.HIRES_COMPAT_PASS = hires_var.get()
            config.DEDUP_PRE_REORGANIZE = dedup_var.get()
            self._append_log(
                f"[Settings] AUDIT_STALE_DAYS={config.AUDIT_STALE_DAYS}  "
                f"AUDIT_BATCH_SIZE={config.AUDIT_BATCH_SIZE}  "
                f"HIRES_COMPAT_PASS={config.HIRES_COMPAT_PASS}  "
                f"DEDUP_PRE_REORGANIZE={config.DEDUP_PRE_REORGANIZE}", "INFO"
            )
            dlg.destroy()

        tk.Button(dlg, text="Apply", command=_apply,
                  bg=FG_TEAL, fg=BG_DARK,
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=12, pady=4).pack(pady=10)

    # -----------------------------------------------------------------------
    # Rev 14.7 — Album promotion dry-run handler
    # -----------------------------------------------------------------------

    def _promote_dryrun(self):
        """Background: scan the device, build a PromotionPlan, write it to disk.

        Never touches files — this is the reviewable-plan phase only.
        Execution requires config.PROMOTE_ENABLED=True and a separate code
        path that does not yet exist in the GUI (intentional: Rev 14.7 ships
        dry-run only).
        """
        self._append_log("Album promotion dry-run starting...", "INFO")
        threading.Thread(target=self._promote_dryrun_thread, daemon=True).start()

    def _promote_dryrun_thread(self):
        plan, _ = self._promote_build_plan()
        if plan is None:
            return
        self.after(0, lambda: self._promote_popup(plan, self._promote_plan_path()))

    def _promote_plan_path(self):
        return pathlib.Path(config.BASE_DIR) / "walkman_promote_plan.txt"

    def _promote_journal_path(self):
        return pathlib.Path(config.BASE_DIR) / "walkman_promote_rollback.json"

    def _promote_files_to_specs(self, device_files, wp):
        """
        Convert adb_transfer.scan_device_audio() dicts (filename/ext/parts/
        storage only — no path or tags) into walkman_promote.FileSpec objects.
        Path must be the real absolute device path (AdbDeviceOps.delete_file
        shells out to it directly); artist/album/track/title are derived from
        the folder+filename convention via walkman_dedup's existing parsers
        since a full tag read per file would mean pulling all ~24k files.
        """
        import adb_transfer as _adbxfer
        from walkman_dedup import (
            _parse_folder_artist_album, _extract_track_number, _parse_filename,
        )
        specs = []
        for df in device_files:
            filename = df.get("filename", "")
            parts = df.get("parts", []) or []
            storage = df.get("storage", "")
            ext = (df.get("ext") or "").lstrip(".").lower()
            try:
                storage_root = _adbxfer._get_storage_path(storage)
                path = "/".join([storage_root.rstrip("/")] + list(parts) + [filename])
            except Exception:
                path = ""
            artist, album = _parse_folder_artist_album(parts)
            tn = _extract_track_number(filename)
            _, title = _parse_filename(filename)
            specs.append(wp.FileSpec(
                path=path, ext=ext,
                size_bytes=0,
                artist=artist,
                album=album,
                disc=1,
                track_number=int(tn) if tn else 0,
                title=title or pathlib.Path(filename).stem,
            ))
        return specs

    def _promote_build_plan(self):
        """
        Shared between dry-run and execute: scans the device, builds the
        plan, writes the plan text.  Returns (plan, specs) or (None, None)
        on error.  Caches the last plan on self so _promote_execute can
        re-use it without re-scanning.
        """
        try:
            import walkman_promote as wp
            import mtp_transfer as mtp
        except Exception as e:
            self._append_log(f"[Promote] import failed: {e}", "ERROR")
            return None, None

        try:
            self._append_log("  [Promote] Scanning device ...", "INFO")
            device_files = mtp.scan_device_audio(config.DEVICE_NAME, config.STORAGE_ROOTS)
            self._append_log(f"  [Promote] Device scan: {len(device_files)} audio files", "INFO")
        except Exception as e:
            self._append_log(f"[Promote] device scan failed: {e}", "ERROR")
            return None, None

        specs = self._promote_files_to_specs(device_files, wp)
        self._append_log(f"  [Promote] Converted {len(specs)} entries to FileSpec", "INFO")

        resolve_cb = None
        try:
            import metadata_enricher as _me
            if hasattr(_me, "resolve_album_artist"):
                resolve_cb = _me.resolve_album_artist
        except Exception:
            pass
        if resolve_cb is None:
            try:
                import walkman_promote_dryrun as _pdr
                resolve_cb = _pdr.make_resolver(specs)
            except Exception:
                resolve_cb = None

        plan = wp.build_plan(specs, resolve_artist=resolve_cb)
        report = wp.plan_to_text(plan)

        out_path = self._promote_plan_path()
        try:
            out_path.write_text(report, encoding="utf-8")
        except Exception as e:
            self._append_log(f"[Promote] failed to write plan: {e}", "ERROR")
            return None, None

        self._append_log(
            f"[Promote] Plan written: {out_path.name}  "
            f"(winners={len(plan.keep)}, discards={len(plan.discard)}, "
            f"relocations={len(plan.relocate)}, unsupported={len(plan.unsupported_singletons)})",
            "INFO",
        )
        self._last_promote_plan = plan   # stash for execute
        return plan, specs

    def _promote_popup(self, plan, out_path):
        win = tk.Toplevel(self)
        win.title("Album promotion plan — DRY-RUN")
        win.geometry("520x340")
        win.configure(bg=BG_DARK)
        tk.Label(win, text="Promotion plan complete (no files were touched)",
                 bg=BG_DARK, fg=FG_TEAL,
                 font=("Segoe UI", 11, "bold")).pack(pady=(12, 6))
        lines = [
            f"Winners kept:          {len(plan.keep)}",
            f"Losers to discard:     {len(plan.discard)}",
            f"Folders to relocate:   {len(plan.relocate)}",
            f"Unsupported singletons:{len(plan.unsupported_singletons)}",
            f"Operator flags:        {len(plan.flags)}",
            "",
            f"Plan file: {out_path}",
            "",
            "config.PROMOTE_ENABLED is currently:  "
            f"{getattr(config, 'PROMOTE_ENABLED', False)}",
            "",
            "Review the plan file before enabling execution.",
            "To execute: set PROMOTE_ENABLED=True, then click 'Execute promotion plan...'",
        ]
        st = scrolledtext.ScrolledText(win, bg=BG_DARK, fg=FG_WHITE,
                                        font=("Consolas", 9), relief="flat",
                                        height=14)
        st.pack(expand=True, fill="both", padx=10, pady=6)
        for line in lines:
            st.insert(tk.END, line + "\n")
        st.config(state="disabled")

        def _open_plan():
            try:
                os.startfile(str(out_path))  # Windows default handler
            except Exception:
                try:
                    _subprocess.Popen(["notepad", str(out_path)])
                except Exception:
                    pass

        tk.Button(win, text="Open plan in default editor",
                  command=_open_plan,
                  bg=BG_BTN, fg=FG_TEAL,
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=14, pady=6,
                  cursor="hand2").pack(pady=(4, 12))

    # -----------------------------------------------------------------------
    # Rev 14.7 — Album promotion EXECUTE handler (destructive)
    # -----------------------------------------------------------------------

    def _promote_execute(self):
        """Destructive execute: multi-step confirm, then run with AdbDeviceOps."""
        if not getattr(config, "PROMOTE_ENABLED", False):
            messagebox.showwarning(
                "Execute disabled",
                "config.PROMOTE_ENABLED is False.\n\n"
                "This is the safety gate.  To enable destructive execution:\n"
                "  1. Run the dry-run first and review walkman_promote_plan.txt\n"
                "  2. Back up your Walkman library to a local drive\n"
                "  3. Edit config.py and set PROMOTE_ENABLED = True\n"
                "  4. Restart the agent and click Execute again"
            )
            return

        # Step 1: confirm the user has a backup.
        if not messagebox.askyesno(
            "Backup confirmation",
            "Have you backed up your Walkman library to a local drive?\n\n"
            "Deletes cannot be undone.  Relocations can be undone via the "
            "rollback button, but only if the journal file is intact.\n\n"
            "Click Yes only if you have a current backup."
        ):
            self._append_log("[Promote/execute] aborted: no backup confirmed", "INFO")
            return

        # Step 2: require typed confirmation to prevent accidental clicks.
        typed = simpledialog.askstring(
            "Final confirmation",
            "Type EXECUTE in capitals to proceed.\n\n"
            "Anything else cancels.",
            parent=self
        )
        if typed != "EXECUTE":
            self._append_log("[Promote/execute] aborted: confirmation not typed", "INFO")
            return

        self._append_log("Album promotion EXECUTE starting...", "WARNING")
        threading.Thread(target=self._promote_execute_thread, daemon=True).start()

    def _promote_execute_thread(self):
        try:
            import walkman_promote as wp
            import mtp_transfer as mtp
        except Exception as e:
            self._append_log(f"[Promote/execute] import failed: {e}", "ERROR")
            return

        # Scan + build plan (same path as the dry-run, reused here)
        try:
            self._append_log("  [Promote/execute] Scanning device ...", "INFO")
            device_files = mtp.scan_device_audio(config.DEVICE_NAME, config.STORAGE_ROOTS)
        except Exception as e:
            self._append_log(f"[Promote/execute] device scan failed: {e}", "ERROR")
            return

        specs = self._promote_files_to_specs(device_files, wp)

        # Orphan resolver: prefer Spotify/AI-backed
        resolve_cb = None
        try:
            import metadata_enricher as _me
            if hasattr(_me, "resolve_album_artist"):
                resolve_cb = _me.resolve_album_artist
        except Exception:
            pass

        plan = wp.build_plan(specs, resolve_artist=resolve_cb)
        self._append_log(
            f"  [Promote/execute] Plan built: discards={len(plan.discard)} "
            f"relocations={len(plan.relocate)}",
            "INFO"
        )

        # Execute with AdbDeviceOps + journal
        journal = str(pathlib.Path(config.BASE_DIR) / "walkman_promote_journal.json")
        try:
            ops = wp.AdbDeviceOps(device_name=config.DEVICE_NAME)
        except Exception as e:
            self._append_log(f"[Promote/execute] AdbDeviceOps init failed: {e}", "ERROR")
            return

        import logging as _logging
        report = wp.execute_plan(
            plan, ops,
            dry_run=False,
            journal_path=journal,
            logger=_logging.getLogger("walkman_promote"),
            transcode_unsupported=False,   # device-side transcode not supported
        )

        self._append_log(
            f"[Promote/execute] COMPLETE: "
            f"deleted={report.deleted} moved={report.moved} "
            f"failed={len(report.failed)} journal={pathlib.Path(journal).name}",
            "WARNING" if report.failed else "INFO"
        )
        if report.failed:
            self._append_log(
                f"  [Promote/execute] {len(report.failed)} failures — "
                f"check the log and consider rollback.",
                "ERROR"
            )

    # -----------------------------------------------------------------------
    # Rev 14.7 — Rollback handler
    # -----------------------------------------------------------------------

    def _promote_rollback(self):
        journal = pathlib.Path(config.BASE_DIR) / "walkman_promote_journal.json"
        if not journal.exists():
            messagebox.showinfo(
                "No journal",
                f"No journal found at {journal}.\n\n"
                "Rollback needs the journal written during Execute.  If you "
                "never ran Execute, there is nothing to roll back."
            )
            return
        if not messagebox.askyesno(
            "Rollback promotion?",
            f"Attempt to undo the most recent promotion using\n"
            f"{journal.name}?\n\n"
            "Relocations will be reversed by moving files back.  Deletes "
            "cannot be undone.  The journal will be left in place so you "
            "can re-run rollback if needed."
        ):
            return
        self._append_log("Promotion rollback starting...", "INFO")
        threading.Thread(target=self._promote_rollback_thread,
                          args=(str(journal),), daemon=True).start()

    def _promote_rollback_thread(self, journal_path: str):
        try:
            import walkman_promote as wp
            ops = wp.AdbDeviceOps(device_name=config.DEVICE_NAME)
            import logging as _logging
            result = wp.rollback_from_journal(
                journal_path, ops,
                logger=_logging.getLogger("walkman_promote"),
            )
            self._append_log(f"[Promote/rollback] {result}", "INFO")
        except Exception as e:
            self._append_log(f"[Promote/rollback] failed: {e}", "ERROR")

    # --- EXECUTE (destructive) -------------------------------------------

    def _promote_execute(self):
        """Gated destructive execute of the last promotion plan.

        Multiple safety layers:
          1. config.PROMOTE_ENABLED must be True.
          2. A current plan must exist (either via in-memory cache or a
             fresh dry-run scan triggered here).
          3. User must type PROMOTE into a confirmation dialog.
          4. A rollback journal is written BEFORE any destructive op runs.
        """
        if not getattr(config, "PROMOTE_ENABLED", False):
            messagebox.showwarning(
                "Promotion disabled",
                "config.PROMOTE_ENABLED is False.\n\n"
                "Set it to True in config.py and restart the agent before "
                "running destructive execute.",
            )
            return

        # Always rebuild the plan to avoid stale caches
        self._append_log("Execute: rebuilding plan first to ensure it's current...", "INFO")
        threading.Thread(target=self._promote_execute_thread, daemon=True).start()

    def _promote_execute_thread(self):
        plan, _ = self._promote_build_plan()
        if plan is None:
            return

        summary = (
            f"You are about to EXECUTE the promotion plan against the device.\n\n"
            f"This will:\n"
            f"  • Delete {len(plan.discard)} duplicate files\n"
            f"  • Relocate {len(plan.relocate)} orphan album folders\n"
            f"  • Attempt to transcode {len(plan.unsupported_singletons)} "
            f"unsupported-format singletons\n\n"
            f"A rollback journal will be written to\n"
            f"  {self._promote_journal_path()}\n"
            f"before any destructive operation runs.\n\n"
            f"Deletes CANNOT be undone after they complete.\n"
            f"Relocations CAN be reversed via 'Rollback last promotion'."
        )

        # Ask the user to type PROMOTE
        typed = simpledialog.askstring(
            "Confirm promotion execute",
            summary + "\n\nType  PROMOTE  (uppercase) to confirm:",
            parent=self,
        )
        if typed != "PROMOTE":
            self._append_log("[Promote] Execute cancelled (confirmation not given).", "INFO")
            return

        try:
            import walkman_promote as wp
            ops = wp.AdbDeviceOps(config.DEVICE_NAME)
        except Exception as e:
            self._append_log(f"[Promote] AdbDeviceOps init failed: {e}", "ERROR")
            return

        self._append_log("[Promote] EXECUTE starting — rollback journal active.", "WARNING")
        import logging as _logging
        report = wp.execute_plan(
            plan=plan,
            ops=ops,
            dry_run=False,
            journal_path=str(self._promote_journal_path()),
            logger=_logging.getLogger("walkman_promote"),
            transcode_unsupported=True,
        )
        msg = (
            f"Execute complete:\n"
            f"  deleted={report.deleted}\n"
            f"  moved={report.moved}\n"
            f"  transcoded={report.transcoded}\n"
            f"  skipped={report.skipped}\n"
            f"  failed={len(report.failed)}"
        )
        self._append_log(msg.replace("\n", " | "), "INFO")
        self.after(0, lambda: messagebox.showinfo("Promotion executed", msg))

    # --- ROLLBACK --------------------------------------------------------

    def _promote_rollback(self):
        """Reverse relocations from the last executed plan using the journal."""
        journal = self._promote_journal_path()
        if not journal.exists():
            messagebox.showwarning(
                "No journal",
                f"No rollback journal found at:\n{journal}\n\n"
                f"The journal is only written when you run 'Execute promotion plan...'.",
            )
            return
        if not messagebox.askyesno(
            "Rollback promotion",
            "This will attempt to reverse the relocations from the last\n"
            "promotion execute.  Deletes CANNOT be undone.\n\nContinue?",
        ):
            return
        threading.Thread(target=self._promote_rollback_thread, daemon=True).start()

    def _promote_rollback_thread(self):
        try:
            import walkman_promote as wp
            ops = wp.AdbDeviceOps(config.DEVICE_NAME)
        except Exception as e:
            self._append_log(f"[Promote/rollback] init failed: {e}", "ERROR")
            return
        import logging as _logging
        summary = wp.rollback_from_journal(
            str(self._promote_journal_path()), ops,
            logger=_logging.getLogger("walkman_promote"),
        )
        self._append_log(f"[Promote/rollback] {summary}", "INFO")
        self.after(0, lambda: messagebox.showinfo(
            "Rollback complete",
            f"Restored moves: {summary.get('restored_moves', 0)}\n"
            f"Cannot undo deletes: {summary.get('cannot_undo_deletes', 0)}\n"
            f"Failed: {summary.get('failed', 0)}",
        ))

    # -----------------------------------------------------------------------
    # Spiral pipeline helper (MK11 Spiral Redesign)
    # -----------------------------------------------------------------------

    def _spiral_run(
        self,
        mode: str,
        track_map: dict | None = None,
        liked_album_ids: set | None = None,
        sp_client=None,
        dry_run: bool = False,
    ) -> None:
        """
        Run one spiral revolution cycle for the given mode.

        Handles preflight + session_guard (shared), then delegates to
        SpiralOrchestrator.  Called by all four thread methods when
        config.SPIRAL_ENABLED is True.

        2026-08-22: dry_run was never accepted/threaded here at all — every
        caller got SpiralOrchestrator's default (dry_run=False), so the GUI
        had no way to actually dry-run any spiral-based mode regardless of
        what a caller's dry-run checkbox said. Only _reorganize_thread had
        such a checkbox, and it was unreachable anyway (read after the
        SPIRAL_ENABLED early-return). Now threaded through properly.

        2026-09-03: by default (GUI_SUBPROCESS_PIPELINE=True) the heavy
        pipeline now runs as a `python cli_runner.py <mode>` CHILD PROCESS,
        with its stdout streamed into this log pane. A 6.5h in-GUI-thread run
        of the same orchestrator leaked GDI/USER handles into the six figures
        (COM/MTP + Tk churn) and ResourceMonitor aborted it; the identical
        work run as cli_runner completed clean. Isolating it in a disposable
        process that exits and frees everything is the fix. Set the flag False
        to fall back to the in-process path below.
        """
        if (getattr(config, "GUI_SUBPROCESS_PIPELINE", True)
                and track_map is None):
            return self._spiral_run_subprocess(mode, dry_run=dry_run)

        try:
            import preflight as _pf
            self._append_log("[Spiral] Running preflight cleanup...", "INFO")
            _pf.ensure_run_once()
        except Exception as _pfe:
            self._append_log(f"Preflight warning (non-fatal): {_pfe}", "WARNING")

        try:
            import session_guard as _sg
            _sg.preflight_checks()
        except Exception as _sge:
            self._append_log(f"Session-guard warning (non-fatal): {_sge}", "WARNING")

        # For download / full modes: fetch Spotify track map if not supplied
        if mode in ("full", "download") and track_map is None:
            try:
                if getattr(config, "OFFLINE_MODE", False):
                    import spotify_local_data as _sld
                    track_map, _liked, liked_album_ids = _sld.fetch_and_build_offline()
                    sp_client = None
                else:
                    import spotify_client as sc
                    sp_client  = sc.get_spotify_client()
                    track_map, _liked, liked_album_ids = sc.fetch_and_build(sp_client)
            except Exception as _se:
                self._append_log(f"[Spiral] Spotify fetch failed: {_se}", "ERROR")
                return

        from spiral_orchestrator import SpiralOrchestrator
        orch = SpiralOrchestrator(
            mode=mode,
            log_callback=self._on_log,
            cancel_flag=self._cancel_flag,
            track_map=track_map or {},
            liked_album_ids=liked_album_ids or set(),
            sp_client=sp_client,
            dry_run=dry_run,
        )
        stats = orch.run()

        # Summarise to UI
        # Use the fatal-only boolean stored by SpiralOrchestrator (non-fatal
        # probes like content_provider have ok=False but don't abort the run).
        canary_ok = stats.get("canary_passed", True)
        revs = stats.get("revolutions", {})
        # reorganize now runs once after all revolutions (see spiral_orchestrator.py),
        # so its stats live at the top level, not inside a per-revolution dict.
        total_moved = stats.get("reorganize", {}).get("moved", 0)
        total_dl = sum(
            r.get("download", {}).get("downloaded", 0)
            for r in revs.values() if isinstance(r, dict)
        )
        total_audited = sum(
            r.get("audit", {}).get("audited", 0)
            for r in revs.values() if isinstance(r, dict)
        )
        audit_stats = {
            "audited": total_audited,
            "changed": sum(r.get("audit", {}).get("changed", 0) for r in revs.values() if isinstance(r, dict)),
            "skipped": sum(r.get("audit", {}).get("skipped", 0) for r in revs.values() if isinstance(r, dict)),
            "errors":  sum(r.get("audit", {}).get("errors", 0) for r in revs.values() if isinstance(r, dict)),
        }
        self.after(0, lambda s=audit_stats: self._update_audit_stats(s))
        summary = (
            f"Mode: {mode} | Canary: {'OK' if canary_ok else 'FAILED'}\n"
            f"Downloaded: {total_dl}  Moved: {total_moved}  Audited: {total_audited}"
        )
        self.after(0, lambda: messagebox.showinfo(f"Spiral {mode} complete", summary))

    def _spiral_run_subprocess(self, mode: str, dry_run: bool = False) -> None:
        """
        Run the pipeline as `python cli_runner.py <mode>` and stream its stdout
        into the log pane. Keeps the leak-prone orchestrator/downloader/audit +
        COM/MTP work out of the long-lived GUI process (see _spiral_run docs).
        Cancellation terminates the whole child process tree.
        """
        import json as _json

        cmd = [_sys_early.executable, "-u", "cli_runner.py", mode]
        if dry_run:
            cmd.append("--dry-run")
        _fa = getattr(self, "_force_audit_var", None)
        if _fa is not None and _fa.get():
            cmd.append("--force-audit")
        _na = getattr(self, "_nuke_audit_var", None)
        if _na is not None and _na.get():
            cmd.append("--fresh-audit")
        _fm = getattr(self, "_folder_mode_var", None)
        if mode == "audit" and _fm is not None and _fm.get():
            cmd.append("--folder-audit")

        self._append_log(f"[Spiral] launching child: {' '.join(cmd[2:])}", "INFO")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            proc = _subprocess.Popen(
                cmd,
                stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(config.BASE_DIR), env=env,
                # Own hidden console — without it, comtypes/pywinauto UIA calls
                # in the child's DialogWatcher hard-crash (0xC0000005). Same
                # reason as _run_cli_mode.
                creationflags=_subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            self._append_log(f"[Spiral] child launch failed: {e}", "ERROR")
            self.after(0, lambda: messagebox.showerror(
                "Spiral", f"Could not launch pipeline subprocess:\n{e}"))
            return

        self._spiral_proc = proc
        stats_obj: dict | None = None
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    self._on_log(line)
                    if "[CLI] Stats:" in line:
                        try:
                            stats_obj = _json.loads(
                                line.split("[CLI] Stats:", 1)[1].strip().replace("'", '"')
                            )
                        except Exception:
                            stats_obj = None
                if self._cancel_flag[0]:
                    self._append_log("[Spiral] cancel — terminating child tree ...", "WARNING")
                    self._kill_spiral_proc()
                    break
            proc.wait()
        finally:
            rc = proc.returncode
            self._spiral_proc = None

        if self._cancel_flag[0]:
            self.after(0, lambda: messagebox.showinfo(
                f"Spiral {mode}", "Cancelled."))
            return

        tag = "SUCCESS" if rc == 0 else "ERROR"
        self._append_log(f"[Spiral] child exited (code {rc})", tag)
        if isinstance(stats_obj, dict):
            revs = stats_obj.get("revolutions", {}) or {}
            def _sum(path_a, path_b):
                return sum((r.get(path_a, {}) or {}).get(path_b, 0)
                           for r in revs.values() if isinstance(r, dict))
            audit_stats = {
                "audited": _sum("audit", "audited"),
                "changed": _sum("audit", "changed"),
                "skipped": _sum("audit", "skipped"),
                "errors":  _sum("audit", "errors"),
            }
            self.after(0, lambda s=audit_stats: self._update_audit_stats(s))
            summary = (
                f"Mode: {mode} | exit {rc}\n"
                f"Downloaded: {_sum('download','downloaded')}  "
                f"Moved: {(stats_obj.get('reorganize',{}) or {}).get('moved',0)}  "
                f"Audited: {audit_stats['audited']}"
            )
        else:
            summary = f"Mode: {mode} | exit {rc}\n(see log for details)"
        self.after(0, lambda: messagebox.showinfo(
            f"Spiral {mode}{' (dry-run)' if dry_run else ''} complete", summary))

    def _kill_spiral_proc(self) -> None:
        """Terminate the running pipeline subprocess and its whole tree."""
        proc = getattr(self, "_spiral_proc", None)
        if proc is None or proc.poll() is not None:
            return
        try:
            _subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                creationflags=_subprocess.CREATE_NO_WINDOW,
                timeout=15,
            )
        except Exception as e:
            self._append_log(f"[Spiral] taskkill failed ({e}) — trying proc.terminate()", "WARNING")
            try:
                proc.terminate()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Background threads (sync / download / audit / reorganize)
    # -----------------------------------------------------------------------

    def _sync_thread(self):
        with _SleepBlocker("WalkmanSync-sync"):
            try:
                self._cancel_flag = [False]
                if getattr(config, "SPIRAL_ENABLED", True):
                    self._spiral_run("full")
                    return

                # ── Waterfall fallback (SPIRAL_ENABLED=False) ──────────────
                # REV 14: preflight environmental cleanup — kills stray adb.exe,
                # closes Explorer windows on the Walkman, disables AutoPlay.
                # Prevents MTP dialogs before they can be spawned.
                # REV 14.1: self-announcing — previously silent-on-success
                # hooks were indistinguishable from missing hooks in LOG-9.
                try:
                    import preflight as _pf
                    self._append_log("Running preflight cleanup...", "INFO")
                    _pf.ensure_run_once()
                except Exception as _pfe:
                    self._append_log(f"Preflight warning (non-fatal): {_pfe}", "WARNING")

                # REV 14.3: Windows-Update pending-restart check + stale
                # audit temp dir scan.  Driven by the 2026-04-18 incident
                # where a triple TrustedInstaller restart interrupted an
                # overnight run.  Warnings only — never blocks the run.
                try:
                    import session_guard as _sg
                    self._append_log("Running session-integrity checks...", "INFO")
                    _sg.preflight_checks()
                except Exception as _sge:
                    self._append_log(f"Session-guard warning (non-fatal): {_sge}", "WARNING")

                # REV 14.4: pre-sync dedup pass — HARD delete of zero-length
                # / corrupt files and inferior-format duplicates (MP3 when a
                # FLAC of the same track exists).  Frees device space before
                # the sync starts writing new files.  Default DEDUP_MODE in
                # config is "report" — set to "active" to actually delete.
                try:
                    import walkman_dedup as _dd
                    self._append_log(
                        "Running pre-sync dedup (hard-delete mode)...", "INFO"
                    )
                    _dd_stats = _dd.prune_before_sync()
                    self._append_log(
                        f"Pre-sync dedup: {_dd_stats.get('planned',0)} planned, "
                        f"{_dd_stats.get('deleted',0)} deleted, "
                        f"{_dd_stats.get('failed',0)} failed "
                        f"(mode={_dd_stats.get('mode','?')})",
                        "INFO",
                    )
                except Exception as _dde:
                    self._append_log(f"Dedup warning (non-fatal): {_dde}", "WARNING")

                self._agent = WalkmanSyncAgent(log_callback=self._on_log)
                self._agent.run_sync()

                # REV 14: end-of-session Walkman Music app DB refresh.
                # Android MediaStore scans do NOT touch the Walkman app's
                # private DB — this step does.  Gated by
                # config.WALKMAN_DB_REFRESH_MODE.  Non-fatal on failure.
                try:
                    import walkman_app_refresh as _war
                    mode = getattr(__import__("config"), "WALKMAN_DB_REFRESH_MODE", "ui_tap")
                    if mode == "off":
                        self._append_log(
                            "WALKMAN_DB_REFRESH_SKIPPED (WALKMAN_DB_REFRESH_MODE=off). "
                            "⚠ Set WALKMAN_DB_REFRESH_MODE='ui_tap' to auto-refresh, or "
                            "tap Menu → Update database in the Music app manually.",
                            "WARNING"
                        )
                    elif _war.is_configured():
                        self._append_log(f"Running end-of-session Walkman app refresh (mode={mode})...", "INFO")
                        ok = _war.refresh()
                        self._append_log(
                            f"Walkman refresh {'succeeded' if ok else 'did not confirm success'}", "INFO"
                        )
                    else:
                        self._append_log(
                            "WALKMAN_DB_REFRESH_SKIPPED — no Walkman app package detected on device. "
                            "⚠ Tags may have been corrected on-device but the Sony Music app still shows "
                            "old metadata until its database is refreshed. "
                            "Fix: (1) ensure USB debugging is on and device is connected, OR "
                            "(2) open Music app manually → tap ⋮ → Update database. "
                            "Check 'adb shell pm list packages | findstr -i walk' and set SONY_WALKMAN_PKG in config.",
                            "WARNING"
                        )
                except Exception as _ware:
                    self._append_log(f"Walkman refresh warning (non-fatal): {_ware}", "WARNING")

                self.after(0, lambda: messagebox.showinfo(
                    "Sync complete",
                    self._sync_summary() + self._audit_summary(),
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Sync error", str(exc)))
                self._append_log(f"ERROR: {exc}", "ERROR")
            finally:
                self.after(0, self._done)

    def _download_thread(self):
        with _SleepBlocker("WalkmanSync-download"):
            try:
                self._cancel_flag = [False]
                if getattr(config, "SPIRAL_ENABLED", True):
                    self._spiral_run("download")
                    return

                # ── Waterfall fallback (SPIRAL_ENABLED=False) ──────────────
                # REV 14.1: self-announcing preflight.
                try:
                    import preflight as _pf
                    self._append_log("Running preflight cleanup...", "INFO")
                    _pf.ensure_run_once()
                except Exception as _pfe:
                    self._append_log(f"Preflight warning (non-fatal): {_pfe}", "WARNING")

                # REV 14.3: session-integrity checks.
                try:
                    import session_guard as _sg
                    self._append_log("Running session-integrity checks...", "INFO")
                    _sg.preflight_checks()
                except Exception as _sge:
                    self._append_log(f"Session-guard warning (non-fatal): {_sge}", "WARNING")

                import spotify_client as sc
                import downloader as dl
                setup_logging(self._on_log)
                # Rev 14.9.5 — honor OFFLINE_MODE: skip Spotify auth entirely.
                # The local-data loader builds the same 3-tuple shape.
                if getattr(config, "OFFLINE_MODE", False):
                    self._append_log(
                        "OFFLINE_MODE=True — using local Spotify export, "
                        "skipping API auth", "INFO"
                    )
                    import spotify_local_data as _sld
                    track_map, _liked_ids, saved_album_ids = _sld.fetch_and_build_offline()
                    sp = None  # downloader's sp_client param tolerates None
                else:
                    sp = sc.get_spotify_client()
                    track_map, _liked_ids, saved_album_ids = sc.fetch_and_build(sp)
                stats = dl.run_downloads(
                    track_map=track_map,
                    cancelled=self._cancel_flag,
                    liked_album_ids=saved_album_ids,
                    sp_client=sp,
                    log_callback=self._on_log,
                )
                # REV 14.1: self-announcing Walkman app DB refresh.
                try:
                    import walkman_app_refresh as _war
                    mode = getattr(__import__("config"), "WALKMAN_DB_REFRESH_MODE", "ui_tap")
                    if mode == "off":
                        self._append_log(
                            "WALKMAN_DB_REFRESH_SKIPPED (WALKMAN_DB_REFRESH_MODE=off). "
                            "⚠ Set WALKMAN_DB_REFRESH_MODE='ui_tap' to auto-refresh, or "
                            "tap Menu → Update database in the Music app manually.",
                            "WARNING"
                        )
                    elif _war.is_configured():
                        self._append_log(f"Running end-of-session Walkman app refresh (mode={mode})...", "INFO")
                        ok = _war.refresh()
                        self._append_log(
                            f"Walkman refresh {'succeeded' if ok else 'did not confirm success'}", "INFO"
                        )
                    else:
                        self._append_log(
                            "Walkman app refresh skipped — no Walkman app package detected on device.",
                            "WARNING"
                        )
                except Exception as _ware:
                    self._append_log(f"Walkman refresh warning (non-fatal): {_ware}", "WARNING")

                self.after(0, lambda: messagebox.showinfo(
                    "Download complete",
                    f"Downloaded: {stats['downloaded']}  "
                    f"Failed: {stats['failed']}  "
                    f"Albums: {stats['albums']}"
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Download error", str(exc)))
                self._append_log(f"ERROR: {exc}", "ERROR")
            finally:
                self.after(0, self._done)

    def _audit_thread(self):
        with _SleepBlocker("WalkmanSync-audit"):
            try:
                self._cancel_flag = [False]
                if getattr(config, "SPIRAL_ENABLED", True):
                    self._spiral_run("audit")
                    return

                # ── Waterfall fallback (SPIRAL_ENABLED=False) ──────────────
                # REV 14.1: self-announcing preflight.
                try:
                    import preflight as _pf
                    self._append_log("Running preflight cleanup...", "INFO")
                    _pf.ensure_run_once()
                except Exception as _pfe:
                    self._append_log(f"Preflight warning (non-fatal): {_pfe}", "WARNING")

                # REV 14.3: session-integrity checks.
                try:
                    import session_guard as _sg
                    self._append_log("Running session-integrity checks...", "INFO")
                    _sg.preflight_checks()
                except Exception as _sge:
                    self._append_log(f"Session-guard warning (non-fatal): {_sge}", "WARNING")

                stats = run_audit(
                    log_callback=self._on_log,
                    cancelled_flag=self._cancel_flag,
                )
                self._update_audit_stats(stats)
                if self._reorg_after_audit_var.get():
                    self._append_log("Running reorganise after audit...", "INFO")
                    _dry_run_flag = getattr(self, "_reorg_dryrun_var", None)
                    _dry = bool(_dry_run_flag.get()) if _dry_run_flag is not None else False

                    # REV 14.4: pre-reorganize dedup pass — SOFT delete.
                    # Skipped in dry-run mode to match reorg behaviour.
                    if not _dry:
                        try:
                            import walkman_dedup as _dd
                            self._append_log(
                                "Running pre-reorganize dedup (soft-delete mode)...", "INFO"
                            )
                            _dd_stats = _dd.prune_during_audit()
                            self._append_log(
                                f"Pre-reorganize dedup: {_dd_stats.get('planned',0)} planned, "
                                f"{_dd_stats.get('deleted',0)} moved to .deleted/, "
                                f"{_dd_stats.get('failed',0)} failed "
                                f"(mode={_dd_stats.get('mode','?')})",
                                "INFO",
                            )
                        except Exception as _dde:
                            self._append_log(f"Dedup warning (non-fatal): {_dde}", "WARNING")

                    reorg_stats = run_reorganize(
                        log_callback=self._on_log,
                        cancelled_flag=self._cancel_flag,
                        dry_run=_dry,
                    )
                    self._append_log(
                        f"Reorganise: {reorg_stats.get('folders_renamed',0)} folder(s) renamed, "
                        f"{reorg_stats.get('files_renamed',0)} file(s) renamed, "
                        f"{reorg_stats.get('errors',0)} error(s)",
                        "INFO"
                    )

                    # MK10: Post-reorganize audit (AUDIT_AFTER_REORG).
                    if not _dry and getattr(__import__("config"), "AUDIT_AFTER_REORG", True):
                        if not self._cancel_flag[0]:
                            self._append_log(
                                "Post-reorganize audit: verifying tags on moved files ...", "INFO"
                            )
                            try:
                                post_stats = run_audit(
                                    log_callback=self._on_log,
                                    cancelled_flag=self._cancel_flag,
                                )
                                self._update_audit_stats(post_stats)
                                self._append_log(
                                    f"Post-reorganize audit: "
                                    f"{post_stats.get('audited',0)} files checked, "
                                    f"{post_stats.get('changed',0)} tag(s) fixed.",
                                    "INFO",
                                )
                            except Exception as _pae2:
                                self._append_log(
                                    f"Post-reorganize audit error (non-fatal): {_pae2}", "WARNING"
                                )

                # Walkman DB refresh is handled inside LibraryAuditor.run()
                # when stats['changed'] > 0 — no separate call needed here.

                self.after(0, lambda: messagebox.showinfo(
                    "Audit complete", self._audit_summary()
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Audit error", str(exc)))
                self._append_log(f"ERROR: {exc}", "ERROR")
            finally:
                self.after(0, self._done)

    def _full_pipeline_thread(self):
        """
        MK10: Full five-stage pipeline execution thread.

        Sequence (non-dry-run only):
          Stage 0  Pre-sync dedup    — hard-delete zero-byte / inferior-format dupes
          Stage 1  Sync              — download → tag → transfer (includes Stage 8 audit)
          Stage 2  Pre-reorg dedup   — soft-delete remaining dupes to .deleted/
          Stage 3  Reorganize        — move files into canonical Artist/Album/NN-Title paths
          Stage 4  Post-reorg audit  — verify tags on all moved files, fix any stragglers

        Each stage is wrapped in its own try/except so a failure in one stage
        does not prevent the remaining stages from running.  Cancellation is
        checked between every stage.
        """
        with _SleepBlocker("WalkmanSync-full-pipeline"):
            try:
                self._cancel_flag = [False]
                self._append_log(
                    "╔══════════════════════════════════════════════════╗", "INFO"
                )
                self._append_log(
                    "║  FULL PIPELINE  — all 5 stages                   ║", "INFO"
                )
                self._append_log(
                    "╚══════════════════════════════════════════════════╝", "INFO"
                )

                # ── Preflight ────────────────────────────────────────────────
                try:
                    import preflight as _pf
                    self._append_log("Preflight cleanup ...", "INFO")
                    _pf.ensure_run_once()
                except Exception as _pfe:
                    self._append_log(f"Preflight warning: {_pfe}", "WARNING")

                # MK14 FIX-B: Pre-load device inventory ONCE at pipeline start,
                # before any worker threads spin up. This prevents the ADB race
                # condition (38 concurrent subprocess.run('adb devices') calls
                # from 25 worker threads that caused "ADB not connected" spam).
                try:
                    import device_inventory as _di
                    if not _di.is_refreshed():
                        self._append_log(
                            "Preloading device inventory (ADB find) ...", "INFO"
                        )
                        n = _di.refresh(force=False)
                        self._append_log(
                            f"Device inventory: {n} audio files indexed", "INFO"
                        )
                        # MK19-RC2: mark inventory as live for dedup gate
                        try:
                            import walkman_sync as _ws_ref
                            _ws_ref._DEVICE_INVENTORY_IS_LIVE = True
                        except Exception:
                            pass
                        # MK15-R0 v4: halt gate (single — duplicates collapsed)
                        # ADB inventory=0 almost always means race condition, not empty device.
                        # Stage 1 gate checks MTP before deciding to abort.
                        if n == 0:
                            self._inventory_zero_halt = True
                            self._append_log(
                                "[MK15-R0] ADB inventory=0 — likely USB/ADB race condition. "
                                "Stage 1 will verify via MTP before skipping transfers.",
                                "WARNING"
                            )
                        else:
                            self._inventory_zero_halt = False
                        
                        
                        
                        
                    else:
                        self._append_log(
                            f"Device inventory: {_di.get_device_summary()['total']} "
                            f"files (from cache, age {_di.age_hours():.1f}h)", "INFO"
                        )
                except Exception as _die:
                    self._append_log(
                        f"Device inventory preload skipped: {_die}", "DEBUG"
                    )
                try:
                    import session_guard as _sg
                    _sg.preflight_checks()
                except Exception:
                    pass

                # ── Stage 0: Pre-sync hard dedup ─────────────────────────────
                if not self._cancel_flag[0]:
                    self._append_log(
                        "▶ Stage 0/5 — Pre-sync dedup (hard delete) ...", "INFO"
                    )
                    try:
                        import walkman_dedup as _dd
                        _dd_stats = _dd.prune_before_sync()
                        self._append_log(
                            f"  Dedup: {_dd_stats.get('deleted',0)} deleted, "
                            f"{_dd_stats.get('failed',0)} failed",
                            "INFO",
                        )
                    except Exception as _dde:
                        self._append_log(f"  Dedup warning: {_dde}", "WARNING")

                # ── Stage 1: Sync (download → tag → transfer → Stage 8 audit) ─
                _stage1_ok = False   # P1: track whether Stage 1 completed successfully
                if not self._cancel_flag[0]:
                    self._append_log("▶ Stage 1/5 — Sync + post-sync audit ...", "INFO")
                    try:
                        # MK15-R0 v4: gate check -- MTP-aware, does not hard-block when MTP is reachable
                        if getattr(self, '_inventory_zero_halt', False):
                            # ADB returned 0 but MTP diagnostic showed WALKMAN connected.
                            # Proceed with transfers rather than hard-aborting.
                            self._append_log(
                                "[MK15-R0] ADB inventory=0 but MTP device is reachable — "
                                "proceeding with Stage 1. Watch for unexpected duplicates.",
                                "WARNING"
                            )
                            config.AUDIT_AFTER_SYNC = True
                            self._agent = WalkmanSyncAgent(log_callback=self._on_log)
                            self._agent.run_sync()
                        else:
                            config.AUDIT_AFTER_SYNC = True   # enforce Stage 8 audit
                            self._agent = WalkmanSyncAgent(log_callback=self._on_log)
                            self._agent.run_sync()
                        _stage1_ok = True   # P1: Stage 1 completed without exception
                    except Exception as _se:
                        self._append_log(
                            f"  Sync error: {_se}  ← "
                            f"Stage 2 will be skipped (stage state is inconsistent). "
                            f"Set ALLOW_CONTINUE_ON_STAGE_FAIL=True in config to override.",
                            "ERROR"
                        )

                # ── Stage 2: Pre-reorganize soft dedup ───────────────────────
                # P1 CTO fix: only run Stage 2 if Stage 1 succeeded (or override set)
                _allow_continue = getattr(config, "ALLOW_CONTINUE_ON_STAGE_FAIL", False)
                if not _stage1_ok and not _allow_continue:
                    self._append_log(
                        "  [Pipeline/P1] Stage 2 SKIPPED — Stage 1 did not complete. "
                        "Set config.ALLOW_CONTINUE_ON_STAGE_FAIL=True to override.",
                        "WARNING"
                    )
                if not self._cancel_flag[0] and (_stage1_ok or _allow_continue):
                    self._append_log(
                        "▶ Stage 2/5 — Pre-reorganize dedup (soft delete → .deleted/) ...",
                        "INFO",
                    )
                    try:
                        import walkman_dedup as _dd2
                        _dd2_stats = _dd2.prune_during_audit()
                        self._append_log(
                            f"  Dedup: {_dd2_stats.get('deleted',0)} moved to .deleted/",
                            "INFO",
                        )
                    except Exception as _dde2:
                        self._append_log(f"  Dedup warning: {_dde2}", "WARNING")

                # ── Stage 3: Reorganize ───────────────────────────────────────
                reorg_stats = {}
                if not self._cancel_flag[0]:
                    self._append_log(
                        "▶ Stage 3/5 — Reorganize (canonical paths) ...", "INFO"
                    )
                    try:
                        reorg_stats = run_reorganize(
                            log_callback=self._on_log,
                            cancelled_flag=self._cancel_flag,
                            dry_run=False,
                        )
                        self._append_log(
                            f"  Reorg: {reorg_stats.get('moved',0)} files moved, "
                            f"{reorg_stats.get('errors',0)} errors",
                            "INFO",
                        )
                    except Exception as _re:
                        self._append_log(f"  Reorg error: {_re}", "ERROR")

                # ── Stage 4: Post-reorganize audit ────────────────────────────
                post_stats = {}
                if not self._cancel_flag[0]:
                    self._append_log(
                        "▶ Stage 4/5 — Post-reorganize audit (verify tags) ...", "INFO"
                    )
                    try:
                        post_stats = run_audit(
                            log_callback=self._on_log,
                            cancelled_flag=self._cancel_flag,
                        )
                        self._update_audit_stats(post_stats)
                        self._append_log(
                            f"  Post-reorg audit: {post_stats.get('audited',0)} checked, "
                            f"{post_stats.get('changed',0)} fixed",
                            "INFO",
                        )
                    except Exception as _ae:
                        self._append_log(f"  Post-reorg audit error: {_ae}", "ERROR")

                # ── Stage 5: Walkman DB refresh ───────────────────────────────
                # Stages 3 and 4 fire their own conditional refreshes internally.
                # Stage 5 is the safety-net for sync (which runs via run_sync()
                # here, not _sync_thread, so has no own refresh) and for zero-
                # change pipeline runs where neither reorg nor audit tapped.
                _already_refreshed = (
                    reorg_stats.get("moved", 0) > 0
                    or post_stats.get("changed", 0) > 0
                )
                try:
                    import walkman_app_refresh as _war
                    mode = getattr(__import__("config"), "WALKMAN_DB_REFRESH_MODE", "ui_tap")
                    if mode != "off" and _war.is_configured():
                        if _already_refreshed:
                            self._append_log(
                                "▶ Stage 5/5 — Walkman DB refresh already done by Stage 3/4, skipping.",
                                "INFO",
                            )
                        else:
                            self._append_log(
                                "▶ Stage 5/5 — Walkman DB refresh ...", "INFO"
                            )
                            _war.refresh()
                except Exception:
                    pass

                self._append_log(
                    "╔══════════════════════════════════════════════════╗", "INFO"
                )
                self._append_log(
                    "║  FULL PIPELINE COMPLETE                           ║", "INFO"
                )
                self._append_log(
                    "╚══════════════════════════════════════════════════╝", "INFO"
                )
                self.after(0, lambda: messagebox.showinfo(
                    "Full Pipeline Complete",
                    self._sync_summary() + "\n" + self._audit_summary(),
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Full Pipeline error", str(exc)))
                self._append_log(f"FULL PIPELINE ERROR: {exc}", "ERROR")
            finally:
                self.after(0, self._done)

    def _reorganize_thread(self):
        with _SleepBlocker("WalkmanSync-reorg"):
            try:
                self._cancel_flag = [False]
                if getattr(config, "SPIRAL_ENABLED", True):
                    # 2026-08-22: this checkbox used to be read only in the
                    # unreachable fallback branch below (SPIRAL_ENABLED=False),
                    # so it had zero effect on any real (spiral) run.
                    self._spiral_run("reorganize", dry_run=self._reorg_dryrun_var.get())
                    return

                # ── Waterfall fallback (SPIRAL_ENABLED=False) ──────────────
                # REV 14.1: preflight hook was missing on this thread (found
                # via LOG-9 — zero "Preflight" lines when the user ran
                # audit → reorganise path).  Same pattern as _sync_thread.
                try:
                    import preflight as _pf
                    self._append_log("Running preflight cleanup...", "INFO")
                    _pf.ensure_run_once()
                except Exception as _pfe:
                    self._append_log(f"Preflight warning (non-fatal): {_pfe}", "WARNING")

                # REV 14.3: session-integrity checks.
                try:
                    import session_guard as _sg
                    self._append_log("Running session-integrity checks...", "INFO")
                    _sg.preflight_checks()
                except Exception as _sge:
                    self._append_log(f"Session-guard warning (non-fatal): {_sge}", "WARNING")

                dry_run = self._reorg_dryrun_var.get()
                if not dry_run:
                    self._append_log(
                        "Pre-reorganize: running audit to stamp missing tags...", "INFO"
                    )
                    try:
                        pre_stats = run_audit(
                            log_callback=self._on_log,
                            cancelled_flag=self._cancel_flag,
                        )
                        self._update_audit_stats(pre_stats)
                        self._append_log(
                            f"Pre-reorganize audit: {pre_stats.get('changed',0)} tag(s) fixed.", "INFO"
                        )
                    except Exception as _ae:
                        self._append_log(f"Pre-reorganize audit error ({_ae}) — continuing anyway.", "WARNING")

                # REV 14.4: pre-reorganize dedup pass — SOFT delete (files
                # are moved to <Music>/.deleted/… so a human can review them
                # before any permanent deletion).  Includes all three passes
                # when the audit has already been run (files pulled for tag
                # inspection can be scored for metadata completeness).
                if not dry_run:
                    try:
                        import walkman_dedup as _dd
                        self._append_log(
                            "Running pre-reorganize dedup (soft-delete mode)...", "INFO"
                        )
                        _dd_stats = _dd.prune_during_audit()
                        self._append_log(
                            f"Pre-reorganize dedup: {_dd_stats.get('planned',0)} planned, "
                            f"{_dd_stats.get('deleted',0)} moved to .deleted/, "
                            f"{_dd_stats.get('failed',0)} failed "
                            f"(mode={_dd_stats.get('mode','?')})",
                            "INFO",
                        )
                    except Exception as _dde:
                        self._append_log(f"Dedup warning (non-fatal): {_dde}", "WARNING")

                if self._cancel_flag[0]:
                    self._append_log("Cancelled before reorganise.", "INFO")
                    return
                stats = run_reorganize(
                    log_callback=self._on_log,
                    cancelled_flag=self._cancel_flag,
                    dry_run=dry_run,
                )

                # Walkman DB refresh is handled inside reorganize_device()
                # when stats['moved'] > 0 — no separate call needed here.

                # MK10: Post-reorganize audit (AUDIT_AFTER_REORG).
                # The reorganiser moves files to canonical paths; their tags
                # are already correct from the pre-reorg audit, but a second
                # pass catches any stragglers (e.g. files that the reorganiser
                # created by merging two partially-complete album folders).
                if not dry_run and getattr(__import__("config"), "AUDIT_AFTER_REORG", True):
                    if not self._cancel_flag[0]:
                        self._append_log(
                            "Post-reorganize audit: verifying tags on moved files ...", "INFO"
                        )
                        try:
                            post_stats = run_audit(
                                log_callback=self._on_log,
                                cancelled_flag=self._cancel_flag,
                            )
                            self._update_audit_stats(post_stats)
                            self._append_log(
                                f"Post-reorganize audit: "
                                f"{post_stats.get('audited',0)} files checked, "
                                f"{post_stats.get('changed',0)} tag(s) fixed.",
                                "INFO",
                            )
                        except Exception as _pae:
                            self._append_log(
                                f"Post-reorganize audit error (non-fatal): {_pae}", "WARNING"
                            )

                self.after(0, lambda: messagebox.showinfo(
                    "Reorganize complete",
                    f"Folders renamed: {stats.get('folders_renamed',0)}  "
                    f"Files renamed: {stats.get('files_renamed',0)}  "
                    f"Moved: {stats.get('moved',0)}  "
                    f"Errors: {stats.get('errors',0)}"
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Reorganize error", str(exc)))
                self._append_log(f"ERROR: {exc}", "ERROR")
            finally:
                self.after(0, self._done)

    # -----------------------------------------------------------------------
    # Button state management
    # -----------------------------------------------------------------------

    def _done(self):
        self._btn_sync.config(state="normal",     text="▶  START SYNC")
        self._btn_full.config(state="normal",     text="⚡  FULL PIPELINE")
        self._btn_download.config(state="normal", text="⬇  DOWNLOAD ONLY")
        self._btn_audit.config(state="normal",    text="🔍  AUDIT ONLY")
        self._btn_reorg.config(state="normal",    text="🔀  REORGANISE")
        if hasattr(self, "_btn_curate"):
            self._btn_curate.config(state="normal", text="🧹  CURATE LIBRARY")
        self._btn_cancel.config(state="disabled", text="■  CANCEL")
        self._btn_detect.config(state="normal")
        self._agent = None
        self._inventory_zero_halt = False  # MK15-R0: set True when inventory preload = 0





    def _set_buttons_running(self):
        self._btn_sync.config(state="disabled",     text="⏳  RUNNING ...")
        self._btn_full.config(state="disabled")
        self._btn_download.config(state="disabled")
        self._btn_audit.config(state="disabled")
        self._btn_reorg.config(state="disabled")
        if hasattr(self, "_btn_curate"):
            self._btn_curate.config(state="disabled")
        self._btn_cancel.config(state="normal")
        self._btn_detect.config(state="disabled")

    # -----------------------------------------------------------------------
    # Stats helpers
    # -----------------------------------------------------------------------

    def _update_sync_stats(self):
        if self._agent:
            s = self._agent.stats
            for k in ("downloaded", "copied", "updated", "skipped", "missing"):
                self._sync_stat_vars[k].set(str(s.get(k, 0)))

    def _update_audit_stats(self, stats: dict):
        for k in ("audited", "changed", "skipped", "errors"):
            self._audit_stat_vars[k].set(str(stats.get(k, 0)))

    def _sync_summary(self) -> str:
        if not self._agent:
            return ""
        s = self._agent.stats
        return (
            f"Downloaded: {s.get('downloaded',0)}  Copied: {s['copied']}  "
            f"Updated: {s['updated']}  Skipped: {s['skipped']}  Missing: {s['missing']}\n"
        )

    def _audit_summary(self) -> str:
        vals = {k: self._audit_stat_vars[k].get()
                for k in ("audited", "changed", "skipped", "errors")}
        return (
            f"Audit — Audited: {vals['audited']}  Changed: {vals['changed']}  "
            f"Skipped: {vals['skipped']}  Errors: {vals['errors']}"
        )

    def _reset_stats(self):
        for v in self._sync_stat_vars.values():
            v.set("0")
        for v in self._audit_stat_vars.values():
            v.set("0")

    # -----------------------------------------------------------------------
    # Log helpers
    # -----------------------------------------------------------------------

    def _on_log(self, msg: str, _level: str = "INFO"):
        if   "[ERROR]"   in msg: tag = "ERROR"
        elif "[WARNING]" in msg: tag = "WARNING"
        elif "AUDIT"     in msg: tag = "AUDIT"
        elif "Download"  in msg: tag = "DOWNLOAD"
        elif "COMPLETE"  in msg: tag = "SUCCESS"
        else:                    tag = "INFO"
        self.after(0, self._update_sync_stats)
        self.after(0, lambda m=msg, t=tag: self._append_log(m, t))

    def _append_log(self, msg: str, tag: str = "INFO"):
        self._log_area.config(state="normal")
        self._log_area.insert(tk.END, msg + "\n", tag)
        self._log_area.see(tk.END)
        self._log_area.config(state="disabled")

    def _clear_log(self):
        self._log_area.config(state="normal")
        self._log_area.delete("1.0", tk.END)
        self._log_area.config(state="disabled")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        app = App()
        app.mainloop()
    except Exception as exc:
        import traceback
        error_path = config.BASE_DIR / "startup_error.txt"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
