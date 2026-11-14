"""Hardware validation run: real MCP stdio session against a live dongle.

Covers the pre-hardware checklist: enumerate -> device_info (status decode
+ limits) -> read_vcc + raw_request(0x1F) cross-check -> i2c_scan -> SPI
api-level smoke (no wires attached).
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name("server.py")


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args=None):
                res = await session.call_tool(name, args or {})
                text = res.content[0].text
                if text.startswith("Error executing tool"):  # SDK ToolError wrap
                    text = text.split(": ", 1)[1]
                return json.loads(text)

            print("== enumerate ==")
            print(await call("si104_enumerate"))

            print("== device_info ==")
            info = await call("si104_device_info")
            print(json.dumps(info, indent=2, ensure_ascii=False))

            print("== read_vcc ==")
            print(await call("si104_read_vcc"))

            print("== raw_request(0x1F) VCC cross-check ==")
            raw = await call("si104_raw_request", {"interface": 0, "command": 0x1F})
            print(raw)

            print("== i2c_scan ch0 ==")
            print(await call("si104_i2c_scan", {"channel": 0}))

            print("== spi_transfer tx-only (no wires: API-level) ==")
            print(await call(
                "si104_spi_transfer",
                {"tx_hex": "A55AA55A", "rx_size": 0, "sclk_hz": 1_000_000},
            ))

            print("== spi_control tx_status ==")
            print(await call("si104_spi_control", {"action": "tx_status"}))

            print("== spi_tx_words 4 words ==")
            print(await call(
                "si104_spi_tx_words",
                {"tx_hex": "00010203", "data_bits": 8, "sclk_hz": 1_000_000},
            ))

            print("== i2c_control abort ch0 ==")
            print(await call("si104_i2c_control", {"channel": 0, "action": "abort"}))

            print("== i2c_control reset ch3 (spare channel) ==")
            print(await call("si104_i2c_control", {"channel": 3, "action": "reset"}))

            print("== slave mailbox push ch1@0x45 (reconfigures ch1 to mailbox mode) ==")
            print(await call("si104_i2c_slave", {
                "channel": 1, "action": "push", "slave_addr": 0x45,
                "slave_mode": 1, "data_hex": "A5A5"}))

            print("== slave mailbox pop ch1@0x45 ==")
            print(await call("si104_i2c_slave", {
                "channel": 1, "action": "pop", "slave_addr": 0x45,
                "slave_mode": 1, "size": 4}))

            print("== device_info vcc section ==")
            print(await call("si104_device_info", {"channel": 0}))

    print("HW RUN DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
