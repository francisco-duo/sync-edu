"""Mundo de teste: PostgreSQL real + mock-academico + mock-provedor rodando em processo."""

import uuid
from collections.abc import Callable, Iterable, Mapping

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mock_academico.config import Settings as AcademicoSettings
from mock_academico.main import create_app as create_academico
from mock_provedor.config import Settings as ProvedorSettings
from mock_provedor.main import create_app as create_provedor
from sync_service.clients.academico import AcademicoClient
from sync_service.db.models import IdMapping, SyncAction, SyncRun
from sync_service.db.session import create_session_factory
from sync_service.providers.base import ProvedorDeContas
from sync_service.providers.mock_adapter import MockProvedorAdapter
from sync_service.resilience.retry import RetryPolicy
from sync_service.sync import store
from sync_service.sync.executor import ActionExecutor, ExecutionConfig
from sync_service.sync.runner import SyncRunner
from sync_testkit.transports import FaultyTransport

TEST_POLICY = RetryPolicy(max_attempts=3, base_delay=0.0, jitter="none")
TABLES = "sync_actions, sync_runs, id_mappings"


async def reset_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


class World:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        batch_size: int = 200,
        max_consecutive_failures: int = 25,
        wrap_provider: Callable[[ProvedorDeContas], ProvedorDeContas] | None = None,
    ) -> None:
        self.sessions = create_session_factory(engine)
        self.academico_app = create_academico(AcademicoSettings(seed_students=0, seed_classes=0))
        self.provedor_app = create_provedor(ProvedorSettings(chaos_retry_after_seconds=0))

        self.academico_net = FaultyTransport(httpx.ASGITransport(app=self.academico_app))
        self.provedor_net = FaultyTransport(httpx.ASGITransport(app=self.provedor_app))
        self._academico_http = httpx.AsyncClient(
            transport=self.academico_net, base_url="http://academico"
        )
        self._provedor_http = httpx.AsyncClient(
            transport=self.provedor_net, base_url="http://provedor"
        )
        # Clientes "de admin": falam direto com os mocks, sem passar pelas falhas injetadas.
        self._academico_admin = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.academico_app), base_url="http://academico"
        )
        self._provedor_admin = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.provedor_app), base_url="http://provedor"
        )

        self.adapter: ProvedorDeContas = MockProvedorAdapter(
            self._provedor_http, retry_policy=TEST_POLICY
        )
        if wrap_provider is not None:
            self.adapter = wrap_provider(self.adapter)
        self.runner = SyncRunner(
            self.sessions,
            AcademicoClient(self._academico_http, retry_policy=TEST_POLICY),
            self.adapter,
            ActionExecutor(
                self.adapter,
                ExecutionConfig(
                    retry_policy=TEST_POLICY,
                    batch_size=batch_size,
                    max_consecutive_failures=max_consecutive_failures,
                ),
            ),
        )

    async def aclose(self) -> None:
        await self.runner.shutdown()
        for client in (
            self._academico_http,
            self._provedor_http,
            self._academico_admin,
            self._provedor_admin,
        ):
            await client.aclose()

    # --- montar o estado desejado -----------------------------------------------------------

    async def set_academico(
        self,
        students: Mapping[str, str],
        classes: Iterable[str],
        enrollments: Iterable[tuple[str, str]],
        inactive: Iterable[str] = (),
    ) -> None:
        inactive = set(inactive)
        body = {
            "students": [
                {
                    "id": sid,
                    "first_name": name.partition(" ")[0],
                    "last_name": name.partition(" ")[2],
                    "status": "inactive" if sid in inactive else "active",
                }
                for sid, name in students.items()
            ],
            "classes": [{"id": cid, "name": f"Turma {cid}"} for cid in classes],
            "enrollments": [{"student_id": sid, "class_id": cid} for sid, cid in enrollments],
        }
        response = await self._academico_admin.put("/_admin/state", json=body)
        assert response.status_code == 200, response.text

    # --- executar -----------------------------------------------------------------------------

    async def sync(self, *, dry_run: bool = False) -> SyncRun:
        run_id = await self.runner.create_run(dry_run=dry_run)
        await self.runner.execute_run(run_id, dry_run=dry_run)
        return await self.run(run_id)

    async def retry(self, run_id: uuid.UUID) -> SyncRun:
        assert await self.runner.begin_retry(run_id)
        await self.runner.execute_retry(run_id)
        return await self.run(run_id)

    # --- inspeção -----------------------------------------------------------------------------

    async def run(self, run_id: uuid.UUID) -> SyncRun:
        async with self.sessions() as session:
            return await session.get_one(SyncRun, run_id)

    async def actions(self, run_id: uuid.UUID) -> dict[str, SyncAction]:
        async with self.sessions() as session:
            rows = await session.scalars(select(SyncAction).where(SyncAction.run_id == run_id))
            return {row.action_key: row for row in rows}

    async def mappings(self) -> set[tuple[str, str, str]]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(IdMapping.entity_type, IdMapping.source_id, IdMapping.provider_id)
            )
            return {(t, s, p) for t, s, p in rows.all()}

    @property
    def provider(self):  # noqa: ANN201 (store em memória do mock-provedor)
        return self.provedor_app.state.store

    def provider_members(self) -> dict[str, set[str]]:
        """Turma (id de origem) -> alunos (ids de origem) que o provedor realmente tem."""
        store_ = self.provider
        source_of_user = {u.id: u.external_id for u in store_.users.values()}
        return {
            school_class.external_id: {source_of_user[uid] for uid in store_.members[cid]}
            for cid, school_class in store_.classes.items()
        }

    def provider_users(self) -> dict[str, tuple[str, str]]:
        """Id de origem -> (e-mail, status)."""
        return {u.external_id: (u.email, u.status) for u in self.provider.users.values()}

    async def provider_stats(self) -> dict[str, int]:
        response = await self._provedor_admin.get("/_admin/stats")
        return response.json()["by_route"]

    async def provider_writes(self) -> int:
        routes = await self.provider_stats()
        return sum(n for route, n in routes.items() if not route.startswith("GET"))


__all__ = ["World", "reset_database", "store"]
