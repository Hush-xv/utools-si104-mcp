"""I2C0<->I2C1 hardware loopback: slave on ch1, master on ch0, one USB session.

The MCP server is stateless (open/close per call), so master+slave sides of
the loopback must live inside a single session — this script is that session.
"""
import json
import struct
import sys
import time

from si104 import Device, I2cConfig

SERIAL = sys.argv[1] if len(sys.argv) > 1 else None


def i2c_status(dev, ch):
    raw = dev.status(interface=ch)  # I2C GET_STATUS lives on interfaces 0-3
    if len(raw) < 32:
        return {"raw_hex": raw.hex()}
    fields = struct.unpack("<4B I 2H 2I 2B 5H", raw[:32])
    names = (
        "role", "mode", "addr_10bit", "initialized", "bus_hz", "slave_addr",
        "timeout_ms", "req_count", "err_count", "slave_logic_mode",
        "reg_addr_width", "reg_ptr", "mailbox_rx_len", "mailbox_tx_len",
        "event_pending", "event_dropped",
    )
    d = dict(zip(names, fields))
    d["addr_10bit"] = bool(d["addr_10bit"])
    d["initialized"] = bool(d["initialized"])
    return d


with Device(SERIAL) as dev:
    print("ch1 status before:", json.dumps(i2c_status(dev, 1)))

    dev.i2c_configure(1, I2cConfig(role=1, mode=0, slave_addr=0x42, timeout_ms=1000))
    init, st = False, {}
    for _ in range(40):
        st = i2c_status(dev, 1)
        if st.get("initialized"):
            init = True
            break
        time.sleep(0.05)
    print("ch1 slave(0x42, raw) initialized:", init, json.dumps(st))
    if not init:
        raise SystemExit("slave channel never initialized — check wiring/pull-ups")

    dev.i2c_configure(0, I2cConfig(role=0, mode=0, timeout_ms=1000))

    # Shared VCC output is off by default (IG section 3); NO_POWER (status=11)
    # otherwise. enable=1, save=0 (not persisted).
    dev.request(0, 0x1E, b"\x01\x00")
    v = dev.request(0, 0x1F)
    print("VCC after enable:", v.hex())

    payload = b"hello-si104-loopback"
    dev.i2c_transfer(0, 0x42, payload, 0)  # master write -> slave rx buffer
    got = dev.i2c_slave_read(1, len(payload))
    ok1 = got == payload
    print("master->slave:", payload, "=>", got, "MATCH" if ok1 else "MISMATCH")

    reply = b"ok-from-slave"
    dev.i2c_slave_write(1, reply)
    back = dev.i2c_transfer(0, 0x42, b"", len(reply))  # master read <- slave tx buffer
    ok2 = back == payload[: len(reply)] or back == reply
    print("slave->master:", reply, "=>", back, "MATCH" if ok2 else "MISMATCH")

    print("ch0 status after:", json.dumps(i2c_status(dev, 0)))
    print("ch1 status after:", json.dumps(i2c_status(dev, 1)))
    print("LOOPBACK", "PASS" if ok1 and ok2 else "FAIL")
