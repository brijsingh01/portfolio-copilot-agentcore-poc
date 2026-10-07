"""Minimal MCP Streamable-HTTP client for AgentCore Gateway (stdlib only).

Handles the session handshake, JSON or SSE responses, tool pagination, W3C trace headers, and
unwraps tool results (structuredContent, or JSON text content from Lambda/OpenAPI targets).
"""
from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any

from src.common.tracer import client_span

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.data = code, data


class GatewayClient:
    def __init__(self, url: str, bearer_token: str, *, timeout: float = 60.0) -> None:
        self.url = url
        self.timeout = timeout
        self._token = bearer_token
        self._session: str | None = None
        self._ids = itertools.count(1)
        self.tools: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ transport
    def _post(self, payload: dict[str, Any], span_name: str, attrs: dict[str, Any]) -> dict[str, Any] | None:
        with client_span(span_name, {"rpc.system": "jsonrpc", "rpc.method": payload["method"],
                                     "server.address": self.url, **attrs}) as (span, trace_headers):
            if "id" in payload:  # mirror trace context into MCP `_meta` for hops that drop headers
                payload.setdefault("params", {}).setdefault("_meta", {}).update(
                    {k: v for k, v in trace_headers.items() if k in ("traceparent", "tracestate")})
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "Authorization": f"Bearer {self._token}", "MCP-Protocol-Version": PROTOCOL_VERSION,
                       **trace_headers}
            if self._session:
                headers["Mcp-Session-Id"] = self._session
            req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._session = resp.headers.get("Mcp-Session-Id") or self._session
                    body = resp.read().decode()
                    ctype = resp.headers.get("Content-Type", "")
                    span.set_attribute("http.response.status_code", resp.status)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                span.set_attribute("http.response.status_code", exc.code)
                raise McpError(f"HTTP {exc.code} from gateway: {detail}", exc.code) from exc
            if "id" not in payload or not body.strip():
                return None
            message = self._parse(body, ctype, payload["id"])
            if "error" in message:
                err = message["error"]
                raise McpError(err.get("message", "MCP error"), err.get("code"), err.get("data"))
            return message["result"]

    @staticmethod
    def _parse(body: str, content_type: str, req_id: Any) -> dict[str, Any]:
        if "text/event-stream" not in content_type:
            return json.loads(body)
        for line in body.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if msg.get("id") == req_id:
                    return msg
        raise McpError("no JSON-RPC response in event stream")

    def _request(self, method: str, params: dict[str, Any] | None = None, **attrs: Any) -> dict[str, Any]:
        result = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}},
                            f"mcp.client {method}", attrs)
        return result or {}

    # ------------------------------------------------------------------ protocol
    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                              "clientInfo": {"name": "portfolio-copilot", "version": "1.0.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, "mcp.client notifications/initialized", {})
        return result

    def list_tools(self) -> dict[str, dict[str, Any]]:
        cursor: str | None = None
        while True:
            page = self._request("tools/list", {"cursor": cursor} if cursor else {})
            self.tools.update({t["name"]: t for t in page.get("tools", [])})
            cursor = page.get("nextCursor")
            if not cursor:
                return self.tools

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool and return its decoded payload; tool-level errors come back as
        {"status": "error", ...} so callers can reason about them instead of crashing."""
        result = self._request("tools/call", {"name": name, "arguments": arguments}, **{"gen_ai.tool.name": name})
        if result.get("structuredContent") is not None:
            payload = result["structuredContent"]
        else:
            text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"text": text}
        if result.get("isError") and isinstance(payload, dict):
            payload.setdefault("status", "error")
        return payload if isinstance(payload, dict) else {"result": payload}
