"""Shared wrapper for AgentCore Gateway Lambda targets.

Gateway invokes the function with the tool arguments as `event` and tool metadata in
`context.client_context.custom` (`bedrockAgentCoreToolName` = "<target>___<tool>"). The wrapper:
  * continues the caller's trace (`_meta.traceparent` argument, client context, or X-Ray header),
  * maps input / service failures to structured tool errors and writes an audit log line.
Tools are identity-agnostic: the Gateway's JWT authorizer is the auth boundary, the calling agent enforces tenancy.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any

from opentelemetry.trace import Status, StatusCode

from src.common.tracer import configure_tracing, current_trace_id, extract_lambda, server_span

TOOL_DELIMITER = "___"
log = logging.getLogger("copilot.lambda")
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def gateway_tool_name(event: Mapping[str, Any], context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName") or event.get("_tool") or ""
    return name.split(TOOL_DELIMITER)[-1]


def _gateway_attrs(context: Any) -> dict[str, str]:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    attrs = {"faas.invocation_id": getattr(context, "aws_request_id", "")}
    for key, attr in (("bedrockAgentCoreGatewayId", "agentcore.gateway_id"), ("bedrockAgentCoreTargetId", "agentcore.target_id")):
        if custom.get(key):
            attrs[attr] = custom[key]
    return attrs


def _audit(tool: str, outcome: str, **extra: Any) -> None:
    log.info(json.dumps({"audit": True, "tool": tool, "outcome": outcome, "trace_id": current_trace_id(), **extra},
                        default=str))


ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


def gateway_target(service_name: str, tools: Mapping[str, ToolFn]) -> Callable[[dict, Any], dict]:
    """Build a Lambda handler dispatching Gateway tool calls to `tools[name](args)`."""
    tracer = configure_tracing(service_name)

    def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
        tool = gateway_tool_name(event, context)
        args = {k: v for k, v in event.items() if not k.startswith("_")}
        with server_span(f"lambda {tool or 'unknown'}", extract_lambda(event, context),
                         {"faas.name": service_name, "gen_ai.tool.name": tool, **_gateway_attrs(context)},
                         tracer=tracer) as span:
            try:
                if tool not in tools:
                    raise ToolError("UNKNOWN_TOOL", f"unsupported tool {tool!r}")
                result = tools[tool](args)
                _audit(tool, "OK", args=sorted(args))
                return {"status": "success", "tool": tool, "trace_id": current_trace_id(), **result}
            except ToolError as exc:
                span.set_attribute("tool.error_code", exc.code)
                _audit(tool, "ERROR", error_code=exc.code, reason=str(exc))
                return {"status": "error", "tool": tool, "error_code": exc.code, "message": str(exc),
                        "trace_id": current_trace_id()}
            except Exception as exc:  # upstream/service failure: structured, masked tool error instead of a crash
                log.exception("tool %s failed", tool)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                span.set_attribute("tool.error_code", "INTERNAL")
                _audit(tool, "ERROR", error_code="INTERNAL", reason=type(exc).__name__)
                return {"status": "error", "tool": tool, "error_code": "INTERNAL",
                        "message": "internal error while executing tool", "trace_id": current_trace_id()}

    @wraps(handler)
    def entrypoint(event: dict[str, Any], context: Any) -> dict[str, Any]:
        try:
            return handler(event, context)
        finally:
            _flush()

    return entrypoint


def _flush() -> None:
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=2000)
