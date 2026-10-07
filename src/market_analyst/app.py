"""A2A Server 1 - Market Analyst (AgentCore Runtime, 0.0.0.0:9000).

Computes deterministic position/allocation analytics, proposes IPS-aware target weights for the
Quant MCP `calculate_rebalance` tool, and adds a short Amazon Nova commentary grounded on those facts.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry import trace

from src.common import policy
from src.common.a2a import AgentSpec, InvalidInput, SkillRequest, SkillResult, create_a2a_app
from src.common.identity import JwtValidator
from src.common.llm import LLM, BedrockConverseLLM
from src.common.portfolio import EQUITY_FUND, EQUITY_SINGLE, FIXED_INCOME, Portfolio, bucket_for, instrument
from src.common.prompts import PromptStore
from src.common.server import serve
from src.common.tracer import configure_tracing, internal_span

SERVICE = "market-analyst"
log = logging.getLogger(SERVICE)

PROMPT_NAME = "market_analyst_system"  # Bedrock Prompt Management (pinned version) or src/prompts/<name>.txt


def _esg_blocked(p_flags: tuple[str, ...], esg_mandate: bool) -> bool:
    return esg_mandate and bool(policy.ESG_EXCLUDED_FLAGS.intersection(p_flags))


def position_metrics(pf: Portfolio) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(pf.positions.values(), key=lambda p: -p.market_value):
        ref = instrument(p.ticker)
        rows.append({
            "ticker": p.ticker, "asset_class": p.asset_class, "sector": p.sector,
            "market_value": p.market_value, "weight": round(pf.weight(p.ticker), 4),
            "unrealized_gain_pct": round((p.price / p.avg_cost - 1) if p.avg_cost else 0.0, 4),
            "beta": ref.beta if ref else None, "dividend_yield": ref.dividend_yield if ref else None,
            "esg_flags": list(p.esg_flags),
        })
    return rows


def observations(pf: Portfolio) -> list[str]:
    lim = policy.limits_for(pf.risk_profile)
    alloc = pf.allocation()
    notes = []
    if lim.idle_cash_threshold is not None and alloc["cash"] > lim.idle_cash_threshold:
        notes.append(f"Idle cash {alloc['cash']:.1%} exceeds the {lim.idle_cash_threshold:.0%} deployment threshold.")
    if alloc["fixed_income"] < lim.fixed_income_min:
        notes.append(f"Fixed income {alloc['fixed_income']:.1%} is below the {lim.fixed_income_min:.0%} minimum.")
    if alloc["equity"] > lim.equity_max:
        notes.append(f"Equity {alloc['equity']:.1%} exceeds the {lim.equity_max:.0%} maximum.")
    for t, w in pf.weights().items():
        if pf.positions[t].bucket == EQUITY_SINGLE and w > policy.SINGLE_STOCK_MAX:
            notes.append(f"{t} is {w:.1%} of the portfolio, above the {policy.SINGLE_STOCK_MAX:.0%} single-stock ceiling.")
    if lim.sector_max_of_equity:
        for sector, share in pf.sector_share_of_equity().items():
            if share > lim.sector_max_of_equity:
                notes.append(f"{sector} single stocks are {share:.1%} of equity (cap {lim.sector_max_of_equity:.0%}).")
    for p in pf.positions.values():
        if _esg_blocked(p.esg_flags, pf.esg_mandate):
            notes.append(f"{p.ticker} conflicts with the ESG mandate ({', '.join(p.esg_flags)}).")
    return notes


def target_weights(pf: Portfolio) -> dict[str, float]:
    """Model allocation for the risk profile that respects single-stock, sector and ESG limits."""
    lim = policy.limits_for(pf.risk_profile)
    eq_t, fi_t = lim.target_equity, lim.target_fixed_income
    weights = pf.weights()

    singles = {t: min(w, policy.SINGLE_STOCK_TARGET_CAP) for t, w in weights.items()
               if pf.positions[t].bucket == EQUITY_SINGLE and not _esg_blocked(pf.positions[t].esg_flags, pf.esg_mandate)}
    if lim.sector_max_of_equity:
        sector_cap = lim.sector_max_of_equity * eq_t * 0.95
        by_sector: dict[str, list[str]] = {}
        for t in singles:
            by_sector.setdefault(pf.positions[t].sector, []).append(t)
        for tickers in by_sector.values():
            total = sum(singles[t] for t in tickers)
            if total > sector_cap:
                for t in tickers:
                    singles[t] *= sector_cap / total
    single_total = sum(singles.values())
    if single_total > eq_t * 0.5:  # keep at least half of equity in diversified funds
        singles = {t: w * eq_t * 0.5 / single_total for t, w in singles.items()}
        single_total = eq_t * 0.5

    def funds(bucket: str, budget: float, core: str) -> dict[str, float]:
        held = {t: w for t, w in weights.items() if pf.positions[t].bucket == bucket
                and not _esg_blocked(pf.positions[t].esg_flags, pf.esg_mandate)}
        held_total = sum(held.values())
        if held_total >= budget and held_total > 0:
            return {t: w * budget / held_total for t, w in held.items()}
        out = dict(held)
        out[core] = out.get(core, 0.0) + budget - held_total
        return out

    eq_core = policy.ESG_CORE_EQUITY_FUND if pf.esg_mandate else policy.CORE_EQUITY_FUND
    fi_core = policy.ESG_CORE_BOND_FUND if pf.esg_mandate else policy.CORE_BOND_FUND
    result = {**singles, **funds(EQUITY_FUND, eq_t - single_total, eq_core), **funds(FIXED_INCOME, fi_t, fi_core)}
    return {t: round(w, 4) for t, w in sorted(result.items()) if w > 0.00005}


def target_allocation(pf: Portfolio, targets: dict[str, float]) -> dict[str, float]:
    alloc = {"equity": 0.0, "fixed_income": 0.0}
    for t, w in targets.items():
        held = pf.positions.get(t)
        bucket = held.bucket if held else bucket_for(instrument(t).asset_class)
        alloc["fixed_income" if bucket == FIXED_INCOME else "equity"] += w
    return {**{k: round(v, 4) for k, v in alloc.items()}, "cash": round(1 - sum(targets.values()), 4)}


def make_handler(llm: LLM, prompts: PromptStore):
    def handle(req: SkillRequest) -> SkillResult:
        portfolio_raw = req.data.get("portfolio")
        if not isinstance(portfolio_raw, dict):
            raise InvalidInput("data part must include a `portfolio` object")
        try:
            pf = Portfolio.from_dict(portfolio_raw)
            policy.limits_for(pf.risk_profile)
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        req.caller.authorize(advisor_id=pf.advisor_id, client_id=pf.client_id)
        question = req.data.get("question") or req.text or "Review the portfolio and propose a rebalance."

        with internal_span("market_analyst.analyze", {"copilot.client_id": pf.client_id}):
            targets = target_weights(pf)
            facts = {
                "client_id": pf.client_id, "risk_profile": pf.risk_profile, "esg_mandate": pf.esg_mandate,
                "total_value": pf.total_value,
                "current_allocation": {k: round(v, 4) for k, v in pf.allocation().items()},
                "positions": position_metrics(pf), "observations": observations(pf),
                "target_weights": targets,
                "target_allocation": target_allocation(pf, targets),
                "policy_context": req.data.get("policy_context", [])[:5],
            }
        prompt_info: dict[str, str] = {}
        try:
            system = prompts.get()
            prompt_info = system.describe()
            trace.get_current_span().set_attributes(system.span_attributes())
            commentary = llm.complete(system.text,f"Advisor question: {question}\n\nFacts:\n{json.dumps(facts)}",
                                      max_tokens=400, temperature=0.2)
            source = "llm"
        except Exception as exc:  # degrade to deterministic notes; span already records the Bedrock error
            log.warning("commentary generation failed: %s", exc)
            trace.get_current_span().add_event("commentary_fallback", {"error": type(exc).__name__})
            commentary, source = " ".join(facts["observations"]) or "Portfolio is within model ranges.", "fallback"

        data = {**facts, "commentary": commentary, "commentary_source": source, "system_prompt": prompt_info,
                "data_as_of": "synthetic reference data (data/market_reference.json)"}
        summary = (f"Proposed {len(targets)}-line target allocation for {pf.client_id}; "
                   f"{len(facts['observations'])} observation(s). {commentary}")
        return SkillResult(summary=summary, data=data, artifact_name="market_analysis")

    return handle


def build_spec(llm: LLM, prompts: PromptStore) -> AgentSpec:
    return AgentSpec(
        name="Market Analyst",
        description="Analyzes client holdings, flags drift and concentration, and proposes IPS-aware target weights.",
        version="1.0.0",
        tags={"copilot.agent": SERVICE},
        handler=make_handler(llm, prompts),
        skills=[{
            "id": "portfolio_market_analysis",
            "name": "Portfolio market analysis",
            "description": "Input: data part {portfolio, question?, policy_context?}. Output: position metrics, "
                           "observations, target_weights for rebalancing and an analyst commentary.",
            "tags": ["portfolio", "allocation", "rebalance", "market"],
            "examples": ["Review CLIENT-8801 and propose how to deploy idle cash."],
            "inputModes": ["application/json", "text/plain"],
            "outputModes": ["application/json", "text/plain"],
        }],
    )


def create_app(validator: JwtValidator | None = None, llm: LLM | None = None, prompts: PromptStore | None = None):
    configure_tracing(SERVICE)
    return create_a2a_app(build_spec(llm or BedrockConverseLLM(), prompts or PromptStore.from_settings(PROMPT_NAME)),
                          validator)


if __name__ == "__main__":
    serve(create_app, port=9000)
