# Verification log

## HT34A-0001, firmware 0107 — Linux/BlueZ (ThinkPad)

**Offline:** `selftest_offline.py` — 11/11 byte-equivalence checks PASS
(vs real captured app frames: start, stop, set_time, time_string, CRC, cipher, trailer).

**Live (clean `cli.py selftest`), device-confirmed via its own replies:**
```
[1] set clock 03:33 -> read 03:33   PASS
[2] start zone 1    -> watering=True, 300s remaining   PASS
[3] stop            -> watering=False (idle)           PASS
```

**Reproducibility:** 3/3 consecutive autonomous runs all PASS, with the official
app untouched — confirming it is NOT dependent on app-priming.

**Why it works:** the device only honors a command sent after the full 9-message
arming sequence in the same connection (see README). Isolated commands are ignored.
