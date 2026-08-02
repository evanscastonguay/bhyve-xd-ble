"""
REST API + web UI for local B-Hyve XD control. Runs near the timer.

A thin HTTP layer over bhyve_xd.BHyveXD — the same shared control logic the CLI
uses (arm -> command -> read-back confirm). No BLE logic is duplicated here.

    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8000
    # open http://<host>:8000/

Each call does a full BLE session (~8-12s). Calls are serialized with a lock so
only one BLE operation runs at a time. Wake the timer (press its button) if a
call times out.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import onboarding
from bhyve_xd import BHyveXD

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("BHYVE_CONFIG", os.path.join(HERE, "config.json"))
INDEX = os.path.join(HERE, "index.html")

@asynccontextmanager
async def _lifespan(app):
    task = asyncio.create_task(_scheduler_loop())   # host-driven schedule engine
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="B-Hyve XD Local API", version="1.2.0", lifespan=_lifespan)
_ble_lock = asyncio.Lock()   # the radio does one thing at a time
_job = None                  # the single in-flight onboarding job (or None)
_account_session = None       # in-memory only: {email, key, devices}. NEVER persisted;
                              # the persisted account (email+key) lives in config.json.


class _OnboardJob:
    """One onboarding run: drives onboarding.onboard_flow, accumulating its Step events
    (so the SSE stream is re-attachable) and holding the human gate."""
    def __init__(self):
        self.gate = onboarding.OnboardGate()
        self.events = []
        self.done = False
        self.task = None

    async def run(self, params):
        try:
            async for ev in onboarding.onboard_flow(params, self.gate):
                self.events.append(ev)
        except Exception as err:  # noqa: BLE001 — surface as a failed step, never crash the server
            self.events.append({"id": "error", "title": "Onboarding error",
                                "instruction": str(err), "state": "failed", "verified": False})
        finally:
            self.done = True


def _coerce_device(device):
    """A ?device= query value is always a string; a purely-numeric one selects by
    0-based index, otherwise by name. None -> the first configured device."""
    if isinstance(device, str) and device.lstrip("-").isdigit():
        return int(device)
    return device


def _device(device=None) -> BHyveXD:
    return BHyveXD.from_config(CONFIG, device=_coerce_device(device))


async def _run(coro_fn, device=None):
    """Serialize BLE access and translate errors to HTTP. coro_fn takes the
    device and returns a DeviceStatus."""
    if _job is not None and not _job.done:
        raise HTTPException(503, "onboarding in progress — try again once it finishes")
    dev = _device(device)
    async with _ble_lock:
        try:
            return await coro_fn(dev)
        except Exception as err:  # noqa: BLE001
            raise HTTPException(503, f"BLE error: {err}") from err


class StartBody(BaseModel):
    minutes: float = Field(default=5, gt=0, le=120, description="run time in minutes")


class OnboardBody(BaseModel):
    name: str | None = Field(default=None, description="friendly name for the new timer")
    device_mac: str | None = Field(default=None, description="target a specific unit by MAC")
    email: str | None = Field(default=None, description="Orbit email (only if no saved key yet)")
    password: str | None = Field(default=None, description="Orbit password (never stored)")
    scan_timeout: float = Field(default=60, gt=0, le=180, description="seconds to wait for the timer")


class OnboardStartBody(BaseModel):
    mode: str = Field(default="orbit", description="'orbit' (account key) or 'self' (own key)")
    email: str | None = None
    password: str | None = None
    name: str | None = None
    device_mac: str | None = None


class AccountLoginBody(BaseModel):
    email: str = Field(description="Orbit email")
    password: str = Field(description="Orbit password (used once, never stored)")


def _config_macs() -> set[str]:
    """Upper-cased MACs of timers already in config.json (to flag 'already added')."""
    try:
        with open(CONFIG) as f:
            return {(d.get("mac") or "").upper() for d in json.load(f).get("devices", [])}
    except (OSError, json.JSONDecodeError):
        return set()


@app.post("/api/account/login")
async def account_login(body: AccountLoginBody):
    """Sign in to Orbit ONCE per account: fetch the account's timers and the shared BLE
    key. Persists {email, key} (never the password) and caches the timer list in memory
    so a later add needs no re-login. The network key is NEVER returned to the browser."""
    global _account_session
    try:
        devices = await onboarding.cloud_fetch(body.email, body.password)
    except onboarding.AuthError as e:
        raise HTTPException(401, str(e)) from e
    except onboarding.RateLimited as e:
        raise HTTPException(429, str(e)) from e
    except onboarding.CloudError as e:
        raise HTTPException(502, str(e)) from e
    controllable = [d for d in devices if d.get("network_key")]
    if not controllable:
        raise HTTPException(404, "no controllable timers with a BLE key on this account")
    key = controllable[0]["network_key"]        # shared account/mesh key
    onboarding.write_account(CONFIG, body.email, key)
    timers = [{"name": d.get("name"), "mac": d.get("mac"),
               "stations": int(d.get("stations") or 4)} for d in controllable]
    _account_session = {"email": body.email, "key": key, "devices": timers}
    have = _config_macs()
    return {"email": body.email,
            "timers": [{**t, "added": (t["mac"] or "").upper() in have} for t in timers]}


@app.get("/api/account")
async def account_get():
    """Report account state for the UI: whether we're signed in this session, the email
    (from the session or the persisted account), and whether a saved key exists."""
    saved = onboarding.read_account(CONFIG)
    email = (_account_session or {}).get("email") or (saved or {}).get("email")
    return {"signed_in": _account_session is not None, "email": email,
            "has_saved_key": saved is not None}


@app.get("/api/account/timers")
async def account_timers():
    """The cached account timer list (name/mac/stations, **never the key**), each flagged
    `added`. Empty with signed_in=false if there's no live session (e.g. after a restart) —
    the UI then offers a fresh sign-in or a loginless add with the saved key."""
    sess = _account_session
    if not sess:
        return {"signed_in": False, "timers": []}
    have = _config_macs()
    return {"signed_in": True,
            "timers": [{**t, "added": (t.get("mac") or "").upper() in have}
                       for t in sess.get("devices", [])]}


@app.post("/api/account/forget")
async def account_forget():
    """Forget the account: clear the in-memory session AND remove the persisted account
    block (devices are left untouched)."""
    global _account_session
    _account_session = None
    config = onboarding._load_config(CONFIG)
    if config.pop("account", None) is not None:
        onboarding._atomic_write_config(CONFIG, config)
    return {"ok": True}


class OnboardContinueBody(BaseModel):
    choice: str | None = Field(default=None, description="for a fallback_choice step: "
                               "'orbit_app_first' | 'self_key'")


@app.post("/api/onboard/start")
async def onboard_start(body: OnboardStartBody):
    """Launch the guided onboarding flow (one at a time). Progress is read from
    GET /api/onboard/stream; human steps are advanced with POST /api/onboard/continue."""
    global _job
    if _job is not None and not _job.done and _job.task is not None:
        _job.task.cancel()          # supersede a stale/awaiting job rather than 409-blocking it
    _job = _OnboardJob()
    params = {"mode": body.mode, "email": body.email, "password": body.password,
              "name": body.name, "device_mac": body.device_mac, "path": CONFIG,
              "secrets_path": os.path.join(HERE, "secrets", "generated_keys.md")}
    if body.mode == "account":
        # The browser sends only a MAC; resolve the shared account key server-side (from
        # the live session, else the persisted account) so the key never travels through
        # the client. Fill name/stations from the cached timer list when we have it.
        params["key"], name, stations = None, body.name, 4
        sess = _account_session
        if sess and sess.get("key"):
            params["key"] = sess["key"]
            want = (body.device_mac or "").upper()
            t = next((t for t in sess.get("devices", [])
                      if (t.get("mac") or "").upper() == want), None)
            if t:
                name, stations = name or t.get("name"), int(t.get("stations") or 4)
        else:
            acct = onboarding.read_account(CONFIG)
            params["key"] = acct.get("network_key") if acct else None
        params["name"], params["stations"] = name, stations
    _job.task = asyncio.create_task(_job.run(params))
    return {"ok": True, "mode": body.mode}


@app.post("/api/onboard/cancel")
async def onboard_cancel():
    """Cancel the current onboarding job (if any) and clear it, so the UI can leave the
    add flow cleanly without wedging the next start."""
    global _job
    if _job is not None and not _job.done and _job.task is not None:
        _job.task.cancel()
    _job = None
    return {"ok": True}


@app.post("/api/onboard/continue")
async def onboard_continue(body: OnboardContinueBody):
    """Advance a waiting_user step (e.g. after resetting the timer, or choosing a fallback)."""
    if _job is None:
        raise HTTPException(409, "no onboarding session")
    _job.gate.resume(body.choice)
    return {"ok": True}


@app.get("/api/onboard/stream")
async def onboard_stream():
    """SSE stream of onboarding Step events. Re-attachable: replays events so far, then
    follows new ones until the flow finishes."""
    async def gen():
        if _job is None:
            yield f"data: {json.dumps({'id': 'idle', 'state': 'none'})}\n\n"
            return
        i = 0
        while True:
            while i < len(_job.events):
                yield f"data: {json.dumps(_job.events[i])}\n\n"
                i += 1
            if _job.done:
                break
            await asyncio.sleep(0.3)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.delete("/api/devices/{index}")
async def remove_device(index: int):
    """Remove a configured timer (the UI confirms first). Atomic rewrite of config.json."""
    with open(CONFIG) as f:
        cfg = json.load(f)
    devs = cfg.get("devices", [])
    if not 0 <= index < len(devs):
        raise HTTPException(404, f"no device at index {index}")
    removed = devs.pop(index)
    directory = os.path.dirname(os.path.abspath(CONFIG))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"removed": removed.get("name"),
            "devices": [{"index": i, "name": d.get("name"), "stations": int(d.get("stations") or 4)}
                        for i, d in enumerate(devs)]}


@app.get("/")
async def index():
    # no-store: this control UI must never be served stale from browser cache
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


@app.get("/api/devices")
async def devices():
    """List configured timers so the UI can offer a picker."""
    import json
    with open(CONFIG) as f:
        devs = json.load(f)["devices"]
    return [{"index": i, "name": d.get("name", "B-Hyve XD"),
             "stations": int(d.get("stations", 4))} for i, d in enumerate(devs)]


class SchedulesBody(BaseModel):
    rules: list[dict] = Field(default_factory=list,
                              description="[{valve,start 'HH:MM',days[0..6],minutes,enabled}]")


def _mac_at(index: int) -> str:
    """MAC of the timer at 0-based index, or 404. Schedules are keyed by MAC (stable)."""
    with open(CONFIG) as f:
        devs = json.load(f).get("devices", [])
    if not 0 <= index < len(devs):
        raise HTTPException(404, f"no timer at index {index}")
    mac = devs[index].get("mac")
    if not mac:
        raise HTTPException(409, "this timer has no MAC yet — cannot store schedules")
    return mac


@app.get("/api/timers/{index}/schedules")
async def get_schedules(index: int):
    """The timer's saved watering rules (no BLE, no key). Engine-agnostic store."""
    import schedule as sched
    return {"schedules": sched.read_schedules(CONFIG, _mac_at(index))}


@app.put("/api/timers/{index}/schedules")
async def put_schedules(index: int, body: SchedulesBody):
    """Validate + persist the timer's rules. 400 on a bad rule (nothing written).
    Saving does NOT run anything yet — an engine wires these up later."""
    import schedule as sched
    mac = _mac_at(index)
    try:
        sched.write_schedules(CONFIG, mac, body.rules)
    except sched.ScheduleError as e:
        raise HTTPException(400, str(e)) from e
    return {"schedules": sched.read_schedules(CONFIG, mac)}


class SchedulingBody(BaseModel):
    enabled: bool


_fired: set = set()   # (mac, valve, "YYYY-MM-DD HH:MM") already run — one fire per minute


def _host_scheduling_enabled() -> bool:
    try:
        return bool(onboarding._load_config(CONFIG).get("host_scheduling"))
    except (OSError, ValueError):
        return False


@app.get("/api/scheduling")
async def get_scheduling():
    """Whether host-driven scheduling is enabled (fires while THIS Mac is running)."""
    return {"enabled": _host_scheduling_enabled()}


@app.put("/api/scheduling")
async def put_scheduling(body: SchedulingBody):
    config = onboarding._load_config(CONFIG)
    config["host_scheduling"] = bool(body.enabled)
    onboarding._atomic_write_config(CONFIG, config)
    return {"enabled": bool(body.enabled)}


async def run_due(now: datetime, fire) -> list:
    """Fire every due rule once for minute `now`. `fire(index, valve, minutes)` is injected
    (real one does the BLE start; tests pass a stub). Respects the enable flag, refuses while
    onboarding holds the radio, and is idempotent per (timer, valve, minute)."""
    if _job is not None and not _job.done:       # onboarding owns the radio
        return []
    if not _host_scheduling_enabled():
        return []
    stamp = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%Y-%m-%d")
    for k in [k for k in _fired if not k[2].startswith(today)]:
        _fired.discard(k)                         # prune stale days
    import scheduler
    fired = []
    for i, d in enumerate(onboarding._load_config(CONFIG).get("devices", [])):
        for r in scheduler.due_rules(d.get("schedules", []), now):
            key = (d.get("mac"), r["valve"], stamp)
            if key in _fired:
                continue
            _fired.add(key)
            await fire(i, r["valve"], r["minutes"])
            fired.append(key)
    return fired


async def _fire_start(index: int, valve: int, minutes: int):
    """The real firing action: a serialized BLE start for `minutes` (device auto-stops)."""
    async with _ble_lock:
        await _device(str(index)).start(int(valve), int(minutes) * 60)


async def _scheduler_loop():
    while True:
        try:
            await run_due(datetime.now(), _fire_start)
        except Exception:  # noqa: BLE001 — never let the loop die
            pass
        await asyncio.sleep(20)


@app.get("/api/onboard/state")
async def onboard_state():
    """Tell the UI whether a saved account key exists — if so, adding another timer
    needs no cloud login (the common case), so the wizard can hide the login fields."""
    import onboarding
    return {"has_key": onboarding.key_from_existing_config(CONFIG) is not None}


@app.post("/api/onboard/register")
async def onboard_register(body: OnboardBody):
    """Register a NEW timer: reuse the saved account key (or log in once if there's
    none / an email is given), catch the timer's live advertisement, read its MAC +
    status back, and write config.json. The network key is never returned to the client.

    Precondition (surfaced by the UI): the phone's Bluetooth must be OFF so it isn't
    holding the timer. Serialized with the BLE lock like every other radio operation.
    """
    import onboarding

    key = None if body.email else onboarding.key_from_existing_config(CONFIG)
    stations, name, want_mac = 4, body.name, body.device_mac
    if not key:
        if not body.email or not body.password:
            raise HTTPException(400, "no saved key yet — Orbit email + password required")
        try:
            devices = await onboarding.cloud_fetch(body.email, body.password)
        except onboarding.AuthError as e:
            raise HTTPException(401, str(e)) from e
        except onboarding.RateLimited as e:
            raise HTTPException(429, str(e)) from e
        except onboarding.CloudError as e:
            raise HTTPException(502, str(e)) from e
        controllable = [d for d in devices if d.get("network_key")]
        if not controllable:
            raise HTTPException(404, "no controllable devices with a BLE key on this account")
        if want_mac:
            chosen = next((d for d in controllable
                           if (d.get("mac") or "").upper() == want_mac.upper()), None)
            if not chosen:
                raise HTTPException(404, f"no device with MAC {want_mac} on the account")
        elif len(controllable) == 1:
            chosen = controllable[0]
        else:
            raise HTTPException(409, {"message": "multiple devices — choose one via device_mac",
                                      "devices": [{"mac": d.get("mac"), "name": d.get("name")}
                                                  for d in controllable]})
        key, want_mac = chosen["network_key"], chosen.get("mac")
        stations = int(chosen.get("stations") or 4)
        name = name or chosen.get("name")

    async with _ble_lock:
        try:
            address, mac, st = await onboarding.catch_device(
                key, want_mac=want_mac, scan_timeout=body.scan_timeout)
        except onboarding.ResolveError as e:
            raise HTTPException(504, str(e)) from e

    device = {"name": name or "B-Hyve XD", "address": address,
              "network_key": key, "mac": mac, "stations": stations}
    onboarding.write_config(CONFIG, device)
    return {"registered": {"name": device["name"], "address": address, "mac": mac,
                           "stations": stations}, "status": st.to_dict()}


@app.get("/api/health")
async def health(device: str | None = None):
    d = _device(device)
    return {"ok": True, "name": d.name, "address": d.address, "stations": d.stations}


@app.get("/api/status")
async def status(device: str | None = None):
    st = await _run(lambda d: d.status(), device)
    return st.to_dict()


@app.post("/api/zones/{zone}/start")
async def start(zone: int, body: StartBody, device: str | None = None):
    if not 1 <= zone <= 4:
        raise HTTPException(400, "zone must be 1..4")
    st = await _run(lambda d: d.start(zone, int(body.minutes * 60)), device)
    return {"zone": zone, "requested_minutes": body.minutes,
            "confirmed_watering": st.is_watering, **st.to_dict()}


@app.post("/api/zones/{zone}/stop")
async def stop_zone(zone: int, device: str | None = None):
    if not 1 <= zone <= 4:
        raise HTTPException(400, "zone must be 1..4")
    st = await _run(lambda d: d.stop(zone), device)
    return {"zone": zone, "confirmed_idle": not st.is_watering, **st.to_dict()}


@app.post("/api/stop")
async def stop_all(device: str | None = None):
    st = await _run(lambda d: d.stop(), device)
    return {"confirmed_idle": not st.is_watering, **st.to_dict()}
