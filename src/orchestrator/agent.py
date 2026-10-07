"""Orchestrator Agent - AgentCore Runtime entrypoint (HTTP protocol, 0.0.0.0:8080).

`POST /invocations` {"prompt", "client_id"?, "proposed_trades"?} with the advisor's Entra ID access token.

Workflow (deterministic plan; Amazon Nova Micro only writes the final narrative):
  1. validate the caller's token, screen the prompt with the Bedrock Guardrail (INPUT),
     load AgentCore Memory (recent turns + client constraints)
  2. Gateway -> portfolio-db (DynamoDB) and policy-kb (Bedrock Knowledge Base on S3 Vectors);
     the portfolio's advisor/client must match the caller's token (tenancy is enforced here)
  3. A2A -> Market Analyst runtime (target weights + commentary), with the caller's token
  4. Gateway -> quant-mcp (rebalance trades, tax drag)
  5. Gateway -> compliance-rules (deterministic IPS review)
  6. Gateway -> portfolio-analytics (AgentCore Code Interpreter in the VPC): concentration and risk
  7. compose the recommendation (system prompt from Bedrock Prompt Management), screen it with the Guardrail
     (OUTPUT), persist the turn to Memory
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry import trace
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.common.a2a_client import A2AClient, A2AError, artifact_data
from src.common.config import get_settings
from src.common.guardrail import BedrockGuardrail, GuardrailResult
from src.common.identity import AuthenticationError, AuthorizationError, CallerContext, JwtValidator
from src.common.llm import LLM, BedrockConverseLLM
from src.common.mcp_client import GatewayClient, McpError
from src.common.memory import AgentCoreMemory, MemoryContext
from src.common.prompts import PromptStore
from src.common.server import SESSION_HEADER, ping_route, serve
from src.common.tracer import (configure_tracing, current_trace_id, current_traceparent, extract_context, internal_span,
                               server_span, with_baggage)

SERVICE = "orchestrator"
log = logging.getLogger(SERVICE)

# Gateway tool names: <target>___<tool>
TOOL_PORTFOLIO = "portfolio-db___get_client_portfolio"
TOOL_POLICY = "policy-kb___search_investment_policy"
TOOL_REBALANCE = "quant-mcp___calculate_rebalance"
TOOL_TAX = "quant-mcp___estimate_tax_drag"
TOOL_COMPLIANCE = "compliance-rules___review_compliance"
TOOL_ANALYTICS = "portfolio-analytics___analyze_portfolio"

PROMPT_NAME = "orchestrator_system"  # Bedrock Prompt Management (pinned version) or src/prompts/<name>.txt


class DownstreamError(RuntimeError):
    def __init__(self, component: str, detail: str, status: int = 502) -> None:
        super().__init__(f"{component}: {detail}")
        self.component, self.detail, self.status = component, detail, status


class ToolGateway(Protocol):
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class AnalystClient(Protocol):
    def send(self, *, bearer_token: str, data: dict[str, Any], text: str = "", session_id: str | None = None,
             metadata: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AdvisorRequest:
    prompt: str
    client_id: str | None = None
    proposed_trades: list[dict[str, Any]] | None = None

    @classmethod
    def parse(cls, body: Any) -> AdvisorRequest:
        if not isinstance(body, dict) or not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
            raise ValueError("body must be a JSON object with a non-empty `prompt`")
        trades = body.get("proposed_trades")
        if trades is not None and not (isinstance(trades, list) and all(isinstance(t, dict) for t in trades)):
            raise ValueError("`proposed_trades` must be a list of trade objects")
        return cls(prompt=body["prompt"].strip()[:4000], client_id=body.get("client_id"), proposed_trades=trades)


class PortfolioOrchestrator:
    def __init__(self, *, gateway_factory: Callable[[str], ToolGateway], analyst: AnalystClient, llm: LLM,
                 memory: AgentCoreMemory | None, guardrail: BedrockGuardrail | None = None,
                 prompts: PromptStore | None = None) -> None:
        self.gateway_factory, self.analyst, self.llm = gateway_factory, analyst, llm
        self.memory, self.guardrail = memory, guardrail
        self.prompts = prompts or PromptStore(PROMPT_NAME)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _tool(gw: ToolGateway, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = gw.call(name, {**args, "_meta": {"traceparent": current_traceparent() or ""}})
        except McpError as exc:
            raise DownstreamError(name, str(exc), 401 if exc.code == 401 else 502) from exc
        if result.get("status") == "error":
            status = 404 if result.get("error_code") == "NOT_FOUND" else 502
            raise DownstreamError(name, result.get("message") or json.dumps(result)[:300], status)
        return result

    def _memory_context(self, client_id: str, session_id: str, query: str) -> MemoryContext:
        if self.memory is None:
            return MemoryContext()
        try:
            return self.memory.load_context(client_id, session_id, query, max_turns=6, top_k=5)
        except Exception as exc:  # memory is advisory context; never block a review on it
            log.warning("memory load failed: %s", exc)
            trace.get_current_span().add_event("memory_unavailable", {"error": type(exc).__name__})
            return MemoryContext()

    def _narrative(self, facts: dict[str, Any], prompt: str) -> tuple[str, str, dict[str, str]]:
        """(text, source, system prompt used). A Prompt Management outage degrades like a model outage."""
        prompt_info: dict[str, str] = {}
        try:
            system = self.prompts.get()
            prompt_info = system.describe()
            trace.get_current_span().set_attributes(system.span_attributes())
            text = self.llm.complete(system.text, f"Advisor request: {prompt}\n\nFacts:\n{json.dumps(facts, default=str)}",
                                     max_tokens=600, temperature=0.1)
            if text:
                return text, "llm", prompt_info
        except Exception as exc:
            log.warning("narrative generation failed: %s", exc)
            trace.get_current_span().add_event("narrative_fallback", {"error": type(exc).__name__})
        return self._fallback_narrative(facts), "fallback", prompt_info

    @staticmethod
    def _fallback_narrative(facts: dict[str, Any]) -> str:
        comp = facts["compliance"]
        lines = [f"Compliance: {comp['status']}."]
        lines += [f"- {f['message']}" for f in comp["findings"] if f["severity"] == "VIOLATION"]
        lines += [PortfolioOrchestrator._trade_line(t) for t in facts["proposed_trades"]]
        tax = facts["tax"].get("estimated_tax")
        if isinstance(tax, (int, float)):
            lines.append(f"Estimated tax: ${tax:,.2f}.")
        return "\n".join(lines)

    @staticmethod
    def _trade_line(trade: dict[str, Any]) -> str:
        """Quant-generated trades carry est_value; advisor-supplied trades may carry only ticker/action/shares."""
        line = f"{trade.get('action', '?')} {trade.get('shares', '?')} {trade.get('ticker', '?')}"
        value = trade.get("est_value")
        if value is None and isinstance(trade.get("price"), (int, float)) and isinstance(trade.get("shares"), (int, float)):
            value = trade["price"] * trade["shares"]
        return f"{line} (~${value:,.0f})" if isinstance(value, (int, float)) else line

    def _screen(self, text: str, source: str) -> GuardrailResult | None:
        if self.guardrail is None:
            return None
        with internal_span(f"orchestrator.guardrail.{source.lower()}") as gspan:
            try:
                result = self.guardrail.check(text, source)  # type: ignore[arg-type]
            except Exception as exc:
                # Fail closed: an unavailable guardrail must not let unscreened text through.
                log.error("guardrail %s check failed: %s", source, exc)
                raise DownstreamError("guardrail", f"{source} screening unavailable") from exc
            gspan.set_attribute("guardrail.intervened", result.intervened)
            return result

    @staticmethod
    def _guardrail_report(input_check: GuardrailResult | None, output_check: GuardrailResult | None) -> dict[str, Any]:
        def one(r: GuardrailResult | None) -> str:
            return "NOT_CONFIGURED" if r is None else ("INTERVENED" if r.intervened else "PASSED")
        policies = [p for r in (input_check, output_check) if r is not None for p in r.policies]
        return {"input": one(input_check), "output": one(output_check), "policies": policies}

    def _compliance(self, gw: ToolGateway, portfolio: dict, trades: list[dict], caller: CallerContext) -> dict[str, Any]:
        out = self._tool(gw, TOOL_COMPLIANCE, {"portfolio": portfolio, "proposed_trades": trades,
                                               "requested_by": caller.advisor_id})
        compliance = out.get("review") or {}
        if not compliance.get("status"):
            raise DownstreamError("compliance", "no verdict in compliance response")
        return compliance

    def _analytics(self, gw: ToolGateway, portfolio: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
        with internal_span("orchestrator.analytics") as aspan:
            try:
                return self._tool(gw, TOOL_ANALYTICS, {"portfolio": portfolio, "proposed_trades": trades})["analytics"]
            except Exception as exc:  # analytics enrich the review; never block it
                log.warning("portfolio analytics failed: %s", exc)
                aspan.add_event("analytics_unavailable", {"error": type(exc).__name__})
                return {"error": "analytics_unavailable"}

    # ------------------------------------------------------------------ workflow
    def run(self, caller: CallerContext, session_id: str, req: AdvisorRequest) -> dict[str, Any]:
        client_id = (req.client_id or caller.client_id).upper()
        caller.authorize(client_id=client_id)
        span = trace.get_current_span()
        span.set_attributes({**caller.span_attributes(), "session.id": session_id})

        input_check = self._screen(req.prompt, "INPUT")
        if input_check is not None and input_check.intervened:
            # Blocked before any tool, model or memory call: nothing about the request is acted on or stored.
            span.set_attribute("guardrail.blocked", True)
            return {"session_id": session_id, "trace_id": current_trace_id(), "advisor_id": caller.advisor_id,
                    "guardrail_blocked": True, "recommendation": input_check.text,
                    "guardrail": self._guardrail_report(input_check, None)}

        with internal_span("orchestrator.memory.load"):
            memory = self._memory_context(client_id, session_id, req.prompt)
        constraints = [r.text for r in memory.long_term]

        gw = self.gateway_factory(caller.token)  # the Gateway validates the same Entra token

        with internal_span("orchestrator.gather_context"):
            portfolio = self._tool(gw, TOOL_PORTFOLIO, {"client_id": client_id})["portfolio"]
            # Tenancy: the tools are identity-agnostic, so the record must belong to this advisor and client.
            caller.authorize(advisor_id=portfolio.get("advisor_id"), client_id=portfolio.get("client_id"))
            policy_query = (f"{portfolio['risk_profile']} risk profile allocation limits, single-stock concentration"
                            f"{', ESG negative screening' if portfolio.get('esg_mandate') else ''}")
            policy = self._tool(gw, TOOL_POLICY, {"query": policy_query, "top_k": 4})
        passages = policy.get("passages", [])

        with internal_span("orchestrator.market_analyst"):
            try:
                task = self.analyst.send(bearer_token=caller.token, session_id=session_id, text=req.prompt, data={
                    "portfolio": portfolio, "question": req.prompt,
                    "policy_context": [p["text"] for p in passages] + [f"Client constraint: {c}" for c in constraints]})
            except A2AError as exc:
                raise DownstreamError("market_analyst", str(exc), exc.status if exc.status in (401, 403) else 502) from exc
            analysis = artifact_data(task)

        with internal_span("orchestrator.quant"):
            if req.proposed_trades is not None:
                trades, source = req.proposed_trades, "advisor"
            else:
                rebalance = self._tool(gw, TOOL_REBALANCE, {"portfolio": portfolio,
                                                            "target_weights": analysis["target_weights"]})
                trades, source = rebalance["trades"], "quant_rebalance"
            tax = self._tool(gw, TOOL_TAX, {"portfolio": portfolio, "trades": trades})

        with internal_span("orchestrator.compliance_review") as cspan:
            compliance = self._compliance(gw, portfolio, trades, caller)
            cspan.set_attribute("compliance.verdict", compliance["status"])

        analytics = self._analytics(gw, portfolio, trades)

        facts = {
            "client": {"client_id": client_id, "name": portfolio.get("client_name"), "risk_profile": portfolio["risk_profile"],
                       "esg_mandate": portfolio.get("esg_mandate"), "total_value": portfolio.get("total_portfolio_value")},
            "persistent_constraints": constraints,
            "analyst": {k: analysis.get(k) for k in ("observations", "target_weights", "target_allocation", "commentary")},
            "proposed_trades": trades, "trade_source": source,
            "tax": {k: tax.get(k) for k in ("estimated_tax", "tax_drag_pct_of_portfolio", "realized_gain_long_term",
                                            "realized_gain_short_term")},
            "compliance": {"status": compliance["status"], "findings": compliance.get("findings", []),
                           "reviewed_by": compliance.get("reviewed_by")},
            "analytics": analytics,
            "policy_citations": [{"source": p.get("source"), "score": p.get("score")} for p in passages],
        }
        with internal_span("orchestrator.compose"):
            recommendation, narrative_source, orchestrator_prompt = self._narrative(facts, req.prompt)
        output_check = self._screen(recommendation, "OUTPUT")
        if output_check is not None and output_check.intervened:
            # Never return a recommendation the guardrail blocked; fall back to the deterministic facts summary.
            recommendation, narrative_source = self._fallback_narrative(facts), "guardrail_fallback"

        if self.memory is not None:
            with internal_span("orchestrator.memory.save"):
                try:
                    self.memory.add_turns(client_id, session_id, [("USER", req.prompt), ("ASSISTANT", recommendation)])
                except Exception as exc:
                    log.warning("memory save failed: %s", exc)
                    span.add_event("memory_save_failed", {"error": type(exc).__name__})

        violation = compliance["status"] == "REJECTED"
        span.set_attribute("compliance.violation", violation)
        return {
            "session_id": session_id, "trace_id": current_trace_id(), "advisor_id": caller.advisor_id,
            "recommendation": recommendation, "narrative_source": narrative_source,
            "compliance_violation": violation, **facts,
            "memory": {"recent_turns": len(memory.recent_turns), "constraints_applied": len(constraints)},
            "guardrail": self._guardrail_report(input_check, output_check),
            # which managed prompt version produced each model call
            "system_prompts": {"orchestrator": orchestrator_prompt, "market_analyst": analysis.get("system_prompt") or {}},
        }


def build_orchestrator() -> PortfolioOrchestrator:
    s = get_settings()
    if not s.gateway_url or not s.market_analyst_url:
        raise RuntimeError("GATEWAY_URL and MARKET_ANALYST_URL must be set")

    def gateway_factory(token: str) -> GatewayClient:
        client = GatewayClient(s.gateway_url, token)
        client.initialize()
        return client

    return PortfolioOrchestrator(
        gateway_factory=gateway_factory,
        analyst=A2AClient(s.market_analyst_url, agent_name="market_analyst"),
        llm=BedrockConverseLLM(),
        memory=AgentCoreMemory.from_settings() if s.memory_id else None,
        guardrail=BedrockGuardrail.from_settings(),
        prompts=PromptStore.from_settings(PROMPT_NAME),
    )


def create_app(orchestrator: PortfolioOrchestrator | None = None, validator: JwtValidator | None = None) -> Starlette:
    tracer = configure_tracing(SERVICE)
    orch = orchestrator or build_orchestrator()
    validator = validator or JwtValidator.from_settings()

    async def invocations(request: Request) -> JSONResponse:
        headers = {k.lower(): v for k, v in request.headers.items()}
        session_id = headers.get(SESSION_HEADER.lower()) or f"session-{uuid.uuid4()}"
        with server_span("orchestrator.invoke", extract_context(headers),
                         {"http.request.method": "POST", "url.path": "/invocations"}, tracer=tracer) as span:
            try:
                req = AdvisorRequest.parse(await request.json())
                caller = validator.validate_headers(headers)
            except ValueError as exc:
                return JSONResponse({"error": "invalid_request", "message": str(exc)}, status_code=400)
            except AuthenticationError as exc:
                span.set_attribute("auth.outcome", "unauthenticated")
                return JSONResponse({"error": "unauthenticated", "message": str(exc)}, status_code=401)

            def run() -> dict[str, Any]:
                with with_baggage(advisor_id=caller.advisor_id, session_id=session_id):
                    return orch.run(caller, session_id, req)

            try:
                result = await run_in_threadpool(run)
            except AuthorizationError as exc:
                return JSONResponse({"error": "forbidden", "message": str(exc), "trace_id": current_trace_id()},
                                    status_code=403)
            except DownstreamError as exc:
                span.set_attribute("error.component", exc.component)
                return JSONResponse({"error": "downstream_failure", "component": exc.component, "message": exc.detail,
                                     "trace_id": current_trace_id()}, status_code=exc.status)
            span.set_attribute("http.response.status_code", 200)
            return JSONResponse(result)

    return Starlette(routes=[Route("/invocations", invocations, methods=["POST"]), ping_route])


if __name__ == "__main__":
    serve(create_app, port=8080)
