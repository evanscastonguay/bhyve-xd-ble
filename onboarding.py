"""
onboarding — one-time Orbit cloud fetch: email + password -> device list + BLE keys.

This is the ONLY part of the system that touches the cloud, and only at setup.
Once `cloud_fetch` has given you each device's 16-byte network key (and MAC), all
control is local BLE (see bhyve_xd.py) and the cloud is never needed again.

    from onboarding import cloud_fetch
    devices = await cloud_fetch("me@example.com", "hunter2")
    # -> [{"name","mac","stations","hardware","firmware","network_key"}, ...]

The network key is the durable, account-scoped secret (it decodes/controls every
device on the account). It is written once into the git-ignored config.json; the
password is never persisted.

Design notes:
  * The HTTP client (aiohttp) is imported LAZILY inside cloud_fetch, mirroring how
    bhyve_xd defers bleak — so the pure parsing/error-mapping logic (and the
    offline self-test) runs with no HTTP stack installed.
  * Transport is kept thin; all response shaping lives in the pure, unit-tested
    `_build_devices`. Change the cloud schema handling in one place.

Ported from the proven bhyve-local/bhyvexd/cloud.py, plus the typed error mapping
the onboarding plan calls for.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import tempfile
import time

# Re-exported so onboarding is the single "setup entry point"; the implementation
# lives in bhyve_xd (one source of truth) and is used to default the device clock.
from bhyve_xd import host_tz_offset  # noqa: F401

CLOUD_API_BASE = "https://api.orbitbhyve.com/v1"
CLOUD_APP_ID = "Bhyve-App"
# Orbit's WAF 403s any User-Agent containing "HomeAssistant"; use a browser UA.
CLOUD_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Newer accounts serve keys from /network_topologies instead of /meshes.
KEY_PATHS = ("/meshes/{mesh_id}", "/network_topologies/{mesh_id}")
KEY_FIELDS = ("ble_network_key", "network_key")


# --------------------------------------------------------------------------- #
# Typed errors — callers can catch CloudError broadly or a specific subclass.
# --------------------------------------------------------------------------- #
class CloudError(Exception):
    """Base for every cloud-onboarding failure."""


class AuthError(CloudError):
    """Bad credentials / rejected login (HTTP 400/401/403)."""


class RateLimited(CloudError):
    """Too many requests (HTTP 429) — back off and retry later."""


class MFARequired(CloudError):
    """The account requires multi-factor auth, which this flow can't complete."""


class CloudConnectionError(CloudError):
    """Network failure reaching api.orbitbhyve.com (DNS/TLS/timeout)."""


class ResolveError(Exception):
    """Could not resolve a device's local BLE address (macOS UUID pairing)."""


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O) — the offline self-test exercises these directly.
# --------------------------------------------------------------------------- #
def _raise_for_status(status: int, *, context: str = "request") -> None:
    """Map an HTTP status to a typed error. 2xx/3xx pass through silently."""
    if status == 429:
        raise RateLimited(f"{context}: rate limited (HTTP 429) — wait and retry")
    if status in (400, 401, 403):
        raise AuthError(f"{context}: rejected (HTTP {status}) — check email/password")
    if status >= 400:
        raise CloudError(f"{context}: HTTP {status}")


def _check_mfa(body: object) -> None:
    """Surface a multi-factor challenge as MFARequired rather than a confusing
    'no api key' failure. Orbit MFA hasn't been observed on the test accounts, so
    this is defensive: we key off the flags such challenges conventionally set."""
    if isinstance(body, dict) and any(
        body.get(k) for k in ("mfa_required", "require_two_factor", "two_factor_required")
    ):
        raise MFARequired("account requires multi-factor auth — not supported by this flow")


def current_platform() -> str:
    """'macos' on Darwin, else 'linux' — selects the address-resolution strategy."""
    return "macos" if platform.system() == "Darwin" else "linux"


def _b64_to_hex(b64: str | None) -> str | None:
    if not b64:
        return None
    try:
        return base64.b64decode(b64).hex()
    except Exception:
        return None


def _format_mac(no_colons: str | None) -> str | None:
    if not no_colons or len(no_colons) != 12:
        return None
    return ":".join(no_colons[i:i + 2] for i in range(0, 12, 2)).upper()


def _key_from_mesh(mesh: dict) -> str | None:
    for f in KEY_FIELDS:
        key = _b64_to_hex(mesh.get(f))
        if key:
            return key
    return None


def _build_devices(raw_devices: list, mesh_by_id: dict) -> list[dict]:
    """Pure join of the /devices list with the fetched mesh (key) records.

    Drops bridges (Wi-Fi hubs don't actuate) and any device without a mesh_id.
    Given the same inputs it always yields the same list — so the offline test
    can feed it recorded/mocked JSON with no network involved.
    """
    out: list[dict] = []
    for d in raw_devices:
        if (d.get("type") or "").lower() == "bridge":
            continue  # hubs don't actuate
        mesh_id = d.get("mesh_id") or d.get("network_topology_id")
        if not mesh_id:
            continue
        mesh = mesh_by_id.get(mesh_id) or {}
        out.append({
            "name": d.get("name") or "B-Hyve",
            "mac": _format_mac(d.get("mac_address")),
            "hardware": d.get("hardware_version") or "unknown",
            "firmware": d.get("firmware_version") or "?",
            "stations": int(d.get("num_stations") or 0),
            "network_key": _key_from_mesh(mesh),
        })
    return out


# --------------------------------------------------------------------------- #
# Live transport (lazy aiohttp) — thin; all shaping is in _build_devices.
# --------------------------------------------------------------------------- #
async def cloud_fetch(email: str, password: str) -> list[dict]:
    """Log in to the Orbit cloud and return each controllable device with its
    16-byte BLE network key (hex) filled in. One-time setup call.

    Raises AuthError / RateLimited / MFARequired / CloudConnectionError / CloudError.
    """
    try:
        import aiohttp
    except ModuleNotFoundError as err:
        raise CloudError("cloud login needs aiohttp — pip install -r requirements.txt") from err

    headers = {
        "orbit-app-id": CLOUD_APP_ID,
        "User-Agent": CLOUD_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession() as s:
        # -- login -------------------------------------------------------- #
        try:
            async with s.post(
                f"{CLOUD_API_BASE}/session",
                json={"session": {"email": email, "password": password}},
                headers={**headers, "Content-Type": "application/json"},
                timeout=timeout,
            ) as r:
                _raise_for_status(r.status, context="login")
                body = await r.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(f"cannot reach {CLOUD_API_BASE}: {err}") from err
        _check_mfa(body)
        token = body.get("orbit_api_key")
        if not token:
            raise AuthError("login succeeded but no orbit_api_key in the response")
        headers["orbit-api-key"] = token

        # -- devices ------------------------------------------------------ #
        try:
            async with s.get(
                f"{CLOUD_API_BASE}/devices", headers=headers, timeout=timeout,
            ) as r:
                _raise_for_status(r.status, context="devices")
                raw_devices = await r.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(f"cannot reach {CLOUD_API_BASE}: {err}") from err

        # -- meshes (keys), one fetch per referenced mesh ----------------- #
        mesh_by_id: dict[str, dict] = {}
        for d in raw_devices:
            if (d.get("type") or "").lower() == "bridge":
                continue
            mesh_id = d.get("mesh_id") or d.get("network_topology_id")
            if mesh_id and mesh_id not in mesh_by_id:
                mesh_by_id[mesh_id] = await _fetch_mesh(s, headers, mesh_id, timeout)

    return _build_devices(raw_devices, mesh_by_id)


async def _fetch_mesh(s, headers: dict, mesh_id: str, timeout) -> dict:
    """GET the mesh/key record, trying each KEY_PATH in turn.

    A 401/403 HERE is NOT an expired session — login and /devices already
    succeeded with this token, so an auth-style rejection means the endpoint
    simply doesn't apply to this account's schema (newer accounts 401 on
    /meshes and serve keys from /network_topologies). Treat it like 404 and try
    the next path rather than mislabeling it a credentials failure.
    """
    import aiohttp

    last = None
    for tmpl in KEY_PATHS:
        url = f"{CLOUD_API_BASE}{tmpl.format(mesh_id=mesh_id)}"
        try:
            async with s.get(url, headers=headers, timeout=timeout) as r:
                if r.status in (401, 403, 404):
                    last = r.status
                    continue
                _raise_for_status(r.status, context=f"mesh {mesh_id}")
                return await r.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(f"cannot reach {CLOUD_API_BASE}: {err}") from err
    raise CloudError(f"no key path returned a mesh for {mesh_id} (last status {last})")


# --------------------------------------------------------------------------- #
# Address resolution / pairing — cloud gives a MAC; BLE control needs a local
# address. On Linux that's the MAC; on macOS it's an opaque per-Mac UUID we must
# discover by connecting and reading the device's own MAC back over BLE.
# --------------------------------------------------------------------------- #
async def resolve_address(mac: str, network_key: str, *, platform_name: str | None = None,
                          scan_timeout: float = 12.0, max_probe: int = 12) -> str:
    """Resolve the local BLE address to control the timer whose cloud MAC is `mac`:

      Linux/BlueZ -> the MAC itself (BlueZ addresses peripherals by MAC).
      macOS       -> scan, probe candidates strongest-RSSI first, read each one's
                     own MAC over BLE, and return the opaque UUID whose MAC matches.

    Non-B-Hyve devices are fast-rejected (no fe32 service) without ever being armed.
    On macOS, wake the timer (press/hold its button) right before calling.
    """
    p = platform_name or current_platform()
    if p == "linux":
        return mac
    return await _resolve_macos(mac, network_key, scan_timeout=scan_timeout, max_probe=max_probe)


async def _resolve_macos(mac: str, network_key: str, *, scan_timeout: float, max_probe: int) -> str:
    from bleak import BleakScanner

    from bhyve_xd import BHyveXD, NotABHyveError

    devs = await BleakScanner.discover(timeout=scan_timeout, return_adv=True)
    # macOS hides the fe32 service in advertisements, so we can't pre-filter by
    # service UUID; instead probe strongest-first and let the session fast-reject
    # non-B-Hyve devices (NotABHyveError) before arming. Cap the probes at max_probe.
    candidates = sorted(((adv.rssi if adv.rssi is not None else -999, addr)
                         for addr, (_d, adv) in devs.items()), reverse=True)[:max_probe]
    tried = 0
    for _rssi, addr in candidates:
        try:
            dev = BHyveXD(addr, network_key)     # candidate UUID + the account key
            # Fast probe: 1 connect attempt + short find so a wrong/asleep device
            # fails quickly instead of retrying for the full scan_timeout.
            async with dev.session(scan_timeout=4.0, connect_attempts=1) as sess:
                await sess.arm()
                st = await sess.read_status()
            tried += 1
            if st.device_mac and st.device_mac.upper() == mac.upper():
                return addr
        except NotABHyveError:
            continue  # not a B-Hyve; never armed
        except Exception:
            continue  # asleep / connect failed / wrong device
    raise ResolveError(
        f"could not find timer {mac} nearby (checked {len(candidates)} candidate(s), "
        f"{tried} B-Hyve) — wake it (press/hold its button), keep it close to the Mac, and retry")


# --------------------------------------------------------------------------- #
# Config persistence — merge a resolved device into config.json. Idempotent
# (dedupe/update by MAC) and atomic (temp file + rename), so re-registering a
# drifted address updates in place and a crash never leaves a half-written config.
# --------------------------------------------------------------------------- #
def key_from_existing_config(path: str) -> str | None:
    """Return the account network key already stored in config.json (from the first
    device that has one), or None. The key is account-scoped, so registering another
    timer on the same account can reuse it — no cloud login needed."""
    if not os.path.exists(path):
        return None
    try:
        devices = json.load(open(path)).get("devices", [])
    except (json.JSONDecodeError, OSError, AttributeError):
        return None
    for d in devices:
        if isinstance(d, dict) and d.get("network_key"):
            return d["network_key"]
    return None


def write_config(path: str, device: dict) -> dict:
    """Merge `device` into the `devices` list of `path` and write it back atomically.

    device: {name, address, network_key, mac, stations, tz_offset_sec?}. Matching is
    by `mac` (case-insensitive), falling back to `address`; a match is updated in
    place (preserving any extra fields already there), otherwise the device is
    appended. Returns the full config dict written.

    A missing/empty file starts a fresh config. A file that exists but is not valid
    JSON raises ValueError and is left untouched (never clobbered).
    """
    config: dict = {"devices": []}
    if os.path.exists(path):
        text = open(path).read().strip()
        if text:
            try:
                config = json.loads(text)
            except json.JSONDecodeError as err:
                raise ValueError(f"{path} is not valid JSON — refusing to overwrite") from err
    devices = config.setdefault("devices", [])

    want_mac = (device.get("mac") or "").upper()
    idx = None
    for i, d in enumerate(devices):
        if want_mac and (d.get("mac") or "").upper() == want_mac:
            idx = i
            break
        if not want_mac and d.get("address") and d.get("address") == device.get("address"):
            idx = i
            break
    if idx is None:
        devices.append(device)
    else:
        devices[idx] = {**devices[idx], **device}   # update, keep any extra existing fields

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)   # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return config


# --------------------------------------------------------------------------- #
# Connect-on-detection discovery — the robust registration primitive.
# Unlike resolve_address's scan-then-connect (which can go stale against a
# rotating/private address), this connects to the EXACT advertisement it just saw,
# so it works for both stable and rotating addresses. Precondition: the phone must
# not be holding the timer (its Bluetooth OFF) — then the timer advertises freely.
# --------------------------------------------------------------------------- #
async def catch_device_session(network_key_hex: str, *, want_mac: str | None = None,
                               scan_timeout: float = 90.0, near_rssi: int = -80,
                               tz_offset_sec: int | None = None) -> tuple[str, str, object, object]:
    """Catch a B-Hyve by its live advertisement and return an OPEN, armed session:
    (address, device_mac, DeviceStatus, session).

    On each newly-seen, close-enough advertisement we connect to THAT device (robust
    to rotating addresses — no rescan), fast-reject non-B-Hyve (no fe32), adopt a
    session, arm, and read status. Returns the device whose MAC equals want_mac, or
    the first B-Hyve if want_mac is None. Raises ResolveError on timeout.

    The caller OWNS the returned session and MUST close it when done:
    `await session.__aexit__(None, None, None)`. Use catch_device() for a one-shot
    read that connects and disconnects for you.
    """
    from bleak import BleakClient, BleakScanner

    from bhyve_xd import BHyveXD

    want = want_mac.upper() if want_mac else None
    tz = tz_offset_sec if tz_offset_sec is not None else host_tz_offset()
    seen: set[str] = set()
    queue: asyncio.Queue = asyncio.Queue()
    n_bhyve = 0

    def _cb(dev, adv):
        r = adv.rssi if getattr(adv, "rssi", None) is not None else -999
        if dev.address in seen or r < near_rssi:
            return
        seen.add(dev.address)
        queue.put_nowait(dev)

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    deadline = time.monotonic() + scan_timeout
    try:
        while time.monotonic() < deadline:
            try:
                remaining = max(0.05, deadline - time.monotonic())
                dev = await asyncio.wait_for(queue.get(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue
            try:
                client = BleakClient(dev)
                await client.connect()
            except Exception:
                continue  # advert went stale / asleep / connect failed
            if not any("fe32" in s.uuid.lower() for s in client.services):
                try:
                    await client.disconnect()
                except Exception:
                    pass
                continue  # not a B-Hyve
            n_bhyve += 1
            sess = BHyveXD(dev.address, network_key_hex, tz_offset_sec=tz).session(client=client)
            try:
                await sess.__aenter__()
                await sess.arm()
                st = await sess.read_status()
            except Exception:
                try:
                    await sess.__aexit__(None, None, None)
                except Exception:
                    pass
                continue  # armed the wrong thing / decode failed; try the next advert
            if st is not None and st.device_mac and (want is None or st.device_mac.upper() == want):
                return dev.address, st.device_mac, st, sess   # OPEN — caller closes
            await sess.__aexit__(None, None, None)   # a B-Hyve, but not the one — keep scanning
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass
    target = want_mac or "any B-Hyve"
    raise ResolveError(
        f"could not catch {target} within {scan_timeout:.0f}s (saw {len(seen)} device(s), "
        f"{n_bhyve} B-Hyve) — is the phone's Bluetooth OFF so it isn't holding the timer? "
        "keep the timer close to the Mac and retry")


async def catch_device(network_key_hex: str, *, want_mac: str | None = None,
                       scan_timeout: float = 90.0, near_rssi: int = -80,
                       tz_offset_sec: int | None = None) -> tuple[str, str, object]:
    """One-shot discovery: catch a B-Hyve, read its MAC + status, and disconnect.
    Returns (address, device_mac, DeviceStatus). Built on catch_device_session (which
    keeps the connection open for callers that need it). Raises ResolveError on timeout.
    """
    address, mac, st, sess = await catch_device_session(
        network_key_hex, want_mac=want_mac, scan_timeout=scan_timeout,
        near_rssi=near_rssi, tz_offset_sec=tz_offset_sec)
    await sess.__aexit__(None, None, None)
    return address, mac, st
