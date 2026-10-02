"""Modelos do domínio: tipos imutáveis, sem I/O e sem dependência de framework.

Todos os ids aqui são **ids de origem** (os do sistema acadêmico). A tradução dos ids do
provedor para ids de origem acontece fora do domínio, antes de montar o `CurrentState`.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self


class InvalidStateError(ValueError):
    """Estado malformado: a reconciliação não deve tentar adivinhar o que ele significa."""


class InvalidDesiredStateError(InvalidStateError):
    """O estado desejado (vindo do sistema acadêmico) é inconsistente."""


class InvalidCurrentStateError(InvalidStateError):
    """O estado atual (vindo do provedor) é inconsistente."""


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class ActionType(StrEnum):
    # Não existe DELETE_USER: a ausência do tipo é a garantia de que nunca apagamos usuários.
    CREATE_CLASS = "CREATE_CLASS"
    CREATE_USER = "CREATE_USER"
    REACTIVATE_USER = "REACTIVATE_USER"
    REMOVE_FROM_CLASS = "REMOVE_FROM_CLASS"
    ADD_TO_CLASS = "ADD_TO_CLASS"
    SUSPEND_USER = "SUSPEND_USER"


class EntityType(StrEnum):
    USER = "user"
    CLASS = "class"
    MEMBERSHIP = "membership"


@dataclass(frozen=True, slots=True)
class Student:
    id: str
    first_name: str
    last_name: str


@dataclass(frozen=True, slots=True)
class SchoolClass:
    id: str
    name: str


@dataclass(frozen=True, slots=True, order=True)
class Enrollment:
    """Vínculo aluno-turma. No estado desejado é a matrícula; no atual, o membro da turma."""

    student_id: str
    class_id: str


@dataclass(frozen=True, slots=True)
class DesiredState:
    """O que o sistema acadêmico diz que deve existir (só alunos e matrículas ativos)."""

    students: Mapping[str, Student]
    classes: Mapping[str, SchoolClass]
    enrollments: frozenset[Enrollment]

    def __post_init__(self) -> None:
        for enrollment in self.enrollments:
            if enrollment.student_id not in self.students:
                raise InvalidDesiredStateError(
                    f"matrícula referencia aluno inexistente: {enrollment.student_id!r}"
                )
            if enrollment.class_id not in self.classes:
                raise InvalidDesiredStateError(
                    f"matrícula referencia turma inexistente: {enrollment.class_id!r}"
                )
        per_student = Counter(enrollment.student_id for enrollment in self.enrollments)
        multiple = sorted(student_id for student_id, count in per_student.items() if count > 1)
        if multiple:
            raise InvalidDesiredStateError(f"alunos com mais de uma matrícula ativa: {multiple}")

    @classmethod
    def build(
        cls,
        students: Iterable[Student] = (),
        classes: Iterable[SchoolClass] = (),
        enrollments: Iterable[Enrollment] = (),
    ) -> Self:
        return cls(
            students=_index_by_id(students, lambda s: s.id, InvalidDesiredStateError, "aluno"),
            classes=_index_by_id(classes, lambda c: c.id, InvalidDesiredStateError, "turma"),
            enrollments=frozenset(enrollments),
        )


@dataclass(frozen=True, slots=True)
class ProviderUser:
    source_id: str
    email: str
    status: UserStatus = UserStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class ProviderClass:
    source_id: str
    name: str


@dataclass(frozen=True, slots=True)
class CurrentState:
    """O que existe hoje no provedor, só entidades gerenciadas (com `external_id`).

    `reserved_emails` deve conter todos os e-mails do provedor (inclusive de suspensos e de
    usuários não gerenciados). Os e-mails de `users` são sempre considerados reservados.
    """

    users: Mapping[str, ProviderUser]
    classes: Mapping[str, ProviderClass]
    memberships: frozenset[Enrollment]
    reserved_emails: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def build(
        cls,
        users: Iterable[ProviderUser] = (),
        classes: Iterable[ProviderClass] = (),
        memberships: Iterable[Enrollment] = (),
        reserved_emails: Iterable[str] = (),
    ) -> Self:
        return cls(
            users=_index_by_id(users, lambda u: u.source_id, InvalidCurrentStateError, "usuário"),
            classes=_index_by_id(classes, lambda c: c.source_id, InvalidCurrentStateError, "turma"),
            memberships=frozenset(memberships),
            reserved_emails=frozenset(reserved_emails),
        )


@dataclass(frozen=True, slots=True)
class SyncAction:
    """Uma operação a ser aplicada no provedor. Identidade determinística via `key`."""

    type: ActionType
    entity_type: EntityType
    entity_id: str
    payload: Mapping[str, str] = field(hash=False)
    depends_on: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.type}:{self.entity_id}"


def membership_entity_id(student_id: str, class_id: str) -> str:
    return f"{student_id}|{class_id}"


def _index_by_id[T](
    items: Iterable[T],
    get_id: Callable[[T], str],
    error: type[InvalidStateError],
    label: str,
) -> dict[str, T]:
    indexed: dict[str, T] = {}
    for item in items:
        item_id = get_id(item)
        if item_id in indexed:
            raise error(f"{label} duplicado: {item_id!r}")
        indexed[item_id] = item
    return indexed
