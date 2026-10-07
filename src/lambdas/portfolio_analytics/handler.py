"""Gateway tool - portfolio analytics on AgentCore Code Interpreter (VPC mode).

Tool:
  analyze_portfolio(portfolio, proposed_trades?) -> concentration (HHI, effective holdings, largest position) and
                                   estimated volatility before/after the trades, computed by a fixed program in a
                                   Code Interpreter session (start -> executeCode -> stop). No generated code runs.
The Gateway exposes the stateful session API as one stateless tool call, so any agent can reuse it.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from src.common.code_interpreter import CodeInterpreterAnalytics, CodeInterpreterError
from src.common.lambda_target import ToolError, gateway_target


@lru_cache(maxsize=1)
def _analytics() -> CodeInterpreterAnalytics:
    analytics = CodeInterpreterAnalytics.from_settings()
    if analytics is None:
        raise ToolError("NOT_CONFIGURED", "CODE_INTERPRETER_ID is not set")
    return analytics


def analyze_portfolio(args: dict[str, Any]) -> dict[str, Any]:
    portfolio, trades = args.get("portfolio"), args.get("proposed_trades") or []
    if not isinstance(portfolio, dict) or not isinstance(portfolio.get("holdings"), list):
        raise ToolError("INVALID_ARGUMENT", "`portfolio` must be an object with a `holdings` list")
    if not isinstance(trades, list) or not all(isinstance(t, dict) for t in trades):
        raise ToolError("INVALID_ARGUMENT", "`proposed_trades` must be a list of objects")
    try:
        return {"analytics": _analytics().run(portfolio, trades)}
    except CodeInterpreterError as exc:
        raise ToolError("ANALYTICS_FAILED", str(exc)) from exc


handler = gateway_target("portfolio-analytics-target", {"analyze_portfolio": analyze_portfolio})
