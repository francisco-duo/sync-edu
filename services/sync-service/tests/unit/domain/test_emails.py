import pytest

from sync_service.domain.emails import MAX_LOCAL_PART_LENGTH, allocate_email, build_local_part

DOMAIN = "example.edu"


@pytest.mark.parametrize(
    ("first_name", "last_name", "expected"),
    [
        pytest.param("João", "Silva", "joao.silva", id="simples"),
        pytest.param("JOÃO", "SILVA", "joao.silva", id="maiusculas"),
        pytest.param("José", "Conceição", "jose.conceicao", id="acentos-e-cedilha"),
        pytest.param("Maria Clara", "da Silva Santos", "maria.santos", id="nomes-compostos"),
        pytest.param("  João  ", "  Silva  ", "joao.silva", id="espacos-extras"),
        pytest.param("Ana", "D'Ávila", "ana.davila", id="apostrofo"),
        pytest.param("Ana-Maria", "Souza", "anamaria.souza", id="hifen-no-nome"),
        pytest.param("Zoë", "Müller", "zoe.muller", id="trema"),
    ],
)
def test_build_local_part(first_name: str, last_name: str, expected: str) -> None:
    assert build_local_part(first_name, last_name, "S1") == expected


@pytest.mark.parametrize(
    ("first_name", "last_name"),
    [
        pytest.param("", "Silva", id="nome-vazio"),
        pytest.param("João", "", id="sobrenome-vazio"),
        pytest.param("   ", "   ", id="so-espacos"),
        pytest.param("李", "王", id="sem-caracteres-latinos"),
        pytest.param("---", "Silva", id="so-pontuacao"),
    ],
)
def test_build_local_part_usa_fallback_quando_uma_parte_fica_vazia(
    first_name: str, last_name: str
) -> None:
    assert build_local_part(first_name, last_name, "STU-000123") == "alunostu000123"


def test_build_local_part_respeita_o_limite_de_64_caracteres() -> None:
    local = build_local_part("a" * 80, "b" * 80, "S1")

    assert len(local) == MAX_LOCAL_PART_LENGTH


def test_build_local_part_e_deterministico() -> None:
    assert build_local_part("João", "Silva", "S1") == build_local_part("João", "Silva", "S1")


@pytest.mark.parametrize(
    ("taken", "expected"),
    [
        pytest.param(set(), "joao.silva@example.edu", id="livre"),
        pytest.param({"joao.silva@example.edu"}, "joao.silva2@example.edu", id="segundo"),
        pytest.param(
            {"joao.silva@example.edu", "joao.silva2@example.edu"},
            "joao.silva3@example.edu",
            id="terceiro",
        ),
        pytest.param(
            {"joao.silva@example.edu", "joao.silva3@example.edu"},
            "joao.silva2@example.edu",
            id="ocupa-o-primeiro-numero-livre",
        ),
        pytest.param({"outro.nome@example.edu"}, "joao.silva@example.edu", id="ignora-outros"),
    ],
)
def test_allocate_email_sufixo_incremental(taken: set[str], expected: str) -> None:
    assert allocate_email("joao.silva", DOMAIN, taken) == expected


def test_allocate_email_usa_o_dominio_informado() -> None:
    assert allocate_email("joao.silva", "escola.test", set()) == "joao.silva@escola.test"


def test_allocate_email_nunca_passa_do_limite_mesmo_com_sufixo() -> None:
    local = "a" * MAX_LOCAL_PART_LENGTH
    taken = {f"{local}@{DOMAIN}"}

    email = allocate_email(local, DOMAIN, taken)

    assert email not in taken
    assert len(email.split("@")[0]) == MAX_LOCAL_PART_LENGTH


def test_sequencia_de_colisoes_do_enunciado() -> None:
    taken: set[str] = set()
    allocated = []
    for _ in range(3):
        email = allocate_email("joao.silva", DOMAIN, taken)
        taken.add(email)
        allocated.append(email)

    assert allocated == [
        "joao.silva@example.edu",
        "joao.silva2@example.edu",
        "joao.silva3@example.edu",
    ]
