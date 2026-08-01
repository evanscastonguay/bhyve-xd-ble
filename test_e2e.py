"""
End-to-end tests — every operation exercised against a FAKE B-Hyve XD device.

No hardware, no BLE stack talking to a radio, no button presses. `FakeTimer`
emulates the HT34A protocol at the byte level: it performs the AES handshake,
decrypts our command frames (reassembling fragmentation), maintains watering
state, and pushes back correctly-encrypted status notifications. We patch
bleak's connect/scan so `BHyveXD.session()` drives the fake instead of a radio.

Because the fake honors the SAME cipher + framing the real device does, a test
that reads `is_watering=True` back proves the whole stack works together:
   arm -> command -> encrypted notification -> counter resync -> parse -> DeviceStatus
and, layered on top, the CLI, the REST API, cloud onboarding, and macOS pairing.

Run:  ./venv/bin/python -m pytest test_e2e.py -q
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import struct
import sys
import time
from types import SimpleNamespace

import pytest

import bhyve_xd as B
from bhyve_xd import (FRAME_MAGIC, MSG_HEADER, BHyveXD, NotABHyveError,
                      _keystream_block, parse_reply)

TEST_KEY = "00112233445566778899aabbccddeeff"
TEST_MAC = "AA:BB:CC:DD:EE:FF"
_MAC_BYTES = bytes.fromhex("aabbccddeeff")

READ_CHAR = "00006c73-fe32-4f58-8b78-98e42b2c047f"
WRITE_CHAR = "00006c72-fe32-4f58-8b78-98e42b2c047f"
AES_CHAR = "00006c71-fe32-4f58-8b78-98e42b2c047f"


@pytest.fixture(autouse=True)
def _no_ble_delays(monkeypatch):
    """The product code sleeps to give a real radio time to settle/reply; the
    fake device answers synchronously, so collapse those waits to keep the suite
    fast (the notification is already queued before read_status wakes)."""
    real = asyncio.sleep

    async def instant(_delay, *a, **k):
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", instant)
    yield


# --------------------------------------------------------------------------- #
# The fake device — the other half of the protocol.
# --------------------------------------------------------------------------- #
class FakeTimer:
    """Byte-level emulation of an HT34A B-Hyve XD over the session cipher."""

    def __init__(self, key_hex: str = TEST_KEY, mac: str = TEST_MAC, *, clock: int = 1_751_000_000):
        self.key = bytes.fromhex(key_hex)
        self.mac_bytes = bytes.fromhex(mac.replace(":", ""))
        self.clock = clock
        self.watering = False
        self.station = None
        self.seconds = None
        self.primed = False          # device ignores actuation until it has been armed
        self.arm_count = 0
        # cipher session state (set at handshake)
        self.iv = None
        self.dev_tx = 0              # counter to DECRYPT our writes
        self.dev_rx = 0              # counter to ENCRYPT its replies
        self._rxbuf = b""
        self._emit = None            # notification callback into the session

    @property
    def run_state(self):
        return 4 if self.watering else 1

    # -- handshake -------------------------------------------------------- #
    def handshake(self, init_tx: bytes) -> bytes:
        rx4 = b"\xd7\xcd\x9c\x6a"                      # device's IV contribution (fixed = deterministic)
        self.iv = rx4 + init_tx[4:12]
        self.dev_tx = struct.unpack("<I", init_tx[12:16])[0]
        self.dev_rx = struct.unpack("<I", init_tx[16:20])[0]
        self._rxbuf = b""
        return rx4 + bytes(16)                          # 20-byte handshake reply (only [:4] is used)

    # -- receive our command writes -------------------------------------- #
    def on_write(self, frame: bytes) -> None:
        ln = frame[1]
        ct = frame[2:2 + ln]
        out = bytearray()
        cc = self.dev_tx
        for i in range(0, len(ct), 16):
            out += bytes(a ^ b for a, b in zip(ct[i:i + 16], _keystream_block(self.key, self.iv, cc)))
            cc = (cc + 1) & 0xFFFFFFFF
        self.dev_tx = cc
        self._rxbuf += bytes(out)
        self._drain()

    def _drain(self) -> None:
        while len(self._rxbuf) >= 5 and self._rxbuf[:4] == MSG_HEADER:
            total = 6 + self._rxbuf[4]                  # header(4)+len(1)+zero(1)+protobuf+crc(2)
            if len(self._rxbuf) < total:
                break
            msg, self._rxbuf = self._rxbuf[:total], self._rxbuf[total:]
            self._process(msg)

    def _process(self, msg: bytes) -> None:
        for fn, wt, v in B.iter_fields(msg[6:-2]):
            if fn == 75:                                # setCurrentTime -> primes + sets clock
                self.primed = True
                self.arm_count += 1
                for sfn, _sw, sv in B.iter_fields(v):
                    if sfn == 1:
                        self.clock = sv
            elif fn == 14:                              # timerMode -> start / stop
                self._actuate(v)
            elif fn == 15:                              # getDeviceStatus -> reply
                self._emit_status()
            # fields 18 (time string), 45 (battery), 20/22/120 (setup) are accepted + ignored

    def _actuate(self, timer_mode: bytes) -> None:
        if not self.primed:
            return                                      # unarmed commands are silently ignored
        station = duration = None
        for fn, _wt, v in B.iter_fields(timer_mode):
            if fn == 2:                                 # manual
                for f2, _w2, v2 in B.iter_fields(v):
                    if f2 == 3:                         # stationInfo
                        for f3, _w3, v3 in B.iter_fields(v2):
                            if f3 == 1:
                                station = v3
                            elif f3 == 2:
                                duration = v3
        if station is None:                             # manual with empty station list -> stop ALL
            self.watering, self.station, self.seconds = False, None, None
        elif duration and duration > 0:                 # start that station
            self.watering, self.station, self.seconds = True, station + 1, duration
        else:                                           # duration 0 -> stop that station
            self.watering, self.station, self.seconds = False, None, None

    # -- push a status notification -------------------------------------- #
    def _status_plaintext(self) -> bytes:
        body = B._fb(1, self.mac_bytes) + B._fv(7, self.clock)
        if self.watering:
            sub = B._fv(1, 4) + B._fb(6, B._fv(7, self.seconds or 0))
        else:
            sub = B._fv(1, 1)
        body += B._fb(16, sub)
        return B._wrap(body)

    def _emit_status(self) -> None:
        if self._emit is None:
            return
        pt = self._status_plaintext()
        ct = bytearray()
        cc = self.dev_rx
        for i in range(0, len(pt), 16):
            ct += bytes(a ^ b for a, b in zip(pt[i:i + 16], _keystream_block(self.key, self.iv, cc)))
            cc = (cc + 1) & 0xFFFFFFFF
        self.dev_rx = cc
        trailer = (sum(pt) + FRAME_MAGIC + len(pt)) & 0xFFFF   # not validated by the reader
        self._emit(bytes([FRAME_MAGIC, len(ct)]) + bytes(ct) + struct.pack("<H", trailer))


class FakeClient:
    """Implements the slice of the BleakClient API that _Session uses."""

    def __init__(self, timer: FakeTimer | None):
        self.timer = timer
        self.is_connected = True
        self.mtu_size = 515

    @property
    def services(self):
        uuid = READ_CHAR if self.timer is not None else "0000180f-0000-1000-8000-00805f9b34fb"
        return [SimpleNamespace(uuid=uuid)]

    async def start_notify(self, char, cb):
        self.timer._emit = lambda frame: cb(None, frame)

    async def stop_notify(self, char):
        pass

    async def read_gatt_char(self, char):
        return self.timer._last_rx

    async def write_gatt_char(self, char, data, response=True):
        data = bytes(data)
        if char == AES_CHAR:
            self.timer._last_rx = self.timer.handshake(data)
        elif char == WRITE_CHAR:
            self.timer.on_write(data)

    async def disconnect(self):
        self.is_connected = False


# --------------------------------------------------------------------------- #
# Patching helpers — make BHyveXD.session() drive a FakeClient.
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def fake_ble(resolver):
    """Patch bleak connect/scan so establish_connection(...) yields FakeClient(resolver(address))."""
    import bleak
    import bleak_retry_connector

    async def fake_find(address, timeout=0.0):
        return SimpleNamespace(address=address, name="ASR")

    async def fake_establish(cls, ble, address, max_attempts=3, **kw):
        return FakeClient(resolver(address))

    orig_find = bleak.BleakScanner.find_device_by_address
    orig_est = bleak_retry_connector.establish_connection
    bleak.BleakScanner.find_device_by_address = staticmethod(fake_find)
    bleak_retry_connector.establish_connection = fake_establish
    try:
        yield
    finally:
        bleak.BleakScanner.find_device_by_address = orig_find
        bleak_retry_connector.establish_connection = orig_est


@contextlib.contextmanager
def one_device(timer: FakeTimer):
    """A single fake timer answering at any address."""
    with fake_ble(lambda _addr: timer):
        yield timer


def make_device(timer: FakeTimer) -> BHyveXD:
    return BHyveXD("FAKE-ADDR", TEST_KEY, tz_offset_sec=0, name="Fake XD", stations=4)


# --------------------------------------------------------------------------- #
# Control operations (the core end-to-end path)
# --------------------------------------------------------------------------- #
def test_status_reads_clock_and_idle():
    t = FakeTimer()
    with one_device(t):
        st = asyncio.run(make_device(t).status())
    assert st.device_mac == TEST_MAC
    assert st.is_watering is False and st.run_state == 1
    assert st.device_time == t.clock


def test_start_confirms_watering():
    t = FakeTimer()
    with one_device(t):
        st = asyncio.run(make_device(t).start(1, 300))
    assert st.is_watering is True
    assert st.run_state == 4
    assert st.seconds_remaining == 300
    # device really is watering station 1
    assert t.watering and t.station == 1 and t.seconds == 300


def test_stop_all_confirms_idle():
    t = FakeTimer(); t.primed = True; t.watering = True; t.station = 1; t.seconds = 120
    with one_device(t):
        st = asyncio.run(make_device(t).stop())
    assert st.is_watering is False and st.run_state == 1
    assert t.watering is False


def test_stop_single_zone_confirms_idle():
    t = FakeTimer()
    with one_device(t):
        dev = make_device(t)
        started = asyncio.run(dev.start(2, 600))
        assert started.is_watering and t.station == 2
        stopped = asyncio.run(dev.stop(2))
    assert stopped.is_watering is False
    assert t.watering is False


def test_sync_clock_sets_device_time_to_now():
    t = FakeTimer(clock=1)
    before = int(time.time())
    with one_device(t):
        st = asyncio.run(make_device(t).sync_clock())
    assert st.device_time is not None and st.device_time >= before
    assert t.primed is True                            # arm() ran


def test_arming_is_required_for_actuation():
    """A raw start WITHOUT arm() is ignored by the device; WITH arm() it works.
    Proves the high-level path actually arms before commanding."""
    t = FakeTimer()
    with one_device(t):
        dev = make_device(t)

        async def unarmed():
            async with dev.session() as s:
                await s.start_zone(1, 300)             # no arm() first
                return await s.read_status()

        st = asyncio.run(unarmed())
        assert st.is_watering is False                 # ignored
        assert t.watering is False

        st2 = asyncio.run(dev.start(1, 300))           # high-level = arm + start
        assert st2.is_watering is True


def test_device_mac_survives_counter_resync():
    """arm() emits one reply (advancing the device's tx counter) that we never
    decode; read_status must brute-force resync to still recover the MAC."""
    t = FakeTimer()
    with one_device(t):
        st = asyncio.run(make_device(t).status())
    assert st.device_mac == TEST_MAC


def test_session_adopts_preconnected_client():
    """A session can run the full protocol on an ALREADY-CONNECTED client, with no
    scan and no establish_connection. This is the primitive the live-catch flow
    needs for a rotating-address timer: we grab the live advertisement, connect
    once, then hand that client to the session. Note there is deliberately NO
    fake_ble patching here — if the session tried to scan/connect it would fail."""
    t = FakeTimer()
    client = FakeClient(t)                       # already "connected"
    dev = make_device(t)

    async def run():
        async with dev.session(client=client) as s:
            await s.arm()
            return await s.read_status()

    st = asyncio.run(run())
    assert st.device_mac == TEST_MAC
    assert st.is_watering is False
    assert client.is_connected is False          # exiting the session releases the connection


def test_adopted_session_rejects_non_bhyve():
    """Adopting a connected client that is NOT a B-Hyve (no fe32) still fast-rejects."""
    client = FakeClient(None)                    # None timer -> non-fe32 services
    dev = make_device(FakeTimer())

    async def run():
        async with dev.session(client=client) as s:
            return s

    with pytest.raises(NotABHyveError):
        asyncio.run(run())


# --------------------------------------------------------------------------- #
# REST API (FastAPI handlers) end-to-end over the fake device
# --------------------------------------------------------------------------- #
def _patch_server_device(monkeypatch, timer):
    import server
    monkeypatch.setattr(server, "_device", lambda: make_device(timer))
    return server


def test_api_status(monkeypatch):
    t = FakeTimer()
    server = _patch_server_device(monkeypatch, t)
    with one_device(t):
        body = asyncio.run(server.status())
    assert body["is_watering"] is False
    assert body["run_state"] == 1
    assert set(body) >= {"clock", "device_time", "is_watering", "run_state", "seconds_remaining"}


def test_api_start_and_stop_zone(monkeypatch):
    t = FakeTimer()
    server = _patch_server_device(monkeypatch, t)
    with one_device(t):
        started = asyncio.run(server.start(1, server.StartBody(minutes=2)))
        assert started["confirmed_watering"] is True
        assert started["zone"] == 1 and started["seconds_remaining"] == 120
        stopped = asyncio.run(server.stop_zone(1))
        assert stopped["confirmed_idle"] is True


def test_api_stop_all(monkeypatch):
    t = FakeTimer(); t.primed = True; t.watering = True; t.station = 3; t.seconds = 60
    server = _patch_server_device(monkeypatch, t)
    with one_device(t):
        body = asyncio.run(server.stop_all())
    assert body["confirmed_idle"] is True


def test_api_rejects_bad_zone(monkeypatch):
    from fastapi import HTTPException
    t = FakeTimer()
    server = _patch_server_device(monkeypatch, t)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.start(9, server.StartBody(minutes=1)))
    assert exc.value.status_code == 400


def test_api_ble_error_becomes_503(monkeypatch):
    from fastapi import HTTPException
    t = FakeTimer()
    server = _patch_server_device(monkeypatch, t)

    async def boom(_d):
        raise RuntimeError("radio gone")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(server._run(boom))
    assert exc.value.status_code == 503


# --------------------------------------------------------------------------- #
# CLI end-to-end over the fake device
# --------------------------------------------------------------------------- #
def test_cli_start_and_stop(monkeypatch, capsys):
    import cli
    t = FakeTimer()
    monkeypatch.setattr(cli.BHyveXD, "from_config", classmethod(lambda cls, *a, **k: make_device(t)))
    with one_device(t):
        asyncio.run(cli.cmd_start(1, 300))
        out = capsys.readouterr().out
        assert "START zone 1" in out and "OK" in out
        assert t.watering is True

        asyncio.run(cli.cmd_stop(1))
        out = capsys.readouterr().out
        assert "OK (idle)" in out
        assert t.watering is False


# --------------------------------------------------------------------------- #
# macOS address resolution / pairing end-to-end
# --------------------------------------------------------------------------- #
def test_resolve_macos_matches_by_mac(monkeypatch):
    """A decoy non-B-Hyve at strong RSSI is probed first and fast-rejected; the
    real timer (matching MAC) is found and its UUID returned."""
    import bleak
    import onboarding as O

    real = FakeTimer(mac=TEST_MAC)
    registry = {"UUID-REAL": real, "UUID-DECOY": None}   # None -> non-fe32 device

    async def fake_discover(timeout=0.0, return_adv=False):
        return {
            "UUID-DECOY": (SimpleNamespace(address="UUID-DECOY", name="TV"),
                           SimpleNamespace(rssi=-40, service_uuids=[])),
            "UUID-REAL": (SimpleNamespace(address="UUID-REAL", name="ASR"),
                          SimpleNamespace(rssi=-70, service_uuids=[])),
        }

    monkeypatch.setattr(bleak.BleakScanner, "discover", staticmethod(fake_discover))
    with fake_ble(lambda addr: registry.get(addr)):
        got = asyncio.run(O.resolve_address(TEST_MAC, TEST_KEY, platform_name="macos"))
    assert got == "UUID-REAL"


def test_resolve_linux_returns_mac():
    import onboarding as O
    got = asyncio.run(O.resolve_address(TEST_MAC, TEST_KEY, platform_name="linux"))
    assert got == TEST_MAC


def test_resolve_raises_when_absent(monkeypatch):
    import bleak
    import onboarding as O

    async def fake_discover(timeout=0.0, return_adv=False):
        return {"UUID-DECOY": (SimpleNamespace(address="UUID-DECOY", name="TV"),
                               SimpleNamespace(rssi=-40, service_uuids=[]))}

    monkeypatch.setattr(bleak.BleakScanner, "discover", staticmethod(fake_discover))
    with fake_ble(lambda _addr: None):                  # nothing is a B-Hyve
        with pytest.raises(O.ResolveError):
            asyncio.run(O.resolve_address(TEST_MAC, TEST_KEY, platform_name="macos"))


# --------------------------------------------------------------------------- #
# Cloud onboarding end-to-end (mocked aiohttp transport)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._data


class _Session:
    def __init__(self, routes):
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _resp(self, url):
        for suffix, rv in self._routes.items():
            if suffix in url:
                return _Resp(*rv)
        return _Resp(404, {})

    def post(self, url, **kw):
        return self._resp(url)

    def get(self, url, **kw):
        return self._resp(url)


def _fake_aiohttp(routes):
    return SimpleNamespace(
        ClientSession=lambda *a, **k: _Session(routes),
        ClientTimeout=lambda *a, **k: None,
        ClientError=type("ClientError", (Exception,), {}),
    )


def test_cloud_fetch_joins_devices_and_keys(monkeypatch):
    import onboarding as O
    key_b64 = base64.b64encode(bytes.fromhex(TEST_KEY)).decode()
    routes = {
        "/session": (200, {"orbit_api_key": "tok", "user_id": "u1"}),
        "/devices": (200, [
            {"name": "Front", "type": "sprinkler_timer", "mac_address": "aabbccddeeff",
             "num_stations": 4, "hardware_version": "HT34A", "firmware_version": "0107",
             "mesh_id": "m1"},
            {"name": "Hub", "type": "bridge", "mesh_id": "m1"},
        ]),
        "/meshes/m1": (200, {"ble_network_key": key_b64}),
    }
    monkeypatch.setitem(sys.modules, "aiohttp", _fake_aiohttp(routes))
    devices = asyncio.run(O.cloud_fetch("me@example.com", "pw"))
    assert len(devices) == 1                            # bridge dropped
    assert devices[0]["name"] == "Front"
    assert devices[0]["mac"] == TEST_MAC
    assert devices[0]["stations"] == 4
    assert devices[0]["network_key"] == TEST_KEY


def test_cloud_fetch_maps_bad_login_to_autherror(monkeypatch):
    import onboarding as O
    monkeypatch.setitem(sys.modules, "aiohttp", _fake_aiohttp({"/session": (401, {})}))
    with pytest.raises(O.AuthError):
        asyncio.run(O.cloud_fetch("me@example.com", "wrong"))


def test_cloud_fetch_maps_rate_limit(monkeypatch):
    import onboarding as O
    monkeypatch.setitem(sys.modules, "aiohttp", _fake_aiohttp({"/session": (429, {})}))
    with pytest.raises(O.RateLimited):
        asyncio.run(O.cloud_fetch("me@example.com", "pw"))
