"""Minimal MCP client for the SigNoz MCP server.

Two reasons this exists rather than just registering the server with an AI
client and asking nicely:

* **Provisioning should be scriptable.** Alerts created by a chat session are
  not reproducible; alerts created by a script in version control are.
* **It proves the integration.** Registering an MCP server demonstrates
  configuration. Calling its tools and getting real telemetry back
  demonstrates that it works.

Speaks the streamable-HTTP transport: JSON-RPC over POST, responses arriving
either as JSON or as an SSE `data:` frame, with a session id handed back on
initialize that must be echoed on every subsequent call.

    python scripts/mcp_client.py list
    python scripts/mcp_client.py call signoz_list_metrics '{"limit": 5}'
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.getenv("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str = DEFAULT_URL, api_key: str | None = None) -> None:
        self.url = url
        self.api_key = api_key or os.getenv("SIGNOZ_API_KEY") or ""
        self.session_id: str | None = None
        self._id = 0

    # -- transport -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # The server may reply with either, and refuses the request if we
            # do not advertise both.
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers["SIGNOZ-API-KEY"] = self.api_key
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params

        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers=self._headers(), method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                # initialize hands back the session id in a header.
                sid = response.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise MCPError(f"HTTP {exc.code}: {exc.read().decode()[:400]}") from exc

        message = _parse(body)
        if "error" in message:
            raise MCPError(json.dumps(message["error"])[:500])
        return message.get("result") or {}

    # -- protocol ------------------------------------------------------

    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cadence", "version": "0.1.0"},
            },
        )
        # The spec requires this notification before any tool call; some
        # servers reject tools/call without it.
        try:
            self._notify("notifications/initialized")
        except MCPError:
            pass
        return result

    def _notify(self, method: str) -> None:
        req = urllib.request.Request(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "method": method}).encode(),
            headers=self._headers(), method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError:
            pass

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list").get("tools") or []

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}})


def _parse(body: str) -> dict:
    """Accept a plain JSON body or an SSE stream and return the JSON-RPC message."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            return json.loads(line)
    raise MCPError(f"unparseable response: {body[:300]}")


def text_of(result: dict) -> str:
    """Flatten a tool result's content blocks into plain text."""
    chunks = []
    for block in result.get("content") or []:
        if block.get("type") == "text":
            chunks.append(block.get("text", ""))
    return "\n".join(chunks)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    client = MCPClient()
    client.initialize()
    command = sys.argv[1]

    if command == "list":
        for tool in client.list_tools():
            print(f"  {tool['name']}")
        return 0

    if command == "call":
        name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        result = client.call(name, args)
        text = text_of(result)
        print(text if text else json.dumps(result, indent=1)[:4000])
        return 1 if result.get("isError") else 0

    if command == "schema":
        name = sys.argv[2]
        for tool in client.list_tools():
            if tool["name"] == name:
                print(json.dumps(tool.get("inputSchema"), indent=1)[:6000])
                return 0
        print(f"no such tool: {name}", file=sys.stderr)
        return 1

    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
