"""On-device schedule codec (P4b) — encode the Orbit IpcMsg commands that store a watering
program ON the timer so it runs autonomously, and parse the read-back.

Protocol confirmed by replay on hardware (SPIKE_schedule.md, P4a). A schedule is TWO writes:
  1) setProgramSchedule (IpcMsg field 19) — defines the program
  2) setActivePrograms  (IpcMsg field 20) — enables it (bitmask; bit0 = Program A)
Read back with getActivePrograms (field 77); its reply carries activeProgramFlags.

Fields are exactly as decoded from the app: startTimes are DEVICE-LOCAL minutes-from-midnight,
stationId is 0-INDEXED (valve N -> N-1), runTimeSec in seconds. This module is PURE (bytes in,
bytes out) — the BLE send/verify lives in the caller; that keeps it offline-testable.
"""
from __future__ import annotations

from bhyve_xd import _fb, _fv, _wrap, iter_fields

# IpcMsg field numbers (cross-validated + replay-proven)
_F_SET_PROGRAM = 19
_F_SET_ACTIVE = 20
_F_GET_ACTIVE = 77
# SetProgramSchedule sub-fields
_F_PROGRAM_ID = 1
_F_INTERVAL = 4          # programType oneof: Interval
_F_START_MINS = 8        # repeated uint32 (unpacked)
_F_STATION = 9           # repeated StationInfo
_F_BUDGET = 10
_F_NAME = 17
_F_BASIC = 19
# StationInfo
_F_STATION_ID = 1
_F_RUN_SEC = 2
# Interval
_F_INTERVAL_DAYS = 1

PROGRAM_A = 1            # ProgramId enum: manual=0, a=1..f=6


def encode_set_program_schedule(program_id: int, start_mins: list[int],
                                stations: list[tuple[int, int]], *,
                                interval_days: int = 1, budget_percent: int = 100,
                                name: str | None = None, basic_mode: bool = True) -> bytes:
    """Build the framed IpcMsg.setProgramSchedule (Interval/daily program).

    start_mins: device-local minutes-from-midnight (e.g. 360 = 06:00).
    stations:   list of (station_id_0indexed, run_time_sec).
    Returns the AA775A0F-framed message ready for session._send.
    """
    body = _fv(_F_PROGRAM_ID, program_id)
    body += _fb(_F_INTERVAL, _fv(_F_INTERVAL_DAYS, interval_days))     # programType = Interval
    for t in start_mins:
        body += _fv(_F_START_MINS, int(t))
    for station_id, run_sec in stations:
        body += _fb(_F_STATION, _fv(_F_STATION_ID, int(station_id)) + _fv(_F_RUN_SEC, int(run_sec)))
    body += _fv(_F_BUDGET, int(budget_percent))
    if name:
        body += _fb(_F_NAME, name.encode())
    if basic_mode:
        body += _fv(_F_BASIC, 1)
    return _wrap(_fb(_F_SET_PROGRAM, body))


def encode_set_active(flags: int) -> bytes:
    """Framed IpcMsg.setActivePrograms — bitmask of enabled programs (bit0 = Program A)."""
    return _wrap(_fb(_F_SET_ACTIVE, _fv(1, int(flags))))


def program_bit(program_id: int) -> int:
    """Bit for a program in the activeProgramFlags mask (Program A(id 1) -> bit0)."""
    return 1 << (program_id - 1)


def encode_get_active() -> bytes:
    """Framed IpcMsg.getActivePrograms (no args) — read-back request."""
    return _wrap(_fb(_F_GET_ACTIVE, b""))


MAX_PROGRAMS = 6            # ProgramId a..f


def program_from_rules(rules: list[dict]) -> dict:
    """Map the app's per-timer schedule rules to on-device Programs (the daily/interval subset).

    The device Program model is (shared start-times × station list × programType); our rules are
    per-(valve, time). The faithful mapping is **one Program per rule** (A..F), each a daily
    program with a single start time and one station. Returns:
      {"programs": [{program_id, start_mins:[m], stations:[(station_id0, run_sec)], enabled}],
       "active_mask": int,          # OR of program_bit for ENABLED rules
       "warnings": [str]}           # rules dropped (>6) or day-selection not honored (runs daily)

    station_id is 0-indexed (valve N -> N-1); start is device-local minutes-from-midnight;
    minutes -> run_sec. Day-of-week masks are NOT yet supported (dayFlags bit order unknown), so
    a rule with specific days is mapped as DAILY and a warning is recorded.
    """
    programs, warnings, mask = [], [], 0
    for i, r in enumerate(rules):
        if i >= MAX_PROGRAMS:
            warnings.append(f"only {MAX_PROGRAMS} on-device programs (A–F) are supported; "
                            f"rule #{i + 1} and beyond were not pushed")
            break
        hh, mm = str(r["start"]).split(":")
        start_min = int(hh) * 60 + int(mm)
        station = (int(r["valve"]) - 1, int(r["minutes"]) * 60)     # 0-indexed valve, seconds
        enabled = bool(r.get("enabled", True))
        pid = i + 1                                                  # A=1 .. F=6
        programs.append({"program_id": pid, "start_mins": [start_min],
                         "stations": [station], "enabled": enabled})
        if enabled:
            mask |= program_bit(pid)
        if set(r.get("days") or []) != set(range(7)):
            warnings.append(f"rule #{i + 1} runs on specific days; on-device currently runs it "
                            f"DAILY (weekday masks not supported yet)")
    return {"programs": programs, "active_mask": mask, "warnings": warnings}


def parse_active_flags(inner_msgs: list[bytes]) -> int | None:
    """From decoded reply protobufs, return activeProgramFlags. The device replies with the
    ActivePrograms under IpcMsg field 20 (observed) or 78; flags is sub-field 1."""
    for m in inner_msgs:
        for fn, wt, v in iter_fields(m):
            if fn in (20, 78) and wt == 2 and isinstance(v, (bytes, bytearray)):
                for sfn, _swt, sv in iter_fields(v):
                    if sfn == 1 and isinstance(sv, int):
                        return sv
    return None
