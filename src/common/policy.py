"""Codified limits from data/investment_policy.md (Apex Wealth IPS).

The Knowledge Base holds the authoritative prose; these constants are the machine-enforceable
subset shared by the Market Analyst (target construction), the Quant MCP tools and the compliance rules.
"""
from __future__ import annotations

from dataclasses import dataclass

POLICY_ID = "APEX-IPS-2026"


@dataclass(frozen=True)
class RiskLimits:
    equity_min: float
    equity_max: float
    fixed_income_min: float
    fixed_income_max: float
    cash_min: float
    cash_max: float
    sector_max_of_equity: float | None
    idle_cash_threshold: float | None
    # Model-portfolio targets used when proposing a rebalance (inside the limits above).
    target_equity: float
    target_fixed_income: float
    target_cash: float


RISK_LIMITS: dict[str, RiskLimits] = {
    "Conservative": RiskLimits(0.20, 0.40, 0.50, 0.70, 0.05, 0.15, None, None, 0.30, 0.60, 0.10),
    "Moderate": RiskLimits(0.00, 0.65, 0.25, 1.00, 0.05, 0.20, 0.30, 0.15, 0.60, 0.33, 0.07),
    "Aggressive": RiskLimits(0.60, 0.90, 0.10, 0.30, 0.02, 1.00, None, None, 0.80, 0.17, 0.03),
}

SINGLE_STOCK_MAX = 0.10           # ceiling at time of rebalancing
SINGLE_STOCK_REMEDIATION = 0.12   # organic drift trigger (flag + plan within 30 business days)
SINGLE_STOCK_TARGET_CAP = 0.085   # proposal headroom under the 10% ceiling
ESG_EXCLUDED_FLAGS = frozenset({"weapons", "thermal_coal", "tobacco"})

ESG_CORE_EQUITY_FUND = "ESGV"
ESG_CORE_BOND_FUND = "EAGG"
CORE_EQUITY_FUND = "VOO"
CORE_BOND_FUND = "BND"


def limits_for(risk_profile: str) -> RiskLimits:
    try:
        return RISK_LIMITS[risk_profile]
    except KeyError as exc:
        raise ValueError(f"unknown risk_profile {risk_profile!r}; expected one of {sorted(RISK_LIMITS)}") from exc
