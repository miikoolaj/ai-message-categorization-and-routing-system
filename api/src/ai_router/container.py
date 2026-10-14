"""Composition root: builds and owns all long-lived application components.

`build_container()` wires the production implementations. Tests pass their own model, SMTP sender,
gateway, Ollama admin client or key resolver (dependency injection instead of monkeypatching).
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic_ai import UsageLimits
from pydantic_ai.models import Model

from ai_router import __version__
from ai_router.config import Settings
from ai_router.health import ReadinessChecker, WarmupState
from ai_router.llm.model import ModelHandle, build_model
from ai_router.llm.ollama_admin import (
    OllamaAdmin,
    OllamaAdminClient,
    is_loaded,
    keeps_model_loaded,
)
from ai_router.logging_config import log_event
from ai_router.mail.composer import MailSettings
from ai_router.mail.gateway import InMemoryMailGateway, MailGateway
from ai_router.mail.outbox import OutboxDispatcher, OutboxGateway
from ai_router.mail.sender import EmailSender, SmtpEmailSender
from ai_router.observability import metrics
from ai_router.routing.agent import RoutingAgent, build_agent
from ai_router.routing.deps import AgentRuntime, RoutingDeps
from ai_router.routing.policy import RoutingPolicy, load_policy
from ai_router.routing.prompts import build_user_prompt
from ai_router.routing.service import RoutingService
from ai_router.routing.tickets import FeedbackSigner, TicketService
from ai_router.routing.worker import AsyncRoutingWorker
from ai_router.storage.db import Database, StorageError
from ai_router.storage.feedback import FeedbackRepository
from ai_router.storage.idempotency import IdempotencyRepository
from ai_router.storage.outbox import OutboxRepository
from ai_router.storage.tickets import TicketRepository
from ai_router.web.security import Authenticator, KeyResolver, RateLimiter

logger = logging.getLogger(__name__)

_DAY = 86_400.0


async def _noop_close() -> None:
    return None


@dataclass
class AppContainer:
    settings: Settings
    policy: RoutingPolicy
    database: Database
    http_client: httpx.AsyncClient
    ollama_admin: OllamaAdmin | None
    model: ModelHandle
    routing_agent: RoutingAgent
    runtime: AgentRuntime
    outbox: OutboxRepository
    dispatcher: OutboxDispatcher
    gateway: MailGateway
    routing_service: RoutingService
    ticket_repo: TicketRepository
    tickets: TicketService
    worker: AsyncRoutingWorker
    idempotency: IdempotencyRepository
    feedback: FeedbackRepository
    authenticator: Authenticator
    rate_limiter: RateLimiter
    readiness: ReadinessChecker
    warmup: WarmupState
    signer: FeedbackSigner | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def start(self) -> None:
        """Startup work; nothing here blocks the port for long (warm-up runs in the background)."""
        await self.database.connect()
        secret = self.settings.feedback_secret
        self.signer = FeedbackSigner(
            secret.get_secret_value()
            if secret
            else await self.database.get_or_create_secret("feedback")
        )
        self.tickets.set_signer(self.signer)
        requeued, interrupted = await self.ticket_repo.recover_interrupted()
        stuck = await self.outbox.reset_stuck()
        metrics.BUILD_INFO.info(
            {
                "version": __version__,
                "model": self.model.name,
                "agent_mode": self.routing_agent.mode,
                "prompt_version": self.routing_agent.prompt_version,
                "policy_version": self.policy.version,
            }
        )
        log_event(
            logger,
            logging.INFO,
            "app.started",
            version=__version__,
            model=self.model.name,
            agent_mode=self.routing_agent.mode,
            prompt_version=self.routing_agent.prompt_version,
            policy=self.policy.source,
            policy_version=self.policy.version,
            auth_mode=self.settings.auth_mode,
            requeued_tickets=requeued,
            interrupted_tickets=interrupted,
            outbox_reset=stuck,
        )
        for warning in self.settings.warnings():
            log_event(logger, logging.WARNING, "config.warning", detail=warning)
        self._spawn(self.dispatcher.run(), "outbox-dispatcher")
        self._spawn(self.worker.run(), "async-worker")
        self._spawn(self._maintenance(), "maintenance")
        if self.settings.warmup_on_startup:
            self._spawn(self._warm_up(), "model-warmup")
            if (
                self.ollama_admin is not None
                and self.settings.model_watchdog_interval_seconds > 0
                and keeps_model_loaded(self.settings.ollama_keep_alive)
            ):
                self._spawn(self._model_watchdog(self.ollama_admin), "model-watchdog")
        else:
            self.warmup.status = "disabled"

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.model.close()
        await self.http_client.aclose()
        await self.database.close()

    async def _warm_up(self) -> None:
        """Load the model AND fill Ollama's KV cache with the static prompt prefix.

        A synthetic run through the real agent (with an in-memory gateway - nothing is sent)
        makes Ollama process the instructions and the tool schema once, so the first real
        request only pays for its own message (~9 s instead of ~30 s on CPU).
        """
        started = time.perf_counter()
        if self.ollama_admin is not None:
            name = self.settings.ollama_model
            try:
                if not await self.ollama_admin.has_model(name):
                    log_event(logger, logging.WARNING, "llm.model_missing", model=name)
                    self.warmup.status = "failed"
                    return
                log_event(logger, logging.INFO, "llm.warmup.started", model=name)
                await self.ollama_admin.warm_up(name, self.settings.ollama_keep_alive)
            except Exception as exc:
                self.warmup.status = "failed"
                log_event(logger, logging.WARNING, "llm.warmup.failed", model=name, error=repr(exc))
                return
        await self._prime_prompt_cache()
        self.warmup.status = "warm"
        self.warmup.duration_ms = int((time.perf_counter() - started) * 1000)
        log_event(logger, logging.INFO, "llm.warmup.completed", duration_ms=self.warmup.duration_ms)

    async def _prime_prompt_cache(self) -> None:
        deps = RoutingDeps(
            runtime=self.runtime,
            gateway=InMemoryMailGateway(),
            ticket_id="warmup",
            ticket_code="AIR-WARMUP",
            request_id="warmup",
            sender_email="warmup@localhost",
            sender_verification="none",
            original_message="warm-up",
        )
        try:
            async with asyncio.timeout(self.settings.llm_request_timeout_seconds):
                await self.routing_agent.agent.run(
                    build_user_prompt("Nie działa mi komputer"),
                    deps=deps,
                    usage_limits=UsageLimits(request_limit=3),
                )
        except Exception as exc:  # best effort - a failed warm-up only costs latency
            log_event(logger, logging.INFO, "llm.prompt_cache_warmup_skipped", error=repr(exc))

    async def _model_watchdog(self, admin: OllamaAdmin) -> None:
        """Warm the model up again when Ollama no longer has it loaded.

        With OLLAMA_KEEP_ALIVE=-1 the model should stay in memory forever, so a missing model means
        Ollama was restarted (crash, `docker compose restart ollama`, OOM kill). Without this the
        next request would pay the full cold start (model load + prompt prefill).
        """
        model = self.settings.ollama_model
        while True:
            await asyncio.sleep(self.settings.model_watchdog_interval_seconds)
            if self.warmup.status != "warm":
                continue
            try:
                loaded = await admin.loaded_models()
            except Exception:  # noqa: S112 - Ollama down: readiness reports it, retry later
                continue
            if not is_loaded(model, loaded):
                log_event(logger, logging.WARNING, "llm.model_unloaded", model=model)
                metrics.MODEL_REWARMS.inc()
                self.warmup.status = "pending"
                await self._warm_up()

    async def _maintenance(self) -> None:
        """Gauges every 15 s; data retention (tickets, outbox, feedback, idempotency) hourly."""
        last_purge = 0.0
        while True:
            try:
                for status, count in (await self.ticket_repo.counts_by_status()).items():
                    metrics.TICKETS.labels(status).set(count)
                metrics.OUTBOX_PENDING.set(await self.outbox.pending_count())
                now = time.time()
                if now - last_purge >= 3600:
                    last_purge = now
                    ticket_cut = now - self.settings.ticket_retention_days * _DAY
                    purged = {
                        "tickets": await self.ticket_repo.purge(ticket_cut),
                        "outbox": await self.outbox.purge(ticket_cut),
                        "idempotency": await self.idempotency.purge_expired(),
                        "feedback": await self.feedback.purge(
                            now - self.settings.feedback_retention_days * _DAY
                        ),
                    }
                    if any(purged.values()):
                        log_event(logger, logging.INFO, "retention.purged", **purged)
            except StorageError as exc:
                log_event(logger, logging.ERROR, "maintenance.storage_error", error=str(exc))
            await asyncio.sleep(15)


def build_container(
    settings: Settings,
    *,
    model: Model | None = None,
    smtp_sender: EmailSender | None = None,
    gateway: MailGateway | None = None,
    ollama_admin: OllamaAdmin | None = None,
    policy: RoutingPolicy | None = None,
    key_resolver: KeyResolver | None = None,
) -> AppContainer:
    policy = policy or load_policy(settings.routing_policy_file)
    database = Database(settings.database_path)
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
    admin: OllamaAdmin | None = ollama_admin
    if admin is None and settings.llm_backend == "ollama":
        admin = OllamaAdminClient(
            http_client,
            settings.ollama_base_url,
            warmup_timeout=settings.llm_request_timeout_seconds,
        )
    handle = (
        ModelHandle(model=model, name=settings.ollama_model, close=_noop_close)
        if model is not None
        else build_model(settings, policy)
    )
    routing_agent = build_agent(settings, handle.model, policy)
    runtime = AgentRuntime(
        policy=policy,
        mail_settings=MailSettings.from_settings(settings),
        model_name=handle.name,
        prompt_version=routing_agent.prompt_version,
        confidence_threshold=settings.confidence_threshold,
        low_confidence_action=settings.low_confidence_action,
    )
    outbox = OutboxRepository(database)
    dispatcher = OutboxDispatcher(
        outbox,
        smtp_sender or SmtpEmailSender.from_settings(settings),
        max_attempts=settings.outbox_max_attempts,
        retry_base_seconds=settings.outbox_retry_base_seconds,
        retry_max_seconds=settings.outbox_retry_max_seconds,
    )
    mail_gateway = gateway or OutboxGateway(
        outbox, dispatcher, sync_timeout=settings.mail_sync_delivery_timeout_seconds
    )
    routing_service = RoutingService(
        routing_agent=routing_agent, gateway=mail_gateway, runtime=runtime, settings=settings
    )
    ticket_repo = TicketRepository(database)
    tickets = TicketService(
        tickets=ticket_repo, routing=routing_service, settings=settings, signer=None
    )
    worker = AsyncRoutingWorker(
        ticket_repo, tickets, concurrency=settings.max_concurrent_agent_runs
    )
    tickets.set_worker_notifier(worker.notify)
    warmup = WarmupState()
    return AppContainer(
        settings=settings,
        policy=policy,
        database=database,
        http_client=http_client,
        ollama_admin=admin,
        model=handle,
        routing_agent=routing_agent,
        runtime=runtime,
        outbox=outbox,
        dispatcher=dispatcher,
        gateway=mail_gateway,
        routing_service=routing_service,
        ticket_repo=ticket_repo,
        tickets=tickets,
        worker=worker,
        idempotency=IdempotencyRepository(database, ttl_seconds=settings.idempotency_ttl_seconds),
        feedback=FeedbackRepository(database),
        authenticator=Authenticator(settings, key_resolver=key_resolver),
        rate_limiter=RateLimiter(
            rate_per_minute=settings.rate_limit_per_minute, burst=settings.rate_limit_burst
        ),
        readiness=ReadinessChecker(
            settings=settings, ollama_admin=admin, database=database, warmup=warmup
        ),
        warmup=warmup,
    )
