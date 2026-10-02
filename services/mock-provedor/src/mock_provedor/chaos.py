"""Falhas injetáveis: erros 5xx, 429 e "operação aplicada, mas a resposta se perdeu".

Há modo probabilístico (`*_rate`, com semente) e modo roteirizado (`fail_next`, `lose_next`),
este último para testes determinísticos.
"""

import random
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from mock_provedor.errors import error_body

FAILURE_STATUSES = (500, 502, 503, 504)


@dataclass
class ChaosState:
    error_rate: float = 0.0
    lose_response_rate: float = 0.0
    retry_after_seconds: int = 1
    seed: int | None = None
    fail_next: list[int] = field(default_factory=list)  # status a devolver nas próximas requisições
    lose_next: int = 0  # quantas próximas respostas bem-sucedidas serão "perdidas"
    _rng: random.Random = field(default_factory=random.Random, repr=False)

    def configure(
        self,
        *,
        error_rate: float,
        lose_response_rate: float,
        retry_after_seconds: int,
        seed: int | None,
        fail_next: list[int],
        lose_next: int,
    ) -> None:
        self.error_rate = error_rate
        self.lose_response_rate = lose_response_rate
        self.retry_after_seconds = retry_after_seconds
        self.seed = seed
        self.fail_next = list(fail_next)
        self.lose_next = lose_next
        self._rng = random.Random(seed)

    def failure_before_processing(self) -> int | None:
        if self.fail_next:
            return self.fail_next.pop(0)
        if self._rng.random() < self.error_rate:
            return self._rng.choice(FAILURE_STATUSES)
        return None

    def lose_this_response(self) -> bool:
        if self.lose_next > 0:
            self.lose_next -= 1
            return True
        return self._rng.random() < self.lose_response_rate


@dataclass
class Stats:
    total: int = 0
    by_route: Counter[str] = field(default_factory=Counter)
    by_status: Counter[str] = field(default_factory=Counter)

    def record(self, route: str, status: int) -> None:
        self.total += 1
        self.by_route[route] += 1
        self.by_status[str(status)] += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "by_route": dict(self.by_route),
            "by_status": dict(self.by_status),
        }


def _injected(status: int, retry_after: int) -> JSONResponse:
    if status == 429:
        return JSONResponse(
            status_code=429,
            content=error_body("RATE_LIMITED", "limite de requisições excedido"),
            headers={"Retry-After": str(retry_after)},
        )
    return JSONResponse(
        status_code=status, content=error_body("INJECTED_FAILURE", "falha injetada pelo mock")
    )


def _route_key(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    return f"{request.method} {path}"


async def chaos_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.url.path
    if path == "/health" or path.startswith("/_admin"):
        return await call_next(request)

    chaos: ChaosState = request.app.state.chaos
    stats: Stats = request.app.state.stats

    status = chaos.failure_before_processing()
    if status is not None:
        response: Response = _injected(status, chaos.retry_after_seconds)
        stats.record(_route_key(request), status)
        return response

    response = await call_next(request)
    if chaos.lose_this_response():
        # A operação JÁ foi aplicada; só a resposta some. É o caso difícil da idempotência.
        response = _injected(503, chaos.retry_after_seconds)
    stats.record(_route_key(request), response.status_code)
    return response
