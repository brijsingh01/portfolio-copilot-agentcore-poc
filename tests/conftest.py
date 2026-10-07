"""Shared fixtures. Test tokens are shaped like Microsoft Entra ID v2.0 access tokens."""
import copy
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.common import identity, tracer

TENANT = "11111111-2222-3333-4444-555555555555"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
AUDIENCE = "aaaaaaaa-0000-0000-0000-000000000001"     # API app registration (client) id
AZP = "bbbbbbbb-0000-0000-0000-000000000002"          # client app registration (client) id
ADVISOR_CLAIM = "extension_advisorId"
CLIENT_CLAIM = "extension_clientId"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EXPORTER = InMemorySpanExporter()
tracer.configure_tracing("tests", exporter=EXPORTER)
_SEED = json.loads((Path(__file__).resolve().parents[1] / "data" / "seed_portfolios.json").read_text(encoding="utf-8"))


def make_token(key=SIGNING_KEY, *, advisor_id="ADVISOR-042", client_id="CLIENT-8801", **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "user-1", "iat": now, "exp": now + 300, "aud": AUDIENCE, "azp": AZP, "ver": "2.0",
              "scp": "access_as_user", ADVISOR_CLAIM: advisor_id, CLIENT_CLAIM: client_id, **overrides}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


def make_validator(**kwargs) -> identity.JwtValidator:
    return identity.JwtValidator(ISSUER, audiences=(AUDIENCE,), allowed_client_ids=(AZP,), advisor_claim=ADVISOR_CLAIM,
                                 client_claim=CLIENT_CLAIM, key_resolver=lambda _t: SIGNING_KEY.public_key(), **kwargs)


@pytest.fixture
def validator():
    return make_validator()


@pytest.fixture
def spans():
    EXPORTER.clear()
    return EXPORTER


@pytest.fixture
def marcus() -> dict:
    return copy.deepcopy(next(c for c in _SEED if c["client_id"] == "CLIENT-8801"))


@pytest.fixture
def elena() -> dict:
    return copy.deepcopy(next(c for c in _SEED if c["client_id"] == "CLIENT-8802"))
