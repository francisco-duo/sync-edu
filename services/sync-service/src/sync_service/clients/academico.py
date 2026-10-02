"""Cliente do sistema acadêmico (fonte da verdade). Só leitura."""

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ValidationError

from sync_service.clients.http import send
from sync_service.clients.pagination import Page, fetch_all
from sync_service.domain.models import DesiredState, Enrollment, SchoolClass, Student
from sync_service.resilience.errors import PermanentUpstreamError
from sync_service.resilience.retry import RetryPolicy, retry_async


class _StudentDTO(BaseModel):
    id: str
    first_name: str
    last_name: str


class _ClassDTO(BaseModel):
    id: str
    name: str


class _EnrollmentDTO(BaseModel):
    student_id: str
    class_id: str


class AcademicoClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retry_policy: RetryPolicy,
        concurrency: int = 5,
        page_size: int = 500,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        on_retry: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._client = client
        self._policy = retry_policy
        self._concurrency = concurrency
        self._page_size = page_size
        self._sleep = sleep
        self._uniform = uniform
        self._on_retry = on_retry

    async def fetch_desired_state(self) -> DesiredState:
        """Estado desejado: alunos e matrículas ativos, mais todas as turmas."""
        students, classes, enrollments = await asyncio.gather(
            self._list("/students", _StudentDTO, {"status": "active"}),
            self._list("/classes", _ClassDTO, {}),
            self._list("/enrollments", _EnrollmentDTO, {"status": "active"}),
        )
        return DesiredState.build(
            students=[Student(s.id, s.first_name, s.last_name) for s in students],
            classes=[SchoolClass(c.id, c.name) for c in classes],
            enrollments=[Enrollment(e.student_id, e.class_id) for e in enrollments],
        )

    async def _list[M: BaseModel](
        self, path: str, model: type[M], filters: dict[str, str]
    ) -> list[M]:
        async def fetch_page(page_number: int) -> Page[M]:
            return await retry_async(
                lambda: self._get_page(path, model, filters, page_number),
                self._policy,
                sleep=self._sleep,
                uniform=self._uniform,
                on_retry=self._on_retry,
            )

        return await fetch_all(fetch_page, page_size=self._page_size, concurrency=self._concurrency)

    async def _get_page[M: BaseModel](
        self, path: str, model: type[M], filters: dict[str, str], page_number: int
    ) -> Page[M]:
        response = await send(
            self._client,
            "GET",
            path,
            expected=frozenset({200}),
            params={**filters, "page": page_number, "page_size": self._page_size},
        )
        try:
            body = response.json()
            return Page(
                items=[model.model_validate(item) for item in body["items"]],
                total=int(body["total"]),
            )
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            raise PermanentUpstreamError(
                f"GET {path}: resposta fora do contrato ({exc})", code="INVALID_RESPONSE"
            ) from exc
