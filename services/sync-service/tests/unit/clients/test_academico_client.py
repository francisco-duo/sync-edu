import httpx
import pytest
import respx

from sync_service.clients.academico import AcademicoClient
from sync_service.domain.models import Enrollment, InvalidDesiredStateError
from sync_service.resilience.errors import PermanentUpstreamError
from sync_service.resilience.retry import RetriesExhaustedError, RetryPolicy

BASE = "http://academico"
POLICY = RetryPolicy(max_attempts=3, base_delay=0.0, jitter="none")


async def no_sleep(_: float) -> None:
    return None


def page(items: list[dict], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"items": items, "total": len(items) if total is None else total}
    )


def student(n: int) -> dict:
    return {"id": f"S{n}", "first_name": "João", "last_name": "Silva", "status": "active"}


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


async def client_for(page_size: int = 500) -> tuple[httpx.AsyncClient, AcademicoClient]:
    http = httpx.AsyncClient(base_url=BASE, verify=False)
    return http, AcademicoClient(
        http, retry_policy=POLICY, concurrency=2, page_size=page_size, sleep=no_sleep
    )


async def test_monta_o_estado_desejado(api: respx.MockRouter) -> None:
    api.get("/students").mock(return_value=page([student(1), student(2)]))
    api.get("/classes").mock(return_value=page([{"id": "A", "name": "Turma A", "year": 1}]))
    api.get("/enrollments").mock(
        return_value=page([{"student_id": "S1", "class_id": "A", "status": "active"}])
    )
    http, client = await client_for()

    async with http:
        desired = await client.fetch_desired_state()

    assert sorted(desired.students) == ["S1", "S2"]
    assert desired.students["S1"].first_name == "João"
    assert list(desired.classes) == ["A"]
    assert desired.enrollments == {Enrollment("S1", "A")}


async def test_pede_so_alunos_e_matriculas_ativos(api: respx.MockRouter) -> None:
    students = api.get("/students").mock(return_value=page([]))
    api.get("/classes").mock(return_value=page([]))
    enrollments = api.get("/enrollments").mock(return_value=page([]))
    http, client = await client_for()

    async with http:
        await client.fetch_desired_state()

    assert students.calls.last.request.url.params["status"] == "active"
    assert enrollments.calls.last.request.url.params["status"] == "active"


async def test_le_todas_as_paginas_na_ordem(api: respx.MockRouter) -> None:
    everyone = [student(n) for n in range(1, 6)]

    def students_page(request: httpx.Request) -> httpx.Response:
        number, size = int(request.url.params["page"]), int(request.url.params["page_size"])
        return page(everyone[(number - 1) * size : number * size], total=len(everyone))

    route = api.get("/students").mock(side_effect=students_page)
    api.get("/classes").mock(return_value=page([]))
    api.get("/enrollments").mock(return_value=page([]))
    http, client = await client_for(page_size=2)

    async with http:
        desired = await client.fetch_desired_state()

    assert list(desired.students) == ["S1", "S2", "S3", "S4", "S5"]
    assert route.call_count == 3  # 5 alunos, 2 por página


async def test_pagina_com_falha_transitoria_e_repetida(api: respx.MockRouter) -> None:
    flaky = api.get("/students").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"code": "RATE_LIMITED", "message": "x"}}),
            httpx.Response(500, json={"error": {"code": "INJECTED_FAILURE", "message": "x"}}),
            page([student(1)]),
        ]
    )
    api.get("/classes").mock(return_value=page([]))
    api.get("/enrollments").mock(return_value=page([]))
    http, client = await client_for()

    async with http:
        desired = await client.fetch_desired_state()

    assert list(desired.students) == ["S1"]
    assert flaky.call_count == 3


async def test_falha_transitoria_persistente_esgota_as_tentativas(api: respx.MockRouter) -> None:
    route = api.get("/students").mock(return_value=httpx.Response(503, json={}))
    api.get("/classes").mock(return_value=page([]))
    api.get("/enrollments").mock(return_value=page([]))
    http, client = await client_for()

    async with http:
        with pytest.raises(RetriesExhaustedError):
            await client.fetch_desired_state()

    assert route.call_count == POLICY.max_attempts


async def test_erro_definitivo_nao_e_repetido(api: respx.MockRouter) -> None:
    route = api.get("/students").mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "UNAUTHENTICATED", "message": "x"}}
        )
    )
    api.get("/classes").mock(return_value=page([]))
    api.get("/enrollments").mock(return_value=page([]))
    http, client = await client_for()

    async with http:
        with pytest.raises(PermanentUpstreamError):
            await client.fetch_desired_state()

    assert route.call_count == 1


async def test_matricula_de_aluno_que_nao_esta_no_estado_e_invalida(
    api: respx.MockRouter,
) -> None:
    api.get("/students").mock(return_value=page([]))
    api.get("/classes").mock(return_value=page([{"id": "A", "name": "A", "year": 1}]))
    api.get("/enrollments").mock(
        return_value=page([{"student_id": "S9", "class_id": "A", "status": "active"}])
    )
    http, client = await client_for()

    async with http:
        with pytest.raises(InvalidDesiredStateError):
            await client.fetch_desired_state()


async def test_resposta_fora_do_contrato_e_erro_permanente(api: respx.MockRouter) -> None:
    api.get("/students").mock(
        return_value=httpx.Response(200, json={"items": [{"id": 1}], "total": 1})
    )
    api.get("/classes").mock(return_value=page([]))
    api.get("/enrollments").mock(return_value=page([]))
    http, client = await client_for()

    async with http:
        with pytest.raises(PermanentUpstreamError) as caught:
            await client.fetch_desired_state()

    assert caught.value.code == "INVALID_RESPONSE"
