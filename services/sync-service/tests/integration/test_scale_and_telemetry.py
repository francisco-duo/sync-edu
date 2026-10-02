"""Métricas de requisições/retries, limite de taxa do cliente e ausência de consultas N+1."""

import time
from collections import Counter

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from sync_service.sync.status import RunStatus
from sync_testkit.transports import Rule, post_users
from sync_testkit.world import World

pytestmark = pytest.mark.integration

STUDENTS = {"S1": "João Silva", "S2": "João Silva", "S3": "Maria Souza"}
ENROLLMENTS = [("S1", "A"), ("S2", "A"), ("S3", "B")]


def configure_provider(world: World, **chaos: object) -> None:
    settings = {
        "error_rate": 0.0,
        "lose_response_rate": 0.0,
        "retry_after_seconds": 0,
        "seed": None,
        "fail_next": [],
        "lose_next": 0,
        **chaos,
    }
    world.provedor_app.state.chaos.configure(**settings)


async def test_metricas_de_requisicoes_ficam_registradas_no_run(world: World) -> None:
    await world.set_academico(STUDENTS, ["A", "B"], ENROLLMENTS)

    first = await world.sync()
    second = await world.sync()

    # 1ª: snapshot do provedor (users + classes) + 8 escritas; acadêmico: 3 listagens.
    assert first.metrics["http_calls"] == {"provedor": 2 + 8, "academico": 3}
    assert first.metrics["retries"] == {}
    # 2ª: 0 ações, só leituras (users + classes + membros de cada uma das 2 turmas).
    assert second.total_actions == 0
    assert second.metrics["http_calls"] == {"provedor": 2 + 2, "academico": 3}


async def test_retries_sao_contados(world: World) -> None:
    await world.set_academico({"S1": "João Silva"}, ["A"], [("S1", "A")])
    world.provedor_net.add(Rule(match=post_users("S1"), lose_response=True, times=1))

    run = await world.sync()

    assert run.status == RunStatus.SUCCEEDED
    assert run.metrics["retries"] == {"provedor": 1}
    assert run.metrics["http_calls"]["provedor"] == 2 + 3 + 1  # snapshot + 3 ações + 1 reenvio


async def test_429_do_provedor_e_respeitado_e_repetido(world: World) -> None:
    await world.set_academico(STUDENTS, ["A", "B"], ENROLLMENTS)
    configure_provider(world, fail_next=[429, 429])  # as duas primeiras requisições

    run = await world.sync()

    assert run.status == RunStatus.SUCCEEDED
    assert run.metrics["retries"] == {"provedor": 2}
    assert run.successful_actions == 8


async def test_sem_limitador_o_cliente_estoura_a_cota_e_com_limitador_nao(
    engine: AsyncEngine,
) -> None:
    students = {f"S{n:02d}": "João Silva" for n in range(1, 21)}
    enrollments = [(sid, "A") for sid in students]  # 2 leituras + 1 turma + 20 + 20 = 43 chamadas

    unrestrained = World(engine)
    try:
        await unrestrained.set_academico(students, ["A"], enrollments)
        configure_provider(unrestrained, rate_limit_rps=40)
        await unrestrained.sync()
        assert "429" in (await unrestrained.provider_status_counts())
    finally:
        await unrestrained.aclose()

    polite = World(engine, max_rps=30)  # abaixo da cota de 40 req/s do provedor
    try:
        await polite.set_academico(students, ["A"], enrollments)
        configure_provider(polite, rate_limit_rps=40)
        started = time.perf_counter()
        run = await polite.sync()
        elapsed = time.perf_counter() - started
        assert run.status == RunStatus.SUCCEEDED
        assert "429" not in await polite.provider_status_counts()
        assert run.metrics["retries"] == {}
        assert elapsed >= (43 - 1) / 30 * 0.9  # o ritmo foi realmente limitado
    finally:
        await polite.aclose()


# --- N+1 ----------------------------------------------------------------------------------


class QueryLog:
    def __init__(self, engine: AsyncEngine) -> None:
        self.statements: list[str] = []
        self._engine = engine.sync_engine
        event.listen(self._engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        self.statements.append(" ".join(statement.split()))

    def reset(self) -> None:
        self.statements.clear()

    def close(self) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    @property
    def total(self) -> int:
        return len(self.statements)

    def most_repeated_select(self) -> int:
        selects = Counter(s for s in self.statements if s.startswith("SELECT"))
        return max(selects.values(), default=0)


async def sync_and_count(engine: AsyncEngine, students: int) -> tuple[int, int, int]:
    """Devolve (consultas na 1ª sync, consultas na 2ª sync, SELECT mais repetido)."""
    world = World(engine, batch_size=50)
    log = QueryLog(engine)
    try:
        names = {f"S{n:04d}": "João Silva" for n in range(1, students + 1)}
        classes = [f"C{n}" for n in range(5)]
        enrollments = [(sid, classes[i % 5]) for i, sid in enumerate(names)]
        await world.set_academico(names, classes, enrollments)

        log.reset()
        first = await world.sync()
        assert first.successful_actions == students * 2 + 5
        first_total, repeated = log.total, log.most_repeated_select()

        log.reset()
        assert (await world.sync()).total_actions == 0
        return first_total, log.total, repeated
    finally:
        log.close()
        await world.aclose()


async def test_numero_de_consultas_nao_cresce_com_o_numero_de_alunos(engine: AsyncEngine) -> None:
    small_first, small_second, small_repeated = await sync_and_count(engine, students=20)
    large_first, large_second, large_repeated = await sync_and_count(engine, students=400)

    # 400 alunos = 805 ações. Com N+1 seriam ~800+ consultas; aqui só crescem os lotes (50).
    assert large_first < 805 / 5
    assert large_second == small_second  # sem ações: custo constante, independente do tamanho
    assert large_repeated <= small_repeated + 2  # nenhum SELECT repetido "uma vez por aluno"
    assert large_first - small_first < 40
