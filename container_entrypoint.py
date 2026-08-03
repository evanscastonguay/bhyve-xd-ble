#!/usr/bin/env python3
"""
Container entrypoint. Assembles config.json from Home Assistant add-on options (`/data/options.json`)
when present, then launches the server. For plain Docker you can instead mount your own config.json at
`$BHYVE_CONFIG` (default /data/config.json) and skip options entirely.

`build_config` (pure, unit-tested) maps the add-on's flat options to the server's config.json shape.
"""
from __future__ import annotations

import json
import os
import sys


def build_config(opts: dict) -> dict:
    """Flat add-on options -> server config.json structure (one device + optional MQTT bridge)."""
    dev = {
        "name": opts.get("name") or "B-Hyve XD",
        "address": opts["address"],                       # MAC (Linux) — required
        "network_key": opts["network_key"],               # 16-byte hex — required
        "tz_offset_sec": int(opts.get("tz_offset_sec", 0)),
        "stations": int(opts.get("stations", 4)),
    }
    cfg: dict = {"devices": [dev], "host_scheduling": bool(opts.get("host_scheduling", False))}
    if opts.get("mqtt_host"):                              # MQTT block only when a broker is configured
        cfg["mqtt"] = {
            "host": opts["mqtt_host"],
            "port": int(opts.get("mqtt_port", 1883)),
            "username": opts.get("mqtt_username") or None,
            "password": opts.get("mqtt_password") or None,
            "base_topic": opts.get("mqtt_base_topic", "bhyve"),
            "discovery_prefix": opts.get("mqtt_discovery_prefix", "homeassistant"),
            "default_minutes": int(opts.get("mqtt_default_minutes", 5)),
        }
    return cfg


def main() -> None:
    cfg_path = os.environ.get("BHYVE_CONFIG", "/data/config.json")
    opts_path = "/data/options.json"                      # written by the HA Supervisor for add-ons
    if os.path.exists(opts_path):
        opts = json.load(open(opts_path))
        if opts.get("address") and opts.get("network_key"):
            os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
            with open(cfg_path, "w") as f:
                json.dump(build_config(opts), f)
    if not os.path.exists(cfg_path):
        sys.exit(f"No config found. Set the add-on options, or mount a config.json at {cfg_path}.")
    os.execvp("uvicorn", ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"])


if __name__ == "__main__":
    main()
