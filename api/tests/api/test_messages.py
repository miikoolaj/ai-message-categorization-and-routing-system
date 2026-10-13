import asyncio
from email.message import EmailMessage

import pytest
from pydantic_ai import ModelAPIError

from ai_router.mail.sender import EmailDeliveryError, InMemoryEmailSender
from tests.conftest import ApiFactory
from tests.helpers.scripted_model import slow, text, tool_call

pytestmark = pytest.mark.anyio

PAYLOAD = {"email": "jan.nowak@example.com", "message": "Nie działa mi komputer"}


def only_email(mailer: InMemoryEmailSender) -> EmailMessage:
    assert len(mailer.sent) == 1
    return mailer.sent[0]


async def test_routes_message_through_the_agent_tool(api: ApiFactory) -> None:
    async with api(tool_call("help-desk@example.com", subject="Awaria PC"), text()) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "routed"
    assert body["routed_by"] == "agent"
    assert body["department"] == "help-desk@example.com"
    assert body["email"]["reply_to"] == "jan.nowak@example.com"
    assert body["email"]["subject"] == "[AI Router] Awaria PC"
    assert body["diagnostics"]["llm_requests"] == 2
    email = only_email(h.mailer)
    assert email["To"] == "help-desk@example.com"
    assert email["Reply-To"] == "jan.nowak@example.com"
    assert email["Message-ID"] == body["email"]["message_id"]
    assert email["X-Request-ID"] == response.headers["X-Request-ID"] == body["request_id"]


async def test_fallback_when_agent_fails(api: ApiFactory) -> None:
    async with api(text(), text(), text()) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    body = response.json()
    assert response.status_code == 200
    assert body["routed_by"] == "fallback"
    assert body["department"] == "other@example.com"
    assert body["warnings"] == ["agent_failed: model_behavior"]
    assert only_email(h.mailer)["X-AI-Router-Routed-By"] == "fallback"


async def test_agent_failure_without_fallback_is_502(api: ApiFactory) -> None:
    async with api(text(), text(), text(), routing_fallback_enabled=False) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    assert response.status_code == 502
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith("/agent-failed")
    assert h.mailer.sent == []


async def test_llm_down_without_fallback_is_503_with_retry_after(api: ApiFactory) -> None:
    async with api(ModelAPIError("m", "down"), routing_fallback_enabled=False) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.json()["type"].endswith("/llm-unavailable")


async def test_smtp_down_is_503(api: ApiFactory) -> None:
    failing = InMemoryEmailSender(fail_with=EmailDeliveryError("down"))
    async with api(tool_call(), text(), mailer=failing) as h:
        response = await h.client.post("/api/v1/messages", json=PAYLOAD)
    assert response.status_code == 503
    assert response.json()["type"].endswith("/smtp-unavailable")


async def test_too_many_concurrent_requests_is_429(api: ApiFactory) -> None:
    steps = [slow(tool_call(), 0.4), text(), tool_call(), text()]
    async with api(*steps, max_concurrent_agent_runs=1, agent_queue_timeout_seconds=0.05) as h:
        first, second = await asyncio.gather(
            h.client.post("/api/v1/messages", json=PAYLOAD),
            h.client.post("/api/v1/messages", json=PAYLOAD),
        )
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 429]
    rejected = first if first.status_code == 429 else second
    assert "Retry-After" in rejected.headers


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "message": "hej"},
        {"email": "jan@example.com"},
        {"email": "jan@example.com", "message": ""},
        {"email": "jan@example.com", "message": "   \n\t "},
        {"email": "jan@example.com", "message": "x" * 4001},
        {"email": "jan@example.com", "message": "a\x00b"},
    ],
    ids=["bad-email", "missing-message", "empty", "whitespace", "too-long", "nul-byte"],
)
async def test_invalid_input_is_422_problem(api: ApiFactory, payload: dict[str, str]) -> None:
    async with api() as h:
        response = await h.client.post("/api/v1/messages", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["type"].endswith("/validation-error")
    assert body["errors"]
    assert h.mailer.sent == []


async def test_message_is_cleaned_before_routing(api: ApiFactory) -> None:
    async with api(tool_call(), text()) as h:
        await h.client.post(
            "/api/v1/messages",
            json={"email": "jan@example.com", "message": "  Linia 1\r\nLinia 2\x07  "},
        )
    assert "Linia 1\nLinia 2\n" in only_email(h.mailer).get_content()


async def test_oversized_body_is_413(api: ApiFactory) -> None:
    async with api() as h:
        response = await h.client.post(
            "/api/v1/messages",
            content=b'{"email": "a@example.com", "message": "' + b"x" * 70_000 + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["type"].endswith("/payload-too-large")


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
    assert len(h.mailer.sent) == 1
