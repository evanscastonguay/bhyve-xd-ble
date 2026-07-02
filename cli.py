#!/usr/bin/env python3
"""
bhyve-xd CLI — local BLE control of an Orbit B-Hyve XD hose timer.

Config: reads config.json (see config.example.json) for the device address and
network key. On Linux the address is the MAC (AA:BB:CC:DD:EE:FF). On macOS it is
the CoreBluetooth UUID (run `scan` to find it).

Every command connects, ARMS the device (required setup sequence), then acts.

Usage:
    python cli.py status                 # read + print device status (clock, watering, battery)
    python cli.py start 1 300            # start zone 1 for 300 seconds, confirm watering
    python cli.py stop                   # stop all watering, confirm idle
    python cli.py settime                # sync clock to now, confirm
    python cli.py selftest               # full set->read->start->read->stop->read (autonomous)
    python cli.py scan                   # list nearby BLE devices (find the address)
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from bhyve_xd import BHyveXD


def load_cfg():
    with open("config.json") as f:
        c = json.load(f)
    d = c["devices"][0]
    return d["address"], d["network_key"], int(d.get("tz_offset_sec", -14400))


async def cmd_status():
    addr, key, tz = load_cfg()
    dev = BHyveXD(addr, key, tz_offset_sec=tz)
    async with dev.session() as s:
        await s.arm()
        st = await s.read_status()
    print(f"clock={st.clock_str}  watering={st.is_watering}  run_state={st.run_state}"
          f"  seconds_remaining={st.seconds_remaining}")


async def cmd_start(zone: int, secs: int):
    addr, key, tz = load_cfg()
    dev = BHyveXD(addr, key, tz_offset_sec=tz)
    async with dev.session() as s:
        await s.arm()
        await s.start_zone(zone, secs)
        st = await s.read_status()
    print(f"START zone {zone} for {secs}s -> watering={st.is_watering} "
          f"secs={st.seconds_remaining}  {'OK' if st.is_watering else 'NOT CONFIRMED'}")


async def cmd_stop():
    addr, key, tz = load_cfg()
    dev = BHyveXD(addr, key, tz_offset_sec=tz)
    async with dev.session() as s:
        await s.arm()
        await s.stop()
        st = await s.read_status()
    print(f"STOP -> watering={st.is_watering}  {'OK (idle)' if not st.is_watering else 'STILL WATERING'}")


async def cmd_settime():
    addr, key, tz = load_cfg()
    dev = BHyveXD(addr, key, tz_offset_sec=tz)
    async with dev.session() as s:
        await s.arm()          # arm() already sets the clock to now
        st = await s.read_status()
    print(f"clock synced -> device clock={st.clock_str}")


async def cmd_selftest():
    """Autonomous end-to-end confirmation using the device's own replies."""
    addr, key, tz = load_cfg()
    dev = BHyveXD(addr, key, tz_offset_sec=tz)
    tzinfo = timezone(timedelta(seconds=tz))
    target = datetime.now(tzinfo).replace(hour=3, minute=33, second=0, microsecond=0)
    async with dev.session() as s:
        await s.arm()
        await s.set_clock(target)
        st = await s.read_status()
        hhmm = datetime.fromtimestamp(st.device_time, tzinfo).strftime("%H:%M") if st.device_time else "?"
        print(f"[1] set clock 03:33 -> read {hhmm}  {'PASS' if hhmm == '03:33' else 'FAIL'}")

        await s.start_zone(1, 300)
        st = await s.read_status()
        print(f"[2] start zone 1   -> watering={st.is_watering} secs={st.seconds_remaining}"
              f"  {'PASS' if st.is_watering else 'FAIL'}")

        await s.stop()
        st = await s.read_status()
        print(f"[3] stop           -> watering={st.is_watering}  {'PASS' if not st.is_watering else 'FAIL'}")


async def cmd_scan():
    from bleak import BleakScanner
    print("scanning 12s...")
    devs = await BleakScanner.discover(timeout=12, return_adv=True)
    for addr, (d, adv) in devs.items():
        svcs = [u.lower() for u in (adv.service_uuids or [])]
        tag = "  <== B-HYVE (fe32)" if any("fe32" in u for u in svcs) else ""
        print(f"  {d.name or '(no name)':24} rssi={adv.rssi:>4}  {addr}{tag}")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); return
    cmd = a[0]
    if cmd == "status":
        asyncio.run(cmd_status())
    elif cmd == "start":
        asyncio.run(cmd_start(int(a[1]), int(a[2])))
    elif cmd == "stop":
        asyncio.run(cmd_stop())
    elif cmd == "settime":
        asyncio.run(cmd_settime())
    elif cmd == "selftest":
        asyncio.run(cmd_selftest())
    elif cmd == "scan":
        asyncio.run(cmd_scan())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
