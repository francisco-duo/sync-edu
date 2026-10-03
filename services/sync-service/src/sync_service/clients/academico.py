"""Cliente do sistema acadêmico (fonte da verdade). Só leitura."""

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel

from sync_service.clients.paged import PagedReader
from sync_service.domain.models import DesiredState, Enrollment, SchoolClass, Student
from sync_service.resilience.retry import RetryPolicy


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
        self._reader = PagedReader(
            client,
            retry_policy=retry_policy,
            concurrency=concurrency,
            page_size=page_size,
            sleep=sleep,
            uniform=uniform,
            on_retry=on_retry,
        )

    async def fetch_desired_state(self) -> DesiredState:
        """Estado desejado: alunos e matrículas ativos, mais todas as turmas."""
        students, classes, enrollments = await asyncio.gather(
            self._reader.read_all("/students", _StudentDTO, {"status": "active"}),
            self._reader.read_all("/classes", _ClassDTO),
            self._reader.read_all("/enrollments", _EnrollmentDTO, {"status": "active"}),
        )
        return DesiredState.build(
            students=[Student(s.id, s.first_name, s.last_name) for s in students],
            classes=[SchoolClass(c.id, c.name) for c in classes],
            enrollments=[Enrollment(e.student_id, e.class_id) for e in enrollments],
        )
