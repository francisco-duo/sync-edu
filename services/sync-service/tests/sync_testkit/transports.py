"""Transporte HTTP que injeta falhas controladas entre o sync-service e um app em processo."""

import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

Match = Callable[[httpx.Request], bool]


def post_users(external_id: str | None = None) -> Match:
    def match(request: httpx.Request) -> bool:
        if request.method != "POST" or request.url.path != "/users":
            return False
        return external_id is None or json.loads(request.content)["external_id"] == external_id

    return match


def any_write(request: httpx.Request) -> bool:
    return request.method != "GET"


def any_request(_: httpx.Request) -> bool:
    return True


@dataclass
class Rule:
    match: Match
    respond: Callable[[httpx.Request], httpx.Response] | None = None
    raises: Exception | None = None
    lose_response: bool = False
    times: int | None = None  # None = enquanto a regra existir
    enabled: bool = True
    hits: int = 0

    def applies(self, request: httpx.Request) -> bool:
        exhausted = self.times is not None and self.hits >= self.times
        return self.enabled and not exhausted and self.match(request)


class FaultyTransport(httpx.AsyncBaseTransport):
    """Repassa tudo para `inner`, exceto o que as regras interceptam (na ordem dada)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.rules: list[Rule] = []

    def add(self, rule: Rule) -> Rule:
        self.rules.append(rule)
        return rule

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for rule in self.rules:
            if not rule.applies(request):
                continue
            rule.hits += 1
            if rule.lose_response:
                # O servidor APLICA a operação, mas o cliente nunca vê a resposta.
                response = await self.inner.handle_async_request(request)
                await response.aclose()
                raise httpx.ReadTimeout("resposta perdida", request=request)
            if rule.raises is not None:
                raise rule.raises
            if rule.respond is not None:
                return rule.respond(request)
        return await self.inner.handle_async_request(request)


def error_response(status: int, code: str) -> Callable[[httpx.Request], httpx.Response]:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": code, "message": code.lower(), "details": {}}}
        )

    return respond
