"""Política explícita de classificação de erros: transitório (vale tentar de novo) ou permanente.

É a única fonte da verdade para "isto merece retry?". Fica separada do mecanismo de retry
para poder ser testada em tabela, sem rede e sem relógio.
"""

from enum import StrEnum

import httpx


class ErrorKind(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"


# Falhas que costumam passar sozinhas: limite de taxa, timeout do servidor e indisponibilidade.
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

# Falhas de rede/transporte que costumam passar sozinhas.
TRANSIENT_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)


def classify_status(status_code: int) -> ErrorKind:
    """400, 401, 403, 404, 409, 422 e demais 4xx são definitivos: repetir não muda a resposta."""
    return ErrorKind.TRANSIENT if status_code in RETRYABLE_STATUS_CODES else ErrorKind.PERMANENT


def classify_transport_error(exc: BaseException) -> ErrorKind:
    return (
        ErrorKind.TRANSIENT if isinstance(exc, TRANSIENT_TRANSPORT_ERRORS) else ErrorKind.PERMANENT
    )


class UpstreamError(Exception):
    """Falha ao falar com um sistema externo (acadêmico ou provedor)."""

    def __init__(self, message: str, *, code: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class TransientUpstreamError(UpstreamError):
    """Pode funcionar se tentarmos de novo."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=status_code)
        self.retry_after = retry_after


class PermanentUpstreamError(UpstreamError):
    """Repetir não adianta: o pedido é inválido ou o estado do outro lado o impede."""


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TransientUpstreamError)


def retry_after_of(exc: BaseException) -> float | None:
    return exc.retry_after if isinstance(exc, TransientUpstreamError) else None


def parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` em segundos. Datas HTTP e valores inválidos são ignorados."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def error_from_response(response: httpx.Response) -> UpstreamError:
    """Converte uma resposta HTTP de erro na exceção certa (transitória ou permanente)."""
    code, detail = f"HTTP_{response.status_code}", ""
    try:
        error = response.json()["error"]
        code, detail = str(error["code"]), str(error.get("message", ""))
    except (ValueError, KeyError, TypeError):
        pass
    request = response.request
    message = f"{request.method} {request.url.path} -> {response.status_code} {code}"
    if detail:
        message = f"{message}: {detail}"

    if classify_status(response.status_code) is ErrorKind.TRANSIENT:
        return TransientUpstreamError(
            message,
            code=code,
            status_code=response.status_code,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )
    return PermanentUpstreamError(message, code=code, status_code=response.status_code)


def error_from_transport(exc: httpx.TransportError, *, method: str, path: str) -> UpstreamError:
    message = f"{method} {path} -> {type(exc).__name__}: {exc}"
    code = type(exc).__name__.upper()
    if classify_transport_error(exc) is ErrorKind.TRANSIENT:
        return TransientUpstreamError(message, code=code)
    return PermanentUpstreamError(message, code=code)
