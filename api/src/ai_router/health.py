"""Readiness checks of external dependencies (Ollama, model, SMTP) and model warm-up state."""

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal

from ai_router.config import Settings
from ai_router.llm.ollama_admin import OllamaAdmin
from ai_router.web.schemas import ComponentStatus, ReadinessResponse

WarmupStatus = Literal["disabled", "pending", "warm", "failed"]


@dataclass(slots=True)
class WarmupState:
    status: WarmupStatus = "pending"
    duration_ms: int | None = None


async def check_smtp_banner(host: str, port: int) -> None:
    """Connect, expect the `220` greeting, say QUIT. Raises on any problem.

    The caller is responsible for the timeout (`asyncio.timeout`).
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        banner = await reader.readline()
        if not banner.startswith(b"220"):
            raise ConnectionError(f"unexpected SMTP greeting: {banner[:60]!r}")
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
        ollama_admin: OllamaAdmin,
        warmup: WarmupState,
        check_timeout: float = 2.0,
    ) -> None:
        self._settings = settings
        self._ollama = ollama_admin
        self._warmup = warmup
        self._timeout = check_timeout

    async def check(self) -> ReadinessResponse:
        ollama, model, smtp = await asyncio.gather(
            self._timed(self._ollama.version()),
            self._check_model(),
            self._timed(check_smtp_banner(self._settings.smtp_host, self._settings.smtp_port)),
        )
        model.name = self._settings.ollama_model
        components = {
            "ollama": ollama,
            "model": model,
            "model_warm": ComponentStatus(
                status="up" if self._warmup.status == "warm" else "down", detail=self._warmup.status
            ),
            "smtp": smtp,
        }
        ready = all(c.status == "up" for c in (ollama, model, smtp))
        return ReadinessResponse(status="ready" if ready else "not_ready", components=components)

    async def _check_model(self) -> ComponentStatus:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                present = await self._ollama.has_model(self._settings.ollama_model)
        except Exception as exc:
            return ComponentStatus(status="down", detail=_short(exc))
        latency = int((time.perf_counter() - started) * 1000)
        if not present:
            return ComponentStatus(status="down", latency_ms=latency, detail="model not found")
        return ComponentStatus(status="up", latency_ms=latency)

    async def _timed(self, check: Awaitable[object]) -> ComponentStatus:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                await check
        except Exception as exc:
            return ComponentStatus(status="down", detail=_short(exc))
        return ComponentStatus(status="up", latency_ms=int((time.perf_counter() - started) * 1000))


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:200]
