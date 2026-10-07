"""Deployment driver for the Portfolio Advisory Copilot POC.

Reads config/<env>.json (your existing VPC + private subnets, Microsoft Entra ID settings, tags), deploys
templates/00..09 in order and performs the steps CloudFormation cannot express: building/pushing the images,
packaging the Lambda code, seeding DynamoDB + ingesting the Knowledge Base, looking up workload identity ARNs.
Every step is also a command of its own, so the same code serves an all-in-one run, a pipeline (one command
per stage) and a manual console/CLI deployment (`params` writes each template's parameter file):

    python -m cloudformation.deploy check  --env poc                  # validate config/poc.json against AWS + Entra
    python -m cloudformation.deploy up     --env poc                  # everything (builds and pushes the images)
    python -m cloudformation.deploy up     --env poc --image-tag v1   # images you pushed yourself
    python -m cloudformation.deploy stack  gateway --env poc          # one layer
    python -m cloudformation.deploy params gateway --env poc          # parameters for a manual deployment
    python -m cloudformation.deploy smoke  --env poc                  # needs COPILOT_TOKEN (an Entra access token)
    python -m cloudformation.deploy down   --env poc

Outputs of local steps are recorded in cloudformation/.state/<env>.<step>.json; cloudformation/.state/<env>.json
is read by `smoke` and the end-to-end tests.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, WaiterError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "cloudformation" / "templates"
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "cloudformation" / ".state"
BUILD_DIR = ROOT / "cloudformation" / ".build"
PROJECT = "portfolio-copilot"
EXEC_ROLE_STACK = f"{PROJECT}-cfn-execution-role"
EXEC_ROLE_NAME = f"{PROJECT}-cfn-execution"
ADOT_LAYER = "arn:aws:lambda:{region}:615299751070:layer:AWSOpenTelemetryDistroPython"
REQUIRED_TAGS = ("Application", "Environment", "Provisioned-By")
# AgentCore Runtime VPC mode supports these Availability Zone IDs (subnets elsewhere are rejected)
AGENTCORE_AZ_IDS = {"us-east-1": {"use1-az1", "use1-az2", "use1-az4"}}

# image key -> (repository suffix, python module, port)
IMAGES = {
    "quant_mcp": ("quant-mcp", "src.quant_mcp.app", 8000),
    "market_analyst": ("market-analyst", "src.market_analyst.app", 9000),
    "orchestrator": ("orchestrator", "src.orchestrator.agent", 8080),
}
# lambda key -> source package (handlers are src.lambdas.<dir>.handler.handler)
LAMBDAS = {"kb": "kb_retrieval", "ddb": "ddb_portfolio", "compliance": "compliance_rules", "analytics": "portfolio_analytics"}
# the Lambda runtime's bundled boto3 may predate the bedrock-agentcore data plane (Code Interpreter sessions)
LAMBDA_REQUIREMENTS = {"analytics": ["boto3==1.43.108"]}
# a deployment takes ~35 minutes: retry throttling and transient connection/DNS errors instead of aborting
AWS_RETRIES = Config(retries={"mode": "standard", "max_attempts": 10}, connect_timeout=15, read_timeout=120)


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


# =============================================================================== configuration
def load_config(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"{_rel(path)} not found - copy config/example.json to it and fill in your values")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    net, entra, tags = cfg.get("network", {}), cfg.get("entra", {}), cfg.get("tags", {})
    if not str(net.get("vpc_id", "")).startswith("vpc-"):
        problems.append("network.vpc_id")
    subnets = net.get("private_subnet_ids") or []
    if len(subnets) < 2 or not all(str(s).startswith("subnet-") for s in subnets):
        problems.append("network.private_subnet_ids (at least 2)")
    for key in ("discovery_url", "audience", "allowed_azp", "advisor_id_claim", "client_id_claim"):
        if not entra.get(key) or "<" in str(entra.get(key)):
            problems.append(f"entra.{key}")
    for key in REQUIRED_TAGS:
        if not tags.get(key):
            problems.append(f"tags.{key}")
    if problems:
        raise SystemExit(f"{_rel(path)}: missing or placeholder values: {', '.join(problems)}")
    return cfg


class Env:
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name, self.config = name, config
        self.prefix = f"{PROJECT}-{name}"
        self.runtime_prefix = f"{PROJECT.replace('-', '_')}_{name}"
        self.session = boto3.Session(region_name=config.get("region"))
        self.region = self.session.region_name or "us-east-1"
        self.account = self.session.client("sts").get_caller_identity()["Account"]
        self.cfn = self.session.client("cloudformation", config=AWS_RETRIES)
        self.state_file = STATE_DIR / f"{name}.json"
        self.tags = {**config["tags"], "project": self.prefix}

    def c(self, service: str) -> Any:
        return self.session.client(service, region_name=self.region, config=AWS_RETRIES)

    def stack(self, layer: str) -> str:
        return f"{self.prefix}-{layer}"

    def tag_list(self) -> list[dict[str, str]]:
        return [{"Key": k, "Value": v} for k, v in self.tags.items()]

    @property
    def registry(self) -> str:
        return f"{self.account}.dkr.ecr.{self.region}.amazonaws.com"

    @property
    def subnets(self) -> list[str]:
        return list(self.config["network"]["private_subnet_ids"])


def fetch_json(url: str, timeout: int = 15) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def identity(env: Env) -> dict[str, str]:
    """The identity contract shared by the Gateway, Orchestrator and Market Analyst authorizers."""
    entra = env.config["entra"]
    try:
        issuer = fetch_json(entra["discovery_url"])["issuer"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
        raise SystemExit(f"entra.discovery_url {entra['discovery_url']} is not a reachable OIDC discovery "
                         f"document: {exc}") from exc
    return {"JwtIssuer": issuer, "DiscoveryUrl": entra["discovery_url"], "JwtAudience": entra["audience"],
            "JwtAzp": entra["allowed_azp"], "AdvisorIdClaim": entra["advisor_id_claim"],
            "ClientIdClaim": entra["client_id_claim"]}


# =============================================================================== stack operations
def _failure_events(env: Env, stack: str) -> list[str]:
    try:
        events = env.cfn.describe_stack_events(StackName=stack)["StackEvents"][:40]
    except ClientError:
        return []
    return [f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason')}" for e in events
            if (e["ResourceStatus"].endswith("FAILED") or e["ResourceStatus"].startswith("ROLLBACK_IN_PROGRESS")
                or e["ResourceStatus"] == "UPDATE_ROLLBACK_IN_PROGRESS") and e.get("ResourceStatusReason")]


def _settled_status(env: Env, stack: str) -> str | None:
    """Current stack status, waiting out an operation still in progress (e.g. after an interrupted run)."""
    while True:
        try:
            status = env.cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        except ClientError:
            return None
        if not status.endswith("_IN_PROGRESS"):
            return status
        log(f"{stack} is {status}; waiting for it to finish")
        time.sleep(15)


def deploy_stack(env: Env, stack: str, template: str, params: dict[str, Any], role_arn: str | None) -> dict[str, str]:
    body = (TEMPLATES / template).read_text(encoding="utf-8")
    parameters = [{"ParameterKey": k, "ParameterValue": ",".join(v) if isinstance(v, list) else str(v)}
                  for k, v in params.items()]
    kwargs: dict[str, Any] = dict(StackName=stack, TemplateBody=body, Parameters=parameters,
                                  Capabilities=["CAPABILITY_NAMED_IAM"], Tags=env.tag_list())
    if role_arn:
        kwargs["RoleARN"] = role_arn
    status = _settled_status(env, stack)
    if status == "ROLLBACK_COMPLETE":  # failed first create: must be deleted before re-creating
        env.cfn.delete_stack(StackName=stack, **({"RoleARN": role_arn} if role_arn else {}))
        env.cfn.get_waiter("stack_delete_complete").wait(StackName=stack)
        status = None
    try:
        if status is None:
            log(f"creating {stack} ({template})")
            env.cfn.create_stack(OnFailure="ROLLBACK", **kwargs)
            waiter = "stack_create_complete"
        else:
            log(f"updating {stack} ({template})")
            env.cfn.update_stack(**kwargs)
            waiter = "stack_update_complete"
        env.cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 240})
    except ClientError as exc:
        if "No updates are to be performed" not in str(exc):
            raise
        log(f"{stack} unchanged")
    except WaiterError as exc:
        raise SystemExit(f"{stack} failed:\n  " + "\n  ".join(_failure_events(env, stack))) from exc
    return {o["OutputKey"]: o["OutputValue"] for o in env.cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])}


def delete_stack(env: Env, stack: str, role_arn: str | None) -> bool:
    try:
        env.cfn.describe_stacks(StackName=stack)
    except ClientError:
        return True
    log(f"deleting {stack}")
    env.cfn.delete_stack(StackName=stack, **({"RoleARN": role_arn} if role_arn else {}))
    try:
        env.cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 240})
        return True
    except WaiterError:
        log(f"{stack} delete failed:\n  " + "\n  ".join(_failure_events(env, stack)))
        return False


# =============================================================================== out-of-band steps (CloudFormation gaps)
def _content_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(_rel(path).encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def build_images(env: Env) -> dict[str, str]:
    """Build + push linux/arm64 images tagged with a content hash (unchanged sources are not rebuilt)."""
    ecr = env.c("ecr")
    token = base64.b64decode(ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]).decode()
    subprocess.run(["docker", "login", "--username", "AWS", "--password-stdin", env.registry],
                   input=token.split(":", 1)[1], text=True, check=True, capture_output=True)
    sources = [*(ROOT / "src").rglob("*.py"), *(ROOT / "src" / "prompts").glob("*.txt"), ROOT / "requirements.txt",
               ROOT / "docker" / "Dockerfile", ROOT / "data" / "market_reference.json"]
    tag = _content_hash([p for p in sources if "__pycache__" not in p.parts])
    uris = {}
    for key, (repo, module, port) in IMAGES.items():
        repository = f"{env.prefix}/{repo}"
        uri = f"{env.registry}/{repository}:{tag}"
        uris[key] = uri
        try:
            ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])
            log(f"image exists {uri}")
            continue
        except ClientError:
            pass
        log(f"building {uri}")
        subprocess.run(["docker", "buildx", "build", "--platform", "linux/arm64", "--provenance=false",
                        "-f", str(ROOT / "docker" / "Dockerfile"), "--build-arg", f"APP_MODULE={module}",
                        "--build-arg", f"APP_PORT={port}", "--build-arg", f"SERVICE_NAME={repo}",
                        "-t", uri, "--push", str(ROOT)], check=True)
    return uris


def _vendored_deps(requirements: list[str]) -> Path | None:
    if not requirements:
        return None
    key = hashlib.sha256("|".join(requirements).encode()).hexdigest()[:12]
    deps = BUILD_DIR / f"lambda-deps-{key}"
    if not deps.exists():
        log(f"vendoring lambda deps {requirements} for arm64")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(deps), "--platform",
                        "manylinux2014_aarch64", "--implementation", "cp", "--python-version", "3.12",
                        "--only-binary=:all:", "--upgrade", *requirements], check=True)
    return deps


def lambda_zip(package: str, deps: Path | None) -> bytes:
    """Deterministic zip: vendored wheels (if any) + src/common + the handler package + market reference data."""
    files: dict[str, Path] = {}
    if deps is not None:
        files = {p.relative_to(deps).as_posix(): p for p in deps.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    for path in [ROOT / "src" / "__init__.py", ROOT / "src" / "lambdas" / "__init__.py",
                 *(ROOT / "src" / "common").glob("*.py"), *(ROOT / "src" / "lambdas" / package).glob("*.py"),
                 ROOT / "data" / "market_reference.json"]:
        files[_rel(path)] = path
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name].read_bytes())
    return buf.getvalue()


def package_lambdas(env: Env, bucket: str) -> dict[str, str]:
    s3, keys = env.c("s3"), {}
    for key, package in LAMBDAS.items():
        code = lambda_zip(package, _vendored_deps(LAMBDA_REQUIREMENTS.get(key, [])))
        s3_key = f"lambda/{key}/{hashlib.sha256(code).hexdigest()[:16]}.zip"
        s3.put_object(Bucket=bucket, Key=s3_key, Body=code)
        keys[key] = s3_key
        log(f"uploaded s3://{bucket}/{s3_key}")
    return keys


def system_prompt_params(label: str, name: str) -> dict[str, str]:
    """Publish src/prompts/<name>.txt to Bedrock Prompt Management; the hash cuts a new version only on change."""
    text = (ROOT / "src" / "prompts" / f"{name}.txt").read_text(encoding="utf-8").strip()
    return {f"{label}SystemPrompt": text, f"{label}PromptRevision": hashlib.sha256(text.encode()).hexdigest()[:16]}


def seed_and_ingest(env: Env, data: dict[str, str]) -> None:
    table = env.session.resource("dynamodb", region_name=env.region).Table(data["PortfolioTableName"])
    items = json.loads((ROOT / "data" / "seed_portfolios.json").read_text(), parse_float=Decimal)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    log(f"seeded {len(items)} portfolios into {data['PortfolioTableName']}")
    env.c("s3").upload_file(str(ROOT / "data" / "investment_policy.md"), data["DocumentBucketName"],
                            f"{data['DocumentPrefix']}investment_policy.md", ExtraArgs={"ContentType": "text/markdown"})
    agent = env.c("bedrock-agent")
    kb, ds = data["KnowledgeBaseId"], data["DataSourceId"]
    job = agent.start_ingestion_job(knowledgeBaseId=kb, dataSourceId=ds)["ingestionJob"]["ingestionJobId"]
    while True:
        j = agent.get_ingestion_job(knowledgeBaseId=kb, dataSourceId=ds, ingestionJobId=job)["ingestionJob"]
        if j["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            break
        time.sleep(8)
    if j["status"] != "COMPLETE" or j.get("statistics", {}).get("numberOfDocumentsFailed"):
        raise SystemExit(f"KB ingestion {j['status']}: {j.get('failureReasons')}")
    log(f"KB ingestion complete: {j.get('statistics')}")


def adot_layer(env: Env) -> str:
    lam, base = env.c("lambda"), ADOT_LAYER.format(region=env.region)
    for version in range(45, 15, -1):
        try:
            return lam.get_layer_version_by_arn(Arn=f"{base}:{version}")["LayerVersionArn"]
        except ClientError:
            continue
    raise SystemExit(f"no AWS Distro for OpenTelemetry Python layer found in {env.region} ({base}:<version>)")


# =============================================================================== manifests (outputs of local steps)
def manifest_path(env: Env, kind: str) -> Path:
    return STATE_DIR / f"{env.name}.{kind}.json"


def write_manifest(env: Env, kind: str, data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path(env, kind).write_text(json.dumps(data, indent=2, sort_keys=True))


def read_manifest(env: Env, kind: str, hint: str) -> dict[str, Any]:
    path = manifest_path(env, kind)
    if not path.exists():
        raise SystemExit(f"{_rel(path)} not found - run `{hint}` first")
    return json.loads(path.read_text())


def stack_outputs(env: Env, layer: str) -> dict[str, str]:
    name = LAYERS[layer].stack(env)
    try:
        stack = env.cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as exc:
        raise SystemExit(f"stack {name} not found - deploy layer `{layer}` first") from exc
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def images(env: Env) -> dict[str, str]:
    return read_manifest(env, "images", f"python -m cloudformation.deploy images --env {env.name}")


def gateway_mcp_url(gw: dict[str, str]) -> str:
    url = gw["GatewayUrl"].rstrip("/")
    return url if url.endswith("/mcp") else f"{url}/mcp"


# =============================================================================== stack parameters (one function per template)
def _p_exec(env: Env) -> dict[str, Any]:
    return {"ProjectPrefix": PROJECT, "RoleName": EXEC_ROLE_NAME}


def _p_prefix(env: Env) -> dict[str, Any]:
    return {"Prefix": env.prefix}


def _p_security_group(env: Env) -> dict[str, Any]:
    return {"Prefix": env.prefix, "VpcId": env.config["network"]["vpc_id"]}


def _p_lambdas(env: Env) -> dict[str, Any]:
    sg, arts, data = stack_outputs(env, "security-group"), stack_outputs(env, "artifacts"), stack_outputs(env, "data")
    code = read_manifest(env, "lambdas", f"python -m cloudformation.deploy lambdas --env {env.name}")
    return {"Prefix": env.prefix, "RuntimeNamePrefix": env.runtime_prefix, "PrivateSubnetIds": env.subnets,
            "SecurityGroupId": sg["SecurityGroupId"], "ArtifactBucket": arts["LambdaArtifactBucketName"],
            "KbCodeKey": code["kb"], "DdbCodeKey": code["ddb"], "ComplianceCodeKey": code["compliance"],
            "AnalyticsCodeKey": code["analytics"], "AdotLayerArn": adot_layer(env),
            "PortfolioTableName": data["PortfolioTableName"], "PortfolioTableArn": data["PortfolioTableArn"],
            "KnowledgeBaseId": data["KnowledgeBaseId"], "KnowledgeBaseArn": data["KnowledgeBaseArn"]}


def _runtime_common(env: Env) -> dict[str, Any]:
    return {"Prefix": env.prefix, "RuntimeNamePrefix": env.runtime_prefix, "PrivateSubnetIds": env.subnets,
            "SecurityGroupId": stack_outputs(env, "security-group")["SecurityGroupId"], **identity(env)}


def _p_runtimes(env: Env) -> dict[str, Any]:
    img = images(env)
    return {**_runtime_common(env), "QuantMcpImageUri": img["quant_mcp"], "MarketAnalystImageUri": img["market_analyst"],
            **system_prompt_params("MarketAnalyst", "market_analyst_system")}


def _p_gateway(env: Env) -> dict[str, Any]:
    ident, lam, rts = identity(env), stack_outputs(env, "lambdas"), stack_outputs(env, "agent-runtimes")
    return {"Prefix": env.prefix, "DiscoveryUrl": ident["DiscoveryUrl"], "JwtAudience": ident["JwtAudience"],
            "JwtAzp": ident["JwtAzp"], "KbFunctionArn": lam["KbFunctionArn"], "DdbFunctionArn": lam["DdbFunctionArn"],
            "ComplianceFunctionArn": lam["ComplianceFunctionArn"], "AnalyticsFunctionArn": lam["AnalyticsFunctionArn"],
            "QuantMcpRuntimeArn": rts["QuantMcpRuntimeArn"], "QuantMcpRuntimeUrl": rts["QuantMcpRuntimeUrl"]}


def _p_orchestrator(env: Env) -> dict[str, Any]:
    rts, gw = stack_outputs(env, "agent-runtimes"), stack_outputs(env, "gateway")
    return {**_runtime_common(env), "OrchestratorImageUri": images(env)["orchestrator"], "GatewayUrl": gateway_mcp_url(gw),
            "MarketAnalystRuntimeUrl": rts["MarketAnalystRuntimeUrl"],
            **system_prompt_params("Orchestrator", "orchestrator_system")}


def _p_observability(env: Env) -> dict[str, Any]:
    return {}


def _p_telemetry(env: Env) -> dict[str, Any]:
    """Workload identities are created implicitly by the runtimes and the gateway; their ARNs are looked up here."""
    lam, rts = stack_outputs(env, "lambdas"), stack_outputs(env, "agent-runtimes")
    gw, orch = stack_outputs(env, "gateway"), stack_outputs(env, "orchestrator")
    ctl = env.c("bedrock-agentcore-control")
    params: dict[str, Any] = {"Prefix": env.prefix, "GatewayArn": gw["GatewayArn"], "MemoryArn": orch["MemoryArn"],
                              "CodeInterpreterArn": lam["CodeInterpreterArn"]}
    for label, arn in (("QuantMcp", rts["QuantMcpRuntimeArn"]), ("MarketAnalyst", rts["MarketAnalystRuntimeArn"]),
                       ("Orchestrator", orch["OrchestratorRuntimeArn"])):
        details = ctl.get_agent_runtime(agentRuntimeId=arn.split("/")[-1])["workloadIdentityDetails"]
        params.update({f"{label}RuntimeArn": arn, f"{label}WorkloadIdentityArn": details["workloadIdentityArn"]})
    params["GatewayWorkloadIdentityArn"] = ctl.get_gateway(
        gatewayIdentifier=gw["GatewayId"])["workloadIdentityDetails"]["workloadIdentityArn"]
    return params


class Layer:
    def __init__(self, template: str, params: Any, stack_name: str | None = None, uses_exec_role: bool = True) -> None:
        self.template, self.params, self._stack_name, self.uses_exec_role = template, params, stack_name, uses_exec_role

    def stack(self, env: Env) -> str:
        return self._stack_name or env.stack(self.template.split("-", 1)[1].removesuffix(".yaml"))


# layer name -> template, in deployment order
LAYERS: dict[str, Layer] = {
    "execution-role": Layer("00-execution-role.yaml", _p_exec, EXEC_ROLE_STACK, uses_exec_role=False),
    "security-group": Layer("01-security-group.yaml", _p_security_group),
    "artifacts": Layer("02-artifacts.yaml", _p_prefix),
    "data": Layer("03-data.yaml", _p_prefix),
    "lambdas": Layer("04-lambdas.yaml", _p_lambdas),
    "agent-runtimes": Layer("05-agent-runtimes.yaml", _p_runtimes),
    "gateway": Layer("06-gateway.yaml", _p_gateway),
    "orchestrator": Layer("07-orchestrator.yaml", _p_orchestrator),
    "observability": Layer("08-observability.yaml", _p_observability, f"{PROJECT}-observability"),
    "telemetry": Layer("09-telemetry.yaml", _p_telemetry),
}
TEARDOWN = ("telemetry", "orchestrator", "gateway", "agent-runtimes", "lambdas", "data", "artifacts", "security-group")


def exec_role_arn(env: Env) -> str:
    return f"arn:aws:iam::{env.account}:role/{EXEC_ROLE_NAME}"


def deploy_layer(env: Env, layer: str) -> dict[str, str]:
    spec = LAYERS[layer]
    outputs = deploy_stack(env, spec.stack(env), spec.template, spec.params(env),
                           exec_role_arn(env) if spec.uses_exec_role else None)
    if layer == "execution-role":
        time.sleep(10)  # IAM propagation before CloudFormation can assume the new role
    return outputs


# =============================================================================== commands
def cmd_check(env: Env, args: argparse.Namespace) -> None:
    """Validate config/<env>.json against AWS (VPC, subnets, AZs) and Entra ID (discovery document)."""
    net = env.config["network"]
    subnets = env.c("ec2").describe_subnets(SubnetIds=env.subnets)["Subnets"]
    wrong_vpc = [s["SubnetId"] for s in subnets if s["VpcId"] != net["vpc_id"]]
    if wrong_vpc:
        raise SystemExit(f"subnets {wrong_vpc} are not in {net['vpc_id']}")
    supported = AGENTCORE_AZ_IDS.get(env.region)
    if supported:
        bad = [f"{s['SubnetId']} ({s['AvailabilityZoneId']})" for s in subnets if s["AvailabilityZoneId"] not in supported]
        if bad:
            raise SystemExit(f"AgentCore VPC mode in {env.region} supports AZ IDs {sorted(supported)}; not: {bad}")
    public = [s["SubnetId"] for s in subnets if s.get("MapPublicIpOnLaunch")]
    if public:
        log(f"WARNING: {public} assign public IPs - AgentCore and Lambda need private subnets with NAT egress")
    ident = identity(env)
    log(f"network ok: {net['vpc_id']} subnets {[s['SubnetId'] + '/' + s['AvailabilityZoneId'] for s in subnets]}")
    log(f"Entra ok: issuer {ident['JwtIssuer']} audience {ident['JwtAudience']} azp {ident['JwtAzp']} "
        f"claims {ident['AdvisorIdClaim']} / {ident['ClientIdClaim']}")
    if env.c("xray").get_trace_segment_destination().get("Destination") != "CloudWatchLogs":
        log("NOTE: CloudWatch Transaction Search is off - deploy with --with-observability to enable it")
    log(f"tags: {env.tags}")


def cmd_images(env: Env, args: argparse.Namespace) -> None:
    """Build locally + push to the ECR repositories of 02-artifacts, or record pre-pushed images (--image-tag)."""
    if args.image_tag:
        ecr, uris = env.c("ecr"), {}
        for key, (repo, _module, _port) in IMAGES.items():
            repository = f"{env.prefix}/{repo}"
            try:
                ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": args.image_tag}])
            except ClientError as exc:
                raise SystemExit(f"{env.registry}/{repository}:{args.image_tag} not found - push it first "
                                 f"(scripts/build_and_push_images.sh)") from exc
            uris[key] = f"{env.registry}/{repository}:{args.image_tag}"
        log(f"using pushed images :{args.image_tag}")
    else:
        uris = build_images(env)
    write_manifest(env, "images", uris)


def cmd_lambdas(env: Env, args: argparse.Namespace) -> None:
    write_manifest(env, "lambdas", package_lambdas(env, stack_outputs(env, "artifacts")["LambdaArtifactBucketName"]))


def cmd_seed(env: Env, args: argparse.Namespace) -> None:
    seed_and_ingest(env, stack_outputs(env, "data"))


def cmd_stack(env: Env, args: argparse.Namespace) -> None:
    deploy_layer(env, args.layer)


def cmd_params(env: Env, args: argparse.Namespace) -> None:
    """Write the parameters for one template, for a manual console / AWS CLI deployment."""
    spec = LAYERS[args.layer]
    params = [{"ParameterKey": k, "ParameterValue": ",".join(v) if isinstance(v, list) else str(v)}
              for k, v in spec.params(env).items()]
    out = STATE_DIR / "params" / env.name / f"{spec.template.removesuffix('.yaml')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(params, indent=2))
    role = f" --role-arn {exec_role_arn(env)}" if spec.uses_exec_role else ""
    tags = " ".join(f"Key={t['Key']},Value={t['Value']}" for t in env.tag_list())
    print(json.dumps({"stack": spec.stack(env), "template": f"cloudformation/templates/{spec.template}",
                      "parameters_file": _rel(out)}, indent=2))
    print(f"\n# AWS CLI (create-stack the first time, update-stack afterwards):\n"
          f"aws cloudformation create-stack --stack-name {spec.stack(env)} "
          f"--template-body file://cloudformation/templates/{spec.template} "
          f"--parameters file://{_rel(out)} --capabilities CAPABILITY_NAMED_IAM{role} --tags {tags}")


def cmd_state(env: Env, args: argparse.Namespace) -> None:
    """Write cloudformation/.state/<env>.json (read by smoke and the end-to-end tests) from the deployed stacks."""
    ident = identity(env)
    data, lam = stack_outputs(env, "data"), stack_outputs(env, "lambdas")
    rts, gw, orch = stack_outputs(env, "agent-runtimes"), stack_outputs(env, "gateway"), stack_outputs(env, "orchestrator")
    state = {
        "environment": env.name, "region": env.region,
        "jwt_issuer": ident["JwtIssuer"], "jwt_discovery_url": ident["DiscoveryUrl"],
        "jwt_audience": ident["JwtAudience"], "jwt_azp": ident["JwtAzp"],
        "advisor_id_claim": ident["AdvisorIdClaim"], "client_id_claim": ident["ClientIdClaim"],
        "portfolio_table": data["PortfolioTableName"], "knowledge_base_id": data["KnowledgeBaseId"],
        "kb_lambda_name": lam["KbFunctionName"], "ddb_lambda_name": lam["DdbFunctionName"],
        "compliance_lambda_name": lam["ComplianceFunctionName"], "analytics_lambda_name": lam["AnalyticsFunctionName"],
        "gateway_id": gw["GatewayId"], "gateway_arn": gw["GatewayArn"], "gateway_url": gateway_mcp_url(gw),
        "code_interpreter_id": lam["CodeInterpreterId"], "code_interpreter_arn": lam["CodeInterpreterArn"],
        "guardrail_id": orch["GuardrailId"], "guardrail_version": orch["GuardrailVersion"],
        "memory_id": orch["MemoryId"], "memory_arn": orch["MemoryArn"],
        "orchestrator_prompt_id": orch["OrchestratorPromptId"], "orchestrator_prompt_version": orch["OrchestratorPromptVersion"],
        "market_analyst_prompt_id": rts["MarketAnalystPromptId"], "market_analyst_prompt_version": rts["MarketAnalystPromptVersion"],
        "vpc_id": env.config["network"]["vpc_id"], "private_subnet_ids": env.subnets,
        "security_group_id": stack_outputs(env, "security-group")["SecurityGroupId"],
        "orchestrator_runtime_arn": orch["OrchestratorRuntimeArn"], "orchestrator_runtime_id": orch["OrchestratorRuntimeId"],
        "orchestrator_runtime_url": orch["OrchestratorRuntimeUrl"],
    }
    for key, label in (("quant_mcp", "QuantMcp"), ("market_analyst", "MarketAnalyst")):
        state.update({f"{key}_runtime_arn": rts[f"{label}RuntimeArn"], f"{key}_runtime_id": rts[f"{label}RuntimeId"],
                      f"{key}_runtime_url": rts[f"{label}RuntimeUrl"]})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env.state_file.write_text(json.dumps(state, indent=2, sort_keys=True))
    log(f"state written to {_rel(env.state_file)}")


def cmd_up(env: Env, args: argparse.Namespace) -> None:
    log(f"env={env.name} prefix={env.prefix} region={env.region}")
    cmd_check(env, args)
    deploy_layer(env, "execution-role")
    deploy_layer(env, "security-group")
    deploy_layer(env, "artifacts")
    cmd_images(env, args)
    deploy_layer(env, "data")
    cmd_seed(env, args)
    cmd_lambdas(env, args)
    for layer in ("lambdas", "agent-runtimes", "gateway", "orchestrator"):
        deploy_layer(env, layer)
    if args.with_observability:
        deploy_layer(env, "observability")
    deploy_layer(env, "telemetry")
    cmd_state(env, args)
    log("done")


def cmd_smoke(env: Env, args: argparse.Namespace) -> None:
    token = os.getenv("COPILOT_TOKEN")
    if not token:
        raise SystemExit("set COPILOT_TOKEN to an Entra ID access token of a test advisor "
                         "(python scripts/get_entra_token.py prints one)")
    state = json.loads(env.state_file.read_text())
    req = urllib.request.Request(f"{state['orchestrator_runtime_url']}/invocations?qualifier=DEFAULT", method="POST",
                                 data=json.dumps({"prompt": "Propose a compliant rebalance for my client."}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"smoke-{secrets.token_hex(16)}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"orchestrator returned {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc
    log(f"client={body['client']['client_id']} compliance={body['compliance']['status']} "
        f"trades={len(body['proposed_trades'])} guardrail={body['guardrail']} prompts={body['system_prompts']}")
    log(f"recommendation: {body['recommendation'][:300]}")
    log("smoke test passed")


def _empty_bucket(env: Env, name: str) -> None:
    try:
        env.session.resource("s3", region_name=env.region).Bucket(name).object_versions.delete()
    except ClientError:
        pass


def cmd_down(env: Env, args: argparse.Namespace) -> None:
    role = exec_role_arn(env)
    for layer in TEARDOWN:
        if layer == "artifacts":
            _empty_bucket(env, f"{env.prefix}-artifacts-{env.account}-{env.region}")
        if layer == "data":
            for bucket in (f"{env.prefix}-kb-{env.account}-{env.region}", f"{env.prefix}-kb-media-{env.account}-{env.region}"):
                _empty_bucket(env, bucket)
        if not delete_stack(env, LAYERS[layer].stack(env), role) and layer == "security-group":
            log("the security group is still attached to network interfaces that AgentCore/Lambda release "
                "asynchronously (up to 8 h) - run `down` again later")
            return
    for path in [env.state_file, *STATE_DIR.glob(f"{env.name}.*.json")]:
        path.unlink(missing_ok=True)
    log(f"teardown of {env.prefix} complete (the shared execution role stack {EXEC_ROLE_STACK} is kept)")


COMMANDS = {
    "check": (cmd_check, "validate config/<env>.json: VPC, subnets, AgentCore AZs, Entra discovery URL"),
    "up": (cmd_up, "deploy everything (check, then all steps below in order)"),
    "images": (cmd_images, "build the 3 linux/arm64 images locally and push them (or --image-tag for pushed ones)"),
    "seed": (cmd_seed, "seed DynamoDB + upload the policy document + run KB ingestion (after data)"),
    "lambdas": (cmd_lambdas, "package the 4 Lambda zips and upload them (after artifacts)"),
    "stack": (cmd_stack, "deploy one layer: stack <layer>"),
    "params": (cmd_params, "write one template's parameters for a manual console/CLI deployment: params <layer>"),
    "state": (cmd_state, "write cloudformation/.state/<env>.json for smoke and the E2E tests"),
    "smoke": (cmd_smoke, "one advisory round-trip with COPILOT_TOKEN"),
    "down": (cmd_down, "delete the environment's stacks"),
}


def main() -> None:
    epilog = "commands:\n" + "\n".join(f"  {name:8} {help_}" for name, (_, help_) in COMMANDS.items()) + \
             "\n\nlayers: " + ", ".join(LAYERS)
    parser = argparse.ArgumentParser(description=__doc__, epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=list(COMMANDS))
    parser.add_argument("layer", nargs="?", choices=list(LAYERS), help="for `stack` and `params`")
    parser.add_argument("--env", default="poc", help="environment name = config/<env>.json (lowercase alphanumeric)")
    parser.add_argument("--image-tag", help="use images already pushed to <registry>/portfolio-copilot-<env>/<service>:<tag>")
    parser.add_argument("--with-observability", action="store_true",
                        help="also enable CloudWatch Transaction Search (account singleton; skip if already on)")
    args = parser.parse_args()
    if not args.env.isalnum() or not args.env.islower():
        sys.exit("--env must be lowercase alphanumeric")
    if args.action in ("stack", "params") and not args.layer:
        sys.exit(f"`{args.action}` needs a layer: {', '.join(LAYERS)}")
    env = Env(args.env, load_config(args.env))
    COMMANDS[args.action][0](env, args)


if __name__ == "__main__":
    main()
