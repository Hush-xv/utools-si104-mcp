"""Smoke test: spawn server.py over stdio, initialize, list tools, call a few.

No hardware required for the no-device paths; with the dongle plugged in
it also exercises the real open path. Without the vendor wheel the server
falls back to si104_stub (STUB MODE) so the tool surface stays testable
anywhere — behavior then comes from the test double, not firmware.
"""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name("server.py")

# Probe without importing: the vendor wheel loads a native DLL as a side effect.
HAVE_REAL_SDK = importlib.util.find_spec("si104") is not None


async def main() -> int:
    env = None
    if not HAVE_REAL_SDK:
        print("NOTE: vendor wheel not installed — running server in STUB MODE "
              "(si104_stub test double, not real firmware)")
        env = {**os.environ, "SI104_MCP_STUB": "1"}
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.serverInfo.name, "proto:", init.protocolVersion)

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            required = {
                "si104_enumerate", "si104_device_info", "si104_spi_transfer",
                "si104_spi_tx_words", "si104_spi_control", "si104_i2c_transfer",
                "si104_i2c_read_regs", "si104_i2c_write_regs", "si104_i2c_scan",
                "si104_i2c_control", "si104_i2c_slave", "si104_raw_request",
                "si104_read_vcc", "si104_vcc_control",
            }
            missing = required - set(names)
            assert not missing, f"missing tools: {missing}"

            res = await session.call_tool("si104_enumerate", {})
            payload = json.loads(res.content[0].text)
            print("enumerate:", payload)
            assert "count" in payload

            res = await session.call_tool("si104_device_info", {})
            payload = json.loads(res.content[0].text)
            print("device_info:", payload)

            res = await session.call_tool(
                "si104_spi_transfer", {"tx_hex": "0000", "rx_size": 2}
            )
            payload = json.loads(res.content[0].text)
            print("spi_transfer:", payload)
            # no dongle -> structured device error, with dongle -> rx reply
            assert "error" in payload or "rx_hex" in payload

            res = await session.call_tool(
                "si104_raw_request", {"command": 0x1F}
            )  # VCC_THRESHOLD query
            payload = json.loads(res.content[0].text)
            print("raw_request:", payload)
            assert "error" in payload or "reply_hex" in payload

            res = await session.call_tool("si104_read_vcc", {})
            payload = json.loads(res.content[0].text)
            print("read_vcc:", payload)
            assert "error" in payload or "measured_mv" in payload

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
