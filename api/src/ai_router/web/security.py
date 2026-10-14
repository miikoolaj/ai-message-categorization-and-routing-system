"""Authentication, sender verification and per-client rate limiting.

AUTH_MODE:
* `none` (default, so the README cURL works out of the box) - anonymous clients, sender address
  NOT verified; e-mails carry a warning banner. Never expose this beyond localhost.
* `api_key` - header `X-API-Key`; keys are configured as SHA-256 hashes (`API_KEYS`). The key
  identifies a client application, which vouches for the sender (verification: `client`).
* `oidc` - `Authorization: Bearer <JWT>` from the company SSO, verified against its JWKS. The
  sender address is taken from the token, so nobody can claim to be someone else (`sso`).
"""

import asyncio
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from starlette.requests import Request

from ai_router.config import Settings
from ai_router.errors import AuthenticationError, PermissionDeniedError, TooManyRequestsError
from ai_router.observability import metrics
from ai_router.routing.deps import SenderVerification

KeyResolver = Callable[[str], Any]  # JWT -> verification key


@dataclass(frozen=True, slots=True)
class AuthContext:
    client_id: str
    rate_key: str
    sender_verification: SenderVerification
    verified_email: str | None = None

    def sender_for(self, claimed_email: str) -> str:
        """The address used as Reply-To; with SSO it must match the authenticated user."""
        if self.verified_email is None:
            return claimed_email
        if claimed_email.lower() != self.verified_email.lower():
            metrics.REJECTED.labels("sender_mismatch").inc()
            raise PermissionDeniedError("`email` must match the authenticated user.")
        return self.verified_email


def parse_api_keys(spec: str) -> dict[str, str]:
    """`"client-a:<sha256hex>,client-b:<sha256hex>"` -> {sha256hex: client_id}."""
    keys: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        client_id, _, digest = item.partition(":")
        if not client_id or len(digest) != 64:
            raise ValueError(f"invalid API_KEYS entry for {client_id or '?'!r}")
        keys[digest.lower()] = client_id
    return keys


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Authenticator:
    def __init__(self, settings: Settings, *, key_resolver: KeyResolver | None = None) -> None:
        self._settings = settings
        self._api_keys = (
            parse_api_keys(settings.api_keys) if settings.auth_mode == "api_key" else {}
        )
        self._key_resolver = key_resolver
        if settings.auth_mode == "oidc" and key_resolver is None:
            client = jwt.PyJWKClient(str(settings.oidc_jwks_url), cache_keys=True)
            self._key_resolver = lambda token: client.get_signing_key_from_jwt(token).key

    async def authenticate(self, request: Request) -> AuthContext:
        mode = self._settings.auth_mode
        if mode == "none":
            host = request.client.host if request.client else "unknown"
            return AuthContext("anonymous", f"ip:{host}", "none")
        if mode == "api_key":
            return self._api_key(request.headers.get("x-api-key"))
        return await self._oidc(request.headers.get("authorization"))

    def _api_key(self, key: str | None) -> AuthContext:
        if key:
            digest = hash_api_key(key)
            for known, client_id in self._api_keys.items():
                if hmac.compare_digest(known, digest):
                    return AuthContext(client_id, f"client:{client_id}", "client")
        metrics.REJECTED.labels("unauthenticated").inc()
        raise AuthenticationError("A valid `X-API-Key` header is required.")

    async def _oidc(self, header: str | None) -> AuthContext:
        if not header or not header.lower().startswith("bearer "):
            metrics.REJECTED.labels("unauthenticated").inc()
            raise AuthenticationError("A Bearer token from the company SSO is required.")
        token = header[7:].strip()
        assert self._key_resolver is not None  # noqa: S101 - set in __init__ for oidc
        try:
            key = await asyncio.to_thread(self._key_resolver, token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"require": ["exp", "iat"], "verify_iss": bool(self._settings.oidc_issuer)},
            )
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            metrics.REJECTED.labels("unauthenticated").inc()
            raise AuthenticationError("The token is invalid or expired.") from exc
        email = claims.get(self._settings.oidc_email_claim)
        if not isinstance(email, str) or "@" not in email:
            raise AuthenticationError("The token does not contain an e-mail address.")
        subject = str(claims.get("sub", email))
        return AuthContext(subject, f"user:{subject}", "sso", verified_email=email)

    def require_admin(self, request: Request) -> None:
        """Admin endpoints (feedback export): `Authorization: Bearer <ADMIN_TOKEN>` if set."""
        expected = self._settings.admin_token
        if expected is None:
            if self._settings.auth_mode == "none":
                return  # local PoC mode
            raise PermissionDeniedError("ADMIN_TOKEN is not configured.")
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header, f"Bearer {expected.get_secret_value()}"):
            raise PermissionDeniedError("Admin token required.")


class RateLimiter:
    """Token bucket per client: `rate_per_minute` sustained, `burst` at once. In-memory (one
    instance); with several replicas move the buckets to Redis."""

    def __init__(
        self, *, rate_per_minute: int, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = rate_per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, key: str) -> None:
        if self._rate <= 0:
            return
        now = self._clock()
        tokens, updated = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - updated) * self._rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            metrics.REJECTED.labels("rate_limited").inc()
            retry_after = max(1, int((1.0 - tokens) / self._rate) + 1)
            raise TooManyRequestsError(
                "Rate limit exceeded for this client.", retry_after=retry_after
            )
        self._buckets[key] = (tokens - 1.0, now)
        if len(self._buckets) > 10_000:  # bound memory: forget the oldest buckets
            for stale in sorted(self._buckets, key=lambda k: self._buckets[k][1])[:1_000]:
                del self._buckets[stale]
