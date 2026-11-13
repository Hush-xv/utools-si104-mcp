"""Stress + boundary suite on the bench loopbacks (I2C ch0<->ch1, SPI D0<->D1).

Boundary: exact-fit payloads at every documented cap (504 tx / 512 rx /
506 rx with VCC monitor / 1024 words), plus one-over rejections, plus
speed extremes (50k / 40M).
Stress: sustained transaction counts, USB open/close churn, long-TX runs.
Timing and pass rates are reported; no assertion aborts the run.
"""
import json
import struct
import time

from si104 import Device, I2cConfig, SpiConfig, Error

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def i2c_status(dev, ch):
    raw = dev.status(interface=ch)
    f = struct.unpack("<4B I 2H 2I 2B 5H", raw[:32])
    names = ("role", "mode", "addr_10bit", "initialized", "bus_hz", "slave_addr",
             "timeout_ms", "req_count", "err_count", "slave_logic_mode",
             "reg_addr_width", "reg_ptr", "mailbox_rx_len", "mailbox_tx_len",
             "event_pending", "event_dropped")
    d = dict(zip(names, f))
    return d


with Device(None) as dev:
    # ---- setup: register slave on ch1, master on ch0
    v = dev.request(0, 0x1F)
    if not (v and v[0]):
        dev.request(0, 0x1E, b"\x01\x00")
    dev.i2c_configure(1, I2cConfig(role=1, mode=0, slave_addr=0x45, timeout_ms=1000, slave_mode=2))
    for _ in range(40):
        if i2c_status(dev, 1).get("initialized"):
            break
        time.sleep(0.05)
    dev.i2c_configure(0, I2cConfig(role=0, mode=0, slave_addr=0x45, timeout_ms=1000))
    dev.spi_configure(SpiConfig(data_bits=8, sclk_hz=1_000_000))

    # ================= boundary =================
    # B1: I2C master write exact fit 504 B (1 pointer + 503 data)
    payload = bytes([0x00]) + bytes((i * 7 + 1) & 0xFF for i in range(503))
    try:
        dev.i2c_transfer(0, 0x45, payload, 0)
        check("B1 i2c tx exact 504B accepted", True)
    except Error as e:
        check("B1 i2c tx exact 504B accepted", False, str(e))

    # B2: I2C read exact 512 B back from the 8KB mirror — auto-increment is
    # page-scoped (256B pages, TM section 8): the pointer write lands only up
    # to the page end, the rest of the mirror stays zero.
    try:
        rx = dev.i2c_transfer(0, 0x45, bytes([0x00]), 512)
        mismatch = next((i for i in range(503) if rx[i] != payload[i + 1]), None)
        check("B2 mirror auto-increment page-scoped (stops at 256)", mismatch == 256,
              f"first_mismatch={mismatch}")
    except Error as e:
        check("B2 mirror auto-increment page-scoped (stops at 256)", False, str(e))

    # B3: VCC-monitored rx exact 506 B
    try:
        rx = dev.i2c_transfer(0, 0x45, bytes([0x00]), 506, monitor_vcc=True)
        v = dev.last_i2c_vcc or {}
        check("B3 monitor_vcc rx exact 506B", rx[:4] == payload[1:5] and bool(v.get("adc_valid")),
              f"adc={v.get('adc_mv')}")
    except Error as e:
        check("B3 monitor_vcc rx exact 506B", False, str(e))

    # B4: VCC-monitored rx 507 B — firmware should reject (doc cap 506)
    try:
        dev.i2c_transfer(0, 0x45, bytes([0x00]), 507, monitor_vcc=True)
        check("B4 monitor_vcc rx 507B rejected by firmware", False, "accepted?! doc says 506")
    except Error as e:
        check("B4 monitor_vcc rx 507B rejected by firmware", True, str(e)[:60])

    # B5: I2C tx 505 B — firmware should reject (doc cap 504)
    try:
        dev.i2c_transfer(0, 0x45, bytes([0x00]) + bytes(504), 0)
        check("B5 i2c tx 505B rejected by firmware", False, "accepted?! doc says 504")
    except Error as e:
        check("B5 i2c tx 505B rejected by firmware", True, str(e)[:60])

    # B6: SPI tx exact 504 B + rx exact 512 B full-duplex
    tx = bytes((i * 13 + 5) & 0xFF for i in range(504))
    try:
        rx = dev.spi_transfer(tx, 512)
        check("B6 spi tx 504B + rx 512B exact fit", rx[:504] == tx, f"tail={rx[500:508].hex()}")
    except Error as e:
        check("B6 spi tx 504B + rx 512B exact fit", False, str(e))

    # B7: SPI tx 505 B — firmware silently routes >504B TX-only transfers to
    # the long-TX path (doc 504 is the single-command path cap)
    try:
        dev.spi_transfer(bytes(505), 0)
        check("B7 spi 505B TX-only auto-routed to long-TX", True, "firmware behavior")
    except Error as e:
        check("B7 spi 505B TX-only auto-routed to long-TX", False, str(e)[:60])
    try:
        dev.spi_transfer(bytes(505), 4)
        check("B7b spi 505B with rx rejected (long-TX is TX-only)", False, "accepted?!")
    except Error as e:
        check("B7b spi 505B with rx rejected (long-TX is TX-only)", True, str(e)[:60])

    # B8: long TX exact 1024 words (8-bit)
    try:
        dev.spi_configure(SpiConfig(data_bits=8, sclk_hz=1_000_000))
        dev.spi_tx_words(((i * 37 + 11) & 0xFF for i in range(1024)))
        check("B8 long-TX exact 1024 words", True)
    except Error as e:
        check("B8 long-TX exact 1024 words", False, str(e))

    # B9: long TX 1025 words — binding should reject (1..1024)
    try:
        dev.spi_tx_words((0 for _ in range(1025)))
        check("B9 long-TX 1025 words rejected", False, "accepted?!")
    except (Error, ValueError) as e:
        check("B9 long-TX 1025 words rejected", True, str(e)[:60])

    # B10: speed extremes roundtrip
    for hz in (50_000, 40_000_000):
        try:
            dev.spi_configure(SpiConfig(data_bits=8, sclk_hz=hz))
            pat = bytes(range(32))
            rx = dev.spi_transfer(pat, 32)
            check(f"B10 spi roundtrip @ {hz} Hz", rx == pat, "jumper wiring may cap signal integrity")
        except Error as e:
            check(f"B10 spi roundtrip @ {hz} Hz", False, str(e)[:60])
    dev.spi_configure(SpiConfig(data_bits=8, sclk_hz=1_000_000))

    # ================= stress =================
    # S1: 1000 I2C transactions (500 register write+read pairs)
    t0, bad = time.time(), 0
    for i in range(500):
        data = bytes([i & 0xFF, (i >> 8) & 0xFF, 0xA5, 0x5A])
        dev.i2c_transfer(0, 0x45, bytes([0x30]) + data, 0)
        rx = dev.i2c_transfer(0, 0x45, bytes([0x30]), 4)
        if rx != data:
            bad += 1
    dt = time.time() - t0
    check("S1 i2c 1000-txn integrity", bad == 0, f"bad={bad} {dt:.1f}s ({1000/dt:.0f} txn/s)")

    # S2: 300 SPI full-duplex 64B roundtrips
    t0, bad = time.time(), 0
    pat = bytes((i * 29 + 3) & 0xFF for i in range(64))
    for i in range(300):
        rx = dev.spi_transfer(pat, 64)
        if rx != pat:
            bad += 1
    dt = time.time() - t0
    check("S2 spi 300x64B integrity", bad == 0, f"bad={bad} {dt:.1f}s ({300/dt:.0f} txn/s)")

    # S3: 20x long-TX 1024 words
    t0, bad = time.time(), 0
    for i in range(20):
        try:
            dev.spi_tx_words(((i + j) & 0xFF for j in range(1024)))
        except Error:
            bad += 1
    dt = time.time() - t0
    check("S3 20x long-TX 1024 words", bad == 0, f"bad={bad} {dt:.1f}s")

    # error counters after everything
    st0, st1 = i2c_status(dev, 0), i2c_status(dev, 1)
    print(f"ch0 err_count={st0['err_count']} req_count={st0['req_count']}", flush=True)
    print(f"ch1 err_count={st1['err_count']} req_count={st1['req_count']}", flush=True)

# S4 runs OUTSIDE the long-lived session: a second handle on the same USB
# device conflicts with the outer one (the MCP server never holds two).
t0, bad = time.time(), 0
for i in range(300):
    try:
        with Device(None) as d:
            d.info()
    except (Error, RuntimeError) as e:
        if i == 0:
            print("  first failure:", str(e)[:80], flush=True)
        bad += 1
dt = time.time() - t0
check("S4 300x open/close churn", bad == 0, f"bad={bad} {dt:.1f}s ({300/dt:.0f} open/s)")

print("\n== summary ==")
fails = [n for n, ok in RESULTS if not ok]
print(f"{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS" + (f"; FAIL: {fails}" if fails else ""))
