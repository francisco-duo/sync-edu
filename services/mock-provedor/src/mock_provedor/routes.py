from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from mock_provedor.chaos import ChaosState, Stats
from mock_provedor.errors import ApiError
from mock_provedor.store import SchoolClass, Store, User

PageNumber = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=500)]

users_router = APIRouter(tags=["users"])
classes_router = APIRouter(tags=["classes"])
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


def _user_out(user: User) -> dict[str, Any]:
    return asdict(user)


def _class_out(school_class: SchoolClass) -> dict[str, Any]:
    return asdict(school_class)


def _get_user(store: Store, user_id: str) -> User:
    user = store.users.get(user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", f"usuário {user_id} não existe")
    return user


def _get_class(store: Store, class_id: str) -> SchoolClass:
    school_class = store.classes.get(class_id)
    if school_class is None:
        raise ApiError(404, "CLASS_NOT_FOUND", f"turma {class_id} não existe")
    return school_class


# --- usuários -----------------------------------------------------------------------------


class CreateUser(BaseModel):
    external_id: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)


@users_router.post("/users", status_code=201)
async def create_user(request: Request, body: CreateUser) -> dict[str, Any]:
    store = _store(request)
    # O external_id é checado ANTES do e-mail: é o que permite ao cliente reconhecer um
    # "já criei este usuário" (resposta perdida) e diferenciá-lo de uma colisão real de e-mail.
    existing = store.user_by_external(body.external_id)
    if existing is not None:
        raise ApiError(
            409,
            "USER_ALREADY_EXISTS",
            "já existe usuário com este external_id",
            {"conflict_field": "external_id", "existing_id": existing.id},
        )
    by_email = store.user_by_email(body.email)
    if by_email is not None:
        raise ApiError(
            409,
            "USER_ALREADY_EXISTS",
            "já existe usuário com este e-mail",
            {"conflict_field": "email", "existing_id": by_email.id},
        )
    return _user_out(store.add_user(body.external_id, body.email, body.first_name, body.last_name))


@users_router.get("/users")
async def list_users(
    request: Request, page: PageNumber = 1, page_size: PageSize = 100, status: str | None = None
) -> dict[str, Any]:
    users = sorted(_store(request).users.values(), key=lambda u: u.id)
    if status is not None:
        users = [u for u in users if u.status == status]
    return _page([_user_out(u) for u in users], page, page_size)


@users_router.get("/users/{user_id}")
async def get_user(request: Request, user_id: str) -> dict[str, Any]:
    return _user_out(_get_user(_store(request), user_id))


@users_router.post("/users/{user_id}/suspend")
async def suspend_user(request: Request, user_id: str) -> dict[str, Any]:
    user = _get_user(_store(request), user_id)
    user.status = "suspended"  # idempotente: suspender quem já está suspenso devolve 200
    return _user_out(user)


@users_router.post("/users/{user_id}/reactivate")
async def reactivate_user(request: Request, user_id: str) -> dict[str, Any]:
    user = _get_user(_store(request), user_id)
    user.status = "active"
    return _user_out(user)


# --- turmas e membros ---------------------------------------------------------------------


class CreateClass(BaseModel):
    external_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class AddMember(BaseModel):
    user_id: str = Field(min_length=1)


@classes_router.post("/classes", status_code=201)
async def create_class(request: Request, body: CreateClass) -> dict[str, Any]:
    store = _store(request)
    existing = store.class_by_external(body.external_id)
    if existing is not None:
        raise ApiError(
            409,
            "CLASS_ALREADY_EXISTS",
            "já existe turma com este external_id",
            {"conflict_field": "external_id", "existing_id": existing.id},
        )
    return _class_out(store.add_class(body.external_id, body.name))


@classes_router.get("/classes")
async def list_classes(
    request: Request, page: PageNumber = 1, page_size: PageSize = 100
) -> dict[str, Any]:
    classes = sorted(_store(request).classes.values(), key=lambda c: c.id)
    return _page([_class_out(c) for c in classes], page, page_size)


@classes_router.get("/classes/{class_id}")
async def get_class(request: Request, class_id: str) -> dict[str, Any]:
    return _class_out(_get_class(_store(request), class_id))


@classes_router.get("/classes/{class_id}/members")
async def list_members(
    request: Request, class_id: str, page: PageNumber = 1, page_size: PageSize = 100
) -> dict[str, Any]:
    store = _store(request)
    _get_class(store, class_id)
    members = sorted(store.members[class_id].items())
    return _page(
        [{"user_id": uid, "joined_at": joined} for uid, joined in members], page, page_size
    )


@classes_router.post("/classes/{class_id}/members", status_code=201)
async def add_member(request: Request, class_id: str, body: AddMember) -> dict[str, Any]:
    store = _store(request)
    _get_class(store, class_id)
    user = _get_user(store, body.user_id)
    if user.status != "active":
        raise ApiError(422, "USER_SUSPENDED", "não é possível matricular um usuário suspenso")
    if user.id in store.members[class_id]:
        raise ApiError(409, "MEMBERSHIP_ALREADY_EXISTS", "o usuário já é membro da turma")
    joined = datetime.now(UTC).isoformat()
    store.members[class_id][user.id] = joined
    return {"user_id": user.id, "joined_at": joined}


@classes_router.delete("/classes/{class_id}/members/{user_id}", status_code=204)
async def remove_member(request: Request, class_id: str, user_id: str) -> Response:
    store = _store(request)
    _get_class(store, class_id)
    if user_id not in store.members[class_id]:
        raise ApiError(404, "MEMBERSHIP_NOT_FOUND", "o usuário não é membro da turma")
    del store.members[class_id][user_id]
    return Response(status_code=204)


# --- admin (só para testes e demos) -------------------------------------------------------


class ChaosConfig(BaseModel):
    error_rate: float = Field(default=0.0, ge=0, le=1)
    lose_response_rate: float = Field(default=0.0, ge=0, le=1)
    retry_after_seconds: int = Field(default=1, ge=0)
    seed: int | None = None
    fail_next: list[int] = Field(default_factory=list)
    lose_next: int = Field(default=0, ge=0)


def _chaos_out(chaos: ChaosState) -> dict[str, Any]:
    return {
        "error_rate": chaos.error_rate,
        "lose_response_rate": chaos.lose_response_rate,
        "retry_after_seconds": chaos.retry_after_seconds,
        "seed": chaos.seed,
        "fail_next": list(chaos.fail_next),
        "lose_next": chaos.lose_next,
    }


@admin_router.put("/chaos")
async def set_chaos(request: Request, body: ChaosConfig) -> dict[str, Any]:
    chaos: ChaosState = request.app.state.chaos
    chaos.configure(**body.model_dump())
    return _chaos_out(chaos)


@admin_router.get("/chaos")
async def get_chaos(request: Request) -> dict[str, Any]:
    return _chaos_out(request.app.state.chaos)


@admin_router.get("/stats")
async def get_stats(request: Request) -> dict[str, object]:
    stats: Stats = request.app.state.stats
    return stats.as_dict()


@admin_router.post("/reset")
async def reset(request: Request) -> dict[str, str]:
    request.app.state.store = Store()
    request.app.state.stats = Stats()
    request.app.state.chaos.configure(
        error_rate=0.0,
        lose_response_rate=0.0,
        retry_after_seconds=1,
        seed=None,
        fail_next=[],
        lose_next=0,
    )
    return {"status": "reset"}
