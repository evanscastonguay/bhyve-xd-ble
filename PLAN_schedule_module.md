# Plan — Schedule module (modular nav + evidence-gated engine)

**From `/s3`:** add device scheduling to a today-manual-only app **without re-cluttering it**,
by reorganizing navigation into **app-level** (account, add/remove) vs **per-timer**
(Control / Schedule) layers, each choice separate and obvious.

**From `/s4` (Solution 3, spike-gated):** the UI/module is **engine-agnostic** and built once
(`Now | Schedule` mode switch + self-contained `ScheduleModule`); the decisive fork —
**who keeps time & triggers a run** — is settled by a **reverse-engineering spike FIRST**:
- **on-device programs** (device runs itself, Mac can be off) — robust, but needs an unproven
  BLE program format reversed; or
- **host-driven** (server fires BLE start at T) — trivial (reuses proven start/stop) but only
  works while this Mac is awake, running, in BLE range.

Do the spike → let evidence pick the engine → build the same UI over either.

---

## Navigation model (decided in s3, common to every engine)
```
APP LEVEL   header chip → Account (takeover) ;  ＋Add → Add (takeover)
TIMER LEVEL (needs ≥1 timer)  tabs = which timer  →  mode switch:  [ Now | Schedule ]
   Now      = existing Control view (unchanged)
   Schedule = new ScheduleModule view (own endpoints)  ← surfaced only when applicable
```
View state machine gains one view: `empty | control | schedule | add | account`.

## Safety (do-not-brick) — governing rules for any device write
**Evidence (read-only GATT survey, 2026-08-02):** the timer exposes ONLY the `fe32` service
with chars `6c71` (handshake, r/w), `6c72` (command, write), `6c73` (notify), `6c76`
(provisioning, write). **No DFU/OTA/bootloader service is exposed** → there is **no BLE path
to firmware flash**; an app-level protobuf write cannot corrupt firmware. Worst case of a bad
schedule payload is a **recoverable misconfiguration** (re-write, or proven factory-reset +
re-enroll). Frames are AES-gated + CRC16-checked, so malformed writes drop at parse.

Rails (MANDATORY before/for P4):
1. **Re-run the GATT survey and assert NO DFU/OTA service** immediately before any write
   session (guard against a firmware update having added one). Abort writes if one appears.
2. **Write ONLY to the schedule characteristic identified by the P0 capture** (expected
   `6c72`, the command char we already use). Never write `6c76` or any unidentified char.
3. **Replay before synthesize:** the first write test replays the app's **exact captured
   bytes**, byte-for-byte — not a hand-built payload — to prove the char+format are correct
   and non-destructive. Only then parameterize (valve/time/days/duration).
4. **Recovery path confirmed first:** factory-reset + re-enroll is proven to restore the
   device — so there is always a way back before the first schedule write.
5. **Physical safety:** disconnect the hose during write experiments so a mistakenly-opened
   valve floods nothing; test on a spare or the least-critical timer first.
6. **Bounded, validated values only** (valve 1–N, sane duration, valid day mask); **read-back
   verify** after each write; **power-cycle durability** proof before trusting (as provisioning).
7. **Host-driven (P3) stays the zero-device-write default;** on-device (P4) is opt-in/
   experimental and only after the replay-proof passes.

## Scope
**In**
- **P0 spike:** capture the Orbit app setting a schedule, decrypt with the account key,
  decode the program characteristic + field layout → go/no-go for on-device. (`SPIKE_schedule.md`)
- **Model + store:** an engine-agnostic `Schedule`/`Program` model and a `schedule_store`
  (read/write per-timer schedules), persisted in `config.json` under each device.
- **UI:** `Now | Schedule` switch + `v_schedule` view + a rule editor (valve, start time,
  days, duration), reading/writing new REST endpoints. Reuses view-machine, tabs, banner.
- **Engine v1 (host-driven):** server scheduler that fires the proven BLE start (+auto-stop),
  gated behind an explicit **enable** and honestly labeled "runs while this Mac is on."
- **Engine v2 (conditional on P0 = GO):** write (and read-back if feasible) on-device programs;
  swap the store backend behind the same UI; prove durable on hardware.

**Out**
- Agronomy features (ET/weather budgets, rain-delay, soak cycles) beyond day/time/duration.
- Multi-account; the control/provisioning/adopt/account/cipher core (untouched).
- Any autonomous BLE watering without an explicit user enable.

## Deliverables
1. `SPIKE_schedule.md` — capture findings + engine go/no-go (mirrors `SPIKE_provisioning.md`).
2. `Schedule` model + `schedule_store` (+ tests).
3. `index.html` Schedule module (mode switch, editor) — engine-agnostic (+ served checks).
4. Host-driven scheduler engine (+ tests with injected clock).
5. (If GO) on-device program write/read + durability proof; migration from v1 rules.
6. Live confirmation a schedule actually waters.

---

## Phases (each proves something)

### P0 — RE spike ⛳ **evidence gate** (no engine code until this resolves)
- Reuse the PacketLogger → decrypt-with-account-key pipeline from `SPIKE_provisioning.md`.
  Capture the official app **creating one schedule** (e.g. Valve 1, 06:00, Mon/Wed/Fri, 5 min)
  and **editing/deleting** it. Decrypt frames with the account key (cipher in `bhyve_xd.py`).
- Decode: which characteristic (likely the `6c72` write / same protobuf family as `msg_*`),
  the **program field layout** (start time(s), duration, day mask, valve, program id), the
  **time basis** (device clock is **UTC** + stored `tz_offset` — how schedule times map), and
  whether programs can be **read back** for display.
- **Verdict:** GO (write feasible; note read-back yes/no) or NO-GO (host-driven only).
- **Proves:** whether on-device is real — the one fact that picks the engine. Non-destructive
  (capture only; no writes to the device in this phase).

### P1 — Engine-agnostic model + store (offline, TDD)
- `Schedule`/`Program` dataclass: `{valve, start_hhmm, days[], duration_min, enabled}`; a
  timer holds a list. `schedule_store.read(timer)/write(timer, schedules)` persists under the
  device in `config.json` (atomic, via the existing `_load_config`/`_atomic_write_config`).
- **Tests:** round-trip; per-timer isolation; preserves devices/account blocks; validation
  (valid HHMM, 1–4 valve, ≥1 day, duration bounds). Existing 93 stay green.
- **Proves:** schedules persist and are backend-neutral (the UI/engine both read this store).

### P2 — Schedule UI shell + REST (index.html + server, TDD)
- REST: `GET /api/timers/{i}/schedules`, `PUT …/schedules` (validate; key never involved).
- UI: `[ Now | Schedule ]` switch on the selected timer; `v_schedule` view listing rules with
  add/edit/delete + an **enable** toggle; surfaced only when a timer exists (and, once v2
  lands, only if the device supports it). One context at a time; banner feedback; "Valve N".
- **Tests:** endpoints validate + persist via the store; served page has the switch + view;
  all onclick handlers resolve.
- **Proves:** a person can author a schedule with low cognitive load, engine not yet wired.

### P3 — Engine v1: host-driven scheduler (server, TDD)
- A scheduler loop (asyncio) that, per enabled rule, fires the **existing** BLE start at T
  (+auto-stop after duration), serialized on `_ble_lock`, refused during onboarding.
- Gated behind an explicit **"Enable host scheduling (requires this Mac running)"** toggle;
  OFF by default (no surprise autonomous watering).
- **Tests:** with an **injected clock/trigger** (no real time/BLE), a due rule invokes the
  start path with the right valve+duration; disabled rules don't fire; onboarding blocks it.
- **Proves:** schedules run end-to-end today, with honest reliability labeling.

### P4 — Engine v2: on-device programs (CONDITIONAL on P0 = GO; TDD + hardware proof)
Follows every rail in **Safety (do-not-brick)** above.
- **P4a — replay proof:** re-send the app's captured schedule bytes verbatim to the captured
  char; read-back / observe it took effect. No synthesized bytes yet. (Hose disconnected;
  GATT survey re-asserted no-DFU first.)
- **P4b — parameterize:** encode/write our own program per the reversed format (bounded,
  validated); read-back verify. Swap the store backend to device-resident; migrate v1 rules.
- **Proof (like provisioning):** write a schedule, **power-cycle**, observe it runs
  autonomously with the Mac process dead — negative control + durability, fresh process.
- **Proves:** true set-and-forget on the device, established without ever risking firmware.

### P5 — Live ⛳: author a schedule in the UI → confirm it waters (v1 now; v2 if GO).

---

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| RE can't decode programs (P0 NO-GO) | fall back to host-driven only; UI/model already engine-agnostic, nothing wasted |
| Autonomous BLE opens water unexpectedly | host scheduling OFF by default + explicit enable; per-rule enable; never fire without user opt-in |
| Timezone/UTC mismatch (device clock is UTC + tz_offset) | resolve the time basis in P0; store schedule times with an explicit tz; test the mapping |
| Conflicts with app-set device programs | on-device (v2) shares the device's programs (single source); host-driven (v1) labeled as separate + a "device also has its own schedules" note |
| Writing wrong program bytes (v2) | same rigor as provisioning: capture-proven format, negative control, power-cycle durability before trusting |
| Host-driven fragility disappoints | labeled honestly at the toggle; positioned as stopgap when v2 is GO |
| Schedule UI re-clutters Control | separate `v_schedule` mode; Control view untouched (asserted) |

## Tests / validation
- **Offline:** P1 store round-trip/validation; P2 endpoint validate+persist + served-page
  checks; P3 scheduler with injected clock (due→start, disabled→noop, onboarding-blocked).
  Full suite stays green each phase.
- **Live:** P0 capture/decrypt; P4 durability proof (power-cycle, fresh process); P5 real water.

## Checkpoints
- ⛳ **After P0** — engine decided by evidence (GO/NO-GO), before any engine code.
- ⛳ **After P2** — a schedule is authorable in a clean, separate mode (no engine yet).
- ⛳ **After P3** — schedules run (host-driven), honestly labeled.
- ⛳ **After P4** — on-device autonomous, proven durable (if GO).
- ⛳ **After P5** — a real, user-authored schedule waters.

## First concrete action
**P0:** create `SPIKE_schedule.md` (scaffold + exact capture recipe — the same iOS
PacketLogger flow as provisioning) and a small decrypt harness reusing `bhyve_xd`'s cipher;
then have the user capture the Orbit app setting one schedule. Decode → record the go/no-go.
No engine code, no device writes, until P0 resolves. `config.json`/`secrets/` stay git-ignored;
captures contain the key → treated like `capture.pklg` (git-ignored, deleted after).
