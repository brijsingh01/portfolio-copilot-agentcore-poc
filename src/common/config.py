"""Environment-driven settings shared by every runtime, Lambda and test."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

# Amazon Nova Micro: lowest-cost Bedrock text model with Converse tool-use support.
# Cross-region inference profile ID; override per runtime with BEDROCK_MODEL_ID if needed.
DEFAULT_MODEL_ID = "us.amazon.nova-micro-v1:0"


def _csv(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.getenv(name, "").split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "us-east-1"))
    service_name: str = field(default_factory=lambda: os.getenv("OTEL_SERVICE_NAME", "portfolio-copilot"))
    model_id: str = field(default_factory=lambda: os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID))

    # Inbound JWT - the same Microsoft Entra ID contract as the AgentCore authorizers
    jwt_issuer: str = field(default_factory=lambda: os.getenv("JWT_ISSUER", ""))
    jwt_discovery_url: str = field(default_factory=lambda: os.getenv("JWT_DISCOVERY_URL", ""))
    jwt_audiences: tuple[str, ...] = field(default_factory=lambda: _csv("JWT_AUDIENCES"))
    jwt_allowed_client_ids: tuple[str, ...] = field(default_factory=lambda: _csv("JWT_ALLOWED_CLIENT_IDS"))  # azp
    jwt_advisor_claim: str = field(default_factory=lambda: os.getenv("JWT_ADVISOR_CLAIM", "advisor_id"))
    jwt_client_claim: str = field(default_factory=lambda: os.getenv("JWT_CLIENT_CLAIM", "client_id"))

    # Downstream endpoints used by the orchestrator
    gateway_url: str = field(default_factory=lambda: os.getenv("GATEWAY_URL", ""))
    market_analyst_url: str = field(default_factory=lambda: os.getenv("MARKET_ANALYST_URL", ""))

    # AgentCore Memory
    memory_id: str = field(default_factory=lambda: os.getenv("AGENTCORE_MEMORY_ID", ""))
    memory_constraints_namespace: str = field(
        default_factory=lambda: os.getenv("MEMORY_CONSTRAINTS_NAMESPACE", "/clients/{actorId}/constraints")
    )

    # Amazon Bedrock Guardrail applied to advisor prompts (INPUT) and generated recommendations (OUTPUT)
    guardrail_id: str = field(default_factory=lambda: os.getenv("BEDROCK_GUARDRAIL_ID", ""))
    guardrail_version: str = field(default_factory=lambda: os.getenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT"))

    # AgentCore Code Interpreter (custom, VPC mode) used by the portfolio-analytics Lambda tool
    code_interpreter_id: str = field(default_factory=lambda: os.getenv("CODE_INTERPRETER_ID", ""))

    # Amazon Bedrock Prompt Management: this agent's system prompt (prompt id or ARN + pinned version).
    # Unset -> the bundled source file src/prompts/<name>.txt (local runs and unit tests).
    system_prompt_id: str = field(default_factory=lambda: os.getenv("SYSTEM_PROMPT_ID", ""))
    system_prompt_version: str = field(default_factory=lambda: os.getenv("SYSTEM_PROMPT_VERSION", ""))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
