import pytest
from fastapi.testclient import TestClient

from mock_academico.config import Settings
from mock_academico.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(seed_students=10, seed_classes=3, seed_random=1)))


def test_dataset_inicial_e_deterministico() -> None:
    def names(seed: int) -> list[str]:
        client = TestClient(create_app(Settings(seed_students=8, seed_classes=2, seed_random=seed)))
        return [s["first_name"] + s["last_name"] for s in client.get("/students").json()["items"]]

    assert names(1) == names(1)
    assert names(1) != names(2)


def test_todo_aluno_ativo_tem_exatamente_uma_matricula_ativa(client: TestClient) -> None:
    students = client.get("/students", params={"status": "active"}).json()["items"]
    enrollments = client.get("/enrollments", params={"status": "active"}).json()["items"]

    per_student = [e["student_id"] for e in enrollments]
    assert sorted(per_student) == sorted(s["id"] for s in students)


def test_paginacao(client: TestClient) -> None:
    page = client.get("/students", params={"page": 2, "page_size": 4}).json()

    assert (page["page"], page["page_size"], page["total"]) == (2, 4, 10)
    assert [s["id"] for s in page["items"]] == [
        "STU-000005",
        "STU-000006",
        "STU-000007",
        "STU-000008",
    ]


def test_pagina_alem_da_ultima_vem_vazia(client: TestClient) -> None:
    page = client.get("/students", params={"page": 99}).json()

    assert page["items"] == []
    assert page["total"] == 10


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 501}, {"status": "ghost"}])
def test_parametros_invalidos_devolvem_422_no_envelope(client: TestClient, params: dict) -> None:
    response = client.get("/students", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_consulta_por_id_e_404(client: TestClient) -> None:
    assert client.get("/students/STU-000001").status_code == 200
    missing = client.get("/students/NOPE")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "STUDENT_NOT_FOUND")
    assert client.get("/classes/CLS-001").status_code == 200
    assert client.get("/classes/NOPE").json()["error"]["code"] == "CLASS_NOT_FOUND"


def test_trocar_de_turma_encerra_a_matricula_antiga(client: TestClient) -> None:
    client.patch("/_admin/students/STU-000001", json={"class_id": "CLS-003"})

    active = client.get("/enrollments", params={"status": "active"}).json()["items"]
    ended = client.get("/enrollments", params={"status": "ended"}).json()["items"]

    assert {"student_id": "STU-000001", "class_id": "CLS-003", "status": "active"} in active
    assert [e["student_id"] for e in active].count("STU-000001") == 1
    assert ended == [{"student_id": "STU-000001", "class_id": "CLS-001", "status": "ended"}]


def test_inativar_aluno_o_tira_da_lista_de_ativos_e_encerra_a_matricula(
    client: TestClient,
) -> None:
    client.patch("/_admin/students/STU-000002", json={"status": "inactive"})

    active = client.get("/students", params={"status": "active"}).json()
    enrollments = client.get("/enrollments", params={"status": "active"}).json()

    assert active["total"] == 9
    assert "STU-000002" not in [e["student_id"] for e in enrollments["items"]]


def test_substituir_o_estado_inteiro(client: TestClient) -> None:
    state = {
        "students": [{"id": "S1", "first_name": "João", "last_name": "Silva"}],
        "classes": [{"id": "A", "name": "Turma A"}],
        "enrollments": [{"student_id": "S1", "class_id": "A"}],
    }

    assert client.put("/_admin/state", json=state).json() == {"students": 1, "classes": 1}
    assert client.get("/students").json()["total"] == 1
    assert client.get("/enrollments").json()["items"][0]["class_id"] == "A"
