"""Domain logic: portfolio maths, IPS policy, compliance rules engine, quant tools, seed data."""
import copy
import json

import pytest

from src.market_analyst.app import observations, target_allocation, target_weights
from src.common.compliance import VIOLATION, WARNING, evaluate, verdict
from src.common import policy
from src.common.portfolio import Portfolio, Position, Trade, bucket_for
from src.quant_mcp.tools import ToolInputError, calculate_rebalance, estimate_tax_drag


def pf_dict(risk="Moderate", cash=0.0, holdings=(), esg=False, client="CLIENT-T1"):
    return {"client_id": client, "advisor_id": "ADVISOR-042", "risk_profile": risk, "cash_balance": cash,
            "esg_mandate": esg, "holdings": [dict(h) for h in holdings]}


def h(ticker, shares, price=None, avg=None, asset_class=None):
    d = {"ticker": ticker, "shares": shares}
    if price is not None:
        d["current_price"] = price
    if avg is not None:
        d["avg_cost"] = avg
    if asset_class:
        d["asset_class"] = asset_class
    return d


def rules(findings):
    return {(f.rule_id, f.severity) for f in findings}


# ------------------------------------------------------------------ policy table
@pytest.mark.parametrize("profile", sorted(policy.RISK_LIMITS))
def test_model_targets_sit_inside_limits(profile):
    lim = policy.limits_for(profile)
    assert lim.equity_min <= lim.target_equity <= lim.equity_max
    assert lim.fixed_income_min <= lim.target_fixed_income <= lim.fixed_income_max
    assert lim.cash_min <= lim.target_cash <= lim.cash_max
    assert lim.target_equity + lim.target_fixed_income + lim.target_cash == pytest.approx(1.0)


def test_policy_constants_match_ips_document():
    assert policy.RISK_LIMITS["Moderate"].equity_max == 0.65 and policy.SINGLE_STOCK_MAX == 0.10
    assert policy.SINGLE_STOCK_TARGET_CAP < policy.SINGLE_STOCK_MAX < policy.SINGLE_STOCK_REMEDIATION
    assert {"weapons", "thermal_coal"} <= policy.ESG_EXCLUDED_FLAGS


def test_unknown_risk_profile():
    with pytest.raises(ValueError, match="unknown risk_profile"):
        policy.limits_for("YOLO")


# ------------------------------------------------------------------ portfolio model
def test_bucket_mapping_and_unknown_asset_class():
    assert bucket_for(" Index ETF ") == "equity_fund" and bucket_for("bond etf") == "fixed_income"
    with pytest.raises(ValueError):
        bucket_for("Crypto")


def test_unknown_ticker_requires_price_and_class():
    with pytest.raises(ValueError):
        Position.build("ZZZZ", 10)
    p = Position.build("zzzz", 10, price=5.0, asset_class="Equity")
    assert p.ticker == "ZZZZ" and p.known is False and p.sector == "Unknown"


def test_price_derived_from_market_value():
    pf = Portfolio.from_dict(pf_dict(holdings=[{"ticker": "AAPL", "shares": 10, "market_value": 2000, "asset_class": "Equity"}]))
    assert pf.positions["AAPL"].price == 200.0


@pytest.mark.parametrize("bad", [
    {"risk_profile": "Moderate", "holdings": []},                               # no client_id
    pf_dict(cash=-1),                                                            # negative cash
    pf_dict(holdings=[{"ticker": "AAPL"}]),                                      # no shares
    pf_dict(holdings=[h("AAPL", 1, asset_class="Crypto")]),                     # unsupported class
])
def test_malformed_portfolios(bad):
    with pytest.raises(ValueError):
        Portfolio.from_dict(bad)


def test_empty_portfolio_has_safe_math():
    pf = Portfolio.from_dict(pf_dict())
    assert pf.total_value == 0 and pf.weight("AAPL") == 0.0
    assert pf.allocation() == {"equity": 0.0, "fixed_income": 0.0, "cash": 0.0} and pf.sector_share_of_equity() == {}


def test_roundtrip_to_dict(marcus):
    pf = Portfolio.from_dict(marcus)
    again = Portfolio.from_dict(pf.to_dict())
    assert again.total_value == pf.total_value and again.weights() == pf.weights()


def test_apply_trades_semantics():
    pf = Portfolio.from_dict(pf_dict(cash=100.0, holdings=[h("AAPL", 10, 200.0, 100.0, "Equity")]))
    # sell-before-buy: proceeds fund the buy even though listed second
    after = pf.apply_trades([Trade("VOO", "BUY", 3, 500.0), Trade("AAPL", "SELL", 10, 200.0)])
    assert "AAPL" not in after.positions and after.positions["VOO"].shares == 3 and after.cash == 600.0
    # buying more of an existing line re-weights average cost
    avg = pf.apply_trades([Trade("AAPL", "BUY", 0.5, 200.0)]).positions["AAPL"].avg_cost
    assert avg == pytest.approx((10 * 100 + 0.5 * 200) / 10.5, rel=1e-4)
    with pytest.raises(ValueError, match="cannot sell"):
        pf.apply_trades([Trade("AAPL", "SELL", 11, 200.0)])
    with pytest.raises(ValueError, match="cannot sell"):
        pf.apply_trades([Trade("MSFT", "SELL", 1, 420.0)])
    with pytest.raises(ValueError, match="insufficient cash"):
        pf.apply_trades([Trade("VOO", "BUY", 1, 500.0)])


@pytest.mark.parametrize("raw", [{"ticker": "AAPL", "action": "HOLD", "shares": 1}, {"ticker": "AAPL", "action": "BUY", "shares": 0},
                                 {"ticker": "AAPL", "action": "BUY", "shares": -5}, {"ticker": "NOPE", "action": "BUY", "shares": 1}])
def test_invalid_trades(raw):
    with pytest.raises(ValueError):
        Trade.from_dict(raw, {})


def test_trade_price_resolution_order():
    assert Trade.from_dict({"ticker": "aapl", "action": "buy", "shares": 1, "price": 1.0}, {"AAPL": 2.0}).price == 1.0
    assert Trade.from_dict({"ticker": "AAPL", "action": "BUY", "shares": 1}, {"AAPL": 2.0}).price == 2.0
    assert Trade.from_dict({"ticker": "AAPL", "action": "BUY", "shares": 1}, {}).price == 225.0  # reference data


# ------------------------------------------------------------------ analyst proposal => always compliant
SCENARIOS = {
    "marcus": None,
    "elena": None,
    "conservative_all_cash": pf_dict("Conservative", cash=500_000),
    "moderate_esg_with_banned_names": pf_dict("Moderate", cash=10_000, esg=True,
                                              holdings=[h("LMT", 100), h("BTU", 1000), h("MO", 200), h("VOO", 50)]),
    "aggressive_bond_heavy": pf_dict("Aggressive", cash=1_000, holdings=[h("BND", 2000)]),
    "moderate_non_esg_concentrated": pf_dict("Moderate", cash=5_000,
                                             holdings=[h("JNJ", 600), h("NVDA", 500), h("AAPL", 200), h("MSFT", 100)]),
    "moderate_esg_unknown_ticker": pf_dict("Moderate", cash=50_000, esg=True,
                                           holdings=[h("XYZ", 100, price=50.0, asset_class="Equity")]),
    "conservative_over_equity": pf_dict("Conservative", cash=1_000, holdings=[h("VOO", 300), h("EAGG", 100)]),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_analyst_targets_rebalance_and_pass_compliance(name, marcus, elena):
    raw = {"marcus": marcus, "elena": elena}.get(name) or copy.deepcopy(SCENARIOS[name])
    pf = Portfolio.from_dict(raw)
    targets = target_weights(pf)
    lim = policy.limits_for(pf.risk_profile)
    alloc = target_allocation(pf, targets)
    assert sum(targets.values()) <= 1.0 + 1e-6 and all(w > 0 for w in targets.values())
    assert alloc["equity"] == pytest.approx(lim.target_equity, abs=1e-3)
    if pf.esg_mandate:
        assert not any(policy.ESG_EXCLUDED_FLAGS & set(pf.positions[t].esg_flags) for t in targets if t in pf.positions)
    reb = calculate_rebalance({"portfolio": raw, "target_weights": targets})
    after = pf.apply_trades([Trade.from_dict(t, pf.prices()) for t in reb["trades"]])
    findings = evaluate(after, post_trade=True)
    assert verdict(findings) != "REJECTED", [f.message for f in findings]
    assert after.cash >= 0


def test_esg_flags_raise_observations():
    pf = Portfolio.from_dict(copy.deepcopy(SCENARIOS["moderate_esg_with_banned_names"]))
    notes = " ".join(observations(pf))
    assert "LMT" in notes and "BTU" in notes and "MO" in notes


# ------------------------------------------------------------------ compliance rule boundaries
def single_stock_pf(weight, risk="Moderate"):
    # AAPL at exactly `weight` of a $100k portfolio; remainder balanced to satisfy allocation rules
    aapl = 100_000 * weight
    return pf_dict(risk, cash=7_000, holdings=[h("AAPL", 1, aapl, aapl, "Equity"),
                                                h("VOO", 1, 60_000 - aapl, 1, "Index ETF"),
                                                h("EAGG", 1, 33_000, 1, "Bond ETF")])


def test_single_stock_exactly_at_cap_is_clean():
    findings = evaluate(Portfolio.from_dict(single_stock_pf(0.10)), post_trade=True)
    assert not any(f.rule_id.startswith("SINGLE_STOCK") for f in findings)


def test_single_stock_just_over_cap_post_trade_is_violation():
    assert ("SINGLE_STOCK_LIMIT", VIOLATION) in rules(evaluate(Portfolio.from_dict(single_stock_pf(0.1001)), post_trade=True))


def test_drift_band_between_10_and_12_is_warning_only():
    findings = evaluate(Portfolio.from_dict(single_stock_pf(0.11)), post_trade=False)
    assert ("SINGLE_STOCK_LIMIT", WARNING) in rules(findings) and verdict(findings) == "APPROVED_WITH_WARNINGS"


def test_drift_over_12_requires_remediation():
    assert ("SINGLE_STOCK_REMEDIATION", VIOLATION) in rules(evaluate(Portfolio.from_dict(single_stock_pf(0.125)), post_trade=False))


def test_funds_are_exempt_from_single_stock_rule():
    findings = evaluate(Portfolio.from_dict(pf_dict(cash=7_000, holdings=[h("VOO", 1, 60_000, 1, "Index ETF"),
                                                                          h("EAGG", 1, 33_000, 1, "Bond ETF")])), post_trade=True)
    assert findings == []


def test_esg_rules_only_apply_with_mandate():
    base = pf_dict(cash=7_000, holdings=[h("LMT", 1, 5_000, 1, "Equity"), h("VOO", 1, 55_000, 1, "Index ETF"),
                                         h("EAGG", 1, 33_000, 1, "Bond ETF")])
    assert not any(f.rule_id == "ESG_EXCLUSION" for f in evaluate(Portfolio.from_dict(base), post_trade=True))
    base["esg_mandate"] = True
    assert ("ESG_EXCLUSION", VIOLATION) in rules(evaluate(Portfolio.from_dict(base), post_trade=True))


def test_unverified_esg_is_warning():
    raw = pf_dict(cash=7_000, esg=True, holdings=[h("XYZ", 1, 5_000, 1, "Equity"), h("VOO", 1, 55_000, 1, "Index ETF"),
                                                  h("EAGG", 1, 33_000, 1, "Bond ETF")])
    assert ("ESG_UNVERIFIED", WARNING) in rules(evaluate(Portfolio.from_dict(raw), post_trade=True))


def test_sector_cap_only_for_moderate():
    holdings = [h("AAPL", 1, 9_000, 1, "Equity"), h("MSFT", 1, 9_000, 1, "Equity"), h("NVDA", 1, 9_000, 1, "Equity"),
                h("VOO", 1, 50_000, 1, "Index ETF"), h("EAGG", 1, 20_000, 1, "Bond ETF")]
    moderate = rules(evaluate(Portfolio.from_dict(pf_dict("Moderate", 3_000, holdings)), post_trade=True))
    aggressive = rules(evaluate(Portfolio.from_dict(pf_dict("Aggressive", 3_000, holdings)), post_trade=True))
    assert ("SECTOR_CONCENTRATION", VIOLATION) in moderate and not any(r[0] == "SECTOR_CONCENTRATION" for r in aggressive)


@pytest.mark.parametrize("risk,cash,expected", [
    ("Conservative", 100_000, "EQUITY_MIN"), ("Aggressive", 1, "CASH_MIN"), ("Moderate", 100_000, "CASH_MAX")])
def test_allocation_bands(risk, cash, expected):
    raw = pf_dict(risk, cash=cash, holdings=[h("VOO", 1, 100, 1, "Index ETF")] if cash == 1 else [])
    assert (expected, VIOLATION) in rules(evaluate(Portfolio.from_dict(raw), post_trade=True))


def test_verdict_levels():
    assert verdict([]) == "APPROVED"


# ------------------------------------------------------------------ quant tools
def test_rebalance_input_validation(marcus):
    for targets in ({}, {"AAPL": 1.5}, {"AAPL": -0.1}, {"AAPL": 0.6, "VOO": 0.6}, None):
        with pytest.raises(ToolInputError):
            calculate_rebalance({"portfolio": marcus, "target_weights": targets})
    with pytest.raises(ToolInputError, match="no price"):
        calculate_rebalance({"portfolio": marcus, "target_weights": {"NOPE": 0.1}})
    with pytest.raises(ToolInputError):
        calculate_rebalance({"target_weights": {"AAPL": 0.1}})


def test_rebalance_noop_when_on_target(marcus):
    pf = Portfolio.from_dict(marcus)
    assert calculate_rebalance({"portfolio": marcus, "target_weights": pf.weights()})["trades"] == []


def test_rebalance_min_trade_threshold(marcus):
    pf = Portfolio.from_dict(marcus)
    w = pf.weights()
    w["AAPL"] += 50 / pf.total_value  # $50 drift < $100 minimum
    assert calculate_rebalance({"portfolio": marcus, "target_weights": w})["trades"] == []
    assert calculate_rebalance({"portfolio": marcus, "target_weights": w, "min_trade_value": 0})["trades"] == []  # < 1 share


def test_rebalance_full_liquidation(marcus):
    out = calculate_rebalance({"portfolio": marcus, "target_weights": {"VOO": 0.0}})
    assert {(t["ticker"], t["action"], t["shares"]) for t in out["trades"]} == {("AAPL", "SELL", 120), ("MSFT", "SELL", 80),
                                                                                 ("VOO", "SELL", 115)}
    assert out["post_trade"]["allocation"]["cash"] == pytest.approx(1.0)


def test_tax_short_term_netting_and_losses():
    raw = pf_dict(cash=0, holdings=[h("AAPL", 10, 225.0, 300.0, "Equity"), h("MSFT", 10, 420.0, 320.0, "Equity")])
    trades = [{"ticker": "AAPL", "action": "SELL", "shares": 10, "holding_days": 400},     # LT loss -750
              {"ticker": "MSFT", "action": "SELL", "shares": 10, "holding_days": 30},      # ST gain +1000
              {"ticker": "VOO", "action": "BUY", "shares": 1}]                             # buys ignored
    out = estimate_tax_drag({"portfolio": raw, "trades": trades, "state_rate": 0.05})
    assert out["realized_gain_long_term"] == -750 and out["realized_gain_short_term"] == 1000
    assert out["estimated_tax"] == pytest.approx(250 * (0.37 + 0.05))
    only_losses = estimate_tax_drag({"portfolio": raw, "trades": trades[:1]})
    assert only_losses["estimated_tax"] == 0 and only_losses["tax_drag_pct_of_portfolio"] == 0


def test_tax_input_validation(marcus):
    with pytest.raises(ToolInputError):
        estimate_tax_drag({"portfolio": marcus, "trades": "AAPL"})
    with pytest.raises(ToolInputError):
        estimate_tax_drag({"portfolio": marcus, "trades": [], "long_term_rate": 0.9})
    with pytest.raises(ToolInputError, match="cannot sell"):
        estimate_tax_drag({"portfolio": marcus, "trades": [{"ticker": "AAPL", "action": "SELL", "shares": 999}]})
    assert estimate_tax_drag({"portfolio": marcus, "trades": []})["estimated_tax"] == 0


# ------------------------------------------------------------------ seed data and policy document
def test_seed_dataset_invariants():
    with open("data/seed_portfolios.json", encoding="utf-8") as f:
        clients = {c["client_id"]: c for c in json.load(f)}
    marcus = clients["CLIENT-8801"]
    assert marcus["client_name"] == "Marcus Vance" and marcus["cash_balance"] == 85000
    assert marcus["risk_profile"] == "Moderate" and marcus["esg_mandate"] is True
    assert {h["ticker"] for h in marcus["holdings"]} == {"AAPL", "MSFT", "VOO"}
    total = marcus["cash_balance"] + sum(h["market_value"] for h in marcus["holdings"])
    assert total == pytest.approx(marcus["total_portfolio_value"])
    with open("data/investment_policy.md", encoding="utf-8") as f:
        policy = f.read()
    assert all(s in policy for s in ("65%", "10%", "Thermal Coal", "Weapons"))
