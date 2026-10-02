import dataclasses

import pytest

from sync_service.domain.models import (
    ActionType,
    CurrentState,
    DesiredState,
    Enrollment,
    InvalidCurrentStateError,
    InvalidDesiredStateError,
    ProviderUser,
    SchoolClass,
    Student,
    SyncAction,
)
from sync_testkit.builders import desired


def test_matricula_com_aluno_inexistente_e_invalida() -> None:
    with pytest.raises(InvalidDesiredStateError, match="aluno inexistente"):
        desired(students={}, classes=["A"], enrollments=[("S1", "A")])


def test_matricula_com_turma_inexistente_e_invalida() -> None:
    with pytest.raises(InvalidDesiredStateError, match="turma inexistente"):
        desired(students={"S1": "João Silva"}, classes=[], enrollments=[("S1", "A")])


def test_aluno_com_duas_matriculas_ativas_e_invalido() -> None:
    with pytest.raises(InvalidDesiredStateError, match="mais de uma matrícula"):
        desired(
            students={"S1": "João Silva"},
            classes=["A", "B"],
            enrollments=[("S1", "A"), ("S1", "B")],
        )


def test_ids_duplicados_no_estado_desejado_sao_invalidos() -> None:
    with pytest.raises(InvalidDesiredStateError, match="aluno duplicado"):
        DesiredState.build(students=[Student("S1", "A", "B"), Student("S1", "C", "D")])
    with pytest.raises(InvalidDesiredStateError, match="turma duplicado"):
        DesiredState.build(classes=[SchoolClass("A", "x"), SchoolClass("A", "y")])


def test_usuarios_duplicados_no_estado_atual_sao_invalidos() -> None:
    with pytest.raises(InvalidCurrentStateError, match="usuário duplicado"):
        CurrentState.build(users=[ProviderUser("S1", "a@x.edu"), ProviderUser("S1", "b@x.edu")])


def test_estado_vazio_e_valido() -> None:
    state = DesiredState.build()

    assert not state.students
    assert not state.classes
    assert not state.enrollments


def test_modelos_sao_imutaveis() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Student("S1", "João", "Silva").first_name = "Outro"  # type: ignore[misc]


def test_chave_da_acao_identifica_tipo_e_entidade() -> None:
    action = SyncAction(
        type=ActionType.ADD_TO_CLASS,
        entity_type="membership",  # type: ignore[arg-type]
        entity_id="S1|A",
        payload={},
    )

    assert action.key == "ADD_TO_CLASS:S1|A"


def test_enrollments_sao_ordenaveis() -> None:
    assert sorted([Enrollment("S2", "A"), Enrollment("S1", "B")]) == [
        Enrollment("S1", "B"),
        Enrollment("S2", "A"),
    ]


def test_nao_existe_acao_de_exclusao_de_usuario() -> None:
    assert not [t for t in ActionType if "DELETE" in t.value]
