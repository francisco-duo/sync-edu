"""Provedor simulado em memória: aplica um plano com as regras de um provedor real.

Serve para provar propriedades do plano (ele é executável na ordem dada, converge e não
tem ações redundantes) sem HTTP e sem banco.
"""

from dataclasses import replace

from sync_service.domain.models import (
    ActionType,
    CurrentState,
    Enrollment,
    ProviderClass,
    ProviderUser,
    SyncAction,
    UserStatus,
)


class PlanNotExecutableError(AssertionError):
    """O plano violou uma regra do provedor simulado (ordem errada ou ação redundante)."""


def apply_plan(state: CurrentState, plan: list[SyncAction]) -> CurrentState:
    users = dict(state.users)
    classes = dict(state.classes)
    memberships = set(state.memberships)
    emails = {e.lower() for e in state.reserved_emails} | {u.email.lower() for u in users.values()}
    applied_keys: set[str] = set()

    for action in plan:
        missing = [key for key in action.depends_on if key not in applied_keys]
        if missing:
            raise PlanNotExecutableError(f"{action.key} depende de {missing}, ainda não aplicadas")
        _apply(action, users, classes, memberships, emails)
        applied_keys.add(action.key)

    return CurrentState(
        users=users,
        classes=classes,
        memberships=frozenset(memberships),
        reserved_emails=frozenset(emails),
    )


def _apply(
    action: SyncAction,
    users: dict[str, ProviderUser],
    classes: dict[str, ProviderClass],
    memberships: set[Enrollment],
    emails: set[str],
) -> None:
    payload = action.payload
    match action.type:
        case ActionType.CREATE_CLASS:
            _require(payload["class_source_id"] not in classes, action, "turma já existe")
            class_id = payload["class_source_id"]
            classes[class_id] = ProviderClass(source_id=class_id, name=payload["name"])
        case ActionType.CREATE_USER:
            student_id, email = payload["student_source_id"], payload["email"].lower()
            _require(student_id not in users, action, "usuário já existe")
            _require(email not in emails, action, f"e-mail já em uso: {email}")
            users[student_id] = ProviderUser(source_id=student_id, email=email)
            emails.add(email)
        case ActionType.REACTIVATE_USER:
            user = users.get(payload["student_source_id"])
            _require(user is not None, action, "usuário não existe")
            _require(user.status is UserStatus.SUSPENDED, action, "usuário não está suspenso")
            users[user.source_id] = replace(user, status=UserStatus.ACTIVE)
        case ActionType.SUSPEND_USER:
            user = users.get(payload["student_source_id"])
            _require(user is not None, action, "usuário não existe")
            _require(user.status is UserStatus.ACTIVE, action, "usuário já está suspenso")
            users[user.source_id] = replace(user, status=UserStatus.SUSPENDED)
        case ActionType.ADD_TO_CLASS:
            member = Enrollment(payload["student_source_id"], payload["class_source_id"])
            user = users.get(member.student_id)
            _require(user is not None, action, "usuário não existe")
            _require(user.status is UserStatus.ACTIVE, action, "USER_SUSPENDED")
            _require(member.class_id in classes, action, "turma não existe")
            _require(member not in memberships, action, "já é membro")
            memberships.add(member)
        case ActionType.REMOVE_FROM_CLASS:
            member = Enrollment(payload["student_source_id"], payload["class_source_id"])
            _require(member in memberships, action, "não é membro")
            memberships.remove(member)


def _require(condition: object, action: SyncAction, reason: str) -> None:
    if not condition:
        raise PlanNotExecutableError(f"{action.key}: {reason}")
