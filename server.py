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
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from bhyve_xd import BHyveXD

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("BHYVE_CONFIG", os.path.join(HERE, "config.json"))
INDEX = os.path.join(HERE, "index.html")

app = FastAPI(title="B-Hyve XD Local API", version="1.1.0")
_ble_lock = asyncio.Lock()   # the radio does one thing at a time


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


@app.get("/")
async def index():
    return FileResponse(INDEX)


@app.get("/api/devices")
async def devices():
    """List configured timers so the UI can offer a picker."""
    import json
    with open(CONFIG) as f:
        devs = json.load(f)["devices"]
    return [{"index": i, "name": d.get("name", "B-Hyve XD"),
             "stations": int(d.get("stations", 4))} for i, d in enumerate(devs)]


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
