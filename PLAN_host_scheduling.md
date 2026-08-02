# Plan — Host scheduling on the Mac: harden + prove (on-device set aside)

**Goal:** make **host-driven scheduling** (the server on the Mac fires the valve at each rule's
time over BLE) solid and **verified end-to-end on the Mac**. Explicitly **set aside all
embedded/on-device (standalone) scheduling** — that code stays dormant, untouched, unsurfaced.

**Why now:** host scheduling (P3) is built and unit-tested with an injected clock, and it's the
only engine we can actually verify on this hardware (on-device execution never fired — see
[[on-device-schedule-unverified]] / SPIKE_schedule.md). But it has real gaps before a user can
rely on it:
- **Reliability bug:** `run_due` adds a rule to `_fired` *before* the BLE start succeeds
  (`server.py`), so a failed/timed-out fire is marked done and **never retried** → a missed
  watering with no recovery.
- **No confirmation / no visibility:** the loop swallows exceptions silently; there's no record
  of whether a run actually started, so the user can't trust it.
- **Never proven live:** we verified the decision logic, not the background loop actually opening
  a real valve at a real time on the Mac.

Note (in our favor): host scheduling already honors **day-of-week** (`scheduler.due_rules` checks
`now.weekday()` against `rule.days`) and the device **auto-stops** after the passed duration.

## Scope
**In**
- Fix the fire path: mark a rule fired **only after a confirmed start**; **retry** on the next
  tick(s) if it failed/timed out; guard against **double-fire** (if the device already reports
  that valve watering, treat as fired).
- **Bounded catch-up:** fire a rule whose time was within the last N minutes and not yet fired
  today (covers a slow tick / a just-started server), capped small so it never waters hours late.
- **Observability:** track per-rule last-run {time, valve, ok/failed}; `GET /api/scheduling`
  returns `{enabled, last_runs, next_due}`; the Schedule tab shows "Scheduling on · last: Valve 2
  06:00 ✓ · next: Valve 1 tomorrow 06:00".
- **Live end-to-end proof on the Mac** (a real valve fires on schedule via the loop).
- Docs: how to run it on the Mac + honest limitations (Mac must be awake, running, in BLE range).

**Out (set aside)**
- All embedded/on-device scheduling: `schedule_device.py`, `/api/timers/{i}/schedules/push|clear`,
  `device_active_mask` arm-restore — leave in place, tested, dormant. Do not surface or extend.
- launchd/systemd auto-start on the Mac (note only; belongs to the later Linux phase).
- Multi-account; cipher/provision/adopt core.

## Deliverables
1. Hardened `run_due` / fire path (retry, confirm, double-fire guard, bounded catch-up).
2. Per-rule last-run tracking + `GET /api/scheduling` status + UI status line.
3. Automated tests for all the above (injected clock + scripted fire success/failure).
4. Live behavioral proof on the Mac.
5. README section: running host scheduling on the Mac + limitations.

## Phases (each proves something; TDD)
### P1 — Harden the fire path (offline, TDD)
- `run_due`: only add to `_fired` when `fire()` reports a **confirmed** start; on failure, leave
  it unfired so the next tick retries; before firing, if the device already reports that valve
  watering, mark fired (no double-start). `_fire_start` returns confirmed/failed (reads back
  `is_watering`/`active_zone`, which control already does).
- **Bounded catch-up:** treat a rule as due if `now` is within `[start, start+GRACE_MIN]` and its
  key isn't fired today (default `GRACE_MIN` small, e.g. 2).
- Tests (injected clock, stub fire): (a) fire raises on tick 1 → rule retried and fires on tick 2;
  (b) confirmed fire → not re-fired same minute; (c) catch-up fires a rule 1 min late but NOT 30
  min late; (d) already-watering that valve → not re-fired.
- **Proves:** a transient BLE failure no longer silently drops a watering; no double-runs.

### P2 — Status + UI (TDD + served check)
- Track `_last_runs` (per timer+valve: last attempt time + ok). `GET /api/scheduling` →
  `{enabled, last_runs:[...], next_due}`; compute `next_due` from the rules + now.
- UI: Schedule tab status line shows enabled state + last run (✓/✗ + time) + next due.
- Tests: endpoint shape; last-run recorded after a fire; UI markers served.
- **Proves:** the user can see scheduling is alive and whether the last run succeeded.

### P3 — Live end-to-end on the Mac ⛳ (user, hose off)
- Enable "On this Mac", add a rule ~2–3 min out, keep the server running, and confirm the
  **background loop opens the valve at the scheduled minute** (observe via status/log; hose off).
  Confirm the status line reflects the successful run.
- **Proves:** host scheduling actually waters on schedule on the Mac — the thing never yet shown.

### P4 — Docs
- README: enable scheduling, keep `uvicorn` running (foreground or `caffeinate` so the Mac
  doesn't sleep), and the limitation that a sleeping/off/out-of-range Mac misses that run — the
  motivation for moving to the always-on Linux box next.

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Retry double-fires a run that actually started | before firing, read status; if that valve is already watering, mark fired, don't restart |
| Catch-up waters hours late after a long sleep | strict small `GRACE_MIN`; never fire outside `[start, start+GRACE]` |
| Loop hides errors | record last-run ok/failed + surface it; keep loop alive but log failures |
| Fire collides with manual control | already serialized on `_ble_lock`; unchanged |
| Mac sleeps → missed runs | documented limitation; `caffeinate` tip; real fix is the Linux phase |

## Tests / validation
- Offline: P1 retry/confirm/catch-up/double-fire (injected clock + scripted fire); P2 status.
  Full suite stays green (124 → +new).
- Live (P3): a real scheduled valve run on the Mac, reflected in status.

## Checkpoints
- ⛳ P1 — a failed fire is retried, not lost; no double-runs.
- ⛳ P2 — scheduling status visible + trustworthy.
- ⛳ P3 — a real schedule waters on the Mac.

## First concrete action
P1: add a FAILING test — a rule whose `fire()` raises on tick 1 is **retried and fires on tick 2**
(and is added to `_fired` only after a confirmed start) — then rework `run_due`/`_fire_start`
until green. Do not touch on-device code. `config.json`/secrets stay git-ignored.
