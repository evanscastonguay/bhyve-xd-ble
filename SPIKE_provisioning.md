# Spike — App-free device provisioning (Phase B feasibility & go/no-go)

_Investigation only. No device reset, no implementation, until the B0 decision below.
Written 2026-08-01 from first principles + what this project already established._

> **RESOLVED 2026-08-01 — B1 capture done → B2 analysed → B3 = GO (feasible, simple).**
> A live HCI capture (PacketLogger) of the official app enrolling a factory-reset device
> showed provisioning is **hypothesis H1**: a single **plaintext ATT Write Request** of the
> **account network key** to a dedicated characteristic — **no pairing, no encryption, no
> cloud challenge**. See "B1/B2 findings" below. (Supersedes any earlier NO-GO.)

## B1/B2 findings — how enrollment actually works
Captured `capture.pklg` (git-ignored; contains the key — delete after). On the fresh
device the app did, in order:
1. Discover GATT. The **`fe32` service spans handles `0x0010–0x0019`** with characteristics:
   `6c71` (val `0x0012`, AES handshake), `6c72` (val `0x0014`, write), `6c73` (val `0x0016`,
   notify), and a **new one: `6c76` (val `0x0019`, props `Write`)** — the provisioning char.
2. **Write the key:** `ATT Write Request` to **`6c76` (handle `0x0019`)** with value
   **`0x0100` ‖ `<16-byte account network key>`** (18 bytes total). This is the whole
   "provisioning" — plaintext, unauthenticated.
3. Then the **normal session** we already implement: AES handshake on `6c71`, framed
   `0x11…` commands on `6c72`, notifications on `6c73`.

**Security check:** `0` SMP (pairing) packets and `0` HCI Encryption-Change events before
the key write → the link is **unencrypted/unpaired**; `6c76` accepts a plain write. HCI is
below the air-encryption layer anyway, so the key was visible in the clear regardless.

**Conclusion:** app-free enrollment = *connect to the pairing-mode device → one write to
`6c76` (`0x0100`+key) → verify with our normal handshake/read-back*. No sniffer or app
needed once implemented. The account key itself still comes from the cloud/`config.json`
(account-scoped); local control needs only the on-device key write, not cloud registration.

## Objective
Decide whether we can enroll a **factory-fresh / factory-reset** B-Hyve XD **without the
official Orbit app** — i.e., have our own tooling perform the **initial provisioning**
(bootstrap trust → write the account network key onto the device) so control works
afterward. Output is a **go/no-go recommendation**, not a build.

## What we already know (established this session)
- **Control is key-gated** (see `PROJECT_STATUS.md` §5). The AES *handshake is
  key-independent*; the account `network_key` is used at the *data layer*.
- The account key is **account/mesh-scoped** and is **provisioned INTO the device by the
  official app at setup** (verified indirectly: a wrong key connects+handshakes but
  nothing decodes; our flow only ever *reuses* a key the device already holds).
- The cloud API is understood: `/session` → token, `/devices`, `/meshes|
  /network_topologies` → `ble_network_key`. **No MFA** on this account (from Phase A2).
- **Unknown (the whole spike):** how a *keyless* device bootstraps trust and receives the
  key. We never captured the first-time-setup BLE exchange.

## The core unknown → four hypotheses (each implies a different feasibility)
| # | Bootstrap mechanism | Replicable app-free? |
|---|---|---|
| **H1** | Device ships with a **well-known default/factory key**; app connects with it and writes the account key | **Likely easy** — find the default key, reuse our write path |
| **H2** | **BLE pairing/bonding** (Just-Works or a button/passkey), then key written over the encrypted link | **Feasible** — bond from our central, then write |
| **H3** | **Asymmetric exchange** (device public key from a label/QR; ECDH → session key → write) | **Possible, harder** — need the device pubkey + crypto |
| **H4** | **Cloud-mediated**: app relays a challenge between the device (per-unit manufacturing secret) and Orbit's cloud | **Likely infeasible** app-free without cloud cooperation |

We cannot know which without **observing the app enroll a fresh device**. A hint that
leans harder: the app clearly does cloud round-trips during setup — but the actual
**key-write to the device is a local BLE operation we can observe**, so capture is decisive.

## How to find out — capture the app's first-time enrollment
Recommended: **iOS Bluetooth debug profile → PacketLogger** (cleanest, decryptable, no
over-the-air hassle):
1. Install Apple's **Bluetooth logging configuration profile** on the iPhone
   (developer.apple.com → “Bluetooth” profile). It raises HCI logging.
2. Reproduce enrollment: **factory-reset a spare device**, start a **sysdiagnose**
   capture, **add the device in the official B-Hyve app**, stop the capture.
3. Pull the sysdiagnose, open the HCI log in **PacketLogger** (Additional Tools for
   Xcode). Because the capture is at the HCI/SMP level, **bonded/encrypted GATT traffic
   is decryptable** in PacketLogger (it logs the pairing/keys).
4. Analyze: the GATT service/characteristic used for provisioning, the message sequence,
   **where and how the network key is written**, and any pairing/key-exchange preceding it.

Alternatives (fallbacks, not preferred): external sniffer (**nRF52840 + nRF Sniffer for
Wireshark**, or **Ubertooth**) captures over-the-air but must handle channel-hopping and,
if the link is bonded, must have caught the pairing to derive keys — fiddlier than the
iOS HCI capture.

## Prerequisites (what a real capture needs)
- **A sacrificial device** to factory-reset. ⚠️ **Destructive**: reset removes it from the
  account; recover by **re-adding it in the app** (which re-provisions the same account
  key — so it's recoverable, just an app round-trip). You have two timers; one would be
  the guinea pig.
- An **iPhone with the BT debug profile** installed, plus **PacketLogger** on the Mac.
- ~1–2 h for capture; **analysis time is open-ended** (minutes if H1, days if H3/H4).

## Feasibility (a-priori, before capture)
- **H1/H2** → app-free provisioning is **worth building**.
- **H3** → build **possible** but meaningfully more effort (crypto + getting the device
  pubkey, possibly off a label).
- **H4** → **stop**; app-free enrollment is effectively **infeasible** (needs Orbit's
  cloud/manufacturing secret). The official app stays mandatory for first enrollment.

Honest expectation: modern IoT often uses H2 or H4. H4 would be a dead end; H2 would be
very doable. **Only the capture resolves it.**

## Recommendation — staged go/no-go
**Conditional go for the CAPTURE only, gated on appetite.** Do not build anything until a
capture classifies the mechanism.

- **B0 — Decision (do this first):** Are you willing to (a) **factory-reset a spare timer**
  and (b) set up the **iOS BT debug profile + PacketLogger**? 
  - **No →** **NO-GO.** Accept the documented limitation: factory-reset recovery is a
    ~30-second **re-add in the official app**, after which our tooling works. This is a
    reasonable product stance and needs zero further work.
  - **Yes →** proceed to B1.
- **B1 — Capture:** reset the spare device, capture the app enrolling it (method above).
- **B2 — Analyze:** classify H1–H4; write findings here.
- **B3 — Go/no-go on build:** H1/H2 (and maybe H3) → scope an implementation plan
  (`provision_device`); H4 → stop, document as infeasible.

## Accepted limitation if we stop here (no-go path)
> Our system controls and re-onboards **app-enrolled** devices. A **factory reset**
> requires a one-time **re-add in the official Orbit app** (which re-provisions the same
> account key); afterwards `cli.py register` / the web wizard work immediately. App-free
> *initial* enrollment is out of scope pending a provisioning capture.

## If we get a "go" — rough shape of the build (not committed)
- New `onboarding.provision_device(...)`: connect to a factory device’s setup mode →
  perform the captured bootstrap (H1/H2/H3) → **write the account network key** → verify
  by reading its MAC/status back with that key.
- Possibly register the device to the account via the cloud API (if local control needs
  the mesh association — unknown; the capture would tell us).
- TDD against a fake “factory” device mirroring the captured sequence, same as the rest
  of the suite.

## References
`PROJECT_STATUS.md` (§5 key-gating, §6 factory-reset, §10 gaps), `PLAN_first_run.md`
(Phase B pointer), `control-key-gated` memory.
