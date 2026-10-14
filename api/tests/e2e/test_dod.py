"""End-to-end checks against the running stack (run: make e2e; or make e2e-fake in CI)."""

import asyncio
import os
import uuid

import httpx
import pytest

from tests.helpers.mailpit import MAILPIT_BASE_URL, find_message, message_headers

pytestmark = [pytest.mark.e2e, pytest.mark.anyio]

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8080")

CASES = [
    ("Nie działa mi komputer", "help-desk@example.com"),
    ("Chciałbym zgłosić urlop na jutro", "kadry@example.com"),
    ("Serwer produkcyjny nie odpowiada, nikt w firmie nie może pracować", "it@example.com"),
]


async def test_swagger_docs_are_available() -> None:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=10) as api:
        docs = await api.get("/api/v1/docs")
        schema = await api.get("/api/v1/openapi.json")
    assert docs.status_code == 200
    assert "swagger-ui" in docs.text
    assert "/api/v1/messages" in schema.json()["paths"]


async def test_stack_is_ready_and_exposes_metrics() -> None:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=10) as api:
        ready = await api.get("/api/v1/health/ready")
        metrics = await api.get("/metrics")
    assert ready.status_code == 200, ready.text
    assert "ai_router_info{" in metrics.text


@pytest.mark.parametrize(("message", "expected_department"), CASES)
async def test_message_is_routed_and_captured_with_reply_to(
    message: str, expected_department: str
) -> None:
    sender = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=200) as api:
        response = await api.post("/api/v1/messages", json={"email": sender, "message": message})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["routed_by"] == "agent", body
    assert body["department"] == expected_department, body["reason"]
    assert body["email"]["delivery"] == "sent"

    async with httpx.AsyncClient(base_url=MAILPIT_BASE_URL, timeout=10) as mailpit:
        captured = await find_message(mailpit, f'reply-to:"{sender}"')
        headers = await message_headers(mailpit, captured["ID"])
    assert captured["To"][0]["Address"] == expected_department
    assert captured["ReplyTo"][0]["Address"] == sender
    assert headers["x-request-id"] == [body["request_id"]]
    assert headers["x-ai-router-ticket"] == [body["ticket_code"]]
    assert body["ticket_code"] in captured["Subject"]
    assert message in captured["Text"]
    assert "/api/v1/feedback/" in captured["Text"]


async def test_async_mode_end_to_end() -> None:
    sender = f"e2e-async-{uuid.uuid4().hex[:8]}@example.com"
    payload = {"email": sender, "message": "Chciałbym zgłosić urlop na jutro"}
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30) as api:
        accepted = await api.post(
            "/api/v1/messages", json=payload, headers={"Prefer": "respond-async"}
        )
        assert accepted.status_code == 202, accepted.text
        location = accepted.headers["Location"]
        status: dict[str, object] = {}
        for _ in range(100):  # up to ~200 s on CPU
            status = (await api.get(location)).json()
            if status["status"] in ("routed", "failed"):
                break
            await asyncio.sleep(2)
    assert status["status"] == "routed", status
    result = status["result"]
    assert isinstance(result, dict) and result["department"] == "kadry@example.com"
    async with httpx.AsyncClient(base_url=MAILPIT_BASE_URL, timeout=10) as mailpit:
        captured = await find_message(mailpit, f'reply-to:"{sender}"')
    assert captured["To"][0]["Address"] == "kadry@example.com"
