# Project status & knowledge base — bhyve-xd-ble

_Snapshot as of 2026-08-01. Captures what the system does, how it works, the hard-won
BLE facts, the key-gating finding, and the factory-reset analysis. Companion to the
`PLAN_*.md` files and `README.md`._

## 1. What this is
Local Bluetooth control of Orbit **B-Hyve XD** hose timers — **no cloud, no hub at
runtime**. Set the clock, start/stop zones, and **read status back for confirmation**.
Reverse-engineered and verified on real hardware (HT34A-0001, fw 0107). The cloud is
touched **once**, at setup, only to fetch the account's BLE network key.

## 2. Capabilities (working today)
- **Control** — status · start zone · stop zone · stop all · sync clock, each confirmed
  by the device's own read-back. Proven live on hardware.
- **Three surfaces, one engine** — CLI (`cli.py`), REST API (`server.py`), web UI
  (`index.html`); all call the same `bhyve_xd.BHyveXD` methods (no duplicated logic).
- **Multi-device** — `config.json` holds a `devices` list; pick one via `--device`
  (CLI), `?device=` (REST), or the UI dropdown. First entry is the default.
- **Onboarding / register a new timer** — one flow, two front-ends:
  - CLI: `cli.py register [email] [--name N] [--device-mac MAC]`
  - Web: **＋ Add timer** in the UI (`POST /api/onboard/register`)
  Reuses the account key already in config (**no cloud login for additional timers**),
  prompts to release the phone, **catches the timer's advertisement**, reads its MAC +
  status, writes `config.json`, and verifies.
- **First run from zero config** — with just an Orbit email + password it logs in, fetches
  the account key, and onboards the device. **Live-verified 2026-08-01** end-to-end (empty
  config → cloud login → key fetch → catch → write → status read-back). A numbered chooser
  handles multiple account devices; MFA accounts are reported (not supported).
- **Discovery** — `onboarding.catch_device` (connect-on-detection; robust to rotating
  addresses). `catch_device_session` returns an open session for held-connection use.
- **Testing** — hardware-free: **55 offline checks + 41 end-to-end tests** (fake device).

## 3. Architecture (one source of truth)
```
bhyve_xd.py   pure protocol core: cipher · framing · protobuf · BHyveXD controller +
              _Session (arm/command/read-back, adopt an already-connected client)
onboarding.py cloud_fetch · catch_device / catch_device_session · write_config ·
              key_from_existing_config · resolve_address  (all discovery/pairing/cloud)
cli.py        thin CLI            server.py  thin FastAPI REST     index.html  web UI
bhyve_lab.py  interactive diagnostic (reuses onboarding.catch_device_session)
selftest_offline.py / test_e2e.py   the two test layers
```
Dependencies point inward: everything depends on `bhyve_xd`; nothing in `bhyve_xd`
depends on the surfaces.

## 4. The BLE reality (hard-won facts)
- **Arming is mandatory.** The device decrypts an isolated command correctly but
  **ignores** it. It only acts after the app's 9-message setup sequence in the same
  connection (`session.arm()`), then your command works.
- **One connection at a time.** A timer accepts a single BLE connection and **does not
  advertise while connected**. So if the **phone** (app, or iOS in the background) holds
  it, the Mac can't see it. Fix: **phone Bluetooth OFF** → the timer advertises freely.
- **Rotating/private address on macOS.** An unbonded Mac may see a different UUID each
  advertisement. `catch_device` connects to the *exact* advertisement it just saw
  (no rescan), so it's robust to this. In practice our units' caught UUID has been
  **stable enough to persist** in `config.json`.
- **Address form:** macOS = opaque per-Mac CoreBluetooth UUID; Linux/BlueZ = the MAC.

## 5. Key finding — control is KEY-GATED (verified 2026-08-01)
The 16-byte `network_key` (account-scoped, fetched from the cloud, **provisioned into
the device by the official app at setup**) is required at the **data layer**. The AES
handshake is **key-independent** — it always appears to succeed.

Non-destructive experiment on a working timer (same device, same connection):

| Key used | Connect + handshake | Reply decodes? | `device_mac` | `device_time` |
|---|---|---|---|---|
| **wrong** (`ffff…`) | ✅ yes | ❌ no (empty) | `None` | `None` |
| **real** (`…3322`) | ✅ yes | ✅ yes | `44:67:55:…` | populated |

**Implication:** the connection layer never tells you the key is wrong. A wrong key, a
device that doesn't hold the key, and a genuinely locked device all present identically:
*connects, handshakes, then empty status / ignored commands.* (This reconciles the
earlier "empty status after handshake = lock-up" note — it's really the **key-mismatch
signature**.)

## 6. Factory-reset analysis
**Our flow controls an already-provisioned device; it does NOT enroll a factory-fresh
one.** Consequences of a factory reset:
- The reset **wipes the device's account key and account association**. To our (correct)
  key the device now behaves like the **wrong-key column above** — connects, handshakes,
  nothing decodes, commands ignored.
- The **initial provisioning handshake** is now **reverse-engineered** (2026-08-01 HCI
  capture, `SPIKE_provisioning.md`): the app writes the account key as a **plaintext ATT
  Write to characteristic `6c76` (handle `0x0019`)**, value `0x0100`‖key — no pairing/
  encryption. So app-free enrollment is feasible (a single BLE write); not yet implemented.
- **Recovery:** re-add the timer in the **official Orbit app**; it is re-provisioned with
  the **same** account key (account-scoped, unchanged), after which our existing
  `config.json` key works again and `cli.py register` re-discovers the (possibly new)
  address. No cloud re-fetch needed.

## 7. Onboarding flow, in detail
1. **Key:** reuse `key_from_existing_config()` (no cloud) — or, first device only / when
   an email is given, one `cloud_fetch()` login then pick the device (`--device-mac`, or
   auto when a single controllable device exists).
2. **Precondition:** prompt the user to turn the **phone's Bluetooth OFF**.
3. **Catch:** `catch_device(key, want_mac)` grabs the live advertisement, fast-rejects
   non-`fe32`, arms, reads the device's own MAC + status.
4. **Persist:** `write_config()` — atomic (temp+rename), idempotent (dedupe/update by
   MAC), never clobbers malformed JSON.
5. **Verify:** confirm MAC + status read-back; the network key is **never** returned to
   the web client.

## 8. Config & devices
`config.json` is **git-ignored** and holds the account **network key** (the durable
secret — treat like a password). It's a `devices` list; the first entry is the CLI/UI
default. Multiple timers on one account share one account key.

## 9. Test & backup status
- `selftest_offline.py` → 55/55 · `pytest test_e2e.py` → 41/41 (hardware-free).
- Live-verified: CLI + REST status/register on real hardware.
- Backup checkpoint tag: **`onboarding-complete-2026-08-01`** (pushed).

## 10. Known gaps / possible future work
- **App-free factory-reset onboarding — PROVEN durable (2026-08-02).** `cli.py provision`
  / `onboarding.provision_device` enroll a factory-fresh timer with the Orbit app never
  opened. Mechanism: write `0x0100`‖key to char `6c76`, then (the device drops the link
  after keying, so **two-phase**) reconnect and replay the app's finalize sequence
  (`provision_setup`, incl. the **station-config field 94**) that persists it.
  **Proof (`provision_proof.py`, evidence log):** on one device, a single trial —
  **negative control confirmed the device KEYLESS first** (our key did not decode) →
  provisioned app-free → **survived 3 consecutive power-cycles**, each re-verified in a
  **fresh process** (status decoded + zone start/stop confirmed), app untouched. That
  causal chain (keyless → only-our-writes → durable) is what makes it non-circular.
  Safety: refuses to write when >1 fresh B-Hyve is nearby. History/correction:
  `SPIKE_provisioning.md`. **Remaining caveats:** only **one physical device / macOS**
  tested; the finalize bytes are **HT34 4-station specific**; the device is enrolled for
  **local control only** (not registered in the Orbit cloud/app); provisioning takes ~2–3
  min (the reconnect).
- **Catch-once-and-hold resident server** (`PLAN_catch_and_hold.md`) — deferred; the
  stable UUID made it unnecessary for the current units.
- **Linux/BlueZ headless host** — addresses by MAC (no rotating-UUID problem); the
  recommended always-on/robust deployment.
- **Web wizard live-progress** — a scan-progress indicator during the catch.

## 11. References
- `README.md` — usage, protocol summary, register section.
- `PLAN_register.md` — the completed one-command registration plan.
- `PLAN_catch_and_hold.md` — deferred resident-hold design (same catch engine).
- `ONBOARDING_PLAN.md` — original cloud-onboarding plan.
