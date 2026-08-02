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
PROVISION_CHAR = "00006c76-fe32-4f58-8b78-98e42b2c047f"   # key-write char (from Phase B capture)


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

    def __init__(self, key_hex: str | None = TEST_KEY, mac: str = TEST_MAC, *, clock: int = 1_751_000_000):
        self.key = bytes.fromhex(key_hex) if key_hex else None   # None = factory-fresh (unprovisioned)
        self._provision_write = None                              # last value written to 6c76
        self.persisted = key_hex is not None                     # key durably saved? set by field 94
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
            elif fn == 94:                              # station config -> finalizes enrollment
                self.persisted = True
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

    async def connect(self):
        self.is_connected = True
        return True

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
        elif char == PROVISION_CHAR:                 # 0x0100 || 16-byte key
            self.timer._provision_write = data
            self.timer.key = data[2:]                # device now holds the account key

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
    monkeypatch.setattr(server, "_device", lambda *a, **k: make_device(timer))
    return server


def test_api_lists_devices(monkeypatch, tmp_path):
    """GET /api/devices returns each configured device's index/name/stations."""
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [
        {"name": "Old", "address": "A", "network_key": TEST_KEY, "stations": 4},
        {"name": "New Timer", "address": "B", "network_key": TEST_KEY, "stations": 6},
    ]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    got = asyncio.run(server.devices())
    assert got == [
        {"index": 0, "name": "Old", "stations": 4},
        {"index": 1, "name": "New Timer", "stations": 6},
    ]


def test_api_status_selects_device(monkeypatch):
    """A ?device= selection is forwarded to from_config for the BLE call."""
    import server
    seen = {}

    def fake_from_config(cls, path=None, device=None):
        seen["device"] = device
        return make_device(FakeTimer())

    monkeypatch.setattr(server.BHyveXD, "from_config", classmethod(fake_from_config))
    with one_device(FakeTimer()):
        asyncio.run(server.status(device="New Timer"))
    assert seen["device"] == "New Timer"


def test_api_onboard_state_reports_saved_key(monkeypatch, tmp_path):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "A", "network_key": TEST_KEY}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    assert asyncio.run(server.onboard_state())["has_key"] is True
    cfg.write_text(json.dumps({"devices": []}))
    assert asyncio.run(server.onboard_state())["has_key"] is False


def test_api_onboard_register_reuses_key(monkeypatch, tmp_path):
    """The web register endpoint reuses the saved key (no cloud), catches the timer,
    writes config, and does NOT leak the key back to the browser."""
    import json
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [
        {"name": "Old", "address": "OLD", "network_key": TEST_KEY,
         "mac": "AA:BB:CC:DD:EE:FF", "stations": 4}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))

    async def fake_catch(key, *, want_mac=None, **kw):
        assert key == TEST_KEY
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    def no_cloud(*a, **k):
        raise AssertionError("cloud_fetch must not be called when a key exists")

    monkeypatch.setattr(O, "catch_device", fake_catch)
    monkeypatch.setattr(O, "cloud_fetch", no_cloud)
    res = asyncio.run(server.onboard_register(server.OnboardBody(name="New Timer")))
    assert res["registered"]["mac"] == "AA:BB:CC:DD:EE:01"
    assert "network_key" not in res["registered"]          # key never leaves the server
    data = json.loads(cfg.read_text())
    assert [d["name"] for d in data["devices"]] == ["Old", "New Timer"]
    assert data["devices"][1]["network_key"] == TEST_KEY   # reused, written


def test_api_onboard_register_maps_catch_failure_to_504(monkeypatch, tmp_path):
    import json
    from fastapi import HTTPException
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "Old", "network_key": TEST_KEY}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))

    async def boom(key, *, want_mac=None, **kw):
        raise O.ResolveError("nothing caught — phone Bluetooth OFF?")

    monkeypatch.setattr(O, "catch_device", boom)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.onboard_register(server.OnboardBody(name="New Timer")))
    assert exc.value.status_code == 504
    assert len(json.loads(cfg.read_text())["devices"]) == 1   # nothing written


def test_api_onboard_register_cloud_path(monkeypatch, tmp_path):
    """Web register with no saved key logs in, fetches the key, catches, and writes it."""
    import json
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": []}))          # no key
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    FETCHED = "aabbccddeeff00112233445566778899"

    async def fake_cloud(email, password):
        return [{"name": "Yard", "mac": "AA:BB:CC:DD:EE:01", "network_key": FETCHED, "stations": 4}]

    async def fake_catch(key, *, want_mac=None, **kw):
        assert key == FETCHED
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    monkeypatch.setattr(O, "cloud_fetch", fake_cloud)
    monkeypatch.setattr(O, "catch_device", fake_catch)
    res = asyncio.run(server.onboard_register(
        server.OnboardBody(name="Yard", email="me@x.com", password="pw")))
    assert res["registered"]["mac"] == "AA:BB:CC:DD:EE:01"
    assert "network_key" not in res["registered"]
    assert json.loads(cfg.read_text())["devices"][0]["network_key"] == FETCHED


def test_api_onboard_register_multi_device_409(monkeypatch, tmp_path):
    import json
    from fastapi import HTTPException
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": []}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))

    async def fake_cloud(email, password):
        return [{"name": "A", "mac": "AA", "network_key": TEST_KEY, "stations": 4},
                {"name": "B", "mac": "BB", "network_key": TEST_KEY, "stations": 4}]

    monkeypatch.setattr(O, "cloud_fetch", fake_cloud)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.onboard_register(
            server.OnboardBody(email="me@x.com", password="pw")))   # no device_mac
    assert exc.value.status_code == 409


def test_api_onboard_register_needs_creds_without_saved_key(monkeypatch, tmp_path):
    import json
    from fastapi import HTTPException
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": []}))          # no key yet
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.onboard_register(server.OnboardBody(name="First")))
    assert exc.value.status_code == 400


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
def test_cli_extract_device_selection():
    """--device is pulled out of argv (name or numeric index) and the rest of the
    args are left intact for the command dispatcher."""
    import cli
    assert cli._extract_device(["status"]) == (None, ["status"])
    assert cli._extract_device(["status", "--device", "New Timer"]) == ("New Timer", ["status"])
    assert cli._extract_device(["--device=2", "start", "1", "300"]) == (2, ["start", "1", "300"])
    assert cli._extract_device(["stop", "--device", "0"]) == (0, ["stop"])


def test_cli_status_targets_selected_device(monkeypatch, capsys):
    """cmd_status(device=...) forwards the selection to from_config."""
    import cli
    seen = {}

    def fake_from_config(cls, path="config.json", device=None):
        seen["device"] = device
        return make_device(FakeTimer())

    monkeypatch.setattr(cli.BHyveXD, "from_config", classmethod(fake_from_config))
    t = FakeTimer()
    with one_device(t):
        asyncio.run(cli.cmd_status(device="New Timer"))
    assert seen["device"] == "New Timer"


def test_cli_parse_register_args():
    import cli
    assert cli._parse_register([]) == (None, None, None, False)
    assert cli._parse_register(["me@x.com"]) == ("me@x.com", None, None, False)
    assert cli._parse_register(["--name", "New Timer", "--device-mac", "AA:BB"]) == \
        (None, "New Timer", "AA:BB", False)
    assert cli._parse_register(["me@x.com", "--name=Yard", "--show-key"]) == \
        ("me@x.com", "Yard", None, True)


def test_cli_register_reuses_key_without_cloud(tmp_path, monkeypatch, capsys):
    """Registering a 2nd timer reuses the account key already in config — NO cloud
    login — catches the device, and appends it with the reused key."""
    import json
    import cli
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"devices": [
        {"name": "Old", "address": "UUID-OLD", "network_key": TEST_KEY,
         "mac": "AA:BB:CC:DD:EE:FF", "stations": 4}]}))

    async def fake_catch(key, *, want_mac=None, **kw):
        assert key == TEST_KEY                     # the reused account key
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    def no_cloud(*a, **k):
        raise AssertionError("cloud_fetch must NOT be called when a key already exists")

    monkeypatch.setattr(O, "catch_device", fake_catch)
    monkeypatch.setattr(O, "cloud_fetch", no_cloud)
    asyncio.run(cli.cmd_register(name="New Timer", path=str(p), ask_prompt=False))

    data = json.loads(p.read_text())
    assert [d["name"] for d in data["devices"]] == ["Old", "New Timer"]
    new = data["devices"][1]
    assert new["address"] == "UUID-NEW"
    assert new["mac"] == "AA:BB:CC:DD:EE:01"
    assert new["network_key"] == TEST_KEY          # reused, not re-fetched


def test_cli_register_reports_catch_failure(tmp_path, monkeypatch, capsys):
    """A failed catch is reported (with guidance) and nothing is written."""
    import json
    import cli
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"devices": [
        {"name": "Old", "network_key": TEST_KEY, "mac": "AA:BB:CC:DD:EE:FF"}]}))

    async def boom(key, *, want_mac=None, **kw):
        raise O.ResolveError("nothing caught — phone Bluetooth OFF?")

    monkeypatch.setattr(O, "catch_device", boom)
    asyncio.run(cli.cmd_register(name="New Timer", path=str(p), ask_prompt=False))
    out = capsys.readouterr().out
    assert "failed" in out.lower()
    # config untouched (still just the one device)
    assert len(json.loads(p.read_text())["devices"]) == 1


def test_cli_choose_device():
    import cli
    devs = [{"mac": "AA", "name": "Front"}, {"mac": "BB", "name": "Back"}]
    assert cli._choose_device(devs, choose_fn=lambda _p: "1") is devs[1]
    assert cli._choose_device(devs, choose_fn=lambda _p: "0") is devs[0]
    assert cli._choose_device(devs, choose_fn=lambda _p: "9") is None      # out of range
    assert cli._choose_device(devs, choose_fn=lambda _p: "x") is None      # non-numeric
    assert cli._choose_device(devs, choose_fn=lambda _p: "") is None       # cancel


def test_cli_register_cloud_first_writes_fetched_key(tmp_path, monkeypatch):
    """First run: NO config yet. Log in to the cloud, fetch the key, catch the timer,
    and write a config entry whose network_key is the FETCHED key."""
    import getpass
    import json
    import cli
    import onboarding as O
    p = tmp_path / "config.json"                       # absent -> first run

    FETCHED_KEY = "aabbccddeeff00112233445566778899"

    async def fake_cloud(email, password):
        assert email == "me@x.com" and password == "pw"
        return [{"name": "Front", "mac": "AA:BB:CC:DD:EE:01",
                 "network_key": FETCHED_KEY, "stations": 4}]

    async def fake_catch(key, *, want_mac=None, **kw):
        assert key == FETCHED_KEY                       # the fetched key is used to catch
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    monkeypatch.setattr(O, "cloud_fetch", fake_cloud)
    monkeypatch.setattr(O, "catch_device", fake_catch)
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "pw")
    asyncio.run(cli.cmd_register("me@x.com", path=str(p), ask_prompt=False))

    data = json.loads(p.read_text())
    assert len(data["devices"]) == 1
    d = data["devices"][0]
    assert d["mac"] == "AA:BB:CC:DD:EE:01"
    assert d["address"] == "UUID-NEW"
    assert d["network_key"] == FETCHED_KEY             # fetched, not typed in


def test_cli_register_cloud_multi_device_uses_chooser(tmp_path, monkeypatch):
    """Several devices on the account + no --device-mac -> the chooser picks one."""
    import getpass
    import json
    import cli
    import onboarding as O
    p = tmp_path / "config.json"

    async def fake_cloud(email, password):
        return [{"name": "Front", "mac": "AA:BB:CC:DD:EE:FF", "network_key": TEST_KEY, "stations": 4},
                {"name": "Back", "mac": "AA:BB:CC:DD:EE:01", "network_key": TEST_KEY, "stations": 6}]

    captured = {}

    async def fake_catch(key, *, want_mac=None, **kw):
        captured["want_mac"] = want_mac
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    monkeypatch.setattr(O, "cloud_fetch", fake_cloud)
    monkeypatch.setattr(O, "catch_device", fake_catch)
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "pw")
    asyncio.run(cli.cmd_register("me@x.com", path=str(p), ask_prompt=False,
                                 choose_fn=lambda _p: "1"))     # pick "Back"

    assert captured["want_mac"] == "AA:BB:CC:DD:EE:01"          # chose device [1]
    data = json.loads(p.read_text())
    assert [d["name"] for d in data["devices"]] == ["Back"]
    assert data["devices"][0]["stations"] == 6


def test_cli_register_cloud_auth_error_surfaced(tmp_path, monkeypatch, capsys):
    import getpass
    import cli
    import onboarding as O
    p = tmp_path / "config.json"

    async def bad_login(email, password):
        raise O.AuthError("rejected (HTTP 401) — check email/password")

    monkeypatch.setattr(O, "cloud_fetch", bad_login)
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "wrong")
    asyncio.run(cli.cmd_register("me@x.com", path=str(p), ask_prompt=False))
    out = capsys.readouterr().out.lower()
    assert "login failed" in out or "401" in out
    assert not p.exists()                                        # nothing written


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


@contextlib.contextmanager
def fake_catch_ble(adverts, resolver):
    """Patch bleak for connect-on-detection: a fake BleakScanner emits `adverts`
    (each (BLEDevice, adv)) to its detection callback on start(), and BleakClient(dev)
    yields FakeClient(resolver(dev.address)). No find_device_by_address / rescan —
    so a rotating address can't slip away between scan and connect."""
    import bleak

    class FakeScanner:
        def __init__(self, detection_callback=None):
            self._cb = detection_callback

        async def start(self):
            for dev, adv in adverts:
                if self._cb:
                    self._cb(dev, adv)

        async def stop(self):
            pass

    orig_scanner, orig_client = bleak.BleakScanner, bleak.BleakClient
    bleak.BleakScanner = FakeScanner
    bleak.BleakClient = lambda dev: FakeClient(resolver(dev.address))
    try:
        yield
    finally:
        bleak.BleakScanner, bleak.BleakClient = orig_scanner, orig_client


def _adv(address, name, rssi):
    return (SimpleNamespace(address=address, name=name), SimpleNamespace(rssi=rssi))


def test_catch_device_finds_bhyve_by_advertisement():
    """A decoy (no fe32) is probed + rejected; the timer is caught by connecting to
    its live advertisement, and its MAC is read back over BLE."""
    import onboarding as O
    timer = FakeTimer(mac=TEST_MAC)
    adverts = [_adv("UUID-DECOY", "TV", -40), _adv("UUID-ROT-1", "ASR", -60)]
    resolver = lambda a: {"UUID-ROT-1": timer}.get(a)   # decoy -> None (non-fe32)
    with fake_catch_ble(adverts, resolver):
        addr, mac, st = asyncio.run(O.catch_device(TEST_KEY, scan_timeout=2.0))
    assert addr == "UUID-ROT-1"
    assert mac == TEST_MAC
    assert st.device_mac == TEST_MAC


def test_catch_device_matches_wanted_mac_among_several():
    """With want_mac set, a non-matching B-Hyve is skipped and the right one returned."""
    import onboarding as O
    other = FakeTimer(mac="AA:BB:CC:DD:EE:03")
    want = FakeTimer(mac=TEST_MAC)
    adverts = [_adv("UUID-OTHER", "ASR", -50), _adv("UUID-WANT", "ASR", -70)]
    resolver = lambda a: {"UUID-OTHER": other, "UUID-WANT": want}.get(a)
    with fake_catch_ble(adverts, resolver):
        addr, mac, _st = asyncio.run(O.catch_device(TEST_KEY, want_mac=TEST_MAC, scan_timeout=2.0))
    assert addr == "UUID-WANT" and mac == TEST_MAC


def test_catch_device_session_stays_open_for_live_control():
    """catch_device_session returns an OPEN, armed session so a caller (e.g. bhyve_lab)
    can issue several commands on the one caught connection, then close it."""
    import onboarding as O
    timer = FakeTimer(mac=TEST_MAC)
    adverts = [_adv("UUID-ROT-1", "ASR", -60)]
    resolver = lambda a: {"UUID-ROT-1": timer}.get(a)

    async def run():
        addr, mac, _st, sess = await O.catch_device_session(TEST_KEY, scan_timeout=2.0)
        await sess.start_zone(1, 60)          # session still open -> command works
        st2 = await sess.read_status()
        await sess.__aexit__(None, None, None)
        return addr, mac, st2

    with fake_catch_ble(adverts, resolver):
        addr, mac, st2 = asyncio.run(run())
    assert addr == "UUID-ROT-1" and mac == TEST_MAC
    assert st2.is_watering is True and timer.watering is True


def test_provision_device_writes_key_then_verifies():
    """App-free enrollment: catch a FACTORY-FRESH device (no key), write 0x0100||key to
    the 6c76 characteristic, then verify with our normal handshake/read-back."""
    import onboarding as O
    t = FakeTimer(mac=TEST_MAC, key_hex=None)      # factory-fresh: no key yet
    assert t.key is None
    adverts = [_adv("UUID-FRESH", "", -55)]
    with fake_catch_ble(adverts, lambda _a: t):
        addr, mac, st = asyncio.run(O.provision_device(TEST_KEY, scan_timeout=2.0))
    assert t.key == bytes.fromhex(TEST_KEY)                 # key written onto the device
    assert t._provision_write[:2] == b"\x01\x00"           # the 0x0100 prefix
    assert t._provision_write[2:] == bytes.fromhex(TEST_KEY)
    assert addr == "UUID-FRESH" and mac == TEST_MAC
    assert st.device_mac == TEST_MAC                        # verified live after the write


def test_provision_device_persists_via_station_config():
    """The fix: provision_device must send the app's enrollment setup (incl. the station-
    config, field 94) that PERSISTS the key — not just a plain arm(). The fake only marks
    itself 'persisted' when it receives field 94."""
    import onboarding as O
    t = FakeTimer(mac=TEST_MAC, key_hex=None)          # factory-fresh, not persisted
    assert t.persisted is False
    with fake_catch_ble([_adv("UUID-FRESH", "", -55)], lambda _a: t):
        addr, mac, st = asyncio.run(O.provision_device(TEST_KEY, scan_timeout=2.0))
    assert t.key == bytes.fromhex(TEST_KEY)
    assert t.persisted is True                          # finalize step was sent
    assert mac == TEST_MAC


def test_provision_device_times_out_when_no_bhyve():
    import onboarding as O
    with fake_catch_ble([_adv("UUID-DECOY", "TV", -40)], lambda _a: None):
        with pytest.raises(O.ResolveError):
            asyncio.run(O.provision_device(TEST_KEY, scan_timeout=0.3))


def test_cli_register_provision_uses_provision_device(tmp_path, monkeypatch):
    """`register --provision` writes the key to a fresh device (provision_device) rather
    than just catching an already-enrolled one (catch_device)."""
    import json
    import cli
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"devices": [
        {"name": "Old", "address": "X", "network_key": TEST_KEY, "mac": "AA", "stations": 4}]}))
    called = {}

    async def fake_provision(key, *, want_mac=None, **kw):
        called["key"] = key
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    async def must_not_catch(*a, **k):
        raise AssertionError("provision path must not call catch_device")

    monkeypatch.setattr(O, "provision_device", fake_provision)
    monkeypatch.setattr(O, "catch_device", must_not_catch)
    asyncio.run(cli.cmd_register(name="Fresh", path=str(p), ask_prompt=False, provision=True))
    assert called["key"] == TEST_KEY
    data = json.loads(p.read_text())
    assert [d["name"] for d in data["devices"]] == ["Old", "Fresh"]


def test_catch_device_times_out_when_no_bhyve():
    import onboarding as O
    adverts = [_adv("UUID-DECOY", "TV", -40)]
    with fake_catch_ble(adverts, lambda _a: None):
        with pytest.raises(O.ResolveError):
            asyncio.run(O.catch_device(TEST_KEY, scan_timeout=0.3))


def test_write_config_creates_new_file(tmp_path):
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    dev = {"name": "New", "address": "UUID-A", "network_key": TEST_KEY, "mac": TEST_MAC, "stations": 4}
    O.write_config(str(p), dev)
    data = json.loads(p.read_text())
    assert data["devices"] == [dev]


def test_write_config_appends_distinct_device(tmp_path):
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    O.write_config(str(p), {"name": "A", "address": "UUID-A", "network_key": TEST_KEY,
                            "mac": "AA:BB:CC:DD:EE:FF", "stations": 4})
    O.write_config(str(p), {"name": "B", "address": "UUID-B", "network_key": TEST_KEY,
                            "mac": "AA:BB:CC:DD:EE:01", "stations": 6})
    data = json.loads(p.read_text())
    assert [d["name"] for d in data["devices"]] == ["A", "B"]


def test_write_config_updates_in_place_by_mac(tmp_path):
    """Re-registering the same MAC (drifted address) updates in place — no duplicate,
    and pre-existing fields are preserved."""
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    O.write_config(str(p), {"name": "New Timer", "address": "OLD-UUID", "network_key": TEST_KEY,
                            "mac": TEST_MAC, "stations": 4, "tz_offset_sec": -14400})
    O.write_config(str(p), {"name": "New Timer", "address": "NEW-UUID", "network_key": TEST_KEY,
                            "mac": TEST_MAC, "stations": 4})
    data = json.loads(p.read_text())
    assert len(data["devices"]) == 1
    assert data["devices"][0]["address"] == "NEW-UUID"
    assert data["devices"][0]["tz_offset_sec"] == -14400   # prior field preserved


def test_write_config_is_atomic_no_temp_left(tmp_path):
    import onboarding as O
    p = tmp_path / "config.json"
    O.write_config(str(p), {"name": "A", "address": "UUID-A", "network_key": TEST_KEY,
                            "mac": TEST_MAC, "stations": 4})
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "config.json"]
    assert leftovers == []


def test_write_config_refuses_to_clobber_malformed(tmp_path):
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text("{ this is not json ")
    with pytest.raises(ValueError):
        O.write_config(str(p), {"name": "A", "address": "UUID-A", "network_key": TEST_KEY,
                                "mac": TEST_MAC, "stations": 4})
    assert p.read_text() == "{ this is not json "   # original left intact


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
