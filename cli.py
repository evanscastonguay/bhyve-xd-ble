#!/usr/bin/env python3
"""
bhyve-xd CLI — local BLE control + onboarding for an Orbit B-Hyve XD hose timer.

Thin wrapper over bhyve_xd.BHyveXD and onboarding.py. Every control command arms
the device and reads status back for confirmation. Add --device <name|index> to
target a specific timer when the config has several.

  Setup (once, needs your Orbit account):
    python cli.py login               # email+password -> writes config.json
    python cli.py login --dry-run     # just list account devices, write nothing
    python cli.py find [name]         # re-resolve a drifted macOS address (no re-login)

  Control:
    python cli.py status              # clock / watering / battery
    python cli.py settime             # sync clock to now
    python cli.py start 1 300         # start zone 1 for 300s (confirms watering)
    python cli.py stop                # stop ALL zones
    python cli.py stop 1              # stop zone 1 only
    python cli.py selftest            # autonomous set->read->start->read->stop->read
    python cli.py scan                # list nearby BLE devices

  Target a specific timer:  python cli.py --device Back start 1 300
"""
import asyncio
import getpass
import json
import sys
from datetime import datetime, timedelta, timezone

import onboarding
from bhyve_xd import BHyveXD


def _extract_device(args):
    """Pull `--device <val>` out of args. Returns (device, rest). device is an
    int when the value is numeric (an index), else a str (a name), else None."""
    device, rest, i = None, [], 0
    while i < len(args):
        if args[i] == "--device":
            if i + 1 < len(args):
                v = args[i + 1]
                device = int(v) if v.lstrip("-").isdigit() else v
                i += 2
            else:
                i += 1   # trailing --device with no value: drop it
        else:
            rest.append(args[i]); i += 1
    return device, rest


def _dev(device):
    return BHyveXD.from_config("config.json", device=device)


def _show(prefix, st):
    print(f"{prefix}clock={st.clock_str}  watering={st.is_watering}  "
          f"run_state={st.run_state}  seconds_remaining={st.seconds_remaining}")


# --- control commands -------------------------------------------------------- #
async def cmd_status(device):
    _show("", await _dev(device).status())


async def cmd_settime(device):
    _show("clock synced -> ", await _dev(device).sync_clock())


async def cmd_start(device, zone, secs):
    st = await _dev(device).start(zone, secs)
    _show(f"START zone {zone} {secs}s -> {'OK' if st.is_watering else 'NOT CONFIRMED'} | ", st)


async def cmd_stop(device, zone=None):
    st = await _dev(device).stop(zone)
    what = "ALL" if zone is None else f"zone {zone}"
    _show(f"STOP {what} -> {'OK (idle)' if not st.is_watering else 'STILL WATERING'} | ", st)


async def cmd_selftest(device):
    dev = _dev(device)
    tzinfo = timezone(timedelta(seconds=dev.tz_offset_sec))
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
    for addr, (d, adv) in (await BleakScanner.discover(timeout=12, return_adv=True)).items():
        svcs = [u.lower() for u in (adv.service_uuids or [])]
        tag = "  <== B-HYVE (fe32)" if any("fe32" in u for u in svcs) else ""
        print(f"  {d.name or '(no name)':24} rssi={adv.rssi:>4}  {addr}{tag}")


# --- onboarding commands ----------------------------------------------------- #
async def cmd_login(dry_run):
    email = input("Orbit email: ").strip()
    password = getpass.getpass("Orbit password (hidden): ").strip()
    print("\nlogging in and fetching devices...")
    devices = await onboarding.cloud_fetch(email, password)
    if not devices:
        print("no timers found on this account."); return
    print(f"found {len(devices)} device(s):")
    for d in devices:
        print(f"  - {d['name']}: hw={d['hardware']} fw={d['firmware']} stations={d['stations']} "
              f"mac={d['mac']} key={'yes' if d['network_key'] else 'NO'}")
    if dry_run:
        print("\n--dry-run: no config written."); return
    plat = onboarding.current_platform()
    resolved = []
    for d in devices:
        if not d["network_key"] or not d["mac"]:
            print(f"  skip {d['name']} (missing key/mac)"); continue
        print(f"\nresolving BLE address for {d['name']} ({plat})...", flush=True)
        if plat == "macos":
            print("  >> WAKE this timer (press/hold its button) now...")
        addr = await onboarding.resolve_address(d["mac"], d["network_key"])
        print(f"  address = {addr}")
        resolved.append({**d, "address": addr})
    onboarding.write_config(resolved, "config.json")
    print(f"\nwrote config.json with {len(resolved)} device(s). Try: python cli.py status")


async def cmd_find(name):
    """Re-resolve the BLE address for one configured device (macOS UUID drift)."""
    cfg = json.load(open("config.json"))
    devs = cfg["devices"]
    target = next((d for d in devs if d.get("name") == name), None) if name else devs[0]
    if target is None:
        print(f"no device named {name!r} in config"); return
    if not target.get("mac"):
        print("this device has no stored MAC to match against — re-run `login`"); return
    plat = onboarding.current_platform()
    if plat == "macos":
        print(">> WAKE the timer (press/hold its button) now...")
    addr = await onboarding.resolve_address(target["mac"], target["network_key"])
    old = target.get("address")
    target["address"] = addr
    json.dump(cfg, open("config.json", "w"), indent=2)
    print(f"{target['name']}: address {old} -> {addr}")


def main():
    device, a = _extract_device(sys.argv[1:])
    if not a:
        print(__doc__); return
    cmd = a[0]
    if cmd == "status":
        asyncio.run(cmd_status(device))
    elif cmd == "settime":
        asyncio.run(cmd_settime(device))
    elif cmd == "start":
        asyncio.run(cmd_start(device, int(a[1]), int(a[2])))
    elif cmd == "stop":
        asyncio.run(cmd_stop(device, int(a[1]) if len(a) > 1 else None))
    elif cmd == "selftest":
        asyncio.run(cmd_selftest(device))
    elif cmd == "scan":
        asyncio.run(cmd_scan())
    elif cmd == "login":
        asyncio.run(cmd_login("--dry-run" in a))
    elif cmd == "find":
        asyncio.run(cmd_find(a[1] if len(a) > 1 else None))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
