import asyncio

import pytest

from tests.conftest import ApiFactory, FakeSmtpServer
from tests.helpers.fakes import FakeOllamaAdmin

pytestmark = pytest.mark.anyio


async def test_swagger_ui_is_served_under_api_v1_docs(api: ApiFactory) -> None:
    async with api() as h:
        docs = await h.client.get("/api/v1/docs")
        redoc = await h.client.get("/api/v1/redoc")
    assert docs.status_code == 200 and "swagger-ui" in docs.text
    assert "cdn.jsdelivr.net" in docs.text  # no local assets in tests -> CDN fallback
    assert redoc.status_code == 200 and "redoc" in redoc.text.lower()


async def test_swagger_ui_uses_local_assets_when_present(api: ApiFactory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "swagger-ui-bundle.js",
        "swagger-ui.css",
        "redoc.standalone.js",
        "favicon-32x32.png",
    ):
        (tmp_path / name).write_text("/* asset */")
    async with api(static_dir=str(tmp_path)) as h:
        docs = await h.client.get("/api/v1/docs")
        asset = await h.client.get("/api/v1/static/swagger-ui-bundle.js")
    assert "/api/v1/static/swagger-ui-bundle.js" in docs.text
    assert "cdn.jsdelivr.net" not in docs.text  # works offline
    assert asset.status_code == 200


async def test_openapi_schema_describes_the_api(api: ApiFactory) -> None:
    async with api() as h:
        schema = (await h.client.get("/api/v1/openapi.json")).json()
    post = schema["paths"]["/api/v1/messages"]["post"]
    assert {"200", "202", "401", "422", "429", "502", "503", "504"} <= set(post["responses"])
    assert "hardware" in post["requestBody"]["content"]["application/json"]["examples"]
    assert "/api/v1/messages/{ticket_id}" in schema["paths"]
    assert "/api/v1/feedback/{ticket_id}" in schema["paths"]
    assert "kadry@example.com" in schema["info"]["description"]


async def test_root_redirects_to_docs(api: ApiFactory) -> None:
    async with api() as h:
        response = await h.client.get("/")
    assert response.status_code == 307 and response.headers["location"] == "/api/v1/docs"


async def test_liveness_and_readiness(api: ApiFactory, fake_smtp_server: FakeSmtpServer) -> None:
    async with api(smtp_host=fake_smtp_server.host, smtp_port=fake_smtp_server.port) as h:
        live = await h.client.get("/api/v1/health/live")
        ready = await h.client.get("/api/v1/health/ready")
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200 and ready.json()["components"]["storage"]["status"] == "up"


async def test_readiness_503_when_model_lacks_tools(
    api: ApiFactory, fake_smtp_server: FakeSmtpServer
) -> None:
    admin = FakeOllamaAdmin(capabilities={"completion"})
    async with api(
        admin=admin, smtp_host=fake_smtp_server.host, smtp_port=fake_smtp_server.port
    ) as h:
        response = await h.client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["components"]["model"]["detail"] == "model does not support tool calling"


async def test_metrics_endpoint_exposes_application_metrics(api: ApiFactory) -> None:
    async with api() as h:
        await h.client.get("/api/v1/health/live")
        metrics = await h.client.get("/metrics")
    assert metrics.status_code == 200
    assert "ai_router_http_requests_total" in metrics.text
    assert 'route="/api/v1/health/live"' in metrics.text
    assert "ai_router_info{" in metrics.text


async def test_warmup_runs_in_background_and_primes_the_prompt_cache(api: ApiFactory) -> None:
    from tests.helpers.scripted_model import text, tool_call

    admin = FakeOllamaAdmin()
    async with api(tool_call(), text(), admin=admin, warmup_on_startup=True) as h:
        for _ in range(100):
            if h.container.warmup.status == "warm":
                break
            await asyncio.sleep(0.01)
        assert h.container.warmup.status == "warm"
        assert h.model.remaining_steps == 0  # the synthetic agent run consumed both steps
    assert admin.warmups == ["qwen3:4b-instruct-2507-q4_K_M"]


async def test_watchdog_warms_the_model_again_after_ollama_restart(api: ApiFactory) -> None:
    from tests.helpers.scripted_model import text, tool_call

    admin = FakeOllamaAdmin()
    steps = (tool_call(), text(), tool_call(), text())
    async with api(
        *steps, admin=admin, warmup_on_startup=True, model_watchdog_interval_seconds=0.01
    ) as h:
        for _ in range(200):
            if h.container.warmup.status == "warm":
                break
            await asyncio.sleep(0.01)
        assert admin.warmups == ["qwen3:4b-instruct-2507-q4_K_M"]
        admin.loaded.clear()  # Ollama restarted: the model is no longer in memory
        for _ in range(200):
            if len(admin.warmups) == 2 and h.container.warmup.status == "warm":
                break
            await asyncio.sleep(0.01)
        assert len(admin.warmups) == 2
        assert h.container.warmup.status == "warm"
        assert h.model.remaining_steps == 0  # the prompt cache was primed again


async def test_request_id_is_echoed_or_generated(api: ApiFactory) -> None:
    async with api() as h:
        echoed = await h.client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-123"})
        generated = await h.client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id!"})
    assert echoed.headers["X-Request-ID"] == "abc-123"
    assert generated.headers["X-Request-ID"] not in ("bad id!", "")


async def test_unknown_route_is_problem_json(api: ApiFactory) -> None:
    async with api() as h:
        response = await h.client.get("/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_unhandled_exception_returns_500_problem_with_request_id(api: ApiFactory) -> None:
    async def boom() -> None:
        raise RuntimeError("unexpected secret detail")

    async with api() as h:
        h.app.add_api_route("/boom", boom)
        response = await h.client.get("/boom", headers={"X-Request-ID": "trace-500"})
    assert response.status_code == 500
    assert response.json()["request_id"] == "trace-500"
    assert "secret detail" not in response.text


async def test_streamed_body_over_limit_is_413(api: ApiFactory) -> None:
    async def chunks():  # type: ignore[no-untyped-def]
        yield b'{"email": "a@example.com", "message": "'
        for _ in range(20):
            yield b"x" * 4096
        yield b'"}'

    async with api() as h:
        response = await h.client.post(
            "/api/v1/messages", content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413
