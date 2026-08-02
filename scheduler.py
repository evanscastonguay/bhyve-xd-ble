"""Host-driven schedule engine — the PURE decision logic (no BLE, no I/O, no clock).

`due_rules(rules, now)` returns the rules that should fire at minute `now`. The server's
loop calls this each tick and, only if host scheduling is enabled and nothing else is using
the radio, fires the existing BLE start for each due rule. Keeping the decision pure makes
it deterministically testable with an injected `now`.

Days are 0=Mon..6=Sun (matching datetime.weekday()); `start` is 24h "HH:MM" in the host's
local time (host-driven = the Mac near the timer triggers at its own local clock).
"""
from __future__ import annotations


def due_rules(rules: list[dict], now, grace_min: int = 0) -> list[dict]:
    """Rules enabled, scheduled for now.weekday(), whose start time is at (or up to `grace_min`
    minutes before) now. grace_min>0 is a BOUNDED catch-up so a rule isn't lost to a slow tick or
    a just-started server — but never fires hours late. Same-day only (no midnight wrap)."""
    now_min = now.hour * 60 + now.minute
    wd = now.weekday()
    out = []
    for r in rules or []:
        if not isinstance(r, dict) or r.get("enabled", True) is False:
            continue
        try:
            hh, mm = str(r["start"]).split(":")
            start_min = int(hh) * 60 + int(mm)
        except (KeyError, ValueError):
            continue
        if wd in (r.get("days") or []) and 0 <= now_min - start_min <= grace_min:
            out.append(r)
    return out
