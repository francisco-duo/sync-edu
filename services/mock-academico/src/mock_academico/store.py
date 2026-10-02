"""Estado em memória do sistema acadêmico fictício (a fonte da verdade) e seus geradores.

Todos os dados são sintéticos (Faker). Com a mesma semente, o resultado é sempre o mesmo.
"""

import random
import string
from dataclasses import dataclass, field

from faker import Faker

STUDENT_PREFIX = "STU-"
CLASS_PREFIX = "CLS-"


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


def _student_id(number: int) -> str:
    return f"{STUDENT_PREFIX}{number:06d}"


def _class_id(number: int) -> str:
    return f"{CLASS_PREFIX}{number:03d}"


def _make_class(number: int) -> SchoolClass:
    """Turma n: 10 turmas por ano, 'A' a 'J' (90 turmas = 9 anos x 10 turmas)."""
    year, index = divmod(number - 1, 10)
    return SchoolClass(
        _class_id(number), f"{year + 1}º Ano {string.ascii_uppercase[index]}", year + 1
    )


def _faker(seed: int) -> Faker:
    fake = Faker("pt_BR")
    fake.seed_instance(seed)
    return fake


def generate(students: int, classes: int, seed: int) -> Store:
    """Dataset determinístico: cada aluno ativo com uma matrícula ativa (distribuição uniforme)."""
    fake = _faker(seed)
    store = Store()
    for number in range(1, classes + 1):
        school_class = _make_class(number)
        store.classes[school_class.id] = school_class
    class_ids = sorted(store.classes)
    for number in range(1, students + 1):
        student = Student(_student_id(number), fake.first_name(), fake.last_name())
        store.students[student.id] = student
        if class_ids:
            store.enrollments.append(
                Enrollment(student.id, class_ids[(number - 1) % len(class_ids)])
            )
    return store


class ChangeRequestError(ValueError):
    """O pedido de alterações não cabe no estado atual (ex.: mais saídas do que alunos)."""


def apply_daily_changes(
    store: Store,
    *,
    new_students: int,
    left_students: int,
    moved_students: int,
    new_classes: int,
    seed: int,
) -> dict[str, int]:
    """Simula um dia de alterações: novas turmas, saídas, trocas de turma e novos alunos."""
    rng = random.Random(seed)
    fake = _faker(seed)
    active = sorted(s.id for s in store.students.values() if s.status == "active")
    if left_students + moved_students > len(active):
        raise ChangeRequestError("saídas + trocas excedem a quantidade de alunos ativos")

    first_new_class = len(store.classes) + 1
    for number in range(first_new_class, first_new_class + new_classes):
        school_class = _make_class(number)
        store.classes[school_class.id] = school_class
    class_ids = sorted(store.classes)

    chosen = rng.sample(active, left_students + moved_students)
    leavers, movers = chosen[:left_students], chosen[left_students:]

    for student_id in leavers:
        store.students[student_id].status = "inactive"
        _end_active_enrollment(store, student_id)

    for student_id in movers:
        current = store.active_enrollment(student_id)
        options = [c for c in class_ids if current is None or c != current.class_id]
        _end_active_enrollment(store, student_id)
        store.enrollments.append(Enrollment(student_id, rng.choice(options)))

    next_number = 1 + max((int(s.removeprefix(STUDENT_PREFIX)) for s in store.students), default=0)
    for number in range(next_number, next_number + new_students):
        student = Student(_student_id(number), fake.first_name(), fake.last_name())
        store.students[student.id] = student
        store.enrollments.append(Enrollment(student.id, rng.choice(class_ids)))

    return {
        "new_students": new_students,
        "left_students": left_students,
        "moved_students": moved_students,
        "new_classes": new_classes,
    }


def _end_active_enrollment(store: Store, student_id: str) -> None:
    for enrollment in store.enrollments:
        if enrollment.student_id == student_id and enrollment.status == "active":
            enrollment.status = "ended"


def summary(store: Store) -> dict[str, int]:
    return {
        "students_active": sum(s.status == "active" for s in store.students.values()),
        "students_inactive": sum(s.status != "active" for s in store.students.values()),
        "classes": len(store.classes),
        "enrollments_active": sum(e.status == "active" for e in store.enrollments),
    }
