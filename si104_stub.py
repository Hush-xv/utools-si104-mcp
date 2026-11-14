"""In-memory TEST DOUBLE for the utools_si104 vendor SDK.

shortcut: models only the SDK surface server.py uses and only the semantics
the tests rely on (register-mirror page scoping, mailbox append, NACK vs
NO_POWER vs wrong-face errors, VCC state, SPI loopback echo). It is NOT a
firmware emulator — any behavior not spelled out here is unspecified; when
real-hardware questions arise, run the hardware suites instead. Upgrade
trigger: a test needs vendor behavior this file does not model.

Activated by server.py only when SI104_MCP_STUB=1 and the vendor wheel is
missing. State lives at module level (one virtual dongle) to mimic the
firmware keeping channel/VCC config across USB sessions while powered.
"""
from __future__ import annotations

import logging
import struct

LOG = logging.getLogger("si104-stub")

STUB_SERIAL = "STUB-SI104"
VID, PID = 0x34B7, 0xE481  # official USB identity (SDK Guide section 2)
PAGE = 256  # TM section 8: register auto-increment is page-scoped


class Error(Exception):
    """Mirrors the vendor Error attributes the server relies on."""

    def __init__(self, message, code=-1, backend_code=0, firmware_status=0,
                 completion_unknown=False):
        super().__init__(message)
        self.code = code
        self.backend_code = backend_code
        self.firmware_status = firmware_status
        self.completion_unknown = completion_unknown


class SpiConfig:
    def __init__(self, data_bits=8, sclk_hz=1_000_000, timeout_ms=1000, cpol=0,
                 cpha=0, lsb_first=False, cs_active_high=False):
        self.data_bits = data_bits
        self.sclk_hz = sclk_hz
        self.timeout_ms = timeout_ms
        self.cpol = cpol
        self.cpha = cpha
        self.lsb_first = lsb_first
        self.cs_active_high = cs_active_high


class I2cConfig:
    def __init__(self, mode=0, bus_hz=100_000, slave_addr=0, timeout_ms=1000,
                 addr_10bit=False, role=0, slave_mode=0, reg_addr_16bit=False,
                 data_16bit=False, data_big_endian=False):
        self.mode = mode
        self.bus_hz = bus_hz
        self.slave_addr = slave_addr
        self.timeout_ms = timeout_ms
        self.addr_10bit = addr_10bit
        self.role = role
        self.slave_mode = slave_mode
        self.reg_addr_16bit = reg_addr_16bit
        self.data_16bit = data_16bit
        self.data_big_endian = data_big_endian


class _Bus:
    """One virtual dongle's persistent state."""

    def __init__(self):
        self.vcc_enabled = False  # shared VCC output defaults to off
        self.vcc_persist = False
        self.vcc_threshold_mv = 3000
        self.vcc_adc_mv = 3100
        # channel -> I2cConfig (firmware keeps config across USB sessions)
        self.i2c_channels = {}
        self.slaves = {}  # address -> {"mode", "mirror", "reg_ptr", ...}
        self.spi_config = None
        self.tx_words_count = 0
        self.long_tx_state = 0  # 0 idle .. 3 done_ok, 5 cancelled
        self.long_tx_bytes = 0
        self.req_counts = [0] * 6
        self.err_counts = [0] * 6


_BUS = _Bus()


def _page_read(mirror, ptr, size):
    """Register read with page-scoped auto-increment: bytes past the 256 B
    page end read as 0 (hardware-verified in stress_boundary_test B2)."""
    page, off = divmod(ptr, PAGE)
    return bytes(
        mirror[page * PAGE + off + i] if off + i < PAGE else 0 for i in range(size)
    )


def _page_write(mirror, ptr, data):
    page, off = divmod(ptr, PAGE)
    for i, b in enumerate(data):
        if off + i < PAGE:
            mirror[page * PAGE + off + i] = b


class Device:
    def __init__(self, serial=None):
        if serial is not None and serial != STUB_SERIAL:
            # SDK Guide section 8 error table: -2 = device not found.
            raise Error(f"SI104 not found (requested serial {serial!r})", code=-2)
        self.last_i2c_vcc = None

    def __enter__(self):
        LOG.info("device open (%s)", STUB_SERIAL)
        return self

    def __exit__(self, exc_type, exc, tb):
        LOG.info("device close")
        return False

    @classmethod
    def enumerate(cls):
        return [{"vid": VID, "pid": PID, "bus": 1, "address": 42, "serial": STUB_SERIAL}]

    # ------------------------------------------------------------ diagnostics
    def info(self):
        return {
            "serial": STUB_SERIAL,
            "vid": VID,
            "pid": PID,
            "firmware": "1.3-stub",
            "interfaces": 6,
        }

    def version(self):
        return b"\x01\x03"

    def runtime(self):
        return (0, sum(_BUS.req_counts), sum(_BUS.err_counts))

    def capabilities(self, interface):
        return {"interface": interface, "vendor_commands": True, "stub": True}

    def limits(self, interface):
        return {
            "interface": interface,
            "i2c_master_tx": 504,
            "i2c_rx": 512,
            "spi_tx": 504,
            "spi_rx": 512,
            "long_tx_words": 1024,
        }

    def status(self, interface):
        cfg = _BUS.i2c_channels.get(interface)
        slave = _BUS.slaves.get(cfg.slave_addr) if cfg and cfg.role == 1 else None
        fields = (
            cfg.role if cfg else 0,
            cfg.mode if cfg else 0,
            1 if (cfg and cfg.addr_10bit) else 0,
            1 if (slave and slave["initialized"]) else 0,
            cfg.bus_hz if cfg else 0,
            cfg.slave_addr if cfg else 0,
            cfg.timeout_ms if cfg else 0,
            _BUS.req_counts[interface],
            _BUS.err_counts[interface],
            slave["mode"] if slave else 0,
            2 if (slave and slave.get("reg_addr_16bit")) else 1,
            slave.get("reg_ptr", 0) if slave else 0,
            len(slave.get("mailbox_rx", b"")) if slave else 0,
            len(slave.get("mailbox_tx", b"")) if slave else 0,
            0,  # event_pending
            0,  # event_dropped
        )
        return struct.pack("<4B I 2H 2I 2B 5H", *fields)

    # ------------------------------------------------------- vendor requests
    def request(self, interface, command, payload=b""):
        _BUS.req_counts[interface] += 1
        if command in (0x1E, 0x1F):  # VCC lives on the I2C faces (TM section 5)
            if not 0 <= interface <= 3:
                raise Error("VCC command on non-I2C interface", code=-10, firmware_status=4)
            if command == 0x1E and len(payload) >= 2:
                _BUS.vcc_enabled = bool(payload[0])
                _BUS.vcc_persist = bool(payload[1])
                LOG.info("vcc set: enabled=%s persist=%s", _BUS.vcc_enabled, _BUS.vcc_persist)
                return b""
            if command == 0x1F and len(payload) >= 3:
                _BUS.vcc_threshold_mv = struct.unpack("<H", payload[:2])[0]
                return b""
            return struct.pack(
                "<BBHHBBH",
                1 if _BUS.vcc_enabled else 0,
                1 if _BUS.vcc_persist else 0,
                _BUS.vcc_threshold_mv,
                _BUS.vcc_adc_mv if _BUS.vcc_enabled else 0,
                0,
                0,
                _BUS.vcc_adc_mv if _BUS.vcc_enabled else 0,
            )
        if command == 0x26:  # SPI_TX_STATUS lives on the SPI face
            if interface != 4:
                raise Error("SPI command on non-SPI interface", code=-10, firmware_status=4)
            return self._pack_tx_status()
        return b""  # unmodeled vendor commands ack with an empty reply

    # -------------------------------------------------------------------- SPI
    def _pack_tx_status(self):
        return struct.pack(
            "<2B5HI",
            _BUS.long_tx_state,
            0,
            _BUS.long_tx_bytes,
            _BUS.long_tx_bytes,
            0,
            0,
            0,
            0,
        )

    def spi_configure(self, config):
        _BUS.spi_config = config

    def spi_transfer(self, tx, rx_size):
        if _BUS.spi_config is None:
            raise Error("SPI not configured", code=-7)
        _BUS.req_counts[4] += 1
        # D0<->D1 loopback semantics (stress B6): rx head echoes tx, tail is 0.
        return (bytes(tx) + b"\x00" * rx_size)[:rx_size]

    def spi_tx_words(self, words):
        if _BUS.spi_config is None:
            raise Error("SPI not configured", code=-7)
        data = b"".join(w.to_bytes(_BUS.spi_config.data_bits // 8, "big") for w in words)
        _BUS.tx_words_count = len(data) // (_BUS.spi_config.data_bits // 8)
        _BUS.long_tx_bytes = len(data)
        _BUS.long_tx_state = 3  # done_ok immediately (commit included)
        return _BUS.tx_words_count

    def spi_abort(self):
        _BUS.long_tx_state = 5  # cancelled
        return None  # real binding returns None — stay faithful

    def spi_tx_status(self):
        return self._pack_tx_status()

    # -------------------------------------------------------------------- I2C
    def i2c_configure(self, channel, config):
        _BUS.i2c_channels[channel] = config
        if config.role == 1:
            _BUS.slaves.setdefault(
                config.slave_addr,
                {
                    "mode": config.slave_mode,
                    "initialized": False,
                    "mirror": bytearray(4096),
                    "reg_ptr": 0,
                    "reg_addr_16bit": config.reg_addr_16bit,
                    "rx_buf": bytearray(),  # raw-mode incoming (0x12 SLAVE_READ)
                    "tx_buf": bytearray(),  # raw-mode outgoing (0x13 SLAVE_WRITE)
                    "mailbox_rx": bytearray(),  # mailbox incoming (0x17 POP)
                    "mailbox_tx": bytearray(),  # mailbox outgoing (0x16 PUSH)
                },
            )
            _BUS.slaves[config.slave_addr]["initialized"] = True

    def _fail(self, interface, msg, code, firmware_status=0):
        _BUS.err_counts[interface] += 1
        raise Error(msg, code=code, firmware_status=firmware_status)

    def i2c_transfer(self, channel, address, tx, rx_size, monitor_vcc=False):
        cfg = _BUS.i2c_channels.get(channel)
        if cfg is None or cfg.role != 0:
            self._fail(channel, "I2C master transfer on unconfigured channel", code=-7)
        if not _BUS.vcc_enabled:
            self._fail(channel, "NO_POWER: shared VCC output is off", code=-10,
                  firmware_status=11)
        _BUS.req_counts[channel] += 1
        slave = _BUS.slaves.get(address)
        if slave is None:
            self._fail(channel, f"no device at 0x{address:02X} (NACK)", code=-10,
                  firmware_status=7)
        if monitor_vcc:
            self.last_i2c_vcc = {
                "adc_mv": _BUS.vcc_adc_mv,
                "threshold_mv": _BUS.vcc_threshold_mv,
                "adc_valid": True,
                "output_enabled": _BUS.vcc_enabled,
            }
        tx = bytes(tx)
        if slave["mode"] == 2:  # register logic
            if tx:
                slave["reg_ptr"] = tx[0] if not slave["reg_addr_16bit"] \
                    else int.from_bytes(tx[:2], "big")
                data = tx[2:] if slave["reg_addr_16bit"] else tx[1:]
                _page_write(slave["mirror"], slave["reg_ptr"], data)
            return _page_read(slave["mirror"], slave["reg_ptr"], rx_size)
        if rx_size:
            src = slave["mailbox_tx"] if slave["mode"] == 1 else slave["tx_buf"]
            return bytes(src[:rx_size]).ljust(rx_size, b"\x00")
        # pure write into the slave receive side of the active logic mode
        if slave["mode"] == 1:  # mailbox: first byte is the pointer
            slave["mailbox_rx"] += tx[1:]
        else:  # raw
            slave["rx_buf"] += tx
        return b""

    def i2c_slave_read(self, channel, size):
        slave = _BUS.slaves.get(_BUS.i2c_channels[channel].slave_addr)
        got = bytes(slave["rx_buf"][:size])
        del slave["rx_buf"][: len(got)]
        return got

    def i2c_slave_write(self, channel, data):
        slave = _BUS.slaves.get(_BUS.i2c_channels[channel].slave_addr)
        slave["tx_buf"] = bytearray(data)
        return len(data)

    def i2c_mailbox_pop(self, channel, size):
        slave = _BUS.slaves.get(_BUS.i2c_channels[channel].slave_addr)
        got = bytes(slave["mailbox_rx"][:size])
        del slave["mailbox_rx"][: len(got)]
        return got

    def i2c_mailbox_push(self, channel, data):
        slave = _BUS.slaves.get(_BUS.i2c_channels[channel].slave_addr)
        slave["mailbox_tx"] += bytes(data)
        return len(data)

    def i2c_abort(self, channel):
        # Stateless model: nothing is ever in flight, so abort just counts.
        _BUS.req_counts[channel] += 1
        return None  # real binding returns None

    def i2c_reset(self, channel):
        # SDK Guide section 7: reset clears the channel configuration,
        # including a slave set up on that channel.
        cfg = _BUS.i2c_channels.pop(channel, None)
        if cfg is not None and cfg.role == 1:
            _BUS.slaves.pop(cfg.slave_addr, None)
        return None  # real binding returns None
