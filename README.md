# bhyve-xd-ble

Local Bluetooth control of the **Orbit B-Hyve XD** 4-port hose timer — no cloud,
no Wi-Fi hub. Set the clock, start/stop zones, and **read status back for
confirmation**, all from your own code over BLE.

Verified working on real hardware: **HT34A-0001, firmware 0107** (FCC ML6-HT34BT),
driven from Linux (BlueZ) via `bleak`. Self-confirmed end to end: the device
reports `watering, 300s remaining` after a start and `idle` after a stop.

> ⚠️ Unofficial, reverse-engineered. Not affiliated with Orbit. Do **not** update
> your device firmware — the protocol was reverse-engineered against fw 0107 and
> an update may change it.

## The one thing that makes this work

Many prior attempts fail because the device **decrypts your command correctly but
silently ignores it**. The fix, discovered here: the device only honors a command
sent **after the official app's full "arming" sequence** within the same
connection:

```
time_string, set_time, get_status, get_battery,
SETUP_FIELD22, SETUP_FIELD20, SETUP_FIELD120, time_string, set_time
```

Send that (`session.arm()`), **then** your `start_zone` / `stop` / `set_clock`.
An isolated command is ignored; the armed one works. This is the crux insight.

## How the protocol works (short version)

- GATT service `fe32`: `6c71` (AES handshake), `6c72` (data out), `6c73` (notify).
- **Handshake:** write 20 random bytes (byte[11]=0) to `6c71`, read 20 back.
  `IV = rx[:4] + init_tx[4:12]`; `tx_ctr = LE32(init_tx[12:16])`;
  `rx_ctr = LE32(init_tx[16:20])`.
- **Cipher:** AES-128-ECB used as a CTR keystream — `AES(key, IV||ctr_LE)` XOR data,
  counter +1 per 16-byte block, continuous across the session.
- **Framing:** each write is a `<=16`-byte plaintext chunk as
  `[0x11][len][ciphertext][trailer u16 LE]`, `trailer = (sum(chunk)+0x11+len) & 0xFFFF`.
  Longer messages are fragmented into chunks. Writes use **Write Command**
  (write-without-response).
- **Inner message:** `[AA 77 5A 0F][len][00][protobuf][crc16-ccitt]`.
- **Replies:** after arming, the device sends rich notifications. Decode with
  `rx_ctr` (+1/block; brute-force near the running value to resync). Useful fields:
  `7` = device clock (epoch), `16` = status (`sub1` run_state 1=idle/4=watering,
  `sub6.7` = seconds remaining), `46` = battery.

The **network key** is a 16-byte, account-specific secret fetched once from the
Orbit cloud (it decodes/controls all devices on the account — treat it like a
password). See `config.example.json`.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # then edit in your address + network key
```

- **Linux (BlueZ):** `address` is the MAC, e.g. `AA:BB:CC:DD:EE:FF`. Recommended
  platform — this is where it was verified.
- **macOS (CoreBluetooth):** `address` is an opaque per-Mac UUID (stable per
  peripheral per Mac); find it once by connecting. **Verified working end-to-end**
  on macOS — identical behavior to Linux (the arming sequence is platform-neutral).
- The timer is battery BLE — it only advertises briefly, so **press a button on
  the timer** to wake it right before running a command.

## Usage

```bash
python cli.py status            # read clock / watering state / battery
python cli.py settime           # sync the clock to now
python cli.py start 1 300       # start zone 1 for 300s (confirms watering)
python cli.py stop              # stop all zones (confirms idle)
python cli.py selftest          # autonomous: set clock -> read -> start -> read -> stop -> read
python cli.py scan              # list BLE devices (find the address)
```

Library use:

```python
from bhyve_xd import BHyveXD
dev = BHyveXD("AA:BB:CC:DD:EE:FF", "<network_key_hex>", tz_offset_sec=-14400)
async with dev.session() as s:
    await s.arm()                    # REQUIRED first
    await s.start_zone(1, 300)
    st = await s.read_status()
    print(st.is_watering, st.seconds_remaining)
    await s.stop()
```

## Architecture (one source of truth)

All Bluetooth/protocol logic lives in **`bhyve_xd.py`**. The CLI and the REST
server are thin wrappers that call the same high-level methods — no duplicated
control logic:

```
              bhyve_xd.py  (cipher · protocol · BHyveXD.status/start/stop/sync_clock · read-back)
                 ▲                     ▲
        cli.py ──┘                     └── server.py ──▶ index.html (web UI calls the REST API)
```

`BHyveXD.from_config()` is the single config loader; `DeviceStatus.to_dict()` is
the single serializer. Change the protocol once, in one place.

## Files

| File | Purpose |
|---|---|
| `bhyve_xd.py` | **the library** — cipher, protocol, `BHyveXD` controller + read-back (all logic) |
| `cli.py` | thin CLI over `bhyve_xd` |
| `server.py` | thin FastAPI REST API over `bhyve_xd` |
| `index.html` | web UI (calls the REST API) |
| `selftest_offline.py` | offline byte checks (no device) — validates message builders |
| `config.example.json` | copy to `config.json`, fill in address + network key |

## Credits

Protocol reverse-engineered with reference to the community projects
`wxfield/Orbit_B-Hyve_4Port_Controller`, `troxor/bhyve_ble`, and
`ljmerza/orbit-bhyve-ble`. The **full-arming-sequence** requirement and the
two-way read-back confirmation were established here on live HT34A hardware.
