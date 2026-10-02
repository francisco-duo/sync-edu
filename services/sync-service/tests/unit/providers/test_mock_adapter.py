"""MockProvedorAdapter contra respostas simuladas (respx): tradução HTTP -> contrato do motor."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from sync_service.providers.base import Outcome
from sync_service.providers.mock_adapter import MockProvedorAdapter
from sync_service.resilience.errors import PermanentUpstreamError, TransientUpstreamError
from sync_service.resilience.retry import RetryPolicy

BASE = "http://provedor"
FAST = RetryPolicy(max_attempts=3, base_delay=0.0, jitter="none")


def error(code: str, status: int, **details: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": code.lower(), "details": details}}
    )


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def adapter() -> AsyncIterator[MockProvedorAdapter]:
    async with httpx.AsyncClient(base_url=BASE, verify=False) as client:
        yield MockProvedorAdapter(
            client, retry_policy=FAST, concurrency=2, page_size=2, sleep=no_sleep
        )


@pytest.fixture
def provider_api() -> respx.MockRouter:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


class TestCriacao:
    async def test_criar_usuario_201_e_aplicado(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        route = provider_api.post("/users").respond(201, json={"id": "usr_1"})

        result = await adapter.create_user(
            external_id="S1", email="joao.silva@example.edu", first_name="João", last_name="Silva"
        )

        assert (result.outcome, result.provider_id) == (Outcome.APPLIED, "usr_1")
        sent = json.loads(route.calls.last.request.content)
        assert sent == {
            "external_id": "S1",
            "email": "joao.silva@example.edu",
            "first_name": "João",
            "last_name": "Silva",
        }

    async def test_409_por_external_id_adota_o_usuario_existente(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.post("/users").mock(
            return_value=error(
                "USER_ALREADY_EXISTS", 409, conflict_field="external_id", existing_id="usr_9"
            )
        )

        result = await adapter.create_user(
            external_id="S1", email="a@example.edu", first_name="A", last_name="B"
        )

        assert (result.outcome, result.provider_id) == (Outcome.ALREADY_APPLIED, "usr_9")

    async def test_409_por_email_e_erro_permanente(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.post("/users").mock(
            return_value=error(
                "USER_ALREADY_EXISTS", 409, conflict_field="email", existing_id="usr_7"
            )
        )

        with pytest.raises(PermanentUpstreamError) as caught:
            await adapter.create_user(
                external_id="S1", email="a@example.edu", first_name="A", last_name="B"
            )

        assert caught.value.code == "EMAIL_CONFLICT"

    async def test_criar_turma_201_e_409(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.post("/classes").mock(
            side_effect=[
                httpx.Response(201, json={"id": "cls_1"}),
                error(
                    "CLASS_ALREADY_EXISTS", 409, conflict_field="external_id", existing_id="cls_1"
                ),
            ]
        )

        first = await adapter.create_class(external_id="A", name="Turma A")
        again = await adapter.create_class(external_id="A", name="Turma A")

        assert (first.outcome, first.provider_id) == (Outcome.APPLIED, "cls_1")
        assert (again.outcome, again.provider_id) == (Outcome.ALREADY_APPLIED, "cls_1")


class TestMembros:
    async def test_adicionar_membro_201_e_409(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.post("/classes/cls_1/members").mock(
            side_effect=[
                httpx.Response(201, json={"user_id": "usr_1"}),
                error("MEMBERSHIP_ALREADY_EXISTS", 409),
            ]
        )

        first = await adapter.add_member(provider_class_id="cls_1", provider_user_id="usr_1")
        again = await adapter.add_member(provider_class_id="cls_1", provider_user_id="usr_1")

        assert first.outcome is Outcome.APPLIED
        assert again.outcome is Outcome.ALREADY_APPLIED

    async def test_adicionar_usuario_suspenso_e_erro_permanente_sem_retry(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        route = provider_api.post("/classes/cls_1/members").mock(
            return_value=error("USER_SUSPENDED", 422)
        )

        with pytest.raises(PermanentUpstreamError) as caught:
            await adapter.add_member(provider_class_id="cls_1", provider_user_id="usr_1")

        assert caught.value.code == "USER_SUSPENDED"
        assert route.call_count == 1

    async def test_remover_membro_204_e_404_de_membro_inexistente(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.delete("/classes/cls_1/members/usr_1").mock(
            side_effect=[httpx.Response(204), error("MEMBERSHIP_NOT_FOUND", 404)]
        )

        first = await adapter.remove_member(provider_class_id="cls_1", provider_user_id="usr_1")
        again = await adapter.remove_member(provider_class_id="cls_1", provider_user_id="usr_1")

        assert first.outcome is Outcome.APPLIED
        assert again.outcome is Outcome.ALREADY_APPLIED  # 404 esperado: o objetivo já valia

    async def test_remover_de_turma_inexistente_e_erro_permanente(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.delete("/classes/cls_x/members/usr_1").mock(
            return_value=error("CLASS_NOT_FOUND", 404)
        )

        with pytest.raises(PermanentUpstreamError) as caught:
            await adapter.remove_member(provider_class_id="cls_x", provider_user_id="usr_1")

        assert caught.value.code == "CLASS_NOT_FOUND"


class TestSuspensao:
    async def test_suspender_e_reativar(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.post("/users/usr_1/suspend").respond(200, json={})
        provider_api.post("/users/usr_1/reactivate").respond(200, json={})

        assert (await adapter.suspend_user(provider_user_id="usr_1")).outcome is Outcome.APPLIED
        assert (await adapter.reactivate_user(provider_user_id="usr_1")).outcome is Outcome.APPLIED

    async def test_suspender_usuario_inexistente_e_erro_permanente(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.post("/users/usr_x/suspend").mock(return_value=error("USER_NOT_FOUND", 404))

        with pytest.raises(PermanentUpstreamError) as caught:
            await adapter.suspend_user(provider_user_id="usr_x")

        assert caught.value.code == "USER_NOT_FOUND"


class TestErrosDeTransporte:
    async def test_escrita_nao_faz_retry_sozinha(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        # Quem repete a ação inteira (e conta as tentativas) é o executor, não o adapter.
        route = provider_api.post("/users/usr_1/suspend").mock(return_value=error("X", 503))

        with pytest.raises(TransientUpstreamError):
            await adapter.suspend_user(provider_user_id="usr_1")

        assert route.call_count == 1

    async def test_timeout_vira_erro_transitorio(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.post("/users/usr_1/suspend").mock(side_effect=httpx.ReadTimeout("lento"))

        with pytest.raises(TransientUpstreamError):
            await adapter.suspend_user(provider_user_id="usr_1")


class TestSnapshot:
    async def test_le_todas_as_paginas_e_os_membros_de_cada_turma(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        users = [
            {"id": f"usr_{n}", "external_id": f"S{n}", "email": f"u{n}@x.edu", "status": "active"}
            for n in range(1, 4)
        ]
        users.append(
            {"id": "usr_9", "external_id": None, "email": "orfao@x.edu", "status": "active"}
        )

        def paged(items: list[dict]) -> httpx.Response:
            return httpx.Response(200, json={"items": items, "total": len(items)})

        def users_page(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            return httpx.Response(
                200, json={"items": users[(page - 1) * 2 : page * 2], "total": len(users)}
            )

        provider_api.get("/users").mock(side_effect=users_page)
        provider_api.get("/classes").mock(
            return_value=paged([{"id": "cls_1", "external_id": "A", "name": "Turma A"}])
        )
        provider_api.get("/classes/cls_1/members").mock(
            return_value=paged([{"user_id": "usr_1"}, {"user_id": "usr_2"}])
        )

        snapshot = await adapter.snapshot()

        assert [u.provider_id for u in snapshot.users] == ["usr_1", "usr_2", "usr_3", "usr_9"]
        assert snapshot.users[3].external_id is None
        assert [c.external_id for c in snapshot.classes] == ["A"]
        assert snapshot.memberships == [("cls_1", "usr_1"), ("cls_1", "usr_2")]

    async def test_pagina_com_falha_transitoria_e_repetida(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        users = provider_api.get("/users").mock(
            side_effect=[
                error("INJECTED_FAILURE", 500),
                httpx.Response(200, json={"items": [], "total": 0}),
            ]
        )
        provider_api.get("/classes").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        snapshot = await adapter.snapshot()

        assert snapshot.users == []
        assert users.call_count == 2

    async def test_resposta_fora_do_contrato_e_erro_permanente(
        self, adapter: MockProvedorAdapter, provider_api: respx.MockRouter
    ) -> None:
        provider_api.get("/users").mock(return_value=httpx.Response(200, json={"oops": 1}))
        provider_api.get("/classes").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        with pytest.raises(PermanentUpstreamError) as caught:
            await adapter.snapshot()

        assert caught.value.code == "INVALID_RESPONSE"
