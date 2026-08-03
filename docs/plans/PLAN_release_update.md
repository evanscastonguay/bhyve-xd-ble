# Plan — Automated release & update pipeline (Mac → Linux), Solution 2

**Chosen design (from s4):** push-based **immutable symlinked releases** with health-check +
auto-rollback. The Mac drives; the box needs **no GitHub credentials**. "Fully automated on
release" = one `make release` on the Mac (Solution 3's box-side pull is explicitly deferred; its
mechanics would reuse this same switch logic if ever wanted).

## Key facts to design around
- `server.py`: `CONFIG = os.environ.get("BHYVE_CONFIG", os.path.join(HERE, "config.json"))` —
  config path is **env-overridable**. `secrets/` and `config.json` are **gitignored** and live only
  on the box (authoritative, must never be clobbered).
- `app = FastAPI(version="1.2.0")` — hardcoded; no real build-version endpoint yet.
- Box: `bhyve.service` (system unit, `User=evans`, enabled, reboot-survival proven in P4).
  **`sudo` needs a password** (not passwordless); SSH is key-based. GitHub reachable but the box has
  **no repo auth**. `~/bhyve-local` is stale July scratch — leave untouched.
- Test gate: `pytest test_e2e.py` (131 green today).

## Target box layout (Capistrano-style)
```
~/bhyve/
  releases/<ts>_<sha>/     immutable copy of the tree + its own venv + a VERSION file
  shared/config.json       the box's real config (authoritative)
  shared/secrets/          self-key material (if any)
  current -> releases/<latest-good>     (atomic symlink)
```
Each release dir gets symlinks `config.json -> ../../shared/config.json` and
`secrets -> ../../shared/secrets`, so relative paths resolve to shared state. The unit points at
`~/bhyve/current` (`WorkingDirectory` + `ExecStart=.../current/venv/bin/uvicorn …`). Deploys flip the
symlink + restart; they **never** rewrite the unit or touch `shared/`.

## Scope
- In: Mac→Linux push deploy script; immutable release dirs + `current` symlink (atomic switch +
  instant rollback); post-restart health-check with **auto-rollback**; `/api/version` (git SHA +
  build time); a scoped `sudoers` drop-in for unattended restart; prune-old-releases; `make release`
  wrapper; docs + a tracked `deploy/` dir.
- Out: GitHub-Release-triggered **pull** / box self-update (Solution 3); multi-box; CI runner;
  containers; zero-downtime (a few-second restart gap is acceptable for a sprinkler); auto-trigger on
  Mac-side git events beyond `make release`.

## Phases (each proves something)

### P0 — `/api/version` endpoint + VERSION stamping (Mac-side, TDD, no box)
- Add `GET /api/version` → `{"version", "git_sha", "released_at"}`. Reads a `VERSION` file
  (`{sha}\n{iso8601}`) from `HERE` if present (deploy writes it); else falls back to the app version
  + `git rev-parse --short HEAD` if a repo, else `"dev"`. Never throws.
- **TDD:** add a `test_e2e.py` case (fake VERSION file → parsed fields; missing file → graceful
  fallback). **Gate:** suite 131 → 132 green. Mergeable on its own.
- **Proves:** version visibility — the primitive every later phase health-checks against.

### P1 — Box restructure to `~/bhyve/{releases,shared,current}` + unit re-point (one-time; 1 sudo step)
- **Non-destructive:** create `~/bhyve/shared`, **copy** (not move) the current
  `~/bhyve-xd-ble/config.json` + `secrets/` into `shared/`, verify the key is intact
  (`keylen==32`) before anything else. Seed `releases/<ts>_<sha>/` from the current tree, link
  shared in, build its venv, write VERSION, point `current` at it.
- Rewrite the unit for the new paths; **user installs it** (`sudo cp` + `daemon-reload` +
  `restart`). Keep `~/bhyve-xd-ble` as-is until P1 is green (fallback).
- **Gate:** service `active` on the new layout, `/api/status` + `/api/version` OK on the LAN, and it
  **survives a reboot** (repeat the P4 check). **Proves:** the release layout runs prod.

### P2 — `deploy/deploy.sh` core: land + atomic switch + restart (no rollback yet) ✅ DONE 2026-08-02
**Result:** `deploy.sh` ran green end to end — test gate (133) → rsync `releases/<ts>_<sha>/` →
fresh venv → VERSION stamp → atomic `current` flip → unattended restart → confirmed
`/api/version` = new SHA `dc56c0a`. Config preserved (`shared/config.json` keylen=32, untouched);
old release `4c73ff6` retained as a rollback target. Chose a **fresh venv per release** over the
`cp -a` reuse optimization (a copied venv hardcodes absolute interpreter paths — fragile once the
source release is pruned).

- Mac script: (1) `pytest test_e2e.py` gate → abort on red; (2) `id=<ts>_<short-sha>`;
  (3) `rsync` tree → `releases/$id` (exclude `venv .git config.json secrets __pycache__`); (4) on
  box: link shared, build venv — **reuse prev release's venv via `cp -a` when `requirements.txt`
  hash is unchanged**, else fresh; write VERSION; (5) `ln -sfn` flip `current`→`$id` (atomic);
  (6) restart via scoped sudo; (7) print `/api/version`.
- **Gate:** deploy a trivial change (bump a string) → `/api/version.git_sha` changes; assert
  `shared/config.json` key still `keylen==32` (config untouched). **Proves:** end-to-end push works.

### P2a — Scoped `sudoers` drop-in (one-time; user installs via `visudo`) ✅ DONE 2026-08-02
**Result:** `deploy/sudoers.bhyve-deploy` installed to `/etc/sudoers.d/bhyve-deploy` (via
`visudo -cf` validation). `deploy.sh`'s `sudo systemctl restart bhyve.service` now runs with no
password prompt — verified by a full unattended deploy.

- `/etc/sudoers.d/bhyve-deploy`:
  `evans ALL=(root) NOPASSWD: /usr/bin/systemctl restart bhyve.service, /usr/bin/systemctl start bhyve.service, /usr/bin/systemctl stop bhyve.service, /usr/bin/systemctl status bhyve.service`
  Install with `sudo visudo -cf` validation to avoid lockout. **Proves:** `deploy.sh` restarts
  **unattended** (no password prompt).

### P3 — Health-check + auto-rollback (the robustness proof) ⛳ ✅ DONE 2026-08-02
**Result:** `deploy.sh` now records the current release, flips, restarts, then polls `/api/version`
(SHA match) + `/` (200) for ~30s — **pure signals, no BLE** (a sleeping timer can't cause a false
rollback). On failure it flips `current` back, restarts, re-verifies, and exits **non-zero** with a
journal tail. **Proven both directions live:** a healthy deploy passed the check (`6c82063`); a
deliberately broken build (startup `raise` in `_lifespan` — passes the pytest gate since tests don't
run lifespan, but crashes uvicorn) was deployed → health-check failed → **auto-rolled-back to
`6c82063`, box healthy, exit code 1**. A bad release can no longer take down the controller.

- After restart, poll for up to ~30 s until `/api/version.git_sha == $id`'s sha **and**
  `/api/status` returns 200 with a decoded clock. On failure: **flip `current` back to the previous
  release, restart, re-verify**, and exit non-zero with the captured journal tail.
- **Gate (deliberate-break test):** deploy a release with a forced fault (bad import / bad bind) →
  script detects the failed health-check → **auto-reverts** to the last good release → service still
  serving the previous good version. **Proves:** a bad release cannot take down watering.

### P4 — Prune + idempotency + `make release` ✅ DONE 2026-08-03
- Keep last **5** releases (prune older, never the `current` target); `make deploy`/`release`/
  `rollback`/`test`/`run`; `make rollback` = flip to previous + restart + health-check.
- **Result (verified live):** a deploy pruned the box **7 → 5** releases (current preserved);
  `rollback.sh` flipped `08b62eb → 7ca1b68`, health-checked green (root 200), then a redeploy
  restored the latest. Re-running deploy is safe (new id, same outcome).

### P5 — Docs + tracked `deploy/` ✅ DONE 2026-08-03
- Committed `deploy/deploy.sh`, `deploy/rollback.sh`, `deploy/bhyve.service` (template),
  `deploy/sudoers.bhyve-deploy`, `deploy/README.md` (files list + one-time setup + daily use), plus a
  `Makefile` and the README **Run it 24/7 (Linux)** section. Cross-links `PLAN_linux_deploy.md`.

## Risks & mitigations
- **Restart drops BLE mid-watering** → deploys are manual/rare; health-check guarantees recovery;
  (nice-to-have) `deploy.sh` refuses to deploy while `is_watering` unless `--force`.
- **Symlink/systemd race** → flip *then* restart; systemd re-reads `ExecStart`/`WorkingDirectory` at
  (re)start. Use `ln -sfn` (atomic replace).
- **Partial venv / pip failure** → build the new venv *before* the symlink flip; abort on failure so
  `current` never points at a broken release.
- **Losing config in restructure (P1)** → copy-then-verify (`keylen==32`) before touching anything;
  keep `~/bhyve-xd-ble` as a fallback until green.
- **`sudoers` typo → sudo lockout** → a `/etc/sudoers.d/` drop-in installed with `visudo -c`
  validation; user performs it.
- **Disk growth** → prune keep-last-5 (each release ≈ tree + ~40 MB venv).

## Tests / validation
- P0: unit test for `/api/version` (132 green). P1: live status/version + reboot-survival.
- P2: version changes + config-preserved assertion. P3: **deliberate-break → auto-rollback** (headline).
- P4: prune count + `make rollback`. Each deploy self-verifies via health-check.

## Checkpoints (natural stops)
- After **P0** (mergeable code, no box). After **P1** (new layout live + reboot-safe). After **P3**
  (rollback proven — the core "robust & solid" claim). P4–P5 are polish.

## First concrete action
**P0:** on a feature branch off `main`, TDD `GET /api/version` — write the failing `test_e2e.py`
case first, implement the endpoint + VERSION-file reader in `server.py`, get the suite to **132
green**, self-review, PR. (No box interaction; safe to build immediately.)
