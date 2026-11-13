"""UTools-SI104 MCP server — stdio, stateless.

Each tool call is atomic: open -> operate -> close. The dongle is held only
for the duration of one call (milliseconds), so the uTools GUI app can still
use it between AI calls. Designed for same-time single-client usage.
"""
from __future__ import annotations

import itertools
import logging
import struct
import sys
import threading
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from si104 import Device, SpiConfig, I2cConfig, Error

# Logging goes to stderr; stdout is reserved for the MCP protocol on stdio.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
LOG = logging.getLogger("si104-mcp")

mcp = FastMCP(
    "si104",
    instructions=(
        "UTools-SI104 USB-to-SPI/I2C adapter server. Binary data crosses the "
        "tool boundary as lowercase hex strings without separators. serial=None "
        "targets the first dongle found; run si104_enumerate when unsure. Every "
        "call opens and closes the device atomically. Payload caps: I2C master "
        "TX 504 B, RX 512 B (506 with VCC monitor), register read 256 B, "
        "register write 508 B, SPI 512 B; larger transfers must be chunked. "
        "Device errors come back as a dict with code/backend_code/"
        "firmware_status; if completion_unknown is true the transfer may have "
        "hit the bus — verify device state before re-sending. Run "
        "si104_enumerate first if a call says the device is missing."
    ),
)

# 512: hard cap enforced by the SDK python binding (_integer(x, 512)).
MAX_PAYLOAD = 512
# i2c_scan probe timeout: NACK fails immediately, the timeout only guards
# against clock-stretch hangs. Keeps a full 0x08..0x77 sweep under ~10 s.
PROBE_TIMEOUT_MS = 50

# Manual LM-SI104-TM-001 §7.2: SPI_TX_STATUS reply is BB5HI (16 bytes).
_SPI_TX_STATE = {
    0: "idle",
    1: "uploading",
    2: "committable",
    3: "done_ok",
    4: "done_error",
    5: "cancelled_or_expired",
}


def _decode_spi_tx_status(raw):
    if len(raw) < 16:
        return {"raw_hex": raw.hex()}
    state, _r, total, received, begin_seq, commit_seq, result, session = struct.unpack(
        "<2B5HI", raw[:16]
    )
    return {
        "state": _SPI_TX_STATE.get(state, state),
        "total_bytes": total,
        "received_bytes": received,
        "begin_seq": begin_seq,
        "commit_seq": commit_seq,
        "result": result,
        "session_id": session,
    }


def _decode_i2c_status(raw):
    """Manual §8 GET_STATUS: u8 role/mode/addr_10bit/initialized, u32 bus_hz,
    u16 slave_addr/timeout_ms, u32 req_count/err_count, u8 slave_logic_mode/
    reg_addr_width, u16 reg_ptr/mailbox_rx_len/mailbox_tx_len/event_pending/
    event_dropped — 32 bytes total."""
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


def _parse_hex(value, name):
    """Accept '', None, 'deadbeef', '0xDEAD BEEF' -> bytes. ValueError on bad hex."""
    if value is None:
        return b""
    cleaned = "".join(str(value).strip().removeprefix("0x").removeprefix("0X").split())
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError(f"{name} is not valid hex: {value!r}") from None


def _check_payload(name, size):
    if size > MAX_PAYLOAD:
        raise ValueError(
            f"{name} is {size} bytes; one call carries at most 512 bytes, split into chunks"
        )


def _check_i2c_args(channel, address, timeout_ms, bus_hz, mode, addr_10bit=False):
    """Entry validation per TM section 4/8: channels 0-3, 7/10-bit address
    range, u16 timeout (0 = firmware default 1000 ms), custom-mode speed."""
    if not 0 <= channel <= 3:
        raise ValueError(f"channel must be 0..3 (TM section 4), got {channel}")
    if not 0 <= address <= (1023 if addr_10bit else 127):
        raise ValueError(
            f"address out of range for {'10' if addr_10bit else '7'}-bit addressing, got {address}"
        )
    if not 0 <= timeout_ms <= 65535:
        raise ValueError(
            f"timeout_ms must be 0..65535 (u16; 0 = firmware default 1000 ms), got {timeout_ms}"
        )
    if mode == 3 and not 10_000 <= bus_hz <= 1_000_000:
        raise ValueError(
            f"custom-mode bus_hz must be 10_000..1_000_000 (TM section 4), got {bus_hz}"
        )


# Per-address absence is an ALLOWLIST, not a blocklist: only firmware NACK
# (status=7 IO_ERROR) and probe timeout (status=8 TIMEOUT) count as "nobody
# at this address" — verified across ~18k live-bus probes. Any other
# signature (USB disconnect, busy, ...) aborts the scan; that stays safe
# even for transport signatures never yet observed.
_SCAN_ABSENT = ((-10, 7), (-10, 8))


# Per-call correlation id. FastMCP runs each sync tool in one worker thread,
# so entry (_log_call) and exit (_with_device) see the same thread-local id.
_SEQ = itertools.count(1)
_CALL = threading.local()


def _log_call(name, **params):
    # Log sizes only, never payload bytes.
    cid = next(_SEQ)
    _CALL.cid = cid
    body = ", ".join(f"{k}={v!r}" for k, v in params.items())
    LOG.info("#%d %s(%s)", cid, name, body)
    return cid


def _log_result(cid, name, result):
    if isinstance(result, dict) and "error" in result:
        LOG.warning("#%d %s failed: %s", cid, name, result["error"])
    else:
        LOG.info("#%d %s -> ok", cid, name)


def _with_device(serial, operate, name):
    """Open the dongle, run operate(dev), close. Device Error -> structured dict.

    Log classes: [param] bad input reaching the SDK layer, [device] structured
    dongle error, [internal] unexpected python-side fault.
    """
    cid = getattr(_CALL, "cid", None) or next(_SEQ)
    try:
        with Device(serial or None) as dev:
            result = operate(dev)
    except Error as e:
        LOG.exception("#%d [device] error in %s", cid, name)
        result = {
            "error": str(e),
            "code": e.code,
            "backend_code": e.backend_code,
            "firmware_status": e.firmware_status,
            "completion_unknown": e.completion_unknown,
            "hint": "run si104_enumerate to check the dongle is present",
        }
        if e.completion_unknown:
            result["retry_hint"] = (
                "completion_unknown=true: the transfer may have hit the bus; "
                "verify device state before re-sending (SDK Guide section 8)"
            )
        if e.firmware_status == 11:  # TM section 5: NO_POWER
            result["hint"] = (
                "NO_POWER: the shared VCC output is off. Enable it once per "
                "power-up with si104_raw_request(interface=I2C channel, "
                "command=0x1E, payload_hex='0100'), then retry"
            )
    except ValueError as e:
        LOG.warning("#%d [param] %s: %s", cid, name, e)
        raise
    except Exception:
        LOG.exception("#%d [internal] fault in %s", cid, name)
        raise
    _log_result(cid, name, result)
    return result


# ---------------------------------------------------------------- diagnostics


@mcp.tool()
def si104_enumerate() -> dict:
    """List SI104 dongles over USB. Returns {count, devices:[{vid,pid,bus,address,serial}]}. An empty list means nothing is plugged in."""
    devices = Device.enumerate()
    LOG.info("si104_enumerate -> %d device(s)", len(devices))
    return {"count": len(devices), "devices": devices}


@mcp.tool()
def si104_device_info(serial: Optional[str] = None, channel: int = 0) -> dict:
    """Read-only diagnostics: identity/firmware/runtime plus per-face capabilities, limits and the decoded 32-byte I2C channel status.

    The dongle exposes functional USB interfaces (TM section 1): 0-3 = I2C
    channels, 4 = SPI, 5 = maintenance. info/version/runtime are device-wide
    (queried on the SPI face); capabilities/limits come back for both the
    SPI face and the selected I2C channel; status is the 32-byte I2C
    GET_STATUS of that channel, decoded per TM section 8.
    """
    _log_call("si104_device_info", serial=serial, channel=channel)
    if not 0 <= channel <= 3:
        raise ValueError(f"channel must be 0..3, got {channel}")

    def op(dev):
        out = {}

        def put(key, fn):
            try:
                out[key] = fn()
            except (Error, struct.error, RuntimeError) as e:
                out[key] = {"error": str(e)}  # keep the rest of the diagnostics

        put("info", dev.info)
        put("version_hex", lambda: dev.version().hex() or "(empty)")
        put("runtime", lambda: list(dev.runtime()))
        put("spi", lambda: {"capabilities": dev.capabilities(4), "limits": dev.limits(4)})
        put(
            "i2c",
            lambda: {
                "channel": channel,
                "capabilities": dev.capabilities(channel),
                "limits": dev.limits(channel),
                "status": _decode_i2c_status(dev.status(channel)),
            },
        )
        return out

    return _with_device(serial, op, "si104_device_info")


# ---------------------------------------------------------------------- SPI


@mcp.tool()
def si104_spi_transfer(
    serial: Optional[str] = None,
    tx_hex: str = "",
    rx_size: int = 0,
    sclk_hz: int = 1_000_000,
    data_bits: int = 8,
    cpol: int = 0,
    cpha: int = 0,
    lsb_first: bool = False,
    cs_active_high: bool = False,
    timeout_ms: int = 1000,
) -> dict:
    """One atomic SPI transaction: configure bus, full-duplex transfer, close.

    tx_hex is clocked out; rx_size bytes are clocked in during the same
    transaction (lengths are independent: TX-only = rx_size 0, RX-only =
    empty tx_hex). tx is at most 512 bytes and rx_size at most 512.
    Typical: data_bits 8/16/32, cpol/cpha 0 or 1, sclk_hz 1_000..10_000_000.
    """
    _log_call(
        "si104_spi_transfer",
        serial=serial,
        tx_bytes=len(tx_hex) // 2,
        rx_size=rx_size,
        sclk_hz=sclk_hz,
        data_bits=data_bits,
        cpol=cpol,
        cpha=cpha,
    )
    tx = _parse_hex(tx_hex, "tx_hex")
    # Caps per Integration Guide section 3: normal SPI TX <= 504 B, RX <= 512 B.
    if len(tx) > 504:
        raise ValueError(f"tx_hex is {len(tx)} bytes; SPI normal transfer caps at 504 bytes per call")
    step = data_bits // 8
    if data_bits > 8 and (len(tx) % step or rx_size % step):
        raise ValueError(
            f"data_bits={data_bits}: tx length and rx_size must be multiples of {step} "
            "(complete words required, TM section 7)"
        )
    if not 0 <= rx_size <= MAX_PAYLOAD:
        raise ValueError(f"rx_size must be 0..512, got {rx_size}")
    if sclk_hz <= 0:
        raise ValueError(f"sclk_hz must be positive, got {sclk_hz}")

    def op(dev):
        dev.spi_configure(
            SpiConfig(
                data_bits=data_bits,
                sclk_hz=sclk_hz,
                timeout_ms=timeout_ms,
                cpol=cpol,
                cpha=cpha,
                lsb_first=lsb_first,
                cs_active_high=cs_active_high,
            )
        )
        rx = dev.spi_transfer(tx, rx_size)
        return {"rx_hex": rx.hex(), "rx_bytes": len(rx)}

    return _with_device(serial, op, "si104_spi_transfer")


# ---------------------------------------------------------------------- I2C


def _i2c_config(address, bus_hz, timeout_ms, mode=0, addr_10bit=False):
    # mode: 0 standard(100k) / 1 fast(400k) / 2 fast-plus(1M) / 3 custom(bus_hz applies) — TM §8
    return I2cConfig(
        mode=mode,
        bus_hz=bus_hz,
        slave_addr=address,
        timeout_ms=timeout_ms,
        addr_10bit=addr_10bit,
    )


@mcp.tool()
def si104_i2c_transfer(
    serial: Optional[str] = None,
    channel: int = 0,
    address: int = 0x30,
    tx_hex: str = "",
    rx_size: int = 0,
    bus_hz: int = 100_000,
    mode: int = 0,
    addr_10bit: bool = False,
    monitor_vcc: bool = False,
    timeout_ms: int = 1000,
) -> dict:
    """One atomic I2C master transaction: configure, transfer, close.

    Writes tx_hex then reads rx_size bytes in one sequence
    (START addr W, tx, repeated-START addr R, rx, STOP; firmware-managed).
    Empty tx_hex + rx_size>0 = pure read. mode: 0 standard 100k / 1 fast 400k /
    2 fast-plus 1M / 3 custom (bus_hz applies only in mode 3). monitor_vcc=true
    adds a 'vcc' dict {adc_mv, threshold_mv, adc_valid, output_enabled} —
    the monitor only samples; it does not enable the output supply (Guide §7).
    """
    _log_call(
        "si104_i2c_transfer",
        serial=serial,
        channel=channel,
        address=hex(address),
        tx_bytes=len(tx_hex) // 2,
        rx_size=rx_size,
        bus_hz=bus_hz,
        mode=mode,
    )
    tx = _parse_hex(tx_hex, "tx_hex")
    # Caps per SDK Guide section 7: master TX <= 504 B; RX <= 512 B, <= 506 B with VCC monitor.
    if len(tx) > 504:
        raise ValueError(f"tx_hex is {len(tx)} bytes; I2C master write caps at 504 bytes per call")
    if not 0 <= rx_size <= (506 if monitor_vcc else 512):
        raise ValueError(f"rx_size must be 0..{506 if monitor_vcc else 512} when monitor_vcc={monitor_vcc}, got {rx_size}")
    _check_i2c_args(channel, address, timeout_ms, bus_hz, mode, addr_10bit)

    def op(dev):
        dev.i2c_configure(
            channel, _i2c_config(address, bus_hz, timeout_ms, mode=mode, addr_10bit=addr_10bit)
        )
        rx = dev.i2c_transfer(channel, address, tx, rx_size, monitor_vcc=monitor_vcc)
        result = {"rx_hex": rx.hex(), "rx_bytes": len(rx)}
        if monitor_vcc:
            v = dev.last_i2c_vcc or {}
            result["vcc"] = {
                "adc_mv": v.get("adc_mv"),
                "threshold_mv": v.get("threshold_mv"),
                "adc_valid": bool(v.get("adc_valid")),
                "output_enabled": bool(v.get("output_enabled")),
            }
        return result

    return _with_device(serial, op, "si104_i2c_transfer")


@mcp.tool()
def si104_i2c_read_regs(
    serial: Optional[str] = None,
    channel: int = 0,
    address: int = 0x30,
    reg: int = 0,
    size: int = 1,
    bus_hz: int = 100_000,
    mode: int = 0,
    reg_addr_16bit: bool = False,
    timeout_ms: int = 1000,
) -> dict:
    """Master register read: write the register pointer then repeated-START read, one atomic transaction.

    Verified on hardware against a register-mode slave over the ch0<->ch1
    loopback. reg is 0..255 with reg_addr_16bit=false (single pointer byte);
    with reg_addr_16bit=true reg is 0..65535 and the pointer phase carries
    two bytes MSB-first. size is at most 512 bytes (the transfer rx cap).
    mode: 0 standard 100k / 1 fast 400k / 2 fast-plus 1M / 3 custom (bus_hz
    applies only in mode 3).
    """
    _log_call(
        "si104_i2c_read_regs",
        serial=serial,
        channel=channel,
        address=hex(address),
        reg=hex(reg),
        size=size,
        mode=mode,
        reg_addr_16bit=reg_addr_16bit,
    )
    if reg_addr_16bit:
        if not 0 <= reg <= 65535:
            raise ValueError(f"reg must be 0..65535 with reg_addr_16bit, got {reg}")
        ptr = reg.to_bytes(2, "big")
    else:
        if not 0 <= reg <= 255:
            raise ValueError(f"reg must be 0..255, got {reg}")
        ptr = bytes([reg])
    if not 1 <= size <= MAX_PAYLOAD:
        raise ValueError(f"size must be 1..512, got {size}")
    _check_i2c_args(channel, address, timeout_ms, bus_hz, mode)

    def op(dev):
        dev.i2c_configure(
            channel,
            _i2c_config(address, bus_hz, timeout_ms, mode=mode),
        )
        data = dev.i2c_transfer(channel, address, ptr, size)
        return {"data_hex": data.hex(), "size": len(data)}

    return _with_device(serial, op, "si104_i2c_read_regs")


@mcp.tool()
def si104_i2c_write_regs(
    serial: Optional[str] = None,
    channel: int = 0,
    address: int = 0x30,
    reg: int = 0,
    data_hex: str = "",
    bus_hz: int = 100_000,
    mode: int = 0,
    reg_addr_16bit: bool = False,
    timeout_ms: int = 1000,
) -> dict:
    """Master register write: pointer bytes + data in one write transaction (register auto-increment per target device protocol).

    Verified on hardware against a register-mode slave over the loopback.
    reg is 0..255 with reg_addr_16bit=false (single pointer byte); with
    reg_addr_16bit=true reg is 0..65535 and the pointer phase carries two
    bytes MSB-first. data_hex is at most 502 bytes per call with 8-bit
    pointers (504-byte transfer tx cap minus the pointer byte; 501 with
    16-bit pointers). mode: 0 standard 100k / 1 fast 400k / 2 fast-plus 1M /
    3 custom (bus_hz applies only in mode 3).
    """
    _log_call(
        "si104_i2c_write_regs",
        serial=serial,
        channel=channel,
        address=hex(address),
        reg=hex(reg),
        data_bytes=len(data_hex) // 2,
        mode=mode,
        reg_addr_16bit=reg_addr_16bit,
    )
    if reg_addr_16bit:
        if not 0 <= reg <= 65535:
            raise ValueError(f"reg must be 0..65535 with reg_addr_16bit, got {reg}")
        ptr = reg.to_bytes(2, "big")
    else:
        if not 0 <= reg <= 255:
            raise ValueError(f"reg must be 0..255, got {reg}")
        ptr = bytes([reg])
    data = _parse_hex(data_hex, "data_hex")
    if not data:
        raise ValueError("data_hex is required")
    if len(ptr) + len(data) > 504:
        raise ValueError(
            f"pointer ({len(ptr)} B) + data ({len(data)} B) exceeds the 504-byte transfer tx cap"
        )
    _check_i2c_args(channel, address, timeout_ms, bus_hz, mode)

    def op(dev):
        dev.i2c_configure(
            channel,
            _i2c_config(address, bus_hz, timeout_ms, mode=mode),
        )
        dev.i2c_transfer(channel, address, ptr + data, 0)
        return {"written_bytes": len(data)}

    return _with_device(serial, op, "si104_i2c_write_regs")


@mcp.tool()
def si104_i2c_scan(
    serial: Optional[str] = None,
    channel: int = 0,
    bus_hz: int = 100_000,
    mode: int = 0,
    first: int = 0x08,
    last: int = 0x77,
) -> dict:
    """Probe 7-bit addresses first..last on one I2C channel and return the ones that ACK a 1-byte read. 7-bit only by design.

    Use this before talking to an unknown bus: one call replaces ~112
    individual probe attempts. Each probe uses a 50 ms timeout so an
    empty bus sweeps in seconds. mode: 0 standard 100k / 1 fast 400k /
    2 fast-plus 1M / 3 custom (bus_hz applies only in mode 3). Dongle-level
    errors (device lost, busy) abort the scan; per-address errors count as
    "no device here" — the protocol has no dedicated NACK code (TM §8).
    """
    _log_call("si104_i2c_scan", serial=serial, channel=channel, bus_hz=bus_hz, mode=mode)
    if not 0x08 <= first <= last <= 0x77:
        raise ValueError("address range must be within 0x08..0x77")
    _check_i2c_args(channel, first, 0, bus_hz, mode)

    def op(dev):
        dev.i2c_configure(
            channel, _i2c_config(first, bus_hz, PROBE_TIMEOUT_MS, mode=mode)
        )
        found = []
        for addr in range(first, last + 1):
            try:
                dev.i2c_transfer(channel, addr, b"", 1)
                found.append(f"0x{addr:02X}")
                LOG.info("si104_i2c_scan: 0x%02X acked", addr)
            except Error as e:
                if (e.code, e.firmware_status) not in _SCAN_ABSENT:
                    LOG.warning("si104_i2c_scan aborted at 0x%02X: %s", addr, e)
                    return {
                        "error": f"scan aborted at 0x{addr:02X}: {e}",
                        "code": e.code,
                        "firmware_status": e.firmware_status,
                        "found_before_abort": found,
                        "hint": "run si104_enumerate to check the dongle is present",
                    }
                continue  # allowlisted NACK/timeout = nobody at this address
        return {"found": found, "range": [f"0x{first:02X}", f"0x{last:02X}"]}

    return _with_device(serial, op, "si104_i2c_scan")


@mcp.tool()
def si104_read_vcc(serial: Optional[str] = None, channel: int = 0) -> dict:
    """Read the shared VCC rail state via the documented VCC_THRESHOLD (0x1F) empty query — no bus traffic, works without a target device.

    channel selects the I2C interface that serves the query (VCC commands
    live on I2C interfaces 0-3, not the SPI/maintenance faces). Returns
    output/persist enable, threshold_mv, adc_mv, measured_mv, fail_reason,
    op_result (0 = success; op_result=0 alone does not prove a power request
    took effect). output_enabled=false + transfers failing with NO_POWER
    (firmware_status=11) means the rail is off: enable via
    si104_raw_request(channel, 0x1E, '0100').
    """
    _log_call("si104_read_vcc", serial=serial, channel=channel)
    if not 0 <= channel <= 3:
        raise ValueError(f"channel must be 0..3, got {channel}")

    def op(dev):
        raw = dev.request(channel, 0x1F)
        if len(raw) < 10:
            return {
                "error": f"VCC query reply too short ({len(raw)} bytes)",
                "raw_hex": raw.hex(),
            }
        (
            output_enabled,
            persist_enable,
            threshold_mv,
            adc_mv,
            fail_reason,
            op_result,
            measured_mv,
        ) = struct.unpack("<BBHHBBH", raw[:10])
        return {
            "output_enabled": bool(output_enabled),
            "persist_enable": bool(persist_enable),
            "threshold_mv": threshold_mv,
            "adc_mv": adc_mv,
            "fail_reason": fail_reason,
            "op_result": op_result,
            "measured_mv": measured_mv,
        }

    return _with_device(serial, op, "si104_read_vcc")


@mcp.tool()
def si104_spi_tx_words(
    serial: Optional[str] = None,
    tx_hex: str = "",
    data_bits: int = 16,
    sclk_hz: int = 1_000_000,
    cpol: int = 0,
    cpha: int = 0,
    lsb_first: bool = False,
    cs_active_high: bool = False,
    timeout_ms: int = 1000,
) -> dict:
    """Stream SPI words TX-only via the staged long-TX path (BEGIN/DATA/COMMIT; the bus starts on commit).

    data_bits must be 8/16/32 (long-TX restriction, TM §7.1); tx_hex length
    must be a non-zero multiple of data_bits/8, 1..1024 words per call. Word
    assembly is big-endian: first hex byte = word MSB (SPI wire order). No
    read-back: use si104_spi_transfer when rx is needed, and
    si104_spi_control(action="tx_status") to confirm completion.
    """
    _log_call("si104_spi_tx_words", serial=serial, tx_bytes=len(tx_hex) // 2, data_bits=data_bits, sclk_hz=sclk_hz)
    tx = _parse_hex(tx_hex, "tx_hex")
    if data_bits not in (8, 16, 32):
        raise ValueError(f"data_bits must be 8/16/32 for long TX, got {data_bits}")
    if sclk_hz <= 0:
        raise ValueError(f"sclk_hz must be positive, got {sclk_hz}")
    step = data_bits // 8
    if not tx or len(tx) % step:
        raise ValueError(f"tx_hex length must be a non-zero multiple of {step}, got {len(tx)} bytes")
    if len(tx) // step > 1024:
        raise ValueError(f"at most 1024 words per call, got {len(tx) // step}")

    def op(dev):
        dev.spi_configure(
            SpiConfig(
                data_bits=data_bits,
                sclk_hz=sclk_hz,
                timeout_ms=timeout_ms,
                cpol=cpol,
                cpha=cpha,
                lsb_first=lsb_first,
                cs_active_high=cs_active_high,
            )
        )
        dev.spi_tx_words(
            int.from_bytes(tx[i : i + step], "big") for i in range(0, len(tx), step)
        )
        return {"words": len(tx) // step}

    return _with_device(serial, op, "si104_spi_tx_words")


@mcp.tool()
def si104_spi_control(
    serial: Optional[str] = None,
    action: str = "tx_status",
) -> dict:
    """SPI session control: action="abort" cancels a pending long-TX upload or running transfer; action="tx_status" reports long-TX state (idle/uploading/committable/done_ok/done_error/cancelled_or_expired) with byte counters and result code.

    State is per USB session: in this stateless server every call is a fresh
    session, so tx_status normally reports idle; it matters when a previous
    call crashed mid-upload and the dongle needs recovery.
    """
    if action not in ("abort", "tx_status"):
        raise ValueError('action must be "abort" or "tx_status"')
    _log_call("si104_spi_control", serial=serial, action=action)

    def op(dev):
        if action == "abort":
            reply = dev.spi_abort()
            return {"action": "abort", "reply_hex": reply.hex()}
        return {"action": "tx_status", **_decode_spi_tx_status(dev.spi_tx_status())}

    return _with_device(serial, op, "si104_spi_control")


@mcp.tool()
def si104_raw_request(
    serial: Optional[str] = None,
    interface: int = 4,
    command: int = 0,
    payload_hex: str = "",
) -> dict:
    """Raw vendor request passthrough to the control interface (interface 4) for firmware commands without a dedicated tool.

    Known IDs: I2C CONFIG=0x10, MASTER_XFER=0x11, SLAVE_READ=0x12,
    SLAVE_WRITE=0x13, ABORT/RESET=0x14/0x15, MAILBOX_PUSH/POP=0x16/0x17,
    REG_WRITE=0x18, REG_READ/REG_REPLY=0x19/0x1A, EVENT_POLL=0x1B,
    VADDR_SET/GET=0x1C/0x1D, VCC_SET=0x1E (u8 enable, u8 save),
    VCC_THRESHOLD=0x1F (empty query or u16 threshold_mv + u8 save),
    SPI_CONFIG=0x20, SPI_XFER=0x21, SPI_ABORT=0x22, SPI_TX_STATUS=0x26.
    Wrong commands can disturb device state — prefer dedicated tools when one
    exists. payload ≤ 508 B; the reply (up to 512 B) comes back as reply_hex.
    """
    if not 0 <= command <= 255:
        raise ValueError(f"command must be 0..255, got {command}")
    payload = _parse_hex(payload_hex, "payload_hex")
    _check_payload("payload_hex", len(payload))
    _log_call("si104_raw_request", serial=serial, interface=interface, command=hex(command), payload_bytes=len(payload))

    def op(dev):
        reply = dev.request(interface, command, payload)
        return {"reply_hex": reply.hex(), "reply_bytes": len(reply)}

    return _with_device(serial, op, "si104_raw_request")


@mcp.tool()
def si104_i2c_slave(
    serial: Optional[str] = None,
    channel: int = 0,
    action: str = "write",
    data_hex: str = "",
    size: int = 1,
    bus_hz: int = 100_000,
    mode: int = 0,
    slave_addr: int = 0x30,
    slave_mode: int = 0,
    reg_addr_16bit: bool = False,
    data_16bit: bool = False,
    data_big_endian: bool = False,
    timeout_ms: int = 1000,
) -> dict:
    """I2C slave buffer read/write on one channel (raw/mailbox/register logic modes).

    Verified on hardware: the firmware keeps channel config across USB
    sessions while powered, so a slave configured by an earlier call stays
    alive between MCP calls — master-side tools can talk to it in separate
    calls. The tool reuses an existing matching config (initialized + role=1
    + same slave_addr + same logic mode) instead of clobbering it; mismatch
    or missing config is (re)configured, then it polls GET_STATUS
    initialized=1 (up to ~1 s). slave_mode: 0 raw / 1 mailbox / 2 register
    (TM section 8). action="write" sets the slave send buffer (what an
    external master reads next), up to 256 B; action="read" fetches bytes
    the external master wrote, up to 512 B.
    """
    if action not in ("read", "write"):
        raise ValueError('action must be "read" or "write"')
    data = _parse_hex(data_hex, "data_hex")
    if action == "write":
        if not data:
            raise ValueError('data_hex is required for action="write"')
        if len(data) > 256:
            raise ValueError(f"slave write carries at most 256 bytes, got {len(data)}")
    if action == "read" and not 1 <= size <= MAX_PAYLOAD:
        raise ValueError(f"size must be 1..512, got {size}")
    _check_i2c_args(channel, slave_addr, timeout_ms, bus_hz, mode)
    _log_call("si104_i2c_slave", serial=serial, channel=channel, action=action, bytes=len(data), slave_mode=slave_mode)

    def op(dev):
        # Firmware keeps channel config across USB sessions while powered.
        # Reuse a matching slave config instead of clobbering it with defaults.
        cur = _decode_i2c_status(dev.status(channel))
        if not (
            cur.get("initialized")
            and cur.get("role") == 1
            and cur.get("slave_addr") == slave_addr
            and (slave_mode == 0 or cur.get("slave_logic_mode") == slave_mode)
        ):
            dev.i2c_configure(
                channel,
                I2cConfig(
                    role=1,
                    mode=mode,
                    bus_hz=100_000 if mode != 3 else bus_hz,
                    slave_addr=slave_addr,
                    timeout_ms=timeout_ms,
                    slave_mode=slave_mode,
                    reg_addr_16bit=reg_addr_16bit,
                    data_16bit=data_16bit,
                    data_big_endian=data_big_endian,
                ),
            )
        status = {}
        initialized = False
        for _ in range(20):
            status = _decode_i2c_status(dev.status(channel))
            if status.get("initialized"):
                initialized = True
                break
            time.sleep(0.05)
        if not initialized:
            return {
                "error": "channel did not reach initialized=1 after slave config",
                "status": status,
                "hint": "check slave_mode value and external pull-ups; config ack does not guarantee init",
            }
        if action == "read":
            got = dev.i2c_slave_read(channel, size)
            return {
                "initialized": True,
                "data_hex": got.hex(),
                "size": len(got),
                "status": _decode_i2c_status(dev.status(channel)),
            }
        # action == "write"
        dev.i2c_slave_write(channel, data)
        return {"initialized": True, "written_bytes": len(data)}

    return _with_device(serial, op, "si104_i2c_slave")


if __name__ == "__main__":
    mcp.run()  # stdio
