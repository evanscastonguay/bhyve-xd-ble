# Plan — Simple, robust device registration (`cli.py register`)

> **STATUS: COMPLETE.** All 5 phases done + Phase 4 live gate PASSED on real hardware
> (registered the new timer in one command, phone off, <1 min, key reused, no cloud).
> `cli.py register` is the onboarding path. Deferred: web onboarding wizard (Solution 3).


**Problem (`/s3-define`):** registering a new timer is a fragile, manual, multi-tool
ritual (blind phone-release timing + hand-editing `config.json`). We want **one
command** that takes a timer from unknown → controllable in <1 min, ≤3 guided steps.

**Chosen solution (`/s4-solutions`, Solution 2):** a unified `cli.py register` built
on a robust **connect-on-detection** discovery primitive (`catch_device`), with a
single clear precondition — **phone Bluetooth OFF** — instead of mid-scan timing.

**First-principles core:** the only hard step is *"connect to a currently-advertising
timer and read its MAC back."* It becomes easy once (a) the phone isn't holding the
timer (phone off up front — proven: an ordinary 30 s scan then works), and (b) we
connect to the exact advertisement we just saw (robust to rotating addresses, unlike
scan-then-connect which we saw fail with `CBError Code=12`). Everything else — key
fetch, identify, write, verify — is deterministic.

Supersedes the macOS-only discovery assumptions in `ONBOARDING_PLAN.md`; complements
`PLAN_catch_and_hold.md` (same catch engine, different goal: register vs. hold).

---

## Scope

**In scope**
- `onboarding.catch_device(...)` — connect-on-detection discovery: scan, connect to the
  live advertisement, verify `fe32`, adopt the session, read the device MAC + status.
  (Lives in `onboarding` alongside `resolve_address`/`ResolveError`, keeping `bhyve_xd`
  the pure protocol core. Works for stable AND rotating addresses on any platform.)
- `onboarding.write_config(...)` — atomic, idempotent merge of a device into the
  `config.json` `devices` list (dedupe/update by MAC; never half-write).
- `cli.py register` — orchestration: obtain key (reuse existing account key, else one
  cloud login) → "phone off" prompt → `catch_device` → `write_config` → verify by
  read-back → clear retry guidance on failure.
- Tests: offline unit/e2e for all three; a live-hardware gate.
- Retire `bhyve_lab.py`'s duplicated protocol (becomes a thin wrapper or a diagnostic).

**Out of scope (deferred, not blocked)**
- Web onboarding wizard (Solution 3) — sits on top of `catch_device` later.
- MFA beyond the existing typed error; keychain/credential storage; non-HT34 models.
- Replacing `resolve_address` (kept for compatibility; `register` uses `catch_device`).

## Deliverables
1. `onboarding.py`: `catch_device()` (reuses the merged `session(client=...)` adopt path). ✅ Phase 1 done.
2. `onboarding.py`: `write_config()` (+ a `key_from_existing_config()` helper for reuse).
3. `cli.py`: `register` command + `main()` wiring + usage docs.
4. `test_e2e.py`/`selftest_offline.py`: new coverage (see Tests).
5. README "Register a new timer" section; `bhyve_lab.py` slimmed to reuse `catch_device`.

---

## Phases (each proves something)

### Phase 1 — `catch_device` primitive (robust discovery)  ⛳ substrate  ✅ DONE
Connect-on-detection in `onboarding`: `catch_device(network_key_hex, *, want_mac=None,
scan_timeout=90, near_rssi=-80) -> (address, mac, DeviceStatus)`.
- macOS: `BleakScanner` detection stream → on a new close advert, connect to THAT
  `BLEDevice`, `fe32` fast-reject, adopt `session(client=...)`, `arm()`, `read_status()`;
  return the one matching `want_mac` (or the first B-Hyve if `want_mac is None`).
- Linux: connect directly by MAC (address == MAC); read status to confirm.
- Cap probes + back off (respect the lock-up gotcha); connect only to genuinely new,
  close adverts.
- **Test:** extend the fake harness with a fake `BleakScanner` that emits adverts
  (incl. a *rotating-address* timer + a non-`fe32` decoy). Assert: found+MAC read;
  decoy rejected; `want_mac` match among several; timeout → `ResolveError`.
- **Proves:** robust discovery for stable AND rotating addresses, in product code.

### Phase 2 — `write_config` (atomic, idempotent)  ✅ DONE
`onboarding.write_config(path, device)` merges `{name,address,network_key,mac,stations,
tz_offset_sec?}` into the `devices` list.
- Dedupe/update by `mac` (re-registering a drifted address updates in place, doesn't
  duplicate). Write to a temp file + rename (never leave a half-written config).
- **Test:** tmp_path — create new; append second; idempotent update by MAC; malformed/
  missing file handled; temp-rename leaves valid JSON.
- **Proves:** persistence is safe and reproducible (success criterion 5).

### Phase 3 — `cli.py register` end-to-end (offline)  ✅ DONE
`register [email] [--name NAME] [--reuse-key] [--show-key]`:
1. **Key:** if `config.json` already has an account key → reuse it (no cloud, no login).
   Else prompt creds → `cloud_fetch` → choose device (match caught MAC, else pick/prompt).
2. Prompt: **"Turn your phone's Bluetooth OFF and keep the timer near the Mac. Enter."**
3. `catch_device(key, want_mac=…?)` → address, mac, status.
4. `write_config(...)` with a friendly default name.
5. Verify: print the confirmed status + MAC; on timeout, actionable retry hint
   ("phone still holding it? Bluetooth OFF and retry").
- **Test:** mock `catch_device` + `cloud_fetch` + tmp config → registering writes the
  right entry and reuses the key on a 2nd device with NO cloud call.
- **Checkpoint ⛳:** one command registers a device end-to-end with no hand-editing (offline).

### Phase 4 — Live hardware gate  ⛳
With phone Bluetooth OFF, run `cli.py register --name "New Timer 2"` (or re-register the
known unit into a throwaway config) and confirm: caught, MAC-confirmed, written, status
read back — in <1 min, ≤3 manual actions.
- **Proves:** the real UX target (success criteria 1–3, 7). Do not claim done until green.

### Phase 5 — Cleanup & docs  ✅ DONE
- README "Register a new timer" (phone-off precondition front and centre).
- Slim `bhyve_lab.py` to call `catch_device` (kill duplicated crypto/framing) or mark it
  purely diagnostic. Full offline + e2e suites green. Commit + push per phase.

---

## Risks & mitigations
| Risk | Mitigation | Status |
|---|---|---|
| Rotating address stale between scan and connect | **connect-on-detection** to the advert's own `BLEDevice` (no rescan) | core of Sol. 2 |
| Phone still holding the timer → empty scan | single up-front "Bluetooth OFF" precondition + actionable retry, not blind timing | by design |
| Several B-Hyve timers in range | match by `want_mac` when known; else list + pick first with a clear warning | Phase 1/3 |
| Connect storms lock the device | connect only to new/close adverts; cap + backoff | Phase 1 |
| Half-written config on crash | temp-file + atomic rename; validate before replace | Phase 2 |
| Cloud MFA / rate-limit on first device | existing typed errors (`AuthError/RateLimited/MFARequired`) surfaced with guidance | reuse |
| Testing connect-on-detection (concurrency) | inject a fake `BleakScanner` advert stream into the existing fake harness | Phase 1 |

## Tests / validation
- **Offline/e2e (automated):** `catch_device` (rotating + decoy + match + timeout);
  `write_config` (create/append/idempotent/atomic); `register` (reuse-key no-cloud path
  + cloud path, tmp config). Existing 25 e2e + 55 offline must stay green.
- **Live gate (Phase 4):** one-command register on real hardware, phone off, <1 min.

## Checkpoints
- ⛳ **After Phase 3** — offline one-command registration proven.
- ⛳ **After Phase 4** — real UX target met on hardware.

## First concrete action  ✅ DONE (PR: register-phase1-catch-device)
**Phase 1:** added `catch_device` to `onboarding.py` with failing-first tests — a fake
rotating-address timer discovered by connect-on-detection + MAC read back, want_mac
match among several, and a non-`fe32` decoy rejected / timeout raises `ResolveError`.
28 e2e + 55 offline green.

**Phase 2 done:** `onboarding.write_config()` — atomic (temp+rename), idempotent
merge by MAC, refuses to clobber malformed JSON. 33 e2e + 55 offline green.

**Phase 3 done:** `cli.py register` — reuse-key/no-cloud path (+ cloud path with
device pick), phone-off prompt, `catch_device` → `write_config` → confirm; failure
reports guidance and writes nothing. `_parse_register` for arg parsing. 36 e2e + 55
offline green.

**Next:** Phase 4 — LIVE hardware gate (user-run): `cli.py register --name "..."` with
phone Bluetooth OFF registers the real timer in <1 min.

## Notes
- Reuse: the merged `session(client=...)` adopt path is the connection substrate;
  `cloud_fetch` unchanged; `catch_device` is the productized `bhyve_lab` engine.
- Biggest UX unlock discovered this session: **phone Bluetooth OFF up front** removes
  the timing dance entirely — an ordinary scan then finds the timer.
- Commit per phase; keep `config.json` git-ignored.
