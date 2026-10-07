"""Shared HTTP plumbing for AgentCore Runtime containers: JSON-RPC 2.0, /ping, launcher."""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.common.identity import AuthorizationError

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHENTICATED = -32040
FORBIDDEN = -32043

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

log = logging.getLogger("copilot.server")


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None, http_status: int = 200) -> None:
        super().__init__(message)
        self.code, self.message, self.data, self.http_status = code, message, data, http_status

    @classmethod
    def from_identity(cls, exc: Exception) -> JsonRpcError:
        if isinstance(exc, AuthorizationError):
            return cls(FORBIDDEN, "Forbidden", str(exc), http_status=403)
        return cls(UNAUTHENTICATED, "Unauthenticated", str(exc), http_status=401)


def rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, err: JsonRpcError) -> dict[str, Any]:
    body: dict[str, Any] = {"code": err.code, "message": err.message}
    if err.data is not None:
        body["data"] = err.data
    return {"jsonrpc": "2.0", "id": req_id, "error": body}


def error_response(req_id: Any, err: JsonRpcError, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(rpc_error(req_id, err), status_code=err.http_status, headers=dict(headers or {}))


async def read_jsonrpc(request: Request) -> dict[str, Any]:
    """Parse a single JSON-RPC 2.0 message (batches are not supported)."""
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError) as exc:
        raise JsonRpcError(PARSE_ERROR, "Parse error", str(exc), http_status=400) from exc
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request", "expected a single JSON-RPC 2.0 object", http_status=400)
    if "method" in body and not isinstance(body["method"], str):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request", "method must be a string", http_status=400)
    params = body.get("params", {})
    if params is not None and not isinstance(params, dict):
        raise JsonRpcError(INVALID_PARAMS, "Invalid params", "params must be an object")
    return body


_STARTED = int(time.time())


async def _ping(_: Request) -> JSONResponse:
    # AgentCore Runtime health contract.
    return JSONResponse({"status": "Healthy", "time_of_last_update": _STARTED})


ping_route = Route("/ping", _ping, methods=["GET"])


# Health checks must not produce log output. AgentCore writes each runtime session's stdout to its own
# `[runtime-logs]<session>` stream, and the Gateway's MCP target health pings (and AgentCore's port probes) each
# land in a fresh session - so any line printed at start-up or per probe becomes a separate, noise-only stream.
# Silent start-up + silent probes => sessions that only answered health checks write nothing and create no stream.
NOISY_LIBRARIES = ("botocore", "boto3", "urllib3", "uvicorn", "uvicorn.error", "opentelemetry")
PROBE_NOISE = ("Invalid HTTP request received",)  # uvicorn: TCP health probes that send no HTTP request


class _DropProbeNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(text in record.getMessage() for text in PROBE_NOISE)


class _TraceContext(logging.Filter):
    """Adds the active trace/span ids so stdout lines correlate with X-Ray traces (replaces the OTel logging
    instrumentation, which is disabled in the image because it prints a start-up warning)."""

    def filter(self, record: logging.LogRecord) -> bool:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else ""
        record.span_id = format(ctx.span_id, "016x") if ctx.is_valid else ""
        return True


def configure_logging() -> None:
    """Idempotent: JSON lines with trace ids at LOG_LEVEL for application code, WARNING for libraries,
    probe noise dropped."""
    root = logging.getLogger()
    if getattr(root, "_copilot_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","trace_id":"%(trace_id)s",'
        '"span_id":"%(span_id)s","msg":"%(message)s"}'))
    handler.addFilter(_TraceContext())
    handler.addFilter(_DropProbeNoise())
    root.addHandler(handler)
    # also on the emitting logger, so no other handler (e.g. one installed by OTel auto-instrumentation) prints it
    logging.getLogger("uvicorn.error").addFilter(_DropProbeNoise())
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    root._copilot_configured = True  # type: ignore[attr-defined]


def serve(app_factory: Callable[[], Any], port: int) -> None:
    """Configure logging *before* building the app (boto3 clients log credential discovery at creation)."""
    import uvicorn

    configure_logging()
    app = app_factory()
    # log_config=None: uvicorn's loggers propagate to the root handler above (same format and filters);
    # uvicorn itself only logs warnings (no start-up banner), and access logs (incl. GET /ping) are off.
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=None, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="*", access_log=False)
