"""SPI loopback on D0<->D1 shorted wire, over a real MCP stdio session.

- 8-bit pattern: basic D0->D1 full-duplex path
- 16/32-bit word patterns: settles the >8-bit word packing order dispute
  (rx byte order reveals how firmware loads word values into the shift
  register; spi_tx_words shares that path)
- long-TX commit + tx_status with the wire attached
"""
import os
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name("server.py")


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                   env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args=None):
                res = await session.call_tool(name, args or {})
                text = res.content[0].text
                if text.startswith("Error executing tool"):  # SDK ToolError wrap
                    text = text.split(": ", 1)[1]
                return json.loads(text)

            print("8-bit pattern    :", await call(
                "si104_spi_transfer", {"tx_hex": "A55AC35C", "rx_size": 4}))
            print("16-bit words     :", await call(
                "si104_spi_transfer",
                {"tx_hex": "DEADBEEF", "rx_size": 4, "data_bits": 16}))
            print("32-bit word      :", await call(
                "si104_spi_transfer",
                {"tx_hex": "01234567", "rx_size": 4, "data_bits": 32}))
            print("16-bit lsb_first :", await call(
                "si104_spi_transfer",
                {"tx_hex": "DEADBEEF", "rx_size": 4, "data_bits": 16, "lsb_first": True}))
            print("tx_words commit  :", await call(
                "si104_spi_tx_words", {"tx_hex": "DEADBEEF", "data_bits": 16}))
            print("tx_status        :", await call(
                "si104_spi_control", {"action": "tx_status"}))

    print("SPI LOOPBACK RUN DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
