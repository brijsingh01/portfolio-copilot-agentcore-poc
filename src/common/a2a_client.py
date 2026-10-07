"""A2A JSON-RPC 2.0 client (stdlib only) for AgentCore Runtime-hosted A2A servers."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any

from src.common.server import SESSION_HEADER
from src.common.tracer import client_span, inject_jsonrpc


class A2AError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code, self.status = code, status


class A2AClient:
    def __init__(self, url: str, *, agent_name: str = "remote-agent", timeout: float = 120.0) -> None:
        self.url, self.agent_name, self.timeout = url, agent_name, timeout

    def send(self, *, bearer_token: str, data: dict[str, Any], text: str = "", session_id: str | None = None,
             metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """`message/send` and return the completed Task (raises A2AError on JSON-RPC/HTTP errors)."""
        parts: list[dict[str, Any]] = [{"kind": "data", "data": data}]
        if text:
            parts.insert(0, {"kind": "text", "text": text})
        request = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
                   "params": {"message": {"role": "user", "kind": "message", "messageId": str(uuid.uuid4()),
                                          "parts": parts}, "metadata": dict(metadata or {})}}
        with client_span("a2a.client message/send", {"rpc.system": "jsonrpc", "rpc.method": "message/send",
                                                      "a2a.agent": self.agent_name}) as (span, trace_headers):
            inject_jsonrpc(request)  # trace context also travels in params.metadata
            headers = {"Content-Type": "application/json", "Accept": "application/json",
                       "Authorization": f"Bearer {bearer_token}", **trace_headers}
            if session_id:
                headers[SESSION_HEADER] = session_id
            req = urllib.request.Request(self.url, data=json.dumps(request).encode(), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read())
                    span.set_attribute("http.response.status_code", resp.status)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                span.set_attribute("http.response.status_code", exc.code)
                raise A2AError(f"{self.agent_name} HTTP {exc.code}: {detail}", status=exc.code) from exc
        if "error" in body:
            raise A2AError(f"{self.agent_name}: {body['error'].get('message')} ({body['error'].get('data')})",
                           code=body["error"].get("code"))
        task = body["result"]
        if task.get("status", {}).get("state") != "completed":
            raise A2AError(f"{self.agent_name} task ended in state {task.get('status', {}).get('state')}")
        return task


def artifact_data(task: dict[str, Any]) -> dict[str, Any]:
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                return part["data"]
    return {}
