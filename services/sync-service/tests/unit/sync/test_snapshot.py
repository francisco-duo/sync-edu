import pytest

from sync_service.domain.models import Enrollment, InvalidCurrentStateError, UserStatus
from sync_service.providers.base import (
    ProviderClassRecord,
    ProviderSnapshot,
    ProviderUserRecord,
)
from sync_service.sync.snapshot import build_current_view


def user(pid: str, external: str | None, email: str, status: str = "active") -> ProviderUserRecord:
    return ProviderUserRecord(pid, external, email, status)


def test_traduz_ids_do_provedor_para_ids_de_origem() -> None:
    snapshot = ProviderSnapshot(
        users=[user("usr_1", "S1", "a@x.edu"), user("usr_2", "S2", "b@x.edu", "suspended")],
        classes=[ProviderClassRecord("cls_1", "A", "Turma A")],
        memberships=[("cls_1", "usr_1")],
    )

    view = build_current_view(snapshot)

    assert view.state.users["S2"].status is UserStatus.SUSPENDED
    assert view.state.memberships == {Enrollment("S1", "A")}
    assert view.resolver.users == {"S1": "usr_1", "S2": "usr_2"}
    assert view.resolver.classes == {"A": "cls_1"}


def test_orfaos_nao_entram_no_estado_mas_reservam_o_email() -> None:
    snapshot = ProviderSnapshot(
        users=[user("usr_1", "S1", "a@x.edu"), user("usr_9", None, "orfao@x.edu")],
        classes=[
            ProviderClassRecord("cls_1", "A", "Turma A"),
            ProviderClassRecord("cls_9", None, "Turma manual"),
        ],
        memberships=[("cls_1", "usr_9"), ("cls_9", "usr_1"), ("cls_1", "usr_1")],
    )

    view = build_current_view(snapshot)

    assert set(view.state.users) == {"S1"}
    assert set(view.state.classes) == {"A"}
    assert view.state.reserved_emails == {"a@x.edu", "orfao@x.edu"}
    # Só o vínculo entre entidades gerenciadas existe para o motor.
    assert view.state.memberships == {Enrollment("S1", "A")}


def test_membro_que_aponta_para_usuario_desconhecido_e_ignorado() -> None:
    snapshot = ProviderSnapshot(
        users=[user("usr_1", "S1", "a@x.edu")],
        classes=[ProviderClassRecord("cls_1", "A", "Turma A")],
        memberships=[("cls_1", "usr_404"), ("cls_404", "usr_1")],
    )

    assert build_current_view(snapshot).state.memberships == frozenset()


def test_external_id_duplicado_no_provedor_e_estado_invalido() -> None:
    snapshot = ProviderSnapshot(
        users=[user("usr_1", "S1", "a@x.edu"), user("usr_2", "S1", "b@x.edu")]
    )

    with pytest.raises(InvalidCurrentStateError, match="usuário duplicado"):
        build_current_view(snapshot)
