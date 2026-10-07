"""Portfolio domain model shared by the agents, MCP tools and Lambda targets."""
from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

EQUITY_SINGLE = "equity_single"
EQUITY_FUND = "equity_fund"
FIXED_INCOME = "fixed_income"

_BUCKETS = {
    "equity": EQUITY_SINGLE,
    "stock": EQUITY_SINGLE,
    "index etf": EQUITY_FUND,
    "equity etf": EQUITY_FUND,
    "mutual fund": EQUITY_FUND,
    "bond etf": FIXED_INCOME,
    "fixed income": FIXED_INCOME,
    "bond": FIXED_INCOME,
}
_DEFAULT_REFERENCE = Path(__file__).resolve().parents[2] / "data" / "market_reference.json"


def bucket_for(asset_class: str) -> str:
    try:
        return _BUCKETS[asset_class.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unsupported asset_class {asset_class!r}") from exc


@dataclass(frozen=True)
class Instrument:
    ticker: str
    name: str
    asset_class: str
    sector: str
    price: float
    beta: float = 1.0
    dividend_yield: float = 0.0
    esg_flags: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def reference_data() -> dict[str, Instrument]:
    path = Path(os.getenv("MARKET_REFERENCE_PATH", str(_DEFAULT_REFERENCE)))
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    return {i["ticker"]: Instrument(**{**i, "esg_flags": tuple(i.get("esg_flags", ()))}) for i in raw["instruments"]}


def instrument(ticker: str) -> Instrument | None:
    return reference_data().get(ticker.upper())


@dataclass(frozen=True)
class Position:
    ticker: str
    shares: float
    price: float
    avg_cost: float
    asset_class: str
    sector: str = "Unknown"
    esg_flags: tuple[str, ...] = ()
    known: bool = True  # present in reference data (ESG screen verified)

    @property
    def market_value(self) -> float:
        return round(self.shares * self.price, 2)

    @property
    def bucket(self) -> str:
        return bucket_for(self.asset_class)

    @classmethod
    def build(cls, ticker: str, shares: float, *, price: float | None = None, avg_cost: float | None = None,
              asset_class: str | None = None) -> Position:
        ticker = ticker.upper()
        ref = instrument(ticker)
        px = price if price is not None else (ref.price if ref else None)
        cls_name = asset_class or (ref.asset_class if ref else None)
        if px is None or px <= 0 or not cls_name:
            raise ValueError(f"no price/asset_class available for {ticker}")
        bucket_for(cls_name)
        return cls(ticker=ticker, shares=float(shares), price=float(px),
                   avg_cost=float(avg_cost if avg_cost is not None else px), asset_class=cls_name,
                   sector=ref.sector if ref else "Unknown", esg_flags=ref.esg_flags if ref else (), known=ref is not None)


@dataclass(frozen=True)
class Trade:
    ticker: str
    action: str  # BUY | SELL
    shares: float
    price: float

    @property
    def value(self) -> float:
        return round(self.shares * self.price, 2)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], price_lookup: Mapping[str, float]) -> Trade:
        ticker = str(d["ticker"]).upper()
        action = str(d["action"]).upper()
        shares = float(d["shares"])
        if action not in ("BUY", "SELL") or shares <= 0:
            raise ValueError(f"invalid trade {dict(d)}")
        price = d.get("price") or price_lookup.get(ticker) or (instrument(ticker).price if instrument(ticker) else None)
        if not price:
            raise ValueError(f"no price for {ticker}")
        return cls(ticker, action, shares, float(price))

    def to_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "action": self.action, "shares": self.shares, "price": self.price,
                "est_value": self.value}


@dataclass(frozen=True)
class Portfolio:
    client_id: str
    risk_profile: str
    esg_mandate: bool
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    advisor_id: str | None = None
    client_name: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Portfolio:
        """Accepts the `ClientPortfolios` item shape (see data/seed_portfolios.json)."""
        try:
            positions: dict[str, Position] = {}
            for h in d.get("holdings", []):
                price = h.get("current_price")
                if price is None and h.get("market_value") is not None and h.get("shares"):
                    price = float(h["market_value"]) / float(h["shares"])
                p = Position.build(h["ticker"], float(h["shares"]), price=None if price is None else float(price),
                                   avg_cost=None if h.get("avg_cost") is None else float(h["avg_cost"]),
                                   asset_class=h.get("asset_class"))
                positions[p.ticker] = p
            cash = float(d.get("cash_balance", d.get("cash", 0.0)))
            if cash < 0:
                raise ValueError("cash_balance cannot be negative")
            return cls(client_id=str(d["client_id"]), risk_profile=str(d["risk_profile"]),
                       esg_mandate=bool(d.get("esg_mandate", d.get("esg", False))), cash=cash, positions=positions,
                       advisor_id=d.get("advisor_id"), client_name=d.get("client_name"))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"malformed portfolio: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id, "advisor_id": self.advisor_id, "client_name": self.client_name,
            "risk_profile": self.risk_profile, "esg_mandate": self.esg_mandate,
            "cash_balance": round(self.cash, 2), "total_portfolio_value": self.total_value,
            "holdings": [{"ticker": p.ticker, "asset_class": p.asset_class, "shares": p.shares, "avg_cost": p.avg_cost,
                          "current_price": p.price, "market_value": p.market_value} for p in self.positions.values()],
        }

    @property
    def total_value(self) -> float:
        return round(self.cash + sum(p.market_value for p in self.positions.values()), 2)

    def weight(self, ticker: str) -> float:
        p = self.positions.get(ticker.upper())
        return p.market_value / self.total_value if p and self.total_value else 0.0

    def weights(self) -> dict[str, float]:
        return {t: self.weight(t) for t in self.positions}

    def allocation(self) -> dict[str, float]:
        total = self.total_value or 1.0
        eq = sum(p.market_value for p in self.positions.values() if p.bucket in (EQUITY_SINGLE, EQUITY_FUND))
        fi = sum(p.market_value for p in self.positions.values() if p.bucket == FIXED_INCOME)
        return {"equity": eq / total, "fixed_income": fi / total, "cash": self.cash / total}

    def sector_share_of_equity(self) -> dict[str, float]:
        """Single-stock sector exposure as a share of total equity (funds count as diversified)."""
        equity = sum(p.market_value for p in self.positions.values() if p.bucket in (EQUITY_SINGLE, EQUITY_FUND))
        out: dict[str, float] = {}
        for p in self.positions.values():
            if p.bucket == EQUITY_SINGLE and equity:
                out[p.sector] = out.get(p.sector, 0.0) + p.market_value / equity
        return out

    def prices(self) -> dict[str, float]:
        return {t: p.price for t, p in self.positions.items()}

    def apply_trades(self, trades: Iterable[Trade]) -> Portfolio:
        """Return the post-trade portfolio (sells settle before buys)."""
        positions = dict(self.positions)
        cash = self.cash
        for t in sorted(trades, key=lambda t: t.action != "SELL"):
            held = positions.get(t.ticker)
            if t.action == "SELL":
                if held is None or t.shares > held.shares + 1e-9:
                    raise ValueError(f"cannot sell {t.shares} {t.ticker}; holding {held.shares if held else 0}")
                remaining = held.shares - t.shares
                positions[t.ticker] = replace(held, shares=remaining)
                if math.isclose(remaining, 0.0, abs_tol=1e-9):
                    del positions[t.ticker]
                cash += t.value
            else:
                if t.value > cash + 0.005:
                    raise ValueError(f"insufficient cash for BUY {t.shares} {t.ticker} (${t.value:,.2f} > ${cash:,.2f})")
                if held:
                    shares = held.shares + t.shares
                    avg = (held.avg_cost * held.shares + t.price * t.shares) / shares
                    positions[t.ticker] = replace(held, shares=shares, avg_cost=round(avg, 4))
                else:
                    positions[t.ticker] = Position.build(t.ticker, t.shares, price=t.price)
                cash -= t.value
        return replace(self, cash=round(cash, 2), positions=positions)
