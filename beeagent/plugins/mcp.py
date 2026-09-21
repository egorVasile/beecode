"""Minimal MCP (Model Context Protocol) stdio client — no external deps.

Speaks newline-delimited JSON-RPC 2.0 over a subprocess: initialize handshake,
tools/list, tools/call. Each configured server becomes a set of agent tools
named mcp_<server>_<tool>.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path

MCP_CONFIG_PATH = Path(".beeagent") / "mcp.json"
MCP_CACHE_PATH = Path(".beeagent") / "mcp-cache.json"

PROTOCOL_VERSION = "2024-11-05"
INIT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0


def _resolve_command(command: str) -> str:
    """Absolute path for the configured command, npx.cmd-style included."""
    if Path(command).is_absolute():
        return command
    found = shutil.which(command)
    if found:
        return found
    for variant in (f"{command}.cmd", f"{command}.exe", f"{command}.bat"):
        if shutil.which(variant):
            return shutil.which(variant)
    return command


def spawn_argv(command: str, args: list[str]) -> list[str]:
    """Full argv for create_subprocess_exec.

    Node's npx/npm are .cmd batch files, and CreateProcess cannot run those on
    its own — they have to go through cmd.exe.
    """
    resolved = _resolve_command(command)
    if Path(resolved).suffix.lower() in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", resolved, *args]
    return [resolved, *args]


class McpStdioClient:
    """One running MCP server process."""

    def __init__(self, name: str, command: str, args: list[str], env: dict | None = None):
        self.name = name
        self.command = command
        self.args = list(args)
        self.env = env or {}
        self._proc = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *spawn_argv(self.command, self.args),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "beeagent", "version": "0.1.0"},
        }, timeout=INIT_TIMEOUT)
        await self._notify("notifications/initialized")

    async def _send(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def _request(self, method: str, params: dict | None = None,
                       timeout: float = CALL_TIMEOUT) -> dict:
        assert self._proc is not None and self._proc.stdout is not None
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params
            await self._send(msg)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"MCP {self.name}: timeout waiting for {method}")
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=remaining)
                if not line:
                    raise RuntimeError(f"MCP {self.name}: server closed stdout")
                text = line.decode("utf-8", errors="replace").strip()
                if not text or not text.startswith("{"):
                    continue  # server banner/noise
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if data.get("id") == req_id:
                    if "error" in data:
                        err = data["error"]
                        raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
                    return data.get("result", {})

    async def list_tools(self) -> list[dict]:
        result = await self._request("tools/list", {}, timeout=30.0)
        return result.get("tools", [])

    async def call_tool(self, tool: str, arguments: dict) -> str:
        result = await self._request(
            "tools/call", {"name": tool, "arguments": arguments}, timeout=CALL_TIMEOUT)
        parts = []
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "\n".join(parts).strip()
        if result.get("isError"):
            return f"(MCP error) {text or 'tool call failed'}"
        return text or "(empty MCP result)"

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                from beeagent.tools.shell import kill_process_tree

                # A `.cmd` shim (npx on Windows) is a wrapper: terminating it
                # leaves the real server running with its pipes open.
                kill_process_tree(self._proc)
        self._proc = None


class _LoopThread:
    """One background event loop shared by every MCP client.

    Servers are long-lived subprocesses, so they must stay attached to a single
    loop; asyncio.run() per call would strand them on a closed loop.
    """

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, coro_factory, timeout: float | None = None):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._start_locked()
            loop = self._loop
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return future.result(timeout)

    def _start_locked(self) -> None:
        # Build the loop here so callers can submit before run_forever starts.
        loop = asyncio.new_event_loop()
        self._loop = loop

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self._thread = threading.Thread(target=runner, daemon=True,
                                        name="beeagent-mcp")
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)


_shared_loop = _LoopThread()


def run_coro_blocking(coro_factory, timeout: float | None = None):
    """Run a coroutine from sync code on the shared MCP loop (never deadlocks
    the caller, even when that caller is itself inside an event loop)."""
    return _shared_loop.submit(coro_factory, timeout)


class McpManager:
    """Configured servers + cached clients + cached tool schemas."""

    def __init__(self, config_path: Path = MCP_CONFIG_PATH,
                 cache_path: Path = MCP_CACHE_PATH):
        self.config_path = config_path
        self.cache_path = cache_path
        self._clients: dict[str, McpStdioClient] = {}
        self._clients_lock = threading.Lock()

    def _config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"servers": {}}

    def servers(self) -> dict[str, dict]:
        return self._config().get("servers", {})

    # --- schema cache ---------------------------------------------------------

    def _cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self, cache: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    def cached_tools(self, server: str) -> list[dict] | None:
        return self._cache().get(server, {}).get("tools")

    def store_tools(self, server: str, tools: list[dict]) -> None:
        cache = self._cache()
        cache[server] = {"tools": tools}
        self._save_cache(cache)

    # --- live calls -------------------------------------------------------------

    def get_client(self, server: str) -> McpStdioClient:
        with self._clients_lock:
            client = self._clients.get(server)
        if client is not None:
            return client
        cfg = self.servers().get(server)
        if cfg is None:
            raise ValueError(f"MCP server '{server}' is not configured")
        client = McpStdioClient(server, cfg.get("command", ""), cfg.get("args", []), cfg.get("env"))
        with self._clients_lock:
            self._clients[server] = client
        return client

    def discover_tools_blocking(self, server: str, timeout: float = 60.0) -> list[dict]:
        """Start the server and list its tools; result cached on disk."""
        client = self.get_client(server)

        async def go():
            await client.start()
            return await client.list_tools()

        try:
            tools = run_coro_blocking(lambda: _with_timeout(go(), timeout))
        except Exception as e:
            # The server was started for this call. Leaving it behind means every
            # failed `/mcp connect` costs a live subprocess.
            run_coro_blocking(lambda: client.stop(), timeout=10)
            with self._clients_lock:
                self._clients.pop(server, None)
            raise RuntimeError(f"server '{server}' unreachable: {e}") from e
        self.store_tools(server, tools)
        return tools

    def call_blocking(self, server: str, tool: str, arguments: dict) -> str:
        client = self.get_client(server)

        async def go():
            await client.start()
            return await client.call_tool(tool, arguments)

        try:
            return run_coro_blocking(go, timeout=CALL_TIMEOUT + 30)
        except Exception as e:
            return f"(MCP call failed: {e})"

    def shutdown(self) -> None:
        with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                run_coro_blocking(lambda: client.stop(), timeout=5)
            except Exception:
                pass


async def _with_timeout(coro, timeout: float):
    return await asyncio.wait_for(coro, timeout=timeout)
