import asyncio
import time
from email.message import EmailMessage
from typing import Any

import jwt
import pytest
from pydantic_ai import ModelAPIError

from ai_router.mail.gateway import InMemoryMailGateway, MailGatewayError
from ai_router.mail.sender import EmailDeliveryError
from ai_router.web.security import hash_api_key
from tests.conftest import ApiFactory
from tests.helpers.scripted_model import slow, text, tool_call
from tests.unit.test_security import PRIVATE_KEY, PUBLIC_KEY

pytestmark = pytest.mark.anyio

PAYLOAD = {"email": "jan.nowak@example.com", "message": "Nie działa mi komputer"}


class FailingSmtp:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        raise EmailDeliveryError("smtp down")


async def test_routes_message_through_the_agent_tool_and_outbox(api: ApiFactory) -> None:
    async with api(tool_call("help-desk@example.com", subject="Awaria PC"), text()) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
        ticket = await h.container.tickets.get(response.json()["ticket_id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "routed" and body["routed_by"] == "agent"
    assert body["department"] == "help-desk@example.com"
    assert body["ticket_code"].startswith("AIR-")
    assert body["email"]["delivery"] == "sent"
    assert body["email"]["subject"] == f"[AI Router] [{body['ticket_code']}] Awaria PC"
    assert body["sender_verification"] == "none"
    assert body["diagnostics"]["llm_requests"] == 2 and body["diagnostics"]["tool_calls"] == 1
    assert len(h.smtp.sent) == 1
    email = h.smtp.sent[0]
    assert email["To"] == "help-desk@example.com"
    assert email["Reply-To"] == "jan.nowak@example.com"
    assert email["X-Request-ID"] == response.headers["X-Request-ID"] == body["request_id"]
    assert ticket is not None and ticket.status == "routed"


async def test_smtp_outage_does_not_lose_the_message(api: ApiFactory) -> None:
    async with api(tool_call("it@example.com"), text(), smtp=FailingSmtp()) as h:  # type: ignore[arg-type]
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
        pending = await h.container.outbox.pending_count()
    assert response.status_code == 200
    assert response.json()["email"]["delivery"] == "queued"
    assert "delivery_queued" in response.json()["warnings"]
    assert pending == 1  # waiting in the outbox for the next retry


async def test_fallback_when_agent_fails(api: ApiFactory) -> None:
    async with api(text(), text(), text()) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    body = response.json()
    assert response.status_code == 200
    assert body["routed_by"] == "fallback" and body["department"] == "other@example.com"
    assert body["needs_review"] is True
    assert h.smtp.sent[0]["X-AI-Router-Routed-By"] == "fallback"


async def test_agent_failure_without_fallback_is_502(api: ApiFactory) -> None:
    async with api(text(), text(), text(), routing_fallback_enabled=False) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
        ticket = await h.container.ticket_repo.counts_by_status()
    assert response.status_code == 502
    assert response.json()["type"].endswith("/agent-failed")
    assert ticket["failed"] == 1  # the failure is part of the audit trail


async def test_llm_down_without_fallback_is_503_with_retry_after(api: ApiFactory) -> None:
    async with api(ModelAPIError("m", "down"), routing_fallback_enabled=False) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


async def test_storage_failure_is_503_without_internal_details(api: ApiFactory) -> None:
    failing = InMemoryMailGateway(fail_with=MailGatewayError("disk full at /app/data"))
    async with api(tool_call(), text(), gateway=failing) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    assert response.status_code == 503
    assert response.json()["type"].endswith("/storage-unavailable")
    assert "/app/data" not in response.text


async def test_overload_is_429(api: ApiFactory) -> None:
    steps = [slow(tool_call(), 0.4), text(), tool_call(), text()]
    async with api(*steps, max_concurrent_agent_runs=1, agent_queue_timeout_seconds=0.05) as h:
        first, second = await asyncio.gather(
            h.client.post("/api/v1/messages", json=PAYLOAD),
            h.client.post("/api/v1/messages", json=PAYLOAD),
        )
    assert sorted([first.status_code, second.status_code]) == [200, 429]


async def test_async_mode_returns_202_and_the_ticket_can_be_polled(api: ApiFactory) -> None:
    async with api(tool_call("kadry@example.com"), text()) as h:
        accepted = await h.client.post(
            "/api/v1/messages", json=PAYLOAD, headers={"Prefer": "respond-async"}
        )
        assert accepted.status_code == 202
        location = accepted.headers["Location"]
        assert accepted.json()["status"] == "queued"
        for _ in range(200):
            status = (await h.client.get(location)).json()
            if status["status"] == "routed":
                break
            await asyncio.sleep(0.02)
    assert status["status"] == "routed" and status["mode"] == "async"
    assert status["result"]["department"] == "kadry@example.com"
    assert [m["To"] for m in h.smtp.sent] == ["kadry@example.com"]


async def test_unknown_ticket_is_404(api: ApiFactory) -> None:
    async with api() as h:
        response = await h.client.get(f"/api/v1/messages/{'0' * 32}")
    assert response.status_code == 404
    assert response.json()["type"].endswith("/ticket-not-found")


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "message": "hej"},
        {"email": "jan@example.com"},
        {"email": "jan@example.com", "message": "   \n\t "},
        {"email": "jan@example.com", "message": "x" * 4001},
        {"email": "jan@example.com", "message": "a\x00b"},
    ],
    ids=["bad-email", "missing-message", "whitespace", "too-long", "nul-byte"],
)
async def test_invalid_input_is_422_problem(api: ApiFactory, payload: dict[str, str]) -> None:
    async with api() as h:
        response = await h.client.post("/api/v1/messages", json=payload)
    assert response.status_code == 422
    assert response.json()["errors"]


async def test_message_limit_comes_from_settings_and_is_documented(api: ApiFactory) -> None:
    async with api(max_message_chars=150) as h:
        schema = (await h.client.get("/api/v1/openapi.json")).json()
        too_long = await h.client.post(
            "/api/v1/messages", json={"email": "jan@example.com", "message": "x" * 151}
        )
    message = schema["components"]["schemas"]["RouteMessageRequest"]["properties"]["message"]
    assert message["maxLength"] == 150 and message["minLength"] == 1
    assert too_long.status_code == 422
    assert too_long.json()["errors"][0]["loc"] == ["body", "message"]
    assert h.smtp.sent == []


async def test_idempotency_key_prevents_a_second_email(api: ApiFactory) -> None:
    headers = {"Idempotency-Key": "order-1"}
    async with api(tool_call(), text()) as h:
        first = await h.client.post("/api/v1/messages", json=PAYLOAD, headers=headers)
        second = await h.client.post("/api/v1/messages", json=PAYLOAD, headers=headers)
        conflict = await h.client.post(
            "/api/v1/messages", json={**PAYLOAD, "message": "inna treść"}, headers=headers
        )
    assert first.status_code == second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert conflict.status_code == 422
    assert len(h.smtp.sent) == 1


async def test_api_key_mode_requires_a_key(api: ApiFactory) -> None:
    settings: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": f"intranet:{hash_api_key('k3y')}",
    }
    async with api(tool_call(), text(), **settings) as h:
        missing = await h.client.post("/api/v1/messages", json=PAYLOAD)
        ok = await h.client.post("/api/v1/messages", json=PAYLOAD, headers={"X-API-Key": "k3y"})
    assert missing.status_code == 401
    assert ok.status_code == 200 and ok.json()["sender_verification"] == "client"
    assert "nie został zweryfikowany" not in h.smtp.sent[0].get_body(("plain",)).get_content()  # type: ignore[union-attr]


def sso_token(email: str = "jan.nowak@example.com") -> str:
    now = int(time.time())
    claims = {"sub": "u-7", "email": email, "aud": "ai-router", "iat": now, "exp": now + 300}
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256")


async def test_oidc_mode_verifies_the_sender(api: ApiFactory) -> None:
    settings: dict[str, Any] = {
        "auth_mode": "oidc",
        "oidc_jwks_url": "https://sso.test/jwks",
        "oidc_audience": "ai-router",
    }
    async with api(tool_call(), text(), key_resolver=lambda _t: PUBLIC_KEY, **settings) as h:
        spoofed = await h.client.post(
            "/api/v1/messages",
            json={**PAYLOAD, "email": "prezes@example.com"},
            headers={"Authorization": f"Bearer {sso_token()}"},
        )
        ok = await h.client.post(
            "/api/v1/messages", json=PAYLOAD, headers={"Authorization": f"Bearer {sso_token()}"}
        )
    assert spoofed.status_code == 403  # cannot send as somebody else
    assert ok.status_code == 200 and ok.json()["sender_verification"] == "sso"
    assert h.smtp.sent[0]["X-AI-Router-Sender-Verification"] == "sso"


async def test_rate_limit_per_client(api: ApiFactory) -> None:
    steps = [tool_call(), text(), tool_call(), text()]
    async with api(*steps, rate_limit_per_minute=1, rate_limit_burst=2) as h:
        statuses = [
            (await h.client.post("/api/v1/messages", json=PAYLOAD)).status_code for _ in range(3)
        ]
        limited = await h.client.post("/api/v1/messages", json=PAYLOAD)
    assert statuses == [200, 200, 429]
    assert int(limited.headers["Retry-After"]) >= 1
