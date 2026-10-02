"""Estado em memória do sistema acadêmico fictício (a fonte da verdade)."""

import random
from dataclasses import dataclass, field

FIRST_NAMES = [
    "João",
    "Maria",
    "Ana",
    "Pedro",
    "Lucas",
    "Beatriz",
    "Gabriel",
    "Júlia",
    "Rafael",
    "Camila",
    "Mateus",
    "Larissa",
    "Felipe",
    "Isabela",
    "Bruno",
    "Letícia",
    "Diego",
    "Carolina",
    "Thiago",
    "Amanda",
]
LAST_NAMES = [
    "Silva",
    "Souza",
    "Oliveira",
    "Santos",
    "Lima",
    "Pereira",
    "Costa",
    "Almeida",
    "Ribeiro",
    "Carvalho",
    "Gomes",
    "Martins",
    "Araújo",
    "Barbosa",
    "Rocha",
]


@dataclass
class Student:
    id: str
    first_name: str
    last_name: str
    status: str = "active"


@dataclass
class SchoolClass:
    id: str
    name: str
    year: int


@dataclass
class Enrollment:
    student_id: str
    class_id: str
    status: str = "active"


@dataclass
class Store:
    students: dict[str, Student] = field(default_factory=dict)
    classes: dict[str, SchoolClass] = field(default_factory=dict)
    enrollments: list[Enrollment] = field(default_factory=list)

    def active_enrollment(self, student_id: str) -> Enrollment | None:
        return next(
            (e for e in self.enrollments if e.student_id == student_id and e.status == "active"),
            None,
        )


def generate(students: int, classes: int, seed: int) -> Store:
    """Dataset determinístico: o mesmo seed gera sempre os mesmos dados."""
    rng = random.Random(seed)
    store = Store()
    for n in range(1, classes + 1):
        store.classes[f"CLS-{n:03d}"] = SchoolClass(f"CLS-{n:03d}", f"Turma {n}", year=n)
    class_ids = sorted(store.classes)
    for n in range(1, students + 1):
        student = Student(f"STU-{n:06d}", rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))
        store.students[student.id] = student
        if class_ids:
            store.enrollments.append(Enrollment(student.id, class_ids[(n - 1) % len(class_ids)]))
    return store
