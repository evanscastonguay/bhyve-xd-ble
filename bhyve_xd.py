"""
bhyve_xd — local BLE control of the Orbit B-Hyve XD (HT34A) hose timer.

Self-contained, no cloud, no Wi-Fi hub. Reverse-engineered and verified on real
hardware (HT34A-0001, firmware 0107). Provides two-way control: set the clock,
start/stop a zone, AND read the device's status back (clock, watering state,
battery) — a closed confirmation loop.

THE KEY INSIGHT (why naive attempts fail):
  The device decrypts our frames correctly but IGNORES an isolated command.
  It only acts on commands sent AFTER the official app's full "arming" sequence
  in the same connection: time_string, set_time, get_status, get_battery, three
  opaque setup messages, time_string, set_time. Send that, THEN your command.
  See BHyveXD.arm().

PROTOCOL SUMMARY:
  GATT service fe32, chars: 6c71 (AES handshake), 6c72 (data out), 6c73 (notify).
  Handshake: write 20 random bytes (byte[11]=0) to 6c71, read 20 back.
    IV      = rx[:4] + init_tx[4:12]
    tx_ctr  = uint32_LE(init_tx[12:16])   (our send counter)
    rx_ctr  = uint32_LE(init_tx[16:20])   (device reply counter)
  Cipher: AES-128-ECB used as a CTR keystream. keystream_block = AES(key, IV||ctr_LE),
    XOR with plaintext. Counter += 1 per 16-byte block, continuous across the session.
  Frame (per write, <=16-byte plaintext chunk): [0x11][len][ciphertext][trailer u16 LE]
    trailer = (sum(plaintext_chunk) + 0x11 + len) & 0xFFFF   (content checksum)
  Messages > ~21 bytes MUST be fragmented into <=16-byte chunks (each its own frame,
    counter continues per block). Writes use Write Command (write-without-response).
  Inner message: [AA 77 5A 0F][len][00][protobuf][crc16-ccitt u16 LE].

Dependencies: bleak, bleak-retry-connector, cryptography.
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# bleak / bleak_retry_connector are imported lazily inside _Session so that the
# protocol + cipher (and the offline self-test) work without a BLE stack.

# GATT characteristics (same across all B-Hyve BLE models).
AES_CHAR = "00006c71-fe32-4f58-8b78-98e42b2c047f"
WRITE_CHAR = "00006c72-fe32-4f58-8b78-98e42b2c047f"
READ_CHAR = "00006c73-fe32-4f58-8b78-98e42b2c047f"

MSG_HEADER = bytes([0xAA, 0x77, 0x5A, 0x0F])
FRAME_MAGIC = 0x11


class NotABHyveError(Exception):
    """A connected BLE device is not a B-Hyve timer (no fe32 GATT service).
    Lets the onboarding scan fast-reject unrelated devices before arming them."""


# --------------------------------------------------------------------------- #
# Protocol: protobuf + CRC16 message builders / reply parser
# --------------------------------------------------------------------------- #
def _crc16_ccitt(data: bytes, init: int = 0) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _varint(val: int) -> bytes:
    r = bytearray()
    while val > 0x7F:
        r.append((val & 0x7F) | 0x80)
        val >>= 7
    r.append(val & 0x7F)
    return bytes(r)


def _fv(f: int, v: int) -> bytes:   # protobuf varint field
    return _varint((f << 3) | 0) + _varint(v)


def _fb(f: int, d: bytes) -> bytes:  # protobuf length-delimited field
    return _varint((f << 3) | 2) + _varint(len(d)) + d


def _rd_varint(b: bytes, i: int):
    v = s = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << s
        if not x & 0x80:
            return v, i
        s += 7


def iter_fields(data: bytes):
    """Yield (field_num, wire_type, value) for one protobuf message level."""
    i, n = 0, len(data)
    while i < n:
        try:
            tag, i = _rd_varint(data, i)
        except IndexError:
            return
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _rd_varint(data, i)
            yield fn, 0, v
        elif wt == 2:
            ln, i = _rd_varint(data, i)
            yield fn, 2, data[i:i + ln]
            i += ln
        elif wt == 5:
            yield fn, 5, data[i:i + 4]; i += 4
        elif wt == 1:
            yield fn, 1, data[i:i + 8]; i += 8
        else:
            return


def _wrap(protobuf: bytes) -> bytes:
    """Wrap a protobuf body in the [AA775A0F][len][00]...[crc16] envelope."""
    msg = MSG_HEADER + bytes([len(protobuf) + 2, 0x00]) + protobuf
    return msg + struct.pack("<H", _crc16_ccitt(msg, 0))


# Command messages ----------------------------------------------------------- #
def msg_start(station: int, duration_sec: int) -> bytes:
    """Start a zone. station is 1-indexed (wire is 0-indexed)."""
    station_info = _fv(1, station - 1) + _fv(2, duration_sec)
    manual = _fb(3, station_info)
    timer_mode = _fv(1, 2) + _fb(2, manual)
    return _wrap(_fb(14, timer_mode))


def msg_stop() -> bytes:
    """Global stop — manual mode with empty station list (stops ALL zones)."""
    return _wrap(bytes.fromhex("720408021200"))


def msg_stop_zone(station: int) -> bytes:
    """Stop a single zone by commanding it to run for 0 seconds (manual mode
    with that station, duration 0). station is 1-indexed."""
    return msg_start(station, 0)


def msg_get_status() -> bytes:
    return _wrap(bytes.fromhex("7a00"))


def msg_get_battery() -> bytes:
    return _wrap(bytes.fromhex("ea0200"))


def msg_set_time(epoch: int, tz_offset_sec: int) -> bytes:
    """field 75 = setCurrentTime{1=epoch, 2=tz_offset_sec (signed, 2's-comp u64)}."""
    inner = _fv(1, epoch) + _fv(2, tz_offset_sec & ((1 << 64) - 1))
    return _wrap(_fb(75, inner))


def msg_time_string(iso: str) -> bytes:
    """field 18 = local time as ISO string, e.g. 2026-07-01T12:00:00-04:00."""
    return _wrap(_fb(18, _fb(1, iso.encode())))


# Opaque constant setup messages, captured verbatim from the app. Part of the
# arming sequence; their exact meaning is not needed, only that they are sent.
SETUP_FIELD22 = bytes.fromhex("aa775a0f0500b20100e1dc")
SETUP_FIELD20 = bytes.fromhex("aa775a0f0700a201020800353e")
SETUP_FIELD120 = bytes.fromhex("aa775a0f0700c207020a001266")


def host_tz_offset() -> int:
    """The host's current UTC offset in seconds (e.g. -14400 for EDT). Used to
    set the device clock to local time without a hardcoded offset."""
    return int(datetime.now().astimezone().utcoffset().total_seconds())


@dataclass
class DeviceStatus:
    device_time: int | None = None     # epoch seconds (device clock)
    run_state: int | None = None       # 1 = idle, 4 = watering
    seconds_remaining: int | None = None
    is_watering: bool = False
    device_mac: str | None = None      # "AA:BB:CC:DD:EE:FF" (reply field 1)

    @property
    def clock_str(self) -> str:
        if self.device_time is None:
            return "?"
        return datetime.fromtimestamp(self.device_time, timezone.utc).strftime("%H:%M:%S UTC")

    def to_dict(self) -> dict:
        """JSON-serializable view (used by the REST API and anywhere else)."""
        return {
            "clock": self.clock_str,
            "device_time": self.device_time,
            "is_watering": self.is_watering,
            "run_state": self.run_state,
            "seconds_remaining": self.seconds_remaining,
        }


def parse_reply(pt: bytes) -> DeviceStatus | None:
    """Parse a decrypted device reply (deviceStatusInfo) into a DeviceStatus."""
    if pt[:4] != MSG_HEADER:
        return None
    st = DeviceStatus()
    for fn, wt, v in iter_fields(pt[6:-2]):
        if fn == 1 and wt == 2 and isinstance(v, (bytes, bytearray)) and len(v) == 6:
            st.device_mac = ":".join(f"{b:02X}" for b in v)   # device's own MAC
        elif fn == 7 and wt == 0 and 1_700_000_000 < v < 2_000_000_000:
            st.device_time = v
        elif fn == 16 and wt == 2:  # deviceStatusInfo
            for sfn, swt, sv in iter_fields(v):
                if sfn == 1 and swt == 0:
                    st.run_state = sv
                    st.is_watering = (sv == 4)
                elif sfn == 6 and swt == 2:
                    st.is_watering = True
                    for tfn, twt, tv in iter_fields(sv):
                        if tfn == 7 and twt == 0:
                            st.seconds_remaining = tv
    return st


# --------------------------------------------------------------------------- #
# Cipher
# --------------------------------------------------------------------------- #
def _keystream_block(key: bytes, iv: bytes, ctr: int) -> bytes:
    return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(
        iv + struct.pack("<I", ctr & 0xFFFFFFFF))


# --------------------------------------------------------------------------- #
# Device controller
# --------------------------------------------------------------------------- #
class BHyveXD:
    """A B-Hyve XD timer. Two API levels:

    High-level (one call = one connection; used by the CLI + REST server):
        dev = BHyveXD.from_config("config.json")
        st = await dev.start(1, 300)     # arm + start zone 1 + read-back
        st = await dev.stop(1)           # arm + stop zone 1 + read-back
        st = await dev.stop()            # arm + stop ALL + read-back
        st = await dev.status()          # arm + read-back
        st = await dev.sync_clock()      # arm (sets clock to now) + read-back
        print(st.is_watering, st.seconds_remaining)

    Low-level (manual control within one connection):
        async with dev.session() as s:
            await s.arm()                # REQUIRED before any command
            await s.start_zone(1, 300)
            st = await s.read_status()
    """

    def __init__(self, address: str, network_key_hex: str, *, tz_offset_sec: int = -14400,
                 name: str = "B-Hyve XD", stations: int = 4):
        self.address = address
        self.key = bytes.fromhex(network_key_hex)
        self.tz_offset_sec = tz_offset_sec
        self.name = name
        self.stations = stations

    @classmethod
    def from_config(cls, path: str = "config.json", device: "int | str | None" = None) -> "BHyveXD":
        """Build a device from a config.json (see config.example.json). The one
        place config is loaded — shared by the CLI and the REST server.

        device selects which timer: None -> first, an int -> index, a str -> name.
        tz_offset_sec defaults to the host's offset when not set in config."""
        import json
        with open(path) as f:
            devices = json.load(f)["devices"]
        if device is None:
            d = devices[0]
        elif isinstance(device, int):
            d = devices[device]
        else:
            matches = [x for x in devices if x.get("name") == device]
            if not matches:
                raise KeyError(f"no device named {device!r} in {path} "
                               f"(have: {[x.get('name') for x in devices]})")
            d = matches[0]
        return cls(d["address"], d["network_key"],
                   tz_offset_sec=int(d["tz_offset_sec"]) if "tz_offset_sec" in d else host_tz_offset(),
                   name=d.get("name", "B-Hyve XD"),
                   stations=int(d.get("stations", 4)))

    def session(self, *, scan_timeout: float = 30.0, connect_attempts: int = 3):
        """Low-level: open a BLE session for manual arm()/command/read control.
        connect_attempts=1 makes onboarding probes fail fast on the wrong device."""
        return _Session(self, scan_timeout, connect_attempts)

    # -- High-level one-shot operations ----------------------------------- #
    # Each opens its own connection, ARMS the device (required), performs the
    # action, and returns the confirmed DeviceStatus. This is THE shared control
    # logic used by both the CLI and the REST server — no duplication.

    async def status(self) -> DeviceStatus:
        async with self.session() as s:
            await s.arm()
            return await s.read_status()

    async def sync_clock(self) -> DeviceStatus:
        async with self.session() as s:
            await s.arm()          # arm() already sets the clock to now
            return await s.read_status()

    async def start(self, station: int, duration_sec: int) -> DeviceStatus:
        async with self.session() as s:
            await s.arm()
            await s.start_zone(station, duration_sec)
            return await s.read_status()

    async def stop(self, station: int | None = None) -> DeviceStatus:
        """Stop one zone (station given) or ALL zones (station=None)."""
        async with self.session() as s:
            await s.arm()
            if station is None:
                await s.stop()
            else:
                await s.stop_zone(station)
            return await s.read_status()


class _Session:
    def __init__(self, dev: BHyveXD, scan_timeout: float, connect_attempts: int = 3):
        self._dev = dev
        self._scan_timeout = scan_timeout
        self._connect_attempts = connect_attempts
        self._client: BleakClient | None = None
        self._iv: bytes | None = None
        self._tx = 0
        self._rx = 0            # running device-reply counter
        self._notifs: list[bytes] = []

    async def __aenter__(self):
        from bleak import BleakClient, BleakScanner
        from bleak_retry_connector import establish_connection
        d = self._dev
        ble = await BleakScanner.find_device_by_address(d.address, timeout=self._scan_timeout)
        if ble is None:
            raise RuntimeError(f"{d.address} not found — is the timer awake (press its button)?")
        self._client = await establish_connection(BleakClient, ble, d.address,
                                                  max_attempts=self._connect_attempts)
        # Fast-reject: only B-Hyve timers expose the fe32 service. This stops the
        # macOS onboarding scan from arming unrelated BLE devices it probes.
        if not any("fe32" in s.uuid.lower() for s in self._client.services):
            await self._client.disconnect()
            raise NotABHyveError(f"{d.address} is not a B-Hyve device (no fe32 service)")
        await self._client.start_notify(READ_CHAR, lambda _s, data: self._notifs.append(bytes(data)))
        await asyncio.sleep(0.5)
        # AES handshake
        init_tx = bytearray(os.urandom(20))
        init_tx[11] = 0x00
        init_tx = bytes(init_tx)
        await self._client.write_gatt_char(AES_CHAR, init_tx)
        rx = bytes(await self._client.read_gatt_char(AES_CHAR))
        self._iv = rx[:4] + init_tx[4:12]
        self._tx = struct.unpack("<I", init_tx[12:16])[0]
        self._rx = struct.unpack("<I", init_tx[16:20])[0]
        return self

    async def __aexit__(self, *exc):
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    # -- low level -------------------------------------------------------- #
    def _enc_chunk(self, chunk: bytes) -> bytes:
        ks = _keystream_block(self._dev.key, self._iv, self._tx)
        ct = bytes(a ^ b for a, b in zip(chunk, ks[:len(chunk)]))
        self._tx = (self._tx + 1) & 0xFFFFFFFF
        trailer = (sum(chunk) + FRAME_MAGIC + len(chunk)) & 0xFFFF
        return bytes([FRAME_MAGIC, len(ct)]) + ct + struct.pack("<H", trailer)

    async def _send(self, message: bytes, *, settle: float = 0.12):
        """Fragment a message into <=16B chunks and write each (Write Command)."""
        for i in range(0, len(message), 16):
            await self._client.write_gatt_char(WRITE_CHAR, self._enc_chunk(message[i:i + 16]),
                                                response=False)
        await asyncio.sleep(settle)

    def _iso_now(self) -> str:
        tz = timezone(timedelta(seconds=self._dev.tz_offset_sec))
        t = datetime.now(tz)
        z = t.strftime("%z")
        return t.strftime("%Y-%m-%dT%H:%M:%S") + z[:3] + ":" + z[3:]

    # -- public API ------------------------------------------------------- #
    async def arm(self):
        """Send the app's full setup sequence. REQUIRED before any command will
        be honored. Sets the clock to now as part of the sequence."""
        iso = self._iso_now()
        ts, st = msg_time_string(iso), msg_set_time(int(time.time()), self._dev.tz_offset_sec)
        for m in (ts, st, msg_get_status(), msg_get_battery(),
                  SETUP_FIELD22, SETUP_FIELD20, SETUP_FIELD120, ts, st):
            await self._send(m)

    async def set_clock(self, dt: datetime):
        """Set the device clock to a specific local datetime (used by selftest;
        normal clock sync happens inside arm())."""
        z = dt.strftime("%z") or "+0000"
        iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + z[:3] + ":" + z[3:]
        await self._send(msg_time_string(iso))
        await self._send(msg_set_time(int(dt.timestamp()), self._dev.tz_offset_sec))

    async def start_zone(self, station: int, duration_sec: int):
        await self._send(msg_start(station, duration_sec))

    async def stop(self):
        """Stop ALL zones."""
        await self._send(msg_stop())

    async def stop_zone(self, station: int):
        """Stop a single zone (manual watering, 0 seconds)."""
        await self._send(msg_stop_zone(station))

    async def read_status(self, *, wait: float = 1.5) -> DeviceStatus:
        """Query status and decode the newest valid reply. The device's replies
        are the source of truth for confirmation."""
        self._notifs.clear()
        await self._send(msg_get_status())
        await asyncio.sleep(wait)
        best = None
        for d in self._notifs:
            if len(d) < 6 or d[0] != FRAME_MAGIC:
                continue
            ct = d[2:2 + d[1]]
            # brute-force the reply counter near the running value to resync
            for off in range(-4, 80):
                c = (self._rx + off) & 0xFFFFFFFF
                out = bytearray()
                cc = c
                for i in range(0, len(ct), 16):
                    out += bytes(a ^ b for a, b in
                                 zip(ct[i:i + 16], _keystream_block(self._dev.key, self._iv, cc)))
                    cc = (cc + 1) & 0xFFFFFFFF
                pt = bytes(out)
                if pt[:4] == MSG_HEADER:
                    self._rx = cc
                    best = parse_reply(pt)
                    break
        return best or DeviceStatus()
