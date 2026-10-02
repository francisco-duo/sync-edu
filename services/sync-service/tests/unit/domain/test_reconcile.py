"""Matriz de casos da reconciliação (table-driven).

Cada caso descreve estado desejado, estado atual e as chaves de ação esperadas, na ordem
exata de execução (fase e, dentro da fase, ordem alfabética da chave).
"""

import pytest

from sync_service.domain.models import (
    ActionType,
    CurrentState,
    DesiredState,
    EntityType,
    SyncAction,
)
from sync_service.domain.reconcile import reconcile
from sync_testkit.builders import current, desired, suspended

JOAO = "João Silva"
MARIA = "Maria Souza"
JOAO_EMAIL = "joao.silva@example.edu"
MARIA_EMAIL = "maria.souza@example.edu"


def keys(plan: list[SyncAction]) -> list[str]:
    return [action.key for action in plan]


def by_key(plan: list[SyncAction]) -> dict[str, SyncAction]:
    return {action.key: action for action in plan}


CASES = [
    # --- sem mudança / vazio ---------------------------------------------------------------
    pytest.param(desired(), current(), [], id="tudo-vazio"),
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current({"S1": JOAO_EMAIL}, ["A"], [("S1", "A")]),
        [],
        id="1-nenhuma-mudanca",
    ),
    pytest.param(
        desired({"S1": JOAO, "S2": MARIA}, ["A", "B"], [("S1", "A"), ("S2", "B")]),
        current({"S1": JOAO_EMAIL, "S2": MARIA_EMAIL}, ["A", "B"], [("S1", "A"), ("S2", "B")]),
        [],
        id="nenhuma-mudanca-varios-alunos-e-turmas",
    ),
    pytest.param(
        desired({"S1": JOAO}),
        current({"S1": JOAO_EMAIL}),
        [],
        id="nenhuma-mudanca-aluno-sem-turma",
    ),
    # --- aluno novo ------------------------------------------------------------------------
    pytest.param(
        desired({"S1": JOAO}),
        current(),
        ["CREATE_USER:S1"],
        id="2-aluno-novo-sem-turma",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current(classes=["A"]),
        ["CREATE_USER:S1", "ADD_TO_CLASS:S1|A"],
        id="2-aluno-novo-em-turma-existente",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current(),
        ["CREATE_CLASS:A", "CREATE_USER:S1", "ADD_TO_CLASS:S1|A"],
        id="aluno-novo-em-turma-nova",
    ),
    pytest.param(
        desired(
            {"S1": JOAO, "S2": MARIA, "S3": "Ana Lima"},
            ["A"],
            [(s, "A") for s in ("S1", "S2", "S3")],
        ),
        current(),
        [
            "CREATE_CLASS:A",
            "CREATE_USER:S1",
            "CREATE_USER:S2",
            "CREATE_USER:S3",
            "ADD_TO_CLASS:S1|A",
            "ADD_TO_CLASS:S2|A",
            "ADD_TO_CLASS:S3|A",
        ],
        id="varios-alunos-novos-na-mesma-turma",
    ),
    # --- turma nova ------------------------------------------------------------------------
    pytest.param(
        desired(classes=["A", "B"]),
        current(),
        ["CREATE_CLASS:A", "CREATE_CLASS:B"],
        id="3-turmas-novas-sem-alunos",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A", "B"], [("S1", "A")]),
        current({"S1": JOAO_EMAIL}, ["A"], [("S1", "A")]),
        ["CREATE_CLASS:B"],
        id="3-turma-nova-sem-matriculas",
    ),
    # --- matrícula nova --------------------------------------------------------------------
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current({"S1": JOAO_EMAIL}, ["A"]),
        ["ADD_TO_CLASS:S1|A"],
        id="4-matricula-nova",
    ),
    # --- troca de turma --------------------------------------------------------------------
    pytest.param(
        desired({"S1": JOAO}, ["A", "B"], [("S1", "B")]),
        current({"S1": JOAO_EMAIL}, ["A", "B"], [("S1", "A")]),
        ["REMOVE_FROM_CLASS:S1|A", "ADD_TO_CLASS:S1|B"],
        id="5-troca-de-turma",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A", "B"], [("S1", "B")]),
        current({"S1": JOAO_EMAIL}, ["A"], [("S1", "A")]),
        ["CREATE_CLASS:B", "REMOVE_FROM_CLASS:S1|A", "ADD_TO_CLASS:S1|B"],
        id="troca-para-turma-que-ainda-nao-existe",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A"], []),
        current({"S1": JOAO_EMAIL}, ["A"], [("S1", "A")]),
        ["REMOVE_FROM_CLASS:S1|A"],
        id="aluno-ativo-sem-matricula-sai-da-turma",
    ),
    # --- aluno que saiu --------------------------------------------------------------------
    pytest.param(
        desired(),
        current({"S1": JOAO_EMAIL}),
        ["SUSPEND_USER:S1"],
        id="6-aluno-saiu",
    ),
    pytest.param(
        desired(classes=["A"]),
        current({"S1": JOAO_EMAIL}, ["A"], [("S1", "A")]),
        ["REMOVE_FROM_CLASS:S1|A", "SUSPEND_USER:S1"],
        id="6-aluno-saiu-estando-em-turma",
    ),
    pytest.param(
        desired(),
        current({"S1": suspended(JOAO_EMAIL)}),
        [],
        id="aluno-que-saiu-ja-suspenso-nao-gera-acao",
    ),
    pytest.param(
        desired(classes=["A"]),
        current({"S1": suspended(JOAO_EMAIL)}, ["A"], [("S1", "A")]),
        ["REMOVE_FROM_CLASS:S1|A"],
        id="suspenso-com-turma-residual-sai-da-turma",
    ),
    pytest.param(
        desired(),
        current({"S1": JOAO_EMAIL, "S2": MARIA_EMAIL, "S3": "ana.lima@example.edu"}),
        ["SUSPEND_USER:S1", "SUSPEND_USER:S2", "SUSPEND_USER:S3"],
        id="varios-alunos-sairam",
    ),
    # --- aluno que voltou ------------------------------------------------------------------
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current({"S1": suspended(JOAO_EMAIL)}, ["A"]),
        ["REACTIVATE_USER:S1", "ADD_TO_CLASS:S1|A"],
        id="aluno-suspenso-que-voltou",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current({"S1": suspended(JOAO_EMAIL)}, ["A"], [("S1", "A")]),
        ["REACTIVATE_USER:S1"],
        id="aluno-que-voltou-ja-na-turma",
    ),
    # --- estado parcialmente divergente ----------------------------------------------------
    pytest.param(
        desired({"S1": JOAO}, ["A", "B"], [("S1", "A")]),
        current({"S1": JOAO_EMAIL}, ["B"]),
        ["CREATE_CLASS:A", "ADD_TO_CLASS:S1|A"],
        id="turma-apagada-manualmente-no-provedor",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current({"S1": JOAO_EMAIL}, ["A", "B"], [("S1", "A"), ("S1", "B")]),
        ["REMOVE_FROM_CLASS:S1|B"],
        id="membro-extra-em-turma-nao-desejada",
    ),
    pytest.param(
        desired({"S1": JOAO}, ["A"], [("S1", "A")]),
        current({"S1": JOAO_EMAIL}, ["A", "B"], [("S1", "A"), ("S1", "B")]),
        ["REMOVE_FROM_CLASS:S1|B"],
        id="aluno-em-duas-turmas-no-provedor-fica-so-na-desejada",
    ),
    # --- entidades que o sync não gerencia -------------------------------------------------
    pytest.param(
        desired(),
        current(classes=["A"], memberships=[("X", "A")]),
        [],
        id="membro-de-usuario-nao-gerenciado-e-ignorado",
    ),
    pytest.param(
        desired({"S1": JOAO}),
        current({"S1": JOAO_EMAIL}, memberships=[("S1", "Z")]),
        [],
        id="membro-de-turma-nao-gerenciada-e-ignorado",
    ),
    # --- tudo ao mesmo tempo ---------------------------------------------------------------
    pytest.param(
        desired(
            {"S1": JOAO, "S2": MARIA, "S4": "Ana Lima", "S5": "Pedro Alves"},
            ["A", "B", "C"],
            [("S1", "B"), ("S2", "C"), ("S4", "A"), ("S5", "B")],
        ),
        current(
            {
                "S1": JOAO_EMAIL,
                "S2": MARIA_EMAIL,
                "S3": "carlos.reis@example.edu",
                "S4": "ana.lima@example.edu",
            },
            ["A", "B"],
            [("S1", "A"), ("S2", "A"), ("S3", "A"), ("S4", "A")],
        ),
        [
            "CREATE_CLASS:C",
            "CREATE_USER:S5",
            "REMOVE_FROM_CLASS:S1|A",
            "REMOVE_FROM_CLASS:S2|A",
            "REMOVE_FROM_CLASS:S3|A",
            "ADD_TO_CLASS:S1|B",
            "ADD_TO_CLASS:S2|C",
            "ADD_TO_CLASS:S5|B",
            "SUSPEND_USER:S3",
        ],
        id="varias-mudancas-simultaneas",
    ),
]


@pytest.mark.parametrize(("desired_state", "current_state", "expected"), CASES)
def test_reconcile_produz_o_plano_esperado(
    desired_state: DesiredState, current_state: CurrentState, expected: list[str]
) -> None:
    assert keys(reconcile(desired_state, current_state)) == expected


# --- colisão de e-mail --------------------------------------------------------------------

COLLISION_CASES = [
    pytest.param(
        {"S1": JOAO, "S2": JOAO},
        current(),
        {"S1": "joao.silva@example.edu", "S2": "joao.silva2@example.edu"},
        id="dois-joao-silva-novos",
    ),
    pytest.param(
        {"S1": JOAO, "S2": JOAO, "S3": JOAO},
        current(),
        {
            "S1": "joao.silva@example.edu",
            "S2": "joao.silva2@example.edu",
            "S3": "joao.silva3@example.edu",
        },
        id="tres-joao-silva-novos",
    ),
    pytest.param(
        {"S2": JOAO, "S1": JOAO},
        current(),
        {"S1": "joao.silva@example.edu", "S2": "joao.silva2@example.edu"},
        id="ordem-do-dicionario-nao-decide-quem-fica-com-o-e-mail-base",
    ),
    pytest.param(
        {"S1": JOAO},
        current({"S9": JOAO_EMAIL}),
        {"S1": "joao.silva2@example.edu"},
        id="colide-com-usuario-existente-ativo",
    ),
    pytest.param(
        {"S1": JOAO},
        current({"S9": suspended(JOAO_EMAIL)}),
        {"S1": "joao.silva2@example.edu"},
        id="colide-com-usuario-suspenso-o-email-continua-reservado",
    ),
    pytest.param(
        {"S1": JOAO},
        current(reserved=[JOAO_EMAIL, "joao.silva2@example.edu"]),
        {"S1": "joao.silva3@example.edu"},
        id="colide-com-usuarios-nao-gerenciados-do-provedor",
    ),
    pytest.param(
        {"S1": JOAO},
        current(reserved=["JOAO.SILVA@EXAMPLE.EDU"]),
        {"S1": "joao.silva2@example.edu"},
        id="comparacao-sem-diferenciar-maiusculas",
    ),
    pytest.param(
        {"S1": JOAO, "S2": MARIA},
        current(reserved=[JOAO_EMAIL]),
        {"S1": "joao.silva2@example.edu", "S2": "maria.souza@example.edu"},
        id="so-quem-colide-ganha-sufixo",
    ),
    pytest.param(
        {"S1": "José Conceição", "S2": "Jose Conceicao"},
        current(),
        {"S1": "jose.conceicao@example.edu", "S2": "jose.conceicao2@example.edu"},
        id="nomes-com-e-sem-acento-colidem",
    ),
]


@pytest.mark.parametrize(("students", "current_state", "expected_emails"), COLLISION_CASES)
def test_colisao_de_email(
    students: dict[str, str], current_state: CurrentState, expected_emails: dict[str, str]
) -> None:
    plan = reconcile(desired(students), current_state)

    emails = {a.entity_id: a.payload["email"] for a in plan if a.type is ActionType.CREATE_USER}
    assert emails == expected_emails


def test_email_usa_o_dominio_configurado() -> None:
    plan = reconcile(desired({"S1": JOAO}), current(), email_domain="Escola.Test")

    assert plan[0].payload["email"] == "joao.silva@escola.test"


def test_usuario_existente_nao_tem_o_email_recalculado() -> None:
    # S1 já existe com um e-mail "fora do padrão": nenhuma ação deve mexer nele.
    plan = reconcile(desired({"S1": JOAO}), current({"S1": "outro.nome@example.edu"}))

    assert plan == []


# --- identidade, payload e dependências ---------------------------------------------------


def test_payload_e_tipos_das_acoes() -> None:
    plan = by_key(
        reconcile(
            desired({"S1": JOAO}, ["A"], [("S1", "A")]),
            current(),
        )
    )

    assert plan["CREATE_CLASS:A"].entity_type is EntityType.CLASS
    assert plan["CREATE_CLASS:A"].payload == {"class_source_id": "A", "name": "Turma A"}
    assert plan["CREATE_USER:S1"].entity_type is EntityType.USER
    assert plan["CREATE_USER:S1"].payload == {
        "student_source_id": "S1",
        "first_name": "João",
        "last_name": "Silva",
        "email": "joao.silva@example.edu",
    }
    assert plan["ADD_TO_CLASS:S1|A"].entity_type is EntityType.MEMBERSHIP
    assert plan["ADD_TO_CLASS:S1|A"].payload == {
        "student_source_id": "S1",
        "class_source_id": "A",
    }


@pytest.mark.parametrize(
    ("desired_state", "current_state", "expected_dependencies"),
    [
        pytest.param(
            desired({"S1": JOAO}, ["A"], [("S1", "A")]),
            current(),
            ("CREATE_CLASS:A", "CREATE_USER:S1"),
            id="aluno-novo-e-turma-nova",
        ),
        pytest.param(
            desired({"S1": JOAO}, ["A"], [("S1", "A")]),
            current(classes=["A"]),
            ("CREATE_USER:S1",),
            id="aluno-novo-turma-existente",
        ),
        pytest.param(
            desired({"S1": JOAO}, ["A"], [("S1", "A")]),
            current({"S1": JOAO_EMAIL}),
            ("CREATE_CLASS:A",),
            id="aluno-existente-turma-nova",
        ),
        pytest.param(
            desired({"S1": JOAO}, ["A"], [("S1", "A")]),
            current({"S1": suspended(JOAO_EMAIL)}, ["A"]),
            ("REACTIVATE_USER:S1",),
            id="aluno-que-voltou",
        ),
        pytest.param(
            desired({"S1": JOAO}, ["A"], [("S1", "A")]),
            current({"S1": JOAO_EMAIL}, ["A"]),
            (),
            id="tudo-ja-existe",
        ),
    ],
)
def test_add_to_class_depende_so_do_que_esta_no_plano(
    desired_state: DesiredState,
    current_state: CurrentState,
    expected_dependencies: tuple[str, ...],
) -> None:
    plan = by_key(reconcile(desired_state, current_state))

    assert plan["ADD_TO_CLASS:S1|A"].depends_on == expected_dependencies


def test_remove_e_suspend_nao_tem_dependencias() -> None:
    plan = reconcile(
        desired(classes=["A"]),
        current({"S1": JOAO_EMAIL}, ["A"], [("S1", "A")]),
    )

    assert all(action.depends_on == () for action in plan)
