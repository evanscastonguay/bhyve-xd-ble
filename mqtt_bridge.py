"""
Optional Home Assistant bridge — mirror each timer onto MQTT so HA auto-creates entities via
MQTT Discovery. **The server stays the sole BLE owner; HA never touches Bluetooth** (that's the whole
reason this is an MQTT bridge and not a native BLE integration — the timer allows one connection).

Opt-in: a top-level `"mqtt"` block in config.json. Absent -> the bridge never starts and nothing
changes. See docs/plans/PLAN_home_assistant.md.

P1 (this module): topic layout + Discovery config payloads + state payloads (pure, unit-tested) and a
thin async publisher (`MqttBridge`) plus a lazy-imported live runner (`run_bridge`). Command handling
(start/stop *from* HA) arrives in P2 — the `command_topic`s are advertised now but not yet consumed.
"""
from __future__ import annotations

import asyncio
import json

DEFAULT_PREFIX = "homeassistant"   # HA's default MQTT Discovery prefix
DEFAULT_BASE = "bhyve"             # our own state/command topic root

# --- pure helpers (fully unit-tested; no I/O) -------------------------------------------------- #

def topics(base: str, idx: int) -> dict:
    """The topic layout for one timer: a single JSON state topic + a shared availability topic."""
    return {
        "state": f"{base}/{idx}/state",
        "availability": f"{base}/availability",
        "cmd_valve": f"{base}/{idx}/valve",   # + /<z>/set  (consumed in P2)
    }


def _device_block(idx: int, name: str, version: str) -> dict:
    return {
        "identifiers": [f"{DEFAULT_BASE}_{idx}"],
        "name": name,
        "model": "B-Hyve XD (HT34)",
        "manufacturer": "Orbit (unofficial)",
        "sw_version": version,
    }


def discovery_configs(idx: int, name: str, stations: int, version: str,
                      base: str = DEFAULT_BASE, prefix: str = DEFAULT_PREFIX):
    """Yield (config_topic, payload) pairs for HA MQTT Discovery: a switch per valve, a watering
    binary_sensor, and a seconds-remaining sensor. Every entity reads the one JSON state topic via a
    value_template and shares the device + availability, so HA groups them under one device."""
    t = topics(base, idx)
    dev = _device_block(idx, name, version)
    avail = [{"topic": t["availability"], "payload_available": "online",
              "payload_not_available": "offline"}]
    out = []

    for z in range(1, stations + 1):
        uid = f"{base}_{idx}_valve{z}"
        out.append((f"{prefix}/switch/{uid}/config", {
            "name": f"Valve {z}",
            "unique_id": uid,
            "state_topic": t["state"],
            "value_template": f"{{{{ 'ON' if value_json.active_zone == {z} else 'OFF' }}}}",
            "command_topic": f"{t['cmd_valve']}/{z}/set",   # advertised now, consumed in P2
            "payload_on": "ON", "payload_off": "OFF",
            "icon": "mdi:sprinkler",
            "device": dev, "availability": avail,
        }))

    uid = f"{base}_{idx}_watering"
    out.append((f"{prefix}/binary_sensor/{uid}/config", {
        "name": "Watering",
        "unique_id": uid,
        "state_topic": t["state"],
        "value_template": "{{ 'ON' if value_json.is_watering else 'OFF' }}",
        "payload_on": "ON", "payload_off": "OFF",
        "device_class": "running",
        "device": dev, "availability": avail,
    }))

    uid = f"{base}_{idx}_seconds_remaining"
    out.append((f"{prefix}/sensor/{uid}/config", {
        "name": "Seconds remaining",
        "unique_id": uid,
        "state_topic": t["state"],
        "value_template": "{{ value_json.seconds_remaining | default(0) }}",
        "unit_of_measurement": "s",
        "icon": "mdi:timer-sand",
        "device": dev, "availability": avail,
    }))
    return out


def state_payload(status: dict) -> dict:
    """The subset of a DeviceStatus.to_dict() the HA entities template off of."""
    return {
        "is_watering": bool(status.get("is_watering")),
        "active_zone": status.get("active_zone"),
        "seconds_remaining": status.get("seconds_remaining"),
    }


# --- thin publisher over an async MQTT client (client injected -> testable with a fake) --------- #

class MqttBridge:
    """Publishes Discovery/state/availability via any client exposing
    `async publish(topic, payload, retain=False, qos=0)` (aiomqtt's Client, or a fake in tests)."""

    def __init__(self, client, base: str = DEFAULT_BASE, prefix: str = DEFAULT_PREFIX):
        self.client = client
        self.base = base
        self.prefix = prefix

    async def publish_discovery(self, devices, version: str):
        for d in devices:
            for ctopic, payload in discovery_configs(
                    d["index"], d.get("name") or f"Timer {d['index']}",
                    int(d.get("stations", 4)), version, self.base, self.prefix):
                await self.client.publish(ctopic, json.dumps(payload), retain=True)

    async def publish_state(self, idx: int, status: dict):
        await self.client.publish(topics(self.base, idx)["state"],
                                  json.dumps(state_payload(status)), retain=True)

    async def publish_availability(self, online: bool):
        await self.client.publish(f"{self.base}/availability",
                                  "online" if online else "offline", retain=True)


# --- live runner (lazy aiomqtt; started from the server lifespan only when configured) ---------- #

_active: MqttBridge | None = None


def active() -> MqttBridge | None:
    """The connected bridge, or None. server._run calls this to push confirmed state to HA."""
    return _active


async def run_bridge(config: dict, devices, version: str, stop: asyncio.Event):
    """Connect to the broker (with an offline LWT), publish Discovery + online + a first state, then
    stay connected until `stop` is set. Kept alive for the server's lifetime; publishing of live state
    is driven by server._run via active().publish_state(). Best-effort: connection errors are logged,
    never fatal to the server."""
    global _active
    try:
        import aiomqtt  # lazy: only needed when MQTT is configured
    except Exception as err:  # noqa: BLE001
        print(f"[mqtt] disabled — aiomqtt not installed ({err}); pip install -r requirements.txt")
        return
    base = config.get("base_topic", DEFAULT_BASE)
    prefix = config.get("discovery_prefix", DEFAULT_PREFIX)
    will = aiomqtt.Will(topic=f"{base}/availability", payload="offline", retain=True)
    try:
        async with aiomqtt.Client(
                hostname=config["host"], port=int(config.get("port", 1883)),
                username=config.get("username"), password=config.get("password"),
                will=will) as client:
            br = MqttBridge(client, base=base, prefix=prefix)
            await br.publish_discovery(devices, version)
            await br.publish_availability(True)
            _active = br
            print(f"[mqtt] bridge online -> {config['host']} (base '{base}')")
            try:
                await stop.wait()
            finally:
                await br.publish_availability(False)
    except Exception as err:  # noqa: BLE001 — a broken broker must never take down control
        print(f"[mqtt] bridge error ({err}); HA integration off, local control unaffected")
    finally:
        _active = None
