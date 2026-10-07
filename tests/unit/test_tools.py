"""Gateway tools: the Lambda target wrapper and its four tools, the Quant MCP server, Code Interpreter analytics."""
import json
import logging
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from src.common import tracer
from src.common.code_interpreter import CodeInterpreterAnalytics, CodeInterpreterError, analytics_input, render_program
from src.common.lambda_target import ToolError
from src.lambdas.compliance_rules import handler as compliance
from src.lambdas.ddb_portfolio import handler as ddb
from src.lambdas.kb_retrieval import handler as kb
from src.lambdas.portfolio_analytics import handler as analytics
from src.quant_mcp import app as mcp_app
from tests.unit.fakes import FakeCodeInterpreter, FakeKb, FakeTable, gateway_context, run_python

PORTFOLIO = "portfolio-db___get_client_portfolio"
POLICY = "policy-kb___search_investment_policy"
COMPLIANCE = "compliance-rules___review_compliance"
ANALYTICS = "portfolio-analytics___analyze_portfolio"


@pytest.fixture(autouse=True)
def aws(monkeypatch, marcus, elena):
    table, fake_kb, ci = FakeTable([marcus, elena]), FakeKb(), FakeCodeInterpreter()
    monkeypatch.setattr(ddb, "_table", lambda: table)
    monkeypatch.setattr(kb, "_client", lambda: fake_kb)
    monkeypatch.setattr(kb, "KB_ID", "KB123")
    monkeypatch.setattr(analytics, "_analytics", lambda: CodeInterpreterAnalytics("ci-1", client=ci))
    return SimpleNamespace(kb=fake_kb, ci=ci)


# ------------------------------------------------------------------ Lambda target wrapper
def test_wrapper_continues_trace_and_strips_meta(spans):
    with tracer.client_span("gateway.invoke_lambda") as (parent, headers):
        out = ddb.handler({"client_id": "CLIENT-8801", "_meta": {"traceparent": headers["traceparent"]}},
                          gateway_context(PORTFOLIO))
    assert out["status"] == "success" and out["tool"] == "get_client_portfolio" and out["trace_id"]
    assert out["portfolio"]["client_name"] == "Marcus Vance" and isinstance(out["portfolio"]["holdings"][0]["avg_cost"], float)
    span = next(s for s in spans.get_finished_spans() if s.name == "lambda get_client_portfolio")
    assert span.context.trace_id == parent.get_span_context().trace_id
    assert span.attributes["agentcore.gateway_id"] == "gw-1" and span.attributes["gen_ai.tool.name"] == "get_client_portfolio"


def test_unknown_tool_and_direct_invoke():
    assert ddb.handler({}, gateway_context("portfolio-db___drop_table"))["error_code"] == "UNKNOWN_TOOL"
    out = ddb.handler({"_tool": "get_client_portfolio", "client_id": "CLIENT-8801", "_meta": {}}, None)
    assert out["status"] == "success" and out["tool"] == "get_client_portfolio"


def test_audit_log_records_outcome_without_payloads(caplog):
    caplog.set_level(logging.INFO, logger="copilot.lambda")
    ddb.handler({"client_id": "CLIENT-8801"}, gateway_context(PORTFOLIO))
    ddb.handler({"client_id": "nope"}, gateway_context(PORTFOLIO))
    audits = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith('{"audit"')]
    assert [a["outcome"] for a in audits] == ["OK", "ERROR"] and audits[0]["args"] == ["client_id"]
    assert audits[1]["error_code"] == "INVALID_ARGUMENT"


def test_service_errors_become_masked_tool_errors(monkeypatch, spans):
    def boom(**_):
        raise RuntimeError("ThrottlingException internal detail")

    monkeypatch.setattr(kb, "_client", lambda: SimpleNamespace(retrieve=boom))
    out = kb.handler({"query": "x"}, gateway_context(POLICY))
    assert out["status"] == "error" and out["error_code"] == "INTERNAL" and "detail" not in out["message"]
    span = next(s for s in spans.get_finished_spans() if s.name == "lambda search_investment_policy")
    assert span.status.status_code.name == "ERROR"


# ------------------------------------------------------------------ portfolio-db
@pytest.mark.parametrize("cid", ["8801", "DROP TABLE", "client 8801", ""])
def test_portfolio_rejects_invalid_ids(cid):
    assert ddb.handler({"client_id": cid}, gateway_context(PORTFOLIO))["error_code"] == "INVALID_ARGUMENT"


def test_portfolio_normalises_and_reports_not_found():
    assert ddb.handler({"client_id": " client-8801 "}, gateway_context(PORTFOLIO))["portfolio"]["client_id"] == "CLIENT-8801"
    assert ddb.handler({"client_id": "CLIENT-9999"}, gateway_context(PORTFOLIO))["error_code"] == "NOT_FOUND"


# ------------------------------------------------------------------ policy-kb
def test_policy_search_returns_cited_passages(aws):
    out = kb.handler({"query": "max equity moderate", "top_k": 50}, gateway_context(POLICY))
    assert out["status"] == "success" and "65%" in out["passages"][0]["text"]
    assert out["passages"][0]["source"].endswith("investment_policy.md")
    assert aws.kb.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 10


def test_policy_search_validation_and_filters(aws, monkeypatch):
    assert kb.handler({}, gateway_context(POLICY))["error_code"] == "INVALID_ARGUMENT"
    kb.handler({"query": "q" * 5000}, gateway_context(POLICY))
    assert len(aws.kb.calls[-1]["retrievalQuery"]["text"]) == kb.MAX_QUERY_CHARS
    monkeypatch.setattr(kb, "MIN_SCORE", 0.95)
    out = kb.handler({"query": "x", "top_k": 0}, gateway_context(POLICY))
    assert out["passages"] == [] and aws.kb.calls[-1]["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 4
    monkeypatch.setattr(kb, "KB_ID", "")
    assert kb.handler({"query": "x"}, gateway_context(POLICY))["error_code"] == "MISCONFIGURED"


# ------------------------------------------------------------------ compliance-rules
def test_compliance_rules_tool(marcus):
    current = compliance.handler({"portfolio": marcus, "requested_by": "ADVISOR-042"}, gateway_context(COMPLIANCE))
    assert current["status"] == "success" and current["review"]["status"] == "REJECTED"  # over-concentrated today
    assert current["review"]["reviewed_by"] == "ADVISOR-042" and current["review"]["evaluated"] == "current_holdings"
    lmt = compliance.handler({"portfolio": marcus, "proposed_trades": [{"ticker": "LMT", "action": "BUY", "shares": 20}]},
                             gateway_context(COMPLIANCE))
    assert lmt["review"]["status"] == "REJECTED" and lmt["review"]["evaluated"] == "post_trade"
    assert any(f["rule_id"] == "ESG_EXCLUSION" for f in lmt["review"]["findings"])
    for bad in ({"portfolio": "nope"}, {"portfolio": marcus, "proposed_trades": "BUY"}, {"portfolio": {**marcus, "risk_profile": "YOLO"}}):
        out = compliance.handler(bad, gateway_context(COMPLIANCE))
        assert out["status"] == "error" and out["error_code"] == "INVALID_ARGUMENT"


# ------------------------------------------------------------------ portfolio-analytics (Code Interpreter)
def test_analytics_program_round_trips_payload(marcus):
    code = render_program(analytics_input(marcus, [{"ticker": "AAPL", "action": "SELL", "shares": 10}]))
    assert "{payload}" not in code
    rc, out, err = run_python(code)
    assert rc == 0, err
    result = json.loads(out.strip().splitlines()[-1])
    assert result["before"]["hhi"] > 0 and result["after"]["effective_holdings"] > 0


def test_code_interpreter_session_lifecycle(marcus, spans):
    client = FakeCodeInterpreter()
    out = CodeInterpreterAnalytics("ci-1", client=client).run(marcus, [{"ticker": "LMT", "action": "BUY", "shares": 5}])
    assert [c[0] for c in client.calls] == ["start", "invoke", "stop"]
    invoke = client.calls[1][1]
    assert invoke["name"] == "executeCode" and invoke["arguments"]["language"] == "python"
    assert invoke["sessionId"] == "sess-1" and invoke["codeInterpreterIdentifier"] == "ci-1"
    assert out["unpriced_trades"] == ["LMT"]
    assert "agentcore.code_interpreter.execute" in [s.name for s in spans.get_finished_spans()]


@pytest.mark.parametrize("mode", ["program_error", "stream_error"])
def test_code_interpreter_failures_still_stop_session(marcus, mode):
    client = FakeCodeInterpreter(mode)
    with pytest.raises(CodeInterpreterError):
        CodeInterpreterAnalytics("ci-1", client=client).run(marcus, [])
    assert client.calls[-1][0] == "stop"


def test_analytics_tool(aws, marcus):
    ctx = gateway_context(ANALYTICS)
    out = analytics.handler({"portfolio": marcus, "proposed_trades": [{"ticker": "AAPL", "action": "SELL", "shares": 50}]}, ctx)
    assert out["status"] == "success" and out["analytics"]["engine"] == "agentcore-code-interpreter"
    assert out["analytics"]["after"]["weights"]["AAPL"] < out["analytics"]["before"]["weights"]["AAPL"]
    for bad in ({"portfolio": {"holdings": "x"}}, {"portfolio": marcus, "proposed_trades": "x"}):
        assert analytics.handler(bad, ctx)["error_code"] == "INVALID_ARGUMENT"
    aws.ci.mode = "program_error"
    assert analytics.handler({"portfolio": marcus}, ctx)["error_code"] == "ANALYTICS_FAILED"


def test_analytics_tool_requires_configuration(monkeypatch):
    from src.common.config import get_settings
    monkeypatch.undo()  # the real _analytics, without the autouse fake
    monkeypatch.delenv("CODE_INTERPRETER_ID", raising=False)
    get_settings.cache_clear()
    analytics._analytics.cache_clear()
    with pytest.raises(ToolError):
        analytics._analytics()
    get_settings.cache_clear()


# ------------------------------------------------------------------ Quant MCP server
def rpc(method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if rid is not None:
        body["id"] = rid
    return body


@pytest.fixture
def mcp():
    return TestClient(mcp_app.create_app())


def test_mcp_initialize_session_and_discovery(mcp):
    resp = mcp.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18"}))
    session = resp.headers["Mcp-Session-Id"]
    assert resp.json()["result"]["protocolVersion"] == "2025-06-18" and session
    assert mcp.post("/mcp", json=rpc("notifications/initialized", rid=None), headers={"Mcp-Session-Id": session}).status_code == 202
    listed = mcp.post("/mcp", json=rpc("tools/list"), headers={"Mcp-Session-Id": session})
    tools = listed.json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"calculate_rebalance", "estimate_tax_drag"}
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True and set(t["inputSchema"]["required"]) <= set(t["inputSchema"]["properties"])
    assert mcp.post("/mcp", json=rpc("ping"), headers={"MCP-Protocol-Version": "1999-01-01"}).status_code == 400


def test_mcp_version_negotiation_and_stateless_sessions(mcp):
    assert mcp.post("/mcp", json=rpc("initialize", {"protocolVersion": "1999-01-01"})).json()["result"]["protocolVersion"] == \
        mcp_app.SUPPORTED_VERSIONS[0]
    s1 = mcp.post("/mcp", json=rpc("initialize")).headers["Mcp-Session-Id"]
    assert s1 != mcp.post("/mcp", json=rpc("initialize")).headers["Mcp-Session-Id"]
    resp = mcp.post("/mcp", json=rpc("ping"), headers={"Mcp-Session-Id": "never-issued"})
    assert resp.json()["result"] == {} and resp.headers["Mcp-Session-Id"] == "never-issued"


def test_mcp_rebalance_and_tax(mcp, marcus, spans):
    targets = {"AAPL": 0.08, "MSFT": 0.08, "VOO": 0.30, "ESGV": 0.20, "EAGG": 0.25}
    with tracer.client_span("gateway.tools_call") as (_, headers):
        result = mcp.post("/mcp", headers=headers, json=rpc("tools/call", {"name": "calculate_rebalance", "arguments": {
            "portfolio": marcus, "target_weights": targets, "_meta": {"traceparent": "x"}}})).json()["result"]
    assert result["isError"] is False
    trades = result["structuredContent"]["trades"]
    assert {t["ticker"] for t in trades if t["action"] == "SELL"} == {"AAPL", "MSFT"}
    span = next(s for s in spans.get_finished_spans() if s.name == "mcp tools/call")
    assert span.attributes["mcp.tool.name"] == "calculate_rebalance" and span.parent is not None
    tax = mcp.post("/mcp", json=rpc("tools/call", {"name": "estimate_tax_drag", "arguments": {
        "portfolio": marcus, "trades": trades}})).json()["result"]["structuredContent"]
    assert tax["estimated_tax"] > 0 and 0 < tax["tax_drag_pct_of_portfolio"] < 0.01


def test_mcp_input_errors_are_tool_results(mcp, marcus):
    resp = mcp.post("/mcp", json=rpc("tools/call", {"name": "calculate_rebalance",
                                                    "arguments": {"portfolio": marcus, "target_weights": {"AAPL": 1.5}}}))
    assert resp.status_code == 200 and resp.json()["result"]["isError"] is True


@pytest.mark.parametrize("params", [{"name": "rm_rf"}, {"name": 5}, {"name": "estimate_tax_drag", "arguments": "x"}])
def test_mcp_bad_tool_calls(mcp, params):
    assert mcp.post("/mcp", json=rpc("tools/call", params)).json()["error"]["code"] == -32602


def test_mcp_protocol_errors(mcp):
    assert mcp.post("/mcp", json=rpc("resources/list")).json()["error"]["code"] == -32601
    assert mcp.post("/mcp", content=b"{oops").status_code == 400
    assert mcp.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "result": {}}).status_code == 202
    assert mcp.delete("/mcp").status_code == 405 and mcp.get("/mcp").headers["allow"] == "POST"
    assert mcp.get("/ping").json()["status"] == "Healthy"


def test_mcp_ping_is_answered_without_a_span(mcp, spans):
    """Gateway health checks must not create a trace (or log line) each."""
    resp = mcp.post("/mcp", json=rpc("ping", rid=7))
    assert resp.status_code == 200 and resp.json() == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert not any(s.name == "mcp ping" for s in spans.get_finished_spans())
