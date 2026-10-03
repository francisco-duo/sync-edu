"""Adapter do mock-provedor: traduz o contrato HTTP dele para `ProvedorDeContas`.

Aqui moram os detalhes HTTP (paths, status, códigos de erro). O motor não os conhece.
Cada operação é idempotente: 409/404 "esperados" viram `Outcome.ALREADY_APPLIED`.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel

from sync_service.clients.http import send
from sync_service.clients.paged import PagedReader
from sync_service.providers.base import (
    ApplyResult,
    Outcome,
    ProviderClassRecord,
    ProviderSnapshot,
    ProviderUserRecord,
)
from sync_service.resilience.errors import PermanentUpstreamError
from sync_service.resilience.retry import RetryPolicy


class _UserDTO(BaseModel):
    id: str
    external_id: str | None = None
    email: str
    status: str


class _ClassDTO(BaseModel):
    id: str
    external_id: str | None = None
    name: str


class _MemberDTO(BaseModel):
    user_id: str


class MockProvedorAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retry_policy: RetryPolicy,
        concurrency: int = 5,
        page_size: int = 500,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        on_retry: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._client = client
        self._concurrency = concurrency
        self._reader = PagedReader(
            client,
            retry_policy=retry_policy,
            concurrency=concurrency,
            page_size=page_size,
            sleep=sleep,
            uniform=uniform,
            on_retry=on_retry,
        )

    # --- leitura --------------------------------------------------------------------------

    async def snapshot(self) -> ProviderSnapshot:
        users, classes = await asyncio.gather(
            self._reader.read_all("/users", _UserDTO), self._reader.read_all("/classes", _ClassDTO)
        )
        semaphore = asyncio.Semaphore(self._concurrency)

        async def members_of(class_id: str) -> list[tuple[str, str]]:
            async with semaphore:
                members = await self._reader.read_all(f"/classes/{class_id}/members", _MemberDTO)
            return [(class_id, member.user_id) for member in members]

        per_class = await asyncio.gather(*(members_of(c.id) for c in classes))
        return ProviderSnapshot(
            users=[ProviderUserRecord(u.id, u.external_id, u.email, u.status) for u in users],
            classes=[ProviderClassRecord(c.id, c.external_id, c.name) for c in classes],
            memberships=[pair for pairs in per_class for pair in pairs],
        )

    # --- escrita (todas idempotentes) -----------------------------------------------------

    async def create_class(self, *, external_id: str, name: str) -> ApplyResult:
        response = await self._write(
            "POST", "/classes", {201, 409}, json={"external_id": external_id, "name": name}
        )
        if response.status_code == 201:
            return ApplyResult(Outcome.APPLIED, response.json()["id"])
        # 409: a turma já existe para este external_id -> adota o id existente.
        return ApplyResult(Outcome.ALREADY_APPLIED, _existing_id(response))

    async def create_user(
        self, *, external_id: str, email: str, first_name: str, last_name: str
    ) -> ApplyResult:
        response = await self._write(
            "POST",
            "/users",
            {201, 409},
            json={
                "external_id": external_id,
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
            },
        )
        if response.status_code == 201:
            return ApplyResult(Outcome.APPLIED, response.json()["id"])
        details = _error_details(response)
        if details.get("conflict_field") == "external_id":
            # Provavelmente uma tentativa anterior foi aplicada e a resposta se perdeu.
            return ApplyResult(Outcome.ALREADY_APPLIED, _existing_id(response))
        # O e-mail pertence a outra pessoa: o e-mail alocado ficou obsoleto. Não adianta repetir.
        raise PermanentUpstreamError(
            f"POST /users -> 409: e-mail {email} já pertence a outro usuário",
            code="EMAIL_CONFLICT",
            status_code=409,
        )

    async def suspend_user(self, *, provider_user_id: str) -> ApplyResult:
        await self._write("POST", f"/users/{provider_user_id}/suspend", {200})
        return ApplyResult(Outcome.APPLIED)

    async def reactivate_user(self, *, provider_user_id: str) -> ApplyResult:
        await self._write("POST", f"/users/{provider_user_id}/reactivate", {200})
        return ApplyResult(Outcome.APPLIED)

    async def add_member(self, *, provider_class_id: str, provider_user_id: str) -> ApplyResult:
        response = await self._write(
            "POST",
            f"/classes/{provider_class_id}/members",
            {201, 409},
            json={"user_id": provider_user_id},
        )
        if response.status_code == 201:
            return ApplyResult(Outcome.APPLIED)
        return ApplyResult(Outcome.ALREADY_APPLIED)  # 409 MEMBERSHIP_ALREADY_EXISTS

    async def remove_member(self, *, provider_class_id: str, provider_user_id: str) -> ApplyResult:
        path = f"/classes/{provider_class_id}/members/{provider_user_id}"
        try:
            await self._write("DELETE", path, {204})
        except PermanentUpstreamError as exc:
            if exc.code == "MEMBERSHIP_NOT_FOUND":  # já não era membro: objetivo atingido
                return ApplyResult(Outcome.ALREADY_APPLIED)
            raise
        return ApplyResult(Outcome.APPLIED)

    # --- internos -------------------------------------------------------------------------

    async def _write(
        self, method: str, path: str, expected: set[int], json: dict[str, str] | None = None
    ) -> httpx.Response:
        # Sem retry aqui: o executor repete a ação inteira e registra as tentativas.
        return await send(self._client, method, path, expected=frozenset(expected), json=json)


def _error_details(response: httpx.Response) -> dict[str, str]:
    try:
        return dict(response.json()["error"].get("details") or {})
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def _existing_id(response: httpx.Response) -> str:
    existing = _error_details(response).get("existing_id")
    if not existing:
        raise PermanentUpstreamError(
            "409 sem existing_id: não dá para adotar o recurso existente",
            code="INVALID_RESPONSE",
            status_code=409,
        )
    return existing
