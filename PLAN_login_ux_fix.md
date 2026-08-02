# Plan — Fix the onboarding login UX (clear, low cognitive load, working)

**Problem (`/s3-define`):** the Add-a-timer card crams mode choice + all fields + explanation
together, with the **action buttons above unmarked, non-required credential fields** — so
users start Orbit mode with an empty login and hit "Login failed." The login **works**
(proven: a real 401 from Orbit; `aiohttp` present); the UI just never clearly **asks for or
requires** credentials. Plus two backend step-state bugs make it *look* broken.

**Chosen approach (`/s4-solutions`, Solution 2 + grafts):** a **linear multi-screen wizard**
— choose path → (Orbit) a real **login form** with a Sign-in button gated on non-empty
creds → the SSE step tracker. Graft in **client-side gating** (can't submit empty) and
**reuse-key awareness** (skip login when a saved key exists). Fix the two backend bugs.

Builds on `PLAN_web_onboarding.md` (the state machine + SSE + wizard already shipped).

---

## Scope

**In scope**
- **Backend (`onboarding.onboard_flow`)**:
  - mark `get_key` **failed** on login failure (no frozen "⟳ …almost there");
  - emit a **resolved (`done`) event** for gated steps (`await_reset`, `fallback_choice`,
    `app_instructions`) after they're actioned, so an SSE reconnect/replay doesn't resurrect
    old buttons;
  - add **mode `reuse`**: use `key_from_existing_config(path)` (the saved account key) with
    **no cloud login**; error step if there's no saved key.
- **UI (`index.html`)**: a small client **screen state machine** — ① Choose (two cards; a
  **"Add another (uses saved key)"** primary when `has_key`) → ②a **Orbit login** (email +
  password + **"Sign in & find my timer"**, disabled until both filled, inline error) / ②b
  **Standalone** (name + impact note) → ③ the existing SSE tracker. Back/Cancel.
- Live retry of a real Orbit login.

**Out of scope**
- A `verify-login` endpoint / live "test sign-in" (Solution 3) — defer.
- Reworking control/valves UI; the SSE tracker itself (reused as-is).
- Server/transport changes beyond what the flow needs.

## Deliverables
1. `onboarding.py`: `onboard_flow` step-state fixes + `mode="reuse"`.
2. `index.html`: the multi-screen wizard (choose → login/name → tracker) with gating + reuse.
3. `test_e2e.py`: coverage for the flow changes.
4. Live confirmation you can log in (or knowingly pick standalone/reuse) without guessing.

---

## Phases (each proves something)

### P1 — Backend flow fixes + reuse mode (offline, TDD)
- `get_key` → on `auth_failed`/`no_device_on_account`, first yield `get_key` **failed**, then
  the `fallback_choice`.
- After each `await gate.wait()` on a gated step, yield that step id again as **`done`**
  (e.g. `await_reset` "searching…", `fallback_choice` "chose standalone", `app_instructions`
  "retrying…") so the event log's *last* state for that id is resolved.
- `mode == "reuse"`: `key = key_from_existing_config(path)`; if `None`, yield a failed step
  and stop; else proceed as an orbit-keyed device (no `cloud_fetch`).
- **Tests:** (a) auth-fail emits `get_key` failed then `fallback_choice`; (b) after resuming
  `await_reset`, an `await_reset` `done` event exists and it's the last for that id; (c)
  `reuse` with a saved key provisions without calling `cloud_fetch` (assert it's never
  called); (d) `reuse` with no key → failed step, no provision. Existing onboard tests + 65
  e2e + 55 offline stay green.
- **Proves:** the flow no longer freezes/duplicates, and reuse works loginless.

### P2 — Multi-screen wizard UI (`index.html`)
- Client screen state: `choose | orbit | self | progress`. Render one screen at a time.
- **① Choose**: two cards with a one-line pro each; fetch `GET /api/onboard/state` — if
  `has_key`, show a primary **"Add another timer (uses your saved Orbit key)"** → `mode=reuse`.
- **②a Orbit**: email + password inputs + **"Sign in & find my timer"** button **disabled
  until both non-empty**; a "why" line; on the flow's `get_key` failed → show the error +
  the fallback choice (Orbit-app-first / standalone) cleanly.
- **②b Standalone**: name + the impact note + "Set up standalone".
- **③ Progress**: the existing `EventSource` step tracker (unchanged), now clean thanks to P1.
- Back/Cancel; password cleared after submit.
- **Proves:** a person picks a path and (for Orbit) logs in via an obvious, gated form.

### P3 — Live retry  ⛳ gate
- Reload the page (no-store is set), click **＋ Add timer**, choose Orbit, **enter real
  creds**, Sign in → confirm it fetches the key and proceeds (or, if MFA, the message is
  clear and standalone/app-first is one click). Also sanity-check standalone + reuse.
- **Proves:** the original blocker — "I can't log in" — is gone.

---

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Resolved-event change breaks existing onboard tests | additive `done` events aren't `waiting_user`, so the test driver ignores them; assert-by-`any`/`[-1]==save` still hold; re-run suite |
| `reuse` mode mislabels key source | set `key_source="orbit"` (it *is* the account key); covered by test |
| Client screen state machine bugs (stuck screen) | keep it tiny (4 screens, explicit `show(screen)`); Back/Cancel always available |
| SSE replay still flashes old states | last-write-by-id on the client + P1 resolved events = final state is correct |
| MFA account still can't log in | not solvable; UI states it and offers standalone/app-first (already in fallback) |

## Tests / validation
- **Offline (automated):** P1 flow tests (get_key-failed, resolved events, reuse with/without
  key). Full 65 e2e + 55 offline stay green.
- **Live (P3):** real Orbit login through the new form.

## Checkpoints
- ⛳ **After P1** — flow fixed + reuse works, offline.
- ⛳ **After P2** — wizard is a clear choose→login→progress on the served page.
- ⛳ **After P3** — you can actually log in.

## First concrete action
**P1:** add a FAILING test — on `auth_failed`, `onboard_flow` emits a `get_key` **failed**
event before `fallback_choice`; and `mode="reuse"` with a saved key provisions **without
calling `cloud_fetch`**. Then implement the flow changes until green.

## Notes
- Reuse: `key_from_existing_config`, the SSE tracker, the fake BLE harness.
- Commit per phase; `config.json`/`secrets/` stay git-ignored.
