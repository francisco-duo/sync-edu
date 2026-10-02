"""Chamada HTTP única que traduz falhas em exceções classificadas (sem retry aqui)."""

import httpx

from sync_service.resilience.errors import error_from_response, error_from_transport


async def send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    expected: frozenset[int],
    params: dict[str, str | int] | None = None,
    json: dict[str, str] | None = None,
) -> httpx.Response:
    """Devolve a resposta se o status estiver em `expected`; senão levanta o erro classificado."""
    try:
        response = await client.request(method, path, params=params, json=json)
    except httpx.TransportError as exc:
        raise error_from_transport(exc, method=method, path=path) from exc
    if response.status_code not in expected:
        raise error_from_response(response)
    return response
