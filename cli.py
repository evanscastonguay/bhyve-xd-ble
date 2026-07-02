#!/usr/bin/env python3
"""
bhyve-xd CLI — local BLE control of an Orbit B-Hyve XD hose timer.

Thin wrapper over bhyve_xd.BHyveXD — the same shared logic the REST server uses.
Reads config.json (see config.example.json). Every command arms the device and
reads status back for confirmation.

    python cli.py status              # read clock / watering / battery state
    python cli.py settime             # sync clock to now
    python cli.py start 1 300         # start zone 1 for 300s (confirms watering)
    python cli.py stop                # stop ALL zones
    python cli.py stop 1              # stop zone 1 only
    python cli.py selftest            # autonomous set->read->start->read->stop->read
    python cli.py scan                # list nearby BLE devices (find the address)
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from bhyve_xd import BHyveXD


def _show(prefix, st):
    print(f"{prefix}clock={st.clock_str}  watering={st.is_watering}  "
          f"run_state={st.run_state}  seconds_remaining={st.seconds_remaining}")


async def cmd_status():
    _show("", await BHyveXD.from_config().status())


async def cmd_settime():
    _show("clock synced -> ", await BHyveXD.from_config().sync_clock())


async def cmd_start(zone, secs):
    st = await BHyveXD.from_config().start(zone, secs)
    _show(f"START zone {zone} {secs}s -> {'OK' if st.is_watering else 'NOT CONFIRMED'} | ", st)


async def cmd_stop(zone=None):
    st = await BHyveXD.from_config().stop(zone)
    what = "ALL" if zone is None else f"zone {zone}"
    _show(f"STOP {what} -> {'OK (idle)' if not st.is_watering else 'STILL WATERING'} | ", st)


async def cmd_selftest():
    """Autonomous end-to-end confirmation via the device's own replies."""
    dev = BHyveXD.from_config()
    tzinfo = timezone(timedelta(seconds=dev.tz_offset_sec))
    target = datetime.now(tzinfo).replace(hour=3, minute=33, second=0, microsecond=0)
    async with dev.session() as s:
        await s.arm()
        await s.set_clock(target)
        st = await s.read_status()
        hhmm = (datetime.fromtimestamp(st.device_time, tzinfo).strftime("%H:%M")
                if st.device_time else "?")
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
    for addr, (d, adv) in (await BleakScanner.discover(timeout=12, return_adv=True)).items():
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
    elif cmd == "settime":
        asyncio.run(cmd_settime())
    elif cmd == "start":
        asyncio.run(cmd_start(int(a[1]), int(a[2])))
    elif cmd == "stop":
        asyncio.run(cmd_stop(int(a[1]) if len(a) > 1 else None))
    elif cmd == "selftest":
        asyncio.run(cmd_selftest())
    elif cmd == "scan":
        asyncio.run(cmd_scan())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
