"""In-agent JWT validation: the Entra ID contract (issuer, audience, azp, tenancy claims) and token attacks."""
import base64
import hashlib
import hmac
import io
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.common import identity
from src.common.config import Settings
from tests.conftest import ADVISOR_CLAIM, AUDIENCE, AZP, CLIENT_CLAIM, ISSUER, SIGNING_KEY, make_token, make_validator


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(**extra):
    now = int(time.time())
    return {"iss": ISSUER, "sub": "user-1", "iat": now, "exp": now + 300, "aud": AUDIENCE, "azp": AZP,
            ADVISOR_CLAIM: "ADVISOR-042", CLIENT_CLAIM: "CLIENT-8801", **extra}


# ------------------------------------------------------------------ the contract
def test_valid_token_yields_caller(validator):
    token = make_token()
    caller = validator.validate_headers({"authorization": f"Bearer {token}"})
    assert (caller.advisor_id, caller.client_id, caller.subject) == ("ADVISOR-042", "CLIENT-8801", "user-1")
    assert caller.scopes == ("access_as_user",) and token not in repr(caller)
    assert token not in json.dumps(caller.span_attributes())


@pytest.mark.parametrize("overrides", [
    {"iss": "https://login.microsoftonline.com/other-tenant/v2.0"},  # another tenant
    {"aud": "api://some-other-api"},                                 # token for another API
    {"azp": "cccccccc-0000-0000-0000-000000000003"},                 # another client application
    {"azp": None},                                                   # v1 token / no azp
    {"exp": int(time.time()) - 3600},
])
def test_contract_violations_rejected(validator, overrides):
    with pytest.raises(identity.AuthenticationError):
        validator.validate(make_token(**overrides))


@pytest.mark.parametrize("missing", ["advisor_id", "client_id"])
def test_tenancy_claims_required(validator, missing):
    with pytest.raises(identity.AuthenticationError):
        validator.validate(make_token(**{missing: None}))
    with pytest.raises(identity.AuthenticationError):
        validator.validate(make_token(**{missing: ""}))


def test_claim_names_are_configurable():
    v = identity.JwtValidator(ISSUER, audiences=(AUDIENCE,), allowed_client_ids=(AZP,), advisor_claim="advisor",
                              client_claim="client", key_resolver=lambda _t: SIGNING_KEY.public_key())
    caller = v.validate(make_token(advisor_id=None, client_id=None, advisor="ADVISOR-099", client="CLIENT-8802"))
    assert (caller.advisor_id, caller.client_id) == ("ADVISOR-099", "CLIENT-8802")


@pytest.mark.parametrize("scope,expected", [("a b c", ("a", "b", "c")), (["x", "y"], ("x", "y")), (None, ())])
def test_scope_parsing(validator, scope, expected):
    assert validator.validate(make_token(scp=scope)).scopes == expected


# ------------------------------------------------------------------ forgery / algorithm attacks
def test_forged_signature_rejected(validator):
    forged = jwt.encode(_claims(), rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256")
    with pytest.raises(identity.AuthenticationError):
        validator.validate(forged)


def test_alg_none_rejected(validator):
    with pytest.raises(identity.AuthenticationError):
        validator.validate(jwt.encode(_claims(), None, algorithm="none"))


def test_hs256_key_confusion_rejected(validator):
    """Classic RS256->HS256 confusion: HMAC-sign with the public key PEM."""
    pem = SIGNING_KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(_claims(**{ADVISOR_CLAIM: "ADVISOR-099"})).encode())
    sig = _b64(hmac.new(pem, f"{header}.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(identity.AuthenticationError):
        validator.validate(f"{header}.{body}.{sig}")


def test_tampered_payload_rejected(validator):
    header, _, sig = make_token().split(".")
    body = _b64(json.dumps(_claims(**{ADVISOR_CLAIM: "ADVISOR-099"})).encode())
    with pytest.raises(identity.AuthenticationError):
        validator.validate(f"{header}.{body}.{sig}")


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b", "a.b.c.d", "eyJhbGciOiJSUzI1NiJ9.e30."])
def test_malformed_tokens_rejected(validator, garbage):
    with pytest.raises(identity.AuthenticationError):
        validator.validate(garbage)


# ------------------------------------------------------------------ registered claims
def test_not_before_in_future_rejected(validator):
    with pytest.raises(identity.AuthenticationError):
        validator.validate(make_token(nbf=int(time.time()) + 3600))


@pytest.mark.parametrize("missing", ["sub", "iat", "exp", "iss"])
def test_required_registered_claims(validator, missing):
    with pytest.raises(identity.AuthenticationError):
        validator.validate(make_token(**{missing: None}))


def test_clock_skew_leeway(validator):
    assert validator.validate(make_token(exp=int(time.time()) - 10)).subject == "user-1"  # within 30s leeway
    with pytest.raises(identity.AuthenticationError):
        validator.validate(make_token(exp=int(time.time()) - 120))


# ------------------------------------------------------------------ configuration
def test_validator_requires_issuer():
    with pytest.raises(ValueError):
        identity.JwtValidator("", advisor_claim="a", client_claim="c")


def test_signing_keys_come_from_the_discovery_document(monkeypatch):
    """Entra publishes its keys at .../discovery/v2.0/keys - not <issuer>/.well-known/jwks.json."""
    seen = []

    def fake_urlopen(url, timeout=0):
        seen.append(url)
        return io.BytesIO(json.dumps({"issuer": ISSUER,
                                      "jwks_uri": "https://login.microsoftonline.com/t/discovery/v2.0/keys"}).encode())

    monkeypatch.setattr(identity.urllib.request, "urlopen", fake_urlopen)
    captured = {}
    monkeypatch.setattr(identity.jwt, "PyJWKClient", lambda uri, **kw: captured.setdefault("uri", uri))
    identity.JwtValidator.from_settings(Settings(
        jwt_issuer=ISSUER, jwt_discovery_url=f"{ISSUER}/.well-known/openid-configuration", jwt_audiences=(AUDIENCE,),
        jwt_allowed_client_ids=(AZP,), jwt_advisor_claim=ADVISOR_CLAIM, jwt_client_claim=CLIENT_CLAIM))
    assert seen == [f"{ISSUER}/.well-known/openid-configuration"]
    assert captured["uri"] == "https://login.microsoftonline.com/t/discovery/v2.0/keys"


def test_validator_from_settings_wires_the_contract(monkeypatch):
    monkeypatch.setattr(identity, "jwks_uri_from_discovery", lambda url: "https://keys.example/jwks")
    v = identity.JwtValidator.from_settings(Settings(jwt_issuer=ISSUER, jwt_audiences=("a1",), jwt_allowed_client_ids=("c1",),
                                                     jwt_advisor_claim="adv", jwt_client_claim="cli"))
    assert (v.issuer, v.audiences, v.allowed_client_ids, v.advisor_claim, v.client_claim) == (
        ISSUER, ("a1",), ("c1",), "adv", "cli")


# ------------------------------------------------------------------ headers and tenancy
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "},
                                     {"Authorization": "Bearer"}, {"X-Other": "Bearer abc"}])
def test_bearer_token_missing_or_wrong_scheme(headers):
    with pytest.raises(identity.AuthenticationError):
        identity.bearer_token(headers)


def test_bearer_scheme_is_case_insensitive():
    assert identity.bearer_token({"authorization": "bearer tok"}) == "tok"
    assert identity.bearer_token({"AUTHORIZATION": "BEARER tok"}) == "tok"


def test_authorize_both_dimensions(validator):
    caller = validator.validate(make_token())
    caller.authorize(advisor_id="ADVISOR-042", client_id="CLIENT-8801")
    with pytest.raises(identity.AuthorizationError):
        caller.authorize(advisor_id="ADVISOR-099")
    with pytest.raises(identity.AuthorizationError):
        caller.authorize(client_id="CLIENT-8802")


def test_make_validator_matches_fixture():
    assert make_validator().validate(make_token()).advisor_id == "ADVISOR-042"
