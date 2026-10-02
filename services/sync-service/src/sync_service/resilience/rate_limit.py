"""Limite de taxa do lado do cliente: no máximo N requisições por segundo, espaçadas."""

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx


class AsyncRateLimiter:
    """Reserva "vagas" espaçadas de `1/rate` segundo. Seguro para muitas corrotinas.

    Diferente do semáforo (que limita quantas chamadas rodam AO MESMO TEMPO), isto limita
    quantas começam por segundo. Os dois juntos: poucas simultâneas e ritmo controlado.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second deve ser positivo")
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            start = max(now, self._next_slot)
            self._next_slot = start + self._interval
            delay = start - now
        if delay > 0:
            await self._sleep(delay)  # fora do lock: os demais já reservaram as suas vagas

    async def request_hook(self, _: httpx.Request) -> None:
        """Gancho `event_hooks["request"]` do httpx: toda requisição, retries inclusive."""
        await self.acquire()
