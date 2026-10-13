"""Helpers for asserting on e-mails captured by Mailpit (REST API)."""

import asyncio
import os
from typing import Any

import httpx

MAILPIT_BASE_URL = os.environ.get("MAILPIT_BASE_URL", "http://127.0.0.1:8025")


async def find_message(
    client: httpx.AsyncClient, query: str, *, wait_seconds: float = 30.0
) -> dict[str, Any]:
    """Poll `GET /api/v1/search` until a message matches, then return its full JSON."""
    try:
        async with asyncio.timeout(wait_seconds):
            while True:
                response = await client.get("/api/v1/search", params={"query": query})
                response.raise_for_status()
                messages = response.json().get("messages", [])
                if messages:
                    detail = await client.get(f"/api/v1/message/{messages[0]['ID']}")
                    detail.raise_for_status()
                    result: dict[str, Any] = detail.json()
                    return result
                await asyncio.sleep(0.5)
    except TimeoutError:
        raise AssertionError(f"no e-mail matching {query!r} within {wait_seconds} s") from None


async def message_headers(client: httpx.AsyncClient, message_id: str) -> dict[str, list[str]]:
    """Raw headers with lower-cased names (Mailpit returns canonicalised names)."""
    response = await client.get(f"/api/v1/message/{message_id}/headers")
    response.raise_for_status()
    return {name.lower(): values for name, values in response.json().items()}
