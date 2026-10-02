"""Falhas injetáveis: erros 5xx, 429 e "operação aplicada, mas a resposta se perdeu".

Há modo probabilístico (`*_rate`, com semente) e modo roteirizado (`fail_next`, `lose_next`),
este último para testes determinísticos.
"""

import asyncio
import random
import time
from collections import Counter, deque
from dataclasses import dataclass, field

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mock_provedor.errors import error_body

FAILURE_STATUSES = (500, 502, 503, 504)


@dataclass
class ChaosState:
    error_rate: float = 0.0
    lose_response_rate: float = 0.0
    retry_after_seconds: int = 1
    rate_limit_rps: int = 0  # 0 = sem limite; acima disso, 429 (janela deslizante de 1 s)
    latency_ms: int = 0  # atraso artificial em toda requisição de negócio
    seed: int | None = None
    fail_next: list[int] = field(default_factory=list)  # status a devolver nas próximas requisições
    lose_next: int = 0  # quantas próximas respostas bem-sucedidas serão "perdidas"
    _rng: random.Random = field(default_factory=random.Random, repr=False)
    _window: deque[float] = field(default_factory=deque, repr=False)

    def configure(
        self,
        *,
        error_rate: float,
        lose_response_rate: float,
        retry_after_seconds: int,
        seed: int | None,
        fail_next: list[int],
        lose_next: int,
        rate_limit_rps: int = 0,
        latency_ms: int = 0,
    ) -> None:
        self.error_rate = error_rate
        self.lose_response_rate = lose_response_rate
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_rps = rate_limit_rps
        self.latency_ms = latency_ms
        self._window.clear()
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

    def over_rate_limit(self) -> bool:
        """Janela deslizante de 1 s. Requisições recusadas não consomem a cota."""
        if self.rate_limit_rps <= 0:
            return False
        now = time.monotonic()
        while self._window and now - self._window[0] >= 1.0:
            self._window.popleft()
        if len(self._window) >= self.rate_limit_rps:
            return True
        self._window.append(now)
        return False

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


def _route_key(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", scope["path"])
    return f"{scope['method']} {path}"


class ChaosMiddleware:
    """Middleware ASGI puro (`BaseHTTPMiddleware` cria tarefas por requisição e é bem mais lento).

    Aplica falhas injetadas e conta as requisições. `/health` e `/_admin/*` ficam de fora.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path == "/health" or path.startswith("/_admin"):
            await self.app(scope, receive, send)
            return

        state = scope["app"].state
        chaos: ChaosState = state.chaos
        stats: Stats = state.stats

        if chaos.latency_ms:
            await asyncio.sleep(chaos.latency_ms / 1000)
        status = chaos.failure_before_processing()
        if status is None and chaos.over_rate_limit():
            status = 429
        if status is not None:
            stats.record(_route_key(scope), status)
            await _injected(status, chaos.retry_after_seconds)(scope, receive, send)
            return

        lose_response = chaos.lose_this_response()
        sent_status = 0

        async def capture(message: Message) -> None:
            nonlocal sent_status
            if message["type"] == "http.response.start":
                sent_status = message["status"]
            if not lose_response:
                await send(message)

        await self.app(scope, receive, capture)
        if lose_response:
            # A operação JÁ foi aplicada; só a resposta some. É o caso difícil da idempotência.
            stats.record(_route_key(scope), 503)
            await _injected(503, chaos.retry_after_seconds)(scope, receive, send)
            return
        stats.record(_route_key(scope), sent_status)
