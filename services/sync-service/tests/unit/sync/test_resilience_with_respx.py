"""Casos de resiliência: executor + MockProvedorAdapter reais, provedor simulado por respx.

Mostra, com contagem de chamadas HTTP, QUANDO há retry (429, 5xx, timeout) e quando NÃO há
(400, 422, 404 definitivo), e o que acontece quando as tentativas se esgotam.
Nenhum teste espera de verdade: o `sleep` é falso e só registra os atrasos pedidos.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from sync_service.domain.models import ActionType
from sync_service.providers.base import Outcome
from sync_service.providers.mock_adapter import MockProvedorAdapter
from sync_service.resilience.retry import RetryPolicy
from sync_service.sync.executor import ActionExecutor, ExecutionConfig
from sync_service.sync.resolver import IdResolver
from sync_service.sync.status import ActionStatus, ErrorCode
from sync_testkit.actions import planned

BASE = "http://provedor"
POLICY = RetryPolicy(max_attempts=5, base_delay=0.5, factor=2.0, max_delay=30.0, jitter="none")


def api_error(status: int, code: str, **details: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": code.lower(), "details": details}}
    )


CREATED = httpx.Response(201, json={"id": "usr_1"})


class Env:
    def __init__(self, executor: ActionExecutor, sleeps: list[float]) -> None:
        self.executor = executor
        self.sleeps = sleeps
        self.resolver = IdResolver(users={"S1": "usr_1"}, classes={"A": "cls_1"})


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(base_url=BASE, verify=False) as client:
        adapter = MockProvedorAdapter(client, retry_policy=POLICY, sleep=fake_sleep)
        executor = ActionExecutor(adapter, ExecutionConfig(retry_policy=POLICY), sleep=fake_sleep)
        yield Env(executor, sleeps)


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


CREATE_USER = planned(
    ActionType.CREATE_USER,
    "S1",
    student_source_id="S1",
    first_name="João",
    last_name="Silva",
    email="joao.silva@example.edu",
)
SUSPEND = planned(ActionType.SUSPEND_USER, "S1", student_source_id="S1")


async def test_429_429_200_faz_dois_retries_e_conclui(env: Env, api: respx.MockRouter) -> None:
    route = api.post("/users").mock(
        side_effect=[
            api_error(429, "RATE_LIMITED"),
            api_error(429, "RATE_LIMITED"),
            CREATED,
        ]
    )

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert result.status is ActionStatus.SUCCEEDED
    assert result.outcome is Outcome.APPLIED
    assert result.attempts == 3
    assert route.call_count == 3
    assert env.sleeps == [0.5, 1.0]  # backoff exponencial entre as tentativas


async def test_429_com_retry_after_espera_pelo_menos_o_que_o_servidor_pediu(
    env: Env, api: respx.MockRouter
) -> None:
    api.post("/users").mock(
        side_effect=[
            httpx.Response(
                429,
                json={"error": {"code": "RATE_LIMITED", "message": "x"}},
                headers={"Retry-After": "7"},
            ),
            CREATED,
        ]
    )

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert result.status is ActionStatus.SUCCEEDED
    assert env.sleeps == [7.0]


async def test_500_200_faz_um_retry(env: Env, api: respx.MockRouter) -> None:
    route = api.post("/users").mock(side_effect=[api_error(500, "INJECTED_FAILURE"), CREATED])

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert result.status is ActionStatus.SUCCEEDED
    assert result.attempts == 2
    assert route.call_count == 2


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
async def test_cada_status_transitorio_e_repetido(
    env: Env, api: respx.MockRouter, status: int
) -> None:
    route = api.post("/users").mock(side_effect=[api_error(status, "TEMP"), CREATED])

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert result.status is ActionStatus.SUCCEEDED
    assert route.call_count == 2


async def test_timeout_de_rede_e_repetido(env: Env, api: respx.MockRouter) -> None:
    route = api.post("/users").mock(side_effect=[httpx.ConnectTimeout("lento"), CREATED])

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert result.status is ActionStatus.SUCCEEDED
    assert route.call_count == 2


async def test_400_nao_faz_retry(env: Env, api: respx.MockRouter) -> None:
    route = api.post("/users").mock(return_value=api_error(400, "VALIDATION_ERROR"))

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert route.call_count == 1  # NENHUM retry
    assert env.sleeps == []
    assert result.status is ActionStatus.FAILED
    assert result.attempts == 1
    assert result.error_code == "VALIDATION_ERROR"
    assert result.last_error is not None
    assert "400" in result.last_error
    assert result.attempted_at is not None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "UNAUTHENTICATED"),
        (403, "FORBIDDEN"),
        (422, "USER_SUSPENDED"),
        (404, "USER_NOT_FOUND"),
    ],
)
async def test_erros_definitivos_nao_fazem_retry(
    env: Env, api: respx.MockRouter, status: int, code: str
) -> None:
    route = api.post("/users/usr_1/suspend").mock(return_value=api_error(status, code))

    result = await env.executor.execute_action(SUSPEND, env.resolver)

    assert route.call_count == 1
    assert result.status is ActionStatus.FAILED
    assert result.error_code == code
    assert result.attempts == 1


async def test_429_para_sempre_esgota_o_limite_de_tentativas(
    env: Env, api: respx.MockRouter
) -> None:
    route = api.post("/users").mock(return_value=api_error(429, "RATE_LIMITED"))

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert route.call_count == POLICY.max_attempts == 5
    assert env.sleeps == [0.5, 1.0, 2.0, 4.0]  # não dorme depois da última tentativa
    assert result.status is ActionStatus.FAILED
    assert result.error_code == ErrorCode.RETRIES_EXHAUSTED
    assert result.attempts == 5
    assert result.last_error is not None
    assert "429" in result.last_error


async def test_resposta_perdida_e_reenvio_nao_duplicam_o_usuario(
    env: Env, api: respx.MockRouter
) -> None:
    """O provedor aplicou a criação, mas a resposta se perdeu (503).

    O cliente acha que falhou e repete. O 409 por external_id prova "isto já foi criado por
    mim": a ação conclui como ALREADY_APPLIED e adota o id existente, sem duplicar nada.
    """
    route = api.post("/users").mock(
        side_effect=[
            api_error(503, "INJECTED_FAILURE"),
            api_error(
                409, "USER_ALREADY_EXISTS", conflict_field="external_id", existing_id="usr_77"
            ),
        ]
    )

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert route.call_count == 2
    assert result.status is ActionStatus.SUCCEEDED
    assert result.outcome is Outcome.ALREADY_APPLIED
    assert result.attempts == 2
    assert result.mapping is not None
    assert (result.mapping.source_id, result.mapping.provider_id) == ("S1", "usr_77")
    assert env.resolver.users["S1"] == "usr_77"


async def test_email_de_outra_pessoa_e_erro_definitivo(env: Env, api: respx.MockRouter) -> None:
    route = api.post("/users").mock(
        return_value=api_error(
            409, "USER_ALREADY_EXISTS", conflict_field="email", existing_id="usr_5"
        )
    )

    result = await env.executor.execute_action(CREATE_USER, env.resolver)

    assert route.call_count == 1
    assert result.status is ActionStatus.FAILED
    assert result.error_code == "EMAIL_CONFLICT"


async def test_tentativas_de_execucoes_anteriores_sao_acumuladas(
    env: Env, api: respx.MockRouter
) -> None:
    api.post("/users/usr_1/suspend").mock(side_effect=[api_error(503, "X"), httpx.Response(200)])
    previously_failed = planned(ActionType.SUSPEND_USER, "S1", student_source_id="S1", attempts=5)

    result = await env.executor.execute_action(previously_failed, env.resolver)

    assert result.attempts == 7  # 5 de antes + 2 agora
