"""FastAPI application factory (entry point: `uvicorn ai_router.main:app`)."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from ai_router import __version__
from ai_router.config import Settings, get_settings
from ai_router.container import AppContainer, build_container
from ai_router.logging_config import configure_logging
from ai_router.routing.prompts import departments_markdown
from ai_router.web.errors import register_exception_handlers
from ai_router.web.middleware import RequestContextMiddleware
from ai_router.web.routers import health, messages

ContainerFactory = Callable[[Settings], AppContainer]

API_DESCRIPTION = """\
Intelligent message router (Proof of Concept).

Send a free-form message: a local LLM agent (Ollama) interprets it, chooses the responsible
department and forwards the message **by calling its `send_email` tool** (function calling).
The forwarded e-mail has `Reply-To` set to the sender's address.

* Mailpit (captured e-mails): http://localhost:8025
* On CPU a request usually takes **5-20 s** (the first one after start may take longer).

### Departments

{departments}
"""


def create_app(
    settings: Settings | None = None,
    container_factory: ContainerFactory = build_container,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = container_factory(settings)
        app.state.container = container
        await container.start()
        try:
            yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="AI Message Router",
        version=__version__,
        description=API_DESCRIPTION.format(departments=departments_markdown()),
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        openapi_tags=[
            {"name": "messages", "description": "Message categorisation and routing."},
            {"name": "health", "description": "Liveness and readiness probes."},
        ],
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware, max_body_bytes=64 * 1024)
    register_exception_handlers(app)
    app.include_router(messages.router)
    app.include_router(health.router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/api/v1/docs")

    return app


app = create_app()
