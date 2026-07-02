"""
REST API + web UI for local B-Hyve XD control. Runs near the timer (Linux/BlueZ
recommended). Serves index.html and exposes JSON endpoints that each connect,
ARM the device, act, and read the status back for confirmation.

    pip install -r requirements.txt        # includes fastapi + uvicorn
    uvicorn server:app --host 0.0.0.0 --port 8000
    # open http://<host>:8000/

Each call does a full BLE session (connect -> arm -> command -> read -> disconnect),
so a call takes ~8-12s. Calls are serialized with a lock (one BLE op at a time).
The timer is battery BLE — press a button on it to wake it just before acting.
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from bhyve_xd import BHyveXD, DeviceStatus

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("BHYVE_CONFIG", os.path.join(HERE, "config.json"))
INDEX = os.path.join(HERE, "index.html")

app = FastAPI(title="B-Hyve XD Local API", version="1.0.0")
_ble_lock = asyncio.Lock()   # serialize BLE — the radio does one thing at a time


def _load():
    with open(CONFIG) as f:
        c = json.load(f)["devices"][0]
    dev = BHyveXD(c["address"], c["network_key"], tz_offset_sec=int(c.get("tz_offset_sec", -14400)))
    return dev, c


def _status_dict(st: DeviceStatus) -> dict:
    return {
        "clock": st.clock_str,
        "device_time": st.device_time,
        "is_watering": st.is_watering,
        "active_run_state": st.run_state,
        "seconds_remaining": st.seconds_remaining,
    }


class StartBody(BaseModel):
    minutes: float = Field(default=5, gt=0, le=120, description="run time in minutes")


@app.get("/")
async def index():
    return FileResponse(INDEX)


@app.get("/api/health")
async def health():
    try:
        _, c = _load()
        return {"ok": True, "name": c.get("name", "B-Hyve XD"),
                "address": c["address"], "stations": int(c.get("stations", 4))}
    except Exception as err:  # noqa: BLE001
        raise HTTPException(500, f"config error: {err}") from err


@app.get("/api/status")
async def status():
    dev, _ = _load()
    async with _ble_lock:
        try:
            async with dev.session() as s:
                await s.arm()
                st = await s.read_status()
        except Exception as err:  # noqa: BLE001
            raise HTTPException(503, f"BLE error: {err}") from err
    return _status_dict(st)


@app.post("/api/zones/{zone}/start")
async def start(zone: int, body: StartBody):
    if not 1 <= zone <= 4:
        raise HTTPException(400, "zone must be 1..4")
    dev, _ = _load()
    async with _ble_lock:
        try:
            async with dev.session() as s:
                await s.arm()
                await s.start_zone(zone, int(body.minutes * 60))
                st = await s.read_status()
        except Exception as err:  # noqa: BLE001
            raise HTTPException(503, f"BLE error: {err}") from err
    return {"zone": zone, "requested_minutes": body.minutes,
            "confirmed_watering": st.is_watering, **_status_dict(st)}


@app.post("/api/zones/{zone}/stop")
async def stop_zone(zone: int):
    if not 1 <= zone <= 4:
        raise HTTPException(400, "zone must be 1..4")
    dev, _ = _load()
    async with _ble_lock:
        try:
            async with dev.session() as s:
                await s.arm()
                await s.stop_zone(zone)
                st = await s.read_status()
        except Exception as err:  # noqa: BLE001
            raise HTTPException(503, f"BLE error: {err}") from err
    return {"zone": zone, "confirmed_idle": not st.is_watering, **_status_dict(st)}


@app.post("/api/stop")
async def stop():
    """Stop ALL zones."""
    dev, _ = _load()
    async with _ble_lock:
        try:
            async with dev.session() as s:
                await s.arm()
                await s.stop()
                st = await s.read_status()
        except Exception as err:  # noqa: BLE001
            raise HTTPException(503, f"BLE error: {err}") from err
    return {"confirmed_idle": not st.is_watering, **_status_dict(st)}
