"""Readiness checks of dependencies (Ollama + model capabilities, SMTP, storage) and warm-up state.

The public report contains only coarse, safe descriptions ("connection failed", "timeout",
"model does not support tool calling"); exception text with host names stays in the logs.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal

import httpx

from ai_router.config import Settings
from ai_router.llm.ollama_admin import OllamaAdmin
from ai_router.logging_config import log_event
from ai_router.storage.db import Database, StorageError
from ai_router.web.schemas import ComponentStatus, ReadinessResponse

logger = logging.getLogger(__name__)

WarmupStatus = Literal["disabled", "pending", "warm", "failed"]


@dataclass(slots=True)
class WarmupState:
    status: WarmupStatus = "pending"
    duration_ms: int | None = None


def describe_failure(exc: BaseException) -> str:
    """Safe, coarse description of a failed check (no host names, ports or messages)."""
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, ConnectionError | OSError | httpx.TransportError):
        return "connection failed"
    if isinstance(exc, StorageError):
        return "storage error"
    return type(exc).__name__


async def check_smtp_banner(host: str, port: int) -> None:
    """Connect, expect the `220` greeting, say QUIT. Raises on any problem.

    The caller is responsible for the timeout (`asyncio.timeout`).
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        banner = await reader.readline()
        if not banner.startswith(b"220"):
            raise ConnectionError("unexpected SMTP greeting")
        writer.write(b"QUIT\r\n")
        await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class ReadinessChecker:
    def __init__(
        self,
        *,
        settings: Settings,
        ollama_admin: OllamaAdmin | None,
        database: Database,
        warmup: WarmupState,
        check_timeout: float = 2.0,
    ) -> None:
        self._settings = settings
        self._ollama = ollama_admin
        self._db = database
        self._warmup = warmup
        self._timeout = check_timeout

    async def check(self) -> ReadinessResponse:
        smtp, storage = await asyncio.gather(
            self._timed(
                "smtp", check_smtp_banner(self._settings.smtp_host, self._settings.smtp_port)
            ),
            self._timed("storage", self._db.ping()),
        )
        if self._ollama is None:  # LLM_BACKEND=fake
            skipped = ComponentStatus(status="up", detail="skipped (fake LLM backend)")
            ollama, model = skipped, skipped.model_copy(update={"name": "fake-rules"})
        else:
            ollama, model = await asyncio.gather(
                self._timed("ollama", self._ollama.version()), self._check_model()
            )
        warm = ComponentStatus(
            status="up" if self._warmup.status in ("warm", "disabled") else "down",
            detail=self._warmup.status,
        )
        components = {
            "ollama": ollama,
            "model": model,
            "model_warm": warm,
            "smtp": smtp,
            "storage": storage,
        }
        required = [ollama, model, smtp, storage]
        if self._settings.readiness_require_warm:
            required.append(warm)
        ready = all(c.status == "up" for c in required)
        return ReadinessResponse(status="ready" if ready else "not_ready", components=components)

    async def _check_model(self) -> ComponentStatus:
        assert self._ollama is not None  # noqa: S101
        name = self._settings.ollama_model
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                capabilities = await self._ollama.capabilities(name)
        except Exception as exc:
            log_event(
                logger, logging.DEBUG, "health.check_failed", component="model", error=repr(exc)
            )
            return ComponentStatus(status="down", name=name, detail=describe_failure(exc))
        latency = int((time.perf_counter() - started) * 1000)
        if capabilities is None:
            return ComponentStatus(
                status="down", name=name, latency_ms=latency, detail="model not found"
            )
        if "tools" not in capabilities:
            return ComponentStatus(
                status="down",
                name=name,
                latency_ms=latency,
                detail="model does not support tool calling",
            )
        return ComponentStatus(status="up", name=name, latency_ms=latency)

    async def _timed(self, component: str, check: Awaitable[object]) -> ComponentStatus:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                await check
        except Exception as exc:
            log_event(
                logger, logging.DEBUG, "health.check_failed", component=component, error=repr(exc)
            )
            return ComponentStatus(status="down", detail=describe_failure(exc))
        return ComponentStatus(status="up", latency_ms=int((time.perf_counter() - started) * 1000))
