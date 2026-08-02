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
        self.programs = {}           # program_id -> stored (schedule persists on device)
        self.active_mask = 0         # setActivePrograms bitmask
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
            elif fn == 19:                              # setProgramSchedule -> define/delete
                sub = {sfn: sv for sfn, _sw, sv in B.iter_fields(v)}
                pid = sub.get(1)
                if pid is not None:
                    if 8 in sub:                        # has start times -> define; else -> delete
                        self.programs[pid] = v
                    else:
                        self.programs.pop(pid, None)
            elif fn == 20:                              # setActivePrograms -> enable bitmask
                for sfn, _sw, sv in B.iter_fields(v):
                    if sfn == 1:
                        self.active_mask = sv
            elif fn == 77:                              # getActivePrograms -> reply
                self._emit_active()
            # fields 18 (time string), 45 (battery), 22/120 (setup) are accepted + ignored

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
            # field 6 = active run: tfn1 = constant run-type(2), tfn4 = 0-INDEXED station
            # (self.station is 1-indexed, so emit station-1), tfn7 = seconds. Mirrors the
            # real device wire format so parse_reply's +1 yields the 1-indexed active_zone.
            sub = B._fv(1, 4) + B._fb(6, B._fv(1, 2) + B._fv(4, (self.station or 1) - 1) + B._fv(7, self.seconds or 0))
        else:
            sub = B._fv(1, 1)
        body += B._fb(16, sub)
        return B._wrap(body)

    def _send_notif(self, pt: bytes) -> None:
        if self._emit is None:
            return
        ct = bytearray()
        cc = self.dev_rx
        for i in range(0, len(pt), 16):
            ct += bytes(a ^ b for a, b in zip(pt[i:i + 16], _keystream_block(self.key, self.iv, cc)))
            cc = (cc + 1) & 0xFFFFFFFF
        self.dev_rx = cc
        trailer = (sum(pt) + FRAME_MAGIC + len(pt)) & 0xFFFF   # not validated by the reader
        self._emit(bytes([FRAME_MAGIC, len(ct)]) + bytes(ct) + struct.pack("<H", trailer))

    def _emit_status(self) -> None:
        self._send_notif(self._status_plaintext())

    def _emit_active(self) -> None:
        # reply carries ActivePrograms under field 20 (as the real device does)
        self._send_notif(B._wrap(B._fb(20, B._fv(1, self.active_mask))))


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


# Real decrypted status bytes captured live from the device (Smart Hose Tap Timer).
# The active station in field 16->6->tfn4 is 0-INDEXED (proven by a controlled start-sweep:
# Valve N -> f4 = N-1), so active_zone = f4 + 1. This capture has raw f4=2 => Valve 3.
_REAL_WATERING = bytes.fromhex(
    "aa775a0f5b000a06446755d871b038cfd5bdd3068201480804120f0802120b10001a05080210ac02"
    "2000321508021800200228ac0232070800100218ac0238ac023a00480050006a0408002000720318"
    "89167a0082010800000000000000000939")
_REAL_IDLE = bytes.fromhex(
    "aa775a0f37000a06446755d871b038e3d5bdd3068201240800120208003a00480050006a04080020"
    "0072031889167a0082010800000000000000006296")
# Same device, a different zone — raw f4=3 => Valve 4. Proves the parser DISTINGUISHES
# zones (f4=2 vs 3 above; tfn1 is a constant 2 in both, which is why reading it lit all as #2).
_REAL_WATERING_Z3 = bytes.fromhex(
    "aa775a0f5b000a06446755d871b038d0ddbdd3068201480804120f0802120b10001a05080310ac02"
    "2000321508021800200328ac0232070800100318ac0238ac023a00480050006a0408002000720318"
    "fc157a0082010800000000000000007ac7")


def test_active_zone_parsed_from_real_watering_capture():
    """PROOF (real device bytes): raw f4=2 (0-indexed) -> 1-indexed active_zone == 3 (Valve 3)."""
    st = parse_reply(_REAL_WATERING)
    assert st.is_watering is True and st.seconds_remaining == 300
    assert st.active_zone == 3                        # raw f4=2, +1 -> Valve 3
    assert st.to_dict()["active_zone"] == 3           # surfaced to the REST/UI layer


def test_active_zone_distinguishes_zones_real_capture():
    """PROOF the fix isn't a single-zone coincidence, and the +1 offset holds: two real
    captures with raw f4=2 and 3 map to Valve 3 and Valve 4."""
    a = parse_reply(_REAL_WATERING)       # raw f4=2 -> 3
    b = parse_reply(_REAL_WATERING_Z3)    # raw f4=3 -> 4
    assert a.active_zone == 3 and b.active_zone == 4


def test_active_zone_none_when_idle_real_capture():
    st = parse_reply(_REAL_IDLE)
    assert st.is_watering is False
    assert st.active_zone is None                    # no active-run block when idle
    assert st.to_dict()["active_zone"] is None


def test_fake_status_reports_active_zone():
    """The fake now mirrors the wire format, so the whole stack can assert active_zone."""
    t = FakeTimer(); t.primed = True; t.watering = True; t.station = 3; t.seconds = 120
    st = parse_reply(t._status_plaintext())
    assert st.is_watering and st.active_zone == 3


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


def test_api_remove_device(monkeypatch, tmp_path):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "A", "mac": "AA"}, {"name": "B", "mac": "BB"}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    res = asyncio.run(server.remove_device(0))
    assert res["removed"] == "A"
    assert [d["name"] for d in res["devices"]] == ["B"]
    assert [d["name"] for d in json.load(open(cfg))["devices"]] == ["B"]   # persisted


def test_api_remove_device_bad_index(monkeypatch, tmp_path):
    import json
    from fastapi import HTTPException
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "A"}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.remove_device(5))
    assert exc.value.status_code == 404


def test_api_onboard_start_stream_continue(monkeypatch, tmp_path):
    """start launches the flow; the events accumulate; continue advances a waiting_user
    step; the SSE stream projects the events."""
    import server
    import onboarding as O
    monkeypatch.setattr(server, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(server, "_job", None, raising=False)

    async def fake_flow(params, gate):
        yield {"id": "await_reset", "title": "Reset", "instruction": "reset it",
               "state": "waiting_user", "verified": False}
        await gate.wait()
        yield {"id": "save", "title": "Saved", "instruction": "done",
               "state": "done", "verified": True}

    monkeypatch.setattr(O, "onboard_flow", fake_flow)

    async def scenario():
        r = await server.onboard_start(server.OnboardStartBody(mode="self"))
        assert r["ok"] is True
        for _ in range(20):                          # let the task reach the waiting_user yield
            await asyncio.sleep(0)
            if server._job.events:
                break
        assert server._job.events[-1]["id"] == "await_reset"
        await server.onboard_continue(server.OnboardContinueBody())
        await server._job.task
        assert server._job.events[-1]["id"] == "save"
        # SSE stream replays the events to a (re)connecting client
        resp = await server.onboard_stream()
        body = ""
        async for chunk in resp.body_iterator:
            body += chunk if isinstance(chunk, str) else chunk.decode()
        assert "await_reset" in body and "save" in body

    asyncio.run(scenario())


def test_onboard_start_account_mode_injects_key_from_session(monkeypatch, tmp_path):
    """P3: the browser sends only a MAC; the server injects the account key from the
    in-memory session (never round-tripping the key through the client)."""
    import server
    import onboarding as O
    monkeypatch.setattr(server, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(server, "_job", None, raising=False)
    monkeypatch.setattr(server, "_account_session",
                        {"email": "me@x.com", "key": "dd" * 16,
                         "devices": [{"name": "Smart Hose Tap Timer",
                                      "mac": "AA:BB:CC:DD:EE:01", "stations": 4}]}, raising=False)
    captured = {}

    async def fake_flow(params, gate):
        captured.update(params)
        yield {"id": "save", "title": "s", "instruction": "", "state": "done", "verified": True}

    monkeypatch.setattr(O, "onboard_flow", fake_flow)

    async def scenario():
        await server.onboard_start(server.OnboardStartBody(mode="account",
                                                           device_mac="AA:BB:CC:DD:EE:01"))
        await server._job.task

    asyncio.run(scenario())
    assert captured["mode"] == "account" and captured["key"] == "dd" * 16   # injected server-side
    assert captured["name"] == "Smart Hose Tap Timer"                       # from the cached list
    assert captured["device_mac"] == "AA:BB:CC:DD:EE:01"


def test_onboard_start_account_mode_falls_back_to_saved_key(monkeypatch, tmp_path):
    """P3: no live session -> inject the persisted account key (add-a-timer after restart)."""
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    O.write_account(str(cfg), "me@x.com", "ee" * 16)
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_job", None, raising=False)
    monkeypatch.setattr(server, "_account_session", None, raising=False)
    captured = {}

    async def fake_flow(params, gate):
        captured.update(params)
        yield {"id": "save", "title": "s", "instruction": "", "state": "done", "verified": True}

    monkeypatch.setattr(O, "onboard_flow", fake_flow)

    async def scenario():
        await server.onboard_start(server.OnboardStartBody(mode="account",
                                                           device_mac="AA:BB:CC:DD:EE:01"))
        await server._job.task

    asyncio.run(scenario())
    assert captured["key"] == "ee" * 16                                     # from persisted account


def test_onboard_start_supersedes_stale_job(monkeypatch, tmp_path):
    """A new onboarding start must SUPERSEDE a stale/awaiting job (cancel it) instead of
    409 'already running' — the bug where an abandoned flow wedged the Add button."""
    import server
    import onboarding as O
    monkeypatch.setattr(server, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(server, "_job", None, raising=False)

    async def hang_flow(params, gate):
        yield {"id": "await_reset", "title": "Reset", "instruction": "reset it",
               "state": "waiting_user", "verified": False}
        await gate.wait()          # never resumed -> job hangs (the stale-job scenario)
        yield {"id": "save", "title": "Saved", "instruction": "", "state": "done", "verified": True}

    monkeypatch.setattr(O, "onboard_flow", hang_flow)

    async def scenario():
        await server.onboard_start(server.OnboardStartBody(mode="self"))
        for _ in range(20):
            await asyncio.sleep(0)
            if server._job.events:
                break
        first = server._job                       # hung, not done
        assert not first.done
        r = await server.onboard_start(server.OnboardStartBody(mode="self"))  # must NOT raise 409
        assert r["ok"] is True
        assert server._job is not first           # a fresh job replaced it
        for _ in range(20):
            await asyncio.sleep(0)
        assert first.task.cancelled() or first.done   # the stale task was cancelled/ended

    asyncio.run(scenario())


def test_onboard_cancel_clears_job(monkeypatch, tmp_path):
    import server
    import onboarding as O
    monkeypatch.setattr(server, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(server, "_job", None, raising=False)

    async def hang_flow(params, gate):
        yield {"id": "await_reset", "title": "Reset", "instruction": "", "state": "waiting_user",
               "verified": False}
        await gate.wait()

    monkeypatch.setattr(O, "onboard_flow", hang_flow)

    async def scenario():
        await server.onboard_start(server.OnboardStartBody(mode="self"))
        for _ in range(20):
            await asyncio.sleep(0)
            if server._job.events:
                break
        r = await server.onboard_cancel()
        assert r["ok"] is True
        for _ in range(20):
            await asyncio.sleep(0)
        assert server._job is None                 # cleared

    asyncio.run(scenario())


def test_api_run_blocks_during_onboarding(monkeypatch):
    """Control endpoints must refuse (503) while an onboarding job is running, to avoid
    two BLE operations on the one radio."""
    from fastapi import HTTPException
    import server

    class _FakeJob:
        done = False
    monkeypatch.setattr(server, "_job", _FakeJob(), raising=False)

    async def noop(_d):
        return None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(server._run(noop))
    assert exc.value.status_code == 503


def test_api_onboard_state_reports_saved_key(monkeypatch, tmp_path):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "A", "network_key": TEST_KEY}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    assert asyncio.run(server.onboard_state())["has_key"] is True
    cfg.write_text(json.dumps({"devices": []}))
    assert asyncio.run(server.onboard_state())["has_key"] is False


# --- P2: REST account layer (login/list/forget; key never in a response body) ---
def _multi_account_cloud():
    async def fake_cloud(email, pw):
        return [
            {"name": "zone1-4 timer", "mac": "AA:BB:CC:DD:EE:02", "network_key": TEST_KEY, "stations": 4},
            {"name": "Smart Hose Tap Timer", "mac": "AA:BB:CC:DD:EE:01", "network_key": TEST_KEY, "stations": 4},
        ]
    return fake_cloud


def test_account_login_caches_persists_and_hides_key(monkeypatch, tmp_path):
    import json
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [                       # first timer already added
        {"name": "zone1-4 timer", "mac": "AA:BB:CC:DD:EE:02", "network_key": TEST_KEY, "stations": 4}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_account_session", None, raising=False)
    monkeypatch.setattr(O, "cloud_fetch", _multi_account_cloud())

    res = asyncio.run(server.account_login(server.AccountLoginBody(email="me@x.com", password="pw")))
    assert res["email"] == "me@x.com"
    assert {t["mac"] for t in res["timers"]} == {"AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:01"}
    added = {t["mac"]: t["added"] for t in res["timers"]}
    assert added["AA:BB:CC:DD:EE:02"] is True and added["AA:BB:CC:DD:EE:01"] is False
    assert TEST_KEY not in json.dumps(res)                        # key never in the body
    acct = O.read_account(str(cfg))                               # persisted {email,key}
    assert acct == {"email": "me@x.com", "network_key": TEST_KEY}
    assert server._account_session["email"] == "me@x.com"         # cached in memory


def test_account_login_bad_creds_401(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    cfg.write_text('{"devices":[]}')
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_account_session", None, raising=False)

    async def bad(email, pw):
        raise O.AuthError("bad creds")

    monkeypatch.setattr(O, "cloud_fetch", bad)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.account_login(server.AccountLoginBody(email="me@x.com", password="wrong")))
    assert exc.value.status_code == 401
    assert O.read_account(str(cfg)) is None                       # nothing persisted on failure


def test_account_timers_lists_session_with_added_flags(monkeypatch, tmp_path):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text('{"devices":[{"mac":"AA:BB:CC:DD:EE:02"}]}')
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_account_session",
                        {"email": "me@x.com", "key": TEST_KEY, "devices": [
                            {"name": "A", "mac": "AA:BB:CC:DD:EE:02", "stations": 4},
                            {"name": "B", "mac": "AA:BB:CC:DD:EE:01", "stations": 4}]}, raising=False)
    res = asyncio.run(server.account_timers())
    assert res["signed_in"] is True
    added = {t["mac"]: t["added"] for t in res["timers"]}
    assert added["AA:BB:CC:DD:EE:02"] is True and added["AA:BB:CC:DD:EE:01"] is False
    assert TEST_KEY not in json.dumps(res)               # key never in the body


def test_account_timers_empty_when_not_signed_in(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "CONFIG", str(tmp_path / "c.json"))
    monkeypatch.setattr(server, "_account_session", None, raising=False)
    res = asyncio.run(server.account_timers())
    assert res["signed_in"] is False and res["timers"] == []


def test_account_get_reflects_state(monkeypatch, tmp_path):
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_account_session", None, raising=False)
    st = asyncio.run(server.account_get())
    assert st["signed_in"] is False and st["has_saved_key"] is False
    O.write_account(str(cfg), "me@x.com", TEST_KEY)
    st = asyncio.run(server.account_get())
    assert st["signed_in"] is False and st["has_saved_key"] is True and st["email"] == "me@x.com"


def test_account_forget_clears_memory_and_persisted(monkeypatch, tmp_path):
    import server
    import onboarding as O
    cfg = tmp_path / "config.json"
    O.write_account(str(cfg), "me@x.com", TEST_KEY)
    O.write_config(str(cfg), {"name": "A", "address": "UUID-A", "network_key": TEST_KEY,
                              "mac": TEST_MAC, "stations": 4})
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_account_session",
                        {"email": "me@x.com", "key": TEST_KEY, "devices": []}, raising=False)
    asyncio.run(server.account_forget())
    assert server._account_session is None
    assert O.read_account(str(cfg)) is None                       # account block gone
    import json
    assert json.loads(cfg.read_text())["devices"][0]["mac"] == TEST_MAC  # devices untouched


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
    assert new["key_source"] == "orbit"            # labelled as the shared account key


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


def test_proof_evaluator_gates_correctly():
    """The proof's pass/fail evaluator must PASS only on the full non-circular chain:
    keyless-first + provisioned + all >= MIN_CYCLES power-cycle verifies succeed."""
    from provision_proof import evaluate_trial, MIN_CYCLES
    ok_cycles = [True] * MIN_CYCLES
    # the one and only PASS
    assert evaluate_trial(False, True, ok_cycles)[0] is True
    # negative control decoded -> confounded -> FAIL
    assert evaluate_trial(True, True, ok_cycles)[0] is False
    # not provisioned -> FAIL
    assert evaluate_trial(False, False, ok_cycles)[0] is False
    # too few cycles -> FAIL
    assert evaluate_trial(False, True, [True] * (MIN_CYCLES - 1))[0] is False
    # a cycle failed (key didn't persist) -> FAIL
    assert evaluate_trial(False, True, [True, False, True])[0] is False


async def _drive_onboard(gen, gate, choices):
    """Drive the onboard_flow async generator; resume the gate at each waiting_user step
    with choices[step_id] (None if absent)."""
    events = []
    while True:
        try:
            e = await gen.__anext__()
        except StopAsyncIteration:
            break
        events.append(e)
        if e["state"] == "waiting_user":
            gate.resume(choices.get(e["id"]))
    return events


def _onboard_device_from_cloud(mac="AA:BB:CC:DD:EE:01"):
    return {"name": "Yard", "mac": mac, "network_key": TEST_KEY, "stations": 4}


async def _fake_provision(key, *, want_mac=None, **kw):
    st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
    return "UUID-NEW", "AA:BB:CC:DD:EE:01", st


def test_onboard_flow_orbit_success(tmp_path, monkeypatch):
    import json
    import onboarding as O
    p = tmp_path / "config.json"

    async def fake_cloud(email, pw):
        return [_onboard_device_from_cloud()]

    monkeypatch.setattr(O, "cloud_fetch", fake_cloud)
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    params = {"mode": "orbit", "email": "me@x.com", "password": "pw",
              "device_mac": "AA:BB:CC:DD:EE:01", "path": str(p)}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate, {"await_reset": None}))
    assert events[-1]["id"] == "save" and events[-1]["verified"]
    d = json.loads(p.read_text())["devices"][0]
    assert d["key_source"] == "orbit" and d["network_key"] == TEST_KEY
    assert TEST_KEY not in json.dumps(events)            # key never in an event


def test_onboard_flow_multiple_devices_shows_picker(tmp_path, monkeypatch):
    """Login OK but the account has >1 key-bearing timer: the flow must present a
    pick_device choice (by name) and provision the CHOSEN device's key — not dead-end.
    This is the real bug: login worked in the app, but the web client had two timers
    and no MAC, so it reported 'no device on account'."""
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    KEY_A, KEY_B = "aa" * 16, "bb" * 16

    async def multi_cloud(email, pw):
        return [
            {"name": "zone1-4 timer", "mac": "AA:BB:CC:DD:EE:02", "network_key": KEY_A, "stations": 4},
            {"name": "Smart Hose Tap Timer", "mac": "AA:BB:CC:DD:EE:01", "network_key": KEY_B, "stations": 4},
        ]

    captured = {}

    async def cap_provision(key, *, want_mac=None, **kw):
        captured["key"], captured["mac"] = key, want_mac
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    monkeypatch.setattr(O, "cloud_fetch", multi_cloud)
    monkeypatch.setattr(O, "provision_device", cap_provision)
    gate = O.OnboardGate()
    params = {"mode": "orbit", "email": "me@x.com", "password": "pw", "path": str(p)}  # no device_mac
    events = asyncio.run(_drive_onboard(
        O.onboard_flow(params, gate), gate,
        {"pick_device": "AA:BB:CC:DD:EE:01", "await_reset": None}))

    pick = next(e for e in events if e["id"] == "pick_device" and e["state"] == "waiting_user")
    assert {o["value"] for o in pick["options"]} == {"AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:01"}
    assert any(o["label"] == "Smart Hose Tap Timer" for o in pick["options"])  # picked by NAME
    assert captured["key"] == KEY_B                       # the CHOSEN device's key provisions
    saved = json.loads(p.read_text())["devices"][0]
    assert saved["key_source"] == "orbit" and saved["network_key"] == KEY_B
    assert saved["name"] == "Smart Hose Tap Timer"
    assert KEY_A not in json.dumps(events) and KEY_B not in json.dumps(events)  # no key leaks


def test_onboard_flow_account_with_mac_adopts_not_provisions(tmp_path, monkeypatch):
    """PROVEN FIX: a timer already on the Orbit account (has a MAC) is already keyed, so
    account 'add' must ADOPT it (catch_device: connect + read + save, NO reset/key-write),
    never provision. Backed by live Experiment C (catch_device read status with the account
    key and no reset)."""
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    ACC = "cc" * 16
    called = {"adopt": 0, "prov": 0}

    async def rec_catch(key, *, want_mac=None, **kw):
        called["adopt"] += 1
        assert key == ACC and want_mac == "AA:BB:CC:DD:EE:01"
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-ADOPT", "AA:BB:CC:DD:EE:01", st

    async def rec_prov(key, *, want_mac=None, **kw):
        called["prov"] += 1
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID", "AA:BB:CC:DD:EE:01", st

    def no_cloud(*a, **k):
        raise AssertionError("account mode must not call cloud_fetch")

    monkeypatch.setattr(O, "catch_device", rec_catch)
    monkeypatch.setattr(O, "provision_device", rec_prov)
    monkeypatch.setattr(O, "cloud_fetch", no_cloud)
    gate = O.OnboardGate()
    params = {"mode": "account", "key": ACC, "name": "Smart Hose Tap Timer",
              "device_mac": "AA:BB:CC:DD:EE:01", "stations": 4, "path": str(p)}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate, {"await_wake": None}))
    assert called["adopt"] == 1 and called["prov"] == 0          # ADOPT, not provision
    assert any(e["id"] == "await_wake" for e in events)          # "wake", not...
    assert not any(e["id"] == "await_reset" for e in events)     # ...factory reset
    saved = json.loads(p.read_text())["devices"][0]
    assert saved["key_source"] == "orbit" and saved["network_key"] == ACC
    assert saved["address"] == "UUID-ADOPT" and saved["name"] == "Smart Hose Tap Timer"
    assert events[-1]["id"] == "save"
    assert ACC not in json.dumps(events)


def test_onboard_flow_account_without_mac_provisions_fresh(tmp_path, monkeypatch):
    """An account 'timer that's not listed' (no MAC) is a FRESH unit -> provision (key-write),
    not adopt."""
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    ACC = "dd" * 16
    called = {"adopt": 0, "prov": 0}

    async def rec_catch(key, *, want_mac=None, **kw):
        called["adopt"] += 1
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID", "AA:BB:CC:DD:EE:01", st

    async def rec_prov(key, *, want_mac=None, **kw):
        called["prov"] += 1
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-PROV", "AA:BB:CC:DD:EE:01", st

    monkeypatch.setattr(O, "catch_device", rec_catch)
    monkeypatch.setattr(O, "provision_device", rec_prov)
    gate = O.OnboardGate()
    params = {"mode": "account", "key": ACC, "stations": 4, "path": str(p)}   # no device_mac
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate, {"await_reset": None}))
    assert called["prov"] == 1 and called["adopt"] == 0          # PROVISION, not adopt
    assert any(e["id"] == "await_reset" for e in events)
    assert json.loads(p.read_text())["devices"][0]["network_key"] == ACC


def test_onboard_flow_adopt_failure_reports_and_saves_nothing(tmp_path, monkeypatch):
    import onboarding as O
    p = tmp_path / "config.json"

    async def boom(key, *, want_mac=None, **kw):
        raise O.ResolveError("could not catch the timer — phone Bluetooth OFF?")

    monkeypatch.setattr(O, "catch_device", boom)
    gate = O.OnboardGate()
    params = {"mode": "account", "key": "ee" * 16, "device_mac": "AA:BB:CC:DD:EE:01",
              "stations": 4, "path": str(p)}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate, {"await_wake": None}))
    assert events[-1]["id"] == "adopt" and events[-1]["state"] == "failed"
    assert not any(e["id"] == "save" for e in events)
    assert not p.exists()                                        # nothing written on failure             # key never in an event


def test_onboard_flow_account_mode_missing_key_fails(tmp_path, monkeypatch):
    import onboarding as O
    p = tmp_path / "config.json"

    async def boom(*a, **k):
        raise AssertionError("must not provision without a key")

    monkeypatch.setattr(O, "provision_device", boom)
    gate = O.OnboardGate()
    events = asyncio.run(_drive_onboard(
        O.onboard_flow({"mode": "account", "path": str(p)}, gate), gate, {}))
    assert events[-1]["id"] == "get_key" and events[-1]["state"] == "failed"
    assert not any(e["id"] == "save" for e in events)
    assert not p.exists()                                # nothing written


def test_onboard_flow_auth_fail_then_self_key(tmp_path, monkeypatch):
    import json
    import onboarding as O
    p = tmp_path / "config.json"

    async def bad_cloud(email, pw):
        raise O.AuthError("bad creds")

    monkeypatch.setattr(O, "cloud_fetch", bad_cloud)
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    params = {"mode": "orbit", "email": "me@x.com", "password": "wrong",
              "device_mac": "AA:BB:CC:DD:EE:01", "path": str(p),
              "secrets_path": str(tmp_path / "s.md")}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate,
                                        {"fallback_choice": "self_key", "await_reset": None}))
    assert any(e["id"] == "fallback_choice" for e in events)
    d = json.loads(p.read_text())["devices"][0]
    assert d["key_source"] == "self"
    assert d["network_key"] not in json.dumps(events)    # generated key never in an event


def test_onboard_flow_first_user_no_device_then_self_key(tmp_path, monkeypatch):
    import onboarding as O
    p = tmp_path / "config.json"

    async def empty_cloud(email, pw):
        return []                                        # logged in, but no device on account

    monkeypatch.setattr(O, "cloud_fetch", empty_cloud)
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    params = {"mode": "orbit", "email": "me@x.com", "password": "pw", "path": str(p),
              "secrets_path": str(tmp_path / "s.md")}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate,
                                        {"fallback_choice": "self_key", "await_reset": None}))
    gk_fail = next(e for e in events if e["id"] == "get_key" and e["state"] == "failed")
    assert "account" in gk_fail["instruction"].lower()   # the reason is on the failed step
    assert events[-1]["id"] == "save"


def test_onboard_flow_app_first_then_retry_succeeds(tmp_path, monkeypatch):
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    calls = {"n": 0}

    async def flaky_cloud(email, pw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise O.AuthError("first attempt fails")
        return [_onboard_device_from_cloud()]

    monkeypatch.setattr(O, "cloud_fetch", flaky_cloud)
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    params = {"mode": "orbit", "email": "me@x.com", "password": "pw",
              "device_mac": "AA:BB:CC:DD:EE:01", "path": str(p)}
    events = asyncio.run(_drive_onboard(
        O.onboard_flow(params, gate), gate,
        {"fallback_choice": "orbit_app_first", "app_instructions": None, "await_reset": None}))
    assert calls["n"] == 2                                # retried after the app step
    assert any(e["id"] == "app_instructions" for e in events)
    assert json.loads(p.read_text())["devices"][0]["key_source"] == "orbit"


def test_onboard_flow_marks_get_key_failed_on_auth_fail(tmp_path, monkeypatch):
    """Bug fix: a failed login must transition get_key to 'failed' (not stay 'working'),
    before the fallback_choice."""
    import onboarding as O

    async def bad_cloud(email, pw):
        raise O.AuthError("bad creds")

    monkeypatch.setattr(O, "cloud_fetch", bad_cloud)
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    params = {"mode": "orbit", "email": "me@x.com", "password": "wrong",
              "device_mac": "AA:BB:CC:DD:EE:01", "path": str(tmp_path / "c.json"),
              "secrets_path": str(tmp_path / "s.md")}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate,
                                        {"fallback_choice": "self_key", "await_reset": None}))
    gk_failed = [i for i, e in enumerate(events) if e["id"] == "get_key" and e["state"] == "failed"]
    fb = [i for i, e in enumerate(events) if e["id"] == "fallback_choice"]
    assert gk_failed and fb and gk_failed[0] < fb[0]


def test_onboard_flow_gated_steps_emit_resolved(tmp_path, monkeypatch):
    """Bug fix: a gated step's LAST event is a resolved 'done' (so SSE replay won't
    resurrect its buttons)."""
    import onboarding as O

    async def fake_cloud(email, pw):
        return [_onboard_device_from_cloud()]

    monkeypatch.setattr(O, "cloud_fetch", fake_cloud)
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    params = {"mode": "orbit", "email": "me@x.com", "password": "pw",
              "device_mac": "AA:BB:CC:DD:EE:01", "path": str(tmp_path / "c.json")}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate, {"await_reset": None}))
    ar = [e for e in events if e["id"] == "await_reset"]
    assert ar and ar[-1]["state"] == "done"          # resolved, not left waiting_user


def test_onboard_flow_reuse_mode_no_cloud(tmp_path, monkeypatch):
    """mode='reuse' provisions with the saved account key and NEVER logs in."""
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"devices": [{"name": "Old", "mac": "AA:BB:CC:DD:EE:FF",
                                          "network_key": TEST_KEY, "address": "OLD", "stations": 4}]}))

    async def no_cloud(*a, **k):
        raise AssertionError("reuse mode must NOT call cloud_fetch")

    monkeypatch.setattr(O, "cloud_fetch", no_cloud)
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    params = {"mode": "reuse", "device_mac": "AA:BB:CC:DD:EE:01", "path": str(p)}
    events = asyncio.run(_drive_onboard(O.onboard_flow(params, gate), gate, {"await_reset": None}))
    assert events[-1]["id"] == "save"
    new = json.loads(p.read_text())["devices"][-1]
    assert new["network_key"] == TEST_KEY and new["key_source"] == "orbit"


def test_onboard_flow_reuse_no_saved_key_fails(tmp_path, monkeypatch):
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"devices": []}))
    monkeypatch.setattr(O, "provision_device", _fake_provision)
    gate = O.OnboardGate()
    events = asyncio.run(_drive_onboard(
        O.onboard_flow({"mode": "reuse", "path": str(p)}, gate), gate, {}))
    assert any(e["id"] == "get_key" and e["state"] == "failed" for e in events)
    assert not any(e["id"] == "save" for e in events)


def test_provision_device_refuses_multiple_fresh_devices():
    """SAFETY (P0): with >1 fresh B-Hyve present, provision_device must refuse and write
    the key to NEITHER — never spray the account key onto an unknown device."""
    import onboarding as O
    a = FakeTimer(mac="AA:BB:CC:DD:EE:FF", key_hex=None)
    b = FakeTimer(mac="AA:BB:CC:DD:EE:01", key_hex=None)
    adverts = [_adv("UUID-A", "", -50), _adv("UUID-B", "", -55)]
    resolver = lambda addr: {"UUID-A": a, "UUID-B": b}.get(addr)
    with fake_catch_ble(adverts, resolver):
        with pytest.raises(O.ResolveError):
            asyncio.run(O.provision_device(TEST_KEY, want_mac="AA:BB:CC:DD:EE:01", scan_timeout=2.0))
    assert a._provision_write is None            # key was NOT written to either device
    assert b._provision_write is None


def test_provision_device_reports_wanted_mac_mismatch():
    """If the single caught device's MAC != want_mac, report it (don't hunt/write others)."""
    import onboarding as O
    only = FakeTimer(mac="AA:BB:CC:DD:EE:FF", key_hex=None)
    with fake_catch_ble([_adv("UUID-ONLY", "", -50)], lambda _a: only):
        with pytest.raises(O.ResolveError):
            asyncio.run(O.provision_device(TEST_KEY, want_mac="AA:BB:CC:DD:EE:01",
                                           scan_timeout=0.4, reconnect_attempts=1))


def test_provision_device_times_out_when_no_bhyve():
    import onboarding as O
    with fake_catch_ble([_adv("UUID-DECOY", "TV", -40)], lambda _a: None):
        with pytest.raises(O.ResolveError):
            asyncio.run(O.provision_device(TEST_KEY, scan_timeout=0.3))


def test_cli_parse_key_flags():
    import cli
    assert cli._parse_key_flags(["1", "300"]) == (False, None)
    assert cli._parse_key_flags(["--self-key"]) == (True, None)
    assert cli._parse_key_flags(["--key=00112233445566778899aabbccddeeff"]) == \
        (False, "00112233445566778899aabbccddeeff")
    assert cli._parse_key_flags(["--key", "deadbeef"]) == (False, "deadbeef")


def test_cli_provision_self_key_generates_stashes_and_labels(tmp_path, monkeypatch):
    """--self-key: mint a random key, stash it (so it can't be lost), provision with it,
    and label the device key_source='self'. No cloud involved."""
    import json
    import cli
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text('{"devices": []}')
    secrets = tmp_path / "gen_keys.md"
    captured = {}

    async def fake_provision(key, *, want_mac=None, **kw):
        captured["key"] = key
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    def no_cloud(*a, **k):
        raise AssertionError("cloud_fetch must NOT be called in self-key mode")

    monkeypatch.setattr(O, "provision_device", fake_provision)
    monkeypatch.setattr(O, "cloud_fetch", no_cloud)
    asyncio.run(cli.cmd_register(name="Standalone", path=str(p), ask_prompt=False,
                                 provision=True, self_key=True, secrets_path=str(secrets)))
    k = captured["key"]
    assert len(k) == 32 and int(k, 16) >= 0                 # a random 16-byte hex key
    data = json.loads(p.read_text())
    d = data["devices"][0]
    assert d["key_source"] == "self"
    assert d["network_key"] == k
    assert k in secrets.read_text()                          # stashed so it can't be lost


def test_cli_provision_byo_key(tmp_path, monkeypatch):
    """--key HEX: use the supplied key, labelled self."""
    import json
    import cli
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text('{"devices": []}')
    captured = {}

    async def fake_provision(key, *, want_mac=None, **kw):
        captured["key"] = key
        st = parse_reply(FakeTimer(mac="AA:BB:CC:DD:EE:01")._status_plaintext())
        return "UUID-NEW", "AA:BB:CC:DD:EE:01", st

    monkeypatch.setattr(O, "provision_device", fake_provision)
    asyncio.run(cli.cmd_register(path=str(p), ask_prompt=False, provision=True,
                                 provided_key=TEST_KEY, secrets_path=str(tmp_path / "s.md")))
    assert captured["key"] == TEST_KEY
    assert json.loads(p.read_text())["devices"][0]["key_source"] == "self"


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


# --- P1: account model (email + key persisted; password NEVER) ---------------
def test_account_roundtrip(tmp_path):
    import onboarding as O
    p = tmp_path / "config.json"
    assert O.read_account(str(p)) is None                 # absent
    O.write_account(str(p), "me@x.com", TEST_KEY)
    acct = O.read_account(str(p))
    assert acct == {"email": "me@x.com", "network_key": TEST_KEY}


def test_write_account_preserves_devices(tmp_path):
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    O.write_config(str(p), {"name": "A", "address": "UUID-A", "network_key": TEST_KEY,
                            "mac": TEST_MAC, "stations": 4})
    O.write_account(str(p), "me@x.com", TEST_KEY)
    cfg = json.loads(p.read_text())
    assert cfg["account"]["email"] == "me@x.com"
    assert len(cfg["devices"]) == 1 and cfg["devices"][0]["mac"] == TEST_MAC  # devices intact


def test_write_config_preserves_account(tmp_path):
    import json
    import onboarding as O
    p = tmp_path / "config.json"
    O.write_account(str(p), "me@x.com", TEST_KEY)
    O.write_config(str(p), {"name": "A", "address": "UUID-A", "network_key": TEST_KEY,
                            "mac": TEST_MAC, "stations": 4})
    cfg = json.loads(p.read_text())
    assert cfg["account"]["email"] == "me@x.com"          # account survives a device write
    assert cfg["devices"][0]["mac"] == TEST_MAC


def test_read_account_none_on_device_only_config(tmp_path):
    import onboarding as O
    p = tmp_path / "config.json"
    O.write_config(str(p), {"name": "A", "address": "UUID-A", "network_key": TEST_KEY,
                            "mac": TEST_MAC, "stations": 4})
    assert O.read_account(str(p)) is None                 # backward compatible


def test_write_account_refuses_to_clobber_malformed(tmp_path):
    import onboarding as O
    p = tmp_path / "config.json"
    p.write_text("{ not json ")
    with pytest.raises(ValueError):
        O.write_account(str(p), "me@x.com", TEST_KEY)
    assert p.read_text() == "{ not json "                 # original left intact


# --- P1: engine-agnostic schedule store (per-timer, no BLE, no engine) ---------
def _cfg_with_timer(tmp_path, mac=TEST_MAC, stations=4, name="A"):
    import onboarding as O
    p = tmp_path / "config.json"
    O.write_config(str(p), {"name": name, "address": "UUID-" + name, "network_key": TEST_KEY,
                            "mac": mac, "stations": stations})
    return str(p)


def test_schedule_roundtrip_by_mac(tmp_path):
    import schedule as S
    p = _cfg_with_timer(tmp_path)
    assert S.read_schedules(p, TEST_MAC) == []
    S.write_schedules(p, TEST_MAC, [{"valve": 2, "start": "06:00", "days": [0, 2, 4], "minutes": 5}])
    got = S.read_schedules(p, TEST_MAC)
    assert len(got) == 1
    r = got[0]
    assert r["valve"] == 2 and r["start"] == "06:00" and r["days"] == [0, 2, 4]
    assert r["minutes"] == 5 and r["enabled"] is True          # default enabled


def test_schedule_per_device_isolation_and_preserves_config(tmp_path):
    import json
    import onboarding as O
    import schedule as S
    p = _cfg_with_timer(tmp_path, mac="AA:BB:CC:DD:EE:02", name="Zone")
    O.write_config(p, {"name": "Tap", "address": "UUID-Tap", "network_key": TEST_KEY,
                       "mac": "AA:BB:CC:DD:EE:01", "stations": 4})
    O.write_account(p, "me@x.com", TEST_KEY)
    S.write_schedules(p, "AA:BB:CC:DD:EE:01", [{"valve": 1, "start": "07:30", "days": [6], "minutes": 10}])
    assert S.read_schedules(p, "AA:BB:CC:DD:EE:02") == []       # other timer untouched
    assert len(S.read_schedules(p, "AA:BB:CC:DD:EE:01")) == 1
    cfg = json.loads(open(p).read())
    assert cfg["account"]["email"] == "me@x.com"                # account preserved
    assert {d["name"] for d in cfg["devices"]} == {"Zone", "Tap"}


def test_schedule_validation_rejects_bad_rules(tmp_path):
    import pytest as _pt
    import schedule as S
    p = _cfg_with_timer(tmp_path, stations=4)
    for bad in [
        {"valve": 0, "start": "06:00", "days": [0], "minutes": 5},     # valve < 1
        {"valve": 5, "start": "06:00", "days": [0], "minutes": 5},     # valve > stations
        {"valve": 1, "start": "6:00", "days": [0], "minutes": 5},      # bad HH:MM
        {"valve": 1, "start": "24:00", "days": [0], "minutes": 5},     # hour out of range
        {"valve": 1, "start": "06:00", "days": [], "minutes": 5},      # no days
        {"valve": 1, "start": "06:00", "days": [7], "minutes": 5},     # day out of range
        {"valve": 1, "start": "06:00", "days": [0], "minutes": 0},     # duration < 1
        {"valve": 1, "start": "06:00", "days": [0], "minutes": 999},   # duration too big
    ]:
        with _pt.raises(S.ScheduleError):
            S.write_schedules(p, TEST_MAC, [bad])
    assert S.read_schedules(p, TEST_MAC) == []                  # nothing written on rejection


def test_schedule_write_unknown_mac_raises(tmp_path):
    import pytest as _pt
    import schedule as S
    p = _cfg_with_timer(tmp_path)
    with _pt.raises(S.ScheduleError):
        S.write_schedules(p, "AA:BB:CC:DD:EE:FF", [{"valve": 1, "start": "06:00", "days": [0], "minutes": 5}])


# --- P2: schedule REST (GET/PUT per timer; validate; key never in a response) ---
def _server_with_timer(monkeypatch, tmp_path):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [
        {"name": "A", "mac": TEST_MAC, "network_key": TEST_KEY, "stations": 4}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    return server


def test_api_schedules_get_put_roundtrip(monkeypatch, tmp_path):
    import json
    server = _server_with_timer(monkeypatch, tmp_path)
    assert asyncio.run(server.get_schedules(0))["schedules"] == []
    body = server.SchedulesBody(rules=[{"valve": 2, "start": "06:00", "days": [0, 2, 4], "minutes": 5}])
    r = asyncio.run(server.put_schedules(0, body))
    assert r["schedules"][0]["valve"] == 2 and r["schedules"][0]["enabled"] is True
    assert asyncio.run(server.get_schedules(0))["schedules"][0]["start"] == "06:00"
    assert TEST_KEY not in json.dumps(r)                      # key never in the response


def test_api_schedules_bad_rule_400(monkeypatch, tmp_path):
    from fastapi import HTTPException
    server = _server_with_timer(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.put_schedules(0, server.SchedulesBody(
            rules=[{"valve": 9, "start": "06:00", "days": [0], "minutes": 5}])))
    assert exc.value.status_code == 400
    assert asyncio.run(server.get_schedules(0))["schedules"] == []   # nothing persisted


def test_api_schedules_bad_index_404(monkeypatch, tmp_path):
    from fastapi import HTTPException
    server = _server_with_timer(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.get_schedules(9))
    assert exc.value.status_code == 404


# --- P3: host-driven schedule engine (pure decision + gated firing) ------------
def test_due_rules_matches_day_time_and_enabled():
    from datetime import datetime
    import scheduler
    now = datetime(2026, 8, 3, 6, 30)                 # a Monday, 06:30 -> weekday()==0
    rules = [
        {"valve": 1, "start": "06:30", "days": [0], "minutes": 5},           # due
        {"valve": 2, "start": "06:30", "days": [1], "minutes": 5},           # wrong day
        {"valve": 3, "start": "06:31", "days": [0], "minutes": 5},           # wrong minute
        {"valve": 4, "start": "06:30", "days": [0], "minutes": 5, "enabled": False},  # off
    ]
    due = scheduler.due_rules(rules, now)
    assert [r["valve"] for r in due] == [1]


def _sched_server(monkeypatch, tmp_path, enabled, rule_now):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "host_scheduling": enabled,
        "devices": [{"name": "A", "mac": TEST_MAC, "network_key": TEST_KEY, "stations": 4,
                     "schedules": [{"valve": 2, "start": rule_now.strftime("%H:%M"),
                                    "days": [rule_now.weekday()], "minutes": 7}]}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_job", None, raising=False)
    monkeypatch.setattr(server, "_fired", set(), raising=False)
    return server


def test_run_due_fires_when_enabled(monkeypatch, tmp_path):
    from datetime import datetime
    now = datetime(2026, 8, 3, 6, 30)
    server = _sched_server(monkeypatch, tmp_path, True, now)
    calls = []
    async def fake_fire(i, valve, minutes): calls.append((i, valve, minutes))
    fired = asyncio.run(server.run_due(now, fake_fire))
    assert calls == [(0, 2, 7)] and len(fired) == 1


def test_run_due_skipped_when_disabled(monkeypatch, tmp_path):
    from datetime import datetime
    now = datetime(2026, 8, 3, 6, 30)
    server = _sched_server(monkeypatch, tmp_path, False, now)   # host_scheduling off
    calls = []
    async def fake_fire(i, valve, minutes): calls.append(1)
    assert asyncio.run(server.run_due(now, fake_fire)) == [] and calls == []


def test_run_due_skipped_during_onboarding(monkeypatch, tmp_path):
    from datetime import datetime
    now = datetime(2026, 8, 3, 6, 30)
    server = _sched_server(monkeypatch, tmp_path, True, now)
    class _Busy:  # a not-done onboarding job holds the radio
        done = False
    monkeypatch.setattr(server, "_job", _Busy(), raising=False)
    calls = []
    async def fake_fire(i, valve, minutes): calls.append(1)
    assert asyncio.run(server.run_due(now, fake_fire)) == [] and calls == []


def test_run_due_idempotent_within_minute(monkeypatch, tmp_path):
    from datetime import datetime
    now = datetime(2026, 8, 3, 6, 30)
    server = _sched_server(monkeypatch, tmp_path, True, now)
    calls = []
    async def fake_fire(i, valve, minutes): calls.append((valve, minutes))
    asyncio.run(server.run_due(now, fake_fire))
    asyncio.run(server.run_due(now, fake_fire))       # same minute again
    assert calls == [(2, 7)]                            # fired only once


def test_api_scheduling_toggle(monkeypatch, tmp_path):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": []}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    assert asyncio.run(server.get_scheduling())["enabled"] is False   # default OFF
    asyncio.run(server.put_scheduling(server.SchedulingBody(enabled=True)))
    assert asyncio.run(server.get_scheduling())["enabled"] is True
    assert json.loads(cfg.read_text())["host_scheduling"] is True


# --- P4b: on-device schedule encoder (pure; matches the replay-proven capture) ---
def _unwrap(framed):
    import bhyve_xd as B
    assert framed[:4] == B.MSG_HEADER
    return framed[6:-2]                    # strip [hdr4][len][00] ... [crc16]

def _field(proto, num):
    import bhyve_xd as B
    for fn, wt, v in B.iter_fields(proto):
        if fn == num:
            return v
    return None


def test_encode_set_program_schedule_matches_capture():
    """Encoding Program A, 06:00, Valve 1 (station 0), 600s reproduces the fields decoded
    from the app's real capture: programId=1, Interval daily, startTimes=[360], station(0,600)."""
    import bhyve_xd as B
    import schedule_device as SD
    framed = SD.encode_set_program_schedule(SD.PROGRAM_A, [360], [(0, 600)])
    body = _field(_unwrap(framed), 19)                 # SetProgramSchedule
    assert _field(body, 1) == 1                         # programId = A
    assert _field(body, 4) is not None                 # programType = Interval present
    assert _field(_field(body, 4), 1) == 1             # intervalDays = 1
    # startTimesMinsFromMidnight (repeated field 8) — collect all
    starts = [v for fn, wt, v in B.iter_fields(body) if fn == 8]
    assert starts == [360]                              # 06:00 local, matches capture
    station = _field(body, 9)
    assert _field(station, 1) == 0 and _field(station, 2) == 600   # station 0 (Valve 1), 10 min
    assert _field(body, 10) == 100                      # budgetPercent
    assert _field(body, 19) == 1                        # basicProgramMode


def test_encode_multiple_starts_and_stations():
    import bhyve_xd as B
    import schedule_device as SD
    body = _field(_unwrap(SD.encode_set_program_schedule(2, [360, 1200], [(0, 300), (3, 600)])), 19)
    assert _field(body, 1) == 2                          # Program B
    assert [v for fn, _, v in B.iter_fields(body) if fn == 8] == [360, 1200]   # two start times
    stations = [v for fn, _, v in B.iter_fields(body) if fn == 9]
    got = [(_field(s, 1), _field(s, 2)) for s in stations]
    assert got == [(0, 300), (3, 600)]                   # Valve 1 5min, Valve 4 10min (0-indexed)


def test_encode_set_active_and_parse_roundtrip():
    import schedule_device as SD
    assert SD.program_bit(1) == 1 and SD.program_bit(2) == 2 and SD.program_bit(3) == 4
    framed = SD.encode_set_active(SD.program_bit(1))
    body = _field(_unwrap(framed), 20)                   # setActivePrograms
    assert _field(body, 1) == 1                          # flags = bit0 (Program A)
    # parse_active_flags reads the device's reply shape (ActivePrograms under field 20)
    import bhyve_xd as B
    reply = B._fb(20, B._fv(1, 5))                       # flags = 0b101 (A + C)
    assert SD.parse_active_flags([reply]) == 5


def test_encode_get_active_is_empty_request():
    import schedule_device as SD
    body = _field(_unwrap(SD.encode_get_active()), 77)
    assert body == b""                                   # getActivePrograms takes no args


# --- P4c-1: pure rule -> device-program mapping ---------------------------------
def test_program_from_rules_single_daily():
    import schedule_device as SD
    r = [{"valve": 1, "start": "06:00", "days": [0, 1, 2, 3, 4, 5, 6], "minutes": 5, "enabled": True}]
    m = SD.program_from_rules(r)
    assert len(m["programs"]) == 1
    p = m["programs"][0]
    assert p["program_id"] == SD.PROGRAM_A and p["start_mins"] == [360]
    assert p["stations"] == [(0, 300)]            # valve1 -> station0 (0-idx), 5min -> 300s
    assert m["active_mask"] == 1                   # program A bit
    assert m["warnings"] == []


def test_program_from_rules_valve_and_time_conversion():
    import schedule_device as SD
    m = SD.program_from_rules([{"valve": 4, "start": "06:30", "days": list(range(7)),
                                "minutes": 10, "enabled": True}])
    p = m["programs"][0]
    assert p["start_mins"] == [390] and p["stations"] == [(3, 600)]   # 06:30, valve4->station3, 10min


def test_program_from_rules_only_enabled_rules_get_slots():
    import schedule_device as SD
    rules = [
        {"valve": 1, "start": "06:00", "days": list(range(7)), "minutes": 5, "enabled": True},
        {"valve": 2, "start": "20:00", "days": list(range(7)), "minutes": 5, "enabled": False},
        {"valve": 3, "start": "07:00", "days": list(range(7)), "minutes": 5, "enabled": True},
    ]
    m = SD.program_from_rules(rules)
    # disabled rule consumes NO slot; the two enabled rules become programs A and B
    assert [p["program_id"] for p in m["programs"]] == [1, 2]
    assert [p["stations"][0][0] for p in m["programs"]] == [0, 2]      # valves 1 and 3 (0-indexed)
    assert m["active_mask"] == (SD.program_bit(1) | SD.program_bit(2)) == 3


def test_program_from_rules_six_enabled_cap_ignores_disabled():
    import schedule_device as SD
    rules = [{"valve": 1, "start": f"0{i}:00", "days": list(range(7)), "minutes": 5,
              "enabled": False} for i in range(3)]                     # 3 disabled (no slots)
    rules += [{"valve": 1, "start": f"1{i}:00", "days": list(range(7)), "minutes": 5,
               "enabled": True} for i in range(6)]                     # 6 enabled -> A..F
    m = SD.program_from_rules(rules)
    assert len(m["programs"]) == 6                                     # disabled didn't starve the cap


def test_program_from_rules_specific_days_warns_daily():
    import schedule_device as SD
    m = SD.program_from_rules([{"valve": 1, "start": "06:00", "days": [0, 2, 4],
                                "minutes": 5, "enabled": True}])
    assert len(m["programs"]) == 1                 # still mapped (as daily)
    assert any("dai" in w.lower() or "day" in w.lower() for w in m["warnings"])


def test_program_from_rules_caps_at_six():
    import schedule_device as SD
    rules = [{"valve": 1, "start": f"0{i}:00", "days": list(range(7)), "minutes": 5,
              "enabled": True} for i in range(7)]
    m = SD.program_from_rules(rules)
    assert len(m["programs"]) == 6                 # A..F only
    assert any("6" in w for w in m["warnings"])    # warns the 7th is dropped


# --- P4c-2: arm re-enables active programs; device push (fake-BLE integration) ---
def test_api_push_and_clear_device_schedules(monkeypatch, tmp_path):
    """End-to-end (fake BLE): POST push stores the saved rules on the device, verifies via
    getActivePrograms, persists the mask; POST clear disables them."""
    import json
    import server
    import schedule as S
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "A", "address": "FAKE-ADDR", "mac": TEST_MAC,
                                            "network_key": TEST_KEY, "stations": 4}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_job", None, raising=False)
    S.write_schedules(str(cfg), TEST_MAC,
                      [{"valve": 1, "start": "06:00", "days": list(range(7)), "minutes": 5}])
    t = FakeTimer(mac=TEST_MAC)
    with one_device(t):
        res = asyncio.run(server.push_schedules(0))
        assert res["active_mask"] == 1 and res["verified"] is True
        assert t.active_mask == 1 and set(t.programs) == {1}
        assert asyncio.run(server.device_active(0))["device_active_mask"] == 1
        assert json.loads(cfg.read_text())["devices"][0]["device_active_mask"] == 1
        asyncio.run(server.clear_device_schedules(0))
    assert t.active_mask == 0
    assert json.loads(cfg.read_text())["devices"][0]["device_active_mask"] == 0


def test_api_push_502_and_not_saved_when_unverified(monkeypatch, tmp_path):
    """If the device doesn't confirm the read-back, push returns 502 and persists NOTHING."""
    from fastapi import HTTPException
    import json
    import server
    import schedule as S
    import schedule_device as SD
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "A", "address": "FAKE-ADDR", "mac": TEST_MAC,
                                            "network_key": TEST_KEY, "stations": 4}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_job", None, raising=False)
    S.write_schedules(str(cfg), TEST_MAC,
                      [{"valve": 1, "start": "06:00", "days": list(range(7)), "minutes": 5}])

    async def unconfirmed(sess, **kw):     # simulate a failed/garbled read-back
        return None
    monkeypatch.setattr(SD, "read_active_mask", unconfirmed)
    t = FakeTimer(mac=TEST_MAC)
    with one_device(t):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.push_schedules(0))
    assert exc.value.status_code == 502
    assert "device_active_mask" not in json.loads(cfg.read_text())["devices"][0]  # not persisted


def test_api_push_empty_rules_clears(monkeypatch, tmp_path):
    import json
    import server
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"devices": [{"name": "A", "address": "FAKE-ADDR", "mac": TEST_MAC,
                                            "network_key": TEST_KEY, "stations": 4}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_job", None, raising=False)
    t = FakeTimer(mac=TEST_MAC)
    with one_device(t):
        res = asyncio.run(server.push_schedules(0))       # no rules stored
    assert res["active_mask"] == 0 and res["verified"] is True and t.active_mask == 0


def test_api_push_bad_rule_is_400_not_504(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import json
    import server
    cfg = tmp_path / "config.json"
    # a hand-edited config with a malformed rule (bypasses write-time validation)
    cfg.write_text(json.dumps({"devices": [{"name": "A", "address": "FAKE-ADDR", "mac": TEST_MAC,
                                            "network_key": TEST_KEY, "stations": 4,
                                            "schedules": [{"valve": 1, "start": "BAD", "days": [0],
                                                           "minutes": 5}]}]}))
    monkeypatch.setattr(server, "CONFIG", str(cfg))
    monkeypatch.setattr(server, "_job", None, raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.push_schedules(0))
    assert exc.value.status_code == 400                    # data error, not a 504 BLE error


def test_control_session_reenables_active_programs():
    """SAFETY: arm() sends setActivePrograms{0} every connect. A control op on a device with
    device_active_mask set must RE-ENABLE it, so controlling a valve never wipes a schedule."""
    t = FakeTimer()
    with one_device(t):
        dev = BHyveXD("FAKE-ADDR", TEST_KEY, tz_offset_sec=0, stations=4, device_active_mask=5)
        asyncio.run(dev.status())
    assert t.active_mask == 5                     # SETUP_FIELD20 zeroed it; re-enable restored A+C


def test_control_without_mask_does_not_reenable():
    t = FakeTimer(); t.active_mask = 0
    with one_device(t):
        asyncio.run(BHyveXD("FAKE-ADDR", TEST_KEY, tz_offset_sec=0).status())  # mask defaults 0
    assert t.active_mask == 0


def test_encode_push_frames_orders_programs_then_active():
    import schedule_device as SD
    rules = [{"valve": 1, "start": "06:00", "days": list(range(7)), "minutes": 5, "enabled": True},
             {"valve": 3, "start": "07:00", "days": list(range(7)), "minutes": 5, "enabled": True}]
    info = SD.encode_push_frames(rules)
    assert info["active_mask"] == 3 and len(info["frames"]) == 3      # 2 programs (A,B) + setActive
    last = _field(_unwrap(info["frames"][-1]), 20)                    # final = setActivePrograms
    assert _field(last, 1) == 3


def test_push_program_to_device_persists_and_verifies():
    import schedule_device as SD
    t = FakeTimer()
    rules = [{"valve": 1, "start": "06:00", "days": list(range(7)), "minutes": 5, "enabled": True},
             {"valve": 3, "start": "07:00", "days": list(range(7)), "minutes": 5, "enabled": True}]
    with one_device(t):
        dev = BHyveXD("FAKE-ADDR", TEST_KEY, tz_offset_sec=0, stations=4)
        res = asyncio.run(SD.push_program_to_device(dev, rules))
    assert res["active_mask"] == 3 and res["verified"] is True
    assert t.active_mask == 3 and set(t.programs) == {1, 2}           # programs A + B stored
    assert dev.device_active_mask == 3                               # future control re-enables


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
