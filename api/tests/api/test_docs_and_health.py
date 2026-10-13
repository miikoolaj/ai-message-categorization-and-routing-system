import pytest

from tests.conftest import ApiFactory, FakeSmtpServer
from tests.helpers.fakes import FakeOllamaAdmin

pytestmark = pytest.mark.anyio


async def test_swagger_ui_is_served_under_api_v1_docs(api: ApiFactory) -> None:
    async with api() as h:
        response = await h.client.get("/api/v1/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text


async def test_openapi_schema_describes_the_endpoint(api: ApiFactory) -> None:
    async with api() as h:
        schema = (await h.client.get("/api/v1/openapi.json")).json()
    assert schema["info"]["title"] == "AI Message Router"
    operation = schema["paths"]["/api/v1/messages"]["post"]
    assert {"200", "422", "429", "502", "503", "504"} <= set(operation["responses"])
    examples = operation["requestBody"]["content"]["application/json"]["examples"]
    assert "hardware" in examples
    assert "kadry@example.com" in schema["info"]["description"]


async def test_root_redirects_to_docs(api: ApiFactory) -> None:
    async with api() as h:
        response = await h.client.get("/")
    assert response.status_code == 307
    assert response.headers["location"] == "/api/v1/docs"


async def test_liveness(api: ApiFactory) -> None:
    async with api() as h:
        response = await h.client.get("/api/v1/health/live")
    assert response.json() == {"status": "ok"}


async def test_readiness_ok(api: ApiFactory, fake_smtp_server: FakeSmtpServer) -> None:
    async with api(smtp_host=fake_smtp_server.host, smtp_port=fake_smtp_server.port) as h:
        response = await h.client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_readiness_503_when_model_missing(
    api: ApiFactory, fake_smtp_server: FakeSmtpServer
) -> None:
    admin = FakeOllamaAdmin(model_present=False)
    async with api(
        admin=admin, smtp_host=fake_smtp_server.host, smtp_port=fake_smtp_server.port
    ) as h:
        response = await h.client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["components"]["model"]["status"] == "down"


async def test_warmup_runs_in_background_on_startup(api: ApiFactory) -> None:
    admin = FakeOllamaAdmin()
    async with api(admin=admin, warmup_on_startup=True) as h:
        for _ in range(50):
            if h.container.warmup.status == "warm":
                break
            import asyncio

            await asyncio.sleep(0.01)
    assert admin.warmups == ["qwen3:4b-instruct-2507-q4_K_M"]


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
    body = response.json()
    assert body["type"].endswith("/not-found")
    assert body["request_id"] == response.headers["X-Request-ID"]


async def test_unhandled_exception_returns_500_problem_with_request_id(api: ApiFactory) -> None:
    async def boom() -> None:
        raise RuntimeError("unexpected")

    async with api() as h:
        h.app.add_api_route("/boom", boom)
        response = await h.client.get("/boom", headers={"X-Request-ID": "trace-500"})
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "trace-500"
    body = response.json()
    assert body["type"].endswith("/internal-error")
    assert body["request_id"] == "trace-500"
    assert "unexpected" not in response.text  # no internal details leak


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
    assert response.json()["type"].endswith("/payload-too-large")
