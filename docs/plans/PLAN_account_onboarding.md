# Plan — Coherent per-account onboarding (clean two flows, remembered account)

**Problem (`/s3-define`):** the Orbit login screen asks for a timer **name + MAC** the account
supplies moments later in the picker, and there is **no account concept** — credentials are
handled per-timer, never remembered, so the with-account and standalone paths are tangled and
ask for data the user doesn't have.

**Chosen approach (`/s4-solutions`, Solution 1 + session-token graft):** decouple
**key/account acquisition** (once per account, reusable) from **timer selection + provisioning**
(per timer). Persist only `{email, network_key}` (git-ignored); keep the login token **in
server memory for the session** so a second add shows the live list without re-login; re-login
on demand across sessions. Password is never stored; the key never reaches the browser.

**Locked decisions:** persist `{email, key}` only (no token at rest, no password); one Orbit
account per install; the proven BLE/provisioning core (`provision_device`,
`catch_device_session`, cipher/framing, SSE tracker) is untouched.

---

## The clean model (what each flow needs, and when)

| | **Orbit account flow** | **Standalone flow** |
|---|---|---|
| Auth | email+password **once per account** (else reuse remembered account) | none |
| Name | **from the account** (picker) | **user-typed** (only place a name is input) |
| MAC | **from the account** (picker); discovered at provision | discovered at provision |
| Key | account mesh key (shared, server-side only) | generated |
| User picks | **which timer** from the account list | nothing |

Sequence: **Orbit** = *authenticate → list → pick → provision*; **Standalone** = *name → provision*.
No screen asks for data the next screen supplies. **MAC is never user input; name is user input
only in standalone.**

---

## Scope

**In scope**
- **Account model (`onboarding.py`)**: `read_account(path)`, `write_account(path, email, key)`
  — a top-level `account: {email, network_key}` block in `config.json`, atomic, preserving
  `devices`; backward compatible with device-only configs.
- **REST account layer (`server.py`)**: in-memory `_account_session` (email, token, key,
  device list); `POST /api/account/login`, `GET /api/account`, `POST /api/account/forget`.
  Login persists `{email, key}`, caches the token+list in memory, returns the timer list
  **without the key**.
- **Onboard `account` mode (`onboarding.py` + `server.py`)**: factor the proven
  reset→provision→verify→save tail into `_provision_and_save(...)`; add `mode="account"` where
  the **server** injects the key (from session cache, else the persisted account block) keyed by
  `device_mac` sent from the browser — the browser never carries the key.
- **UI (`index.html`)**: two clean flows — Orbit (login **email+password only** → picker →
  tracker; skips login when signed in, shows *"signed in as email"* + switch) and Standalone
  (name → tracker). Remove name/MAC from the login screen; remove the ad-hoc "reuse" button.

**Out of scope**
- The account-centric panel with per-timer adoption states (Solution 3) — deferred.
- Multiple Orbit accounts; token persistence across sessions.
- Changes to the cipher/framing, `provision_device` internals, or valves/control UI.
- CLI `register` behavior (untouched; `onboard_flow`'s existing orbit/self/reuse modes stay for it/tests).

## Deliverables
1. `onboarding.py`: account read/write, `_provision_and_save`, `mode="account"`.
2. `server.py`: `_account_session` + `/api/account/{login,,forget}` + `account` in `/onboard/start`.
3. `index.html`: the two clean flows with remembered-account state.
4. `test_e2e.py`: coverage for each phase (key-never-leaks assertions throughout).
5. Live confirmation: log in once, add two timers, second add needs no login.

---

## Phases (each proves something)

### P1 — Account model (offline, TDD)
- `read_account(path) -> {email, network_key} | None`; `write_account(path, email, key)` merges
  an `account` block alongside `devices`, atomically, never clobbering devices.
- **Tests:** round-trip; `write_account` preserves existing devices and vice-versa; `read_account`
  → None on absent/device-only config; invalid JSON refused (like `write_config`).
- **Proves:** the account (email+key, no password) persists and is backward compatible.

### P2 — REST account layer + session graft (offline, TDD)
- `POST /api/account/login {email,password}` → `cloud_fetch` → cache `_account_session`
  (email, token if present, key, devices) → `write_account(email,key)` → return
  `{email, timers:[{name,mac,stations,added}]}` (**no key**; `added` = MAC already in
  `devices`). Auth/MFA/conn errors → HTTP 401/502 with a clear message.
- `GET /api/account` → `{signed_in, email?, has_saved_key}`.
- `POST /api/account/forget` → clear memory session **and** remove the persisted account block.
- **Tests:** login caches+persists+returns list; **assert the key is in no response body**;
  `added` reflects config; bad creds → 401; GET reflects signed-in/has-key; forget clears both.
- **Proves:** login/list is decoupled from provisioning and the key stays server-side.

### P3 — Onboard `account` mode + factor the tail (offline, TDD)
- Extract reset→provision→verify→save into `_provision_and_save(key, key_source, want_mac,
  name, stations, path, gate)`; existing orbit/self/reuse modes call it (**no behavior change →
  existing onboard tests stay green**).
- `mode="account"`: server resolves `key` (session cache → persisted account block), and
  `name`/`stations` (cached list → defaults) by `device_mac`; flow emits `get_key` **done
  (from your account)** then `_provision_and_save`. `POST /api/onboard/start` accepts
  `{mode:"account", device_mac}` and injects the key server-side.
- **Tests:** account mode provisions with the resolved key, saves `key_source="orbit"`, **key in
  no event/body**; missing key → failed step, no provision; the 71 existing tests stay green.
- **Proves:** provisioning consumes an already-resolved key; the browser sends only a MAC.

### P4 — UI: the two clean flows (`index.html`)
- **Choose:** `GET /api/account` → if `signed_in`/`has_saved_key`, primary **"Add a timer to
  <email>"** + a "switch account" link; else **"Use my Orbit account"** / **"Standalone"**.
- **Orbit:** not signed in → **login (email+password only)** → `POST /account/login` → **picker**
  (timers by name; mark *already added*; auto-select if one) → `POST /onboard/start
  {mode:"account",device_mac}` → SSE tracker. Signed in → straight to the picker.
- **Standalone:** name only → `mode:"self"`.
- Remove name/MAC from the Orbit screen; remove the old reuse button.
- **Proves (served-page checks):** login screen has **no** name/MAC inputs; a picker screen and
  an account-state line exist.

### P5 — Live end-to-end  ⛳ gate (user)
- Hard-refresh; **Orbit login once** → pick a timer → provision. Then **＋ Add a timer** again →
  **no login**, straight to the picker → add the second. Confirm *"signed in as email"* + switch.
- Confirm the two timers' account keys are **byte-identical** (the shared-mesh-key assumption
  from s3; both share `mesh_id=6a41a5be…`).
- **Proves:** coherent, reusable, don't-make-me-think — login is asked once per account, never
  per timer, and no screen asks for data it's about to show.

---

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Key leaks to browser via the new list/account mode | login returns list **without** key; account mode takes only `device_mac`, server injects key; explicit "key in no body/event" asserts in P2 & P3 |
| Refactoring the tail regresses proven provisioning | `_provision_and_save` is a pure extraction; existing onboard tests must stay green as the guard |
| Session token lost on server restart → second add "forgets" | that's the accepted design; `has_saved_key` still lets account mode inject the persisted key by MAC without a live list; UI falls back to re-login for a fresh list |
| Account block breaks old configs | `read_account` tolerates its absence; `write_account`/`write_config` share the atomic, JSON-guarded writer |
| Shared-key assumption wrong (per-timer keys) | P5 confirms byte-identity before relying on one account key; if false, fall back to per-device keys (already stored per device) |
| Two provisioning entry paths drift | single shared `_provision_and_save` tail; modes differ only in how the key is obtained |

## Tests / validation
- **Offline (automated):** P1 account round-trip; P2 login/list/forget + key-never-in-body; P3
  account-mode provision + tail extraction + existing suite green.
- **Live (P5):** one login, two adds, second loginless; key byte-identity check.

## Checkpoints
- ⛳ **After P1** — account persists (email+key), offline.
- ⛳ **After P2** — login/list decoupled, key server-side, offline.
- ⛳ **After P3** — provisioning consumes a resolved key; browser sends only a MAC.
- ⛳ **After P4** — the two clean flows render on the served page.
- ⛳ **After P5** — you log in once and add timers without re-login.

## First concrete action
**P1:** add a FAILING test — `read_account`/`write_account` round-trip, and `write_account`
preserves an existing `devices` list — then implement until green. `config.json`/`secrets/`
stay git-ignored; the password is never written; the key never enters an HTTP body or SSE event.
