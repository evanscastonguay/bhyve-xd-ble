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


# --- commands FROM Home Assistant (parsing is pure + tested; I/O lives in run_bridge) ---------- #

def command_sub(base: str) -> str:
    """The single wildcard subscription that catches every valve command topic."""
    return f"{base}/+/valve/+/set"


def parse_command_topic(base: str, topic: str):
    """`{base}/{idx}/valve/{z}/set` -> (idx, zone), else None."""
    p = topic.split("/")
    if len(p) == 5 and p[0] == base and p[2] == "valve" and p[4] == "set":
        try:
            return int(p[1]), int(p[3])
        except ValueError:
            return None
    return None


async def dispatch_command(base: str, topic: str, payload, on_command) -> bool:
    """Parse a command message and invoke `on_command(idx, zone, on)`. Returns False (no-op) for
    anything that isn't a valve command. Payload `ON` -> start, anything else -> stop."""
    parsed = parse_command_topic(base, topic)
    if parsed is None:
        return False
    idx, zone = parsed
    on = str(payload).strip().upper() == "ON"
    await on_command(idx, zone, on)
    return True


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


async def run_bridge(config: dict, devices, version: str, stop: asyncio.Event, on_command=None):
    """Connect to the broker (with an offline LWT), publish Discovery + online, subscribe to valve
    commands (if `on_command` given), then stay connected until `stop` is set. Kept alive for the
    server's lifetime; live state is pushed by server._run via active().publish_state(). Best-effort:
    connection errors are logged, never fatal to the server."""
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
            pump = None
            if on_command is not None:
                await client.subscribe(command_sub(base))

                async def _pump():
                    async for m in client.messages:
                        raw = m.payload
                        payload = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
                        try:
                            await dispatch_command(base, str(m.topic), payload, on_command)
                        except Exception as err:  # noqa: BLE001 — one bad command must not kill the loop
                            print(f"[mqtt] command error ({err})")

                pump = asyncio.create_task(_pump())
            try:
                await stop.wait()
            finally:
                if pump is not None:
                    pump.cancel()
                await br.publish_availability(False)
    except Exception as err:  # noqa: BLE001 — a broken broker must never take down control
        print(f"[mqtt] bridge error ({err}); HA integration off, local control unaffected")
    finally:
        _active = None
