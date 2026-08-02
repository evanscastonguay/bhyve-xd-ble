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

## Findings (2026-08-02) — VERDICT: **GO** (schema-documented, cross-validated)

The capture wasn't even the decisive input — the **full Orbit BLE protobuf schema already
exists** in the original RE project: `project/water/bhyve_ble/custom_components/bhyve_ble/
orbit_pb_api.proto`. It is **cross-validated against our device**: the top-level envelope
`OrbitPbApi_IpcMsg` assigns `deviceStatusInfo = 16`, `setEpochTime = 75`, `setStationCfg = 94`
— the exact field numbers we independently reverse-engineered. So the schedule command is a
first-class, documented message on the same command char (`6c72`), AES-framed like everything
else. No DFU/firmware path involved (see [[no-dfu-cant-brick]]).

**Schedule-write command** = `IpcMsg{ setProgramSchedule = 19 : SetProgramSchedule }`:
```
SetProgramSchedule {
  programId                    = 1   // enum: manual=0, a=1 .. f=6  (required)
  startTimesMinsFromMidnight   = 8   // repeated uint32, e.g. 360 = 06:00
  stationInfo                  = 9   // repeated StationInfo
  budgetPercent                = 10  // optional
  oneof programType {
    programTypeDayOfWeek       = 3 : DayOfWeek { dayFlags = 1 }   // day bitmask
    programTypeInterval        = 4 : Interval  { intervalDays = 1 }
    programTypeRunOnce         = 7 : RunOnce
    ... Even=6, Odd=5, NotSet=2
  }
}
StationInfo { stationId = 1 (uint32); runTimeSec = 2 (uint32); type = 10 (station/soak); ... }
```
- **Read-back:** `getProgramSchedule = 69` → device returns `programSchedule = 76 :
  ProgramSchedule` (same shape) — so we CAN read/verify what we wrote. `programSchedules = 115`
  returns all. `getNextStartTime` exists too.
- **Delete/disable:** set an empty/NotSet program for that programId (confirm via read-back).

**Capture status:** `schedule1.pklg` (and its twin `2.04 PM.pklg`) recorded **no ACL data
channel** — only advertising/HCI control — so unusable (validated: my parser DOES extract the
fe32 `0x11`-framed writes/notifies from the known-good `water/full_4_zones_on_and_off.pklg`).
Cause: the app was already connected, so the connect+handshake+writes weren't in the trace.

**3 small ambiguities the schema doesn't pin down** (resolve by a clean capture to replay, or
by write→read-back experiments under the safety rails):
1. `dayFlags` bit order (which bit = Monday?).
2. `startTimesMinsFromMidnight` basis — device-local vs UTC (device stores a tz_offset).
3. `stationId` indexing here — expected **0-indexed** (matches manual start + our live
   [[status-active-zone-0indexed]] finding), to confirm.

**P4 path (safe):** encode `SetProgramSchedule` → write to `6c72` → **read back** via
`getProgramSchedule` to verify → power-cycle durability. If we get a clean capture first,
**replay its exact bytes** before synthesizing (do-not-brick rail).

---

## DECODED from a real capture (capture2_schedule.pklg, 2026-08-02) — unknowns RESOLVED
Decrypted the app's own `IpcMsg.setProgramSchedule` (field 19). Three ops seen (create/edit/
delete of Program A). Create message:
```
setProgramSchedule {
  programId(1)=1                         # Program A
  programType(oneof)=Interval(4){ intervalDays(1)=1, startIso(2)="2026-08-02T02:00:00-04:00" }
  startTimesMinsFromMidnight(8)=360      # 06:00, minutes-from-midnight, DEVICE-LOCAL
  stationInfo(9){ stationId(1)=0, runTimeSec(2)=600 }   # Valve 1 (0-indexed), 10 min
  budgetPercent(10)=100  programName(17)="Prog1"  basicProgramMode(19)=1
  startDateSecEpochUtc(11)  lastChangeId(14)  originDateSecEpochUtc(21)
}
```
- **Time basis = device-local minutes-from-midnight** (360=06:00). ✅
- **stationId = 0-indexed** (0 = Valve 1). ✅  matches [[status-active-zone-0indexed]].
- **Delete** = same programId with startTimes/stationInfo omitted.
- The app used **Interval** (not DayOfWeek) for this schedule; day-of-week `dayFlags` bit order
  is the only remaining unknown (only needed for explicit weekday programs).
- **Exact create/edit/delete bytes captured → available to REPLAY verbatim in P4a.**

Decoder: `$CLAUDE_JOB_DIR/tmp/pklg_decode2.py` (multi-block AES-CTR, CRC-validated per frame).

## P4a — REPLAY proven on hardware (2026-08-02)
Replayed the app's EXACT captured bytes on the live device (no-DFU re-asserted first):
`setProgramSchedule(19)` + `setActivePrograms(20){flags=1}`. Read-back via
`getActivePrograms(77)` (reply carries `activeProgramFlags`) flipped **0 → 1 → 0**
(baseline → create+enable → disable+delete). So the device **accepts, stores, activates**
our replayed schedule and reverts cleanly — non-destructive & reversible.
- **Confirmation query that works on this firmware:** `getActivePrograms` (field 77);
  `getProgramSchedule` (69) elicits no reply here.
- **A schedule = TWO writes:** `setProgramSchedule` (defines it) + `setActivePrograms`
  (enables it, bitmask; bit0 = Program A). Delete = setProgramSchedule w/o startTimes/stations.
- Multi-block AES-CTR framing: the app sends large frames; counter advances per 16-byte block
  (our `_enc_chunk` uses ≤16B chunks = 1 block each — both decode identically).
**P4b next:** encode SetProgramSchedule+SetActivePrograms from the UI's saved rules; verify by
`getActivePrograms`; power-cycle durability. (Day-of-week `dayFlags` bit order still TBD —
interval/daily fully covered.)
