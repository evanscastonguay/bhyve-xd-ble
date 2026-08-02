# Plan — P4c: wire on-device schedules into the app

**Context:** P4a/P4b proved (on hardware) that we can store a watering program ON the timer so
it runs **autonomously with the Mac off** (persists across a 25s disconnect+reconnect). The
encoder (`schedule_device.py`) is pure + offline-tested and byte-identical to the app. P3
(host-driven) already ships. This plan wires on-device scheduling into the app as a selectable,
reliable engine — **without regressing control** and **without silently wiping schedules**.

## Load-bearing facts from the spike (must design around)
- Schedule = **two writes**: `setProgramSchedule(19)` (define) + `setActivePrograms(20)` (enable;
  bitmask, bit0 = Program A). Read-back via **`getActivePrograms(77)`** (`getProgramSchedule(69)`
  is not answered by this firmware).
- **⚠️ `arm()` sends `SETUP_FIELD20 = setActivePrograms{0}` on EVERY connect** → arming DISABLES
  all active programs. Any control session (start/stop/status) arms, so it will wipe an
  on-device schedule unless we **re-enable active programs after arming** (as the app does).
- Fields: `startTimesMinsFromMidnight` = **device-local** minutes; `stationId` = **0-indexed**;
  `runTimeSec` seconds. `programType` **Interval** (daily) is proven; **DayOfWeek `dayFlags` bit
  order is UNKNOWN** (needs one small capture) — so weekday programs are out until confirmed.

## Scope
**In**
- Device-schedule engine (`schedule_device` + a thin BLE driver): push the config's rules to the
  timer (`setProgramSchedule`+`setActivePrograms`), verify via `getActivePrograms`, and a
  read-of-active helper that arms **without** clearing (arm-keep-active).
- **Arm/re-enable safety**: after any `arm()` in a session that will coexist with schedules,
  re-assert the active-programs mask so control never disables the user's schedule. Centralize so
  every control path (valve start/stop, status) preserves active programs.
- Rule→program **mapping** (documented, lossless for the supported subset): map the config's
  schedule rules to device Program(s). Supported subset = **daily (interval) programs**; group
  rules that share days into a program with its start-times + per-valve station list.
- UI: a per-timer **engine choice** in the Schedule tab — “Run on this device (works with the Mac
  off)” vs the existing “Run while this Mac is on” (P3). Honest scope note: daily only until
  weekday masks are confirmed.
- Server: an endpoint to push/clear on-device schedules and report the active mask.

**Out**
- **Day-of-week** programs (blocked on `dayFlags` bit order — a separate 1-capture spike).
- Battery power-cycle persistence (inconclusive/secondary; revisit if needed).
- Multiple Orbit accounts; touching the proven cipher/provision/adopt core.

## Phases (each proves something; every device write under the do-not-brick rails)
### P4c-1 — arm-keep-active + engine primitives (TDD, mostly offline)
- Add an `arm` variant / post-arm hook that **re-enables the desired active mask** (so control
  never wipes schedules). Add `push_program(session, rules)` and `read_active(session)` (arm
  WITHOUT SETUP_FIELD20) building on `schedule_device`.
- Tests (offline): encoder→bytes already covered; add mapping tests (rules→program: start-times,
  0-indexed stations, program bit); assert the control path re-enables the active mask (fake
  session records the setActivePrograms call after arm).
- **Proves:** control no longer disables schedules; the push/read primitives are correct.

### P4c-2 — rule→program mapping + server endpoint (TDD)
- `program_from_rules(rules)` → a device Program (daily): distinct start-times, station list
  (valve→0-indexed, minutes→sec). Reject/annotate rules with specific weekdays (unsupported yet).
- `POST /api/timers/{i}/schedules/push` (encode+write+verify via getActivePrograms) and
  `.../clear`; `GET .../device-active` (the active mask). Key never leaves the server.
- Tests: mapping correctness; endpoint pushes+verifies against a fake BLE session; weekday rule →
  clear 400/annotation.
- **Proves:** saved rules become a correct device program via one action.

### P4c-3 — UI engine choice (index.html)
- In the Schedule tab, a two-way choice: **On device** (P4c) vs **This Mac** (P3), OFF by
  default; “On device” shows the honest daily-only note and a verified/active indicator.
- **Proves:** a person can push a daily schedule to the timer in one obvious step.

### P4c-4 — live end-to-end ⛳ (user)
- Push a daily schedule from the UI; disconnect; reconnect read-active (no-clear) shows it active;
  then a short **behavioral** run (schedule ~2 min out, hose off, observe the valve actuate).
- **Proves:** the UI path yields a real autonomous schedule.

### (separate mini-spike, optional) DayOfWeek `dayFlags`
- One capture of the app setting a Mon/Wed/Fri schedule → decode `dayFlags` bit order → unlock
  weekday programs. Until then, on-device = daily only; weekday stays host-driven.

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Control silently disables schedules (arm→setActivePrograms 0) | central post-arm re-enable of the active mask; test asserts it; this is P4c-1's first job |
| Lossy rule→program mapping | support daily subset explicitly; annotate/queue unsupported (weekday) rules; document |
| Wrong bytes to device | reuse the replay-proven encoder; verify every push via getActivePrograms; no-DFU already established |
| dayFlags guessed wrong | do NOT support weekday until captured; daily uses Interval (proven) |
| Two engines confuse users | one per-timer choice, OFF by default, honest labels; P3 stays the fallback |

## Tests / validation
- Offline: mapping, encoder (done), control-re-enables-active, endpoint push/verify/clear.
- Live (P4c-4): push from UI → disconnect → read-active persists → behavioral valve run (hose off).

## Checkpoints
- ⛳ P4c-1 control never wipes schedules (offline).
- ⛳ P4c-2 rules→device program pushes+verifies.
- ⛳ P4c-3 UI engine choice on the served page.
- ⛳ P4c-4 a UI-authored schedule runs autonomously.

## First concrete action
P4c-1: add the **post-arm re-enable** hook + a failing test proving a control session preserves
the active-programs mask (arm no longer leaves it 0), then implement. No new device-write risk
beyond the proven encoder; captures (`*.pklg`) still hold the key → delete once P4c is done.
