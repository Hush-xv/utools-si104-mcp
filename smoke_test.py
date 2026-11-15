"""Smoke test: spawn server.py over stdio, initialize, list tools, call a few.

Always starts the in-memory SDK test double. Real USB checks belong in the
explicit hardware scripts; this smoke test must be safe on developer machines.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name("server.py")

def parse(res):
    """CallToolResult text -> dict. The SDK prefixes tool-raised ToolError
    messages with "Error executing tool <name>: "; the structured JSON
    payload follows it verbatim."""
    text = res.content[0].text
    if text.startswith("Error executing tool"):
        text = text.split(": ", 1)[1]
    return json.loads(text)


async def main() -> int:
    print("NOTE: running in STUB MODE (si104_stub test double, not real firmware)")
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

            # MCP spec conformance: every tool declares a structured-output
            # schema; enumerate is read-only with a display title.
            assert all(t.outputSchema is not None for t in tools.tools), \
                "tools missing outputSchema"
            enum_tool = next(t for t in tools.tools if t.name == "si104_enumerate")
            assert enum_tool.annotations and enum_tool.annotations.readOnlyHint is True
            assert enum_tool.title == "List SI104 dongles on USB"

            # Input-side conformance: enum-constrained choices and per-parameter
            # descriptions (the model sees the inputSchema, not the docstring).
            ctl = next(t for t in tools.tools if t.name == "si104_i2c_control")
            assert "enum" in ctl.inputSchema["properties"]["action"], "action enum missing"
            assert "description" in ctl.inputSchema["properties"]["channel"]
            tw = next(t for t in tools.tools if t.name == "si104_spi_tx_words")
            assert tw.inputSchema["properties"]["data_bits"].get("enum") == [8, 16, 32]

            res = await session.call_tool("si104_enumerate", {})
            payload = parse(res)
            print("enumerate:", payload)
            assert "count" in payload
            assert res.structuredContent is not None, "missing structuredContent"
            assert res.structuredContent.get("count") == payload["count"]

            res = await session.call_tool("si104_device_info", {})
            payload = parse(res)
            print("device_info:", payload)

            res = await session.call_tool(
                "si104_spi_transfer", {"tx_hex": "0000", "rx_size": 2}
            )
            payload = parse(res)
            print("spi_transfer:", payload)
            # no dongle -> structured device error, with dongle -> rx reply
            assert "error" in payload or "rx_hex" in payload

            res = await session.call_tool(
                "si104_raw_request", {"command": 0x1F}
            )  # VCC_THRESHOLD query
            payload = parse(res)
            print("raw_request:", payload)
            assert "error" in payload or "reply_hex" in payload
            # device-level failures carry the MCP isError flag, text stays JSON
            assert res.isError == ("error" in payload)

            res = await session.call_tool("si104_read_vcc", {})
            payload = parse(res)
            print("read_vcc:", payload)
            assert "error" in payload or "measured_mv" in payload

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
