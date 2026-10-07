"""Agents: the Orchestrator (full flow against the real tool code), the Market Analyst A2A server, the Bedrock
Guardrail, Prompt Management and quiet health-check logging."""
import io
import json
import logging
import time
import uuid
from types import SimpleNamespace

import pytest
from opentelemetry.trace import SpanKind
from starlette.testclient import TestClient

from src.common import server, tracer
from src.common.a2a import create_a2a_app
from src.common.a2a_client import A2AError
from src.common.code_interpreter import CodeInterpreterAnalytics
from src.common.guardrail import BedrockGuardrail
from src.common.mcp_client import McpError
from src.common.prompts import PromptStore, PromptUnavailable
from src.lambdas.compliance_rules import handler as compliance
from src.lambdas.ddb_portfolio import handler as ddb
from src.lambdas.kb_retrieval import handler as kb
from src.lambdas.portfolio_analytics import handler as analytics
from src.market_analyst import app as analyst_app
from src.orchestrator import agent
from src.quant_mcp import app as mcp_app
from tests.conftest import make_token
from tests.unit.fakes import (FakeBedrockAgent, FakeBedrockRuntime, FakeCodeInterpreter, FakeKb, FakeLLM, FakeMemory,
                              FakeTable, InProcessAnalyst, InProcessGateway)


# =========================================================================== Orchestrator
@pytest.fixture
def stack(monkeypatch, validator, marcus, elena):
    table, ci = FakeTable([marcus, elena]), FakeCodeInterpreter()
    monkeypatch.setattr(ddb, "_table", lambda: table)
    monkeypatch.setattr(kb, "_client", lambda: FakeKb())
    monkeypatch.setattr(kb, "KB_ID", "KB123")
    monkeypatch.setattr(analytics, "_analytics", lambda: CodeInterpreterAnalytics("ci-1", client=ci))
    gateway = InProcessGateway({"portfolio-db": ddb.handler, "policy-kb": kb.handler, "compliance-rules": compliance.handler,
                                "portfolio-analytics": analytics.handler}, TestClient(mcp_app.create_app()))
    guard, llm, memory = FakeBedrockRuntime(block_words=("guarantee", "123-45-6789")), FakeLLM(), FakeMemory()
    orch = agent.PortfolioOrchestrator(
        gateway_factory=lambda _token: gateway, llm=llm, memory=memory,
        analyst=InProcessAnalyst(TestClient(analyst_app.create_app(validator, FakeLLM()))),
        guardrail=BedrockGuardrail("gr-1", "1", client=guard))
    return SimpleNamespace(app=TestClient(agent.create_app(orch, validator)), gateway=gateway, guard=guard, llm=llm,
                           memory=memory, ci=ci, orch=orch)


def invoke(stack, body, token=None, session="session-0123456789abcdef0123456789abcdef"):
    return stack.app.post("/invocations", json=body, headers={
        "Authorization": f"Bearer {token or make_token()}", "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session})


def test_end_to_end_recommendation(stack, spans):
    resp = invoke(stack, {"prompt": "Marcus has idle cash. Propose a compliant rebalance."})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["compliance"]["status"] in ("APPROVED", "APPROVED_WITH_WARNINGS") and not out["compliance_violation"]
    assert out["compliance"]["reviewed_by"] == "ADVISOR-042"
    assert {t["ticker"] for t in out["proposed_trades"] if t["action"] == "SELL"} == {"AAPL", "MSFT"}
    assert out["tax"]["estimated_tax"] > 0 and out["persistent_constraints"] and out["narrative_source"] == "llm"
    assert out["guardrail"] == {"input": "PASSED", "output": "PASSED", "policies": []}
    assert out["analytics"]["engine"] == "agentcore-code-interpreter"
    assert out["system_prompts"]["orchestrator"]["source"] == "bundled_file"
    assert [n for n, _ in stack.gateway.calls] == [agent.TOOL_PORTFOLIO, agent.TOOL_POLICY, agent.TOOL_REBALANCE,
                                                   agent.TOOL_TAX, agent.TOOL_COMPLIANCE, agent.TOOL_ANALYTICS]
    for name, args in stack.gateway.calls:  # identity-agnostic tools: plain arguments + trace context only
        assert set(args) - {"_meta"} and all(not k.startswith("_") or k == "_meta" for k in args), name
    trace_ids = {format(s.context.trace_id, "032x") for s in spans.get_finished_spans()}
    names = {s.name for s in spans.get_finished_spans()}
    assert trace_ids == {out["trace_id"]}  # one trace across orchestrator, Lambda tools, analyst A2A and MCP
    assert {"orchestrator.invoke", "lambda get_client_portfolio", "a2a message/send", "mcp tools/call",
            "lambda review_compliance", "bedrock.apply_guardrail", "agentcore.code_interpreter.execute"} <= names


def test_compliance_violation_flagged(stack):
    out = invoke(stack, {"prompt": "Buy Lockheed for Marcus", "proposed_trades": [
        {"ticker": "LMT", "action": "BUY", "shares": 20}]}).json()
    assert out["compliance_violation"] is True and out["trade_source"] == "advisor"
    assert any(f["rule_id"] == "ESG_EXCLUSION" for f in out["compliance"]["findings"])
    assert "REJECTED" in stack.llm.calls[-1]


def test_tenancy_enforced_by_the_orchestrator(stack):
    # another advisor's token that claims Marcus: the tools would return him, the orchestrator must refuse
    resp = invoke(stack, {"prompt": "Show Marcus"}, token=make_token(advisor_id="ADVISOR-099", client_id="CLIENT-8801"))
    assert resp.status_code == 403 and [n for n, _ in stack.gateway.calls] == [agent.TOOL_PORTFOLIO]
    assert invoke(stack, {"prompt": "Show Elena", "client_id": "CLIENT-8802"}).status_code == 403  # token is for 8801


def test_memory_persisted_per_session(stack):
    session = "session-memory-0123456789abcdef0123456789"
    invoke(stack, {"prompt": "First question"}, session=session)
    second = invoke(stack, {"prompt": "Follow-up question"}, session=session).json()
    assert second["memory"]["recent_turns"] == 2
    assert [e[2] for e in stack.memory.events] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]
    assert all(e[0] == "CLIENT-8801" for e in stack.memory.events)


# ------------------------------------------------------------------ request validation
@pytest.mark.parametrize("body", [None, [], "text", {}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 5},
                                  {"prompt": "x", "proposed_trades": "BUY"}, {"prompt": "x", "proposed_trades": [1]}])
def test_advisor_request_rejects(body):
    with pytest.raises(ValueError):
        agent.AdvisorRequest.parse(body)


def test_advisor_request_trims_and_caps():
    req = agent.AdvisorRequest.parse({"prompt": "  " + "a" * 5000 + "  "})
    assert len(req.prompt) == 4000 and req.proposed_trades is None


def test_bad_requests(stack):
    assert stack.app.post("/invocations", json={"prompt": "x"}).status_code == 401
    assert invoke(stack, {"prompt": "x"}, token=make_token(exp=int(time.time()) - 3600)).status_code == 401
    assert invoke(stack, {"nope": 1}).status_code == 400
    resp = stack.app.post("/invocations", content=b"not json", headers={"Authorization": f"Bearer {make_token()}"})
    assert resp.status_code == 400


def test_client_id_normalised_and_default(stack):
    assert invoke(stack, {"prompt": "review", "client_id": "client-8801"}).json()["client"]["client_id"] == "CLIENT-8801"
    assert invoke(stack, {"prompt": "review"}).json()["client"]["client_id"] == "CLIENT-8801"


def test_empty_trade_list_reviews_current_holdings(stack):
    out = invoke(stack, {"prompt": "do nothing", "proposed_trades": []}).json()
    assert out["compliance"]["status"] == "REJECTED" and out["tax"]["estimated_tax"] == 0 and out["trade_source"] == "advisor"


def test_the_callers_own_token_is_used_for_the_gateway(stack):
    tokens = []
    real_factory = stack.orch.gateway_factory
    stack.orch.gateway_factory = lambda t: tokens.append(t) or real_factory(t)
    user_token = make_token()
    invoke(stack, {"prompt": "review"}, token=user_token)
    assert tokens == [user_token]


# ------------------------------------------------------------------ resilience
def test_memory_failures_do_not_block(stack):
    class Broken:
        def load_context(self, *a, **k):
            raise RuntimeError("memory down")

        def add_turns(self, *a, **k):
            raise RuntimeError("memory down")

    stack.orch.memory = Broken()
    out = invoke(stack, {"prompt": "review"}).json()
    assert out["compliance"]["status"] and out["memory"] == {"recent_turns": 0, "constraints_applied": 0}
    stack.orch.memory = None
    assert invoke(stack, {"prompt": "review"}).status_code == 200


def test_llm_failure_falls_back_to_deterministic_summary(stack):
    stack.llm.fail = True
    out = invoke(stack, {"prompt": "review"}).json()
    assert out["narrative_source"] == "fallback" and out["recommendation"].startswith("Compliance:")
    stack.orch.llm = SimpleNamespace(complete=lambda *a, **k: "")
    assert invoke(stack, {"prompt": "review"}).json()["narrative_source"] == "fallback"


@pytest.mark.parametrize("status,expected", [(503, 502), (403, 403)])
def test_analyst_failures(stack, status, expected):
    class Down:
        def send(self, **_):
            raise A2AError(f"analyst HTTP {status}", status=status)

    stack.orch.analyst = Down()
    resp = invoke(stack, {"prompt": "review"})
    assert resp.status_code == expected and resp.json()["trace_id"]


@pytest.mark.parametrize("exc,status", [(McpError("HTTP 401", 401), 401), (McpError("HTTP 500", 500), 502),
                                        (McpError("rpc", -32602), 502)])
def test_gateway_transport_failures(stack, exc, status):
    class Failing:
        def call(self, *_):
            raise exc

    stack.orch.gateway_factory = lambda _t: Failing()
    assert invoke(stack, {"prompt": "review"}).status_code == status


def test_tool_errors_map_to_http(stack, monkeypatch):
    monkeypatch.setattr(ddb, "_table", lambda: SimpleNamespace(get_item=lambda **_: {}))
    resp = invoke(stack, {"prompt": "review"})
    assert resp.status_code == 404 and resp.json()["component"] == agent.TOOL_PORTFOLIO


def test_internal_tool_error_is_502(stack, monkeypatch):
    def boom(**_):
        raise RuntimeError("down")

    monkeypatch.setattr(kb, "_client", lambda: SimpleNamespace(retrieve=boom))
    resp = invoke(stack, {"prompt": "review"})
    assert resp.status_code == 502 and resp.json()["component"] == agent.TOOL_POLICY


def test_compliance_without_verdict_is_502(stack):
    real = stack.gateway.call
    stack.gateway.call = lambda n, a: {"status": "success", "review": {}} if n == agent.TOOL_COMPLIANCE else real(n, a)
    resp = invoke(stack, {"prompt": "review"})
    assert resp.status_code == 502 and resp.json()["component"] == "compliance"


def test_analytics_failure_is_not_fatal(stack):
    stack.ci.mode = "program_error"
    out = invoke(stack, {"prompt": "Propose a rebalance"}).json()
    assert out["analytics"] == {"error": "analytics_unavailable"} and out["compliance"]["status"]


# ------------------------------------------------------------------ guardrail in the flow
def test_input_guardrail_blocks_before_any_tool(stack):
    out = invoke(stack, {"prompt": "Guarantee Marcus 20% returns"}).json()
    assert out["guardrail_blocked"] is True and out["recommendation"] == "BLOCKED-INPUT"
    assert out["guardrail"]["input"] == "INTERVENED" and "topic:Guaranteed returns" in out["guardrail"]["policies"]
    assert stack.gateway.calls == [] and stack.llm.calls == [] and stack.ci.calls == [] and stack.memory.events == []


def test_output_guardrail_falls_back(stack, monkeypatch):
    monkeypatch.setattr(stack.llm, "complete", lambda *a, **k: "We guarantee this will outperform.")
    out = invoke(stack, {"prompt": "Propose a rebalance"}).json()
    assert out["narrative_source"] == "guardrail_fallback" and "guarantee" not in out["recommendation"].lower()
    assert out["guardrail"]["output"] == "INTERVENED"


def test_output_guardrail_fallback_with_advisor_trades(stack, monkeypatch):
    """Regression: advisor-supplied trades have no est_value; the fallback narrative must not crash."""
    monkeypatch.setattr(stack.llm, "complete", lambda *a, **k: "We guarantee Lockheed will outperform.")
    resp = invoke(stack, {"prompt": "Buy Lockheed for Marcus", "proposed_trades": [{"ticker": "LMT", "action": "BUY", "shares": 20}]})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["narrative_source"] == "guardrail_fallback" and "BUY 20 LMT" in out["recommendation"]


def test_guardrail_failure_fails_closed(stack):
    stack.guard.fail = True
    resp = invoke(stack, {"prompt": "Propose a rebalance"})
    assert resp.status_code == 502 and resp.json()["component"] == "guardrail" and stack.gateway.calls == []


def test_guardrail_client(spans):
    client = FakeBedrockRuntime()
    g = BedrockGuardrail("gr-1", "3", client=client)
    assert not g.check("Rebalance Marcus into ESG funds", "INPUT").intervened
    blocked = g.check("Guarantee 20% returns", "OUTPUT")
    assert blocked.intervened and blocked.text == "BLOCKED-OUTPUT"
    assert blocked.policies == ("topic:Guaranteed returns", "pii:US_SOCIAL_SECURITY_NUMBER")
    assert client.calls[0]["guardrailIdentifier"] == "gr-1" and client.calls[0]["guardrailVersion"] == "3"
    assert [s.name for s in spans.get_finished_spans()].count("bedrock.apply_guardrail") == 2


# ------------------------------------------------------------------ Prompt Management
def test_orchestrator_uses_managed_prompt_and_reports_version(stack):
    stack.orch.prompts = PromptStore("orchestrator_system", prompt_id="PRMT123", version="7",
                                     client=FakeBedrockAgent("MANAGED ORCHESTRATOR PROMPT v7"))
    out = invoke(stack, {"prompt": "Propose a rebalance"}).json()
    assert stack.llm.systems == ["MANAGED ORCHESTRATOR PROMPT v7"]
    assert out["system_prompts"]["orchestrator"] == {"source": "prompt_management", "id": "PRMT123", "version": "7"}


def test_prompt_outage_degrades_to_fallback_narrative(stack):
    stack.orch.prompts = PromptStore("orchestrator_system", prompt_id="P", version="1", client=FakeBedrockAgent(fail=5))
    out = invoke(stack, {"prompt": "Propose a rebalance"}).json()
    assert out["narrative_source"] == "fallback" and out["compliance"]["status"] and stack.llm.calls == []


def test_prompt_store_pins_and_caches(spans):
    client = FakeBedrockAgent()
    store = PromptStore("orchestrator_system", prompt_id="PRMT123", version="3", client=client)
    first, second = store.get(), store.get()
    assert first is second and first.describe() == {"source": "prompt_management", "id": "PRMT123", "version": "3"}
    assert client.calls == [{"promptIdentifier": "PRMT123", "promptVersion": "3"}]
    assert "bedrock.get_prompt" in [s.name for s in spans.get_finished_spans()]
    assert PromptStore("x", prompt_id="P", version="1", client=FakeBedrockAgent("chat system", template="CHAT")).get().text == "chat system"
    with pytest.raises(ValueError):
        PromptStore("x", prompt_id="P", version="")


def test_prompt_store_retries_after_failure():
    client = FakeBedrockAgent(fail=1)
    store = PromptStore("x", prompt_id="P", version="1", client=client)
    with pytest.raises(PromptUnavailable):
        store.get()
    assert store.get().text == "You are a managed prompt." and len(client.calls) == 2


def test_bundled_prompt_files():
    for name in ("orchestrator_system", "market_analyst_system"):
        prompt = PromptStore(name).get()
        assert prompt.source == "bundled_file" and len(prompt.text) > 100


# =========================================================================== Market Analyst (A2A)
def auth(token=None):
    return {"Authorization": f"Bearer {token or make_token()}"}


def a2a_send(data, *, text="Review this portfolio", role="user", parts=None):
    msg = {"role": role, "messageId": str(uuid.uuid4()), "kind": "message",
           "parts": parts if parts is not None else [{"kind": "text", "text": text}, {"kind": "data", "data": data}]}
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send", "params": {"message": msg}}


def artifact(resp):
    return resp.json()["result"]["artifacts"][0]["parts"][0]["data"]


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def analyst(validator, llm):
    return TestClient(analyst_app.create_app(validator, llm))


def test_agent_card_and_ping(analyst):
    card = analyst.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Market Analyst" and card["protocolVersion"] == "0.3.0" and card["preferredTransport"] == "JSONRPC"
    assert card["skills"] and card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert analyst.get("/.well-known/agent.json").status_code == 200 and analyst.get("/ping").json()["status"] == "Healthy"


def test_analyst_targets_respect_ips(analyst, llm, marcus, spans):
    with tracer.client_span("orchestrator.market_analyst") as (parent, headers):
        resp = analyst.post("/", json=a2a_send({"portfolio": marcus}), headers={**auth(), **headers})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["status"]["state"] == "completed"
    data = artifact(resp)
    targets = data["target_weights"]
    assert targets["AAPL"] <= 0.10 and targets["MSFT"] <= 0.10
    assert {"ESGV", "EAGG"} <= set(targets) and "BND" not in targets  # ESG mandate -> ESG core funds
    assert data["target_allocation"]["equity"] <= 0.65 and data["target_allocation"]["fixed_income"] >= 0.25
    assert any("Idle cash" in o for o in data["observations"])
    assert data["commentary_source"] == "llm" and "CLIENT-8801" in llm.calls[0]
    assert data["system_prompt"]["source"] == "bundled_file"
    server_span = next(s for s in spans.get_finished_spans() if s.name == "a2a message/send")
    assert server_span.kind == SpanKind.SERVER and server_span.parent.span_id == parent.get_span_context().span_id
    assert server_span.attributes["copilot.advisor_id"] == "ADVISOR-042"


def test_analyst_requires_the_callers_own_client(analyst, marcus):
    assert analyst.post("/", json=a2a_send({"portfolio": marcus})).status_code == 401
    other = make_token(advisor_id="ADVISOR-099", client_id="CLIENT-8801")
    resp = analyst.post("/", json=a2a_send({"portfolio": marcus}), headers=auth(other))
    assert resp.status_code == 403 and resp.json()["error"]["code"] == -32043


def test_analyst_degrades_when_llm_fails(validator, marcus):
    client = TestClient(analyst_app.create_app(validator, FakeLLM(fail=True)))
    data = artifact(client.post("/", json=a2a_send({"portfolio": marcus}), headers=auth()))
    assert data["commentary_source"] == "fallback" and "Idle cash" in data["commentary"]


def test_jsonrpc_errors(analyst):
    assert analyst.post("/", content=b"{not json", headers=auth()).json()["error"]["code"] == -32700
    assert analyst.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "nope"}, headers=auth()).json()["error"]["code"] == -32601
    assert analyst.post("/", json={"jsonrpc": "2.0", "id": 2, "method": "message/stream"}, headers=auth()).json()["error"]["code"] == -32004
    assert analyst.post("/", json=a2a_send({"question": "no portfolio"}), headers=auth()).json()["error"]["code"] == -32602
    for body in ([{"jsonrpc": "2.0", "id": 1}], {"jsonrpc": "1.0", "id": 1, "method": "message/send"},
                 {"jsonrpc": "2.0", "id": 1, "method": 42}):
        resp = analyst.post("/", json=body, headers=auth())
        assert resp.status_code == 400 and resp.json()["error"]["code"] == -32600
    assert analyst.get("/").status_code == 405


@pytest.mark.parametrize("kwargs", [{"role": "agent"}, {"parts": []}, {"parts": [{"kind": "file", "file": {}}]},
                                    {"parts": [{"kind": "text"}]}, {"parts": ["not-an-object"]}])
def test_message_validation(analyst, marcus, kwargs):
    assert analyst.post("/", json=a2a_send({"portfolio": marcus}, **kwargs), headers=auth()).json()["error"]["code"] == -32602


def test_tasks_lifecycle_and_ownership(validator, marcus):
    client = TestClient(create_a2a_app(analyst_app.build_spec(FakeLLM(), PromptStore(analyst_app.PROMPT_NAME)),
                                       validator, max_tasks=2))
    ids = [client.post("/", json=a2a_send({"portfolio": marcus}), headers=auth()).json()["result"]["id"] for _ in range(3)]

    def call(method, tid, token=None):
        return client.post("/", json={"jsonrpc": "2.0", "id": 9, "method": method, "params": {"id": tid}},
                           headers=auth(token)).json()

    assert call("tasks/get", ids[0])["error"]["code"] == -32001          # evicted (LRU of 2)
    assert call("tasks/get", ids[2])["result"]["id"] == ids[2]
    assert call("tasks/get", ids[2], make_token(sub="user-2"))["error"]["code"] == -32001  # another caller's task
    assert call("tasks/cancel", ids[2])["error"]["code"] == -32002      # completed tasks cannot be canceled


def test_internal_errors_are_masked(validator, marcus):
    def boom(_req):
        raise RuntimeError("secret stack detail")

    spec = analyst_app.build_spec(FakeLLM(), PromptStore(analyst_app.PROMPT_NAME))
    spec = spec.__class__(**{**spec.__dict__, "handler": boom})
    body = TestClient(create_a2a_app(spec, validator)).post("/", json=a2a_send({"portfolio": marcus}), headers=auth()).json()
    assert body["error"]["code"] == -32603 and "secret" not in json.dumps(body)


# =========================================================================== quiet health checks
def test_logging_drops_probe_noise_and_quiets_libraries():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, {n: logging.getLogger(n).level for n in server.NOISY_LIBRARIES})
    root.handlers = []
    root.__dict__.pop("_copilot_configured", None)
    try:
        server.configure_logging()
        server.configure_logging()  # idempotent
        assert len(root.handlers) == 1
        buf = io.StringIO()
        root.handlers[0].setStream(buf)
        logging.getLogger("uvicorn.error").warning("Invalid HTTP request received.")
        logging.getLogger("uvicorn.error").info("Started server process [1]")
        logging.getLogger("botocore.credentials").info("Found credentials from IAM Role: execution_role")
        logging.getLogger("copilot.app").warning("real problem")
        lines = buf.getvalue().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["msg"] == "real problem" and "trace_id" in json.loads(lines[0])
        record = logging.LogRecord("uvicorn.error", logging.WARNING, __file__, 1, "Invalid HTTP request received.", None, None)
        assert logging.getLogger("uvicorn.error").filter(record) is False  # filtered for every handler
    finally:
        root.handlers, root.level = saved[0], saved[1]
        root.__dict__.pop("_copilot_configured", None)
        for n, lvl in saved[2].items():
            logging.getLogger(n).setLevel(lvl)


def test_ping_route_is_not_logged(caplog):
    client = TestClient(mcp_app.create_app())
    with caplog.at_level("DEBUG"):
        assert client.get("/ping").json()["status"] == "Healthy"
    assert not [r for r in caplog.records if "/ping" in r.getMessage() and not r.name.startswith("httpx")]
