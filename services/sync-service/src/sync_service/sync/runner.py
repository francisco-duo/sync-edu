"""Orquestra um run: buscar estados, reconciliar, registrar o plano e (se real) executá-lo.

Fluxo (veja docs/ESPECIFICACAO.md, seção 1.4):
  buscar desejado + atual -> reconciliar (função pura) -> gravar plano -> executar -> fechar run
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Coroutine
from typing import Any

from sync_service.clients.academico import AcademicoClient
from sync_service.domain.models import InvalidStateError
from sync_service.domain.reconcile import reconcile
from sync_service.providers.base import ProvedorDeContas
from sync_service.resilience.errors import UpstreamError
from sync_service.resilience.retry import RetriesExhaustedError
from sync_service.sync import store
from sync_service.sync.executor import ActionExecutor, ActionResult, MappingUpdate
from sync_service.sync.resolver import IdResolver
from sync_service.sync.snapshot import CurrentView, build_current_view
from sync_service.sync.status import ActionStatus, RunTrigger
from sync_service.telemetry import Telemetry

logger = logging.getLogger(__name__)


class SyncRunner:
    def __init__(
        self,
        sessions: store.SessionFactory,
        academico: AcademicoClient,
        provider: ProvedorDeContas,
        executor: ActionExecutor,
        *,
        email_domain: str = "example.edu",
        telemetry: Telemetry | None = None,
    ) -> None:
        self._sessions = sessions
        self._academico = academico
        self._provider = provider
        self._executor = executor
        self._email_domain = email_domain
        self._telemetry = telemetry or Telemetry()
        self._tasks: set[asyncio.Task[None]] = set()

    # --- ciclo de vida das tarefas em segundo plano ---------------------------------------

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Ex.: o banco caiu até na hora de fechar o run. O run pode ficar "running" até o
            # próximo startup marcá-lo como `interrupted`; pelo menos isto aparece no log.
            logger.error("tarefa de sincronização terminou com erro", exc_info=task.exception())

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.wait_idle()

    # --- runs -----------------------------------------------------------------------------

    async def create_run(self, *, dry_run: bool, trigger: str = RunTrigger.MANUAL) -> uuid.UUID:
        return await store.create_run(self._sessions, dry_run=dry_run, trigger=trigger)

    async def execute_run(self, run_id: uuid.UUID, *, dry_run: bool) -> None:
        """Calcula o plano e, se não for dry-run, o executa. Falhas esperadas viram `failed`."""
        metrics: dict[str, Any] = {}
        before = self._telemetry.snapshot()
        try:
            started = time.perf_counter()
            desired, snapshot = await asyncio.gather(
                self._academico.fetch_desired_state(), self._provider.snapshot()
            )
            metrics["fetch_ms"] = _elapsed_ms(started)

            view = build_current_view(snapshot)
            if not dry_run:
                await self._sync_mappings(view)

            started = time.perf_counter()
            plan = reconcile(desired, view.state, email_domain=self._email_domain)
            metrics["reconcile_ms"] = _elapsed_ms(started)

            status = ActionStatus.PLANNED if dry_run else ActionStatus.PENDING
            await store.insert_actions(self._sessions, run_id, plan, status)

            aborted = False
            if not dry_run:
                started = time.perf_counter()
                aborted = await self._execute_pending(run_id, view.resolver)
                metrics["execute_ms"] = _elapsed_ms(started)
            metrics.update(self._telemetry_since(before))
            await store.finalize_run(self._sessions, run_id, aborted=aborted, metrics=metrics)
        except asyncio.CancelledError:
            raise  # o startup seguinte marca o run como `interrupted`
        except (UpstreamError, RetriesExhaustedError, InvalidStateError) as exc:
            logger.warning("run %s falhou: %s", run_id, exc)
            metrics.update(self._telemetry_since(before))
            await store.finalize_run(self._sessions, run_id, error=str(exc), metrics=metrics)
        except Exception as exc:
            logger.exception("run %s falhou de forma inesperada", run_id)
            metrics.update(self._telemetry_since(before))
            await store.finalize_run(self._sessions, run_id, error=repr(exc), metrics=metrics)

    async def begin_retry(self, run_id: uuid.UUID) -> bool:
        """Reabre o run. False = não há ações elegíveis (nada a fazer)."""
        return await store.begin_retry(self._sessions, run_id)

    async def execute_retry(self, run_id: uuid.UUID) -> None:
        """Reprocessa só as ações elegíveis, SEM recalcular o plano.

        Os ids do provedor vêm da tabela `id_mappings`, então não é preciso buscar o estado de
        novo. O plano original continua válido porque cada operação é idempotente.
        """
        before = self._telemetry.snapshot()
        try:
            resolver = await store.load_resolver(self._sessions)
            aborted = await self._execute_pending(run_id, resolver)
            metrics = {"last_retry": self._telemetry_since(before)}
            await store.finalize_run(self._sessions, run_id, aborted=aborted, metrics=metrics)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("retry do run %s falhou de forma inesperada", run_id)
            await store.finalize_run(self._sessions, run_id, error=repr(exc))

    # --- internos -------------------------------------------------------------------------

    async def _sync_mappings(self, view: CurrentView) -> None:
        """Alinha `id_mappings` com o que o provedor realmente tem (só em runs reais).

        O snapshot manda: vínculo que sumiu do provedor é descartado (a entidade será
        recriada) e os que faltam são gravados. Dry-run não escreve nada aqui.
        """
        live = [
            MappingUpdate(entity_type, source_id, provider_id)
            for entity_type, ids in (
                ("user", view.resolver.users),
                ("class", view.resolver.classes),
            )
            for source_id, provider_id in ids.items()
        ]
        await store.mirror_live_mappings(self._sessions, live)

    async def _execute_pending(self, run_id: uuid.UUID, resolver: IdResolver) -> bool:
        actions = await store.load_pending_actions(self._sessions, run_id)
        statuses = await store.load_run_statuses(self._sessions, run_id)

        async def persist(results: list[ActionResult]) -> None:
            await store.persist_results(self._sessions, results)

        report = await self._executor.run(actions, statuses, resolver, persist)
        return report.aborted

    def _telemetry_since(self, before: dict[str, dict[str, int]]) -> dict[str, Any]:
        delta = Telemetry.delta(before, self._telemetry.snapshot())
        return {"http_calls": delta["http_calls"], "retries": delta["retries"]}


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
