# MCP client and Tool registry
# Supports discovery of MCP servers (HTTP/SSE) and stdio tools (local subprocesses).
# Provides streaming interfaces for tool output.

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Generator, List, Optional, Tuple

import requests


@dataclass
class Tool:
    name: str
    label: str = ""
    description: str = ""
    transport: str = "http"  # http | sse | stdio
    endpoint: str = ""  # url or command
    metadata: Dict = field(default_factory=dict)
    enabled: bool = True
    allowed_roles: List[str] = field(default_factory=lambda: ["admin"])  # roles allowed to run


class MCPClient:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def discover_tools(self) -> List[Tool]:
        # Try common MCP discovery endpoints. Expect JSON list of tools or an object {tools: [...]}
        candidates = [
            f"{self.base}/.well-known/mcp",
            f"{self.base}/.well-known/mcp/tools",
            f"{self.base}/mcp/tools",
            f"{self.base}/tools",
        ]
        tools: List[Tool] = []
        for url in candidates:
            try:
                r = requests.get(url, timeout=5)
                if not r.ok:
                    continue
                data = r.json()
                # normalize
                items = []
                if isinstance(data, dict):
                    # maybe {"tools": [...]} 
                    if "tools" in data and isinstance(data["tools"], list):
                        items = data["tools"]
                    elif "items" in data and isinstance(data["items"], list):
                        items = data["items"]
                    else:
                        # If data itself looks like a tool
                        if all(k in data for k in ("name", "transport", "endpoint")):
                            items = [data]
                elif isinstance(data, list):
                    items = data

                for it in items:
                    try:
                        name = it.get("name") or it.get("id") or it.get("tool")
                        transport = (it.get("transport") or "http").lower()
                        endpoint = it.get("endpoint") or it.get("url") or it.get("command") or ""
                        label = it.get("label") or it.get("title") or name
                        desc = it.get("description") or ""
                        allowed = it.get("allowed_roles") or it.get("roles") or it.get("permissions") or ["admin"]
                        t = Tool(name=str(name), label=label, description=desc, transport=transport, endpoint=str(endpoint), metadata=it, enabled=True, allowed_roles=list(allowed))
                        tools.append(t)
                    except Exception:
                        continue
                if tools:
                    return tools
            except Exception:
                continue
        return tools

    def run_http(self, tool: Tool, payload: Dict) -> Generator[str, None, None]:
        # POST to endpoint and stream response if possible
        url = tool.endpoint
        try:
            with requests.post(url, json=payload, stream=True, timeout=30) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    yield line + "\n"
        except Exception as exc:
            yield f"[Error] {exc}\n"

    def run_sse(self, tool: Tool, payload: Dict) -> Generator[str, None, None]:
        # SSE: many MCP tool servers provide event-stream at an endpoint
        url = tool.endpoint
        try:
            with requests.post(url, json=payload, stream=True, timeout=30) as r:
                r.raise_for_status()
                for raw in r.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    data = raw
                    # typical SSE line: data: {...}
                    if data.startswith("data:"):
                        data = data[5:].strip()
                    yield data + "\n"
        except Exception as exc:
            yield f"[Error] {exc}\n"

    def run_stdio(self, tool: Tool, payload: Dict) -> Generator[str, None, None]:
        # endpoint treated as command; send JSON input via stdin and stream stdout
        cmd = tool.endpoint
        if not cmd:
            yield "[Error] stdio tool missing command" + "\n"
            return
        args = shlex.split(cmd)
        try:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            # send payload as JSON
            try:
                inp = json.dumps(payload)
                proc.stdin.write(inp)
                proc.stdin.close()
            except Exception:
                pass

            for line in proc.stdout:
                yield line
            proc.wait()
        except Exception as exc:
            yield f"[Error] {exc}\n"

    def run_tool(self, tool: Tool, payload: Dict) -> Generator[str, None, None]:
        transport = tool.transport or "http"
        if transport == "http":
            yield from self.run_http(tool, payload)
        elif transport == "sse":
            yield from self.run_sse(tool, payload)
        elif transport == "stdio":
            yield from self.run_stdio(tool, payload)
        else:
            yield f"[Error] Unknown transport: {transport}\n"


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._clients: Dict[str, MCPClient] = {}

    def register_tool(self, tool: Tool, source: str = "local"):
        self._tools[tool.name] = tool

    def unregister_tool(self, name: str):
        if name in self._tools:
            del self._tools[name]

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def set_enabled(self, name: str, enabled: bool):
        t = self._tools.get(name)
        if t:
            t.enabled = bool(enabled)
            # persist if DB available
            try:
                from db.repos import SettingsRepo
                sr = SettingsRepo()
                sr.set(f"tool:{name}:enabled", "1" if enabled else "0")
            except Exception:
                pass
            return True
        return False

    def load_persisted_states(self):
        try:
            from db.repos import SettingsRepo
            sr = SettingsRepo()
            for name, t in list(self._tools.items()):
                val = sr.get(f"tool:{name}:enabled")
                if val is not None:
                    t.enabled = val == "1"
        except Exception:
            # DB not ready or settings not available
            return

    def auto_discover(self, servers: List[str]):
        for s in servers:
            s = s.strip()
            if not s:
                continue
            if s.startswith("stdio://"):
                # format: stdio:///path/to/executable?name=toolname
                # minimal parsing
                cmd = s[len("stdio://"):]
                # allow name query
                name = os.path.basename(cmd.split("?")[0])
                tool = Tool(name=name, label=name, transport="stdio", endpoint=cmd, description=f"stdio tool {cmd}")
                self.register_tool(tool, source=s)
                continue
            client = MCPClient(s)
            try:
                tools = client.discover_tools()
                for t in tools:
                    # If endpoint is relative, join with base
                    if t.endpoint and t.endpoint.startswith("/"):
                        t.endpoint = s.rstrip("/") + t.endpoint
                    self.register_tool(t, source=s)
                # cache client for running
                self._clients[s] = client
            except Exception:
                continue

    def run(self, name: str, payload: Dict) -> Generator[str, None, None]:
        tool = self.get(name)
        if not tool:
            yield f"[Error] Tool not found: {name}\n"
            return
        # pick client if endpoint matches a known MCP server
        for base, client in self._clients.items():
            if tool.endpoint.startswith(base):
                yield from client.run_tool(tool, payload)
                return
        # fallback: create a client on the fly if endpoint is http/sse
        if tool.transport in ("http", "sse") and tool.endpoint.startswith("http"):
            client = MCPClient(tool.endpoint)
            yield from client.run_tool(tool, payload)
            return
        # stdio
        if tool.transport == "stdio":
            client = MCPClient("stdio://")
            yield from client.run_tool(tool, payload)
            return
        yield f"[Error] No client available for tool {name}\n"


# Singleton
REGISTRY = ToolRegistry()

# Auto-discover list from env var MCP_SERVERS (comma separated)
_mcp_env = os.environ.get("MCP_SERVERS", "").strip()
if _mcp_env:
    servers = [s.strip() for s in _mcp_env.split(",") if s.strip()]
    REGISTRY.auto_discover(servers)
