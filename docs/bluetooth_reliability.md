# Bluetooth reliability (the box ↔ your timers)

The server controls timers with **`bleak`/BlueZ on the box's own Bluetooth adapter**. Reliability is
almost entirely about **signal strength (RSSI)**: roughly, ≥ −70 dBm is solid, −70…−80 gets occasional
connect-retries, and past ~−82 dBm ops start failing (empty read-backs / "device disappeared"). One
weak timer in a deployment shows up as "that zone doesn't work / needs several taps."

Check RSSI from the box:

```bash
python - <<'PY'
import asyncio
from bleak import BleakScanner
async def m():
    for a,(d,adv) in (await BleakScanner.discover(timeout=15, return_adv=True)).items():
        if a.upper().startswith("44:67:55"): print(a, adv.rssi)
asyncio.run(m())
PY
```

## Fixes, cheapest first
1. **Reposition** — move the box (a Pi is easy to relocate) or the weak timer so both are within good
   range. Line-of-sight and fewer walls matter more than distance.
2. **USB extension cable** — put the box anywhere, but run its Bluetooth adapter on a short USB
   extension to a better spot.
3. **External USB Bluetooth adapter with an antenna** — a BT 4/5 USB dongle with an external antenna
   has far better sensitivity than a built-in/interal one; plug it into the box and disable the internal.
4. **A second small host near the far timer** — last resort: run another instance of the server on a
   cheap Pi/host next to the distant timer (each host controls the timer it can reach).

## Note on ESPHome Bluetooth proxies
ESP32 **ESPHome Bluetooth proxies relay BLE to Home Assistant's own Bluetooth stack** — they do **not**
feed a standalone `bleak`/BlueZ app like this server, so they won't help here. (They'd only be relevant
if you re-architected onto HA-native Bluetooth, which this project deliberately does not use.)
