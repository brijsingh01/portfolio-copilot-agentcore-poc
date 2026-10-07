"""Quant tools: rebalance trade generation and tax-drag estimation."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.common.portfolio import Portfolio, Trade, instrument


class ToolInputError(ValueError):
    """Bad tool arguments; reported as an MCP tool result with isError=true."""


_PORTFOLIO_SCHEMA = {
    "type": "object",
    "description": "ClientPortfolios record: client_id, risk_profile, cash_balance, holdings[{ticker, shares, "
                   "current_price, avg_cost, asset_class}], esg_mandate",
    "required": ["client_id", "risk_profile", "holdings"],
}
_TRADES_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "required": ["ticker", "action", "shares"],
              "properties": {"ticker": {"type": "string"}, "action": {"type": "string", "enum": ["BUY", "SELL"]},
                             "shares": {"type": "number", "exclusiveMinimum": 0}, "price": {"type": "number"},
                             "holding_days": {"type": "integer", "minimum": 0}}},
}


def _portfolio(args: Mapping[str, Any]) -> Portfolio:
    if not isinstance(args.get("portfolio"), Mapping):
        raise ToolInputError("`portfolio` object is required")
    try:
        return Portfolio.from_dict(args["portfolio"])
    except ValueError as exc:
        raise ToolInputError(str(exc)) from exc


def _price(pf: Portfolio, ticker: str) -> float:
    if ticker in pf.positions:
        return pf.positions[ticker].price
    ref = instrument(ticker)
    if ref is None:
        raise ToolInputError(f"no price available for {ticker}")
    return ref.price


def calculate_rebalance(args: Mapping[str, Any]) -> dict[str, Any]:
    pf = _portfolio(args)
    raw_targets = args.get("target_weights")
    if not isinstance(raw_targets, Mapping) or not raw_targets:
        raise ToolInputError("`target_weights` {ticker: weight} is required")
    targets = {str(t).upper(): float(w) for t, w in raw_targets.items()}
    if any(w < 0 or w > 1 for w in targets.values()) or sum(targets.values()) > 1.0001:
        raise ToolInputError("target weights must be within [0,1] and sum to <= 1 (remainder is cash)")
    min_trade = float(args.get("min_trade_value", 100.0))
    total = pf.total_value

    sells, buys = [], []
    for ticker in sorted(set(pf.positions) | set(targets)):
        price = _price(pf, ticker)
        diff = targets.get(ticker, 0.0) * total - (pf.positions[ticker].market_value if ticker in pf.positions else 0.0)
        if abs(diff) < min_trade:
            continue
        if diff < 0:  # round sells up so the post-trade weight lands at or under target
            shares = min(pf.positions[ticker].shares, math.ceil(-diff / price - 1e-9))
            if shares > 0:  # float noise around zero must never yield a 0-share order
                sells.append(Trade(ticker, "SELL", float(shares), price))
        else:
            shares = math.floor(diff / price + 1e-9)
            if shares:
                buys.append(Trade(ticker, "BUY", float(shares), price))

    available = pf.cash + sum(t.value for t in sells)
    needed = sum(t.value for t in buys)
    if needed > available and needed:
        scale = available / needed
        buys = [Trade(t.ticker, "BUY", float(math.floor(t.shares * scale)), t.price) for t in buys]
        buys = [t for t in buys if t.shares]
    trades = sells + buys
    after = pf.apply_trades(trades)
    return {
        "client_id": pf.client_id,
        "trades": [t.to_dict() for t in trades],
        "turnover_pct": round(sum(t.value for t in trades) / total, 4) if total else 0.0,
        "pre_trade": {"allocation": _round(pf.allocation()), "weights": _round(pf.weights())},
        "post_trade": {"allocation": _round(after.allocation()), "weights": _round(after.weights()),
                       "cash_balance": after.cash, "portfolio": after.to_dict()},
    }


def estimate_tax_drag(args: Mapping[str, Any]) -> dict[str, Any]:
    pf = _portfolio(args)
    raw_trades = args.get("trades")
    if not isinstance(raw_trades, list):
        raise ToolInputError("`trades` list is required")
    lt_rate = float(args.get("long_term_rate", 0.15))
    st_rate = float(args.get("short_term_rate", 0.37))
    state_rate = float(args.get("state_rate", 0.0))
    if not all(0 <= r <= 0.6 for r in (lt_rate, st_rate, state_rate)):
        raise ToolInputError("tax rates must be between 0 and 0.6")

    lines, lt_gain, st_gain, proceeds = [], 0.0, 0.0, 0.0
    for raw in raw_trades:
        try:
            trade = Trade.from_dict(raw, pf.prices())
        except (ValueError, KeyError) as exc:
            raise ToolInputError(str(exc)) from exc
        if trade.action != "SELL":
            continue
        held = pf.positions.get(trade.ticker)
        if held is None or trade.shares > held.shares + 1e-9:
            raise ToolInputError(f"cannot sell {trade.shares} {trade.ticker}")
        gain = round((trade.price - held.avg_cost) * trade.shares, 2)
        long_term = int(raw.get("holding_days", 366)) > 365
        lt_gain += gain if long_term else 0.0
        st_gain += 0.0 if long_term else gain
        proceeds += trade.value
        lines.append({"ticker": trade.ticker, "shares": trade.shares, "proceeds": trade.value,
                      "cost_basis": round(held.avg_cost * trade.shares, 2), "realized_gain": gain,
                      "term": "long" if long_term else "short"})

    # Simplified IRS netting: a net loss in one term offsets gains in the other; leftovers carry forward.
    net_st, net_lt = st_gain, lt_gain
    if net_st < 0 < net_lt:
        net_lt, net_st = max(0.0, net_lt + net_st), 0.0
    elif net_lt < 0 < net_st:
        net_st, net_lt = max(0.0, net_st + net_lt), 0.0
    tax = max(net_lt, 0) * (lt_rate + state_rate) + max(net_st, 0) * (st_rate + state_rate)
    total = pf.total_value
    return {
        "client_id": pf.client_id,
        "sells": lines,
        "realized_gain_long_term": round(lt_gain, 2),
        "realized_gain_short_term": round(st_gain, 2),
        "estimated_tax": round(tax, 2),
        "tax_drag_pct_of_portfolio": round(tax / total, 5) if total else 0.0,
        "tax_drag_pct_of_proceeds": round(tax / proceeds, 5) if proceeds else 0.0,
        "assumptions": ["average-cost basis per position (no lot-level data)",
                        "holding period > 365 days unless `holding_days` given",
                        f"rates: LT {lt_rate:.0%}, ST {st_rate:.0%}, state {state_rate:.1%}"],
    }




def _round(d: Mapping[str, float]) -> dict[str, float]:
    return {k: round(v, 4) for k, v in d.items()}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[[Mapping[str, Any]], dict[str, Any]]

    def descriptor(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema,
                "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}}


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("calculate_rebalance",
         "Generate whole-share BUY/SELL trades that move a portfolio to target weights (remainder stays cash). "
         "Returns trades plus pre/post-trade allocation.",
         {"type": "object", "required": ["portfolio", "target_weights"],
          "properties": {"portfolio": _PORTFOLIO_SCHEMA,
                         "target_weights": {"type": "object", "additionalProperties": {"type": "number"}},
                         "min_trade_value": {"type": "number", "minimum": 0, "default": 100}}},
         calculate_rebalance),
    Tool("estimate_tax_drag",
         "Estimate realized capital gains tax and tax drag for proposed SELL trades using average cost basis.",
         {"type": "object", "required": ["portfolio", "trades"],
          "properties": {"portfolio": _PORTFOLIO_SCHEMA, "trades": _TRADES_SCHEMA,
                         "long_term_rate": {"type": "number", "default": 0.15},
                         "short_term_rate": {"type": "number", "default": 0.37},
                         "state_rate": {"type": "number", "default": 0.0}}},
         estimate_tax_drag),
)}
