"""Invariantes da reconciliação, verificadas em centenas de cenários aleatórios reproduzíveis.

Cada cenário vem de uma semente fixa (sem flakiness). O plano é aplicado num provedor
simulado que recusa ações fora de ordem ou redundantes, então "o plano é executável e
mínimo" é verificado de verdade, não só por inspeção das chaves.
"""

import copy
import random

import pytest

from sync_service.domain.models import (
    ActionType,
    CurrentState,
    DesiredState,
    Enrollment,
    ProviderClass,
    ProviderUser,
    SchoolClass,
    Student,
    SyncAction,
    UserStatus,
)
from sync_service.domain.reconcile import reconcile
from sync_testkit.builders import current, desired
from sync_testkit.scenarios import random_scenario
from sync_testkit.simulator import apply_plan

SEEDS = range(300)


def shuffled_copy(
    desired_state: DesiredState, current_state: CurrentState, rng: random.Random
) -> tuple[DesiredState, CurrentState]:
    """Mesmo conteúdo lógico, mas construído com as entradas em outra ordem."""

    def mix[T](items: list[T]) -> list[T]:
        items = list(items)
        rng.shuffle(items)
        return items

    new_desired = DesiredState.build(
        students=mix(list(desired_state.students.values())),
        classes=mix(list(desired_state.classes.values())),
        enrollments=mix(list(desired_state.enrollments)),
    )
    new_current = CurrentState.build(
        users=mix(list(current_state.users.values())),
        classes=mix(list(current_state.classes.values())),
        memberships=mix(list(current_state.memberships)),
        reserved_emails=mix(list(current_state.reserved_emails)),
    )
    return new_desired, new_current


@pytest.mark.parametrize("seed", SEEDS)
class TestInvariantesEmCenariosAleatorios:
    def test_aplicar_o_plano_converge_e_a_segunda_execucao_nao_gera_acoes(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)

        plan = reconcile(desired_state, current_state)
        after = apply_plan(current_state, plan)  # falha se o plano não for executável

        assert reconcile(desired_state, after) == []

    def test_e_deterministico(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)

        assert reconcile(desired_state, current_state) == reconcile(desired_state, current_state)

    def test_a_ordem_das_entradas_nao_altera_o_resultado(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)
        expected = reconcile(desired_state, current_state)

        for attempt in range(3):
            mixed_desired, mixed_current = shuffled_copy(
                desired_state, current_state, random.Random(seed * 10 + attempt)
            )
            assert reconcile(mixed_desired, mixed_current) == expected

    def test_nao_modifica_as_entradas(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)
        desired_before = copy.deepcopy(desired_state)
        current_before = copy.deepcopy(current_state)

        reconcile(desired_state, current_state)

        assert desired_state == desired_before
        assert current_state == current_before

    def test_nao_ha_acoes_duplicadas(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)

        plan = reconcile(desired_state, current_state)

        keys = [action.key for action in plan]
        assert len(keys) == len(set(keys))

    def test_dependencias_aparecem_antes_de_quem_depende(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)

        plan = reconcile(desired_state, current_state)

        position = {action.key: index for index, action in enumerate(plan)}
        for action in plan:
            for dependency in action.depends_on:
                assert position[dependency] < position[action.key]

    def test_aluno_fica_somente_na_turma_desejada(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)

        after = apply_plan(current_state, reconcile(desired_state, current_state))

        for student_id in desired_state.students:
            memberships = {m.class_id for m in after.memberships if m.student_id == student_id}
            wanted = {e.class_id for e in desired_state.enrollments if e.student_id == student_id}
            assert memberships == wanted

    def test_aluno_removido_e_suspenso_sem_turmas_e_nunca_apagado(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)

        plan = reconcile(desired_state, current_state)
        after = apply_plan(current_state, plan)

        for student_id in current_state.users:
            if student_id in desired_state.students:
                continue
            assert student_id in after.users  # continua existindo: nunca é excluído
            assert after.users[student_id].status is UserStatus.SUSPENDED
            assert not [m for m in after.memberships if m.student_id == student_id]
        assert all("DELETE" not in action.type.value for action in plan)

    def test_alunos_desejados_terminam_ativos(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)

        after = apply_plan(current_state, reconcile(desired_state, current_state))

        for student_id in desired_state.students:
            assert after.users[student_id].status is UserStatus.ACTIVE

    def test_emails_novos_sao_unicos_e_nao_colidem_com_reservados(self, seed: int) -> None:
        desired_state, current_state = random_scenario(seed)
        reserved = {e.lower() for e in current_state.reserved_emails}
        reserved |= {u.email.lower() for u in current_state.users.values()}

        plan = reconcile(desired_state, current_state)

        new_emails = [a.payload["email"] for a in plan if a.type is ActionType.CREATE_USER]
        assert len(new_emails) == len(set(new_emails))
        assert not set(new_emails) & reserved

    def test_so_gera_acoes_que_alteram_o_estado(self, seed: int) -> None:
        # O simulador rejeita ação redundante (criar o que existe, remover quem não é membro...).
        desired_state, current_state = random_scenario(seed)

        apply_plan(current_state, reconcile(desired_state, current_state))


def test_partindo_do_zero_o_estado_resultante_nao_gera_novas_acoes() -> None:
    desired_state = desired(
        {"S1": "João Silva", "S2": "João Silva", "S3": "Maria Souza"},
        ["A", "B"],
        [("S1", "A"), ("S2", "A"), ("S3", "B")],
    )

    after = apply_plan(current(), reconcile(desired_state, current()))

    assert reconcile(desired_state, after) == []


def test_o_mesmo_estado_nao_gera_acoes() -> None:
    desired_state = desired({"S1": "João Silva"}, ["A"], [("S1", "A")])
    current_state = current({"S1": "joao.silva@example.edu"}, ["A"], [("S1", "A")])

    assert reconcile(desired_state, current_state) == []


def test_ordem_das_entradas_com_estados_construidos_a_mao() -> None:
    students = [Student("S1", "João", "Silva"), Student("S2", "João", "Silva")]
    classes = [SchoolClass("A", "Turma A"), SchoolClass("B", "Turma B")]
    enrollments = [Enrollment("S1", "A"), Enrollment("S2", "B")]
    users = [ProviderUser("S3", "joao.silva@example.edu"), ProviderUser("S4", "x@example.edu")]
    provider_classes = [ProviderClass("A", "Turma A"), ProviderClass("C", "Turma C")]
    memberships = [Enrollment("S3", "A"), Enrollment("S4", "C")]

    plans = []
    for order in (1, -1):
        plans.append(
            reconcile(
                DesiredState.build(students[::order], classes[::order], enrollments[::order]),
                CurrentState.build(users[::order], provider_classes[::order], memberships[::order]),
            )
        )

    assert plans[0] == plans[1]
    assert plans[0]  # não vale se ambos forem vazios


def test_troca_de_turma_nunca_deixa_o_aluno_nas_duas_turmas_em_nenhum_passo() -> None:
    desired_state = desired({"S1": "João Silva"}, ["A", "B"], [("S1", "B")])
    current_state = current({"S1": "joao.silva@example.edu"}, ["A", "B"], [("S1", "A")])

    plan = reconcile(desired_state, current_state)

    state = current_state
    for action in plan:
        state = apply_plan(state, [_without_dependencies(action)])
        classes_of_student = {m.class_id for m in state.memberships if m.student_id == "S1"}
        assert len(classes_of_student) <= 1


def _without_dependencies(action: SyncAction) -> SyncAction:
    # Aplica uma ação isolada: as dependências já foram verificadas em outro teste.
    return SyncAction(
        type=action.type,
        entity_type=action.entity_type,
        entity_id=action.entity_id,
        payload=action.payload,
    )
