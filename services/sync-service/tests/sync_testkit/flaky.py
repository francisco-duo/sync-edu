from typing import Any

from sync_service.providers.base import ApplyResult, ProvedorDeContas
from sync_service.resilience.errors import PermanentUpstreamError


class FlakyProvider:
    """Envolve um provedor real e faz `create_user` falhar (erro permanente) para certos alunos."""

    def __init__(self, inner: ProvedorDeContas, fail_users: set[str] | None = None) -> None:
        self.inner = inner
        self.fail_users = set(fail_users or ())

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def create_user(self, *, external_id: str, **kwargs: str) -> ApplyResult:
        if external_id in self.fail_users:
            raise PermanentUpstreamError(
                f"dados inválidos para {external_id}", code="VALIDATION_ERROR", status_code=400
            )
        return await self.inner.create_user(external_id=external_id, **kwargs)
