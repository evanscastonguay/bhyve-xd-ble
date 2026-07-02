"""
onboarding — credential-driven setup for bhyve-xd.

email + password  ─▶  Orbit cloud login  ─▶  device list + network keys
                                            ─▶  resolve BLE address per platform
                                            ─▶  write config.json (no password stored)

Cloud is used ONLY here (setup). After onboarding, all control is local BLE.

Reusable module: the CLI calls it today; a web flow could call the same functions
later. Pure logic (parsing, formatting, error mapping) is unit-tested offline in
test_onboarding.py; the live pieces (cloud_fetch, macOS pairing scan) are exercised
by `cli.py login`.
"""
from __future__ import annotations

import base64
import platform

# --- Orbit cloud constants (from the proven bhyve-local/cloud.py) ------------ #
CLOUD_API_BASE = "https://api.orbitbhyve.com/v1"
CLOUD_APP_ID = "Bhyve-App"
# Orbit's WAF 403s any User-Agent containing "HomeAssistant"; use a browser UA.
CLOUD_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Newer accounts serve keys from /network_topologies instead of /meshes.
KEY_PATHS = ("/meshes/{mesh_id}", "/network_topologies/{mesh_id}")
KEY_FIELDS = ("ble_network_key", "network_key")


# --- errors ------------------------------------------------------------------ #
class OnboardError(Exception):
    pass


class CloudError(OnboardError):
    pass


class AuthError(OnboardError):
    """Bad email/password (HTTP 400/401/403)."""


class RateLimited(OnboardError):
    """Too many requests (HTTP 429)."""


def error_for_status(status: int):
    """Map an HTTP status to the exception CLASS to raise, or None if OK (2xx)."""
    if 200 <= status < 300:
        return None
    if status in (400, 401, 403):
        return AuthError
    if status == 429:
        return RateLimited
    return CloudError


# --- pure helpers (unit-tested) ---------------------------------------------- #
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
        k = _b64_to_hex(mesh.get(f))
        if k:
            return k
    return None


def parse_devices(raw_devices: list[dict], meshes: dict[str, dict]) -> list[dict]:
    """Join raw cloud devices with their mesh (holding the key) into a clean list
    of {name, mac, network_key, stations, hardware, firmware}. Skips bridges/hubs."""
    out = []
    for d in raw_devices:
        if (d.get("type") or "").lower() == "bridge":
            continue
        mesh_id = d.get("mesh_id") or d.get("network_topology_id")
        mesh = meshes.get(mesh_id, {}) if mesh_id else {}
        out.append({
            "name": d.get("name") or "B-Hyve",
            "mac": _format_mac(d.get("mac_address")),
            "network_key": _key_from_mesh(mesh),
            "stations": int(d.get("num_stations") or 0),
            "hardware": d.get("hardware_version") or "unknown",
            "firmware": d.get("firmware_version") or "?",
        })
    return out


def current_platform() -> str:
    return "macos" if platform.system() == "Darwin" else "linux"


def write_config(devices: list[dict], path: str = "config.json") -> None:
    """Write a config.json from resolved device dicts. Omits tz_offset_sec so it
    derives from the host at load time. Never writes the password."""
    import json
    out = {"devices": [
        {"name": d.get("name", "B-Hyve XD"),
         "address": d["address"],
         "network_key": d["network_key"],
         "stations": int(d.get("stations", 4)),
         "mac": d.get("mac")}
        for d in devices]}
    with open(path, "w") as f:
        json.dump(out, f, indent=2)


# --- live: cloud fetch ------------------------------------------------------- #
async def cloud_fetch(email: str, password: str) -> list[dict]:
    """Log in to Orbit and return the account's timers with network keys:
    [{name, mac, network_key, stations, hardware, firmware}]. Cloud only."""
    import aiohttp

    headers = {"orbit-app-id": CLOUD_APP_ID, "User-Agent": CLOUD_USER_AGENT,
               "Accept": "application/json, text/plain, */*"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{CLOUD_API_BASE}/session",
                          json={"session": {"email": email, "password": password}},
                          headers={**headers, "Content-Type": "application/json"}) as r:
            exc = error_for_status(r.status)
            if exc is AuthError:
                raise AuthError("login rejected — check email/password")
            if exc:
                raise exc(f"login failed (HTTP {r.status})")
            body = await r.json()
        token = body.get("orbit_api_key")
        if not token:
            raise CloudError("login ok but no orbit_api_key returned (MFA on the account?)")
        headers["orbit-api-key"] = token

        async with s.get(f"{CLOUD_API_BASE}/devices", headers=headers) as r:
            if (exc := error_for_status(r.status)):
                raise exc(f"list devices failed (HTTP {r.status})")
            raw_devices = await r.json()

        meshes: dict[str, dict] = {}
        for d in raw_devices:
            mid = d.get("mesh_id") or d.get("network_topology_id")
            if mid and mid not in meshes:
                meshes[mid] = await _get_mesh(s, headers, mid)
        return parse_devices(raw_devices, meshes)


async def _get_mesh(s, headers, mesh_id) -> dict:
    last = None
    for tmpl in KEY_PATHS:
        async with s.get(f"{CLOUD_API_BASE}{tmpl.format(mesh_id=mesh_id)}", headers=headers) as r:
            if r.status in (401, 403, 404):
                last = r.status
                continue
            if (exc := error_for_status(r.status)):
                raise exc(f"mesh fetch failed (HTTP {r.status})")
            return await r.json()
    raise CloudError(f"no key path worked for mesh {mesh_id} (last {last})")


# --- live: address resolution ("pairing") ------------------------------------ #
async def resolve_address(mac: str, network_key: str, *, platform_name: str | None = None,
                          scan_timeout: float = 6.0, top_n: int = 4) -> str:
    """Resolve the local BLE address for `mac`:
      Linux/BlueZ -> the MAC itself.
      macOS       -> scan, probe the strongest candidates, arm each and read its
                     MAC, and return the opaque UUID whose device MAC matches.
    Wake the timer (press its button) before calling on macOS."""
    p = platform_name or current_platform()
    if p == "linux":
        return mac
    return await _resolve_macos(mac, network_key, scan_timeout=scan_timeout, top_n=top_n)


async def _resolve_macos(mac: str, network_key: str, *, scan_timeout: float, top_n: int) -> str:
    from bleak import BleakScanner
    from bhyve_xd import BHyveXD

    devs = await BleakScanner.discover(timeout=scan_timeout, return_adv=True)
    candidates = sorted(((adv.rssi if adv.rssi is not None else -999, addr)
                         for addr, (d, adv) in devs.items()), reverse=True)[:top_n]
    for _rssi, addr in candidates:
        try:
            dev = BHyveXD(addr, network_key)     # candidate UUID + the account key
            async with dev.session(scan_timeout=6.0) as sess:
                await sess.arm()                 # non-B-Hyve devices fail fast here
                st = await sess.read_status()
            if st.device_mac and st.device_mac.upper() == mac.upper():
                return addr
        except Exception:
            continue  # not this one (not a B-Hyve, or wrong device, or asleep)
    raise OnboardError(f"could not find timer {mac} nearby — wake it (press its button) and retry")
