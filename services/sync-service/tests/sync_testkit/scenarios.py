"""Gerador de cenários aleatórios, porém reproduzíveis (a semente define o cenário)."""

import random

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

STUDENT_POOL = [f"S{n:02d}" for n in range(14)]
CLASS_POOL = [f"C{n}" for n in range(6)]
# Poucos nomes de propósito: força colisões de e-mail com frequência.
FIRST_NAMES = ["João", "Maria", "Ana"]
LAST_NAMES = ["Silva", "Souza"]


def random_scenario(seed: int) -> tuple[DesiredState, CurrentState]:
    rng = random.Random(seed)

    desired_class_ids = _subset(rng, CLASS_POOL)
    desired_student_ids = _subset(rng, STUDENT_POOL)
    students = [
        Student(sid, rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)) for sid in desired_student_ids
    ]
    enrollments = [
        Enrollment(sid, rng.choice(desired_class_ids))
        for sid in desired_student_ids
        if desired_class_ids and rng.random() < 0.85
    ]
    desired = DesiredState.build(
        students=students,
        classes=[SchoolClass(cid, f"Turma {cid}") for cid in desired_class_ids],
        enrollments=enrollments,
    )

    provider_class_ids = _subset(rng, CLASS_POOL)
    provider_user_ids = _subset(rng, STUDENT_POOL)
    users = [
        ProviderUser(
            source_id=sid,
            email=_existing_email(rng, n),
            status=rng.choice([UserStatus.ACTIVE, UserStatus.ACTIVE, UserStatus.SUSPENDED]),
        )
        for n, sid in enumerate(provider_user_ids)
    ]
    memberships = [
        Enrollment(sid, cid)
        for sid in provider_user_ids
        for cid in provider_class_ids
        if rng.random() < 0.25
    ]
    orphan_emails = [rng.choice(["joao.silva", "maria.souza", "ana.silva2"]) + "@example.edu"]
    current = CurrentState.build(
        users=users,
        classes=[ProviderClass(cid, f"Turma {cid}") for cid in provider_class_ids],
        memberships=memberships,
        reserved_emails=orphan_emails,
    )
    return desired, current


def _subset(rng: random.Random, pool: list[str]) -> list[str]:
    return [item for item in pool if rng.random() < 0.6]


def _existing_email(rng: random.Random, n: int) -> str:
    # O índice garante e-mails distintos entre os usuários existentes.
    return f"{rng.choice(FIRST_NAMES)}.{rng.choice(LAST_NAMES)}{n or ''}@example.edu".lower()
