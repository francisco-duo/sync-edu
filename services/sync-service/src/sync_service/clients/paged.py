"""Leitura de listagens paginadas (GET com page/page_size), com retry por página."""

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping

import httpx
from pydantic import BaseModel, ValidationError

from sync_service.clients.http import send
from sync_service.clients.pagination import Page, fetch_all
from sync_service.resilience.errors import PermanentUpstreamError
from sync_service.resilience.retry import RetryPolicy, retry_async


class PagedReader:
    """Lê todas as páginas de um endpoint: 1ª sequencial, demais concorrentes e limitadas.

    Cada página tem o seu próprio retry (uma leitura é segura de repetir). Resposta fora do
    contrato é erro permanente: repetir não a corrige.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retry_policy: RetryPolicy,
        concurrency: int,
        page_size: int,
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

    async def read_all[M: BaseModel](
        self, path: str, model: type[M], filters: Mapping[str, str] | None = None
    ) -> list[M]:
        async def fetch_page(page_number: int) -> Page[M]:
            return await retry_async(
                lambda: self._get_page(path, model, filters or {}, page_number),
                self._policy,
                sleep=self._sleep,
                uniform=self._uniform,
                on_retry=self._on_retry,
            )

        return await fetch_all(fetch_page, page_size=self._page_size, concurrency=self._concurrency)

    async def _get_page[M: BaseModel](
        self, path: str, model: type[M], filters: Mapping[str, str], page_number: int
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
