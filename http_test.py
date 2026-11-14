"""HTTP-transport smoke: web console + MCP-over-streamable-HTTP against the stub.

Starts server.py with SI104_MCP_TRANSPORT=http (stub mode) and checks:
- GET / serves the console page
- MCP client over streamable-http: initialize, 14 tools, real tool calls
- token gate: no/wrong token -> 401, correct token -> calls succeed,
  console page stays public (it holds no secrets)

Run: python http_test.py   (local only; CI keeps the stdio suite)
"""
import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

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

    async def open_flow():
        async with streamablehttp_client("http://127.0.0.1:8801/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                check("14 tools over http", len(tools.tools) == 14, str(len(tools.tools)))
                res = await s.call_tool("si104_enumerate", {})
                check("enumerate over http",
                      res.isError is False and (res.structuredContent or {}).get("count") == 1)
                res = await s.call_tool("si104_read_vcc", {"channel": 0})
                check("read_vcc over http (stub rail off)",
                      res.isError is False
                      and (res.structuredContent or {}).get("output_enabled") is False
                      and (res.structuredContent or {}).get("raw_hex") == "0000b80b000000000000")

    asyncio.run(open_flow())
finally:
    p1.terminate()

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
