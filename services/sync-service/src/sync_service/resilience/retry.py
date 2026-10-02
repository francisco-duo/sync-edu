"""Retry com backoff exponencial e jitter.

`sleep`, `uniform` e `monotonic` são injetáveis: os testes não esperam de verdade.
Quem decide *o que* merece retry é `is_retryable` (veja `resilience/errors.py`).
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from sync_service.resilience.errors import is_transient, retry_after_of


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5  # tentativas totais, contando a primeira
    base_delay: float = 0.5
    factor: float = 2.0
    max_delay: float = 30.0
    jitter: Literal["full", "none"] = "full"
    max_elapsed: float = 120.0  # orçamento total de tempo para uma operação
    max_retry_after: float = 60.0  # teto para o que o servidor pede em Retry-After

    def delay_cap(self, retry_number: int) -> float:
        """Teto do atraso antes do `retry_number`-ésimo retry (1 = primeiro retry)."""
        return min(self.max_delay, self.base_delay * self.factor ** (retry_number - 1))


class RetriesExhaustedError(Exception):
    """Todas as tentativas falharam com erros transitórios."""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"{attempts} tentativas esgotadas; último erro: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    is_retryable: Callable[[BaseException], bool] = is_transient,
    retry_after: Callable[[BaseException], float | None] = retry_after_of,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    uniform: Callable[[float, float], float] = random.uniform,
    monotonic: Callable[[], float] = time.monotonic,
    on_retry: Callable[[BaseException], None] | None = None,
) -> T:
    """Executa `operation`; repete só erros retentáveis. Erros permanentes sobem na hora."""
    started = monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempt >= policy.max_attempts:
                raise RetriesExhaustedError(attempt, exc) from exc
            delay = _next_delay(policy, attempt, exc, retry_after, uniform)
            if monotonic() - started + delay > policy.max_elapsed:
                raise RetriesExhaustedError(attempt, exc) from exc
            if on_retry is not None:
                on_retry(exc)
            await sleep(delay)


def _next_delay(
    policy: RetryPolicy,
    attempt: int,
    exc: BaseException,
    retry_after: Callable[[BaseException], float | None],
    uniform: Callable[[float, float], float],
) -> float:
    cap = policy.delay_cap(attempt)
    delay = uniform(0.0, cap) if policy.jitter == "full" else cap
    requested = retry_after(exc)
    if requested is not None:
        delay = max(delay, min(requested, policy.max_retry_after))
    return delay
