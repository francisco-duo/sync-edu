"""Mecanismo de retry. `sleep`, `uniform` e `monotonic` são falsos: nada espera de verdade."""

import pytest

from sync_service.resilience.errors import PermanentUpstreamError, TransientUpstreamError
from sync_service.resilience.retry import RetriesExhaustedError, RetryPolicy, retry_async


class FakeClock:
    """Relógio e sleep falsos: `sleep` registra o atraso e avança o relógio."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def transient(retry_after: float | None = None) -> TransientUpstreamError:
    return TransientUpstreamError(
        "falha", code="HTTP_503", status_code=503, retry_after=retry_after
    )


class Flaky:
    """Falha com os erros dados, na ordem, e depois devolve 'ok'."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


NO_JITTER = RetryPolicy(max_attempts=5, base_delay=0.5, factor=2.0, max_delay=30.0, jitter="none")


async def run(operation: Flaky, policy: RetryPolicy, clock: FakeClock, **kwargs: object) -> str:
    return await retry_async(
        operation,
        policy,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        uniform=kwargs.get("uniform", lambda low, high: high),  # type: ignore[arg-type]
    )


async def test_sucesso_na_primeira_tentativa_nao_espera() -> None:
    clock, operation = FakeClock(), Flaky()

    assert await run(operation, NO_JITTER, clock) == "ok"
    assert operation.calls == 1
    assert clock.sleeps == []


async def test_repete_ate_dar_certo() -> None:
    clock, operation = FakeClock(), Flaky(transient(), transient())

    assert await run(operation, NO_JITTER, clock) == "ok"
    assert operation.calls == 3
    assert clock.sleeps == [0.5, 1.0]  # backoff exponencial: 0,5 -> 1,0


async def test_backoff_exponencial_respeita_o_teto() -> None:
    policy = RetryPolicy(max_attempts=6, base_delay=1.0, factor=2.0, max_delay=5.0, jitter="none")
    clock, operation = FakeClock(), Flaky(*[transient() for _ in range(5)])

    await run(operation, policy, clock)

    assert clock.sleeps == [1.0, 2.0, 4.0, 5.0, 5.0]


async def test_esgota_o_limite_de_tentativas() -> None:
    clock, operation = FakeClock(), Flaky(*[transient() for _ in range(10)])

    with pytest.raises(RetriesExhaustedError) as caught:
        await run(operation, NO_JITTER, clock)

    assert caught.value.attempts == 5
    assert operation.calls == 5
    assert len(clock.sleeps) == 4  # não dorme depois da última tentativa
    assert isinstance(caught.value.last_error, TransientUpstreamError)


async def test_erro_permanente_nao_e_repetido() -> None:
    clock = FakeClock()
    operation = Flaky(PermanentUpstreamError("inválido", code="VALIDATION_ERROR", status_code=400))

    with pytest.raises(PermanentUpstreamError):
        await run(operation, NO_JITTER, clock)

    assert operation.calls == 1
    assert clock.sleeps == []


async def test_excecao_desconhecida_nao_e_repetida() -> None:
    clock, operation = FakeClock(), Flaky(ValueError("bug"))

    with pytest.raises(ValueError, match="bug"):
        await run(operation, NO_JITTER, clock)

    assert operation.calls == 1


async def test_full_jitter_sorteia_entre_zero_e_o_teto() -> None:
    clock, operation = FakeClock(), Flaky(transient(), transient(), transient())
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, factor=2.0, max_delay=30.0, jitter="full")
    ranges: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        ranges.append((low, high))
        return high / 2

    await retry_async(
        operation, policy, sleep=clock.sleep, monotonic=clock.monotonic, uniform=uniform
    )

    assert ranges == [(0.0, 1.0), (0.0, 2.0), (0.0, 4.0)]
    assert clock.sleeps == [0.5, 1.0, 2.0]


async def test_retry_after_do_servidor_vira_o_piso_do_atraso() -> None:
    clock, operation = FakeClock(), Flaky(transient(retry_after=3.0))

    await run(operation, NO_JITTER, clock)

    assert clock.sleeps == [3.0]  # o backoff pediria 0,5; o servidor pediu 3


async def test_retry_after_nao_encurta_o_backoff() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay=4.0, factor=2.0, max_delay=30.0, jitter="none")
    clock, operation = FakeClock(), Flaky(transient(retry_after=1.0))

    await run(operation, policy, clock)

    assert clock.sleeps == [4.0]


async def test_retry_after_absurdo_e_limitado() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay=0.5, jitter="none", max_retry_after=10.0)
    clock, operation = FakeClock(), Flaky(transient(retry_after=3600.0))

    await run(operation, policy, clock)

    assert clock.sleeps == [10.0]


async def test_orcamento_de_tempo_interrompe_antes_de_esgotar_as_tentativas() -> None:
    policy = RetryPolicy(
        max_attempts=10, base_delay=10.0, factor=1.0, max_delay=10.0, jitter="none", max_elapsed=25
    )
    clock, operation = FakeClock(), Flaky(*[transient() for _ in range(10)])

    with pytest.raises(RetriesExhaustedError) as caught:
        await run(operation, policy, clock)

    assert caught.value.attempts == 3  # 10 s + 10 s = 20 s; mais 10 s estouraria os 25 s
    assert clock.sleeps == [10.0, 10.0]


async def test_uma_tentativa_so_significa_sem_retry() -> None:
    policy = RetryPolicy(max_attempts=1, jitter="none")
    clock, operation = FakeClock(), Flaky(transient())

    with pytest.raises(RetriesExhaustedError):
        await run(operation, policy, clock)

    assert operation.calls == 1
    assert clock.sleeps == []


def test_teto_do_atraso_por_numero_do_retry() -> None:
    policy = RetryPolicy(base_delay=0.5, factor=2.0, max_delay=3.0)

    assert [policy.delay_cap(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 3.0, 3.0]


async def test_on_retry_e_chamado_uma_vez_por_nova_tentativa() -> None:
    clock, operation = FakeClock(), Flaky(transient(), transient())
    retried: list[BaseException] = []

    await retry_async(
        operation,
        NO_JITTER,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        on_retry=retried.append,
    )

    assert len(retried) == 2  # 3 tentativas = 2 retries; a 1ª tentativa não conta


async def test_on_retry_nao_conta_a_tentativa_final_que_esgota() -> None:
    clock, operation = FakeClock(), Flaky(*[transient() for _ in range(10)])
    retried: list[BaseException] = []

    with pytest.raises(RetriesExhaustedError):
        await retry_async(
            operation,
            NO_JITTER,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            on_retry=retried.append,
        )

    assert len(retried) == NO_JITTER.max_attempts - 1
