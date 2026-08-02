#!/usr/bin/env python3
"""
provision_proof.py — prove DURABLE app-free provisioning beyond doubt.

The adversarial review showed our old "evidence" was circular + confounded. This harness
runs the only sequence that settles it causally, on real hardware, with a timestamped log:

  1) NEGATIVE CONTROL — right after a factory reset, confirm the device is genuinely
     KEYLESS (our account key must FAIL to decode). If it decodes, the trial is confounded
     (app / leftover state) and aborts.
  2) PROVISION app-free — our tooling writes the key + finalize (Orbit app NEVER opened).
  3) POWER-CYCLE + verify in a FRESH PROCESS — status must decode AND start/stop must work.
  4) Repeat the power-cycle N times.

PASS only if: keyless before -> provisioned by us -> survives >= N power-cycles, app untouched.
That chain can only be explained by our provisioning (kills the circularity + the confound).

RUN (in your own Terminal; you perform the physical reset/power-cycles at the prompts):
    cd /Users/evans/project/bhyve-xd-ble
    ./venv/bin/python provision_proof.py                 # uses config.json device 0's key
    ./venv/bin/python provision_proof.py --device-mac=44:67:55:D8:71:B0
    ./venv/bin/python provision_proof.py --self-key --device-mac=44:67:55:D8:71:B0
        # ^ proves the STANDALONE path: generate our OWN key (no Orbit account), then the
        #   same negative-control + power-cycle durability test. Key is stashed to secrets/.

Evidence log: /tmp/provision_proof.log (contains the key -> delete after).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime

LOG_PATH = "/tmp/provision_proof.log"
MIN_CYCLES = 3


# --------------------------------------------------------------------------- #
# Pure pass/fail evaluation — unit-tested; the harness cannot "cheat" past this.
# --------------------------------------------------------------------------- #
def evaluate_trial(neg_control_decoded: bool, provisioned: bool, cycle_results: list[bool]):
    """Return (passed: bool, reason: str). PASS requires: device was keyless first
    (negative control did NOT decode), we provisioned it, and every one of >= MIN_CYCLES
    post-power-cycle verifications (in a fresh process) succeeded."""
    if neg_control_decoded:
        return False, ("negative control DECODED — the device was already keyed, so a later "
                       "success proves nothing (confounded by the app / leftover state). ABORT.")
    if not provisioned:
        return False, "provisioning did not succeed — nothing to prove durability of."
    if len(cycle_results) < MIN_CYCLES:
        return False, f"only {len(cycle_results)} power-cycle verification(s); need >= {MIN_CYCLES}."
    if not all(cycle_results):
        bad = [i + 1 for i, ok in enumerate(cycle_results) if not ok]
        return False, (f"post-power-cycle verify FAILED on cycle(s) {bad} — the key did NOT "
                       "persist. Durable app-free provisioning is NOT proven.")
    return True, (f"keyless before -> provisioned app-free -> decoded + actuated after "
                  f"{len(cycle_results)} power-cycles, app untouched. Durability PROVEN.")


# --------------------------------------------------------------------------- #
# Logging + prompts
# --------------------------------------------------------------------------- #
class Log:
    def __init__(self, path):
        self._f = open(path, "a", buffering=1)
        self.line(f"\n===== provision_proof {datetime.now().isoformat(timespec='seconds')} =====")

    def line(self, msg=""):
        self._f.write((f"[{datetime.now().strftime('%H:%M:%S')}] {msg}" if msg else "") + "\n")
        print(msg)


async def ask(prompt):
    return (await asyncio.to_thread(input, prompt)).strip()


def _load_key(log=None):
    try:
        k = json.load(open("config.json"))["devices"][0]["network_key"]
        if log:
            log.line(f"using account key from config.json (...{k[-4:]})")
        return k
    except Exception as e:
        if log:
            log.line(f"could not read config.json network_key: {e}")
        return None


# --------------------------------------------------------------------------- #
# Fresh-process verify entrypoint (spawned as `provision_proof.py --verify`).
# Reads the key from $BHYVE_KEY so it never appears in the process argument list.
# --------------------------------------------------------------------------- #
async def verify_once():
    from onboarding import catch_device_session, ResolveError
    key = os.environ.get("BHYVE_KEY") or _load_key()
    try:
        addr, mac, st, sess = await catch_device_session(key, scan_timeout=45)
    except ResolveError as e:
        print(json.dumps({"decoded": False, "error": str(e)}))
        return
    out = {"decoded": True, "mac": mac, "addr": addr, "clock": st.clock_str}
    try:
        await sess.start_zone(1, 10)
        s2 = await sess.read_status()
        out["watering"] = bool(s2 and s2.is_watering)
        await sess.stop()
        s3 = await sess.read_status()
        out["stopped_idle"] = bool(s3 and not s3.is_watering)
    except Exception as e:
        out["actuation_error"] = str(e)
    finally:
        try:
            await sess.__aexit__(None, None, None)
        except Exception:
            pass
    print(json.dumps(out))


async def _spawn_verify(key, log):
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, BHYVE_KEY=key, PYTHONPATH=here)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.abspath(__file__), "--verify",
        cwd=here, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    for line in reversed(out.decode().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    return {"decoded": False, "error": "no JSON from verify subprocess",
            "stderr": err.decode()[-300:]}


# --------------------------------------------------------------------------- #
# The proof protocol
# --------------------------------------------------------------------------- #
async def _negative_control(key, log):
    from onboarding import catch_device, ResolveError
    try:
        addr, mac, _st = await catch_device(key, scan_timeout=20)
        log.line(f"  NEG-CONTROL: device DECODED with our key (mac={mac}) -> ALREADY KEYED")
        return {"decoded": True, "present": True}
    except ResolveError as e:
        m = re.search(r"(\d+)\s+B-Hyve", str(e))
        n = int(m.group(1)) if m else 0
        log.line(f"  NEG-CONTROL: no decode (good). fe32 B-Hyve seen this pass: {n}")
        return {"decoded": False, "present": n >= 1}


async def run_proof(log, key, want_mac=None, cycles=MIN_CYCLES):
    log.line(f"target key ...{key[-4:]}   want_mac={want_mac or '(auto, single device)'}   cycles={cycles}")
    log.line("STEP 1 — factory-reset the timer into PAIRING MODE (dial OFF, hold ~10s until the "
             "full display lights). Phone Bluetooth OFF. Do NOT open the B-Hyve app.")
    await ask("  press Enter when the timer is reset + in pairing mode... ")

    log.line("STEP 2 — negative control (the device must NOT decode with our key)...")
    neg = await _negative_control(key, log)
    if neg["decoded"]:
        _verdict(log, evaluate_trial(True, False, []))
        return
    if not neg["present"]:
        log.line("  ABORT: no B-Hyve detected — ensure it's awake/in pairing mode and close, then retry.")
        return
    log.line("  keyless confirmed (B-Hyve present, our key does not decode).")

    log.line("STEP 3 — provisioning app-free (write key + finalize; app stays closed)...")
    from onboarding import provision_device, ResolveError
    provisioned = False
    try:
        addr, mac, st = await provision_device(key, want_mac=want_mac, scan_timeout=45)
        provisioned = True
        log.line(f"  provisioned addr={addr} mac={mac} status={st.to_dict()}")
    except ResolveError as e:
        log.line(f"  provision FAILED: {e}")
    if not provisioned:
        _verdict(log, evaluate_trial(False, False, []))
        return

    cyc = []
    for i in range(1, cycles + 1):
        log.line(f"STEP 4.{i} — POWER-CYCLE the timer now (pull batteries ~15s, reinsert). "
                 "Phone still OFF, app still closed.")
        await ask(f"  press Enter after power-cycle {i}/{cycles}... ")
        res = await _spawn_verify(key, log)
        ok = bool(res.get("decoded") and res.get("watering") and res.get("stopped_idle"))
        log.line(f"  cycle {i} FRESH-PROCESS verify -> {res}  => {'OK' if ok else 'FAIL'}")
        cyc.append(ok)

    _verdict(log, evaluate_trial(neg["decoded"], provisioned, cyc))


def _verdict(log, result):
    passed, reason = result
    log.line("")
    log.line(f"=== VERDICT: {'PASS ✅ — durable app-free provisioning PROVEN' if passed else 'FAIL ❌'} ===")
    log.line(reason)
    log.line(f"(full evidence: {LOG_PATH})")


def main():
    if "--verify" in sys.argv:
        asyncio.run(verify_once())
        return
    log = Log(LOG_PATH)
    log.line(f"logging to {LOG_PATH}")
    want = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--device-mac=")), None)
    if "--self-key" in sys.argv:
        # STANDALONE proof: mint + stash our OWN key, then prove durability with it.
        from cli import _stash_key
        key = os.urandom(16).hex()
        sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets", "generated_keys.md")
        try:
            _stash_key(key, want, sp)
            log.line(f"SELF-KEY MODE: generated + stashed our own key (…{key[-4:]}); full key in {sp}")
        except Exception as e:
            log.line(f"SELF-KEY MODE: generated our own key …{key[-4:]} (stash failed: {e} — SAVE IT: {key})")
    else:
        key = _load_key(log)
        if not key:
            return
    try:
        asyncio.run(run_proof(log, key, want_mac=want))
    except KeyboardInterrupt:
        log.line("interrupted.")


if __name__ == "__main__":
    main()
