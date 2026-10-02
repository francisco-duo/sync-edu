"""Traduz o snapshot cru do provedor para o `CurrentState` do domínio (ids de origem)."""

from dataclasses import dataclass

from sync_service.domain.models import (
    CurrentState,
    Enrollment,
    ProviderClass,
    ProviderUser,
    UserStatus,
)
from sync_service.providers.base import ProviderSnapshot
from sync_service.sync.resolver import IdResolver


@dataclass(frozen=True, slots=True)
class CurrentView:
    state: CurrentState
    resolver: IdResolver  # ids do provedor das entidades gerenciadas, por id de origem


def build_current_view(snapshot: ProviderSnapshot) -> CurrentView:
    """Só entidades com `external_id` entram no estado (as demais são "órfãs": nunca tocadas).

    Os e-mails de TODOS os usuários, inclusive órfãos e suspensos, ficam reservados.
    """
    external_user = {u.provider_id: u.external_id for u in snapshot.users if u.external_id}
    external_class = {c.provider_id: c.external_id for c in snapshot.classes if c.external_id}

    users = [
        ProviderUser(u.external_id, u.email, UserStatus(u.status))
        for u in snapshot.users
        if u.external_id
    ]
    classes = [ProviderClass(c.external_id, c.name) for c in snapshot.classes if c.external_id]
    memberships = [
        Enrollment(external_user[user_id], external_class[class_id])
        for class_id, user_id in snapshot.memberships
        if user_id in external_user and class_id in external_class
    ]
    state = CurrentState.build(
        users=users,
        classes=classes,
        memberships=memberships,
        reserved_emails=[u.email for u in snapshot.users],
    )
    resolver = IdResolver(
        users={ext: pid for pid, ext in external_user.items()},
        classes={ext: pid for pid, ext in external_class.items()},
    )
    return CurrentView(state=state, resolver=resolver)
