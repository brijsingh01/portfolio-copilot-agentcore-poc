"""AgentCore native Memory wrapper (data plane `bedrock-agentcore`).

Short-term: conversational events per (actor, session) via `create_event` / `list_events`.
Long-term:  strategy-extracted records via `retrieve_memory_records` (semantic search).
Actor = client_id (constraints persist per client); session = advisor conversation ID.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import boto3

from src.common.config import Settings, get_settings
from src.common.tracer import client_span, current_trace_id

Role = Literal["USER", "ASSISTANT", "TOOL", "OTHER"]
MAX_EVENT_TEXT = 9000  # stay under the per-payload content limit


@dataclass(frozen=True)
class Turn:
    role: Role
    text: str
    timestamp: datetime | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    text: str
    score: float
    namespaces: tuple[str, ...] = ()
    strategy_id: str | None = None


@dataclass(frozen=True)
class MemoryContext:
    recent_turns: list[Turn] = field(default_factory=list)
    long_term: list[MemoryRecord] = field(default_factory=list)

    def render(self) -> str:
        """Compact prompt block for the orchestrator's system prompt."""
        lines: list[str] = []
        if self.long_term:
            lines.append("Persistent client constraints:")
            lines += [f"- {r.text}" for r in self.long_term]
        if self.recent_turns:
            lines.append("Recent conversation:")
            lines += [f"{t.role.lower()}: {t.text}" for t in self.recent_turns]
        return "\n".join(lines)


class AgentCoreMemory:
    def __init__(self, memory_id: str, *, region: str | None = None, client: Any = None,
                 constraints_namespace: str = "/clients/{actorId}/constraints") -> None:
        if not memory_id:
            raise ValueError("memory_id is required (AGENTCORE_MEMORY_ID)")
        self.memory_id = memory_id
        self.constraints_namespace = constraints_namespace
        self._client = client or boto3.client("bedrock-agentcore", region_name=region or get_settings().region)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, client: Any = None) -> AgentCoreMemory:
        s = settings or get_settings()
        return cls(s.memory_id, region=s.region, client=client, constraints_namespace=s.memory_constraints_namespace)

    def _attrs(self, actor_id: str, session_id: str | None = None) -> dict[str, str]:
        attrs = {"agentcore.memory_id": self.memory_id, "agentcore.actor_id": actor_id}
        if session_id:
            attrs["agentcore.session_id"] = session_id
        return attrs

    # ------------------------------------------------------------------ short-term
    def add_turns(self, actor_id: str, session_id: str, turns: Iterable[tuple[Role, str]]) -> str:
        """Persist one event holding ordered conversational turns; returns the event ID."""
        payload = [{"conversational": {"role": role, "content": {"text": text[:MAX_EVENT_TEXT]}}}
                   for role, text in turns if text]
        if not payload:
            raise ValueError("no turns to persist")
        request: dict[str, Any] = {
            "memoryId": self.memory_id,
            "actorId": actor_id,
            "sessionId": session_id,
            "eventTimestamp": datetime.now(timezone.utc),
            "payload": payload,
            "clientToken": str(uuid.uuid4()),
        }
        if trace_id := current_trace_id():
            request["metadata"] = {"trace_id": {"stringValue": trace_id}}
        with client_span("agentcore.memory.create_event", {**self._attrs(actor_id, session_id), "agentcore.turns": len(payload)}):
            resp = self._client.create_event(**request)
        return resp["event"]["eventId"]

    def add_turn(self, actor_id: str, session_id: str, role: Role, text: str) -> str:
        return self.add_turns(actor_id, session_id, [(role, text)])

    def get_recent_turns(self, actor_id: str, session_id: str, max_turns: int = 20) -> list[Turn]:
        """Return up to `max_turns` most recent turns, oldest first."""
        events: list[Mapping[str, Any]] = []
        kwargs: dict[str, Any] = {"memoryId": self.memory_id, "actorId": actor_id, "sessionId": session_id,
                                  "includePayloads": True, "maxResults": min(max_turns, 100)}
        with client_span("agentcore.memory.list_events", self._attrs(actor_id, session_id)) as (span, _):
            while True:
                resp = self._client.list_events(**kwargs)
                events.extend(resp.get("events", []))
                if len(events) >= max_turns or not resp.get("nextToken"):
                    break
                kwargs["nextToken"] = resp["nextToken"]
            span.set_attribute("agentcore.events", len(events))

        turns: list[Turn] = []
        for event in sorted(events, key=lambda e: e["eventTimestamp"]):
            for item in event.get("payload", []):
                conv = item.get("conversational")
                if conv and conv.get("content", {}).get("text"):
                    turns.append(Turn(role=conv["role"], text=conv["content"]["text"],
                                      timestamp=event["eventTimestamp"], event_id=event["eventId"]))
        return turns[-max_turns:]

    # ------------------------------------------------------------------ long-term
    def retrieve_records(self, actor_id: str, query: str, *, top_k: int = 5, namespace: str | None = None,
                         min_score: float = 0.0) -> list[MemoryRecord]:
        ns = (namespace or self.constraints_namespace).replace("{actorId}", actor_id)
        with client_span("agentcore.memory.retrieve_memory_records", {**self._attrs(actor_id), "agentcore.namespace": ns}) as (span, _):
            resp = self._client.retrieve_memory_records(
                memoryId=self.memory_id,
                namespace=ns,
                searchCriteria={"searchQuery": query, "topK": top_k},
                maxResults=top_k,
            )
            summaries = resp.get("memoryRecordSummaries", [])
            span.set_attribute("agentcore.records", len(summaries))
        records = [
            MemoryRecord(record_id=s["memoryRecordId"], text=s["content"]["text"], score=float(s.get("score", 0.0)),
                         namespaces=tuple(s.get("namespaces", [])), strategy_id=s.get("memoryStrategyId"))
            for s in summaries if s.get("content", {}).get("text")
        ]
        return [r for r in records if r.score >= min_score]

    def load_context(self, actor_id: str, session_id: str, query: str, *, max_turns: int = 10, top_k: int = 5) -> MemoryContext:
        return MemoryContext(recent_turns=self.get_recent_turns(actor_id, session_id, max_turns),
                             long_term=self.retrieve_records(actor_id, query, top_k=top_k))
