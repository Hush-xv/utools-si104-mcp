"""Start or reuse the local SI104 MCP HTTP server, then open its console."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


LOG = logging.getLogger("si104-launcher")
SERVICE = "si104-mcp"
START_TIMEOUT = 15


@contextlib.contextmanager
def _launch_lock(path: Path):
    """Hold the startup decision across repeated double-clicks."""
    with path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _probe(host: str, port: int) -> tuple[str, dict | None]:
    """Return (ready|occupied|absent, health). Never talks to the USB device."""
    host_url = f"[{host}]" if host == "::1" else host
    url = f"http://{host_url}:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=0.8) as response:
            health = json.load(response)
        if (isinstance(health, dict) and health.get("service") == SERVICE
                and health.get("status") == "ok" and health.get("ready") is True
                and isinstance(health.get("version"), str)):
            LOG.debug("health check matched service=%s version=%s host=%s port=%d", SERVICE, health.get("version"), host, port)
            return "ready", health
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        LOG.debug("health check unavailable host=%s port=%d error=%s", host, port, exc)
    try:
        with socket.create_connection((host, port), timeout=0.4):
            LOG.debug("health check found unrelated listener host=%s port=%d", host, port)
            return "occupied", None
    except OSError:
        LOG.debug("health check found no listener host=%s port=%d", host, port)
        return "absent", None


def _server_env(host: str, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(SI104_MCP_TRANSPORT="http", SI104_MCP_HOST=host, SI104_MCP_PORT=str(port))
    if sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64"):
        runtime = Path(__file__).resolve().parent / "runtime" / "macos-arm64"
        for key, filename in (("SI104_SDK_LIBRARY", "libsi104_sdk.dylib"),
                              ("SI104_LIBUSB", "libusb-1.0.dylib")):
            candidate = runtime / filename
            if key not in env and candidate.is_file():
                env[key] = str(candidate)
                LOG.debug("bundled runtime selected key=%s path=%s", key, candidate)
    return env


def _start_server(host: str, port: int, log_path: Path) -> subprocess.Popen:
    server = Path(__file__).resolve().with_name("server.py")
    if not server.is_file():
        raise RuntimeError(f"找不到 MCP 服务入口：{server}")
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    LOG.info("启动 MCP 服务 host=%s port=%d python=%s", host, port, sys.executable)
    with log_path.open("ab", buffering=0) as output:
        return subprocess.Popen(
            [sys.executable, str(server)], cwd=str(server.parent), env=_server_env(host, port),
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **options,
        )


def _stop_started(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    LOG.warning("停止未就绪的 MCP 服务 pid=%d", process.pid)
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            return
        process.wait(timeout=2)


def _ensure_service(host: str, port: int, log_path: Path) -> dict:
    LOG.debug("ensuring service host=%s port=%d", host, port)
    state, health = _probe(host, port)
    if state == "ready":
        LOG.info("复用已有 SI104 服务 version=%s", health.get("version"))
        return health
    if state == "occupied":
        raise RuntimeError(f"{host}:{port} 已由其他程序占用；请更换 SI104_MCP_PORT 或关闭占用程序")
    process = _start_server(host, port, log_path)
    deadline = time.monotonic() + START_TIMEOUT
    occupied_since = None
    ready = False
    try:
        while time.monotonic() < deadline:
            state, health = _probe(host, port)
            if state == "ready":
                ready = True
                LOG.info("SI104 服务已就绪 pid=%d version=%s", process.pid, health.get("version"))
                return health
            if state == "occupied":
                occupied_since = occupied_since or time.monotonic()
                if time.monotonic() - occupied_since >= 1 or process.poll() is not None:
                    raise RuntimeError(f"{host}:{port} 被其他程序占用；日志：{log_path}")
            else:
                occupied_since = None
            if process.poll() is not None:
                raise RuntimeError(f"MCP 服务启动失败（退出码 {process.returncode}）；日志：{log_path}")
            time.sleep(0.2)
        raise RuntimeError(f"等待 MCP 服务就绪超过 {START_TIMEOUT} 秒；日志：{log_path}")
    finally:
        if not ready:
            _stop_started(process)


def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("SI104_MCP_DEBUG") == "1" else logging.INFO,
        format="%(levelname)s %(name)s %(message)s", stream=sys.stderr,
    )  # TODO: remove debug logging before release
    host = os.environ.get("SI104_MCP_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        LOG.error("启动器只打开本机控制台；SI104_MCP_HOST 必须是 127.0.0.1、localhost 或 ::1")
        return 1
    try:
        port = int(os.environ.get("SI104_MCP_PORT", "8800"))
        if not 1 <= port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        log_dir = Path(tempfile.gettempdir())
        log_path = log_dir / f"si104-mcp-console-{port}.log"
        with _launch_lock(log_dir / f"si104-mcp-console-{port}.lock"):
            _ensure_service(host, port, log_path)
        host_url = f"[{host}]" if host == "::1" else host
        url = f"http://{host_url}:{port}/"
        LOG.info("打开控制台 %s", url)
        if not webbrowser.open(url):
            LOG.warning("未能自动打开浏览器，请手动访问 %s", url)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        LOG.error("SI104 控制台启动失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
