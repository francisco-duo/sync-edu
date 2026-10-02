"""Construtores compactos de estados para deixar as tabelas de teste legíveis."""

from collections.abc import Iterable, Mapping

from sync_service.domain.models import (
    CurrentState,
    DesiredState,
    Enrollment,
    ProviderClass,
    ProviderUser,
    SchoolClass,
    Student,
    UserStatus,
)

UserSpec = str | tuple[str, UserStatus]


def suspended(email: str) -> tuple[str, UserStatus]:
    return email, UserStatus.SUSPENDED


def student(student_id: str, full_name: str) -> Student:
    """`"Maria da Silva"` vira first_name `Maria` e last_name `da Silva`."""
    first, _, last = full_name.partition(" ")
    return Student(id=student_id, first_name=first, last_name=last)


def desired(
    students: Mapping[str, str] | None = None,
    classes: Iterable[str] = (),
    enrollments: Iterable[tuple[str, str]] = (),
) -> DesiredState:
    return DesiredState.build(
        students=[student(sid, name) for sid, name in (students or {}).items()],
        classes=[SchoolClass(id=cid, name=f"Turma {cid}") for cid in classes],
        enrollments=[Enrollment(sid, cid) for sid, cid in enrollments],
    )


def current(
    users: Mapping[str, UserSpec] | None = None,
    classes: Iterable[str] = (),
    memberships: Iterable[tuple[str, str]] = (),
    reserved: Iterable[str] = (),
) -> CurrentState:
    provider_users = []
    for sid, spec in (users or {}).items():
        email, status = (spec, UserStatus.ACTIVE) if isinstance(spec, str) else spec
        provider_users.append(ProviderUser(source_id=sid, email=email, status=status))
    return CurrentState.build(
        users=provider_users,
        classes=[ProviderClass(source_id=cid, name=f"Turma {cid}") for cid in classes],
        memberships=[Enrollment(sid, cid) for sid, cid in memberships],
        reserved_emails=reserved,
    )
