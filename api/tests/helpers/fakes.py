"""Fake external dependencies for tests."""

import httpx


class FakeOllamaAdmin:
    def __init__(
        self,
        *,
        reachable: bool = True,
        model_present: bool = True,
        capabilities: set[str] | None = None,
    ) -> None:
        self.reachable = reachable
        self.model_present = model_present
        self._capabilities = {"completion", "tools"} if capabilities is None else capabilities
        self.warmups: list[str] = []
        self.loaded: set[str] = set()

    def _check(self) -> None:
        if not self.reachable:
            raise httpx.ConnectError("connection refused to secret-host:11434")

    async def version(self) -> str:
        self._check()
        return "0.40.3"

    async def has_model(self, name: str) -> bool:
        self._check()
        return self.model_present

    async def capabilities(self, name: str) -> set[str] | None:
        self._check()
        return self._capabilities if self.model_present else None

    async def warm_up(self, name: str, keep_alive: str) -> None:
        self._check()
        self.warmups.append(name)
        self.loaded.add(name)

    async def loaded_models(self) -> set[str]:
        self._check()
        return set(self.loaded)
