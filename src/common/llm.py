"""Amazon Bedrock Converse client pinned to low-cost Amazon Nova models."""
from __future__ import annotations

from typing import Any, Protocol

import boto3
from botocore.config import Config

from src.common.config import get_settings
from src.common.tracer import client_span


class LLM(Protocol):
    def complete(self, system: str, prompt: str, *, max_tokens: int = 512, temperature: float = 0.2) -> str: ...


class BedrockConverseLLM:
    def __init__(self, model_id: str | None = None, *, region: str | None = None, client: Any = None) -> None:
        settings = get_settings()
        self.model_id = model_id or settings.model_id
        if "nova" not in self.model_id:
            raise ValueError(f"POC is restricted to Amazon Nova models, got {self.model_id}")
        self._client = client or boto3.client(
            "bedrock-runtime", region_name=region or settings.region,
            config=Config(retries={"mode": "adaptive", "max_attempts": 4}, read_timeout=60),
        )

    def converse(self, *, system: str, messages: list[dict[str, Any]], tool_config: dict[str, Any] | None = None,
                 max_tokens: int = 512, temperature: float = 0.2) -> dict[str, Any]:
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if tool_config:
            request["toolConfig"] = tool_config
        attrs = {"gen_ai.system": "aws.bedrock", "gen_ai.operation.name": "chat", "gen_ai.request.model": self.model_id,
                 "gen_ai.request.max_tokens": max_tokens, "gen_ai.request.temperature": temperature}
        with client_span("bedrock.converse", attrs) as (span, _):
            resp = self._client.converse(**request)
            usage = resp.get("usage", {})
            span.set_attribute("gen_ai.usage.input_tokens", usage.get("inputTokens", 0))
            span.set_attribute("gen_ai.usage.output_tokens", usage.get("outputTokens", 0))
            span.set_attribute("gen_ai.response.finish_reason", resp.get("stopReason", ""))
        return resp

    def complete(self, system: str, prompt: str, *, max_tokens: int = 512, temperature: float = 0.2) -> str:
        resp = self.converse(system=system, messages=[{"role": "user", "content": [{"text": prompt}]}],
                             max_tokens=max_tokens, temperature=temperature)
        blocks = resp["output"]["message"]["content"]
        return "".join(b.get("text", "") for b in blocks).strip()
