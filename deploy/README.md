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
./deploy/deploy.sh
```

It: runs the test suite (aborts on red) → rsyncs a new release → builds its venv → stamps `VERSION`
→ flips `~/bhyve/current` → restarts the service → confirms `GET /api/version` reports the new SHA.
Override targets with env vars: `BHYVE_REMOTE`, `BHYVE_BASE`, `BHYVE_HEALTH_URL`, `BHYVE_SERVICE`.

## One-time box setup

1. **Layout + unit** (plan P1): `~/bhyve/{releases,shared,current}` with `config.json`/`secrets/` in
   `shared/`, and `bhyve.service` pointing `WorkingDirectory`/`ExecStart` at `~/bhyve/current`
   (template: `deploy/bhyve.service`, added in P5).
2. **Passwordless restart** (plan P2a): install `sudoers.bhyve-deploy` so the restart step needs no
   password — see the header of that file for the safe `visudo`-validated install command.
