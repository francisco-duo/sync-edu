from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from mock_academico.config import Settings
from mock_academico.errors import ApiError
from mock_academico.store import Enrollment, SchoolClass, Store, Student, generate

PageNumber = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=500)]

router = APIRouter()
admin_router = APIRouter(prefix="/_admin", tags=["admin"])


def _store(request: Request) -> Store:
    return request.app.state.store


def _page(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(items),
    }


@router.get("/students", tags=["students"])
async def list_students(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = 100,
    status: Literal["active", "inactive"] | None = None,
) -> dict[str, Any]:
    students = sorted(_store(request).students.values(), key=lambda s: s.id)
    if status is not None:
        students = [s for s in students if s.status == status]
    return _page([asdict(s) for s in students], page, page_size)


@router.get("/students/{student_id}", tags=["students"])
async def get_student(request: Request, student_id: str) -> dict[str, Any]:
    student = _store(request).students.get(student_id)
    if student is None:
        raise ApiError(404, "STUDENT_NOT_FOUND", f"aluno {student_id} não existe")
    return asdict(student)


@router.get("/classes", tags=["classes"])
async def list_classes(
    request: Request, page: PageNumber = 1, page_size: PageSize = 100
) -> dict[str, Any]:
    classes = sorted(_store(request).classes.values(), key=lambda c: c.id)
    return _page([asdict(c) for c in classes], page, page_size)


@router.get("/classes/{class_id}", tags=["classes"])
async def get_class(request: Request, class_id: str) -> dict[str, Any]:
    school_class = _store(request).classes.get(class_id)
    if school_class is None:
        raise ApiError(404, "CLASS_NOT_FOUND", f"turma {class_id} não existe")
    return asdict(school_class)


@router.get("/enrollments", tags=["enrollments"])
async def list_enrollments(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = 100,
    status: Literal["active", "ended"] | None = None,
) -> dict[str, Any]:
    enrollments = sorted(_store(request).enrollments, key=lambda e: (e.student_id, e.class_id))
    if status is not None:
        enrollments = [e for e in enrollments if e.status == status]
    return _page([asdict(e) for e in enrollments], page, page_size)


# --- admin (só para testes e demos) -------------------------------------------------------


class StudentIn(BaseModel):
    id: str
    first_name: str
    last_name: str
    status: Literal["active", "inactive"] = "active"


class ClassIn(BaseModel):
    id: str
    name: str
    year: int = 1


class EnrollmentIn(BaseModel):
    student_id: str
    class_id: str
    status: Literal["active", "ended"] = "active"


class StateIn(BaseModel):
    students: list[StudentIn] = Field(default_factory=list)
    classes: list[ClassIn] = Field(default_factory=list)
    enrollments: list[EnrollmentIn] = Field(default_factory=list)


class StudentPatch(BaseModel):
    status: Literal["active", "inactive"] | None = None
    class_id: str | None = None


@admin_router.put("/state")
async def replace_state(request: Request, body: StateIn) -> dict[str, int]:
    """Substitui TODO o estado (útil em testes e demos)."""
    store = Store(
        students={s.id: Student(**s.model_dump()) for s in body.students},
        classes={c.id: SchoolClass(**c.model_dump()) for c in body.classes},
        enrollments=[Enrollment(**e.model_dump()) for e in body.enrollments],
    )
    request.app.state.store = store
    return {"students": len(store.students), "classes": len(store.classes)}


@admin_router.post("/reset")
async def reset(request: Request) -> dict[str, int]:
    settings: Settings = request.app.state.settings
    store = generate(settings.seed_students, settings.seed_classes, settings.seed_random)
    request.app.state.store = store
    return {"students": len(store.students), "classes": len(store.classes)}


@admin_router.patch("/students/{student_id}")
async def patch_student(request: Request, student_id: str, body: StudentPatch) -> dict[str, Any]:
    """Inativa o aluno e/ou troca a turma (encerra a matrícula antiga, abre a nova)."""
    store = _store(request)
    student = store.students.get(student_id)
    if student is None:
        raise ApiError(404, "STUDENT_NOT_FOUND", f"aluno {student_id} não existe")
    if body.class_id is not None:
        if body.class_id not in store.classes:
            raise ApiError(404, "CLASS_NOT_FOUND", f"turma {body.class_id} não existe")
        current = store.active_enrollment(student_id)
        if current is not None:
            current.status = "ended"
        store.enrollments.append(Enrollment(student_id, body.class_id))
    if body.status is not None:
        student.status = body.status
        if body.status == "inactive":
            for enrollment in store.enrollments:
                if enrollment.student_id == student_id and enrollment.status == "active":
                    enrollment.status = "ended"
    return asdict(student)
