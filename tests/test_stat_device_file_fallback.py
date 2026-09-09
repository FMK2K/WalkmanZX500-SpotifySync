"""
test_stat_device_file_fallback.py — isolated verification for
walkman_dedup._stat_device_file()'s COM fallback and the loud warning added
to _pass_zero_length(), 2026-07-26.

Previously _stat_device_file was ADB-only and silently returned -1 whenever
ADB wasn't connected; _pass_zero_length treated -1 as "skip, can't tell,"
so an entire pass1 run could silently report "0 candidates" (indistinguishable
from a genuinely clean library) whenever ADB happened to be flaky. Confirmed
live: an entire 16-track album of genuinely 0-byte corrupt files went
completely undetected this way. No device required — adb_transfer and
mtp_transfer_com are mocked.
"""
from unittest import mock

import walkman_dedup as wd

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


print("[1] ADB healthy and returns a real size -> used directly, COM never touched")
with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/emulated/0"), \
     mock.patch("adb_transfer._adb_cmd", return_value="4096000") as mock_cmd, \
     mock.patch("mtp_transfer_com._get_storage_root") as mock_com:
    size = wd._stat_device_file("Internal shared storage", ["Music", "A"], "x.mp3")
    _check("returns the real ADB-reported size", size == 4096000)
    _check("COM never called when ADB succeeds", not mock_com.called)

print("\n[1b] a path containing spaces/parens is single-quoted, not passed as bare argv "
      "(2026-07-27 fix — adb shell word-splits unquoted paths, see _shell_quote)")
with mock.patch("adb_transfer.device_is_connected", return_value=True), \
     mock.patch("adb_transfer._get_storage_path", return_value="/storage/emulated/0"), \
     mock.patch("adb_transfer._adb_cmd", return_value="40960") as mock_cmd:
    size = wd._stat_device_file(
        "Internal shared storage", ["Music", "Surf"],
        "Nico Segal - Just Wait (SPOTISAVER).mp3",
    )
    _check("returns the real size instead of a word-split garbage result", size == 40960)
    called_args = mock_cmd.call_args[0][0]
    _check("routed through sh -c (not bare argv after 'shell')",
           called_args[:3] == ["shell", "sh", "-c"])
    shell_str = called_args[3]
    _check("the full path is wrapped in single quotes",
           "'/storage/emulated/0/Music/Surf/Nico Segal - Just Wait (SPOTISAVER).mp3'" in shell_str)

print("\n[2] ADB down -> falls back to COM, which reports a genuine 0-byte file")
with mock.patch("adb_transfer.device_is_connected", return_value=False), \
     mock.patch("mtp_transfer.COM_AVAILABLE", True), \
     mock.patch("mtp_transfer_com._get_storage_root", return_value="ROOT"), \
     mock.patch("mtp_transfer_com._navigate", return_value="FOLDER"), \
     mock.patch("mtp_transfer_com._find_remote_item", return_value=mock.Mock(Size=0)):
    size = wd._stat_device_file("Internal shared storage", ["Music", "Surf"], "corrupt.mp3")
    _check("COM fallback correctly reports 0 for a genuinely corrupt file", size == 0)

print("\n[3] ADB down -> COM fallback reports a genuine healthy file's real size")
with mock.patch("adb_transfer.device_is_connected", return_value=False), \
     mock.patch("mtp_transfer.COM_AVAILABLE", True), \
     mock.patch("mtp_transfer_com._get_storage_root", return_value="ROOT"), \
     mock.patch("mtp_transfer_com._navigate", return_value="FOLDER"), \
     mock.patch("mtp_transfer_com._find_remote_item", return_value=mock.Mock(Size=8_500_000)):
    size = wd._stat_device_file("Internal shared storage", ["Music", "A"], "healthy.mp3")
    _check("COM fallback correctly reports a real, non-zero size", size == 8_500_000)

print("\n[4] both ADB and COM fail -> returns -1, does not raise")
with mock.patch("adb_transfer.device_is_connected", return_value=False), \
     mock.patch("mtp_transfer.COM_AVAILABLE", False):
    try:
        size = wd._stat_device_file("Internal shared storage", ["Music", "A"], "x.mp3")
        _check("returns -1 when both transports are unavailable", size == -1)
    except Exception as e:
        _check(f"returns -1 without raising (raised {e!r} instead)", False)

print("\n[5] _pass_zero_length warns loudly when most lookups are unknown (-1)")
files = [
    {"storage": "Internal shared storage", "parts": ["Music", "A"], "filename": f"t{i}.mp3", "ext": ".mp3"}
    for i in range(10)
]
with mock.patch.object(wd.log, "warning") as mock_warn:
    result = wd._pass_zero_length(files, lambda e: -1, 10 * 1024)
    _check("no candidates flagged when every lookup is unknown", result == [])
    _check("warning logged for the unreliable-detection case", mock_warn.called)
    if mock_warn.called:
        msg = mock_warn.call_args[0][0]
        _check("warning message mentions UNRELIABLE", "UNRELIABLE" in msg)

print("\n[6] _pass_zero_length stays quiet when lookups mostly succeed (normal case)")
with mock.patch.object(wd.log, "warning") as mock_warn:
    # 9 healthy, 1 unknown -> below the 50% threshold, no warning
    sizes = {f"t{i}.mp3": (5_000_000 if i < 9 else -1) for i in range(10)}
    result = wd._pass_zero_length(files, lambda e: sizes[e["filename"]], 10 * 1024)
    _check("no false candidates for healthy files", result == [])
    _check("no warning when only a minority of lookups are unknown", not mock_warn.called)

print("\n[7] _pass_zero_length still correctly flags a real corrupt file when ADB/COM work fine")
with mock.patch.object(wd.log, "warning") as mock_warn:
    sizes = {f"t{i}.mp3": (5_000_000 if i != 3 else 0) for i in range(10)}
    result = wd._pass_zero_length(files, lambda e: sizes[e["filename"]], 10 * 1024)
    _check("exactly one candidate flagged", len(result) == 1)
    _check("flagged the genuinely corrupt file", result[0].filename == "t3.mp3")
    _check("flagged as zero_length", result[0].reason == "zero_length")
    _check("no false unreliable-detection warning", not mock_warn.called)

print(f"\n{'=' * 60}")
print(f"_stat_device_file COM fallback + pass1 warning: {passed} passed, {failed} failed")
print("=" * 60)
if failed:
    raise SystemExit(1)
