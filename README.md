# bhyve-xd-ble

Local Bluetooth control of the **Orbit B-Hyve XD** 4-port hose timer — no cloud, no Wi-Fi hub, no
Orbit app required. It runs as a **small always-on local server next to the timer** — a browser UI,
a REST API, and a CLI — that sets the clock, starts/stops any zone, runs a watering schedule, and
**reads the device's own state back for confirmation**, all over BLE.

Verified on real hardware — **HT34A-0001, firmware 0107** (FCC ML6-HT34BT) — on both Linux (BlueZ)
and macOS (CoreBluetooth) via [`bleak`](https://github.com/hbldh/bleak). Every action self-confirms:
the device reports `watering, 300s remaining` after a start and `idle` after a stop.

> ⚠️ Unofficial and reverse-engineered; not affiliated with Orbit. **Don't update the timer's
> firmware** — the protocol was reverse-engineered against fw 0107 and an update may change it.

## What works

| | |
|---|---|
| **Control** | status · start · stop · set clock — each confirmed by the device's own read-back ✅ |
| **Scheduling** | per-valve watering rules, run automatically by the server ✅ (verified end-to-end) |
| **Onboarding** | add a timer from your Orbit account — `cli.py register` or the web wizard ✅ |
| **App-free enrollment** | key a factory-fresh timer with no Orbit app ✅ (durable across power-cycles) |
| **Interfaces** | **Web UI** · **REST API** · **CLI** — same shared control core |
| **Deployment** | one command (`deploy/deploy.sh`) to an always-on Linux box: systemd, test-gated, health-checked, **auto-rollback** ✅ |
| On-device standalone schedules (runs with no host) | ⚠️ dormant — the timer accepts a program but was never seen to run one autonomously |

Honest caveats are in [Status & caveats](#status--caveats).

## Quick start

**Before you begin — you need all of:**
- ☐ An Orbit **B-Hyve XD** hose timer (HT34 **4-station** is what's verified).
- ☐ The host (Mac / Raspberry Pi / Linux box) **within Bluetooth range (~10 m)** of the timer.
- ☐ An **Orbit account** — to fetch the BLE key (`register`) — **or** a factory-fresh timer to key it
  yourself (`provision --self-key`).
- ☐ Your **phone's Bluetooth OFF** — the timer accepts only one BLE connection, so the app/phone must
  not be holding it.
- ☐ **Python 3.10+**; on Linux, a running **BlueZ** stack (`bluetoothd`).

The timer is battery BLE and only advertises briefly, so **press a button on it** to wake it right
before a first command.

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

**A) Try it from the CLI (starting from nothing).** One command logs into Orbit once, fetches the
key, finds the timer, and writes `config.json`:

```bash
./venv/bin/python cli.py register you@example.com --name "Back Yard"
./venv/bin/python cli.py status            # clock / watering state / battery, read back live
```

**B) Run the web UI + API.** Start the server and open it in any browser on your network:

```bash
./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
# open http://<this-host>:8000/  →  ＋ Add timer (same onboarding flow), then control + schedule
```

> **Security:** the server has **no login**, and `--host 0.0.0.0` exposes it to your whole network.
> Run it only on a trusted LAN — see [Security & scope](#security--scope). To keep it to this machine
> only, use `--host 127.0.0.1`.

**Already have a `config.json`** (address + network key)? Skip registration — `cli.py status` or the
server just work. See [The network key](#the-network-key) for what goes in it.

- **Linux (BlueZ):** `address` in `config.json` is the **MAC** (`AA:BB:CC:DD:EE:FF`).
- **macOS (CoreBluetooth):** `address` is an opaque per-Mac **UUID**; `cli.py scan` / `register`
  finds it for you. Behavior is identical to Linux — the arming sequence is platform-neutral.

## Interfaces

Three ways in, one shared control core (`bhyve_xd.py`) — pick by task:

- **Web UI** (`/`) — the everyday interface: per-valve ON/OFF with a run time, multi-timer tabs, the
  schedule editor, and the **Add timer** wizard. Loads instantly; status fills in a moment later
  (it's [cached server-side](#a-note-on-latency)).
- **REST API** — automate it: `GET /api/status`, `GET /api/version`, `POST /api/zones/{z}/start`,
  `POST /api/zones/{z}/stop`, `POST /api/stop`, `GET|PUT /api/scheduling`, plus the onboarding routes.
- **CLI** (`cli.py`) — scripting and setup from a terminal.

## Usage (CLI)

```bash
python cli.py status            # read clock / watering state / battery
python cli.py settime           # sync the clock to now
python cli.py start 1 300       # start zone 1 for 300s (confirms watering)
python cli.py stop              # stop all zones (confirms idle)
python cli.py selftest          # set clock → read → start → read → stop → read
python cli.py scan              # list BLE devices (find the address)
python cli.py register          # discover + save a NEW timer to config.json
```

With several timers in `config.json`, target one with `--device <name|index>` (default = the first).

```python
# library use
from bhyve_xd import BHyveXD
dev = BHyveXD("AA:BB:CC:DD:EE:FF", "<network_key_hex>", tz_offset_sec=-14400)
async with dev.session() as s:
    await s.arm()                    # REQUIRED first — see "Why arming matters"
    await s.start_zone(1, 300)
    st = await s.read_status()
    print(st.is_watering, st.seconds_remaining)
    await s.stop()
```

## Scheduling

Author per-valve rules in the web UI (**🗓 Schedule** tab): valve · start time · days · duration.
Then pick how they run:

- **Automatic** — the server fires each valve at its scheduled time over Bluetooth (the device
  auto-stops after the duration). **Verified end-to-end.** A failed/timed-out fire is retried on the
  next tick; a rule fires once per day even if a tick is late (bounded catch-up). The tab shows the
  next run and whether the last one succeeded (`GET /api/scheduling`).
- **Off** — rules are saved but nothing runs them.

Automatic scheduling runs **only while the server is up and in Bluetooth range** — which is exactly
why you run it on an always-on box (next section), not a laptop that sleeps.

> On-device (standalone) scheduling — storing the program on the timer so it runs with no host — is
> **not enabled**: the timer accepts and activates a program byte-identically to the Orbit app, but
> was never seen to execute one autonomously in testing (see `docs/SPIKE_schedule.md`). That code
> stays in the repo, tested but dormant.

## Run it 24/7 (Linux)

For real use you want the server always on, next to the timer — a Raspberry Pi or any always-on
Linux box, not your laptop. The repo ships a small, robust deployment setup:

- **Immutable releases:** each deploy lands under `~/bhyve/releases/<timestamp>_<sha>/` with its own
  venv; `config.json` + `secrets/` live once in `~/bhyve/shared/` and are symlinked into every
  release (so an update can never clobber your key); a `~/bhyve/current` symlink is what runs.
- **systemd service** (`bhyve.service`) runs `uvicorn` on boot and restarts on failure — survives
  reboots, no terminal needed. (On a laptop, set logind `HandleLidSwitch=ignore` so a closed lid
  doesn't suspend it.)
- **One-command updates from your dev machine:**

  ```bash
  ./deploy/deploy.sh        # from the repo on your Mac/dev box
  ```

  It runs the **test suite** (aborts on red), rsyncs a new release, builds its venv, stamps the
  version, **atomically** flips `current`, restarts the service, then **health-checks** the new
  release (`/api/version` + `/`) and **automatically rolls back** to the previous release if it
  doesn't come up. A bad deploy can't take down watering.

One-time box setup (the release layout, the systemd unit, and a scoped `sudoers` line for
password-less restart) is documented in **`deploy/README.md`**.

## Security & scope

**LAN-only, no login.** The server has **no authentication**, and it binds all interfaces
(`--host 0.0.0.0`), so anything on your network can reach `http://<host>:8000` and **control your
valves** (and hit the onboarding/login endpoints). Run it **only on a trusted home network — do not
port-forward it or expose it to the internet.** To restrict it to the machine itself, bind
`--host 127.0.0.1`. Treat the **network key** in `config.json` like a password: it controls **every**
timer on your Orbit account.

**What it deliberately does _not_ do:**
- **No remote control from outside your home** — local BLE + LAN only, no cloud relay.
- **No scheduling while the host is off** — automatic runs need the server up and in range (hence the
  always-on box).
- **No web-UI accounts / multi-user** — anyone who can open the page has full control.
- **Only HT34 4-station is verified**; other B-Hyve models are untested, and **MFA Orbit accounts are
  not supported**.
- **No on-device standalone schedules** (see [Scheduling](#scheduling)).

## Add a timer

**Which one?** Already using the timer in the Orbit app → **`register`** (below). Brand-new or
factory-reset with no app → **`provision`** (further down).

### From your Orbit account (`register`)

`cli.py register` turns a new timer into a working `config.json` entry in one command — no
hand-editing, no copying keys or addresses:

```bash
python cli.py register --name "Back Yard"                 # reuse the key already in config.json
python cli.py register you@example.com --name "Back Yard" # first run: log in once, fetch the key
```

It reuses the account key already in `config.json` when present (so adding another timer needs no
cloud login), otherwise logs in once, then: prompts you to release the phone → **catches the timer's
live advertisement** → reads its MAC + status back → writes `config.json` → confirms. First-run
password is read at a hidden prompt (never stored; MFA is unsupported and reported clearly). Several
timers on the account → a numbered chooser (or pass `--device-mac <MAC>`). The web **Add timer**
wizard runs the same flow with live status.

### Factory-fresh, app-free (`provision`)

`cli.py provision` enrolls a **factory-reset / brand-new** timer **without the Orbit app** — it
writes the account key onto the device (char `6c76`), then saves it:

```bash
python cli.py provision                 # (or: python cli.py provision you@example.com)
```

Factory-reset the timer into **pairing mode** (dial to OFF, hold ~10 s until the full display
lights), turn the **phone's Bluetooth OFF**, then run it.

> **The one precondition:** phone Bluetooth **OFF** before you press Enter. A B-Hyve accepts only one
> BLE connection and won't advertise while the phone holds it — released, it advertises freely and is
> caught reliably. Add `--device-mac <MAC>` to target a specific unit.

Two key modes: **Orbit** (`provision` — the account key; the Orbit app and our tools interoperate)
and **self-key** (`provision --self-key` — our own generated key, no Orbit account; stashed in
`secrets/`, and the Orbit app can no longer control the device). Both are proven durable
(see [Status & caveats](#status--caveats)). Mechanism: `docs/SPIKE_provisioning.md`.

## Troubleshooting

- **`device disappeared` / `not found` / connect timeout** — the timer is asleep. **Press a button on
  it** to wake it, then retry. Also confirm your **phone's Bluetooth is off** (it may be holding the
  timer's single BLE connection).
- **Wrong `address`** — on **Linux** it's the **MAC** (`44:67:55:…`); on **macOS** it's a
  **CoreBluetooth UUID**. Run `python cli.py scan` to find it.
- **Nothing runs on schedule** — set the Schedule tab to **Automatic**, and keep the server up and in
  range (see **Run it 24/7 (Linux)**).
- **First status read is slow (~10 s)** — that's the one live BLE round-trip; it's cached afterward,
  so the page stays instant.

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
  value to resync). Fields: `7` = clock (epoch), `16` = status (`sub1` run_state, `sub6.7` = seconds
  remaining), `46` = battery.

Full reverse-engineering detail: `docs/SPIKE_provisioning.md`, `docs/SPIKE_schedule.md`.

### A note on latency

A BLE session (connect → arm → read) takes ~8–12 s, so the server **caches the last status** and
serves `GET /api/status` instantly; a live read happens only on a cold cache or `?fresh=1` (the web
UI's ↻ Refresh). Start/stop always talk to the device and refresh the cache. This is why the page is
instant even though the radio is slow.

## Architecture

All Bluetooth/protocol logic lives in **`bhyve_xd.py`**. The CLI and the REST server are thin
wrappers over the same high-level methods — no duplicated control logic:

```
       bhyve_xd.py  (cipher · protocol · BHyveXD.status/start/stop/sync_clock · read-back)
          ▲                     ▲
 cli.py ──┘                     └── server.py ──▶ index.html (web UI calls the REST API)
```

| Path | Purpose |
|---|---|
| `bhyve_xd.py` | **the library** — cipher, protocol, `BHyveXD` controller + read-back (all logic) |
| `cli.py` | thin CLI over `bhyve_xd` |
| `server.py` | thin FastAPI REST API + web UI + host scheduler + status cache |
| `index.html` | web UI (calls the REST API) |
| `onboarding.py` | cloud login, device discovery, config write — powers `register` / the wizard |
| `schedule.py` / `scheduler.py` | host-scheduling store + due-rule / next-run logic |
| `schedule_device.py` | on-device schedule codec (dormant — see `docs/SPIKE_schedule.md`) |
| `provision_proof.py` | live-hardware proof of durable provisioning |
| `deploy/` | `deploy.sh` (push-deploy) + `sudoers` snippet + setup `README` |
| `test_e2e.py` | end-to-end tests against a fake device (no hardware) |
| `config.example.json` | copy to `config.json`, fill in address + network key |
| `docs/` | protocol reference (`SPIKE_*`), `plans/`, and `archive/` |

## Testing

Hardware-free and repeatable:

```bash
pip install -r requirements-dev.txt
python -m pytest test_e2e.py -q                  # 136 tests, no device needed (~1s)
```

`test_e2e.py` runs every operation against a **fake B-Hyve** (`FakeTimer`) that emulates the HT34A at
the byte level — AES handshake, frame decrypt/reassembly, watering state, correctly-encrypted status
notifications. `bleak` and `aiohttp` are patched, so no radio or network is touched. Reading
`is_watering=True` back proves the whole chain: `arm → command → encrypted notification → counter
resync → parse → DeviceStatus`, plus the CLI, REST API, status cache, macOS pairing, and cloud login.

## Status & caveats

**Proven on real hardware (2026-08):**

- **Control & scheduling** — status/start/stop each confirmed by the device's own read-back;
  host-driven scheduling fired a valve on time with a read-back-confirmed start. Runs the same on the
  Linux deployment as on the Mac.
- **App-free provisioning is durable** — `provision_proof.py` confirmed a device was **keyless first**
  (negative control), provisioned it **app-free**, and it **survived 3 power-cycles**, each re-verified
  in a fresh process. **Both** key modes (Orbit key and self-key, app never opened) met this bar. The
  device drops the BLE link after keying, so provisioning is two-phase (write → reconnect → finalize),
  ~2–3 min.

**Honest limits:**

- Provisioning is proven on **one device** (finalize bytes are **HT34 4-station** specific); not
  repeated across devices/models or re-verified on Linux/BlueZ.
- Enrolls for **local control only** — the device is not registered in Orbit's cloud/app.
- Automatic scheduling runs **only while the server is up and in BLE range** (hence the always-on box).
- **On-device standalone schedules are unverified** — accepted and activated byte-identically to the
  app, but never seen to run autonomously; kept in the repo, tested but dormant.

Detail + evidence: `docs/SPIKE_provisioning.md`, `docs/SPIKE_schedule.md`, `provision_proof.py`,
`docs/archive/PROJECT_STATUS.md`.

## Credits

Protocol reverse-engineered with reference to the community projects
`wxfield/Orbit_B-Hyve_4Port_Controller`, `troxor/bhyve_ble`, and `ljmerza/orbit-bhyve-ble`. The
**full-arming-sequence** requirement and the two-way read-back confirmation were established here on
live HT34A hardware.
