"""Construtor de `PlannedAction` para testes do executor."""

from itertools import count

from sync_service.domain.models import ActionType
from sync_service.domain.reconcile import PHASE_BY_TYPE
from sync_service.sync.executor import PlannedAction

_ids = count(1)


def planned(
    action_type: ActionType,
    entity_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    attempts: int = 0,
    action_id: int | None = None,
    seq: int | None = None,
    **payload: str,
) -> PlannedAction:
    n = action_id if action_id is not None else next(_ids)
    return PlannedAction(
        id=n,
        seq=seq if seq is not None else PHASE_BY_TYPE[action_type] * 1000 + n,
        key=f"{action_type}:{entity_id}",
        type=action_type,
        payload=payload,
        depends_on=depends_on,
        attempts=attempts,
    )
