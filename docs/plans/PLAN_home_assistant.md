# Scope — Home Assistant integration

## Problem (one sentence)
The project is standalone (own Web UI/API); the biggest ecosystem gap vs. the popular Orbit projects
is that valves aren't exposed to **Home Assistant** — so users can't control/automate the timer
alongside the rest of their smart home, **without the cloud**.

## The hard constraint that picks the design
A B-Hyve accepts **one BLE connection at a time**, and our server already owns it (with the status
cache + scheduler + control lock). Therefore the integration must go **through the server**, not talk
BLE itself — anything that opens its own BLE link would fight the server for the single connection.
That single fact rules out the "obvious" native-BLE HA integration running next to our server.

## Options

### A) MQTT bridge from the server + HA MQTT Discovery ⭐ (recommended)
The server keeps **exclusive BLE ownership** and mirrors each timer onto MQTT; HA auto-creates
entities via MQTT Discovery. HA never touches Bluetooth.
- **Pros:** reuses everything we built (status cache, scheduler, control-under-lock); **no BLE
  conflict**; real HA entities + device registry via Discovery; local/no-cloud; testable with a fake
  broker; ships via `deploy.sh`.
- **Cons:** needs an **MQTT broker** (most HA users run Mosquitto); adds one Python dep; MQTT creds
  live in `config.json`.

### B) Native HA custom component over BLE (+ ESPHome Bluetooth proxy)
Reimplement control inside a HACS integration on HA's Bluetooth stack; an ESP32 BT proxy solves range.
- **Pros:** the idiomatic HA experience (Bluetooth auto-discovery, BT proxies for range, HACS distro).
- **Cons:** **reimplements the protocol** in HA's framework; **HA must own the radio**, so you'd drop
  the standalone server/CLI (or never run them together); most code + ongoing HA-API maintenance;
  hardest to test. Only worth it if going *all-in* on HA and retiring the standalone server.

### C) HA REST / `command_line` against the existing API
Point HA's RESTful sensor/switch at `/api/status` + `/api/zones/...`.
- **Pros:** ~an hour, zero new server code, pure HA YAML.
- **Cons:** no device/entity model, no discovery, polling only, clunky — a stopgap, not a product.

## Recommendation
**Build A** (MQTT bridge). It's the only option that keeps the single-BLE-owner model intact while
giving first-class HA entities, and it reuses the whole existing stack. Offer **C** as a documented
same-day stopgap. Treat **B** as a future path only if the goal becomes "HA-native, no standalone
server."

## Entity model (option A) — one HA *device* per timer
- **`switch` per valve** — `on` → `start_zone(z, <duration>)`, `off` → `stop`. (Simplest, broadest
  HA compatibility. Alternative: the newer MQTT **`valve`** platform — revisit in P3.)
- **`number` per valve** (optional) — run-time minutes for the next ON (default from config).
- **`binary_sensor` watering** + **`sensor`** active zone / seconds-remaining / battery / clock.
- **`switch` "Automatic"** — maps to `PUT /api/scheduling {enabled}`; **`sensor`** next-run / last-run.
- **Availability** via MQTT LWT (server online/offline). Device: name, model `HT34A`, sw =
  `/api/version`.

## Data flow
- **State out:** publish retained Discovery configs on startup; publish state after **every** BLE op
  (the server already produces a confirmed `DeviceStatus`) and on the scheduler's fires, plus a
  periodic refresh. Reuse the status cache.
- **Commands in:** subscribe to command topics → call the **existing** control functions under the
  BLE lock → publish the **confirmed** read-back state (never optimistic).

## Config (opt-in — no behavior change if absent)
`config.json` gains an optional `mqtt` block: `{host, port, username, password, base_topic,
discovery_prefix}`. No block → MQTT disabled.

## Phases (each testable, hardware-free)
- **P0 — this scope + entity model decision.** ✅ DONE
- **P1 — publish:** ✅ DONE — `mqtt_bridge.py` (lazy `aiomqtt`) started in the lifespan (opt-in);
  Discovery + LWT availability + confirmed state via `_run`. TDD against a fake broker.
- **P2 — control:** ✅ DONE — wildcard command subscription → `_mqtt_command` → start/stop under
  `_ble_lock`; republishes confirmed state.
- **P3 — sensors + schedule:** ✅ DONE — watering / active-zone / seconds-remaining sensors + the
  global **Automatic** switch (toggles `host_scheduling`), seeded at startup. (Battery not in
  `to_dict`, skipped; next/last-run sensors deferred.)
- **P4 — docs + ship:** ✅ DONE (docs) — README "Home Assistant (MQTT)" section (broker prereq,
  `mqtt` config block, entities, LWT, security) + the **C** stopgap. **⏳ Live verification in real
  HA pending an MQTT broker** (deploy the box's `config.json` `mqtt` block + `deploy.sh`, confirm
  entities appear and toggle valves). `default_minutes` added for the valve ON duration.
- **(Future) B** — native HACS BLE integration, only if retiring the standalone server.

## Risks & mitigations
- **Needs an MQTT broker** → state as a prerequisite; degrade cleanly (disabled if no `mqtt` config).
- **Single BLE link** → *preserved* — HA never touches BLE; the server stays sole owner (A's whole point).
- **Restart resilience** → retained Discovery configs + LWT availability so HA recovers state.
- **Optimistic state** → only publish after confirmed read-back (same rigor as the Web UI/API).
- **New dependency + MQTT creds** → one lib in `requirements.txt`; creds in the already-`chmod 600`
  `config.json`; keep the broker on the trusted LAN (same posture as the no-auth server).

## Effort
A/P1–P3: a few focused sessions (bounded, TDD, reuses the stack). C stopgap: ~1 short session.

## First concrete action
**P1:** add an opt-in `mqtt` config block + a `mqtt_bridge` module started from the server lifespan
that connects to the broker, publishes MQTT Discovery for one timer's valve switches, and publishes
current status — TDD against a fake broker. No BLE/behavior change when `mqtt` is absent.
