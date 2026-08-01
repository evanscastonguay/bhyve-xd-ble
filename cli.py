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
    python cli.py login [email]       # cloud login -> print devices + keys (--dry-run)
    python cli.py register [email]    # discover + save a NEW timer to config.json
                                      #   --name NAME  --device-mac MAC  --show-key

With multiple timers in config.json, pick one with --device (name or 0-based index);
the default is the first device:
    python cli.py status --device "New Timer"
    python cli.py start 1 300 --device 1
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from bhyve_xd import BHyveXD


def _show(prefix, st):
    print(f"{prefix}clock={st.clock_str}  watering={st.is_watering}  "
          f"run_state={st.run_state}  seconds_remaining={st.seconds_remaining}")


async def cmd_status(device=None):
    _show("", await BHyveXD.from_config(device=device).status())


async def cmd_settime(device=None):
    _show("clock synced -> ", await BHyveXD.from_config(device=device).sync_clock())


async def cmd_start(zone, secs, device=None):
    st = await BHyveXD.from_config(device=device).start(zone, secs)
    _show(f"START zone {zone} {secs}s -> {'OK' if st.is_watering else 'NOT CONFIRMED'} | ", st)


async def cmd_stop(zone=None, device=None):
    st = await BHyveXD.from_config(device=device).stop(zone)
    what = "ALL" if zone is None else f"zone {zone}"
    _show(f"STOP {what} -> {'OK (idle)' if not st.is_watering else 'STILL WATERING'} | ", st)


async def cmd_selftest(device=None):
    """Autonomous end-to-end confirmation via the device's own replies."""
    dev = BHyveXD.from_config(device=device)
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


async def cmd_login(email=None, *, show_key=False):
    """Cloud login -> print the account's devices with their BLE network keys.

    Dry run only (Phase 1): nothing is written. The password is read from a hidden
    prompt and never persisted or logged. Address resolution + config writing land
    in later phases (cli.py login without --dry-run).
    """
    import getpass

    from onboarding import CloudError, cloud_fetch

    if not email:
        email = input("Orbit account email: ").strip()
    password = getpass.getpass("Orbit password (hidden): ")
    try:
        devices = await cloud_fetch(email, password)
    except CloudError as e:
        print(f"login failed: {e}")
        return
    if not devices:
        print("logged in, but no controllable B-Hyve devices found on this account.")
        return
    print(f"\nfound {len(devices)} device(s):")
    for i, d in enumerate(devices):
        key = d.get("network_key")
        if not key:
            keyview = "(no key!)"
        elif show_key:
            keyview = key
        else:
            keyview = f"****{key[-4:]} ({len(key)} hex chars) ✓"
        print(f"  [{i}] {d['name']}  mac={d.get('mac')}  stations={d.get('stations')}  "
              f"hw={d.get('hardware')} fw={d.get('firmware')}  key={keyview}")
    print("\n(dry run — no config written. Re-run with --show-key to reveal the full "
          "key. Address resolution + config writing arrive in a later phase.)")


async def cmd_register(email=None, *, name=None, want_mac=None, show_key=False,
                       path="config.json", ask_prompt=True):
    """Discover a NEW timer and save it to config.json — one command, no hand-editing.

    Key: reuse the account key already in config.json (adding another timer on the
    same account needs NO cloud login); only log in when there's no key yet (or an
    email is given). Then: prompt to release the phone -> catch the timer's live
    advertisement -> read its MAC + status back -> write config -> confirm.
    """
    import getpass

    import onboarding

    key = None if email else onboarding.key_from_existing_config(path)
    stations = 4
    if key:
        print(f"reusing the account key from {path} (...{key[-4:]}) — no cloud login needed.")
    else:
        if not email:
            email = input("Orbit account email: ").strip()
        password = getpass.getpass("Orbit password (hidden): ")
        try:
            devices = await onboarding.cloud_fetch(email, password)
        except onboarding.CloudError as e:
            print(f"cloud login failed: {e}")
            return
        controllable = [d for d in devices if d.get("network_key")]
        if not controllable:
            print("no controllable devices with a BLE key on this account.")
            return
        if want_mac:
            chosen = next((d for d in controllable
                           if (d.get("mac") or "").upper() == want_mac.upper()), None)
            if not chosen:
                print(f"no device with MAC {want_mac} on the account.")
                return
        elif len(controllable) == 1:
            chosen = controllable[0]
        else:
            print("multiple devices on the account — re-run with --device-mac <MAC>:")
            for d in controllable:
                print(f"  {d.get('mac')}  {d.get('name')}")
            return
        key, want_mac = chosen["network_key"], chosen.get("mac")
        stations = int(chosen.get("stations") or 4)
        name = name or chosen.get("name")

    if ask_prompt:
        input("\nTurn your phone's Bluetooth OFF so it isn't holding the timer, keep the "
              "timer close to the Mac, then press Enter to search... ")
    try:
        address, mac, st = await onboarding.catch_device(key, want_mac=want_mac)
    except onboarding.ResolveError as e:
        print(f"\nregister failed: {e}")
        return

    device = {"name": name or "B-Hyve XD", "address": address,
              "network_key": key, "mac": mac, "stations": stations}
    onboarding.write_config(path, device)
    keyview = key if show_key else f"****{key[-4:]}"
    print(f"\n✓ registered '{device['name']}'  mac={mac}  address={address}  key={keyview}")
    print(f"  status: clock={st.clock_str}  watering={st.is_watering}")
    print(f"  saved to {path}. Control it with:  cli.py status --device \"{device['name']}\"")


def _parse_register(args):
    """Parse register args: optional email positional + --name/--device-mac/--show-key.
    Returns (email, name, want_mac, show_key)."""
    email = name = want = None
    show = False
    i = 0
    while i < len(args):
        x = args[i]
        if x == "--name":
            name = args[i + 1] if i + 1 < len(args) else None; i += 2
        elif x.startswith("--name="):
            name = x.split("=", 1)[1]; i += 1
        elif x == "--device-mac":
            want = args[i + 1] if i + 1 < len(args) else None; i += 2
        elif x.startswith("--device-mac="):
            want = x.split("=", 1)[1]; i += 1
        elif x == "--show-key":
            show = True; i += 1
        elif x.startswith("--"):
            i += 1
        else:
            if email is None:
                email = x
            i += 1
    return email, name, want, show


async def cmd_scan():
    from bleak import BleakScanner
    print("scanning 12s...")
    for addr, (d, adv) in (await BleakScanner.discover(timeout=12, return_adv=True)).items():
        svcs = [u.lower() for u in (adv.service_uuids or [])]
        tag = "  <== B-HYVE (fe32)" if any("fe32" in u for u in svcs) else ""
        print(f"  {d.name or '(no name)':24} rssi={adv.rssi:>4}  {addr}{tag}")


def _extract_device(argv):
    """Pull an optional `--device <name|idx>` (or `--device=...`) out of argv.
    Returns (device, remaining_argv). A purely-numeric value becomes an int index."""
    device, rest, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--device":
            device = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
        elif a.startswith("--device="):
            device = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    if isinstance(device, str) and device.lstrip("-").isdigit():
        device = int(device)
    return device, rest


def main():
    device, a = _extract_device(sys.argv[1:])
    if not a:
        print(__doc__); return
    cmd = a[0]
    if cmd == "status":
        asyncio.run(cmd_status(device=device))
    elif cmd == "settime":
        asyncio.run(cmd_settime(device=device))
    elif cmd == "start":
        asyncio.run(cmd_start(int(a[1]), int(a[2]), device=device))
    elif cmd == "stop":
        asyncio.run(cmd_stop(int(a[1]) if len(a) > 1 else None, device=device))
    elif cmd == "selftest":
        asyncio.run(cmd_selftest(device=device))
    elif cmd == "scan":
        asyncio.run(cmd_scan())
    elif cmd == "login":
        rest = a[1:]
        show_key = "--show-key" in rest
        pos = [x for x in rest if not x.startswith("--")]   # --dry-run is the default
        asyncio.run(cmd_login(pos[0] if pos else None, show_key=show_key))
    elif cmd == "register":
        email, name, want_mac, show_key = _parse_register(a[1:])
        asyncio.run(cmd_register(email, name=name, want_mac=want_mac, show_key=show_key))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
