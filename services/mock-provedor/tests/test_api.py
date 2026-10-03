import time

import pytest
from fastapi.testclient import TestClient

from mock_provedor.config import Settings
from mock_provedor.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings()))


def new_user(client: TestClient, external_id: str = "S1", email: str = "joao.silva@example.edu"):
    return client.post(
        "/users",
        json={
            "external_id": external_id,
            "email": email,
            "first_name": "João",
            "last_name": "Silva",
        },
    )


def new_class(client: TestClient, external_id: str = "A"):
    return client.post(
        "/classes", json={"external_id": external_id, "name": f"Turma {external_id}"}
    )


class TestUsuarios:
    def test_criar_e_consultar(self, client: TestClient) -> None:
        created = new_user(client)

        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "active"
        assert body["external_id"] == "S1"
        assert client.get(f"/users/{body['id']}").json() == body

    def test_409_por_external_id_informa_o_existente(self, client: TestClient) -> None:
        first = new_user(client).json()

        again = new_user(client, email="outro@example.edu")

        assert again.status_code == 409
        error = again.json()["error"]
        assert error["code"] == "USER_ALREADY_EXISTS"
        assert error["details"] == {"conflict_field": "external_id", "existing_id": first["id"]}

    def test_409_por_email_de_outra_pessoa(self, client: TestClient) -> None:
        first = new_user(client).json()

        other = new_user(client, external_id="S2")

        assert other.status_code == 409
        assert other.json()["error"]["details"] == {
            "conflict_field": "email",
            "existing_id": first["id"],
        }

    def test_external_id_tem_prioridade_sobre_email_no_conflito(self, client: TestClient) -> None:
        new_user(client)

        again = new_user(client)  # mesmo external_id E mesmo e-mail

        assert again.json()["error"]["details"]["conflict_field"] == "external_id"

    def test_email_e_comparado_sem_diferenciar_maiusculas(self, client: TestClient) -> None:
        new_user(client)

        other = new_user(client, external_id="S2", email="JOAO.SILVA@EXAMPLE.EDU")

        assert other.status_code == 409

    def test_validacao_devolve_422_no_envelope_padrao(self, client: TestClient) -> None:
        response = client.post("/users", json={"external_id": "S1", "email": "sem-arroba"})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_usuario_inexistente_devolve_404(self, client: TestClient) -> None:
        response = client.get("/users/usr_nope")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "USER_NOT_FOUND"

    def test_suspender_e_reativar_sao_idempotentes(self, client: TestClient) -> None:
        user_id = new_user(client).json()["id"]

        for _ in range(2):
            assert client.post(f"/users/{user_id}/suspend").json()["status"] == "suspended"
        for _ in range(2):
            assert client.post(f"/users/{user_id}/reactivate").json()["status"] == "active"

    def test_listagem_paginada_com_filtro_de_status(self, client: TestClient) -> None:
        ids = [new_user(client, f"S{n}", f"u{n}@example.edu").json()["id"] for n in range(1, 6)]
        client.post(f"/users/{ids[0]}/suspend")

        page = client.get("/users", params={"page": 2, "page_size": 2}).json()
        suspended = client.get("/users", params={"status": "suspended"}).json()

        assert (page["page"], page["page_size"], page["total"]) == (2, 2, 5)
        assert [u["id"] for u in page["items"]] == ids[2:4]
        assert [u["id"] for u in suspended["items"]] == [ids[0]]

    @pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 501}])
    def test_paginacao_invalida_devolve_422(self, client: TestClient, params: dict) -> None:
        assert client.get("/users", params=params).status_code == 422


class TestTurmasEMembros:
    def test_criar_turma_e_409_idempotente(self, client: TestClient) -> None:
        created = new_class(client).json()

        again = new_class(client)

        assert again.status_code == 409
        assert again.json()["error"]["details"]["existing_id"] == created["id"]

    def test_matricular_remover_e_repetir(self, client: TestClient) -> None:
        class_id = new_class(client).json()["id"]
        user_id = new_user(client).json()["id"]
        members = f"/classes/{class_id}/members"

        assert client.post(members, json={"user_id": user_id}).status_code == 201
        again = client.post(members, json={"user_id": user_id})
        assert (again.status_code, again.json()["error"]["code"]) == (
            409,
            "MEMBERSHIP_ALREADY_EXISTS",
        )
        assert [m["user_id"] for m in client.get(members).json()["items"]] == [user_id]

        assert client.delete(f"{members}/{user_id}").status_code == 204
        gone = client.delete(f"{members}/{user_id}")
        assert (gone.status_code, gone.json()["error"]["code"]) == (404, "MEMBERSHIP_NOT_FOUND")

    def test_usuario_suspenso_nao_pode_ser_matriculado(self, client: TestClient) -> None:
        class_id = new_class(client).json()["id"]
        user_id = new_user(client).json()["id"]
        client.post(f"/users/{user_id}/suspend")

        response = client.post(f"/classes/{class_id}/members", json={"user_id": user_id})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "USER_SUSPENDED"

    def test_turma_ou_usuario_inexistente_devolve_404_com_codigo_proprio(
        self, client: TestClient
    ) -> None:
        class_id = new_class(client).json()["id"]

        no_class = client.post("/classes/cls_x/members", json={"user_id": "usr_1"})
        no_user = client.post(f"/classes/{class_id}/members", json={"user_id": "usr_x"})
        remove = client.delete("/classes/cls_x/members/usr_1")

        assert no_class.json()["error"]["code"] == "CLASS_NOT_FOUND"
        assert no_user.json()["error"]["code"] == "USER_NOT_FOUND"
        assert remove.json()["error"]["code"] == "CLASS_NOT_FOUND"


class TestFalhasInjetadas:
    def configure(self, client: TestClient, **chaos: object) -> None:
        assert client.put("/_admin/chaos", json=chaos).status_code == 200

    def test_fail_next_responde_o_status_pedido_e_nao_processa(self, client: TestClient) -> None:
        self.configure(client, fail_next=[429, 503])

        first = new_user(client)
        second = new_user(client)
        third = new_user(client)

        assert (first.status_code, first.json()["error"]["code"]) == (429, "RATE_LIMITED")
        assert first.headers["Retry-After"] == "1"
        assert second.status_code == 503
        assert third.status_code == 201  # as duas anteriores não criaram nada
        assert client.get("/users").json()["total"] == 1

    def test_retry_after_e_configuravel(self, client: TestClient) -> None:
        self.configure(client, fail_next=[429], retry_after_seconds=0)

        assert new_user(client).headers["Retry-After"] == "0"

    def test_resposta_perdida_aplica_a_operacao_mas_responde_503(self, client: TestClient) -> None:
        self.configure(client, lose_next=1)

        lost = new_user(client)
        retry = new_user(client)

        assert lost.status_code == 503
        assert client.get("/users").json()["total"] == 1  # a criação FOI aplicada
        assert retry.status_code == 409  # e o reenvio é reconhecido como "já existe"
        assert retry.json()["error"]["details"]["conflict_field"] == "external_id"

    def test_taxa_de_erro_com_semente_e_reproduzivel(self, client: TestClient) -> None:
        def statuses() -> list[int]:
            self.configure(client, error_rate=0.5, seed=7)
            return [client.get("/users").status_code for _ in range(20)]

        first, second = statuses(), statuses()

        assert first == second
        assert 200 in first
        assert set(first) - {200} <= {500, 502, 503, 504}
        assert set(first) != {200}

    def test_health_e_admin_nao_sofrem_caos(self, client: TestClient) -> None:
        self.configure(client, error_rate=1.0)

        assert client.get("/health").status_code == 200
        assert client.get("/_admin/stats").status_code == 200
        assert client.get("/users").status_code >= 500

    def test_reset_limpa_estado_e_caos(self, client: TestClient) -> None:
        new_user(client)
        self.configure(client, error_rate=1.0)

        client.post("/_admin/reset")

        assert client.get("/users").json()["total"] == 0

    def test_stats_contam_chamadas_por_rota_e_status(self, client: TestClient) -> None:
        new_user(client)
        new_user(client)  # 409
        client.get("/users")

        stats = client.get("/_admin/stats").json()

        assert stats["total"] == 3
        assert stats["by_route"] == {"POST /users": 2, "GET /users": 1}
        assert stats["by_status"] == {"201": 1, "409": 1, "200": 1}


class TestLimiteDeTaxa:
    def test_acima_do_limite_responde_429_com_retry_after(self, client: TestClient) -> None:
        client.put("/_admin/chaos", json={"rate_limit_rps": 3, "retry_after_seconds": 2})

        statuses = [client.get("/users").status_code for _ in range(5)]

        assert statuses == [200, 200, 200, 429, 429]
        rejected = client.get("/users")
        assert rejected.json()["error"]["code"] == "RATE_LIMITED"
        assert rejected.headers["Retry-After"] == "2"

    def test_requisicoes_recusadas_nao_consomem_a_cota_e_a_janela_se_renova(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 100.0}
        monkeypatch.setattr("mock_provedor.chaos.time.monotonic", lambda: clock["now"])
        client.put("/_admin/chaos", json={"rate_limit_rps": 2})

        assert [client.get("/users").status_code for _ in range(4)] == [200, 200, 429, 429]
        clock["now"] += 1.01  # passou 1 s: a janela esvaziou

        assert [client.get("/users").status_code for _ in range(3)] == [200, 200, 429]

    def test_health_e_admin_nao_contam_para_o_limite(self, client: TestClient) -> None:
        client.put("/_admin/chaos", json={"rate_limit_rps": 1})

        for _ in range(5):
            assert client.get("/health").status_code == 200
            assert client.get("/_admin/stats").status_code == 200
        assert client.get("/users").status_code == 200

    def test_limite_zero_significa_sem_limite(self, client: TestClient) -> None:
        client.put("/_admin/chaos", json={"rate_limit_rps": 0})

        assert {client.get("/users").status_code for _ in range(50)} == {200}


def test_summary_conta_usuarios_turmas_e_membros(client: TestClient) -> None:
    class_id = new_class(client).json()["id"]
    first = new_user(client).json()["id"]
    second = new_user(client, "S2", "b@example.edu").json()["id"]
    client.post(f"/classes/{class_id}/members", json={"user_id": first})
    client.post(f"/users/{second}/suspend")

    assert client.get("/_admin/summary").json() == {
        "users_active": 1,
        "users_suspended": 1,
        "classes": 1,
        "memberships": 1,
    }


def test_latencia_artificial_atrasa_as_requisicoes_de_negocio_mas_nao_o_health(
    client: TestClient,
) -> None:
    client.put("/_admin/chaos", json={"latency_ms": 80})

    started = time.perf_counter()
    client.get("/users")
    business = time.perf_counter() - started
    started = time.perf_counter()
    client.get("/health")
    health = time.perf_counter() - started

    assert business >= 0.07
    assert health < business  # /health não sofre a latência artificial
