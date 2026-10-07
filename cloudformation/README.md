# CloudFormation reference

How the POC is built: the request flow, each template and its parameters, what CloudFormation can't do, and the
security model. To deploy, see [../docs/DEPLOYMENT_GUIDE.md](../docs/DEPLOYMENT_GUIDE.md).

## Request flow

```
advisor ──Entra ID access token──▶ Orchestrator runtime  (JWT authorizer: discovery URL, audience, azp)
  1  ApplyGuardrail(INPUT) ─ blocked? → 200 {guardrail_blocked: true}; nothing else runs or is stored
  2  AgentCore Memory: recent turns + /clients/{client}/constraints
  3  Gateway (same token, same authorizer) ─▶ portfolio-db___get_client_portfolio      Lambda → DynamoDB
                                           ─▶ policy-kb___search_investment_policy    Lambda → Bedrock Knowledge Base
     tenancy: the portfolio's advisor/client must match the token, else 403
  4  Market Analyst runtime (A2A, same token, same authorizer) → target weights + Nova commentary
  5  Gateway ─▶ quant-mcp___calculate_rebalance / estimate_tax_drag    MCP server runtime (SigV4, Gateway role)
  6  Gateway ─▶ compliance-rules___review_compliance                   Lambda (deterministic IPS rules)
  7  Gateway ─▶ portfolio-analytics___analyze_portfolio                Lambda → Code Interpreter session (VPC)
  8  Nova Micro narrative (system prompt = pinned Prompt Management version) → ApplyGuardrail(OUTPUT)
     blocked? → deterministic summary instead
  9  Memory: save the turn  ─  one W3C trace across every hop (CloudWatch Transaction Search / X-Ray)
```

**Why the tools don't check tokens.** The Gateway is the tools' authentication boundary: every call through it
carries a token already validated against the discovery URL, audience and `azp`. The tools take plain arguments, so
other agents can reuse them. Per-client tenancy is enforced by the orchestrator. The trade-off: anyone holding a
valid advisor token can call a Gateway tool directly for any client. For production, add AgentCore Gateway
interceptors or AgentCore Policy on the tool arguments.

**Why the Code Interpreter sits behind a Lambda.** Its API is a stateful session (start, execute, stop, with a
streamed result), while a Gateway tool call is a single request and response. The `portfolio-analytics` Lambda
turns the session into one tool call. It runs a fixed analytics program (data passed as base64 JSON; no generated
code), and only its role may open sessions.

## Templates

| # | Template | Creates | Parameters (from) |
|---|---|---|---|
| 00 | `execution-role` | CloudFormation service role scoped to `portfolio-copilot-*` | `ProjectPrefix`, `RoleName` |
| 01 | `security-group` | one SG: no inbound, egress 443 | `VpcId` (config) |
| 02 | `artifacts` | 3 ECR repositories, Lambda code bucket | — |
| 03 | `data` | DynamoDB `ClientPortfolios`, KB document bucket, S3 Vectors index, Knowledge Base (Nova multimodal embeddings) | — |
| 04 | `lambdas` | 4 Lambda tools (arm64, ADOT layer, X-Ray), Code Interpreter (VPC) | subnets (config), SG (01), bucket + code keys (02 + `lambdas`), table + KB (03) |
| 05 | `agent-runtimes` | Quant MCP runtime (IAM), Market Analyst runtime (A2A, JWT), analyst Prompt + PromptVersion | subnets, SG, Entra contract (config), image URIs (`images`), prompt text (`src/prompts`) |
| 06 | `gateway` | Gateway (JWT authorizer) + 5 targets (4 Lambda, 1 MCP) + Gateway role | Entra contract, Lambda ARNs (04), MCP runtime (05) |
| 07 | `orchestrator` | Guardrail + version, Memory, orchestrator Prompt + PromptVersion, Orchestrator runtime | subnets, SG, Entra contract, image URI, Gateway URL (06), analyst URL (05) |
| 08 | `observability` | CloudWatch Transaction Search (account singleton, optional) | — |
| 09 | `telemetry` | 24 log/trace deliveries (runtimes, Code Interpreter, Gateway, Memory, workload identities) | ARNs (04–07) + workload identity ARNs (looked up) |

Every template takes `Prefix` = `portfolio-copilot-<env>`. Templates 04, 05 and 07 also take
`RuntimeNamePrefix` = `portfolio_copilot_<env>`, because runtime names allow only letters, digits and `_`. The
stacks are wired by parameters, not exports, so any template can be updated on its own. `deploy.py params <layer>`
prints the exact values.

The **Entra contract** is the same six parameters wherever it's needed: `JwtIssuer` (read from the discovery
document), `DiscoveryUrl`, `JwtAudience`, `JwtAzp`, `AdvisorIdClaim` and `ClientIdClaim`. AgentCore's authorizers
use the discovery URL, audience and `azp`. The agents check the same token again in-process (keys from the discovery
document's `jwks_uri`) and read the two tenancy claims.

## Components

* **Bedrock Guardrail** (07):
  * denied topics *Guaranteed returns* and *Insider trading*;
  * content filters, with prompt attack at HIGH on input;
  * PII blocking: SSN, card numbers, bank account numbers.

  The orchestrator screens the advisor's prompt and the generated recommendation; it doesn't screen internal tool
  data. If the guardrail is unavailable, the request fails closed.
* **Prompt Management** (05, 07): each agent's system prompt is an `AWS::Bedrock::Prompt` with a pinned
  `AWS::Bedrock::PromptVersion`. The text comes from `src/prompts/*.txt`, and its hash is in the version's
  description, so a new version is published only when the text changes. Runtimes get `SYSTEM_PROMPT_ID` and
  `SYSTEM_PROMPT_VERSION` and fetch the prompt once with `bedrock:GetPrompt`, which is allowed on their own prompt
  only. Responses report the versions used (`system_prompts`).
* **Memory** (07): short-term events per client and session, plus a semantic strategy that extracts client
  constraints into `/clients/{actorId}/constraints`.
* **Telemetry** (09): vended application and usage logs go to `/aws/vendedlogs/bedrock-agentcore/…`, and traces
  go to X-Ray (`aws/spans`). The application code adds ADOT spans with W3C `traceparent` propagation, so one request
  is one trace. Service-side spans arrive 3–5 minutes after the request.
* **Quiet health checks:** containers log nothing at start-up or for health checks (`ping`), so the per-session
  `[runtime-logs]` streams AgentCore creates hold only real warnings and errors.

## What CloudFormation can't do (handled by `deploy.py`)

| Gap | How it's handled |
|---|---|
| Build container images | built locally with `docker buildx` (linux/arm64), or by you with `scripts/build_and_push_images.*` |
| Lambda packages | deterministic zips of `src/common` + the handler; the analytics Lambda vendors a current boto3 for the Code Interpreter API |
| DynamoDB items, KB documents | `seed` writes `data/seed_portfolios.json`, uploads `data/investment_policy.md` and runs the ingestion job |
| Issuer for the authorizers | read from the Entra discovery document, so it always matches the tokens |
| Workload identity ARNs (telemetry) | created implicitly by AgentCore; looked up with `GetAgentRuntime` / `GetGateway` |
| Runtime and Gateway URLs | runtime invocation URLs embed the URL-encoded runtime ARN (built with `!Sub`); the Gateway URL is normalized to `…/mcp` |
| Non-empty buckets on delete | `down` empties them first |
| Network interfaces after delete | AgentCore releases its ENIs asynchronously (up to 8 h); `down` asks you to re-run it if the SG is still in use |

## Security

* **Who can deploy:** the stacks run under `portfolio-copilot-cfn-execution`, which is scoped to
  `portfolio-copilot-*` resources. The deployer only needs `bootstrap/ci-deployer-role.yaml`: CloudFormation on
  `portfolio-copilot-*` stacks, `iam:PassRole` to CloudFormation only, ECR push, the seed/ingest calls, and
  read-only checks.
* **Inbound requests:** AgentCore rejects missing, malformed, forged, expired, wrong-audience or wrong-`azp`
  tokens before they reach a container. The agents check them again, and the orchestrator enforces advisor/client
  tenancy.
* **Quant MCP runtime:** accepts only SigV4 calls from the Gateway role; bearer tokens are rejected.
* **Network:** every workload runs in your private subnets with one security group that has no inbound rules and
  outbound 443 only.
* **IAM per component:** each Lambda and runtime has its own role. Only the agents may call Bedrock models, and only
  Amazon Nova (`LlmModelId` `AllowedPattern`). Only the orchestrator may apply the guardrail, and only the analytics
  Lambda may use the Code Interpreter.
