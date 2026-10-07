"""Gateway tool - firm investment & ESG policy retrieval (Bedrock Knowledge Base on S3 Vectors).

Tool: search_investment_policy(query, top_k?) -> ranked passages with S3 citations.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import boto3

from src.common.lambda_target import ToolError, gateway_target
from src.common.tracer import client_span

KB_ID = os.getenv("KNOWLEDGE_BASE_ID", "")
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.0"))
MAX_QUERY_CHARS = 1000


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client("bedrock-agent-runtime")


def search_investment_policy(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("INVALID_ARGUMENT", "query is required")
    if not KB_ID:
        raise ToolError("MISCONFIGURED", "KNOWLEDGE_BASE_ID is not set")
    top_k = max(1, min(int(args.get("top_k") or 4), 10))
    with client_span("bedrock.knowledge_base.retrieve", {"aws.bedrock.knowledge_base.id": KB_ID, "kb.top_k": top_k}) as (span, _h):
        resp = _client().retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query[:MAX_QUERY_CHARS]},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
        )
        results = resp.get("retrievalResults", [])
        span.set_attribute("kb.results", len(results))
    passages = [
        {
            "text": r["content"]["text"],
            "score": round(float(r.get("score", 0.0)), 4),
            "source": (r.get("location", {}).get("s3Location") or {}).get("uri"),
        }
        for r in results
        if r.get("content", {}).get("text") and float(r.get("score", 0.0)) >= MIN_SCORE
    ]
    return {"query": query, "passages": passages}


handler = gateway_target("kb-retrieval-target", {"search_investment_policy": search_investment_policy})
