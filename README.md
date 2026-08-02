# bhyve-xd-ble

Local Bluetooth control of the **Orbit B-Hyve XD** 4-port hose timer — no cloud,
no Wi-Fi hub, no Orbit app required. Set the clock, start/stop any zone, run a
watering schedule, and **read the device's own state back for confirmation** — all
from your own code, over BLE.

Verified on real hardware — **HT34A-0001, firmware 0107** (FCC ML6-HT34BT) — on both
Linux (BlueZ) and macOS (CoreBluetooth) via [`bleak`](https://github.com/hbldh/bleak).
Every action self-confirms: the device reports `watering, 300s remaining` after a
start and `idle` after a stop.

> ⚠️ Unofficial and reverse-engineered; not affiliated with Orbit. **Don't update the
> timer's firmware** — the protocol was reverse-engineered against fw 0107 and an
> update may change it.

## What works

| Capability | Status |
|---|---|
| **Control** — status · start · stop · set clock | ✅ Verified, each confirmed by device read-back |
| **Scheduling** — per-valve rules, host-driven | ✅ Verified end-to-end (the server fires each valve at its time) |
| **Onboarding** — add a timer from your Orbit account | ✅ `cli.py register` (CLI) or the web wizard |
| **App-free enrollment** — key a factory-fresh timer | ✅ Durable across power-cycles (macOS, one device) |
| On-device standalone schedules (runs without a host) | ⚠️ Dormant — the timer accepts a program but was never seen to run one autonomously |

Full, honest caveats are in [Status & caveats](#status--caveats) at the bottom.

## Quick start

**Already have a network key?** (see [The network key](#the-network-key)) — three steps to a confirmed status read:

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # edit in your address + network key
./venv/bin/python cli.py status         # press a button on the timer first to wake it
```

You should see the clock, watering state, and battery read back from the device.

**Starting from nothing?** Jump to [Add a timer](#add-a-timer) — one command logs into your
Orbit account, fetches the key, finds the timer, and writes `config.json` for you.

### Platform & prerequisites

- **Linux (BlueZ):** `address` in `config.json` is the **MAC**, e.g. `AA:BB:CC:DD:EE:FF`.
- **macOS (CoreBluetooth):** `address` is an opaque per-Mac **UUID** (stable per peripheral,
  per Mac); `cli.py scan` / `register` finds it for you. Behavior is identical to Linux —
  the arming sequence is platform-neutral.
- The timer is **battery BLE** and only advertises briefly, so **press a button on the timer**
  to wake it right before running a command.

## Usage

```bash
python cli.py status            # read clock / watering state / battery
python cli.py settime           # sync the clock to now
python cli.py start 1 300       # start zone 1 for 300s (confirms watering)
python cli.py stop              # stop all zones (confirms idle)
python cli.py selftest          # set clock → read → start → read → stop → read
python cli.py scan              # list BLE devices (find the address)
python cli.py register          # discover + save a NEW timer to config.json
```

With several timers in `config.json`, target one with `--device <name|index>` (default =
the first device):

```bash
python cli.py status --device "New Timer"
python cli.py start 1 300 --device 1
```

### Library use

```python
from bhyve_xd import BHyveXD
dev = BHyveXD("AA:BB:CC:DD:EE:FF", "<network_key_hex>", tz_offset_sec=-14400)
async with dev.session() as s:
    await s.arm()                    # REQUIRED first — see "Why arming matters"
    await s.start_zone(1, 300)
    st = await s.read_status()
    print(st.is_watering, st.seconds_remaining)
    await s.stop()
```

## Scheduling (host-driven)

Author per-valve watering rules in the web UI (**🗓 Schedule** tab): valve · start time · days ·
duration. Then choose how they run:

- **On this Mac** — the server fires each valve at its scheduled time over Bluetooth (the device
  auto-stops after the duration). **Verified end-to-end.** A failed/timed-out fire is retried on
  the next tick; a rule fires once per day even if a tick is late (bounded catch-up). The Schedule
  tab shows the next run and whether the last one succeeded (`GET /api/scheduling`).
- **Off** — rules are saved but nothing runs them.

**This only runs while the server (and host) is up and in Bluetooth range.** On macOS, keep it awake:

```bash
caffeinate -s uvicorn server:app --host 0.0.0.0 --port 8000
```

If the host sleeps, is off, or is out of range at a scheduled time, that run is missed — which is
why the next step is running the server on an always-on Linux box near the timer.

> On-device (standalone) scheduling — storing the program on the timer so it runs without a host —
> is **not enabled**: the timer accepts and activates a program byte-identically to the Orbit app,
> but was never seen to autonomously execute one in testing (see `docs/SPIKE_schedule.md`). That
> code stays in the repo, tested but dormant, pending confirmation that this unit runs schedules at all.

## Add a timer

### From your Orbit account (`register`)

`cli.py register` turns a new timer into a working `config.json` entry in one command — no
hand-editing, no copying keys or addresses:

```bash
python cli.py register --name "Back Yard"                 # reuse the key already in config.json
python cli.py register you@example.com --name "Back Yard" # first run: log in once, fetch the key
```

It reuses the account key already in `config.json` when present (so adding another timer needs
**no cloud login**), otherwise logs in once, then: prompts you to release the phone → **catches the
timer's live advertisement** → reads its MAC + status back → writes `config.json` → confirms.

- First-run password is read at a **hidden** prompt (never stored, never logged; MFA accounts are
  unsupported and reported clearly).
- Several timers on the account → a **numbered chooser** (or pass `--device-mac <MAC>` to skip it).

**Or from the browser:** run the server (`uvicorn server:app`), open the web UI, click **＋ Add
timer** — a wizard runs the same flow with live status and error hints.

### Factory-fresh, app-free (`provision`)

`cli.py provision` enrolls a **factory-reset / brand-new** timer **without the Orbit app** — it
writes the account key onto the device itself (char `6c76`), then saves it:

```bash
python cli.py provision                 # (or: python cli.py provision you@example.com)
```

Factory-reset the timer into **pairing mode** (dial to OFF, hold ~10 s until the full display
lights), turn the **phone's Bluetooth OFF**, then run it.

> **The one precondition:** phone Bluetooth **OFF** before you press Enter. A B-Hyve accepts only
> one BLE connection and won't advertise while the phone holds it — released, it advertises freely
> and is caught reliably. Add `--device-mac <MAC>` to target a specific unit.

Two key modes: **Orbit** (`provision` — the account key; the Orbit app and our tools interoperate)
and **self-key** (`provision --self-key` — our own generated key, no Orbit account; stashed in
`secrets/`, and the Orbit app can no longer control the device). Both are proven durable on hardware
(see [Status & caveats](#status--caveats)). Mechanism: `docs/SPIKE_provisioning.md`.

## How it works

### The network key

The **network key** is a 16-byte, account-specific secret fetched once from the Orbit cloud. It
decodes and controls **every** device on the account — treat it like a password. See
`config.example.json`; `register` fetches it for you on first run.

### Why arming matters (the crux)

Many prior attempts fail because the device **decrypts your command correctly but silently ignores
it**. The fix discovered here: the device only honors a command sent **after the official app's full
"arming" sequence**, within the same connection:

```
time_string, set_time, get_status, get_battery,
SETUP_FIELD22, SETUP_FIELD20, SETUP_FIELD120, time_string, set_time
```

Send that (`session.arm()`), **then** your `start_zone` / `stop` / `set_clock`. An isolated command
is ignored; the armed one works.

### The protocol (short version)

- **GATT service `fe32`:** `6c71` (AES handshake), `6c72` (data out), `6c73` (notify).
- **Handshake:** write 20 random bytes (byte[11]=0) to `6c71`, read 20 back. `IV = rx[:4] +
  init_tx[4:12]`; `tx_ctr = LE32(init_tx[12:16])`; `rx_ctr = LE32(init_tx[16:20])`.
- **Cipher:** AES-128-ECB as a CTR keystream — `AES(key, IV||ctr_LE)` XOR data, +1 per 16-byte
  block, continuous across the session.
- **Framing:** each write is a `<=16`-byte plaintext chunk as `[0x11][len][ciphertext][trailer u16
  LE]`, `trailer = (sum(chunk)+0x11+len) & 0xFFFF`; longer messages are fragmented. Writes use
  **Write Command** (write-without-response).
- **Inner message:** `[AA 77 5A 0F][len][00][protobuf][crc16-ccitt]`.
- **Replies:** after arming, rich notifications; decode with `rx_ctr` (brute-force near the running
  value to resync). Fields: `7` = clock (epoch), `16` = status (`sub1` run_state 1=idle/4=watering,
  `sub6.7` = seconds remaining), `46` = battery.

Full reverse-engineering detail: `docs/SPIKE_provisioning.md`, `docs/SPIKE_schedule.md`.

## Architecture

All Bluetooth/protocol logic lives in **`bhyve_xd.py`**. The CLI and the REST server are thin
wrappers that call the same high-level methods — no duplicated control logic:

```
       bhyve_xd.py  (cipher · protocol · BHyveXD.status/start/stop/sync_clock · read-back)
          ▲                     ▲
 cli.py ──┘                     └── server.py ──▶ index.html (web UI calls the REST API)
```

`BHyveXD.from_config()` is the single config loader; `DeviceStatus.to_dict()` the single serializer.
Change the protocol once, in one place.

| File | Purpose |
|---|---|
| `bhyve_xd.py` | **the library** — cipher, protocol, `BHyveXD` controller + read-back (all logic) |
| `cli.py` | thin CLI over `bhyve_xd` |
| `server.py` | thin FastAPI REST API + host scheduler over `bhyve_xd` |
| `index.html` | web UI (calls the REST API) |
| `onboarding.py` | cloud login, device discovery, and config write — powers `register` |
| `schedule.py` / `scheduler.py` | host-scheduling store + due-rule / next-run logic |
| `schedule_device.py` | on-device schedule codec (dormant — see `docs/SPIKE_schedule.md`) |
| `provision_proof.py` | live-hardware proof of durable provisioning |
| `test_e2e.py` | end-to-end tests against a fake device (no hardware) |
| `config.example.json` | copy to `config.json`, fill in address + network key |
| `docs/` | protocol reference (`SPIKE_*`), `plans/`, and `archive/` |

## Testing

Hardware-free and repeatable:

```bash
pip install -r requirements-dev.txt
python -m pytest test_e2e.py -q                  # 131 tests, no device needed (~1s)
```

`test_e2e.py` runs every operation against a **fake B-Hyve** (`FakeTimer`) that emulates the HT34A
at the byte level — AES handshake, frame decrypt/reassembly, watering state, correctly-encrypted
status notifications. `bleak` and `aiohttp` are patched, so no radio or network is touched. Reading
`is_watering=True` back proves the whole chain: `arm → command → encrypted notification → counter
resync → parse → DeviceStatus`, plus the CLI, REST API, macOS pairing, and cloud login.

## Status & caveats

**Proven on real hardware (2026-08-02):**

- **Control & scheduling** — status/start/stop each confirmed by the device's own read-back;
  host-driven scheduling fired a valve on time with a read-back-confirmed start.
- **App-free provisioning is durable** — `provision_proof.py` confirmed a device was **keyless
  first** (negative control), provisioned it **app-free**, and it **survived 3 power-cycles**, each
  re-verified in a fresh process. **Both** key modes (Orbit key and self-key, app never opened) met
  this same bar. The device drops the BLE link after keying, so provisioning is two-phase
  (write → reconnect → finalize) and takes ~2–3 min.

**Honest limits:**

- Provisioning is proven on **one device** (finalize bytes are **HT34 4-station** specific); not
  repeated across devices/models or re-verified on Linux/BlueZ.
- Enrolls for **local control only** — the device is not registered in Orbit's cloud/app.
- Host-driven scheduling runs **only while the host is awake, serving, and in BLE range**.
- **On-device standalone schedules are unverified** — accepted and activated byte-identically to the
  app, but never seen to run autonomously; kept in the repo, tested but dormant.

Detail + evidence: `docs/SPIKE_provisioning.md`, `docs/SPIKE_schedule.md`, `provision_proof.py`,
`docs/archive/PROJECT_STATUS.md`.

## Credits

Protocol reverse-engineered with reference to the community projects
`wxfield/Orbit_B-Hyve_4Port_Controller`, `troxor/bhyve_ble`, and `ljmerza/orbit-bhyve-ble`. The
**full-arming-sequence** requirement and the two-way read-back confirmation were established here on
live HT34A hardware.
