"""HTTP-transport smoke: web console + MCP-over-streamable-HTTP against the stub.

Starts server.py with SI104_MCP_TRANSPORT=http (stub mode) and checks:
- GET / serves the console page
- MCP client over streamable-http: initialize, 14 tools, real tool calls
- token gate: no/wrong token -> 401, correct token -> calls succeed,
  console page stays public (it holds no secrets)

Run: python http_test.py   (local only; CI keeps the stdio suite)
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import si104_launcher

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(BASE, "server.py")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.2)
    return False


def start(port, token=None):
    env = {
        **os.environ,
        "SI104_MCP_STUB": "1",
        "SI104_MCP_TRANSPORT": "http",
        "SI104_MCP_HOST": "127.0.0.1",
        "SI104_MCP_PORT": str(port),
    }
    if token:
        env["SI104_MCP_TOKEN"] = token
    return subprocess.Popen([sys.executable, SERVER], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---- open server (no token) -------------------------------------------------
p1 = start(8801)
try:
    check("port open", wait_port(8801))
    status, page = http_get("http://127.0.0.1:8801/")
    check("console page served", status == 200 and "<html" in page and "SI104 工作台" in page
          and 'id="channels"' in page and 'id="slave"' in page and 'id="spi"' in page)
    status, body = http_get("http://127.0.0.1:8801/health")
    check("health identifies the running service without USB access",
          status == 200 and json.loads(body) == {
              "service": "si104-mcp", "version": "1.1.0", "status": "ok", "ready": True,
          }, body[:120])
    state, _ = si104_launcher._probe("127.0.0.1", 8801)
    check("launcher recognizes an existing SI104 service", state == "ready", state)
    with patch.object(si104_launcher, "_start_server", side_effect=AssertionError("unneeded start")):
        reused = si104_launcher._ensure_service(
            "127.0.0.1", 8801, Path(tempfile.gettempdir()) / "si104-launcher-test.log"
        )
    check("launcher reuses service without another process", reused.get("ready") is True)

    async def open_flow():
        async with streamablehttp_client("http://127.0.0.1:8801/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                check("14 tools over http", len(tools.tools) == 14, str(len(tools.tools)))
                slave = next(t for t in tools.tools if t.name == "si104_i2c_slave")
                check("slave configure appears in MCP action schema",
                      "configure" in slave.inputSchema["properties"]["action"].get("enum", []))
                res = await s.call_tool("si104_enumerate", {})
                check("enumerate over http",
                      res.isError is False and (res.structuredContent or {}).get("devices", [{}])[0].get("serial")
                      == "STUB-SI104")
                res = await s.call_tool("si104_i2c_slave", {"channel": 2, "action": "configure",
                                                           "slave_addr": 0x45, "slave_mode": 2})
                check("configure-only over MCP returns initialized status",
                      res.isError is False and (res.structuredContent or {}).get("status", {}).get("initialized") is True)
                res = await s.call_tool("si104_read_vcc", {"channel": 0})
                check("read_vcc over http (stub rail off)",
                      res.isError is False
                      and (res.structuredContent or {}).get("output_enabled") is False
                      and (res.structuredContent or {}).get("raw_hex") == "0000b80b000000000000")
                res = await s.call_tool("si104_i2c_scan", {"channel": 0, "first": 8, "last": 8})
                error_text = res.content[0].text
                error = json.loads(error_text[error_text.index("{"):])
                check("scan failure keeps position and NO_POWER recovery hint over MCP",
                      res.isError is True and error.get("aborted_at") == "0x08"
                      and error.get("firmware_status") == 11 and "si104_vcc_control" in error.get("hint", ""))

    asyncio.run(open_flow())
finally:
    p1.terminate()

with socket.socket() as occupied:
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(2)
    port = occupied.getsockname()[1]
    state, _ = si104_launcher._probe("127.0.0.1", port)
    check("launcher detects an unrelated process on the port", state == "occupied", state)

# ---- token-gated server -----------------------------------------------------
p2 = start(8802, token="sekrit")
try:
    check("token port open", wait_port(8802))
    code, _ = http_get("http://127.0.0.1:8802/mcp")
    check("/mcp without token -> 401", code == 401, str(code))
    code, _ = http_get("http://127.0.0.1:8802/mcp", headers={"Authorization": "Bearer wrong"})
    check("wrong token -> 401", code == 401, str(code))
    code, page = http_get("http://127.0.0.1:8802/")
    check("console page stays public", code == 200)
    code, body = http_get("http://127.0.0.1:8802/health")
    check("health stays public and exposes no token", code == 200 and "sekrit" not in body)

    async def token_flow():
        async with streamablehttp_client(
            "http://127.0.0.1:8802/mcp", headers={"Authorization": "Bearer sekrit"}
        ) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("si104_enumerate", {})
                check("tool call with correct token",
                      res.isError is False and (res.structuredContent or {}).get("count") == 1)

    asyncio.run(token_flow())
finally:
    p2.terminate()

fails = [n for n, ok in RESULTS if not ok]
print(f"\n== summary == {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS"
      + (f"; FAIL: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
