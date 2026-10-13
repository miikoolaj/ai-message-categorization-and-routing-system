import json

import httpx
import pytest

from ai_router.llm.ollama_admin import OllamaAdminClient

pytestmark = pytest.mark.anyio


def client_with(handler) -> OllamaAdminClient:  # type: ignore[no-untyped-def]
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaAdminClient(http, "http://ollama:11434/")


async def test_version() -> None:
    admin = client_with(lambda request: httpx.Response(200, json={"version": "0.40.3"}))
    assert await admin.version() == "0.40.3"


async def test_has_model_uses_native_show_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = json.loads(request.content)["model"]
        return httpx.Response(200 if name == "qwen3" else 404, json={})

    admin = client_with(handler)
    assert await admin.has_model("qwen3") is True
    assert await admin.has_model("missing") is False
    assert str(seen[0].url) == "http://ollama:11434/api/show"


async def test_has_model_raises_on_server_error() -> None:
    admin = client_with(lambda request: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await admin.has_model("qwen3")


async def test_warm_up_sends_empty_generate_request_with_keep_alive() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "load"})

    await client_with(handler).warm_up("qwen3", "24h")
    assert bodies == [{"model": "qwen3", "keep_alive": "24h", "stream": False}]
