"""Contrato entre o motor de sincronização e qualquer provedor de contas.

O motor só conhece esta interface. Para falar com outro provedor (por exemplo Google
Workspace) basta escrever outro adapter que a implemente.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Outcome(StrEnum):
    APPLIED = "applied"
    # O estado desejado já valia (recurso já existia, membro já removido...). Conta como sucesso.
    ALREADY_APPLIED = "already_applied"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    outcome: Outcome
    provider_id: str | None = None  # preenchido em criações


@dataclass(frozen=True, slots=True)
class ProviderUserRecord:
    provider_id: str
    external_id: str | None
    email: str
    status: str


@dataclass(frozen=True, slots=True)
class ProviderClassRecord:
    provider_id: str
    external_id: str | None
    name: str


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    users: list[ProviderUserRecord] = field(default_factory=list)
    classes: list[ProviderClassRecord] = field(default_factory=list)
    # (provider_class_id, provider_user_id)
    memberships: list[tuple[str, str]] = field(default_factory=list)


class ProvedorDeContas(Protocol):
    """Todas as operações são idempotentes: repetir uma chamada já aplicada não é erro.

    Falhas chegam como `TransientUpstreamError` (vale tentar de novo) ou
    `PermanentUpstreamError` (não vale), definidas em `sync_service.resilience.errors`.
    """

    async def snapshot(self) -> ProviderSnapshot: ...

    async def create_class(self, *, external_id: str, name: str) -> ApplyResult: ...

    async def create_user(
        self, *, external_id: str, email: str, first_name: str, last_name: str
    ) -> ApplyResult: ...

    async def suspend_user(self, *, provider_user_id: str) -> ApplyResult: ...

    async def reactivate_user(self, *, provider_user_id: str) -> ApplyResult: ...

    async def add_member(self, *, provider_class_id: str, provider_user_id: str) -> ApplyResult: ...

    async def remove_member(
        self, *, provider_class_id: str, provider_user_id: str
    ) -> ApplyResult: ...
