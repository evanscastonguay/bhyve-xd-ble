# Plan — Local control of a rotating-address timer (catch-once-and-hold)

**Problem (from `/s3-define`):** the new timer (`AA:BB:CC:DD:EE:01`) advertises with a
**rotating/private BLE address** the unbonded Mac can't pre-target, and only
**re-advertises for a few seconds after the bonded iPhone releases it**. The proven
onboarding flow (`ONBOARDING_PLAN.md`, E6) assumed a *stable* macOS UUID — that
assumption is now false for this unit.

**Chosen solution (from `/s4-solutions`):** **Solution 2 — catch-once-and-hold.** A
resident process catches the timer **once**, then **keeps the BLE connection open**
and serves all commands over it (holding the single BLE slot also locks the phone
out). **Destination: Solution 3** — move control to a Linux/BlueZ host that addresses
the timer by its MAC, deleting the macOS rotating-address problem entirely.

Core insight: *a held connection **is** the stable handle we need.*

---

## Scope

**In scope**
- Prove end-to-end control of the new timer on the Mac (key + protocol + catch).
- Characterize the address behavior (truly rotating? does macOS ever give a stable UUID?).
- A reusable **live-catch** primitive: find+connect a B-Hyve by MAC via advertisement,
  independent of any pre-known address.
- **Catch-once-and-hold** resident controller in `server.py`: hold the connection,
  keepalive, auto-reconnect (re-catch) on drop; REST + web UI work through it.
- Tests (fake rotating device + held session) and cleanup of scratchpad scripts.

**Out of scope (deferred, not blocked)**
- Full Solution 3 build-out (Linux/BlueZ host) — documented + config-ready, not deployed here.
- BLE bonding on macOS to force a stable UUID (uncertain; only revisit if Phase 1 shows it's easy).
- The broader architecture decomposition (separate, later effort).
- Any change to the working **old** timer's stable-address path — it must keep working.

## Deliverables
1. `bhyve_lab.py` (exists) — interactive, logged capture + live control. The Phase 0/1 instrument.
2. `bhyve_xd.py`: a `catch_bhyve(mac, key, ...)` live-catch finder (scan → new advert → connect →
   `fe32` → arm → match MAC), and a session that can **adopt an already-connected client**.
3. `server.py`: catch-once-and-hold resident controller with keepalive + auto re-catch.
4. Tests: e2e fake device simulating rotating address + a persistent held session.
5. Docs: README "Rotating-address timers / always-on control" + a Linux/BlueZ note (Solution 3).

---

## Phases (each proves something)

### Phase 0 — Prove control *at all* (manual, user-run, logged)  ⛳ gate
Run `bhyve_lab.py`: one capture, then `g → zone 1 → 10s` from the `live>` menu.
- **Proves:** the account key is valid for THIS device, the protocol works, and the
  catch flow connects + arms + confirms MAC `AA:BB:CC:DD:EE:01` — the foundation all
  else stands on.
- **Test/validation:** timer physically waters; `bhyve_lab.log` shows `fe32`, `CONFIRMED`,
  and `watering=True`. Claude reads the log.
- **Do not proceed until this passes.**

### Phase 1 — Characterize the address (data, not assumptions)
Capture **twice** (two separate `bhyve_lab` runs). Compare the two caught addresses.
- If **different** → rotating confirmed → catch-once-and-hold is the path (Phase 2+).
- If **same** → macOS is giving a stable UUID after connect → we can just persist it to
  `config.json` and reuse the old, simple path (short-circuits most of this plan).
- **Test/validation:** two `bhyve_lab.log` sessions with their `captured address` lines.
- **Proves:** which world we're in — decides how much of Phase 3 is needed.

### Phase 2 — Extract a reusable live-catch primitive (library)
Promote the lab's catch logic into `bhyve_xd.py`, without touching the stable-address path:
- `async def catch_bhyve(mac, key, *, baseline=7, near=-80, timeout=90) -> connected client`
  — scan, skip ambient, on a new close advert connect + verify `fe32` + arm + match MAC.
- Let `_Session` **adopt an already-connected client** (so catch and control share one code path;
  removes the duplicate protocol currently inlined in `bhyve_lab.py`).
- **Test/validation:** offline e2e — extend `FakeTimer`/`FakeClient` to present a *rotating*
  address and only advertise after a simulated release; assert `catch_bhyve` finds+arms it.
- **Proves:** the catch is product code, reused by both lab and server — no duplication.

### Phase 3 — Catch-once-and-hold in the server (the solution)
- On startup (or first request), `server.py` **catches once** and keeps the client open in a
  singleton held session; all endpoints run on it (arm is done once, not per call).
- **Keepalive:** periodic lightweight read; **auto-reconnect:** on drop, transition to a
  `RECONNECTING` state and re-run `catch_bhyve` (surface a clear "release the phone" hint via
  `/api/health`).
- Web UI shows connection state (HELD / RECONNECTING / catching).
- **Checkpoint ⛳:** from a cold start, the server catches once and serves `status/start/stop`
  repeatedly with the phone Bluetooth **on but idle** (proving the held slot locks the phone out).
- **Test/validation:** e2e — held session serves N sequential commands on one connection;
  simulate a drop → server re-catches → next command succeeds.

### Phase 4 — Robustness + Solution-3 readiness
- Config carries `platform`/`address_mode` so the **same code** runs on Linux by MAC
  (no catch) vs macOS by catch-and-hold. `resolve_address` already returns the MAC on Linux.
- Backoff on re-catch to respect the **lock-up gotcha** (no rapid connect storms).
- README documents the Linux/BlueZ always-on path as the recommended durable setup.
- **Test/validation:** offline — config selects Linux MAC path with no catch; macOS path uses catch.
- **Proves:** a clean migration route to Solution 3 with no rewrite.

### Phase 5 — Tests, docs, cleanup
- Full offline + e2e suites green (incl. the new rotating-device + held-session tests).
- Remove/relocate scratchpad probes; keep `bhyve_lab.py` as the supported diagnostic tool.
- README section; commit + push per phase; `config.json` stays git-ignored.

---

## Risks & mitigations
| Risk | Mitigation | Status |
|---|---|---|
| Phone re-grabs the timer before/while Mac catches | Bluetooth OFF during catch; **held connection locks phone out** afterward | core of Sol. 2 |
| Held BLE connection drops silently | keepalive read + `RECONNECTING` state + auto re-catch with backoff | Phase 3 |
| Re-catch storms lock the device (needs power-cycle) | exponential backoff; cap attempts; clear user hint instead of hammering | Phase 4 |
| Several B-Hyve timers in range | arm+read-MAC disambiguates to the target MAC (proven E3) | proven |
| macOS gives a *new* UUID each connect → can't persist | Phase 1 measures it; catch-once-and-hold doesn't need persistence | by design |
| Address actually stable (over-engineering) | Phase 1 gate short-circuits to the simple persisted-address path | gate |
| Breaking the working old timer | old stable-address path untouched; catch path is additive | constraint |

## Tests / validation
- **Live gate (Phase 0):** real watering + `CONFIRMED` MAC in `bhyve_lab.log`.
- **Live measure (Phase 1):** two captures; compare addresses.
- **Offline/e2e (automated):** `FakeTimer` rotating-address + release simulation; `catch_bhyve`
  finds+arms; held session serves N commands; drop → re-catch → success. Existing 55 offline +
  19 e2e checks must stay green.
- **Live checkpoint (Phase 3):** cold-start server catches once, serves status/start/stop with
  phone Bluetooth on-but-idle.

## Checkpoints
- ⛳ **After Phase 0** — control proven; do not build further until green.
- ⛳ **After Phase 3** — durable held control on the Mac (the working solution).

## First concrete action
**Phase 0, user-run:** in a Terminal —
```
cd path/to/bhyve-xd-ble
./venv/bin/python bhyve_lab.py      # menu → 2 (capture) → follow prompts → g, zone 1, 10s
```
Then tell Claude; Claude reads `bhyve_lab.log` and we proceed from real data.

## Notes
- Reuse, don't duplicate: Phase 2 folds `bhyve_lab.py`'s inline protocol back into `bhyve_xd.py`.
- `ONBOARDING_PLAN.md` (cloud onboarding) remains valid; this plan supersedes only its
  *macOS-address-is-stable* assumption for privacy-address units.
- Commit per phase; keep `config.json` git-ignored.
