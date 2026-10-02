"""Reconciliação: compara o estado desejado com o atual e produz o plano de ações.

Função pura: sem I/O, sem relógio, sem aleatoriedade, sem estado global e sem mutar as
entradas. A mesma entrada sempre produz a mesma lista, na mesma ordem.
"""

from sync_service.domain.emails import DEFAULT_EMAIL_DOMAIN, allocate_email, build_local_part
from sync_service.domain.models import (
    ActionType,
    CurrentState,
    DesiredState,
    Enrollment,
    EntityType,
    SyncAction,
    UserStatus,
    membership_entity_id,
)

# Fases de execução. Dentro de cada fase as ações são independentes entre si.
PHASE_BY_TYPE: dict[ActionType, int] = {
    ActionType.CREATE_CLASS: 0,
    ActionType.CREATE_USER: 1,
    ActionType.REACTIVATE_USER: 1,
    ActionType.REMOVE_FROM_CLASS: 2,
    ActionType.ADD_TO_CLASS: 3,
    ActionType.SUSPEND_USER: 4,
}


def reconcile(
    desired: DesiredState,
    current: CurrentState,
    *,
    email_domain: str = DEFAULT_EMAIL_DOMAIN,
) -> list[SyncAction]:
    new_class_ids = sorted(desired.classes.keys() - current.classes.keys())
    new_student_ids = sorted(desired.students.keys() - current.users.keys())
    returning_ids = sorted(
        student_id
        for student_id in desired.students.keys() & current.users.keys()
        if current.users[student_id].status is UserStatus.SUSPENDED
    )
    leaving_ids = sorted(
        student_id
        for student_id, user in current.users.items()
        if user.status is UserStatus.ACTIVE and student_id not in desired.students
    )

    # Só interessam os membros de usuários e turmas que o sync gerencia.
    managed = frozenset(
        m
        for m in current.memberships
        if m.student_id in current.users and m.class_id in current.classes
    )
    to_remove = sorted(managed - desired.enrollments)
    to_add = sorted(desired.enrollments - managed)

    actions = [
        *_create_classes(desired, new_class_ids),
        *_create_users(desired, current, new_student_ids, email_domain),
        *_reactivate_users(returning_ids),
        *_remove_from_classes(to_remove),
        *_add_to_classes(to_add, set(new_class_ids), set(new_student_ids), set(returning_ids)),
        *_suspend_users(leaving_ids),
    ]
    return sorted(actions, key=lambda action: (PHASE_BY_TYPE[action.type], action.key))


def _create_classes(desired: DesiredState, class_ids: list[str]) -> list[SyncAction]:
    return [
        SyncAction(
            type=ActionType.CREATE_CLASS,
            entity_type=EntityType.CLASS,
            entity_id=class_id,
            payload={"class_source_id": class_id, "name": desired.classes[class_id].name},
        )
        for class_id in class_ids
    ]


def _create_users(
    desired: DesiredState,
    current: CurrentState,
    student_ids: list[str],
    email_domain: str,
) -> list[SyncAction]:
    taken = {email.lower() for email in current.reserved_emails}
    taken.update(user.email.lower() for user in current.users.values())

    actions: list[SyncAction] = []
    for student_id in student_ids:  # já ordenado: quem tem o menor id fica com o e-mail base
        student = desired.students[student_id]
        local_part = build_local_part(student.first_name, student.last_name, student_id)
        email = allocate_email(local_part, email_domain.lower(), taken)
        taken.add(email)
        actions.append(
            SyncAction(
                type=ActionType.CREATE_USER,
                entity_type=EntityType.USER,
                entity_id=student_id,
                payload={
                    "student_source_id": student_id,
                    "first_name": student.first_name,
                    "last_name": student.last_name,
                    "email": email,
                },
            )
        )
    return actions


def _reactivate_users(student_ids: list[str]) -> list[SyncAction]:
    return [
        SyncAction(
            type=ActionType.REACTIVATE_USER,
            entity_type=EntityType.USER,
            entity_id=student_id,
            payload={"student_source_id": student_id},
        )
        for student_id in student_ids
    ]


def _suspend_users(student_ids: list[str]) -> list[SyncAction]:
    return [
        SyncAction(
            type=ActionType.SUSPEND_USER,
            entity_type=EntityType.USER,
            entity_id=student_id,
            payload={"student_source_id": student_id},
        )
        for student_id in student_ids
    ]


def _remove_from_classes(enrollments: list[Enrollment]) -> list[SyncAction]:
    return [
        _membership_action(ActionType.REMOVE_FROM_CLASS, enrollment) for enrollment in enrollments
    ]


def _add_to_classes(
    enrollments: list[Enrollment],
    new_class_ids: set[str],
    new_student_ids: set[str],
    returning_ids: set[str],
) -> list[SyncAction]:
    actions: list[SyncAction] = []
    for enrollment in enrollments:
        depends_on: list[str] = []
        if enrollment.student_id in new_student_ids:
            depends_on.append(f"{ActionType.CREATE_USER}:{enrollment.student_id}")
        if enrollment.student_id in returning_ids:
            depends_on.append(f"{ActionType.REACTIVATE_USER}:{enrollment.student_id}")
        if enrollment.class_id in new_class_ids:
            depends_on.append(f"{ActionType.CREATE_CLASS}:{enrollment.class_id}")
        actions.append(
            _membership_action(ActionType.ADD_TO_CLASS, enrollment, tuple(sorted(depends_on)))
        )
    return actions


def _membership_action(
    action_type: ActionType,
    enrollment: Enrollment,
    depends_on: tuple[str, ...] = (),
) -> SyncAction:
    return SyncAction(
        type=action_type,
        entity_type=EntityType.MEMBERSHIP,
        entity_id=membership_entity_id(enrollment.student_id, enrollment.class_id),
        payload={
            "student_source_id": enrollment.student_id,
            "class_source_id": enrollment.class_id,
        },
        depends_on=depends_on,
    )
