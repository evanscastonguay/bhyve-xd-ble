# Verification log

## HT34A-0001, firmware 0107 — Linux/BlueZ (ThinkPad)

**Offline:** `selftest_offline.py` — 11/11 byte-equivalence checks PASS
(vs real captured app frames: start, stop, set_time, time_string, CRC, cipher, trailer).

**Live (clean `cli.py selftest`), device-confirmed via its own replies:**
```
[1] set clock 03:33 -> read 03:33   PASS
[2] start zone 1    -> watering=True, 300s remaining   PASS
[3] stop            -> watering=False (idle)           PASS
```

**Reproducibility:** 3/3 consecutive autonomous runs all PASS, with the official
app untouched — confirming it is NOT dependent on app-priming.

**Why it works:** the device only honors a command sent after the full 9-message
arming sequence in the same connection (see README). Isolated commands are ignored.

## REST API + Web UI — live end-to-end (2026-07-01)
Server runs on the ThinkPad as a systemd service (bhyve.service). Verified
through the full stack (web UI -> REST -> BLE -> device):
```
GET  /api/status           -> {"clock":"...","is_watering":false}
POST /api/zones/1/start {minutes:2}
     -> {"confirmed_watering":true,"run_state":4,"seconds_remaining":120}
POST /api/stop
     -> {"confirmed_idle":true,"run_state":1}
```
Web UI opened at http://<host>:8000/ — 4 valves with ON buttons + Stop all + live status.

## Per-zone stop — TDD + live (2026-07-01)
Added msg_stop_zone(station) = manual watering at 0s. Offline: 20/20 byte checks pass.
Live via API: start zone 1 -> watering 180s; POST /api/zones/1/stop -> confirmed idle.
Web UI now has per-valve Turn ON / Turn OFF plus a global Stop ALL.
