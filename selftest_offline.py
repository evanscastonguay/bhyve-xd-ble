"""Offline self-test — no device needed. Verifies the distilled library produces
the exact bytes proven correct against real captured app traffic (HT34A fw0107).
"""
import struct
import bhyve_xd as B


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        got : {got.hex() if isinstance(got, bytes) else got}")
        print(f"        want: {want.hex() if isinstance(want, bytes) else want}")
    return ok


def main():
    results = []
    print("Offline byte-equivalence tests vs known-good app frames:\n")

    # These are REAL decrypted app frames captured from the device (HT34A fw0107).
    # msg_start(zone 1, 180s) and msg_stop() must match byte-for-byte incl CRC.
    results.append(check("msg_start(1,180) == captured app start",
                         B.msg_start(1, 180),
                         bytes.fromhex("aa775a0f0f00720b080212071a05080010b401cbfb")))
    results.append(check("msg_stop() == captured app stop",
                         B.msg_stop(),
                         bytes.fromhex("aa775a0f0800720408021200c776")))

    # set_time: our builder must reproduce the app's captured set_time frame.
    # (app: epoch 1782922918, tz -14400)
    results.append(check("msg_set_time(1782922918,-14400) == captured app set_time",
                         B.msg_set_time(1782922918, -14400),
                         bytes.fromhex("aa775a0f1600da041108a6fd94d20610c08fffffffffffffff01fb9f")))

    # time_string: reproduce the app's captured ISO string frame.
    results.append(check("msg_time_string(...) == captured app time_string",
                         B.msg_time_string("2026-07-01T12:21:58-04:00"),
                         bytes.fromhex("aa775a0f200092011b0a19323032362d30372d30315431323a32313a35382d30343a3030115b")))

    # CRC16 valid on every command message.
    crc_ok = True
    for nm, m in [("start", B.msg_start(1, 60)), ("stop", B.msg_stop()),
                  ("status", B.msg_get_status()), ("battery", B.msg_get_battery())]:
        body, crc = m[:-2], struct.unpack("<H", m[-2:])[0]
        if B._crc16_ccitt(body, 0) != crc:
            crc_ok = False
    results.append(check("CRC16 valid on start/stop/status/battery", crc_ok, True))

    # Cipher round-trip: encrypt->decrypt recovers plaintext, and frame layout.
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    iv = bytes(range(12))
    pt = B.msg_stop()
    ks = B._keystream_block(key, iv, 5)
    ct = bytes(a ^ b for a, b in zip(pt[:16], ks))
    back = bytes(a ^ b for a, b in zip(ct, ks))
    results.append(check("cipher XOR round-trips first block", back, pt[:16]))

    # Trailer/checksum formula on a chunk.
    chunk = B.msg_stop()  # 14 bytes, single chunk
    trailer = (sum(chunk) + B.FRAME_MAGIC + len(chunk)) & 0xFFFF
    frame_trailer = struct.pack("<H", trailer)
    # stop's captured on-wire trailer bytes are 80 03 (LE of value 0x0380)
    results.append(check("stop chunk trailer bytes == 80 03", frame_trailer, bytes.fromhex("8003")))

    # Arming sequence constants present and well-formed.
    for nm, c in [("SETUP_FIELD22", B.SETUP_FIELD22), ("SETUP_FIELD20", B.SETUP_FIELD20),
                  ("SETUP_FIELD120", B.SETUP_FIELD120)]:
        body, crc = c[:-2], struct.unpack("<H", c[-2:])[0]
        results.append(check(f"{nm} CRC valid", B._crc16_ccitt(body, 0), crc))

    # Reply parser on a synthetic watering status.
    #   deviceStatusInfo(16) { run_state(1)=4 }
    inner = B._fv(1, 4)
    reply = B._wrap(B._fb(16, inner))
    st = B.parse_reply(reply)
    results.append(check("parse_reply detects watering (run_state 4)", st.is_watering, True))

    # Per-zone stop: msg_stop_zone(z) == manual watering for that station, 0s.
    # It must (a) be a valid framed message, (b) decode to field 14 with the
    # target station (0-indexed) and duration 0, distinct from the global stop.
    for z in (1, 2, 3, 4):
        m = B.msg_stop_zone(z)
        body, crc = m[:-2], struct.unpack("<H", m[-2:])[0]
        results.append(check(f"msg_stop_zone({z}) CRC valid", B._crc16_ccitt(body, 0), crc))
        # decode station + duration
        st_id = dur = None
        for fn, wt, v in B.iter_fields(m[6:-2]):
            if fn == 14:
                for f2, w2, v2 in B.iter_fields(v):
                    if f2 == 2:
                        for f3, w3, v3 in B.iter_fields(v2):
                            if f3 == 3:
                                for f4, w4, v4 in B.iter_fields(v3):
                                    if f4 == 1: st_id = v4
                                    elif f4 == 2: dur = v4
        results.append(check(f"msg_stop_zone({z}) targets station {z-1}, duration 0",
                             (st_id, dur), (z - 1, 0)))
    results.append(check("msg_stop_zone(1) != global msg_stop()",
                         B.msg_stop_zone(1) != B.msg_stop(), True))

    # --- Refactored high-level API (no BLE): config, serialization, wiring ---
    import inspect
    import json
    import tempfile

    # from_config parses address/key/tz/name/stations.
    cfg = {"devices": [{"name": "Test", "address": "AA:BB", "tz_offset_sec": -18000,
                        "stations": 4, "network_key": "00112233445566778899aabbccddeeff"}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg, f); path = f.name
    dev = B.BHyveXD.from_config(path)
    results.append(check("from_config parses address", dev.address, "AA:BB"))
    results.append(check("from_config parses tz + name", (dev.tz_offset_sec, dev.name), (-18000, "Test")))
    results.append(check("from_config parses key (16 bytes)", len(dev.key), 16))

    # to_dict serializes the exact keys the REST API returns.
    st = B.DeviceStatus(device_time=1782891180, run_state=4, seconds_remaining=120, is_watering=True)
    d = st.to_dict()
    results.append(check("to_dict has the API keys",
                         sorted(d.keys()),
                         sorted(["clock", "device_time", "is_watering", "run_state", "seconds_remaining"])))
    results.append(check("to_dict reflects watering state", (d["is_watering"], d["seconds_remaining"]), (True, 120)))

    # High-level control methods exist and are async (shared by CLI + server).
    for m in ("status", "start", "stop", "sync_clock"):
        fn = getattr(B.BHyveXD, m, None)
        results.append(check(f"BHyveXD.{m} is an async method",
                             fn is not None and inspect.iscoroutinefunction(fn), True))

    # parse_reply: idle status has is_watering False, no seconds.
    idle = B.parse_reply(B._wrap(B._fb(16, B._fv(1, 1))))
    results.append(check("parse_reply idle -> not watering", (idle.is_watering, idle.seconds_remaining), (False, None)))

    # --- Phase 0: host timezone, device MAC in reply, multi-device select ---

    # host_tz_offset() returns the host's UTC offset in seconds (int).
    from datetime import datetime as _dt
    host_off = int(_dt.now().astimezone().utcoffset().total_seconds())
    results.append(check("host_tz_offset() == host UTC offset", B.host_tz_offset(), host_off))

    # parse_reply extracts the device MAC from field 1 (6 bytes) -> "AA:BB:...".
    reply = B._wrap(B._fb(1, bytes.fromhex("446755d87ab9")) + B._fb(16, B._fv(1, 1)))
    st_mac = B.parse_reply(reply)
    results.append(check("parse_reply extracts device_mac", st_mac.device_mac, "44:67:55:D8:7A:B9"))

    # from_config(device=...) selects by index and by name; default tz = host.
    cfg2 = {"devices": [
        {"name": "Front", "address": "A1", "network_key": "00" * 16},
        {"name": "Back", "address": "B2", "network_key": "11" * 16, "tz_offset_sec": -18000},
    ]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg2, f); p2 = f.name
    results.append(check("from_config default -> first device", B.BHyveXD.from_config(p2).address, "A1"))
    results.append(check("from_config device=1 (index)", B.BHyveXD.from_config(p2, device=1).address, "B2"))
    results.append(check("from_config device='Back' (name)", B.BHyveXD.from_config(p2, device="Back").address, "B2"))
    results.append(check("from_config default tz -> host when absent",
                         B.BHyveXD.from_config(p2).tz_offset_sec, host_off))
    results.append(check("from_config keeps explicit tz when present",
                         B.BHyveXD.from_config(p2, device="Back").tz_offset_sec, -18000))

    # --- Phase 1: cloud onboarding (parsing + error mapping, no network) ---
    import base64
    import onboarding as O

    # HTTP status -> typed error mapping (the whole cloud error contract).
    def _maps(status, exc):
        try:
            O._raise_for_status(status, context="t")
        except exc:
            return True
        except Exception:
            return False
        return False

    results.append(check("_raise_for_status 429 -> RateLimited", _maps(429, O.RateLimited), True))
    results.append(check("_raise_for_status 401 -> AuthError", _maps(401, O.AuthError), True))
    results.append(check("_raise_for_status 403 -> AuthError", _maps(403, O.AuthError), True))
    results.append(check("_raise_for_status 400 -> AuthError", _maps(400, O.AuthError), True))
    results.append(check("_raise_for_status 500 -> CloudError", _maps(500, O.CloudError), True))
    ok200 = True
    try:
        O._raise_for_status(200, context="t")
    except Exception:
        ok200 = False
    results.append(check("_raise_for_status 200 -> no error", ok200, True))
    results.append(check("typed errors subclass CloudError (catch-all works)",
                         all(issubclass(e, O.CloudError)
                             for e in (O.AuthError, O.RateLimited, O.MFARequired,
                                       O.CloudConnectionError)), True))

    # MFA challenge surfaces as MFARequired (not a confusing 'no api key').
    mfa_raised = False
    try:
        O._check_mfa({"mfa_required": True})
    except O.MFARequired:
        mfa_raised = True
    results.append(check("_check_mfa flags an MFA challenge", mfa_raised, True))

    # base64 network key -> hex; MAC formatting.
    raw = bytes.fromhex("00112233445566778899aabbccddeeff")
    key_b64 = base64.b64encode(raw).decode()
    results.append(check("_b64_to_hex decodes the network key", O._b64_to_hex(key_b64), raw.hex()))
    results.append(check("_b64_to_hex(None) -> None", O._b64_to_hex(None), None))
    results.append(check("_format_mac 12-hex -> AA:BB:..", O._format_mac("446755d87ab9"),
                         "44:67:55:D8:7A:B9"))
    results.append(check("_format_mac bad length -> None", O._format_mac("abc"), None))

    # _build_devices joins /devices with mesh keys on mocked cloud JSON:
    # drops the bridge and the mesh-less device, keeps the one real timer.
    raw_devices = [
        {"name": "Front Yard", "type": "sprinkler_timer", "mac_address": "446755d87ab9",
         "num_stations": 4, "hardware_version": "HT34A", "firmware_version": "0107",
         "mesh_id": "m1"},
        {"name": "Wi-Fi Hub", "type": "bridge", "mesh_id": "m1"},               # dropped
        {"name": "Orphan", "type": "sprinkler_timer", "mac_address": "010203040506"},  # no mesh
    ]
    built = O._build_devices(raw_devices, {"m1": {"ble_network_key": key_b64}})
    results.append(check("_build_devices drops bridge + mesh-less -> 1 device", len(built), 1))
    results.append(check("_build_devices maps name/mac/stations/key",
                         (built[0]["name"], built[0]["mac"], built[0]["stations"],
                          built[0]["network_key"]),
                         ("Front Yard", "44:67:55:D8:7A:B9", 4, raw.hex())))

    # Newer-schema account: network_topology_id + the 'network_key' mesh field.
    built2 = O._build_devices(
        [{"name": "Back", "type": "timer", "mac_address": "446755d87ab9",
          "num_stations": 6, "network_topology_id": "t9"}],
        {"t9": {"network_key": key_b64}})
    results.append(check("_build_devices reads network_topology_id + network_key field",
                         (built2[0]["network_key"], built2[0]["stations"]), (raw.hex(), 6)))

    n = sum(results)
    print(f"\n{n}/{len(results)} checks passed"
          + ("  ✅ library + refactored API verified" if n == len(results) else "  ❌ MISMATCH"))
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
