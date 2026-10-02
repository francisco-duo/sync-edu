"""Estado em memória do provedor fictício.

Idempotência (explícita): criar com um `external_id` que já existe NÃO duplica; devolve 409 com
`existing_id`, e o chamador pode adotar o recurso. Adicionar membro já existente devolve 409,
remover membro inexistente devolve 404 `MEMBERSHIP_NOT_FOUND`, e suspender/reativar repetido
devolve 200. Assim, repetir qualquer chamada é seguro.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class User:
    id: str
    external_id: str
    email: str
    first_name: str
    last_name: str
    status: str = "active"
    created_at: str = field(default_factory=_now)


@dataclass
class SchoolClass:
    id: str
    external_id: str
    name: str


class Store:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.classes: dict[str, SchoolClass] = {}
        self.members: dict[str, dict[str, str]] = {}  # class_id -> {user_id: joined_at}
        self._user_by_external: dict[str, str] = {}
        self._user_by_email: dict[str, str] = {}
        self._class_by_external: dict[str, str] = {}
        self._user_seq = count(1)
        self._class_seq = count(1)

    def user_by_external(self, external_id: str) -> User | None:
        user_id = self._user_by_external.get(external_id)
        return self.users[user_id] if user_id else None

    def user_by_email(self, email: str) -> User | None:
        user_id = self._user_by_email.get(email.lower())
        return self.users[user_id] if user_id else None

    def class_by_external(self, external_id: str) -> SchoolClass | None:
        class_id = self._class_by_external.get(external_id)
        return self.classes[class_id] if class_id else None

    def add_user(self, external_id: str, email: str, first_name: str, last_name: str) -> User:
        user = User(f"usr_{next(self._user_seq):06d}", external_id, email, first_name, last_name)
        self.users[user.id] = user
        self._user_by_external[external_id] = user.id
        self._user_by_email[email.lower()] = user.id
        return user

    def add_class(self, external_id: str, name: str) -> SchoolClass:
        school_class = SchoolClass(f"cls_{next(self._class_seq):06d}", external_id, name)
        self.classes[school_class.id] = school_class
        self._class_by_external[external_id] = school_class.id
        self.members[school_class.id] = {}
        return school_class
