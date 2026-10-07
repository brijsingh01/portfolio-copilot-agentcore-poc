"""Portfolio analytics executed in an AgentCore Code Interpreter sandbox (custom interpreter, VPC mode).

The orchestrator sends a fixed, reviewed Python program plus the portfolio/trade data (base64 JSON, so no data
can alter the code) and receives JSON on stdout: pre/post-trade weights, concentration (HHI, effective number of
holdings), largest position and an illustrative volatility estimate. No model-generated code is executed.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import boto3
from botocore.config import Config

from src.common.config import get_settings
from src.common.tracer import client_span, current_traceparent

ANALYTICS_PROGRAM = r'''
import base64, json, math
data = json.loads(base64.b64decode("{payload}").decode())
# Illustrative annualised volatilities by asset class and one pairwise correlation (POC assumptions).
VOL = {"Equity": 0.28, "Index ETF": 0.16, "Bond ETF": 0.06, "Bond": 0.06, "Cash": 0.0}
RHO = 0.5

def snapshot(holdings, cash):
    values = {h["ticker"]: max(float(h["shares"]) * float(h["price"]), 0.0) for h in holdings}
    total = sum(values.values()) + cash
    if total <= 0:
        return {"total_value": 0.0}
    w = {t: v / total for t, v in values.items() if v > 0}
    cls = {h["ticker"]: h.get("asset_class", "Equity") for h in holdings}
    hhi = sum(x * x for x in w.values())
    vols = {t: VOL.get(cls[t], 0.25) for t in w}
    var = sum((w[t] * vols[t]) ** 2 for t in w)
    var += sum(2 * RHO * w[a] * vols[a] * w[b] * vols[b] for i, a in enumerate(w) for b in list(w)[i + 1:])
    top = max(w, key=w.get) if w else None
    return {"total_value": round(total, 2), "cash_weight": round(cash / total, 4),
            "weights": {t: round(x, 4) for t, x in sorted(w.items(), key=lambda kv: -kv[1])},
            "hhi": round(hhi, 4), "effective_holdings": round(1 / hhi, 2) if hhi else None,
            "largest_position": {"ticker": top, "weight": round(w[top], 4)} if top else None,
            "est_annual_volatility": round(math.sqrt(var), 4)}

holdings = {h["ticker"]: dict(h) for h in data["holdings"]}
cash = float(data["cash"])
unpriced = []
for t in data["trades"]:
    tk, shares = t["ticker"], float(t["shares"])
    price = t.get("price") or (t["est_value"] / shares if t.get("est_value") and shares else None) \
        or holdings.get(tk, {}).get("price")
    if not price:
        unpriced.append(tk)
        continue
    h = holdings.setdefault(tk, {"ticker": tk, "shares": 0.0, "price": price, "asset_class": t.get("asset_class", "Equity")})
    signed = shares if t["action"] == "BUY" else -shares
    h["shares"] = float(h["shares"]) + signed
    cash -= signed * float(price)
before = snapshot(data["holdings"], float(data["cash"]))
after = snapshot(list(holdings.values()), cash)
print(json.dumps({"engine": "agentcore-code-interpreter", "before": before, "after": after,
                  "hhi_change": round(after.get("hhi", 0) - before.get("hhi", 0), 4),
                  "unpriced_trades": unpriced}))
'''


class CodeInterpreterError(RuntimeError):
    pass


def analytics_input(portfolio: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
    holdings = [{"ticker": h["ticker"], "shares": h["shares"], "price": h.get("current_price") or h.get("price") or 0,
                 "asset_class": h.get("asset_class", "Equity")} for h in portfolio.get("holdings", [])]
    return {"holdings": holdings, "cash": portfolio.get("cash_balance", 0),
            "trades": [{k: t.get(k) for k in ("ticker", "action", "shares", "price", "est_value", "asset_class")}
                       for t in trades]}


def render_program(data: dict[str, Any]) -> str:
    return ANALYTICS_PROGRAM.replace("{payload}", base64.b64encode(json.dumps(data, default=float).encode()).decode())


class CodeInterpreterAnalytics:
    def __init__(self, interpreter_id: str, *, region: str | None = None, client: Any = None,
                 session_timeout: int = 300) -> None:
        self.interpreter_id, self.session_timeout = interpreter_id, session_timeout
        self._client = client or boto3.client("bedrock-agentcore", region_name=region or get_settings().region,
                                              config=Config(retries={"mode": "standard", "max_attempts": 3}, read_timeout=90))

    @classmethod
    def from_settings(cls) -> CodeInterpreterAnalytics | None:
        s = get_settings()
        return cls(s.code_interpreter_id, region=s.region) if s.code_interpreter_id else None

    def _trace(self) -> dict[str, str]:
        tp = current_traceparent()
        return {"traceParent": tp} if tp else {}

    def run(self, portfolio: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
        attrs = {"agentcore.code_interpreter.id": self.interpreter_id, "copilot.trades": len(trades)}
        with client_span("agentcore.code_interpreter.execute", attrs) as (span, _):
            session = self._client.start_code_interpreter_session(
                codeInterpreterIdentifier=self.interpreter_id, name="portfolio-analytics",
                sessionTimeoutSeconds=self.session_timeout, **self._trace())["sessionId"]
            span.set_attribute("agentcore.code_interpreter.session_id", session)
            try:
                resp = self._client.invoke_code_interpreter(
                    codeInterpreterIdentifier=self.interpreter_id, sessionId=session, name="executeCode",
                    arguments={"language": "python", "code": render_program(analytics_input(portfolio, trades))},
                    **self._trace())
                stdout, stderr = "", ""
                for event in resp["stream"]:
                    if "result" not in event:
                        raise CodeInterpreterError(f"code interpreter error: {next(iter(event))}")
                    result = event["result"]
                    structured = result.get("structuredContent", {})
                    stdout += structured.get("stdout", "") or ""
                    stderr += structured.get("stderr", "") or ""
                    if result.get("isError") or structured.get("exitCode", 0):
                        raise CodeInterpreterError(f"analytics program failed: {(stderr or stdout)[-300:]}")
            finally:
                self._client.stop_code_interpreter_session(codeInterpreterIdentifier=self.interpreter_id, sessionId=session)
            try:
                out = json.loads(stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                raise CodeInterpreterError(f"unexpected analytics output: {stdout[-300:]!r}") from exc
            span.set_attribute("copilot.analytics.hhi_after", out.get("after", {}).get("hhi", 0))
        return out
