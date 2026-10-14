"""Authentication modes, sender verification and rate limiting."""

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from ai_router.errors import AuthenticationError, PermissionDeniedError, TooManyRequestsError
from ai_router.web.security import Authenticator, RateLimiter, hash_api_key, parse_api_keys
from tests.conftest import make_settings

pytestmark = pytest.mark.anyio

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()


def http_request(headers: dict[str, str] | None = None, client: str = "10.0.0.7") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {"type": "http", "headers": raw, "client": (client, 1234), "method": "POST", "path": "/"}
    )


def token(**claims: Any) -> str:
    now = int(time.time())
    payload = {
        "sub": "u-1",
        "email": "jan@example.com",
        "aud": "ai-router",
        "iss": "https://sso.test",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    return jwt.encode(
        {k: v for k, v in payload.items() if v is not None}, PRIVATE_KEY, algorithm="RS256"
    )


def oidc() -> Authenticator:
    settings = make_settings(
        auth_mode="oidc",
        oidc_jwks_url="https://sso.test/jwks",
        oidc_audience="ai-router",
        oidc_issuer="https://sso.test",
    )
    return Authenticator(settings, key_resolver=lambda _token: PUBLIC_KEY)


async def test_no_auth_mode_is_anonymous_and_unverified() -> None:
    ctx = await Authenticator(make_settings()).authenticate(http_request())
    assert ctx.client_id == "anonymous" and ctx.rate_key == "ip:10.0.0.7"
    assert ctx.sender_verification == "none"
    assert ctx.sender_for("any@example.com") == "any@example.com"


async def test_api_key_mode() -> None:
    settings = make_settings(auth_mode="api_key", api_keys=f"intranet:{hash_api_key('s3cret')}")
    auth = Authenticator(settings)
    ctx = await auth.authenticate(http_request({"X-API-Key": "s3cret"}))
    assert ctx.client_id == "intranet" and ctx.sender_verification == "client"
    for headers in ({}, {"X-API-Key": "wrong"}):
        with pytest.raises(AuthenticationError):
            await auth.authenticate(http_request(headers))


def test_parse_api_keys_validates_entries() -> None:
    digest = hash_api_key("k")
    assert parse_api_keys(f"a:{digest}, b:{digest.upper()}") == {digest: "b"}
    with pytest.raises(ValueError):
        parse_api_keys("broken-entry")


async def test_oidc_takes_the_sender_from_the_token() -> None:
    ctx = await oidc().authenticate(http_request({"Authorization": f"Bearer {token()}"}))
    assert ctx.sender_verification == "sso" and ctx.verified_email == "jan@example.com"
    assert ctx.sender_for("JAN@example.com") == "jan@example.com"
    with pytest.raises(PermissionDeniedError):
        ctx.sender_for("prezes@example.com")  # cannot claim to be someone else


@pytest.mark.parametrize(
    "bad_token",
    [
        lambda: token(exp=int(time.time()) - 10),  # expired
        lambda: token(aud="someone-else"),  # wrong audience
        lambda: token(iss="https://evil.test"),  # wrong issuer
        lambda: token(email=None),  # no e-mail claim
        lambda: "not-a-jwt",
    ],
    ids=["expired", "audience", "issuer", "no-email", "garbage"],
)
async def test_oidc_rejects_invalid_tokens(bad_token) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AuthenticationError):
        await oidc().authenticate(http_request({"Authorization": f"Bearer {bad_token()}"}))


async def test_oidc_requires_a_bearer_token() -> None:
    with pytest.raises(AuthenticationError):
        await oidc().authenticate(http_request())


def test_admin_check() -> None:
    open_mode = Authenticator(make_settings())
    open_mode.require_admin(http_request())  # local PoC mode: allowed
    guarded = Authenticator(make_settings(admin_token="adm1n"))
    guarded.require_admin(http_request({"Authorization": "Bearer adm1n"}))
    with pytest.raises(PermissionDeniedError):
        guarded.require_admin(http_request({"Authorization": "Bearer nope"}))


def test_rate_limiter_token_bucket() -> None:
    clock = {"now": 0.0}
    limiter = RateLimiter(rate_per_minute=60, burst=2, clock=lambda: clock["now"])
    limiter.check("a")
    limiter.check("a")
    with pytest.raises(TooManyRequestsError) as info:
        limiter.check("a")
    assert info.value.retry_after == 2
    limiter.check("b")  # other clients are independent
    clock["now"] = 1.0  # one token refilled after a second
    limiter.check("a")


def test_rate_limiter_disabled() -> None:
    limiter = RateLimiter(rate_per_minute=0, burst=1)
    for _ in range(100):
        limiter.check("a")
