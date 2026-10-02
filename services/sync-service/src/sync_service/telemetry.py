"""Contadores de requisições HTTP e retries por sistema externo (para auditoria e benchmark)."""

from collections import Counter
from collections.abc import Awaitable, Callable

import httpx


class Telemetry:
    def __init__(self) -> None:
        self._http_calls: Counter[str] = Counter()
        self._retries: Counter[str] = Counter()

    def count_call(self, target: str) -> None:
        self._http_calls[target] += 1

    def count_retry(self, target: str) -> None:
        self._retries[target] += 1

    def request_hook(self, target: str) -> Callable[[httpx.Request], Awaitable[None]]:
        """Gancho `event_hooks["request"]`: conta cada requisição enviada, inclusive retries."""

        async def hook(_: httpx.Request) -> None:
            self.count_call(target)

        return hook

    def retry_hook(self, target: str) -> Callable[[BaseException], None]:
        def hook(_: BaseException) -> None:
            self.count_retry(target)

        return hook

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {"http_calls": dict(self._http_calls), "retries": dict(self._retries)}

    @staticmethod
    def delta(
        before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]
    ) -> dict[str, dict[str, int]]:
        """O que aconteceu entre dois snapshots. Aproximado se dois runs rodarem juntos."""
        return {
            kind: {
                target: after[kind][target] - before[kind].get(target, 0)
                for target in after[kind]
                if after[kind][target] - before[kind].get(target, 0)
            }
            for kind in after
        }
