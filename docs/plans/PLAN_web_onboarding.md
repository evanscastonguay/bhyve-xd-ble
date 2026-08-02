# Plan — Fast, guided web onboarding (add/remove timers, SSE step tracker)

**Problem (`/s3-define`):** onboarding is terminal-only, slow (1–3 min), opaque (blind
waits, manual timing, JSON editing), with no web add/remove. We want a **web wizard** that
tries the **Orbit account key first**, **falls back to a self-key** (impact clearly
explained), with **step-by-step instructions, live wait countdowns, per-step verification,
low cognitive load**, and **minimized latency**.

**Chosen approach (`/s4-solutions`, Solution 2):** a server-side **onboarding state machine**
whose per-step progress is **streamed to the browser via SSE**, driving a live step tracker.
Plus remove-with-confirm and the latency fixes. No websockets, no BLE-contending presence
scan (Solution 3's trap), no laggy polling (Solution 1).

**"Orbit mode" + fallback (resolved):** the user enters their Orbit **email + password**; we
attempt to **actually obtain a usable key** (`cloud_fetch` → a controllable device *with* a
`network_key`). "Could we get the key?" has three outcomes, and the flow branches on the
real result — not just on "did login return 200":
  1. **Key obtained** (login OK + a device with a key) → provision with the **account key**
     (key-compatible); UI is honest: "to also manage it in the Orbit app, add it there once."
  2. **Login failed / MFA** → we couldn't get a key → **fallback choice** (below).
  3. **Login OK but this timer isn't on the account yet** (empty/no matching device — the
     *first-time-user* case) → also a **fallback choice**.

**The fallback is a clear choice, not an auto-jump** (this is the part the user asked to be
sure of):
  - **Option A — "set it up in the Orbit app first" (offered, optional):** show short
    first-time-user instructions (open the Orbit app → Add device → follow Orbit's setup),
    with an **"I've done it — retry"** button that re-runs the login so we can reuse the
    now-present account key (full app interop). For users who *want* the app.
  - **Option B — "standalone, my own key" (self-key):** skip Orbit entirely; provision with a
    generated key; a plain-language impact card (app won't control it; back up the key).
  - The user can **decline Option A and pick B** at any time — nothing forces the app path.
We still do **not** reverse-engineer Orbit's cloud device-registration.

Latency baseline (from proof logs): provision 58 s–2.7 min; contributors = a fixed 10 s
scan-collect, the two-phase reconnect (45 s scans + backoff), arm/finalize + read waits.

---

## Scope

**In scope**
- **Onboarding state machine** (`onboarding` module): an async generator that emits typed
  step events and drives choose-mode → key (orbit login / self-gen) → guided reset →
  two-phase provision → verify → save, with the Orbit→self fallback. Human-gated steps
  pause on an `asyncio.Event`.
- **Latency fixes**: react to the **first** qualifying advertisement (drop the fixed 10 s
  collect); tighten the two-phase reconnect; emit **countdowns** for the device-imposed
  drop-after-keying wait. Keep the P0 "refuse if >1 fresh B-Hyve" safety.
- **SSE + control endpoints** in `server.py`: stream progress, start a job, continue a
  human step, accept the self-key fallback, and **remove a device**.
- **Web wizard** in `index.html`: mode-choice cards, a live step tracker (EventSource) with
  title/instruction/expected-wait/countdown/state/✓, Continue buttons, a self-key impact
  card, and per-device **Remove (with confirm)**.
- Tests (offline state machine + endpoints) and a live run with latency measurement.

**Out of scope**
- Websockets; live device-presence scanning (contends with provisioning on one radio).
- Orbit **cloud device-registration** (app-listing) — documented as a one-time "add in app".
- Multi-timer concurrent onboarding; non-HT34 finalize.
- Any key reaching the browser; storing the password.

## Deliverables
1. `onboarding.py`: `onboard_flow(...)` async-generator state machine + progress hooks; the
   latency fixes in `catch_device_session`/`provision_device`.
2. `server.py`: `GET /api/onboard/stream` (SSE), `POST /api/onboard/{start,continue,fallback}`,
   `DELETE /api/devices/{index}`.
3. `index.html`: the add-timer wizard + remove-with-confirm.
4. Tests in `test_e2e.py`; a short latency note appended to `PROJECT_STATUS.md`.

---

## Phases (each proves something)

### P1 — Onboarding state machine + latency fixes (offline, TDD)
- `onboard_flow(params, gate)` → async generator yielding `Step` events
  `{id, title, instruction, state: waiting_user|working|done|failed, expected_wait_s,
  verified, detail, choices?}`. Happy path: `choose` → `get_key` → `await_reset`
  (waiting_user) → `provision` (write→drop→reconnect→finalize, sub-progress + countdown) →
  `verify` (read-back + MAC match) → `save` (`write_config` incl. `key_source`).
- **`get_key` must determine whether a key is truly obtainable**, not just whether login
  succeeded:
  - self mode → generate + stash a key (`key_source="self"`).
  - orbit mode → `cloud_fetch(email,password)`; then require a **controllable device with a
    `network_key`** (matching `device_mac` if given, else single/chooser). Classify:
    `key_obtained` / `auth_failed` (bad creds/MFA) / `no_device_on_account` (first-user).
- **`fallback_choice` step** (emitted for `auth_failed` or `no_device_on_account`), a
  `waiting_user` event carrying `choices`:
  - `orbit_app_first` → emit instruction steps (Add device in the Orbit app), then an
    **"I've done it — retry"** gate that re-runs `get_key` (loop back).
  - `self_key` → generate + stash a key, continue as self mode.
  The gate/`/continue` payload says which choice; declining `orbit_app_first` just picks
  `self_key`.
- Latency: `catch_device_session`/`provision_device` gain an optional `on_progress` callback
  and **react-to-first-advert** (connect on the first qualifying advert rather than a fixed
  collect); tightened reconnect. Preserve refuse-if->1.
- **Tests:** drive the generator with the fake BLE harness — assert event sequences for
  (a) orbit **key_obtained** → provision → verify; (b) **auth_failed** → `fallback_choice`;
  (c) **no_device_on_account** (first-user) → `fallback_choice`; (d) choice `self_key` →
  provisions with a generated key; (e) choice `orbit_app_first` → instructions → retry →
  now `key_obtained`; (f) **no emitted event contains the key**. Existing 57 e2e + 55 offline
  stay green.
- **Proves:** the whole guided flow + fallback is correct and legible, hardware-free.

### P2 — SSE + remove endpoints (server, TDD)
- `POST /api/onboard/start {mode, email?, password?, name?, device_mac?}` → launches one job
  (serialized with `_ble_lock`); returns a job id.
- `GET /api/onboard/stream` → `text/event-stream` emitting the `Step` events (re-attachable).
- `POST /api/onboard/continue {choice?}` advances a `waiting_user` step; for a
  `fallback_choice` step the payload is `choice: "orbit_app_first" | "self_key"` (and the
  `orbit_app_first` "I've done it — retry" is another `continue`). Key never serialized to the
  client; password never stored (used once for `cloud_fetch`, then dropped).
- `DELETE /api/devices/{index}` → remove from config (atomic rewrite); returns the new list.
- **Tests:** start→events (mocked flow); remove; fallback path; error mapping. 
- **Proves:** the browser can drive + observe the flow over HTTP, and remove works.

### P3 — Web wizard UI (`index.html`)
- **Add timer**: mode cards ("Use my Orbit account" / "Standalone — my own key"); a **live
  step tracker** (`EventSource` on `/api/onboard/stream`): current step highlighted with its
  one-line instruction + **expected wait / live countdown**, past steps collapsed with ✓,
  `Continue` buttons for human steps.
- **Fallback choice card** (when `get_key` can't obtain a key): shows *why* ("login failed"
  or "this timer isn't on your Orbit account yet") and two clear buttons —
  **"Set it up in the Orbit app first"** (reveals short first-time instructions + an "I've
  done it — retry" button) and **"Set up standalone (my own key)"** (with the plain-language
  impact). The user can pick standalone without ever touching the app path.
- **Remove**: a per-device control → **confirm dialog** naming the device (+ self-key warning
  "its key lives only in secrets/").
- Low cognitive load: one active step, quiet ✓ ticks (no raw log firehose), countdown instead
  of blank waits.
- **Proves:** a person can add/remove a timer from the browser, guided, without guessing.

### P4 — Live run + latency measurement  ⛳ gate
- Run the web wizard live (self-key and/or orbit), phone Bluetooth off. Measure end-to-end;
  confirm **< ~90 s machine time**, clear per-step visibility, actionable errors, no blank
  hang. Tune waits/countdowns from what's observed.
- **Proves:** the real UX + speed targets on hardware.

---

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| SSE + long BLE op blocks the async loop | run one job, `await` BLE (already async), `yield` between steps; serialize with `_ble_lock` |
| Browser refresh mid-onboard loses progress | job state kept server-side; `/stream` re-attaches to the running job |
| React-to-first-advert weakens the >1-device safety | keep the fe32 identify + **refuse if a 2nd B-Hyve appears** before writing |
| Faster reconnect re-triggers device lock-up | keep a backoff floor; countdown the device-imposed drop wait rather than hammering |
| Key/password leakage via SSE/UI | events carry status only; key stays server-side; password never stored |
| "Orbit app doesn't list the device" confusion | UI states it plainly + the one-time "add in app" note |

## Tests / validation
- **Offline (automated):** state-machine event sequences (orbit / fallback / provision /
  no-key-leak); endpoint tests (start/stream/continue/fallback/remove). 57 e2e + 55 offline
  stay green.
- **Live (P4):** end-to-end web onboarding, timed, on hardware.

## Checkpoints
- ⛳ **After P1** — flow + fallback + latency fixes proven offline.
- ⛳ **After P3** — wizard usable end-to-end against the fake/live server.
- ⛳ **After P4** — real UX + <~90 s speed confirmed on hardware.

## First concrete action
**P1:** add `onboard_flow` to `onboarding.py` and a FAILING test that drives it with the fake
BLE harness and asserts the **event sequence for the orbit-success path** and the
**login-fail → self-key fallback** transition (and that **no emitted event contains the
key**). Then implement the state machine + react-to-first-advert until green.

## Notes
- Reuse: `cloud_fetch`, `provision_device` (two-phase), `write_config`, `key_from_existing_config`,
  the fake BLE harness. The state machine mostly *orchestrates* proven pieces + emits events.
- Commit per phase; keep `config.json`/`secrets/` git-ignored; never commit keys.
