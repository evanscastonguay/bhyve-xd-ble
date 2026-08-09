# Plan — single-page 8-zone dashboard (2 timers, one active zone)

Goal: one dead-simple page for **fast, one-click** watering across **2 B-Hyve XD timers (8 zones)** —
no manual connect/login/sync, no waiting between buttons. Replaces the "open the app, connect ~30s,
turn on ~30s, repeat per zone" pain.

## Confirmed spec (from /s2-clarify)
- **8 buttons**: zones 1–4 = *zone1-4 timer*, zones 5–8 = *zone5-8 timer*.
- **Exactly one zone ON globally** (pressure only supports one): clicking a zone **stops whatever is
  running on either timer**, then starts the new one. **STOP** turns everything off on both.
- **Single click = start 60 min** (device auto-stops). Duration adjustable via a second click, but the
  common path is one click.
- **Responsive/optimistic UI**: click → button shows "starting…" → ✓/✗; no manual connect; clicks
  queue. Each action is still ~8–12 s over BLE (cross-timer switch ≈ 2 hops), never blocking.
- Runs on `bhyve-linux`; both timers **confirmed in BLE range** (weak-ish −85 / −74 — may need the
  box closer or an ESP32 BT proxy later). Timer B connects at its **advertised** address (differs from
  its cloud MAC) — addresses live only in the box's `config.json`, never in this repo.

## Key facts to design around
- The server already: holds a single `_ble_lock` (BLE is one-op-at-a-time), caches last status per
  device, confirms every action by read-back, supports `--device <index>`.
- New need: **global** single-active-zone across *two* devices, and a combined 8-zone view.

## Scope
- **In:** add timer B to the box config (2-device); a server "exclusive start" (stop-current-then-start,
  confirmed) + "stop all" + a combined `/api/zones` view tracking the one global active zone; a new
  minimal `zones.html` dashboard (optimistic, queued, polling); deploy + live verify; docs.
- **Out:** multi-zone concurrency (hardware can't); persistent/warm BLE connections (possible later if
  ~10 s still feels slow); scheduling changes; HA/MQTT changes; a native mobile app.

## Phases (each testable)

### P0 — 2-device config + lock the mapping ⛳ (on the box; one brief valve test)
- Add timer B to `~/bhyve/shared/config.json` (address = its advertised BLE address, shared key,
  `name: "zone5-8 timer"`); rename timer A `"zone1-4 timer"`. Keep the real config on the box only.
- **Control-test each** (`cli.py start <z> 30 --device N` → confirm watering → `stop`) to (a) prove
  both are controllable with the key and (b) bind which physical timer is zones 1–4 vs 5–8. Briefly
  opens a valve (~seconds).
- **Gate:** both timers start+stop from the box with read-back confirmation; mapping recorded.

### P1 — Server: global exclusive-start + stop-all + `/api/zones` (TDD, fake devices)
- Track `_active_zone = (device_index, zone) | None`.
- `POST /api/zones/start {device, zone, minutes=60}`: under the BLE lock — if a *different* zone is
  active, **stop that device and confirm idle first**, then `start_zone`, confirm watering, set
  `_active_zone`. (≤2 BLE hops; never two valves open — old stop is confirmed before new start.)
- `POST /api/zones/stop-all`: stop **both** devices (guaranteed all-off), clear `_active_zone`.
- `GET /api/zones`: the 8-zone view — `[{index, zone, label, active, seconds_remaining}]` built from
  the status cache + `_active_zone` (instant; no forced BLE).
- **TDD** against two fake timers: start on B while A active → A stopped then B started; stop-all →
  both stopped; `/api/zones` shape + single active flag. **Gate:** suite green.

### P2 — `zones.html` single-page dashboard (optimistic, queued)
- One page: 8 big zone buttons (grouped 1–4 / 5–8) + a prominent **STOP ALL**; the active zone lit,
  others off; a small duration control (defaults 60, used on click; change = second tap).
- Click → optimistically light the new zone + clear others → `POST /api/zones/start` → ✓/✗ from the
  confirmed response; reconcile against a light `GET /api/zones` poll. No manual connect; a click while
  one is in flight **queues** (never blocks). Reuse the fast-load/optimistic patterns already in the app.
- Serve at `/` (primary quick-access); keep the full existing UI at `/advanced`.
- **Gate:** JS parses; page renders 8 zones from `/api/zones`; single-active reflected.

### P3 — Deploy + live end-to-end (on the box)
- `./deploy/deploy.sh`; then live: click zone 1 → valve opens; click zone 6 → zone 1 stops, zone 6
  opens; STOP → all off — each confirmed by read-back. **Gate:** the switch-across-timers + all-off work
  live; weak-signal retries tolerated.

### P4 — Docs
- README: a short "Zone dashboard" note; mapping/verification tip. Mark this plan done.

## Risks & mitigations
- **Weak BLE (−85/−74)** → connect retries (bleak-retry-connector); surface failures as ✗ in the UI;
  if painful, move the box or add an ESP32 BT proxy (note in docs).
- **Cross-timer switch latency (~20 s)** → optimistic UI + confirmed read-back; the button shows
  "starting…" the whole time; never blocks other clicks.
- **Two valves open (pressure)** → always **stop-old-confirmed before start-new**; STOP hits both devices.
- **State drift** if the Orbit app is used concurrently → phone Bluetooth must stay off (single BLE
  connection anyway); `/api/zones` poll + a manual refresh reconcile.
- **Wrong zone↔timer mapping** → locked by the P0 control test, not assumed.

## Tests / validation
- P1: fake-two-timer unit tests (exclusive start, stop-all, view). P0/P3: live control on the box.

## Checkpoints
After **P0** (both controllable + mapping locked) · after **P1** (logic green) · after **P3** (live).

## First concrete action
**P0:** on the box, add timer B to `config.json` (advertised address + shared key, `zone5-8 timer`),
rename A `zone1-4 timer`, and run a brief `cli.py start/stop` on each to confirm control and bind the
zones-1–4/5–8 mapping.
