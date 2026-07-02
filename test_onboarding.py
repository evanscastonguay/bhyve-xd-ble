"""Offline tests for onboarding.py — the credential-driven setup module.

Live parts (cloud login, macOS pairing scan) need real creds / hardware; here we
test the PURE logic: response parsing, error mapping, MAC/key formatting, config
write round-trip, and platform detection. No network, no BLE.
"""
import json
import tempfile

import onboarding as O
from bhyve_xd import BHyveXD


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    return ok


def main():
    r = []
    print("onboarding.py offline tests:\n")

    # base64 key -> hex
    r.append(check("_b64_to_hex decodes", O._b64_to_hex("ABEiM0RVZneImaq7zN3u/w=="),
                   "00112233445566778899aabbccddeeff"))
    r.append(check("_b64_to_hex(None) -> None", O._b64_to_hex(None), None))

    # MAC formatting
    r.append(check("_format_mac colons+upper", O._format_mac("446755d87ab9"), "44:67:55:D8:7A:B9"))
    r.append(check("_format_mac bad len -> None", O._format_mac("abc"), None))

    # error mapping (HTTP status -> typed exception class)
    r.append(check("401 -> AuthError", O.error_for_status(401).__class__ is type and O.error_for_status(401) is O.AuthError, True))
    r.append(check("403 -> AuthError", O.error_for_status(403) is O.AuthError, True))
    r.append(check("429 -> RateLimited", O.error_for_status(429) is O.RateLimited, True))
    r.append(check("500 -> CloudError", O.error_for_status(500) is O.CloudError, True))
    r.append(check("200 -> None", O.error_for_status(200), None))

    # parse_devices: join raw device + mesh into {name, mac, network_key, stations}.
    raw_devices = [
        {"id": "d1", "name": "Front", "type": "sprinkler_timer", "mac_address": "446755d87ab9",
         "num_stations": 4, "hardware_version": "HT34A-0001", "firmware_version": "0107", "mesh_id": "m1"},
        {"id": "hub", "name": "Hub", "type": "bridge", "mac_address": "001122334455", "mesh_id": "m1"},
    ]
    meshes = {"m1": {"ble_network_key": "ABEiM0RVZneImaq7zN3u/w=="}}
    parsed = O.parse_devices(raw_devices, meshes)
    r.append(check("parse_devices skips bridge (1 device)", len(parsed), 1))
    r.append(check("parse_devices name", parsed[0]["name"], "Front"))
    r.append(check("parse_devices mac", parsed[0]["mac"], "44:67:55:D8:7A:B9"))
    r.append(check("parse_devices key (hex)", parsed[0]["network_key"], "00112233445566778899aabbccddeeff"))
    r.append(check("parse_devices stations", parsed[0]["stations"], 4))

    # write_config -> loadable by BHyveXD.from_config (round-trip).
    with tempfile.TemporaryDirectory() as td:
        p = f"{td}/config.json"
        O.write_config(parsed_with_addr := [
            {"name": "Front", "address": "44:67:55:D8:7A:B9",
             "network_key": "00112233445566778899aabbccddeeff", "stations": 4}], p)
        loaded = json.load(open(p))
        r.append(check("write_config makes a devices list", isinstance(loaded.get("devices"), list), True))
        dev = BHyveXD.from_config(p)
        r.append(check("written config loads via from_config", dev.address, "44:67:55:D8:7A:B9"))
        r.append(check("written config omits tz (host-derived)", "tz_offset_sec" in loaded["devices"][0], False))

    # platform detection
    r.append(check("current_platform in {macos,linux}", O.current_platform() in ("macos", "linux"), True))

    # error_for_status(400) -> AuthError (was untested)
    r.append(check("400 -> AuthError", O.error_for_status(400) is O.AuthError, True))

    # resolve_address Linux branch returns the MAC untouched (no BLE).
    import asyncio
    linux_addr = asyncio.run(
        O.resolve_address("44:67:55:D8:7A:B9", "00" * 16, platform_name="linux"))
    r.append(check("resolve_address(linux) -> MAC", linux_addr, "44:67:55:D8:7A:B9"))

    # cloud_fetch success path, mocked (no network): login -> devices -> mesh -> parse.
    import sys as _sys, types as _types

    class _Resp:
        def __init__(self, status, data): self.status, self._d = status, data
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def json(self): return self._d

    class _Session:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def post(self, url, **k): return _Resp(200, {"orbit_api_key": "TOK", "user_id": "u1"})
        def get(self, url, **k):
            if url.endswith("/devices"):
                return _Resp(200, [{"id": "d1", "name": "Yard", "type": "sprinkler_timer",
                                    "mac_address": "446755d87ab9", "num_stations": 4,
                                    "hardware_version": "HT34A", "firmware_version": "0107",
                                    "mesh_id": "m1"}])
            return _Resp(200, {"ble_network_key": "ABEiM0RVZneImaq7zN3u/w=="})  # mesh

    fake_aiohttp = _types.ModuleType("aiohttp")
    fake_aiohttp.ClientSession = _Session
    fake_aiohttp.ClientTimeout = lambda **k: None
    _saved = _sys.modules.get("aiohttp")
    _sys.modules["aiohttp"] = fake_aiohttp
    try:
        devs = asyncio.run(O.cloud_fetch("e@x.com", "pw"))
    finally:
        if _saved is not None: _sys.modules["aiohttp"] = _saved
        else: _sys.modules.pop("aiohttp", None)
    r.append(check("cloud_fetch mocked -> 1 device", len(devs), 1))
    r.append(check("cloud_fetch device name", devs[0]["name"], "Yard"))
    r.append(check("cloud_fetch device key (hex)", devs[0]["network_key"],
                   "00112233445566778899aabbccddeeff"))
    r.append(check("cloud_fetch device mac", devs[0]["mac"], "44:67:55:D8:7A:B9"))

    n = sum(r)
    print(f"\n{n}/{len(r)} onboarding checks passed" + ("  ✅" if n == len(r) else "  ❌"))
    return 0 if n == len(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
