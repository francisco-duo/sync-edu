"""Execução do plano: transforma cada `SyncAction` numa chamada ao `ProvedorDeContas`.

Princípios:
- uma ação que falha NÃO interrompe as demais (falha parcial);
- retry só em erro transitório, com backoff e jitter (`resilience/`);
- ações cujas dependências falharam são puladas, sem chamar o provedor;
- se muitas ações seguidas esgotam as tentativas, o provedor parece fora do ar: aborta e
  deixa o resto como `skipped` para ser retomado depois (`retry-failed`);
- este módulo não conhece banco nem HTTP: quem grava os resultados é o `persist` recebido.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from typing import Any

from sync_service.domain.models import ActionType
from sync_service.domain.reconcile import PHASE_BY_TYPE
from sync_service.providers.base import ApplyResult, Outcome, ProvedorDeContas
from sync_service.resilience.errors import PermanentUpstreamError, UpstreamError
from sync_service.resilience.retry import RetriesExhaustedError, RetryPolicy, retry_async
from sync_service.sync.resolver import IdResolver
from sync_service.sync.status import ActionStatus, ErrorCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlannedAction:
    id: int
    seq: int
    key: str
    type: ActionType
    payload: Mapping[str, Any]
    depends_on: tuple[str, ...]
    attempts: int = 0  # tentativas de execuções anteriores (retry-failed acumula)


@dataclass(frozen=True, slots=True)
class MappingUpdate:
    entity_type: str  # "user" | "class"
    source_id: str
    provider_id: str


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: int
    key: str
    status: ActionStatus
    attempts: int  # total acumulado
    attempted_at: datetime | None
    outcome: Outcome | None = None
    error_code: str | None = None
    last_error: str | None = None
    mapping: MappingUpdate | None = None


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    retry_policy: RetryPolicy
    concurrency: int = 10
    batch_size: int = 200
    max_consecutive_failures: int = 25


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    aborted: bool


Persist = Callable[[list[ActionResult]], Awaitable[None]]


class ActionExecutor:
    def __init__(
        self,
        provider: ProvedorDeContas,
        config: ExecutionConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_retry: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._provider = provider
        self._config = config
        self._sleep = sleep
        self._uniform = uniform
        self._now = now
        self._on_retry = on_retry

    async def run(
        self,
        actions: list[PlannedAction],
        statuses: dict[str, ActionStatus],
        resolver: IdResolver,
        persist: Persist,
    ) -> ExecutionReport:
        """Executa `actions` por fase (barreira entre fases) e em lotes concorrentes.

        `statuses` mapeia a chave de TODAS as ações do run para o status atual; é usado para
        checar dependências e é atualizado conforme os lotes terminam.
        """
        semaphore = asyncio.Semaphore(self._config.concurrency)
        ordered = sorted(actions, key=lambda a: a.seq)
        consecutive_failures = 0

        for _, phase_iter in groupby(ordered, key=lambda a: PHASE_BY_TYPE[a.type]):
            phase = list(phase_iter)
            for start in range(0, len(phase), self._config.batch_size):
                batch = phase[start : start + self._config.batch_size]
                results = await asyncio.gather(
                    *(self._run_one(a, statuses, resolver, semaphore) for a in batch)
                )
                await persist(list(results))
                for result in results:
                    statuses[result.key] = result.status
                    if result.error_code == ErrorCode.RETRIES_EXHAUSTED:
                        consecutive_failures += 1
                    elif result.status is ActionStatus.SUCCEEDED:
                        consecutive_failures = 0
                if consecutive_failures >= self._config.max_consecutive_failures:
                    await self._abort_remaining(ordered, statuses, persist)
                    return ExecutionReport(aborted=True)
        return ExecutionReport(aborted=False)

    async def execute_action(self, action: PlannedAction, resolver: IdResolver) -> ActionResult:
        """Uma ação com retry. Nunca levanta: o resultado descreve sucesso ou falha."""
        calls = 0

        async def operation() -> ApplyResult:
            nonlocal calls
            calls += 1
            return await self._dispatch(action, resolver)

        try:
            applied = await retry_async(
                operation,
                self._config.retry_policy,
                sleep=self._sleep,
                uniform=self._uniform,
                on_retry=self._on_retry,
            )
        except RetriesExhaustedError as exc:
            return self._failed(action, calls, ErrorCode.RETRIES_EXHAUSTED, str(exc.last_error))
        except PermanentUpstreamError as exc:
            return self._failed(action, calls, exc.code, exc.message)
        except UpstreamError as exc:
            return self._failed(action, calls, exc.code, exc.message)
        except Exception as exc:  # um bug numa ação não pode derrubar o run inteiro
            logger.exception("erro inesperado ao executar %s", action.key)
            return self._failed(action, calls, ErrorCode.UNEXPECTED_ERROR, repr(exc))

        mapping = self._record_mapping(action, applied, resolver)
        return ActionResult(
            action_id=action.id,
            key=action.key,
            status=ActionStatus.SUCCEEDED,
            attempts=action.attempts + calls,
            attempted_at=self._now(),
            outcome=applied.outcome,
            mapping=mapping,
        )

    # --- internos -------------------------------------------------------------------------

    async def _run_one(
        self,
        action: PlannedAction,
        statuses: Mapping[str, ActionStatus],
        resolver: IdResolver,
        semaphore: asyncio.Semaphore,
    ) -> ActionResult:
        broken = [
            dep for dep in action.depends_on if statuses.get(dep) is not ActionStatus.SUCCEEDED
        ]
        if broken:
            return ActionResult(
                action_id=action.id,
                key=action.key,
                status=ActionStatus.SKIPPED,
                attempts=action.attempts,
                attempted_at=None,
                error_code=ErrorCode.DEPENDENCY_FAILED,
                last_error=f"dependências não concluídas: {', '.join(broken)}",
            )
        async with semaphore:
            return await self.execute_action(action, resolver)

    async def _abort_remaining(
        self,
        ordered: list[PlannedAction],
        statuses: dict[str, ActionStatus],
        persist: Persist,
    ) -> None:
        skipped = [
            ActionResult(
                action_id=a.id,
                key=a.key,
                status=ActionStatus.SKIPPED,
                attempts=a.attempts,
                attempted_at=None,
                error_code=ErrorCode.RUN_ABORTED,
                last_error="execução abortada: o provedor falhou repetidamente",
            )
            for a in ordered
            if statuses.get(a.key) in (ActionStatus.PENDING, None)
        ]
        for result in skipped:
            statuses[result.key] = result.status
        if skipped:
            await persist(skipped)

    def _failed(self, action: PlannedAction, calls: int, code: str, message: str) -> ActionResult:
        return ActionResult(
            action_id=action.id,
            key=action.key,
            status=ActionStatus.FAILED,
            attempts=action.attempts + calls,
            attempted_at=self._now(),
            error_code=code,
            last_error=message,
        )

    async def _dispatch(self, action: PlannedAction, resolver: IdResolver) -> ApplyResult:
        payload = action.payload
        match action.type:
            case ActionType.CREATE_CLASS:
                return await self._provider.create_class(
                    external_id=payload["class_source_id"], name=payload["name"]
                )
            case ActionType.CREATE_USER:
                return await self._provider.create_user(
                    external_id=payload["student_source_id"],
                    email=payload["email"],
                    first_name=payload["first_name"],
                    last_name=payload["last_name"],
                )
            case ActionType.REACTIVATE_USER:
                return await self._provider.reactivate_user(
                    provider_user_id=_user_id(payload, resolver)
                )
            case ActionType.SUSPEND_USER:
                return await self._provider.suspend_user(
                    provider_user_id=_user_id(payload, resolver)
                )
            case ActionType.ADD_TO_CLASS:
                return await self._provider.add_member(
                    provider_class_id=_class_id(payload, resolver),
                    provider_user_id=_user_id(payload, resolver),
                )
            case ActionType.REMOVE_FROM_CLASS:
                return await self._provider.remove_member(
                    provider_class_id=_class_id(payload, resolver),
                    provider_user_id=_user_id(payload, resolver),
                )

    @staticmethod
    def _record_mapping(
        action: PlannedAction, applied: ApplyResult, resolver: IdResolver
    ) -> MappingUpdate | None:
        if applied.provider_id is None:
            return None
        if action.type is ActionType.CREATE_USER:
            source_id = action.payload["student_source_id"]
            resolver.users[source_id] = applied.provider_id
            return MappingUpdate("user", source_id, applied.provider_id)
        if action.type is ActionType.CREATE_CLASS:
            source_id = action.payload["class_source_id"]
            resolver.classes[source_id] = applied.provider_id
            return MappingUpdate("class", source_id, applied.provider_id)
        return None


def _user_id(payload: Mapping[str, Any], resolver: IdResolver) -> str:
    provider_id = resolver.user(payload["student_source_id"])
    if provider_id is None:
        raise PermanentUpstreamError(
            f"sem mapeamento para o aluno {payload['student_source_id']}",
            code=ErrorCode.MAPPING_NOT_FOUND,
        )
    return provider_id


def _class_id(payload: Mapping[str, Any], resolver: IdResolver) -> str:
    provider_id = resolver.school_class(payload["class_source_id"])
    if provider_id is None:
        raise PermanentUpstreamError(
            f"sem mapeamento para a turma {payload['class_source_id']}",
            code=ErrorCode.MAPPING_NOT_FOUND,
        )
    return provider_id
