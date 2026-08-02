# Plan — Deploy the server to the always-on Linux box (`bhyve-linux`)

**Goal:** run `uvicorn server:app` on the Ubuntu box near the timer so **control and host
scheduling no longer depend on the Mac** (which sleeps / roams out of BLE range). The Mac becomes
optional; the Linux box is the always-on controller.

**Target:** `ssh bhyve-linux` (passwordless) → `pi@192.168.1.50`, Ubuntu 24.04.4, Python 3.12,
working `hci0` Bluetooth (BlueZ 5.72). See [[linux-control-box]].

## Key facts to design around
- **Address differs by platform:** macOS uses a CoreBluetooth UUID; **Linux/BlueZ uses the real
  MAC as the address**. So the Mac's `config.json` (`address` = `29EFE4EB-…`) will NOT work on
  Linux — the Linux config needs `address = AA:BB:CC:DD:EE:01` (the MAC). Confirmed by the old
  `bhyve_config.linux.json`. `current_platform()`→linux and the linux `resolve_address` branch
  already exist.
- **The network key is secret** — it lives in `config.json` (git-ignored). It must be transferred
  **securely** (scp over the SSH key we set up) or the timer re-adopted on the box; never
  committed. **Do not type any password** (key auth only).
- **BLE preconditions on Linux:** the box must be **physically near the timer**, the **phone's
  Bluetooth off** (single BLE link), and `evans` must be able to talk to BlueZ (usually fine as a
  normal user; may need the `bluetooth` group / adapter powered — `hci0` is UP).
- **Avoid double-scheduling:** once Linux runs scheduling, **disable it on the Mac** (or stop the
  Mac server) so a rule doesn't fire twice.

## Scope
**In**
- Provision the box: clone the repo, create a venv, install `requirements.txt`, confirm imports +
  a read-only BLE scan (bleak/BlueZ sees the timer).
- Linux `config.json` with `address = MAC`, same key (built on-box or scp'd + edited), 0600.
- Verify **live control from Linux** (status read; then valve start/stop, hose off).
- **systemd service**: run uvicorn on boot, restart on failure, bound to the LAN.
- Verify **host scheduling fires from the Linux box** (the payoff) + survives a reboot.
- Docs: how to reach the UI (`http://192.168.1.50:8000/`), and switching the Mac off scheduling.

**Out**
- On-device/standalone scheduling (dormant, unchanged).
- Exposing the server beyond the LAN / TLS / auth (LAN-only for now; note it).
- Any change to the control/schedule logic — this is deployment, not new features.

## Phases (each proves something)
### P0 — Repo cleanup (on the Mac / git, before the Linux clone)
So the box clones only product-relevant files. Verified by usage analysis (imports + refs).
- **Organize docs under `docs/`** (README.md stays at root):
  - `docs/` ← `SPIKE_provisioning.md`, `SPIKE_schedule.md` (reverse-engineering **reference** — keep handy).
  - `docs/plans/` ← the `PLAN_*.md` (active `PLAN_linux_deploy.md` + historical).
  - `docs/archive/` ← `PROJECT_STATUS.md`, `VERIFIED.md`, `ONBOARDING_PLAN.md` (stale/historical).
- **Delete orphan/stale scripts** (git history retains them — decision: delete):
  - `selftest_offline.py` — stale (54/55, one mismatch) + redundant with `test_e2e.py`.
  - `bhyve_lab.py` — original manual script, superseded by `cli.py`, imported by nothing.
  - (local `bhyve_lab.log` is git-ignored; remove it locally too.)
- **Keep at root** (decision): product modules (`bhyve_xd`, `onboarding`, `cli`, `server`,
  `index.html`, `schedule`, `scheduler`, `schedule_device`), `config.example.json`,
  `requirements*.txt`, `README.md`, `.gitignore`, and the tests **`test_e2e.py`** +
  **`provision_proof.py`** (the latter is imported by `test_e2e.py:1292` — must stay importable).
- **Fix references**: update `README.md` (it points to `selftest_offline.py` and will point at
  `docs/…`); update any doc cross-links as needed.
- **Gate:** `pytest test_e2e.py` still **131 green**; `python -c "import server, cli, onboarding,
  bhyve_xd, schedule, scheduler, schedule_device"` OK. Do it via git mv (preserve history), one PR.
- **Proves:** the tree is only what the product needs — a clean base to deploy.

### P1 — Provision (no device writes) ✅ DONE 2026-08-02
- On `bhyve-linux`: `git clone` the repo (or rsync), `python3 -m venv venv`, `pip install -r
  requirements.txt`. Install any BlueZ deps if pip/bleak need them (`libglib2.0`, dbus present by
  default on 24.04).
- Confirm: `python -c "import bleak, fastapi, uvicorn, cryptography, aiohttp"`; a **read-only BLE
  scan** that sees a device advertising the `fe32` service (proves bleak↔BlueZ↔adapter works).
- **Proves:** the code runs and the radio is usable on the box.
- **Result:** private repo → anon `git clone` failed, so deployed by **rsync** to `~/bhyve-xd-ble`
  (excluding `venv/ .git/ secrets/ config.json`). venv built; runtime + product imports OK. Radio
  proof: scan saw **"New Timer" `AA:BB:CC:DD:EE:01`** (rssi −57); a **read-only** connect + GATT
  discovery confirmed **`fe32`** with chars 6c71/6c76/6c72/6c73 (no writes, no arm).
- **⚠ Finding for P2:** timers do **NOT** advertise `fe32` — it's post-connect GATT. On Linux,
  connect by **MAC**, don't filter scans by service UUID. `config.json`/`secrets/` not yet on the box.

### P2 — Config + live status ⛳ ✅ DONE 2026-08-02
- Create `/home/pi/.../config.json` on the box with `address = AA:BB:CC:DD:EE:01`, `mac` same,
  the current network key, `stations 4`. (scp the key material over SSH, or re-adopt on the box;
  chmod 600.) `.gitignore` already excludes it.
- `python cli.py status` (or `GET /api/status`) **from the box** → decodes clock/state.
- **Result:** `~/bhyve-xd-ble/config.json` written via SSH pipe (key never printed), `chmod 600`,
  `address` = MAC `AA:BB:CC:DD:EE:01`, tz −14400, 4 stations. **Live status decoded from the box**
  — CLI twice (clock advanced `21:31:24 → 21:31:54`, live) and **`GET /api/status`** (`{"clock":
  "21:33:38 UTC","is_watering":false,"run_state":0,...}`, root 200 / API 200). Full app stack runs
  on Linux (handshake → arm → encrypted notify → counter resync → parse). No valve writes.
- **Gotcha (recorded):** never `pkill -f "uvicorn server:app"` over SSH — the pattern matches the
  SSH shell's own argv and kills the session. Kill by PID (skip `$$`); launch detached
  (`setsid … </dev/null &` + pidfile).
- **Proves:** the Linux box can actually control THIS timer over BLE.

### P3 — Control parity (hose off)
- Start/stop a valve from the box (`cli.py start 1 60`, `stop`) → confirmed watering/idle.
- **Proves:** full manual control works from Linux, same as the Mac.

### P4 — systemd service
- Unit `bhyve.service`: `ExecStart=…/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000`,
  `Restart=on-failure`, `WorkingDirectory` the repo, `User=evans`, `After=bluetooth.target`.
  `systemctl --user` or system unit (system unit needs sudo — user performs the privileged step).
- Enable on boot; verify it serves `http://192.168.1.50:8000/`, and **survives a reboot**.
- **Proves:** always-on; no terminal needed.

### P5 — Schedule on Linux ⛳ (the payoff)
- Author/enable a rule via the box's UI/API; confirm the loop **fires the valve on schedule** from
  the box (like the Mac P3 proof) — with the Mac's scheduling **off**.
- **Proves:** schedules run without the Mac.

### P6 — Access + docs
- README: LAN URL, that it's LAN-only (no auth), the Mac-vs-Linux scheduling switch, and where the
  key lives on the box.

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| macOS UUID address in config won't connect on Linux | Linux config uses `address = MAC` (proven by old linux config) |
| Secret key transfer | scp over the existing SSH key, chmod 600; never commit; or re-adopt on-box |
| BlueZ perms / adapter | run as `evans`; add to `bluetooth` group if needed; `hci0` already UP |
| Box not in BLE range of the timer | position the box near the timer; P2 status read is the gate |
| Double-firing (Mac + Linux both scheduling) | turn Mac scheduling off once Linux is authoritative |
| systemd needs sudo | the user runs the one privileged install step; I prep the unit file |

## Tests / validation
- P1 imports + scan; P2 live status; P3 start/stop; P4 serves + reboot-survives; P5 live schedule fire.
- (No new automated tests — this is deployment; the app's 131 tests already cover the code.)

## Checkpoints
- ⛳ P2 — Linux controls the timer.
- ⛳ P4 — always-on service.
- ⛳ P5 — schedules fire from Linux without the Mac.

## First concrete action
P0: on the Mac, `git mv` the docs into `docs/` (+ `docs/plans/`, `docs/archive/`), delete the two
orphan scripts, update README references, confirm `pytest test_e2e.py` stays 131 green + product
imports OK, and open a PR. THEN P1 clones the cleaned repo onto `bhyve-linux`. (Any `sudo`/
privileged Linux step I'll hand to you; no SSH password typing.)
