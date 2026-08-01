#!/usr/bin/env python3
"""
bhyve_lab.py — interactive diagnostic for controlling a B-Hyve timer live on macOS.

For NORMAL onboarding use `cli.py register` (one command). This tool is the hands-on
diagnostic: it catches a timer and drops you into a live menu so you can repeatedly
start/stop/read on ONE held connection while watching a timestamped log — handy for
debugging a flaky unit or the phone hand-off.

It reuses the SAME discovery + protocol as the product: `onboarding.catch_device_session`
(connect-on-detection, robust to rotating addresses) → the proven bhyve_xd session. No
duplicated BLE logic lives here anymore.

HOW TO RUN (in your own Terminal, from the project directory)
    cd path/to/bhyve-xd-ble
    ./venv/bin/python bhyve_lab.py

  Everything you see is ALSO written, timestamped, to  bhyve_lab.log

MENU
  1) Ambient scan        — list nearby BLE devices (no timer needed)
  2) Capture the timer   — phone-off prompt -> catch -> live control
  3) Where is the log    — print the log file path
  q) Quit

Precondition for capture: the phone must NOT be holding the timer (its Bluetooth OFF),
so the timer advertises freely. Nothing here resets the device.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

from bleak import BleakScanner

WANT_MAC = "AA:BB:CC:DD:EE:01"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bhyve_lab.log")


# --------------------------------------------------------------------------- #
# Logging: every line goes to the screen AND to bhyve_lab.log with a timestamp.
# --------------------------------------------------------------------------- #
class Log:
    def __init__(self, path: str):
        self._f = open(path, "a", buffering=1)
        self.line(f"\n===== bhyve_lab session started {datetime.now().isoformat(timespec='seconds')} =====")

    def line(self, msg: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        self._f.write((f"[{ts}] {msg}" if msg else "") + "\n")
        print(msg)

    def raw(self, msg: str):
        self._f.write(msg + "\n")
        print(msg)


async def ask(prompt: str) -> str:
    """Non-blocking input() so the BLE event loop keeps running (keepalives etc.)."""
    return (await asyncio.to_thread(input, prompt)).strip()


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
    log.line("ambient scan 10s — devices nearby (helps confirm the Mac's BLE works):")
    devs = await BleakScanner.discover(timeout=10, return_adv=True)
    rows = sorted(((adv.rssi or -999, d.name, addr) for addr, (d, adv) in devs.items()),
                  key=lambda t: t[0], reverse=True)
    for rssi, name, addr in rows:
        log.raw(f"    rssi={rssi:>4}  {name or '(no name)':26}  {addr}")
    log.line(f"ambient scan done ({len(rows)} devices).")


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
    import onboarding

    log.line("")
    log.line("=== CAPTURE ===")
    log.line("Turn your phone's Bluetooth OFF so it isn't holding the timer, keep it close.")
    await ask("Press Enter to search for the timer... ")
    try:
        address, mac, st, sess = await onboarding.catch_device_session(keyhex, scan_timeout=90.0)
    except onboarding.ResolveError as e:
        log.line(f"capture failed: {e}")
        return

    log.line(f"caught {mac} at {address}")
    log.line("first status after arm: " + _fmt(st))
    if mac and mac.upper() == WANT_MAC.upper():
        log.line(f"*** matches target {WANT_MAC}")
    try:
        await live_control(sess, address, log)
    finally:
        try:
            await sess.__aexit__(None, None, None)
        except Exception:
            pass
        log.line(f"released connection to {address}.")


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
