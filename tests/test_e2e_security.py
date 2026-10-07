"""Deployed negative-path, identity and posture checks (opt-in):

    E2E=1 COPILOT_TOKEN=<Entra token> [COPILOT_TOKEN_OTHER_APP=<token for another client app>] \
        python -m pytest tests/test_e2e_security.py -v

One identity contract everywhere (the same Entra discovery URL, audience and `azp` on the Gateway, the Orchestrator
runtime and the Market Analyst runtime). Tools behind the Gateway are identity-agnostic; the Gateway is their auth
boundary and the orchestrator enforces tenancy.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from src.common.mcp_client import GatewayClient
from tests.e2e_support import ROOT, Deployment, new_session, post_json, requires_deployment

pytestmark = requires_deployment

MCP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
A2A_SEND = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {"role": "user", "messageId": "m", "parts": [{"kind": "text", "text": "hi"}]}}}
REJECTED = (401, 403)


@pytest.fixture(scope="module")
def dep() -> Deployment:
    return Deployment()


BAD_TOKENS = ["missing", "garbage", "forged", "other_app"]


def bad_token(dep, kind: str) -> str | None:
    """Every way a caller can fail the contract. `other_app` = a genuine Entra token issued to another client app."""
    if kind == "other_app":
        return dep.token("COPILOT_TOKEN_OTHER_APP", expect=None)
    return {"missing": None, "garbage": "garbage", "forged": dep.forged_token()}[kind]


def gateway(dep, token) -> GatewayClient:
    gw = GatewayClient(dep.state["gateway_url"], token)
    gw.initialize()
    return gw


def _auth(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def marcus() -> dict:
    return next(c for c in json.loads((ROOT / "data" / "seed_portfolios.json").read_text()) if c["client_id"] == "CLIENT-8801")


# ------------------------------------------------------------------ identity contract
def test_authorizers_use_the_entra_contract(dep):
    with urllib.request.urlopen(dep.state["jwt_discovery_url"], timeout=30) as resp:
        assert json.loads(resp.read())["issuer"] == dep.state["jwt_issuer"]
    ac = dep.c("bedrock-agentcore-control")
    authorizers = {"gateway": ac.get_gateway(gatewayIdentifier=dep.state["gateway_id"])["authorizerConfiguration"]}
    for key in ("orchestrator", "market_analyst"):
        authorizers[key] = ac.get_agent_runtime(agentRuntimeId=dep.state[f"{key}_runtime_id"])["authorizerConfiguration"]
    for name, cfg in authorizers.items():
        auth = cfg["customJWTAuthorizer"]
        assert auth["discoveryUrl"] == dep.state["jwt_discovery_url"], name
        assert auth["allowedAudience"] == [dep.state["jwt_audience"]], name
        (claim,) = auth["customClaims"]
        assert claim["inboundTokenClaimName"] == "azp", name
        assert claim["authorizingClaimMatchValue"]["claimMatchValue"]["matchValueString"] == dep.state["jwt_azp"], name


# ------------------------------------------------------------------ inbound authentication on every entry point
@pytest.mark.parametrize("kind", BAD_TOKENS)
def test_gateway_rejects(dep, kind):
    status, _ = post_json(dep.state["gateway_url"], MCP_INIT, _auth(bad_token(dep, kind)))
    assert status in REJECTED, (kind, status)


@pytest.mark.parametrize("kind", BAD_TOKENS)
def test_orchestrator_rejects(dep, kind):
    status, _ = dep.invoke(bad_token(dep, kind), {"prompt": "review"})
    assert status in REJECTED, (kind, status)


@pytest.mark.parametrize("kind", BAD_TOKENS)
def test_market_analyst_rejects(dep, kind):
    url = f"{dep.state['market_analyst_runtime_url']}/invocations"
    status, _ = post_json(url, A2A_SEND, {**_auth(bad_token(dep, kind)), "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": new_session()})
    assert status in REJECTED, (kind, status)


def test_valid_token_accepted_everywhere(dep):
    token = dep.token()
    assert post_json(dep.state["gateway_url"], MCP_INIT, _auth(token))[0] == 200
    status, body = post_json(f"{dep.state['market_analyst_runtime_url']}/invocations", {
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"portfolio": marcus(), "question": "Concentration?"}}]}}},
        {**_auth(token), "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": new_session()}, timeout=120)
    assert status == 200 and "result" in body, body


def test_mcp_runtime_rejects_bearer_tokens(dep):
    """The Quant MCP runtime is IAM (SigV4) only - reachable through the Gateway role, never with a user token."""
    url = f"{dep.state['quant_mcp_runtime_url']}/invocations?qualifier=DEFAULT"
    status, _ = post_json(url, MCP_INIT, {**_auth(dep.token()),
                                          "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": new_session()})
    assert 400 <= status < 500


# ------------------------------------------------------------------ Gateway tools (identity-agnostic, reusable)
def test_gateway_lists_exactly_the_poc_tools(dep):
    names = set(gateway(dep, dep.token()).list_tools())
    # target tools are "<target>___<tool>"; the Gateway may add its own built-in search tool
    assert {n for n in names if "___" in n} == {
        "policy-kb___search_investment_policy", "portfolio-db___get_client_portfolio",
        "compliance-rules___review_compliance", "portfolio-analytics___analyze_portfolio",
        "quant-mcp___calculate_rebalance", "quant-mcp___estimate_tax_drag"}


def test_gateway_tools_take_plain_arguments(dep):
    gw = gateway(dep, dep.token())
    out = gw.call("portfolio-db___get_client_portfolio", {"client_id": "CLIENT-8801"})
    assert out["status"] == "success" and out["portfolio"]["advisor_id"] == "ADVISOR-042"


def test_compliance_rules_tool(dep):
    gw = gateway(dep, dep.token())
    out = gw.call("compliance-rules___review_compliance", {
        "portfolio": marcus(), "proposed_trades": [{"ticker": "LMT", "action": "BUY", "shares": 20}], "requested_by": "e2e"})
    assert out["status"] == "success" and out["review"]["status"] == "REJECTED" and out["review"]["reviewed_by"] == "e2e"
    bad = gw.call("compliance-rules___review_compliance", {"portfolio": {"holdings": "nope"}})
    assert bad.get("status") == "error"


def test_analytics_tool_runs_code_interpreter(dep):
    gw = gateway(dep, dep.token())
    out = gw.call("portfolio-analytics___analyze_portfolio",
                  {"portfolio": marcus(), "proposed_trades": [{"ticker": "AAPL", "action": "SELL", "shares": 50}]})
    assert out["status"] == "success", out
    analytics = out["analytics"]
    assert analytics["engine"] == "agentcore-code-interpreter"
    assert analytics["after"]["weights"]["AAPL"] < analytics["before"]["weights"]["AAPL"]
    bad = gw.call("portfolio-analytics___analyze_portfolio", {"portfolio": {"holdings": "nope"}})
    assert bad.get("status") == "error"


def test_kb_retrieves_relevant_esg_policy(dep):
    out = gateway(dep, dep.token()).call(
        "policy-kb___search_investment_policy", {"query": "Are thermal coal companies allowed for ESG mandate clients?", "top_k": 2})
    assert out["status"] == "success" and "Thermal Coal" in " ".join(p["text"] for p in out["passages"])


def test_quant_input_errors_are_tool_errors(dep):
    gw = gateway(dep, dep.token())
    out = gw.call("quant-mcp___calculate_rebalance", {"portfolio": marcus(), "target_weights": {"AAPL": 2.0}})
    assert out.get("status") == "error"
    out = gw.call("quant-mcp___calculate_rebalance", {"portfolio": marcus(), "target_weights": {"AAPL": 0.0}, "min_trade_value": 0})
    assert out["trades"] and all(t["shares"] > 0 for t in out["trades"])


def test_orchestrator_rejects_invalid_body(dep):
    status, body = dep.invoke(dep.token(), {"not_prompt": True})
    assert status == 400 or (status == 424 and "(400)" in json.dumps(body)), (status, body)


# ------------------------------------------------------------------ infrastructure posture
def test_runtimes_ready_in_vpc(dep):
    ac = dep.c("bedrock-agentcore-control")
    for key in ("orchestrator", "market_analyst", "quant_mcp"):
        rt = ac.get_agent_runtime(agentRuntimeId=dep.state[f"{key}_runtime_id"])
        net = rt["networkConfiguration"]
        assert rt["status"] == "READY" and net["networkMode"] == "VPC", key
        assert set(net["networkModeConfig"]["subnets"]) == set(dep.state["private_subnet_ids"]), key
        assert net["networkModeConfig"]["securityGroups"] == [dep.state["security_group_id"]], key


def test_code_interpreter_in_vpc(dep):
    ci = dep.c("bedrock-agentcore-control").get_code_interpreter(codeInterpreterId=dep.state["code_interpreter_id"])
    net = ci["networkConfiguration"]
    assert ci["status"] == "READY" and net["networkMode"] == "VPC"
    assert set(net["vpcConfig"]["subnets"]) == set(dep.state["private_subnet_ids"])
    assert net["vpcConfig"]["securityGroups"] == [dep.state["security_group_id"]]


def test_lambdas_in_vpc_with_tracing(dep):
    lam = dep.c("lambda")
    for key in ("kb", "ddb", "compliance", "analytics"):
        cfg = lam.get_function_configuration(FunctionName=dep.state[f"{key}_lambda_name"])
        assert cfg["VpcConfig"]["VpcId"] == dep.state["vpc_id"], key
        assert cfg["VpcConfig"]["SecurityGroupIds"] == [dep.state["security_group_id"]], key
        assert cfg["TracingConfig"]["Mode"] == "Active" and cfg["Architectures"] == ["arm64"] and cfg["Runtime"] == "python3.12"


def test_security_group_has_no_inbound_and_https_only_egress(dep):
    sg = dep.c("ec2").describe_security_groups(GroupIds=[dep.state["security_group_id"]])["SecurityGroups"][0]
    assert sg["VpcId"] == dep.state["vpc_id"] and sg["IpPermissions"] == []
    assert [(r["IpProtocol"], r.get("FromPort"), r.get("ToPort")) for r in sg["IpPermissionsEgress"]] == [("tcp", 443, 443)]


def test_guardrail_configuration(dep):
    g = dep.c("bedrock").get_guardrail(guardrailIdentifier=dep.state["guardrail_id"],
                                       guardrailVersion=dep.state["guardrail_version"])
    assert g["status"] == "READY"
    assert {t["name"] for t in g["topicPolicy"]["topics"]} == {"Guaranteed returns", "Insider trading"}
    assert any(f["type"] == "PROMPT_ATTACK" and f["inputStrength"] == "HIGH" for f in g["contentPolicy"]["filters"])
    assert any(e["type"] == "US_SOCIAL_SECURITY_NUMBER" and e["action"] == "BLOCK"
               for e in g["sensitiveInformationPolicy"]["piiEntities"])


def test_memory_resource_configuration(dep):
    mem = dep.c("bedrock-agentcore-control").get_memory(memoryId=dep.state["memory_id"])["memory"]
    assert mem["status"] == "ACTIVE"
    strategies = mem.get("strategies") or mem.get("memoryStrategies") or []
    assert any("/clients/{actorId}/constraints" in s.get("namespaces", []) for s in strategies), strategies


def test_log_deliveries_configured(dep):
    logs = dep.c("logs")
    prefix = dep.state["kb_lambda_name"].rsplit("-kb", 1)[0]
    sources = [s for page in logs.get_paginator("describe_delivery_sources").paginate()
               for s in page["deliverySources"] if s["name"].startswith(prefix)]
    by_type: dict[str, int] = {}
    for s in sources:
        by_type[s["logType"]] = by_type.get(s["logType"], 0) + 1
    assert by_type == {"APPLICATION_LOGS": 10, "USAGE_LOGS": 4, "TRACES": 10}, by_type


# ------------------------------------------------------------------ organisation tags (cloudformation/tags.json)
def test_key_resources_carry_organisation_tags(dep):
    required = json.loads((ROOT / "config" / f"{dep.state['environment']}.json").read_text(encoding="utf-8"))["tags"]
    s, region = dep.state, dep.region
    account = s["gateway_arn"].split(":")[4]
    arns = [s["gateway_arn"], s["memory_arn"], s["code_interpreter_arn"],
            *(s[f"{k}_runtime_arn"] for k in ("orchestrator", "market_analyst", "quant_mcp")),
            *(f"arn:aws:lambda:{region}:{account}:function:{s[f'{k}_lambda_name']}"
              for k in ("kb", "ddb", "compliance", "analytics")),
            f"arn:aws:dynamodb:{region}:{account}:table/{s['portfolio_table']}",
            f"arn:aws:bedrock:{region}:{account}:knowledge-base/{s['knowledge_base_id']}",
            f"arn:aws:bedrock:{region}:{account}:guardrail/{s['guardrail_id']}",
            *(f"arn:aws:bedrock:{region}:{account}:prompt/{s[f'{k}_prompt_id']}" for k in ("orchestrator", "market_analyst"))]
    found = {r["ResourceARN"]: {t["Key"]: t["Value"] for t in r["Tags"]}
             for r in dep.c("resourcegroupstaggingapi").get_resources(ResourceARNList=arns)["ResourceTagMappingList"]}
    missing = {a: {k: found.get(a, {}).get(k) for k in required if found.get(a, {}).get(k) != required[k]}
               for a in arns if any(found.get(a, {}).get(k) != v for k, v in required.items())}
    assert not missing, missing
