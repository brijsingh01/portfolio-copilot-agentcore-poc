"""Static checks for the CloudFormation templates, the deployer role, config/example.json and the deployment driver."""
import argparse
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = sorted((ROOT / "cloudformation" / "templates").glob("*.yaml"))
PSEUDO = {"AWS::AccountId", "AWS::Region", "AWS::Partition", "AWS::StackName", "AWS::StackId", "AWS::NoValue",
          "AWS::URLSuffix", "AWS::NotificationARNs"}


class Tag:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


class CfnLoader(yaml.SafeLoader):
    pass


def _construct(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return Tag(suffix, value)


CfnLoader.add_multi_constructor("!", _construct)


def load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=CfnLoader)


def tpl_path(name: str) -> Path:
    return ROOT / "cloudformation" / "templates" / name


def walk(node):
    yield node
    if isinstance(node, Tag):
        yield from walk(node.value)
    elif isinstance(node, dict):
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def sub_vars(template: str) -> set[str]:
    return {v for v in re.findall(r"\$\{([^}!]+)\}", template)}


def ref_name(node) -> str:
    """Name behind !Ref X / !GetAtt X.Y (for asserting wiring without resolving it)."""
    value = node.value if isinstance(node, Tag) else node
    return value if isinstance(value, str) else value[0]


@pytest.fixture(scope="module", params=TEMPLATES, ids=lambda p: p.name)
def tpl(request):
    return request.param, load(request.param)


# ------------------------------------------------------------------ every template
def test_templates_found():
    assert [p.name for p in TEMPLATES] == [
        "00-execution-role.yaml", "01-security-group.yaml", "02-artifacts.yaml", "03-data.yaml", "04-lambdas.yaml",
        "05-agent-runtimes.yaml", "06-gateway.yaml", "07-orchestrator.yaml", "08-observability.yaml", "09-telemetry.yaml"]


def test_no_yaml_anchors_or_aliases(tpl):
    path, _ = tpl
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"(:\s+|-\s+)&\w+", text) and not re.search(r"(:\s+|-\s+)\*\w+\s*$", text, re.M)


def test_structure_and_references(tpl):
    path, doc = tpl
    assert doc["AWSTemplateFormatVersion"] == "2010-09-09" and doc["Description"] and doc["Resources"]
    params, resources = set(doc.get("Parameters", {})), set(doc["Resources"])
    conditions = set(doc.get("Conditions", {}))
    known = params | resources | PSEUDO
    used = set()
    for node in walk(doc):
        if not isinstance(node, Tag):
            continue
        if node.tag == "Ref":
            assert node.value in known, f"{path.name}: !Ref {node.value}"
            used.add(node.value)
        elif node.tag == "GetAtt":
            target = node.value.split(".")[0] if isinstance(node.value, str) else node.value[0]
            assert target in resources, f"{path.name}: !GetAtt {node.value}"
        elif node.tag == "Sub":
            text = node.value if isinstance(node.value, str) else node.value[0]
            local = set(node.value[1]) if isinstance(node.value, list) else set()
            for var in sub_vars(text):
                base = var.split(".")[0]
                assert base in known | local or var in PSEUDO, f"{path.name}: ${{{var}}} unresolved"
                used.add(base)
        elif node.tag == "If":
            assert node.value[0] in conditions
    assert not params - used, f"{path.name}: unused parameters {params - used}"


def test_only_expected_resource_types(tpl):
    path, doc = tpl
    allowed = ("AWS::IAM::Role", "AWS::BedrockAgentCore::", "AWS::DynamoDB::Table", "AWS::S3::", "AWS::S3Vectors::",
               "AWS::Bedrock::", "AWS::ECR::Repository", "AWS::Lambda::Function", "AWS::Logs::",
               "AWS::XRay::TransactionSearchConfig", "AWS::EC2::SecurityGroup")
    for name, res in doc["Resources"].items():
        assert res["Type"].startswith(allowed), f"{path.name}:{name} {res['Type']}"


def test_no_network_or_identity_provider_is_created(tpl):
    """Users bring their VPC/subnets and Microsoft Entra ID: only a security group is created."""
    path, doc = tpl
    for name, res in doc["Resources"].items():
        assert not res["Type"].startswith(("AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::NatGateway", "AWS::EC2::Internet",
                                           "AWS::EC2::Route", "AWS::EC2::EIP", "AWS::Cognito::")), f"{path.name}:{name}"
        assert "OAuth2CredentialProvider" not in res["Type"]
    text = path.read_text(encoding="utf-8").lower()
    assert "cognito" not in text and "auth0" not in text and "_obo" not in text


def test_project_roles_use_prefix(tpl):
    path, doc = tpl
    for name, res in doc["Resources"].items():
        if res["Type"] == "AWS::IAM::Role" and path.name != "00-execution-role.yaml":
            assert "${Prefix}" in res["Properties"]["RoleName"].value, name


# ------------------------------------------------------------------ network: existing VPC + one security group
def test_security_group_is_egress_https_only():
    doc = load(tpl_path("01-security-group.yaml"))
    (sg,) = doc["Resources"].values()
    props = sg["Properties"]
    assert ref_name(props["VpcId"]) == "VpcId" and "SecurityGroupIngress" not in props
    assert props["SecurityGroupEgress"] == [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0",
                                             "Description": "HTTPS to AWS APIs and Entra ID"}]


def test_every_workload_runs_in_the_private_subnets_with_the_one_security_group():
    found = []
    for name in ("04-lambdas.yaml", "05-agent-runtimes.yaml", "07-orchestrator.yaml"):
        for logical, res in load(tpl_path(name))["Resources"].items():
            props = res.get("Properties", {})
            if res["Type"] in ("AWS::BedrockAgentCore::Runtime", "AWS::BedrockAgentCore::CodeInterpreterCustom"):
                net = props["NetworkConfiguration"]
                vpc = net.get("NetworkModeConfig") or net["VpcConfig"]
                assert net["NetworkMode"] == "VPC", logical
            elif res["Type"] == "AWS::Lambda::Function":
                vpc = {"Subnets": props["VpcConfig"]["SubnetIds"], "SecurityGroups": props["VpcConfig"]["SecurityGroupIds"]}
            else:
                continue
            assert ref_name(vpc["Subnets"]) == "PrivateSubnetIds", logical
            assert [ref_name(s) for s in vpc["SecurityGroups"]] == ["SecurityGroupId"], logical
            found.append(res["Type"])
    assert found.count("AWS::BedrockAgentCore::Runtime") == 3 and found.count("AWS::Lambda::Function") == 4
    assert found.count("AWS::BedrockAgentCore::CodeInterpreterCustom") == 1


# ------------------------------------------------------------------ identity: one Entra contract everywhere
def test_gateway_orchestrator_and_analyst_share_the_entra_contract():
    auths = {}
    for path in TEMPLATES:
        for name, res in load(path)["Resources"].items():
            jwt = res.get("Properties", {}).get("AuthorizerConfiguration", {}).get("CustomJWTAuthorizer")
            if jwt is not None:
                auths[f"{path.name}:{name}"] = jwt
    assert set(auths) == {"05-agent-runtimes.yaml:MarketAnalystRuntime", "06-gateway.yaml:Gateway",
                          "07-orchestrator.yaml:OrchestratorRuntime"}
    for where, jwt in auths.items():
        assert ref_name(jwt["DiscoveryUrl"]) == "DiscoveryUrl", where
        assert [ref_name(a) for a in jwt["AllowedAudience"]] == ["JwtAudience"], where
        assert "AllowedClients" not in jwt, where
        (claim,) = jwt["CustomClaims"]
        assert claim["InboundTokenClaimName"] == "azp" and claim["InboundTokenClaimValueType"] == "STRING", where
        assert claim["AuthorizingClaimMatchValue"]["ClaimMatchOperator"] == "EQUALS", where
        assert ref_name(claim["AuthorizingClaimMatchValue"]["ClaimMatchValue"]["MatchValueString"]) == "JwtAzp", where


@pytest.mark.parametrize("template,runtime", [("05-agent-runtimes.yaml", "MarketAnalystRuntime"),
                                              ("07-orchestrator.yaml", "OrchestratorRuntime")])
def test_agents_validate_tokens_with_the_same_contract(template, runtime):
    env = load(tpl_path(template))["Resources"][runtime]["Properties"]["EnvironmentVariables"]
    assert {k: ref_name(v) for k, v in env.items() if k.startswith("JWT_")} == {
        "JWT_ISSUER": "JwtIssuer", "JWT_DISCOVERY_URL": "DiscoveryUrl", "JWT_AUDIENCES": "JwtAudience",
        "JWT_ALLOWED_CLIENT_IDS": "JwtAzp", "JWT_ADVISOR_CLAIM": "AdvisorIdClaim", "JWT_CLIENT_CLAIM": "ClientIdClaim"}


def test_quant_mcp_runtime_is_iam_only():
    props = load(tpl_path("05-agent-runtimes.yaml"))["Resources"]["QuantMcpRuntime"]["Properties"]
    assert "AuthorizerConfiguration" not in props and props["ProtocolConfiguration"] == "MCP"
    assert not any(k.startswith("JWT_") for k in props["EnvironmentVariables"])


# ------------------------------------------------------------------ gateway: tools only
def _gateway():
    return load(tpl_path("06-gateway.yaml"))["Resources"]


def _lambda_tools():
    return {f'{r["Properties"]["Name"]}___{t["Name"]}': t
            for r in _gateway().values() if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
            for t in r["Properties"]["TargetConfiguration"]["Mcp"].get("Lambda", {}).get("ToolSchema", {}).get("InlinePayload", [])}


def test_gateway_targets_are_tools_only():
    targets = {r["Properties"]["Name"]: next(iter(r["Properties"]["TargetConfiguration"]["Mcp"]))
               for r in _gateway().values() if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget"}
    assert targets == {"policy-kb": "Lambda", "portfolio-db": "Lambda", "compliance-rules": "Lambda",
                       "portfolio-analytics": "Lambda", "quant-mcp": "McpServer"}


def test_gateway_targets_use_gateway_role_and_are_serialized():
    res = _gateway()
    order = ["PolicyKbTarget", "PortfolioDbTarget", "ComplianceTarget", "AnalyticsTarget", "QuantMcpTarget"]
    assert "DependsOn" not in res[order[0]]
    for prev, cur in zip(order, order[1:]):
        assert res[cur]["DependsOn"] == prev
    for name in order:
        (cred,) = res[name]["Properties"]["CredentialProviderConfigurations"]
        assert cred["CredentialProviderType"] == "GATEWAY_IAM_ROLE"


def test_tool_schemas_match_the_orchestrator_and_the_code():
    from src.orchestrator import agent
    tools = _lambda_tools()
    assert set(tools) == {agent.TOOL_PORTFOLIO, agent.TOOL_POLICY, agent.TOOL_COMPLIANCE, agent.TOOL_ANALYTICS}
    from src.lambdas.compliance_rules import handler as c
    from src.lambdas.ddb_portfolio import handler as d
    from src.lambdas.kb_retrieval import handler as k
    from src.lambdas.portfolio_analytics import handler as a
    for tool in tools.values():
        props = tool["InputSchema"]["Properties"]
        assert "_meta" in props and "_obo" not in props, tool["Name"]
    assert tools[agent.TOOL_COMPLIANCE]["InputSchema"]["Required"] == ["portfolio"]
    assert {c.review_compliance.__name__, d.get_client_portfolio.__name__, k.search_investment_policy.__name__,
            a.analyze_portfolio.__name__} == {t.split("___")[1] for t in tools}


# ------------------------------------------------------------------ guardrail, code interpreter, prompts
def _orchestrator():
    return load(tpl_path("07-orchestrator.yaml"))["Resources"]


def test_guardrail_policies():
    g = _orchestrator()["AdvisorGuardrail"]["Properties"]
    assert {t["Name"] for t in g["TopicPolicyConfig"]["TopicsConfig"]} == {"Guaranteed returns", "Insider trading"}
    filters = {f["Type"]: (f["InputStrength"], f["OutputStrength"]) for f in g["ContentPolicyConfig"]["FiltersConfig"]}
    assert filters["PROMPT_ATTACK"] == ("HIGH", "NONE") and set(filters) >= {"HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"}
    pii = {e["Type"]: e["Action"] for e in g["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert pii.get("US_SOCIAL_SECURITY_NUMBER") == "BLOCK"
    env = _orchestrator()["OrchestratorRuntime"]["Properties"]["EnvironmentVariables"]
    assert ref_name(env["BEDROCK_GUARDRAIL_ID"]).startswith("AdvisorGuardrail")
    assert ref_name(env["BEDROCK_GUARDRAIL_VERSION"]).startswith("AdvisorGuardrailVersion")


def test_code_interpreter_is_reached_only_through_the_analytics_tool():
    res = load(tpl_path("04-lambdas.yaml"))["Resources"]
    fn = res["AnalyticsFunction"]["Properties"]
    assert ref_name(fn["Environment"]["Variables"]["CODE_INTERPRETER_ID"]) == "CodeInterpreter.CodeInterpreterId"
    stmt = next(s for s in res["AnalyticsRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
                if s["Sid"] == "CodeInterpreterSessions")
    assert ref_name(stmt["Resource"]) == "CodeInterpreter.CodeInterpreterArn"
    assert "CodeInterpreterSession" not in tpl_path("07-orchestrator.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("template,label,runtime", [("05-agent-runtimes.yaml", "MarketAnalyst", "MarketAnalystRuntime"),
                                                    ("07-orchestrator.yaml", "Orchestrator", "OrchestratorRuntime")])
def test_system_prompts_come_from_pinned_prompt_versions(template, label, runtime):
    res = load(tpl_path(template))["Resources"]
    (variant,) = res[f"{label}Prompt"]["Properties"]["Variants"]
    assert ref_name(variant["TemplateConfiguration"]["Text"]["Text"]) == f"{label}SystemPrompt"
    assert f"{label}PromptRevision" in res[f"{label}PromptVersion"]["Properties"]["Description"].value
    env = res[runtime]["Properties"]["EnvironmentVariables"]
    assert ref_name(env["SYSTEM_PROMPT_ID"]) == f"{label}Prompt.Id"
    assert ref_name(env["SYSTEM_PROMPT_VERSION"]) == f"{label}PromptVersion.Version"


def test_prompt_text_lives_only_in_prompt_files():
    from src.market_analyst import app as analyst
    from src.orchestrator import agent
    assert not hasattr(agent, "SYSTEM_PROMPT") and not hasattr(analyst, "SYSTEM_PROMPT")
    for name in (agent.PROMPT_NAME, analyst.PROMPT_NAME):
        assert (ROOT / "src" / "prompts" / f"{name}.txt").exists()


# ------------------------------------------------------------------ telemetry
def _deliveries_by_source():
    res = load(tpl_path("09-telemetry.yaml"))["Resources"]
    out = {}
    for r in res.values():
        if r["Type"] == "AWS::Logs::Delivery":
            src = res[r["Properties"]["DeliverySourceName"].value]["Properties"]
            dest = r["Properties"]["DeliveryDestinationArn"].value.split(".")[0]
            out.setdefault(src["ResourceArn"].value, {})[src["LogType"]] = res[dest]["Properties"]["DeliveryDestinationType"]
    return out


def test_telemetry_covers_every_agentcore_resource():
    full = {"APPLICATION_LOGS": "CWL", "USAGE_LOGS": "CWL", "TRACES": "XRAY"}
    logs_traces = {"APPLICATION_LOGS": "CWL", "TRACES": "XRAY"}
    labels = ("QuantMcp", "MarketAnalyst", "Orchestrator")
    assert _deliveries_by_source() == {**{f"{lb}RuntimeArn": full for lb in labels},
                                       **{f"{lb}WorkloadIdentityArn": logs_traces for lb in labels + ("Gateway",)},
                                       "CodeInterpreterArn": full, "GatewayArn": logs_traces, "MemoryArn": logs_traces}


def test_telemetry_deliveries_are_serialized():
    res = load(tpl_path("09-telemetry.yaml"))["Resources"]
    deliveries = [n for n, r in res.items() if r["Type"] == "AWS::Logs::Delivery"]
    assert len(deliveries) == 24 and "DependsOn" not in res[deliveries[0]]
    for prev, cur in zip(deliveries, deliveries[1:]):
        assert res[cur]["DependsOn"] == prev


# ------------------------------------------------------------------ IAM
def test_execution_role_permissions():
    text = tpl_path("00-execution-role.yaml").read_text(encoding="utf-8")
    for action in ("logs:CreateDelivery", "xray:PutResourcePolicy", "bedrock:CreateGuardrail", "bedrock:CreatePrompt",
                   "ec2:CreateSecurityGroup", "iam:CreateServiceLinkedRole"):
        assert action in text, action
    for absent in ("ec2:CreateVpc", "ec2:CreateNatGateway", "ec2:CreateSubnet", "cognito-idp", "secretsmanager"):
        assert absent not in text, absent


def test_deployer_role_is_least_privilege():
    doc = load(ROOT / "cloudformation/bootstrap/ci-deployer-role.yaml")
    stmts = doc["Resources"]["CiDeployerRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions = [a for s in stmts for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
    assert not any(a == "*" or a.endswith(":*") for a in actions)
    assert not any(a.startswith("secretsmanager:") for a in actions)
    pass_role = next(s for s in stmts if s["Sid"] == "PassExecutionRole")
    assert pass_role["Condition"] == {"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}}


# ------------------------------------------------------------------ config + driver
def test_example_config_has_every_field_as_a_placeholder():
    cfg = json.loads((ROOT / "config" / "example.json").read_text(encoding="utf-8"))
    assert set(cfg) == {"region", "network", "entra", "tags"}
    assert set(cfg["entra"]) == {"discovery_url", "audience", "allowed_azp", "advisor_id_claim", "client_id_claim"}
    assert cfg["tags"] == {"Application": "A00000", "Environment": "C0", "Provisioned-By": "CICD"}
    from cloudformation import deploy
    with pytest.raises(SystemExit, match="placeholder"):
        deploy.load_config("example")  # a fresh copy cannot be deployed by accident


@pytest.fixture
def driver(monkeypatch, tmp_path):
    from cloudformation import deploy

    class FakeSession:
        region_name = "us-east-1"

        def __init__(self, **_):
            pass

        def client(self, service, **_):
            return type("C", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})()

    monkeypatch.setattr(deploy.boto3, "Session", FakeSession)
    monkeypatch.setattr(deploy, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(deploy, "CONFIG_DIR", tmp_path)
    cfg = {"region": "us-east-1",
           "network": {"vpc_id": "vpc-0abc", "private_subnet_ids": ["subnet-a1", "subnet-b2"]},
           "entra": {"discovery_url": "https://login.microsoftonline.com/t/v2.0/.well-known/openid-configuration",
                     "audience": "api-app", "allowed_azp": "client-app", "advisor_id_claim": "adv", "client_id_claim": "cli"},
           "tags": {"Application": "A00000", "Environment": "C0", "Provisioned-By": "CICD"}}
    (tmp_path / "poc.json").write_text(json.dumps(cfg))
    return deploy, deploy.Env("poc", deploy.load_config("poc"))


def test_driver_supplies_every_required_parameter():
    driver_text = (ROOT / "cloudformation/deploy.py").read_text(encoding="utf-8")
    for path in TEMPLATES:
        for name, spec in load(path).get("Parameters", {}).items():
            if "Default" not in spec:
                templated = any(name.startswith(lb) and f'"{lb}"' in driver_text for lb in ("QuantMcp", "MarketAnalyst", "Orchestrator"))
                assert f'"{name}"' in driver_text or templated, f"{path.name}: driver never passes {name}"


def test_layers_cover_every_template_in_order(driver):
    deploy, env = driver
    assert [lyr.template for lyr in deploy.LAYERS.values()] == [p.name for p in TEMPLATES]
    assert deploy.LAYERS["security-group"].stack(env) == "portfolio-copilot-poc-security-group"
    assert deploy.LAYERS["execution-role"].stack(env) == "portfolio-copilot-cfn-execution-role"
    assert not deploy.LAYERS["execution-role"].uses_exec_role and deploy.LAYERS["gateway"].uses_exec_role
    assert deploy._p_security_group(env) == {"Prefix": "portfolio-copilot-poc", "VpcId": "vpc-0abc"}


def test_stacks_carry_the_config_tags(driver):
    _, env = driver
    assert {"Key": "Provisioned-By", "Value": "CICD"} in env.tag_list()
    assert {"Key": "project", "Value": "portfolio-copilot-poc"} in env.tag_list()
    driver_text = (ROOT / "cloudformation/deploy.py").read_text(encoding="utf-8")
    assert "Tags=env.tag_list()" in driver_text


def test_identity_reads_the_issuer_from_the_discovery_document(driver, monkeypatch):
    deploy, env = driver
    monkeypatch.setattr(deploy, "fetch_json", lambda url, timeout=15: {"issuer": "https://login.microsoftonline.com/t/v2.0"})
    assert deploy.identity(env) == {"JwtIssuer": "https://login.microsoftonline.com/t/v2.0",
                                    "DiscoveryUrl": env.config["entra"]["discovery_url"], "JwtAudience": "api-app",
                                    "JwtAzp": "client-app", "AdvisorIdClaim": "adv", "ClientIdClaim": "cli"}

    def unreachable(url, timeout=15):
        raise deploy.urllib.error.URLError("no route")

    monkeypatch.setattr(deploy, "fetch_json", unreachable)
    with pytest.raises(SystemExit, match="not a reachable OIDC discovery"):
        deploy.identity(env)


def test_check_rejects_unsupported_availability_zones(driver, monkeypatch):
    deploy, env = driver
    subnets = [{"SubnetId": "subnet-a1", "VpcId": "vpc-0abc", "AvailabilityZoneId": "use1-az1"},
               {"SubnetId": "subnet-b2", "VpcId": "vpc-0abc", "AvailabilityZoneId": "use1-az3"}]
    monkeypatch.setattr(env, "c", lambda service: type("C", (), {"describe_subnets": lambda self, **k: {"Subnets": subnets}})())
    with pytest.raises(SystemExit, match="use1-az3"):
        deploy.cmd_check(env, argparse.Namespace())
    subnets[1]["VpcId"] = "vpc-other"
    with pytest.raises(SystemExit, match="not in vpc-0abc"):
        deploy.cmd_check(env, argparse.Namespace())


def test_missing_manifest_explains_the_step_to_run(driver):
    deploy, env = driver
    with pytest.raises(SystemExit, match="python -m cloudformation.deploy images --env poc"):
        deploy.images(env)


def test_lambda_zip_is_deterministic():
    from cloudformation import deploy
    a, b = deploy.lambda_zip("ddb_portfolio", None), deploy.lambda_zip("ddb_portfolio", None)
    assert a == b
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(a)).namelist()
    assert "src/lambdas/ddb_portfolio/handler.py" in names and "src/common/lambda_target.py" in names
    assert not any("/idp/" in n or "a2a_server" in n for n in names)


def test_readme_and_guide_cover_the_essentials():
    text = ((ROOT / "README.md").read_text(encoding="utf-8") + (ROOT / "docs" / "DEPLOYMENT_GUIDE.md").read_text(encoding="utf-8")).lower()
    for topic in ("entra", "discovery", "audience", "azp", "config/example.json", "existing vpc", "security group",
                  "guardrail", "code interpreter", "prompt management", "build_and_push_images", "get_entra_token", "params"):
        assert topic in text, topic
