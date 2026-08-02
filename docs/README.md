# docs/

Reference and process history for bhyve-xd-ble. (Product code lives at the repo root; see the
top-level `README.md`.)

## Reference — how the device actually works (keep current)
- [SPIKE_provisioning.md](SPIKE_provisioning.md) — app-free provisioning: the fe32 GATT chars, the
  AES-CTR link cipher, and the two-phase key-write + finalize (proven on hardware).
- [SPIKE_schedule.md](SPIKE_schedule.md) — the Orbit schedule protocol (`IpcMsg` envelope,
  `SetProgramSchedule`/`SetActivePrograms`, 0-indexed stations, local-minute start times), and why
  on-device autonomous execution is unverified (host-driven scheduling is what ships).

## plans/ — per-feature plans (s3/s5), newest-relevant
`PLAN_linux_deploy.md` is the active one (deploy the server to the always-on Linux box, with a P0
repo-cleanup phase). The rest are the plans behind shipped features — account onboarding, register/
first-run, catch-and-hold, login-UX, web IA redesign, provisioning proof, and the schedule module
(module → p4c → host scheduling). Kept for the reasoning/decisions, not as current status.

## archive/ — historical / stale
`PROJECT_STATUS.md`, `VERIFIED.md`, `ONBOARDING_PLAN.md` — early status/verification notes; their
numbers are outdated (the current test suite is `test_e2e.py`). Kept for history only.
