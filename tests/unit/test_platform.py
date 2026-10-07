"""Platform: tracing, AgentCore Memory, the Bedrock LLM client, settings, and the Gateway (MCP) / A2A HTTP clients."""
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber
from opentelemetry import baggage, trace
from opentelemetry.trace import SpanKind

from src.common import a2a_client as a2a_client_mod
from src.common import config, tracer
from src.common import mcp_client as mcp_client_mod
from src.common.a2a_client import A2AClient, A2AError, artifact_data
from src.common.llm import BedrockConverseLLM
from src.common.mcp_client import GatewayClient, McpError
from src.common.memory import MAX_EVENT_TEXT, AgentCoreMemory, MemoryContext
from tests.conftest import EXPORTER


MEMORY_ID = "PortfolioCopilotMemory-AbCdEf1234"
TS = datetime(2026, 10, 3, tzinfo=timezone.utc)


def client(name):
    return boto3.client(name, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")


def _span_ctx(ctx):
    return trace.get_current_span(ctx).get_span_context()


# ------------------------------------------------------------------ config
def test_csv_parsing_and_defaults(monkeypatch):
    monkeypatch.setenv("JWT_AUDIENCES", " a , ,b,")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    s = config.Settings()
    assert s.jwt_audiences == ("a", "b") and s.model_id == config.DEFAULT_MODEL_ID and "nova" in s.model_id


# ------------------------------------------------------------------ tracer
@pytest.mark.parametrize("bad", ["garbage", "00-00000000000000000000000000000000-0000000000000000-01",
                                 "zz-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", ""])
def test_invalid_traceparent_starts_new_trace(bad):
    assert not _span_ctx(tracer.extract_context({"traceparent": bad})).is_valid


def test_extract_handles_none_and_non_string_values():
    assert not _span_ctx(tracer.extract_context(None)).is_valid
    assert not _span_ctx(tracer.extract_context({"traceparent": None, "x": 5})).is_valid


def test_header_context_wins_over_jsonrpc_metadata():
    with tracer.internal_span("a") as a:
        header_tp = tracer.current_traceparent()
    with tracer.internal_span("b"):
        req = tracer.inject_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "m", "params": {}})
    ctx = tracer.extract_jsonrpc(req, {"traceparent": header_tp})
    assert _span_ctx(ctx).trace_id == a.get_span_context().trace_id


def test_mcp_meta_carrier_and_non_mapping_params():
    with tracer.internal_span("root") as root:
        tp = tracer.current_traceparent()
    ctx = tracer.extract_jsonrpc({"params": {"_meta": {"traceparent": tp}}}, {})
    assert _span_ctx(ctx).trace_id == root.get_span_context().trace_id
    assert not _span_ctx(tracer.extract_jsonrpc({"params": ["x"]}, {})).is_valid


def test_tracestate_and_baggage_propagate():
    with tracer.internal_span("root"), tracer.with_baggage(advisor_id="ADVISOR-042"):
        headers = tracer.inject_headers()
    assert "baggage" in headers and "advisor_id=ADVISOR-042" in headers["baggage"]
    assert baggage.get_baggage("advisor_id", tracer.extract_context(headers)) == "ADVISOR-042"
    assert baggage.get_baggage("advisor_id") is None  # detached after the block


def test_lambda_xray_env_fallback(monkeypatch):
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1")
    ctx = tracer.extract_lambda({}, None)
    sc = _span_ctx(ctx)
    if sc.is_valid:  # AwsXRayPropagator installed (ADOT)
        assert format(sc.trace_id, "032x") == "5759e988bd862e3fe1be46a994272793"


def test_lambda_without_any_carrier_uses_current_context(monkeypatch):
    monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
    assert not _span_ctx(tracer.extract_lambda({"a": 1}, object())).is_valid


def test_trace_id_none_outside_span():
    assert tracer.current_trace_id() is None and tracer.current_traceparent() is None


def test_configure_tracing_is_idempotent(spans):
    before = trace.get_tracer_provider()
    tracer.configure_tracing("again")
    assert trace.get_tracer_provider() is before


def test_client_span_records_errors(spans):
    with pytest.raises(RuntimeError):
        with tracer.client_span("down"):
            raise RuntimeError("boom")
    span = spans.get_finished_spans()[-1]
    assert span.status.status_code == trace.StatusCode.ERROR and span.events[0].name == "exception"


# ------------------------------------------------------------------ memory
def test_memory_requires_id():
    with pytest.raises(ValueError):
        AgentCoreMemory("", client=object())


def test_add_turns_rejects_empty_and_truncates():
    c = client("bedrock-agentcore")
    mem = AgentCoreMemory(MEMORY_ID, client=c)
    with pytest.raises(ValueError):
        mem.add_turns("CLIENT-8801", "s1", [("USER", "")])
    long_text = "x" * (MAX_EVENT_TEXT + 500)
    with Stubber(c) as stub:
        stub.add_response("create_event", {"event": {"memoryId": MEMORY_ID, "actorId": "CLIENT-8801", "sessionId": "s1",
                                                     "eventId": "e1", "eventTimestamp": TS, "payload": []}},
                          {"memoryId": MEMORY_ID, "actorId": "CLIENT-8801", "sessionId": "s1", "eventTimestamp": ANY,
                           "clientToken": ANY,
                           "payload": [{"conversational": {"role": "USER", "content": {"text": "x" * MAX_EVENT_TEXT}}}]})
        mem.add_turns("CLIENT-8801", "s1", [("USER", long_text), ("ASSISTANT", "")])  # empty turn dropped


def _event(eid, ts, payload):
    return {"memoryId": MEMORY_ID, "actorId": "CLIENT-8801", "sessionId": "s1", "eventId": eid, "eventTimestamp": ts,
            "payload": payload}


def _conv(role, text):
    return {"conversational": {"role": role, "content": {"text": text}}}


def test_list_events_paginates_sorts_and_filters():
    c = client("bedrock-agentcore")
    mem = AgentCoreMemory(MEMORY_ID, client=c)
    with Stubber(c) as stub:
        stub.add_response("list_events", {"events": [_event("e3", TS + timedelta(minutes=2), [_conv("ASSISTANT", "third")])],
                                          "nextToken": "page2"})
        stub.add_response("list_events", {"events": [
            _event("e1", TS, [_conv("USER", "first")]),
            _event("e2", TS + timedelta(minutes=1), [{"blob": "ignored"}, _conv("USER", "second")])]},
            {"memoryId": MEMORY_ID, "actorId": "CLIENT-8801", "sessionId": "s1", "includePayloads": True,
             "maxResults": 10, "nextToken": "page2"})
        turns = mem.get_recent_turns("CLIENT-8801", "s1", max_turns=10)
    assert [t.text for t in turns] == ["first", "second", "third"]


def test_recent_turns_capped():
    c = client("bedrock-agentcore")
    mem = AgentCoreMemory(MEMORY_ID, client=c)
    events = [_event(f"e{i}", TS + timedelta(seconds=i), [_conv("USER", f"t{i}")]) for i in range(5)]
    with Stubber(c) as stub:
        stub.add_response("list_events", {"events": events, "nextToken": "more"})
        turns = mem.get_recent_turns("CLIENT-8801", "s1", max_turns=3)
    assert [t.text for t in turns] == ["t2", "t3", "t4"]


def test_retrieve_records_min_score_and_custom_namespace():
    c = client("bedrock-agentcore")
    mem = AgentCoreMemory(MEMORY_ID, client=c)
    rid = "mem-" + "a" * 40
    with Stubber(c) as stub:
        stub.add_response("retrieve_memory_records", {"memoryRecordSummaries": [
            {"memoryRecordId": rid, "content": {"text": "strong"}, "memoryStrategyId": "s", "namespaces": ["/x"],
             "createdAt": TS, "score": 0.9},
            {"memoryRecordId": rid, "content": {"text": "weak"}, "memoryStrategyId": "s", "namespaces": ["/x"],
             "createdAt": TS, "score": 0.1}]},
            {"memoryId": MEMORY_ID, "namespace": "/advisors/CLIENT-8801/notes",
             "searchCriteria": {"searchQuery": "q", "topK": 2}, "maxResults": 2})
        records = mem.retrieve_records("CLIENT-8801", "q", top_k=2, namespace="/advisors/{actorId}/notes", min_score=0.5)
    assert [r.text for r in records] == ["strong"]


def test_memory_errors_propagate():
    c = client("bedrock-agentcore")
    with Stubber(c) as stub:
        stub.add_client_error("retrieve_memory_records", "ResourceNotFoundException", "no memory")
        with pytest.raises(Exception, match="no memory"):
            AgentCoreMemory(MEMORY_ID, client=c).retrieve_records("CLIENT-8801", "q")


def test_empty_memory_context_renders_empty():
    assert MemoryContext().render() == ""


# ------------------------------------------------------------------ LLM
def test_llm_rejects_non_nova_models():
    with pytest.raises(ValueError):
        BedrockConverseLLM("anthropic.claude-3-haiku", client=object())


def test_llm_complete_joins_blocks_and_records_usage(spans):
    c = client("bedrock-runtime")
    with Stubber(c) as stub:
        stub.add_response("converse", {
            "output": {"message": {"role": "assistant", "content": [{"text": "Hello "}, {"text": "advisor"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 11, "outputTokens": 4, "totalTokens": 15},
            "metrics": {"latencyMs": 5}},
            {"modelId": "us.amazon.nova-micro-v1:0", "system": [{"text": "sys"}],
             "messages": [{"role": "user", "content": [{"text": "hi"}]}],
             "inferenceConfig": {"maxTokens": 50, "temperature": 0.0}})
        out = BedrockConverseLLM("us.amazon.nova-micro-v1:0", client=c).complete("sys", "hi", max_tokens=50, temperature=0.0)
    assert out == "Hello advisor"
    span = next(s for s in spans.get_finished_spans() if s.name == "bedrock.converse")
    assert span.attributes["gen_ai.usage.input_tokens"] == 11 and span.attributes["gen_ai.request.model"].endswith("nova-micro-v1:0")


def test_llm_throttling_surfaces(spans):
    c = client("bedrock-runtime")
    with Stubber(c) as stub:
        stub.add_client_error("converse", "ThrottlingException", "slow down")
        with pytest.raises(Exception, match="slow down"):
            BedrockConverseLLM("us.amazon.nova-micro-v1:0", client=c).complete("s", "p")
    assert spans.get_finished_spans()[-1].status.status_code == trace.StatusCode.ERROR


# ------------------------------------------------------------------ tracer (cross-service)
def test_http_traceparent_roundtrip_creates_child_span():
    with tracer.client_span("orchestrator.call") as (parent, headers):
        parent_ctx = parent.get_span_context()
    assert headers["traceparent"].split("-")[1] == format(parent_ctx.trace_id, "032x")
    with tracer.server_span("gateway.handle", tracer.extract_context({"Traceparent": headers["traceparent"]})) as child:
        assert child.get_span_context().trace_id == parent_ctx.trace_id
    server = next(s for s in EXPORTER.get_finished_spans() if s.name == "gateway.handle")
    assert server.kind == SpanKind.SERVER and server.parent.span_id == parent_ctx.span_id


def test_jsonrpc_metadata_fallback_and_lambda_extraction():
    with tracer.internal_span("root") as root:
        req = tracer.inject_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}})
        trace_id = root.get_span_context().trace_id
    ctx = tracer.extract_jsonrpc(req, headers={})
    assert trace.get_current_span(ctx).get_span_context().trace_id == trace_id

    class _LambdaCtx:
        class client_context:
            custom = {"traceparent": req["params"]["metadata"]["traceparent"]}

    ctx = tracer.extract_lambda({"ticker": "AAPL"}, _LambdaCtx())
    assert trace.get_current_span(ctx).get_span_context().trace_id == trace_id


def test_server_span_records_errors():
    with pytest.raises(ValueError):
        with tracer.server_span("boom", tracer.extract_context({})):
            raise ValueError("bad")
    assert EXPORTER.get_finished_spans()[-1].status.status_code == trace.StatusCode.ERROR


# ------------------------------------------------------------------ memory round trip
def test_memory_create_list_retrieve():
    stub_client = client("bedrock-agentcore")
    ts = datetime(2026, 10, 3, tzinfo=timezone.utc)
    mem = AgentCoreMemory(MEMORY_ID, client=stub_client)
    with Stubber(stub_client) as stub:
        stub.add_response(
            "create_event",
            {"event": {"memoryId": MEMORY_ID, "actorId": "CLIENT-8801", "sessionId": "s1",
                       "eventId": "e1", "eventTimestamp": ts,
                       "payload": [{"conversational": {"role": "USER", "content": {"text": "Deploy idle cash"}}}]}},
            {"memoryId": MEMORY_ID, "actorId": "CLIENT-8801", "sessionId": "s1", "eventTimestamp": ANY,
             "payload": [{"conversational": {"role": "USER", "content": {"text": "Deploy idle cash"}}},
                         {"conversational": {"role": "ASSISTANT", "content": {"text": "Proposed VOO buy"}}}],
             "clientToken": ANY, "metadata": ANY},
        )
        stub.add_response("list_events", {"events": [
            {"memoryId": MEMORY_ID, "actorId": "CLIENT-8801", "sessionId": "s1", "eventId": "e1",
             "eventTimestamp": ts,
             "payload": [{"conversational": {"role": "USER", "content": {"text": "Deploy idle cash"}}}]}]})
        stub.add_response(
            "retrieve_memory_records",
            {"memoryRecordSummaries": [
                {"memoryRecordId": "mem-" + "a" * 40, "content": {"text": "Client refuses fossil fuel exposure"},
                 "memoryStrategyId": "semantic-1", "namespaces": ["/clients/CLIENT-8801/constraints"],
                 "createdAt": ts, "score": 0.91}]},
            {"memoryId": MEMORY_ID, "namespace": "/clients/CLIENT-8801/constraints",
             "searchCriteria": {"searchQuery": "ESG constraints", "topK": 5}, "maxResults": 5},
        )
        with tracer.internal_span("request"):
            assert mem.add_turns("CLIENT-8801", "s1",
                                 [("USER", "Deploy idle cash"), ("ASSISTANT", "Proposed VOO buy")]) == "e1"
            ctx = mem.load_context("CLIENT-8801", "s1", "ESG constraints")
    assert [t.text for t in ctx.recent_turns] == ["Deploy idle cash"]
    assert ctx.long_term[0].score == pytest.approx(0.91)
    assert "fossil fuel" in ctx.render()
    names = {s.name for s in EXPORTER.get_finished_spans()}
    assert {"agentcore.memory.create_event", "agentcore.memory.list_events",
            "agentcore.memory.retrieve_memory_records"} <= names


# ------------------------------------------------------------------ HTTP clients (urlopen faked)
class FakeResp:
    def __init__(self, body, ctype="application/json", headers=None, status=200):
        self.body, self.status = body.encode() if isinstance(body, str) else body, status
        self.headers = {"Content-Type": ctype, **(headers or {})}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(responses, sent):
    def _open(req, timeout=None):
        sent.append({"url": req.full_url, "headers": dict(req.header_items()), "body": json.loads(req.data or b"{}")})
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return _open


def http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode()))


def test_gateway_client_session_pagination_and_unwrapping(monkeypatch, spans):
    sent = []
    responses = [
        FakeResp('{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}', headers={"Mcp-Session-Id": "sess-1"}),
        FakeResp(""),
        FakeResp('event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"a"}],"nextCursor":"c2"}}\n\n',
                 ctype="text/event-stream"),
        FakeResp('{"jsonrpc":"2.0","id":3,"result":{"tools":[{"name":"b"}]}}'),
        FakeResp('{"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"{\\"status\\":\\"success\\",\\"x\\":1}"}]}}'),
        FakeResp('{"jsonrpc":"2.0","id":5,"result":{"content":[{"type":"text","text":"plain words"}]}}'),
        FakeResp('{"jsonrpc":"2.0","id":6,"result":{"isError":true,"content":[{"type":"text","text":"{\\"message\\":\\"bad\\"}"}]}}'),
        FakeResp('{"jsonrpc":"2.0","id":7,"result":{"structuredContent":{"y":2},"content":[]}}'),
    ]
    monkeypatch.setattr(mcp_client_mod.urllib.request, "urlopen", fake_urlopen(responses, sent))
    gw = GatewayClient("https://gw/mcp", "tok")
    gw.initialize()
    assert set(gw.list_tools()) == {"a", "b"}
    assert gw.call("t", {}) == {"status": "success", "x": 1}
    assert gw.call("t", {}) == {"text": "plain words"}
    assert gw.call("t", {}) == {"message": "bad", "status": "error"}
    assert gw.call("t", {}) == {"y": 2}
    assert sent[2]["headers"]["Mcp-session-id"] == "sess-1" and sent[0]["headers"]["Authorization"] == "Bearer tok"
    assert "traceparent" in {k.lower() for k in sent[4]["headers"]} and sent[4]["body"]["params"]["_meta"]["traceparent"]
    assert sent[3]["body"]["params"] == {"cursor": "c2", "_meta": sent[3]["body"]["params"]["_meta"]}


def test_gateway_client_errors(monkeypatch):
    responses = [http_error(401, "expired"), FakeResp('{"jsonrpc":"2.0","id":2,"error":{"code":-32602,"message":"bad args"}}'),
                 FakeResp('data: {"jsonrpc":"2.0","id":999,"result":{}}\n', ctype="text/event-stream")]
    monkeypatch.setattr(mcp_client_mod.urllib.request, "urlopen", fake_urlopen(responses, []))
    gw = GatewayClient("https://gw/mcp", "tok")
    with pytest.raises(McpError) as e401:
        gw.call("t", {})
    assert e401.value.code == 401 and "expired" in str(e401.value)
    with pytest.raises(McpError) as rpc_err:
        gw.call("t", {})
    assert rpc_err.value.code == -32602
    with pytest.raises(McpError, match="no JSON-RPC response"):
        gw.call("t", {})


def test_a2a_client_paths(monkeypatch):
    task = {"kind": "task", "id": "t1", "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": "x"}, {"kind": "data", "data": {"ok": 1}}]}]}
    sent = []
    responses = [FakeResp(json.dumps({"jsonrpc": "2.0", "id": "1", "result": task})),
                 FakeResp(json.dumps({"jsonrpc": "2.0", "id": "1", "error": {"code": -32602, "message": "Invalid params"}})),
                 http_error(403, "forbidden"),
                 FakeResp(json.dumps({"jsonrpc": "2.0", "id": "1", "result": {**task, "status": {"state": "failed"}}}))]
    monkeypatch.setattr(a2a_client_mod.urllib.request, "urlopen", fake_urlopen(responses, sent))
    client = A2AClient("https://analyst/invocations", agent_name="analyst")
    out = client.send(bearer_token="tok", data={"a": 1}, text="hi", session_id="s" * 40, metadata={"k": "v"})
    assert artifact_data(out) == {"ok": 1}
    body, headers = sent[0]["body"], sent[0]["headers"]
    assert body["params"]["message"]["parts"][0] == {"kind": "text", "text": "hi"} and body["params"]["metadata"]["k"] == "v"
    assert headers["X-amzn-bedrock-agentcore-runtime-session-id"] == "s" * 40
    with pytest.raises(A2AError) as e:
        client.send(bearer_token="tok", data={})
    assert e.value.code == -32602
    with pytest.raises(A2AError) as e:
        client.send(bearer_token="tok", data={})
    assert e.value.status == 403
    with pytest.raises(A2AError, match="failed"):
        client.send(bearer_token="tok", data={})
    assert artifact_data({"artifacts": [{"parts": [{"kind": "text", "text": "only"}]}]}) == {}


def test_mcp_client_parses_sse():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n\n'
    assert GatewayClient._parse(body, "text/event-stream", 7)["result"] == {"tools": []}
