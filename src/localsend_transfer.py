"""
localsend_transfer.py — LocalSend protocol client + pipeline integration.

Protocol v2.1 per https://github.com/localsend/protocol. WiFi/HTTP-based file
transfer — no USB, no MTP/WPD/ADB — built as the default transport for
device-bound writes after 2026-07-17's extensive USB-transport instability
(MTP live-locks, SD card slowness, ADB dropping unpredictably even after a
cable replug).

Hard protocol constraint (confirmed live 2026-07-17): LocalSend can only be
driven headlessly in ONE direction — pushing files TO the Walkman. Getting
the device to push files back out requires a manual on-screen tap (confirmed
via an adb-triggered SEND intent — it opens LocalSend's confirm screen and
waits). So this module only ever originates sends; reads/deletes/moves still
go through adb_transfer.py / mtp_transfer.py.

The Walkman's LocalSend is configured to auto-receive into a FIXED, FLAT
folder — "Internal shared storage"/Music (confirmed live: a sent file landed
at /storage/emulated/0/Music/<filename>, no subfolder awareness). Two
consequences baked into try_folder_transfer()/try_file_transfer() below:
  1. LocalSend can only ever be tried when the caller has already decided the
     destination is Internal storage — it has no path to SD card at all.
  2. Every send needs a follow-up ADB `mv` from the flat inbox into the real
     Artist/Album destination folder.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import socket
import time
import uuid
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

MULTICAST_GROUP = "224.0.0.167"
MULTICAST_PORT = 53317
_ALIAS = "SpotifyTunerMK11"
_FINGERPRINT = f"spotifytunermk11-{uuid.uuid4()}"

# Confirmed live 2026-07-17: the Walkman's LocalSend auto-accepts every
# incoming file into this fixed, flat folder regardless of what the sender
# requests — there is no way to ask it for a subfolder.
LOCALSEND_INBOX_SUBDIR = "Music"


def discover(timeout: float = 5.0) -> dict[str, dict]:
    """
    Broadcast a LocalSend announcement and collect responses from peers on
    the LAN. Returns {ip: info_dict}.
    """
    announce = json.dumps({
        "alias": _ALIAS,
        "version": "2.1",
        "deviceModel": "Windows",
        "deviceType": "headless",
        "fingerprint": _FINGERPRINT,
        "port": MULTICAST_PORT,
        "protocol": "http",
        "download": False,
        "announce": True,
    }).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", MULTICAST_PORT))
    except OSError as e:
        log.warning(f"[LocalSend] Could not bind UDP {MULTICAST_PORT}: {e}")
        sock.close()
        return {}
    mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(timeout)

    sock.sendto(announce, (MULTICAST_GROUP, MULTICAST_PORT))

    peers: dict[str, dict] = {}
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            break
        except Exception as e:
            log.debug(f"[LocalSend] recv error: {e}")
            break
        try:
            info = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        if info.get("fingerprint") == _FINGERPRINT:
            continue  # our own announcement looped back
        peers[addr[0]] = info
        log.info(f"  [LocalSend] Discovered peer: {info.get('alias')!r} at {addr[0]} "
                  f"(protocol={info.get('protocol')}, port={info.get('port')})")
    sock.close()
    return peers


def _local_subnet_hosts() -> list[str]:
    """
    Return every candidate IP across this machine's real LAN-like interfaces.

    2026-07-25: the original version picked a subnet by connecting a UDP
    socket to 8.8.8.8 and reading back whichever local IP the OS chose for
    that route, then assumed a /24. Confirmed live this is wrong on a machine
    with an active VPN: the VPN's point-to-point tunnel adapter (a /32, no
    real subnet at all) had captured the default route, so that trick
    returned the VPN's tunnel IP — silently scanning the wrong network while
    the real WiFi LAN (which happened to be a /22, not a /24 either) went
    unscanned entirely.

    Uses psutil (already a dependency elsewhere in this project, e.g.
    mk15_file_coherence.py) to enumerate every interface's real
    address+netmask directly, independent of routing. Falls back to the old
    route-based trick only if psutil isn't importable.
    """
    import ipaddress

    hosts: list[str] = []
    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family != socket.AF_INET or not a.address or not a.netmask:
                    continue
                if a.address.startswith(("127.", "169.254.")):
                    continue
                if not a.address.startswith(("10.", "172.", "192.168.")):
                    continue  # only scan private-range interfaces
                try:
                    net = ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False)
                except ValueError:
                    continue
                # Skip point-to-point /32s (VPN tunnels — no subnet to scan)
                # and anything broader than /20 (keeps the parallel HTTP scan
                # under ~1 minute worst-case; a real /22 LAN, the case that
                # motivated this fix, is ~1000 hosts and scans in ~15s).
                if net.prefixlen >= 32 or net.prefixlen < 20:
                    continue
                hosts.extend(str(ip) for ip in net.hosts())
    except ImportError:
        pass

    if hosts:
        return sorted(set(hosts))

    # Fallback: the old default-route trick — works fine when nothing is
    # capturing the default route; wrong subnet if something is, but still
    # better than not scanning at all.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()
        prefix = ".".join(local_ip.split(".")[:3])
        hosts = [f"{prefix}.{i}" for i in range(1, 255)]
    except Exception as e:
        log.debug(f"[LocalSend] Could not determine local subnet: {e}")
    return hosts


def discover_http_fallback(timeout: float = 1.0, max_workers: int = 64) -> dict[str, dict]:
    """
    2026-07-25: HTTP-based discovery fallback for when multicast finds
    nothing. Confirmed live that multicast discovery can be asymmetric: this
    PC's announce reached the Walkman (its own LocalSend UI showed the PC as
    present) but the Walkman's multicast reply never made it back here (0
    peers from discover()) — consistent with router AP/client isolation,
    which blocks direct device-to-device WiFi traffic including multicast but
    typically still allows normal unicast HTTP through the AP. Scans this
    machine's local /24 directly via each candidate IP's LocalSend info
    endpoint — the same fallback real LocalSend clients use when multicast is
    unreliable. Returns {ip: info_dict}, same shape as discover().
    """
    from concurrent.futures import ThreadPoolExecutor

    hosts = _local_subnet_hosts()
    if not hosts:
        return {}

    peers: dict[str, dict] = {}

    def _probe(ip: str):
        try:
            r = requests.get(
                f"http://{ip}:{MULTICAST_PORT}/api/localsend/v2/info",
                timeout=timeout,
            )
            if r.status_code == 200:
                info = r.json()
                if info.get("fingerprint") != _FINGERPRINT:
                    return ip, info
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for result in pool.map(_probe, hosts):
            if result:
                ip, info = result
                peers[ip] = info
                log.info(
                    f"  [LocalSend/HTTP] Discovered peer: {info.get('alias')!r} at {ip} "
                    f"(deviceModel={info.get('deviceModel')})"
                )
    return peers


def discover_all(timeout: float = 5.0) -> dict[str, dict]:
    """
    Try multicast discovery first (fast, the protocol's normal path); only if
    that finds nothing, fall back to the HTTP subnet scan above. Keeps the
    common case (multicast just works) at its original 5s cost and only pays
    the subnet-scan cost when needed.
    """
    peers = discover(timeout=timeout)
    if peers:
        return peers
    log.info("[LocalSend] Multicast found nothing — trying HTTP subnet-scan fallback...")
    return discover_http_fallback()


def send_file(peer_ip: str, peer_port: int, protocol: str, filepath: Path,
              pin: "str | None" = None, timeout: float = 60.0) -> bool:
    """Send a single file to a discovered LocalSend peer. Returns True on success."""
    filepath = Path(filepath)
    file_id = "f1"
    size = filepath.stat().st_size
    mime, _ = mimetypes.guess_type(filepath.name)

    body = {
        "info": {
            "alias": _ALIAS,
            "version": "2.1",
            "deviceModel": "Windows",
            "deviceType": "headless",
            "fingerprint": _FINGERPRINT,
            "port": MULTICAST_PORT,
            "protocol": "http",
            "download": False,
        },
        "files": {
            file_id: {
                "id": file_id,
                "fileName": filepath.name,
                "size": size,
                "fileType": mime or "application/octet-stream",
            }
        },
    }

    base = f"{protocol}://{peer_ip}:{peer_port}"
    params = {"pin": pin} if pin else {}

    log.info(f"  [LocalSend] prepare-upload: {filepath.name} ({size} bytes) -> {base}")
    r = requests.post(f"{base}/api/localsend/v2/prepare-upload", json=body,
                       params=params, verify=False, timeout=15)
    if r.status_code != 200:
        log.warning(f"  [LocalSend] prepare-upload failed: HTTP {r.status_code} {r.text[:200]!r}")
        return False
    resp = r.json()
    session_id = resp["sessionId"]
    token = resp["files"][file_id]

    log.info(f"  [LocalSend] uploading (session={session_id})...")
    with open(filepath, "rb") as f:
        r2 = requests.post(
            f"{base}/api/localsend/v2/upload",
            params={"sessionId": session_id, "fileId": file_id, "token": token},
            data=f, verify=False, timeout=timeout,
        )
    if r2.status_code != 200:
        log.warning(f"  [LocalSend] upload failed: HTTP {r2.status_code} {r2.text[:200]!r}")
        return False
    log.info(f"  [LocalSend] {filepath.name} sent successfully")
    return True


def send_folder(peer_ip: str, peer_port: int, protocol: str, filepaths: list[Path],
                 pin: "str | None" = None, timeout: float = 120.0) -> dict[str, bool]:
    """
    Send a whole folder's files as ONE LocalSend session (single prepare-upload
    covering every file, then one upload call per file). Returns
    {filepath_str: bool} per-file success — a folder-level prepare-upload
    failure marks every file False without attempting any upload.

    Batching matters here: this is the actual "folder by folder" unit tonight's
    design calls for (2026-07-17) — one session per album, not one session per
    file, so a receiver sees/accepts one coherent transfer instead of dozens of
    unrelated single-file prompts (moot with auto-accept, but keeps LocalSend's
    own session/rate-limit accounting sane for a busy library sync).
    """
    filepaths = [Path(p) for p in filepaths]
    results: dict[str, bool] = {str(p): False for p in filepaths}
    if not filepaths:
        return results

    file_entries = {}
    id_to_path = {}
    for i, p in enumerate(filepaths):
        file_id = f"f{i}"
        mime, _ = mimetypes.guess_type(p.name)
        file_entries[file_id] = {
            "id": file_id,
            "fileName": p.name,
            "size": p.stat().st_size,
            "fileType": mime or "application/octet-stream",
        }
        id_to_path[file_id] = p

    body = {
        "info": {
            "alias": _ALIAS,
            "version": "2.1",
            "deviceModel": "Windows",
            "deviceType": "headless",
            "fingerprint": _FINGERPRINT,
            "port": MULTICAST_PORT,
            "protocol": "http",
            "download": False,
        },
        "files": file_entries,
    }

    base = f"{protocol}://{peer_ip}:{peer_port}"
    params = {"pin": pin} if pin else {}

    log.info(f"  [LocalSend] prepare-upload: {len(filepaths)} file(s) -> {base}")
    r = requests.post(f"{base}/api/localsend/v2/prepare-upload", json=body,
                       params=params, verify=False, timeout=30)
    if r.status_code != 200:
        log.warning(f"  [LocalSend] folder prepare-upload failed: HTTP {r.status_code} {r.text[:200]!r}")
        return results
    resp = r.json()
    session_id = resp["sessionId"]
    tokens = resp["files"]

    for file_id, p in id_to_path.items():
        token = tokens.get(file_id)
        if not token:
            log.warning(f"  [LocalSend] no token for {p.name} — receiver rejected this file")
            continue
        try:
            with open(p, "rb") as f:
                r2 = requests.post(
                    f"{base}/api/localsend/v2/upload",
                    params={"sessionId": session_id, "fileId": file_id, "token": token},
                    data=f, verify=False, timeout=timeout,
                )
            if r2.status_code == 200:
                results[str(p)] = True
                log.info(f"  [LocalSend] {p.name} sent successfully")
            else:
                log.warning(f"  [LocalSend] {p.name} upload failed: HTTP {r2.status_code} {r2.text[:200]!r}")
        except Exception as e:
            log.warning(f"  [LocalSend] {p.name} upload error: {e}")

    return results


_peer_cache: "dict | None" = None


def get_walkman_peer(device_model_hint: str = "SonyAudio", rediscover: bool = False,
                      timeout: float = 5.0) -> "tuple[str, int, str] | None":
    """
    Cached discovery — find the Walkman's LocalSend peer once per process,
    only re-run discovery if rediscover=True (e.g. after a send failure, the
    peer's IP may have changed). Uses discover_all() — multicast first, HTTP
    subnet-scan fallback if that finds nothing (2026-07-25, see
    discover_http_fallback's docstring for why multicast alone isn't
    reliable on every network).

    2026-08-09: discover_all() only falls back to the HTTP scan when
    multicast finds NOTHING — but multicast can find a real, unrelated peer
    (e.g. another LocalSend-compatible device on the same LAN) while still
    missing the Walkman, which used to make this function give up too early.
    Confirmed live: multicast alone found an unrelated Windows peer while
    the actual Walkman only showed up via the HTTP subnet-scan. So if the
    target device_model_hint isn't among discover_all()'s results, this now
    tries discover_http_fallback() directly as a second pass before giving
    up, regardless of what multicast did or didn't find.
    Returns (ip, port, protocol) or None if not found.
    """
    global _peer_cache
    if _peer_cache is not None and not rediscover:
        return _peer_cache

    def _match(peers: dict) -> "tuple[str, int, str] | None":
        for ip, info in peers.items():
            if info.get("deviceModel") == device_model_hint:
                return (ip, info.get("port", MULTICAST_PORT), info.get("protocol", "https"))
        return None

    peers = discover_all(timeout=timeout)
    match = _match(peers)
    if match is None and peers:
        # multicast found something, but not the target device — try the
        # HTTP subnet-scan explicitly rather than trusting discover_all()'s
        # "multicast found something so skip HTTP" shortcut.
        peers = {**peers, **discover_http_fallback()}
        match = _match(peers)

    if match is not None:
        _peer_cache = match
        return _peer_cache

    log.warning(f"  [LocalSend] No peer with deviceModel={device_model_hint!r} found "
                f"among {len(peers)} discovered peer(s)")
    return None


def push_file_via_localsend(
    src, dest_parts: list, device_name: str, storage_name: str,
    overwrite: bool = False, device_index=None,
) -> bool:
    """
    Push a file to the device over LocalSend, then move it out of the fixed
    flat auto-accept inbox into its real Artist/Album destination.

    The follow-up relocation tries ADB first (a cheap, in-place on-device
    `mv` — no file bytes cross USB/MTP again) and falls back to MTP/COM
    (added 2026-07-17, after ADB was found to drop offline mid-diagnosis on
    this same device) if ADB is unavailable or the mv fails for any reason.
    The COM fallback re-pushes the SAME local `src` straight to its real
    destination — COM has no in-place on-device move primitive, so a fresh
    push is the fallback's cost — then deletes the stray flat-inbox copy
    LocalSend left behind via a silent PowerShell-subprocess delete (no ADB
    involved in that cleanup step either).

    Same call shape as adb_transfer.copy_file_to_device /
    mtp_transfer_com.copy_file_to_device so it can be tried as a first-choice
    transport ahead of them. Never raises — returns False on any failure
    (no peer found, send failed, both relocation paths failed) so the caller
    can fall back to the existing ADB/MTP routed transport unchanged.

    Only usable for storage_name == "Internal shared storage" — LocalSend's
    receiver has no path to the SD card at all (see module docstring).
    """
    if storage_name != "Internal shared storage":
        return False

    peer = get_walkman_peer()
    if peer is None:
        # 2026-07-18: found live that the default 5s discovery window can miss
        # a genuinely-reachable peer intermittently (manual 8s checks found it
        # consistently when this 5s call had just missed it, back to back) —
        # one immediate rediscovery retry before giving up, matching the retry
        # already used below for a failed send.
        peer = get_walkman_peer(rediscover=True)
        if peer is None:
            return False

    src = Path(src)
    ok = send_file(*peer, src)
    if not ok:
        # Peer IP may have changed since the cached discovery — one retry.
        peer = get_walkman_peer(rediscover=True)
        if peer is None:
            return False
        ok = send_file(*peer, src)
    if not ok:
        return False

    # Best-effort, ADB-independent display path — always defined by the time
    # either relocation attempt below needs it for logging/device_index.
    # Overwritten with the precise absolute path if the ADB branch runs.
    dest_path = "/".join([storage_name] + list(dest_parts) + [src.name])

    # ── Relocation, attempt 1: ADB in-place mv (cheap, no re-transfer) ──────
    try:
        import adb_transfer as _adb
        if getattr(_adb, "device_is_connected", lambda _n: False)(""):
            storage_path = _adb._get_storage_path(storage_name)
            inbox_path = f"{storage_path}/{LOCALSEND_INBOX_SUBDIR}/{src.name}"
            dest_dir = storage_path + ("/" + "/".join(dest_parts) if dest_parts else "")
            dest_path = f"{dest_dir}/{src.name}"

            if not overwrite and _adb._exists(dest_path):
                log.debug(f"  [LocalSend] Skip mv (dest exists): {src.name}")
                _adb._rm_f(inbox_path)  # discard the duplicate LocalSend just delivered
                return True

            if _adb._mv(inbox_path, dest_path):
                if device_index is not None:
                    try:
                        device_index.mark_file(dest_path, size=src.stat().st_size)
                    except Exception:
                        device_index.mark_file(dest_path)
                log.info(f"  [LocalSend] {src.name} delivered + relocated (ADB mv) -> {dest_path}")
                return True
            log.warning(f"  [LocalSend] ADB follow-up mv failed for {src.name!r}; "
                        f"falling back to MTP/COM")
        else:
            log.info(f"  [LocalSend] ADB not connected; using MTP/COM for the follow-up move "
                      f"of {src.name!r}")
    except Exception as e:
        log.warning(f"  [LocalSend] ADB follow-up mv errored ({e}); falling back to MTP/COM")

    # ── Relocation, attempt 2: MTP/COM re-push + flat-inbox cleanup ─────────
    # Subprocess-isolated (2026-07-18): confirmed live that the plain in-process
    # copy_file_to_device() can hang indefinitely on this device (the same
    # "blocked COM call holds the GIL" class of bug Task 4 fixed for the WPD
    # scanner, just never fixed for the copy path) — copy_file_to_device_isolated
    # applies the same quiet-timeout + wall-clock-cap subprocess guards.
    try:
        import mtp_transfer_com as _com
        result = _com.copy_file_to_device_isolated(
            src, dest_parts, device_name, storage_name, overwrite=overwrite,
        )
        if result is None:
            log.warning(f"  [LocalSend] MTP/COM fallback push failed or timed out for "
                        f"{src.name!r}; giving up — file may still be sitting in the flat inbox")
            return False
    except Exception as e:
        log.warning(f"  [LocalSend] MTP/COM fallback push failed for {src.name!r} ({e}); "
                    f"giving up — file may still be sitting in the flat inbox")
        return False

    try:
        import wpd_transfer as _wpd
        _wpd.delete_file_ps(
            device_name=device_name, storage_name=storage_name,
            dest_parts=[LOCALSEND_INBOX_SUBDIR], filename=src.name,
        )
    except Exception as e:
        log.warning(f"  [LocalSend] Could not clean up stray flat-inbox copy of {src.name!r} "
                    f"after MTP/COM fallback ({e}) — file is correctly at its destination, "
                    f"but a duplicate may remain in the flat inbox")

    if device_index is not None:
        try:
            device_index.mark_file(dest_path, size=src.stat().st_size)
        except Exception:
            device_index.mark_file(dest_path)
    log.info(f"  [LocalSend] {src.name} delivered via LocalSend, relocated via MTP/COM fallback "
             f"-> {dest_path}")
    return True


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%H:%M:%S")
    log.info("[LocalSend] Discovering peers (5s multicast, HTTP subnet-scan fallback if needed)...")
    peers = discover_all(timeout=5.0)
    if not peers:
        log.warning("[LocalSend] No peers found.")
        sys.exit(1)
    for ip, info in peers.items():
        print(f"  {ip}: {info}")
