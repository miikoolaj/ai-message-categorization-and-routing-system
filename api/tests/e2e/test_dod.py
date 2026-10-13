"""End-to-end Definition of Done checks against the running stack (run: make e2e)."""

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


async def test_stack_is_ready() -> None:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=10) as api:
        response = await api.get("/api/v1/health/ready")
    assert response.status_code == 200, response.text


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

    async with httpx.AsyncClient(base_url=MAILPIT_BASE_URL, timeout=10) as mailpit:
        captured = await find_message(mailpit, f'reply-to:"{sender}"')
        headers = await message_headers(mailpit, captured["ID"])
    assert captured["To"][0]["Address"] == expected_department
    assert captured["ReplyTo"][0]["Address"] == sender
    assert headers["x-request-id"] == [body["request_id"]]
    assert message in captured["Text"]
