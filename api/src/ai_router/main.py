"""FastAPI application factory (entry point: `uvicorn ai_router.main:app`)."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ai_router import __version__
from ai_router.config import Settings, get_settings
from ai_router.container import AppContainer, build_container
from ai_router.logging_config import configure_logging
from ai_router.observability.tracing import setup_tracing
from ai_router.routing.policy import load_policy
from ai_router.routing.prompts import departments_markdown
from ai_router.web.errors import register_exception_handlers
from ai_router.web.middleware import RequestContextMiddleware
from ai_router.web.routers import feedback, health, messages

ContainerFactory = Callable[[Settings], AppContainer]

OPENAPI_URL = "/api/v1/openapi.json"
CDN = "https://cdn.jsdelivr.net/npm"

API_DESCRIPTION = """\
Intelligent message router (Proof of Concept).

Send a free-form message: a local LLM agent (Ollama) interprets it, chooses the responsible
department and forwards the message **by calling its `send_email` tool** (function calling).
The forwarded e-mail has `Reply-To` set to the sender and is delivered through a durable outbox.

* Mailpit (captured e-mails): http://localhost:8025
* On CPU a synchronous request usually takes **5-20 s**; use `Prefer: respond-async` to get a
  ticket immediately and poll its status.
* Prometheus metrics: `/metrics`

### Departments (routing policy `{policy_version}`)

{departments}
"""


def _docs_routes(app: FastAPI, settings: Settings) -> None:
    """Swagger UI / ReDoc served from local assets when available (works offline)."""
    static = Path(settings.static_dir)
    local = (static / "swagger-ui-bundle.js").is_file()
    if local:
        app.mount("/api/v1/static", StaticFiles(directory=static), name="static")
    base = "/api/v1/static" if local else None
    swagger_js = (
        f"{base}/swagger-ui-bundle.js" if base else f"{CDN}/swagger-ui-dist@5/swagger-ui-bundle.js"
    )
    swagger_css = f"{base}/swagger-ui.css" if base else f"{CDN}/swagger-ui-dist@5/swagger-ui.css"
    favicon = (
        f"{base}/favicon-32x32.png" if base else "https://fastapi.tiangolo.com/img/favicon.png"
    )
    redoc_js = (
        f"{base}/redoc.standalone.js" if base else f"{CDN}/redoc@2/bundles/redoc.standalone.js"
    )

    @app.get("/api/v1/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=OPENAPI_URL,
            title="AI Message Router - Swagger UI",
            swagger_js_url=swagger_js,
            swagger_css_url=swagger_css,
            swagger_favicon_url=favicon,
        )

    @app.get("/api/v1/redoc", include_in_schema=False)
    async def redoc() -> HTMLResponse:
        return get_redoc_html(
            openapi_url=OPENAPI_URL,
            title="AI Message Router - ReDoc",
            redoc_js_url=redoc_js,
            redoc_favicon_url=favicon,
            with_google_fonts=False,
        )


def _publish_limits_in_openapi(app: FastAPI, settings: Settings) -> None:
    """Show the configured MAX_MESSAGE_CHARS as `maxLength` in the schema (and in Swagger UI)."""
    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = default_openapi()
            message = schema["components"]["schemas"]["RouteMessageRequest"]["properties"]
            message["message"].update(minLength=1, maxLength=settings.max_message_chars)
        return app.openapi_schema  # type: ignore[return-value]

    app.openapi = openapi  # type: ignore[method-assign]


def create_app(
    settings: Settings | None = None,
    container_factory: ContainerFactory = build_container,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    policy = load_policy(settings.routing_policy_file)  # validated before anything else starts
    shutdown_tracing: Callable[[], None] | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = container_factory(settings)
        app.state.container = container
        await container.start()
        try:
            yield
        finally:
            await container.aclose()
            if shutdown_tracing is not None:
                shutdown_tracing()

    app = FastAPI(
        title="AI Message Router",
        version=__version__,
        description=API_DESCRIPTION.format(
            departments=departments_markdown(policy), policy_version=policy.version
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_URL,
        openapi_tags=[
            {"name": "messages", "description": "Message categorisation and routing."},
            {"name": "feedback", "description": "Corrections from departments (feedback loop)."},
            {"name": "health", "description": "Liveness and readiness probes."},
        ],
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware, max_body_bytes=64 * 1024)
    register_exception_handlers(app)
    app.include_router(messages.router)
    app.include_router(feedback.router)
    app.include_router(health.router)
    _docs_routes(app, settings)
    _publish_limits_in_openapi(app, settings)
    shutdown_tracing = setup_tracing(settings, app)

    if settings.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics() -> Response:
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/api/v1/docs")

    return app


app = create_app()
