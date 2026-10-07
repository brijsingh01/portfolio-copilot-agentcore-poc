"""System prompts from Amazon Bedrock Prompt Management.

Agents carry no prompt text. Each deployed agent is configured with a prompt id and a **pinned version**
(SYSTEM_PROMPT_ID / SYSTEM_PROMPT_VERSION) and fetches it with bedrock-agent GetPrompt. A version is immutable,
so it is fetched once per process and cached. Changing the prompt means publishing a new version and pointing the
agent at it, which is an auditable, reversible deployment step.

Without a configured prompt (local runs, unit tests) the bundled source file src/prompts/<name>.txt is used; the
deployment publishes those same files to Prompt Management, so there is one source of truth.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from src.common.config import get_settings
from src.common.tracer import client_span

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


class PromptUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SystemPrompt:
    text: str
    source: str            # "prompt_management" | "bundled_file"
    prompt_id: str = ""
    version: str = ""
    arn: str = ""

    def describe(self) -> dict[str, str]:
        return {"source": self.source, "id": self.prompt_id, "version": self.version}

    def span_attributes(self) -> dict[str, str]:
        return {"gen_ai.prompt.source": self.source, "gen_ai.prompt.id": self.prompt_id,
                "gen_ai.prompt.version": self.version}


def _variant_text(prompt: dict[str, Any]) -> str:
    variants = prompt.get("variants") or []
    default = prompt.get("defaultVariant")
    variant = next((v for v in variants if v.get("name") == default), variants[0] if variants else None)
    if variant is None:
        raise PromptUnavailable(f"prompt {prompt.get('id')} has no variants")
    cfg = variant.get("templateConfiguration", {})
    if "text" in cfg:
        text = cfg["text"].get("text", "")
    else:  # CHAT template: the system blocks are the system prompt
        text = "\n".join(block.get("text", "") for block in cfg.get("chat", {}).get("system", []))
    if not text.strip():
        raise PromptUnavailable(f"prompt {prompt.get('id')} variant {variant.get('name')} has no text")
    return text.strip()


class PromptStore:
    def __init__(self, name: str, *, prompt_id: str = "", version: str = "", region: str | None = None,
                 client: Any = None) -> None:
        if prompt_id and not version:
            raise ValueError("a managed prompt must be pinned to a version (SYSTEM_PROMPT_VERSION)")
        self.name, self.prompt_id, self.version = name, prompt_id, version
        self._region, self._client = region, client
        self._cached: SystemPrompt | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, name: str) -> PromptStore:
        s = get_settings()
        return cls(name, prompt_id=s.system_prompt_id, version=s.system_prompt_version, region=s.region)

    def _bedrock_agent(self) -> Any:
        if self._client is None:
            self._client = boto3.client("bedrock-agent", region_name=self._region or get_settings().region,
                                        config=Config(retries={"mode": "standard", "max_attempts": 3}))
        return self._client

    def _load(self) -> SystemPrompt:
        if not self.prompt_id:
            path = PROMPT_DIR / f"{self.name}.txt"
            return SystemPrompt(path.read_text(encoding="utf-8").strip(), "bundled_file", self.name, "local")
        attrs = {"gen_ai.prompt.id": self.prompt_id, "gen_ai.prompt.version": self.version}
        with client_span("bedrock.get_prompt", attrs):
            try:
                resp = self._bedrock_agent().get_prompt(promptIdentifier=self.prompt_id, promptVersion=self.version)
            except Exception as exc:
                raise PromptUnavailable(f"GetPrompt {self.prompt_id}:{self.version} failed: {exc}") from exc
        return SystemPrompt(_variant_text(resp), "prompt_management", resp.get("id", self.prompt_id),
                            str(resp.get("version", self.version)), resp.get("arn", ""))

    def get(self) -> SystemPrompt:
        """The pinned prompt; fetched once, then served from memory. Failures are not cached (next call retries)."""
        if self._cached is None:
            with self._lock:
                if self._cached is None:
                    self._cached = self._load()
        return self._cached
