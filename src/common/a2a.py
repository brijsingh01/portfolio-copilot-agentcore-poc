"""Minimal A2A (v0.3, JSON-RPC 2.0 transport) server factory for AgentCore Runtime.

Routes: POST / (message/send, tasks/get, tasks/cancel), GET /.well-known/agent-card.json, GET /ping.
Tasks complete synchronously; finished tasks are kept in a bounded per-container LRU for tasks/get.
"""
from __future__ import annotations

import logging
import os
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.common.identity import AuthenticationError, AuthorizationError, CallerContext, JwtValidator
from src.common.server import (INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, SESSION_HEADER, JsonRpcError,
                               error_response, ping_route, read_jsonrpc, rpc_result)
from src.common.tracer import current_traceparent, extract_jsonrpc, get_tracer, server_span

A2A_PROTOCOL_VERSION = "0.3.0"
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
UNSUPPORTED_OPERATION = -32004

log = logging.getLogger("copilot.a2a")


class InvalidInput(ValueError):
    """Raised by skill handlers for malformed task input (-> JSON-RPC -32602)."""


@dataclass(frozen=True)
class SkillRequest:
    text: str
    data: dict[str, Any]
    caller: CallerContext
    context_id: str
    task_id: str


@dataclass(frozen=True)
class SkillResult:
    summary: str
    data: dict[str, Any]
    artifact_name: str


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    version: str
    skills: list[dict[str, Any]]
    handler: Callable[[SkillRequest], SkillResult]
    tags: dict[str, str] = field(default_factory=dict)

    def card(self) -> dict[str, Any]:
        url = os.getenv("A2A_PUBLIC_URL") or os.getenv("AGENTCORE_RUNTIME_URL") or "http://localhost:9000/"
        return {
            "protocolVersion": A2A_PROTOCOL_VERSION,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "url": url,
            "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
            "defaultInputModes": ["application/json", "text/plain"],
            "defaultOutputModes": ["application/json", "text/plain"],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}},
            "security": [{"bearer": []}],
            "skills": self.skills,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_message(params: Mapping[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    message = params.get("message")
    if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("parts"), list):
        raise JsonRpcError(INVALID_PARAMS, "Invalid params", "params.message must be a user message with parts")
    texts: list[str] = []
    data: dict[str, Any] = {}
    for part in message["parts"]:
        kind = part.get("kind") if isinstance(part, dict) else None
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "data" and isinstance(part.get("data"), dict):
            data.update(part["data"])
    if not texts and not data:
        raise JsonRpcError(INVALID_PARAMS, "Invalid params", "message contains no text or data parts")
    return message, "\n".join(texts), data


def create_a2a_app(spec: AgentSpec, validator: JwtValidator | None = None, *, max_tasks: int = 256) -> Starlette:
    validator = validator or JwtValidator.from_settings()
    tracer = get_tracer(spec.name)
    tasks: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()  # task_id -> (subject, task)

    def remember(subject: str, task: dict[str, Any]) -> None:
        tasks[task["id"]] = (subject, task)
        while len(tasks) > max_tasks:
            tasks.popitem(last=False)

    async def send(params: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        message, text, data = _parse_message(params)
        caller = validator.validate_headers(headers)
        trace.get_current_span().set_attributes(caller.span_attributes())
        context_id = message.get("contextId") or headers.get(SESSION_HEADER.lower()) or str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        req = SkillRequest(text=text, data=data, caller=caller, context_id=context_id, task_id=task_id)

        result = await run_in_threadpool(spec.handler, req)
        agent_message = {"kind": "message", "role": "agent", "messageId": str(uuid.uuid4()), "taskId": task_id,
                         "contextId": context_id, "parts": [{"kind": "text", "text": result.summary}]}
        task = {
            "kind": "task", "id": task_id, "contextId": context_id,
            "status": {"state": "completed", "timestamp": _now(), "message": agent_message},
            "artifacts": [{"artifactId": str(uuid.uuid4()), "name": result.artifact_name,
                           "parts": [{"kind": "data", "data": result.data}, {"kind": "text", "text": result.summary}]}],
            "history": [{**message, "taskId": task_id, "contextId": context_id}],
            "metadata": {"traceparent": current_traceparent(), "agent": spec.name},
        }
        remember(caller.subject, task)
        return task

    def get_task(params: Mapping[str, Any], headers: Mapping[str, str], cancel: bool = False) -> dict[str, Any]:
        caller = validator.validate_headers(headers)
        owner, task = tasks.get(str(params.get("id")), (None, None))
        if task is None or owner != caller.subject:
            raise JsonRpcError(TASK_NOT_FOUND, "Task not found")
        if cancel:
            raise JsonRpcError(TASK_NOT_CANCELABLE, "Task cannot be canceled", "task already completed")
        return task

    async def rpc(request: Request) -> JSONResponse:
        req_id = None
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            body = await read_jsonrpc(request)
            req_id = body.get("id")
            method, params = body.get("method", ""), body.get("params") or {}
            attrs = {"rpc.system": "jsonrpc", "rpc.method": method, "a2a.agent": spec.name, **spec.tags}
            with server_span(f"a2a {method}", extract_jsonrpc(body, headers), attrs, tracer=tracer) as span:
                try:
                    if method == "message/send":
                        result = await send(params, headers)
                    elif method in ("tasks/get", "tasks/cancel"):
                        result = get_task(params, headers, cancel=method == "tasks/cancel")
                    elif method in ("message/stream", "tasks/resubscribe") or method.startswith("tasks/pushNotificationConfig"):
                        raise JsonRpcError(UNSUPPORTED_OPERATION, "Unsupported operation", f"{method} is not supported")
                    else:
                        raise JsonRpcError(METHOD_NOT_FOUND, "Method not found", method)
                except (AuthenticationError, AuthorizationError) as exc:
                    raise JsonRpcError.from_identity(exc) from exc
                except InvalidInput as exc:
                    raise JsonRpcError(INVALID_PARAMS, "Invalid params", str(exc)) from exc
                except JsonRpcError:
                    raise
                except Exception as exc:
                    log.exception("a2a task failed")
                    raise JsonRpcError(INTERNAL_ERROR, "Internal error", type(exc).__name__) from exc
                span.set_attribute("a2a.status", "completed")
            return JSONResponse(rpc_result(req_id, result))
        except JsonRpcError as err:
            return error_response(req_id, err)

    async def agent_card(_: Request) -> JSONResponse:
        return JSONResponse(spec.card())

    return Starlette(routes=[
        Route("/", rpc, methods=["POST"]),
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/.well-known/agent.json", agent_card, methods=["GET"]),  # pre-0.3 clients
        ping_route,
    ])
