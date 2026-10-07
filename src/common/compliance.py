"""Deterministic IPS compliance engine. No LLM: verdicts must be reproducible and auditable.

`review()` is the entry point used by the compliance-rules Gateway tool (Lambda target).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from src.common import policy
from src.common.portfolio import EQUITY_SINGLE, Portfolio, Trade
from src.common.tracer import internal_span

VIOLATION = "VIOLATION"
WARNING = "WARNING"
_EPS = 1e-9


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    message: str
    subject: str
    observed: float | None = None
    limit: float | None = None
    policy_ref: str = ""

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def evaluate(pf: Portfolio, *, post_trade: bool) -> list[Finding]:
    """`post_trade=True` applies the hard 10% ceiling; current holdings use the 12% remediation trigger."""
    lim = policy.limits_for(pf.risk_profile)
    alloc = pf.allocation()
    out: list[Finding] = []

    def band(rule: str, name: str, value: float, lo: float, hi: float, ref: str) -> None:
        if value < lo - _EPS:
            out.append(Finding(f"{rule}_MIN", VIOLATION, f"{name} {value:.1%} below {pf.risk_profile} minimum {lo:.0%}",
                               "portfolio", value, lo, ref))
        if value > hi + _EPS:
            out.append(Finding(f"{rule}_MAX", VIOLATION, f"{name} {value:.1%} above {pf.risk_profile} maximum {hi:.0%}",
                               "portfolio", value, hi, ref))

    band("EQUITY", "Equity", alloc["equity"], lim.equity_min, lim.equity_max, "IPS §1")
    band("FIXED_INCOME", "Fixed income", alloc["fixed_income"], lim.fixed_income_min, lim.fixed_income_max, "IPS §1")
    band("CASH", "Cash", alloc["cash"], lim.cash_min, lim.cash_max, "IPS §1")
    if lim.idle_cash_threshold is not None and alloc["cash"] > lim.idle_cash_threshold + _EPS:
        out.append(Finding("IDLE_CASH", WARNING, f"Idle cash {alloc['cash']:.1%} should be deployed into diversified funds",
                           "portfolio", alloc["cash"], lim.idle_cash_threshold, "IPS §4"))

    for p in pf.positions.values():
        w = pf.weight(p.ticker)
        if p.bucket == EQUITY_SINGLE:
            if post_trade and w > policy.SINGLE_STOCK_MAX + _EPS:
                out.append(Finding("SINGLE_STOCK_LIMIT", VIOLATION, f"{p.ticker} would be {w:.1%} after trading",
                                   p.ticker, w, policy.SINGLE_STOCK_MAX, "IPS §2"))
            elif not post_trade and w > policy.SINGLE_STOCK_REMEDIATION + _EPS:
                out.append(Finding("SINGLE_STOCK_REMEDIATION", VIOLATION,
                                   f"{p.ticker} at {w:.1%} breaches the 12% drift trigger; rebalance within 30 business days",
                                   p.ticker, w, policy.SINGLE_STOCK_REMEDIATION, "IPS §2"))
            elif not post_trade and w > policy.SINGLE_STOCK_MAX + _EPS:
                out.append(Finding("SINGLE_STOCK_LIMIT", WARNING, f"{p.ticker} at {w:.1%} exceeds the 10% ceiling",
                                   p.ticker, w, policy.SINGLE_STOCK_MAX, "IPS §2"))
        if pf.esg_mandate:
            banned = policy.ESG_EXCLUDED_FLAGS.intersection(p.esg_flags)
            if banned:
                out.append(Finding("ESG_EXCLUSION", VIOLATION,
                                   f"{p.ticker} is prohibited under the ESG mandate ({', '.join(sorted(banned))})",
                                   p.ticker, w, 0.0, "IPS §3"))
            elif not p.known:
                out.append(Finding("ESG_UNVERIFIED", WARNING, f"{p.ticker} has no ESG screening data",
                                   p.ticker, w, None, "IPS §3"))

    if lim.sector_max_of_equity:
        for sector, share in pf.sector_share_of_equity().items():
            if share > lim.sector_max_of_equity + _EPS:
                out.append(Finding("SECTOR_CONCENTRATION", VIOLATION,
                                   f"{sector} is {share:.1%} of equity (max {lim.sector_max_of_equity:.0%})",
                                   sector, share, lim.sector_max_of_equity, "IPS §2"))
    return out


def verdict(findings: list[Finding]) -> str:
    if any(f.severity == VIOLATION for f in findings):
        return "REJECTED"
    return "APPROVED_WITH_WARNINGS" if findings else "APPROVED"


class InvalidReviewInput(ValueError):
    """Malformed portfolio or trade list."""


def review(raw_portfolio: object, raw_trades: object, *, requested_by: str | None = None) -> dict:
    """Evaluate current holdings (no trades) or the post-trade portfolio against the IPS.

    A pure function of its inputs; `requested_by` is recorded for audit only.
    """
    if not isinstance(raw_portfolio, dict):
        raise InvalidReviewInput("`portfolio` must be an object")
    if raw_trades is not None and not isinstance(raw_trades, list):
        raise InvalidReviewInput("`proposed_trades` must be a list")
    try:
        current = Portfolio.from_dict(raw_portfolio)
        policy.limits_for(current.risk_profile)
        trades = [Trade.from_dict(t, current.prices()) for t in raw_trades or []]
        evaluated = current.apply_trades(trades) if trades else current
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidReviewInput(str(exc)) from exc

    with internal_span("compliance.evaluate", {"copilot.client_id": current.client_id,
                                               "compliance.trades": len(trades)}) as span:
        findings = evaluate(evaluated, post_trade=bool(trades))
        status = verdict(findings)
        span.set_attribute("compliance.verdict", status)
        span.set_attribute("compliance.violations", sum(f.severity == VIOLATION for f in findings))

    return {
        "status": status,
        "policy_id": policy.POLICY_ID,
        "evaluated": "post_trade" if trades else "current_holdings",
        "client_id": current.client_id,
        "risk_profile": current.risk_profile,
        "esg_mandate": current.esg_mandate,
        "allocation": {k: round(v, 4) for k, v in evaluated.allocation().items()},
        "findings": [f.to_dict() for f in findings],
        "reviewed_by": requested_by,
    }
