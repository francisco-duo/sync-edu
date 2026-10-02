"""Acesso ao PostgreSQL do sync. Funções simples sobre `AsyncSession`, sem N+1.

Cada função abre a sua própria transação curta: o chamador não precisa gerenciar sessão.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sync_service.db.models import IdMapping, SyncAction, SyncRun
from sync_service.domain.models import ActionType
from sync_service.domain.models import SyncAction as PlannedSyncAction
from sync_service.sync.executor import ActionResult, MappingUpdate, PlannedAction
from sync_service.sync.resolver import IdResolver
from sync_service.sync.status import RETRYABLE_SKIP_CODES, ActionStatus, RunStatus

SessionFactory = async_sessionmaker[AsyncSession]
INSERT_BLOCK = 1000
SINGLE_RUNNING_INDEX = "ux_sync_runs_single_running"


class RunNotFoundError(Exception):
    pass


class RunInProgressError(Exception):
    def __init__(self, run_id: uuid.UUID | None) -> None:
        super().__init__(f"já existe um run em execução: {run_id}")
        self.run_id = run_id


class RunIsDryRunError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


# --- runs -----------------------------------------------------------------------------------


async def create_run(
    sessions: SessionFactory, *, dry_run: bool, trigger: str, now: datetime | None = None
) -> uuid.UUID:
    run = SyncRun(
        id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        trigger=trigger,
        dry_run=dry_run,
        started_at=now or _now(),
    )
    try:
        async with sessions() as session, session.begin():
            session.add(run)
    except IntegrityError as exc:
        if SINGLE_RUNNING_INDEX not in str(exc.orig):
            raise
        raise RunInProgressError(await running_run_id(sessions)) from exc
    return run.id


async def running_run_id(sessions: SessionFactory) -> uuid.UUID | None:
    async with sessions() as session:
        return await session.scalar(
            select(SyncRun.id).where(
                SyncRun.status == RunStatus.RUNNING, SyncRun.dry_run.is_(False)
            )
        )


async def get_run(sessions: SessionFactory, run_id: uuid.UUID) -> SyncRun | None:
    async with sessions() as session:
        return await session.get(SyncRun, run_id)


async def list_runs(
    sessions: SessionFactory,
    *,
    page: int,
    page_size: int,
    status: str | None,
    dry_run: bool | None,
) -> tuple[list[SyncRun], int]:
    filters = []
    if status is not None:
        filters.append(SyncRun.status == status)
    if dry_run is not None:
        filters.append(SyncRun.dry_run.is_(dry_run))
    async with sessions() as session:
        total = await session.scalar(select(func.count()).select_from(SyncRun).where(*filters))
        rows = await session.scalars(
            select(SyncRun)
            .where(*filters)
            .order_by(SyncRun.started_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), total or 0


async def list_actions(
    sessions: SessionFactory,
    run_id: uuid.UUID,
    *,
    page: int,
    page_size: int,
    status: str | None,
    action_type: str | None,
) -> tuple[list[SyncAction], int]:
    filters = [SyncAction.run_id == run_id]
    if status is not None:
        filters.append(SyncAction.status == status)
    if action_type is not None:
        filters.append(SyncAction.action_type == action_type)
    async with sessions() as session:
        total = await session.scalar(select(func.count()).select_from(SyncAction).where(*filters))
        rows = await session.scalars(
            select(SyncAction)
            .where(*filters)
            .order_by(SyncAction.seq)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), total or 0


async def mark_interrupted_runs(sessions: SessionFactory) -> int:
    """No startup: um run `running` sem processo vivo foi interrompido por uma queda."""
    async with sessions() as session, session.begin():
        result = await session.execute(
            update(SyncRun)
            .where(SyncRun.status == RunStatus.RUNNING)
            .values(status=RunStatus.INTERRUPTED, finished_at=_now())
        )
    return result.rowcount or 0


async def finalize_run(
    sessions: SessionFactory,
    run_id: uuid.UUID,
    *,
    aborted: bool = False,
    error: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> RunStatus:
    """Fecha o run recalculando os contadores por agregação (nunca por contagem em memória)."""
    async with sessions() as session, session.begin():
        run = await session.get(SyncRun, run_id, with_for_update=True)
        if run is None:
            raise RunNotFoundError(str(run_id))
        status_rows = await session.execute(
            select(SyncAction.status, func.count())
            .where(SyncAction.run_id == run_id)
            .group_by(SyncAction.status)
        )
        by_status = dict(status_rows.all())
        type_rows = await session.execute(
            select(SyncAction.action_type, func.count())
            .where(SyncAction.run_id == run_id)
            .group_by(SyncAction.action_type)
        )
        by_type = dict(type_rows.all())
        failed = by_status.get(ActionStatus.FAILED, 0)
        skipped = by_status.get(ActionStatus.SKIPPED, 0)

        if error is not None:
            status = RunStatus.FAILED
        elif run.dry_run:
            status = RunStatus.PLANNED
        elif aborted:
            status = RunStatus.ABORTED
        elif failed or skipped or by_status.get(ActionStatus.PENDING, 0):
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.SUCCEEDED

        run.status = status
        run.finished_at = _now()
        run.error = error
        run.total_actions = sum(by_status.values())
        run.successful_actions = by_status.get(ActionStatus.SUCCEEDED, 0)
        run.failed_actions = failed
        run.skipped_actions = skipped
        run.counters = by_type
        if metrics:
            run.metrics = {**run.metrics, **metrics}
    return status


# --- ações ----------------------------------------------------------------------------------


async def insert_actions(
    sessions: SessionFactory,
    run_id: uuid.UUID,
    plan: Sequence[PlannedSyncAction],
    status: ActionStatus,
) -> None:
    rows = [
        {
            "run_id": run_id,
            "seq": seq,
            "action_key": action.key,
            "action_type": action.type.value,
            "entity_type": action.entity_type.value,
            "entity_id": action.entity_id,
            "payload": dict(action.payload),
            "depends_on": list(action.depends_on),
            "status": status.value,
        }
        for seq, action in enumerate(plan)
    ]
    async with sessions() as session, session.begin():
        for start in range(0, len(rows), INSERT_BLOCK):
            await session.execute(pg_insert(SyncAction), rows[start : start + INSERT_BLOCK])


async def load_run_statuses(sessions: SessionFactory, run_id: uuid.UUID) -> dict[str, ActionStatus]:
    async with sessions() as session:
        rows = await session.execute(
            select(SyncAction.action_key, SyncAction.status).where(SyncAction.run_id == run_id)
        )
        return {key: ActionStatus(status) for key, status in rows.all()}


async def load_pending_actions(sessions: SessionFactory, run_id: uuid.UUID) -> list[PlannedAction]:
    async with sessions() as session:
        rows = await session.scalars(
            select(SyncAction)
            .where(SyncAction.run_id == run_id, SyncAction.status == ActionStatus.PENDING)
            .order_by(SyncAction.seq)
        )
        return [
            PlannedAction(
                id=row.id,
                seq=row.seq,
                key=row.action_key,
                type=ActionType(row.action_type),
                payload=row.payload,
                depends_on=tuple(row.depends_on),
                attempts=row.attempts,
            )
            for row in rows
        ]


# Um único UPDATE por lote (em vez de um comando por ação): os valores viajam como arrays e o
# PostgreSQL faz o join com `unnest`. 1 ida ao banco para N linhas, usando a chave primária.
_BULK_UPDATE_ACTIONS = text(
    """
    UPDATE sync_actions AS a
    SET status = v.status,
        outcome = v.outcome,
        attempts = v.attempts,
        error_code = v.error_code,
        last_error = v.last_error,
        last_attempt_at = v.last_attempt_at,
        updated_at = now()
    FROM unnest(
        CAST(:ids AS bigint[]),
        CAST(:statuses AS varchar[]),
        CAST(:outcomes AS varchar[]),
        CAST(:attempts AS integer[]),
        CAST(:error_codes AS varchar[]),
        CAST(:last_errors AS text[]),
        CAST(:attempted_at AS timestamptz[])
    ) AS v(id, status, outcome, attempts, error_code, last_error, last_attempt_at)
    WHERE a.id = v.id
    """
)


async def persist_results(sessions: SessionFactory, results: Sequence[ActionResult]) -> None:
    """Grava um lote de resultados (e os mapeamentos criados) numa única transação."""
    if not results:
        return
    params = {
        "ids": [r.action_id for r in results],
        "statuses": [r.status.value for r in results],
        "outcomes": [r.outcome.value if r.outcome else None for r in results],
        "attempts": [r.attempts for r in results],
        "error_codes": [r.error_code for r in results],
        "last_errors": [r.last_error for r in results],
        "attempted_at": [r.attempted_at for r in results],
    }
    mappings = [r.mapping for r in results if r.mapping is not None]
    async with sessions() as session, session.begin():
        await session.execute(_BULK_UPDATE_ACTIONS, params)
        await _upsert_mappings(session, mappings)


async def begin_retry(sessions: SessionFactory, run_id: uuid.UUID) -> bool:
    """Reabre o run para reprocessar só o que não terminou bem. False = nada a reprocessar."""
    retryable = or_(
        SyncAction.status.in_([ActionStatus.FAILED, ActionStatus.PENDING]),
        and_(
            SyncAction.status == ActionStatus.SKIPPED,
            SyncAction.error_code.in_(RETRYABLE_SKIP_CODES),
        ),
    )
    try:
        async with sessions() as session, session.begin():
            run = await session.get(SyncRun, run_id, with_for_update=True)
            if run is None:
                raise RunNotFoundError(str(run_id))
            if run.dry_run:
                raise RunIsDryRunError(str(run_id))
            if run.status == RunStatus.RUNNING:
                raise RunInProgressError(run_id)
            ids = list(
                await session.scalars(
                    select(SyncAction.id).where(SyncAction.run_id == run_id, retryable)
                )
            )
            if not ids:
                return False
            await session.execute(
                update(SyncAction).where(SyncAction.id.in_(ids)).values(status=ActionStatus.PENDING)
            )
            run.status = RunStatus.RUNNING  # o índice único recusa se outro run real está ativo
            run.finished_at = None
            run.retry_count += 1
            run.last_retry_at = _now()
    except IntegrityError as exc:
        if SINGLE_RUNNING_INDEX not in str(exc.orig):
            raise
        raise RunInProgressError(await running_run_id(sessions)) from exc
    return True


# --- mapeamentos ----------------------------------------------------------------------------


async def load_resolver(sessions: SessionFactory) -> IdResolver:
    async with sessions() as session:
        rows = await session.execute(
            select(IdMapping.entity_type, IdMapping.source_id, IdMapping.provider_id)
        )
        resolver = IdResolver()
        for entity_type, source_id, provider_id in rows.all():
            target = resolver.users if entity_type == "user" else resolver.classes
            target[source_id] = provider_id
        return resolver


async def mirror_live_mappings(sessions: SessionFactory, live: Iterable[MappingUpdate]) -> None:
    """Faz `id_mappings` espelhar exatamente o que o provedor tem agora (snapshot = verdade).

    Remove os vínculos que não existem mais (ou que mudaram) e insere os que faltam, sem
    estourar as restrições de unicidade, mesmo que o provedor reaproveite ids.
    """
    wanted = {(m.entity_type, m.source_id, m.provider_id) for m in live}
    async with sessions() as session, session.begin():
        rows = await session.execute(
            select(IdMapping.id, IdMapping.entity_type, IdMapping.source_id, IdMapping.provider_id)
        )
        existing = {(t, s, p): row_id for row_id, t, s, p in rows.all()}
        stale_ids = [row_id for triple, row_id in existing.items() if triple not in wanted]
        if stale_ids:
            await session.execute(delete(IdMapping).where(IdMapping.id.in_(stale_ids)))
        missing = [MappingUpdate(*triple) for triple in wanted - existing.keys()]
        await _upsert_mappings(session, missing)


async def _upsert_mappings(session: AsyncSession, mappings: list[MappingUpdate]) -> None:
    if not mappings:
        return
    statement = pg_insert(IdMapping)
    statement = statement.on_conflict_do_update(
        constraint="uq_id_mappings_entity_type_source_id",
        set_={"provider_id": statement.excluded.provider_id, "updated_at": func.now()},
    )
    rows = [
        {"entity_type": m.entity_type, "source_id": m.source_id, "provider_id": m.provider_id}
        for m in mappings
    ]
    await session.execute(statement, rows)
