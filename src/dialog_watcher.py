"""
dialog_watcher.py  —  Background Win11 shell dialog auto-dismisser
====================================================================
Win11 shows confirmation dialogs during MTP operations regardless of
FOF_NOCONFIRMATION flags. This module runs a background daemon thread
that polls every 250ms and auto-clicks the correct button.

Dialogs handled:
  "Copy File"             → click "Copy and Replace"
  "Replace or Skip Files" → click "Replace the files in the destination"
  "Confirm File Delete"   → click "Yes"
  "Confirm Folder Delete" → click "Yes"  (MK8: MTP items appear as folders)
  "Error Moving File or Folder" → click "OK"

Auto-click waterfall (tried in order per dialog):
  1. pywinauto UIA  — most reliable, finds controls by automation ID
  2. ctypes title match — EnumWindows + button text substring
  3. ctypes buttonless fallback — if dialog found but no matching button,
     send Enter keypress (accepts default/focused button)

The "Do this for all conflicts" checkbox is ticked before clicking
whenever present, so a single click resolves all queued conflicts.

Usage:
    import dialog_watcher
    dialog_watcher.start()   # once at startup

Dependencies: pywinauto optional (pip install pywinauto).
              ctypes fallback requires no extra deps.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

# ==========================================================================
# MK18 #9 — Dialog denylist for elevation / system / security dialogs
# ==========================================================================
# ROOT CAUSE: The Enter-key fallback fires on the "focused control" without
# verifying that the dialog belongs to the transfer process.  Elevation
# prompts (UAC), BitLocker, Windows Security Center, and System dialogs
# can appear during a long sync.  An accidental Enter on a UAC prompt
# "Yes/No" defaults to "Yes" and can approve privilege escalation.
#
# Fix: check the window title against a denylist BEFORE any button click
# or Enter-key send.  If matched, log a CRITICAL warning and skip.
# The denylist is strict substring matching (case-insensitive).
#
# MK18 also adds the concept of "transfer-dialog signature" — any window
# we interact with must contain at least one of the REQUIRED_TRANSFER_WORDS.
# If a window matches neither a known transfer dialog title NOR contains a
# transfer word, the Enter fallback is suppressed entirely.
# ==========================================================================

# Titles whose presence means we MUST NOT send keystrokes
_DIALOG_DENYLIST: tuple[str, ...] = (
    # ── Security / elevation dialogs ────────────────────────────────────────
    "user account control",
    "do you want to allow",        # UAC variant phrasing
    "windows security",
    "windows defender",
    "bitlocker",
    "credential required",
    "elevation required",
    "smartscreen",
    "publisher could not be verified",
    "open file - security warning",
    "internet security warning",
    "certificate",
    # ── MK26 C7 (A4): Project folder windows — never valid transfer targets ──
    # Evidence (LOG_6_5_26): "Tab+Space sent to 'SpotifyTunerMK10 - Copy - File Explorer'"
    # The code folder was open in Windows Explorer; Tab+Space went there instead of
    # the device transfer dialog. "spotifytuner" is never a device name.
    "spotifytuner",
    "walkmanzx500 spotifysync",    # project root folder path
    "- copy - file explorer",      # explicit "- Copy -" subfolder in Explorer
    "- copy",                      # any "Copy" variant of the project folder                  # TLS/cert dialogs
    "firewall",
    "task manager",
    "event viewer",
    "registry editor",
    "format ",                      # "Format Drive" dialogs
    "are you sure",                 # generic destructive confirm
    "uninstall",
)

# A genuine transfer dialog must contain at least one of these substrings
_REQUIRED_TRANSFER_WORDS: tuple[str, ...] = (
    "copying",
    "moving",
    "transfer",
    "walkman",
    "sd card",
    "storage",
    "conflict",
    "error copying",
    "replace",
    "skip",
    "retry",
)


def _is_denylisted_dialog(title: str) -> bool:
    """Return True if the window title matches a denylist entry."""
    t = title.lower()
    return any(d in t for d in _DIALOG_DENYLIST)


# MK26 C7 (A4): Device keyword guard — transfer dialogs must reference
# the actual device (Walkman, SD card, etc.) not the project folder.
_DEVICE_TITLE_KEYWORDS: tuple[str, ...] = (
    "walkman",
    "sd card",
    "nw-zx",
    "storage",
    "portable media",
    "copying",
    "moving",
    "confirm file delete",
    "delete multiple",
    "replace",
    "skip",
    "conflict",
)


def _is_transfer_dialog(title: str) -> bool:
    """
    MK26 C7 (A4): Returns True if the window is a genuine device transfer dialog.
    Requires BOTH: (a) not in denylist AND (b) title contains a device keyword.
    Prevents Tab+Space going to project-folder Explorer windows.
    """
    t = title.lower()
    # Must contain at least one word from transfer words list
    has_transfer_word = any(w in t for w in _REQUIRED_TRANSFER_WORDS)
    # Must also contain at least one device/operation keyword
    has_device_keyword = any(k in t for k in _DEVICE_TITLE_KEYWORDS)
    return has_transfer_word and has_device_keyword


# ── Dialog table ──────────────────────────────────────────────────────────────
# (window_title_substring, button_text_substring)
# Both matched case-insensitively. & prefixes on button text are stripped.
_DIALOGS: list[tuple[str, str]] = [
    # Copy-file collision dialog — Win11 shows this as "Copy File" or "Copying..."
    ("Copy File",                   "Copy and Replace"),
    ("Copying",                     "Copy and Replace"),   # Win11 sometimes uses "Copying..."
    ("Replace or Skip Files",       "Replace the files in the destination"),
    ("Replace or Skip Files",       "Replace the files"),
    # Delete confirmation — both file and folder variants.
    # MK8 Fix (LOG_17_3): "Confirm Folder Delete" appears when Shell.Application
    # InvokeVerb("delete") is called on an MTP item that the COM layer treats as a
    # folder (the Walkman strips file extensions from item.Name).  The primary fix
    # is to use mtp.delete_file_on_device() / wpd_transfer.delete_file_ps() which
    # runs in a subprocess with hwnd=0 (no dialog possible).  This entry is a safety
    # net for any remaining in-process InvokeVerb("delete") call sites.
    ("Confirm Folder Delete",       "Yes"),
    ("Confirm File Delete",         "Yes"),
    ("Delete File",                 "Yes"),
    ("Delete Multiple Items",       "Yes"),
    ("Delete Folder",               "Yes"),
    # Misc
    ("File In Use",                 "Continue"),
    # Error dialogs — matched so the consecutive-error counter fires
    # (Enter dismisses the OK button; counter aborts after N repeats)
    ("Error Copying File",          "OK"),
    ("Error Copying File or Folder","OK"),
    ("Error Moving File",           "OK"),
    ("Error Moving File or Folder", "OK"),
    # Broad catch-all — any window title containing "copy" with a "replace" button
    ("Copy",                        "Replace"),
]

# Window title substrings that should NEVER be auto-dismissed (safety guard)
_NEVER_DISMISS = {"task manager", "visual studio", "chrome", "firefox", "edge"}

_running  = False
_paused   = False
_thread: threading.Thread | None = None
_lock     = threading.Lock()

# MK17 Patch 13 (post-LOG_17_1 fix): tighter polling.  LOG_17_1 showed
# "The requested resource is in use" dialogs slipping through — Windows
# was popping them faster than the 250ms poll could catch.  100ms feels
# instant to the user without measurably impacting CPU.  Pre-MK17 P13:
# POLL_INTERVAL_S = 0.25
POLL_INTERVAL_S = 0.10
_dismissed_at: dict[int, float] = {}   # hwnd → last dismiss timestamp
# MK17 Patch 13: shorter cooldown.  Pre-MK17 P13 was 2.0s which blocked
# legitimate consecutive dismissals when Windows reused the same hwnd
# (Win11 batches dialogs into one toplevel sometimes).  300ms is enough
# to avoid double-clicks within a single user-perceived dialog while
# allowing rapid-fire dismissal of distinct dialogs.
_DISMISS_COOLDOWN_S = 0.3              # do not re-dismiss same hwnd for 300ms

# ── Consecutive error tracking ────────────────────────────────────────────────
# If the same "Error Copying File or Folder" dialog appears more than
# _MAX_CONSECUTIVE_ERRORS times in a row (each ~62s apart), the transfer is
# permanently stuck. We signal the provided cancel_flag and stop dismissing
# so the caller can abort cleanly instead of looping for hours.
# Raised from 3 → 10: the old threshold killed a 25,292-file Reorganizer pass
# after just 3 copy errors on Piñata files (Output.4.2 Run 1), which is too
# aggressive for large passes where a few problem files are expected.
_MAX_CONSECUTIVE_ERRORS = 10
_consecutive_error_count = 0
_last_error_hwnd: int | None = None
_cancel_flag: list | None = None          # [False] mutable flag set by caller (GUI cancel)

# ── REV 14.7: separate abort flag for dialog_watcher error-loop trips ────
# LOG-11 (2026-04-19 19:15:19) revealed that mixing "user pressed GUI Cancel"
# and "dialog_watcher detected a persistent error dialog" into one shared
# flag makes the consumer's log message lie about causation.  This second
# flag is set ONLY by the error-loop detector in the watcher thread, so the
# consumer can distinguish the two cases and log accordingly.
_dw_abort_flag: list | None = None        # [False] set only by error-loop detection

# ── REV 14: public dismissals counter ─────────────────────────────────────
# Monotonic count of every dialog this watcher has auto-dismissed since
# start().  The transfer layer reads this before and after each MTP
# CopyHere to detect "dialog storm" bursts — if the counter jumped by
# >N during a single file copy, the session is almost certainly having
# MTP stack trouble and should pre-emptively failover to ADB.
_dismissals_total = 0
_dismissals_lock  = threading.Lock()

_ERROR_TITLES = {"error copying", "error moving", "error copying file", "error moving file",
                 "error copying file or folder", "error moving file or folder"}


def get_dismissals_total() -> int:
    """Return the monotonic count of dismissals performed this session."""
    with _dismissals_lock:
        return _dismissals_total


def _bump_dismissals() -> None:
    global _dismissals_total
    with _dismissals_lock:
        _dismissals_total += 1


def set_cancel_flag(flag: list) -> None:
    """
    Provide a mutable flag list (e.g. [False]) that dialog_watcher will set
    to True if it detects a persistent copy error. The reorganiser checks this
    flag after each transfer and aborts if set.

    NOTE (Rev 14.7): this is typically the SAME list as the GUI Cancel button.
    Use set_abort_flag() for a separate flag that's only set by error-loop
    detection, so callers can distinguish "user cancelled" from "watcher gave up".
    """
    global _cancel_flag
    _cancel_flag = flag


def set_abort_flag(flag: list) -> None:
    """
    Rev 14.7: provide a separate mutable flag that dialog_watcher will set
    to True ONLY when error-loop detection trips.  The consumer should check
    THIS flag to emit the "ERROR LOOP ABORT" log line — checking _cancel_flag
    (as Rev 14.5 did) misattributes GUI cancel and sync-level cancel to the
    watcher, as LOG-11 on 2026-04-19 showed.
    """
    global _dw_abort_flag
    _dw_abort_flag = flag


def _is_error_dialog(win_title: str) -> bool:
    wl = win_title.lower()
    return any(e in wl for e in _ERROR_TITLES)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_amp(text: str) -> str:
    """Remove Windows accelerator & prefix from button text."""
    return text.replace("&", "")


def _title_matches(win_title: str, pattern: str) -> bool:
    wl = win_title.lower()
    if any(bad in wl for bad in _NEVER_DISMISS):
        return False
    return pattern.lower() in wl


def _btn_matches(btn_text: str, pattern: str) -> bool:
    return pattern.lower() in _strip_amp(btn_text).lower()


# ── Method 1: pywinauto ───────────────────────────────────────────────────────

def _try_pywinauto() -> bool:
    try:
        import re as _re
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        dismissed = False

        for title_pat, btn_pat in _DIALOGS:
            try:
                wins = desktop.windows(
                    title_re=f".*{_re.escape(title_pat)}.*",
                    visible_only=True,
                )
            except Exception:
                continue

            for win in wins:
                win_title = ""
                try:
                    win_title = win.window_text() or ""
                except Exception:
                    pass
                if any(bad in win_title.lower() for bad in _NEVER_DISMISS):
                    continue

                # Tick "Do this for all conflicts" checkbox
                try:
                    for ctrl in win.descendants(control_type="CheckBox"):
                        txt = (ctrl.window_text() or "").lower()
                        if "all" in txt or "conflict" in txt:
                            if not ctrl.get_toggle_state():
                                ctrl.toggle()
                                time.sleep(0.1)
                except Exception:
                    pass

                # REV 14.2: Win11 Shell "Copy File" dialog renders action
                # options as Hyperlink/TextBlock controls, NOT Button.  The
                # screenshot that prompted this fix showed:
                #   → Copy and Replace   (rendered as Hyperlink)
                #   → Don't copy         (rendered as Hyperlink)
                #   Cancel               (rendered as Button — do NOT click)
                # Previous Button-only enumeration missed the first two and
                # fell through to the Enter-key fallback, which accepts the
                # focused control — typically Cancel.  We now try Hyperlink
                # and Text controls FIRST, then Button as legacy fallback.
                clicked = False
                for ctrl_type in ("Hyperlink", "Text", "Button"):
                    try:
                        for ctrl in win.descendants(control_type=ctrl_type):
                            name = _strip_amp(ctrl.window_text() or "")
                            if not name:
                                continue
                            # Safety: never click Cancel/Close/Skip even if
                            # the pattern would match (it shouldn't, but
                            # defense in depth).
                            low = name.lower()
                            if any(bad in low for bad in ("cancel", "skip", "don't", "dont", "close")):
                                continue
                            # Text controls are dialog body text, not buttons.
                            # Require exact match to avoid false positives when
                            # a file/album name in the body contains the button
                            # word (e.g. "Yes Lawd!" matching button pattern "Yes").
                            if ctrl_type == "Text" and low.strip() != btn_pat.lower():
                                continue
                            if btn_pat.lower() in low:
                                try:
                                    ctrl.click_input()
                                except Exception:
                                    # Hyperlink controls sometimes don't
                                    # support click_input — try invoke.
                                    try:
                                        ctrl.invoke()
                                    except Exception:
                                        continue
                                log.info(
                                    f"  [DialogWatcher/uia/{ctrl_type}] '{name}' in '{win_title}'"
                                )
                                dismissed = True
                                _bump_dismissals()
                                time.sleep(0.3)
                                clicked = True
                                break
                    except Exception:
                        continue
                    if clicked:
                        break

                # MK17 Patch 13 (post-LOG_17_1 fix): Enter-key fallback.
                # LOG_17_1 user reported some "Error Copying File or Folder:
                # The requested resource is in use" dialogs slipping
                # through to require manual OK clicks.  When the named-
                # button dispatch above doesn't fire (rare timing race
                # where the window exists but its OK control isn't
                # accessible yet), send Enter to the foreground.  The OK
                # button on these error dialogs is the default action,
                # so Enter dismisses cleanly.  Only do this for known
                # error-dialog title patterns so we don't accidentally
                # dismiss something else.
                if not clicked and _is_error_dialog(win_title):
                    try:
                        # Use SetForegroundWindow + Enter via SendKeys.
                        # We do this via win.set_focus() + win.type_keys()
                        # which pywinauto provides directly.
                        try:
                            win.set_focus()
                            time.sleep(0.05)
                        except Exception:
                            pass
                        # MK17/MK18 FIX-F9: denylist guard before Enter fallback.
                        # ROOT CAUSE: unguarded VK_RETURN confirmed UAC, delete, format
                        # dialogs during overnight runs. Guard with class + title denylist.
                        _DENY_CLASSES = {
                            "Credential Dialog Xaml Host",
                            "Shell_TrayWnd",
                            "Consent UI for Windows",
                        }
                        _DENY_TITLE_FRAGMENTS = [
                            "User Account Control", "Windows Security",
                            "Administrator", "Elevation", "Password",
                            "Delete", "Format", "Uninstall",
                        ]
                        try:
                            _win_cls = getattr(win, "class_name", lambda: "")()
                        except Exception:
                            _win_cls = ""
                        _title_upper = (win_title or "").upper()
                        _deny = (
                            _win_cls in _DENY_CLASSES
                            or any(f.upper() in _title_upper for f in _DENY_TITLE_FRAGMENTS)
                        )
                        if _deny:
                            log.warning(
                                f"  [DialogWatcher/EnterFallback] DENIED — "
                                f"'{win_title}' (class={_win_cls!r}) matched denylist"
                            )
                        else:
                            try:
                                win.type_keys("{ENTER}", set_foreground=False)
                                log.info(
                                    f"  [DialogWatcher/uia/EnterFallback] '{win_title}' "
                                    f"(named-button match missed; sent ENTER as default action)"
                                )
                                dismissed = True
                                _bump_dismissals()
                                time.sleep(0.3)
                                clicked = True
                            except Exception as fbe:
                                log.debug(f"  [DialogWatcher/EnterFallback] failed: {fbe}")
                    except Exception:
                        pass

        return dismissed
    except ImportError:
        return False
    except Exception as e:
        log.debug(f"  [DialogWatcher/uia] error: {e}")
        return False


# ── Method 2: ctypes EnumWindows ─────────────────────────────────────────────

def _try_ctypes() -> bool:
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32    = ctypes.windll.user32
        dismissed = False

        # Snapshot all visible top-level windows
        visible: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _enum_top(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                visible.append((hwnd, buf.value))
            return True

        user32.EnumWindows(_enum_top, 0)

        for title_pat, btn_pat in _DIALOGS:
            for hwnd, win_title in visible:
                if not _title_matches(win_title, title_pat):
                    continue

                # Enumerate child controls
                children: list[tuple[int, str, str]] = []

                @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
                def _enum_child(ch, _):
                    cls = ctypes.create_unicode_buffer(256)
                    txt = ctypes.create_unicode_buffer(512)
                    user32.GetClassNameW(ch, cls, 256)
                    user32.GetWindowTextW(ch, txt, 512)
                    children.append((ch, cls.value, txt.value))
                    return True

                children.clear()
                user32.EnumChildWindows(hwnd, _enum_child, 0)

                # Tick "Do this for all conflicts" checkbox
                for ch, cls, txt in children:
                    if cls == "Button" and ("all" in txt.lower() or "conflict" in txt.lower()):
                        checked = user32.SendMessageW(ch, 0x00F0, 0, 0)  # BM_GETCHECK
                        if not checked:
                            user32.SendMessageW(ch, 0x00F1, 1, 0)        # BM_SETCHECK
                            time.sleep(0.1)

                # Cooldown guard — don't hammer the same dialog twice
                now = time.time()
                if _dismissed_at.get(hwnd, 0) > now - _DISMISS_COOLDOWN_S:
                    continue

                # Click matching button (classic Win32 Button control)
                clicked = False
                for ch, cls, txt in children:
                    if cls == "Button" and _btn_matches(txt, btn_pat):
                        # REV 14.2 safety: never click a button whose text
                        # looks destructive (Cancel, Skip, Don't, Close).
                        # The _DIALOGS table should already avoid matching
                        # these, but defense in depth — a pattern match on
                        # "Copy" could accidentally hit "Don't copy".
                        txt_low = _strip_amp(txt).lower()
                        if any(bad in txt_low for bad in ("cancel", "skip", "don't", "dont", "close")):
                            continue
                        user32.SendMessageW(ch, 0x00F5, 0, 0)            # BM_CLICK
                        log.info(
                            f"  [DialogWatcher/ctypes] '{_strip_amp(txt)}'"
                            f" in '{win_title}'"
                        )
                        _dismissed_at[hwnd] = now
                        dismissed = True
                        clicked   = True
                        _bump_dismissals()
                        time.sleep(0.3)
                        break

                # REV 14.2: Win11 Fluent copy-conflict dialog detection.
                # If the classic Button path found nothing, check whether
                # this is a Fluent dialog — identified by the presence of
                # a DirectUIHWND child.  Win11's "Copy File" dialog renders
                # action options as XAML Hyperlink controls inside a
                # DirectUIHWND container; EnumChildWindows cannot see them.
                # For these, we use keyboard navigation instead.
                has_directui = any(cls == "DirectUIHWND" for _ch, cls, _txt in children)

                if not clicked and has_directui:
                    # Fluent dialog.  Strategy: focus the window, then use
                    # Tab to cycle through focusable elements.  The first
                    # Tab press typically lands on the first action
                    # (Copy and Replace in the overwrite-conflict dialog,
                    # Replace the files in the destination in the
                    # multi-file version).  We press Space to activate the
                    # focused link/button — Space works for both Hyperlink
                    # and Button controls; Enter would dismiss via the
                    # default (Cancel) instead.
                    log.debug(
                        f"  [DialogWatcher/fluent] Detected Fluent dialog '{win_title}' — "
                        "using Tab+Space keyboard navigation"
                    )
                    VK_TAB   = 0x09
                    VK_SPACE = 0x20
                    KEYEVENTF_KEYUP = 0x0002
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.1)
                    # Single Tab to reach first action link, then Space.
                    user32.keybd_event(VK_TAB, 0, 0, 0)
                    user32.keybd_event(VK_TAB, 0, KEYEVENTF_KEYUP, 0)
                    time.sleep(0.05)
                    user32.keybd_event(VK_SPACE, 0, 0, 0)
                    user32.keybd_event(VK_SPACE, 0, KEYEVENTF_KEYUP, 0)
                    log.info(
                        f"  [DialogWatcher/fluent] Tab+Space sent to '{win_title}' "
                        f"(first action — expected: {btn_pat!r})"
                    )
                    _dismissed_at[hwnd] = now
                    dismissed = True
                    clicked   = True
                    _bump_dismissals()
                    time.sleep(0.4)

                # ── Method 3: Enter-key fallback ──────────────────────────────
                # If we matched the window title but found no button by text
                # and it's NOT a Fluent dialog, send Enter to accept the
                # focused/default button. This handles legacy dialogs where
                # button text is rendered as a custom control (not Win11
                # Fluent, which we handle above).
                #
                # 2026-07-22: the broad "Copy"/"Replace" catch-all entry at
                # the end of _DIALOGS matches ANY visible window with the
                # substring "copy" anywhere in its title — confirmed live to
                # match an unrelated window (its title happened to contain
                # "CopyHere") and send it real Enter keypresses roughly every
                # 4s for 5+ minutes, because no button was found on it so
                # control fell straight through to this fallback. A bare
                # "copy" substring with no button and no real dialog
                # structure is too weak a signal to justify a keystroke.
                # _is_transfer_dialog() already existed for this purpose
                # (MK26 C7) but was never actually called anywhere in this
                # file. Wiring it in for every entry would also break
                # legitimate specific dialogs like "Confirm File Delete"
                # (their titles don't contain any _REQUIRED_TRANSFER_WORDS
                # either), so it's scoped narrowly here: only the generic
                # "Copy" pattern is gated, and only when there's neither a
                # Fluent dialog structure (has_directui — real evidence this
                # is an actual dialog window) nor a transfer+device keyword
                # match in the title.
                if (
                    title_pat == "Copy"
                    and not has_directui
                    and not _is_transfer_dialog(win_title)
                ):
                    log.debug(
                        f"  [DialogWatcher] Skipping Enter-fallback on "
                        f"'{win_title}' — matched only the broad 'Copy' "
                        f"catch-all with no button, no Fluent structure, and "
                        f"no transfer+device keywords; too weak a signal to "
                        f"send a keystroke."
                    )
                    continue

                if not clicked:
                    # MK9 ISSUE-2 FIX: restructured so error-dialog counter and
                    # Enter-refuse guard are mutually exclusive.  Pre-MK9 code
                    # put the Enter-refuse check AFTER the error counter, so
                    # "Error Copying File or Folder" (title contains "copying")
                    # hit the refuse guard and was NEVER dismissed — causing the
                    # consecutive-error loop to trip after 3 non-dismissals and
                    # falsely abort the entire transfer session (~20k log lines).
                    #
                    # New flow:
                    #   Branch A — IS an error dialog:
                    #     → increment counter
                    #     → if >= MAX: signal abort, stop
                    #     → else: SEND ENTER to dismiss (error dialogs have OK as
                    #       default button; Enter is always safe here)
                    #   Branch B — is a COPY-CONFLICT dialog (Copy File, Replace or Skip):
                    #     → refuse Enter (default is Cancel in Win11 Fluent)
                    #     → log the pywinauto install hint ONCE
                    #   Branch C — any other dialog without a matching button:
                    #     → send Enter (legacy Win32 dialogs, default is usually OK)
                    global _consecutive_error_count, _last_error_hwnd, _cancel_flag, _dw_abort_flag

                    win_lc = win_title.lower()

                    if _is_error_dialog(win_title):
                        # ── Branch A: error dialog ────────────────────────────
                        if hwnd == _last_error_hwnd:
                            _consecutive_error_count += 1
                        else:
                            _consecutive_error_count = 1
                            _last_error_hwnd = hwnd

                        if _consecutive_error_count >= _MAX_CONSECUTIVE_ERRORS:
                            log.error(
                                f"  [DialogWatcher] ERROR LOOP DETECTED: '{win_title}' "
                                f"dismissed {_consecutive_error_count} times — the copy "
                                f"operation is permanently stuck. Signalling abort."                            )
                            if _cancel_flag is not None:
                                _cancel_flag[0] = True
                            if _dw_abort_flag is not None:
                                _dw_abort_flag[0] = True
                            continue  # stop dismissing; let caller abort cleanly

                        # < MAX errors: dismiss this instance via Enter (OK is
                        # default on all known "Error Copying File or Folder" dialogs)
                        # MK18 #9: final denylist guard before keystroke
                        if _is_denylisted_dialog(win_title):
                            log.critical(
                                f"  [DialogWatcher/DENYLIST] BLOCKED Enter on "
                                f"'{win_title}' — security/elevation dialog."
                            )
                            continue
                        VK_RETURN = 0x0D
                        KEYEVENTF_KEYUP = 0x0002
                        user32.SetForegroundWindow(hwnd)
                        time.sleep(0.05)
                        user32.keybd_event(VK_RETURN, 0, 0, 0)
                        user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
                        log.info(
                            f"  [DialogWatcher/enter-error] dismissed error #{_consecutive_error_count} "
                            f"via Enter: '{win_title}'"
                        )
                        _dismissed_at[hwnd] = now
                        dismissed = True
                        _bump_dismissals()
                        time.sleep(0.3)

                    elif "copy file" in win_lc or "copying" in win_lc or "replace or skip" in win_lc:
                        # ── Branch B: copy-conflict dialog ────────────────────
                        # Win11 Fluent default button is Cancel — Enter is UNSAFE.
                        # pywinauto UIA is the only safe path for these.
                        # Emit the install hint, but only if pywinauto isn't loaded
                        # (checked once per session via the module-level flag).
                        log.warning(
                            f"  [DialogWatcher] Refusing Enter-fallback on '{win_title}' — "
                            "default button may be Cancel.  Install pywinauto for "
                            "reliable Hyperlink clicking: pip install pywinauto"
                        )
                        # Reset error counter: this is a copy dialog, not an error.
                        _consecutive_error_count = 0
                        _last_error_hwnd = None

                    else:
                        # ── Branch C: legacy dialog — Enter is safe ───────────
                        _consecutive_error_count = 0
                        _last_error_hwnd = None
                        VK_RETURN = 0x0D
                        KEYEVENTF_KEYUP = 0x0002
                        user32.SetForegroundWindow(hwnd)
                        time.sleep(0.05)
                        user32.keybd_event(VK_RETURN, 0, 0, 0)
                        user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
                        log.info(
                            f"  [DialogWatcher/enter] sent Enter to '{win_title}'"
                        )
                        _dismissed_at[hwnd] = now
                        dismissed = True
                        _bump_dismissals()
                        time.sleep(0.3)

        return dismissed

    except Exception as e:
        log.debug(f"  [DialogWatcher/ctypes] error: {e}")
        return False


# ── Watcher loop ──────────────────────────────────────────────────────────────

def _watcher_loop():
    global _running, _paused

    # 2026-08-29: this daemon thread calls pywinauto's UIA backend (comtypes
    # under the hood) on a 100ms poll without ever initializing a COM
    # apartment on this thread. comtypes normally does this lazily on first
    # use, but that path is racy under a tight poll loop started from a
    # background thread of a console-less subprocess — live audit logs show
    # the process dying with exit code 3221225477 (0xC0000005, access
    # violation) right around this thread's early UIA calls, with no Python
    # traceback (a native crash can't be caught by `except Exception`).
    # _mc_sim.py documents the same crash class elsewhere in this codebase
    # ("ThreadPoolExecutor removed from _recurse_audio: scan no longer
    # crashes COM") — COM objects require the calling thread to hold an
    # apartment; using them from a thread that never called CoInitialize is
    # undefined behavior, not just "slower". Explicitly initializing (STA,
    # matching pywinauto's own expectation) and uninitializing on this
    # thread's lifetime closes that gap. A prior fix (CREATE_NO_WINDOW on
    # the parent Popen) reduced but did not eliminate this crash — that
    # addressed a different contributing factor, not this root cause.
    _com_initialized = False
    try:
        import comtypes
        comtypes.CoInitialize()
        _com_initialized = True
    except Exception as e:
        log.debug(f"  [DialogWatcher] CoInitialize failed (continuing without it): {e}")

    try:
        # Detect backend
        try:
            import pywinauto  # noqa: F401
            _has_pwa = True
            log.debug("  [DialogWatcher] pywinauto available")
        except ImportError:
            _has_pwa = False
            log.debug("  [DialogWatcher] using ctypes fallback")

        _cleanup_counter = 0
        while _running:
            _cleanup_counter += 1
            if _cleanup_counter % 40 == 0:  # every ~10s
                cutoff = time.time() - 10
                for k in list(_dismissed_at):
                    if _dismissed_at[k] < cutoff:
                        del _dismissed_at[k]
            if not _paused:
                try:
                    dismissed = False
                    if _has_pwa:
                        dismissed = _try_pywinauto()
                    # Always also run ctypes scan — pywinauto can miss some Win11
                    # fluent-UI dialogs that are still visible to raw win32 APIs.
                    if not dismissed:
                        _try_ctypes()
                except Exception as e:
                    log.debug(f"  [DialogWatcher] loop error: {e}")
            time.sleep(POLL_INTERVAL_S)
    finally:
        if _com_initialized:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


# ── Public API ────────────────────────────────────────────────────────────────

def start():
    """Start the background watcher. Safe to call multiple times."""
    global _running, _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _running = True
        _thread  = threading.Thread(
            target=_watcher_loop,
            name="dialog-watcher",
            daemon=True,
        )
        _thread.start()
        log.info("  [DialogWatcher] Started")


def stop():
    global _running
    _running = False


def pause():
    global _paused
    _paused = True


def resume():
    global _paused
    _paused = False


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
