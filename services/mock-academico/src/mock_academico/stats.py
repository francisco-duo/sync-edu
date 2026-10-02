"""Contagem de requisições recebidas (para o benchmark e para testes)."""

from collections import Counter
from dataclasses import dataclass, field

from starlette.types import ASGIApp, Message, Receive, Scope, Send


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


class StatsMiddleware:
    """Middleware ASGI puro que conta as requisições (exceto /health e /_admin/*)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path == "/health" or path.startswith("/_admin"):
            await self.app(scope, receive, send)
            return

        status = 0

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, capture)
        route = getattr(scope.get("route"), "path", path)
        scope["app"].state.stats.record(f"{scope['method']} {route}", status)
