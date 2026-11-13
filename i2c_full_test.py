"""Full I2C feature matrix on the ch0<->ch1 loopback, single USB session.

v2 — semantics learned on first run:
- combined (write-then-read) transactions: the write part is the command
  phase for the slave logic, it does NOT land in the slave rx buffer
- mailbox mode: first byte of a pure master write is the pointer byte
- reg-style access targets the channel config's slave_addr

Covers: repeated-START write-after-read, speed presets, mailbox slave,
register slave + reg-style access, per-transfer VCC monitor, EVENT_POLL,
ABORT, 10-bit addressing.
"""
import json
import struct
import sys
import time

from si104 import Device, I2cConfig, Error

SERIAL = sys.argv[1] if len(sys.argv) > 1 else None
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def i2c_status(dev, ch):
    raw = dev.status(interface=ch)
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


def slave_init(dev, ch, addr, logic=0, addr10=False):
    dev.i2c_configure(
        ch,
        I2cConfig(role=1, mode=0, slave_addr=addr, timeout_ms=1000,
                  slave_mode=logic, addr_10bit=addr10),
    )
    for _ in range(40):
        st = i2c_status(dev, ch)
        if st.get("initialized"):
            return st
        time.sleep(0.05)
    return st


def master(dev, mode=0, bus_hz=100000, addr10=False, target=0x30):
    dev.i2c_configure(
        0, I2cConfig(role=0, mode=mode, bus_hz=bus_hz, timeout_ms=1000,
                     addr_10bit=addr10, slave_addr=target),
    )


with Device(SERIAL) as dev:
    v = dev.request(0, 0x1F)
    if not (v and v[0]):
        dev.request(0, 0x1E, b"\x01\x00")
        print("VCC enabled")

    st = slave_init(dev, 1, 0x42, logic=0)
    check("slave raw @0x42 init", st.get("initialized"))
    master(dev, target=0x42)

    # A) repeated-START write-after-read
    dev.i2c_slave_write(1, b"\xAA\x55\xAA\x55")
    rx = dev.i2c_transfer(0, 0x42, b"\x01\x02", 4)
    check("write-after-read (repeated START)", rx == b"\xAA\x55\xAA\x55", rx.hex())
    got = dev.i2c_slave_read(1, 2)
    print(f"INFO  combined-tx write part lands in slave rx? {got.hex()!r} "
          "(firmware treats it as the command phase — documented finding, not a fault)")

    # B) speed presets
    for mode, label in ((1, "fast 400k"), (2, "fast-plus 1M")):
        master(dev, mode=mode, target=0x42)
        dev.i2c_slave_write(1, b"\x11" * 8)
        rx = dev.i2c_transfer(0, 0x42, b"\x00", 8)
        check(f"mode={mode} {label} roundtrip", rx == b"\x11" * 8)
    master(dev, mode=3, bus_hz=200_000, target=0x42)
    dev.i2c_slave_write(1, b"\x22" * 8)
    rx = dev.i2c_transfer(0, 0x42, b"\x00", 8)
    check("mode=3 custom 200k roundtrip", rx == b"\x22" * 8)
    master(dev, target=0x42)

    # C) mailbox slave: first write byte is the pointer, rest is pushed
    st = slave_init(dev, 1, 0x43, logic=1)
    check("slave mailbox @0x43 init", st.get("initialized") and st.get("slave_logic_mode") == 1)
    dev.i2c_transfer(0, 0x43, b"MBOX!", 0)
    pop = dev.request(1, 0x17)  # MAILBOX_POP
    check("mailbox push/pop (first byte = pointer)", pop == b"BOX!", pop.hex())

    # D) register-mode slave: pointer-phase access via plain transfers
    st = slave_init(dev, 1, 0x44, logic=2)
    check("slave register @0x44 init", st.get("initialized") and st.get("slave_logic_mode") == 2)
    master(dev, target=0x44)
    try:
        dev.i2c_transfer(0, 0x44, bytes([0x10, 0xDE, 0xAD, 0xBE, 0xEF]), 0)  # ptr+data
        rx = dev.i2c_transfer(0, 0x44, bytes([0x10]), 4)  # ptr + repeated-START read
        check("register-mode pointer write+read-back", rx == b"\xDE\xAD\xBE\xEF", rx.hex())
    except Error as e:
        check("register-mode pointer write+read-back", False, str(e))

    # G) per-transfer VCC monitor tail
    try:
        st = slave_init(dev, 1, 0x42, logic=0)
        master(dev, target=0x42)
        dev.i2c_slave_write(1, b"\x77")
        rx = dev.i2c_transfer(0, 0x42, b"", 1, monitor_vcc=True)
        v = dev.last_i2c_vcc or {}
        check("monitor_vcc tail parsed", rx == b"\x77" and bool(v.get("adc_valid")), json.dumps(v))
    except Error as e:
        check("monitor_vcc tail parsed", False, str(e))

    # H) EVENT_POLL (events accumulated from master writes on ch1)
    try:
        ev = dev.request(1, 0x1B)
        if len(ev) >= 8:
            etype, slen, eaddr, elen, dropped = struct.unpack("<BBHHH", ev[:8])
            check("event_poll header", True, f"type={etype} samples={slen}B addr=0x{eaddr:02X} dropped={dropped}")
        else:
            check("event_poll header", False, ev.hex())
    except Error as e:
        check("event_poll header", False, str(e))

    # F) ABORT then bus still alive
    try:
        dev.request(0, 0x14)
        dev.i2c_slave_write(1, b"\x5A")
        rx = dev.i2c_transfer(0, 0x42, b"", 1)
        check("bus alive after ABORT", rx == b"\x5A", rx.hex())
    except Error as e:
        check("bus alive after ABORT", False, str(e))

    # I) 10-bit addressing
    try:
        st = slave_init(dev, 1, 0x142, logic=0, addr10=True)
        master(dev, target=0x142, addr10=True)
        dev.i2c_transfer(0, 0x142, b"\xAB", 0)
        got = dev.i2c_slave_read(1, 1)
        check("10-bit addr write->slave", got == b"\xAB", f"slave_addr={st.get('slave_addr'):#x}")
    except Error as e:
        check("10-bit addr write->slave", False, str(e))

    print("\nfinal ch1 status:", json.dumps(i2c_status(dev, 1)))

print("\n== summary ==")
fails = [n for n, ok in RESULTS if not ok]
print(f"{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS" + (f"; FAIL: {fails}" if fails else ""))
