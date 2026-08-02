# Plan — Prove durable app-free provisioning beyond doubt

**Problem (`/s3-define`):** our app-free-provisioning "evidence" is worthless — circular
(a fake flag set by the field we send), in-session only, single-device, confounded (the
app did the real enrolling), and on buggy code that can key the wrong device. We need a
**reproducible, confound-free hardware protocol** that proves our tooling alone takes a
factory-reset timer to a state that **survives a power-cycle and stays controllable**.

**Chosen approach (`/s4-solutions`, Solution 2):** fix the safety bug, then an **automated
proof-harness** built around a **negative control** (prove keyless first) → app-free
provision → **power-cycle + fresh-process verify** → **loop N times** → one PASS/FAIL
evidence log. Optional: run twice on two devices for generalization.

**Linchpin:** the *negative control* converts an in-session read-back (circular/confounded)
into causal proof — the device goes from *provably can't decode* to *durably can decode*
with **only our writes** in between and the **app never touched**.

Context: adversarial audit + `SPIKE_provisioning.md` (the finalize sequence, field 94),
`PROJECT_STATUS.md §10` ("built; persistence fix in; live-unconfirmed").

---

## Scope

**In scope**
- **P0 — safety fix** to `onboarding.provision_device`: never write the key to the wrong
  device; refuse ambiguous scans; surface partial-provision failures.
- **P1 — proof harness** (`provision_proof.py`): guided, logged protocol implementing the
  negative-control → provision → power-cycle → verify loop, with a pure, unit-tested
  pass/fail evaluator; post-cycle verify runs in a **fresh subprocess**.
- **P2 — live run**: user performs the physical steps; harness emits a timestamped
  evidence log → PASS/FAIL.
- **P3 — record the outcome honestly** (✅ proven, or ❌ + trigger a retained capture).

**Out of scope (unless P2 fails / user opts in)**
- BLE-sniffer corroboration and mandatory 2-device runs (Solution 3) — optional add-ons.
- Cloud device-registration (local control needs only the on-device key; revisit only if durability fails *with* a good finalize).
- Any claim of success before the live harness passes.

**Definition of "proven" (fixed up front):** the harness PASSES only if, in one trial:
negative control **fails to decode** (device confirmed keyless) → our provision succeeds
→ **≥3 consecutive power-cycles** each followed by a **fresh-process** `status` decode +
`start`/`stop` read-back, with the **app never opened** throughout. (Two devices = stronger,
optional.)

## Deliverables
1. `onboarding.py`: hardened `provision_device` (+ tests).
2. `provision_proof.py`: the harness (+ unit-tested `evaluate_trial`).
3. Evidence log from a live run (git-ignored — contains the key).
4. `PROJECT_STATUS.md`/`SPIKE_provisioning.md` updated with the **actual** result.

---

## Phases (each proves something)

### P0 — Fix the wrong-device / partial-provision bug (TDD, offline)  ✅ DONE
- Scan and **connect-to-check `fe32` WITHOUT writing** (the fe32 check precedes any key
  write, so this is safe). Collect B-Hyve candidates.
  - **0 candidates** → `ResolveError` (nothing to provision).
  - **>1 candidates** → **abort without writing** ("multiple fresh B-Hyve nearby — isolate
    the target"). Never spray the key.
  - **exactly 1** → write key + `provision_setup` + verify; if `want_mac` set and the
    read-back MAC mismatches → report failure (device was singular, so no wrong-device
    write) — do **not** hunt others.
- **Surface partial failures**: if the `6c76` write succeeds but setup/verify throws,
  raise a clear error naming the touched device instead of `except: continue`.
- **Tests:** refuses (writes nothing) when 2 fresh B-Hyve present; writes to the single
  candidate; partial-failure surfaced not swallowed. Full suite stays green.
- **Proves:** a proof trial can't be silently confounded by a mis-targeted key write.

### P1 — Proof harness `provision_proof.py` (logic TDD + guided live steps)  ✅ DONE
Guided script, timestamped log, steps:
1. Prompt: "factory-reset into pairing mode; phone Bluetooth OFF; **do not open the app**."
2. **Negative control:** connect to the B-Hyve (confirm `fe32` present) and attempt
   `arm`+`read_status` with the account key. **Require: no decode / no MAC.** If it
   **does** decode → **ABORT** ("device already keyed — not a clean keyless start").
3. **Provision (app-free):** run the hardened `provision_device`; log the in-session decode.
4. Prompt: "**POWER-CYCLE** the timer (pull batteries ~15 s); phone still OFF."
5. **Durability verify in a FRESH subprocess** (spawn `cli.py status` + a `start`/`stop`):
   require decode + confirmed watering→idle.
6. **Loop 4–5 ≥3×.** Emit a machine-checked **PASS/FAIL** summary.
- Extract `evaluate_trial(step_results) -> PASS/FAIL` as a **pure function** and unit-test
  it (neg-control-must-fail, provision-ok, all cycles-ok). BLE steps are live, not mocked.
- **Proves:** the harness scores a pass *only* on the full non-circular causal chain.

### P2 — Live proof run  ⛳ gate
User runs `provision_proof.py`, performing reset/power-cycles at the prompts; harness
writes `/tmp/provision_proof.log`. Outcome:
- **PASS** → durable app-free provisioning is **proven with evidence** (Row 9 → ✅).
- **FAIL** → provisioning does **not** persist with the current finalize; **honest ❌**, and
  the trigger to capture a fresh app enrollment (retained this time) to find the missing step.

### P3 — Record the outcome
Update `PROJECT_STATUS.md §10` + `SPIKE_provisioning.md` with the **real** result and the
evidence-log reference. No optimistic wording — state exactly what the harness showed.

---

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Device lock-up from many connects (neg-control + provision + N verifies) | the protocol's own **power-cycles clear it**; add backoff; keep counts modest |
| Neg-control ambiguity (keyless vs device absent) | require **`fe32` present but no decode** — proves present-and-keyless |
| "Fresh process" not truly fresh (in-memory carryover) | run the post-cycle verify in a **spawned subprocess** |
| Human confound (app opened / phone on) | harness prompts + **neg-control gate aborts** if the device is already keyed |
| Wrong-device key write during the proof | **P0 fix** (single candidate; refuse >1) |
| Provisioning genuinely doesn't persist | that's a **valid FAIL result** → retained fresh capture to find the finalize |
| Only one device | protocol repeats power-cycles (n≥3); optional 2nd-device run for generalization |

## Tests / validation
- **Offline (automated):** P0 bug-fix tests; `evaluate_trial()` pure-logic tests. Existing
  51 e2e + 55 offline stay green.
- **Live (the actual proof, P2):** the harness run + its timestamped evidence log.

## Checkpoints
- ⛳ **After P0** — provisioning is safe (no wrong-device write); suites green.
- ⛳ **After P1** — harness pass/fail logic proven offline; ready for the live run.
- ⛳ **After P2** — the verdict: durable app-free provisioning PROVEN (✅) or FALSIFIED (❌).

## First concrete action
**P0:** add a FAILING test — `provision_device` must **not** write the key (assert the fake
never receives a `6c76` write) when **two** fresh B-Hyve devices are present, and must
`ResolveError`/abort instead. Then implement the scan-count-then-write-one logic + partial-
failure surfacing until green.

## Notes
- The harness is a human-in-the-loop proof tool (like `bhyve_lab.py`); I cannot perform the
  physical reset/power-cycle — the human does, the harness does the BLE + evidence.
- Evidence logs contain the network key → keep in `/tmp` (or git-ignored), delete after.
- Commit per phase; keep `config.json` git-ignored.
