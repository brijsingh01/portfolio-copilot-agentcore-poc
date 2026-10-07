# Deployment guide

1. [Microsoft Entra ID setup](#microsoft-entra-id-setup)
2. [Network requirements](#network-requirements)
3. [Deployer permissions](#deployer-permissions)
4. [Configuration file](#configuration-file)
5. [Deploy: one command, a pipeline, or template by template](#deploy)
6. [Building and pushing the images yourself](#building-and-pushing-the-images-yourself)
7. [Verify](#verify)
8. [Tear down](#tear-down)
9. [Troubleshooting](#troubleshooting)

## Microsoft Entra ID setup

The Gateway, the Orchestrator runtime and the Market Analyst runtime all accept the same token. AgentCore checks
three things: the token is signed by your tenant (discovery URL), it is issued for your API (audience), and it was
requested by your client app (the `azp` claim). The agents then read two more claims, the advisor id and the client
id, to decide which portfolio the caller may see.

1. **API app registration** (e.g. *Portfolio Copilot API*):
   * *Expose an API*: set the Application ID URI (`api://<api-app-id>`) and add a delegated scope,
     e.g. `access_as_user`.
   * *Manifest*: set `"requestedAccessTokenVersion": 2`. v2.0 tokens carry `azp`, and their `aud` is the API app's
     **Application (client) ID**.
2. **Client app registration** (e.g. *Portfolio Copilot Advisor App*):
   * *API permissions*: add the API's `access_as_user` delegated permission and grant admin consent.
   * *Authentication*: set **Allow public client flows** to *Yes*, which `scripts/get_entra_token.py` (device code
     flow) needs.
3. **Advisor and client claims.** Each test advisor's access token must carry their advisor id and the client id
   they act for, for example through directory extension attributes emitted as optional claims of the API app, or
   a claims mapping policy. The POC's seed data expects:

   | Test user | advisor id claim | client id claim |
   |---|---|---|
   | advisor 1 (required) | `ADVISOR-042` | `CLIENT-8801` (Marcus Vance) |
   | advisor 2 (optional, for the second-advisor tests) | `ADVISOR-099` | `CLIENT-8802` (Elena Rostova) |

   Claim names depend on how you emit them. Get a token (step 4) and use the exact names you see.

4. **Check a token.** Put the values below into `config/<env>.json`, then run:

   ```bash
   python scripts/get_entra_token.py --env poc --scope api://<api-app-id>/access_as_user > /dev/null
   ```

   It prints the token's `iss`, `aud`, `azp` and the two tenancy claims, and warns if any of them doesn't match the
   config.

| `config/<env>.json` → `entra` | Value |
|---|---|
| `discovery_url` | `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` |
| `audience` | the **API** app's Application (client) ID |
| `allowed_azp` | the **client** app's Application (client) ID |
| `advisor_id_claim` / `client_id_claim` | the claim names as they appear in the access token |

## Network requirements

The POC creates only a security group in your VPC. Your subnets must provide the following:

* **At least two private subnets** in different AgentCore Availability Zones. In `us-east-1` these are `use1-az1`,
  `use1-az2` and `use1-az4`; AgentCore rejects other AZ IDs. Because AZ *names* map to different AZ *IDs* in each
  account, `check` reports the ID of each subnet.
* **Outbound HTTPS (443)**, typically through a NAT gateway, to:
  * AWS service endpoints: Bedrock (runtime, agent runtime), AgentCore, DynamoDB, S3, CloudWatch Logs, X-Ray,
    and STS;
  * `login.microsoftonline.com`, because the agents fetch Entra's signing keys to validate tokens.

  If you use VPC endpoints instead of NAT for AWS services, you still need a route to `login.microsoftonline.com`.
  A DynamoDB or S3 gateway endpoint policy must allow the `portfolio-copilot-<env>-*` table and buckets.

AgentCore manages its own network interfaces in these subnets. After `down`, they can take up to 8 hours to be
released, so the security group's stack may need a second `down`.

## Deployer permissions

Whoever deploys, whether a person or a Jenkins agent, needs either administrator access or the least-privilege role.
An administrator creates that role once per account:

```bash
aws cloudformation deploy --stack-name portfolio-copilot-ci-deployer \
  --template-file cloudformation/bootstrap/ci-deployer-role.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides TrustedPrincipalArn=arn:aws:iam::<account>:role/<jenkins-or-your-role> \
  --tags Application=A00000 Environment=C0 Provisioned-By=CICD
```

Then use it through an AWS CLI profile, for example in `~/.aws/config`:

```ini
[profile copilot-deployer]
role_arn = arn:aws:iam::<account>:role/portfolio-copilot-ci-deployer
source_profile = default            # or: credential_source = Ec2InstanceMetadata (Jenkins on EC2)
region = us-east-1
duration_seconds = 7200
```

The role can create stacks only under the names `portfolio-copilot-*`, and only through the CloudFormation
execution role that the first stack (`00-execution-role`) creates. It can push to the `portfolio-copilot-*` ECR
repositories and run the end-to-end checks.

## Configuration file

`cp config/example.json config/<env>.json`, then fill it in. `<env>` is a short lowercase name such as `poc` or
`dev`; resources are named `portfolio-copilot-<env>-…`.

```json
{
  "region": "us-east-1",
  "network": { "vpc_id": "vpc-…", "private_subnet_ids": ["subnet-…", "subnet-…"] },
  "entra":   { "discovery_url": "…", "audience": "…", "allowed_azp": "…",
               "advisor_id_claim": "…", "client_id_claim": "…" },
  "tags":    { "Application": "A00000", "Environment": "C0", "Provisioned-By": "CICD" }
}
```

`python -m cloudformation.deploy check --env <env>` validates the file before anything is created. It checks that
the subnets exist, are in the VPC and are in AgentCore Availability Zones, and that the Entra discovery document is
reachable; it also prints the issuer. Placeholder values are rejected.

## Deploy

All commands are `python -m cloudformation.deploy <command> --env <env>`, run from the repository root.

### One command

```bash
python -m cloudformation.deploy up --env poc                   # builds + pushes the images, deploys everything
python -m cloudformation.deploy up --env poc --image-tag v1    # uses images you already pushed (see below)
```

`up` is safe to re-run. Unchanged stacks are skipped, and images and Lambda packages are content-addressed.

### Step by step (pipeline stages, or a person following along)

| # | Step | Command | Manual equivalent |
|---|---|---|---|
| 1 | check the config | `check` | |
| 2 | execution role | `stack execution-role` | `00-execution-role.yaml` with **no** service role; parameters `ProjectPrefix=portfolio-copilot`, `RoleName=portfolio-copilot-cfn-execution` |
| 3 | security group | `stack security-group` | `01-security-group.yaml` |
| 4 | ECR + code bucket | `stack artifacts` | `02-artifacts.yaml` |
| 5 | **images** | `images` or `images --image-tag <tag>` | `scripts/build_and_push_images.sh <env> <tag>`, then `images --image-tag <tag>` |
| 6 | data | `stack data` | `03-data.yaml` |
| 7 | **seed + ingest** | `seed` | |
| 8 | **Lambda code** | `lambdas` | |
| 9 | Lambda tools + Code Interpreter | `stack lambdas` | `04-lambdas.yaml` |
| 10 | runtimes | `stack agent-runtimes` | `05-agent-runtimes.yaml` |
| 11 | gateway | `stack gateway` | `06-gateway.yaml` |
| 12 | orchestrator | `stack orchestrator` | `07-orchestrator.yaml` |
| 13 | *(optional)* Transaction Search | `stack observability` | `08-observability.yaml`, only if not already enabled in the account |
| 14 | log/trace deliveries | `stack telemetry` | `09-telemetry.yaml` |
| 15 | **state file** | `state` | writes `cloudformation/.state/<env>.json` for `smoke` and the tests |

The bold steps are work CloudFormation can't do. They record their results in `cloudformation/.state/<env>.*.json`
so later steps can compute their parameters. In a Jenkins pipeline, each row is one `sh` step.

### Template by template (AWS console or CLI)

For each template from 01 onwards, after the steps before it:

```bash
python -m cloudformation.deploy params gateway --env poc
```

This writes the template's parameter file (`cloudformation/.state/params/poc/06-gateway.json`, in
CloudFormation's `[{"ParameterKey": …, "ParameterValue": …}]` format) and prints a ready-to-run
`aws cloudformation create-stack` command with the execution role and your tags. Use `update-stack` for later
changes. In the **console**, upload the template, copy the parameter values, add the tags, and choose
`portfolio-copilot-cfn-execution` as the IAM role.

Keep the stack names exactly as printed (`portfolio-copilot-<env>-<layer>`): later steps look up earlier stacks by
name.

## Building and pushing the images yourself

The three runtime images share one Dockerfile and are built for `linux/arm64`:

| Repository (created by 02-artifacts) | Build args |
|---|---|
| `portfolio-copilot-<env>/quant-mcp` | `APP_MODULE=src.quant_mcp.app APP_PORT=8000 SERVICE_NAME=quant-mcp` |
| `portfolio-copilot-<env>/market-analyst` | `APP_MODULE=src.market_analyst.app APP_PORT=9000 SERVICE_NAME=market-analyst` |
| `portfolio-copilot-<env>/orchestrator` | `APP_MODULE=src.orchestrator.agent APP_PORT=8080 SERVICE_NAME=orchestrator` |

```bash
scripts/build_and_push_images.sh poc v1            # Windows: scripts\build_and_push_images.ps1 -EnvName poc -Tag v1
python -m cloudformation.deploy images --env poc --image-tag v1
```

The build context is the repository root, with `--provenance=false`, because AgentCore needs a single-platform
manifest. To roll out new code, push a **new** tag, then run `images --image-tag <new>` and `stack agent-runtimes`,
then `stack orchestrator`. Re-pushing the same tag doesn't redeploy anything, because CloudFormation sees no
parameter change.

## Verify

```bash
export COPILOT_TOKEN=$(python scripts/get_entra_token.py --env poc --scope api://<api-app-id>/access_as_user)
python -m cloudformation.deploy smoke --env poc
E2E=1 python -m pytest tests/test_e2e.py tests/test_e2e_security.py -v
```

Optional tokens unlock more end-to-end checks (otherwise those tests are skipped):

* `COPILOT_TOKEN_ADVISOR2`: the second test advisor, for the tenancy and isolation tests;
* `COPILOT_TOKEN_OTHER_APP`: a token for the same API from a **different** client app, to prove the `azp` check.

Try these requests:

* `"Guarantee Marcus 15% a year"` → `guardrail_blocked: true`.
* `"proposed_trades": [{"ticker": "LMT", "action": "BUY", "shares": 20}]` → compliance `REJECTED`.
* `"client_id": "CLIENT-8802"` with advisor 1's token → HTTP 403.

## Tear down

```bash
python -m cloudformation.deploy down --env poc
```

This deletes the environment's stacks in reverse order, emptying the buckets first. The shared execution-role stack
is kept. If the security-group stack can't be deleted yet, AgentCore is still releasing its network interfaces; run
`down` again later. Manually: delete the stacks from 09 down to 01, emptying the three `portfolio-copilot-<env>-*`
buckets before deleting `data` and `artifacts`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `check`: subnet not in a supported AZ | Use private subnets in `use1-az1`, `use1-az2` or `use1-az4` (AZ **IDs**, not names). |
| `check`: discovery URL not reachable | Wrong tenant id, or no internet access from where you run the driver. |
| Every call returns 401/403 at the edge | `aud` or `azp` doesn't match the config; check them with `get_entra_token.py`. Tokens must be v2.0 (`requestedAccessTokenVersion: 2`). |
| 401 from the orchestrator with "missing the … claims" | The advisor/client claim names in the config don't match the token. |
| 403 "may not access records of advisor" | The token's advisor/client ids don't match the seed data (see the Entra setup table). |
| 401 "invalid token: Unable to find a signing key" | The runtimes can't reach `login.microsoftonline.com` (no NAT/egress). |
| Runtimes stuck creating, or Lambda timeouts | The subnets have no route to AWS endpoints (NAT or VPC endpoints). |
| `analytics: {"error": "analytics_unavailable"}` | The Code Interpreter can't start in the subnets; check the security group and egress. |
| Long-term memory empty right after deploy | Extraction is asynchronous and can take minutes on a new memory. |
| Service spans missing in X-Ray | Transaction Search is off (`stack observability`), or the spans haven't arrived yet (3–5 min delay). |
