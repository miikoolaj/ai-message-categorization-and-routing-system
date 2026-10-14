"""Contract test of the wire format between the agent and Ollama (production model factory).

A fake OpenAI-compatible server on a real port records the JSON bodies, so the test checks exactly
what Ollama would receive - independent of whether the OpenAI SDK uses httpx or httpx2. It guards
the two most expensive bugs of the project:

* `max_tokens` must be sent (Ollama ignores `max_completion_tokens`),
* the department schema must be a flat enum (Ollama drops `$ref` and `const`),
* `LLM_THINKING` must reach Ollama as `reasoning_effort` (it used to be silently dropped).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import uvicorn
from pydantic_ai import models
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ai_router.llm.model import build_ollama_model
from ai_router.routing.agent import build_agent
from tests.conftest import POLICY, make_settings
from tests.unit.test_agent import setup

pytestmark = pytest.mark.anyio

TOOL_ARGS = {
    "reason": "Awaria sprzętu jednego użytkownika",
    "department": "help-desk@example.com",
    "confidence": 0.9,
    "subject": "Komputer nie działa",
}


def completion(message: dict[str, Any], finish_reason: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1_760_000_000,
        "model": "qwen3:4b-instruct-2507-q4_K_M",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": {"prompt_tokens": 1400, "completion_tokens": 40, "total_tokens": 1440},
    }


TOOL_CALL = completion(
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "send_email", "arguments": json.dumps(TOOL_ARGS)},
            }
        ],
    },
    "tool_calls",
)
TEXT = completion({"role": "assistant", "content": "Message forwarded."}, "stop")


@pytest.fixture
async def fake_ollama() -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    captured: list[dict[str, Any]] = []

    async def chat(request: Request) -> JSONResponse:
        captured.append(await request.json())
        return JSONResponse(TOOL_CALL if len(captured) == 1 else TEXT)

    app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"])])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn only exposes a flag
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", captured
    server.should_exit = True
    await task


async def test_wire_format_sent_to_ollama(
    fake_ollama: tuple[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)  # the production model class
    base_url, captured = fake_ollama
    settings = make_settings(ollama_base_url=base_url)
    handle = build_ollama_model(settings)
    routing_agent = build_agent(settings, handle.model, POLICY)
    _, deps, mailbox, _ = setup()
    try:
        await routing_agent.agent.run("Route the following message.", deps=deps)
    finally:
        await handle.close()

    first = captured[0]
    assert first["model"] == "qwen3:4b-instruct-2507-q4_K_M"
    assert first["max_tokens"] == settings.llm_max_tokens
    assert "max_completion_tokens" not in first  # Ollama would silently ignore it
    assert first["temperature"] == 0.0 and first["seed"] == 42
    assert first["messages"][0]["role"] == "system"
    assert "message-routing agent" in first["messages"][0]["content"]

    tools_json = json.dumps(first["tools"])
    assert "$ref" not in tools_json and '"const"' not in tools_json  # Ollama would drop them
    function = first["tools"][0]["function"]
    assert function["name"] == "send_email"
    department = function["parameters"]["properties"]["department"]
    assert department == {**department, "type": "string", "enum": list(POLICY.addresses)}
    assert "reply_to" not in function["parameters"]["properties"]

    second = captured[1]  # tool result goes back with the original tool_call_id
    assert second["messages"][-1]["role"] == "tool"
    assert second["messages"][-1]["tool_call_id"] == "call_1"
    assert [m["To"] for m in mailbox.messages] == ["help-desk@example.com"]


async def test_without_the_profile_override_the_token_limit_would_be_lost(
    fake_ollama: tuple[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documents the library default the override exists for. If this test starts failing,
    Pydantic AI changed its default and the override in llm/model.py may no longer be needed."""
    import ai_router.llm.model as model_module

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setattr(model_module, "OLLAMA_PROFILE_OVERRIDES", {})
    base_url, captured = fake_ollama
    settings = make_settings(ollama_base_url=base_url)
    handle = model_module.build_ollama_model(settings)
    _, deps, _, _ = setup()
    try:
        await build_agent(settings, handle.model, POLICY).agent.run("Route.", deps=deps)
    finally:
        await handle.close()
    assert "max_completion_tokens" in captured[0]
    assert "max_tokens" not in captured[0]


@pytest.mark.parametrize(("thinking", "effort"), [(False, "none"), (True, "medium"), (None, None)])
async def test_thinking_setting_reaches_ollama(
    fake_ollama: tuple[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    thinking: bool | None,
    effort: str | None,
) -> None:
    """Regression: LLM_THINKING was silently dropped (the bundled Qwen profile does not declare
    thinking support), so hybrid models such as qwen3.5 kept thinking and never called the tool."""
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    base_url, captured = fake_ollama
    settings = make_settings(ollama_base_url=base_url, llm_thinking=thinking)
    handle = build_ollama_model(settings)
    _, deps, _, _ = setup()
    try:
        await build_agent(settings, handle.model, POLICY).agent.run("Route.", deps=deps)
    finally:
        await handle.close()
    assert captured[0].get("reasoning_effort") == effort
    assert captured[0]["temperature"] == 0.0 and captured[0]["seed"] == 42  # sampling params kept
