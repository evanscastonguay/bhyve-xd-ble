# Plan — make the zone dashboard trustworthy (Solution 1 + radio track)

From s3/s4. Core: **truth** (screen == device reality, confirmed by read-back + reconciled) and
**honest feedback** (every tap ends in on/off/failed; transient BLE flakiness absorbed by retry).
Software fixes the UX bugs; a **radio fix** (ESP32 proxy) is needed for the weak `…71:B0` link
(measured 0/4) and runs as a parallel, user-driven track.

## Scope
- **In (software):** server auto-retry until read-back confirms; `_active_zone` set **only on
  confirmed** watering + **persisted** + **reconciled by a real read**; `/api/zones` honest state +
  freshness; a UI per-zone **state machine** (idle → starting → on → failed/retry) with no silent
  no-ops and a non-stalling poll; an **e2e QA** pass on the box.
- **Out:** persistent warm BLE connections (s4 Solution 2 — deferred); the radio hardware itself
  (documented, user installs); scheduling/HA changes.

## Phases (each testable)

### P1 — Server: confirmed + retry + persistence + reconcile (TDD, fake timers)
- `_act_confirmed(make_coro, device, want, attempts)`: run the BLE op, retry until read-back
  `is_watering == want` (True=started / False=stopped) or attempts exhausted → `(ok, status)`.
- `zones_start`: if a different zone is active, **stop-old confirmed first**; if the old zone can't be
  confirmed stopped, **refuse** the new start (never two valves open) with a clear reason. Start-new
  with retry; set `_active_zone` **only if confirmed**. Return `{ok, reason, active, ...}`.
- `zones_stop_all`: retry each device to confirmed-idle; clear `_active_zone` only if the active
  device confirmed off; report per-device ok.
- **Persist** `_active_zone` to a small state file next to config; load on startup.
- **Reconcile:** `GET /api/zones?reconcile=1` does a real read of the believed-active device and
  self-corrects (clears if actually idle) → fixes auto-stop / restart / app-use drift.
- **Tests:** retry succeeds after k misses; unconfirmed start ⇒ `ok:false` + `_active_zone` stays;
  stop-old-unconfirmed ⇒ start refused; stop-all partial failure reported; persistence round-trip;
  reconcile clears a stale active when the device reads idle.
- **Gate:** suite green.

### P2 — UI: honest per-zone state machine (`zones.html`)
- Per-zone visual states: **idle / starting (pulse) / on (green) / failed (red, tap-to-retry)**; a
  status line; **no silent outcome** — `ok:false` shows ✗ + reason + retry.
- Robust poll: client-side fetch timeout; poll guarded by a token (never permanently blocked);
  **"as of Xs ago"** freshness; a manual **Refresh** that calls `?reconcile=1`; read-on-load reconciles.
- **Gate:** JS parses; `/zones` renders states; a simulated `ok:false` shows the failed/retry state.

### P3 — Live e2e QA on the box (act as QA; don't stop until green)
- Deploy; run a **matrix**: each of the 8 zones start→confirm→stop; cross-timer switch; stop-all;
  induced failure. Assert the API/UI state matches reality every time. Iterate until **zones 1–4
  (`…78:00`) are rock-solid** and **zones 5–8 behave honestly** (fail visibly + retry) pending radio.
- **Gate:** zone1-4 matrix 100%; zone5-8 honest (no silent/false states).

### P4 — Radio track (parallel, user): ESP32/ESPHome BT proxy for `…71:B0` (doc), then re-QA 5–8.

### P5 — docs + mark plans done.

## Risks & mitigations
- **Retry latency** (~N×10 s) → cap attempts (2–3) + optimistic UI so it never blocks; show progress.
- **Reconcile contends for the BLE lock** → only on load / after action / manual refresh, not the fast poll.
- **Refuse-on-unconfirmed-stop can block switching from the weak timer** → honest message; the radio
  track resolves it (safety > convenience given the pressure constraint).
- **Persisted state still drifts** → reconcile with a real read on load.
- **Weak link unfixable in software** → honest failure + P4.

## Tests / validation
P1 fake-timer unit tests (retry, confirm-gate, refuse, partial stop, persistence, reconcile).
P2 JS check + serve + simulated failure state. P3 live matrix.

## Checkpoints
After **P1** (server truth green) · after **P2** (UI honest) · after **P3** (live QA pass on 1–4).

## First concrete action
**P1:** TDD `_act_confirmed` + rework `zones_start` / `zones_stop_all` / `zones` to be
confirmed-authoritative, persisted, and reconcilable.
