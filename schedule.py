"""Engine-agnostic schedule store — per-timer watering programs in config.json.

No BLE, no engine, no clock: just the data model that BOTH schedule engines read/write
(host-driven fires the existing start/stop at these times; on-device would write these as
device programs). Schedules live under each device in config.json:

    {"devices": [{"mac": "...", "schedules": [
        {"valve": 2, "start": "06:00", "days": [0,2,4], "minutes": 5, "enabled": true}, ...]}]}

`days` are ints 0=Mon .. 6=Sun. `start` is 24h "HH:MM" in the timer's local time (timezone
mapping is the engine's job, not the store's). The store never touches the network key.
"""
from __future__ import annotations

import re

from onboarding import _atomic_write_config, _load_config

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAX_MINUTES = 120


class ScheduleError(ValueError):
    """A schedule rule is malformed / out of range, or the target timer is unknown."""


def validate_rule(r: dict, stations: int) -> dict:
    """Return a normalized rule dict, or raise ScheduleError. Pure — no I/O."""
    if not isinstance(r, dict):
        raise ScheduleError(f"rule must be an object, got {type(r).__name__}")
    try:
        valve = int(r["valve"])
        minutes = int(r["minutes"])
        start = str(r["start"])
        days = list(r.get("days") or [])
        enabled = bool(r.get("enabled", True))
    except (KeyError, TypeError, ValueError) as e:
        raise ScheduleError(f"malformed rule {r!r}: {e}") from e
    if not 1 <= valve <= stations:
        raise ScheduleError(f"valve {valve} out of range 1..{stations}")
    if not _HHMM.match(start):
        raise ScheduleError(f"bad start time {start!r} — want 24h 'HH:MM'")
    try:
        day_ints = {int(d) for d in days}
    except (TypeError, ValueError) as e:
        raise ScheduleError(f"days must be integers 0..6: {e}") from e
    if not day_ints or any(d not in range(7) for d in day_ints):
        raise ScheduleError("days must be a non-empty subset of 0..6 (0=Mon..6=Sun)")
    if not 1 <= minutes <= _MAX_MINUTES:
        raise ScheduleError(f"minutes {minutes} out of range 1..{_MAX_MINUTES}")
    return {"valve": valve, "start": start, "days": sorted(day_ints),
            "minutes": minutes, "enabled": enabled}


def _find_device(devices: list, mac: str) -> dict | None:
    want = (mac or "").upper()
    for d in devices:
        if isinstance(d, dict) and (d.get("mac") or "").upper() == want:
            return d
    return None


def read_schedules(path: str, mac: str) -> list[dict]:
    """The timer's schedules (empty list if none / timer absent). Read-only."""
    dev = _find_device(_load_config(path).get("devices", []), mac)
    return list(dev.get("schedules", [])) if dev else []


def write_schedules(path: str, mac: str, rules: list[dict]) -> dict:
    """Validate and persist `rules` for the timer with `mac`, atomically, preserving the
    rest of config (other devices, account). Raises ScheduleError on a bad rule or unknown
    MAC and writes nothing. Validation uses the timer's own station count."""
    config = _load_config(path)
    dev = _find_device(config.get("devices", []), mac)
    if dev is None:
        raise ScheduleError(f"no timer with MAC {mac} in config")
    stations = int(dev.get("stations") or 4)
    dev["schedules"] = [validate_rule(r, stations) for r in rules]   # all-or-nothing
    return _atomic_write_config(path, config)
