# Plan — Credentials-driven onboarding (Solution A)

Turn the hardcoded single-device setup into: **email + password → working
`config.json`**, with automatic per-platform BLE address resolution ("pairing"),
host-derived timezone, and multi-device support. CLI-first, built as a small
reusable module so a web flow can be added later without rework.

Grounded in live de-risking (see `experiments/`): the two hard unknowns are
already proven — macOS UUID↔MAC pairing (read MAC from reply field 1) and
robust discovery (scan → fast-reject non-`fe32` → arm+read-MAC → match).

## Scope

**In scope**
- `onboarding.py`: cloud login/fetch, address resolver (platform strategy), config store.
- `cli.py login` (email/password → config), `cli.py find` (re-resolve drifted address).
- Host-derived timezone (kills hardcoded `-14400`).
- Multi-device config + `--device <name|idx>` targeting across CLI (and server/UI).
- `parse_reply` exposes the device MAC (needed for pairing).
- Tests: offline unit tests + live end-to-end.

**Out of scope (deferred, not blocked)**
- Web onboarding UI (Solution B) — the module is designed so this drops in later.
- Multi-model abstraction (Solution C) — scope to HT34 now; keep the message layer swappable.
- Credential storage / keychain — password is never persisted (hidden prompt only);
  the network key remains the durable secret in the git-ignored `config.json`.
- Non-HT34 B-Hyve models.

## Deliverables
1. `onboarding.py` — `cloud_fetch(email,password)`, `resolve_address(...)`, `host_tz_offset()`, `write_config(...)`.
2. `cli.py login` / `cli.py find` + `--device` targeting.
3. `bhyve_xd.py`: `parse_reply` → `DeviceStatus.device_mac`; `from_config(path, device=...)` selection; tz default from host.
4. Updated `config.example.json`, README onboarding section, expanded `selftest_offline.py`.

---

## Phases (each proves something)

### Phase 0 — Foundations (offline, no BLE)
- `host_tz_offset()` (from E1: `datetime.now().astimezone().utcoffset()`).
- `parse_reply` extracts field-1 MAC → `DeviceStatus.device_mac` (formatted `AA:BB:...`).
- `BHyveXD.from_config(path, device=None)` selects by name or index; default tz = host.
- **Test:** offline — MAC parse from a synthetic reply; tz derive; multi-device select. Must stay green (target 35+/35+).
- **Proves:** the data plumbing works without hardware.

### Phase 1 — Cloud module (creds → devices + keys)
- Port `bhyve-local/bhyvexd/cloud.py` into `onboarding.cloud_fetch(email,password)` →
  `[{name, mac, network_key, stations}]`. Keep the browser User-Agent + `/meshes|/network_topologies` fallback.
- Clear, typed errors: `AuthError` (400/401/403), `RateLimited` (429), `MFARequired` (if seen), `CloudError`.
- **Test:** offline — response-parsing unit tests with recorded/mocked JSON (no live creds needed);
  error mapping. **Live (user-run):** a `--dry-run` that logs in and prints the device list (no config written).
- **Proves:** we can turn credentials into keys + MACs.

### Phase 2 — Address resolution / pairing (the proven primitive)
- `resolve_address(mac, network_key, *, platform)`:
  - **Linux/BlueZ:** return the MAC directly.
  - **macOS:** implement the **proven E6 flow** — scan (short), rank by RSSI, for the strongest N:
    fast-reject if no `fe32` GATT service; else minimal-arm + read MAC; match to `mac`. Return the UUID.
- Extract the arm+read-MAC helper so control code and pairing share it (no duplication).
- **Test:** live on Mac — resolve the timer's UUID from its MAC (this is exactly E6, already passing).
- **Proves:** the hard part works inside the product code, not just an experiment.

### Phase 3 — `cli.py login` (end-to-end onboarding)
- `login`: hidden-prompt email/password → `cloud_fetch` → for each non-bridge device
  `resolve_address` → `host_tz_offset` → `write_config("config.json")` (device list).
  On macOS, prompt "wake each timer (hold its button)" during resolution; retry-friendly.
- **Test:** live — from a config-less state, `login` then `status`/`start`/`stop` work on the real timer.
- **Checkpoint:** ⛳ full onboarding proven on a clean setup.

### Phase 4 — `find` + multi-device targeting
- `cli.py find [name]` — re-resolve a drifted macOS UUID for one device, update config, no re-login.
- `--device <name|idx>` on `status/start/stop/settime`; server picks device via query/path; UI device selector (thin).
- **Test:** live — rename/clear an address, `find` restores it; target a device by name.
- **Proves:** resilience to UUID drift + multi-device.

### Phase 5 — Polish, docs, regression
- README "Onboarding" section; `config.example.json` reflects auto-generation; scrub remaining hardcoded refs
  (grep for `-14400`, literal MAC/UUID, `network_key` outside config).
- Full offline suite green; live smoke of every CLI op; commit + push each phase.

---

## Risks & mitigations
| Risk | Mitigation | Status |
|---|---|---|
| macOS UUID drift over time | `cli.py find` re-resolves without re-login | designed |
| Discovery flakiness (connect fails) | probe strongest N only + `fe32` fast-reject + "hold button" retry (E6) | **proven** |
| Cloud rate-limit / MFA | typed errors, no retry-hammer, clear guidance | designed |
| Password leakage | hidden prompt; never persisted, never logged; only key+address saved | designed |
| Ambiguity: several B-Hyve timers in range | arm+read-MAC disambiguates each to its cloud MAC | **proven (E3)** |
| Cloud schema differences across accounts | keep `/meshes` + `/network_topologies` fallback + key-field list | ported |

## Tests / validation
- **Offline (automated, `selftest_offline.py`):** MAC parse, tz derive, multi-device select, cloud
  response-parse (mocked), error mapping. Target ≥ 35 checks, all green.
- **Live (user-assisted):** `login --dry-run` (device list), `login` (writes config), `find`, and
  `status/start/stop` on the resolved config — device-confirmed via read-back.
- Regression: existing 30/30 offline + the live CLI smoke must stay green each phase.

## First concrete action
**Phase 0:** in `bhyve_xd.py`, add `host_tz_offset()`, extend `parse_reply` to set
`DeviceStatus.device_mac` (field 1), and add `from_config(path, device=None)` selection
with host-tz default — then add offline tests for all three and confirm the suite is green.

## Notes
- Reuse proven code: `cloud.py` (Phase 1), the E6 pairing loop (Phase 2), the shared
  arm/read-back in `bhyve_xd.py`.
- Commit per phase; push to `evanscastonguay/bhyve-xd-ble`. Keep `config.json` git-ignored.
