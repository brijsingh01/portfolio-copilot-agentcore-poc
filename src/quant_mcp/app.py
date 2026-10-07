"""MCP Server - Quant Calculator (AgentCore Runtime, 0.0.0.0:8000).

Stateless Streamable HTTP transport (MCP 2025-06-18): `POST /mcp` answers each JSON-RPC request with
`application/json`. `initialize` issues an `Mcp-Session-Id`; later requests echo it back but no
server-side session state exists, so any runtime replica can serve any request. GET (SSE) is not offered.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from opentelemetry import trace
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src.common.server import (INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, JsonRpcError, error_response,
                               ping_route, read_jsonrpc, rpc_result, serve)
from src.common.tracer import configure_tracing, extract_jsonrpc, get_tracer, server_span
from src.quant_mcp.tools import TOOLS, ToolInputError

SERVICE = "quant-mcp"
SERVER_INFO = {"name": "quant-calculator", "title": "Quant Rebalance Calculator", "version": "1.0.0"}
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"

log = logging.getLogger(SERVICE)


def create_app() -> Starlette:
    """Identity-agnostic calculators: the runtime accepts only SigV4 calls from the Gateway role."""
    configure_tracing(SERVICE)
    tracer = get_tracer(SERVICE)

    async def call_tool(params: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        name, arguments = params.get("name"), params.get("arguments") or {}
        tool = TOOLS.get(name) if isinstance(name, str) else None
        if tool is None:
            raise JsonRpcError(INVALID_PARAMS, "Invalid params", f"unknown tool {name!r}")
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "Invalid params", "arguments must be an object")
        span = trace.get_current_span()
        span.set_attribute("mcp.tool.name", tool.name)
        args = {k: v for k, v in arguments.items() if not k.startswith("_")}  # drop _meta (trace context)
        try:
            result = await run_in_threadpool(tool.fn, args)
        except ToolInputError as exc:
            span.set_attribute("mcp.tool.is_error", True)
            return {"content": [{"type": "text", "text": f"Input error: {exc}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result, "isError": False}

    async def mcp_post(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        session_id = headers.get(SESSION_HEADER.lower())
        req_id = None
        try:
            version = headers.get(VERSION_HEADER.lower())
            if version and version not in SUPPORTED_VERSIONS:
                raise JsonRpcError(INVALID_REQUEST, "Invalid Request", f"unsupported {VERSION_HEADER} {version}",
                                   http_status=400)
            body = await read_jsonrpc(request)
            req_id, method = body.get("id"), body.get("method")
            if method is None or req_id is None:  # notification or client response
                return Response(status_code=202, headers={SESSION_HEADER: session_id} if session_id else None)
            if method == "ping":  # health check (Gateway target pings): no span, no log, no new trace
                return JSONResponse(rpc_result(req_id, {}), headers={SESSION_HEADER: session_id} if session_id else None)
            params = body.get("params") or {}
            attrs = {"rpc.system": "jsonrpc", "rpc.method": method, "mcp.session.id": session_id or ""}
            with server_span(f"mcp {method}", extract_jsonrpc(body, headers), attrs, tracer=tracer):
                if method == "initialize":
                    requested = params.get("protocolVersion")
                    session_id = uuid.uuid4().hex
                    result = {"protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": SERVER_INFO,
                              "instructions": "Deterministic portfolio math. Pass the ClientPortfolios record as `portfolio`."}
                elif method == "tools/list":
                    result = {"tools": [t.descriptor() for t in TOOLS.values()]}
                elif method == "tools/call":
                    result = await call_tool(params, headers)
                else:
                    raise JsonRpcError(METHOD_NOT_FOUND, "Method not found", method)
            return JSONResponse(rpc_result(req_id, result), headers={SESSION_HEADER: session_id} if session_id else None)
        except JsonRpcError as err:
            return error_response(req_id, err, {SESSION_HEADER: session_id} if session_id else None)
        except Exception:
            log.exception("mcp request failed")
            return error_response(req_id, JsonRpcError(-32603, "Internal error"))

    async def mcp_other(_: Request) -> Response:
        # Stateless server: no standalone SSE stream (GET) and no sessions to terminate (DELETE).
        return Response(status_code=405, headers={"Allow": "POST"})

    return Starlette(routes=[
        Route("/mcp", mcp_post, methods=["POST"]),
        Route("/mcp", mcp_other, methods=["GET", "DELETE"]),
        ping_route,
    ])


if __name__ == "__main__":
    
    serve(create_app, port=8000)
