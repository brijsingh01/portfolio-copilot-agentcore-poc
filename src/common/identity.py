"""Inbound JWT validation for the agents (orchestrator, Market Analyst).

AgentCore already rejects bad tokens at the edge (discovery URL, audience, `azp`). The agents validate the same
token again in-process to get a trusted `CallerContext` (advisor id, client id) for the tenancy checks, using the
same contract: the issuer's discovery document (for the signing keys), the audience, the allowed `azp`, and the
names of the two tenancy claims.
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import jwt

from src.common.config import Settings, get_settings


class IdentityError(Exception):
    """Base identity failure."""


class AuthenticationError(IdentityError):
    """Token missing, malformed, expired or not signed by the trusted issuer (HTTP 401)."""


class AuthorizationError(IdentityError):
    """Authenticated caller is not entitled to the requested resource (HTTP 403)."""


@dataclass(frozen=True)
class CallerContext:
    subject: str
    advisor_id: str
    client_id: str
    token: str = field(repr=False)
    issuer: str
    expires_at: int
    scopes: tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def span_attributes(self) -> dict[str, str]:
        return {"enduser.id": self.subject, "copilot.advisor_id": self.advisor_id, "copilot.client_id": self.client_id}

    def authorize(self, *, advisor_id: str | None = None, client_id: str | None = None) -> None:
        """Tenancy check: caller may only act on their own advisor book and their own client."""
        if advisor_id is not None and advisor_id != self.advisor_id:
            raise AuthorizationError(f"advisor {self.advisor_id} may not access records of advisor {advisor_id}")
        if client_id is not None and client_id != self.client_id:
            raise AuthorizationError(f"token is scoped to {self.client_id}, not {client_id}")


def bearer_token(headers: Mapping[str, Any]) -> str:
    value = next((str(v) for k, v in headers.items() if str(k).lower() == "authorization"), "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("missing bearer token")
    return token.strip()


def jwks_uri_from_discovery(discovery_url: str) -> str:
    """The issuer's signing keys location from its discovery document (Entra ID: `…/discovery/v2.0/keys`)."""
    with urllib.request.urlopen(discovery_url, timeout=10) as resp:
        return json.loads(resp.read())["jwks_uri"]


class JwtValidator:
    """Validates RS256/ES256 access tokens issued by Microsoft Entra ID (any standard OIDC issuer works)."""

    def __init__(
        self,
        issuer: str,
        *,
        advisor_claim: str,
        client_claim: str,
        audiences: tuple[str, ...] = (),
        allowed_client_ids: tuple[str, ...] = (),
        discovery_url: str | None = None,
        key_resolver: Callable[[str], Any] | None = None,
        algorithms: tuple[str, ...] = ("RS256", "ES256"),
        leeway_seconds: int = 30,
    ) -> None:
        if not issuer:
            raise ValueError("issuer is required")
        self.issuer, self.audiences, self.allowed_client_ids = issuer, audiences, allowed_client_ids
        self.advisor_claim, self.client_claim = advisor_claim, client_claim
        self.algorithms, self.leeway = list(algorithms), leeway_seconds
        if key_resolver is None:
            discovery = discovery_url or f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            jwks = jwt.PyJWKClient(jwks_uri_from_discovery(discovery), cache_keys=True, lifespan=3600)
            key_resolver = lambda token: jwks.get_signing_key_from_jwt(token).key  # noqa: E731
        self._resolve_key = key_resolver

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> JwtValidator:
        s = settings or get_settings()
        return cls(s.jwt_issuer, discovery_url=s.jwt_discovery_url or None, audiences=s.jwt_audiences,
                   allowed_client_ids=s.jwt_allowed_client_ids, advisor_claim=s.jwt_advisor_claim,
                   client_claim=s.jwt_client_claim)

    def validate(self, token: str) -> CallerContext:
        try:
            claims = jwt.decode(
                token,
                key=self._resolve_key(token),
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=list(self.audiences) or None,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": bool(self.audiences)},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"invalid token: {exc}") from exc

        if self.allowed_client_ids and claims.get("azp") not in self.allowed_client_ids:
            raise AuthenticationError("token issued to an untrusted client application (azp)")
        advisor_id, client_id = claims.get(self.advisor_claim), claims.get(self.client_claim)
        if not isinstance(advisor_id, str) or not advisor_id or not isinstance(client_id, str) or not client_id:
            raise AuthenticationError(f"token missing the {self.advisor_claim} / {self.client_claim} claims")

        scope = claims.get("scope") or claims.get("scp") or ""
        return CallerContext(
            subject=claims["sub"],
            advisor_id=advisor_id,
            client_id=client_id,
            token=token,
            issuer=claims["iss"],
            expires_at=int(claims["exp"]),
            scopes=tuple(scope.split() if isinstance(scope, str) else scope),
            claims=claims,
        )

    def validate_headers(self, headers: Mapping[str, Any]) -> CallerContext:
        return self.validate(bearer_token(headers))
