"""Unit tests for server.py logic and the tool surface — no hardware needed.

Injects si104_stub as the si104 module before importing server, so this runs
anywhere the `mcp` package installs (CI, machines without the vendor wheel).
Covers: hex parsing, the three binary decoders, every entry-validation path
(caps, mode/cpol/cpha ranges, 508 vendor-request cap), and the end-to-end
flows the stub models (NO_POWER recovery, scan, register roundtrip with
page scoping, mailbox slave, SPI echo, raw-request face checks).

Run: python unit_test.py   (exit 1 on any failure)
"""
import json
import struct
import sys
from unittest.mock import patch

import si104_stub

sys.modules["si104"] = si104_stub  # server imports si104 -> test double

import server  # noqa: E402  (must come after the stub injection)
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

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
    """FastMCP registers the plain function; unwrap if the decorator wrapped it.

    Device-error results surface as ToolError with the full structured dict
    as JSON text (the MCP isError=true path) — unwrap them back into the
    dict the checks expect, mirroring what MCP clients see.
    """
    fn = getattr(server, name)
    fn = fn.fn if hasattr(fn, "fn") else fn

    def invoke(**kw):
        try:
            return fn(**kw)
        except ToolError as e:
            return json.loads(str(e))

    return invoke


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
    "<4B I 2H 2I 2B 5H", 1, 0, 0, 1, 400_000, 0x45, 1000, 5, 1, 2, 8, 0x10, 3, 0, 0, 0
)
d = server._decode_i2c_status(raw)
check("decode i2c_status", d["role"] == 1 and d["initialized"] is True
      and d["bus_hz"] == 400_000 and d["slave_logic_mode"] == 2 and d["reg_addr_width"] == 8
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
expect_value_error("spi sclk below 50k",
                   lambda: tool("si104_spi_transfer")(sclk_hz=49_999), "sclk_hz")
expect_value_error("spi sclk above 40M",
                   lambda: tool("si104_spi_transfer")(sclk_hz=40_000_001), "sclk_hz")
expect_value_error("spi timeout 0",
                   lambda: tool("si104_spi_transfer")(timeout_ms=0), "timeout_ms")
expect_value_error("spi timeout 60001",
                   lambda: tool("si104_spi_transfer")(timeout_ms=60_001), "timeout_ms")
expect_value_error("spi data_bits 0",
                   lambda: tool("si104_spi_transfer")(data_bits=0), "data_bits")
expect_value_error("spi data_bits 33",
                   lambda: tool("si104_spi_transfer")(data_bits=33), "data_bits")
expect_value_error("spi tx 505B",
                   lambda: tool("si104_spi_transfer")(tx_hex="00" * 505), "504")
expect_value_error("spi data_bits 33: range wins over alignment",
                   lambda: tool("si104_spi_transfer")(tx_hex="00" * 5, data_bits=33), "1..32")
expect_value_error("spi rx 513B",
                   lambda: tool("si104_spi_transfer")(rx_size=513), "rx_size")
expect_value_error("spi 16-bit odd tx",
                   lambda: tool("si104_spi_transfer")(tx_hex="000000", data_bits=16), "multiple")
for bits, word_bytes in ((9, 2), (17, 3), (25, 4), (31, 4)):
    expect_value_error(
        f"spi {bits}-bit partial word",
        lambda n=word_bytes, b=bits: tool("si104_spi_transfer")(
            tx_hex="00" * (n - 1), rx_size=0, data_bits=b
        ),
        f"multiples of {word_bytes}",
    )
    d = tool("si104_spi_transfer")(
        tx_hex="00" * word_bytes, rx_size=word_bytes, data_bits=bits
    )
    check(f"spi {bits}-bit complete word accepted",
          d.get("rx_hex") == "00" * word_bytes, str(d)[:90])
expect_value_error("tx_words data_bits=12",
                   lambda: tool("si104_spi_tx_words")(tx_hex="00", data_bits=12), "8/16/32")
expect_value_error("F3 tx_words cpha=3",
                   lambda: tool("si104_spi_tx_words")(tx_hex="00", cpha=3), "cpha")
expect_value_error("tx_words 1025 words",
                   lambda: tool("si104_spi_tx_words")(tx_hex="00" * 1025, data_bits=8), "1024")
expect_value_error("raw command range",
                   lambda: tool("si104_raw_request")(command=256), "command")
expect_value_error("raw payload 513B (cap 512, SDK Guide s7)",
                   lambda: tool("si104_raw_request")(payload_hex="00" * 513), "512")
expect_value_error("vcc_control channel",
                   lambda: tool("si104_vcc_control")(channel=4), "channel")
expect_value_error("i2c_control bad action",
                   lambda: tool("si104_i2c_control")(action="frobnicate"), "abort")
expect_value_error("i2c_control bad channel",
                   lambda: tool("si104_i2c_control")(channel=4), "channel")
expect_value_error("mailbox push 257B",
                   lambda: tool("si104_i2c_slave")(action="push", data_hex="00" * 257), "256")
expect_value_error("slave mode out of range",
                   lambda: tool("si104_i2c_slave")(action="configure", slave_mode=3), "slave_mode")
expect_value_error("mailbox action rejected in register mode",
                   lambda: tool("si104_i2c_slave")(action="push", slave_mode=2, data_hex="AA"), "slave_mode")
expect_value_error("device_info channel",
                   lambda: tool("si104_device_info")(channel=4), "channel")

# ------------------------------------------------------- end-to-end over stub
d = tool("si104_enumerate")()
check("enumerate finds stub", d["count"] == 1
      and d["devices"][0]["serial"] == si104_stub.STUB_SERIAL, str(d))

d = tool("si104_i2c_transfer")(channel=0, address=0x45, rx_size=1)
check("NO_POWER before enable", d.get("firmware_status") == 11
      and d.get("code_meaning") == "device rejected the request"
      and "si104_vcc_control" in d.get("hint", ""), str(d)[:90])

d = tool("si104_i2c_scan")(channel=0)
check("scan aborts on NO_POWER", d.get("firmware_status") == 11
      and d.get("found_before_abort") == []
      and d.get("aborted_at") == "0x08", str(d)[:90])

vcc_control_reply = tool("si104_vcc_control")(channel=0, enable=True)
check("vcc_control enables rail", vcc_control_reply.get("output_enabled") is True
      and vcc_control_reply.get("requested_enable") is True, str(vcc_control_reply)[:90])

d = tool("si104_read_vcc")(channel=0)
check("read_vcc after enable", d.get("output_enabled") is True
      and d.get("threshold_mv") == 3000, str(d)[:90])
raw = tool("si104_raw_request")(interface=0, command=0x1F)
check("VCC read and control return actual reply bytes",
      d.get("raw_hex") == raw["reply_hex"] == vcc_control_reply.get("raw_hex"))

with patch.object(si104_stub.Device, "request", side_effect=[b"", bytes.fromhex("0000b80b000000000000")]) as request:
    d = tool("si104_vcc_control")(channel=0, enable=True)
    check("VCC control rejects opposite state with readback preserved",
          d.get("error") and d.get("output_enabled") is False
          and d.get("raw_hex") == "0000b80b000000000000" and request.call_count == 2)

with patch.object(si104_stub.Device, "request", side_effect=[b"", b"\x01"]) as request:
    d = tool("si104_vcc_control")(channel=0, enable=True)
    check("VCC verification failure is uncertain and does not repeat the command",
          d.get("completion_unknown") is True and d.get("code") == -6
          and d.get("raw_hex") == "01" and request.call_count == 2)

d = tool("si104_raw_request")(interface=4, command=0x1F)
check("raw 0x1F on SPI face rejected", "error" in d and d.get("firmware_status") == 4,
      str(d)[:90])

d = tool("si104_i2c_slave")(channel=1, action="write", slave_addr=0x45,
                            slave_mode=2, data_hex="A1A2A3")
check("register slave ch1 configured", d.get("written_bytes") == 3, str(d)[:90])

d = tool("si104_i2c_scan")(channel=0)
check("scan finds 0x45", d.get("found") == ["0x45"], str(d))

with patch.object(si104_stub.Device, "i2c_transfer",
                  side_effect=si104_stub.Error("bus held low", code=-10, backend_code=23,
                                               firmware_status=8, completion_unknown=True)):
    d = tool("si104_i2c_scan")(channel=0, first=0x08, last=0x08)
    check("scan timeout abort keeps full error and address",
          d.get("aborted_at") == "0x08" and d.get("found_before_abort") == []
          and d.get("firmware_status") == 8 and d.get("backend_code") == 23
          and d.get("completion_unknown") is True and "retry_hint" in d
          and "timed out" in d.get("hint", "").lower(), str(d)[:160])

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

d = tool("si104_i2c_slave")(channel=1, action="write", slave_addr=0x45,
                            slave_mode=0, data_hex="A1")
raw_status = tool("si104_device_info")(channel=1).get("i2c", {}).get("status", {})
check("register slave switches to raw mode", d.get("written_bytes") == 1
      and raw_status.get("slave_logic_mode") == 0, str(raw_status)[:90])
tool("si104_i2c_transfer")(channel=0, address=0x45, tx_hex="D00D", rx_size=0)
d = tool("si104_i2c_slave")(channel=1, action="read", slave_addr=0x45,
                            slave_mode=0, size=2)
check("raw buffer read after register-to-raw switch", d.get("data_hex") == "d00d", str(d)[:90])

d = tool("si104_i2c_slave")(channel=1, action="write", slave_addr=0x45,
                            slave_mode=2, data_hex="00")
reg_status = tool("si104_device_info")(channel=1).get("i2c", {}).get("status", {})
check("raw slave switches back to register mode", d.get("written_bytes") == 1
      and reg_status.get("slave_logic_mode") == 2, str(reg_status)[:90])
d = tool("si104_i2c_read_regs")(channel=0, address=0x45, reg=0x10, size=4)
check("register mirror remains accessible after mode switch", d.get("data_hex") == "deadbeef", str(d))

saved_buffer = bytes(si104_stub._BUS.slaves[0x45]["tx_buf"])
d = tool("si104_i2c_slave")(channel=1, action="configure", slave_addr=0x45,
                            slave_mode=2, reg_addr_16bit=True)
check("configure-only changes pointer width without writing buffer",
      d.get("action") == "configure" and d.get("status", {}).get("reg_addr_width") == 16
      and bytes(si104_stub._BUS.slaves[0x45]["tx_buf"]) == saved_buffer, str(d)[:120])
d = tool("si104_i2c_slave")(channel=1, action="configure", slave_addr=0x45,
                            slave_mode=2, reg_addr_16bit=False)
check("configure-only restores 8-bit pointer",
      d.get("status", {}).get("reg_addr_width") == 8, str(d)[:120])

old_config = si104_stub._BUS.i2c_channels[1]
d = tool("si104_i2c_slave")(channel=1, action="configure", slave_addr=0x45,
                            slave_mode=2, data_16bit=True)
check("configure applies changed word format on matching address",
      d.get("initialized") is True and si104_stub._BUS.i2c_channels[1] is not old_config
      and si104_stub._BUS.i2c_channels[1].data_16bit is True, str(d)[:120])

d = tool("si104_i2c_slave")(channel=2, action="push", slave_addr=0x50,
                            slave_mode=1, data_hex="AABB")
check("mailbox push", d.get("action") == "push" and d.get("pushed_bytes") == 2, str(d)[:90])

tool("si104_i2c_transfer")(channel=0, address=0x50, tx_hex="010203", rx_size=0)
d = tool("si104_i2c_slave")(channel=2, action="pop", slave_addr=0x50,
                            slave_mode=1, size=2)
check("mailbox pop: first write byte is pointer", d.get("data_hex") == "0203", str(d))

d = tool("si104_i2c_transfer")(channel=0, address=0x50, tx_hex="", rx_size=2)
check("master reads pushed mailbox", d.get("rx_hex") == "aabb", str(d))

d = tool("si104_spi_transfer")(tx_hex="01020304", rx_size=6)
check("spi loopback echo + zero tail", d.get("rx_hex") == "010203040000", str(d))

d = tool("si104_spi_tx_words")(tx_hex="00010203", data_bits=8)
check("tx_words count", d.get("words") == 4, str(d))

d = tool("si104_spi_control")(action="tx_status")
check("tx_status done_ok", d.get("state") == "done_ok" and d.get("total_bytes") == 4,
      str(d)[:90])

d = tool("si104_spi_control")(action="abort")
check("abort accepted", d.get("action") == "abort", str(d))
d = tool("si104_spi_control")(action="tx_status")
check("abort preserves completed SPI status", d.get("state") == "done_ok", str(d)[:90])

d = tool("si104_raw_request")(interface=0, command=0x1F)
check("raw 0x1F on I2C face", d.get("reply_bytes") == 10, str(d))

d = tool("si104_raw_request")(interface=4, command=0x26)
check("raw 0x26 on SPI face", d.get("reply_bytes") == 16, str(d))

d = tool("si104_device_info")(channel=1)
st = d.get("i2c", {}).get("status", {})
check("device_info per-face", "spi" in d and "i2c" in d
      and st.get("role") == 1 and st.get("slave_addr") == 0x45
      and st.get("initialized") is True, str(st)[:90])

d = tool("si104_device_info")()
check("device_info vcc rail", d.get("vcc", {}).get("output_enabled") is True
      and d.get("vcc", {}).get("channel") == 0, str(d.get("vcc"))[:90])

with patch.object(si104_stub.Device, "version", side_effect=si104_stub.Error("version read failed", code=-8)):
    with patch.object(si104_stub.Device, "runtime", side_effect=RuntimeError("runtime unavailable")):
        d = tool("si104_device_info")()
        try:
            server.DeviceInfoOut.model_validate(d)
            typed_ok = True
        except Exception:
            typed_ok = False
        check("device_info section failures keep valid output schema",
              typed_ok and d.get("version_hex") is None and d.get("runtime") is None
              and d.get("section_errors", {}).get("version_hex", {}).get("code") == -8
              and "runtime" in d.get("section_errors", {}) and "i2c" in d, str(d)[:130])

d = tool("si104_i2c_control")(channel=0, action="abort")
check("i2c_control abort", d.get("action") == "abort" and d.get("channel") == 0, str(d))

tool("si104_i2c_slave")(channel=3, action="write", slave_addr=0x55,
                        slave_mode=0, data_hex="AA")
st = tool("si104_device_info")(channel=3).get("i2c", {}).get("status", {})
check("slave ch3 configured", st.get("role") == 1 and st.get("initialized") is True,
      str(st)[:80])
tool("si104_i2c_control")(channel=3, action="reset")
st = tool("si104_device_info")(channel=3).get("i2c", {}).get("status", {})
check("i2c reset clears channel config", st.get("role") == 0
      and st.get("initialized") is False, str(st)[:80])

tool("si104_i2c_slave")(channel=3, action="configure", slave_addr=0x55, slave_mode=0)
d = tool("si104_i2c_scan")(channel=0, first=0x55, last=0x55)
check("slave answers while its channel is configured", d.get("found") == ["0x55"], str(d))
tool("si104_i2c_scan")(channel=3, first=0x55, last=0x55)
d = tool("si104_i2c_scan")(channel=0, first=0x55, last=0x55)
check("master reconfiguration removes same-channel slave ACK", d.get("found") == [], str(d))

d = tool("si104_read_vcc")(serial="NOPE")
check("unknown serial -> structured error", "error" in d
      and d.get("code_meaning") == "device not found"
      and "not found" in d["error"], str(d)[:90])

# ------------------------------------------------- review-fix regressions
# Stub "SPI not configured" must report -7 (unsupported), not -2 (not found).
saved_spi = si104_stub._BUS.spi_config
si104_stub._BUS.spi_config = None
try:
    si104_stub.Device().spi_tx_words((1,))
    check("stub tx_words unconfigured -> -7", False, "no error")
except si104_stub.Error as e:
    check("stub tx_words unconfigured -> -7", e.code == -7, str(e)[:60])
si104_stub._BUS.spi_config = saved_spi


def _usb_boom():
    raise si104_stub.Error("usb stack hiccup", code=-8)


orig_enumerate = si104_stub.Device.enumerate
si104_stub.Device.enumerate = staticmethod(_usb_boom)
# tool() unwraps ToolError back into the structured dict, mirroring MCP clients.
d = tool("si104_enumerate")()
check("enumerate failure structured", d.get("code") == -8
      and d.get("code_meaning") == "USB or other IO error", str(d)[:80])
si104_stub.Device.enumerate = orig_enumerate

fails = [n for n, ok in RESULTS if not ok]
print(f"\n== summary == {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS"
      + (f"; FAIL: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
