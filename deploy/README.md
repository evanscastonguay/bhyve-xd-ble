# deploy/ — push releases from the Mac to the always-on Linux box

`deploy.sh` ships the current git HEAD to `bhyve-linux` as an **immutable, symlinked release** and
atomically switches the systemd service onto it. See `docs/plans/PLAN_release_update.md` for the full
design (Solution 2).

```
Mac (dev)  ──rsync──▶  bhyve-linux:~/bhyve/releases/<ts>_<sha>/   (immutable, own venv, VERSION)
                       flip ~/bhyve/current ──▶ that release
                       restart bhyve.service
```

`config.json` and `secrets/` live in `~/bhyve/shared/` and are **symlinked into every release** — a
deploy never copies or overwrites them.

## Daily use

```bash
make deploy       # or: ./deploy/deploy.sh
make rollback     # flip the box back to the previous release
make test         # hardware-free test suite
make run          # run the server locally (127.0.0.1)
```

**Deploy** runs the test suite (aborts on red) → rsyncs a new release → builds its venv → stamps
`VERSION` → flips `~/bhyve/current` → restarts → **health-checks** `GET /api/version` + `/` and
**auto-rolls-back** if the new release doesn't come up → **prunes** to the newest `BHYVE_KEEP` (5)
releases (never the current one). Override targets with env vars: `BHYVE_REMOTE`, `BHYVE_BASE`,
`BHYVE_HEALTH_URL`, `BHYVE_SERVICE`, `BHYVE_KEEP`.

**Rollback** (`deploy/rollback.sh`) flips `current` to the previous release, restarts, and
health-checks it — the manual undo for a release that looked fine to the health check but wasn't.

## Files here
- `deploy.sh` — the push-deploy (test-gate → release → atomic switch → health-check → auto-rollback → prune).
- `rollback.sh` — manual flip to the previous release.
- `bhyve.service` — systemd unit template (paths assume the layout below).
- `sudoers.bhyve-deploy` — scoped NOPASSWD rule for restarting the service unattended.

## One-time box setup

1. **Layout + unit** (plan P1): `~/bhyve/{releases,shared,current}` with `config.json`/`secrets/` in
   `shared/`, and `bhyve.service` (this dir) pointing `WorkingDirectory`/`ExecStart` at
   `~/bhyve/current` — install per the header of `deploy/bhyve.service`.
2. **Passwordless restart** (plan P2a): install `sudoers.bhyve-deploy` so the restart step needs no
   password — see the header of that file for the safe `visudo`-validated install command.
