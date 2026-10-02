import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from sync_service.api.schemas import (
    ActionOut,
    ActionPage,
    RunPage,
    RunSummary,
    RunWithActions,
)
from sync_service.errors import ApiError
from sync_service.sync import store
from sync_service.sync.runner import SyncRunner
from sync_service.sync.status import RunStatus

router = APIRouter(prefix="/sync-runs", tags=["sync-runs"])

PageNumber = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=500)]


def _runner(request: Request) -> SyncRunner:
    return request.app.state.runner


def _sessions(request: Request) -> store.SessionFactory:
    return request.app.state.session_factory


def _in_progress(exc: store.RunInProgressError) -> ApiError:
    return ApiError(
        409,
        "RUN_IN_PROGRESS",
        "já existe uma sincronização em execução",
        {"run_id": str(exc.run_id) if exc.run_id else None},
    )


async def _require_run(request: Request, run_id: uuid.UUID):  # noqa: ANN202
    run = await store.get_run(_sessions(request), run_id)
    if run is None:
        raise ApiError(404, "RUN_NOT_FOUND", f"execução {run_id} não encontrada")
    return run


@router.post("", status_code=202, response_model=RunSummary | RunWithActions)
async def create_sync_run(
    request: Request, response: Response, dry_run: bool = False
) -> RunSummary | RunWithActions:
    """Cria uma sincronização.

    - `dry_run=true`: calcula e devolve o plano (200), sem executar nada no provedor.
    - `dry_run=false`: registra a execução e a roda em segundo plano (202).
    """
    runner = _runner(request)
    try:
        run_id = await runner.create_run(dry_run=dry_run)
    except store.RunInProgressError as exc:
        raise _in_progress(exc) from exc

    if not dry_run:
        runner.spawn(runner.execute_run(run_id, dry_run=False))
        response.headers["Location"] = f"/sync-runs/{run_id}"
        run = await _require_run(request, run_id)
        return RunSummary.model_validate(run)

    await runner.execute_run(run_id, dry_run=True)
    run = await _require_run(request, run_id)
    if run.status == RunStatus.FAILED:
        raise ApiError(
            502,
            "UPSTREAM_UNAVAILABLE",
            run.error or "não foi possível calcular o plano",
            {"run_id": str(run_id)},
        )
    actions, _ = await store.list_actions(
        _sessions(request), run_id, page=1, page_size=1_000_000, status=None, action_type=None
    )
    response.status_code = 200
    return RunWithActions(
        **RunSummary.model_validate(run).model_dump(),
        actions=[ActionOut.model_validate(a) for a in actions],
    )


@router.get("", response_model=RunPage)
async def list_sync_runs(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = 100,
    status: str | None = None,
    dry_run: bool | None = None,
) -> RunPage:
    runs, total = await store.list_runs(
        _sessions(request), page=page, page_size=page_size, status=status, dry_run=dry_run
    )
    return RunPage(
        items=[RunSummary.model_validate(r) for r in runs],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/{run_id}", response_model=RunSummary)
async def get_sync_run(request: Request, run_id: uuid.UUID) -> RunSummary:
    return RunSummary.model_validate(await _require_run(request, run_id))


@router.get("/{run_id}/actions", response_model=ActionPage)
async def list_sync_actions(
    request: Request,
    run_id: uuid.UUID,
    page: PageNumber = 1,
    page_size: PageSize = 100,
    status: str | None = None,
    action_type: str | None = None,
) -> ActionPage:
    await _require_run(request, run_id)
    actions, total = await store.list_actions(
        _sessions(request),
        run_id,
        page=page,
        page_size=page_size,
        status=status,
        action_type=action_type,
    )
    return ActionPage(
        items=[ActionOut.model_validate(a) for a in actions],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post("/{run_id}/retry-failed", status_code=202, response_model=RunSummary)
async def retry_failed(request: Request, response: Response, run_id: uuid.UUID) -> RunSummary:
    """Reprocessa só as ações elegíveis (falhas e puladas por dependência/abort) do run.

    Não recalcula o plano. Sem nada elegível, devolve 200 sem fazer nada (operação idempotente).
    """
    runner = _runner(request)
    try:
        scheduled = await runner.begin_retry(run_id)
    except store.RunNotFoundError as exc:
        raise ApiError(404, "RUN_NOT_FOUND", f"execução {run_id} não encontrada") from exc
    except store.RunIsDryRunError as exc:
        raise ApiError(
            409, "RUN_IS_DRY_RUN", "dry-run não executa ações, então não há o que reprocessar"
        ) from exc
    except store.RunInProgressError as exc:
        raise _in_progress(exc) from exc

    if scheduled:
        runner.spawn(runner.execute_retry(run_id))
    else:
        response.status_code = 200
    return RunSummary.model_validate(await _require_run(request, run_id))
