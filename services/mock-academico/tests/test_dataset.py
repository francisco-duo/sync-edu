from collections import Counter

import pytest
from fastapi.testclient import TestClient

from mock_academico.config import Settings
from mock_academico.main import create_app
from mock_academico.store import ChangeRequestError, apply_daily_changes, generate, summary


def snapshot(store):  # noqa: ANN001, ANN201
    return (
        [(s.id, s.first_name, s.last_name, s.status) for s in store.students.values()],
        [(c.id, c.name) for c in store.classes.values()],
        [(e.student_id, e.class_id, e.status) for e in store.enrollments],
    )


def test_escala_da_demonstracao_4000_alunos_90_turmas() -> None:
    store = generate(students=4000, classes=90, seed=42)

    assert summary(store) == {
        "students_active": 4000,
        "students_inactive": 0,
        "classes": 90,
        "enrollments_active": 4000,
    }
    sizes = Counter(e.class_id for e in store.enrollments)
    assert len(sizes) == 90
    assert max(sizes.values()) - min(sizes.values()) <= 1  # distribuição uniforme


def test_turmas_seguem_o_padrao_ano_e_letra() -> None:
    store = generate(students=0, classes=90, seed=1)

    names = [c.name for c in store.classes.values()]
    assert names[0] == "1º Ano A"
    assert names[9] == "1º Ano J"
    assert names[10] == "2º Ano A"
    assert names[-1] == "9º Ano J"
    assert len(set(names)) == 90


def test_mesma_semente_gera_os_mesmos_dados_e_outra_semente_gera_outros() -> None:
    assert snapshot(generate(500, 20, seed=7)) == snapshot(generate(500, 20, seed=7))
    assert snapshot(generate(500, 20, seed=7)) != snapshot(generate(500, 20, seed=8))


def test_dataset_tem_nomes_repetidos_para_exercitar_colisao_de_email() -> None:
    store = generate(students=4000, classes=90, seed=42)

    full_names = Counter(f"{s.first_name} {s.last_name}" for s in store.students.values())
    assert any(count > 1 for count in full_names.values())


class TestAlteracoesDiarias:
    def changes(self, store, **overrides):  # noqa: ANN001, ANN201
        params = {
            "new_students": 10,
            "left_students": 5,
            "moved_students": 8,
            "new_classes": 2,
            "seed": 3,
        }
        return apply_daily_changes(store, **{**params, **overrides})

    def test_aplica_cada_tipo_de_alteracao(self) -> None:
        store = generate(students=100, classes=10, seed=1)
        before_class = {e.student_id: e.class_id for e in store.enrollments}

        self.changes(store)

        assert len(store.classes) == 12
        assert summary(store) == {
            "students_active": 105,  # 100 - 5 saídas + 10 novos
            "students_inactive": 5,
            "classes": 12,
            "enrollments_active": 105,
        }
        active_per_student = Counter(
            e.student_id for e in store.enrollments if e.status == "active"
        )
        assert set(active_per_student.values()) == {1}  # sempre uma matrícula ativa por aluno
        moved = [
            e.student_id
            for e in store.enrollments
            if e.status == "active"
            and e.student_id in before_class
            and e.class_id != before_class[e.student_id]
        ]
        assert len(moved) == 8

    def test_ids_dos_novos_alunos_continuam_a_sequencia(self) -> None:
        store = generate(students=100, classes=10, seed=1)

        self.changes(store)

        assert "STU-000101" in store.students
        assert "STU-000110" in store.students

    def test_e_deterministico(self) -> None:
        first, second = generate(100, 10, seed=1), generate(100, 10, seed=1)

        self.changes(first)
        self.changes(second)

        assert snapshot(first) == snapshot(second)

    def test_pedido_maior_que_o_estado_e_rejeitado(self) -> None:
        store = generate(students=10, classes=2, seed=1)

        with pytest.raises(ChangeRequestError):
            self.changes(store, left_students=8, moved_students=8)


class TestEndpointsAdmin:
    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(create_app(Settings(seed_students=10, seed_classes=2, seed_random=1)))

    def test_reset_com_parametros(self, client: TestClient) -> None:
        response = client.post("/_admin/reset", json={"students": 40, "classes": 5, "seed": 9})

        assert response.json() == {
            "students_active": 40,
            "students_inactive": 0,
            "classes": 5,
            "enrollments_active": 40,
        }
        assert client.get("/students").json()["total"] == 40

    def test_reset_sem_corpo_usa_o_ambiente(self, client: TestClient) -> None:
        client.post("/_admin/reset", json={"students": 40})

        client.post("/_admin/reset")

        assert client.get("/students").json()["total"] == 10

    def test_changes_e_summary(self, client: TestClient) -> None:
        client.post("/_admin/reset", json={"students": 50, "classes": 5, "seed": 1})

        response = client.post(
            "/_admin/changes",
            json={"new_students": 4, "left_students": 3, "moved_students": 2, "new_classes": 1},
        )

        assert response.status_code == 200
        assert response.json()["students_active"] == 51
        assert client.get("/_admin/summary").json() == {
            "students_active": 51,
            "students_inactive": 3,
            "classes": 6,
            "enrollments_active": 51,
        }

    def test_changes_impossivel_devolve_422(self, client: TestClient) -> None:
        response = client.post("/_admin/changes", json={"left_students": 999})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CHANGES"
