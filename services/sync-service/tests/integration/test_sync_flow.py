"""Sincronização ponta a ponta: PostgreSQL real + mock-academico + mock-provedor em processo."""

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mock_provedor.store import Store
from sync_service.sync import store
from sync_service.sync.status import ActionStatus, ErrorCode, RunStatus
from sync_testkit.flaky import FlakyProvider
from sync_testkit.transports import Rule, any_request, any_write, post_users
from sync_testkit.world import World

pytestmark = pytest.mark.integration

BASE_STUDENTS = {"S1": "João Silva", "S2": "João Silva", "S3": "Maria Souza"}
BASE_ENROLLMENTS = [("S1", "A"), ("S2", "A"), ("S3", "B")]
BASE_EMAILS = {
    "S1": ("joao.silva@example.edu", "active"),
    "S2": ("joao.silva2@example.edu", "active"),
    "S3": ("maria.souza@example.edu", "active"),
}


async def seed_base(world: World) -> None:
    await world.set_academico(BASE_STUDENTS, ["A", "B"], BASE_ENROLLMENTS)


async def keys_in_order(world: World, run_id) -> list[str]:  # noqa: ANN001
    actions = await world.actions(run_id)
    return [a.action_key for a in sorted(actions.values(), key=lambda a: a.seq)]


# --- idempotência -------------------------------------------------------------------------


async def test_primeira_sync_aplica_as_acoes_e_a_segunda_gera_zero(world: World) -> None:
    await seed_base(world)

    first = await world.sync()

    assert first.status == RunStatus.SUCCEEDED
    assert (first.total_actions, first.successful_actions) == (8, 8)
    assert first.counters == {"CREATE_CLASS": 2, "CREATE_USER": 3, "ADD_TO_CLASS": 3}
    assert world.provider_users() == BASE_EMAILS
    assert world.provider_members() == {"A": {"S1", "S2"}, "B": {"S3"}}
    writes_after_first = await world.provider_writes()
    assert writes_after_first == 8

    second = await world.sync()

    assert second.status == RunStatus.SUCCEEDED
    assert second.total_actions == 0  # <- a segunda execução não faz nada
    assert await world.provider_writes() == writes_after_first  # nem uma chamada de escrita


async def test_run_registra_inicio_fim_e_metricas(world: World) -> None:
    await seed_base(world)

    run = await world.sync()

    assert run.started_at <= run.finished_at
    assert not run.dry_run
    assert set(run.metrics) >= {"fetch_ms", "reconcile_ms", "execute_ms"}
    assert run.error is None
    assert run.retry_count == 0


async def test_mudancas_no_academico_sao_refletidas_e_convergem(world: World) -> None:
    await seed_base(world)
    await world.sync()
    # S1 muda de turma, S3 sai, S4 entra numa turma nova.
    await world.set_academico(
        {**BASE_STUDENTS, "S4": "Ana Lima"},
        ["A", "B", "C"],
        [("S1", "B"), ("S2", "A"), ("S4", "C")],
        inactive=["S3"],
    )

    run = await world.sync()

    assert await keys_in_order(world, run.id) == [
        "CREATE_CLASS:C",
        "CREATE_USER:S4",
        "REMOVE_FROM_CLASS:S1|A",
        "REMOVE_FROM_CLASS:S3|B",
        "ADD_TO_CLASS:S1|B",
        "ADD_TO_CLASS:S4|C",
        "SUSPEND_USER:S3",
    ]
    assert run.status == RunStatus.SUCCEEDED
    assert world.provider_members() == {"A": {"S2"}, "B": {"S1"}, "C": {"S4"}}
    users = world.provider_users()
    assert users["S3"] == ("maria.souza@example.edu", "suspended")  # suspenso, NUNCA apagado
    assert users["S4"] == ("ana.lima@example.edu", "active")
    assert (await world.sync()).total_actions == 0


async def test_aluno_suspenso_que_volta_e_reativado(world: World) -> None:
    await seed_base(world)
    await world.sync()
    await world.set_academico(BASE_STUDENTS, ["A", "B"], BASE_ENROLLMENTS[:2], inactive=["S3"])
    await world.sync()
    assert world.provider_users()["S3"][1] == "suspended"

    await world.set_academico(BASE_STUDENTS, ["A", "B"], BASE_ENROLLMENTS)
    run = await world.sync()

    assert await keys_in_order(world, run.id) == ["REACTIVATE_USER:S3", "ADD_TO_CLASS:S3|B"]
    assert world.provider_users() == BASE_EMAILS
    assert (await world.sync()).total_actions == 0


async def test_usuario_orfao_do_provedor_nunca_e_tocado_mas_reserva_o_email(
    world: World,
) -> None:
    world.provider.add_user("", "joao.silva@example.edu", "Orfão", "Manual")
    world.provider.add_class("", "Turma criada à mão")
    await world.set_academico({"S1": "João Silva"}, ["A"], [("S1", "A")])

    run = await world.sync()

    assert world.provider_users()["S1"][0] == "joao.silva2@example.edu"
    assert run.counters == {"CREATE_CLASS": 1, "CREATE_USER": 1, "ADD_TO_CLASS": 1}
    orphan = next(u for u in world.provider.users.values() if u.external_id == "")
    assert orphan.status == "active"
    assert (await world.sync()).total_actions == 0


async def test_mapeamentos_sao_gravados_e_reparados_a_partir_do_snapshot(
    world: World, engine: AsyncEngine
) -> None:
    await seed_base(world)
    await world.sync()
    saved = await world.mappings()
    assert {(t, s) for t, s, _ in saved} == {
        ("user", "S1"),
        ("user", "S2"),
        ("user", "S3"),
        ("class", "A"),
        ("class", "B"),
    }

    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM id_mappings"))
    second = await world.sync()

    assert second.total_actions == 0
    assert await world.mappings() == saved  # o snapshot do provedor reconstruiu o índice


async def test_provedor_recriado_com_ids_reaproveitados_nao_quebra_os_mapeamentos(
    world: World,
) -> None:
    await seed_base(world)
    await world.sync()
    # O provedor "renasce" vazio e reaproveita ids (o contador recomeça). Um usuário órfão criado
    # antes desloca os ids, então os vínculos antigos colidiriam com os novos se sobrassem.
    fresh = Store()
    fresh.add_user("", "orfao@example.edu", "Orfão", "Manual")
    world.provedor_app.state.store = fresh

    run = await world.sync()

    assert run.status == RunStatus.SUCCEEDED
    assert run.total_actions == 8
    users = world.provider_users()
    users.pop("")  # o órfão
    assert users == BASE_EMAILS
    live_ids = {u.id for u in world.provider.users.values()} | set(world.provider.classes)
    assert {provider_id for _, _, provider_id in await world.mappings()} <= live_ids
    assert (await world.sync()).total_actions == 0


# --- dry-run ------------------------------------------------------------------------------


async def test_dry_run_calcula_e_registra_o_plano_sem_executar_nada(world: World) -> None:
    await seed_base(world)

    run = await world.sync(dry_run=True)

    assert run.status == RunStatus.PLANNED
    assert run.dry_run
    assert run.total_actions == 8
    assert run.successful_actions == 0
    actions = await world.actions(run.id)
    assert {a.status for a in actions.values()} == {ActionStatus.PLANNED}
    assert await world.provider_writes() == 0  # NENHUMA escrita no provedor
    assert world.provider.users == {}
    assert await world.mappings() == set()

    real = await world.sync()  # o dry-run não "consome" nada: a execução real faz tudo
    assert (real.total_actions, real.status) == (8, RunStatus.SUCCEEDED)


async def test_dry_run_nao_disputa_o_lock_com_um_run_real(world: World) -> None:
    await seed_base(world)
    running = await world.runner.create_run(dry_run=False)

    dry = await world.sync(dry_run=True)

    assert dry.status == RunStatus.PLANNED
    with pytest.raises(store.RunInProgressError) as caught:
        await world.runner.create_run(dry_run=False)
    assert caught.value.run_id == running


# --- falha parcial e reprocessamento ------------------------------------------------------


@pytest.fixture
async def flaky_world(engine: AsyncEngine):  # noqa: ANN201
    world = World(engine, wrap_provider=lambda inner: FlakyProvider(inner, {"S2"}))
    yield world
    await world.aclose()


async def test_falha_parcial_nao_aborta_e_fica_registrada(flaky_world: World) -> None:
    world = flaky_world
    await seed_base(world)

    run = await world.sync()

    assert run.status == RunStatus.PARTIAL
    assert (run.successful_actions, run.failed_actions, run.skipped_actions) == (6, 1, 1)
    actions = await world.actions(run.id)
    failed = actions["CREATE_USER:S2"]
    assert failed.status == ActionStatus.FAILED
    assert failed.attempts == 1  # erro permanente: sem retry
    assert failed.error_code == "VALIDATION_ERROR"
    assert "inválidos" in failed.last_error
    assert failed.last_attempt_at is not None
    dependent = actions["ADD_TO_CLASS:S2|A"]
    assert dependent.status == ActionStatus.SKIPPED
    assert dependent.error_code == ErrorCode.DEPENDENCY_FAILED
    assert world.provider_members() == {"A": {"S1"}, "B": {"S3"}}  # o resto foi executado


async def test_retry_failed_reprocessa_somente_o_que_nao_terminou(flaky_world: World) -> None:
    world = flaky_world
    await seed_base(world)
    run = await world.sync()
    writes_before = await world.provider_writes()
    before = await world.actions(run.id)
    world.adapter.fail_users.clear()  # o problema foi corrigido

    retried = await world.retry(run.id)

    assert retried.status == RunStatus.SUCCEEDED
    assert (retried.retry_count, retried.last_retry_at is not None) == (1, True)
    assert (retried.successful_actions, retried.failed_actions, retried.skipped_actions) == (
        8,
        0,
        0,
    )
    assert await world.provider_writes() - writes_before == 2  # só CREATE S2 e ADD S2
    after = await world.actions(run.id)
    for key, action in before.items():
        if action.status == ActionStatus.SUCCEEDED:
            assert after[key].attempts == action.attempts  # as bem-sucedidas nem foram tocadas
    assert after["CREATE_USER:S2"].attempts == 2  # 1 da execução original + 1 do retry
    assert after["ADD_TO_CLASS:S2|A"].status == ActionStatus.SUCCEEDED
    assert world.provider_users() == BASE_EMAILS
    assert (await world.sync()).total_actions == 0


async def test_retry_failed_usa_os_ids_gravados_sem_buscar_o_estado_de_novo(
    flaky_world: World,
) -> None:
    world = flaky_world
    await seed_base(world)
    run = await world.sync()
    world.adapter.fail_users.clear()
    snapshots = await world.provider_stats()

    await world.retry(run.id)

    after = await world.provider_stats()
    reads = {
        route: n - snapshots.get(route, 0) for route, n in after.items() if route.startswith("GET")
    }
    assert not any(reads.values())  # nenhuma leitura: o plano original foi reaproveitado


async def test_retry_failed_sem_nada_elegivel_e_no_op(world: World) -> None:
    await seed_base(world)
    run = await world.sync()

    assert not await world.runner.begin_retry(run.id)

    assert (await world.run(run.id)).retry_count == 0


async def test_retry_failed_recusa_dry_run_run_inexistente_e_run_em_execucao(world: World) -> None:
    await seed_base(world)
    dry = await world.sync(dry_run=True)
    with pytest.raises(store.RunIsDryRunError):
        await world.runner.begin_retry(dry.id)

    import uuid

    with pytest.raises(store.RunNotFoundError):
        await world.runner.begin_retry(uuid.uuid4())

    running = await world.runner.create_run(dry_run=False)
    with pytest.raises(store.RunInProgressError):
        await world.runner.begin_retry(running)


# --- resposta perdida (a ação foi aplicada, o cliente acha que falhou) ---------------------


async def test_resposta_perdida_e_reenvio_nao_duplicam_nada(world: World) -> None:
    await world.set_academico({"S1": "João Silva"}, ["A"], [("S1", "A")])
    world.provedor_net.add(Rule(match=post_users("S1"), lose_response=True, times=1))

    run = await world.sync()

    assert run.status == RunStatus.SUCCEEDED
    assert len(world.provider.users) == 1  # o provedor aplicou UMA vez
    action = (await world.actions(run.id))["CREATE_USER:S1"]
    assert action.attempts == 2  # a 1ª "falhou" (resposta perdida) e a 2ª reconheceu o 409
    assert action.outcome == "already_applied"
    routes = await world.provider_stats()
    assert routes["POST /users"] == 2  # foi enviado duas vezes, criado uma só
    provider_id = next(iter(world.provider.users))
    assert ("user", "S1", provider_id) in await world.mappings()  # o id existente foi adotado
    assert world.provider_members() == {"A": {"S1"}}
    assert (await world.sync()).total_actions == 0


# --- provedor fora do ar ------------------------------------------------------------------


async def test_provedor_cai_no_meio_aborta_e_retry_failed_conclui(engine: AsyncEngine) -> None:
    world = World(engine, batch_size=2, max_consecutive_failures=3)
    try:
        students = {f"S{n:02d}": "João Silva" for n in range(1, 11)}
        await world.set_academico(students, ["A"], [(sid, "A") for sid in students])
        outage = world.provedor_net.add(
            Rule(match=any_write, raises=httpx.ConnectError("provedor fora do ar"))
        )

        run = await world.sync()

        assert run.status == RunStatus.ABORTED
        assert run.total_actions == 21  # 1 turma + 10 usuários + 10 matrículas
        assert run.failed_actions == 3  # parou ao atingir 3 falhas seguidas
        assert run.skipped_actions == 18
        actions = await world.actions(run.id)
        assert {a.error_code for a in actions.values() if a.status == ActionStatus.SKIPPED} == {
            ErrorCode.RUN_ABORTED
        }
        assert outage.hits == 9  # 3 ações x 3 tentativas; as demais nem foram tentadas
        assert world.provider.users == {}

        outage.enabled = False  # o provedor voltou
        retried = await world.retry(run.id)

        assert retried.status == RunStatus.SUCCEEDED
        assert retried.successful_actions == 21
        assert len(world.provider_users()) == 10
        assert world.provider_members() == {"A": set(students)}
        assert (await world.sync()).total_actions == 0
    finally:
        await world.aclose()


async def test_falhas_permanentes_seguidas_nao_acionam_o_circuit_breaker(
    engine: AsyncEngine,
) -> None:
    world = World(
        engine,
        batch_size=1,
        max_consecutive_failures=2,
        wrap_provider=lambda inner: FlakyProvider(inner, {"S1", "S2", "S3", "S4"}),
    )
    try:
        students = {f"S{n}": "João Silva" for n in range(1, 5)}
        await world.set_academico(students, ["A"], [(sid, "A") for sid in students])

        run = await world.sync()

        assert run.status == RunStatus.PARTIAL  # e não ABORTED: o problema são os dados
        assert run.failed_actions == 4
    finally:
        await world.aclose()


async def test_provedor_fora_do_ar_antes_de_comecar_falha_o_run_sem_executar_nada(
    world: World,
) -> None:
    await seed_base(world)
    world.provedor_net.add(Rule(match=any_request, raises=httpx.ConnectError("fora do ar")))

    run = await world.sync()

    assert run.status == RunStatus.FAILED
    assert run.error is not None
    assert "tentativas esgotadas" in run.error
    assert run.total_actions == 0
    assert await world.provider_writes() == 0


async def test_academico_fora_do_ar_falha_o_run(world: World) -> None:
    await seed_base(world)
    world.academico_net.add(Rule(match=any_request, raises=httpx.ConnectError("fora do ar")))

    run = await world.sync()

    assert run.status == RunStatus.FAILED
    assert run.total_actions == 0


async def test_estado_desejado_inconsistente_falha_o_run_com_mensagem_clara(world: World) -> None:
    await world.set_academico(
        {"S1": "João Silva"}, [], [("S1", "A")]
    )  # matrícula em turma que não existe

    run = await world.sync()

    assert run.status == RunStatus.FAILED
    assert "turma inexistente" in run.error
    assert await world.provider_writes() == 0


# --- processo interrompido ----------------------------------------------------------------


async def test_run_em_execucao_quando_o_processo_cai_vira_interrupted(world: World) -> None:
    run_id = await world.runner.create_run(dry_run=False)  # fica "running": o processo "caiu"

    assert await store.mark_interrupted_runs(world.sessions) == 1

    run = await world.run(run_id)
    assert run.status == RunStatus.INTERRUPTED
    assert run.finished_at is not None
    await world.runner.create_run(dry_run=False)  # o lock foi liberado
