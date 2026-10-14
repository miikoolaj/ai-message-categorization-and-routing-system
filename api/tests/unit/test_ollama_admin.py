import json

import httpx
import pytest

from ai_router.llm.ollama_admin import (
    OllamaAdminClient,
    is_loaded,
    keep_alive_value,
    keeps_model_loaded,
)

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
    await client_with(handler).warm_up("qwen3", "-1")
    assert bodies == [
        {"model": "qwen3", "keep_alive": "24h", "stream": False},
        {"model": "qwen3", "keep_alive": -1, "stream": False},  # a JSON number, not "-1"
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("-1", -1), ("300", 300), ("0.5", 0.5), ("30m", "30m"), ("1h30m", "1h30m")],
)
def test_keep_alive_value(value: str, expected: object) -> None:
    assert keep_alive_value(value) == expected


async def test_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["model"]
        if name == "missing":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"capabilities": ["completion", "tools"]})

    admin = client_with(handler)
    assert await admin.capabilities("qwen3") == {"completion", "tools"}
    assert await admin.capabilities("missing") is None


async def test_loaded_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [{"name": "qwen3:4b", "model": "qwen3:4b"}]})

    assert await client_with(handler).loaded_models() == {"qwen3:4b"}
    assert (
        await client_with(lambda r: httpx.Response(200, json={"models": []})).loaded_models()
        == set()
    )


def test_keep_alive_semantics() -> None:
    assert keeps_model_loaded("-1") and keeps_model_loaded(" -1m ")
    assert not keeps_model_loaded("30m") and not keeps_model_loaded("0")
    assert is_loaded("qwen3:4b", {"qwen3:4b"})
    assert is_loaded("llama3", {"llama3:latest"})
    assert not is_loaded("qwen3:4b", {"qwen3:8b"})
