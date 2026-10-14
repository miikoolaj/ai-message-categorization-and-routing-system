"""Shared pytest configuration and fixtures."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic_ai import models

from ai_router.config import Settings
from ai_router.container import AppContainer, build_container
from ai_router.mail.gateway import MailGateway
from ai_router.mail.sender import InMemoryEmailSender
from ai_router.main import create_app
from ai_router.routing.policy import RoutingPolicy, load_policy
from ai_router.web.security import KeyResolver
from tests.helpers.fakes import FakeOllamaAdmin
from tests.helpers.scripted_model import ScriptedModel, Step

# Unit tests must never reach a real LLM (integration tests re-enable it explicitly).
models.ALLOW_MODEL_REQUESTS = False

POLICY: RoutingPolicy = load_policy()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_settings(**overrides: Any) -> Settings:
    """Deterministic settings for tests (explicit values win over environment variables)."""
    defaults: dict[str, Any] = {
        "warmup_on_startup": False,
        "smtp_host": "127.0.0.1",
        "smtp_port": 1,
        "log_format": "console",
        "database_path": ":memory:",
        "rate_limit_per_minute": 0,
        "public_base_url": "http://test",
        "static_dir": "/nonexistent",
        "mail_sync_delivery_timeout_seconds": 2.0,
        "auth_mode": "none",
        "llm_backend": "ollama",
        "routing_policy_file": None,
    }
    return Settings(**{**defaults, **overrides})


@dataclass
class FakeSmtpServer:
    host: str
    port: int


@pytest.fixture
async def fake_smtp_server() -> AsyncIterator[FakeSmtpServer]:
    """Minimal TCP server that greets like an SMTP server (enough for readiness checks)."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 fake-smtp ESMTP ready\r\n")
        await writer.drain()
        await reader.readline()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield FakeSmtpServer("127.0.0.1", port)


@dataclass
class ApiHarness:
    client: httpx.AsyncClient
    app: FastAPI
    model: ScriptedModel
    smtp: InMemoryEmailSender
    admin: FakeOllamaAdmin

    @property
    def container(self) -> AppContainer:
        container: AppContainer = self.app.state.container
        return container


ApiFactory = Callable[..., Any]


@pytest.fixture
def api() -> ApiFactory:
    """`async with api(*model_steps, **settings_overrides) as h:` - app wired with fakes.

    The real outbox runs on an in-memory SQLite database and "delivers" to `h.smtp`.
    """

    @asynccontextmanager
    async def factory(
        *steps: Step,
        smtp: InMemoryEmailSender | None = None,
        admin: FakeOllamaAdmin | None = None,
        gateway: MailGateway | None = None,
        key_resolver: KeyResolver | None = None,
        **overrides: Any,
    ) -> AsyncIterator[ApiHarness]:
        settings = make_settings(**overrides)
        scripted = ScriptedModel(*steps)
        sender = smtp if smtp is not None else InMemoryEmailSender()
        fake_admin = admin or FakeOllamaAdmin()

        def container_factory(s: Settings) -> AppContainer:
            return build_container(
                s,
                model=scripted.model,
                smtp_sender=sender,
                gateway=gateway,
                ollama_admin=fake_admin,
                key_resolver=key_resolver,
            )

        app = create_app(settings, container_factory=container_factory)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield ApiHarness(client, app, scripted, sender, fake_admin)

    return factory
