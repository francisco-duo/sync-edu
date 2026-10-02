"""Executor com provedor falso: falha parcial, dependências, fases e circuit breaker."""

import asyncio
from datetime import UTC, datetime

import pytest

from sync_service.domain.models import ActionType
from sync_service.providers.base import ApplyResult, Outcome
from sync_service.resilience.errors import PermanentUpstreamError, TransientUpstreamError
from sync_service.resilience.retry import RetryPolicy
from sync_service.sync.executor import (
    ActionExecutor,
    ActionResult,
    ExecutionConfig,
    PlannedAction,
)
from sync_service.sync.resolver import IdResolver
from sync_service.sync.status import ActionStatus, ErrorCode
from sync_testkit.actions import planned

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
POLICY = RetryPolicy(max_attempts=3, base_delay=0.0, jitter="none")


async def no_sleep(_: float) -> None:
    return None


class FakeProvider:
    """Provedor falso: registra as chamadas e falha para os ids configurados."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.permanent: set[str] = set()  # source_ids que sempre dão erro permanente
        self.transient: set[str] = set()  # source_ids que sempre dão erro transitório
        self.in_flight = 0
        self.max_in_flight = 0

    async def _record(self, name: str, subject: str) -> None:
        self.calls.append(f"{name}:{subject}")
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)  # cede o controle para outras ações rodarem junto
        self.in_flight -= 1
        if subject in self.permanent:
            raise PermanentUpstreamError(f"{subject} inválido", code="VALIDATION_ERROR")
        if subject in self.transient:
            raise TransientUpstreamError(f"{subject} indisponível", code="HTTP_503")

    async def create_class(self, *, external_id: str, name: str) -> ApplyResult:
        await self._record("create_class", external_id)
        return ApplyResult(Outcome.APPLIED, f"cls_{external_id}")

    async def create_user(
        self, *, external_id: str, email: str, first_name: str, last_name: str
    ) -> ApplyResult:
        await self._record("create_user", external_id)
        return ApplyResult(Outcome.APPLIED, f"usr_{external_id}")

    async def suspend_user(self, *, provider_user_id: str) -> ApplyResult:
        await self._record("suspend", provider_user_id)
        return ApplyResult(Outcome.APPLIED)

    async def reactivate_user(self, *, provider_user_id: str) -> ApplyResult:
        await self._record("reactivate", provider_user_id)
        return ApplyResult(Outcome.APPLIED)

    async def add_member(self, *, provider_class_id: str, provider_user_id: str) -> ApplyResult:
        await self._record("add", provider_user_id)
        return ApplyResult(Outcome.APPLIED)

    async def remove_member(self, *, provider_class_id: str, provider_user_id: str) -> ApplyResult:
        await self._record("remove", provider_user_id)
        return ApplyResult(Outcome.APPLIED)

    async def snapshot(self):  # pragma: no cover (não usado aqui)
        raise NotImplementedError


class Harness:
    def __init__(self, **config: int) -> None:
        self.provider = FakeProvider()
        self.executor = ActionExecutor(
            self.provider,
            ExecutionConfig(retry_policy=POLICY, **config),
            sleep=no_sleep,
            now=lambda: NOW,
        )
        self.resolver = IdResolver()
        self.persisted: list[list[ActionResult]] = []

    async def persist(self, results: list[ActionResult]) -> None:
        self.persisted.append(list(results))

    async def run(
        self, actions: list[PlannedAction], statuses: dict[str, ActionStatus] | None = None
    ):
        statuses = statuses if statuses is not None else {}
        for action in actions:
            statuses.setdefault(action.key, ActionStatus.PENDING)
        report = await self.executor.run(actions, statuses, self.resolver, self.persist)
        return report, statuses

    @property
    def results(self) -> dict[str, ActionResult]:
        return {r.key: r for batch in self.persisted for r in batch}


def create_user(student: str, **kwargs) -> PlannedAction:
    return planned(
        ActionType.CREATE_USER,
        student,
        student_source_id=student,
        first_name="N",
        last_name="S",
        email=f"{student}@x.edu",
        **kwargs,
    )


def suspend(student: str, **kwargs) -> PlannedAction:
    return planned(ActionType.SUSPEND_USER, student, student_source_id=student, **kwargs)


# --- falha parcial ------------------------------------------------------------------------


async def test_falha_na_acao_3_nao_aborta_a_execucao() -> None:
    harness = Harness()
    harness.provider.permanent.add("usr_S3")
    harness.resolver.users.update({f"S{n}": f"usr_S{n}" for n in range(1, 5)})
    actions = [suspend(f"S{n}") for n in range(1, 5)]

    report, _ = await harness.run(actions)

    results = harness.results
    assert [results[a.key].status for a in actions] == [
        ActionStatus.SUCCEEDED,
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.SUCCEEDED,  # a ação 4 foi executada mesmo depois da falha da 3
    ]
    assert not report.aborted


async def test_acao_falha_registra_tentativas_ultimo_erro_e_horario() -> None:
    harness = Harness()
    harness.provider.transient.add("usr_S3")
    harness.resolver.users["S3"] = "usr_S3"

    await harness.run([suspend("S3")])

    failed = harness.results["SUSPEND_USER:S3"]
    assert failed.status is ActionStatus.FAILED
    assert failed.attempts == POLICY.max_attempts == 3
    assert failed.error_code == ErrorCode.RETRIES_EXHAUSTED
    assert failed.last_error is not None
    assert "indisponível" in failed.last_error
    assert failed.attempted_at == NOW


async def test_erro_permanente_falha_na_primeira_tentativa() -> None:
    harness = Harness()
    harness.provider.permanent.add("usr_S1")
    harness.resolver.users["S1"] = "usr_S1"

    await harness.run([suspend("S1")])

    failed = harness.results["SUSPEND_USER:S1"]
    assert (failed.status, failed.attempts, failed.error_code) == (
        ActionStatus.FAILED,
        1,
        "VALIDATION_ERROR",
    )


async def test_bug_inesperado_numa_acao_nao_derruba_as_outras() -> None:
    harness = Harness()

    async def boom(**_: str) -> ApplyResult:
        raise RuntimeError("bug")

    harness.provider.suspend_user = boom  # type: ignore[method-assign]
    harness.resolver.users.update({"S1": "usr_S1", "S2": "usr_S2"})
    first = suspend("S1")
    second = planned(ActionType.REACTIVATE_USER, "S2", student_source_id="S2")

    await harness.run([first, second])

    assert harness.results[first.key].error_code == ErrorCode.UNEXPECTED_ERROR
    assert harness.results[second.key].status is ActionStatus.SUCCEEDED


# --- mapeamento, dependências e fases -----------------------------------------------------


async def test_acao_sem_mapeamento_falha_com_erro_claro() -> None:
    harness = Harness()

    await harness.run([suspend("S1")])  # resolver vazio

    failed = harness.results["SUSPEND_USER:S1"]
    assert failed.error_code == ErrorCode.MAPPING_NOT_FOUND
    assert failed.attempts == 1  # erro permanente: não adianta repetir
    assert harness.provider.calls == []


async def test_criacao_registra_o_mapeamento_e_a_proxima_fase_o_usa() -> None:
    harness = Harness()
    create = create_user("S1")
    add = planned(
        ActionType.ADD_TO_CLASS,
        "S1|A",
        depends_on=(create.key,),
        student_source_id="S1",
        class_source_id="A",
    )
    harness.resolver.classes["A"] = "cls_A"

    await harness.run([add, create])  # fora de ordem de propósito

    assert harness.results[create.key].mapping is not None
    assert harness.resolver.users["S1"] == "usr_S1"
    assert harness.results[add.key].status is ActionStatus.SUCCEEDED


async def test_fases_sao_executadas_em_ordem_com_barreira() -> None:
    harness = Harness()
    harness.resolver.classes["A"] = "cls_A"
    harness.resolver.users["S9"] = "usr_S9"
    actions = [
        suspend("S9"),
        planned(
            ActionType.ADD_TO_CLASS,
            "S1|A",
            student_source_id="S1",
            class_source_id="A",
            depends_on=("CREATE_USER:S1",),
        ),
        create_user("S1"),
        planned(ActionType.CREATE_CLASS, "A", class_source_id="A", name="Turma A"),
    ]

    await harness.run(actions)

    assert harness.provider.calls == [
        "create_class:A",
        "create_user:S1",
        "add:usr_S1",
        "suspend:usr_S9",
    ]


async def test_dependencia_que_falhou_pula_a_acao_sem_chamar_o_provedor() -> None:
    harness = Harness()
    harness.provider.permanent.add("S1")
    harness.resolver.classes["A"] = "cls_A"
    create = create_user("S1")
    add = planned(
        ActionType.ADD_TO_CLASS,
        "S1|A",
        depends_on=(create.key,),
        student_source_id="S1",
        class_source_id="A",
    )

    await harness.run([create, add])

    skipped = harness.results[add.key]
    assert skipped.status is ActionStatus.SKIPPED
    assert skipped.error_code == ErrorCode.DEPENDENCY_FAILED
    assert skipped.attempts == 0
    assert "add:usr_S1" not in harness.provider.calls


async def test_dependencia_ja_concluida_em_outro_momento_libera_a_acao() -> None:
    harness = Harness()
    harness.resolver.users["S1"] = "usr_S1"
    harness.resolver.classes["A"] = "cls_A"
    add = planned(
        ActionType.ADD_TO_CLASS,
        "S1|A",
        depends_on=("CREATE_USER:S1",),
        student_source_id="S1",
        class_source_id="A",
    )

    await harness.run([add], statuses={"CREATE_USER:S1": ActionStatus.SUCCEEDED})

    assert harness.results[add.key].status is ActionStatus.SUCCEEDED


# --- persistência em lotes e concorrência -------------------------------------------------


async def test_resultados_sao_persistidos_em_lotes() -> None:
    harness = Harness(batch_size=3)
    harness.resolver.users.update({f"S{n}": f"usr_S{n}" for n in range(1, 8)})

    await harness.run([suspend(f"S{n}") for n in range(1, 8)])

    assert [len(batch) for batch in harness.persisted] == [3, 3, 1]


async def test_concorrencia_e_limitada_pelo_semaforo() -> None:
    harness = Harness(concurrency=2, batch_size=50)
    harness.resolver.users.update({f"S{n}": f"usr_S{n}" for n in range(1, 21)})

    await harness.run([suspend(f"S{n}") for n in range(1, 21)])

    assert harness.provider.max_in_flight == 2


# --- circuit breaker ----------------------------------------------------------------------


async def test_provedor_fora_do_ar_aborta_e_deixa_o_resto_para_retomar() -> None:
    harness = Harness(batch_size=2, max_consecutive_failures=4)
    for n in range(1, 11):
        harness.resolver.users[f"S{n}"] = f"usr_S{n}"
        harness.provider.transient.add(f"usr_S{n}")

    report, statuses = await harness.run([suspend(f"S{n}") for n in range(1, 11)])

    assert report.aborted
    results = harness.results
    exhausted = [r for r in results.values() if r.error_code == ErrorCode.RETRIES_EXHAUSTED]
    aborted = [r for r in results.values() if r.error_code == ErrorCode.RUN_ABORTED]
    assert len(exhausted) == 4  # parou ao atingir o limite de falhas consecutivas
    assert len(aborted) == 6
    assert all(r.status is ActionStatus.SKIPPED and r.attempts == 0 for r in aborted)
    assert harness.provider.calls.count("suspend:usr_S5") == 0  # as restantes nem foram tentadas
    assert set(statuses.values()) == {ActionStatus.FAILED, ActionStatus.SKIPPED}


async def test_um_sucesso_zera_a_contagem_de_falhas_consecutivas() -> None:
    harness = Harness(batch_size=1, max_consecutive_failures=3)
    for n in range(1, 9):
        harness.resolver.users[f"S{n}"] = f"usr_S{n}"
    harness.provider.transient.update({"usr_S1", "usr_S2", "usr_S4", "usr_S5"})  # 2 falhas, ok, 2

    report, _ = await harness.run([suspend(f"S{n}") for n in range(1, 9)])

    assert not report.aborted
    assert harness.results["SUSPEND_USER:S8"].status is ActionStatus.SUCCEEDED


async def test_falhas_permanentes_nao_acionam_o_circuit_breaker() -> None:
    harness = Harness(batch_size=1, max_consecutive_failures=2)
    for n in range(1, 6):
        harness.resolver.users[f"S{n}"] = f"usr_S{n}"
        harness.provider.permanent.add(f"usr_S{n}")

    report, _ = await harness.run([suspend(f"S{n}") for n in range(1, 6)])

    assert not report.aborted  # são problemas dos dados, não do provedor
    assert len(harness.results) == 5


@pytest.mark.parametrize("batch_size", [1, 2, 50])
async def test_o_resultado_nao_depende_do_tamanho_do_lote(batch_size: int) -> None:
    harness = Harness(batch_size=batch_size)
    harness.provider.permanent.add("usr_S3")
    harness.resolver.users.update({f"S{n}": f"usr_S{n}" for n in range(1, 6)})

    await harness.run([suspend(f"S{n}") for n in range(1, 6)])

    statuses = {k: r.status for k, r in harness.results.items()}
    assert statuses["SUSPEND_USER:S3"] is ActionStatus.FAILED
    assert sum(s is ActionStatus.SUCCEEDED for s in statuses.values()) == 4
