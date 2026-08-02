# Spike — On-device schedule format (P0 feasibility & go/no-go)

_Capture & analysis only. NO device writes until this resolves and the P4 safety rails in
`PLAN_schedule_module.md` are met. Mirrors `SPIKE_provisioning.md`._

**Goal:** determine whether the B-Hyve XD stores/runs **on-device programs** we can write over
BLE, and if so, decode the wire format — the one fact that picks the schedule engine
(on-device autonomous vs. host-driven).

**Safety context (already established):** a read-only GATT survey found **no DFU/OTA service**
(only `fe32`: `6c71` handshake, `6c72` command, `6c73` notify, `6c76` provisioning) — so an
app-level protobuf write **cannot reach firmware**. Schedules are expected on `6c72` (the same
command char + protobuf family as `start`/`stop`/`set-time`). Worst case of a bad payload is a
recoverable misconfiguration. See [[no-dfu-cant-brick]].

## Capture recipe (same iOS flow as the provisioning capture)
1. On the iPhone: install the **Bluetooth logging profile**, reboot, connect via USB, open
   **PacketLogger** (macOS, part of Additional Tools for Xcode) → start recording.
2. In the **Orbit B-Hyve app**, on this timer, **create ONE simple schedule** with values we
   can recognize in the bytes:
   - **Valve 1**, start **06:00**, **duration 5 min**, days **Mon/Wed/Fri**.
3. Then **edit** it (change to Valve 2 / 07:00 / 10 min) and finally **delete** it — so we see
   create vs. update vs. delete framing.
4. Stop recording → export `schedule_capture.pklg`. **It contains the account key → git-ignored,
   delete after analysis** (add `*.pklg` to `.gitignore` if not already).

## What to extract (decrypt with the account key; cipher in `bhyve_xd.py`)
- **Which characteristic** the schedule write targets (expect `6c72`).
- The **protobuf field layout** of a program: valve, start time(s), duration, **day mask**,
  program id / slot, and any repeat/soak/budget fields.
- **Time basis:** the device clock is **UTC** with a stored `tz_offset` (field 75 set-time) —
  is the schedule time UTC, device-local, or tz-tagged? (Decides how "06:00" is encoded.)
- **Read-back:** is there a query that returns existing programs (so the UI can display/edit
  current schedules), or is it write-only?
- Recognizable markers: **06:00** (=360 min, or 0x0168), **5 min** (=300 s, 0x012C, or `ac02`
  varint), day mask for Mon/Wed/Fri, valve index (0-indexed on the wire, per
  [[status-active-zone-0indexed]]).

## Decode → verdict
- **GO (on-device feasible):** we can identify the char + fields well enough to WRITE a program
  (and ideally READ it back). → engine v2 (P4), starting with a **replay** of these exact
  captured bytes before synthesizing.
- **NO-GO:** format not decodable / no writable program path. → ship **host-driven only** (P3);
  the engine-agnostic model (P1) + UI (P2) are unaffected.

## Findings
_(fill in after capture)_
- characteristic: …
- program fields: …
- time basis: …
- read-back: …
- **VERDICT:** GO / NO-GO — …
