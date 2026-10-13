"""Thin client for Ollama's native API, used for readiness checks and model warm-up."""

from typing import Protocol

import httpx


class OllamaAdmin(Protocol):
    async def version(self) -> str: ...
    async def has_model(self, name: str) -> bool: ...
    async def warm_up(self, name: str, keep_alive: str) -> None: ...


class OllamaAdminClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        base_url: str,
        *,
        check_timeout: float = 2.0,
        warmup_timeout: float = 120.0,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._check_timeout = check_timeout
        self._warmup_timeout = warmup_timeout

    async def version(self) -> str:
        response = await self._http.get(
            f"{self._base_url}/api/version", timeout=self._check_timeout
        )
        response.raise_for_status()
        return str(response.json().get("version", "unknown"))

    async def has_model(self, name: str) -> bool:
        response = await self._http.post(
            f"{self._base_url}/api/show", json={"model": name}, timeout=self._check_timeout
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        response.raise_for_status()
        return True

    async def warm_up(self, name: str, keep_alive: str) -> None:
        """Load the model into memory: an empty generate request is Ollama's documented preload."""
        response = await self._http.post(
            f"{self._base_url}/api/generate",
            json={"model": name, "keep_alive": keep_alive, "stream": False},
            timeout=self._warmup_timeout,
        )
        response.raise_for_status()
