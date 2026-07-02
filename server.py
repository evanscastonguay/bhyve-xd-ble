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


def _device(device: "str | None" = None) -> BHyveXD:
    """Load a device from config. `device` selects by name (or numeric index) when
    the config has several; defaults to the first."""
    sel = None
    if device is not None:
        sel = int(device) if device.lstrip("-").isdigit() else device
    try:
        return BHyveXD.from_config(CONFIG, device=sel)
    except (KeyError, IndexError) as err:
        raise HTTPException(404, f"device {device!r} not found: {err}") from err


async def _run(coro_fn, device: "str | None" = None):
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


@app.get("/")
async def index():
    return FileResponse(INDEX)


@app.get("/api/devices")
async def devices():
    """List configured timers (name + index) for a multi-device UI selector."""
    import json
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)["devices"]
    except FileNotFoundError as err:
        raise HTTPException(404, "no config.json — run `cli.py login` first") from err
    return [{"index": i, "name": d.get("name", "B-Hyve XD"), "stations": int(d.get("stations", 4))}
            for i, d in enumerate(cfg)]


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
