"""Shared harness for the end-to-end suites against a deployed environment.

Reads cloudformation/.state/<env>.json (written by `python -m cloudformation.deploy up|state`; COPILOT_STATE_FILE
overrides the path) and takes Microsoft Entra ID access tokens from environment variables:

    COPILOT_TOKEN            required  test advisor 1 - claims must be ADVISOR-042 / CLIENT-8801 (seed data)
    COPILOT_TOKEN_ADVISOR2   optional  test advisor 2 - ADVISOR-099 / CLIENT-8802
    COPILOT_TOKEN_OTHER_APP  optional  a token issued to a *different* client app (wrong azp) for the negative tests

`python scripts/get_entra_token.py --env <env> --scope <api scope>` prints a token.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = Path(os.getenv("COPILOT_STATE_FILE", ROOT / "cloudformation" / ".state" / f"{os.getenv('COPILOT_ENV', 'poc')}.json"))
ADVISOR1 = ("ADVISOR-042", "CLIENT-8801")
ADVISOR2 = ("ADVISOR-099", "CLIENT-8802")

requires_deployment = pytest.mark.skipif(
    os.getenv("E2E") != "1" or not STATE_FILE.exists() or not os.getenv("COPILOT_TOKEN"),
    reason="set E2E=1 and COPILOT_TOKEN after `python -m cloudformation.deploy up --env poc`")


def claims_of(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class Deployment:
    def __init__(self, state_file: Path = STATE_FILE) -> None:
        self.state: dict[str, Any] = json.loads(state_file.read_text())
        self.region: str = self.state["region"]
        self.session = boto3.Session(region_name=self.region)

    def c(self, service: str) -> Any:
        return self.session.client(service, region_name=self.region)

    def token(self, which: str = "COPILOT_TOKEN", expect: tuple[str, str] | None = ADVISOR1) -> str:
        token = os.getenv(which)
        if not token:
            pytest.skip(f"{which} not set")
        claims = claims_of(token)
        if claims.get("exp", 0) < time.time() + 60:
            pytest.fail(f"{which} is expired - get a fresh token (scripts/get_entra_token.py)")
        if expect is not None:
            got = (claims.get(self.state["advisor_id_claim"]), claims.get(self.state["client_id_claim"]))
            assert got == expect, (f"{which} carries advisor/client {got}; the seed data expects {expect} "
                                   f"(claims {self.state['advisor_id_claim']} / {self.state['client_id_claim']})")
        return token

    def forged_token(self) -> str:
        """Right issuer, audience, azp and tenancy claims - signed by an attacker's key."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        return jwt.encode({"iss": self.state["jwt_issuer"], "aud": self.state["jwt_audience"], "azp": self.state["jwt_azp"],
                           "sub": "attacker", "iat": now, "exp": now + 300,
                           self.state["advisor_id_claim"]: ADVISOR1[0], self.state["client_id_claim"]: ADVISOR1[1]},
                          key, algorithm="RS256", headers={"kid": "forged"})

    @property
    def orchestrator_url(self) -> str:
        return f"{self.state['orchestrator_runtime_url']}/invocations?qualifier=DEFAULT"

    def invoke(self, token: str | None, body: dict, session_id: str | None = None) -> tuple[int, dict]:
        headers = {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id or new_session()}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return post_json(self.orchestrator_url, body, headers, timeout=240)


def new_session() -> str:
    return f"e2e-{uuid.uuid4()}"  # AgentCore requires >= 33 chars


def post_json(url: str, body: dict, headers: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read().decode(errors="replace"), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(errors="replace"), exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"raw": raw}


def container_status(status: int, body: dict, expected: int) -> bool:
    """AgentCore Runtime surfaces a container error as HTTP 424 "Received error (<code>) from runtime"."""
    return status == expected or (status == 424 and f"({expected})" in json.dumps(body))


def query_spans(dep: Deployment, trace_id: str) -> list[dict]:
    logs = dep.c("logs")
    now = int(time.time())
    qid = logs.start_query(logGroupName="aws/spans", startTime=now - 3600, endTime=now + 60,
                           queryString=f'fields @message | filter traceId = "{trace_id}" | limit 1000')["queryId"]
    while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
        time.sleep(2)
    return [json.loads(next(f["value"] for f in row if f["field"] == "@message")) for row in res["results"]]
