# B-Hyve XD (local BLE) — Home Assistant add-on

> **Beta:** validated by structure + the underlying Docker image's CI, but not yet confirmed on a
> live HA Supervisor. Feedback/issues welcome.

Runs the bhyve-xd-ble server beside Home Assistant. It talks to your timer over **Bluetooth** (no
Orbit cloud, no Wi-Fi hub) and mirrors it into HA over **MQTT Discovery** — valves as switches, plus
watering/seconds/active-zone sensors and an "Automatic schedule" switch.

## Requirements
- The HA host must be **within Bluetooth range** of the timer, with a working Bluetooth adapter.
- An **MQTT broker** (e.g. the Mosquitto add-on) with HA's MQTT integration configured.
- Your timer's **MAC address** and **network key** (get them with `cli.py register` from a laptop once).
- The published image must be **public** on GHCR for the Supervisor to pull it.

## Options
| Option | Meaning |
|---|---|
| `address` | timer MAC (Linux form, e.g. `AA:BB:CC:DD:EE:01`) |
| `network_key` | 16-byte hex account key |
| `tz_offset_sec` | device timezone offset (e.g. `-14400`) |
| `stations` | number of valves (4) |
| `host_scheduling` | run schedules automatically |
| `mqtt_*` | broker host/port/user/pass + default run minutes |

Open the web UI at `http://<ha-host>:8000/` to author schedules and control valves directly.
