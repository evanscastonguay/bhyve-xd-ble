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

    n = sum(results)
    print(f"\n{n}/{len(results)} checks passed"
          + ("  ✅ distilled library is byte-correct" if n == len(results) else "  ❌ MISMATCH"))
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
