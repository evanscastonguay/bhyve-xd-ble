"""Host-driven schedule engine — the PURE decision logic (no BLE, no I/O, no clock).

`due_rules(rules, now)` returns the rules that should fire at minute `now`. The server's
loop calls this each tick and, only if host scheduling is enabled and nothing else is using
the radio, fires the existing BLE start for each due rule. Keeping the decision pure makes
it deterministically testable with an injected `now`.

Days are 0=Mon..6=Sun (matching datetime.weekday()); `start` is 24h "HH:MM" in the host's
local time (host-driven = the Mac near the timer triggers at its own local clock).
"""
from __future__ import annotations


def due_rules(rules: list[dict], now) -> list[dict]:
    """Rules enabled, scheduled for now.weekday(), whose start == now's HH:MM."""
    hhmm = f"{now.hour:02d}:{now.minute:02d}"
    wd = now.weekday()
    out = []
    for r in rules or []:
        if not isinstance(r, dict) or r.get("enabled", True) is False:
            continue
        if r.get("start") == hhmm and wd in (r.get("days") or []):
            out.append(r)
    return out
