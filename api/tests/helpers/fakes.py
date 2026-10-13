"""Fake external dependencies for tests."""

import httpx


class FakeOllamaAdmin:
    def __init__(self, *, reachable: bool = True, model_present: bool = True) -> None:
        self.reachable = reachable
        self.model_present = model_present
        self.warmups: list[str] = []

    def _check(self) -> None:
        if not self.reachable:
            raise httpx.ConnectError("connection refused")

    async def version(self) -> str:
        self._check()
        return "0.40.3"

    async def has_model(self, name: str) -> bool:
        self._check()
        return self.model_present

    async def warm_up(self, name: str, keep_alive: str) -> None:
        self._check()
        self.warmups.append(name)
