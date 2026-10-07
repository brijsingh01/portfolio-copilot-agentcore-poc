"""Gateway tool - IPS compliance rules engine (deterministic, no LLM).

Tool: review_compliance(portfolio, proposed_trades?, requested_by?) -> status APPROVED | APPROVED_WITH_WARNINGS |
      REJECTED with rule-level findings. A pure function of its inputs; `requested_by` is recorded for audit.
"""
from __future__ import annotations

from typing import Any

from src.common.compliance import InvalidReviewInput, review
from src.common.lambda_target import ToolError, gateway_target


def review_compliance(args: dict[str, Any]) -> dict[str, Any]:
    try:
        # nested under "review": the tool wrapper owns the top-level "status" (success | error)
        return {"review": review(args.get("portfolio"), args.get("proposed_trades"), requested_by=args.get("requested_by"))}
    except InvalidReviewInput as exc:
        raise ToolError("INVALID_ARGUMENT", str(exc)) from exc


handler = gateway_target("compliance-rules-target", {"review_compliance": review_compliance})
