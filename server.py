"""UTools-SI104 MCP server — stdio, stateless.

Each tool call is atomic: open -> operate -> close. The dongle is held only
for the duration of one call (milliseconds), so the uTools GUI app can still
use it between AI calls. Designed for same-time single-client usage.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import struct
import sys
import threading
import time
from typing import Annotated, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

try:
    from si104 import Device, SpiConfig, I2cConfig, Error
except ImportError:
    if os.environ.get("SI104_MCP_STUB") == "1":
        # Test-double mode for unit_test/smoke_test/CI: no vendor wheel, the
        # tool surface runs against si104_stub. Never used in production.
        import si104_stub as _stub

        Device, SpiConfig, I2cConfig, Error = (
            _stub.Device,
            _stub.SpiConfig,
            _stub.I2cConfig,
            _stub.Error,
        )
        print(
            "si104-mcp: STUB MODE — using si104_stub test double, no real dongle",
            file=sys.stderr,
        )
    else:
        sys.stderr.write(
            "si104-mcp: the vendor SDK wheel is missing. Install it into this "
            "environment first (the same python that runs server.py), see README:\n"
            "  pip install <sdk-package>/python/wheels/utools_si104-*.whl\n"
            "  pip install -r requirements.txt\n"
        )
        raise SystemExit(1) from None

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
        "TX 504 B, RX 512 B (506 with VCC monitor), register read 512 B per "
        "transfer (the mirror auto-increment is page-scoped to 256 B), "
        "register write 504 B including pointer bytes, SPI TX 504 B / RX 512 "
        "B; larger transfers must "
        "be chunked. Device errors come back as a dict with code/backend_code/"
        "firmware_status; if completion_unknown is true the transfer may have "
        "hit the bus — verify device state before re-sending. firmware_status="
        "11 (NO_POWER) means the shared VCC output is off: enable it once with "
        "si104_vcc_control. I2C channel recovery is si104_i2c_control (abort/"
        "reset); mailbox FIFOs go through si104_i2c_slave push/pop. Device "
        "errors carry code_meaning (official SDK Guide section 8 table). "
        "Transient open conflicts (-2/-3) are retried automatically; run "
        "si104_enumerate first if a call says the device is missing."
    ),
)

# 512: hard cap enforced by the SDK python binding (_integer(x, 512)).
MAX_PAYLOAD = 512
# i2c_scan probe timeout: NACK fails immediately, the timeout only guards
# against clock-stretch hangs. Keeps a full 0x08..0x77 sweep under ~10 s.
PROBE_TIMEOUT_MS = 50


# ---------------------------------------------------------------- MCP output
# Structured-output models (MCP spec: outputSchema + structuredContent).
# pydantic BaseModel, all fields optional: the generated schema marks every
# property nullable, so error results (fields on _ErrBase) and partial
# payloads validate cleanly against it; extra diagnostic keys are preserved
# (extra="allow"). The text JSON fallback stays byte-for-byte the same.
class _ErrBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    error: str | None = None
    code: int | None = None
    code_meaning: str | None = None
    backend_code: int | None = None
    firmware_status: int | None = None
    completion_unknown: bool | None = None
    hint: str | None = None
    retry_hint: str | None = None


class _DeviceEntry(_ErrBase):
    vid: int | None = None
    pid: int | None = None
    bus: int | None = None
    address: int | None = None
    serial: str | None = None


class EnumerateOut(_ErrBase):
    count: int | None = None
    devices: list[_DeviceEntry] | None = None


class DeviceInfoOut(_ErrBase):
    info: dict | None = None
    version_hex: str | None = None
    runtime: list[int] | None = None
    spi: dict | None = None
    i2c: dict | None = None
    vcc: dict | None = None


class TransferOut(_ErrBase):
    rx_hex: str | None = None
    rx_bytes: int | None = None
    vcc: dict | None = None


class RegsReadOut(_ErrBase):
    data_hex: str | None = None
    size: int | None = None


class RegsWriteOut(_ErrBase):
    written_bytes: int | None = None


class ScanOut(_ErrBase):
    found: list[str] | None = None
    range: list[str] | None = None
    found_before_abort: list[str] | None = None


class ControlOut(_ErrBase):
    action: str | None = None
    channel: int | None = None
    reply_hex: str | None = None
    state: str | None = None
    total_bytes: int | None = None
    received_bytes: int | None = None
    begin_seq: int | None = None
    commit_seq: int | None = None
    result: int | None = None
    session_id: int | None = None
    raw_hex: str | None = None


class TxWordsOut(_ErrBase):
    words: int | None = None


class RawRequestOut(_ErrBase):
    reply_hex: str | None = None
    reply_bytes: int | None = None


class VccStatusOut(_ErrBase):
    output_enabled: bool | None = None
    persist_enable: bool | None = None
    threshold_mv: int | None = None
    adc_mv: int | None = None
    fail_reason: int | None = None
    op_result: int | None = None
    measured_mv: int | None = None
    raw_hex: str | None = None


class VccControlOut(VccStatusOut):
    requested_enable: bool | None = None
    requested_persist: bool | None = None


class SlaveOut(_ErrBase):
    initialized: bool | None = None
    action: str | None = None
    data_hex: str | None = None
    size: int | None = None
    written_bytes: int | None = None
    pushed_bytes: int | None = None
    status: dict | None = None


# Shared parameter schemas: the inputSchema is what the model sees when it
# picks and fills a tool (reference: awslabs/mcp FastMCP servers), so every
# parameter carries a Field description and enum-like choices use Literal.
_Serial = Annotated[
    Optional[str],
    Field(description="USB serial number; None targets the first dongle (si104_enumerate lists them)"),
]
_Channel = Annotated[
    int,
    Field(description="I2C channel 0..3 — each channel is its own USB interface (TM section 4)"),
]
_BusMode = Annotated[
    Literal[0, 1, 2, 3],
    Field(description="Speed preset: 0=100k, 1=400k, 2=1M, 3=custom bus_hz"),
]
_BusHz = Annotated[
    int,
    Field(description="Clock in custom mode (mode=3), 10_000..1_000_000 Hz; ignored otherwise"),
]
_I2cTimeout = Annotated[
    int,
    Field(description="u16 bus timeout 0..65535 ms; 0 = firmware default (1000 ms)"),
]
_HexBytes = Annotated[
    str,
    Field(description="Hex bytes ('deadbeef', optional 0x prefix / spaces); '' = none"),
]
_SpiTimeout = Annotated[int, Field(description="Bus timeout 1..60000 ms")]

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


def _decode_vcc_reply(raw):
    """VCC_THRESHOLD (0x1F) empty-query reply: u8 output/persist enable,
    u16 threshold_mv/adc_mv, u8 fail_reason/op_result, u16 measured_mv —
    10 bytes. None if the reply is too short."""
    if len(raw) < 10:
        return None
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


def _query_vcc(dev, channel):
    """0x1F empty query on the channel's I2C interface, decoded. Returns an
    error dict instead of raising so diagnostics keep their other keys."""
    try:
        raw = dev.request(channel, 0x1F)
    except Error as e:
        return {"error": str(e), "code": e.code}
    reply = _decode_vcc_reply(raw)
    if reply is None:
        return {
            "error": f"VCC query reply too short ({len(raw)} bytes)",
            "raw_hex": raw.hex(),
        }
    return reply


def _parse_hex(value, name):
    """Accept '', None, 'deadbeef', '0xDEAD BEEF' -> bytes. ValueError on bad hex."""
    if value is None:
        return b""
    cleaned = "".join(str(value).strip().removeprefix("0x").removeprefix("0X").split())
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError(f"{name} is not valid hex: {value!r}") from None


def _check_payload(name, size, cap=MAX_PAYLOAD):
    if size > cap:
        raise ValueError(
            f"{name} is {size} bytes; one call carries at most {cap} bytes, split into chunks"
        )


def _check_i2c_args(channel, address, timeout_ms, bus_hz, mode, addr_10bit=False):
    """Entry validation per TM section 4/8: channels 0-3, 7/10-bit address
    range, u16 timeout (0 = firmware default 1000 ms), speed-mode selector,
    custom-mode speed."""
    if mode not in (0, 1, 2, 3):
        raise ValueError(
            f"mode must be 0 (100k) / 1 (400k) / 2 (1M) / 3 (custom bus_hz), got {mode}"
        )
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


def _check_spi_args(sclk_hz, timeout_ms, cpol, cpha):
    """Entry validation per SDK Guide section 6: SCLK 50k-40M, bus timeout
    1-60000 ms, cpol/cpha each 0 or 1 (SPI modes 0-3)."""
    if not 50_000 <= sclk_hz <= 40_000_000:
        raise ValueError(
            f"sclk_hz must be 50_000..40_000_000 (SDK Guide section 6), got {sclk_hz}"
        )
    if not 1 <= timeout_ms <= 60_000:
        raise ValueError(
            f"timeout_ms must be 1..60000 (SDK Guide section 6), got {timeout_ms}"
        )
    if cpol not in (0, 1) or cpha not in (0, 1):
        raise ValueError(
            f"cpol and cpha must each be 0 or 1, got cpol={cpol}, cpha={cpha}"
        )


# Per-address absence is an ALLOWLIST, not a blocklist: only firmware NACK
# (status=7 IO_ERROR) and probe timeout (status=8 TIMEOUT) count as "nobody
# at this address" — verified across ~18k live-bus probes. Any other
# signature (USB disconnect, busy, ...) aborts the scan; that stays safe
# even for transport signatures never yet observed.
_SCAN_ABSENT = ((-10, 7), (-10, 8))

# SDK Guide section 8 error table (official meanings of the unified codes).
_ERROR_MEANING = {
    -1: "invalid parameter",
    -2: "device not found",
    -3: "interface or device busy",
    -4: "timeout",
    -5: "USB disconnect",
    -6: "malformed reply",
    -7: "unsupported",
    -8: "USB or other IO error",
    -9: "buffer too small",
    -10: "device rejected the request",
    -11: "multiple devices — specify a serial",
}


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
    dongle error, [internal] unexpected python-side fault. Transient open
    failures (SDK Guide section 8: -2 not found / -3 busy — also how a USB
    mutex clash with the uTools GUI shows up) are retried twice with 100 ms
    backoff. Only the open is retried, never a bus operation.
    """
    cid = getattr(_CALL, "cid", None) or next(_SEQ)
    try:
        dev = None
        for attempt in range(3):
            try:
                dev = Device(serial or None)
                break
            except Error as e:
                if attempt == 2 or e.code not in (-2, -3):
                    raise
                LOG.warning(
                    "#%d [device] open attempt %d/3 failed: %s; retrying in 100 ms",
                    cid,
                    attempt + 1,
                    e,
                )
                time.sleep(0.1)
        with dev:
            result = operate(dev)
    except Error as e:
        LOG.exception("#%d [device] error in %s", cid, name)
        result = {
            "error": str(e),
            "code": e.code,
            "code_meaning": _ERROR_MEANING.get(e.code, f"code {e.code}"),
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
                "power-up with si104_vcc_control(channel=<I2C channel>, "
                "enable=true), then retry"
            )
    except ValueError as e:
        LOG.warning("#%d [param] %s: %s", cid, name, e)
        raise
    except Exception:
        LOG.exception("#%d [internal] fault in %s", cid, name)
        raise
    _log_result(cid, name, result)
    if isinstance(result, dict) and "error" in result:
        # MCP spec: tool execution failures are reported with isError=true.
        # The JSON text keeps every structured field, so callers parse it the
        # same way as before — the protocol layer just gets the failure flag.
        raise ToolError(json.dumps(result))
    return result


# ---------------------------------------------------------------- diagnostics


@mcp.tool(
    title="List SI104 dongles on USB",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def si104_enumerate() -> EnumerateOut:
    """List SI104 dongles over USB. Returns {count, devices:[{vid,pid,bus,address,serial}]}. An empty list means nothing is plugged in."""
    devices = Device.enumerate()
    LOG.info("si104_enumerate -> %d device(s)", len(devices))
    return {"count": len(devices), "devices": devices}


@mcp.tool(
    title="Read-only device diagnostics",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def si104_device_info(
    serial: _Serial = None,
    channel: _Channel = 0,
) -> DeviceInfoOut:
    """Read-only diagnostics: identity/firmware/runtime plus per-face capabilities, limits, the decoded 32-byte I2C channel status and the selected channel's VCC rail state.

    The dongle exposes functional USB interfaces (TM section 1): 0-3 = I2C
    channels, 4 = SPI, 5 = maintenance. info/version/runtime are device-wide
    (queried on the SPI face); capabilities/limits come back for both the
    SPI face and the selected I2C channel; status is the 32-byte I2C
    GET_STATUS of that channel, decoded per TM section 8; vcc is the 0x1F
    empty query of that channel (no bus traffic) — output_enabled=false
    together with NO_POWER transfer failures means: si104_vcc_control.
    Every section degrades independently; one failing query never blanks
    the rest.
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
        put("vcc", lambda: {"channel": channel, **_query_vcc(dev, channel)})
        return out

    return _with_device(serial, op, "si104_device_info")


# ---------------------------------------------------------------------- SPI


@mcp.tool(title="Atomic SPI full-duplex transfer")
def si104_spi_transfer(
    serial: _Serial = None,
    tx_hex: _HexBytes = "",
    rx_size: Annotated[
        int, Field(description="Bytes to clock in during the same transaction, 0..512; 0 = TX-only")
    ] = 0,
    sclk_hz: Annotated[
        int, Field(description="SCLK 50_000..40_000_000 Hz (quantized by the clock divider)")
    ] = 1_000_000,
    data_bits: Annotated[
        int, Field(description="Word width 1..32; >8 requires whole-word-aligned tx/rx lengths")
    ] = 8,
    cpol: Annotated[int, Field(description="Clock polarity 0/1 (with cpha selects SPI mode 0-3)")] = 0,
    cpha: Annotated[int, Field(description="Clock phase 0/1")] = 0,
    lsb_first: Annotated[bool, Field(description="True = least-significant bit first (default MSB)")] = False,
    cs_active_high: Annotated[
        bool, Field(description="True = active-high CS (host single-line I/O only, SDK Guide §6)")
    ] = False,
    timeout_ms: _SpiTimeout = 1000,
) -> TransferOut:
    """One atomic SPI transaction: configure bus, full-duplex transfer, close.

    Use when: one-shot exchange with a known device — read a sensor, verify
    an echo, poke a register.
    Do NOT use when: rx is not needed and the payload exceeds 504 B — use
    si104_spi_tx_words (staged long-TX, up to 1024 words) instead.

    tx_hex is clocked out; rx_size bytes are clocked in during the same
    transaction (lengths are independent: TX-only = rx_size 0, RX-only =
    empty tx_hex). tx is at most 504 bytes and rx_size at most 512.
    Firmware detail (hardware-verified): >504B TX-only transfers get
    auto-routed to the long-TX path (which is TX-only); this tool caps tx
    at 504B for predictable single-command behavior.
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
    if not 1 <= data_bits <= 32:
        raise ValueError(
            f"data_bits must be 1..32 for normal transfers (SDK Guide section 6), got {data_bits}"
        )
    if not 0 <= rx_size <= MAX_PAYLOAD:
        raise ValueError(f"rx_size must be 0..512, got {rx_size}")
    _check_spi_args(sclk_hz, timeout_ms, cpol, cpha)

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


@mcp.tool(title="Atomic I2C master transfer")
def si104_i2c_transfer(
    serial: _Serial = None,
    channel: _Channel = 0,
    address: Annotated[
        int, Field(description="7-bit target address 0..127 (0..1023 with addr_10bit)")
    ] = 0x30,
    tx_hex: _HexBytes = "",
    rx_size: Annotated[
        int,
        Field(description="Bytes to read after a repeated START, 0..512 (0..506 with monitor_vcc); 0 = write-only"),
    ] = 0,
    bus_hz: _BusHz = 100_000,
    mode: _BusMode = 0,
    addr_10bit: Annotated[bool, Field(description="True = 10-bit addressing")] = False,
    monitor_vcc: Annotated[
        bool,
        Field(description="True adds a 'vcc' dict to the result; RX cap drops to 506 B"),
    ] = False,
    timeout_ms: _I2cTimeout = 1000,
) -> TransferOut:
    """One atomic I2C master transaction: configure, transfer, close.

    Use when: raw control over the wire — devices whose protocol is not a
    simple register map, or a pure read (empty tx_hex + rx_size>0).
    Do NOT use when: the target has a register pointer protocol —
    si104_i2c_read_regs / si104_i2c_write_regs do the pointer phase for you.

    Writes tx_hex then reads rx_size bytes in one sequence
    (START addr W, tx, repeated-START addr R, rx, STOP; firmware-managed).
    monitor_vcc=true adds a 'vcc' dict {adc_mv, threshold_mv, adc_valid,
    output_enabled} — the monitor only samples; it does not enable the
    output supply (Guide §7).
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


@mcp.tool(title="I2C register read")
def si104_i2c_read_regs(
    serial: _Serial = None,
    channel: _Channel = 0,
    address: Annotated[
        int, Field(description="7-bit target address 0..127 (0..1023 with addr_10bit)")
    ] = 0x30,
    reg: Annotated[
        int, Field(description="Register address 0..255 (0..65535 with reg_addr_16bit)")
    ] = 0,
    size: Annotated[
        int, Field(description="Bytes to read, 1..512 (auto-increment is page-scoped to 256 B)")
    ] = 1,
    bus_hz: _BusHz = 100_000,
    mode: _BusMode = 0,
    reg_addr_16bit: Annotated[
        bool, Field(description="True = 16-bit register pointer, 2 bytes MSB-first")
    ] = False,
    timeout_ms: _I2cTimeout = 1000,
) -> RegsReadOut:
    """Master register read: write the register pointer then repeated-START read, one atomic transaction.

    Use when: the target speaks the classic register protocol (write pointer,
    repeated-START read) — sensors, EEPROMs, SI104's own register-mode slave.
    Do NOT use when: the device needs raw sequences (use si104_i2c_transfer).

    Verified on hardware against a register-mode slave over the ch0<->ch1
    loopback. reg is 0..255 with reg_addr_16bit=false (single pointer byte);
    with reg_addr_16bit=true reg is 0..65535 and the pointer phase carries
    two bytes MSB-first. size is at most 512 bytes (the transfer rx cap);
    note the auto-increment is page-scoped to 256 B (TM section 8) — split
    larger reads per page like si104_i2c_write_regs when the target device
    follows SI104's own page semantics.
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


@mcp.tool(title="I2C register write")
def si104_i2c_write_regs(
    serial: _Serial = None,
    channel: _Channel = 0,
    address: Annotated[
        int, Field(description="7-bit target address 0..127 (0..1023 with addr_10bit)")
    ] = 0x30,
    reg: Annotated[
        int, Field(description="Register address 0..255 (0..65535 with reg_addr_16bit)")
    ] = 0,
    data_hex: Annotated[
        _HexBytes, Field(description="Register payload hex, at most 502 B (501 with 16-bit pointer)")
    ] = "",
    bus_hz: _BusHz = 100_000,
    mode: _BusMode = 0,
    reg_addr_16bit: Annotated[
        bool, Field(description="True = 16-bit register pointer, 2 bytes MSB-first")
    ] = False,
    timeout_ms: _I2cTimeout = 1000,
) -> RegsWriteOut:
    """Master register write: pointer bytes + data in one write transaction (register auto-increment per target device protocol).

    Use when: writing configuration/data to a register-protocol target.
    Do NOT use when: the payload must not trigger the pointer phase (pure
    mailbox/raw writes go through si104_i2c_transfer).

    Verified on hardware against a register-mode slave over the loopback.
    reg is 0..255 with reg_addr_16bit=false (single pointer byte); with
    reg_addr_16bit=true reg is 0..65535 and the pointer phase carries two
    bytes MSB-first. Auto-increment is target-device-specific: SI104's own
    register-mode slave mirror is page-scoped — one pointer write stops at
    the 256-byte page boundary (TM section 8, hardware-verified); split
    larger writes per page. data_hex is at most 502 bytes per call with
    8-bit pointers (504-byte transfer tx cap minus the pointer byte; 501
    with 16-bit pointers).
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


@mcp.tool(
    title="Scan I2C bus addresses",
    annotations=ToolAnnotations(readOnlyHint=True),
)
def si104_i2c_scan(
    serial: _Serial = None,
    channel: _Channel = 0,
    bus_hz: _BusHz = 100_000,
    mode: _BusMode = 0,
    first: Annotated[int, Field(description="First 7-bit address to probe, 0x08..0x77")] = 0x08,
    last: Annotated[int, Field(description="Last 7-bit address to probe, <=0x77")] = 0x77,
) -> ScanOut:
    """Probe 7-bit addresses first..last on one I2C channel and return the ones that ACK a 1-byte read. 7-bit only by design.

    Use when: first contact with an unknown bus — one call replaces ~112
    individual probe attempts; also useful to verify wiring after enabling
    VCC.
    Do NOT use when: the target is 10-bit addressed (not supported here) or
    you already know the address (go straight to si104_i2c_transfer).

    Each probe uses a 50 ms timeout so an empty bus sweeps in seconds.
    Dongle-level errors (device lost, busy) abort the scan; per-address
    errors count as "no device here" — the protocol has no dedicated NACK
    code (TM §8).
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
                        "code_meaning": _ERROR_MEANING.get(e.code, f"code {e.code}"),
                        "firmware_status": e.firmware_status,
                        "found_before_abort": found,
                        "hint": "run si104_enumerate to check the dongle is present",
                    }
                continue  # allowlisted NACK/timeout = nobody at this address
        return {"found": found, "range": [f"0x{first:02X}", f"0x{last:02X}"]}

    return _with_device(serial, op, "si104_i2c_scan")


@mcp.tool(
    title="I2C channel abort/reset",
    annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True),
)
def si104_i2c_control(
    serial: _Serial = None,
    channel: _Channel = 0,
    action: Annotated[
        Literal["abort", "reset"],
        Field(description='abort = cancel in-flight transfer (0x14); reset = clear channel config (0x15)'),
    ] = "abort",
) -> ControlOut:
    """I2C channel recovery: action="abort" cancels an in-flight transfer on the channel (ABORT=0x14); action="reset" resets the whole channel and clears its configuration (RESET=0x15).

    Use when a transfer wedged (e.g. a target clock-stretches past every
    timeout) or after any error that leaves the channel in an unknown state.
    Executed operations cannot be rolled back (SDK Guide section 7); after a
    reset, reconfigure the channel before the next transfer — master tools
    configure on every call, slave setups must be re-applied.
    """
    if action not in ("abort", "reset"):
        raise ValueError('action must be "abort" or "reset"')
    if not 0 <= channel <= 3:
        raise ValueError(f"channel must be 0..3, got {channel}")
    _log_call("si104_i2c_control", serial=serial, channel=channel, action=action)

    def op(dev):
        if action == "abort":
            dev.i2c_abort(channel)
        else:
            dev.i2c_reset(channel)
        return {"action": action, "channel": channel}

    return _with_device(serial, op, "si104_i2c_control")


@mcp.tool(
    title="Read VCC rail state",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def si104_read_vcc(
    serial: _Serial = None,
    channel: _Channel = 0,
) -> VccStatusOut:
    """Read the shared VCC rail state via the documented VCC_THRESHOLD (0x1F) empty query — no bus traffic, works without a target device.

    channel selects the I2C interface that serves the query (VCC commands
    live on I2C interfaces 0-3, not the SPI/maintenance faces). Returns
    output/persist enable, threshold_mv, adc_mv, measured_mv, fail_reason,
    op_result (0 = success; op_result=0 alone does not prove a power request
    took effect). output_enabled=false + transfers failing with NO_POWER
    (firmware_status=11) means the rail is off: enable via
    si104_vcc_control(channel, enable=true).
    """
    _log_call("si104_read_vcc", serial=serial, channel=channel)
    if not 0 <= channel <= 3:
        raise ValueError(f"channel must be 0..3, got {channel}")

    def op(dev):
        return _query_vcc(dev, channel)

    return _with_device(serial, op, "si104_read_vcc")


@mcp.tool(
    title="Enable/disable VCC output",
    annotations=ToolAnnotations(idempotentHint=True),
)
def si104_vcc_control(
    serial: _Serial = None,
    channel: _Channel = 0,
    enable: Annotated[
        bool, Field(description="True = turn the shared VCC output on (NO_POWER recovery)")
    ] = True,
    persist: Annotated[
        bool, Field(description="True = firmware saves the setting across power cycles")
    ] = False,
) -> VccControlOut:
    """Enable or disable the shared VCC output that feeds the I2C pull-ups (VCC_SET=0x1E: u8 enable + u8 save).

    This is the NO_POWER (firmware_status=11) recovery path: the shared
    output defaults to off at power-up; enabling it once per power-up is
    hardware-verified (VCC ≈ 3.1 V). persist=true asks the firmware to keep
    the setting across dongle power cycles. channel selects the I2C
    interface serving the command (VCC lives on I2C interfaces 0-3, not the
    SPI/maintenance faces). The result is verified with an immediate 0x1F
    query, so the returned output_enabled reflects device state rather than
    just the command ack.
    """
    if not 0 <= channel <= 3:
        raise ValueError(f"channel must be 0..3, got {channel}")
    _log_call(
        "si104_vcc_control", serial=serial, channel=channel, enable=enable, persist=persist
    )

    def op(dev):
        dev.request(channel, 0x1E, bytes([1 if enable else 0, 1 if persist else 0]))
        raw = dev.request(channel, 0x1F)
        reply = _decode_vcc_reply(raw)
        if reply is None:
            return {
                "error": f"VCC query reply too short after VCC_SET ({len(raw)} bytes)",
                "raw_hex": raw.hex(),
            }
        return {"requested_enable": enable, "requested_persist": persist, **reply}

    return _with_device(serial, op, "si104_vcc_control")


@mcp.tool(title="SPI long send (words)")
def si104_spi_tx_words(
    serial: _Serial = None,
    tx_hex: Annotated[
        _HexBytes,
        Field(description="Word payload hex; non-zero multiple of data_bits/8, at most 1024 words"),
    ] = "",
    data_bits: Annotated[
        Literal[8, 16, 32], Field(description="Long-TX word width (8/16/32 only, TM §7.1)")
    ] = 16,
    sclk_hz: Annotated[
        int, Field(description="SCLK 50_000..40_000_000 Hz (quantized by the clock divider)")
    ] = 1_000_000,
    cpol: Annotated[int, Field(description="Clock polarity 0/1")] = 0,
    cpha: Annotated[int, Field(description="Clock phase 0/1")] = 0,
    lsb_first: Annotated[bool, Field(description="True = least-significant bit first (default MSB)")] = False,
    cs_active_high: Annotated[
        bool, Field(description="True = active-high CS (host single-line I/O only)")
    ] = False,
    timeout_ms: _SpiTimeout = 1000,
) -> TxWordsOut:
    """Stream SPI words TX-only via the staged long-TX path (BEGIN/DATA/COMMIT; the bus starts on commit).

    Use when: pushing more than 504 B out (LED strings, firmware images,
    stimulus patterns) — up to 1024 words of 8/16/32 bits per call.
    Do NOT use when: you need to read back during the transfer (long-TX is
    TX-only; use si104_spi_transfer) or the payload fits one normal
    transaction.

    data_bits must be 8/16/32 (long-TX restriction, TM §7.1); tx_hex length
    must be a non-zero multiple of data_bits/8. Word assembly is
    big-endian: first hex byte = word MSB (SPI wire order). Confirm
    completion afterwards with si104_spi_control(action="tx_status").
    """
    _log_call("si104_spi_tx_words", serial=serial, tx_bytes=len(tx_hex) // 2, data_bits=data_bits, sclk_hz=sclk_hz)
    tx = _parse_hex(tx_hex, "tx_hex")
    if data_bits not in (8, 16, 32):
        raise ValueError(f"data_bits must be 8/16/32 for long TX, got {data_bits}")
    _check_spi_args(sclk_hz, timeout_ms, cpol, cpha)
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


@mcp.tool(
    title="SPI abort / tx status",
    annotations=ToolAnnotations(idempotentHint=True),
)
def si104_spi_control(
    serial: _Serial = None,
    action: Annotated[
        Literal["abort", "tx_status"],
        Field(description="abort cancels a pending long-TX; tx_status reports its state"),
    ] = "tx_status",
) -> ControlOut:
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


# No ToolAnnotations on purpose: the passthrough can do anything the
# firmware allows, so promising read-only/idempotent would be wrong.
@mcp.tool(title="Raw vendor request passthrough")
def si104_raw_request(
    serial: _Serial = None,
    interface: Annotated[
        int,
        Field(description="USB interface: 0-3 = I2C channels, 4 = SPI, 5 = maintenance; VCC commands need 0-3"),
    ] = 4,
    command: Annotated[
        int, Field(description="Vendor command ID 0..255 (known IDs listed in the description)")
    ] = 0,
    payload_hex: Annotated[
        _HexBytes, Field(description="Vendor request payload hex, at most 512 B")
    ] = "",
) -> RawRequestOut:
    """Raw vendor request passthrough to the control interface (interface 4) for firmware commands without a dedicated tool.

    Use when: a firmware command has no dedicated tool — EVENT_POLL=0x1B,
    VADDR_SET/GET=0x1C/0x1D, VCC threshold changes (0x1F with payload).
    Do NOT use when: a dedicated tool exists (they encode payloads correctly
    and validate faces) — hand-built payloads can disturb device state.

    Known IDs: I2C CONFIG=0x10, MASTER_XFER=0x11, SLAVE_READ=0x12,
    SLAVE_WRITE=0x13, ABORT/RESET=0x14/0x15, MAILBOX_PUSH/POP=0x16/0x17,
    REG_WRITE=0x18, REG_READ/REG_REPLY=0x19/0x1A, EVENT_POLL=0x1B,
    VADDR_SET/GET=0x1C/0x1D, VCC_SET=0x1E (u8 enable, u8 save),
    VCC_THRESHOLD=0x1F (empty query or u16 threshold_mv + u8 save),
    SPI_CONFIG=0x20, SPI_XFER=0x21, SPI_ABORT=0x22, SPI_TX_STATUS=0x26.
    payload and reply are each at most 512 bytes (SDK Guide section 7).
    """
    if not 0 <= command <= 255:
        raise ValueError(f"command must be 0..255, got {command}")
    payload = _parse_hex(payload_hex, "payload_hex")
    # SDK Guide section 7: request and reply are each at most 512 bytes.
    # (508 is the SI104 register-mirror write cap — do not confuse the two.)
    _check_payload("payload_hex", len(payload))
    _log_call("si104_raw_request", serial=serial, interface=interface, command=hex(command), payload_bytes=len(payload))

    def op(dev):
        reply = dev.request(interface, command, payload)
        return {"reply_hex": reply.hex(), "reply_bytes": len(reply)}

    return _with_device(serial, op, "si104_raw_request")


@mcp.tool(title="I2C slave buffer & mailbox")
def si104_i2c_slave(
    serial: _Serial = None,
    channel: _Channel = 0,
    action: Annotated[
        Literal["read", "write", "push", "pop"],
        Field(description='read/write = raw slave buffer; push/pop = mailbox FIFO (implies slave_mode=1)'),
    ] = "write",
    data_hex: Annotated[
        _HexBytes, Field(description="write/push payload hex, at most 256 B")
    ] = "",
    size: Annotated[int, Field(description="Bytes to fetch for read/pop, 1..512")] = 1,
    bus_hz: _BusHz = 100_000,
    mode: _BusMode = 0,
    slave_addr: Annotated[
        int, Field(description="Slave address 0..127 for this channel's slave role")
    ] = 0x30,
    slave_mode: Annotated[
        int, Field(description="Slave logic mode: 0 raw / 1 mailbox / 2 register (TM §8)")
    ] = 0,
    reg_addr_16bit: Annotated[
        bool, Field(description="Register-mode slave uses 16-bit register pointers")
    ] = False,
    data_16bit: Annotated[
        bool, Field(description="Register-mode slave uses 16-bit data words")
    ] = False,
    data_big_endian: Annotated[
        bool, Field(description="Register-mode slave words are big-endian")
    ] = False,
    timeout_ms: _I2cTimeout = 1000,
) -> SlaveOut:
    """I2C slave buffer and mailbox FIFO access on one channel (raw/mailbox/register logic modes).

    Use when: another I2C channel (or an external master) needs something to
    talk to — set up a loopback peer, preload a slave send buffer, or drain
    its mailbox FIFO.
    Do NOT use when: you are the master talking to a device — use
    si104_i2c_transfer / register tools instead.

    Verified on hardware: the firmware keeps channel config across USB
    sessions while powered, so a slave configured by an earlier call stays
    alive between MCP calls — master-side tools can talk to it in separate
    calls. The tool reuses an existing matching config (initialized + role=1
    + same slave_addr + same logic mode) instead of clobbering it; mismatch
    or missing config is (re)configured, then it polls GET_STATUS
    initialized=1 (up to ~1 s). slave_mode: 0 raw / 1 mailbox / 2 register
    (TM section 8).
    Buffer actions: action="write" sets the slave send buffer (what an
    external master reads next), up to 256 B; action="read" fetches bytes
    the external master wrote, up to 512 B — both hit the raw buffer path.
    Mailbox FIFO actions (imply slave_mode=1): action="push" queues bytes
    for the external master to pop, up to 256 B; action="pop" fetches bytes
    an external master pushed, up to 512 B. In mailbox mode a pure master
    write is pointer-phase first byte + mailbox payload.
    """
    if action not in ("read", "write", "push", "pop"):
        raise ValueError('action must be "read"/"write"/"push"/"pop"')
    # Mailbox FIFO commands imply mailbox logic mode; upgrade the default so
    # push/pop never silently operate on a raw/register channel.
    if action in ("push", "pop") and slave_mode == 0:
        slave_mode = 1
    data = _parse_hex(data_hex, "data_hex")
    if action in ("write", "push"):
        if not data:
            raise ValueError(f'data_hex is required for action="{action}"')
        if len(data) > 256:
            raise ValueError(f"slave write carries at most 256 bytes, got {len(data)}")
    if action in ("read", "pop") and not 1 <= size <= MAX_PAYLOAD:
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
        if action in ("read", "pop"):
            got = (
                dev.i2c_mailbox_pop(channel, size)
                if action == "pop"
                else dev.i2c_slave_read(channel, size)
            )
            return {
                "initialized": True,
                "action": action,
                "data_hex": got.hex(),
                "size": len(got),
                "status": _decode_i2c_status(dev.status(channel)),
            }
        if action == "push":
            dev.i2c_mailbox_push(channel, data)
            return {"initialized": True, "action": "push", "pushed_bytes": len(data)}
        # action == "write"
        dev.i2c_slave_write(channel, data)
        return {"initialized": True, "action": "write", "written_bytes": len(data)}

    return _with_device(serial, op, "si104_i2c_slave")


def main():
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
