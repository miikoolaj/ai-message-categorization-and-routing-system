"""Composition root: builds and owns all long-lived application components.

`build_container()` wires the production implementations. Tests pass their own model, mailer
and Ollama admin client (dependency injection instead of monkeypatching).
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

import httpx
from pydantic_ai import Agent
from pydantic_ai.models import Model

from ai_router.config import Settings
from ai_router.health import ReadinessChecker, WarmupState
from ai_router.llm.model import build_model
from ai_router.llm.ollama_admin import OllamaAdmin, OllamaAdminClient
from ai_router.mail.sender import EmailSender, SmtpEmailSender
from ai_router.routing.agent import build_agent
from ai_router.routing.deps import RoutingDeps
from ai_router.routing.service import RoutingService
from ai_router.web.idempotency import IdempotencyStore

logger = logging.getLogger(__name__)


@dataclass
class AppContainer:
    settings: Settings
    http_client: httpx.AsyncClient
    ollama_admin: OllamaAdmin
    mailer: EmailSender
    agent: Agent[RoutingDeps, str]
    routing_service: RoutingService
    readiness: ReadinessChecker
    idempotency: IdempotencyStore
    warmup: WarmupState
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def start(self) -> None:
        """Startup work that must not block the port from opening."""
        if self.settings.warmup_on_startup:
            task = asyncio.create_task(self._warm_up(), name="model-warmup")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            self.warmup.status = "disabled"

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.http_client.aclose()

    async def _warm_up(self) -> None:
        model = self.settings.ollama_model
        started = time.perf_counter()
        try:
            if not await self.ollama_admin.has_model(model):
                logger.warning("llm.model_missing", extra={"model": model})
                self.warmup.status = "failed"
                return
            logger.info("llm.warmup.started", extra={"model": model})
            await self.ollama_admin.warm_up(model, self.settings.ollama_keep_alive)
        except Exception as exc:
            self.warmup.status = "failed"
            logger.warning("llm.warmup.failed", extra={"model": model, "error": repr(exc)})
            return
        self.warmup.status = "warm"
        self.warmup.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "llm.warmup.completed", extra={"model": model, "duration_ms": self.warmup.duration_ms}
        )


def build_container(
    settings: Settings,
    *,
    model: Model | None = None,
    mailer: EmailSender | None = None,
    ollama_admin: OllamaAdmin | None = None,
) -> AppContainer:
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
    admin = ollama_admin or OllamaAdminClient(
        http_client, settings.ollama_base_url, warmup_timeout=settings.llm_request_timeout_seconds
    )
    sender = mailer or SmtpEmailSender.from_settings(settings)
    agent = build_agent(settings, model or build_model(settings))
    warmup = WarmupState()
    return AppContainer(
        settings=settings,
        http_client=http_client,
        ollama_admin=admin,
        mailer=sender,
        agent=agent,
        routing_service=RoutingService(
            agent=agent, mailer=sender, settings=settings, model_name=settings.ollama_model
        ),
        readiness=ReadinessChecker(settings=settings, ollama_admin=admin, warmup=warmup),
        idempotency=IdempotencyStore(
            ttl_seconds=settings.idempotency_ttl_seconds,
            max_entries=settings.idempotency_max_entries,
        ),
        warmup=warmup,
    )
