"""Gateway tool - ClientPortfolios lookup (DynamoDB).

Tool: get_client_portfolio(client_id) -> holdings, cash, risk profile, ESG mandate and advisor of record.
The orchestrator checks the advisor_id / client_id of the record against the caller's token before using it.
"""
from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from typing import Any

import boto3

from src.common.lambda_target import ToolError, gateway_target
from src.common.tracer import client_span

TABLE_NAME = os.getenv("PORTFOLIO_TABLE", "ClientPortfolios")


@lru_cache(maxsize=1)
def _table() -> Any:
    return boto3.resource("dynamodb").Table(TABLE_NAME)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def get_client_portfolio(args: dict[str, Any]) -> dict[str, Any]:
    client_id = str(args.get("client_id") or "").strip().upper()
    if not client_id.startswith("CLIENT-"):
        raise ToolError("INVALID_ARGUMENT", "client_id must look like CLIENT-NNNN")
    with client_span("dynamodb.GetItem", {"db.system": "dynamodb", "aws.dynamodb.table_names": [TABLE_NAME]}):
        item = _table().get_item(Key={"client_id": client_id}, ConsistentRead=True).get("Item")
    if item is None:
        raise ToolError("NOT_FOUND", f"no portfolio for {client_id}")
    return {"portfolio": _plain(item)}


handler = gateway_target("ddb-portfolio-target", {"get_client_portfolio": get_client_portfolio})
