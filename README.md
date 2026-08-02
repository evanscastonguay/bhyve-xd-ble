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

## Status — the full lifecycle, app-free

This project can take a timer from **factory-fresh to durably controllable with the Orbit
app never opened** — the last step of that is proven, not just claimed.

**Solved (verified on real hardware):**
- **Control** — status / start / stop, each confirmed by the device's own read-back.
- **Onboarding** — one command (`cli.py register`), a first-run cloud flow, and a web wizard.
- **App-free enrollment** — `cli.py provision` writes the key onto a factory-reset device
  (char `6c76`) and replays the app's finalize sequence. **PROVEN durable (2026-08-02):** a
  proof run (`provision_proof.py`) confirmed the device was **keyless first** (negative
  control), provisioned it **app-free**, and it **survived 3 power-cycles**, each re-verified
  in a fresh process. The device drops the BLE link after keying, so provisioning is
  two-phase (write → reconnect → finalize); it takes ~2–3 min.

**Not yet / honest caveats:**
- Proven on **one device, macOS**; not repeated across devices/models (finalize bytes are
  **HT34 4-station** specific) or on Linux/BlueZ.
- Enrolls for **local control only** — not registered in Orbit's cloud/app.
- Two key modes exist: **Orbit** (`provision` / `register` — account key, app + our tools
  interoperate) and **self-key** (`provision --self-key` — our own generated key, no Orbit
  account; the key is stashed in `secrets/` and the Orbit app can't control the device).
  **Both modes now have an airtight, confound-free proof-harness PASS on hardware**
  (2026-08-02): a timer keyed with **our own** key (no Orbit account, app never opened) was
  confirmed keyless first, provisioned, and **survived 3 power-cycles**, each re-verified in
  a fresh process — the same bar the Orbit-key path met.

Full detail + evidence: `PROJECT_STATUS.md`, `SPIKE_provisioning.md`, `provision_proof.py`.

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
python cli.py register          # discover + save a NEW timer to config.json (see below)
```

With several timers in `config.json`, target one with `--device <name|index>` (default
= the first device):

```bash
python cli.py status --device "New Timer"
python cli.py start 1 300 --device 1
```

## Register a new timer

`cli.py register` turns a new timer into a working `config.json` entry in one command —
no hand-editing, no hand-copying keys or addresses:

```bash
python cli.py register --name "Back Yard"
```

What it does: reuse the account **network key already in `config.json`** (so adding
another timer on the same account needs **no cloud login**; the very first device logs
in once to fetch the key) → prompt you to release the phone → **catch the timer's live
advertisement** → read its own MAC + status back → write `config.json` → confirm.

### First run (no config yet)

Starting from **nothing**, give your Orbit email; it logs in once, fetches the account
key, and onboards the device — verified end-to-end on real hardware:

```bash
python cli.py register you@example.com --name "Back Yard"
```

- You'll be prompted for your Orbit password at a **hidden** prompt (never stored, never
  logged; MFA accounts aren't supported and are reported clearly).
- If several timers are on the account, you'll get a **numbered chooser** (or pass
  `--device-mac <MAC>` to skip it).
- After this first run the key is saved, so every later timer skips the cloud login.

### Enroll a factory-fresh device (app-free)

`cli.py provision` enrolls a **factory-reset / brand-new** timer **without the Orbit app** —
it writes the account key onto the device itself, then saves it:

```bash
python cli.py provision            # (or: python cli.py provision you@example.com)
```

Factory-reset the timer into **pairing mode** (dial to OFF, hold the dial ~10 s until the
full display lights up), turn the **phone's Bluetooth OFF**, then run it. It writes the
account key to the device's provisioning characteristic (`6c76`), verifies with a normal
read-back, and saves `config.json`. The account key comes from your existing config or a
one-time cloud login, same as `register`. (Mechanism reverse-engineered in
`SPIKE_provisioning.md`.)

> **The one precondition:** turn your **phone's Bluetooth OFF** before pressing Enter. A
> B-Hyve accepts only one BLE connection and won't advertise while the phone holds it —
> with the phone released, the timer advertises freely and is caught reliably (this also
> handles units that use a rotating/private BLE address). Add `--device-mac <MAC>` to
> target a specific unit when several are in range.

**Or from the browser:** run the server (`uvicorn server:app`), open the web UI, and
click **＋ Add timer** — a wizard runs the same flow (reuse the saved key or log in
once, catch the timer, save it) with live status and error hints. Same phone-Bluetooth-OFF
precondition.

For hands-on debugging (repeated start/stop on one held connection, with a timestamped
log), `bhyve_lab.py` offers an interactive menu built on the same discovery code.

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

## Testing

Two layers, both hardware-free and repeatable:

```bash
python selftest_offline.py                       # 55 offline byte/logic checks
pip install -r requirements-dev.txt
python -m pytest test_e2e.py -q                  # 37 end-to-end tests (~0.6s)
```

`test_e2e.py` runs every operation against a **fake B-Hyve** (`FakeTimer`) that
emulates the HT34A at the byte level — AES handshake, frame decrypt/reassembly,
watering state, and correctly-encrypted status notifications. bleak and aiohttp
are patched, so no radio or network is touched. Reading `is_watering=True` back
proves the whole chain: `arm → command → encrypted notification → counter resync
→ parse → DeviceStatus`, plus the CLI, REST API, macOS pairing, and cloud login.

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
| `onboarding.py` | cloud login (`cloud_fetch`), discovery (`catch_device`), and config write (`write_config`) — powers `cli.py register` |
| `bhyve_lab.py` | interactive live-control diagnostic (reuses `onboarding.catch_device_session`) |
| `selftest_offline.py` | offline byte checks (no device) — validates message builders |
| `test_e2e.py` | end-to-end tests against a fake device (no hardware) — every operation |
| `config.example.json` | copy to `config.json`, fill in address + network key |

## Credits

Protocol reverse-engineered with reference to the community projects
`wxfield/Orbit_B-Hyve_4Port_Controller`, `troxor/bhyve_ble`, and
`ljmerza/orbit-bhyve-ble`. The **full-arming-sequence** requirement and the
two-way read-back confirmation were established here on live HT34A hardware.
