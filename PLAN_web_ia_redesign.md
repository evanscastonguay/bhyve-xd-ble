# Plan — Web app IA redesign (0/1/2/N timers, separated control · setup · account)

**Problem:** the single page conflates control, setup, and account, and renders a control
cockpit even with **zero** timers. Evidence: `init()` calls `buildValves(4)` with no devices
(phantom valves); Clock/State + Remove + Stop-ALL are always in the DOM; the controlled timer's
name shows only in the log; "Add a timer" expands inline *below* the still-visible control; the
account login/switch lives inside the per-timer add; a stale onboarding job wedges with
"an onboarding session is already running"; errors use blocking `alert()/confirm()`.

**Chosen shape (analysis + user decisions):** one page, **one context at a time**, driven by a
tiny view state machine. Multiple timers → **segmented tabs**. Add/Account **take over the card**
(control hidden while active). Account lives in a **header chip**, resolved once, never per-timer.

## Dependency truths this encodes
- Control REQUIRES ≥1 registered timer → 0 timers renders NO control.
- Account (email+shared key) is a shared prerequisite of account-adds → its own header menu.
- Selection matters only at ≥2 → 0: empty state; 1: identity + control; 2+: tabs + control.
- Setup and control are different contexts → never stacked.

## Scope
**In**
- `index.html`: view state machine `empty | control | add | account`; header account chip; tabs
  for 2+; control panel rendered only for the selected timer; per-timer Remove inside control;
  Add/Account overlays that hide control; replace `alert()/confirm()` with inline banners + an
  in-page confirm.
- `server.py`: onboarding job lifecycle — a new `start` **supersedes** a stale/awaiting job
  (cancel its task) instead of 409; add `POST /api/onboard/cancel`.
**Out**
- Cipher/BLE/provisioning core; the account/onboarding backend semantics (P1–P4, unchanged);
  routing/multi-page; renaming timers (nice-to-have, defer).

## Phases
### P1 — Onboarding job lifecycle (backend, TDD)
- `onboard_start`: if a job exists and isn't done, cancel its task before starting the new one
  (no 409). `POST /api/onboard/cancel` cancels + clears the job.
- Tests: starting again while a job is "running" supersedes it (no exception); cancel clears.

### P2 — Page shell + view state machine + empty/control (UI)
- Header: title + `#acct_chip`. Main `#app` with containers `#v_empty #v_control #v_add #v_account`.
- `renderApp()`: GET /api/devices + /api/account → 0 timers ⇒ `empty` (only "Add your first
  timer"); else `control` (tabs if ≥2, select current, load + status). No control DOM at 0.
- Move valves/status INTO `#v_control`; per-timer Remove here.

### P3 — Add & Account as takeover overlays (UI)
- "Add" (from empty / tabs +Add / account) → `add` view (reuse the wiz* flow). Done → `renderApp`.
- Account chip → `account` view: signed-out (sign-in form) or signed-in (email + Sign out /
  Switch). Login here feeds the same session; adding a timer no longer logs in.
- Inline error banners; in-page confirm for Remove.

### P4 — Live ⛳ (user): verify 0 → add first → 1 → add second → 2 (tabs) → remove → back to 1/0.

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Big UI rewrite regresses control | reuse the proven control fns (refresh/startZone/stopZone/stopAll/render); keep endpoints; verify every onclick resolves |
| Can't visually verify (no Chrome bridge) | structural asserts (handlers resolve, served 200, endpoints), then user live P4 |
| Superseding a job mid-BLE | cancel only affects the flow task; BLE ops are lock-guarded and short |

## First action
P1: failing test — a second `onboard_start` while a job is running supersedes it (no 409); then implement.
