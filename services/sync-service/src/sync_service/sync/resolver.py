from dataclasses import dataclass, field


@dataclass(slots=True)
class IdResolver:
    """Traduz ids de origem (acadêmico) em ids do provedor. Atualizado conforme criamos coisas."""

    users: dict[str, str] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)

    def user(self, source_id: str) -> str | None:
        return self.users.get(source_id)

    def school_class(self, source_id: str) -> str | None:
        return self.classes.get(source_id)
