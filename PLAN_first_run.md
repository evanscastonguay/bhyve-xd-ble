# Plan — First-run onboarding validated end-to-end (Phase A) + provisioning spike (Phase B)

**Problem (`/s3-define`, scope C):** the cloud-login-first onboarding path exists in code
but has **never run** (live or in tests), and the first-run edges (no `config.json`, two
devices to disambiguate, bad-creds/MFA) are unexercised. **Phase A:** make that path
provably correct, regression-protected, and self-explanatory, then confirm it live.
**Phase B (later, spike only):** time-boxed investigation of app-free device enrollment →
go/no-go.

**Chosen approach (`/s4-solutions`, Solution 2 + cheap chooser):** harden with offline
tests + first-run UX (incl. a stateless CLI device chooser), *then* a single live
confirmation. Correctness must not depend on creds being available.

Builds on: `PLAN_register.md` (register flow), `PROJECT_STATUS.md` (findings). The account
key is provisioned by the official app — Phase A assumes devices are **already enrolled**
(only the *software* config is missing); Phase B is where app-free enrollment is scoped.

---

## Scope

**In scope (Phase A)**
- Offline e2e for the **cloud-login-first** register path (CLI + web): no-config →
  `cloud_fetch` (mocked) → device select → `catch_device` (mocked) → `write_config`.
- A **stateless, testable CLI device chooser** (`_choose_device`) for when several devices
  are on the account and no `--device-mac` is given.
- **Error surfacing**: AuthError / RateLimited / MFARequired / CloudConnectionError shown
  as clear, actionable first-run messages (CLI + web).
- First-run correctness with **`config.json` absent**.
- **Live confirmation** from an empty config with real creds (user-supplied).

**In scope (Phase B — spike only)**
- Bounded investigation: can we observe the app enrolling a fresh device (PacketLogger +
  iOS BT debug profile, or nRF/Ubertooth)? What does provisioning look like? → a written
  **go/no-go** in `SPIKE_provisioning.md`. **No implementation, no device reset without
  explicit go.**

**Out of scope**
- Implementing app-free provisioning (Reading B) — gated on the Phase B spike.
- Stateful two-step web wizard / interactive web device picker (defer; web keeps its
  single-POST + 409-list behavior).
- Keychain / credential storage; completing MFA (surface it, don't solve it).

## Deliverables
1. `cli.py`: `_choose_device()` + `register` uses it; clearer first-run messaging.
2. `test_e2e.py`: cloud-first register (CLI + web), multi-device pick, error mapping.
3. `README.md` / `PROJECT_STATUS.md`: first-run (cloud-login) section.
4. (Phase B) `SPIKE_provisioning.md`: findings + go/no-go.

---

## Phases (each proves something)

### Phase A1 — Offline coverage + chooser + messaging (TDD)  ✅ DONE
- `_choose_device(devices, choose_fn=input)` — print a numbered list, return the picked
  device (or None on bad input); pure, `choose_fn` injectable for tests.
- Wire into `cmd_register`: `want_mac` match → else single → else `_choose_device`.
- Clear first-run messages: "no saved config — logging in to Orbit…", auth/MFA/rate-limit
  guidance, "found N devices — choose one".
- **Tests:** cloud-first CLI register (absent config → mocked `cloud_fetch` 1 device →
  mocked `catch_device` → `write_config` writes entry **with the fetched key**);
  `_choose_device` (valid pick, out-of-range, non-numeric); multi-device pick via injected
  chooser; auth-error surfaced; web `onboard_register` cloud path (mocked) + multi-device
  409. Existing 55 offline + 41 e2e stay green.
- **Proves:** the cloud-login-first path is correct + regression-proof, creds-independent.

### Phase A2 — Live confirmation from empty config  ⛳ gate
- With a **throwaway empty config** and phone Bluetooth OFF, run
  `cli.py register <email> --device-mac <MAC> --config <tmp>` (or the chooser), enter creds
  at the hidden prompt; confirm it logs in, catches, writes config, and a follow-up
  `status` reads back live — **no key reuse, no hand-editing**.
- **Depends on:** current working Orbit creds. If rotated/MFA, this gate is blocked but A1
  still stands (path proven by tests + a clear error explains why).
- **Proves:** the real first-run works on hardware (success criterion A).

### Phase A3 — Docs
- README "First run (cloud login)" section; note the two-device `--device-mac` need and
  the phone-off precondition. Update `PROJECT_STATUS.md` capabilities/first-run.
- **Proves:** a new user can follow it unaided.

### Phase B — Provisioning spike (separate, after A)  ⛳ decision
- Investigate observability of the app's initial enrollment (no reset without explicit go).
- Output `SPIKE_provisioning.md`: what's needed (hardware, a sacrificial device), what the
  handshake looks like if captured, feasibility, and a **go/no-go**. Decide before building.

---

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Creds rotated / MFA → A2 blocked | A1 makes correctness creds-independent; surface a clear, actionable error; user rotates/provides |
| Cloud schema drift (`/meshes` vs `/network_topologies`) | already handled + covered by `_build_devices` tests |
| Two devices → wrong one registered | `--device-mac` + `_choose_device`; live A2 uses an explicit MAC |
| Interactive prompt hard to test | `choose_fn`/`getpass` injected/monkeypatched; keep chooser pure |
| Live catch fails (phone holding it / asleep) | phone-off precondition + existing retry guidance |
| Phase B tempts destructive testing | spike is investigation-only; no reset without explicit go |

## Tests / validation
- **Offline (automated):** cloud-first CLI register; `_choose_device`; multi-device pick;
  auth-error surfaced; web cloud path + 409. Full 55 offline + 41 e2e stay green.
- **Live gate (A2):** empty config → `register <email>` → `status` reads back.

## Checkpoints
- ⛳ **After A1** — cloud-first path proven offline + regression-protected.
- ⛳ **After A2** — real first-run proven on hardware.
- ⛳ **After B** — go/no-go on app-free enrollment (no build before this).

## First concrete action  ✅ DONE (branch first-run-phase-a1)
**Phase A1 complete:** `_choose_device` (stateless, injectable), wired into `register`;
first-run messaging + typed MFA/auth error surfacing. Offline coverage added — cloud-first
CLI register writes the fetched key; multi-device chooser; auth-error surfaced; web cloud
path + multi-device 409. **47 e2e + 55 offline green.**

**Next:** Phase A2 — LIVE confirmation from an empty config with real Orbit creds
(user-supplied; password never seen). Blocked only if creds are rotated / MFA'd.

## Notes
- Reuse everything: `cloud_fetch`, `catch_device`, `write_config`, `key_from_existing_config`
  all exist — Phase A is validation + a chooser + messaging, not new architecture.
- Commit per phase; keep `config.json` git-ignored; never log the password.
