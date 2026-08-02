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


def _load_config(path: str) -> dict:
    """Load the config dict from `path`, or a fresh {'devices': []} if missing/empty.
    A file that exists but is not valid JSON raises ValueError (never clobbered)."""
    config: dict = {"devices": []}
    if os.path.exists(path):
        text = open(path).read().strip()
        if text:
            try:
                config = json.loads(text)
            except json.JSONDecodeError as err:
                raise ValueError(f"{path} is not valid JSON — refusing to overwrite") from err
    return config


def _atomic_write_config(path: str, config: dict) -> dict:
    """Write `config` to `path` atomically (write temp + os.replace). Returns config."""
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


def write_config(path: str, device: dict) -> dict:
    """Merge `device` into the `devices` list of `path` and write it back atomically.

    device: {name, address, network_key, mac, stations, tz_offset_sec?}. Matching is
    by `mac` (case-insensitive), falling back to `address`; a match is updated in
    place (preserving any extra fields already there), otherwise the device is
    appended. Preserves any `account` block already present. Returns the full config
    dict written.

    A missing/empty file starts a fresh config. A file that exists but is not valid
    JSON raises ValueError and is left untouched (never clobbered).
    """
    config = _load_config(path)
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

    return _atomic_write_config(path, config)


def read_account(path: str) -> dict | None:
    """Return the remembered Orbit account {'email', 'network_key'} from `path`, or None.

    The account is per-install (not per-timer): its network key is the shared account/mesh
    key, reused to add further timers without another login. The password is NEVER stored."""
    try:
        acct = _load_config(path).get("account")
    except (ValueError, OSError):
        return None
    if isinstance(acct, dict) and acct.get("email") and acct.get("network_key"):
        return {"email": acct["email"], "network_key": acct["network_key"]}
    return None


def write_account(path: str, email: str, network_key: str) -> dict:
    """Persist the Orbit account (email + shared network key) into the `account` block of
    `path`, atomically, preserving the `devices` list. NEVER stores the password.

    A file that exists but is not valid JSON raises ValueError and is left untouched."""
    config = _load_config(path)
    config.setdefault("devices", [])
    config["account"] = {"email": email, "network_key": network_key}
    return _atomic_write_config(path, config)


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


# --------------------------------------------------------------------------- #
# Web onboarding state machine — an async generator of per-step events for the
# SSE-driven wizard (PLAN_web_onboarding.md). Tries the Orbit account key first;
# on login failure / first-user (device not on account) it emits a fallback CHOICE
# (guided app setup + retry, or self-key). Human-gated steps pause on a gate the
# caller resumes (optionally with a choice). Never emits the key.
# --------------------------------------------------------------------------- #
class OnboardGate:
    def __init__(self):
        self._ev = asyncio.Event()
        self._choice = None

    def resume(self, choice=None):
        self._choice = choice
        self._ev.set()

    async def wait(self):
        await self._ev.wait()
        self._ev.clear()
        return self._choice


def _step(sid, title, instruction, *, state, expected_wait_s=None, verified=False,
          choices=None, options=None):
    ev = {"id": sid, "title": title, "instruction": instruction, "state": state, "verified": verified}
    if expected_wait_s is not None:
        ev["expected_wait_s"] = expected_wait_s
    if choices is not None:
        ev["choices"] = choices
    if options is not None:                 # [{value, label}] for a device picker
        ev["options"] = options
    return ev


def _stash_key(key, mac, path):
    """Append a self-generated key to a git-ignored stash so it's never lost."""
    if not path:
        return
    from datetime import datetime
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a") as f:
        f.write(f"\n## self-key {datetime.now().isoformat(timespec='seconds')}\n"
                f"- device_mac: {mac or '(pending)'}\n- network_key: {key}\n")


async def _try_orbit_key(email, password, want_mac):
    """Determine whether a usable key is actually obtainable. Returns
    (classification, chosen_device, controllable_devices):
    'key_obtained' | 'auth_failed' | 'no_device_on_account' | 'multiple_devices'.

    'multiple_devices' (login OK, >1 key-bearing timer, no MAC to disambiguate) is NOT
    a failure — the caller presents a picker. The controllable list is returned so the
    caller can select from it without a second login."""
    try:
        devices = await cloud_fetch(email, password)
    except CloudError:                      # AuthError / MFARequired / RateLimited / conn
        return "auth_failed", None, []
    controllable = [d for d in devices if d.get("network_key")]
    want = want_mac.upper() if want_mac else None
    if want:
        chosen = next((d for d in controllable if (d.get("mac") or "").upper() == want), None)
        return ("key_obtained" if chosen else "no_device_on_account", chosen, controllable)
    if len(controllable) == 1:
        return "key_obtained", controllable[0], controllable
    if len(controllable) > 1:
        return "multiple_devices", None, controllable
    return "no_device_on_account", None, controllable


async def onboard_flow(params, gate):
    """Async generator of onboarding Step events (see OnboardGate for human gating).
    params: {mode: 'orbit'|'self', email?, password?, name?, device_mac?, path, secrets_path?}.
    Orchestrates get-key (orbit, with fallback choice, or self) -> reset -> provision ->
    verify -> save. NEVER yields the network key."""
    mode = params.get("mode", "orbit")
    name = params.get("name")
    want_mac = params.get("device_mac")
    path = params.get("path", "config.json")
    secrets_path = params.get("secrets_path")
    stations = 4
    key = None
    key_source = None

    if mode == "self":
        key, key_source = os.urandom(16).hex(), "self"
        _stash_key(key, want_mac, secrets_path)
        yield _step("get_key", "Standalone key", "Using a new self-generated key (no Orbit).",
                    state="done", verified=True)
    elif mode == "reuse":
        key = key_from_existing_config(path)
        if not key:
            yield _step("get_key", "No saved key", "No Orbit key is saved yet — choose “Use my "
                        "Orbit account” or “Standalone” instead.", state="failed")
            return
        key_source = "orbit"
        yield _step("get_key", "Using your saved Orbit key",
                    "Reusing the account key already on this computer — no login needed.",
                    state="done", verified=True)
    else:
        while key is None:
            yield _step("get_key", "Signing in to Orbit", "Fetching your account key…",
                        state="working", expected_wait_s=20)
            klass, chosen, controllable = await _try_orbit_key(
                params.get("email"), params.get("password"), want_mac)
            if klass == "multiple_devices":
                # login worked, but there's more than one timer and no MAC to pick — ask.
                yield _step("get_key", "Signed in to Orbit",
                            "You have more than one timer on your account.",
                            state="done", verified=True)
                opts = [{"value": d.get("mac"), "label": d.get("name") or d.get("mac")}
                        for d in controllable]
                yield _step("pick_device", "Choose which timer to set up",
                            "Your Orbit account has more than one timer. Pick the one to add.",
                            state="waiting_user", options=opts)
                picked = await gate.wait()
                chosen = next((d for d in controllable
                               if (d.get("mac") or "").upper() == (picked or "").upper()), None)
                yield _step("pick_device", "Choose which timer to set up",
                            f"You chose: {chosen.get('name') if chosen else picked}.",
                            state="done", verified=True)
                if chosen:
                    key, key_source = chosen["network_key"], "orbit"
                    want_mac = chosen.get("mac")
                    name = name or chosen.get("name")
                    stations = int(chosen.get("stations") or 4)
                    yield _step("get_key", "Orbit account key", "Got your account key.",
                                state="done", verified=True)
                    break
                continue                    # picked nothing valid — retry the loop
            if klass == "key_obtained":
                key, key_source = chosen["network_key"], "orbit"
                want_mac = want_mac or chosen.get("mac")
                name = name or chosen.get("name")
                stations = int(chosen.get("stations") or 4)
                yield _step("get_key", "Orbit account key", "Got your account key.",
                            state="done", verified=True)
                break
            # login/lookup didn't yield a key -> mark get_key FAILED (not stuck working),
            # then offer the fallback choice.
            reason = ("Login failed — check the email/password (multi-factor accounts aren't "
                      "supported)." if klass == "auth_failed"
                      else "This timer isn't on your Orbit account yet.")
            yield _step("get_key", "Signing in to Orbit", reason, state="failed")
            yield _step("fallback_choice", "Choose a setup method", "Pick how to continue.",
                        state="waiting_user", choices=["orbit_app_first", "self_key"])
            choice = await gate.wait()
            yield _step("fallback_choice", "Choose a setup method",
                        "You chose: " + ("set up in the Orbit app first" if choice == "orbit_app_first"
                                         else "standalone (your own key)") + ".",
                        state="done", verified=True)
            if choice == "orbit_app_first":
                yield _step("app_instructions", "Set it up in the Orbit app first",
                            "Open the Orbit B-Hyve app → Add device → follow its steps to add "
                            "this timer to your account, then press Continue to retry.",
                            state="waiting_user")
                await gate.wait()
                yield _step("app_instructions", "Set it up in the Orbit app first",
                            "Retrying sign-in…", state="done", verified=True)
                continue
            key, key_source = os.urandom(16).hex(), "self"
            _stash_key(key, want_mac, secrets_path)
            yield _step("get_key", "Standalone key",
                        "Using a new self-generated key — the Orbit app won't control this timer; "
                        "your key is saved to secrets/.", state="done", verified=True)

    yield _step("await_reset", "Reset the timer into pairing mode",
                "Turn the dial to OFF, press and hold ~10s until the full display lights, then "
                "release. Keep your phone's Bluetooth OFF. Press Continue when ready.",
                state="waiting_user")
    await gate.wait()
    yield _step("await_reset", "Reset the timer into pairing mode", "Searching for the timer…",
                state="done", verified=True)

    yield _step("provision", "Enrolling the timer",
                "Writing the key and finalizing. The timer briefly drops its link after keying, "
                "so this reconnects on its own — please wait.", state="working", expected_wait_s=90)
    try:
        address, mac, st = await provision_device(key, want_mac=want_mac)
    except ResolveError as e:
        yield _step("provision", "Enrolling the timer", f"Provisioning failed: {e}", state="failed")
        return

    yield _step("verify", "Verifying control",
                f"The timer answered — MAC {mac}, clock {st.clock_str}.", state="done", verified=True)

    device = {"name": name or "B-Hyve XD", "address": address, "network_key": key,
              "mac": mac, "stations": stations, "key_source": key_source}
    write_config(path, device)
    yield _step("save", "Saved and ready",
                f"'{device['name']}' is set up ({key_source} key). You can control it now.",
                state="done", verified=True)


# Provisioning characteristic — reverse-engineered from the Phase B HCI capture
# (SPIKE_provisioning.md): a factory-fresh device accepts the account key as a single
# plaintext write of 0x0100 || key to this characteristic. No pairing/encryption.
PROVISION_CHAR = "00006c76-fe32-4f58-8b78-98e42b2c047f"


async def provision_device(network_key_hex: str, *, want_mac: str | None = None,
                           scan_timeout: float = 90.0, near_rssi: int = -80,
                           tz_offset_sec: int | None = None, max_probe: int = 8,
                           reconnect_attempts: int = 4) -> tuple[str, str, object]:
    """Enroll a FACTORY-FRESH (pairing-mode) B-Hyve XD app-free. TWO-PHASE (the device
    drops the BLE link after keying, like the official app which re-connects):
      A) identify the sole fresh B-Hyve and write 0x0100||key to char 6c76, then release;
      B) reconnect (now keyed), run the finalize sequence (provision_setup, incl. field
         94), and verify by read-back.
    Returns (address, device_mac, DeviceStatus). Raises ResolveError on failure.

    SAFETY: a fresh device has no readable MAC before the write, so we cannot pick the
    right one after the fact. To avoid ever writing the account key to the wrong device,
    we first identify B-Hyve candidates by their fe32 service (NO key write), and REFUSE
    if more than one fresh B-Hyve is present — isolate the target (power off the others).
    Precondition: target factory-reset + in pairing mode, phone Bluetooth OFF, close by.
    """
    from bleak import BleakClient, BleakScanner

    from bhyve_xd import BHyveXD

    key = bytes.fromhex(network_key_hex)
    payload = bytes([0x01, 0x00]) + key
    want = want_mac.upper() if want_mac else None
    tz = tz_offset_sec if tz_offset_sec is not None else host_tz_offset()
    seen: set[str] = set()
    candidates: list = []

    def _cb(dev, adv):
        r = adv.rssi if getattr(adv, "rssi", None) is not None else -999
        if dev.address in seen or r < near_rssi:
            return
        seen.add(dev.address)
        candidates.append(dev)

    # 1) Collect nearby advertisers for a bounded window (no connecting yet).
    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    await asyncio.sleep(min(scan_timeout, 10.0))
    try:
        await scanner.stop()
    except Exception:
        pass

    # 2) Identify B-Hyve candidates by fe32 WITHOUT writing anything; refuse if >1.
    bhyve = []   # (dev, connected client) — hold connections so we can write to the sole one
    for dev in candidates[:max_probe]:
        try:
            client = BleakClient(dev)
            await client.connect()
        except Exception:
            continue
        if any("fe32" in s.uuid.lower() for s in client.services):
            bhyve.append((dev, client))
            if len(bhyve) > 1:
                for _d, c in bhyve:
                    try:
                        await c.disconnect()
                    except Exception:
                        pass
                raise ResolveError(
                    "refusing to provision: more than one fresh B-Hyve is nearby — power off "
                    "or move away all but the target, then retry (won't risk writing the key "
                    "to the wrong device)")
        else:
            try:
                await client.disconnect()
            except Exception:
                pass

    if not bhyve:
        raise ResolveError(
            f"no B-Hyve found within {scan_timeout:.0f}s (saw {len(seen)} device(s)) — is the "
            "target factory-reset and in PAIRING MODE, phone Bluetooth OFF, and close to the Mac?")

    # 3) Exactly one B-Hyve. PHASE A: write the key, then release. The device drops the
    #    BLE link after keying (the app re-connects too), so we do NOT reuse this session.
    dev, client = bhyve[0]
    try:
        await client.write_gatt_char(PROVISION_CHAR, payload, response=True)
    except Exception as err:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise ResolveError(f"key write to {dev.address} failed: {err}") from err
    try:
        await client.disconnect()
    except Exception:
        pass

    # PHASE B: reconnect (device is now keyed + re-advertising) to run the FINALIZE
    # sequence (provision_setup, incl. field 94) and verify by read-back. Retry with
    # backoff because the device may still be settling / re-advertising after keying.
    last = None
    for _attempt in range(reconnect_attempts):
        try:
            addr, mac, st, sess = await catch_device_session(
                network_key_hex, want_mac=want_mac, scan_timeout=min(scan_timeout, 45.0),
                near_rssi=near_rssi, tz_offset_sec=tz)
        except ResolveError as e:
            last = e
            await asyncio.sleep(2.0)
            continue
        try:
            await sess.provision_setup()          # the finalize (field 94 etc.)
            st2 = await sess.read_status()
        finally:
            try:
                await sess.__aexit__(None, None, None)
            except Exception:
                pass
        final = st2 or st
        return addr, (final.device_mac if final and final.device_mac else mac), final

    raise ResolveError(
        f"key was written to {dev.address} but it could not be re-contacted to finalize/"
        f"verify after {reconnect_attempts} attempt(s) ({last}) — power-cycle and retry")


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
