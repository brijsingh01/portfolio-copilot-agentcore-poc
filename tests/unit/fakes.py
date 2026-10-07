"""In-process stand-ins for AWS services and the AgentCore Gateway, used by the unit tests."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

from src.common.a2a_client import A2AError
from src.common.memory import MemoryContext, MemoryRecord, Turn
from src.common.tracer import inject_headers


class FakeTable:
    """DynamoDB Table: get_item only."""

    def __init__(self, items):
        self.items = {i["client_id"]: json.loads(json.dumps(i), parse_float=Decimal) for i in items}

    def get_item(self, Key, **_):
        item = self.items.get(Key["client_id"])
        return {"Item": item} if item else {}


class FakeKb:
    """bedrock-agent-runtime Retrieve."""

    def __init__(self):
        self.calls = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {"retrievalResults": [
            {"content": {"text": "Moderate: Maximum Equity Exposure: 65% of Total Portfolio Value."}, "score": 0.81,
             "location": {"s3Location": {"uri": "s3://bucket/policies/investment_policy.md"}}}]}


class FakeLLM:
    def __init__(self, fail=False):
        self.fail, self.calls, self.systems = fail, [], []

    def complete(self, system, prompt, **_):
        self.calls.append(prompt)
        self.systems.append(system)
        if self.fail:
            raise RuntimeError("bedrock throttled")
        return "Deploy idle cash into ESGV and EAGG while trimming AAPL and MSFT below the cap."


class FakeMemory:
    def __init__(self):
        self.events = []

    def load_context(self, actor_id, session_id, query, **_):
        turns = [Turn(role, text) for a, s, role, text in self.events if a == actor_id and s == session_id]
        return MemoryContext(recent_turns=turns, long_term=[
            MemoryRecord("rec-1", "Client wants to avoid adding to single-stock technology positions", 0.9)])

    def add_turns(self, actor_id, session_id, turns):
        self.events += [(actor_id, session_id, r, t) for r, t in turns]
        return "evt"


class FakeBedrockRuntime:
    """bedrock-runtime ApplyGuardrail: blocks any text containing one of `block_words`."""

    def __init__(self, block_words=("guarantee",), fail=False):
        self.block_words, self.fail, self.calls = block_words, fail, []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("ThrottlingException")
        text = kwargs["content"][0]["text"]["text"]
        if any(w in text.lower() for w in self.block_words):
            return {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": f"BLOCKED-{kwargs['source']}"}],
                    "assessments": [{"topicPolicy": {"topics": [{"name": "Guaranteed returns", "action": "BLOCKED"}]},
                                     "sensitiveInformationPolicy": {"piiEntities": [
                                         {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCKED"}]}}]}
        return {"action": "NONE", "outputs": [], "assessments": []}


def run_python(code):
    """Execute a program the way the Code Interpreter sandbox would (python, stdout captured)."""
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


class FakeCodeInterpreter:
    """bedrock-agentcore Code Interpreter sessions; really runs the program locally."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def start_code_interpreter_session(self, **kw):
        self.calls.append(("start", kw))
        return {"sessionId": "sess-1"}

    def invoke_code_interpreter(self, **kw):
        self.calls.append(("invoke", kw))
        if self.mode == "stream_error":
            return {"stream": [{"validationException": {"message": "bad"}}]}
        code, out, err = run_python(kw["arguments"]["code"]) if self.mode == "ok" else (1, "", "Traceback: boom")
        return {"stream": [{"result": {"isError": code != 0,
                                       "structuredContent": {"stdout": out, "stderr": err, "exitCode": code}}}]}

    def stop_code_interpreter_session(self, **kw):
        self.calls.append(("stop", kw))
        return {}


class FakeBedrockAgent:
    """bedrock-agent GetPrompt (Prompt Management)."""

    def __init__(self, text="You are a managed prompt.", template="TEXT", fail=0):
        self.text, self.template, self.fail, self.calls = text, template, fail, []

    def get_prompt(self, **kw):
        self.calls.append(kw)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("ThrottlingException")
        cfg = ({"text": {"text": self.text}} if self.template == "TEXT"
               else {"chat": {"system": [{"text": self.text}], "messages": []}})
        return {"id": "PRMT123", "arn": "arn:aws:bedrock:us-east-1:1:prompt/PRMT123", "version": kw["promptVersion"],
                "defaultVariant": "default",
                "variants": [{"name": "default", "templateType": self.template, "templateConfiguration": cfg}]}


def gateway_context(tool_name):
    """The Lambda context the AgentCore Gateway passes to a Lambda target."""
    return SimpleNamespace(aws_request_id="req-1", client_context=SimpleNamespace(custom={
        "bedrockAgentCoreToolName": tool_name, "bedrockAgentCoreGatewayId": "gw-1", "bedrockAgentCoreTargetId": "tgt-1"}))


class InProcessGateway:
    """Routes `<target>___<tool>` exactly like the AgentCore Gateway, using the real Lambda handlers and MCP app."""

    def __init__(self, lambdas, mcp_client):
        self.lambdas, self.mcp, self.calls = lambdas, mcp_client, []

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        target, tool = name.split("___")
        if target in self.lambdas:
            return self.lambdas[target](dict(arguments), gateway_context(name))
        resp = self.mcp.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": tool, "arguments": arguments}}, headers=inject_headers())
        return resp.json()["result"]["structuredContent"]


class InProcessAnalyst:
    """A2A client calling the real Market Analyst app."""

    def __init__(self, client):
        self.client = client

    def send(self, *, bearer_token, data, text="", session_id=None, metadata=None):
        body = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
                "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                                       "parts": [{"kind": "data", "data": data}]}}}
        resp = self.client.post("/", json=body, headers=inject_headers({"Authorization": f"Bearer {bearer_token}"}))
        payload = resp.json()
        if "error" in payload:
            raise A2AError(payload["error"]["message"], status=resp.status_code)
        return payload["result"]
