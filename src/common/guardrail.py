"""Amazon Bedrock Guardrail checks (ApplyGuardrail) for advisor prompts and generated recommendations.

The guardrail is applied to the text a person wrote or will read - not to the internal JSON facts sent to the
model - so prompt-attack and topic filters do not trip on structured tool data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import boto3
from botocore.config import Config

from src.common.config import get_settings
from src.common.tracer import client_span

Source = Literal["INPUT", "OUTPUT"]


@dataclass(frozen=True)
class GuardrailResult:
    intervened: bool
    text: str  # blocked message (or masked text) when intervened, else the original text
    policies: tuple[str, ...] = field(default_factory=tuple)  # e.g. ("topic:Guaranteed returns", "pii:US_SOCIAL_SECURITY_NUMBER")


def _policies(assessments: list[dict[str, Any]]) -> tuple[str, ...]:
    hits: list[str] = []
    for a in assessments:
        hits += [f"topic:{t['name']}" for t in a.get("topicPolicy", {}).get("topics", []) if t.get("action") == "BLOCKED"]
        hits += [f"content:{f['type']}" for f in a.get("contentPolicy", {}).get("filters", []) if f.get("action") == "BLOCKED"]
        hits += [f"word:{w['match']}" for w in a.get("wordPolicy", {}).get("customWords", []) if w.get("action") == "BLOCKED"]
        sip = a.get("sensitiveInformationPolicy", {})
        hits += [f"pii:{e['type']}" for e in sip.get("piiEntities", []) if e.get("action") in ("BLOCKED", "ANONYMIZED")]
        hits += [f"regex:{r['name']}" for r in sip.get("regexes", []) if r.get("action") in ("BLOCKED", "ANONYMIZED")]
    return tuple(dict.fromkeys(hits))


class BedrockGuardrail:
    def __init__(self, guardrail_id: str, version: str = "DRAFT", *, region: str | None = None, client: Any = None) -> None:
        self.guardrail_id, self.version = guardrail_id, version
        self._client = client or boto3.client("bedrock-runtime", region_name=region or get_settings().region,
                                              config=Config(retries={"mode": "adaptive", "max_attempts": 4}))

    @classmethod
    def from_settings(cls) -> BedrockGuardrail | None:
        s = get_settings()
        return cls(s.guardrail_id, s.guardrail_version, region=s.region) if s.guardrail_id else None

    def check(self, text: str, source: Source) -> GuardrailResult:
        attrs = {"gen_ai.guardrail.id": self.guardrail_id, "gen_ai.guardrail.version": self.version,
                 "gen_ai.guardrail.source": source}
        with client_span("bedrock.apply_guardrail", attrs) as (span, _):
            resp = self._client.apply_guardrail(guardrailIdentifier=self.guardrail_id, guardrailVersion=self.version,
                                                source=source, content=[{"text": {"text": text}}])
            intervened = resp.get("action") == "GUARDRAIL_INTERVENED"
            policies = _policies(resp.get("assessments", []))
            span.set_attribute("gen_ai.guardrail.intervened", intervened)
            if policies:
                span.set_attribute("gen_ai.guardrail.policies", list(policies))
        if not intervened:
            return GuardrailResult(False, text)
        outputs = "".join(o.get("text", "") for o in resp.get("outputs", []))
        return GuardrailResult(True, outputs or text, policies)
