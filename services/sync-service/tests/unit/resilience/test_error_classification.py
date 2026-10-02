"""A política "este erro merece retry?" é testável em tabela, sem rede."""

import httpx
import pytest

from sync_service.resilience.errors import (
    ErrorKind,
    PermanentUpstreamError,
    TransientUpstreamError,
    classify_status,
    classify_transport_error,
    error_from_response,
    error_from_transport,
    is_transient,
    parse_retry_after,
)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_status_transitorios_merecem_retry(status: int) -> None:
    assert classify_status(status) is ErrorKind.TRANSIENT


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 409, 410, 418, 422, 501, 505])
def test_status_definitivos_nao_merecem_retry(status: int) -> None:
    assert classify_status(status) is ErrorKind.PERMANENT


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        httpx.WriteTimeout("t"),
        httpx.PoolTimeout("t"),
        httpx.ConnectError("c"),
        httpx.ReadError("r"),
        httpx.RemoteProtocolError("p"),
    ],
)
def test_falhas_de_rede_sao_transitorias(exc: httpx.TransportError) -> None:
    assert classify_transport_error(exc) is ErrorKind.TRANSIENT


def test_erro_de_configuracao_de_transporte_e_permanente() -> None:
    assert classify_transport_error(httpx.UnsupportedProtocol("x")) is ErrorKind.PERMANENT


def _response(status: int, body: object = None, headers: dict[str, str] | None = None):
    request = httpx.Request("POST", "http://provedor/users")
    return httpx.Response(status, json=body, headers=headers, request=request)


def test_resposta_429_vira_erro_transitorio_com_retry_after() -> None:
    error = error_from_response(
        _response(
            429, {"error": {"code": "RATE_LIMITED", "message": "calma"}}, {"Retry-After": "3"}
        )
    )

    assert isinstance(error, TransientUpstreamError)
    assert error.retry_after == 3.0
    assert error.code == "RATE_LIMITED"
    assert error.status_code == 429
    assert "POST /users -> 429 RATE_LIMITED" in error.message


def test_resposta_400_vira_erro_permanente() -> None:
    error = error_from_response(_response(400, {"error": {"code": "VALIDATION_ERROR"}}))

    assert isinstance(error, PermanentUpstreamError)
    assert error.code == "VALIDATION_ERROR"
    assert not is_transient(error)


def test_corpo_que_nao_e_json_usa_codigo_generico() -> None:
    request = httpx.Request("GET", "http://x/y")
    error = error_from_response(
        httpx.Response(502, text="<html>bad gateway</html>", request=request)
    )

    assert isinstance(error, TransientUpstreamError)
    assert error.code == "HTTP_502"


def test_excecao_de_transporte_vira_erro_transitorio() -> None:
    error = error_from_transport(httpx.ConnectTimeout("lento"), method="GET", path="/users")

    assert isinstance(error, TransientUpstreamError)
    assert error.code == "CONNECTTIMEOUT"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("3", 3.0),
        ("0", 0.0),
        ("1.5", 1.5),
        (None, None),
        ("abc", None),
        ("-2", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected
