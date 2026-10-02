"""API HTTP do sync-service contra PostgreSQL real e os mocks em processo."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sync_service.config import Settings
from sync_service.main import create_app
from sync_testkit.transports import Rule, any_request, error_response, post_users
from sync_testkit.world import World

pytestmark = pytest.mark.integration

STUDENTS = {"S1": "João Silva", "S2": "João Silva", "S3": "Maria Souza"}
ENROLLMENTS = [("S1", "A"), ("S2", "A"), ("S3", "B")]


class Api:
    def __init__(self, client: httpx.AsyncClient, world: World, app) -> None:  # noqa: ANN001
        self.http = client
        self.world = world
        self.runner = app.state.runner

    async def wait(self) -> None:
        await self.runner.wait_idle()

    async def post_run(self, **params: object) -> httpx.Response:
        return await self.http.post("/sync-runs", params=params)


@pytest.fixture
async def api(engine: AsyncEngine, migrated_database: str) -> AsyncIterator[Api]:
    # `World` só fornece os mocks em processo e as transportes com falhas; o app é o real.
    world = World(engine)
    await world.set_academico(STUDENTS, ["A", "B"], ENROLLMENTS)
    settings = Settings(
        database_url=migrated_database,
        retry_max_attempts=3,
        retry_base_delay_seconds=0.0,
        retry_jitter="none",
    )
    app = create_app(
        settings,
        academico_transport=world.academico_net,
        provedor_transport=world.provedor_net,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sync") as client,
    ):
        yield Api(client, world, app)
    await world.aclose()


async def test_dry_run_devolve_200_com_o_plano_e_nao_executa(api: Api) -> None:
    response = await api.post_run(dry_run="true")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["dry_run"], body["total_actions"]) == ("planned", True, 8)
    assert [a["action_key"] for a in body["actions"]][:3] == [
        "CREATE_CLASS:A",
        "CREATE_CLASS:B",
        "CREATE_USER:S1",
    ]
    assert {a["status"] for a in body["actions"]} == {"planned"}
    assert body["actions"][0]["payload"] == {"class_source_id": "A", "name": "Turma A"}
    assert await api.world.provider_writes() == 0


async def test_execucao_real_devolve_202_e_conclui_em_segundo_plano(api: Api) -> None:
    response = await api.post_run()

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert response.headers["Location"] == f"/sync-runs/{body['id']}"

    await api.wait()
    run = (await api.http.get(response.headers["Location"])).json()
    assert (run["status"], run["total_actions"], run["successful_actions"]) == ("succeeded", 8, 8)
    assert run["finished_at"] is not None


async def test_segunda_execucao_nao_tem_acoes(api: Api) -> None:
    await api.post_run()
    await api.wait()

    second = await api.post_run()
    await api.wait()

    run = (await api.http.get(f"/sync-runs/{second.json()['id']}")).json()
    assert (run["status"], run["total_actions"]) == ("succeeded", 0)


async def test_consulta_de_execucoes_e_de_acoes_com_filtros_e_paginacao(api: Api) -> None:
    await api.post_run(dry_run="true")
    created = await api.post_run()
    await api.wait()
    run_id = created.json()["id"]

    runs = (await api.http.get("/sync-runs")).json()
    real_only = (await api.http.get("/sync-runs", params={"dry_run": "false"})).json()
    page = (await api.http.get(f"/sync-runs/{run_id}/actions", params={"page_size": 3})).json()
    users = (
        await api.http.get(f"/sync-runs/{run_id}/actions", params={"action_type": "CREATE_USER"})
    ).json()
    failed = (
        await api.http.get(f"/sync-runs/{run_id}/actions", params={"status": "failed"})
    ).json()

    assert runs["total"] == 2
    assert runs["items"][0]["id"] == run_id  # mais recente primeiro
    assert [r["id"] for r in real_only["items"]] == [run_id]
    assert (page["total"], len(page["items"]), page["page_size"]) == (8, 3, 3)
    assert [a["seq"] for a in page["items"]] == [0, 1, 2]
    assert users["total"] == 3
    assert failed["total"] == 0


async def test_nova_execucao_com_outra_em_andamento_devolve_409(api: Api) -> None:
    running = await api.runner.create_run(dry_run=False)

    response = await api.post_run()

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "RUN_IN_PROGRESS"
    assert error["details"]["run_id"] == str(running)


async def test_retry_failed_reprocessa_so_as_falhas_e_depois_vira_no_op(api: Api) -> None:
    rejection = api.world.provedor_net.add(
        Rule(match=post_users("S2"), respond=error_response(400, "VALIDATION_ERROR"))
    )
    created = await api.post_run()
    await api.wait()
    run_id = created.json()["id"]
    run = (await api.http.get(f"/sync-runs/{run_id}")).json()
    assert (run["status"], run["failed_actions"], run["skipped_actions"]) == ("partial", 1, 1)
    failed = (
        await api.http.get(f"/sync-runs/{run_id}/actions", params={"status": "failed"})
    ).json()
    assert failed["items"][0]["error_code"] == "VALIDATION_ERROR"
    assert failed["items"][0]["attempts"] == 1
    assert failed["items"][0]["last_attempt_at"] is not None

    # 1º retry: o problema continua, então falha de novo (e as tentativas acumulam)
    again = await api.http.post(f"/sync-runs/{run_id}/retry-failed")
    assert again.status_code == 202
    await api.wait()
    failed = (
        await api.http.get(f"/sync-runs/{run_id}/actions", params={"status": "failed"})
    ).json()
    assert failed["items"][0]["attempts"] == 2

    # 2º retry: o problema foi corrigido
    rejection.enabled = False
    fixed = await api.http.post(f"/sync-runs/{run_id}/retry-failed")
    assert fixed.status_code == 202
    await api.wait()
    run = (await api.http.get(f"/sync-runs/{run_id}")).json()
    assert (run["status"], run["successful_actions"], run["retry_count"]) == ("succeeded", 8, 2)

    # 3º retry: nada elegível -> 200, sem fazer nada
    noop = await api.http.post(f"/sync-runs/{run_id}/retry-failed")
    assert noop.status_code == 200
    assert noop.json()["retry_count"] == 2


async def test_retry_failed_de_run_inexistente_dry_run_ou_em_andamento(api: Api) -> None:
    dry = (await api.post_run(dry_run="true")).json()
    running = await api.runner.create_run(dry_run=False)

    unknown = await api.http.post(f"/sync-runs/{uuid.uuid4()}/retry-failed")
    dry_run = await api.http.post(f"/sync-runs/{dry['id']}/retry-failed")
    busy = await api.http.post(f"/sync-runs/{running}/retry-failed")

    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "RUN_NOT_FOUND")
    assert (dry_run.status_code, dry_run.json()["error"]["code"]) == (409, "RUN_IS_DRY_RUN")
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "RUN_IN_PROGRESS")


async def test_dry_run_com_o_provedor_fora_do_ar_devolve_502_e_registra_o_run(api: Api) -> None:
    api.world.provedor_net.add(Rule(match=any_request, raises=httpx.ConnectError("fora do ar")))

    response = await api.post_run(dry_run="true")

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_UNAVAILABLE"
    run = (await api.http.get(f"/sync-runs/{error['details']['run_id']}")).json()
    assert (run["status"], run["total_actions"]) == ("failed", 0)


async def test_erros_de_consulta_usam_o_envelope_padrao(api: Api) -> None:
    missing = await api.http.get(f"/sync-runs/{uuid.uuid4()}")
    bad_id = await api.http.get("/sync-runs/nao-e-uuid")
    bad_page = await api.http.get("/sync-runs", params={"page_size": 0})
    actions_of_missing = await api.http.get(f"/sync-runs/{uuid.uuid4()}/actions")

    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "RUN_NOT_FOUND")
    assert (bad_id.status_code, bad_id.json()["error"]["code"]) == (422, "VALIDATION_ERROR")
    assert (bad_page.status_code, bad_page.json()["error"]["code"]) == (422, "VALIDATION_ERROR")
    assert actions_of_missing.status_code == 404


async def test_health_com_banco_real(api: Api) -> None:
    response = await api.http.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}
