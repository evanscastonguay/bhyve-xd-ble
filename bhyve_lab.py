#!/usr/bin/env python3
"""
bhyve_lab.py — interactive onboarding lab for a NEW B-Hyve timer on macOS.

WHY THIS EXISTS
  The new timer (MAC AA:BB:CC:DD:EE:01) advertises with a ROTATING/private BLE
  address and only re-advertises for a few seconds after the bonded iPhone lets
  go of it. So we can't pre-target a fixed address; we must CATCH whatever address
  it advertises the instant the phone releases it, then drive the protocol on that
  live connection. This tool lets YOU control the timing while it logs everything.

  The catch connects ONCE to the live advertisement and hands that connected
  client to the proven bhyve_xd session (BHyveXD.session(client=...)) — so this
  tool runs the SAME arm/command/read-back code the CLI and server use, not a copy.

HOW TO RUN (in your own Terminal, from the project directory)
    cd path/to/bhyve-xd-ble
    ./venv/bin/python bhyve_lab.py

  Everything you see is ALSO written, timestamped, to  bhyve_lab.log
  (share that file — or just leave it — and Claude can read the full trace).

MENU
  1) Ambient scan        — list nearby BLE devices (baseline; no timer needed)
  2) Capture the timer   — guided phone-release flow -> connect -> arm -> control
  3) Where is the log    — print the log file path
  q) Quit

Nothing here resets the device or changes the proven bhyve_xd control logic.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner

from bhyve_xd import BHyveXD, NotABHyveError, host_tz_offset

WANT_MAC = "AA:BB:CC:DD:EE:01"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bhyve_lab.log")

BASELINE_S = 7.0        # learn ambient devices before reacting
NEAR_RSSI = -80         # only react to reasonably-close new devices
CAPTURE_TIMEOUT = 90.0  # give up catching the timer after this long


# --------------------------------------------------------------------------- #
# Logging: every line goes to the screen AND to bhyve_lab.log with a timestamp.
# --------------------------------------------------------------------------- #
class Log:
    def __init__(self, path: str):
        self._f = open(path, "a", buffering=1)
        self.line(f"\n===== bhyve_lab session started {datetime.now().isoformat(timespec='seconds')} =====")

    def line(self, msg: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        text = f"[{ts}] {msg}" if msg else ""
        print(msg)
        self._f.write(text + "\n")

    def raw(self, msg: str):
        print(msg)
        self._f.write(msg + "\n")


async def ask(prompt: str) -> str:
    """Non-blocking input() so the BLE event loop keeps running (keepalives etc.)."""
    return (await asyncio.to_thread(input, prompt)).strip()


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def _load_key_hex(log: Log) -> str | None:
    try:
        with open("config.json") as f:
            hexkey = json.load(f)["devices"][0]["network_key"]
        log.line(f"using account key from config.json (...{hexkey[-4:]})")
        return hexkey
    except Exception as e:
        log.line(f"could not read config.json network_key: {e}")
        return None


async def ambient_scan(log: Log):
    log.line("ambient scan 10s — devices nearby (helps us learn what's NOT the timer):")
    devs = await BleakScanner.discover(timeout=10, return_adv=True)
    rows = sorted(((adv.rssi or -999, d.name, addr) for addr, (d, adv) in devs.items()),
                  key=lambda t: t[0], reverse=True)
    for rssi, name, addr in rows:
        log.raw(f"    rssi={rssi:>4}  {name or '(no name)':26}  {addr}")
    log.line(f"ambient scan done ({len(rows)} devices).")


async def catch(log: Log):
    """Guided catch of the rotating-address timer. Returns (BLEDevice, connected
    BleakClient) for the first nearby device that has the fe32 service, else
    (None, None). The scanner is stopped once a B-Hyve is caught."""
    log.line("")
    log.line("=== CAPTURE ===")
    log.line("STEP 1. On your phone: open the B-Hyve app so it CONNECTS to the timer.")
    await ask("        Press Enter once the app is connected to the timer... ")

    baseline: set[str] = set()
    tried: set[str] = set()
    pending: dict = {}
    start = time.monotonic()

    def cb(dev, adv):
        if pending:
            return
        t = time.monotonic() - start
        r = adv.rssi if adv.rssi is not None else -999
        if t < BASELINE_S:
            baseline.add(dev.address)
            return
        if dev.address in baseline or dev.address in tried or r < NEAR_RSSI:
            return
        tried.add(dev.address)
        log.line(f"NEW close device @ {t:.1f}s: {dev.name or '(no name)'} rssi={r} {dev.address}")
        pending['dev'] = dev

    log.line(f"STEP 2. Starting scan. Learning ambient devices for {BASELINE_S:.0f}s — "
             "keep the phone connected during this.")
    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(BASELINE_S)
    log.line(f"        baseline learned ({len(baseline)} ambient devices).")
    log.line("STEP 3. NOW RELEASE THE PHONE: force-quit the B-Hyve app AND turn phone")
    log.line("        Bluetooth OFF. Then keep the timer still and WAIT.")
    log.line(f"        (waiting up to {CAPTURE_TIMEOUT:.0f}s for the timer to re-advertise...)")

    deadline = time.monotonic() + CAPTURE_TIMEOUT
    while time.monotonic() < deadline:
        if not pending:
            await asyncio.sleep(0.3)
            continue
        dev = pending.pop('dev')
        log.line(f"        connecting to {dev.address} ...")
        try:
            client = BleakClient(dev)
            await client.connect()
        except Exception as e:
            log.line(f"        connect failed ({type(e).__name__}: {e}); waiting for next advert...")
            continue
        if not any("fe32" in s.uuid.lower() for s in client.services):
            log.line("        -> not a B-Hyve (no fe32); disconnecting, waiting for next...")
            try:
                await client.disconnect()
            except Exception:
                pass
            continue
        log.line(f"        -> fe32 present: this IS a B-Hyve. Caught {dev.address}")
        await scanner.stop()
        return dev, client

    await scanner.stop()
    log.line("CAPTURE TIMED OUT — no B-Hyve caught. Tips: make sure you turned phone")
    log.line("Bluetooth OFF (not just closed the app), and the timer is close to the Mac.")
    return None, None


def _fmt(st) -> str:
    if st is None:
        return "(no status decoded)"
    return (f"clock={st.clock_str}  watering={st.is_watering}  "
            f"run_state={st.run_state}  seconds_remaining={st.seconds_remaining}")


async def live_control(sess, address: str, log: Log):
    log.line("")
    log.line("=== LIVE CONTROL (connection held open) ===")
    log.line("  s) read status   g) start zone   x) stop ALL   c) stop one zone   q) release")
    while True:
        choice = (await ask("live> ")).lower()
        if choice == "q":
            break
        elif choice == "s":
            log.line("status: " + _fmt(await sess.read_status()))
        elif choice == "g":
            z = int(await ask("  zone (1-based): "))
            secs = int(await ask("  seconds: "))
            await sess.start_zone(z, secs)
            log.line(f"start zone {z} {secs}s -> " + _fmt(await sess.read_status()))
        elif choice == "x":
            await sess.stop()
            log.line("stop ALL -> " + _fmt(await sess.read_status()))
        elif choice == "c":
            z = int(await ask("  zone to stop: "))
            await sess.stop_zone(z)
            log.line(f"stop zone {z} -> " + _fmt(await sess.read_status()))
        else:
            log.line("  (unknown — use s/g/x/c/q)")


async def capture_flow(log: Log):
    keyhex = _load_key_hex(log)
    if keyhex is None:
        return
    dev_ble, client = await catch(log)
    if client is None:
        return
    log.line(f"captured address this session: {dev_ble.address}")
    log.line("NOTE: if this is a rotating/privacy address it will differ next time — "
             "that's expected; we'll solve persistence once control is proven.")
    bh = BHyveXD(dev_ble.address, keyhex, tz_offset_sec=host_tz_offset())
    try:
        # Adopt the connection we just caught and run the PROVEN session protocol.
        async with bh.session(client=client) as sess:
            await sess.arm()
            st = await sess.read_status()
            log.line("first status after arm: " + _fmt(st))
            if st and st.device_mac and st.device_mac.upper() == WANT_MAC.upper():
                log.line(f"*** CONFIRMED new timer {WANT_MAC} at {dev_ble.address}")
            elif st and st.device_mac:
                log.line(f"(a B-Hyve, but MAC {st.device_mac} != target {WANT_MAC})")
            await live_control(sess, dev_ble.address, log)
    except NotABHyveError as e:
        log.line(f"not a B-Hyve after all: {e}")
    except Exception as e:
        log.line(f"session error ({type(e).__name__}: {e})")
    finally:
        log.line(f"released connection to {dev_ble.address}.")


async def main():
    log = Log(LOG_PATH)
    log.raw(__doc__)
    log.line(f"logging to {LOG_PATH}")
    while True:
        log.raw("\n--- MENU ---  1) ambient scan   2) capture the timer   3) log path   q) quit")
        choice = (await ask("menu> ")).lower()
        if choice == "q":
            break
        elif choice == "1":
            await ambient_scan(log)
        elif choice == "2":
            await capture_flow(log)
        elif choice == "3":
            log.line(f"log file: {LOG_PATH}")
        else:
            log.line("  (unknown — use 1/2/3/q)")
    log.line("bye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted.")
