"""Disconnect calibration for the scan fatal-rule.

v2: only NEW error signatures are printed; "address absent" is treated as
an ALLOWLIST of (code=-10, fw in {7 IO_ERROR, 8 TIMEOUT}) — anything else
is transport-level and ends the session for the replug watch.

Usage: start it, unplug the dongle ~5 s in, replug ~5 s later.
"""
import time
from si104 import Device, I2cConfig, Error

WINDOW = 30
sig_counts = {}
sweeps = 0
start = time.time()

with Device(None) as dev:
    v = dev.request(0, 0x1F)
    if not (v and v[0]):
        dev.request(0, 0x1E, b"\x01\x00")
        print("VCC re-enabled", flush=True)

    dev.i2c_configure(1, I2cConfig(role=1, mode=0, slave_addr=0x42, timeout_ms=1000))
    for _ in range(40):
        raw = dev.status(1)
        if len(raw) >= 32 and raw[3]:
            break
        time.sleep(0.05)
    dev.i2c_configure(0, I2cConfig(role=0, mode=0, slave_addr=0x42, timeout_ms=1000))

    dev.i2c_slave_write(1, b"ACK!")
    rx = dev.i2c_transfer(0, 0x42, b"", 4)
    print("liveness precheck:", rx, flush=True)
    if rx != b"ACK!":
        raise SystemExit("bus not alive — abort before window")

    print("WINDOW OPEN (30s) — unplug now, replug in ~5 s", flush=True)
    while time.time() - start < WINDOW:
        try:
            for addr in range(0x08, 0x78):
                dev.i2c_transfer(0, addr, b"", 1)
            sweeps += 1
        except Error as e:
            sig = (e.code, e.firmware_status, e.backend_code)
            if sig not in sig_counts:
                sig_counts[sig] = 0
                print(
                    f"NEW signature [{time.time()-start:5.1f}s]: "
                    f"code={e.code} fw={e.firmware_status} backend={e.backend_code} "
                    f"unknown={e.completion_unknown} — {e}",
                    flush=True,
                )
            sig_counts[sig] += 1
            if not (e.code == -10 and e.firmware_status in (7, 8)):
                print("TRANSPORT-LEVEL signature -> leaving session", flush=True)
                break
            time.sleep(0.01)

print(f"clean sweeps: {sweeps}", flush=True)
for sig, n in sig_counts.items():
    print(f"  {sig} x{n}", flush=True)

print("replug watch (30s)...", flush=True)
deadline = time.time() + 30
while time.time() < deadline:
    try:
        devs = Device.enumerate()
        if devs:
            print("device back:", devs, flush=True)
            break
    except Exception:
        pass
    time.sleep(0.5)
else:
    print("device did not return within 30s", flush=True)
