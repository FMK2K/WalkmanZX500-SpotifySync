"""
wpd_transfer.py  -  MTP file transfer using 3 independent methods
==================================================================
Waterfall approach - tries each method in order, returns True on first success:

  1. PowerShell Shell.Application  - separate process, no COM apartment issues,
                                     confirmed working (nosalan/powershell-mtp-file-transfer)
  2. pywin32 win32com Shell        - SHFileOperation via win32com, different code
                                     path from our main CopyHere calls
  3. comtypes WPD                  - IPortableDeviceContent.CreateObjectWithPropertiesAndData
                                     kept as last resort

Install: pip install pywin32 comtypes
"""
from __future__ import annotations
import ctypes, logging, pathlib, subprocess, sys, tempfile, time

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# REV 14.1 — PowerShell executable resolution (MODULE SCOPE)
# ---------------------------------------------------------------------------
# Previously these lived inside _try_powershell() as function-locals with
# `global _ps_exe` inside the nested _find_ps_exe() helper.  That is a
# scope mismatch — `global` searches the MODULE namespace, not the enclosing
# function, so the first call into _find_ps_exe() after an exception path
# raised `NameError: name '_ps_exe' is not defined`.
#
# LOG-9 (2026-04-18) captured this at line 14574:
#   [PS] Error during Alikiba - CINDERELLA - 04 ...: NameError: ...
#
# Moving them to true module scope makes `global _ps_exe` resolve correctly
# and lets the cache persist across _try_powershell invocations instead of
# being reset on every call.

_PS_CANDIDATES = ["pwsh", "pwsh.exe", "powershell", "powershell.exe"]
_ps_exe: str | None = None


def _find_ps_exe() -> str | None:
    """Return first working PowerShell executable, caching the result."""
    global _ps_exe
    if _ps_exe is not None:
        return _ps_exe or None   # "" sentinel → None
    import shutil as _sh
    for cand in _PS_CANDIDATES:
        if _sh.which(cand):
            _ps_exe = cand
            log.debug(f"  [PS] Using PowerShell executable: {cand}")
            return cand
    log.warning(
        "  [PS] Neither 'pwsh' nor 'powershell' found on PATH. "
        "Add PowerShell to your PATH or install PowerShell 7 "
        "(https://aka.ms/powershell) to enable fast MTP transfers. "
        "Falling back to in-process CopyHere (slower, may timeout)."
    )
    _ps_exe = ""   # sentinel: already searched, nothing found
    return None


# ---------------------------------------------------------------------------
# Method 1: PowerShell Shell.Application (separate process - no COM apartment issues)
# ---------------------------------------------------------------------------

def _try_powershell(
    src_path:     pathlib.Path,
    dest_parts:   list[str],
    device_name:  str,
    storage_name: str,
    timeout_s:    int | None = None,
) -> bool:
    """
    Transfer via PowerShell Shell.Application in a subprocess.
    Running out-of-process avoids COM apartment conflicts and the
    message-pump dependency that causes in-process CopyHere to hang.

    Critical fix: CopyHere() requires a Shell FolderItem object, not a
    bare path string. In a -NonInteractive subprocess context, passing a
    bare path to CopyHere() silently does nothing on Windows 11.
    We use $shell.NameSpace(parent_dir).ParseName(filename) to get a
    proper FolderItem before calling CopyHere($srcItem, 20).

    The PS-internal poll timeout is capped at min(timeout_s-10, 90) so
    the script always exits cleanly before subprocess.run() kills it.

    MK17 Patch 12 — when timeout_s is None, derive it adaptively from
    file size + extension via adaptive_timeout.ps_upload_timeout.  4MB
    MP3 → ~76s, 50MB FLAC → ~370s, ceiling 900s.  Old fixed default
    was 120s which underserved big files and overserved tiny ones.
    """
    # MK17 P12: adaptive timeout derivation
    if timeout_s is None:
        try:
            import adaptive_timeout as _at
            try:
                _size = src_path.stat().st_size
            except Exception:
                _size = None
            timeout_s = int(_at.ps_upload_timeout(
                size_bytes=_size, ext=src_path.suffix
            ))
        except Exception:
            timeout_s = 120  # legacy fallback

    ps_poll_s = max(30, min(timeout_s - 10, 90))

    folder_nav = ""
    for part in dest_parts:
        safe = part.replace("'", "''")
        folder_nav += f"""
$item = $parent.Items() | Where-Object {{ $_.Name -eq '{safe}' }} | Select-Object -First 1
if (-not $item) {{
    $parent.NewFolder('{safe}') | Out-Null
    Start-Sleep -Milliseconds 800
    $item = $parent.Items() | Where-Object {{ $_.Name -eq '{safe}' }} | Select-Object -First 1
}}
if ($item -and $item.IsFolder) {{ $parent = $item.GetFolder; $destination = $parent }}
"""

    safe_device  = device_name.replace("'", "''")
    safe_storage = storage_name.replace("'", "''")
    safe_src_dir = str(src_path.parent).replace("'", "''")
    safe_fname   = src_path.name.replace("'", "''")
    safe_stem    = src_path.stem.replace("'", "''")
    safe_ext     = src_path.suffix.lower()

    script = f"""
        $ErrorActionPreference = 'Stop'
        $shell   = New-Object -ComObject Shell.Application
        $comp    = $shell.NameSpace(0x11).Self.GetFolder
        $device  = $comp.Items() | Where-Object {{ $_.Name -eq '{safe_device}' }} | Select-Object -First 1
        if (-not $device) {{ Write-Output "DEVICE_NOT_FOUND"; exit 1 }}

        $storage = $device.GetFolder.Items() | Where-Object {{ $_.Name -eq '{safe_storage}' }} | Select-Object -First 1
        if (-not $storage) {{ Write-Output "STORAGE_NOT_FOUND"; exit 1 }}

        $destination = $storage.GetFolder
        $parent = $destination
        {folder_nav}

        # Silent delete of any existing file
        $existing = $destination.Items() | Where-Object {{
            $_.Name -eq '{safe_fname}' -or $_.Name -eq '{safe_stem}'
        }} | Select-Object -First 1
        if ($existing) {{
            try {{ $existing.InvokeVerbEx('delete', '') }}
            catch {{ try {{ $shell.ShellExecute($existing.Path, '', '', 'delete', 0) }} catch {{ }} }}
            Start-Sleep -Milliseconds 1500
        }}

        # Copy via FolderItem (NOT bare path string — silently fails in -NonInteractive)
        $srcFolder = $shell.NameSpace('{safe_src_dir}')
        if (-not $srcFolder) {{ Write-Output "SRC_FOLDER_NOT_FOUND"; exit 1 }}
        $srcItem = $srcFolder.ParseName('{safe_fname}')
        if (-not $srcItem) {{ Write-Output "SRC_ITEM_NOT_FOUND"; exit 1 }}

        # 4 = FOF_SILENT  |  16 = FOF_NOCONFIRMATION
        $destination.CopyHere($srcItem, 20)

        # Poll for arrival
        $success = $false
        $start   = Get-Date
        while (((Get-Date) - $start).TotalSeconds -lt {ps_poll_s}) {{
            $found = $destination.Items() | Where-Object {{
                $_.Name -eq '{safe_fname}' -or $_.Name -eq '{safe_stem}' -or
                ($_.Name.StartsWith('{safe_stem}') -and $_.Name.EndsWith('{safe_ext}'))
            }} | Select-Object -First 1
            if ($found) {{ $success = $true; break }}
            Start-Sleep -Seconds 1
        }}

        if ($success) {{
            Write-Output "SUCCESS"
        }} else {{
            $contents = ($destination.Items() | ForEach-Object {{ $_.Name }}) -join ', '
            Write-Output "TRANSFER_TIMEOUT: folder=[$contents]"
        }}
        exit 0
        """

    import tempfile as _tf
    import os as _os
    ps1 = None
    # REV 14.1: _ps_exe and _find_ps_exe are now at MODULE scope (top of file).
    # The previous function-local declaration with `global _ps_exe` inside the
    # nested helper produced NameError on first call.  Now we just call the
    # module-level helper.

    try:
        fd, ps1 = _tf.mkstemp(suffix=".ps1", prefix="wman_")
        _os.close(fd)
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(script)
        ps_exe = _find_ps_exe()
        if ps_exe is None:
            return False   # no PS available — skip immediately, CopyHere will handle it
        result = subprocess.run(
            [ps_exe, "-NonInteractive", "-NoProfile",
             "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True, text=True, timeout=timeout_s,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode == 0 and "SUCCESS" in out:
            log.info(f"  [PS] Transfer complete: {src_path.name}")
            return True
        else:
            full_out = (out or err or "no output")[:400]
            # WARNING (was DEBUG): the PS script writes specific failure
            # sentinels (DEVICE_NOT_FOUND, STORAGE_NOT_FOUND, SRC_ITEM_NOT_FOUND,
            # TRANSFER_TIMEOUT: folder=[...]) that are the only meaningful
            # diagnostic when uploads fail.  Keeping this at DEBUG made 582
            # consecutive cascade-failures completely opaque at INFO level.
            log.warning(f"  [PS] Failed (rc={result.returncode}): {full_out}")
            return False
    except subprocess.TimeoutExpired:
        log.warning(f"  [PS] Timed out after {timeout_s}s for {src_path.name}")
        return False
    except FileNotFoundError:
        log.warning("  [PS] powershell not found on PATH")
        return False
    except Exception as e:
        log.warning(f"  [PS] Error during {src_path.name}: {type(e).__name__}: {e}")
        return False
    finally:
        if ps1:
            try: _os.unlink(ps1)
            except Exception: pass


# ---------------------------------------------------------------------------
# Method 2: pywin32 win32com Shell (SHFileOperation) – kept for fallback
# ---------------------------------------------------------------------------

def _try_win32com(
    src_path:     pathlib.Path,
    dest_parts:   list[str],
    device_name:  str,
    storage_name: str,
    timeout_s:    int = 30,
) -> bool:
    """
    Transfer via pywin32 win32com.shell Shell.Application.
    Uses win32com dispatch (different COM binding path from pythoncom CopyHere).
    """
    try:
        import win32com.client
        import win32com.shell.shell as shell32
        import pywintypes
    except ImportError:
        log.debug("  [win32com] pywin32 not installed - pip install pywin32")
        return False

    try:
        sh = win32com.client.Dispatch("Shell.Application")
        ns = sh.NameSpace(0x11)  # My Computer

        # Find device
        device_item = None
        for item in ns.Items():
            if item.Name.lower() == device_name.lower():
                device_item = item
                break
        if not device_item:
            log.debug(f"  [win32com] Device {device_name!r} not found")
            return False

        # Find storage (GetFolder is a property, not a method, in win32com dispatch)
        storage_item = None
        for item in device_item.GetFolder.Items():
            if item.Name.lower() == storage_name.lower():
                storage_item = item
                break
        if not storage_item:
            log.debug(f"  [win32com] Storage {storage_name!r} not found")
            return False

        # Navigate folders
        folder = storage_item.GetFolder
        for part in dest_parts:
            part_lc = part.lower()
            found = None
            for item in folder.Items():
                if item.Name.lower() == part_lc:
                    found = item.GetFolder
                    break
            if found:
                folder = found
            else:
                folder.NewFolder(part)
                time.sleep(0.8)
                for item in folder.Items():
                    if item.Name.lower() == part_lc:
                        folder = item.GetFolder
                        break

        # Copy
        folder.CopyHere(str(src_path), 0x14)

        # Wait for file to appear (stem match)
        fname_lc = src_path.name.lower()
        stem_lc = src_path.stem.lower()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                for item in folder.Items():
                    iname = (item.Name or "").lower()
                    if iname == fname_lc or iname == stem_lc or \
                       (iname.startswith(stem_lc) and iname.endswith(src_path.suffix.lower())):
                        log.info(f"  [win32com] Transfer complete: {src_path.name}")
                        return True
            except Exception:
                pass
            time.sleep(2)

        log.debug(f"  [win32com] Timeout waiting for {src_path.name}")
        return False

    except Exception as e:
        log.debug(f"  [win32com] Error: {e}")
        return False


# ---------------------------------------------------------------------------
# Method 3: comtypes WPD – kept for completeness
# ---------------------------------------------------------------------------

_wpd_available: bool | None = None
_pdapi   = None
_pdtypes = None
_comtypes = None


def _ensure_wpd() -> bool:
    global _wpd_available, _pdapi, _pdtypes, _comtypes
    if _wpd_available is not None:
        return _wpd_available
    try:
        import comtypes, comtypes.client
        _comtypes = comtypes
        _pdapi   = comtypes.client.GetModule("portabledeviceapi.dll")
        _pdtypes = comtypes.client.GetModule("portabledevicetypes.dll")
        _wpd_available = True
        log.debug("  [WPD] bindings ready")
        return True
    except ImportError:
        log.debug("  [WPD] comtypes not installed")
        _wpd_available = False
        return False
    except Exception as e:
        log.debug(f"  [WPD] init failed: {e}")
        _wpd_available = False
        return False


CLSID_PortableDeviceManager      = "{0AF10CEC-2ECD-4B92-9581-34F6AE0637F3}"
CLSID_PortableDevice             = "{728A21C5-3D9E-48D7-9810-864848F0F404}"
CLSID_PortableDeviceValues       = "{0C15D503-D017-47CE-9016-7B3F978721CC}"
CLSID_PortableDeviceKeyCollection        = "{DE2D022D-2480-43BE-97F0-D1FA2CF98F4F}"
CLSID_PortableDevicePropVariantCollection = "{08A99E2F-6D6D-4B80-AF5A-BAF2BCBE4CB9}"
_VT_LPWSTR = 31

WPD_OBJECT_PARENT_ID          = ("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}", 3)
WPD_OBJECT_SIZE               = ("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}", 11)
WPD_OBJECT_ORIGINAL_FILE_NAME = ("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}", 12)
WPD_OBJECT_NAME               = ("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}", 4)
WPD_OBJECT_CONTENT_TYPE       = ("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}", 7)
WPD_OBJECT_FORMAT             = ("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}", 6)
WPD_CONTENT_TYPE_GENERIC_FILE = "{0085E0A6-8D34-45D7-BC5C-447E59C73D48}"
WPD_CONTENT_TYPE_FOLDER       = "{27E2E392-A111-48E0-AB0C-E17705A05F85}"
WPD_FORMAT_UNSPECIFIED        = "{30010000-AE6C-4804-98BA-C57B46965FE7}"
WPD_DEVICE_OBJECT_ID          = "DEVICE"
_FORMAT_MAP = {
    ".mp3":  "{B9015C86-7A2A-46EA-A2C8-A357FFFE2D07}",
    ".flac": "{B1018A0D-E3A3-4B1A-9A2C-B0F7C28C4DB7}",
    ".m4a":  "{A5B8DD68-7D70-4B46-9AAF-4B8588E3C34B}",
    ".aac":  "{A5B8DD68-7D70-4B46-9AAF-4B8588E3C34B}",
}


def _pk(guid_str, pid):
    pk = _pdapi._tagpropertykey()
    pk.fmtid = _comtypes.GUID(guid_str)
    pk.pid = pid
    return pk

def _create_values():
    return _comtypes.client.CreateObject(
        _comtypes.GUID(CLSID_PortableDeviceValues),
        clsctx=_comtypes.CLSCTX_INPROC_SERVER,
        interface=_pdapi.IPortableDeviceValues,
    )

def _set_str(v, key, val):   v.SetStringValue(_pk(*key), val)
def _set_guid(v, key, g):    v.SetGuidValue(_pk(*key), _comtypes.GUID(g))
def _set_u64(v, key, n):     v.SetUnsignedLargeIntegerValue(_pk(*key), n)


def _get_device_id(device_name: str):
    try:
        mgr = _comtypes.client.CreateObject(
            _comtypes.GUID(CLSID_PortableDeviceManager),
            clsctx=_comtypes.CLSCTX_INPROC_SERVER,
            interface=_pdapi.IPortableDeviceManager,
        )
        # KasparNagu confirmed pattern
        count = ctypes.pointer(ctypes.c_ulong(0))
        mgr.GetDevices(ctypes.POINTER(ctypes.c_wchar_p)(), count)
        n = count.contents.value
        if n == 0:
            log.warning("  [WPD] No WPD devices found — device may be locked by Explorer")
            return None
        ids = (ctypes.c_wchar_p * n)()
        mgr.GetDevices(ids, count)
        name_lc = device_name.lower()
        found_names = []
        first_id = None
        for did in ids[:n]:
            if not did:
                continue
            if first_id is None:
                first_id = did
            try:
                buf  = (ctypes.c_wchar * 512)()
                nlen = ctypes.c_ulong(512)
                mgr.GetDeviceFriendlyName(did, buf, nlen)
                friendly = buf.value
                found_names.append(friendly)
                if name_lc in friendly.lower():
                    log.debug(f"  [WPD] Matched by name: {friendly!r}")
                    return did
            except Exception as _e:
                log.debug(f"  [WPD] GetDeviceFriendlyName failed for {did!r}: {_e}")
                found_names.append(f"<name-unavailable:{did}>")

        # Name matching failed (comtypes binding issue with GetDeviceFriendlyName).
        # If only one device is present it must be the target — use it directly.
        if n == 1 and first_id:
            log.debug(f"  [WPD] Single device, skipping name check: {first_id!r}")
            return first_id
        log.warning(f"  [WPD] Device {device_name!r} not matched. Found: {found_names}")
    except Exception as e:
        log.warning(f"  [WPD] _get_device_id error: {e}")
    return None


def _try_wpd(
    src_path:     pathlib.Path,
    dest_parts:   list[str],
    device_name:  str,
    storage_name: str,
) -> bool:
    if not _ensure_wpd():
        return False
    try:
        device_id = _get_device_id(device_name)
        if not device_id:
            log.debug(f"  [WPD] Device not found")
            return False

        client_info = _create_values()
        device = _comtypes.client.CreateObject(
            _comtypes.GUID(CLSID_PortableDevice),
            clsctx=_comtypes.CLSCTX_INPROC_SERVER,
            interface=_pdapi.IPortableDevice,
        )
        device.Open(device_id, client_info)
        content = device.Content()

        # find storage
        storage_id = None
        keys = _comtypes.client.CreateObject(
            _comtypes.GUID(CLSID_PortableDeviceKeyCollection),
            clsctx=_comtypes.CLSCTX_INPROC_SERVER,
            interface=_pdapi.IPortableDeviceKeyCollection,
        )
        keys.Add(_pk(*WPD_OBJECT_NAME))
        props = content.Properties()

        enum = content.EnumObjects(0, WPD_DEVICE_OBJECT_ID, None)
        BATCH = 16
        while True:
            fetched = ctypes.c_ulong(0)
            buf = (ctypes.c_wchar_p * BATCH)()
            try: enum.Next(BATCH, buf, ctypes.byref(fetched))
            except: break
            if not fetched.value: break
            for oid in buf[:fetched.value]:
                if not oid: continue
                try:
                    vals = props.GetValues(oid, keys)
                    name = vals.GetStringValue(_pk(*WPD_OBJECT_NAME))
                    if storage_name.lower() in name.lower():
                        storage_id = oid
                        break
                except: continue
            if storage_id: break

        if not storage_id:
            log.debug(f"  [WPD] Storage not found")
            device.Close()
            return False

        # navigate/create dest folders
        current_id = storage_id
        for part in dest_parts:
            part_lc = part.lower()
            found_id = None
            e2 = content.EnumObjects(0, current_id, None)
            while True:
                fetched = ctypes.c_ulong(0)
                buf = (ctypes.c_wchar_p * BATCH)()
                try: e2.Next(BATCH, buf, ctypes.byref(fetched))
                except: break
                if not fetched.value: break
                for oid in buf[:fetched.value]:
                    if not oid: continue
                    try:
                        vals = props.GetValues(oid, keys)
                        name = vals.GetStringValue(_pk(*WPD_OBJECT_NAME))
                        if name.lower() == part_lc:
                            found_id = oid
                            break
                    except: continue
                if found_id: break

            if found_id:
                current_id = found_id
            else:
                fp = _create_values()
                _set_str (fp, WPD_OBJECT_PARENT_ID, current_id)
                _set_str (fp, WPD_OBJECT_NAME, part)
                _set_guid(fp, WPD_OBJECT_CONTENT_TYPE, WPD_CONTENT_TYPE_FOLDER)
                _set_guid(fp, WPD_OBJECT_FORMAT, WPD_FORMAT_UNSPECIFIED)
                new_id = ctypes.c_wchar_p()
                content.CreateObjectWithPropertiesOnly(fp, ctypes.byref(new_id))
                current_id = new_id.value

        # stream file
        file_name = src_path.name
        file_size = src_path.stat().st_size
        fmt_guid  = _FORMAT_MAP.get(src_path.suffix.lower(), WPD_FORMAT_UNSPECIFIED)

        op = _create_values()
        _set_str (op, WPD_OBJECT_PARENT_ID,          current_id)
        _set_str (op, WPD_OBJECT_ORIGINAL_FILE_NAME, file_name)
        _set_str (op, WPD_OBJECT_NAME,               file_name)
        _set_u64 (op, WPD_OBJECT_SIZE,               file_size)
        _set_guid(op, WPD_OBJECT_CONTENT_TYPE,       WPD_CONTENT_TYPE_GENERIC_FILE)
        _set_guid(op, WPD_OBJECT_FORMAT,             fmt_guid)

        CHUNK = 512 * 1024
        optimal    = ctypes.c_ulong(CHUNK)
        p_stream   = ctypes.POINTER(_pdapi.IStream)()
        new_obj_id = ctypes.c_wchar_p()

        content.CreateObjectWithPropertiesAndData(
            op,
            ctypes.byref(p_stream),
            ctypes.byref(optimal),
            ctypes.byref(new_obj_id),
        )

        block = optimal.value or CHUNK
        with open(src_path, "rb") as fh:
            while True:
                data = fh.read(block)
                if not data: break
                n = ctypes.c_ulong(0)
                p_stream.RemoteWrite(
                    (ctypes.c_byte * len(data)).from_buffer_copy(data),
                    len(data), ctypes.byref(n),
                )

        p_stream.Commit(0)
        device.Close()
        log.info(f"  [WPD] Transfer complete: {file_name}")
        return True

    except Exception as e:
        log.warning(f"  [WPD] Error: {e}")
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def copy_file_wpd(
    src_path:     pathlib.Path,
    dest_parts:   list[str],
    device_name:  str,
    storage_name: str,
) -> bool:
    """
    PowerShell-first transfer for the ZX500.
    Returns True on success, False if PowerShell fails (caller falls back to CopyHere).

    PowerShell (subprocess) is the only method that reliably works on the ZX500.
    comtypes WPD always fails (COM apartment conflict), win32com also fails.
    Both are kept in the file for diagnostic purposes but not called here.
    """
    log.debug(f"  [Transfer] Trying PowerShell ...")
    if _try_powershell(src_path, dest_parts, device_name, storage_name):
        return True

    log.debug(f"  [Transfer] PowerShell failed for {src_path.name}")
    return False


def is_available() -> bool:
    return True  # PowerShell is always available on Windows 10+




def delete_file_ps(
    device_name:  str,
    storage_name: str,
    dest_parts:   list[str],
    filename:     str,
) -> bool:
    """
    Delete a file from the MTP device silently via a PowerShell subprocess.

    Running in a separate process (WindowStyle Hidden) is the only approach
    that reliably suppresses Win11's "Confirm File Delete" MTP dialog.
    In-process approaches (SHFileOperation, InvokeVerb, InvokeVerbEx) all
    trigger the dialog on Win11 MTP because the Python process has a visible
    window handle that Win11 uses to anchor the confirmation UI.

    PowerShell with -NonInteractive -WindowStyle Hidden has hwnd=0 and no
    UI context, so Win11 cannot attach a dialog to it.

    Returns True on success or if file already absent, False on failure.
    """
    import tempfile as _tf, os as _os, subprocess as _sp

    safe_device  = device_name.replace("'", "''")
    safe_storage = storage_name.replace("'", "''")
    safe_fname   = filename.replace("'", "''")
    safe_fstem   = filename.rsplit(".", 1)[0].replace("'", "''")

    # ParseName is O(1) per level — one MTP named-object lookup.
    # Items()|Where-Object enumerates the whole folder first, which on a
    # near-full SD card can take 30-60 s just for Music/ (300+ artist dirs).
    folder_nav = ""
    for part in dest_parts:
        safe = part.replace("'", "''")
        folder_nav += f"""
$item = $parent.ParseName('{safe}')
if ($item -and $item.IsFolder) {{ $parent = $item.GetFolder }}
"""

    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$shell   = New-Object -ComObject Shell.Application
$comp    = $shell.NameSpace(0x11)
$device  = $comp.Items() | Where-Object {{ $_.Name -eq '{safe_device}' }} | Select-Object -First 1
if (-not $device) {{ Write-Output "DEVICE_NOT_FOUND"; exit 0 }}
$storage = $device.GetFolder.ParseName('{safe_storage}')
if (-not $storage) {{ Write-Output "STORAGE_NOT_FOUND"; exit 0 }}
$parent = $storage.GetFolder
{folder_nav}
$target = $parent.ParseName('{safe_fname}')
if (-not $target) {{ $target = $parent.ParseName('{safe_fstem}') }}
if (-not $target) {{ Write-Output "NOT_FOUND"; exit 0 }}
# InvokeVerb from a hidden, non-interactive PowerShell process has no window
# handle (hwnd=0), so Win11 cannot attach a confirmation dialog to it.
$target.InvokeVerb('delete')
Start-Sleep -Milliseconds 800
Write-Output "DELETED"
exit 0
"""

    ps1 = None
    try:
        fd, ps1 = _tf.mkstemp(suffix=".ps1", prefix="wman_del_")
        _os.close(fd)
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(script)
        ps_exe = _find_ps_exe()
        if ps_exe is None:
            log.debug(f"  [PS Delete] No PowerShell available — skipping delete for {filename!r}")
            return False
        result = _sp.run(
            [
                ps_exe,
                "-NonInteractive", "-NoProfile",
                "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass",
                "-File", ps1,
            ],
            capture_output=True, text=True, timeout=30,
        )
        out = (result.stdout or "").strip()
        log.debug(f"  [PS Delete] {filename}: rc={result.returncode} out={out!r}")
        return result.returncode == 0
    except Exception as e:
        log.debug(f"  [PS Delete] failed for {filename!r}: {e}")
        return False
    finally:
        if ps1:
            try: _os.unlink(ps1)
            except Exception: pass


def _batch_timeout(n_files: int, n_levels: int, free_gb: float = 3.0) -> int:
    """
    Dynamic timeout for batch_delete_files_ps.

    File type is irrelevant (MTP delete is a metadata-only operation).
    File size is weakly relevant but not available from the snapshot.

    Dominant factors:
      - free_gb:  near-full flash triggers GC on every directory write,
                  multiplying both navigation and delete latency
      - n_levels: each ParseName() is one MTP round-trip
      - n_files:  each InvokeVerb('delete') waits for device ACK

    pressure: 1× at 10 GB free, up to 5× when almost full (< 2 GB).
    """
    # Calibrated from live observations on ZX500 at ~3.3 GB free (pressure≈3):
    #   - Music/ cold enum: ~120s, captured in enum_s
    #   - per-file InvokeVerb delete: ~100-120s → base ≈ 40s at 1× pressure
    #   - subsequent ParseName levels (warm cache): ~15s each
    pressure = min(5.0, 10.0 / max(free_gb, 0.5))
    enum_s   = 120 * pressure              # Music/ first-level cold enum
    nav_s    = max(0, n_levels - 1) * 15  # subsequent levels (warm, low cost)
    files_s  = n_files * 40 * pressure    # per InvokeVerb on near-full flash
    return int(30 + enum_s + nav_s + files_s)  # 30s = PS startup buffer


def batch_delete_files_ps(
    device_name:  str,
    storage_name: str,
    folder_parts: list,
    filenames:    list,
    free_gb:      float = 3.0,
) -> dict:
    """
    Delete multiple files from the SAME folder in a single PowerShell process.
    One PS startup + one ParseName navigation chain per album, then one
    InvokeVerb('delete') per file — ~200× faster than one PS process per file
    on a near-full SD card.

    Timeout is computed dynamically via _batch_timeout(n_files, n_levels, free_gb)
    rather than a fixed value — scales with storage pressure and batch size.

    Returns {filename: "deleted" | "not_found" | "error"}.
    """
    if not filenames:
        return {}

    sd = device_name.replace("'", "''")
    ss = storage_name.replace("'", "''")

    # Build ParseName chain for each folder level
    nav = f"$parent = $storage.GetFolder\n"
    for part in folder_parts:
        sp = part.replace("'", "''")
        nav += (
            f"$item = $parent.ParseName('{sp}')\n"
            f"if (-not $item) {{ Write-Output 'NAV_FAILED:{sp}'; exit 0 }}\n"
            f"$parent = $item.GetFolder\n"
        )

    # Build one delete statement per filename, writing result token per line
    deletes = ""
    for fn in filenames:
        sf  = fn.replace("'", "''")
        stem = fn.rsplit(".", 1)[0].replace("'", "''")
        deletes += (
            f"$t = $parent.ParseName('{sf}')\n"
            f"if (-not $t) {{ $t = $parent.ParseName('{stem}') }}\n"
            f"if ($t) {{ $t.InvokeVerb('delete'); Write-Output 'DELETED:{sf}' }} "
            f"else {{ Write-Output 'NOT_FOUND:{sf}' }}\n"
        )

    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$shell   = New-Object -ComObject Shell.Application
$device  = $shell.NameSpace(0x11).Items() | Where-Object {{ $_.Name -eq '{sd}' }} | Select-Object -First 1
if (-not $device) {{ Write-Output 'DEVICE_NOT_FOUND'; exit 0 }}
$storage = $device.GetFolder.ParseName('{ss}')
if (-not $storage) {{ Write-Output 'STORAGE_NOT_FOUND'; exit 0 }}
{nav}
{deletes}
"""
    import tempfile as _tf2, os as _os3, subprocess as _sp3
    timeout_s = _batch_timeout(len(filenames), len(folder_parts), free_gb)
    ps1 = None
    try:
        fd, ps1 = _tf2.mkstemp(suffix=".ps1", prefix="wman_bdel_")
        _os3.close(fd)
        with open(ps1, "w", encoding="utf-8") as fh:
            fh.write(script)
        ps_exe = _find_ps_exe()
        if ps_exe is None:
            return {fn: "error" for fn in filenames}
        r = _sp3.run(
            [ps_exe, "-NonInteractive", "-NoProfile",
             "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True, text=True, timeout=timeout_s,
        )
        results = {fn: "error" for fn in filenames}
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("DELETED:"):
                fn = line[len("DELETED:"):]
                if fn in results:
                    results[fn] = "deleted"
            elif line.startswith("NOT_FOUND:"):
                fn = line[len("NOT_FOUND:"):]
                if fn in results:
                    results[fn] = "not_found"
            elif line in ("DEVICE_NOT_FOUND", "STORAGE_NOT_FOUND") or line.startswith("NAV_FAILED:"):
                log.warning(f"  [BatchDel] Navigation error: {line}")
                return {fn: "error" for fn in filenames}
        return results
    except _sp3.TimeoutExpired:
        log.warning(f"  [BatchDel] Timed out after {timeout_s}s (free_gb={free_gb:.1f}) for {folder_parts}")
        return {fn: "error" for fn in filenames}
    except Exception as e:
        log.warning(f"  [BatchDel] Error: {e}")
        return {fn: "error" for fn in filenames}
    finally:
        if ps1:
            try: _os3.unlink(ps1)
            except Exception: pass


# ---------------------------------------------------------------------------
# Single-PS bulk delete — one PS process, one Music/ enumeration for all groups
# ---------------------------------------------------------------------------
# The per-group PS approach re-enumerates Music/ (~1767 entries) for every
# batch, costing 400-500s each time. Running everything in ONE PS session
# enumerates Music/ once, then all ParseName calls use the cached handle.

def delete_files_single_ps(
    device_name:  str,
    storage_name: str,
    root_folder:  str,          # e.g. "Music"
    groups:       dict,         # {tuple(subfolder_parts): [filenames], ...}
    progress_cb               = None,   # callable(i, n, label, status)
    free_gb:      float       = 3.0,
) -> dict:
    """
    Delete all groups in ONE PowerShell process.
    Navigates to root_folder once; all album ParseName calls reuse the cached
    folder handle — eliminating the 400-500s Music/ re-enumeration per group.

    Returns {(storage, parts_tuple, filename): "deleted"|"not_found"|"error"}.
    """
    import tempfile as _tf5, os as _os5, subprocess as _sp5, json as _json5

    sd  = device_name.replace("'", "''")
    ss  = storage_name.replace("'", "''")
    rf  = root_folder.replace("'", "''")

    # Build token map + JSON payload.  Keeping the .ps1 tiny/fixed-size avoids
    # the PS parse failure that kills a 10 000-line inline-generated script before
    # a single statement executes.
    token_map: dict = {}
    groups_data = []
    for gi, (parts_tuple, fnames) in enumerate(groups.items()):
        sub_parts = list(parts_tuple[1:])   # skip root_folder ("Music")
        files_data = []
        for fi, fn in enumerate(fnames):
            tok = f"GRP{gi}_FIL{fi}"
            token_map[tok] = (parts_tuple, fn)
            files_data.append({"tok": tok, "name": fn, "stem": fn.rsplit(".", 1)[0]})
        groups_data.append({"gi": gi, "parts": sub_parts, "files": files_data})

    n_files = sum(len(v) for v in groups.values())
    pressure  = min(5.0, 10.0 / max(free_gb, 0.5))
    timeout_s = int(30 + 120 * pressure
                       + len(groups) * 2
                       + n_files * 40 * pressure)

    log.info(f"[SinglePS] {len(groups)} groups / {n_files} files — timeout={timeout_s}s ({timeout_s/3600:.1f}h)")

    ps1 = json_f = None
    try:
        # Write group/file data to JSON so the PS script stays ~50 lines fixed.
        fd_j, json_f = _tf5.mkstemp(suffix=".json", prefix="wman_grps_")
        _os5.close(fd_j)
        with open(json_f, "w", encoding="utf-8") as fh:
            _json5.dump(groups_data, fh, ensure_ascii=False)
        jpath = json_f.replace("\\", "\\\\")   # safe inside PS double-quoted string

        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
try {{
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class MtpOps {{
    [Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    [ComImport] interface IShellItem {{
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint n, out IntPtr ppszName);
        void GetAttributes(uint mask, out uint attribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }}
    [Guid("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    [ComImport] interface IFileOperation {{
        void Advise(IntPtr pfops, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOperationFlags(uint dwOperationFlags);
        void SetProgressMessage(string pszMessage);
        void SetProgressDialog(IntPtr popd);
        void SetProperties(IntPtr pproparray);
        void SetOwnerWindow(IntPtr hwndOwner);
        void ApplyPropertiesToItem(IShellItem psiItem);
        void ApplyPropertiesToItems(IntPtr punkItems);
        void RenameItem(IShellItem psiItem, string pszNewName, IntPtr pfopsItem);
        void RenameItems(IntPtr punkItems, string pszNewName);
        void MoveItem(IShellItem psiItem, IShellItem psiDestinationFolder, string pszNewName, IntPtr pfopsItem);
        void MoveItems(IntPtr punkItems, IShellItem psiDestinationFolder);
        void CopyItem(IShellItem psiItem, IShellItem psiDestinationFolder, string pszNewName, IntPtr pfopsItem);
        void CopyItems(IntPtr punkItems, IShellItem psiDestinationFolder);
        void DeleteItem(IShellItem psiItem, IntPtr pfopsItem);
        void DeleteItems(IntPtr punkItems);
        void NewItem(IShellItem psiDestinationFolder, uint dwFileAttributes, string pszName, string pszTemplateName, IntPtr pfopsItem);
        [PreserveSig] int PerformOperations();
        void GetAnyOperationsAborted(out bool pfAnyOperationsAborted);
    }}
    static Guid IID_IShellItem      = new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");
    static Guid CLSID_FileOperation = new Guid("3ad05575-8857-4850-9277-11b85bdb8e09");
    [DllImport("shell32.dll", PreserveSig=false)]
    static extern void SHGetIDListFromObject([MarshalAs(UnmanagedType.IUnknown)] object punk, out IntPtr ppidl);
    [DllImport("shell32.dll", PreserveSig=false)]
    static extern void SHCreateItemFromIDList(IntPtr pidl, ref Guid riid, out IShellItem ppv);
    [DllImport("ole32.dll")]
    static extern void CoTaskMemFree(IntPtr pv);
    public static bool DeleteItem([MarshalAs(UnmanagedType.IUnknown)] object folderItem) {{
        IntPtr pidl = IntPtr.Zero;
        try {{
            SHGetIDListFromObject(folderItem, out pidl);
            if (pidl == IntPtr.Zero) return false;
            IShellItem si;
            SHCreateItemFromIDList(pidl, ref IID_IShellItem, out si);
            if (si == null) return false;
            var fo = (IFileOperation)Activator.CreateInstance(Type.GetTypeFromCLSID(CLSID_FileOperation));
            fo.SetOperationFlags(0x0004 | 0x0010 | 0x0400);
            fo.DeleteItem(si, IntPtr.Zero);
            return fo.PerformOperations() >= 0;
        }} catch {{ return false; }}
        finally {{ if (pidl != IntPtr.Zero) CoTaskMemFree(pidl); }}
    }}
}}
'@
    Write-Output 'ADDTYPE:OK'
}} catch {{
    Write-Output ('ADDTYPE:FAIL:' + ($_.Exception.Message -split "`n")[0].Trim())
}}
$shell   = New-Object -ComObject Shell.Application
$device  = $shell.NameSpace(0x11).Items() | Where-Object {{ $_.Name -eq '{sd}' }} | Select-Object -First 1
if (-not $device) {{ Write-Output 'DEVICE_NOT_FOUND'; exit 1 }}
$storage = $device.GetFolder.ParseName('{ss}')
if (-not $storage) {{ Write-Output 'STORAGE_NOT_FOUND'; exit 1 }}
$root = $storage.GetFolder.ParseName('{rf}')
if (-not $root)    {{ Write-Output 'ROOT_NOT_FOUND'; exit 1 }}
$canDel = [bool]([MtpOps] -as [type])
$groups = Get-Content "{jpath}" -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Output 'ENUM_START'
$musicItems = @{{}}
$root.GetFolder.Items() | ForEach-Object {{ $musicItems[$_.Name] = $_ }}
Write-Output "ENUM_DONE:$($musicItems.Count)"
foreach ($grp in $groups) {{
    Write-Output "GRP_START:$($grp.gi)"
    $parts = @($grp.parts)
    $cur = $root
    $ok  = $true
    if ($parts.Count -ge 1) {{
        $cur = $musicItems[$parts[0]]
        if (-not $cur) {{ Write-Output "ALBUM_MISS:$($grp.gi):$($parts[0])"; $ok = $false }}
    }}
    if ($ok -and $parts.Count -ge 2) {{
        for ($pi = 1; $pi -lt $parts.Count; $pi++) {{
            $nav = $cur.GetFolder.ParseName($parts[$pi])
            if ($nav) {{ $cur = $nav }}
            else {{ Write-Output "ALBUM_MISS:$($grp.gi):$($parts[$pi])"; $ok = $false; break }}
        }}
    }}
    if (-not $ok) {{
        foreach ($f in $grp.files) {{ Write-Output "NF:$($f.tok)" }}
        continue
    }}
    foreach ($f in $grp.files) {{
        $t = $cur.GetFolder.ParseName($f.name)
        if (-not $t) {{ $t = $cur.GetFolder.ParseName($f.stem) }}
        if ($t) {{
            if ($canDel -and [MtpOps]::DeleteItem($t)) {{ Write-Output "OK:$($f.tok)" }}
            else {{ Write-Output "ERR:$($f.tok)" }}
        }} else {{ Write-Output "NF:$($f.tok)" }}
    }}
}}
"""

        fd_p, ps1 = _tf5.mkstemp(suffix=".ps1", prefix="wman_bulk_")
        _os5.close(fd_p)
        with open(ps1, "w", encoding="utf-8") as fh:
            fh.write(script)

        ps_exe = _find_ps_exe()
        if ps_exe is None:
            return {}

        results: dict = {}
        done = 0
        _album_miss_count = [0]   # track for first-N INFO logging

        # MTP Shell.Application enumeration requires STA threading for its message
        # pump.  powershell.exe (PS 5.1) supports -Sta; pwsh (PS 7) does not.
        # Use powershell.exe -Sta when available, fall back to whatever was found.
        import shutil as _shutil5
        _ps_sta = _shutil5.which("powershell.exe") or _shutil5.which("powershell")
        if _ps_sta and "pwsh" not in _ps_sta.lower():
            _ps_args = [_ps_sta, "-Sta", "-NonInteractive", "-NoProfile",
                        "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", ps1]
            log.debug(f"[SinglePS] Using {_ps_sta} -Sta for STA/MTP threading")
        else:
            _ps_args = [ps_exe, "-NonInteractive", "-NoProfile",
                        "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", ps1]

        proc = _sp5.Popen(
            _ps_args,
            stdout=_sp5.PIPE, stderr=_sp5.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )

        # Watchdog: kill process if no stdout line arrives for MAX_QUIET_S, OR
        # if the whole run exceeds timeout_s (computed above but previously
        # never actually enforced anywhere — dead code). quiet-only detection
        # has a confirmed blind spot (2026-07-17, wpd_transfer.py's WPD scan
        # path): a degraded MTP session can keep producing output/heartbeats
        # fast enough to never look "quiet" while never actually finishing —
        # a live-lock, not a dead hang. timeout_s is an independent wall-clock
        # cap that catches that case too.
        import threading as _thr5, time as _t5
        MAX_QUIET_S = 300  # 5 min silence = device hung (file-level ParseName is slow on full SD card)
        _last_line_t = [_t5.time()]
        _started_t   = _t5.time()
        _hung = [False]
        _last_gi = [-1]      # last GRP_START index seen — identifies hung group
        _enum_done = [False]  # True once Music folder enumeration completes
        def _watchdog():
            while proc.poll() is None:
                now     = _t5.time()
                quiet   = now - _last_line_t[0]
                elapsed = now - _started_t
                # 300s during initial Music folder Items() enumeration (~1754 entries);
                # tight MAX_QUIET_S per album after enumeration completes.
                limit = MAX_QUIET_S if _enum_done[0] else 300
                reason = None
                if quiet > limit:
                    phase = "album" if _enum_done[0] else "Music folder enumeration"
                    reason = f"No output for {limit}s ({phase}) — device hung"
                elif elapsed > timeout_s:
                    reason = (f"Run exceeded {timeout_s}s ({timeout_s/3600:.1f}h) despite ongoing "
                              f"output — likely a live-lock, not a dead hang")
                if reason:
                    log.error(f"[SinglePS] {reason} — killing process")
                    _hung[0] = True
                    # Note: Python's Popen.terminate() and .kill() are identical on
                    # Windows (both call TerminateProcess) — there is no SIGTERM to
                    # catch, so the PS process gets no chance to run its own cleanup
                    # either way. `taskkill` without /F at least tries a console
                    # close-event first, which is marginally more likely to let the
                    # CLR release COM refs than an immediate hard kill; fall back to
                    # kill() if that doesn't land within a few seconds.
                    try:
                        _sp5.run(
                            ["taskkill", "/PID", str(proc.pid)],
                            capture_output=True, timeout=5,
                        )
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    if proc.poll() is None:
                        proc.kill()
                    break
                _t5.sleep(10)
        _wd = _thr5.Thread(target=_watchdog, daemon=True)
        _wd.start()

        for line in proc.stdout:
            _last_line_t[0] = _t5.time()
            line = line.strip()
            if line.startswith("OK:"):
                tok = line[3:]
                if tok in token_map:
                    key = token_map[tok][0] + (token_map[tok][1],)
                    results[key] = "deleted"
                done += 1
                if done % 50 == 0:
                    log.info(f"[SinglePS] {done}/{n_files} files processed")
                if progress_cb:
                    progress_cb(done, n_files, tok, "deleted")
            elif line.startswith("NF:"):
                tok = line[3:]
                if tok in token_map:
                    key = token_map[tok][0] + (token_map[tok][1],)
                    results[key] = "not_found"
                done += 1
            elif line.startswith("ERR:"):
                tok = line[4:]
                if tok in token_map:
                    key = token_map[tok][0] + (token_map[tok][1],)
                    results[key] = "error"
                done += 1
                log.debug(f"[SinglePS] IFileOperation delete failed: {tok}")
            elif line.startswith("ADDTYPE:"):
                status = line[8:]
                if status == "OK":
                    log.info("[SinglePS] IFileOperation C# type compiled OK")
                elif status.startswith("FAIL:"):
                    log.warning(f"[SinglePS] Add-Type compile error: {status[5:]}")
                else:
                    log.warning(f"[SinglePS] Add-Type: {status}")
            elif line == "ENUM_START":
                log.info("[SinglePS] Enumerating Music folder into cache (may take 1-2 min)...")
            elif line.startswith("ENUM_DONE:"):
                _enum_done[0] = True
                log.info(f"[SinglePS] Music folder cached: {line[10:]} albums — starting deletions")
            elif line.startswith("GRP_START:"):
                try:
                    _last_gi[0] = int(line[10:])
                except ValueError:
                    pass
            elif line.startswith("ALBUM_MISS:"):
                _album_miss_count[0] += 1
                if _album_miss_count[0] <= 10:
                    log.info(f"[SinglePS] {line}")
                else:
                    log.debug(f"[SinglePS] {line}")
            elif line in ("DEVICE_NOT_FOUND", "STORAGE_NOT_FOUND", "ROOT_NOT_FOUND"):
                # "nav_failed", NOT "not_found": this means we never got to check
                # any individual file (root-level navigation itself failed, e.g.
                # a degraded MTP session) — confirmed live 2026-07-17 that using
                # "not_found" here caused the caller to mark all of a batch as
                # safely done, silently skipping real work on every future retry.
                log.warning(f"[SinglePS] Navigation failed: {line} — batch not attempted, needs retry")
                proc.kill()
                return {(v[0] + (v[1],)): "nav_failed" for v in token_map.values()}
        try:
            proc.wait(timeout=30)
        except _sp5.TimeoutExpired:
            proc.kill()
        if _hung[0]:
            log.warning(f"[SinglePS] Completed {done}/{n_files} — hung at group {_last_gi[0]}")
            if _last_gi[0] >= 0:
                results[("__HUNG_GI__",)] = _last_gi[0]
        return results
    except Exception as e:
        log.error(f"[SinglePS] Error: {e}")
        return {}
    finally:
        for _tmp_path in (ps1, json_f):
            if _tmp_path:
                try: _os5.unlink(_tmp_path)
                except Exception: pass


def delete_files_adb_by_path(device_name: str, storage_name: str, root_folder: str,
                              groups: dict, progress_cb=None, batch_size: int = 300) -> dict:
    """
    Direct-path delete via ADB shell — bypasses MTP/Shell.Application folder
    enumeration entirely.

    NOTE: there is a DIFFERENT, pre-existing, uncalled `delete_files_adb()`
    function later in this file (candidates+snapshot-based, keyed by absolute
    path) — confirmed via grep it has zero callers anywhere in the codebase,
    same "built but never wired up" pattern this project keeps rediscovering
    (Task 1 preflight, Task 5 CLI-dispatch, dialog_watcher's
    _is_transfer_dialog). Not reused here: it needs candidate objects with
    dot-attribute access plus a separate walkman_snapshot.json for path
    resolution (another staleness surface on top of .dedup_log.json's own),
    and returns a differently-shaped {adb_path: status} dict that doesn't
    match delete_files_single_ps's {parts_tuple+(filename,): status}
    contract cli_runner.py's deep-clean loop already parses. This function is
    named distinctly (_by_path suffix) specifically to avoid re-shadowing it.

    2026-07-23: delete_files_single_ps's single `$root.GetFolder.Items()` call
    enumerates the ENTIRE Music folder (thousands of entries) into memory
    before touching any individual file — confirmed live, repeatedly, across
    many sessions, that THIS is exactly what hangs on a near-full/degraded SD
    card (300s quiet-timeout fires on this call specifically). But deep-clean
    already knows every candidate's exact relative path from .dedup_log.json
    — there is nothing to search for, so no enumeration is needed at all.
    ADB shell commands operate directly on the real Android filesystem via
    adbd, a completely separate stack from MTP/WPD/Shell.Application's
    enumeration cache — confirmed live the same night that a real candidate's
    album folder lists instantly via `adb shell ls` while the equivalent COM
    enumeration was hung. Reuses the single persistent `adb shell sh` pattern
    already proven in adb_move_and_delete() below (one open session, rm
    commands fed via stdin) rather than spawning `adb shell` per file.

    device_name is accepted (unused) only to keep this call-compatible with
    delete_files_single_ps at the cli_runner.py call site.

    Returns {parts_tuple + (filename,): "deleted"|"not_found"|"error"} — same
    key/value contract as delete_files_single_ps (storage isn't part of the
    key; the caller already has storage_name from its own loop scope).
    Returns {} if ADB isn't connected or the storage path can't be resolved,
    so the caller can fall back to the PS/MTP path.
    """
    import shlex as _shlex_ad, subprocess as _sp_ad

    try:
        import adb_transfer as _adb_ad
    except ImportError:
        return {}

    if not _adb_ad.device_is_connected(device_name):
        return {}

    try:
        storage_path = _adb_ad._get_storage_path(storage_name)
    except Exception as e:
        log.warning(f"[ADB-Delete] Could not resolve storage path for {storage_name!r}: {e}")
        return {}

    items = []
    for parts_tuple, fnames in groups.items():
        rel = "/".join(parts_tuple)
        for fn in fnames:
            items.append((parts_tuple, fn, f"{storage_path}/{rel}/{fn}"))

    n_total = len(items)
    if n_total == 0:
        return {}
    log.info(f"[ADB-Delete] {n_total} file(s) via direct ADB path (no folder enumeration needed)")

    status_map = {"D": "deleted", "N": "not_found", "E": "error"}
    results: dict = {}
    done = 0

    for i in range(0, n_total, batch_size):
        batch = items[i:i + batch_size]
        tok_map: dict = {}
        script_lines = []
        for j, (parts_tuple, fn, abs_path) in enumerate(batch):
            tok = f"F{j}"
            tok_map[tok] = (parts_tuple, fn)
            q = _shlex_ad.quote(abs_path)
            script_lines.append(
                f"if [ -e {q} ]; then rm -f {q} && printf 'D:{tok}\\n' || printf 'E:{tok}\\n'; "
                f"else printf 'N:{tok}\\n'; fi"
            )
        script = "\n".join(script_lines) + "\n"

        try:
            proc = _sp_ad.Popen(
                ["adb", "shell", "sh"],
                stdin=_sp_ad.PIPE, stdout=_sp_ad.PIPE, stderr=_sp_ad.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            timeout_s = max(60, len(batch) * 2 + 30)
            stdout, stderr = proc.communicate(input=script, timeout=timeout_s)
        except _sp_ad.TimeoutExpired:
            proc.kill()
            log.warning(
                f"[ADB-Delete] Batch {i}-{i + len(batch)} timed out after {timeout_s}s — "
                f"marking error, will retry next run (not marked done)"
            )
            for parts_tuple, fn in tok_map.values():
                results[parts_tuple + (fn,)] = "error"
            continue
        except Exception as e:
            log.warning(f"[ADB-Delete] Batch {i}-{i + len(batch)} failed: {e}")
            for parts_tuple, fn in tok_map.values():
                results[parts_tuple + (fn,)] = "error"
            continue

        for line in stdout.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            status, tok = line.split(":", 1)
            if tok not in tok_map or status not in status_map:
                continue
            parts_tuple, fn = tok_map[tok]
            results[parts_tuple + (fn,)] = status_map[status]

        # Any token that never got a line back (shell died mid-batch etc.) —
        # mark error explicitly rather than silently omitting it, so the
        # caller doesn't mistake "no result" for "safely done."
        for parts_tuple, fn in tok_map.values():
            key = parts_tuple + (fn,)
            if key not in results:
                results[key] = "error"

        done += len(batch)
        if progress_cb:
            progress_cb(done, n_total, "", "batch")
        log.info(f"[ADB-Delete] {done}/{n_total} processed")

    return results


# ---------------------------------------------------------------------------
# ADB move-then-delete: pull to PC, rm from device
# ---------------------------------------------------------------------------

def adb_move_and_delete(
    delete_list:   list,          # [{_data, reason, ext, storage, ...}]
    local_dest:    str,           # destination folder on PC
    device_serial: str  = None,
    dry_run:       bool = False,
) -> dict:
    """
    For each file in delete_list:
      1. adb pull → local_dest/<SD card|Internal>/<rel_path>
      2. Verify local file exists
      3. adb shell rm from device

    Returns {_data_path: "moved" | "pull_failed" | "rm_failed" | "dry_run"}.
    Stops early and returns partial results if ADB is unavailable.
    """
    import re as _re2, shlex as _sh2, subprocess as _sp6, os as _os6
    from pathlib import Path as _P2

    adb = ["adb"] + (["-s", device_serial] if device_serial else [])

    import time as _time6

    # Open a keepalive shell immediately — a persistent open session prevents the
    # ZX500 from dropping the USB connection between individual adb commands.
    _keepalive = None
    try:
        _keepalive = _sp6.Popen(
            adb + ["shell", "sleep 7200"],
            stdin=_sp6.DEVNULL, stdout=_sp6.DEVNULL, stderr=_sp6.DEVNULL,
        )
        _time6.sleep(1)  # give the session a moment to establish
    except FileNotFoundError:
        log.warning("[ADB Move] adb not in PATH")
        return {}
    except Exception as _ka_err:
        log.warning(f"[ADB Move] Keepalive failed to start: {_ka_err}")

    # Now verify the device is reachable
    _adb_ready = False
    for _attempt in range(15):
        try:
            _chk = _sp6.run(adb + ["get-state"], capture_output=True, text=True, timeout=5)
            if "device" in _chk.stdout:
                _adb_ready = True
                break
        except Exception:
            pass
        if _attempt == 0:
            log.info("[ADB Move] Waiting for device...")
        _time6.sleep(1)
    if not _adb_ready:
        if _keepalive:
            _keepalive.terminate()
        log.warning("[ADB Move] Device not ready after 15 s — is USB debugging on and device unlocked?")
        return {}
    log.info(f"[ADB Move] Device ready (keepalive active)")

    dest = _P2(local_dest)
    dest.mkdir(parents=True, exist_ok=True)

    pull_results: dict = {}
    pull_supported = True   # flipped to False if probe fails

    n = len(delete_list)
    log.info(f"[ADB Move] {'DRY-RUN: ' if dry_run else ''}{n} files → {dest}")

    if dry_run:
        for i, item in enumerate(delete_list, 1):
            path = item.get("_data", "")
            pull_results[path] = "dry_run"
        return pull_results

    # ---- Probe: test adb pull on the first accessible file -----------------
    for _probe_item in delete_list:
        _probe_path = _probe_item.get("_data", "")
        if not _probe_path:
            continue
        _pm = _re2.match(r"^/storage/(emulated/\d+|[^/]+)/(.+)$", _probe_path)
        if not _pm:
            continue
        _probe_label   = "Internal" if "emulated" in _pm.group(1) else "SD card"
        _probe_local   = dest / _probe_label / _pm.group(2)
        _probe_local.parent.mkdir(parents=True, exist_ok=True)
        _p = _sp6.run(
            adb + ["pull", _probe_path, str(_probe_local)],
            capture_output=True, text=True, timeout=30,
        )
        if _p.returncode == 0 and _probe_local.exists():
            log.info(f"[ADB Move] Pull probe OK — will copy {n} files to PC before deleting")
            pull_results[_probe_path] = "pulled"
        else:
            err = (_p.stderr or _p.stdout or "").strip()
            log.warning(
                f"[ADB Move] Pull probe FAILED ({err!r}) — "
                f"falling back to delete-only (no PC copy)"
            )
            pull_supported = False
        break
    # -------------------------------------------------------------------------

    mode_label = "moved" if pull_supported else "deleted"

    # Collect valid paths to delete; handle pull for pull_supported mode
    rm_paths: list = []
    for i, item in enumerate(delete_list, 1):
        path = item.get("_data", "")
        if not path:
            continue
        if path in pull_results:      # probe already handled this one
            rm_paths.append(path)
            continue

        m = _re2.match(r"^/storage/(emulated/\d+|[^/]+)/(.+)$", path)
        if not m:
            pull_results[path] = "pull_failed"
            continue

        if pull_supported:
            label      = "Internal" if "emulated" in m.group(1) else "SD card"
            local_path = dest / label / m.group(2)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            pull = _sp6.run(
                adb + ["pull", path, str(local_path)],
                capture_output=True, text=True, timeout=60,
            )
            if pull.returncode != 0 or not local_path.exists():
                pull_results[path] = "pull_failed"
                continue
            pull_results[path] = "pulled"

        rm_paths.append(path)

    # ---- Path-existence probe: verify first 5 paths before running rm ------
    sample = rm_paths[:5]
    probe_cmd = "; ".join(
        f"[ -e {_sh2.quote(p)} ] && printf 'EXISTS:%s\\n' {_sh2.quote(p)} || printf 'MISSING:%s\\n' {_sh2.quote(p)}"
        for p in sample
    )
    probe_r = _sp6.run(adb + ["shell", probe_cmd], capture_output=True, text=True, timeout=30)
    n_exist = n_missing = 0
    for line in probe_r.stdout.splitlines():
        if line.startswith("EXISTS:"):  n_exist  += 1
        elif line.startswith("MISSING:"): n_missing += 1; log.warning(f"[ADB Move] MISSING on device: {line[8:]}")
    if n_exist == 0 and n_missing == 0:
        log.error(f"[ADB Move] Path probe got no output — ADB offline or shell error: {probe_r.stderr.strip()!r}")
        return pull_results
    log.info(f"[ADB Move] Path probe: {n_exist} exist, {n_missing} missing (of {len(sample)} sampled)")
    if n_exist == 0:
        log.error("[ADB Move] ALL sampled paths are MISSING — snapshot is stale, re-run walkman_tree_gen.py first")
        return pull_results

    # ---- Single persistent shell via communicate() --------------------------
    # Use "adb shell sh" so sh reads the script from its stdin.
    # communicate() uses OS threads internally so it handles large stdin
    # without the EPIPE/broken-pipe that raw write()+close() caused.
    # One open connection = device stays connected throughout the run.
    total_rm = len(rm_paths)
    rm_script_lines = []
    for p in rm_paths:
        qp = _sh2.quote(p)
        rm_script_lines.append(
            f"rm {qp} && printf 'RM_OK:%s\\n' {qp} || printf 'RM_ERR:%s\\n' {qp}"
        )
    rm_script = "\n".join(rm_script_lines) + "\n"

    rm_timeout = max(600, total_rm * 2 + 120)
    log.info(f"[ADB Move] Sending {total_rm} rm commands via persistent shell [{mode_label} mode, timeout={rm_timeout}s]")
    n_ok = n_err = 0
    try:
        proc = _sp6.Popen(
            adb + ["shell", "sh"],
            stdin=_sp6.PIPE, stdout=_sp6.PIPE, stderr=_sp6.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        stdout, stderr = proc.communicate(input=rm_script, timeout=rm_timeout)
        if stderr.strip() and not stdout.strip():
            log.error(f"[ADB Move] Shell error: {stderr.strip()[:200]}")
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("RM_OK:"):
                pull_results[line[6:]] = mode_label
                n_ok += 1
                if n_ok == 1 or n_ok % 500 == 0:
                    log.info(f"[ADB Move] {n_ok} {mode_label} so far...")
            elif line.startswith("RM_ERR:"):
                p = line[7:]
                if pull_results.get(p) != "pull_failed":
                    pull_results[p] = "rm_failed"
                n_err += 1
                if n_err <= 5:
                    log.warning(f"[ADB Move] RM_ERR: ...{p[-70:]}")
    except _sp6.TimeoutExpired:
        proc.kill()
        log.error(f"[ADB Move] Timed out after {rm_timeout}s — partial results may be saved")
    except Exception as e:
        log.error(f"[ADB Move] Shell failed: {e}")

    log.info(f"[ADB Move] FINAL — {mode_label}={n_ok}  file_missing={n_err}  (of {total_rm})")

    if _keepalive:
        _keepalive.terminate()

    n_done = sum(1 for v in pull_results.values() if v in ("moved", "deleted"))
    n_fail = sum(1 for v in pull_results.values() if "fail" in v)
    log.info(f"[ADB Move] Done — {mode_label}={n_done} failed={n_fail} (of {n})")
    return pull_results


# ---------------------------------------------------------------------------
# ADB bulk delete — bypasses MTP entirely
# ---------------------------------------------------------------------------
# MTP InvokeVerb('delete') on a near-full SD card: ~100s/file (device-side
# DB update + flash GC). ADB shell rm: ~0.1s/file (direct filesystem, no DB
# update until next device scan). 3500 files: days via MTP, ~5 min via ADB.

def delete_files_adb(candidates: list, snap_path, device_serial: str = None) -> "dict | None":
    """
    Hard-delete plan candidates via `adb shell rm`.

    Returns None if ADB is unavailable (caller should fall back to MTP).
    Returns {adb_path: "deleted" | "not_found" | "error"} on success.

    After deletion, the device MediaStore is stale until the device
    re-scans (happens automatically on USB disconnect or next boot).
    """
    import json as _j, re as _re, posixpath as _pp
    import shlex as _sh, subprocess as _sp4, os as _os4, tempfile as _tf3
    from pathlib import Path as _P

    # -- Build snapshot path index: (storage, parts_tuple, filename) -> _data ----
    try:
        rows = _j.loads(_P(snap_path).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"[ADB Delete] Cannot load snapshot: {e}")
        return None

    snap_index: dict = {}
    _STOR_MAP = {"sd": "SD card", "internal": "Internal shared storage"}
    for r in rows:
        d = r.get("_data") or ""
        if not d:
            continue
        fn  = _pp.basename(d)
        m   = _re.match(r"^/storage/(?:emulated/\d+|[^/]+)/(.+)", d)
        if not m:
            continue
        parts = tuple(m.group(1).split("/")[:-1])
        stor  = _STOR_MAP.get(r.get("storage", "sd"), "SD card")
        snap_index[(stor, parts, fn)] = d

    # -- Match candidates to absolute paths ------------------------------------
    to_delete: dict = {}   # adb_path -> candidate
    unmatched: list = []
    for c in candidates:
        key = (c.storage, tuple(c.parts), c.filename)
        data_path = snap_index.get(key)
        if data_path:
            to_delete[data_path] = c
        else:
            unmatched.append(c)

    if unmatched:
        log.warning(f"[ADB Delete] {len(unmatched)} candidates not in snapshot — will skip")
    log.info(f"[ADB Delete] {len(to_delete)} files mapped to ADB paths")

    # -- Verify ADB connectivity -----------------------------------------------
    adb = ["adb"] + (["-s", device_serial] if device_serial else [])
    try:
        check = _sp4.run(adb + ["get-state"], capture_output=True, text=True, timeout=10)
        if check.returncode != 0 or "device" not in check.stdout:
            log.warning(f"[ADB Delete] ADB device not ready: {check.stdout.strip()} {check.stderr.strip()}")
            return None
    except FileNotFoundError:
        log.warning("[ADB Delete] `adb` not found in PATH — falling back to MTP")
        return None
    except Exception as e:
        log.warning(f"[ADB Delete] ADB check failed: {e}")
        return None

    # -- Write shell script and push to device --------------------------------
    script_remote = "/sdcard/_mk11_deepclean.sh"
    lines = ["#!/system/bin/sh"]
    for path in to_delete:
        q = _sh.quote(path)
        lines.append(
            f"if [ -f {q} ]; then rm -f {q} && echo 'DELETED:{path}' || echo 'ERROR:{path}'; "
            f"else echo 'NOT_FOUND:{path}'; fi"
        )
    script = "\n".join(lines) + "\n"

    tmpf = None
    try:
        fd, tmpf = _tf3.mkstemp(suffix=".sh", prefix="mk11_adb_")
        _os4.close(fd)
        with open(tmpf, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)

        push = _sp4.run(adb + ["push", tmpf, script_remote],
                        capture_output=True, text=True, timeout=30)
        if push.returncode != 0:
            log.error(f"[ADB Delete] Push failed: {push.stderr.strip()}")
            return None

        # Stream stdout line-by-line for live progress
        results = {p: "error" for p in to_delete}
        n = len(to_delete)
        done = 0
        proc = _sp4.Popen(
            adb + ["shell", f"sh {_sh.quote(script_remote)}"],
            stdout=_sp4.PIPE, stderr=_sp4.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("DELETED:"):
                p = line[len("DELETED:"):]
                if p in results:
                    results[p] = "deleted"
                done += 1
                if done % 200 == 0:
                    log.info(f"[ADB Delete] {done}/{n} files processed")
            elif line.startswith("NOT_FOUND:"):
                p = line[len("NOT_FOUND:"):]
                if p in results:
                    results[p] = "not_found"
                done += 1
            elif line.startswith("ERROR:"):
                done += 1
        proc.wait(timeout=60)

        _sp4.run(adb + ["shell", f"rm -f {_sh.quote(script_remote)}"],
                 capture_output=True, timeout=10)
        return results

    except Exception as e:
        log.error(f"[ADB Delete] Execution failed: {e}")
        return None
    finally:
        if tmpf:
            try: _os4.unlink(tmpf)
            except Exception: pass


# ---------------------------------------------------------------------------
# Targeted soft-delete purge
# ---------------------------------------------------------------------------
# Uses ParseName() not Items()|Where-Object — one MTP round-trip per level,
# so it works even when SD card is too full/slow for File Explorer to browse.

def purge_soft_deleted_ps(
    device_name:      str,
    storage_name:     str,
    music_folder:     str = "Music",
    soft_delete_name: str = ".deleted",
    timeout_s:        int = 120,
) -> str:
    """
    Delete the soft-delete staging folder from one device storage via PowerShell.
    Uses ParseName() at each level (O(1) per lookup) rather than enumerating
    all items — works on a near-full SD card where Items() would time out.

    Returns one of: "deleted", "already_clean", "not_found", "error".
    """
    sd = device_name.replace("'", "''")
    ss = storage_name.replace("'", "''")
    sm = music_folder.replace("'", "''")
    sc = soft_delete_name.replace("'", "''")

    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$shell   = New-Object -ComObject Shell.Application
# Device — only a handful of items under This PC, so Items() is fast here
$device  = $shell.NameSpace(0x11).Items() | Where-Object {{ $_.Name -eq '{sd}' }} | Select-Object -First 1
if (-not $device) {{ Write-Output "not_found:device"; exit 0 }}
# Storage — 2 items (Internal, SD card) so Items() is fast
$storage = $device.GetFolder.Items() | Where-Object {{ $_.Name -eq '{ss}' }} | Select-Object -First 1
if (-not $storage) {{ Write-Output "not_found:storage"; exit 0 }}
# ParseName: direct O(1) lookup — no full Music/ enumeration
$music  = $storage.GetFolder.ParseName('{sm}')
if (-not $music) {{ Write-Output "not_found:music"; exit 0 }}
$target = $music.GetFolder.ParseName('{sc}')
if (-not $target) {{ Write-Output "already_clean"; exit 0 }}
$target.InvokeVerb('delete')
Start-Sleep -Seconds 2
Write-Output "deleted"
exit 0
"""
    import tempfile as _tf, os as _os2, subprocess as _sp2
    ps1 = None
    try:
        fd, ps1 = _tf.mkstemp(suffix=".ps1", prefix="wman_purge_")
        _os2.close(fd)
        with open(ps1, "w", encoding="utf-8") as fh:
            fh.write(script)
        ps_exe = _find_ps_exe()
        if ps_exe is None:
            return "error"
        r = _sp2.run(
            [ps_exe, "-NonInteractive", "-NoProfile",
             "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True, text=True, timeout=timeout_s,
        )
        out = (r.stdout or "").strip().splitlines()
        result = out[-1] if out else "error"
        log.info(f"  [Purge] {storage_name}/Music/{soft_delete_name}: {result}")
        return result
    except _sp2.TimeoutExpired:
        log.warning(f"  [Purge] Timed out after {timeout_s}s for {storage_name}")
        return "error"
    except Exception as e:
        log.warning(f"  [Purge] Error for {storage_name}: {e}")
        return "error"
    finally:
        if ps1:
            try: _os2.unlink(ps1)
            except Exception: pass


def purge_soft_deleted(
    device_name:      str,
    soft_delete_name: str = ".deleted",
) -> dict:
    """
    Purge the soft-delete folder from all device storages.
    Returns {storage_name: status_string} for each storage attempted.
    """
    import config as _cfg
    storages = getattr(_cfg, "WRITE_STORAGES",
                       ["SD card", "Internal shared storage"])
    results = {}
    for storage in storages:
        results[storage] = purge_soft_deleted_ps(
            device_name, storage, soft_delete_name=soft_delete_name
        )
    return results


# ---------------------------------------------------------------------------
# WPD batch scan — replaces Shell.Application enumeration
# ---------------------------------------------------------------------------
# IPortableDeviceContent::EnumObjects fetches 32 object IDs per round-trip
# instead of one COM call per item.  No Shell STA apartment binding, and
# device.Close() tears down the MTP session cleanly on error/timeout so it
# never leaves orphaned COM apartments that cascade-fail subsequent scans.

_WPD_AUDIO_EXTS = frozenset({".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a"})


def _wpd_find_child(content, props, keys, parent_id: str,
                    target: str, exact: bool = True, progress_cb=None) -> "str | None":
    """Return WPD object ID of first child matching target (case-insensitive).

    Fetches one object ID per Next() call. The comtypes-generated binding for
    IEnumPortableDeviceObjectIDs::Next on this interface only marshals the
    first element of the [out] array param regardless of celt requested (both
    the raw-buffer calling convention and celt>1 via the friendly API silently
    drop every ID after the first, while still reporting the true fetched
    count) — verified live against the device. celt=1 is the only call shape
    that reliably returns every object, at the cost of one MTP round-trip per
    item instead of up to 32.

    progress_cb, if given, is called before each potentially-blocking COM call
    so a caller-side watchdog can detect a hung EnumObjects/Next/GetValues —
    see scan_device_audio_wpd.
    """
    tgt = target.lower()
    if progress_cb:
        progress_cb()
    try:
        enum = content.EnumObjects(0, parent_id, None)
    except Exception:
        return None
    while True:
        if progress_cb:
            progress_cb()
        try:
            oid, fetched = enum.Next(1)
        except Exception:
            break
        if not fetched or not oid:
            break
        try:
            vals = props.GetValues(oid, keys)
            name = _wpd_obj_name(vals)
            if not name:
                continue
            nl = name.lower()
            hit = (nl == tgt) if exact else (tgt in nl)
            if hit:
                return oid
        except Exception:
            continue
    return None


def _wpd_obj_name(vals) -> str:
    """Original filename preferred; fall back to display name."""
    for key in (WPD_OBJECT_ORIGINAL_FILE_NAME, WPD_OBJECT_NAME):
        try:
            n = vals.GetStringValue(_pk(*key))
            if n:
                return n
        except Exception:
            pass
    return ""


def _wpd_is_folder(vals) -> bool:
    try:
        ct = vals.GetGuidValue(_pk(*WPD_OBJECT_CONTENT_TYPE))
        return _comtypes.GUID(WPD_CONTENT_TYPE_FOLDER) == ct
    except Exception:
        return False


def _wpd_walk(content, props, keys, parent_id: str,
              path_parts: list, storage_name: str, results: list, depth: int = 0,
              progress_cb=None) -> None:
    """Recursively enumerate a WPD folder, appending audio dicts to results.

    See _wpd_find_child's docstring — Next(1) per call is the only calling
    shape that reliably returns every child on this device's WPD binding.
    progress_cb is threaded through recursive calls for watchdog heartbeats —
    see scan_device_audio_wpd.
    """
    if progress_cb:
        progress_cb()
    try:
        enum = content.EnumObjects(0, parent_id, None)
    except Exception as e:
        log.debug(f"  [WPD/Scan] EnumObjects failed {path_parts}: {e}")
        return
    while True:
        if progress_cb:
            progress_cb()
        try:
            oid, fetched = enum.Next(1)
        except Exception:
            break
        if not fetched or not oid:
            break
        try:
            vals = props.GetValues(oid, keys)
            name = _wpd_obj_name(vals)
            if not name:
                continue
            if _wpd_is_folder(vals):
                if depth == 1:  # Artist level — show progress
                    log.info(f"  [WPD/Scan] Artist: {name}/ ({len(results)} files so far)")
                _wpd_walk(content, props, keys, oid,
                          path_parts + [name], storage_name, results, depth + 1,
                          progress_cb=progress_cb)
            else:
                ext = pathlib.Path(name).suffix.lower()
                if ext in _WPD_AUDIO_EXTS:
                    results.append({
                        "filename": name,
                        "ext":      ext,
                        "parts":    path_parts + [name],
                        "storage":  storage_name,
                    })
        except Exception:
            continue


_WPD_SCAN_RESULT_MARKER = "__WPD_SCAN_RESULT__:"
_WPD_SCAN_ERROR_MARKER  = "__WPD_SCAN_ERROR__:"
_WPD_SCAN_HEARTBEAT     = "\x00HB\x00"


def _wpd_scan_worker_main(device_name: str, storage_roots: list) -> None:
    """
    Runs in a CHILD PROCESS (see scan_device_audio_wpd, which spawns
    `python wpd_transfer.py --scan-worker ...`). A thread-based watchdog cannot
    reliably guard a blocked WPD/COM call: confirmed live (2026-07-16) — a scan
    hung ~26min with the in-process watchdog's 180s timeout never firing, most
    likely because the blocked call holds the GIL and freezes the watchdog
    thread too. Only a real OS process can be killed regardless of what it's
    stuck on, which is why this now runs out-of-process.

    Prints a heartbeat marker to stderr before every potentially-blocking COM
    call (same call sites the old in-thread watchdog used) so the parent can
    detect a stall; all other stderr lines are normal log output the parent
    relays into its own logger. On success prints the JSON result to stdout
    behind _WPD_SCAN_RESULT_MARKER; on failure, _WPD_SCAN_ERROR_MARKER.
    """
    import json as _json

    def _beat() -> None:
        print(_WPD_SCAN_HEARTBEAT, file=sys.stderr, flush=True)

    if not _ensure_wpd():
        print(f"{_WPD_SCAN_ERROR_MARKER}WPD not available", flush=True)
        return
    try:
        _comtypes.CoInitialize()
    except Exception as e:
        print(f"{_WPD_SCAN_ERROR_MARKER}CoInitialize failed: {e}", flush=True)
        return

    device_id = _get_device_id(device_name)
    _beat()
    if not device_id:
        print(f"{_WPD_SCAN_ERROR_MARKER}Device {device_name!r} not found", flush=True)
        try:
            _comtypes.CoUninitialize()
        except Exception:
            pass
        return

    device = None
    try:
        client_info = _create_values()
        device = _comtypes.client.CreateObject(
            _comtypes.GUID(CLSID_PortableDevice),
            clsctx=_comtypes.CLSCTX_INPROC_SERVER,
            interface=_pdapi.IPortableDevice,
        )
        device.Open(device_id, client_info)
        _beat()

        content = device.Content()
        props   = content.Properties()
        keys = _comtypes.client.CreateObject(
            _comtypes.GUID(CLSID_PortableDeviceKeyCollection),
            clsctx=_comtypes.CLSCTX_INPROC_SERVER,
            interface=_pdapi.IPortableDeviceKeyCollection,
        )
        keys.Add(_pk(*WPD_OBJECT_NAME))
        keys.Add(_pk(*WPD_OBJECT_ORIGINAL_FILE_NAME))
        keys.Add(_pk(*WPD_OBJECT_CONTENT_TYPE))

        results: list     = []
        storage_ids: dict = {}

        for storage_name, subfolder in storage_roots:
            if storage_name not in storage_ids:
                sid = _wpd_find_child(
                    content, props, keys, WPD_DEVICE_OBJECT_ID, storage_name,
                    exact=False, progress_cb=_beat,
                )
                storage_ids[storage_name] = sid
                if not sid:
                    # 2026-07-30: was log.debug — invisible at the normal INFO
                    # level, so a storage_roots entry being silently skipped
                    # (e.g. "SD card" not present, or the lookup itself
                    # failing during a degraded WPD session) produced zero
                    # operator-visible signal. Live-confirmed this exact class
                    # of silence made a scan that skipped 4/6 configured
                    # storage_roots entries look identical to a scan that
                    # genuinely found the folder empty — see progress.md
                    # "Session: 2026-07-30" for the live investigation.
                    log.warning(f"  [WPD/Scan] Storage not found: {storage_name!r} — "
                                "skipping this storage_roots entry")
                    continue
            else:
                sid = storage_ids[storage_name]
                if not sid:
                    continue

            if subfolder:
                start_id   = _wpd_find_child(content, props, keys, sid, subfolder,
                                              progress_cb=_beat)
                path_start = [subfolder]
                if not start_id:
                    log.warning(f"  [WPD/Scan] Subfolder {subfolder!r} not found under "
                                f"{storage_name!r} — skipping this storage_roots entry "
                                "(if this is unexpected, the WPD session may be degraded "
                                "rather than the folder genuinely being absent)")
                    continue
            else:
                start_id   = sid
                path_start = []

            before = len(results)
            _wpd_walk(content, props, keys, start_id, path_start, storage_name,
                      results, progress_cb=_beat)
            log.info(f"  [WPD/Scan] {storage_name}/{subfolder}: {len(results) - before} audio files")

        print(f"{_WPD_SCAN_RESULT_MARKER}{_json.dumps(results)}", flush=True)
    except Exception as e:
        print(f"{_WPD_SCAN_ERROR_MARKER}Scan error: {e}", flush=True)
    finally:
        try:
            if device is not None:
                device.Close()
        except Exception:
            pass
        try:
            _comtypes.CoUninitialize()
        except Exception:
            pass


def scan_device_audio_wpd(device_name: str, storage_roots: list,
                           quiet_timeout_s: float = 180.0,
                           max_duration_s: float = 600.0) -> "list | None":
    """
    WPD batch enumeration — fast replacement for Shell.Application scan.

    Returns None  if WPD is unavailable (caller should fall back to Shell.Application).
    Returns list  on success (may be empty if device not found or Music is empty).

    Runs the ENTIRE scan in a CHILD PROCESS (`python wpd_transfer.py --scan-worker
    ...`), supervised here with two independent guards. A blocked WPD/COM call
    (GetDevices, GetDeviceFriendlyName, device.Open, EnumObjects, Next,
    GetValues) cannot be cancelled from Python once stuck — this used to run in
    a background THREAD with a quiet-timeout, but that watchdog was confirmed
    live (2026-07-16) to not fire even after ~26 minutes stuck, most likely
    because the blocked call holds the GIL and freezes the watchdog thread too.
    A real process, unlike a thread, can be killed regardless of what it's stuck
    on — that's the whole reason for the subprocess. Same flash-wear-pressure
    hang documented for the Shell.Application deletion path
    (delete_files_single_ps); ~0% CPU while stuck is expected (blocked on
    device I/O, not computing) and is not fixable by more host processing
    power. If no heartbeat arrives for quiet_timeout_s, the subprocess is
    killed and we return None (the caller's documented "WPD unavailable"
    signal, which falls back to Shell.Application) instead of hanging forever.

    quiet_timeout_s alone is not enough: confirmed live (2026-07-17) a SEPARATE
    failure mode where a degraded MTP session keeps a low-level enumeration
    call returning fast (steady heartbeats every few seconds — the watchdog
    never sees silence) without ever completing the folder traversal — a
    live-lock, not a dead hang. max_duration_s is an independent wall-clock
    cap on the whole scan, regardless of heartbeat activity, to catch that.
    """
    if not _ensure_wpd():
        return None

    import json as _json, threading as _thr_wpd, time as _t_wpd

    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--scan-worker", device_name,
             _json.dumps(list(storage_roots))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.warning(f"  [WPD/Scan] Could not start scan subprocess: {e}")
        return None

    started    = _t_wpd.time()
    last_beat  = [started]
    stdout_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            last_beat[0] = _t_wpd.time()
            line = line.rstrip("\n")
            if line and line != _WPD_SCAN_HEARTBEAT:
                log.info(line)

    def _drain_stdout() -> None:
        for line in proc.stdout:
            last_beat[0] = _t_wpd.time()
            stdout_chunks.append(line)

    t_err = _thr_wpd.Thread(target=_drain_stderr, daemon=True)
    t_out = _thr_wpd.Thread(target=_drain_stdout, daemon=True)
    t_err.start()
    t_out.start()

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            now     = _t_wpd.time()
            quiet   = now - last_beat[0]
            elapsed = now - started
            reason  = None
            if quiet > quiet_timeout_s:
                reason = f"No progress for {quiet:.0f}s (limit {quiet_timeout_s:.0f}s)"
            elif elapsed > max_duration_s:
                reason = (f"Scan still running after {elapsed:.0f}s (limit {max_duration_s:.0f}s) "
                          f"despite ongoing heartbeats — likely a live-lock (a degraded MTP session "
                          f"can keep a low-level call returning fast without ever completing the "
                          f"folder traversal), not a dead hang")
            if reason:
                log.warning(
                    f"  [WPD/Scan] {reason} — device likely hung/looping on a WPD call. Killing "
                    f"scan subprocess and falling back to Shell.Application. (a real process kill "
                    f"reliably terminates a blocked COM call; an in-process watchdog thread "
                    f"previously could not)"
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                return None

    t_err.join(timeout=5)
    t_out.join(timeout=5)

    output = "".join(stdout_chunks)
    if _WPD_SCAN_RESULT_MARKER in output:
        try:
            payload = output.split(_WPD_SCAN_RESULT_MARKER, 1)[1].strip()
            return _json.loads(payload)
        except Exception as e:
            log.warning(f"  [WPD/Scan] Could not parse scan subprocess result: {e}")
            return None
    if _WPD_SCAN_ERROR_MARKER in output:
        err = output.split(_WPD_SCAN_ERROR_MARKER, 1)[1].strip()
        log.debug(f"  [WPD/Scan] {err}")
        return None
    log.debug(f"  [WPD/Scan] Scan subprocess exited (code={proc.returncode}) with no result")
    return None


def delete_files_wpd(
    device_name:  str,
    storage_name: str,
    root_folder:  str,   # "Music"
    groups:       dict,  # {tuple(parts): [filenames]} — same as delete_files_single_ps
) -> "dict | None":
    """
    Delete files via WPD IPortableDeviceContent::Delete().
    Direct USB command — no Shell.Application, no confirmation dialog possible.

    Returns None  if WPD is unavailable (caller should fall back to PS).
    Returns dict  {(parts_tuple + (filename,)): "deleted"|"not_found"|"error"} on success.
    """
    if not _ensure_wpd():
        log.warning("[WPD-Del] comtypes/WPD not available")
        return None

    device_id = _get_device_id(device_name)
    if not device_id:
        log.warning(f"[WPD-Del] Device {device_name!r} not found")
        return None

    try:
        client_info = _create_values()
        device = _comtypes.client.CreateObject(
            _comtypes.GUID(CLSID_PortableDevice),
            clsctx=_comtypes.CLSCTX_INPROC_SERVER,
            interface=_pdapi.IPortableDevice,
        )
        device.Open(device_id, client_info)
    except Exception as e:
        log.warning(f"[WPD-Del] Could not open device: {e}")
        return None

    # PROPVARIANT layout for VT_LPWSTR (object ID strings).
    # 2-byte vt + 6 bytes reserved + pointer-width union = 16 bytes on 64-bit.
    class _PropVar(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("pwszVal", ctypes.c_wchar_p)]
        _fields_ = [
            ("vt",         ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("_u",         _U),
        ]

    try:
        content = device.Content()
        props   = content.Properties()
        keys = _comtypes.client.CreateObject(
            _comtypes.GUID(CLSID_PortableDeviceKeyCollection),
            clsctx=_comtypes.CLSCTX_INPROC_SERVER,
            interface=_pdapi.IPortableDeviceKeyCollection,
        )
        keys.Add(_pk(*WPD_OBJECT_NAME))
        keys.Add(_pk(*WPD_OBJECT_ORIGINAL_FILE_NAME))
        keys.Add(_pk(*WPD_OBJECT_CONTENT_TYPE))

        storage_oid = _wpd_find_child(content, props, keys, "DEVICE", storage_name)
        if not storage_oid:
            log.warning(f"[WPD-Del] Storage {storage_name!r} not found")
            return None
        root_oid = _wpd_find_child(content, props, keys, storage_oid, root_folder)
        if not root_oid:
            log.warning(f"[WPD-Del] {root_folder!r} not found in {storage_name!r}")
            return None

        results: dict = {}
        done = 0
        n_files = sum(len(v) for v in groups.values())

        for parts_tuple, fnames in groups.items():
            folder_oid = root_oid
            nav_ok = True
            for part in parts_tuple[1:]:   # parts_tuple[0] == root_folder, already there
                child = _wpd_find_child(content, props, keys, folder_oid, part)
                if not child:
                    nav_ok = False
                    log.debug(f"[WPD-Del] Folder not found: {'/'.join(str(p) for p in parts_tuple)}")
                    break
                folder_oid = child

            for fn in fnames:
                key = parts_tuple + (fn,)
                if not nav_ok:
                    results[key] = "not_found"
                    done += 1
                    continue

                file_oid = _wpd_find_child(content, props, keys, folder_oid, fn)
                if not file_oid and "." in fn:
                    file_oid = _wpd_find_child(content, props, keys, folder_oid, fn.rsplit(".", 1)[0])
                if not file_oid:
                    results[key] = "not_found"
                    done += 1
                    continue

                try:
                    coll = _comtypes.client.CreateObject(
                        _comtypes.GUID(CLSID_PortableDevicePropVariantCollection),
                        clsctx=_comtypes.CLSCTX_INPROC_SERVER,
                        interface=_pdapi.IPortableDevicePropVariantCollection,
                    )
                    pv = _PropVar()
                    pv.vt = _VT_LPWSTR
                    pv._u.pwszVal = file_oid
                    coll.Add(ctypes.byref(pv))
                    content.Delete(0, coll, None)
                    results[key] = "deleted"
                except Exception as e:
                    log.debug(f"[WPD-Del] Delete {fn!r}: {e}")
                    results[key] = "error"

                done += 1
                if done % 50 == 0:
                    log.info(f"[WPD-Del] {done}/{n_files} files processed")

        log.info(f"[WPD-Del] Complete — {done}/{n_files} processed")
        return results

    except Exception as e:
        log.warning(f"[WPD-Del] Fatal: {e}")
        return None
    finally:
        try:
            device.Close()
        except Exception:
            pass


if __name__ == "__main__":
    # Child-process entry point for scan_device_audio_wpd()'s subprocess
    # isolation — see _wpd_scan_worker_main's docstring for why this runs
    # out-of-process instead of in a thread.
    if len(sys.argv) >= 4 and sys.argv[1] == "--scan-worker":
        import json as _json_main
        logging.basicConfig(
            level=logging.INFO, format="%(message)s",
            handlers=[logging.StreamHandler(sys.stderr)], force=True,
        )
        _wpd_scan_worker_main(sys.argv[2], [tuple(x) for x in _json_main.loads(sys.argv[3])])