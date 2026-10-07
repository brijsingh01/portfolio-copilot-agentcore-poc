# Portfolio Advisory Copilot: Amazon Bedrock AgentCore POC

An end-to-end proof of concept of a multi-agent advisory assistant on **Amazon Bedrock AgentCore**, deployed with
**CloudFormation** into **your existing VPC**, authenticated with **Microsoft Entra ID**, using low-cost
**Amazon Nova** models.

An advisor signs in with Entra ID and asks *"Marcus has idle cash. Propose a compliant rebalance."* The orchestrator
agent then does the following:

1. screens the prompt with an **Amazon Bedrock Guardrail**;
2. loads the conversation and the client's constraints from **AgentCore Memory**;
3. calls tools through the **AgentCore Gateway**:
   * the client's portfolio (DynamoDB);
   * the investment policy (Bedrock Knowledge Base on S3 Vectors);
   * rebalance and tax calculations (an MCP server on AgentCore Runtime);
   * the compliance rules;
   * portfolio analytics (**AgentCore Code Interpreter**);
4. asks the **Market Analyst** agent (A2A, on AgentCore Runtime);
5. writes the recommendation with **Nova Micro**, using a system prompt from **Bedrock Prompt Management**, and
   screens the output;
6. returns one response, traced end to end in CloudWatch / X-Ray.

```
                     Entra ID access token (same discovery URL, audience, azp everywhere)
advisor ──▶ Orchestrator runtime ──▶ Gateway ──▶ portfolio-db · policy-kb · compliance-rules · portfolio-analytics (Lambda)
                 │                           └──▶ quant-mcp (MCP runtime)
                 ├──▶ Market Analyst runtime (A2A)
                 ├──▶ Guardrail · Memory · Prompt Management · Nova Micro
```

## Repository

```
├── README.md                    this file
├── docs/DEPLOYMENT_GUIDE.md     Entra setup, network requirements, manual / pipeline / one-command deployment
├── config/example.json          copy to config/<env>.json: your VPC, subnets, Entra settings, tags
├── cloudformation/
│   ├── README.md                technical reference: architecture, templates, parameters, security
│   ├── deploy.py                driver: check, up, or one step at a time (stack <layer>, params <layer>, …)
│   ├── bootstrap/ci-deployer-role.yaml   least-privilege role for the person or pipeline that deploys
│   └── templates/00…09-*.yaml   the stacks
├── scripts/
│   ├── build_and_push_images.sh / .ps1   build the 3 container images locally and push them to ECR
│   └── get_entra_token.py                sign in (device code) and print an Entra access token
├── src/                         orchestrator, market_analyst, quant_mcp, lambdas/, common/, prompts/
├── docker/Dockerfile            one image definition for the three runtimes (linux/arm64)
├── data/                        seed portfolios, investment policy document, market reference data
└── tests/                       unit tests (offline) and end-to-end tests (against a deployment)
```

## What gets created

The stacks create the following resources. They create **no VPC, subnet, NAT gateway or identity provider**:
those are yours.

| Stack | Resources |
|---|---|
| `portfolio-copilot-cfn-execution-role` | the CloudFormation service role (shared by environments) |
| `portfolio-copilot-<env>-security-group` | one security group in your VPC: no inbound rules, outbound 443 only |
| `…-artifacts` | 3 ECR repositories, the Lambda code bucket |
| `…-data` | DynamoDB table, S3 + S3 Vectors, Bedrock Knowledge Base |
| `…-lambdas` | 4 Lambda tools + the Code Interpreter, in your private subnets |
| `…-agent-runtimes` | Quant MCP and Market Analyst runtimes, the analyst's managed prompt |
| `…-gateway` | AgentCore Gateway (Entra JWT authorizer) + 5 tool targets |
| `…-orchestrator` | Guardrail, Memory, the orchestrator's managed prompt, the Orchestrator runtime |
| `…-telemetry` | log and trace deliveries for every AgentCore resource |

Every stack, and every resource CloudFormation creates in it, is tagged with the `tags` from `config/<env>.json`
(by default `Application=A00000`, `Environment=C0`, `Provisioned-By=CICD`) plus `project=portfolio-copilot-<env>`.

## Prerequisites

* **AWS:** region `us-east-1`, with Bedrock model access for **Amazon Nova Micro** and **Amazon Nova Multimodal
  Embeddings**.
* **Existing VPC:** at least two **private subnets** in the AgentCore Availability Zones (`use1-az1`, `use1-az2`,
  `use1-az4`), with outbound HTTPS to AWS and to `login.microsoftonline.com`
  ([details](docs/DEPLOYMENT_GUIDE.md#network-requirements)).
* **Microsoft Entra ID:** an API app registration, a client app registration, and test advisors whose access tokens
  carry an advisor id and a client id ([how](docs/DEPLOYMENT_GUIDE.md#microsoft-entra-id-setup)).
* **Tools:** Python 3.12, the AWS CLI, and Docker with `buildx`. The images are `linux/arm64`; on x86, Docker
  Desktop's emulation handles that.
* **Permissions:** the deployer role in `cloudformation/bootstrap/ci-deployer-role.yaml`, created once by an
  administrator, or administrator access.

## Run it

```bash
pip install -r requirements-dev.txt
python -m pytest tests/unit -q                                  # offline checks (~15 s)

cp config/example.json config/poc.json                          # then fill in your values
python -m cloudformation.deploy check --env poc                 # validates the VPC, subnets and Entra settings

python -m cloudformation.deploy up --env poc                    # ~35 min the first time (arm64 image builds)

export COPILOT_TOKEN=$(python scripts/get_entra_token.py --env poc --scope api://<api-app-id>/access_as_user)
python -m cloudformation.deploy smoke --env poc                 # one advisory round-trip
E2E=1 python -m pytest tests/test_e2e.py tests/test_e2e_security.py -v

python -m cloudformation.deploy down --env poc                  # when you are done
```

Windows PowerShell: `$env:COPILOT_TOKEN = python scripts/get_entra_token.py --env poc --scope api://<api-app-id>/access_as_user`
and `$env:E2E = "1"`.

You can also deploy one template at a time (AWS console, AWS CLI, or a Jenkins-style pipeline), with the images
you built and pushed yourself. See [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md).

## Cost

While the stacks are deployed, everything is pay-per-use and costs pennies for test traffic: Nova Micro, Lambda,
AgentCore Runtime / Gateway / Memory / Code Interpreter, S3 Vectors, on-demand DynamoDB, and CloudWatch logs and
traces. Your VPC's NAT gateway is the main standing cost, and it is yours. Run `down` when you are finished.
