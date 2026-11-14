"""Unit tests for server.py logic and the tool surface — no hardware needed.

Injects si104_stub as the si104 module before importing server, so this runs
anywhere the `mcp` package installs (CI, machines without the vendor wheel).
Covers: hex parsing, the three binary decoders, every entry-validation path
(caps, mode/cpol/cpha ranges, 508 vendor-request cap), and the end-to-end
flows the stub models (NO_POWER recovery, scan, register roundtrip with
page scoping, mailbox slave, SPI echo, raw-request face checks).

Run: python unit_test.py   (exit 1 on any failure)
"""
import struct
import sys

import si104_stub

sys.modules["si104"] = si104_stub  # server imports si104 -> test double

import server  # noqa: E402  (must come after the stub injection)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def expect_value_error(name, fn, needle=None):
    try:
        fn()
    except ValueError as e:
        check(name, needle is None or needle in str(e), str(e)[:70])
        return
    check(name, False, "no ValueError raised")


def tool(name):
    """FastMCP registers the plain function; unwrap if the decorator wrapped it."""
    fn = getattr(server, name)
    return fn.fn if hasattr(fn, "fn") else fn


# ------------------------------------------------------------- hex + decoders
check("parse_hex None", server._parse_hex(None, "x") == b"")
check("parse_hex empty", server._parse_hex("", "x") == b"")
check("parse_hex spaced", server._parse_hex("DE AD", "x") == b"\xde\xad")
check("parse_hex 0x prefix", server._parse_hex("0xDEADBEEF", "x") == b"\xde\xad\xbe\xef")
expect_value_error("parse_hex invalid", lambda: server._parse_hex("zz", "tx_hex"), "tx_hex")

raw = struct.pack("<2B5HI", 3, 0, 1024, 1024, 1, 1, 0, 77)
d = server._decode_spi_tx_status(raw)
check("decode spi_tx_status", d["state"] == "done_ok" and d["total_bytes"] == 1024
      and d["session_id"] == 77, str(d))
check("decode spi_tx_status short", "raw_hex" in server._decode_spi_tx_status(b"\x01"))

raw = struct.pack(
    "<4B I 2H 2I 2B 5H", 1, 0, 0, 1, 400_000, 0x45, 1000, 5, 1, 2, 1, 0x10, 3, 0, 0, 0
)
d = server._decode_i2c_status(raw)
check("decode i2c_status", d["role"] == 1 and d["initialized"] is True
      and d["bus_hz"] == 400_000 and d["slave_logic_mode"] == 2
      and d["mailbox_rx_len"] == 3, str(d))
check("decode i2c_status short", "raw_hex" in server._decode_i2c_status(b"\x00" * 8))

raw = struct.pack("<BBHHBBH", 1, 0, 3000, 3100, 0, 0, 3101)
d = server._decode_vcc_reply(raw)
check("decode vcc reply", d and d["output_enabled"] is True
      and d["threshold_mv"] == 3000 and d["measured_mv"] == 3101, str(d))
check("decode vcc short -> None", server._decode_vcc_reply(b"\x00" * 9) is None)

# ------------------------------------------------------- entry validation (P1)
expect_value_error("F1 i2c mode=5",
                   lambda: tool("si104_i2c_transfer")(mode=5))
expect_value_error("i2c mode=3 bus_hz low",
                   lambda: tool("si104_i2c_transfer")(mode=3, bus_hz=5000), "bus_hz")
expect_value_error("i2c timeout u16",
                   lambda: tool("si104_i2c_transfer")(timeout_ms=65536), "timeout_ms")
expect_value_error("i2c 7-bit addr range",
                   lambda: tool("si104_i2c_transfer")(address=128), "address")
expect_value_error("i2c tx 505B",
                   lambda: tool("si104_i2c_transfer")(tx_hex="00" * 505), "504")
expect_value_error("i2c rx 513B",
                   lambda: tool("si104_i2c_transfer")(rx_size=513), "rx_size")
expect_value_error("read_regs reg 256",
                   lambda: tool("si104_i2c_read_regs")(reg=256), "reg")
expect_value_error("write_regs empty data",
                   lambda: tool("si104_i2c_write_regs")(data_hex=""), "data_hex")
expect_value_error("write_regs 16-bit ptr+data > 504",
                   lambda: tool("si104_i2c_write_regs")(reg=0, reg_addr_16bit=True,
                                                        data_hex="00" * 503), "504")
expect_value_error("scan range low",
                   lambda: tool("si104_i2c_scan")(first=0x07), "0x08")
expect_value_error("slave bad action",
                   lambda: tool("si104_i2c_slave")(action="frobnicate"), "action")
expect_value_error("slave write 257B",
                   lambda: tool("si104_i2c_slave")(action="write", data_hex="00" * 257), "256")
expect_value_error("slave read size 0",
                   lambda: tool("si104_i2c_slave")(action="read", size=0), "size")
expect_value_error("F3 spi cpol=2",
                   lambda: tool("si104_spi_transfer")(cpol=2), "cpol")
expect_value_error("spi tx 505B",
                   lambda: tool("si104_spi_transfer")(tx_hex="00" * 505), "504")
expect_value_error("spi rx 513B",
                   lambda: tool("si104_spi_transfer")(rx_size=513), "rx_size")
expect_value_error("spi 16-bit odd tx",
                   lambda: tool("si104_spi_transfer")(tx_hex="000000", data_bits=16), "multiple")
expect_value_error("tx_words data_bits=12",
                   lambda: tool("si104_spi_tx_words")(tx_hex="00", data_bits=12), "8/16/32")
expect_value_error("F3 tx_words cpha=3",
                   lambda: tool("si104_spi_tx_words")(tx_hex="00", cpha=3), "cpha")
expect_value_error("tx_words 1025 words",
                   lambda: tool("si104_spi_tx_words")(tx_hex="00" * 1025, data_bits=8), "1024")
expect_value_error("raw command range",
                   lambda: tool("si104_raw_request")(command=256), "command")
expect_value_error("F2 raw payload 509B",
                   lambda: tool("si104_raw_request")(payload_hex="00" * 509), "508")
expect_value_error("vcc_control channel",
                   lambda: tool("si104_vcc_control")(channel=4), "channel")
expect_value_error("device_info channel",
                   lambda: tool("si104_device_info")(channel=4), "channel")

# ------------------------------------------------------- end-to-end over stub
d = tool("si104_enumerate")()
check("enumerate finds stub", d["count"] == 1
      and d["devices"][0]["serial"] == si104_stub.STUB_SERIAL, str(d))

d = tool("si104_i2c_transfer")(channel=0, address=0x45, rx_size=1)
check("NO_POWER before enable", d.get("firmware_status") == 11
      and "si104_vcc_control" in d.get("hint", ""), str(d)[:90])

d = tool("si104_i2c_scan")(channel=0)
check("scan aborts on NO_POWER", d.get("firmware_status") == 11
      and d.get("found_before_abort") == [], str(d)[:90])

d = tool("si104_vcc_control")(channel=0, enable=True)
check("vcc_control enables rail", d.get("output_enabled") is True
      and d.get("requested_enable") is True, str(d)[:90])

d = tool("si104_read_vcc")(channel=0)
check("read_vcc after enable", d.get("output_enabled") is True
      and d.get("threshold_mv") == 3000, str(d)[:90])

d = tool("si104_raw_request")(interface=4, command=0x1F)
check("raw 0x1F on SPI face rejected", "error" in d and d.get("firmware_status") == 4,
      str(d)[:90])

d = tool("si104_i2c_slave")(channel=1, action="write", slave_addr=0x45,
                            slave_mode=2, data_hex="A1A2A3")
check("register slave ch1 configured", d.get("written_bytes") == 3, str(d)[:90])

d = tool("si104_i2c_scan")(channel=0)
check("scan finds 0x45", d.get("found") == ["0x45"], str(d))

d = tool("si104_i2c_write_regs")(channel=0, address=0x45, reg=0x10, data_hex="DEADBEEF")
check("write_regs ok", d.get("written_bytes") == 4, str(d))

d = tool("si104_i2c_read_regs")(channel=0, address=0x45, reg=0x10, size=4)
check("read_regs roundtrip", d.get("data_hex") == "deadbeef", str(d))

tool("si104_i2c_write_regs")(channel=0, address=0x45, reg=0xFE, data_hex="04050607")
d = tool("si104_i2c_read_regs")(channel=0, address=0x45, reg=0xFE, size=4)
check("auto-increment page-scoped", d.get("data_hex") == "04050000", str(d))

d = tool("si104_i2c_transfer")(channel=0, address=0x45, tx_hex="201122", rx_size=2)
check("write+repeated-start read", d.get("rx_hex") == "1122", str(d))

d = tool("si104_i2c_transfer")(channel=0, address=0x45, tx_hex="30", rx_size=2,
                               monitor_vcc=True)
check("monitor_vcc dict", d.get("vcc", {}).get("adc_valid") is True
      and d.get("vcc", {}).get("output_enabled") is True, str(d)[:90])

d = tool("si104_i2c_slave")(channel=2, action="write", slave_addr=0x50,
                            slave_mode=1, data_hex="AABB")
tool("si104_i2c_transfer")(channel=0, address=0x50, tx_hex="010203", rx_size=0)
d = tool("si104_i2c_slave")(channel=2, action="read", slave_addr=0x50,
                            slave_mode=1, size=2)
check("mailbox: first write byte is pointer", d.get("data_hex") == "0203", str(d))

d = tool("si104_spi_transfer")(tx_hex="01020304", rx_size=6)
check("spi loopback echo + zero tail", d.get("rx_hex") == "010203040000", str(d))

d = tool("si104_spi_tx_words")(tx_hex="00010203", data_bits=8)
check("tx_words count", d.get("words") == 4, str(d))

d = tool("si104_spi_control")(action="tx_status")
check("tx_status done_ok", d.get("state") == "done_ok" and d.get("total_bytes") == 4,
      str(d)[:90])

d = tool("si104_spi_control")(action="abort")
check("abort accepted", d.get("action") == "abort", str(d))

d = tool("si104_raw_request")(interface=0, command=0x1F)
check("raw 0x1F on I2C face", d.get("reply_bytes") == 10, str(d))

d = tool("si104_raw_request")(interface=4, command=0x26)
check("raw 0x26 on SPI face", d.get("reply_bytes") == 16, str(d))

d = tool("si104_device_info")(channel=1)
st = d.get("i2c", {}).get("status", {})
check("device_info per-face", "spi" in d and "i2c" in d
      and st.get("role") == 1 and st.get("slave_addr") == 0x45
      and st.get("initialized") is True, str(st)[:90])

d = tool("si104_read_vcc")(serial="NOPE")
check("unknown serial -> structured error", "error" in d
      and "not found" in d["error"], str(d)[:90])

fails = [n for n, ok in RESULTS if not ok]
print(f"\n== summary == {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS"
      + (f"; FAIL: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
