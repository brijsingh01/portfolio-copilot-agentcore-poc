"""Deployed end-to-end verification (opt-in):

    E2E=1 COPILOT_TOKEN=<Entra token of advisor ADVISOR-042> python -m pytest tests/test_e2e.py -v

Drives the live Orchestrator runtime with real Microsoft Entra ID access tokens and verifies the whole POC:
  * advisory flow       - Gateway tools (KB, DynamoDB, compliance rules, Quant MCP) + A2A Market Analyst + Nova
  * compliance flagging - an ESG-prohibited trade is REJECTED with an ESG_EXCLUSION finding
  * tenancy             - an advisor cannot review another advisor's client
  * Bedrock Guardrail   - denied topics / PII blocked on input; recommendations screened on output
  * Code Interpreter    - portfolio analytics in the VPC sandbox, reached through the portfolio-analytics tool
  * Prompt Management   - both agents' system prompts come from pinned Bedrock prompt versions
  * memory              - short-term events and long-term constraint extraction, isolated per client
  * tracing             - one trace id spans orchestrator, analyst, MCP server and Lambda tools (aws/spans)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.e2e_support import ADVISOR2, ROOT, Deployment, container_status, new_session, query_spans, requires_deployment

pytestmark = requires_deployment


@pytest.fixture(scope="module")
def dep() -> Deployment:
    return Deployment()


@pytest.fixture(scope="module")
def review(dep):
    session = new_session()
    status, body = dep.invoke(dep.token(), {
        "prompt": "Marcus Vance is sitting on idle cash. He told me he never wants exposure to fossil fuels. "
                  "Propose a compliant rebalance."}, session)
    assert status == 200, body
    return {"session": session, "body": body}


# ------------------------------------------------------------------ advisory flow
def test_recommendation_end_to_end(review):
    body = review["body"]
    assert body["recommendation"] and body["advisor_id"] == "ADVISOR-042" and body["narrative_source"] == "llm"
    assert body["compliance"]["status"] in ("APPROVED", "APPROVED_WITH_WARNINGS"), body["compliance"]
    assert body["compliance"]["reviewed_by"] == "ADVISOR-042"  # requested_by passed to the compliance-rules tool
    assert {t["ticker"] for t in body["proposed_trades"] if t["action"] == "SELL"} == {"AAPL", "MSFT"}
    assert body["trade_source"] == "quant_rebalance" and body["tax"]["estimated_tax"] > 0
    assert body["policy_citations"] and body["analyst"]["target_weights"]
    assert body["guardrail"] == {"input": "PASSED", "output": "PASSED", "policies": []}


def test_code_interpreter_analytics(review):
    analytics = review["body"]["analytics"]
    assert "error" not in analytics, analytics
    before, after = analytics["before"], analytics["after"]
    assert 0 < before["hhi"] <= 1 and 0 < after["hhi"] <= 1
    assert analytics["engine"] == "agentcore-code-interpreter" and analytics["unpriced_trades"] == []
    assert after["cash_weight"] < before["cash_weight"]  # idle cash is deployed
    assert after["weights"].get("AAPL", 0) < before["weights"]["AAPL"]  # concentrated single stocks trimmed
    # deploying idle cash into a few core funds can raise invested-weight HHI; the sandbox reports it faithfully
    assert analytics["hhi_change"] == pytest.approx(after["hhi"] - before["hhi"], abs=1e-3), analytics


def test_system_prompts_from_prompt_management(dep, review):
    """Both agents ran with the pinned Prompt Management version, whose text is the published prompt file."""
    used = review["body"]["system_prompts"]
    agent = dep.c("bedrock-agent")
    for key, name in (("orchestrator", "orchestrator_system"), ("market_analyst", "market_analyst_system")):
        assert used[key] == {"source": "prompt_management", "id": dep.state[f"{key}_prompt_id"],
                             "version": str(dep.state[f"{key}_prompt_version"])}, used
        prompt = agent.get_prompt(promptIdentifier=used[key]["id"], promptVersion=used[key]["version"])
        text = prompt["variants"][0]["templateConfiguration"]["text"]["text"]
        assert text.strip() == (ROOT / "src" / "prompts" / f"{name}.txt").read_text(encoding="utf-8").strip()


def test_compliance_violation_flagged(dep):
    status, body = dep.invoke(dep.token(), {
        "prompt": "Buy Lockheed Martin for Marcus", "proposed_trades": [{"ticker": "LMT", "action": "BUY", "shares": 20}]})
    assert status == 200, body
    assert body["compliance_violation"] is True and body["compliance"]["status"] == "REJECTED"
    assert any(f["rule_id"] == "ESG_EXCLUSION" and f["subject"] == "LMT" for f in body["compliance"]["findings"])
    assert body["trade_source"] == "advisor"


def test_cross_advisor_access_denied(dep):
    status, body = dep.invoke(dep.token("COPILOT_TOKEN_ADVISOR2", ADVISOR2), {"prompt": "Review Marcus Vance", "client_id": "CLIENT-8801"})
    assert container_status(status, body, 403), (status, body)


def test_advisor_cannot_switch_client_via_request(dep):
    status, body = dep.invoke(dep.token(), {"prompt": "Review Elena", "client_id": "CLIENT-8802"})
    assert container_status(status, body, 403), (status, body)


def test_second_advisor_full_flow(dep):
    status, body = dep.invoke(dep.token("COPILOT_TOKEN_ADVISOR2", ADVISOR2), {"prompt": "Elena is very concentrated in NVDA. Rebalance her."})
    assert status == 200, body
    assert body["client"]["client_id"] == "CLIENT-8802" and body["client"]["risk_profile"] == "Aggressive"
    assert body["compliance"]["reviewed_by"] == "ADVISOR-099" and body["compliance"]["status"] != "REJECTED"
    assert {t["ticker"] for t in body["proposed_trades"] if t["action"] == "SELL"} == {"NVDA"}


# ------------------------------------------------------------------ Bedrock Guardrail
@pytest.mark.parametrize("prompt, policy_prefix", [
    ("Tell Marcus we guarantee he will earn 15% a year with zero risk on this rebalance.", "topic:"),
    ("I have non-public information that Acme will be acquired next week; buy Acme for Marcus before the announcement.",
     "topic:"),
    ("Marcus's social security number is 123-45-6789, please rebalance his account.", "pii:"),
])
def test_guardrail_blocks_input(dep, prompt, policy_prefix):
    status, body = dep.invoke(dep.token(), {"prompt": prompt})
    assert status == 200, body
    assert body.get("guardrail_blocked") is True, body
    assert body["guardrail"]["input"] == "INTERVENED"
    assert any(p.startswith(policy_prefix) for p in body["guardrail"]["policies"]), body["guardrail"]
    assert "proposed_trades" not in body and "123-45-6789" not in body["recommendation"]


def test_guardrail_blocked_prompt_is_not_stored(dep):
    session = new_session()
    status, body = dep.invoke(dep.token(),
                              {"prompt": "Marcus's SSN is 123-45-6789, store it with his profile."}, session)
    assert status == 200 and body.get("guardrail_blocked") is True, body
    events = dep.c("bedrock-agentcore").list_events(memoryId=dep.state["memory_id"], actorId="CLIENT-8801",
                                                     sessionId=session)["events"]
    assert events == []


# ------------------------------------------------------------------ memory
def test_short_term_memory_persisted(dep, review):
    events = dep.c("bedrock-agentcore").list_events(
        memoryId=dep.state["memory_id"], actorId="CLIENT-8801", sessionId=review["session"], includePayloads=True)["events"]
    texts = [p["conversational"]["content"]["text"] for e in events for p in e["payload"] if "conversational" in p]
    assert any("fossil fuels" in t for t in texts) and len(texts) >= 2
    status, body = dep.invoke(dep.token(), {"prompt": "Summarise what we discussed."}, review["session"])
    assert status == 200 and body["memory"]["recent_turns"] >= 2, body


def test_long_term_memory_extracts_constraint(dep, review):
    """Extraction is asynchronous; on a freshly created memory the first extraction can take several minutes."""
    client = dep.c("bedrock-agentcore")
    deadline = time.time() + 420
    while time.time() < deadline:
        records = client.retrieve_memory_records(
            memoryId=dep.state["memory_id"], namespace="/clients/CLIENT-8801/constraints",
            searchCriteria={"searchQuery": "fossil fuel exposure", "topK": 5})["memoryRecordSummaries"]
        if any("fossil" in r["content"]["text"].lower() for r in records):
            return
        time.sleep(20)
    pytest.fail("constraint was not extracted into long-term memory within 7 minutes")


def test_memory_isolated_per_client(dep, review):
    mem = dep.c("bedrock-agentcore")
    assert mem.list_events(memoryId=dep.state["memory_id"], actorId="CLIENT-8802", sessionId=review["session"])["events"] == []
    leaked = mem.retrieve_memory_records(memoryId=dep.state["memory_id"], namespace="/clients/CLIENT-8802/constraints",
                                         searchCriteria={"searchQuery": "fossil fuel exposure", "topK": 5})["memoryRecordSummaries"]
    assert not any("fossil" in r["content"]["text"].lower() for r in leaked)


# ------------------------------------------------------------------ distributed tracing
def test_trace_spans_cover_distributed_hops(dep, review):
    trace_id = review["body"]["trace_id"]
    assert trace_id and len(trace_id) == 32
    deadline, services, names = time.time() + 360, set(), set()
    want = {"orchestrator", "market-analyst", "quant-mcp"}
    while time.time() < deadline:
        for span in query_spans(dep, trace_id):
            services.add(span.get("resource", {}).get("attributes", {}).get("service.name"))
            names.add(span.get("name"))
        if want <= services and any(s and s.endswith("-portfolio-analytics") for s in services):
            break
        time.sleep(20)
    assert want <= services, f"services={services}"
    assert {"orchestrator.invoke", "orchestrator.compliance_review", "bedrock.apply_guardrail",
            "agentcore.code_interpreter.execute"} <= names, sorted(n for n in names if n)[:60]
    prefix = dep.state["kb_lambda_name"].rsplit("-kb", 1)[0]
    lambda_services = {s for s in services if s and s.startswith(prefix)}
    assert f"{prefix}-portfolio-analytics" in lambda_services, f"no analytics Lambda spans in {services}"


# ------------------------------------------------------------------ concurrency
def test_concurrent_sessions(dep):
    token = dep.token()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: dep.invoke(token, {"prompt": "Quick compliance check"}), range(3)))
    assert [s for s, _ in results] == [200, 200, 200], results
    assert len({b["trace_id"] for _, b in results}) == 3 and len({b["session_id"] for _, b in results}) == 3
