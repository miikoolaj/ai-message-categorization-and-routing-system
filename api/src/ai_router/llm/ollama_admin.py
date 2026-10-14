"""Thin client for Ollama's native API, used for readiness checks and model warm-up."""

from typing import Protocol

import httpx


def keep_alive_value(value: str) -> str | int | float:
    """Convert OLLAMA_KEEP_ALIVE to the form the JSON API accepts.

    The server's environment variable accepts a bare number of seconds ("-1" = keep forever), but
    the `keep_alive` field of the API must then be a JSON number - the string "-1" is answered with
    400 Bad Request. Durations with a unit ("30m", "24h") are sent unchanged.
    """
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def keeps_model_loaded(keep_alive: str) -> bool:
    """A negative keep-alive ("-1", "-1m") means "never unload" in Ollama."""
    return keep_alive.strip().startswith("-")


def is_loaded(name: str, loaded: set[str]) -> bool:
    """Ollama reports untagged models with an explicit ":latest" tag."""
    return name in loaded or (":" not in name and f"{name}:latest" in loaded)


class OllamaAdmin(Protocol):
    async def version(self) -> str: ...
    async def has_model(self, name: str) -> bool: ...
    async def capabilities(self, name: str) -> set[str] | None: ...
    async def warm_up(self, name: str, keep_alive: str) -> None: ...
    async def loaded_models(self) -> set[str]: ...


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

    async def capabilities(self, name: str) -> set[str] | None:
        """`None` if the model is missing, otherwise e.g. {"completion", "tools", "thinking"}."""
        response = await self._http.post(
            f"{self._base_url}/api/show", json={"model": name}, timeout=self._check_timeout
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        return set(response.json().get("capabilities") or [])

    async def warm_up(self, name: str, keep_alive: str) -> None:
        """Load the model into memory: an empty generate request is Ollama's documented preload."""
        response = await self._http.post(
            f"{self._base_url}/api/generate",
            json={"model": name, "keep_alive": keep_alive_value(keep_alive), "stream": False},
            timeout=self._warmup_timeout,
        )
        response.raise_for_status()

    async def loaded_models(self) -> set[str]:
        """Names of the models currently loaded in memory (`GET /api/ps`)."""
        response = await self._http.get(f"{self._base_url}/api/ps", timeout=self._check_timeout)
        response.raise_for_status()
        models = response.json().get("models") or []
        return {str(m.get("name") or m.get("model")) for m in models}
